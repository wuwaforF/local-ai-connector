import os
from pathlib import Path
import subprocess

import pytest

pytestmark = pytest.mark.macos  # macOS deployment scripts, launchd or Desktop paths

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("script, required", [
    ("register-codex-desktop.command", "--thread-id, --workspace"),
    ("enable-codex-desktop-cold-restore.command", "--thread-id, --workspace"),
    ("migrate-codex-communication-permissions.command", "--worker-project, --antigravity-project-id"),
])
def test_owner_scripts_require_an_explicit_target_before_any_side_effect(tmp_path, script, required):
    # Targets used to be one maintainer's chat and project; they must now come from the caller.
    result = subprocess.run(["/bin/zsh", str(ROOT / "deployment" / script)], capture_output=True, text=True,
                            env={**os.environ, "HOME": str(tmp_path)}, timeout=30)

    assert result.returncode == 2
    assert "required: " + required in result.stderr
    assert list(tmp_path.iterdir()) == []


def test_codex_registration_rejects_a_non_uuid_thread_and_relative_workspace(tmp_path):
    script = ROOT / "deployment/register-codex-desktop.command"
    for args in (["--thread-id", "not-a-thread", "--workspace", "/workspaces/worker"],
                 ["--thread-id", "0190f2c1-7a3b-7c4d-8e5f-6a7b8c9d0e1f", "--workspace", "relative/worker"]):
        result = subprocess.run(["/bin/zsh", str(script), *args], capture_output=True, text=True,
                                env={**os.environ, "HOME": str(tmp_path)}, timeout=30)
        assert result.returncode == 1
        assert "Nothing changed" in result.stderr
    assert list(tmp_path.iterdir()) == []
