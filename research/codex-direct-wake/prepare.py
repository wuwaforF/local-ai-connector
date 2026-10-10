"""Prepare the Codex direct wake-up test: an isolated connector profile loaded only in one test folder.

`setup codex --wake` runs under a throwaway home inside the folder, so it writes no global
configuration. Its entry is added to the folder's project config (`.codex/config.toml`), which also
turns the live `local_ai_connector` server off for chats in this folder. Codex applies project config
only once the folder is trusted. Setup's wake-up binding targets the chat each task was pinned to at
approval.

Run it with the Python environment of the checkout under test; the MCP entry and the wake bridge use it.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tomllib

import tomlkit

LIVE_SERVER = "local_ai_connector"


def prepare(workspace: Path, profile: str = "cwtest") -> dict:
    home = workspace / ".connector-home"
    home.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k not in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "XDG_DATA_HOME")}
    env["HOME"] = str(home)
    result = subprocess.run([sys.executable, "-m", "local_ai_connector.cli", "setup", "codex", "--profile", profile,
                             "--wake"], env=env, capture_output=True, text=True)
    if result.returncode != 0:
        sys.exit(result.stderr)
    report = json.loads(result.stdout)
    data = Path(report["data_dir"])
    entry = tomllib.loads((home / ".codex" / "config.toml").read_text())["mcp_servers"][report["entry"]]

    document = tomlkit.document()
    document.add(tomlkit.comment("Codex direct wake-up test. Applies only to chats in this folder once it is trusted."))
    servers = tomlkit.table(is_super_table=True)
    live = tomlkit.table()
    live["enabled"] = False
    servers[LIVE_SERVER] = live
    test = tomlkit.table()
    for key, value in entry.items():
        test[key] = value
    servers[report["entry"]] = test
    document["mcp_servers"] = servers
    (workspace / ".codex").mkdir(exist_ok=True)
    (workspace / ".codex" / "config.toml").write_text(tomlkit.dumps(document))

    if not (workspace / ".git").exists():
        subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    (workspace / ".gitignore").write_text(".connector-home/\n.probe/\n")
    record = {"data_dir": str(data), "server": report["entry"], "installation_id": report["installation_id"],
              "profile": profile, "wake": report["wake"]}
    (workspace / ".probe").mkdir(exist_ok=True)
    (workspace / ".probe" / "connector-test.json").write_text(json.dumps(record, indent=2) + "\n")
    return record


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--profile", default="cwtest")
    args = parser.parse_args()
    print(json.dumps(prepare(args.workspace.expanduser().resolve(), args.profile), indent=2))
