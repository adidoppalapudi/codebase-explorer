"""Language detection and definition extraction. Failure leaves files text-searchable."""

from __future__ import annotations

import importlib
from functools import lru_cache
from pathlib import Path

from explorer.storage import SymbolRecord

EXTENSIONS = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cs": "csharp",
    ".rb": "ruby",
    ".php": "php",
    ".sql": "sql",
    ".sh": "shell",
    ".md": "markdown",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".html": "html",
    ".css": "css",
    ".vue": "vue",
}

# Tree-sitter grammar module and the function returning its language, per language.
GRAMMARS = {
    "javascript": ("tree_sitter_javascript", "language"),
    "typescript": ("tree_sitter_typescript", "language_typescript"),
    "tsx": ("tree_sitter_typescript", "language_tsx"),
    "go": ("tree_sitter_go", "language"),
    "java": ("tree_sitter_java", "language"),
    "rust": ("tree_sitter_rust", "language"),
    "csharp": ("tree_sitter_c_sharp", "language"),
}
STRUCTURED = ("python", *GRAMMARS)

# Bump when extraction rules change, so existing indexes are rebuilt.
EXTRACTOR_VERSION = "2"

_JS_KINDS = {
    "function_declaration": "function",
    "generator_function_declaration": "function",
    "class_declaration": "class",
    "abstract_class_declaration": "class",
    "method_definition": "method",
    "interface_declaration": "interface",
    "type_alias_declaration": "type",
    "enum_declaration": "enum",
}
_KINDS = {
    "javascript": _JS_KINDS,
    "typescript": _JS_KINDS,
    "tsx": _JS_KINDS,
    "go": {
        "function_declaration": "function",
        "method_declaration": "method",
        "type_alias": "type",
    },
    "java": {
        "class_declaration": "class",
        "interface_declaration": "interface",
        "enum_declaration": "enum",
        "record_declaration": "class",
        "annotation_type_declaration": "interface",
        "method_declaration": "method",
        "constructor_declaration": "method",
    },
    "rust": {
        "struct_item": "struct",
        "union_item": "struct",
        "enum_item": "enum",
        "trait_item": "trait",
        "type_item": "type",
        "mod_item": "module",
        "macro_definition": "macro",
    },
    "csharp": {
        "class_declaration": "class",
        "struct_declaration": "struct",
        "record_struct_declaration": "struct",
        "interface_declaration": "interface",
        "enum_declaration": "enum",
        "record_declaration": "class",
        "method_declaration": "method",
        "constructor_declaration": "method",
        "property_declaration": "property",
        "delegate_declaration": "type",
        "local_function_statement": "function",
    },
}
_FUNCTION_VALUES = {"arrow_function", "function_expression", "generator_function"}
_TOP_LEVEL_ONLY = {"constant", "variable"}


def language_for(path: Path) -> str:
    return EXTENSIONS.get(path.suffix.lower(), "text")


def extract(language: str, text: str) -> tuple[list[SymbolRecord], str, str | None]:
    try:
        if language == "python":
            from explorer.python_symbols import extract_symbols

            return extract_symbols(text.encode("utf-8")), "structure", None
        if language in GRAMMARS:
            return _extract_tree(language, text)
        return [], "text", None
    except (SyntaxError, ValueError, ImportError, RuntimeError, AttributeError) as exc:
        return [], "text", f"{language} structure unavailable: {exc}"


@lru_cache(maxsize=1)
def extractor_signature() -> str:
    """Changes when extraction rules or available parsers change, forcing a rebuild."""
    return f"{EXTRACTOR_VERSION}:" + ",".join(available_languages())


def available_languages() -> list[str]:
    """Languages whose definitions can be extracted in this installation."""
    return ["python", *(language for language in GRAMMARS if _usable(language))]


def _usable(language: str) -> bool:
    try:
        _parser(language)
    except Exception:  # noqa: BLE001 - any failure means the parser is unusable
        return False
    return True


@lru_cache(maxsize=16)
def _parser(language: str):
    from tree_sitter import Language, Parser

    module, function = GRAMMARS[language]
    grammar = importlib.import_module(module)
    return Parser(Language(getattr(grammar, function)()))


