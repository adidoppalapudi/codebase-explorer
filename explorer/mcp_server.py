"""Local stdio MCP tools for exploring repositories. Source files are never executed."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from explorer.repository import Repositories

INSTRUCTIONS = """Open an accessible local folder or ZIP file with open_repository, then retain its repo_id
in every subsequent call. Start with repo_overview or list_files for orientation.
Search names, documentation, configuration and source using search_code. Read evidence
with read_source before explaining implementation; cite path:start-end. Follow next_line
and next_offset to continue. Filename-based entry candidates are hints, not verified facts.
No matches means no indexed evidence, not proof of absence. Inspect coverage and diagnostics.
Reopen the same folder after edits to refresh a stale index. Do not execute repository code."""

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)


def build_server(
    db_path: Path | None = None, cache_dir: Path | None = None
) -> MCPServer:
    repositories = Repositories(
        cache_dir or Path.home() / ".cache" / "codebase-explorer", db_path
    )
    server = MCPServer("codebase-explorer", instructions=INSTRUCTIONS)

    def call(operation, *args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=False, open_world_hint=False
        ),
        structured_output=True,
    )
    def open_repository(
        folder: str, exclude: list[str] | None = None
    ) -> dict[str, Any]:
        """Open a local folder or ZIP path. ZIPs become managed snapshots. Returns repo_id and overview. Writes only the managed cache."""
        return call(repositories.open, folder, exclude)

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def repo_overview(repo_id: str | None = None) -> dict[str, Any]:
        """Languages, directories, project files, entry candidates, evidence and coverage."""
        return call(repositories.overview, repo_id)

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def list_files(
        repo_id: str | None = None,
        path: str = "",
        language: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Browse indexed paths by literal prefix and optional language, with pagination."""
        return call(repositories.files, repo_id, path, language, offset, limit)

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def search_code(
        query: str,
        limit: int = 5,
        kind: str | None = None,
        path: str | None = None,
        language: str | None = None,
        repo_id: str | None = None,
    ) -> dict[str, Any]:
        """Find symbols and text chunks. Exact symbols precede keyword matches. Results include source citations."""
        return {
            "matches": call(
                repositories.search, repo_id, query, limit, kind, path, language
            )
        }

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def read_source(
        path: str,
        start_line: int = 1,
        end_line: int | None = None,
        max_lines: int = 100,
        repo_id: str | None = None,
    ) -> dict[str, Any]:
        """Read hash-verified source. Long ranges return next_line; pass it as start_line to continue."""
        return call(repositories.read, repo_id, path, start_line, end_line, max_lines)

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def file_outline(
        path: str, repo_id: str | None = None, offset: int = 0, limit: int = 50
    ) -> dict[str, Any]:
        """Read a paginated definition outline. Text-only files return no symbols and report coverage."""
        return call(repositories.outline, repo_id, path, offset, limit)

    return server


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, help="optional legacy single-index mode")
    parser.add_argument("--cache-dir", type=Path, help="managed index folder")
    args = parser.parse_args()
    build_server(args.db.resolve() if args.db else None, args.cache_dir).run("stdio")


if __name__ == "__main__":
    main()
