"""Service lifecycle without an OS supervisor: start on demand, detect port owners, stop on request."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import time

import httpx

from . import os_adapter


class ServiceError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def probe(url: str, token: str) -> str:
    """'ours' if the listener accepts this installation's credential, 'absent' if nothing listens, else 'foreign'."""
    try:
        with httpx.Client(base_url=url, trust_env=False, timeout=3) as http:
            response = http.post("/call", json={"action": "status"}, headers={"Authorization": f"Bearer {token}"})
    except httpx.ConnectError:
        return "absent"
    except httpx.HTTPError:
        return "foreign"
    return "ours" if response.status_code == 200 else "foreign"


def ensure(data: Path, url: str, token: str, *, runtime: str = sys.executable, wait: float = 20.0) -> str:
    """Return 'running' or 'started'; never takes over a port owned by something else."""
    state = probe(url, token)
    if state == "ours":
        return "running"
    if state == "foreign":
        raise ServiceError("port_collision", f"Another program is listening on {url}; the connector was not started.")
    os_adapter.spawn_detached([runtime, "-m", "local_ai_connector.cli", "--data", str(data), "serve"],
                              log_path=data / "service.log", cwd=data)
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        state = probe(url, token)
        if state == "ours":
            return "started"
        if state == "foreign":
            raise ServiceError("port_collision", f"Another program took {url}; the connector could not start.")
        time.sleep(0.1)
    raise ServiceError("service_start_failed", f"The service did not start within {wait:.0f} s; see {data / 'service.log'}.")


def ensure_for_endpoint(endpoint_file: Path) -> str:
    config = json.loads(endpoint_file.read_text())
    return ensure(endpoint_file.parent, config["url"], config["token"])


def stop(data: Path, *, wait: float = 15.0) -> str:
    """Ask this installation's service to exit; 'not_running' if it is not up, 'foreign' if the port is not ours."""
    config = json.loads((data / "server.json").read_text())
    try:
        with httpx.Client(base_url=config["url"], trust_env=False, timeout=5) as http:
            response = http.post("/admin", json={"action": "shutdown"},
                                 headers={"Authorization": f"Bearer {config['admin_token']}"})
    except httpx.ConnectError:
        return "not_running"
    if response.status_code != 200:
        return "foreign"
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        # Stopped means the process released the data directory, not only the port: on Windows
        # its database files stay locked until the process has exited.
        try:
            with os_adapter.exclusive_lock(data / "server.lock"):
                return "stopped"
        except os_adapter.LockBusy:
            time.sleep(0.1)
    raise ServiceError("service_stop_failed", "The service did not stop in time.")
