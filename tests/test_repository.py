import asyncio
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from explorer.cli import main
from explorer.repository import Repositories
from explorer.retriever import StaleFileError, search_code


@pytest.fixture
def mixed(tmp_path):
    root = tmp_path / "mixed"
    root.mkdir()
    content = {
        "README.md": "# Shop\nA checkout application.\n",
        "package.json": json.dumps(
            {"scripts": {"start": "node server.js", "test": "vitest"}}
        ),
        "server.js": "export function checkout(cart) { return cart.total; }\nclass Client { send() { return 1; } }\n",
        "types.ts": "export interface Cart { total: number }\nexport const charge = (cart: Cart) => cart.total;\n",
        "View.tsx": "export const View = () => <main>Checkout</main>;\n",
        "main.go": 'package main\nfunc main() { println("checkout") }\n',
        "schema.sql": "CREATE TABLE checkout (id INTEGER);\n",
        "broken.py": "def incomplete(:\n    return checkout\n",
        "empty.txt": "",
    }
    for name, text in content.items():
        (root / name).write_text(text, encoding="utf-8")
    return root


def test_open_overview_search_and_navigation(mixed, tmp_path):
    repos = Repositories(tmp_path / "cache")
    overview = repos.open(str(mixed))
    rid = overview["repo_id"]
    assert overview["files"] == 9
    assert overview["languages"]["go"] == 1
    assert "main.go" in overview["entry_candidates"]
    assert any("vitest" in "\n".join(e["lines"]) for e in overview["evidence"])
    assert any(path == "broken.py" for path, _ in overview["diagnostics"])
    first = repos.files(rid, limit=2)
    second = repos.files(rid, offset=first["next_offset"], limit=2)
    assert first["files"][0] != second["files"][0]
    assert repos.search(rid, "checkout", language="go")[0]["path"] == "main.go"
    assert repos.search(rid, "CREATE TABLE", path="schema.sql")[0]["kind"] == "text"
    assert repos.outline(rid, "main.go")["coverage"] == "text"
    for hit in repos.search(rid, "checkout", limit=50):
        assert repos.read(rid, hit["path"], hit["start_line"], hit["end_line"])[
            "citation"
        ]


def test_tree_sitter_definitions(mixed, tmp_path):
    pytest.importorskip("tree_sitter")
    repos = Repositories(tmp_path / "cache")
    rid = repos.open(str(mixed))["repo_id"]
    assert {
        s["qualified_name"] for s in repos.outline(rid, "server.js")["symbols"]
    } == {"checkout", "Client", "Client.send"}
    assert repos.search(rid, "Cart", kind="interface")[0]["path"] == "types.ts"
    assert repos.search(rid, "charge", kind="function")[0]["path"] == "types.ts"
    assert repos.search(rid, "View", kind="function")[0]["path"] == "View.tsx"
    (mixed / "server.js").write_text(
        "function good() { return 1; }\nfunction bad( {", encoding="utf-8"
    )
    repos.open(str(mixed))
    assert repos.outline(rid, "server.js")["coverage"] == "partial"
    assert repos.search(rid, "good")[0]["qualified_name"] == "good"


def test_large_symbol_continuation_and_late_body(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "large.py").write_text(
        "def huge():\n" + "    pass\n" * 450 + "    return unique_tail\n",
        encoding="utf-8",
    )
    repos = Repositories(tmp_path / "cache")
    rid = repos.open(str(root))["repo_id"]
    hit = repos.search(rid, "huge")[0]
    page = repos.read(rid, hit["path"], hit["start_line"], hit["end_line"])
    lines = list(page["lines"])
    while page["next_line"]:
        page = repos.read(rid, hit["path"], page["next_line"], hit["end_line"])
        lines += page["lines"]
    assert len(lines) == 452
    tail = repos.search(rid, "unique_tail")[0]
    assert "unique_tail" in "\n".join(
        repos.read(rid, tail["path"], tail["start_line"], tail["end_line"])["lines"]
    )


