# Codebase Explorer

Explore a local repository from Codex, Claude Code, another stdio MCP client, or the terminal. Search works across UTF-8 source, documentation and configuration. Python definitions use the standard-library parser. Optional Tree-sitter parsers add JavaScript, JSX, TypeScript and TSX definitions.

The index is local SQLite. The MCP client sends retrieved content to its configured model. The explorer never executes repository source or installs that repository's dependencies.

## Install

Use Python 3.13 for the parser-enabled installation. Run these commands in the explorer folder on Windows:

```powershell
py -3.13 -m venv .venv
.venv\Scripts\python -m pip install -e ".[test,structure]"
.venv\Scripts\python -m pytest -q
```

On macOS/Linux, use `python3.13` and `.venv/bin/python`. A text-search installation needs Python 3.10 or newer and `pip install -e .`. The optional `structure` extra needs compatible native Tree-sitter wheels or a compiler. If it is absent, JavaScript and TypeScript stay searchable as text, with a diagnostic explaining the missing parser.

## Connect once

Replace the paths below with the full path to your installation. No database argument is needed.

Codex CLI:

```powershell
codex mcp add codebase-explorer -- "C:\tools\codebase-explorer\.venv\Scripts\explorer-mcp.exe"
```

Claude Code:

```powershell
claude mcp add codebase-explorer -- "C:\tools\codebase-explorer\.venv\Scripts\explorer-mcp.exe"
```

Other local MCP clients can launch the same executable using stdio. Remote HTTP transport and automatic chat-attachment transfer are not implemented. The server must be able to read the folder or ZIP path you give it.

Then ask:

> Open C:/Projects/shop with codebase-explorer. Explain the main directories and the declared startup and test commands, citing the project files. Then find where checkout is implemented and read the relevant source.

The model calls `open_repository(folder)`, receives a `repo_id`, and passes it to later calls. The `folder` argument accepts a directory or a ZIP file. Each repository has its own index under `~/.cache/codebase-explorer`. Override this with `explorer-mcp --cache-dir PATH`. Reopen a folder to refresh its index after edits. Existing `--db FILE` connections remain supported, but old indexes need rebuilding once.

## Import a ZIP

Use the existing opening command:

```powershell
.venv\Scripts\explorer open "C:\Downloads\shop.zip"
.venv\Scripts\explorer outline "C:\Downloads\shop.zip" src/main.ts
```

Or ask your connected model:

> Open C:/Downloads/shop.zip with codebase-explorer, explain the main directories, and find the checkout implementation with citations.

The server copies and validates the archive, extracts a managed snapshot, and indexes it. `open_repository` returns `source_type="zip"`, `archive_sha256`, the usual `repo_id`, and the overview. Pass that repository ID to the same browsing, search, outline and source-reading tools used for folders.

ZIPs containing a single top-level directory use that directory as the repository root. GitHub-style `shop-main/src/main.ts` therefore becomes `src/main.ts`. Flat archives and archives with multiple top-level directories also work. macOS `__MACOSX` and `.DS_Store` metadata is omitted. Normal index exclusions still apply inside the extracted repository.

Extracted files persist under the cache's `archives` directory. Reads still work after the original ZIP is moved or deleted. Identical archive bytes reuse the same snapshot and repository ID, even when imported under another filename. Changed archive bytes create a separate snapshot and ID, so earlier citations keep referring to the earlier copy. Reopening a ZIP refreshes its index against its stored snapshot. Treat extracted snapshots as managed files rather than editing them.

ZIP limits apply before index exclusions: 100 MiB archive size, 10,000 entries, 50 MiB per extracted file, and 500 MiB total extracted content. The index's existing 1 MB per-file text limit still applies after extraction. Invalid or encrypted archives, symlinks and special files, unsafe paths, and conflicting names are rejected. Failed extraction leaves no published snapshot. No archive contents are executed.

This supports ZIP files accessible to the server. A chat host must still make its attachments accessible as local paths.

## MCP tools

