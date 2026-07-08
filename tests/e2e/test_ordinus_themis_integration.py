"""
Integration test: Ordinus → Themis "send-to-themis" integration.

Verifies:
  1. Sending a layout creates a Themis project with correct source fields
     (source_app, source_user, source_layout_id) and stores themisProjectId
     back in Ordinus bomGenerations.
  2. Resending the same layout reuses Themis library file IDs (per-layout dedup).
  3. Two different layouts that share the same bin model reuse the same Themis
     library file IDs — no duplicate library items are created.

Requirements:
  - Ordinus running at ORDINUS_URL (default http://localhost:3001)
  - Themis  running at THEMIS_URL  (default http://localhost:8001)
  - Default admin account: admin@gridfinity.local / admin

Run:
    pytest tests/e2e/test_ordinus_themis_integration.py --integration
    ORDINUS_URL=http://localhost:3001 pytest tests/e2e/test_ordinus_themis_integration.py --integration
"""

from __future__ import annotations

import os
import time
from typing import Any

import pytest
import requests

ORDINUS_URL = os.environ.get("ORDINUS_URL", "http://localhost:3001")
THEMIS_URL  = os.environ.get("THEMIS_URL",  "http://localhost:8001")

ADMIN_EMAIL    = "admin@gridfinity.local"
ADMIN_PASSWORD = "admin"

BOM_TIMEOUT_S   = 90
POLL_INTERVAL_S = 5


# ── helpers ───────────────────────────────────────────────────────────────────

