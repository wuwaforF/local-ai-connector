import sys

import pytest


def pytest_collection_modifyitems(config, items):
    for item in items:
        if "macos" in item.keywords and sys.platform != "darwin":
            item.add_marker(pytest.mark.skip(reason="macOS host integration"))
        elif "posix" in item.keywords and sys.platform == "win32":
            item.add_marker(pytest.mark.skip(reason="needs POSIX primitives that the Windows adapter does not provide"))
