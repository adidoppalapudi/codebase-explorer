"""Index repository text and language definitions into SQLite, updating only what changed."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from pathspec import GitIgnoreSpec

from explorer import storage
from explorer.languages import extract, extractor_signature, language_for

IGNORED_DIRS = {
    ".git", ".hg", ".svn", ".env", ".venv", "venv", "env", ".tox", ".nox",
    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "node_modules", "build", "dist", ".eggs", "site-packages",
}
GENERATED_SUFFIXES = ("_pb2.py", "_pb2_grpc.py")
GENERATED_MARKERS = (b"@generated", b"do not edit")
MAX_FILE_BYTES = 1_000_000
_INDEX_SUFFIXES = (".db", ".db-wal", ".db-shm", ".db-journal", ".sqlite", ".sqlite3")


@dataclass
class IndexReport:
    root: Path
    db_path: Path
    files_indexed: int = 0
    symbols_indexed: int = 0
    skipped: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[tuple[str, str]] = field(default_factory=list)
    mode: str = "full"  # full, incremental, or unchanged
    reason: str | None = None  # why a full rebuild happened
    added: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)


def index_repo(
    root: Path,
    db_path: Path,
    exclude: tuple[str, ...] = (),
    *,
    full: bool = False,
    force_paths: frozenset[str] = frozenset(),
) -> IndexReport:
    """Bring the index for `root` up to date.

    Unchanged files (same size and modification time) are kept. Changed, added and removed
    files are updated in one transaction. The whole index is rebuilt into a new file, which
    replaces the old one only after it is complete, when `full` is set or the index format,
    available parsers, exclusions or root differ from the existing index. `force_paths` are
    re-read even when their size and time look unchanged.
    """
    root = Path(root).resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"Not a directory: {root}")
    db_path = Path(db_path)
    report = IndexReport(root=root, db_path=db_path)
    expected = {
        "root": str(root),
        "exclude": storage.dumps(sorted(exclude)),
        "extractor": extractor_signature(),
        "in_place_updates": str(storage.CAN_UPDATE),
    }
    reason = "full rebuild requested" if full else storage.needs_rebuild(db_path, expected)
    if reason:
        _rebuild(root, db_path, exclude, expected, report)
        report.reason = reason
    else:
        _update(root, db_path, exclude, report, force_paths)
    report.files_indexed, report.symbols_indexed = storage.counts(db_path)
    if report.mode != "full":
        report.skipped, report.warnings = storage.diagnostics(db_path)
    return report


def _rebuild(root, db_path, exclude, expected, report) -> None:
    files: list[storage.FileRecord] = []
    skipped: list[storage.SkippedRecord] = []
    for path in _text_files(root, exclude, db_path):
        record = _process(path, root)
        _collect(record, files, skipped, report)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".explorer-", suffix=".db", dir=db_path.parent)
    os.close(fd)
    try:
        storage.write_index(Path(temporary), root, files, skipped, expected)
        os.replace(temporary, db_path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    report.mode = "full"


def _update(root, db_path, exclude, report, force_paths) -> None:
    indexed, skipped_before = storage.file_states(db_path)
    files: list[storage.FileRecord] = []
    skipped: list[storage.SkippedRecord] = []
    touched: list[tuple[str, int, int]] = []
    seen: set[str] = set()
    for path in _text_files(root, exclude, db_path):
        rel = path.relative_to(root).as_posix()
        seen.add(rel)
        stamp = _stamp(path)
        if rel not in force_paths:
            if rel in indexed and indexed[rel][:2] == stamp:
                continue
            if rel in skipped_before and skipped_before[rel] == stamp:
                continue
        data, reason, stamp = _read(path, root)
        if not reason and rel in indexed and indexed[rel][2] == hashlib.sha256(data).hexdigest():
            # Same content under a new timestamp, as after a checkout: keep the rows.
            if indexed[rel][:2] != stamp:
                touched.append((rel, *stamp))
            continue
        record = _record(rel, data, reason, stamp, path)
        (report.changed if rel in indexed or rel in skipped_before else report.added).append(rel)
        _collect(record, files, skipped, report)
    report.removed = sorted((set(indexed) | set(skipped_before)) - seen)
    if (files or report.removed) and not storage.CAN_UPDATE:
        # Without in-place deletes, only a rebuild keeps ranking identical.
        expected = storage.read_meta(db_path)
        expected = {k: expected[k] for k in ("root", "exclude", "extractor", "in_place_updates")}
        added, changed, removed = report.added, report.changed, report.removed
        report.skipped, report.warnings = [], []
        _rebuild(root, db_path, exclude, expected, report)
        report.reason = "files changed"
        report.added, report.changed, report.removed = added, changed, removed
    elif files or skipped or report.removed:
        storage.update_index(db_path, files, skipped, report.removed, touched)
        report.mode = "incremental"
    else:
        if touched:
            storage.update_index(db_path, [], [], [], touched)
        report.mode = "unchanged"


def _collect(record, files, skipped, report) -> None:
    if isinstance(record, storage.SkippedRecord):
        skipped.append(record)
        report.skipped.append((record.path, record.reason))
    else:
        files.append(record)
        if record.warning:
            report.warnings.append((record.path, record.warning))


def _stamp(path: Path) -> tuple[int, int]:
    try:
        st = path.stat()
    except OSError:
        return (-1, -1)
    return (st.st_size, st.st_mtime_ns)


def _process(path: Path, root: Path) -> storage.FileRecord | storage.SkippedRecord:
    data, reason, stamp = _read(path, root)
    return _record(path.relative_to(root).as_posix(), data, reason, stamp, path)


def _read(path: Path, root: Path) -> tuple[bytes, str | None, tuple[int, int]]:
    stamp = _stamp(path)
    data, reason = _load_file(path, root)
    return data, reason, stamp


def _record(rel, data, reason, stamp, path) -> storage.FileRecord | storage.SkippedRecord:
    if reason:
        return storage.SkippedRecord(rel, reason, *stamp)
    text = data.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    language = language_for(path)
    symbols, coverage, warning = extract(language, text)
    return storage.FileRecord(
        rel, hashlib.sha256(data).hexdigest(), symbols, text, language, coverage,
        stamp[0], stamp[1], warning,
    )


def _text_files(root: Path, exclude: tuple[str, ...], db_path: Path | None = None):
    specs: dict[Path, GitIgnoreSpec] = {}
    explicit = GitIgnoreSpec.from_lines(exclude)
    own_db = Path(db_path).resolve() if db_path else None
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        local_rules = []
        for name in (".gitignore", ".explorerignore"):
            ignore_file = here / name
            if ignore_file.is_file():
                try:
                    local_rules.extend(ignore_file.read_text(encoding="utf-8-sig").splitlines())
                except (OSError, UnicodeDecodeError):
                    pass
        local = GitIgnoreSpec.from_lines(local_rules)
        # Keep the rule's directory so nested patterns stay relative to that directory.
        parents = [(p, specs[p]) for p in here.parents if p in specs]
        parents.reverse()
        active = [(base, spec) for base, spec in parents + [(here, local)] if spec.patterns]
        specs[here] = local

        def ignored(candidate: Path, directory: bool = False, active=active) -> bool:
            decision = False
            for base, spec in active:
                result = spec.check_file(
                    candidate.relative_to(base).as_posix() + ("/" if directory else "")
                )
                if result.include is not None:
                    decision = result.include
            return decision or (
                bool(explicit.patterns)
                and explicit.match_file(
                    candidate.relative_to(root).as_posix() + ("/" if directory else "")
                )
            )

        dirnames[:] = sorted(
            d
            for d in dirnames
            if d not in IGNORED_DIRS
            and not d.endswith(".egg-info")
            and not (here / d / "pyvenv.cfg").exists()
            and not (here / d).is_symlink()
            and not ignored(here / d, True)
        )
        for name in sorted(filenames):
            if (
                name not in {".env", ".env.local"}
                and not name.startswith(".env.")
                and not name.endswith(_INDEX_SUFFIXES)
                and not ignored(here / name)
            ):
                path = here / name
                if own_db is not None and path.resolve() == own_db:
                    continue
                yield path


def _load_file(path: Path, root: Path) -> tuple[bytes, str | None]:
    """Return the file's bytes and None, or empty bytes and the reason to skip it."""
    if path.is_symlink() and not path.resolve().is_relative_to(root):
        return b"", "symlink outside repository"
    if path.name.endswith(GENERATED_SUFFIXES):
        return b"", "generated file"
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return b"", "larger than 1 MB"
        data = path.read_bytes()
    except OSError as exc:
        return b"", f"unreadable: {exc.strerror}"
    if b"\x00" in data:
        return b"", "binary file"
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return b"", "not UTF-8 text"
    for line in data[:2_000].lower().splitlines()[:10]:
        if not line.strip():
            continue
        if not re.match(rb"^\s*(#|//|/\*|\*|<!--|--)", line):
            break
        if any(marker in line for marker in GENERATED_MARKERS):
            return b"", "generated file"
    return data, None
