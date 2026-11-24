# Codebase Explorer

Explore a code repository from Claude Code, Codex, another stdio MCP client, or the terminal. It searches definitions, documentation and configuration, finds where names are used, and returns source lines with `path:start-end` citations that are checked against the file on disk.

Definitions are extracted for Python (standard-library parser) and, through Tree-sitter, JavaScript, TypeScript, TSX, Go, Java, Rust and C#. Every other UTF-8 file is searchable as text.

The index is a local SQLite file that keeps itself up to date. The MCP client sends the content it retrieves to its configured model. The explorer never executes repository code or installs the repository's dependencies.

## Install and connect

You need Python 3.10 or newer. Install the tool once, then connect it to your client once.

**From GitHub, today** (with [uv](https://docs.astral.sh/uv/) or pipx):

```
uv tool install git+https://github.com/adidoppalapudi/codebase-explorer
codebase-explorer setup claude
```

**From PyPI, once published:**

```
uv tool install codebase-explorer
codebase-explorer setup claude
```

Use `setup codex` for Codex. `setup` runs `claude mcp add --scope user codebase-explorer -- <path to codebase-explorer>`, so the server is available in every project. Add `--print` to see the command without running it, `--replace` to replace an existing registration, or `--scope project` to write a `.mcp.json` that you can commit for your team.

To register it by hand instead:

```
claude mcp add --scope user codebase-explorer -- codebase-explorer
codex mcp add codebase-explorer -- codebase-explorer
```

On Python 3.15, the parser packages have no prebuilt wheels yet, so they are left out of the install. Python is still fully supported; other languages are searchable as text. Install with `[structure]` to compile the parsers if you have a C compiler.

**From a local checkout** (for development):

```
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[test]"
.venv\Scripts\python -m pytest -q
.venv\Scripts\codebase-explorer setup claude
```

On macOS and Linux, use `.venv/bin/` instead of `.venv\Scripts\`.

## Use it

Start your client in any project folder and ask:

> Use codebase-explorer to find how checkout works, from the entry point down. Cite each step as path:start-end.

> What calls `run_quality_check`, and what would break if I changed its signature?

There is nothing to open or rebuild. The server opens the project it was started in (a folder with `.git`, `pyproject.toml`, `package.json`, `go.mod`, `Cargo.toml`, a README or a similar marker) the first time a tool needs it. It builds the index then, which takes a few seconds for a large repository, and keeps it current from then on.

To explore a different folder or a ZIP file, ask the model to open it ("Open C:/Projects/shop with codebase-explorer"). That repository becomes the current one. To pin a repository instead of using the start folder, register the server with `--repo PATH`, or set `CODEBASE_EXPLORER_REPO`.

## How the index stays current

Before each tool call (at most every two seconds, less often for very large repositories) the server compares each file's size and modification time with the index. Unchanged files are skipped, which takes about a tenth of a second for 1,200 files. Changed, added and deleted files are updated in one transaction, in under half a second for dozens of files. Ranking after an update is identical to a fresh rebuild.

Reads check the file's SHA-256 hash. If a file changed since it was indexed, it is re-indexed and read again, and the result says `refreshed` because line numbers from earlier results may have moved. Start the server or command with `--no-refresh` to refuse changed files instead.

The whole index is rebuilt, into a new file that replaces the old one only when complete, when the index format, the installed parsers or the exclusions change. On SQLite older than 3.43 (bundled with some Python 3.10 and 3.11 builds), the search index can't be updated in place, so any content change triggers a rebuild instead; results are the same, only slower.

The index stores search data, not a copy of the text: it reads text back from the repository when it shows it. For a 1,200-file repository it is about 33 MB.

## MCP tools

`repo_id` is optional everywhere: without it, tools use the current repository.

| Tool | Result |
| --- | --- |
| `open_repository(folder, exclude?)` | Opens a folder or ZIP, makes it current, returns its `repo_id` and overview. Writes only the managed cache. |
| `repo_overview(repo_id?)` | Languages, coverage, directories, project files with cited excerpts, entry candidates, diagnostics. |
| `list_files(path?, language?, offset?, limit?)` | Indexed files by literal path prefix, with `next_offset`. |
| `search_code(query, limit?, kind?, path?, language?)` | Exact names first, then ranked keyword matches in definitions and file text, each with evidence. |
| `read_source(path, start_line?, end_line?, max_lines?)` | Hash-verified lines with a citation and `next_line`. Accepts `/` or `\` and absolute paths inside the repository. |
| `file_outline(path, offset?, limit?)` | A file's definitions with citations. |
| `find_references(name, path?, offset?, limit?)` | Where a name is used: each line as a call, import, reference or comment, with the enclosing definition, plus the definitions. |

### Judging search results

Search returns its best keyword matches without hiding weak ones, and tells the model why each matched so it can judge relevance itself:

- `match_type`: `exact_name`, `name` (query words in the definition's name), `definition_text` (in its signature, docstring or body) or `file_text` (a 100-line block of a file).
- `matched_words` and `missing_words`: which query words the match contains.
- `strength`: `strong` when the name is exact or contains every query word, `partial` when every word appears somewhere or half are in the name, otherwise `weak`.
- `snippet`: for body and file-text matches, up to three matching lines with line numbers.

The server instructions tell the model to treat weak matches as leads, to read before claiming, and to try other spellings (such as "frontmatter" for "front matter") before concluding something is absent.

`find_references` matches the name as a whole word in every indexed file. It includes things with the same name elsewhere, so each hit names its enclosing definition; it does not follow aliases or dynamically built calls.

Results use MCP structured content and a JSON text representation. Reads default to 100 lines, with a maximum of 400 lines and 24,000 characters per response; continue with `next_line`. File lists and outlines default to 50 entries and allow at most 100.

## Terminal

Every command works on the current folder, or on `--repo PATH`. Output is readable text; add `--json` for JSON.

```
codebase-explorer index                      # build or update the index, list skipped files
codebase-explorer search "quality check" --path src
codebase-explorer show src\cli.py 3249 3275  # numbered, hash-verified lines
codebase-explorer refs run_quality_check     # definitions and every use
codebase-explorer outline src/cli.py
codebase-explorer files --path src --language python
codebase-explorer overview
codebase-explorer open C:\Downloads\shop.zip
```

`explorer` is the same command. Search filters: `--kind` (function, class, method, interface, type, enum, struct, trait, module, macro, property, constant, variable, text), `--path` (prefix) and `--language`. `index --full` rebuilds from scratch.

A single index file still works: `explorer --db indexes\shop.db index C:\Projects\shop`, then `explorer --db indexes\shop.db search checkout`. The file is updated automatically like the managed cache, and an index made by an older version is rebuilt on first use.

Managed indexes live in `~/.cache/codebase-explorer`; change it with `--cache-dir`.

## ZIP files

`open_repository` and `open` accept a ZIP with the same argument. The archive is copied, validated and extracted into a snapshot in the cache, named by its SHA-256, and indexed like a folder. A single top-level folder (such as GitHub's `shop-main/`) becomes the repository root. Reads keep working after the original ZIP is moved or deleted. Identical bytes reuse the snapshot; changed bytes create a new one, so earlier citations keep pointing at the earlier copy.

Limits apply before exclusions: 100 MiB archive, 10,000 entries, 50 MiB per file and 500 MiB extracted. Invalid or encrypted archives, links and special files, unsafe or conflicting paths are rejected, and a failed import leaves nothing behind. A chat host must make attachments available as local paths.

## What gets indexed

- Nested `.gitignore` and `.explorerignore` files apply, even without Git. Add patterns to `.explorerignore`, pass `exclude` to `open_repository`, or repeat `--exclude`.
- Dependency, environment, VCS, build and cache folders are skipped, and folder symlinks are not followed. File symlinks pointing outside the repository are refused.
- `.env` and `.env.*` files, SQLite files, files whose leading comments say they are generated, binary files, files over 1 MB and non-UTF-8 files are skipped, each with a reason in the diagnostics. This is file selection, not a guarantee that accepted files contain no credentials.
- All accepted text is searchable in 100-line blocks.
- Python: functions, classes and methods at any depth, qualified as `outer.inner` or `Outer.Inner.method`, including definitions inside `if`, `try` and `with` blocks; module-level constants (UPPER_CASE) and variables. Files with syntax errors stay searchable as text.
- JavaScript and TypeScript: functions, arrow and function-expression variables, classes, methods, interfaces, types and enums.
- Go: functions, methods (as `Type.Method`), structs, interfaces, types, top-level constants and variables.
- Java: classes, interfaces, enums, records, methods and constructors.
- Rust: functions, methods in `impl` and `trait` blocks (as `Type.method`), structs, enums, traits, types, modules, macros and constants. A definition starts at its `#[...]` attributes.
- C#: classes, structs, interfaces, enums, records, methods, constructors and properties.
- Search is lexical, not semantic. Import resolution and type-aware references are not implemented.
- The server has the file access of the process running it and is meant for a trusted local client.

## Development

```
.venv\Scripts\python -m pytest -q
.venv\Scripts\python -m pip check
ruff check explorer tests scripts
```

CI runs the tests on Windows, macOS and Linux with Python 3.10, 3.13 and 3.14. `scripts/check_clients.py` runs a live check through installed, signed-in Claude Code and Codex CLIs (it makes model calls).

### Publishing

Pushing a tag such as `v0.2.0` builds the package and publishes it to PyPI through `.github/workflows/publish.yml`. Before the first release, check that the name `codebase-explorer` is free on PyPI (or change `name` in `pyproject.toml`), then add a trusted publisher on pypi.org for this repository with workflow `publish.yml` and environment `pypi`. No API token is needed.

See the [implementation report](docs/implementation-report.md) for what was built and how it was verified.