def test_stale_refresh_isolation_and_invalid_ids(mixed, tmp_path):
    repos = Repositories(tmp_path / "cache")
    rid = repos.open(str(mixed))["repo_id"]
    second = tmp_path / "other"
    second.mkdir()
    (second / "README.md").write_text("differentonly", encoding="utf-8")
    other = repos.open(str(second))["repo_id"]
    assert repos.search(rid, "differentonly") == []
    assert repos.search(other, "differentonly")
    (mixed / "README.md").write_text("changed", encoding="utf-8")
    with pytest.raises(StaleFileError):
        repos.read(rid, "README.md")
    assert repos.open(str(mixed))["repo_id"] == rid
    assert repos.read(rid, "README.md")["lines"] == ["changed"]
    assert Repositories(tmp_path / "cache").overview(other)["files"] == 1
    for bad in ("../other", "x" * 64, "0" * 64):
        with pytest.raises(ValueError):
            repos.overview(bad)
    with pytest.raises(ValueError):
        repos.read(rid, "../outside.py")


def test_ignore_rules_generated_markers_and_cr_lines(tmp_path):
    root = tmp_path / "repo"
    (root / "nested").mkdir(parents=True)
    for name, text in {
        ".gitignore": "*.log\nignored/\n",
        ".explorerignore": "omit.txt\n",
        "nested/.gitignore": "*.txt\n!keep.txt\n",
        "nested/skip.txt": "forbidden",
        "nested/keep.txt": "retained",
        "omit.txt": "forbidden",
        "foo.log": "forbidden",
        "explicit.txt": "forbidden",
        "real.py": 'MARKER = "@generated do not edit"\ndef hello():\n    pass\n',
        "generated.js": "// @generated DO NOT EDIT\nfunction hidden() {}",
        ".github/workflows/test.yml": "run: pytest",
    }.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    (root / "cr.py").write_bytes(b"def carriage():\r    return 1\r")
    (root / "unicode.py").write_text("def \u03bb():\n    pass\n", encoding="utf-8")
    repos = Repositories(tmp_path / "cache")
    rid = repos.open(str(root), ["explicit.txt"])["repo_id"]
    assert not repos.search(rid, "forbidden")
    assert repos.search(rid, "retained")
    assert repos.search(rid, "hello")[0]["qualified_name"] == "hello"
    assert repos.search(rid, "\u03bb")[0]["qualified_name"] == "\u03bb"
    assert repos.read(rid, "cr.py")["lines"] == ["def carriage():", "    return 1"]
    assert not repos.search(rid, "hidden")
    assert repos.files(rid, path=".github")["total"] == 1


def test_cli_errors_and_legacy_rebuild(mixed, tmp_path, capsys):
    db = tmp_path / "old.db"
    with closing(sqlite3.connect(db)) as conn:
        conn.execute(
            "CREATE TABLE files (id INTEGER PRIMARY KEY, path TEXT, sha256 TEXT)"
        )
    assert main(["--db", str(db), "search", "user"]) == 1
    assert "Traceback" not in capsys.readouterr().err
    assert main(["--db", str(db), "index", str(mixed)]) == 0
    assert search_code(db, "checkout")
    assert main(["--db", str(tmp_path / "new" / "index.db"), "index", str(mixed)]) == 0


def test_empty_files_and_failed_refresh_preserve_index(mixed, tmp_path, monkeypatch):
    from explorer import storage

    repos = Repositories(tmp_path / "cache")
    rid = repos.open(str(mixed))["repo_id"]
    assert repos.read(rid, "empty.txt")["lines"] == []
    assert repos.outline(rid, "empty.txt")["symbols"] == []

    def fail(*args, **kwargs):
        raise RuntimeError("write failed")

    monkeypatch.setattr(storage, "write_index", fail)
    with pytest.raises(RuntimeError, match="write failed"):
        repos.open(str(mixed))
    assert repos.search(rid, "checkout")
    assert not list((tmp_path / "cache").glob(".explorer-*"))


