"""Platform scope of the test suite.

The portable core runs everywhere. Modules below exercise host integrations or POSIX
primitives that the Windows OS adapter deliberately does not provide; they are excluded
before import, because several import POSIX-only modules at load time.
"""
from pathlib import Path
import sys

# Tests import the repository's `integrations` package (host bridges not shipped in the wheel).
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# macOS host integrations: zsh deployment scripts, launchd, Desktop paths and bundles. The Claude
# Desktop binding tests also rely on macOS giving a replaced socket a new inode.
MACOS_ONLY = {
    "test_antigravity_native_deployment", "test_chat_approval_deployment", "test_claude_desktop_binding",
    "test_claude_guided_binding", "test_claude_native_preflight", "test_claude_onboarding",
    "test_codex_cold_restore_deployment", "test_codex_desktop_deployment_config", "test_codex_quick_binding_deployment",
    "test_deployment_script_args", "test_native_conversation_deployment",
}
# Unix sockets (including the macOS Codex Desktop IPC probes), owner/mode bits, or the files.py
# workspace write, which the Windows adapter does not provide by design. The native-conversation
# providers and the Codex chat catalog are macOS-verified integrations whose fixtures use POSIX
# workspace paths.
POSIX_ONLY = {
    "test_adapter_socket", "test_claude_binding_usage", "test_claude_code_bridge", "test_claude_native_binding_mcp",
    "test_codex_binding_catalog", "test_codex_desktop_bridge", "test_codex_desktop_probe",
    "test_codex_desktop_status_probe", "test_codex_saved_bindings", "test_codex_selected_routes",
    "test_conversations", "test_example_configs", "test_files", "test_native_host",
}


def pytest_ignore_collect(collection_path, config):
    name = collection_path.stem
    if name in MACOS_ONLY and sys.platform != "darwin":
        return True
    if name in POSIX_ONLY and sys.platform == "win32":
        return True
    return None
