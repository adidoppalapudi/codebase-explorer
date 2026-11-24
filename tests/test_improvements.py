"""Incremental indexing, automatic refresh and opening, path handling, match evidence,
references, more languages, the command line, setup and the MCP workflow without a
repo_id."""

import asyncio
import json
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from explorer import cli, indexer, languages, storage
from explorer.cli import main
from explorer.indexer import index_repo
from explorer.repository import Repositories, default_folder, looks_like_project
from explorer.retriever import SourceError, StaleFileError, search_code


def write(root: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


@pytest.fixture
def project(tmp_path):
    return write(tmp_path / "project", {
        "pyproject.toml": "[project]\nname = 'shop'\n",
        "README.md": "# Shop\nHandles checkout and refunds.\n",
        "app/__init__.py": "",
        "app/cart.py": (
            "from app.pricing import total_price\n"
            "\n"
            "MAX_ITEMS = 50\n"
            "\n"
            "class Cart:\n"
            "    def checkout(self, items):\n"
            "        # checkout validates then charges\n"
            "        return total_price(items)\n"
        ),
        "app/pricing.py": (
            "def total_price(items):\n"
            "    \"\"\"Sum item prices.\"\"\"\n"
            "    return sum(i.price for i in items)\n"
            "\n"
            "def discount(items):\n"
            "    base = total_price(items)\n"
            "    return base * 0.9\n"
        ),
        "tests/test_cart.py": "from app.cart import Cart\n\ndef test_checkout():\n    Cart().checkout([])\n",
    })


# SQLite before 3.43 can't update the index in place, so changes rebuild it instead.
UPDATED = "incremental" if storage.CAN_UPDATE else "full"


def ranking(db, query, **kwargs):
    return [(m.qualified_name, m.path, m.start_line) for m in search_code(db, query, limit=20, **kwargs)]


# ---------------------------------------------------------------- incremental indexing


def test_refresh_updates_only_what_changed(project, tmp_path):
    db = tmp_path / "index.db"
    assert index_repo(project, db).mode == "full"
    assert index_repo(project, db).mode == "unchanged"

    write(project, {"app/pricing.py": "def total_price(items):\n    return 0\n\ndef tax_rate():\n    return 0.2\n"})
    (project / "README.md").unlink()
    write(project, {"app/refunds.py": "def refund_order(order):\n    return order\n"})
    report = index_repo(project, db)
    assert report.mode == UPDATED
    assert (report.changed, report.added, report.removed) == (
        ["app/pricing.py"], ["app/refunds.py"], ["README.md"])
    assert ranking(db, "tax_rate")[0][0] == "tax_rate"
    assert ranking(db, "refund_order")[0][0] == "refund_order"
    assert not search_code(db, "discount", kind="function")
    assert not search_code(db, "refunds", path="README")


def test_incremental_ranking_matches_a_full_rebuild(project, tmp_path):
    db, fresh = tmp_path / "incremental.db", tmp_path / "fresh.db"
    for i in range(20):
        write(project, {f"app/mod{i}.py": f"def helper_{i}(items):\n    '''Price {i} items for checkout.'''\n    return items\n"})
    index_repo(project, db)
    for i in range(0, 20, 3):
        write(project, {f"app/mod{i}.py": f"def changed_{i}(order):\n    '''Refund order {i}.'''\n    return order\n"})
    for i in (1, 4):
        (project / "app" / f"mod{i}.py").unlink()
    assert index_repo(project, db).mode == UPDATED
    index_repo(project, fresh)
    for query in ("checkout", "price items", "refund order", "helper", "changed", "total"):
        for kwargs in ({}, {"kind": "text"}, {"kind": "function"}):
            assert ranking(db, query, **kwargs) == ranking(fresh, query, **kwargs), query
    # The ranking totals were repaired, not just the order: scores are identical too.
    query = storage._fts_query("price items checkout")
    scores = []
    for path in (db, fresh):
        with closing(sqlite3.connect(path)) as conn:
            scores.append(conn.execute(
                "SELECT round(bm25(symbols_fts, 10, 3, 5, 4, 1), 9) FROM symbols_fts"
                " JOIN symbols s ON s.id = symbols_fts.rowid WHERE symbols_fts MATCH ?"
                " ORDER BY s.qualified_name", (query,)).fetchall())
    assert scores[0] == scores[1]


def test_timestamp_only_changes_keep_rows(project, tmp_path):
    db = tmp_path / "index.db"
    index_repo(project, db)
    path = project / "app" / "cart.py"
    os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 5_000_000_000))
    assert index_repo(project, db).mode == "unchanged"
    files, _ = storage.file_states(db)
    assert files["app/cart.py"][1] == path.stat().st_mtime_ns


