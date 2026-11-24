"""Extract Python definitions and byte-accurate signatures without executing source."""

from __future__ import annotations

import ast
import io
import tokenize

from explorer import storage


def extract_symbols(source: bytes) -> list[storage.SymbolRecord]:
    """Top-level functions, top-level classes, and methods defined directly in those classes.

    Raises SyntaxError when the running Python can't parse the file.
    """
    source = (
        source.removeprefix(b"\xef\xbb\xbf")
        .replace(b"\r\n", b"\n")
        .replace(b"\r", b"\n")
    )
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
