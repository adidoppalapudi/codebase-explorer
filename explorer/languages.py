"""Language detection and definition extraction. Failure leaves files text-searchable."""

from functools import lru_cache
from pathlib import Path

from explorer.storage import SymbolRecord

EXTENSIONS = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
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


def language_for(path: Path) -> str:
    return EXTENSIONS.get(path.suffix.lower(), "text")


def extract(language: str, text: str) -> tuple[list[SymbolRecord], str, str | None]:
    try:
        if language == "python":
            from explorer.python_symbols import extract_symbols

            return extract_symbols(text.encode("utf-8")), "structure", None
        if language in {"javascript", "typescript", "tsx"}:
            return _extract_tree(language, text)
        return [], "text", None
    except (SyntaxError, ValueError, ImportError, RuntimeError) as exc:
        return [], "text", f"{language} structure unavailable: {exc}"


@lru_cache(maxsize=3)
def _parser(language: str):
    from tree_sitter import Language, Parser

    if language == "javascript":
        import tree_sitter_javascript as grammar

        capsule = grammar.language()
    else:
        import tree_sitter_typescript as grammar

        capsule = (
            grammar.language_tsx()
            if language == "tsx"
            else grammar.language_typescript()
        )
    return Parser(Language(capsule))


def _extract_tree(language: str, text: str):
    source = text.encode("utf-8")
    tree = _parser(language).parse(source)
    records = []
    kinds = {
        "function_declaration": "function",
        "generator_function_declaration": "function",
        "class_declaration": "class",
        "abstract_class_declaration": "class",
        "method_definition": "method",
        "interface_declaration": "interface",
        "type_alias_declaration": "type",
        "enum_declaration": "enum",
    }
    pending = [(tree.root_node, None)]
    while pending:
        node, parent = pending.pop()
        kind = kinds.get(node.type)
        name = node.child_by_field_name("name")
        target = node
        if node.type == "variable_declarator":
            value = node.child_by_field_name("value")
            if value and value.type in {
                "arrow_function",
                "function_expression",
                "generator_function",
            }:
                kind, target = "function", value
        next_parent = parent
        if kind and name:
            label = source[name.start_byte : name.end_byte].decode("utf-8")
            qualified = f"{parent}.{label}" if parent else label
            body = target.child_by_field_name("body")
            header_end = body.start_byte if body else node.end_byte
            signature = " ".join(
                source[node.start_byte : header_end].decode("utf-8").split()
            )
            records.append(
                SymbolRecord(
                    label,
                    qualified,
                    kind,
                    signature[:1000],
                    node.start_point.row + 1,
                    node.end_point.row + 1,
                    None,
                    source[node.start_byte : node.end_byte].decode("utf-8"),
                )
            )
            next_parent = qualified
        pending.extend((child, next_parent) for child in reversed(node.named_children))
    warning = (
        f"{language} contains syntax errors; outline may be incomplete"
        if tree.root_node.has_error
        else None
    )
    return records, "partial" if warning else "structure", warning
