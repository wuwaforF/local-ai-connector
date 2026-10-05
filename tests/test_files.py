import multiprocessing
from pathlib import Path
import pytest
from local_ai_connector.files import write_lease,write_file,snapshot
from local_ai_connector.core import ConnectorError

pytestmark = pytest.mark.posix  # Unix sockets, owner/mode bits or fcntl


def hold(root,ready,release):
    with write_lease(Path(root)):
        ready.set()
        release.wait(10)


def test_actual_overlapping_processes_and_stale_version(tmp_path):
    f=tmp_path/"shared.txt"
    f.write_text("original")
    before=snapshot(tmp_path,"shared.txt")
    ctx=multiprocessing.get_context("spawn")
    ready,release=ctx.Event(),ctx.Event()
    p=ctx.Process(target=hold,args=(str(tmp_path),ready,release))
    p.start()
    try:
        assert ready.wait(10)
        with pytest.raises(ConnectorError,match="占用"):
            write_file(tmp_path,"shared.txt",b"overlap",before)
        assert f.read_text()=="original"
    finally:
        release.set(); p.join(10)
        if p.is_alive(): p.terminate(); p.join()
    assert p.exitcode==0
    first=write_file(tmp_path,"shared.txt",b"first",before)
    with pytest.raises(ConnectorError,match="版本"):
        write_file(tmp_path,"shared.txt",b"stale",before)
    assert f.read_text()=="first"
    assert write_file(tmp_path,"shared.txt",b"merged",first)==snapshot(tmp_path,"shared.txt")


def test_paths_links_and_new_file(tmp_path):
    outside=tmp_path/"outside"
    outside.write_text("protected")
    root=tmp_path/"root";root.mkdir()
    (root/"link").symlink_to(outside)
    (root/"dir").symlink_to(tmp_path,target_is_directory=True)
    for relative in ("../outside","/outside",".git/config","a//b"):
        with pytest.raises(ConnectorError): write_file(root,relative,b"x",None)
    for relative in ("link","dir/outside"):
        with pytest.raises(OSError): write_file(root,relative,b"x",None)
    (root/"hard").hardlink_to(outside)
    with pytest.raises(ConnectorError): write_file(root,"hard",b"x",None)
    assert outside.read_text()=="protected"
    write_file(root,"new.txt",b"new",None)
    assert (root/"new.txt").read_bytes()==b"new"