def test_rebuilds_when_extraction_or_exclusions_change(project, tmp_path, monkeypatch):
    db = tmp_path / "index.db"
    index_repo(project, db)
    assert index_repo(project, db, ("tests/",)).reason == "exclude changed"
    monkeypatch.setattr(languages, "extractor_signature", lambda: "different")
    monkeypatch.setattr(indexer, "extractor_signature", lambda: "different")
    assert index_repo(project, db, ("tests/",)).reason == "extractor changed"


def test_old_sqlite_rebuilds_instead_of_updating(project, tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "CAN_UPDATE", False)
    db = tmp_path / "index.db"
    index_repo(project, db)
    assert index_repo(project, db).mode == "unchanged"
    write(project, {"app/new.py": "def brand_new():\n    pass\n"})
    report = index_repo(project, db)
    assert (report.mode, report.reason, report.added) == ("full", "files changed", ["app/new.py"])
    assert ranking(db, "brand_new")[0][0] == "brand_new"


def test_index_keeps_no_copy_of_the_text(project, tmp_path):
    db = tmp_path / "index.db"
    index_repo(project, db)
    with closing(sqlite3.connect(db)) as conn:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    assert not {n for n in names if n.endswith("_content")}
    assert ranking(db, "Sum item prices", kind="text")


# ---------------------------------------------------------------- opening and refreshing


