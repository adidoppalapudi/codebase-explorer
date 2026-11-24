"""Extract Python definitions and byte-accurate signatures without executing source."""

from __future__ import annotations

import ast
import io
import re
import tokenize

from explorer import storage

# Statements whose bodies can hold definitions without changing the enclosing scope.
_BLOCKS = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try)
_TRY_STAR = getattr(ast, "TryStar", None)
_MATCH = getattr(ast, "Match", None)
_TYPE_ALIAS = getattr(ast, "TypeAlias", None)
_CONSTANT_NAME = re.compile(r"^_*[A-Z][A-Z0-9_]*$")


def extract_symbols(source: bytes) -> list[storage.SymbolRecord]:
    """Functions, classes, methods and module-level names, including nested definitions.

    Methods are functions defined directly in a class. Nested definitions are qualified by
    their enclosing definitions, such as `outer.inner` or `Outer.Inner.method`. Module-level
    assignments become constants (UPPER_CASE names) or variables. Definitions inside if,
    for, while, with, try and match blocks belong to the enclosing scope.

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
    _visit(tree.body, None, "module", lines, symbols)
    return symbols


def _visit(body, parent: str | None, scope: str, lines, symbols) -> None:
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            kind = "method" if scope == "class" else "function"
            record = _symbol(node, kind, parent, lines)
            symbols.append(record)
            _visit(node.body, record.qualified_name, "function", lines, symbols)
        elif isinstance(node, ast.ClassDef):
            record = _symbol(node, "class", parent, lines)
            symbols.append(record)
            _visit(node.body, record.qualified_name, "class", lines, symbols)
        elif scope == "module" and isinstance(node, (ast.Assign, ast.AnnAssign)):
            symbols.extend(_assignments(node, lines))
        elif scope == "module" and _TYPE_ALIAS and isinstance(node, _TYPE_ALIAS):
            symbols.append(_named_statement(node, node.name.id, "type", lines))
        else:
            for block in _blocks(node):
                _visit(block, parent, scope, lines, symbols)


def _blocks(node: ast.AST):
    if isinstance(node, _BLOCKS) or (_TRY_STAR and isinstance(node, _TRY_STAR)):
        for name in ("body", "orelse", "finalbody"):
            yield getattr(node, name, [])
        for handler in getattr(node, "handlers", []):
            yield handler.body
    elif _MATCH and isinstance(node, _MATCH):
        for case in node.cases:
            yield case.body


def _assignments(node: ast.Assign | ast.AnnAssign, lines) -> list[storage.SymbolRecord]:
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    names: list[str] = []
    for target in targets:
        if isinstance(target, ast.Name):
            names.append(target.id)
        elif isinstance(target, (ast.Tuple, ast.List)):
            names.extend(e.id for e in target.elts if isinstance(e, ast.Name))
    return [
        _named_statement(node, name, "constant" if _CONSTANT_NAME.match(name) else "variable", lines)
        for name in dict.fromkeys(names)
        if name != "__all__"
    ]


def _named_statement(node: ast.stmt, name: str, kind: str, lines) -> storage.SymbolRecord:
    start, end = node.lineno, node.end_lineno
    text = lines[start - 1][node.col_offset :].decode("utf-8")
    return storage.SymbolRecord(
        name=name,
        qualified_name=name,
        kind=kind,
        signature=" ".join(text.split())[:200],
        start_line=start,
        end_line=end,
        docstring=None,
        source=b"\n".join(lines[start - 1 : end]).decode("utf-8"),
    )


def _symbol(
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
    kind: str,
    parent: str | None,
    lines: list[bytes],
) -> storage.SymbolRecord:
    start_line = min([node.lineno] + [d.lineno for d in node.decorator_list])
    end_line = node.end_lineno
    return storage.SymbolRecord(
        name=node.name,
        qualified_name=f"{parent}.{node.name}" if parent else node.name,
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
