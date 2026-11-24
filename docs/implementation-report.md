# Implementation and verification

Version 0.2 changes are described first; the 0.1 milestone report follows.

## Version 0.2: easier to use, more accurate, smaller

| Change | What it does | Checked by |
| --- | --- | --- |
| Install with one command | `codebase-explorer` entry point (MCP server with no arguments, CLI with a command). Tree-sitter parsers install by default where wheels exist (Python 3.10-3.14). Publish workflow for PyPI through trusted publishing. | Installed into a fresh environment; the bare command served MCP to a client; `setup --print` detected the installed command. |
| Opens the current project | Calls without `repo_id` use the last opened repository, else `--repo`, `CODEBASE_EXPLORER_REPO`, `CLAUDE_PROJECT_DIR`, or the start folder if it has a project marker. Home folders and drive roots are refused. | MCP session started inside SkillEvaluator answered overview, search, read and references without `open_repository`. |
| Incremental index | Files are compared by size and modification time; unchanged files are skipped, changed ones updated in one transaction. Rebuilds only when the format, parsers or exclusions change. | SkillEvaluator (1,221 files): no-change refresh 0.10 s, 40-file change 0.44 s, full build 7.6 s. |
| Automatic refresh | Index refreshed before calls (at most every 2 s, scaled up for slow repositories). Changed files are re-indexed on read and marked `refreshed`. `--no-refresh` keeps the strict behaviour. | Tests for edits, deletions and stale reads through the API, CLI and MCP. |
| Forgiving paths | Back slashes, `./`, quotes, absolute paths inside the repository and unambiguous case differences are accepted; outside paths and `..` are still refused; misses suggest similar paths. | Path tests, including Windows-style paths. |
| Simpler terminal | Commands default to the current folder, print readable text (`--json` for JSON), `show` takes optional lines, new `refs`, `serve` and `setup`. `--db` workflows still work and old index files are rebuilt automatically. | CLI tests for both modes. |
| Setup command | `setup claude` / `setup codex` registers the server (user scope by default), with `--print`, `--replace` and `--scope`. | Command construction and execution tests. |
| Evidence for the model | Each match has `match_type`, `strength`, `matched_words`, `missing_words` and a `snippet`, computed from the index and the verified file. Nothing is filtered; the server instructions tell the model to judge. | "Where does this repository send emails" on SkillEvaluator returns only weak matches, each showing the missing word. |
| References | `find_references` / `refs`: whole-word matches classified as call, import (including multi-line imports), reference or comment, with the enclosing definition. | `run_quality_check` on SkillEvaluator: 12 uses found in under a second, each classified. |
| More languages | Go, Java, Rust and C# definitions. Python adds nested functions and classes, definitions inside `if`/`try`/`with`, and module constants and variables. | Per-language tests; SkillEvaluator symbols rose from 23,911 to 32,876. |
| Smaller index | Full-text tables are contentless; text is read back from the verified file. | See accuracy checks below. |

### Accuracy checks

- **Storage change alone:** 40 queries x 4 filters on SkillEvaluator returned identical top-20 results before and after; the index went from 90.0 MB to 27.8 MB.
- **Incremental updates:** after editing 30 files, deleting 5, adding 5 and editing 10 again, 176 of 176 query and filter combinations matched a fresh rebuild exactly, and bm25 scores were identical. SQLite's contentless-delete tables keep stale row and token totals after deletes, which shifted scores; the totals are recomputed from the per-row sizes after each update. SQLite's integrity check passes.
- **New definitions:** 97% of the previous top-5 results for 80 query and filter combinations stay in the new top 10. Constants and variables are ranked at 0.6 of their score, and only methods include their class in name matching, so short constants and helpers nested in long test functions don't crowd out real definitions. With all 0.2 changes the index is 32.6 MB.

### Results

- 124 tests pass with all parsers; 119 pass and 5 parser tests skip without them.
- Ruff passes.
- Python 3.10.12 with SQLite 3.37.2 (no in-place updates, no parsers) passes a 13-check script covering rebuild-on-change, ranking equality with a fresh index, Python extraction, paths, refresh on read, references, the CLI and `--db` mode.


## Version 0.1 milestone

The six-step local MCP milestone and ZIP import are implemented. The explorer accepts an accessible folder or ZIP, manages its index, gives the model an overview, and returns searchable text and verified source citations. This report distinguishes tested behavior from future work.

