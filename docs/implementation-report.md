# Implementation and verification

The six-step local MCP milestone and ZIP import are implemented. The explorer accepts an accessible folder or ZIP, manages its index, gives the model an overview, and returns searchable text and verified source citations. This report distinguishes tested behavior from future work.

| Step | Change | Conditions checked |
| --- | --- | --- |
| 1. Reliability | Generated-file detection examines leading comment headers. Indexing builds a replacement database before swapping it in. Source reading normalizes CRLF and CR line endings. The shared repository module pages long reads and caps source characters. Terminal/MCP errors translate expected failures. | Real marker strings remain indexed. Generated headers are excluded. Failed refresh preserves the old index. Old indexes rebuild. Missing database parent directories work. Unicode names are searchable. A 452-line function can be read across pages. |
| 2. All-text indexing | Every accepted UTF-8 file gets complete content coverage in 100-line chunks. Files keep hashes, language, line counts and extraction coverage. Nested ignore files and explicit exclusions apply. | Documentation, SQL, Go, configuration and unsupported extensions remain searchable. Text near the end of a long function is found. Broken Python and unavailable parsers retain text search. Hidden project configuration remains discoverable. Outside symlinks remain refused. |
| 3. Orientation/navigation | Added repository overview, file listing, outlines, path/language/kind filters, MCP structured results, and source/list continuation. Project-file excerpts give the model evidence for commands. | Overview shows language counts, directories, project files, entry candidates and diagnostics. Entry candidates are explicitly hints. Pagination, empty files, source character budgets and pre-limit language filtering pass. Startup/test commands are read from cited files, not invented or executed. |
| 4. Simple opening | open_repository accepts a folder and returns a persistent repository ID. The cache owns database paths. Reopening refreshes the index. --db mode still works for existing connections after rebuilding old indexes. | One MCP connection can open multiple folders. Each tool call selects its repository explicitly. Repository IDs reject traversal and unknown IDs. A new module instance can reopen the same managed index. Freshness checks reject changed source until refresh. |
| 5. MCP/client testing | Added protocol tests, a repeatable opt-in live-client script, and CI configuration. | All six tools pass stdio tests on Python, TypeScript and mixed fixtures. Installed Codex and Claude each passed all six tools on all three folder types. A separate client smoke test returned app/auth.py:12-17 through both clients. |
| 6. Language structure | Kept the Python adapter and added optional Tree-sitter adapters for JavaScript/JSX, TypeScript and TSX. Python extraction lives in its own module to avoid a dependency cycle with indexing. | Named functions, arrow functions, classes, methods, interfaces, types and enums have extraction support. Tests exercise JS functions/classes/methods, TypeScript interfaces/arrow functions, TSX and partial syntax. Missing parsers report text coverage. |

## Results

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

## Design and remaining limits

The Repositories module owns the interface used by terminal and MCP adapters. It hides cache paths, repository identity, overview construction, filtering and continuation. Language extraction has real Python and Tree-sitter adapters. Tests exercise observable behavior through repository operations and the MCP protocol, with the existing retrieval tests retained for their still-supported interface.

Opening rebuilds the full index. Source reads verify live hashes; search and inventory describe the most recent index. Refreshes within one server instance serialize with reads. Separate processes refreshing the same cached repository concurrently may encounter an operating-system file-lock error and need a retry.

Accepted input now includes local folders and ZIP files. Remote HTTP hosting, automatic attachment transfer, semantic embeddings, incremental indexing and resolved cross-file references remain future work. Python extraction retains the original top-level/direct-method scope. Other languages get text coverage until a tested extractor exists. These limits also appear in the README.

## ZIP import addition

`open_repository` and the terminal `open`, `overview`, `files`, and `outline` commands accept a ZIP using the existing argument. No new MCP tool or dependency is required. The archive module owns validation, bounded extraction, wrapper-directory handling, and snapshot publication. Repository opening indexes the resulting folder through the existing module interface.

The importer copies the archive into temporary storage while hashing it, validates its entries, extracts into staging, and publishes the complete snapshot with a same-filesystem rename. Snapshots persist in the managed cache and use the archive's SHA-256 as their directory name. Byte-identical imports share the snapshot. Updated archives get a separate snapshot. Source reads keep working after deletion of the original ZIP.

Checks cover path traversal, Windows drive and UNC paths, backslashes, alternate data streams, reserved filenames, case/Unicode collisions, file/directory conflicts, links and special files, malformed archives, CRC failures, encryption, archive/entry/extraction limits, and failed-import cleanup. Behavior tests cover wrapped and flat layouts, ignore rules, explicit exclusions, repeated imports, renamed archives, changed archives, persistence, terminal commands, and the complete stdio MCP workflow. The README records the limits and explains that chat-attachment transfer still depends on the host.
