"""The SQLite index. Owns the schema, full-text search rules, and every SQL statement.

The full-text tables are contentless: they keep the search index and ranking statistics but
not a second copy of the text, which stays in the repository and is read back (hash-checked)
when it is shown. SQLite 3.43 and later can delete rows from contentless tables, so changed
files are updated in place with the same ranking a full rebuild would give. Older SQLite
can't, so `CAN_UPDATE` is False and the indexer rebuilds whenever content changes.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

VERSION = "3"
CAN_UPDATE = sqlite3.sqlite_version_info >= (3, 43, 0)
_FTS_OPTIONS = "content = '', contentless_delete = 1" if CAN_UPDATE else "content = ''"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS files (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    path       TEXT NOT NULL UNIQUE,
    sha256     TEXT NOT NULL,
    language   TEXT NOT NULL DEFAULT 'text',
    coverage   TEXT NOT NULL DEFAULT 'text',
    line_count INTEGER NOT NULL DEFAULT 0,
    size       INTEGER NOT NULL DEFAULT -1,
    mtime_ns   INTEGER NOT NULL DEFAULT -1,
    warning    TEXT
);
CREATE TABLE IF NOT EXISTS skipped (
    path     TEXT PRIMARY KEY,
    reason   TEXT NOT NULL,
    size     INTEGER NOT NULL DEFAULT -1,
    mtime_ns INTEGER NOT NULL DEFAULT -1
);
CREATE TABLE IF NOT EXISTS symbols (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id        INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    name           TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    kind           TEXT NOT NULL,
    signature      TEXT NOT NULL,
    start_line     INTEGER NOT NULL,
    end_line       INTEGER NOT NULL,
    docstring      TEXT
);
CREATE INDEX IF NOT EXISTS symbols_file ON symbols(file_id);
CREATE INDEX IF NOT EXISTS symbols_name ON symbols(name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS symbols_qualified_name ON symbols(qualified_name COLLATE NOCASE);
CREATE VIRTUAL TABLE IF NOT EXISTS symbols_fts USING fts5(
    name_terms,
    path,
    signature,
    docstring,
    excerpt,
    {fts},
    tokenize = 'porter unicode61'
);
CREATE TABLE IF NOT EXISTS chunks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id    INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    start_line INTEGER NOT NULL,
    end_line   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS chunks_file ON chunks(file_id);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    path,
    content,
    {fts},
    tokenize = 'porter unicode61'
);
""".replace("{fts}", _FTS_OPTIONS)

