"""Walk a Python repository, extract symbols with the standard-library ast parser, and write the index."""

from __future__ import annotations

import ast
import hashlib
import io
import os
import tokenize
from dataclasses import dataclass, field
from pathlib import Path

from explorer import storage

IGNORED_DIRS = {
    ".git", ".hg", ".svn", ".env", ".venv", "venv", "env", ".tox", ".nox",
    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "node_modules", "build", "dist", ".eggs", "site-packages",
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


def index_repo(root: Path, db_path: Path) -> IndexReport:
    """Rebuild the index for `root` from scratch."""
    root = Path(root).resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"Not a directory: {root}")

    report = IndexReport(root=root, db_path=Path(db_path))
    files: list[storage.FileRecord] = []
    for path in _python_files(root):
        rel = path.relative_to(root).as_posix()
        data, reason = _load_file(path, root)
        if reason:
            report.skipped.append((rel, reason))
            continue
        try:
            symbols = _extract_symbols(data)
        except SyntaxError as exc:
            report.skipped.append((rel, f"syntax error on line {exc.lineno}"))
            continue
        files.append(storage.FileRecord(rel, hashlib.sha256(data).hexdigest(), symbols))
        report.symbols_indexed += len(symbols)

    storage.write_index(report.db_path, root, files)
    report.files_indexed = len(files)
    return report


def _python_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in IGNORED_DIRS
            and not d.endswith(".egg-info")
            and not (here / d / "pyvenv.cfg").exists()
        )
        for name in sorted(filenames):
            if name.endswith(".py"):
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
    header = data[:2_000].lower()
    if any(marker in header for marker in GENERATED_MARKERS):
        return b"", "generated file"
    return data, None


def _extract_symbols(source: bytes) -> list[storage.SymbolRecord]:
    """Top-level functions, top-level classes, and methods defined directly in those classes.

    Raises SyntaxError when the running Python can't parse the file.
    """
    source = source.removeprefix(b"\xef\xbb\xbf")  # a BOM shifts bytes, not lines
    tree = ast.parse(source)
    lines = source.split(b"\n")
    symbols: list[storage.SymbolRecord] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.append(_symbol(node, "function", None, lines))
        elif isinstance(node, ast.ClassDef):
            symbols.append(_symbol(node, "class", None, lines))
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    symbols.append(_symbol(child, "method", node.name, lines))
    return symbols


def _symbol(
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
    kind: str,
    class_name: str | None,
    lines: list[bytes],
) -> storage.SymbolRecord:
    start_line = min([node.lineno] + [d.lineno for d in node.decorator_list])
    end_line = node.end_lineno
    return storage.SymbolRecord(
        name=node.name,
        qualified_name=f"{class_name}.{node.name}" if class_name else node.name,
        kind=kind,
        signature=_signature(node, lines),
        start_line=start_line,
        end_line=end_line,
        docstring=ast.get_docstring(node, clean=True),
        source=b"\n".join(lines[start_line - 1 : end_line]).decode("utf-8"),
    )


def _signature(node: ast.AST, lines: list[bytes]) -> str:
    """The header text before the colon on one line, such as `def f(a, b) -> int`.

    ast reports column offsets in UTF-8 bytes, so the header is cut from bytes.
    """
    first = node.body[0]
    header = [lines[node.lineno - 1][node.col_offset :]]
    header += lines[node.lineno : first.lineno - 1]
    if first.lineno > node.lineno:
        header.append(lines[first.lineno - 1][: first.col_offset])
    else:
        header[0] = lines[node.lineno - 1][node.col_offset : first.col_offset]
    text = b"\n".join(header).decode("utf-8")
    return " ".join(text[: _header_colon(text)].split())


def _header_colon(text: str) -> int:
    """Index of the colon that ends a def or class header, skipping colons inside brackets."""
    depth = 0
    readline = io.StringIO(text).readline
    offsets = [0]
    for line in text.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    try:
        for tok in tokenize.generate_tokens(readline):
            if tok.type == tokenize.OP and tok.string in "([{":
                depth += 1
            elif tok.type == tokenize.OP and tok.string in ")]}":
                depth -= 1
            elif tok.type == tokenize.OP and tok.string == ":" and depth == 0:
                row, col = tok.start
                return offsets[row - 1] + col
    except tokenize.TokenError:
        pass
    return len(text)
