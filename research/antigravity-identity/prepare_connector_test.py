"""Add an isolated connector test profile to the probe workspace. No global host configuration changes.

`setup` runs with a dedicated test home inside the workspace, so its data directory and the
host entry it writes stay there. The entry is then loaded only through this workspace's
`.agents/mcp_config.json`, and a workspace hook denies the live `local_ai_connector` server so
test chats cannot reach an existing installation.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

import make_workspace

LIVE_SERVER = "local_ai_connector"


def prepare(workspace: Path, profile: str = "agtest") -> dict:
    home = workspace / ".connector-home"
    home.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k not in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "XDG_DATA_HOME")}
    env.update(HOME=str(home), USERPROFILE=str(home), LOCALAPPDATA=str(home / "AppData" / "Local"))
    result = subprocess.run([sys.executable, "-m", "local_ai_connector.cli", "setup", "antigravity", "--profile", profile,
                             "--no-start"], env=env, capture_output=True, text=True)
    if result.returncode != 0:
        sys.exit(result.stderr)
    report = json.loads(result.stdout)
    entry = json.loads((home / ".gemini/config/mcp_config.json").read_text())["mcpServers"][report["entry"]]
    make_workspace.build(workspace, extra_servers={report["entry"]: entry}, deny_server=LIVE_SERVER)
    record = {"data_dir": report["data_dir"], "server": report["entry"], "installation_id": report["installation_id"]}
    (workspace / ".probe" / "connector-test.json").write_text(json.dumps(record, indent=2) + "\n")
    return record


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=Path)
    print(json.dumps(prepare(parser.parse_args().workspace.expanduser().resolve()), indent=2))
