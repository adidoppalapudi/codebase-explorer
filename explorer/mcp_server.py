"""Read-only stdio MCP server with two tools, search_code and read_source."""

from __future__ import annotations

import argparse
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from explorer import retriever

INSTRUCTIONS = """\
Call search_code to find a function, class, or method, then read_source to read its lines.
Explain only lines that read_source returned, and cite them as path:start-end.
If search_code finds nothing, say the index has no evidence. Don't guess.
If read_source says a file changed, ask the user to run the index command again."""

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)


def build_server(db_path: Path) -> MCPServer:
    server = MCPServer("codebase-explorer", instructions=INSTRUCTIONS)

    @server.tool(annotations=READ_ONLY)
    def search_code(query: str, limit: int = 5) -> str:
        """Find functions, classes, and methods by name or keywords.

        Each line of the result holds one match, giving path:start-end, kind, qualified name,
        and signature.
        """
        try:
            matches = retriever.search_code(db_path, query, limit)
        except (OSError, ValueError, RuntimeError) as exc:
            raise ToolError(str(exc)) from exc
        if not matches:
            return "No matches."
        return "\n".join(
            f"{m.citation}  {m.kind}  {m.qualified_name}  {m.signature}" for m in matches
        )

    @server.tool(annotations=READ_ONLY)
    def read_source(path: str, start_line: int, end_line: int) -> str:
        """Read numbered source lines from an indexed file.

        Line numbers start at 1 and include both ends. The first line of the result is the
        citation. Fails if the file changed after indexing.
        """
        try:
            return retriever.read_source(db_path, path, start_line, end_line).render()
        except (OSError, ValueError, RuntimeError) as exc:
            raise ToolError(str(exc)) from exc

    return server


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True, help="index file from 'explorer index'")
    args = parser.parse_args()
    build_server(args.db.resolve()).run("stdio")


if __name__ == "__main__":
    main()