def test_default_folder_and_project_detection(project, tmp_path, monkeypatch):
    monkeypatch.delenv("CODEBASE_EXPLORER_REPO", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    assert looks_like_project(project)
    bare = tmp_path / "bare"
    bare.mkdir()
    assert not looks_like_project(bare)
    assert default_folder(cwd=project) == str(project)
    assert default_folder(cwd=bare) is None
    assert default_folder("explicit", cwd=bare) == "explicit"
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    assert default_folder(cwd=bare) == str(project)
    monkeypatch.setattr(Path, "home", lambda: project)
    assert not looks_like_project(project)


def test_calls_without_repo_id_open_the_default(project, tmp_path):
    repos = Repositories(tmp_path / "cache", default=str(project))
    assert repos.search(None, "checkout")[0]["path"] == "app/cart.py"
    other = write(tmp_path / "other", {"notes.md": "elsewhereword\n"})
    rid = repos.open(str(other))["repo_id"]
    assert repos.search(None, "elsewhereword")[0]["path"] == "notes.md"
    assert repos.overview()["repo_id"] == rid
    with pytest.raises(ValueError, match="No repository is open"):
        Repositories(tmp_path / "cache").search(None, "x")


def test_refuses_home_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with pytest.raises(ValueError, match="too broad"):
        Repositories(tmp_path / "cache").open(str(tmp_path))


def test_search_sees_edits_without_reopening(project, tmp_path):
    repos = Repositories(tmp_path / "cache", default=str(project), refresh_interval=0)
    assert not repos.search(None, "shipping_cost")
    write(project, {"app/shipping.py": "def shipping_cost(order):\n    return 5\n"})
    assert repos.search(None, "shipping_cost")[0]["path"] == "app/shipping.py"
    (project / "app" / "shipping.py").unlink()
    assert not repos.search(None, "shipping_cost", kind="function")


def test_changed_and_deleted_files_on_read(project, tmp_path):
    repos = Repositories(tmp_path / "cache", default=str(project))
    repos.overview()
    write(project, {"app/pricing.py": "def total_price(items):\n    return 1\n"})
    page = repos.read(None, "app/pricing.py")
    assert page["refreshed"] and page["lines"][1] == "    return 1"
    assert "moved" in page["note"]
    (project / "app" / "pricing.py").unlink()
    with pytest.raises(SourceError, match="no longer exists|not in the index"):
        repos.read(None, "app/pricing.py")
    strict = Repositories(tmp_path / "cache", default=str(project), auto_refresh=False)
    strict.overview()  # opening always brings the index up to date
    write(project, {"app/cart.py": "changed = True\n"})
    with pytest.raises(StaleFileError):
        strict.read(None, "app/cart.py")


# ---------------------------------------------------------------- paths


def test_paths_as_people_and_models_write_them(project, tmp_path):
    repos = Repositories(tmp_path / "cache", default=str(project))
    for path in ("app\\cart.py", "./app/cart.py", ".\\app\\cart.py", str(project / "app" / "cart.py"),
                 "APP/Cart.py", '"app/cart.py"'):
        assert repos.read(None, path, 1, 1)["path"] == "app/cart.py", path
    assert repos.outline(None, "app\\cart.py")["symbols"]
    for bad in ("../outside.py", "app/../../outside.py", str(tmp_path / "elsewhere.py"), "C:/other/x.py"):
        with pytest.raises(SourceError):
            repos.read(None, bad)
    with pytest.raises(SourceError, match="Did you mean: app/cart.py"):
        repos.read(None, "cart.py")


# ---------------------------------------------------------------- evidence and references


def test_search_explains_each_match(project, tmp_path):
    repos = Repositories(tmp_path / "cache", default=str(project))
    exact = repos.search(None, "total_price")[0]
    assert (exact["match_type"], exact["strength"]) == ("exact_name", "strong")
    named = repos.search(None, "checkout cart", kind="method")[0]
    assert named["qualified_name"] == "Cart.checkout"
    assert (named["match_type"], named["strength"], named["missing_words"]) == ("name", "strong", [])
    text = repos.search(None, "refunds", kind="text")[0]
    assert text["match_type"] == "file_text" and text["snippet"][0]["line"] == 2
    weak = repos.search(None, "checkout kubernetes", kind="text")
    assert weak and all(m["strength"] == "weak" and m["missing_words"] == ["kubernetes"] for m in weak)
    report = repos.search_report(None, "Where is checkout handled?")
    assert report["query_words"] == ["checkout"]


def test_find_references(project, tmp_path):
    repos = Repositories(tmp_path / "cache", default=str(project))
    refs = repos.references(None, "total_price")
    assert [(d["path"], d["line"]) for d in refs["definitions"]] == [("app/pricing.py", 1)]
    uses = {(u["path"], u["line"]): (u["kind"], u["in"]) for u in refs["references"]}
    assert uses == {
        ("app/cart.py", 1): ("import", None),
        ("app/cart.py", 8): ("call", "Cart.checkout"),
        ("app/pricing.py", 6): ("call", "discount"),
    }
    checkout = repos.references(None, "Cart.checkout()")
    kinds = {(u["path"], u["kind"]) for u in checkout["references"]}
    assert ("app/cart.py", "comment") in kinds and ("tests/test_cart.py", "call") in kinds
    assert repos.references(None, "checkout", path="app")["total"] == 1
    assert repos.references(None, "missing_name")["total"] == 0
    with pytest.raises(ValueError):
        repos.references(None, "not a name")


# ---------------------------------------------------------------- languages


def test_python_nested_definitions_and_module_names(tmp_path):
    root = write(tmp_path / "repo", {"mod.py": (
        "import os\n"
        "LIMIT = 3\n"
        "registry = {}\n"
        "if os.name == 'nt':\n"
        "    def windows_only():\n"
        "        pass\n"
        "class Outer:\n"
        "    LEVEL = 1\n"
        "    def method(self):\n"
        "        def helper():\n"
        "            return 1\n"
        "        return helper()\n"
        "    class Inner:\n"
        "        def deep(self):\n"
        "            pass\n"
        "def build():\n"
        "    class Local:\n"
        "        def run(self):\n"
        "            pass\n"
        "    return Local\n"
    )})
    repos = Repositories(tmp_path / "cache", default=str(root))
    found = {(s["qualified_name"], s["kind"]) for s in repos.outline(None, "mod.py")["symbols"]}
    assert found == {
        ("LIMIT", "constant"), ("registry", "variable"), ("windows_only", "function"),
        ("Outer", "class"), ("Outer.method", "method"), ("Outer.method.helper", "function"),
        ("Outer.Inner", "class"), ("Outer.Inner.deep", "method"), ("build", "function"),
        ("build.Local", "class"), ("build.Local.run", "method"),
    }
    assert repos.search(None, "helper")[0]["qualified_name"] == "Outer.method.helper"
    assert repos.search(None, "LIMIT", kind="constant")[0]["start_line"] == 2


LANGUAGE_SAMPLES = {
    "go": ("main.go", ("package main\n\nconst MaxUsers = 10\n\ntype User struct {\n\tName string\n}\n\n"
           "type Store interface {\n\tGet(id int) User\n}\n\nfunc NewUser(name string) *User {\n"
           "\treturn &User{Name: name}\n}\n\nfunc (u *User) Greet() string {\n\treturn \"hi\"\n}\n"),
           {("MaxUsers", "constant"), ("User", "struct"), ("Store", "interface"),
            ("NewUser", "function"), ("User.Greet", "method")}),
    "java": ("UserService.java", ("package shop;\n\npublic class UserService {\n  public UserService() {}\n"
             "  @Override\n  public boolean authenticate(String u) { return true; }\n"
             "  interface Callback { void done(); }\n}\n\nenum Color { RED; void paint() {} }\n"),
             {("UserService", "class"), ("UserService.UserService", "method"),
              ("UserService.authenticate", "method"), ("UserService.Callback", "interface"),
              ("UserService.Callback.done", "method"), ("Color", "enum"), ("Color.paint", "method")}),
    "rust": ("lib.rs", ("const MAX: u32 = 3;\n\n#[derive(Debug)]\npub struct User {\n    name: String,\n}\n\n"
             "pub trait Greet {\n    fn greet(&self) -> String;\n}\n\nimpl User {\n"
             "    pub fn new(name: String) -> Self {\n        User { name }\n    }\n}\n\n"
             "impl Greet for User {\n    fn greet(&self) -> String {\n        self.name.clone()\n    }\n}\n\n"
             "pub fn main() {}\n"),
             {("MAX", "constant"), ("User", "struct"), ("Greet", "trait"), ("Greet.greet", "method"),
              ("User.new", "method"), ("User.greet", "method"), ("main", "function")}),
    "csharp": ("UserService.cs", ("namespace Shop.Services\n{\n    public class UserService\n    {\n"
               "        public UserService() {}\n        public bool Authenticate(string u) => true;\n"
               "        public string Name { get; set; }\n    }\n\n    public interface IStore { void Save(); }\n"
               "    public struct Point { public int X; }\n}\n"),
               {("UserService", "class"), ("UserService.UserService", "method"),
                ("UserService.Authenticate", "method"), ("UserService.Name", "property"),
                ("IStore", "interface"), ("IStore.Save", "method"), ("Point", "struct")}),
}


@pytest.mark.parametrize("language", sorted(LANGUAGE_SAMPLES))
def test_more_language_definitions(language, tmp_path):
    pytest.importorskip("tree_sitter")
    pytest.importorskip(languages.GRAMMARS[language][0])
    name, text, expected = LANGUAGE_SAMPLES[language]
    root = write(tmp_path / "repo", {name: text})
    repos = Repositories(tmp_path / "cache", default=str(root))
    outline = repos.outline(None, name)
    assert outline["coverage"] == "structure"
    assert {(s["qualified_name"], s["kind"]) for s in outline["symbols"]} == expected
    if language == "rust":
        user = next(s for s in outline["symbols"] if s["qualified_name"] == "User")
        assert user["start_line"] == 3  # includes the #[derive] attribute


def test_missing_grammar_keeps_text(tmp_path, monkeypatch):
    def unavailable(language):
        raise ImportError(f"no parser for {language}")

    monkeypatch.setattr(languages, "_parser", unavailable)
    root = write(tmp_path / "repo", {"main.go": "package main\nfunc Hidden() {}\n"})
    repos = Repositories(tmp_path / "cache", default=str(root))
    assert repos.outline(None, "main.go")["coverage"] == "text"
    assert repos.search(None, "Hidden")[0]["kind"] == "text"


# ---------------------------------------------------------------- command line


def test_cli_works_on_the_current_folder(project, tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(project)
    cache = ["--cache-dir", str(tmp_path / "cache")]
    assert main([*cache, "index"]) == 0
    assert "(rebuilt: no index)" in capsys.readouterr().out
    assert main([*cache, "index"]) == 0
    assert "no changes since the last index" in capsys.readouterr().out
    assert main([*cache, "search", "total_price"]) == 0
    out = capsys.readouterr().out
    assert "app/pricing.py:1-3  function  total_price  [strong, exact name]" in out
    assert main([*cache, "show", "app\\pricing.py", "1", "2"]) == 0
    assert capsys.readouterr().out.splitlines()[1] == "1 | def total_price(items):"
    assert main([*cache, "refs", "total_price"]) == 0
    out = capsys.readouterr().out
    assert "app/cart.py:8  call  in Cart.checkout" in out
    assert main([*cache, "outline", "app/cart.py"]) == 0
    assert "Cart.checkout" in capsys.readouterr().out
    assert main([*cache, "files", "--path", "app"]) == 0
    assert "app/cart.py" in capsys.readouterr().out
    assert main([*cache, "--json", "search", "checkout"]) == 0
    assert json.loads(capsys.readouterr().out)["matches"]
    assert main([*cache, "--repo", str(project), "overview"]) == 0
    assert "Files: 6" in capsys.readouterr().out


def test_legacy_db_commands_still_work(project, tmp_path, capsys):
    db = str(tmp_path / "legacy.db")
    assert main(["--db", db, "index", str(project)]) == 0
    assert capsys.readouterr().out.startswith("Indexed ")
    assert main(["--db", db, "search", "checkout"]) == 0
    assert "Cart.checkout" in capsys.readouterr().out
    assert main(["--db", db, "show", "app/cart.py", "5", "6"]) == 0
    assert "5 | class Cart:" in capsys.readouterr().out


def test_legacy_old_format_index_is_rebuilt(project, tmp_path, capsys):
    db = tmp_path / "old.db"
    with closing(sqlite3.connect(db)) as conn:
        conn.executescript(
            "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);"
            "CREATE TABLE files (id INTEGER PRIMARY KEY, path TEXT, sha256 TEXT);"
        )
        conn.execute("INSERT INTO meta VALUES ('root', ?)", (str(project),))
        conn.commit()
    assert main(["--db", str(db), "search", "checkout"]) == 0
    assert "Cart.checkout" in capsys.readouterr().out


def test_setup_prints_and_runs_client_commands(tmp_path, capsys, monkeypatch):
    assert main(["setup", "claude", "--print", "--server-command", "srv --flag"]) == 0
    assert capsys.readouterr().out.split() == [
        "claude", "mcp", "add", "--scope", "user", "codebase-explorer", "--", "srv", "--flag"]
    assert main(["setup", "codex", "--print", "--server-command", "srv"]) == 0
    assert capsys.readouterr().out.split() == ["codex", "mcp", "add", "codebase-explorer", "--", "srv"]

    calls = []
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(cli.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or
                        type("R", (), {"returncode": 0})())
    assert main(["setup", "claude", "--replace", "--scope", "project", "--server-command", "srv"]) == 0
    assert calls == [
        ["/bin/claude", "mcp", "remove", "codebase-explorer", "--scope", "project"],
        ["/bin/claude", "mcp", "add", "--scope", "project", "codebase-explorer", "--", "srv"],
    ]
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    assert main(["setup", "codex", "--server-command", "srv"]) == 1
    assert "was not found" in capsys.readouterr().err


def test_detected_server_command_exists():
    command, _ = cli._server_command(None)
    assert Path(command[0]).exists() or command[0] == "uvx"


# ---------------------------------------------------------------- MCP


def test_mcp_session_in_a_project_needs_no_open_call(project, tmp_path):
    async def exercise():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "explorer.mcp_server", "--cache-dir", str(tmp_path / "cache")],
            cwd=str(project),
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
        )
        async with (
            stdio_client(params) as (reader, writer),
            ClientSession(reader, writer) as session,
        ):
            await session.initialize()
            names = {t.name for t in (await session.list_tools()).tools}
            assert "find_references" in names and len(names) == 7

            async def call(name, args):
                result = await session.call_tool(name, args)
                assert not result.is_error, result
                return result.structured_content

            assert (await call("repo_overview", {}))["files"] == 6
            match = (await call("search_code", {"query": "checkout cart"}))["matches"][0]
            assert match["qualified_name"] == "Cart.checkout" and match["strength"] == "strong"
            page = await call("read_source", {"path": "app\\cart.py", "start_line": 5, "end_line": 6})
            assert page["citation"] == "app/cart.py:5-6"
            refs = await call("find_references", {"name": "total_price"})
            assert {u["kind"] for u in refs["references"]} == {"import", "call"}
            write(project, {"app/cart.py": "class Cart:\n    pass\n"})
            page = await call("read_source", {"path": "app/cart.py"})
            assert page["lines"] == ["class Cart:", "    pass"] and page["refreshed"]

    asyncio.run(exercise())
