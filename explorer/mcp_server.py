"""Local stdio MCP tools for exploring repositories. Source files are never executed."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from explorer.repository import Repositories, default_folder

INSTRUCTIONS = """\
This server explores a local code repository and cites exact source lines.

The project the client was started in opens automatically, so you can omit repo_id. Call
open_repository only to explore a different folder or a ZIP file, then pass its repo_id
(or omit it: the last opened repository is the default). The index refreshes itself before
each call; you never need to rebuild it.

Start with repo_overview or list_files for orientation. Use search_code to find
definitions, documentation and configuration, file_outline to see a file's definitions,
and find_references to see where a name is used or called.

Search is keyword-based and returns its best matches without filtering. Judge each match
yourself from match_type (exact_name, name, definition_text, file_text), strength,
matched_words, missing_words and snippet. Treat weak matches as leads, not evidence. If no
match is strong, the index has no clear evidence; try other words (for example a joined
form such as "frontmatter") before concluding something is absent.

Read evidence with read_source before explaining implementation, and cite path:start-end.
Follow next_line and next_offset to continue. If a read reports refreshed, line numbers from
earlier results may have moved. Entry candidates are filename hints. Do not execute
repository code."""

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
REPO_ID = "repo_id from open_repository; omit it to use the current project."


def build_server(
    db_path: Path | None = None,
    cache_dir: Path | None = None,
    repo: str | None = None,
    auto_refresh: bool = True,
) -> MCPServer:
    repositories = Repositories(
        cache_dir or Path.home() / ".cache" / "codebase-explorer",
        db_path,
        default=default_folder(repo),
        auto_refresh=auto_refresh,
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
    def open_repository(folder: str, exclude: list[str] | None = None) -> dict[str, Any]:
        """Open a local folder or ZIP path and make it the current repository.

        Only needed for a repository other than the current project. ZIPs become managed
        snapshots. Returns repo_id and an overview. Writes only the managed cache.
        """
        return call(repositories.open, folder, exclude)

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def repo_overview(repo_id: str | None = None) -> dict[str, Any]:
        """Languages, directories, project files, entry candidates, cited excerpts of project
        files, coverage and diagnostics. repo_id is optional."""
        return call(repositories.overview, repo_id)

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def list_files(
        path: str = "",
        language: str | None = None,
        offset: int = 0,
        limit: int = 50,
        repo_id: str | None = None,
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
        """Find definitions and text by keywords. Exact names come first, then ranked matches.

        Each match has a citation plus evidence for judging it: match_type, strength,
        matched_words, missing_words and, for body or file-text matches, snippet lines.
        kind filters to function, class, method, interface, type, enum, struct, trait,
        module, macro, property, constant, variable or text. path is a prefix.
        """
        return call(repositories.search_report, repo_id, query, limit, kind, path, language)

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def read_source(
        path: str,
        start_line: int = 1,
        end_line: int | None = None,
        max_lines: int = 100,
        repo_id: str | None = None,
    ) -> dict[str, Any]:
        """Read hash-verified source lines. path may use / or \\ or be absolute inside the
        repository. Long ranges return next_line; pass it as start_line to continue."""
        return call(repositories.read, repo_id, path, start_line, end_line, max_lines)

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def file_outline(
        path: str, offset: int = 0, limit: int = 50, repo_id: str | None = None
    ) -> dict[str, Any]:
        """A file's definitions with citations, coverage and next_offset."""
        return call(repositories.outline, repo_id, path, offset, limit)

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def find_references(
        name: str,
        path: str | None = None,
        offset: int = 0,
        limit: int = 50,
        repo_id: str | None = None,
    ) -> dict[str, Any]:
        """Where a function, class, method or variable is used: each line with its kind
        (call, import, reference, comment) and enclosing definition, plus the definitions.
        Text-based, so same-named things elsewhere are included. path is a prefix."""
        return call(repositories.references, repo_id, name, path, offset, limit)

    return server


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", help="project folder or ZIP to open by default")
    parser.add_argument("--db", type=Path, help="optional legacy single-index file")
    parser.add_argument("--cache-dir", type=Path, help="managed index folder")
    parser.add_argument(
        "--no-refresh", action="store_true",
        help="don't update the index automatically; changed files are refused",
    )


def run(args: argparse.Namespace) -> None:
    build_server(
        args.db.resolve() if args.db else None,
        args.cache_dir,
        args.repo,
        not args.no_refresh,
    ).run("stdio")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    add_arguments(parser)
    run(parser.parse_args())


if __name__ == "__main__":
    main()
