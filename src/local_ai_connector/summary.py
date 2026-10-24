"""Readable summaries of setup, doctor and uninstall for people; --json prints the raw reports."""
from __future__ import annotations

from pathlib import Path

from .hosts import HOSTS

_MARK = {True: "[ok]", None: "[--]", False: "[!!]"}


def _home(path: str) -> str:
    """Show paths under the home folder as ~/..."""
    try:
        return "~/" + Path(path).relative_to(Path.home()).as_posix()
    except ValueError:
        return path


def _entry_line(report: dict) -> str:
    actions = {a["action"] for a in report["actions"]}
    where = f"'{report['entry']}' in {_home(report['config_file'])}"
    if "write_host_entry" not in actions:
        return f"already set up: {where}"
    return f"{'would add' if report['dry_run'] else 'added'} {where}"


def _wake_line(report: dict) -> str:
    wake = report.get("wake")
    if wake == "enabled":
        return "on: the bound chat is woken when a task is approved"
    if wake == "off":
        return f"off (turn it on with: local-ai-connector setup {report['host']} --wake)"
    if wake == "not managed by setup":
        return "set by hand in wakeup.json; setup leaves it alone"
    return "not available here"


def setup_text(result: dict) -> str:
    lines = [f"Local AI Connector, profile '{result['profile']}'" + (" (dry run, nothing written)" if result["dry_run"] else ""), ""]
    for report in result["setups"]:
        name = HOSTS[report["host"]].DISPLAY_NAME
        lines += [name, f"  MCP entry: {_entry_line(report)}", f"  Wake-up:   {_wake_line(report)}"]
        if report.get("wake_note"):
            lines.append(f"  Note:      {report['wake_note']}")
    services = [r["service"] for r in result["setups"] if r.get("service")]
    if services:
        url = result["setups"][-1].get("service_url")
        lines += ["", "Service: " + {"started": "started", "running": "running"}.get(services[-1], services[-1])
                  + (f" at {url}" if url else "")]
    elif not result["dry_run"]:
        lines += ["", "Service: not started now; it starts with the first chat that uses the connector."]

    lines += ["", "Next steps"]
    step = 1
    for report in result["setups"]:
        lines.append(f"  {step}. {HOSTS[report['host']].DISPLAY_NAME}: {report['permissions'][0]}")
        step += 1
    lines.append(f"  {step}. In each chat that should receive tasks, say: \"Use this chat for connector tasks.\"")
    lines.append("     When one chat hands a task to another app, it can bind itself in the same approval, so that "
                 "app can send tasks back.")
    others = [(r["host"], text) for r in result["setups"] for text in r["permissions"][1:]]
    if others:
        lines += ["", "Permissions (setup grants none)"]
        lines += [f"  - {HOSTS[host].DISPLAY_NAME}: {text}" for host, text in others]
    if result.get("doctor") is not None:
        lines += ["", doctor_summary(result["doctor"])]
    return "\n".join(lines)


def doctor_summary(report: dict) -> str:
    checks = report["checks"]
    passed = sum(1 for c in checks if c["ok"] is True)
    failed = [c for c in checks if c["ok"] is False]
    unchecked = sum(1 for c in checks if c["ok"] is None)
    head = "Checks: ready" if report["ready"] else "Checks: NOT READY"
    text = f"{head} ({passed} passed, {len(failed)} failed, {unchecked} not checked or not enabled)"
    if failed:
        text += "".join(f"\n  {_MARK[False]} {c['check']}: {c['detail']}" for c in failed)
        text += "\n  Run local-ai-connector doctor for every check."
    return text


def doctor_text(report: dict) -> str:
    lines = [f"Local AI Connector, profile '{report['profile']}': " + ("ready" if report["ready"] else "NOT READY"), ""]
    width = max((len(c["check"]) for c in report["checks"]), default=0)
    for c in report["checks"]:
        note = "" if c["evidence"] == "verified" else f" ({c['evidence']})"
        lines.append(f"  {_MARK[c['ok']]} {c['check'].ljust(width)}  {c['detail']}{note}")
    return "\n".join(lines)


def uninstall_text(report: dict) -> str:
    lines = [f"Local AI Connector, profile '{report['profile']}'" + (" (dry run, nothing removed)" if report["dry_run"] else "")]
    states = {"removed": "removed", "already_absent": "was already gone", "not_owned": "not set up by this installation",
              "modified_left_in_place": "left in place: it was edited after setup"}
    for entry in report["host_entries"]:
        name = HOSTS[entry["host"]].DISPLAY_NAME if entry["host"] in HOSTS else entry["host"]
        line = f"  {name}: MCP entry {states.get(entry['state'], entry['state'])}"
        if entry.get("wake"):
            line += f"; wake-up {states.get(entry['wake'], entry['wake'])}"
        lines.append(line)
    if report.get("remaining_entries"):
        lines.append("  Still set up: " + ", ".join(HOSTS[h].DISPLAY_NAME for h in report["remaining_entries"]))
    if report.get("data_dir") == "removed":
        lines.append("  Data folder removed.")
    return "\n".join(lines)
