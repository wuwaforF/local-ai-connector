"""OS adapter contract. Runs on every CI platform; access for other users is granted natively."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from hostfakes import grant_others
from local_ai_connector import os_adapter
from local_ai_connector.core import ConnectorError
from local_ai_connector.wakeup import WakeConfigError, load_config


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
    # Released when the holder exits; Windows may release a killed process's lock with a delay.
    deadline = time.monotonic() + 10
    while True:
        try:
            with os_adapter.exclusive_lock(path):
                break
        except os_adapter.LockBusy:
            assert time.monotonic() < deadline, "lock was not released after the holder exited"
            time.sleep(0.1)


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


# --- helpers for the wake bridges -------------------------------------------------------------
def test_owned_text_is_read_as_utf8_and_must_be_ours(tmp_path):
    path = tmp_path / "record.json"
    path.write_bytes('{"cwd": "C:\\\\Users\\\\张伟\\\\Ünïcode"}'.encode("utf-8"))
    assert "张伟" in os_adapter.read_owned_text(path)  # independent of the locale code page
    if sys.platform != "win32":
        with pytest.raises(os_adapter.NotPrivate):
            os_adapter.read_owned_text(path, private=True)  # 0644 is readable by others
        path.chmod(0o600)
        assert "Ünïcode" in os_adapter.read_owned_text(path, private=True)
    grant_others(path, write=True)
    with pytest.raises(os_adapter.NotPrivate):
        os_adapter.read_owned_text(path)


def test_process_liveness_never_signals_and_reports_start_time():
    assert os_adapter.own_process_alive(os.getpid()) is True
    finished = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True)
    assert os_adapter.own_process_alive(int(finished.stdout)) is False
    for invalid in (0, -1, None, "123", True):
        assert os_adapter.own_process_alive(invalid) is False
    started = os_adapter.process_start_time(os.getpid())
    assert started is not None and time.time() - 86400 < started <= time.time() + 2


@pytest.mark.skipif(sys.platform == "win32", reason="Unix sockets")
def test_local_ipc_over_a_unix_socket_and_its_identity():
    import socket
    import tempfile
    with tempfile.TemporaryDirectory(prefix="ipc-", dir="/tmp") as directory:
        path = Path(directory) / "endpoint.sock"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(path))
            path.chmod(0o600)
            server.listen(1)
            identity = os_adapter.endpoint_identity(path)
            assert identity[0] == "socket"
            with os_adapter.connect_local_ipc(path, 2) as client:
                peer, _ = server.accept()
                with peer:
                    client.sendall(b"ping")
                    assert peer.recv(4) == b"ping"
            path.chmod(0o666)
            with pytest.raises(os_adapter.NotPrivate):
                os_adapter.endpoint_identity(path)
        with pytest.raises(ConnectionRefusedError):
            os_adapter.connect_local_ipc(path, 1)  # the server is gone; its socket file is not
    with pytest.raises(FileNotFoundError):
        os_adapter.connect_local_ipc(path, 1)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows named pipes")
@pytest.mark.parametrize("server_ends", ["disconnect", "close"])
def test_local_ipc_over_a_named_pipe_and_its_server_identity(server_ends):
    import threading
    import uuid
    import win32file
    import win32pipe
    name = rf"\\.\pipe\local-ai-connector-test-{uuid.uuid4().hex}"
    server = win32pipe.CreateNamedPipe(name, win32pipe.PIPE_ACCESS_DUPLEX,
                                       win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_READMODE_BYTE | win32pipe.PIPE_WAIT,
                                       1, 65536, 65536, 0, None)
    received = []

    def serve():
        win32pipe.ConnectNamedPipe(server, None)
        received.append(win32file.ReadFile(server, 4)[1])
        win32file.WriteFile(server, b"pong")
        win32file.FlushFileBuffers(server)
        if server_ends == "disconnect":
            win32pipe.DisconnectNamedPipe(server)  # the client then sees ERROR_PIPE_NOT_CONNECTED (233)
        server.Close()  # without a disconnect, the client sees ERROR_BROKEN_PIPE (109)

    thread = threading.Thread(target=serve)
    thread.start()
    with os_adapter.connect_local_ipc(name, 5) as client:
        assert os_adapter.endpoint_identity(name, client)[:2] == ("pipe", os.getpid())
        client.sendall(b"ping")
        assert client.recv(4) == b"pong"
        thread.join(5)
        assert received == [b"ping"]
        assert client.recv(4) == b""  # the server ended its side: end of stream, either way
        with pytest.raises(BrokenPipeError):
            client.sendall(b"late")
    with pytest.raises(FileNotFoundError):
        os_adapter.connect_local_ipc(name, 1)
    with pytest.raises(os_adapter.NotPrivate):
        os_adapter.endpoint_identity(name)  # a pipe is identified only through a connection
