"""Index repository text and optional language definitions into a replacement SQLite file."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from pathspec import GitIgnoreSpec

from explorer import storage
from explorer.languages import extract, language_for

IGNORED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".env",
    ".venv",
    "venv",
    "env",
    ".tox",
    ".nox",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "node_modules",
    "build",
    "dist",
    ".eggs",
    "site-packages",
}
GENERATED_SUFFIXES = ("_pb2.py", "_pb2_grpc.py")
GENERATED_MARKERS = (b"@generated", b"do not edit")
MAX_FILE_BYTES = 1_000_000


@dataclass
class IndexReport:
    root: Path
    db_path: Path
    files_indexed: int = 0
    symbols_indexed: int = 0
    skipped: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[tuple[str, str]] = field(default_factory=list)


def index_repo(root: Path, db_path: Path, exclude: tuple[str, ...] = ()) -> IndexReport:
    """Rebuild the index for `root` from scratch."""
    root = Path(root).resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"Not a directory: {root}")

    report = IndexReport(root=root, db_path=Path(db_path))
    files: list[storage.FileRecord] = []
    for path in _text_files(root, exclude):
        if path.resolve() == Path(db_path).resolve():
            continue
        rel = path.relative_to(root).as_posix()
        data, reason = _load_file(path, root)
        if reason:
            report.skipped.append((rel, reason))
            continue
        text = data.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
        language = language_for(path)
        symbols, coverage, warning = extract(language, text)
        if warning:
            report.warnings.append((rel, warning))
        files.append(
            storage.FileRecord(
                rel, hashlib.sha256(data).hexdigest(), symbols, text, language, coverage
            )
        )
        report.symbols_indexed += len(symbols)

    report.db_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=".explorer-", suffix=".db", dir=report.db_path.parent
    )
    os.close(fd)
    try:
        storage.write_index(
            Path(temporary), root, files, report.skipped + report.warnings
        )
        os.replace(temporary, report.db_path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    report.files_indexed = len(files)
    return report


def _text_files(root: Path, exclude: tuple[str, ...]):
    specs: dict[Path, GitIgnoreSpec] = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        local_rules = []
        for name in (".gitignore", ".explorerignore"):
            ignore_file = here / name
            if ignore_file.is_file():
                local_rules.extend(
                    ignore_file.read_text(encoding="utf-8-sig").splitlines()
                )
        local = GitIgnoreSpec.from_lines(local_rules)
        explicit = GitIgnoreSpec.from_lines(exclude)
        # Keep the rule's directory so nested patterns stay relative to that directory.
        parents = [(p, specs[p]) for p in here.parents if p in specs]
        parents.reverse()
        active = parents + [(here, local)]
        specs[here] = local

        def ignored(
            candidate: Path, directory: bool = False, active=active, explicit=explicit
        ) -> bool:
            decision = False
            for base, spec in active:
                result = spec.check_file(
                    candidate.relative_to(base).as_posix() + ("/" if directory else "")
                )
                if result.include is not None:
                    decision = result.include
            return decision or explicit.match_file(
                candidate.relative_to(root).as_posix() + ("/" if directory else "")
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
                not ignored(here / name)
                and name not in {".env", ".env.local"}
                and not name.startswith(".env.")
                and not name.endswith((".db", ".db-wal", ".db-shm"))
            ):
                yield here / name


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
