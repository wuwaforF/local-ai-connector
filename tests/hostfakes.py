"""Isolated host homes for installer tests. Never points at a real user's host configuration."""
import json
import os
from pathlib import Path
import sys

from local_ai_connector.install import Context

FAKE_CLAUDE = r'''
import json, os, pathlib, sys
args = sys.argv[1:]
home = pathlib.Path(os.environ.get("CLAUDE_CONFIG_DIR") or os.environ["HOME"])
path = home / ".claude.json"
document = json.loads(path.read_text()) if path.exists() else {}
servers = document.setdefault("mcpServers", {})
with (home / "claude-cli.log").open("a") as log:
    log.write(json.dumps(args) + "\n")
if args[:4] == ["mcp", "add-json", "--scope", "user"]:
    name, entry = args[4], json.loads(args[5])
    if name in servers:
        sys.exit("already exists")
    servers[name] = entry
elif args[:4] == ["mcp", "remove", "--scope", "user"]:
    if args[4] not in servers:
        sys.exit("not found")
    del servers[args[4]]
else:
    sys.exit(2)
path.write_text(json.dumps(document, indent=2))
'''

# Variables that could redirect an installer or host to real configuration.
_REAL = ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "XDG_DATA_HOME", "LOCALAPPDATA", "APPDATA", "USERPROFILE", "HOME",
         "LOCAL_AI_CONNECTOR_CLAUDE_CLI")


def isolated_environ(home: Path) -> dict:
    environ = {k: v for k, v in os.environ.items() if k not in _REAL}
    fake = home.parent / "fake_claude.py"
    fake.write_text(FAKE_CLAUDE)
    environ.update({"HOME": str(home), "USERPROFILE": str(home),
                    "LOCALAPPDATA": str(home / "AppData" / "Local"), "APPDATA": str(home / "AppData" / "Roaming"),
                    "LOCAL_AI_CONNECTOR_CLAUDE_CLI": json.dumps([sys.executable, str(fake)])})
    return environ


def context(tmp_path: Path, *, runtime: str = sys.executable) -> Context:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    return Context(home=home, environ=isolated_environ(home), runtime=runtime)


def grant_others(path: Path, *, write: bool):
    """Give a principal other than the owner read (or write) access."""
    if sys.platform == "win32":
        import ntsecuritycon as con
        import win32security
        everyone = win32security.ConvertStringSidToSid("S-1-1-0")
        descriptor = win32security.GetNamedSecurityInfo(str(path), win32security.SE_FILE_OBJECT,
                                                        win32security.DACL_SECURITY_INFORMATION)
        dacl = descriptor.GetSecurityDescriptorDacl()
        mask = con.FILE_GENERIC_WRITE if write else con.FILE_GENERIC_READ
        dacl.AddAccessAllowedAce(win32security.ACL_REVISION, mask, everyone)
        win32security.SetNamedSecurityInfo(str(path), win32security.SE_FILE_OBJECT,
                                           win32security.DACL_SECURITY_INFORMATION, None, None, dacl, None)
    else:
        os.chmod(path, 0o666 if write else 0o644)
