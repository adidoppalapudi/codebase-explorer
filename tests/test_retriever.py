import os

import pytest

from explorer.retriever import (
    MAX_SOURCE_LINES,
    SourceError,
    StaleFileError,
    read_source,
    search_code,
)


def names(matches):
    return [m.qualified_name for m in matches]


@pytest.mark.parametrize(
    ("query", "expected_first"),
    [
        ("authentication", "UserService.authenticate"),
        ("Where is authentication handled?", "UserService.authenticate"),
        ("database connection", "open_connection"),
        ("UserService", "UserService"),
        ("userservice", "UserService"),
        ("refund a payment", "refund"),
        ("hash password", "hash_password"),
    ],
)
def test_search_finds_expected_symbol_first(db, query, expected_first):
    assert names(search_code(db, query))[0] == expected_first


def test_search_returns_citations(db):
    match = search_code(db, "hash_password")[0]
    assert (match.kind, match.citation) == ("function", "app/auth.py:20-22")


def test_search_without_evidence_is_empty(db):
    assert search_code(db, "kubernetes deployment manifest") == []
    assert search_code(db, "where is the") == []
    assert search_code(db, "   ") == []


def test_search_respects_limit_kind_and_path(db):
    assert len(search_code(db, "user", limit=2)) == 2
    assert {m.kind for m in search_code(db, "user", limit=50, kind="method")} == {"method"}
    assert {m.path for m in search_code(db, "user", limit=50, path="app/db")} == {"app/db.py"}
    # LIKE wildcards in the prefix are literal
    assert search_code(db, "user", path="app/%") == []


def test_search_rejects_bad_arguments(db):
    with pytest.raises(ValueError):
        search_code(db, "user", limit=0)
    with pytest.raises(ValueError):
        search_code(db, "user", kind="bogus")


@pytest.mark.parametrize(
    "query",
    ['auth* OR "x"', "NEAR(a b)", 'name:foo"bar', "-", "AND", "'; DROP TABLE symbols; --"],
)
def test_fts_syntax_in_queries_is_treated_as_words(db, query):
    assert isinstance(search_code(db, query), list)  # sqlite3.OperationalError would escape


def test_missing_index_fails_clearly(tmp_path):
    with pytest.raises(FileNotFoundError, match="Run 'index"):
        search_code(tmp_path / "missing.db", "user")


def test_read_source_returns_exact_numbered_lines(db):
    excerpt = read_source(db, "app/auth.py", 20, 22)
    assert excerpt.citation == "app/auth.py:20-22"
    assert excerpt.lines[0] == "def hash_password(password: str) -> str:"
    assert excerpt.render().splitlines() == [
        "app/auth.py:20-22",
        "20 | def hash_password(password: str) -> str:",
        '21 |     """Hash a password with SHA-256."""',
        "22 |     return hashlib.sha256(password.encode()).hexdigest()",
    ]


def test_every_search_citation_opens_to_its_definition(db):
    for match in search_code(db, "user", limit=50):
        if match.kind == 'text':
            continue
        excerpt = read_source(db, match.path, match.start_line, match.end_line)
        definition = next(line for line in excerpt.lines if not line.lstrip().startswith("@"))
        name = match.qualified_name.rsplit(".", 1)[-1]
        assert f"def {name}" in definition or f"class {name}" in definition


def test_read_source_accepts_dot_prefixed_paths(db):
    assert read_source(db, "./app/auth.py", 1, 1).path == "app/auth.py"


@pytest.mark.parametrize(
    "path",
    ["../outside.py", "app/../../outside.py", "/etc/passwd", "app\\auth.py", "", "."],
)
def test_read_source_rejects_paths_outside_the_repository(db, path):
    with pytest.raises(SourceError):
        read_source(db, path, 1, 1)


def test_read_source_only_reads_indexed_files(repo, db):
    (repo / ".env").write_text("API_KEY=secret\n")
    with pytest.raises(SourceError, match="not in the index"):
        read_source(db, ".env", 1, 1)


@pytest.mark.parametrize(
    ("start", "end"),
    [(0, 1), (5, 4), (-1, 3), (1, MAX_SOURCE_LINES + 1), (20, 23)],
)
def test_read_source_rejects_invalid_ranges(db, start, end):
    with pytest.raises(SourceError):
        read_source(db, "app/auth.py", start, end)


def test_read_source_detects_changed_files(repo, db):
    path = repo / "app" / "auth.py"
    path.write_text(path.read_text() + "\n# edited\n")
    with pytest.raises(StaleFileError, match="Run 'index' again"):
        read_source(db, "app/auth.py", 1, 1)


def test_read_source_detects_missing_files(repo, db):
    (repo / "app" / "auth.py").unlink()
    with pytest.raises(SourceError, match="no longer exists"):
        read_source(db, "app/auth.py", 1, 1)


def test_read_source_rejects_indexed_file_replaced_by_outside_symlink(repo, db, tmp_path):
    outside = tmp_path / "outside.py"
    outside.write_text((repo / "app" / "auth.py").read_text())
    (repo / "app" / "auth.py").unlink()
    try:
        os.symlink(outside, repo / "app" / "auth.py")
    except OSError:
        pytest.skip("symlinks not available")
    with pytest.raises(SourceError, match="outside the repository"):
        read_source(db, "app/auth.py", 1, 1)