def test_missing_parser_preserves_text(mixed, tmp_path, monkeypatch):
    from explorer import languages

    def unavailable(*args):
        raise ImportError("parser not installed")

    monkeypatch.setattr(languages, "_parser", unavailable)
    repos = Repositories(tmp_path / "cache")
    rid = repos.open(str(mixed))["repo_id"]
    assert repos.outline(rid, "server.js")["coverage"] == "text"
    assert repos.search(rid, "checkout", language="javascript")


def test_response_budgets_preserve_whole_lines(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "README.md").write_text("x" * 5000, encoding="utf-8")
    (root / "package.json").write_text("x" * 25000, encoding="utf-8")
    (root / "data.txt").write_text(("x" * 300 + "\n") * 100, encoding="utf-8")
    repos = Repositories(tmp_path / "cache")
    overview = repos.open(str(root))
    rid = overview["repo_id"]
    assert set(overview["evidence_omitted"]) == {"README.md", "package.json"}
    page = repos.read(rid, "data.txt")
    assert page["next_line"] is not None
    assert len("\n".join(page["lines"])) <= 24000
    assert all(len(line) == 300 for line in page["lines"])
    tail = repos.read(rid, "data.txt", page["next_line"])
    assert len(page["lines"]) + len(tail["lines"]) == 100
    with pytest.raises(ValueError, match="display limit"):
        repos.read(rid, "package.json")


def test_language_filter_applies_before_limit(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    for i in range(60):
        (root / f"file{i:02}.txt").write_text("sharedword", encoding="utf-8")
    (root / "z.go").write_text("package sharedword", encoding="utf-8")
    repos = Repositories(tmp_path / "cache")
    rid = repos.open(str(root))["repo_id"]
    assert repos.search(rid, "sharedword", limit=1, language="go")[0]["path"] == "z.go"


@pytest.mark.parametrize("fixture", ["python", "typescript", "mixed"])
def test_stdio_workflow(tmp_path, mixed, fixture):
    async def exercise():
        folder = mixed
        query, path = "checkout", "README.md"
        if fixture == "python":
            folder = Path("tests/fixtures/sample_repo").resolve()
            query, path = "authenticate", "app/auth.py"
        elif fixture == "typescript":
            folder = tmp_path / "ts"
            folder.mkdir()
            (folder / "main.ts").write_text(
                "export function checkout() { return 1; }", encoding="utf-8"
            )
            path = "main.ts"
        params = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m",
                "explorer.mcp_server",
                "--cache-dir",
                str(tmp_path / "mcp-cache"),
            ],
        )
        async with (
            stdio_client(params) as (reader, writer),
            ClientSession(reader, writer) as session,
        ):
            await session.initialize()
            tools = await session.list_tools()
            assert len(tools.tools) == 6
            assert (
                next(
                    t for t in tools.tools if t.name == "open_repository"
                ).annotations.read_only_hint
                is False
            )

            async def call(name, args):
                result = await session.call_tool(name, args)
                assert not result.is_error, result
                assert result.structured_content is not None
                return json.loads(result.content[0].text)

            opened = await call("open_repository", {"folder": str(folder)})
            rid = opened["repo_id"]
            assert (await call("repo_overview", {"repo_id": rid}))["files"] > 0
            assert (await call("list_files", {"repo_id": rid}))["files"]
            assert (await call("search_code", {"repo_id": rid, "query": query}))[
                "matches"
            ]
            assert (await call("read_source", {"repo_id": rid, "path": path}))[
                "citation"
            ]
            assert "symbols" in await call(
                "file_outline", {"repo_id": rid, "path": path}
            )
            error = await session.call_tool(
                "read_source", {"repo_id": rid, "path": "../secret"}
            )
            assert error.is_error

    asyncio.run(exercise())
