"""Search the index and read verified source lines. No LLM involved."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from explorer import storage
from explorer.storage import Match

KINDS = ("function", "class", "method", "interface", "type", "enum", "text")
MAX_RESULTS = 50
MAX_SOURCE_LINES = 400

_IDENTIFIER = re.compile(r"[^\W\d]\w*(\.[^\W\d]\w*)?", re.UNICODE)
_BOM = "\ufeff"


class SourceError(ValueError):
    """read_source refused the request."""


class StaleFileError(SourceError):
    """The file changed after indexing."""


@dataclass(frozen=True)
class SourceExcerpt:
    path: str
    start_line: int
    end_line: int
    lines: list[str]

    @property
    def citation(self) -> str:
        return f"{self.path}:{self.start_line}-{self.end_line}"

    def render(self) -> str:
        width = len(str(self.end_line))
        numbered = (
            f"{number:>{width}} | {text}"
            for number, text in enumerate(self.lines, start=self.start_line)
        )
        return "\n".join([self.citation, *numbered])


def search_code(
    db_path: Path,
    query: str,
    limit: int = 5,
    kind: str | None = None,
    path: str | None = None,
    language: str | None = None,
) -> list[Match]:
    """Exact name matches first, then full-text matches ranked by bm25.

    Returns an empty list when nothing matches.
    """
    if limit < 1:
        raise ValueError("limit must be at least 1")
    if kind is not None and kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    limit = min(limit, MAX_RESULTS)
    query = query.strip()
    storage.inventory(Path(db_path))  # Validate the index even for an empty query.

    results: list[Match] = []
    if kind != "text" and _IDENTIFIER.fullmatch(query):
        results = storage.search_names(
            Path(db_path), query, kind, path, limit, language
        )
    if len(results) < limit:
        for hit in storage.search_text(
            Path(db_path), query, kind, path, limit, language
        ):
            if hit not in results:
                results.append(hit)
    if kind in (None, "text"):
        for hit in storage.search_chunks(Path(db_path), query, path, limit, language):
            if not any(
                m.path == hit.path
                and m.start_line <= hit.start_line
                and m.end_line >= hit.end_line
                for m in results
            ):
                results.append(hit)
    return results[:limit]


def read_source(
    db_path: Path, path: str, start_line: int, end_line: int
) -> SourceExcerpt:
    """Return lines start_line through end_line of an indexed file.

    Line numbers start at 1 and include both ends. Raises SourceError for paths
    outside the index, bad ranges, and missing files. Raises StaleFileError when
    the file no longer matches the hash recorded at indexing.
    """
    if start_line < 1 or end_line < start_line:
        raise SourceError(f"Invalid line range {start_line}-{end_line}.")
    if end_line - start_line + 1 > MAX_SOURCE_LINES:
        raise SourceError(f"Request at most {MAX_SOURCE_LINES} lines at a time.")

    rel, lines = verified_lines(db_path, path)
    if end_line > len(lines):
        raise SourceError(
            f"{rel} has {len(lines)} lines, but the request asks for line {end_line}."
        )
    return SourceExcerpt(rel, start_line, end_line, lines[start_line - 1 : end_line])


def verified_lines(db_path: Path, path: str) -> tuple[str, list[str]]:
    """Read and verify an indexed file, including empty files. Newlines match the index."""
    rel = _normalize(path)
    root, indexed_hash = storage.indexed_file(Path(db_path), rel)
    file = (root / rel).resolve()
    if not file.is_relative_to(root):
        raise SourceError(f"{path} resolves outside the repository.")
    if indexed_hash is None:
        raise SourceError(f"{rel} is not in the index.")
    try:
        data = file.read_bytes()
    except FileNotFoundError:
        raise SourceError(f"{rel} no longer exists. Run 'index' again.") from None
    if hashlib.sha256(data).hexdigest() != indexed_hash:
        raise StaleFileError(f"{rel} changed after indexing. Run 'index' again.")

    lines = (
        data.decode("utf-8")
        .removeprefix(_BOM)
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .split("\n")
    )
    if lines[-1] == "":
        lines.pop()
    return rel, lines


def _normalize(path: str) -> str:
    """Check a repository-relative POSIX path and return it in the form the index stores."""
    if "\\" in path or "\x00" in path:
        raise SourceError(
            f"Use a repository-relative path with forward slashes, not {path!r}."
        )
    pure = PurePosixPath(path)
    if pure.is_absolute() or ".." in pure.parts:
        raise SourceError(f"{path} is outside the repository.")
    rel = pure.as_posix()
    if rel in ("", "."):
        raise SourceError("Pass a file path.")
    return rel
