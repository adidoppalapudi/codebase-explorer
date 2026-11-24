"""The SQLite index. Owns the schema, full-text search rules, and every SQL statement."""

from __future__ import annotations

import json
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
    sha256 TEXT NOT NULL,
    language TEXT NOT NULL DEFAULT 'text',
    coverage TEXT NOT NULL DEFAULT 'text',
    line_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS symbols (
    id             INTEGER PRIMARY KEY,
    file_id        INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    name           TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    kind           TEXT NOT NULL,
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
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(path, content, tokenize='porter unicode61');
"""

# bm25 column weights, in symbols_fts column order.
# A hit in a symbol's name counts far more than a hit in its body.
_BM25_WEIGHTS = (10.0, 3.0, 5.0, 4.0, 1.0)
_EXCERPT_MAX_LINES = 40
_EXCERPT_MAX_CHARS = 3_000
_MAX_QUERY_TERMS = 16
_IDENTIFIER_PART = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "can",
        "do",
        "does",
        "for",
        "from",
        "handled",
        "how",
        "i",
        "in",
        "is",
        "it",
        "its",
        "me",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "with",
    ]
)
_MATCH_COLUMNS = (
    "s.qualified_name, s.kind, f.path, s.start_line, s.end_line, s.signature"
)


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
    text: str = ""
    language: str = "text"
    coverage: str = "text"


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


def write_index(
    db_path: Path, root: Path, files: list[FileRecord], diagnostics: list | None = None
) -> None:
    """Replace the whole index with `files` in one transaction."""
    _check_fts5()
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = _connect(db_path)
    try:
        # Rebuilding is the migration path for indexes made by 0.1.
        columns = {row[1] for row in conn.execute("PRAGMA table_info(files)")}
        if columns and "coverage" not in columns:
            conn.close()
            raise RuntimeError(
                "Legacy index format. Choose a new database path or remove the old index and rebuild."
            )
        conn.executescript(SCHEMA)
        with conn:
            conn.execute("DELETE FROM chunks_fts")
            conn.execute("DELETE FROM chunks")
            conn.execute("DELETE FROM symbols_fts")
            conn.execute("DELETE FROM symbols")
            conn.execute("DELETE FROM files")
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('root', ?)",
                (str(root),),
            )
            conn.execute("INSERT OR REPLACE INTO meta VALUES ('version', '2')")
            conn.execute(
                "INSERT OR REPLACE INTO meta VALUES ('diagnostics', ?)",
                (json.dumps(diagnostics or []),),
            )
            for file in files:
                _insert_file(conn, file)
    finally:
        conn.close()


def search_names(
    db_path: Path,
    name: str,
    kind: str | None,
    path_prefix: str | None,
    limit: int,
    language: str | None = None,
) -> list[Match]:
    """Symbols whose name or qualified name equals `name`, ignoring case."""
    with _reader(db_path) as conn:
        rows = conn.execute(
            f"""SELECT {_MATCH_COLUMNS}
                FROM symbols s JOIN files f ON f.id = s.file_id
                WHERE (s.name = ?1 COLLATE NOCASE OR s.qualified_name = ?1 COLLATE NOCASE)
                  AND (?2 IS NULL OR s.kind = ?2)
                  AND (?3 IS NULL OR f.path LIKE ?3 ESCAPE '\\')
                  AND (?5 IS NULL OR f.language = ?5)
                ORDER BY f.path, s.start_line
                LIMIT ?4""",
            (name, kind, _like_prefix(path_prefix), limit, language),
        ).fetchall()
    return [Match(*row) for row in rows]


def search_text(
    db_path: Path,
    text: str,
    kind: str | None,
    path_prefix: str | None,
    limit: int,
    language: str | None = None,
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
                  AND (?5 IS NULL OR f.language = ?5)
                ORDER BY bm25(symbols_fts, {weights}), f.path, s.start_line
                LIMIT ?4""",
            (match, kind, _like_prefix(path_prefix), limit, language),
        ).fetchall()
    return [Match(*row) for row in rows]


def indexed_file(db_path: Path, path: str) -> tuple[Path, str | None]:
    """The repository root, and the SHA-256 recorded for `path` or None if the index lacks it."""
    with _reader(db_path) as conn:
        root = conn.execute("SELECT value FROM meta WHERE key = 'root'").fetchone()
        sha = conn.execute(
            "SELECT sha256 FROM files WHERE path = ?", (path,)
        ).fetchone()
    if root is None:
        raise RuntimeError(
            "The index has no repository root. Run 'index <folder>' again."
        )
    return Path(root[0]), sha[0] if sha else None


