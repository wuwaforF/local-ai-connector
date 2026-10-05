"""Preview/install the registered Codex ingress's native conversation hooks."""
import argparse
import copy
import fcntl
import json
import os
from pathlib import Path
import plistlib
import shlex
import sqlite3
import subprocess
import sys
import time
import tomllib
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from local_ai_connector.registry import _stage_private


def prepare(root, data, peer):
    server_path = data / "server.json"
    server = json.loads(server_path.read_bytes())
    endpoint = json.loads((data / f"{peer}.json").read_bytes())
    binding = json.loads((data / "wakeup.json").read_bytes())["bindings"][peer]
    target = binding["target"]
    if (binding.get("command") != [str(root / ".venv/bin/python"), str(root / "integrations/codex_desktop/bridge.py")]
            or endpoint.get("bound_session") != "codex:" + target["thread_id"]
            or endpoint.get("peer") != peer or endpoint.get("token") != server["peers"][peer]):
        raise ValueError("The registered Codex bridge and ingress identity do not match")
    project = Path(target["workspace"])
    hooks_path, project_config = project / ".codex/hooks.json", project / ".codex/config.toml"
    paths = [server_path, hooks_path, project_config]
    for path in paths:
        if path.is_symlink() or not path.is_file() or path.stat().st_uid != os.getuid():
            raise ValueError(f"Expected an owner-controlled regular configuration: {path}")
    originals = {path: path.read_bytes() for path in paths}
    hooks = json.loads(originals[hooks_path])
    old_command = shlex.join([str(root / ".venv/bin/python"), str(root / "integrations/codex_desktop/creation_approval.py"),
                             "--store", str(data / "codex-creation-probe-g/grants.sqlite3"),
                             "--grant-id", "codex-create-20260929-g"])
    old_hooks = {"hooks": {"PermissionRequest": [{"matcher": "^mcp__codex_app__create_thread$",
                  "hooks": [{"type": "command", "command": old_command, "timeout": 10}]}]}}
    command = shlex.join([str(root / ".venv/bin/python"), str(root / "integrations/codex_desktop/conversation_hook.py"),
                         "--store", str(data / "state.sqlite3"), "--peer", peer,
                         "--session", target["thread_id"], "--cwd", str(project)])
    entry = {"matcher": "^mcp__codex_app__(create_thread|send_message_to_thread)$",
             "hooks": [{"type": "command", "command": command, "timeout": 10}]}
    new_hooks = {"hooks": {"PermissionRequest": [entry], "PostToolUse": [copy.deepcopy(entry)]}}
    if hooks not in (old_hooks, new_hooks):
        raise ValueError("Hooks differ from the known creation fixture or this installer; review before replacing")
    registration = {"provider": "codex_ingress", "session_id": target["thread_id"], "cwd": str(project)}
    entries = server.setdefault("conversations", {})
    if peer in entries and entries[peer] != registration:
        raise ValueError("A different native conversation registration already exists")
    entries[peer] = registration
    source = originals[project_config].decode()
    document = tomllib.loads(source)
    worker = document.get("mcp_servers", {}).get("local_ai_connector_codex_desktop_worker", {})
    if (worker.get("command") != str(root / ".venv/bin/python")
            or worker.get("args") != ["-m", "local_ai_connector.cli", "mcp", "--config", str(data / f"{peer}.json")]):
        raise ValueError("The ingress project MCP configuration points elsewhere")
    tools = document.get("plugins", {}).get("codex-app-tools@openai-bundled", {}).get("mcp_servers", {}).get("codex_app", {}).get("tools", {})
    for tool in ("create_thread", "send_message_to_thread"):
        if tool in tools:
            if tools[tool].get("approval_mode") != "prompt" or tools[tool].get("enabled") is False:
                raise ValueError("A conflicting native tool policy requires review")
        else:
            source += f'\n[plugins."codex-app-tools@openai-bundled".mcp_servers.codex_app.tools.{tool}]\napproval_mode = "prompt"\n'
    tomllib.loads(source)
    replacements = {server_path: (json.dumps(server, indent=2, ensure_ascii=False) + "\n").encode(),
                    hooks_path: (json.dumps(new_hooks, indent=2) + "\n").encode(), project_config: source.encode()}
    return originals, replacements


def quiet(data):
    with sqlite3.connect((data / "state.sqlite3").as_uri() + "?mode=ro", uri=True) as db:
        if db.execute("SELECT count(*) FROM channels WHERE status='pending'").fetchone()[0] or db.execute(
                "SELECT count(*) FROM messages m JOIN channels c ON c.id=m.channel "
                "WHERE c.status='active' AND m.kind='question' AND m.resolved=0").fetchone()[0]:
            raise ValueError("Wait for pending approvals and unanswered connector work to finish")