| Step | Change | Conditions checked |
| --- | --- | --- |
| 1. Reliability | Generated-file detection examines leading comment headers. Indexing builds a replacement database before swapping it in. Source reading normalizes CRLF and CR line endings. The shared repository module pages long reads and caps source characters. Terminal/MCP errors translate expected failures. | Real marker strings remain indexed. Generated headers are excluded. Failed refresh preserves the old index. Old indexes rebuild. Missing database parent directories work. Unicode names are searchable. A 452-line function can be read across pages. |
| 2. All-text indexing | Every accepted UTF-8 file gets complete content coverage in 100-line chunks. Files keep hashes, language, line counts and extraction coverage. Nested ignore files and explicit exclusions apply. | Documentation, SQL, Go, configuration and unsupported extensions remain searchable. Text near the end of a long function is found. Broken Python and unavailable parsers retain text search. Hidden project configuration remains discoverable. Outside symlinks remain refused. |
| 3. Orientation/navigation | Added repository overview, file listing, outlines, path/language/kind filters, MCP structured results, and source/list continuation. Project-file excerpts give the model evidence for commands. | Overview shows language counts, directories, project files, entry candidates and diagnostics. Entry candidates are explicitly hints. Pagination, empty files, source character budgets and pre-limit language filtering pass. Startup/test commands are read from cited files, not invented or executed. |
| 4. Simple opening | open_repository accepts a folder and returns a persistent repository ID. The cache owns database paths. Reopening refreshes the index. --db mode still works for existing connections after rebuilding old indexes. | One MCP connection can open multiple folders. Each tool call selects its repository explicitly. Repository IDs reject traversal and unknown IDs. A new module instance can reopen the same managed index. Freshness checks reject changed source until refresh. |
| 5. MCP/client testing | Added protocol tests, a repeatable opt-in live-client script, and CI configuration. | All six tools pass stdio tests on Python, TypeScript and mixed fixtures. Installed Codex and Claude each passed all six tools on all three folder types. A separate client smoke test returned app/auth.py:12-17 through both clients. |
| 6. Language structure | Kept the Python adapter and added optional Tree-sitter adapters for JavaScript/JSX, TypeScript and TSX. Python extraction lives in its own module to avoid a dependency cycle with indexing. | Named functions, arrow functions, classes, methods, interfaces, types and enums have extraction support. Tests exercise JS functions/classes/methods, TypeScript interfaces/arrow functions, TSX and partial syntax. Missing parsers report text coverage. |

### Results

- Windows, Python 3.13.18 with parser dependencies: 98 tests passed, including 38 ZIP-import cases.
- Windows, Python 3.15.2 without optional parser dependencies: 97 passed, one parser-specific test skipped.
- Dependency checks passed for both installed environments. Ruff checks and git whitespace checks passed.
- Fresh public Click repository: 177 accepted files, 91 with Python structural coverage. Overview, exact-symbol search, outline and source reading passed.
- Fresh public sindresorhus/is repository: 18 accepted files, five with TypeScript structural coverage. Overview, exact-symbol search, outline and source reading passed.
- Self-indexing finds index_repo in explorer/indexer.py. It no longer excludes that file as generated.
- Live client matrix ran against disposable fixtures and caches. It did not change saved client configurations or execute fixture code.
- CI is configured for Windows/Linux and Python 3.10/3.13. Hosted CI has not run in this workspace.

Run deterministic checks:

```powershell
.venv-test\Scripts\python -m pytest -q
uv pip check --python .venv-test/Scripts/python.exe
```

The parser-enabled validation environment is .venv-test. The pre-existing Python 3.15 .venv remains a working text-only installation. The README documents a fresh Python 3.13 installation in .venv.

Repeat live client checks with authenticated CLIs:

```powershell
.venv-test\Scripts\python scripts/check_clients.py
```

This command makes model calls through the installed clients. It uses temporary MCP configuration rather than editing user settings. Claude's invocation has a one-dollar budget cap. Codex follows the account's configured billing.

### Design and remaining limits

Version 0.2 added incremental indexing, automatic refresh, references and more languages; the limits below describe 0.1.

The Repositories module owns the interface used by terminal and MCP adapters. It hides cache paths, repository identity, overview construction, filtering and continuation. Language extraction has real Python and Tree-sitter adapters. Tests exercise observable behavior through repository operations and the MCP protocol, with the existing retrieval tests retained for their still-supported interface.

Opening rebuilds the full index. Source reads verify live hashes; search and inventory describe the most recent index. Refreshes within one server instance serialize with reads. Separate processes refreshing the same cached repository concurrently may encounter an operating-system file-lock error and need a retry.

Accepted input now includes local folders and ZIP files. Remote HTTP hosting, automatic attachment transfer, semantic embeddings, incremental indexing and resolved cross-file references remain future work. Python extraction retains the original top-level/direct-method scope. Other languages get text coverage until a tested extractor exists. These limits also appear in the README.

### ZIP import addition

`open_repository` and the terminal `open`, `overview`, `files`, and `outline` commands accept a ZIP using the existing argument. No new MCP tool or dependency is required. The archive module owns validation, bounded extraction, wrapper-directory handling, and snapshot publication. Repository opening indexes the resulting folder through the existing module interface.

The importer copies the archive into temporary storage while hashing it, validates its entries, extracts into staging, and publishes the complete snapshot with a same-filesystem rename. Snapshots persist in the managed cache and use the archive's SHA-256 as their directory name. Byte-identical imports share the snapshot. Updated archives get a separate snapshot. Source reads keep working after deletion of the original ZIP.

Checks cover path traversal, Windows drive and UNC paths, backslashes, alternate data streams, reserved filenames, case/Unicode collisions, file/directory conflicts, links and special files, malformed archives, CRC failures, encryption, archive/entry/extraction limits, and failed-import cleanup. Behavior tests cover wrapped and flat layouts, ignore rules, explicit exclusions, repeated imports, renamed archives, changed archives, persistence, terminal commands, and the complete stdio MCP workflow. The README records the limits and explains that chat-attachment transfer still depends on the host.