# bm25 column weights, in symbols_fts column order.
# A hit in a symbol's name counts far more than a hit in its body.
_BM25_WEIGHTS = (10.0, 3.0, 5.0, 4.0, 1.0)
# Module constants and variables are short, so bm25 favours them. Scale their scores down
# so functions, classes and methods with the same words rank first; exact names still win.
_VALUE_KIND_FACTOR = 0.6
_CHUNK_LINES = 100
_EXCERPT_MAX_LINES = 40
_EXCERPT_MAX_CHARS = 3_000
_MAX_QUERY_TERMS = 16
_IDENTIFIER_PART = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
STOPWORDS = frozenset(
    [
        "a", "an", "and", "are", "as", "at", "be", "by", "can", "do", "does", "for",
        "from", "handled", "how", "i", "in", "is", "it", "its", "me", "of", "on", "or",
        "that", "the", "this", "to", "what", "when", "where", "which", "who", "why",
        "with",
    ]
)
_MATCH_COLUMNS = (
    "s.qualified_name, s.kind, f.path, s.start_line, s.end_line, s.signature, s.id"
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
    size: int = -1
    mtime_ns: int = -1
    warning: str | None = None


@dataclass(frozen=True)
class SkippedRecord:
    path: str
    reason: str
    size: int = -1
    mtime_ns: int = -1


@dataclass(frozen=True)
class Match:
    qualified_name: str
    kind: str
    path: str
    start_line: int
    end_line: int
    signature: str
    # Row id in symbols or chunks. Not part of equality, so results compare by content.
    ref: int | None = field(default=None, compare=False, repr=False)

    @property
    def citation(self) -> str:
        return f"{self.path}:{self.start_line}-{self.end_line}"


# ---------------------------------------------------------------- writing


def write_index(
    db_path: Path,
    root: Path,
    files: list[FileRecord],
    skipped: list[SkippedRecord] | None = None,
    meta: dict[str, str] | None = None,
) -> None:
    """Create a complete index in `db_path`, which should be a new, empty file."""
    _check_fts5()
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = _connect(db_path)
    try:
        conn.executescript(SCHEMA)
        with conn:
            for table in ("chunks_fts", "chunks", "symbols_fts", "symbols", "skipped", "files", "meta"):
                conn.execute(f"DELETE FROM {table}")
            values = {"root": str(root), "version": VERSION, **(meta or {})}
            conn.executemany("INSERT INTO meta (key, value) VALUES (?, ?)", values.items())
            for file in files:
                _insert_file(conn, file)
            _insert_skipped(conn, skipped or [])
    finally:
        conn.close()


def update_index(
    db_path: Path,
    files: list[FileRecord],
    skipped: list[SkippedRecord],
    removed: Iterable[str],
    touched: Iterable[tuple[str, int, int]] = (),
) -> None:
    """Replace the rows for changed paths and drop removed paths, in one transaction.

    `touched` paths kept their content but have a new size or time to record.
    """
    paths = [f.path for f in files] + [s.path for s in skipped] + list(removed)
    if (files or removed) and not CAN_UPDATE:
        raise RuntimeError("This SQLite can't update the index in place; rebuild it instead.")
    conn = _connect(db_path)
    try:
        with conn:
            conn.executemany(
                "UPDATE files SET size = ?, mtime_ns = ? WHERE path = ?",
                [(size, mtime, path) for path, size, mtime in touched],
            )
            for batch in _batches(paths):
                marks = ",".join("?" * len(batch))
                ids = f"SELECT id FROM files WHERE path IN ({marks})"
                conn.execute(
                    f"DELETE FROM symbols_fts WHERE rowid IN (SELECT id FROM symbols WHERE file_id IN ({ids}))",
                    batch,
                )
                conn.execute(
                    f"DELETE FROM chunks_fts WHERE rowid IN (SELECT id FROM chunks WHERE file_id IN ({ids}))",
                    batch,
                )
                conn.execute(f"DELETE FROM files WHERE path IN ({marks})", batch)
                conn.execute(f"DELETE FROM skipped WHERE path IN ({marks})", batch)
            for file in files:
                _insert_file(conn, file)
            _insert_skipped(conn, skipped)
        if files or removed:
            with conn:
                _repair_totals(conn, "symbols_fts", 5)
                _repair_totals(conn, "chunks_fts", 2)
    finally:
        conn.close()


def _repair_totals(conn: sqlite3.Connection, table: str, columns: int) -> None:
    """Recompute the row and token totals that bm25 uses, from the per-row sizes.

    Deleting from a contentless-delete table leaves these totals counting the deleted rows,
    which shifts every score. The per-row sizes are exact, so the totals are rebuilt from
    them, giving the same ranking as a fresh index. Skipped if the stored format is not the
    documented one.
    """
    average = conn.execute(f"SELECT block FROM {table}_data WHERE id = 1").fetchone()
    if average is None or len(_get_varints(average[0])) != columns + 1:
        return
    rows, totals = 0, [0] * columns
    for (size,) in conn.execute(f"SELECT sz FROM {table}_docsize"):
        values = _get_varints(size)
        if len(values) != columns:
            return
        rows += 1
        totals = [a + b for a, b in zip(totals, values)]
    block = b"".join(_put_varint(v) for v in [rows, *totals])
    conn.execute(f"UPDATE {table}_data SET block = ? WHERE id = 1", (block,))


def _get_varints(data: bytes) -> list[int]:
    """Decode SQLite's big-endian varints (7 bits per byte, a full ninth byte)."""
    values, i = [], 0
    while i < len(data):
        value = 0
        for k in range(9):
            byte = data[i]
            i += 1
            if k == 8:
                value = (value << 8) | byte
                break
            value = (value << 7) | (byte & 0x7F)
            if not byte & 0x80:
                break
        values.append(value)
    return values


def _put_varint(value: int) -> bytes:
    if value < 0 or value >= 1 << 56:
        raise ValueError("value out of range for this encoder")
    groups = [value & 0x7F]
    value >>= 7
    while value:
        groups.append((value & 0x7F) | 0x80)
        value >>= 7
    return bytes(reversed(groups))


def _insert_file(conn: sqlite3.Connection, file: FileRecord) -> None:
    lines = file.text.split("\n") if file.text else []
    if lines and lines[-1] == "":
        lines.pop()
    file_id = conn.execute(
        """INSERT INTO files (path, sha256, language, coverage, line_count, size, mtime_ns, warning)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (file.path, file.sha256, file.language, file.coverage, len(lines),
         file.size, file.mtime_ns, file.warning),
    ).lastrowid
    for start in range(0, len(lines), _CHUNK_LINES):
        end = min(start + _CHUNK_LINES, len(lines))
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
            (file_id, sym.name, sym.qualified_name, sym.kind, sym.signature,
             sym.start_line, sym.end_line, sym.docstring),
        ).lastrowid
        excerpt = "\n".join(sym.source.split("\n")[:_EXCERPT_MAX_LINES])[:_EXCERPT_MAX_CHARS]
        conn.execute(
            """INSERT INTO symbols_fts (rowid, name_terms, path, signature, docstring, excerpt)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (symbol_id, " ".join(_identifier_terms(_name_scope(sym.qualified_name, sym.kind))), file.path,
             sym.signature, sym.docstring or "", excerpt),
        )


def _name_scope(qualified_name: str, kind: str) -> str:
    """The part of a qualified name that counts as the definition's name in search.

    Methods keep their class (`UserService.authenticate`), so "user service" finds them.
    Other definitions use their own name only, so a helper nested in a long test function
    doesn't match every word of the test's name.
    """
    parts = qualified_name.split(".")
    return ".".join(parts[-2:]) if kind == "method" else parts[-1]


def _insert_skipped(conn: sqlite3.Connection, skipped: list[SkippedRecord]) -> None:
    conn.executemany(
        "INSERT OR REPLACE INTO skipped (path, reason, size, mtime_ns) VALUES (?, ?, ?, ?)",
        [(s.path, s.reason, s.size, s.mtime_ns) for s in skipped],
    )


def _batches(items: list[str], size: int = 400) -> Iterator[list[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


# ---------------------------------------------------------------- index state


def read_meta(db_path: Path) -> dict[str, str]:
    """Metadata from an index of any version. Empty when the file isn't an index."""
    if not Path(db_path).is_file():
        return {}
    try:
        conn = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error:
        return {}
    try:
        return dict(conn.execute("SELECT key, value FROM meta").fetchall())
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


def needs_rebuild(db_path: Path, expected: dict[str, str]) -> str | None:
    """Why the index must be rebuilt from scratch, or None if it can be updated in place."""
    meta = read_meta(db_path)
    if not meta:
        return "no index"
    if meta.get("version") != VERSION:
        return "index format changed"
    for key, value in expected.items():
        if meta.get(key) != value:
            return f"{key} changed"
    return None


def file_states(db_path: Path) -> tuple[dict[str, tuple[int, int, str]], dict[str, tuple[int, int]]]:
    """Recorded size and mtime for indexed and skipped paths, for change detection."""
    with _reader(db_path) as conn:
        files = {p: (s, m, h) for p, s, m, h in conn.execute(
            "SELECT path, size, mtime_ns, sha256 FROM files")}
        skipped = {p: (s, m) for p, s, m in conn.execute(
            "SELECT path, size, mtime_ns FROM skipped")}
    return files, skipped


def diagnostics(db_path: Path) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Skipped files with reasons, and indexed files whose structure extraction warned."""
    with _reader(db_path) as conn:
        skipped = conn.execute("SELECT path, reason FROM skipped ORDER BY path").fetchall()
        warnings = conn.execute(
            "SELECT path, warning FROM files WHERE warning IS NOT NULL ORDER BY path"
        ).fetchall()
    return skipped, warnings


def counts(db_path: Path) -> tuple[int, int]:
    with _reader(db_path) as conn:
        return conn.execute(
            "SELECT (SELECT count(*) FROM files), (SELECT count(*) FROM symbols)"
        ).fetchone()


# ---------------------------------------------------------------- searching


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
                ORDER BY bm25(symbols_fts, {weights})
                         * (CASE WHEN s.kind IN ('constant', 'variable') THEN {_VALUE_KIND_FACTOR} ELSE 1 END),
                         f.path, s.start_line
                LIMIT ?4""",
            (match, kind, _like_prefix(path_prefix), limit, language),
        ).fetchall()
    return [Match(*row) for row in rows]


def search_chunks(
    db_path: Path, query: str, path: str | None, limit: int, language: str | None = None
) -> list[Match]:
    match = _fts_query(query)
    if not match:
        return []
    with _reader(db_path) as conn:
        rows = conn.execute(
            """SELECT f.path, 'text', f.path, c.start_line, c.end_line, '', c.id
            FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid
            JOIN files f ON f.id = c.file_id
            WHERE chunks_fts MATCH ? AND (? IS NULL OR f.path LIKE ? ESCAPE '\\')
            AND (? IS NULL OR f.language = ?)
            ORDER BY bm25(chunks_fts, 3.0, 1.0), f.path, c.start_line LIMIT ?""",
            (match, _like_prefix(path), _like_prefix(path), language, language, limit),
        ).fetchall()
    return [Match(*row) for row in rows]


def matching_rows(db_path: Path, table: str, term: str, ids: list[int], column: str | None = None) -> set[int]:
    """Which of `ids` in symbols_fts or chunks_fts contain `term`, optionally in one column."""
    if not ids or table not in ("symbols_fts", "chunks_fts"):
        return set()
    query = f'"{term}"' if column is None else f'{column} : "{term}"'
    marks = ",".join("?" * len(ids))
    with _reader(db_path) as conn:
        return {row[0] for row in conn.execute(
            f"SELECT rowid FROM {table} WHERE {table} MATCH ? AND rowid IN ({marks})",
            [query, *ids],
        )}


def phrase_chunks(db_path: Path, word: str, path: str | None, limit: int) -> list[tuple[str, int, int]]:
    """Chunks containing `word` as a phrase of its tokens, in path order."""
    terms = re.findall(r"\w+", word)
    if not terms:
        return []
    with _reader(db_path) as conn:
        return conn.execute(
            """SELECT f.path, c.start_line, c.end_line
            FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid
            JOIN files f ON f.id = c.file_id
            WHERE chunks_fts MATCH ? AND (? IS NULL OR f.path LIKE ? ESCAPE '\\')
            ORDER BY f.path, c.start_line LIMIT ?""",
            ('"' + " ".join(terms) + '"', _like_prefix(path), _like_prefix(path), limit),
        ).fetchall()


# ---------------------------------------------------------------- browsing


def indexed_file(db_path: Path, path: str) -> tuple[Path, str | None]:
    """The repository root, and the SHA-256 recorded for `path` or None if the index lacks it."""
    with _reader(db_path) as conn:
        root = conn.execute("SELECT value FROM meta WHERE key = 'root'").fetchone()
        sha = conn.execute("SELECT sha256 FROM files WHERE path = ?", (path,)).fetchone()
    if root is None:
        raise RuntimeError("The index has no repository root. Run 'index <folder>' again.")
    return Path(root[0]), sha[0] if sha else None


def file_info(db_path: Path, path: str) -> dict | None:
    with _reader(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT path, language, coverage, line_count FROM files WHERE path = ?", (path,)
        ).fetchone()
    return dict(row) if row else None


def similar_paths(db_path: Path, path: str, limit: int = 5) -> list[str]:
    """Indexed paths equal to `path` ignoring case, then paths sharing its file name."""
    name = path.rsplit("/", 1)[-1]
    with _reader(db_path) as conn:
        same = [r[0] for r in conn.execute(
            "SELECT path FROM files WHERE path = ? COLLATE NOCASE ORDER BY path LIMIT ?",
            (path, limit))]
        if same:
            return same
        return [r[0] for r in conn.execute(
            """SELECT path FROM files
               WHERE path = ?1 COLLATE NOCASE OR path LIKE ?2 ESCAPE '\\'
               ORDER BY length(path), path LIMIT ?3""",
            (name, "%/" + _like_prefix(name)[:-1], limit))]


def inventory(db_path: Path) -> tuple[list[dict], list]:
    with _reader(db_path) as conn:
        conn.row_factory = sqlite3.Row
        files = [dict(row) for row in conn.execute(
            "SELECT path, language, coverage, line_count FROM files ORDER BY path")]
        diagnostics = [list(row) for row in conn.execute(
            """SELECT path, reason FROM skipped
               UNION ALL SELECT path, warning FROM files WHERE warning IS NOT NULL
               ORDER BY 1""")]
    return files, diagnostics


def list_files(
    db_path: Path, prefix: str, language: str | None, offset: int, limit: int
) -> tuple[list[dict], int]:
    with _reader(db_path) as conn:
        conn.row_factory = sqlite3.Row
        where = "path LIKE ? ESCAPE '\\' AND (? IS NULL OR language = ?)"
        args = (_like_prefix(prefix), language, language)
        total = conn.execute(f"SELECT count(*) FROM files WHERE {where}", args).fetchone()[0]
        rows = [dict(r) for r in conn.execute(
            f"""SELECT path, language, coverage, line_count FROM files WHERE {where}
                ORDER BY path LIMIT ? OFFSET ?""", (*args, limit, offset))]
    return rows, total


def outline(db_path: Path, path: str) -> list[Match]:
    with _reader(db_path) as conn:
        rows = conn.execute(
            f"""SELECT {_MATCH_COLUMNS} FROM symbols s
            JOIN files f ON f.id = s.file_id WHERE f.path = ? ORDER BY s.start_line, s.id""",
            (path,),
        ).fetchall()
    return [Match(*row) for row in rows]


def symbols_named(db_path: Path, name: str) -> list[Match]:
    with _reader(db_path) as conn:
        rows = conn.execute(
            f"""SELECT {_MATCH_COLUMNS} FROM symbols s JOIN files f ON f.id = s.file_id
                WHERE s.name = ? ORDER BY f.path, s.start_line""",
            (name,),
        ).fetchall()
    return [Match(*row) for row in rows]


def check(db_path: Path) -> None:
    """Raise unless `db_path` is a readable index of the current format."""
    with _reader(db_path):
        pass


# ---------------------------------------------------------------- query text


def query_words(text: str) -> list[str]:
    """The words of a query that are searched, in order, without stopwords or repeats."""
    words = [w for w in re.findall(r"\w+", text) if w.lower() not in STOPWORDS]
    return list(dict.fromkeys(words))


def _fts_query(text: str) -> str:
    """Quoted terms joined by OR.

    Every term contains Unicode word characters, so user text never reaches FTS5 operators,
    column filters, or quoting.
    """
    terms: list[str] = []
    for word in query_words(text):
        terms.extend(identifier_terms(word))
    terms = list(dict.fromkeys(terms))[:_MAX_QUERY_TERMS]
    return " OR ".join(f'"{term}"' for term in terms)


def identifier_terms(identifier: str) -> list[str]:
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


_identifier_terms = identifier_terms  # earlier name, kept for callers


# ---------------------------------------------------------------- connections


def _check_fts5() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE VIRTUAL TABLE probe USING fts5(x, content='')")
    except sqlite3.OperationalError as exc:
        raise RuntimeError(
            f"SQLite {sqlite3.sqlite_version} in this Python has no FTS5 support."
        ) from exc
    finally:
        conn.close()


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def _reader(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Open the index read-only for one block, then close it."""
    if not Path(db_path).is_file():
        raise FileNotFoundError(f"No index at {db_path}. Run 'index <folder>' first.")
    conn = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    try:
        conn.execute("PRAGMA query_only = ON")
        version = conn.execute("SELECT value FROM meta WHERE key='version'").fetchone()
        if version != (VERSION,):
            raise RuntimeError(
                "This index was made by an older version. Run 'index <folder>' again to rebuild it."
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


def dumps(value) -> str:
    return json.dumps(value, sort_keys=True)
