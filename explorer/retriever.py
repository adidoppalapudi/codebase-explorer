"""Search the index and read verified source lines. No LLM involved."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from explorer import storage
from explorer.storage import Match

KINDS = (
    "function", "class", "method", "interface", "type", "enum", "struct", "trait",
    "module", "macro", "property", "constant", "variable", "text",
)
MAX_RESULTS = 50
MAX_SOURCE_LINES = 400

_IDENTIFIER = re.compile(r"[^\W\d]\w*(\.[^\W\d]\w*)?", re.UNICODE)
_BOM = "\ufeff"


class SourceError(ValueError):
    """read_source refused the request."""


class StaleFileError(SourceError):
    """The file changed after indexing."""


class MissingFileError(SourceError):
    """An indexed file no longer exists."""


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
    storage.check(Path(db_path))  # Validate the index even for an empty query.

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
        raise MissingFileError(f"{rel} no longer exists. Run 'index' again.") from None
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


# ---------------------------------------------------------------- match evidence

_SUFFIXES = ("ations", "ation", "ings", "ing", "ions", "ion", "ments", "ment", "ers", "er", "ed", "es", "s")


def explain(db_path: Path, query: str, matches: list[Match]) -> list[dict]:
    """Describe why each match was returned, so the reader can judge its relevance.

    Each result gets `match_type` (exact_name, name, definition_text or file_text), the query
    words it contains and lacks, a `strength` summary, and for text matches up to three
    matching lines read from the verified file. Nothing is filtered out.
    """
    words = storage.query_words(query)
    terms = {w: storage.identifier_terms(w) for w in words}
    flat = list(dict.fromkeys(t for w in words for t in terms[w]))
    symbol_ids = [m.ref for m in matches if m.kind != "text" and m.ref is not None]
    chunk_ids = [m.ref for m in matches if m.kind == "text" and m.ref is not None]
    in_symbol = {t: storage.matching_rows(db_path, "symbols_fts", t, symbol_ids) for t in flat}
    in_name = {t: storage.matching_rows(db_path, "symbols_fts", t, symbol_ids, "name_terms") for t in flat}
    in_chunk = {t: storage.matching_rows(db_path, "chunks_fts", t, chunk_ids) for t in flat}
    wanted = query.strip().lower()
    files: dict[str, list[str] | None] = {}
    results = []
    for m in matches:
        text_hit = m.kind == "text"
        found = in_chunk if text_hit else in_symbol
        matched = [w for w in words if any(m.ref in found[t] for t in terms[w])]
        named = [] if text_hit else [w for w in words if any(m.ref in in_name[t] for t in terms[w])]
        exact = not text_hit and wanted in (
            m.qualified_name.lower(), m.qualified_name.rsplit(".", 1)[-1].lower()
        )
        if exact:
            match_type = "exact_name"
        elif named:
            match_type = "name"
        else:
            match_type = "file_text" if text_hit else "definition_text"
        coverage = len(matched) / len(words) if words else 0.0
        if exact or (words and len(named) == len(words)):
            strength = "strong"  # the name itself carries every query word
        elif coverage == 1 or len(named) >= len(words) / 2:
            strength = "partial"
        else:
            strength = "weak"
        snippet = []
        if match_type in ("file_text", "definition_text") and matched:
            snippet = _snippet(db_path, m, [t for w in matched for t in terms[w]], files)
        results.append({
            "match_type": match_type,
            "strength": strength,
            "matched_words": matched,
            "missing_words": [w for w in words if w not in matched],
            "snippet": snippet,
        })
    return results


def _snippet(db_path: Path, match: Match, terms: list[str], files: dict) -> list[dict]:
    if match.path not in files:
        try:
            files[match.path] = verified_lines(db_path, match.path)[1]
        except SourceError:
            files[match.path] = None
    lines = files[match.path]
    if not lines:
        return []
    stems = {_stem(t) for t in terms}
    found = []
    for number in range(match.start_line, min(match.end_line, len(lines)) + 1):
        lowered = lines[number - 1].lower()
        if any(stem in lowered for stem in stems):
            found.append({"line": number, "text": lines[number - 1].strip()[:160]})
            if len(found) == 3:
                break
    return found


def _stem(term: str) -> str:
    """A rough stem for highlighting lines, close to what the porter tokenizer matched."""
    for suffix in _SUFFIXES:
        if len(term) - len(suffix) >= 4 and term.endswith(suffix):
            return term[: -len(suffix)]
    return term
