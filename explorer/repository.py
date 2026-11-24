"""Repository operations shared by terminal and MCP callers.

Repository IDs are hashes of canonical folder paths. When no ID is given, calls use the
repository opened most recently, or else the default folder (the project the server or
command was started in). Before each call the index is brought up to date, at most every
few seconds, so search reflects edits without an explicit refresh. Reads verify the file's
hash; a file that changed since indexing is re-indexed and read again.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from collections import Counter
from dataclasses import asdict
from pathlib import Path, PureWindowsPath
from threading import RLock

from explorer import indexer, languages, retriever, storage
from explorer.archives import import_zip

PROJECT_MARKERS = {
    ".git", ".hg", ".svn", "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt",
    "package.json", "go.mod", "Cargo.toml", "pom.xml", "build.gradle", "build.gradle.kts",
    "Gemfile", "composer.json", "Makefile", "CMakeLists.txt", "Dockerfile", "deno.json",
}
PROJECT_SUFFIXES = (".sln", ".csproj")
MANIFESTS = {
    "package.json", "pyproject.toml", "Cargo.toml", "go.mod", "pom.xml", "Makefile",
    "Dockerfile", "CMakeLists.txt", "build.gradle", "Gemfile",
}
ENTRY_NAMES = {
    "__main__.py", "main.py", "app.py", "main.go", "main.rs", "index.js", "index.ts",
    "main.ts", "server.js", "server.ts", "Program.cs", "Main.java", "lib.rs",
}
NO_REPOSITORY = (
    "No repository is open. Call open_repository with the project's folder or ZIP path, "
    "or start the server inside a project folder."
)
REFRESHED_NOTE = (
    "This file changed since it was indexed. The index was updated and these lines are "
    "current, but line numbers from earlier results may have moved; search again if needed."
)
_LEGACY = "legacy"
_IDENTIFIER = re.compile(r"[^\W\d]\w*")
_IMPORT_LINE = re.compile(
    r"^\s*(import\b|from\s+\S+\s+import\b|use\s|using\s|#\s*include\b|export\s*[{*]|require\b)"
    r"|\brequire\s*\("
)
_COMMENT_LINE = re.compile(r"^\s*(#|//|/\*|\*|--|<!--)")
_MAX_REFERENCE_FILES = 2000


def looks_like_project(folder: Path) -> bool:
    """True for a folder with a project marker that isn't the home folder or a drive root."""
    folder = Path(folder)
    try:
        folder = folder.resolve()
        if folder.parent == folder or folder == Path.home().resolve():
            return False
        names = {entry.name for entry in folder.iterdir()}
    except OSError:
        return False
    return bool(names & PROJECT_MARKERS) or any(
        n.endswith(PROJECT_SUFFIXES) or n.lower().startswith("readme") for n in names
    )


def default_folder(explicit: str | None = None, cwd: Path | None = None) -> str | None:
    """The folder to open when a call names no repository.

    An explicit --repo wins, then CODEBASE_EXPLORER_REPO, then CLAUDE_PROJECT_DIR, then the
    working directory when it looks like a project.
    """
    for candidate in (explicit, os.environ.get("CODEBASE_EXPLORER_REPO"),
                      os.environ.get("CLAUDE_PROJECT_DIR")):
        if candidate:
            return candidate
    cwd = Path.cwd() if cwd is None else cwd
    return str(cwd) if looks_like_project(cwd) else None