def _login(session: requests.Session) -> str:
    resp = session.post(
        f"{ORDINUS_URL}/api/v1/auth/login",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["data"]["accessToken"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _first_library_item(session: requests.Session, token: str) -> tuple[str, dict[str, Any]]:
    """Return (library_id, item) for the first available item."""
    resp = session.get(f"{ORDINUS_URL}/api/v1/libraries", headers=_auth(token), timeout=10)
    resp.raise_for_status()
    lib_id: str = resp.json()["data"][0]["id"]

    resp = session.get(f"{ORDINUS_URL}/api/v1/libraries/{lib_id}/items", headers=_auth(token), timeout=10)
    resp.raise_for_status()
    item: dict[str, Any] = resp.json()["data"][0]
    return lib_id, item


def _create_layout(session: requests.Session, token: str, name: str,
                   lib_id: str, item: dict[str, Any], quantity: int) -> int:
    resp = session.post(
        f"{ORDINUS_URL}/api/v1/layouts",
        headers=_auth(token),
        json={
            "name": name,
            "gridX": 4, "gridY": 2, "widthMm": 168.0, "depthMm": 84.0,
            "placedItems": [{
                "libraryId": lib_id,
                "itemId": item["id"],
                "x": 0, "y": 0,
                "width": item["widthUnits"],
                "height": item["heightUnits"],
                "rotation": 0,
                "quantity": quantity,
            }],
        },
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["data"]["id"]


def _generate_and_wait(session: requests.Session, token: str,
                       layout_id: int, lib_id: str, item: dict[str, Any],
                       quantity: int) -> None:
    resp = session.post(
        f"{ORDINUS_URL}/api/v1/bom/generate/{layout_id}",
        headers=_auth(token),
        json={"bomItems": [{
            "libraryId": lib_id,
            "itemId": item["id"],
            "widthUnits": item["widthUnits"],
            "heightUnits": item["heightUnits"],
            "quantity": quantity,
        }]},
        timeout=10,
    )
    resp.raise_for_status()

    deadline = time.monotonic() + BOM_TIMEOUT_S
    while time.monotonic() < deadline:
        time.sleep(POLL_INTERVAL_S)
        r = session.get(f"{ORDINUS_URL}/api/v1/bom/generation/{layout_id}",
                        headers=_auth(token), timeout=10)
        r.raise_for_status()
        status = r.json()["data"]["status"]
        if status == "ready":
            return
        if status == "error":
            pytest.fail(f"BOM generation failed for layout {layout_id}: "
                        f"{r.json()['data'].get('errorMessage')}")
    pytest.fail(f"BOM generation timed out for layout {layout_id}")


def _send_to_themis(session: requests.Session, token: str, layout_id: int) -> int:
    resp = session.post(
        f"{ORDINUS_URL}/api/v1/bom/send-to-themis/{layout_id}",
        headers=_auth(token),
        timeout=30,
    )
    resp.raise_for_status()
    url: str = resp.json()["data"]["projectUrl"]
    return int(url.rstrip("/").split("/")[-1])


def _themis_project_file_ids(project_id: int) -> list[int]:
    resp = requests.get(f"{THEMIS_URL}/api/v1/projects/{project_id}", timeout=10)
    resp.raise_for_status()
    return sorted(item["file_id"] for item in resp.json().get("items", []))


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def ordinus(request: pytest.FixtureRequest) -> requests.Session:
    s = requests.Session()
    try:
        s.get(f"{ORDINUS_URL}/api/v1/health", timeout=5).raise_for_status()
    except Exception as exc:
        pytest.skip(f"Ordinus not reachable at {ORDINUS_URL}: {exc}")
    return s


@pytest.fixture(scope="module")
def themis(request: pytest.FixtureRequest) -> requests.Session:
    s = requests.Session()
    try:
        s.get(f"{THEMIS_URL}/api/v1/health", timeout=5).raise_for_status()
    except Exception as exc:
        pytest.skip(f"Themis not reachable at {THEMIS_URL}: {exc}")
    return s


@pytest.fixture(scope="module")
def token(ordinus: requests.Session) -> str:
    return _login(ordinus)


@pytest.fixture(scope="module")
def library_item(ordinus: requests.Session, token: str) -> tuple[str, dict[str, Any]]:
    return _first_library_item(ordinus, token)


# ── tests ─────────────────────────────────────────────────────────────────────

@pytest.mark.integration
def test_send_to_themis_source_fields_and_bidirectional_link(
    ordinus: requests.Session,
    themis: requests.Session,
    token: str,
    library_item: tuple[str, dict[str, Any]],
) -> None:
    """Themis project carries Ordinus source fields; themisProjectId stored back in Ordinus."""
    lib_id, item = library_item

    layout_id = _create_layout(ordinus, token, "E2E: source fields test", lib_id, item, quantity=2)
    _generate_and_wait(ordinus, token, layout_id, lib_id, item, quantity=2)
    themis_pid = _send_to_themis(ordinus, token, layout_id)

    # Bidirectional link: themisProjectId written back to bomGenerations
    gen = ordinus.get(f"{ORDINUS_URL}/api/v1/bom/generation/{layout_id}",
                      headers=_auth(token), timeout=10).json()["data"]
    assert gen["themisProjectId"] == themis_pid, (
        f"Expected themisProjectId={themis_pid}, got {gen['themisProjectId']}"
    )

    # Source fields on Themis project
    proj = themis.get(f"{THEMIS_URL}/api/v1/projects/{themis_pid}", timeout=10).json()
    assert proj["source_app"] == "ordinus"
    assert proj["source_user"] == "admin"
    assert proj["source_layout_id"] == layout_id


@pytest.mark.integration
def test_resend_same_layout_reuses_file_ids(
    ordinus: requests.Session,
    themis: requests.Session,
    token: str,
    library_item: tuple[str, dict[str, Any]],
) -> None:
    """Resending the same layout to Themis reuses the same library file IDs (per-layout dedup)."""
    lib_id, item = library_item

    layout_id = _create_layout(ordinus, token, "E2E: resend dedup test", lib_id, item, quantity=1)
    _generate_and_wait(ordinus, token, layout_id, lib_id, item, quantity=1)

    pid_first  = _send_to_themis(ordinus, token, layout_id)
    pid_second = _send_to_themis(ordinus, token, layout_id)

    assert pid_first != pid_second, "Each send should create a new Themis project"
    assert _themis_project_file_ids(pid_first) == _themis_project_file_ids(pid_second), (
        "Resending the same layout must reuse Themis library file IDs — no duplicates"
    )


@pytest.mark.integration
def test_shared_bin_model_across_layouts_reuses_file_ids(
    ordinus: requests.Session,
    themis: requests.Session,
    token: str,
    library_item: tuple[str, dict[str, Any]],
) -> None:
    """Two layouts containing the same bin model share Themis library file IDs.

    Layout A and layout B both contain bin-1x1 (same STL content).  Sending
    each layout to Themis must not create duplicate library items — the file
    uploaded for layout A must be reused when layout B is sent.
    """
    lib_id, item = library_item

    layout_a = _create_layout(ordinus, token, "E2E: shared bin A", lib_id, item, quantity=2)
    layout_b = _create_layout(ordinus, token, "E2E: shared bin B", lib_id, item, quantity=3)

    _generate_and_wait(ordinus, token, layout_a, lib_id, item, quantity=2)
    _generate_and_wait(ordinus, token, layout_b, lib_id, item, quantity=3)

    pid_a = _send_to_themis(ordinus, token, layout_a)
    pid_b = _send_to_themis(ordinus, token, layout_b)

    file_ids_a = _themis_project_file_ids(pid_a)
    file_ids_b = _themis_project_file_ids(pid_b)

    assert file_ids_a, "Project A should have at least one item"
    assert file_ids_a == file_ids_b, (
        f"Both layouts use the same bin model but got different Themis file IDs.\n"
        f"  Layout A (project {pid_a}) file_ids: {file_ids_a}\n"
        f"  Layout B (project {pid_b}) file_ids: {file_ids_b}\n"
        "The dedup check must match by content hash within the target folder."
    )
