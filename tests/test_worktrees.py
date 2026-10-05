import subprocess


def test_isolated_worktrees_and_checked_handoff(tmp_path):
    repo=tmp_path/"repo";repo.mkdir()
    def git(*args,cwd=repo,check=True):
        return subprocess.run(["git",*args],cwd=cwd,check=check,capture_output=True)
    git("init","-q")
    (repo/"shared.txt").write_text("baseline\n")
    git("add","shared.txt")
    git("-c","user.name=Connector Test","-c","user.email=test@example.invalid","commit","-qm","baseline","--no-gpg-sign")
    base=git("rev-parse","HEAD").stdout.decode().strip()
    a,b=tmp_path/"codex",tmp_path/"zcode"
    git("worktree","add","--detach",str(a),base)
    git("worktree","add","--detach",str(b),base)
    (a/"shared.txt").write_text("codex change\n")
    (b/"shared.txt").write_text("zcode change\n")
    assert (repo/"shared.txt").read_text()=="baseline\n"
    assert (a/"shared.txt").read_text()!=(b/"shared.txt").read_text()
    patch=tmp_path/"handoff.patch"
    patch.write_bytes(git("diff","--binary",base,cwd=a).stdout)
    assert git("apply","--check",str(patch),cwd=b,check=False).returncode!=0
    assert (b/"shared.txt").read_text()=="zcode change\n"
    git("diff","--check",cwd=a)
    git("diff","--check",cwd=b)
