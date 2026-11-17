"""The SQLite index. Owns the schema, full-text search rules, and every SQL statement."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS files (
    id     INTEGER PRIMARY KEY,
    path   TEXT NOT NULL UNIQUE,
    sha256 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS symbols (
    id             INTEGER PRIMARY KEY,
    file_id        INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    name           TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    kind           TEXT NOT NULL CHECK (kind IN ('function', 'class', 'method')),
    signature      TEXT NOT NULL,
    start_line     INTEGER NOT NULL,
    end_line       INTEGER NOT NULL,
    docstring      TEXT
);
CREATE INDEX IF NOT EXISTS symbols_name ON symbols(name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS symbols_qualified_name ON symbols(qualified_name COLLATE NOCASE);
CREATE VIRTUAL TABLE IF NOT EXISTS symbols_fts USING fts5(
    name_terms,
    path,
    signature,
    docstring,
    excerpt,
    tokenize = 'porter unicode61'
);
"""

# bm25 column weights, in symbols_fts column order.
# A hit in a symbol's name counts far more than a hit in its body.
_BM25_WEIGHTS = (10.0, 3.0, 5.0, 4.0, 1.0)
_EXCERPT_MAX_LINES = 40
_EXCERPT_MAX_CHARS = 3_000
_MAX_QUERY_TERMS = 16
_IDENTIFIER_PART = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
_STOPWORDS = frozenset("""
    a an and are as at be by can do does for from handled how i in is it its me
    of on or that the this to what when where which who why with
""".split())
_MATCH_COLUMNS = "s.qualified_name, s.kind, f.path, s.start_line, s.end_line, s.signature"


@dataclass(frozen=True)
class SymbolRecord:
    name: str
    qualified_name: str
    kind: str
    signature: str
    start_line: int
    end_line: int
    docstring: str | None
    source: str


@dataclass(frozen=True)
class FileRecord:
    path: str
    sha256: str
    symbols: list[SymbolRecord]


@dataclass(frozen=True)
class Match:
    qualified_name: str
    kind: str
    path: str
    start_line: int
    end_line: int
    signature: str

    @property
    def citation(self) -> str:
        return f"{self.path}:{self.start_line}-{self.end_line}"


def write_index(db_path: Path, root: Path, files: list[FileRecord]) -> None:
    """Replace the whole index with `files` in one transaction."""
    _check_fts5()
    conn = _connect(db_path)
    try:
        conn.executescript(SCHEMA)
        with conn:
            conn.execute("DELETE FROM symbols_fts")
            conn.execute("DELETE FROM symbols")
            conn.execute("DELETE FROM files")
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('root', ?)", (str(root),)
            )
            for file in files:
                _insert_file(conn, file)
    finally:
        conn.close()


def search_names(
    db_path: Path, name: str, kind: str | None, path_prefix: str | None, limit: int
) -> list[Match]:
    """Symbols whose name or qualified name equals `name`, ignoring case."""
    with _reader(db_path) as conn:
        rows = conn.execute(
            f"""SELECT {_MATCH_COLUMNS}
                FROM symbols s JOIN files f ON f.id = s.file_id
                WHERE (s.name = ?1 COLLATE NOCASE OR s.qualified_name = ?1 COLLATE NOCASE)
                  AND (?2 IS NULL OR s.kind = ?2)
                  AND (?3 IS NULL OR f.path LIKE ?3 ESCAPE '\\')
                ORDER BY f.path, s.start_line
                LIMIT ?4""",
            (name, kind, _like_prefix(path_prefix), limit),
        ).fetchall()
    return [Match(*row) for row in rows]


