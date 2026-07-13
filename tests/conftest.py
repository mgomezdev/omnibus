import os
from pathlib import Path

import pytest


def _load_dotenv(path: Path) -> None:
    """Parse a .env file and populate os.environ for keys not already set."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


# Load project-root .env so ORDINUS_PORT, HOST_PORT, etc. are available
_load_dotenv(Path(__file__).parent.parent / ".env")


def pytest_addoption(parser):
    parser.addoption(
        "--integration",
        action="store_true",
        default=False,
        help="Run integration tests (requires live Docker stack)",
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "integration: requires the full Concordia Docker stack to be running",
    )
    config.addinivalue_line(
        "markers",
        "slow: invokes OrcaSlicer; deselect with -m 'not slow'",
    )


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--integration"):
        skip = pytest.mark.skip(reason="pass --integration to run")
        for item in items:
            if "integration" in item.keywords:
                item.add_marker(skip)