| Tool | Result |
| --- | --- |
| `open_repository(folder, exclude?)` | Opens a folder or ZIP and returns its ID and overview. Writes only the managed cache. |
| `repo_overview(repo_id)` | Languages, coverage, directories, project files, entry candidates, and cited project-file excerpts. |
| `list_files(repo_id, path?, language?, offset?, limit?)` | Files matching a literal path prefix, with `next_offset`. |
| `search_code(query, repo_id, limit?, kind?, path?, language?)` | Exact definitions first, then keyword matches and text chunks. |
| `read_source(path, repo_id, start_line?, end_line?, max_lines?)` | Verified source with citation and `next_line`. |
| `file_outline(path, repo_id, offset?, limit?)` | Definitions, structural coverage and `next_offset`. |

Results use MCP structured content and a JSON text representation. Reads default to 100 lines, with a maximum of 400 lines and 24,000 source characters per response. Continue using `next_line` and the same requested end line. A single line exceeding the character limit returns an error. Empty files return no citation. File lists and outlines default to 50 entries and allow at most 100.

The overview includes up to the first 40 lines of 20 README/manifest/build files, capped at 4,000 characters per file and 16,000 total. Files whose first line exceeds that budget appear in evidence_omitted. Read further to verify commands outside those excerpts. Entry candidates come from filenames and do not prove runtime behavior. Coverage is `structure`, `partial`, or `text`. Diagnostics explain skipped files and incomplete extraction.

## Terminal

Open a folder with automatic index management:

```powershell
.venv\Scripts\explorer open "C:\Projects\shop"
.venv\Scripts\explorer files "C:\Projects\shop"
.venv\Scripts\explorer outline "C:\Projects\shop" src/main.ts
```

The explicit database workflow also remains available:

```powershell
.venv\Scripts\explorer --db indexes/shop.db index "C:\Projects\shop"
.venv\Scripts\explorer --db indexes/shop.db search checkout --path src --language typescript
.venv\Scripts\explorer --db indexes/shop.db show src/main.ts 1 500
```

`show` prints a continuation line for long ranges. `open`, `overview`, `files`, and `outline` refresh the folder before reporting. Terminal file lists and outlines return the first page; MCP provides continuation parameters.

## File selection and limits

- Nested `.gitignore` and `.explorerignore` files apply even to copied folders without Git metadata. Add patterns to `.explorerignore`, pass `exclude` to `open_repository`, or repeat `--exclude` on the terminal `index` command.
- Dependency, environment, VCS, build and cache directories are excluded. Directory symlinks are not traversed. File symlinks outside the repository are refused.
- Environment files named `.env` or `.env.*`, SQLite files, generated-header files, binary files, files over 1 MB, and non-UTF-8 files are excluded. Check diagnostics for skipped files. These rules are file selection, not a guarantee that accepted source contains no credentials.
- Text chunks cover the entire accepted file in blocks of 100 lines. Keyword search is lexical, not semantic. Exact symbol matches take precedence; `kind="text"` searches file content directly.
- Python extraction covers top-level functions/classes and their direct methods. Syntax errors retain text coverage. JavaScript/TypeScript extraction covers named functions, arrow/function variables, classes and methods, plus TypeScript interfaces, types and enums. Broken syntax can yield partial outlines. Import resolution and precise cross-file references are not implemented.
- Indexing rebuilds the repository. It writes a temporary database and replaces the old index only after success. Source reads verify the original file hash before returning lines. Search results and inventory describe the last index; reopen after edits. Files and line references are scoped to the selected repository.
- The server has the filesystem access of the process running it. It is intended for a trusted local client. Hosted multi-user access would require separate access controls.

## Verification

```powershell
.venv\Scripts\python -m pytest -q
.venv\Scripts\python -m pip check
```

The suite covers the existing Python behavior, full-file search, exclusion rules, parser fallback, JavaScript/TypeScript/TSX definitions, stale reads, long-source continuation, repository isolation, database migration, and stdio MCP workflows against Python, TypeScript and mixed fixtures. CI runs these checks on Windows and Linux with Python 3.10 and 3.13.

See [implementation report](docs/implementation-report.md) for checks against the six planned steps and the client smoke-test results.
