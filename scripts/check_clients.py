"""Run opt-in live MCP checks using installed, authenticated Codex and Claude CLIs."""

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--clients", nargs="+", choices=["codex", "claude"], default=["codex", "claude"]
    )
    args = parser.parse_args()
    base = Path(__file__).resolve().parents[1]
    results = {}
    with tempfile.TemporaryDirectory(prefix="explorer-client-check-") as temporary:
        temp = Path(temporary)
        roots = []
        for kind, files in {
            "python": {"main.py": "def checkout():\n    return 1\n"},
            "typescript": {"main.ts": "export function checkout() { return 1; }\n"},
            "mixed": {
                "main.py": "def checkout():\n    return 1\n",
                "main.ts": "export function checkout() { return 1; }\n",
                "main.go": "package main\nfunc main() {}\n",
            },
        }.items():
            root = temp / kind
            root.mkdir()
            for name, text in files.items():
                (root / name).write_text(text, encoding="utf-8")
            roots.append(str(root))
        command = [
            sys.executable,
            "-m",
            "explorer.mcp_server",
            "--cache-dir",
            str(temp / "cache"),
        ]
        config = temp / "mcp.json"
        config.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "codebase-explorer": {
                            "command": command[0],
                            "args": command[1:],
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        prompt = (
            "Use only codebase-explorer MCP tools. For EACH folder in "
            + json.dumps(roots)
            + ", call open_repository and retain its distinct repo_id. Call repo_overview, list_files, "
            "search_code with checkout, file_outline and read_source on the main file found. "
            "Report citations. Do not run shell commands, edit source, or execute repository code. "
            "Finish with MATRIX_PASS only if all three folders succeeded, otherwise explain the failure."
        )
        for client in args.clients:
            executable = shutil.which(client)
            if executable is None:
                results[client] = {"passed": False, "error": "CLI not installed"}
                continue
            if client == "codex":
                invocation = [
                    executable,
                    "exec",
                    "--ignore-user-config",
                    "--ephemeral",
                    "--sandbox",
                    "read-only",
                    "--json",
                    "-c",
                    "mcp_servers.codebase-explorer.command=" + json.dumps(command[0]),
                    "-c",
                    "mcp_servers.codebase-explorer.args=" + json.dumps(command[1:]),
                    "-c",
                    'mcp_servers.codebase-explorer.default_tools_approval_mode="auto"',
                    prompt,
                ]
            else:
                invocation = [
                    executable,
                    "-p",
                    prompt,
                    "--mcp-config",
                    str(config),
                    "--strict-mcp-config",
                    "--no-session-persistence",
                    "--tools",
                    "",
                    "--allowedTools",
                    "mcp__codebase-explorer__*",
                    "--permission-mode",
                    "dontAsk",
                    "--setting-sources",
                    "",
                    "--output-format",
                    "json",
                    "--max-budget-usd",
                    "1",
                ]
            try:
                run = subprocess.run(
                    invocation,
                    cwd=base,
                    capture_output=True,
                    text=True,
                    check=False,
                    encoding="utf-8",
                    errors="replace",
                    timeout=180,
                )
                if client == "claude":
                    record = json.loads(run.stdout)
                    text = record.get("result", "")
                else:
                    events = [
                        json.loads(line)
                        for line in run.stdout.splitlines()
                        if line.startswith("{")
                    ]
                    text = "\n".join(e.get("item", {}).get("text", "") for e in events)
                results[client] = {
                    "passed": run.returncode == 0 and "MATRIX_PASS" in text,
                    "exit_code": run.returncode,
                    "report": text[-4000:],
                }
            except (subprocess.TimeoutExpired, ValueError) as exc:
                results[client] = {"passed": False, "error": str(exc)}
            print(json.dumps({client: results[client]}, indent=2), flush=True)
    return 0 if all(r["passed"] for r in results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