def search_text(
    db_path: Path, text: str, kind: str | None, path_prefix: str | None, limit: int
) -> list[Match]:
    """Full-text search over names, paths, signatures, docstrings, and source, best first.

    Accepts any user text. Returns an empty list when `text` has no searchable words.
    """
    match = _fts_query(text)
    if not match:
        return []
    weights = ", ".join(str(w) for w in _BM25_WEIGHTS)
    with _reader(db_path) as conn:
        rows = conn.execute(
            f"""SELECT {_MATCH_COLUMNS}
                FROM symbols_fts
                JOIN symbols s ON s.id = symbols_fts.rowid
                JOIN files f ON f.id = s.file_id
                WHERE symbols_fts MATCH ?1
                  AND (?2 IS NULL OR s.kind = ?2)
                  AND (?3 IS NULL OR f.path LIKE ?3 ESCAPE '\\')
                ORDER BY bm25(symbols_fts, {weights}), f.path, s.start_line
                LIMIT ?4""",
            (match, kind, _like_prefix(path_prefix), limit),
        ).fetchall()
    return [Match(*row) for row in rows]


def indexed_file(db_path: Path, path: str) -> tuple[Path, str | None]:
    """The repository root, and the SHA-256 recorded for `path` or None if the index lacks it."""
    with _reader(db_path) as conn:
        root = conn.execute("SELECT value FROM meta WHERE key = 'root'").fetchone()
        sha = conn.execute("SELECT sha256 FROM files WHERE path = ?", (path,)).fetchone()
    if root is None:
        raise RuntimeError("The index has no repository root. Run 'index <folder>' again.")
    return Path(root[0]), sha[0] if sha else None


def _insert_file(conn: sqlite3.Connection, file: FileRecord) -> None:
    file_id = conn.execute(
        "INSERT INTO files (path, sha256) VALUES (?, ?)", (file.path, file.sha256)
    ).lastrowid
    for sym in file.symbols:
        symbol_id = conn.execute(
            """INSERT INTO symbols (file_id, name, qualified_name, kind,
                   signature, start_line, end_line, docstring)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (file_id, sym.name, sym.qualified_name, sym.kind, sym.signature,
             sym.start_line, sym.end_line, sym.docstring),
        ).lastrowid
        excerpt = "\n".join(sym.source.split("\n")[:_EXCERPT_MAX_LINES])[:_EXCERPT_MAX_CHARS]
        conn.execute(
            """INSERT INTO symbols_fts (rowid, name_terms, path, signature, docstring, excerpt)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (symbol_id, " ".join(_identifier_terms(sym.qualified_name)), file.path,
             sym.signature, sym.docstring or "", excerpt),
        )


def _fts_query(text: str) -> str:
    """Quoted terms joined by OR.

    Every term matches [a-z0-9_]+, so user text never reaches FTS5 operators,
    column filters, or quoting.
    """
    terms: list[str] = []
    for word in re.findall(r"[A-Za-z0-9_]+", text):
        if word.lower() not in _STOPWORDS:
            terms.extend(_identifier_terms(word))
    terms = list(dict.fromkeys(terms))[:_MAX_QUERY_TERMS]
    return " OR ".join(f'"{term}"' for term in terms)


def _identifier_terms(identifier: str) -> list[str]:
    """Lowercase terms for an identifier plus its camelCase and snake_case parts.

    `UserService.get_DB` gives userservice, user, service, get_db, get, db.
    """
    terms: list[str] = []
    for word in re.findall(r"\w+", identifier):
        terms.append(word.lower())
        parts = [p.lower() for p in _IDENTIFIER_PART.findall(word)]
        if len(parts) > 1:
            terms.extend(parts)
    return list(dict.fromkeys(terms))


def _check_fts5() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE VIRTUAL TABLE probe USING fts5(x)")
    except sqlite3.OperationalError as exc:
        raise RuntimeError(
            f"SQLite {sqlite3.sqlite_version} in this Python has no FTS5 support."
        ) from exc
    finally:
        conn.close()


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def _reader(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Open the index read-only for one block, then close it."""
    if not Path(db_path).is_file():
        raise FileNotFoundError(f"No index at {db_path}. Run 'index <folder>' first.")
    conn = _connect(Path(db_path))
    try:
        conn.execute("PRAGMA query_only = ON")
        yield conn
    finally:
        conn.close()


def _like_prefix(prefix: str | None) -> str | None:
    if prefix is None:
        return None
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped + "%"
