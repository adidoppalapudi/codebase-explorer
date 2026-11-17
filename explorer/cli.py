"""Terminal commands: index, search, show."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from explorer import indexer, retriever

DEFAULT_DB = Path(".codebase-explorer.db")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="explorer", description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB,
                        help=f"SQLite index file, default {DEFAULT_DB}")
    commands = parser.add_subparsers(dest="command", required=True)

    index = commands.add_parser("index", help="rebuild the index for a folder")
    index.add_argument("folder", type=Path)

    search = commands.add_parser("search", help="find functions, classes, and methods")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=5)
    search.add_argument("--kind", choices=retriever.KINDS)
    search.add_argument("--path", help="only paths starting with this prefix")

    show = commands.add_parser("show", help="print numbered source lines")
    show.add_argument("path")
    show.add_argument("start_line", type=int)
    show.add_argument("end_line", type=int)

    args = parser.parse_args(argv)
    try:
        if args.command == "index":
            _index(args.folder, args.db)
        elif args.command == "search":
            _search(args.db, args.query, args.limit, args.kind, args.path)
        else:
            print(retriever.read_source(args.db, args.path, args.start_line, args.end_line).render())
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def _index(folder: Path, db_path: Path) -> None:
    report = indexer.index_repo(folder, db_path)
    print(f"Indexed {report.symbols_indexed} symbols in {report.files_indexed} files "
          f"from {report.root} into {report.db_path}")
    for path, reason in report.skipped:
        print(f"  skipped {path}: {reason}")


def _search(db_path: Path, query: str, limit: int, kind: str | None, path: str | None) -> None:
    matches = retriever.search_code(db_path, query, limit, kind=kind, path=path)
    if not matches:
        print("No matches.")
        return
    for match in matches:
        print(f"{match.citation}  {match.kind:<8}  {match.qualified_name}")
        print(f"    {match.signature}")


if __name__ == "__main__":
    sys.exit(main())