def _insert_file(conn: sqlite3.Connection, file: FileRecord) -> None:
    file_id = conn.execute(
        "INSERT INTO files (path, sha256, language, coverage, line_count) VALUES (?, ?, ?, ?, ?)",
        (
            file.path,
            file.sha256,
            file.language,
            file.coverage,
            len(file.text.split("\n")) - int(file.text.endswith("\n"))
            if file.text
            else 0,
        ),
    ).lastrowid
    lines = file.text.split("\n")
    if lines[-1] == "":
        lines.pop()
    for start in range(0, len(lines), 100):
        end = min(start + 100, len(lines))
        chunk_id = conn.execute(
            "INSERT INTO chunks (file_id, start_line, end_line) VALUES (?, ?, ?)",
            (file_id, start + 1, end),
        ).lastrowid
        conn.execute(
            "INSERT INTO chunks_fts (rowid, path, content) VALUES (?, ?, ?)",
            (chunk_id, file.path, "\n".join(lines[start:end])),
        )
    for sym in file.symbols:
        symbol_id = conn.execute(
            """INSERT INTO symbols (file_id, name, qualified_name, kind,
                   signature, start_line, end_line, docstring)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                file_id,
                sym.name,
                sym.qualified_name,
                sym.kind,
                sym.signature,
                sym.start_line,
                sym.end_line,
                sym.docstring,
            ),
        ).lastrowid
        excerpt = "\n".join(sym.source.split("\n")[:_EXCERPT_MAX_LINES])[
            :_EXCERPT_MAX_CHARS
        ]
        conn.execute(
            """INSERT INTO symbols_fts (rowid, name_terms, path, signature, docstring, excerpt)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                symbol_id,
                " ".join(_identifier_terms(sym.qualified_name)),
                file.path,
                sym.signature,
                sym.docstring or "",
                excerpt,
            ),
        )


def _fts_query(text: str) -> str:
    """Quoted terms joined by OR.

    Every term contains Unicode word characters, so user text never reaches FTS5 operators,
    column filters, or quoting.
    """
    terms: list[str] = []
    for word in re.findall(r"\w+", text):
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
    conn = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA query_only = ON")
        if conn.execute("SELECT value FROM meta WHERE key='version'").fetchone() != (
            "2",
        ):
            raise RuntimeError(
                "Unsupported index format. Rebuild using a new database path."
            )
        yield conn
    except sqlite3.Error as exc:
        raise RuntimeError(f"Cannot read index: {exc}. Rebuild the index.") from exc
    finally:
        conn.close()


def _like_prefix(prefix: str | None) -> str | None:
    if prefix is None:
        return None
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped + "%"


def search_chunks(
    db_path: Path, query: str, path: str | None, limit: int, language: str | None = None
) -> list[Match]:
    with _reader(db_path) as conn:
        match = _fts_query(query)
        if not match:
            return []
        rows = conn.execute(
            """SELECT f.path, 'text', f.path, c.start_line, c.end_line, ''
            FROM chunks_fts JOIN chunks c ON c.id=chunks_fts.rowid
            JOIN files f ON f.id=c.file_id
            WHERE chunks_fts MATCH ? AND (? IS NULL OR f.path LIKE ? ESCAPE '\\')
            AND (? IS NULL OR f.language = ?)
            ORDER BY bm25(chunks_fts, 3.0, 1.0), f.path, c.start_line LIMIT ?""",
            (match, _like_prefix(path), _like_prefix(path), language, language, limit),
        ).fetchall()
    return [Match(*row) for row in rows]


def inventory(db_path: Path) -> tuple[list[dict], list]:
    with _reader(db_path) as conn:
        conn.row_factory = sqlite3.Row
        files = [
            dict(row)
            for row in conn.execute(
                "SELECT path, language, coverage, line_count FROM files ORDER BY path"
            )
        ]
        diagnostics = json.loads(
            conn.execute("SELECT value FROM meta WHERE key='diagnostics'").fetchone()[0]
        )
    return files, diagnostics


def outline(db_path: Path, path: str) -> list[Match]:
    with _reader(db_path) as conn:
        rows = conn.execute(
            f"""SELECT {_MATCH_COLUMNS} FROM symbols s
            JOIN files f ON f.id=s.file_id WHERE f.path=? ORDER BY s.start_line, s.id""",
            (path,),
        ).fetchall()
    return [Match(*row) for row in rows]