class Repositories:
    def __init__(
        self,
        cache_dir: Path,
        legacy_db: Path | None = None,
        *,
        default: str | None = None,
        auto_refresh: bool = True,
        refresh_interval: float = 2.0,
    ):
        self.cache_dir = Path(cache_dir)
        self.legacy_db = legacy_db
        self.default = default
        self.auto_refresh = auto_refresh
        self.refresh_interval = refresh_interval
        self._current: str | None = None
        self._next_refresh: dict[str, float] = {}
        self._refresh_error: dict[str, str] = {}
        self._lock = RLock()

    # ------------------------------------------------------------ resolving repositories

    def _db(self, repo_id: str | None) -> tuple[str, Path]:
        if repo_id is None:
            if self.legacy_db is not None:
                return _LEGACY, Path(self.legacy_db)
            if self._current is None:
                if not self.default:
                    raise ValueError(NO_REPOSITORY)
                self.open(self.default)
            repo_id = self._current
        if (
            not repo_id
            or len(repo_id) != 64
            or any(c not in "0123456789abcdef" for c in repo_id)
        ):
            raise ValueError("Use the repo_id returned by open_repository, or omit it.")
        db = self.cache_dir / f"{repo_id}.db"
        if not db.is_file():
            raise ValueError("Unknown repository. Call open_repository with its folder first.")
        return repo_id, db

    def _fresh(self, key: str, db: Path, force_paths: frozenset[str] = frozenset()) -> None:
        """Bring the index up to date unless it was refreshed moments ago."""
        if not self.auto_refresh:
            return
        now = time.monotonic()
        if not force_paths and now < self._next_refresh.get(key, 0.0):
            return
        meta = storage.read_meta(db)
        if "root" not in meta:
            return
        exclude = tuple(json.loads(meta.get("exclude", "[]")))
        started = time.monotonic()
        try:
            indexer.index_repo(Path(meta["root"]), db, exclude, force_paths=force_paths)
            self._refresh_error.pop(key, None)
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
            if force_paths:
                raise
            self._refresh_error[key] = f"Could not refresh the index: {exc}"
        took = time.monotonic() - started
        # Large repositories refresh less often, so refreshing stays a small share of the time.
        interval = max(self.refresh_interval, 10 * took) if self.refresh_interval else 0.0
        self._next_refresh[key] = time.monotonic() + interval

    def _ready(self, repo_id: str | None) -> tuple[str, Path]:
        with self._lock:
            key, db = self._db(repo_id)
            self._fresh(key, db)
            return key, db

    def _warn(self, key: str, result: dict) -> dict:
        if key in self._refresh_error:
            result["index_warning"] = self._refresh_error[key]
        return result

    # ------------------------------------------------------------ operations

    def open(self, folder: str, exclude: list[str] | None = None, full: bool = False) -> dict:
        root = Path(folder).expanduser().resolve()
        if root.is_dir() and (root.parent == root or root == Path.home().resolve()):
            raise ValueError(
                f"{root} is a home folder or drive root, which is too broad. Open a project folder."
            )
        with self._lock:
            archive = (
                import_zip(root, self.cache_dir / "archives") if root.is_file() else None
            )
            if archive:
                root = archive.root
            repo_id = hashlib.sha256(str(root).encode()).hexdigest()
            db = self.cache_dir / f"{repo_id}.db"
            report = indexer.index_repo(root, db, tuple(exclude or ()), full=full)
            self._refresh_error.pop(repo_id, None)
            self._next_refresh[repo_id] = time.monotonic() + self.refresh_interval
            self._current = repo_id
            result = self.overview(repo_id)
            result["root"] = str(root)
            result["files_indexed"] = report.files_indexed
            result["symbols_indexed"] = report.symbols_indexed
            result["source_type"] = "zip" if archive else "folder"
            result["index"] = _index_summary(report)
            if archive:
                result["archive_sha256"] = archive.sha256
            return result

    def overview(self, repo_id: str | None = None) -> dict:
        with self._lock:
            key, db = self._ready(repo_id)
            files, diagnostics = storage.inventory(db)
            paths = {f["path"] for f in files}
            config = [
                f for f in files
                if Path(f["path"]).name in MANIFESTS
                or Path(f["path"]).name.lower().startswith("readme")
            ]
            evidence, evidence_omitted, evidence_characters = [], [], 0
            for f in config[:20]:
                if not f["line_count"]:
                    continue
                try:
                    rel, lines = retriever.verified_lines(db, f["path"])
                except retriever.SourceError:
                    evidence_omitted.append(f["path"])
                    continue
                selected, characters = [], 0
                for line in lines[:40]:
                    if characters + len(line) + 1 > min(4000, 16000 - evidence_characters):
                        break
                    selected.append(line)
                    characters += len(line) + 1
                if not selected:
                    evidence_omitted.append(rel)
                    continue
                evidence_characters += characters
                excerpt = retriever.SourceExcerpt(rel, 1, len(selected), selected)
                evidence.append(
                    {"path": f["path"], "citation": excerpt.citation, "lines": excerpt.lines}
                )
            meta = storage.read_meta(db)
            return self._warn(key, {
                "repo_id": None if key == _LEGACY else key,
                "root": meta.get("root"),
                "files": len(files),
                "languages": dict(Counter(f["language"] for f in files)),
                "coverage": dict(Counter(f["coverage"] for f in files)),
                "structure_languages": languages.available_languages(),
                "directories": sorted({p.split("/")[0] for p in paths if "/" in p})[:100],
                "project_files": [f["path"] for f in config[:100]],
                "project_files_total": len(config),
                "entry_candidates": sorted(p for p in paths if Path(p).name in ENTRY_NAMES)[:50],
                "evidence": evidence,
                "evidence_omitted": evidence_omitted,
                "diagnostics": diagnostics[:100],
                "diagnostics_total": len(diagnostics),
                "note": (
                    "Entry candidates are filename hints. Read project files to verify startup "
                    "and test commands. The index refreshes automatically before each call."
                ),
            })

    def files(
        self,
        repo_id: str | None = None,
        path: str = "",
        language: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> dict:
        if offset < 0 or not 1 <= limit <= 100:
            raise ValueError("offset must be nonnegative; limit must be between 1 and 100.")
        with self._lock:
            key, db = self._ready(repo_id)
            rows, total = storage.list_files(db, path.replace("\\", "/"), language, offset, limit)
            end = offset + limit
            return self._warn(key, {
                "files": rows,
                "total": total,
                "next_offset": end if end < total else None,
            })

    def search(
        self,
        repo_id: str | None,
        query: str,
        limit: int = 5,
        kind: str | None = None,
        path: str | None = None,
        language: str | None = None,
    ) -> list[dict]:
        return self.search_report(repo_id, query, limit, kind, path, language)["matches"]

    def search_report(
        self,
        repo_id: str | None,
        query: str,
        limit: int = 5,
        kind: str | None = None,
        path: str | None = None,
        language: str | None = None,
    ) -> dict:
        with self._lock:
            key, db = self._ready(repo_id)
            if path:
                path = path.replace("\\", "/").removeprefix("./")
            matches = retriever.search_code(db, query, limit, kind, path, language)
            evidence = retriever.explain(db, query, matches)
            results = []
            for m, why in zip(matches, evidence):
                record = asdict(m)
                record.pop("ref", None)
                results.append(dict(record, citation=m.citation, **why))
            return self._warn(key, {
                "matches": results,
                "query_words": storage.query_words(query),
            })

    def read(
        self,
        repo_id: str | None,
        path: str,
        start_line: int = 1,
        end_line: int | None = None,
        max_lines: int = 100,
    ) -> dict:
        if not 1 <= max_lines <= retriever.MAX_SOURCE_LINES:
            raise ValueError("max_lines must be between 1 and 400.")
        with self._lock:
            key, db = self._ready(repo_id)
            rel, file = self._resolve_path(db, path)
            refreshed = False
            try:
                result = self._read(db, rel, file, start_line, end_line, max_lines)
            except (retriever.StaleFileError, retriever.MissingFileError):
                if not self.auto_refresh:
                    raise
                self._fresh(key, db, frozenset({rel}))
                refreshed = True
                file = storage.file_info(db, rel)
                if file is None:
                    raise retriever.SourceError(f"{rel} no longer exists.") from None
                result = self._read(db, rel, file, start_line, end_line, max_lines)
            if refreshed:
                result["refreshed"] = True
                result["note"] = REFRESHED_NOTE
            return self._warn(key, result)

    def _read(self, db, rel, file, start_line, end_line, max_lines) -> dict:
        target = file["line_count"] if end_line is None else end_line
        if file["line_count"] == 0 and start_line == 1 and end_line is None:
            retriever.verified_lines(db, rel)
            return {
                "path": rel, "lines": [], "citation": None, "rendered": f"{rel} is empty.",
                "start_line": 1, "end_line": 0, "next_line": None, "requested_end_line": 0,
            }
        if target > file["line_count"] or start_line < 1 or target < start_line:
            raise retriever.SourceError(
                f"Invalid line range {start_line}-{target}: {rel} has {file['line_count']} lines."
            )
        end = min(target, start_line + max_lines - 1)
        excerpt = retriever.read_source(db, rel, start_line, end)
        while len("\n".join(excerpt.lines)) > 24000 and len(excerpt.lines) > 1:
            end -= 1
            excerpt = retriever.SourceExcerpt(rel, start_line, end, excerpt.lines[:-1])
        if len("\n".join(excerpt.lines)) > 24000:
            raise retriever.SourceError(
                "This source line exceeds the 24,000 character display limit."
            )
        return dict(
            asdict(excerpt),
            citation=excerpt.citation,
            rendered=excerpt.render(),
            next_line=end + 1 if end < target else None,
            requested_end_line=target,
        )

    def outline(
        self, repo_id: str | None, path: str, offset: int = 0, limit: int = 50
    ) -> dict:
        if offset < 0 or not 1 <= limit <= 100:
            raise ValueError("offset must be nonnegative; limit must be between 1 and 100.")
        with self._lock:
            key, db = self._ready(repo_id)
            rel, file = self._resolve_path(db, path)
            refreshed = False
            try:
                # Verify source freshness before presenting stored definitions.
                retriever.verified_lines(db, rel)
            except (retriever.StaleFileError, retriever.MissingFileError):
                if not self.auto_refresh:
                    raise
                self._fresh(key, db, frozenset({rel}))
                refreshed = True
                file = storage.file_info(db, rel)
                if file is None:
                    raise retriever.SourceError(f"{rel} no longer exists.") from None
            matches = storage.outline(db, rel)
            result = {
                "path": rel,
                "coverage": file["coverage"],
                "symbols": [
                    dict(_public(m), citation=m.citation) for m in matches[offset : offset + limit]
                ],
                "total": len(matches),
                "next_offset": offset + limit if offset + limit < len(matches) else None,
            }
            if refreshed:
                result["refreshed"] = True
            return self._warn(key, result)

    def references(
        self,
        repo_id: str | None,
        name: str,
        path: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> dict:
        """Where a name is used: whole-word matches across indexed files, with context.

        Text-based. Same-named things elsewhere are included, so each hit names the
        enclosing definition. The definition lines themselves are listed separately.
        """
        if offset < 0 or not 1 <= limit <= 200:
            raise ValueError("offset must be nonnegative; limit must be between 1 and 200.")
        word = name.strip().removesuffix("()").rsplit(".", 1)[-1].rsplit("::", 1)[-1]
        if not _IDENTIFIER.fullmatch(word):
            raise ValueError("Pass a function, class, method or variable name.")
        with self._lock:
            key, db = self._ready(repo_id)
            prefix = path.replace("\\", "/").removeprefix("./") if path else None
            chunks = storage.phrase_chunks(db, word, prefix, 100_000)
            candidates = list(dict.fromkeys(p for p, _, _ in chunks))
            truncated = len(candidates) > _MAX_REFERENCE_FILES
            pattern = re.compile(rf"(?<!\w){re.escape(word)}(?!\w)")
            definitions, uses = [], []
            for rel in candidates[:_MAX_REFERENCE_FILES]:
                try:
                    _, lines = retriever.verified_lines(db, rel)
                except (retriever.StaleFileError, retriever.MissingFileError):
                    if not self.auto_refresh:
                        raise
                    self._fresh(key, db, frozenset({rel}))
                    try:
                        _, lines = retriever.verified_lines(db, rel)
                    except retriever.SourceError:
                        continue
                symbols = storage.outline(db, rel)
                defining = {}
                for s in symbols:
                    if s.qualified_name.rsplit(".", 1)[-1] == word:
                        for number in range(s.start_line, min(s.end_line, len(lines)) + 1):
                            if pattern.search(lines[number - 1]):
                                defining[number] = s
                                break
                for number, line in enumerate(lines, 1):
                    found = pattern.search(line)
                    if not found:
                        continue
                    text = line.strip()[:200]
                    if number in defining:
                        s = defining[number]
                        definitions.append({
                            "path": rel, "line": number, "text": text, "kind": s.kind,
                            "qualified_name": s.qualified_name, "citation": s.citation,
                        })
                        continue
                    enclosing = _innermost(symbols, number)
                    kind = "import" if _in_import_block(lines, number) else _use_kind(line, found.end())
                    uses.append({
                        "path": rel,
                        "line": number,
                        "text": text,
                        "kind": kind,
                        "in": enclosing.qualified_name if enclosing else None,
                        "in_citation": enclosing.citation if enclosing else None,
                    })
            end = offset + limit
            return self._warn(key, {
                "name": word,
                "definitions": definitions,
                "references": uses[offset:end],
                "total": len(uses),
                "next_offset": end if end < len(uses) else None,
                "files_scanned": min(len(candidates), _MAX_REFERENCE_FILES),
                "truncated": truncated,
                "note": (
                    "Whole-word text matches of the name in indexed files. Things with the same "
                    "name elsewhere are included; use `in` and imports to tell them apart. "
                    "Calls made through other names or generated code are not found."
                ),
            })

    # ------------------------------------------------------------ paths

    def _resolve_path(self, db: Path, path: str) -> tuple[str, dict]:
        """Map what a caller passed to an indexed repository-relative path.

        Accepts forward or back slashes, a leading ./, and absolute paths inside the
        repository. Falls back to a case-insensitive match when it is unambiguous.
        """
        raw = (path or "").strip().strip('"').strip("'")
        if not raw:
            raise retriever.SourceError("Pass a file path.")
        root = Path(storage.read_meta(db).get("root", ""))
        text = raw.replace("\\", "/")
        drive = bool(re.match(r"^[A-Za-z]:/", text)) or text.startswith("//")
        if Path(raw).is_absolute() or text.startswith("/") or drive:
            text = _inside(raw, root)
        while text.startswith("./"):
            text = text[2:]
        rel = retriever._normalize(text)
        file = storage.file_info(db, rel)
        if file is None:
            similar = storage.similar_paths(db, rel)
            if len(similar) == 1 and similar[0].lower() == rel.lower():
                rel, file = similar[0], storage.file_info(db, similar[0])
            else:
                hint = f" Did you mean: {', '.join(similar)}?" if similar else ""
                raise retriever.SourceError(f"{rel} is not in the index.{hint}")
        return rel, file


def _inside(raw: str, root: Path) -> str:
    """The part of an absolute path below `root`, or an error if it is outside."""
    outside = retriever.SourceError(f"{raw} is outside the repository at {root}.")
    if not str(root):
        raise outside
    candidate = Path(raw)
    if candidate.is_absolute():
        try:
            return candidate.resolve().relative_to(root).as_posix()
        except (ValueError, OSError):
            pass
        base, full = os.path.normcase(str(root)), os.path.normcase(os.path.abspath(raw))
        if full.startswith(base.rstrip(os.sep) + os.sep):
            return os.path.abspath(raw)[len(base.rstrip(os.sep)) + 1 :].replace(os.sep, "/")
        raise outside
    # A Windows path given to a server on another system: compare as text.
    windows_root, windows_raw = PureWindowsPath(str(root)), PureWindowsPath(raw)
    try:
        return windows_raw.relative_to(windows_root).as_posix()
    except ValueError:
        raise outside from None


def _public(match: storage.Match) -> dict:
    record = asdict(match)
    record.pop("ref", None)
    return record


def _innermost(symbols: list[storage.Match], line: int) -> storage.Match | None:
    containing = [s for s in symbols if s.start_line <= line <= s.end_line]
    return min(containing, key=lambda s: s.end_line - s.start_line, default=None)


def _in_import_block(lines: list[str], number: int) -> bool:
    """True inside a parenthesized multi-line import, such as `from x import (` ... `)`."""
    for index in range(number - 2, max(number - 60, 0) - 1, -1):
        previous = lines[index]
        if ")" in previous:
            return False
        if re.match(r"^\s*(from\s+\S+\s+)?import\s*\(", previous) or re.match(
            r"^\s*import\s*\($|^\s*use\s+\S*\{\s*$", previous
        ):
            return True
    return False


def _use_kind(line: str, end: int) -> str:
    if _IMPORT_LINE.search(line):
        return "import"
    if _COMMENT_LINE.match(line):
        return "comment"
    after = line[end:].lstrip()
    if after.startswith(("(", "!(")) or re.match(r"^(\[[^\]]*\]|<[^>]*>)\s*\(", after):
        return "call"
    return "reference"


def _index_summary(report: indexer.IndexReport) -> dict:
    summary = {
        "mode": report.mode,
        "added": len(report.added),
        "changed": len(report.changed),
        "removed": len(report.removed),
    }
    if report.reason:
        summary["reason"] = report.reason
    return summary
