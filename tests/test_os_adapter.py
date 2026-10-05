"""OS adapter contract. Runs on every CI platform; access for other users is granted natively."""
import json
import os
from pathlib import Path
import sys
import time

import pytest

from local_ai_connector import os_adapter
from local_ai_connector.core import ConnectorError
from local_ai_connector.wakeup import WakeConfigError, load_config


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


def test_application_directories_are_per_user_and_platform_specific(tmp_path):
    home = tmp_path / "home"
    assert os_adapter.app_data_dir(platform="darwin", environ={}, home=home) == \
        home / "Library" / "Application Support" / "Local AI Connector"
    assert os_adapter.app_data_dir(platform="linux", environ={}, home=home) == home / ".local/share/local-ai-connector"
    assert os_adapter.app_data_dir(platform="linux", environ={"XDG_DATA_HOME": str(tmp_path / "xdg")}, home=home) == \
        tmp_path / "xdg" / "local-ai-connector"
    # A relative XDG value is ignored, as the XDG specification requires.
    assert os_adapter.app_data_dir(platform="linux", environ={"XDG_DATA_HOME": "rel"}, home=home) == \
        home / ".local/share/local-ai-connector"
    local = tmp_path / "Local"
    assert os_adapter.app_data_dir(platform="win32", environ={"LOCALAPPDATA": str(local)}, home=home) == \
        local / "Local AI Connector"


def test_exclusive_lock_excludes_a_second_holder_and_releases(tmp_path):
    path = tmp_path / "server.lock"
    with os_adapter.exclusive_lock(path):
        with pytest.raises(os_adapter.LockBusy):
            with os_adapter.exclusive_lock(path):
                pass
        assert isinstance(os_adapter.LockBusy("x"), BlockingIOError)
    with os_adapter.exclusive_lock(path):
        pass


def test_lock_is_exclusive_across_processes(tmp_path):
    import subprocess
    path = tmp_path / "server.lock"
    ready = tmp_path / "ready"
    holder = subprocess.Popen([sys.executable, "-c", (
        "import sys,time,pathlib\n"
        "from local_ai_connector import os_adapter\n"
        "with os_adapter.exclusive_lock(pathlib.Path(sys.argv[1])):\n"
        "    pathlib.Path(sys.argv[2]).write_text('1')\n"
        "    time.sleep(30)\n"), str(path), str(ready)])
    try:
        for _ in range(200):
            if ready.exists():
                break
            time.sleep(0.05)
        assert ready.exists()
        with pytest.raises(os_adapter.LockBusy):
            with os_adapter.exclusive_lock(path):
                pass
    finally:
        holder.kill()
        holder.wait()
    with os_adapter.exclusive_lock(path):  # released when the holder exits
        pass


def test_private_directory_and_files_are_verified(tmp_path):
    data = tmp_path / "profile" / "data"
    os_adapter.ensure_private_dir(data)
    os_adapter.ensure_private_dir(data)  # idempotent
    secret = data / "endpoint.json"
    os_adapter.create_private_file(secret, b'{"token": "x"}')
    assert os_adapter.is_private(data) and os_adapter.is_private(secret)
    with pytest.raises(FileExistsError):
        os_adapter.create_private_file(secret, b"other")
    os_adapter.replace_private_file(secret, b'{"token": "y"}')
    assert secret.read_bytes() == b'{"token": "y"}' and os_adapter.is_private(secret)
    assert not [p for p in data.iterdir() if p.name.startswith(".tmp-")]


@pytest.mark.parametrize("write", [False, True])
def test_access_by_other_principals_is_detected(tmp_path, write):
    os_adapter.ensure_private_dir(tmp_path / "d")
    path = tmp_path / "d" / "file.json"
    os_adapter.create_private_file(path, b"{}")
    grant_others(path, write=write)
    assert not os_adapter.is_private(path)
    assert os_adapter.is_private(path, writable_only=True) is (not write)


def test_wakeup_config_writable_by_others_is_rejected(tmp_path):
    os_adapter.ensure_private_dir(tmp_path / "data")
    data = tmp_path / "data"
    os_adapter.create_private_file(data / "wakeup.json", json.dumps({"enabled": False, "bindings": {}}).encode())
    assert load_config(data, set()) == ({}, None)
    grant_others(data / "wakeup.json", write=True)
    with pytest.raises(WakeConfigError):
        load_config(data, set())


@pytest.mark.skipif(sys.platform == "win32", reason="symbolic links need privileges on Windows")
def test_symbolic_links_are_not_private(tmp_path):
    target = tmp_path / "target"
    os_adapter.ensure_private_dir(tmp_path / "real")
    os_adapter.create_private_file(target, b"{}")
    link = tmp_path / "real" / "link"
    link.symlink_to(target)
    assert not os_adapter.is_private(link)


def test_unverified_capabilities_are_reported_not_emulated(tmp_path):
    if sys.platform == "win32":
        assert not os_adapter.supports("unix_socket_bridge")
        assert not os_adapter.supports("safe_workspace_write")
        from local_ai_connector.files import write_file
        with pytest.raises(ConnectorError) as error:
            write_file(tmp_path, "a.txt", b"x", None)
        assert error.value.code == "unsupported_platform"
        with pytest.raises(os_adapter.Unsupported):
            os_adapter.require("unix_socket_bridge")
    else:
        assert os_adapter.supports("unix_socket_bridge") and os_adapter.supports("safe_workspace_write")


def test_detached_process_outlives_its_parent_session(tmp_path):
    marker = tmp_path / "done"
    pid = os_adapter.spawn_detached(
        [sys.executable, "-c", "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('ok')", str(marker)],
        log_path=tmp_path / "child.log")
    assert pid > 0
    for _ in range(200):
        if marker.exists():
            break
        time.sleep(0.05)
    assert marker.read_text() == "ok"
