# Codebase Explorer

Indexs a Python repository into a local SQLite file, then search it from a terminal or from Claude Code. Every result is a real file and line range, and the tool checks the file hasn't changed before it returns any lines. No model runs the search and nothing leaves your machine, apart from the snippets Claude reads.

## Set up once

You need Python 3.10 or newer. Unzip the project to a permanent folder such as `C:\tools\codebase-explorer`. Moving it later breaks the Claude Code connection.

Open Command Prompt in that folder and run:

```
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[test]"
.venv\Scripts\python -m pytest
mkdir indexes
```

The tests should end with `47 passed`. On Mac and Linux, use `python3` and `.venv/bin/python` instead.

## Use it on a repository

Run these steps for every repository. The examples use a project called `shop` at `C:\Projects\shop`.

**1. Build the index.** Run this from the tool folder. Put paths with spaces in quotes.

```
.venv\Scripts\explorer --db indexes\shop.db index "C:\Projects\shop"
```

It prints how many symbols it found and any files it skipped.

**2. Try a search.**

```
.venv\Scripts\explorer --db indexes\shop.db search "checkout"
```

Each result looks like `src/cart.py:10-40  function  checkout`. To read those lines, run `show` with that path and line range.

```
.venv\Scripts\explorer --db indexes\shop.db show src/cart.py 10 40
```

**3. Connect Claude Code.** Go into the repository folder, then run this as one line. Use the full path to your tool folder.

```
cd "C:\Projects\shop"
claude mcp add codebase-explorer -- C:\tools\codebase-explorer\.venv\Scripts\explorer-mcp.exe --db C:\tools\codebase-explorer\indexes\shop.db
```

**4. Start Claude Code in that folder and ask.**

```
claude
```

Type `/mcp` to confirm `codebase-explorer` is connected. Then ask something like:

> Use the codebase-explorer tools to find where checkout is handled. Explain it step by step and cite each step as path:start-end.

## After you change the code

Run step 1 again. It rebuilds the index in seconds. Until you do, Claude reports "changed after indexing".

## For another repository

Repeat steps 1, 3 and 4 with a new index name such as `blog.db`. Run step 3 from inside the new repository's folder.

## If something goes wrong

- **"No index at ..."**. The `--db` value must end in a file name like `shop.db`. Run `claude mcp remove codebase-explorer`, then repeat step 3 as one line.
- **"defined in multiple scopes"**. Run `claude mcp remove codebase-explorer -s user` and `claude mcp remove codebase-explorer -s local`, each from inside the repository folder, then repeat step 3.
- **`codebase-explorer` missing from `/mcp`**. Start `claude` from the repository folder you ran step 3 in. If it was already running, type `/exit` and start it again.
- **"is not in the index"**. Copy the path from a search result. It uses forward slashes.
- **Search results are mostly tests**. Add `--path src`, or your code folder, to the search.

## Limits

- Python only, one repository per index.
- It indexes functions, classes, and methods. Nested functions and module-level variables are skipped.
- Search matches keywords, not meaning. "authentication" finds `authenticate`, but "login" won't find `sign_in`.
- Files with syntax errors are skipped and listed when you index.