def listener(port, expected):
    result = subprocess.run(["/usr/sbin/lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
                            capture_output=True, text=True, check=False)
    if result.returncode not in (0, 1):
        raise RuntimeError("Cannot inspect connector listener: " + result.stderr)
    pids = set(result.stdout.split())
    if not pids:
        return None
    if len(pids) != 1:
        raise RuntimeError("Multiple connector listeners")
    pid = pids.pop()
    if (subprocess.check_output(["/bin/ps", "-p", pid, "-o", "uid="], text=True).strip() != str(os.getuid())
            or subprocess.check_output(["/bin/ps", "-ww", "-p", pid, "-o", "command="], text=True).strip() != " ".join(expected)):
        raise RuntimeError("The listener is not the installed connector process")
    return pid


def replace(path, content):
    staged = _stage_private(path, content)
    try:
        os.replace(staged, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        staged.unlink(missing_ok=True)


def apply(root, data, originals, replacements, plist_path, *, readiness=None, precommit=None):
    quiet(data)
    label, domain = "dev.local-ai-connector.service", f"gui/{os.getuid()}"
    job = domain + "/" + label
    expected = [str(root / ".venv/bin/python"), "-m", "local_ai_connector.cli", "--data", str(data), "serve"]
    plist = plistlib.loads(plist_path.read_bytes())
    if (plist.get("Label") != label or plist.get("ProgramArguments") != expected
            or plist.get("EnvironmentVariables", {}).get("PYTHONPATH") != str(root / "src")):
        raise ValueError("Installed launchd definition does not match this connector")
    server = json.loads(originals[data / "server.json"])
    previous = listener(server["port"], expected)
    if previous is None:
        raise RuntimeError("The connector service is not running")
    jobs = [line.split() for line in subprocess.check_output(["/bin/launchctl", "list"], text=True).splitlines()
            if len(line.split()) == 3 and line.split()[2] == label]
    if len(jobs) != 1 or jobs[0][0] != previous:
        raise RuntimeError("The listener does not belong to the installed launchd job")
    backup = data / ("native-conversations-backup-" + str(uuid4()))
    backup.mkdir(mode=0o700)
    for index, (path, content) in enumerate(originals.items()):
        if content is not None:
            replace(backup / f"{index}-{path.name}", content)
    replace(backup / "manifest.json", json.dumps([str(p) for p in originals]).encode())
    absent = [str(path) for path, content in originals.items() if content is None]
    if absent:
        replace(backup / "originally-absent.json", json.dumps(absent).encode())
    subprocess.run(["/bin/launchctl", "bootout", job], check=True)
    written = []
    created_directories = []
    started = False

    def start_and_wait():
        nonlocal started
        subprocess.run(["/bin/launchctl", "bootstrap", domain, str(plist_path)], check=True)
        started = True
        for _ in range(40):
            current = listener(server["port"], expected)
            if current and current != previous:
                return current
            time.sleep(1)
        raise RuntimeError("The replacement service did not become ready within 40 seconds")

    try:
        with (data / "server.lock").open("a") as lock:
            for _ in range(50):
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    time.sleep(0.1)
            else:
                raise RuntimeError("The old service did not release its lock")
            quiet(data)
            if any((path.read_bytes() if path.exists() else None) != original for path, original in originals.items()):
                raise RuntimeError("Configuration changed during deployment")
            if precommit is not None:
                precommit()
            for path, content in replacements.items():
                if content != originals[path]:
                    if not path.parent.exists():
                        path.parent.mkdir(mode=0o700)
                        created_directories.append(path.parent)
                    replace(path, content)
                    written.append(path)
        current = start_and_wait()
        if readiness is not None:
            readiness()
    except BaseException as failure:
        try:
            if started:
                subprocess.run(["/bin/launchctl", "bootout", job], check=True)
                started = False
            for path in reversed(written):
                if path.read_bytes() != replacements[path]:
                    raise RuntimeError(f"Configuration changed after install; restore from {backup}: {path}")
                if originals[path] is None:
                    path.unlink()
                else:
                    replace(path, originals[path])
            for directory in reversed(created_directories):
                directory.rmdir()
            start_and_wait()
        except BaseException as recovery:
            raise BaseExceptionGroup(f"Deployment and recovery failed; backup: {backup}", [failure, recovery])
        raise
    print(f"Connector restarted as PID {current}; backup: {backup}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    data = Path.home() / ".local/share/local-ai-connector"
    originals, replacements = prepare(ROOT, data, "codex_desktop")
    changed = [path for path in originals if originals[path] != replacements[path]]
    for path in changed:
        print("Update:", path)
    if not changed:
        print("Already installed. Review/trust the project hooks and reopen Codex before testing.")
    elif args.apply:
        apply(ROOT, data, originals, replacements,
              Path.home() / "Library/LaunchAgents/dev.local-ai-connector.service.plist")
        print("Review/trust the changed ingress project hooks, fully reopen Codex, and reload the connector in Antigravity/Claude. Then initiate acceptance manually.")
    else:
        print("Dry run only. --apply replaces the old creation fixture with broker-bound hooks and registers native creation. No files or services changed.")


if __name__ == "__main__":
    main()
