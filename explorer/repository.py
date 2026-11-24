"""Repository operations shared by terminal and MCP callers.

Repository IDs are hashes of canonical folder paths. Opening always refreshes the
index. Reads verify live file hashes; a changed folder must be opened again.
"""

import hashlib
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from threading import RLock

from explorer import indexer, retriever, storage
from explorer.archives import import_zip


class Repositories:
    def __init__(self, cache_dir: Path, legacy_db: Path | None = None):
        self.cache_dir = Path(cache_dir)
        self.legacy_db = legacy_db
        self._lock = RLock()

    def _db(self, repo_id: str | None) -> Path:
        if repo_id is None and self.legacy_db is not None:
            return Path(self.legacy_db)
        if (
            not repo_id
            or len(repo_id) != 64
            or any(c not in "0123456789abcdef" for c in repo_id)
        ):
            raise ValueError("Use the repo_id returned by open_repository.")
        db = self.cache_dir / f"{repo_id}.db"
        if not db.is_file():
            raise ValueError(
                "Unknown repository. Call open_repository with its folder first."
            )
        return db

    def open(self, folder: str, exclude: list[str] | None = None) -> dict:
        root = Path(folder).expanduser().resolve()
        with self._lock:
            archive = (
                import_zip(root, self.cache_dir / "archives")
                if root.is_file()
                else None
            )
            if archive:
                root = archive.root
            repo_id = hashlib.sha256(str(root).encode()).hexdigest()
            report = indexer.index_repo(
                root, self.cache_dir / f"{repo_id}.db", tuple(exclude or ())
            )
            result = self.overview(repo_id)
            result["root"] = str(root)
            result["files_indexed"] = report.files_indexed
            result["source_type"] = "zip" if archive else "folder"
            if archive:
                result["archive_sha256"] = archive.sha256
            return result

    def overview(self, repo_id: str | None = None) -> dict:
        with self._lock:
            db = self._db(repo_id)
            files, diagnostics = storage.inventory(db)
            manifests = {
                "package.json",
                "pyproject.toml",
                "Cargo.toml",
                "go.mod",
                "pom.xml",
                "Makefile",
                "Dockerfile",
                "CMakeLists.txt",
                "build.gradle",
                "Gemfile",
            }
            paths = {f["path"] for f in files}
            config = [
                f
                for f in files
                if Path(f["path"]).name in manifests
                or Path(f["path"]).name.lower().startswith("readme")
            ]
            evidence = []
            evidence_characters = 0
            evidence_omitted = []
            for f in config[:20]:
                if f["line_count"]:
                    rel, lines = retriever.verified_lines(db, f["path"])
                    selected = []
                    characters = 0
                    for line in lines[:40]:
                        if characters + len(line) + 1 > min(
                            4000, 16000 - evidence_characters
                        ):
                            break
                        selected.append(line)
                        characters += len(line) + 1
                    if not selected:
                        evidence_omitted.append(rel)
                        continue
                    evidence_characters += characters
                    excerpt = retriever.SourceExcerpt(rel, 1, len(selected), selected)
                    evidence.append(
                        {
                            "path": f["path"],
                            "citation": excerpt.citation,
                            "lines": excerpt.lines,
                        }
                    )
            return {
                "repo_id": repo_id,
                "files": len(files),
                "languages": dict(Counter(f["language"] for f in files)),
                "coverage": dict(Counter(f["coverage"] for f in files)),
                "directories": sorted({p.split("/")[0] for p in paths if "/" in p})[
                    :100
                ],
                "project_files": [f["path"] for f in config[:100]],
                "project_files_total": len(config),
                "entry_candidates": sorted(
                    p
                    for p in paths
                    if Path(p).name
                    in {
                        "__main__.py",
                        "main.py",
                        "app.py",
                        "main.go",
                        "main.rs",
                        "index.js",
                        "index.ts",
                        "main.ts",
                        "server.js",
                        "server.ts",
                        "Program.cs",
                    }
                )[:50],
                "evidence": evidence,
                "evidence_omitted": evidence_omitted,
                "diagnostics": diagnostics[:100],
                "diagnostics_total": len(diagnostics),
                "note": "Entry candidates are filename hints. Read project files to verify startup and test commands. Overview describes the last index; reopen after edits.",
            }

    def files(
        self,
        repo_id: str | None = None,
        path: str = "",
        language: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> dict:
        if offset < 0 or not 1 <= limit <= 100:
            raise ValueError(
                "offset must be nonnegative; limit must be between 1 and 100."
            )
        with self._lock:
            files, _ = storage.inventory(self._db(repo_id))
            files = [
                f
                for f in files
                if f["path"].startswith(path)
                and (language is None or f["language"] == language)
            ]
            end = offset + limit
            return {
                "files": files[offset:end],
                "total": len(files),
                "next_offset": end if end < len(files) else None,
            }

    def search(
        self,
        repo_id: str | None,
        query: str,
        limit: int = 5,
        kind: str | None = None,
        path: str | None = None,
        language: str | None = None,
    ) -> list[dict]:
        with self._lock:
            matches = retriever.search_code(
                self._db(repo_id), query, limit, kind, path, language
            )
            return [dict(asdict(m), citation=m.citation) for m in matches]

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
            db = self._db(repo_id)
            files, _ = storage.inventory(db)
            rel = retriever._normalize(path)
            file = next((f for f in files if f["path"] == rel), None)
            if file is None:
                raise retriever.SourceError(f"{rel} is not in the index.")
            target = file["line_count"] if end_line is None else end_line
            if file["line_count"] == 0 and start_line == 1 and end_line is None:
                retriever.verified_lines(db, rel)
                return {
                    "path": rel,
                    "lines": [],
                    "citation": None,
                    "rendered": f"{rel} is empty.",
                    "start_line": 1,
                    "end_line": 0,
                    "next_line": None,
                    "requested_end_line": 0,
                }
            if target > file["line_count"] or start_line < 1 or target < start_line:
                raise retriever.SourceError("Invalid line range for this indexed file.")
            end = min(target, start_line + max_lines - 1)
            excerpt = retriever.read_source(db, rel, start_line, end)
            while len("\n".join(excerpt.lines)) > 24000 and len(excerpt.lines) > 1:
                end -= 1
                excerpt = retriever.SourceExcerpt(
                    rel, start_line, end, excerpt.lines[:-1]
                )
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
            raise ValueError(
                "offset must be nonnegative; limit must be between 1 and 100."
            )
        with self._lock:
            db = self._db(repo_id)
            # Verify source freshness before presenting stored definitions.
            retriever.verified_lines(db, path)
            matches = storage.outline(db, retriever._normalize(path))
            files, _ = storage.inventory(db)
            file = next(f for f in files if f["path"] == retriever._normalize(path))
            return {
                "coverage": file["coverage"],
                "symbols": [
                    dict(asdict(m), citation=m.citation)
                    for m in matches[offset : offset + limit]
                ],
                "next_offset": offset + limit
                if offset + limit < len(matches)
                else None,
            }