def _extract_tree(language: str, text: str):
    source = text.encode("utf-8")
    tree = _parser(language).parse(source)
    records: list[SymbolRecord] = []
    # Each entry: node, qualified name of the enclosing definition, its kind.
    pending = [(tree.root_node, None, None)]
    while pending:
        node, parent, parent_kind = pending.pop()
        scope, scope_kind = parent, parent_kind
        next_parent, next_kind = parent, parent_kind
        for kind, label, target, scope_only in _definitions(language, node, source, parent_kind):
            qualified = f"{scope}.{label}" if scope else label
            if scope_only:
                # A qualifier without a definition of its own, such as a Rust impl block
                # or a Go method's receiver type.
                scope, scope_kind = qualified, kind
                next_parent, next_kind = scope, scope_kind
                continue
            if kind in _TOP_LEVEL_ONLY and parent_kind is not None:
                continue
            records.append(_record(language, node, target, label, qualified, kind, source))
            next_parent, next_kind = qualified, kind
        pending.extend(
            (child, next_parent, next_kind) for child in reversed(node.named_children)
        )
    warning = (
        f"{language} contains syntax errors; outline may be incomplete"
        if tree.root_node.has_error
        else None
    )
    return records, "partial" if warning else "structure", warning


def _text(node, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8")


def _definitions(language: str, node, source: bytes, parent_kind: str | None):
    """Yield (kind, name, node whose body ends the signature, scope_only) for `node`."""
    kind = _KINDS[language].get(node.type)
    name = node.child_by_field_name("name")
    if language in ("javascript", "typescript", "tsx"):
        if node.type == "variable_declarator":
            value = node.child_by_field_name("value")
            if value is not None and value.type in _FUNCTION_VALUES and name is not None:
                yield "function", _text(name, source), value, False
            return
    elif language == "go":
        if node.type == "method_declaration" and name is not None:
            receiver = _go_receiver(node, source)
            if receiver:
                yield "scope", receiver, node, True
        elif node.type == "type_spec" and name is not None:
            shape = node.child_by_field_name("type")
            kind = {"struct_type": "struct", "interface_type": "interface"}.get(
                shape.type if shape is not None else "", "type"
            )
        elif node.type in ("const_spec", "var_spec"):
            kind = "constant" if node.type == "const_spec" else "variable"
            for label in node.children_by_field_name("name"):
                yield kind, _text(label, source), node, False
            return
    elif language == "rust":
        if node.type == "impl_item":
            target = node.child_by_field_name("type")
            if target is not None:
                yield "impl", _rust_type_name(target, source), node, True
            return
        if node.type in ("function_item", "function_signature_item"):
            kind = "method" if parent_kind in ("impl", "trait") else "function"
            if node.type == "function_signature_item" and kind == "function":
                return
        elif node.type in ("const_item", "static_item"):
            kind = "constant"
    if kind and name is not None:
        yield kind, _text(name, source), node, False


def _go_receiver(node, source: bytes) -> str | None:
    receiver = node.child_by_field_name("receiver")
    if receiver is None:
        return None
    stack = [receiver]
    while stack:
        current = stack.pop(0)
        if current.type == "type_identifier":
            return _text(current, source)
        stack.extend(current.named_children)
    return None


def _rust_type_name(node, source: bytes) -> str:
    if node.type == "generic_type":
        inner = node.child_by_field_name("type")
        return _rust_type_name(inner, source) if inner is not None else _text(node, source)
    if node.type == "scoped_type_identifier":
        name = node.child_by_field_name("name")
        return _text(name, source) if name is not None else _text(node, source)
    return _text(node, source)


def _record(language, node, target, label, qualified, kind, source) -> SymbolRecord:
    body = target.child_by_field_name("body")
    header_end = body.start_byte if body is not None else node.end_byte
    signature = " ".join(source[node.start_byte : header_end].decode("utf-8").split())
    start = node
    if language == "rust":
        # Attributes such as #[derive(...)] are siblings before the item.
        previous = node.prev_named_sibling
        while previous is not None and previous.type == "attribute_item":
            start, previous = previous, previous.prev_named_sibling
    return SymbolRecord(
        label,
        qualified,
        kind,
        signature[:1000],
        start.start_point.row + 1,
        node.end_point.row + 1,
        None,
        source[start.start_byte : node.end_byte].decode("utf-8"),
    )
