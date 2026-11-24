"""Explore a repository from the terminal, run the MCP server, or connect it to a client.

Commands work on the current folder unless you pass a folder, --repo, or a legacy --db file.
The index is created and kept up to date automatically.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

from explorer import indexer, retriever
from explorer.repository import Repositories

DEFAULT_CACHE = Path.home() / ".cache" / "codebase-explorer"
SERVER_NAME = "codebase-explorer"


COMMANDS = ("open", "overview", "files", "outline", "search", "show", "refs", "index", "serve", "setup")


def entry() -> None:
    """The `codebase-explorer` command: the MCP server, unless a command is named.

    `codebase-explorer` alone (or with server options such as --repo) starts the stdio
    server, so clients can register it without arguments. `codebase-explorer setup claude`,
    `codebase-explorer search ...` and the other commands work as with `explorer`.
    """
    argv = sys.argv[1:]
    if any(arg in ("-h", "--help", "--version") for arg in argv[:1]) or any(a in COMMANDS for a in argv):
        sys.exit(main(argv))
    sys.exit(main(["serve", *argv]))


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "serve":
            from explorer import mcp_server

            mcp_server.run(args)
            return 0
        if args.command == "setup":
            return _setup(args)
        if args.command == "index" and args.db is not None:
            folder = args.folder or args.repo or "."
            _print_index(indexer.index_repo(Path(folder), args.db, tuple(args.exclude), full=args.full), args)
            return 0
        return _run(args)
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="explorer", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    from explorer import __version__

    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--repo", help="project folder or ZIP (default: current folder)")
    parser.add_argument("--db", type=Path, help="use a single legacy index file instead of the cache")
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE, help="managed index folder")
    parser.add_argument("--json", action="store_true", help="print JSON instead of text")
    parser.add_argument("--no-refresh", action="store_true",
                        help="don't update the index; refuse files changed since indexing")
    commands = parser.add_subparsers(dest="command", required=True)

    command = commands.add_parser("open", help="open or refresh a repository and summarize it")
    command.add_argument("folder", nargs="?", help="folder or ZIP file")
    command.add_argument("--exclude", action="append", default=[], help="gitignore-style pattern")
    command = commands.add_parser("overview", help="summarize a repository")
    command.add_argument("folder", nargs="?")
    command = commands.add_parser("files", help="list indexed files")
    command.add_argument("folder", nargs="?")
    command.add_argument("--path", default="", help="only paths starting with this prefix")
    command.add_argument("--language")
    command.add_argument("--offset", type=int, default=0)
    command.add_argument("--limit", type=int, default=50)
    command = commands.add_parser("outline", help="list a file's definitions")
    command.add_argument("target", nargs="+", metavar="[FOLDER] PATH")

    search = commands.add_parser("search", help="find definitions and text")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=5)
    search.add_argument("--kind", choices=retriever.KINDS)
    search.add_argument("--path", help="only paths starting with this prefix")
    search.add_argument("--language")

    show = commands.add_parser("show", help="print numbered, verified source lines")
    show.add_argument("path")
    show.add_argument("start_line", type=int, nargs="?", default=1)
    show.add_argument("end_line", type=int, nargs="?")

    refs = commands.add_parser("refs", help="find where a name is used")
    refs.add_argument("name")
    refs.add_argument("--path", help="only paths starting with this prefix")
    refs.add_argument("--offset", type=int, default=0)
    refs.add_argument("--limit", type=int, default=50)

    index = commands.add_parser("index", help="build or update the index")
    index.add_argument("folder", nargs="?")
    index.add_argument("--exclude", action="append", default=[], help="gitignore-style pattern")
    index.add_argument("--full", action="store_true", help="rebuild from scratch")

    from explorer import mcp_server

    serve = commands.add_parser("serve", help="run the MCP server on stdio")
    mcp_server.add_arguments(serve)

    setup = commands.add_parser("setup", help="connect the MCP server to Claude Code or Codex")
    setup.add_argument("client", choices=["claude", "codex"])
    setup.add_argument("--scope", choices=["user", "project", "local"], default="user",
                       help="Claude Code scope (default: user, so it works in every project)")
    setup.add_argument("--replace", action="store_true", help="remove an existing registration first")
    setup.add_argument("--print", action="store_true", help="show the command without running it")
    setup.add_argument("--server-command", help="command that starts the server, if not detected")
    return parser


def _run(args: argparse.Namespace) -> int:
    folder = getattr(args, "folder", None)
    if args.command == "outline":
        if len(args.target) > 2:
            raise ValueError("outline takes an optional folder and a file path.")
        folder, path = (args.target if len(args.target) == 2 else (None, args.target[0]))
    target = folder or args.repo or os.getcwd()
    repos = Repositories(args.cache_dir, args.db, default=None if args.db else target,
                         auto_refresh=not args.no_refresh)
    if args.command == "open" or (args.command == "index" and args.db is None):
        full = args.command == "index" and args.full
        result = repos.open(target, args.exclude, full=full)
        return _emit(args, result, _format_index if args.command == "index" else _format_overview)
    if args.command == "overview":
        return _emit(args, repos.overview(None), _format_overview)
    if args.command == "files":
        return _emit(args, repos.files(None, args.path, args.language, args.offset, args.limit), _format_files)
    if args.command == "outline":
        return _emit(args, repos.outline(None, path, 0, 100), _format_outline)
    if args.command == "search":
        report = repos.search_report(None, args.query, args.limit, args.kind, args.path, args.language)
        return _emit(args, report, _format_search)
    if args.command == "show":
        return _emit(args, repos.read(None, args.path, args.start_line, args.end_line, 400), _format_show)
    if args.command == "refs":
        return _emit(args, repos.references(None, args.name, args.path, args.offset, args.limit), _format_refs)
    raise ValueError(f"Unknown command {args.command}")


# ---------------------------------------------------------------- output


def _emit(args, result: dict, formatter) -> int:
    print(json.dumps(result, indent=2) if args.json else formatter(result))
    return 0


def _print_index(report: indexer.IndexReport, args) -> None:
    if args.json:
        print(json.dumps({
            "root": str(report.root), "db": str(report.db_path), "mode": report.mode,
            "reason": report.reason, "files": report.files_indexed,
            "symbols": report.symbols_indexed, "added": report.added,
            "changed": report.changed, "removed": report.removed,
            "skipped": report.skipped, "warnings": report.warnings,
        }, indent=2))
        return
    status = {
        "full": f"rebuilt: {report.reason}" if report.reason else "rebuilt",
        "incremental": f"updated {len(report.added)} added, {len(report.changed)} changed, "
                       f"{len(report.removed)} removed",
        "unchanged": "no changes since the last index",
    }[report.mode]
    print(f"Indexed {report.symbols_indexed} symbols in {report.files_indexed} files "
          f"from {report.root} into {report.db_path} ({status})")
    for path, reason in report.skipped:
        print(f"  skipped {path}: {reason}")
    for path, reason in report.warnings:
        print(f"  text only or partial {path}: {reason}")


def _format_index(o: dict) -> str:
    i = o["index"]
    status = {
        "full": f"rebuilt: {i['reason']}" if i.get("reason") else "rebuilt",
        "incremental": f"updated {i['added']} added, {i['changed']} changed, {i['removed']} removed",
        "unchanged": "no changes since the last index",
    }[i["mode"]]
    lines = [f"Indexed {o['symbols_indexed']} symbols in {o['files_indexed']} files from {o['root']} ({status})"]
    lines += [f"  skipped or partial {path}: {reason}" for path, reason in o["diagnostics"]]
    if o["diagnostics_total"] > len(o["diagnostics"]):
        lines.append(f"  ... {o['diagnostics_total']} in total")
    return "\n".join(lines)


def _top(counter: dict, n: int = 8) -> str:
    items = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
    shown = ", ".join(f"{k} {v}" for k, v in items[:n])
    return shown + (f", +{len(items) - n} more" if len(items) > n else "")


def _format_overview(o: dict) -> str:
    lines = [f"Repository: {o.get('root')}"]
    if "index" in o:
        i = o["index"]
        lines.append("Index: " + {
            "full": f"built ({i.get('reason', 'new')})",
            "incremental": f"updated ({i['added']} added, {i['changed']} changed, {i['removed']} removed)",
            "unchanged": "up to date",
        }.get(i["mode"], i["mode"]))
    lines.append(f"Files: {o['files']} ({_top(o['languages'])})")
    lines.append(f"Coverage: {_top(o['coverage'])}")
    if o["directories"]:
        lines.append("Directories: " + ", ".join(o["directories"][:20]))
    if o["project_files"]:
        lines.append("Project files: " + ", ".join(o["project_files"][:15]))
    if o["entry_candidates"]:
        lines.append("Entry candidates (filename hints): " + ", ".join(o["entry_candidates"][:10]))
    if o["diagnostics_total"]:
        lines.append(f"Skipped or partial files ({o['diagnostics_total']}):")
        lines += [f"  {path}: {reason}" for path, reason in o["diagnostics"][:15]]
    if o.get("repo_id"):
        lines.append(f"repo_id: {o['repo_id']}")
    if o.get("index_warning"):
        lines.append(f"Warning: {o['index_warning']}")
    return "\n".join(lines)


def _format_files(r: dict) -> str:
    if not r["files"]:
        return "No files."
    width = max(len(f["path"]) for f in r["files"])
    lines = [f"{f['path']:<{width}}  {f['language']:<10}  {f['coverage']:<9}  {f['line_count']} lines"
             for f in r["files"]]
    if r["next_offset"] is not None:
        lines.append(f"... {r['total']} files; continue with --offset {r['next_offset']}")
    return "\n".join(lines)


def _format_outline(r: dict) -> str:
    lines = [f"{r['path']} ({r['coverage']})"]
    for s in r["symbols"]:
        lines.append(f"  {s['start_line']}-{s['end_line']}  {s['kind']:<9} {s['qualified_name']}")
    if not r["symbols"]:
        lines.append("  No definitions extracted.")
    if r["next_offset"] is not None:
        lines.append(f"  ... {r['total']} definitions in total")
    return "\n".join(lines)


def _format_search(r: dict) -> str:
    if not r["matches"]:
        return "No matches."
    lines = []
    for m in r["matches"]:
        label = m["match_type"].replace("_", " ")
        detail = f"{m['strength']}, {label}"
        if m["missing_words"]:
            detail += ", missing: " + " ".join(m["missing_words"])
        lines.append(f"{m['citation']}  {m['kind']:<8}  {m['qualified_name'] if m['kind'] != 'text' else ''}"
                     .rstrip() + f"  [{detail}]")
        if m["signature"]:
            lines.append(f"    {m['signature']}")
        for hit in m["snippet"]:
            lines.append(f"    {hit['line']}: {hit['text']}")
    if r.get("index_warning"):
        lines.append(f"Warning: {r['index_warning']}")
    return "\n".join(lines)


def _format_show(r: dict) -> str:
    lines = [r["rendered"]]
    if r["next_line"]:
        lines.append(f"Continue at line {r['next_line']} through {r['requested_end_line']}.")
    if r.get("note"):
        lines.append(f"Note: {r['note']}")
    return "\n".join(lines)


def _format_refs(r: dict) -> str:
    lines = []
    if r["definitions"]:
        lines.append("Defined at:")
        lines += [f"  {d['citation']}  {d['kind']}  {d['qualified_name']}" for d in r["definitions"]]
    lines.append(f"Used {r['total']} time{'' if r['total'] == 1 else 's'}" + (" (first files only)" if r["truncated"] else "") + ":")
    for u in r["references"]:
        where = f"  in {u['in']}" if u["in"] else ""
        lines.append(f"  {u['path']}:{u['line']}  {u['kind']}{where}")
        lines.append(f"      {u['text']}")
    if r["next_offset"] is not None:
        lines.append(f"  ... continue with --offset {r['next_offset']}")
    return "\n".join(lines)


# ---------------------------------------------------------------- setup


def _server_command(override: str | None) -> tuple[list[str], str | None]:
    """The command a client should run to start this server, and a note if it is fragile."""
    if override:
        return shlex.split(override, posix=os.name != "nt"), None
    if "archive-v" in sys.prefix.replace("\\", "/"):
        # A temporary uvx environment: register uvx itself so the client can recreate it.
        return ["uvx", SERVER_NAME], "Registered through uvx, which downloads the package when needed."
    scripts = Path(sys.executable).parent
    for name in (f"{SERVER_NAME}.exe", SERVER_NAME, "explorer-mcp.exe", "explorer-mcp"):
        candidate = scripts / name
        if candidate.is_file():
            return [str(candidate)], None
    return [sys.executable, "-m", "explorer.mcp_server"], None


def _setup(args: argparse.Namespace) -> int:
    server, note = _server_command(args.server_command)
    if args.client == "claude":
        add = ["claude", "mcp", "add", "--scope", args.scope, SERVER_NAME, "--", *server]
        remove = ["claude", "mcp", "remove", SERVER_NAME, "--scope", args.scope]
    else:
        add = ["codex", "mcp", "add", SERVER_NAME, "--", *server]
        remove = ["codex", "mcp", "remove", SERVER_NAME]
    shown = subprocess.list2cmdline(add) if os.name == "nt" else shlex.join(add)
    if args.print:
        print(shown)
        return 0
    executable = shutil.which(args.client)
    if executable is None:
        print(f"error: the {args.client} command was not found. Install it, or run this yourself:\n  {shown}",
              file=sys.stderr)
        return 1
    if args.replace:
        subprocess.run([executable, *remove[1:]], check=False, capture_output=True)
    print(f"Running: {shown}")
    result = subprocess.run([executable, *add[1:]], check=False)
    if result.returncode != 0:
        print(f"error: {args.client} exited with code {result.returncode}. If the server is already "
              f"registered, rerun with --replace.", file=sys.stderr)
        return result.returncode
    if note:
        print(note)
    if args.client == "claude":
        print("Done. Start `claude` in any project folder and type /mcp to check the connection.")
    else:
        print("Done. Start `codex` in any project folder to use it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
