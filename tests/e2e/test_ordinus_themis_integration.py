"""
Integration test: Ordinus -> Themis "send-to-themis" integration.

Verifies:
  1. Sending a layout creates a Themis project with correct source fields
     (source_app, source_layout_id) and stores themisProjectId back in Ordinus
     bomGenerations.
  2. Resending the same layout reuses Themis library file IDs (per-layout dedup).
  3. Two different layouts that share the same bin model reuse the same Themis
     library file IDs -- no duplicate library items are created.
  4. A layout sent to Themis can be queued for slicing on an Elegoo Centauri Carbon
     printer and reaches "sliced" status.

Requirements:
  - Ordinus running at ORDINUS_URL (default http://localhost:3001)
  - Themis  running at THEMIS_URL  (default http://localhost:8001)
  - No authentication required (customer-profiles branch removes user auth)

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

BOM_TIMEOUT_S    = 120
SLICE_TIMEOUT_S  = 300
POLL_INTERVAL_S  = 5

MACHINE_PROFILE  = "Elegoo Centauri Carbon 0.4 nozzle"
PROCESS_PROFILE  = "0.16mm Optimal @Elegoo CC 0.4 nozzle"
FILAMENT_PROFILE = "Elegoo PLA @ECC"


# -- helpers ------------------------------------------------------------------

def _first_library_item(session: requests.Session) -> tuple[str, dict[str, Any]]:
    """Return (library_id, item) for the first available item."""
    resp = session.get(f"{ORDINUS_URL}/api/v1/libraries", timeout=10)
    resp.raise_for_status()
    lib_id: str = resp.json()["data"][0]["id"]

    resp = session.get(f"{ORDINUS_URL}/api/v1/libraries/{lib_id}/items", timeout=10)
    resp.raise_for_status()
    item: dict[str, Any] = resp.json()["data"][0]
    return lib_id, item


def _create_layout(session: requests.Session, name: str,
                   lib_id: str, item: dict[str, Any], quantity: int) -> int:
    resp = session.post(
        f"{ORDINUS_URL}/api/v1/layouts",
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


def _generate_and_wait(session: requests.Session, layout_id: int,
                       lib_id: str, item: dict[str, Any], quantity: int) -> None:
    resp = session.post(
        f"{ORDINUS_URL}/api/v1/bom/generate/{layout_id}",
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
        r = session.get(f"{ORDINUS_URL}/api/v1/bom/generation/{layout_id}", timeout=10)
        r.raise_for_status()
        status = r.json()["data"]["status"]
        if status == "ready":
            return
        if status == "error":
            pytest.fail(f"BOM generation failed for layout {layout_id}: "
                        f"{r.json()['data'].get('errorMessage')}")
    pytest.fail(f"BOM generation timed out for layout {layout_id}")


def _send_to_themis(session: requests.Session, layout_id: int) -> int:
    resp = session.post(
        f"{ORDINUS_URL}/api/v1/bom/send-to-themis/{layout_id}",
        timeout=30,
    )
    resp.raise_for_status()
    url: str = resp.json()["data"]["projectUrl"]
    return int(url.rstrip("/").split("/")[-1])


def _themis_project(project_id: int) -> dict[str, Any]:
    resp = requests.get(f"{THEMIS_URL}/api/v1/projects/{project_id}", timeout=10)
    resp.raise_for_status()
    return resp.json()


def _themis_project_file_ids(project_id: int) -> list[int]:
    return sorted(item["file_id"] for item in _themis_project(project_id).get("items", []))


def _find_centauri_placeholder_printer_id(session: requests.Session) -> int:
    resp = session.get(f"{THEMIS_URL}/api/v1/printers", timeout=10)
    resp.raise_for_status()
    for p in resp.json():
        if "Centauri Carbon" in p["name"] and "placeholder" in p["name"].lower():
            return p["id"]
    pytest.skip("Placeholder Elegoo Centauri Carbon printer not found in Themis -- is it seeded?")


def _drain_active_jobs_for_printer(session: requests.Session, printer_id: int) -> None:
    resp = session.get(f"{THEMIS_URL}/api/v1/jobs", timeout=10)
    resp.raise_for_status()
    active_statuses = {"queued", "blocked", "slicing", "sliced", "uploading", "printing"}
    for job in resp.json():
        if job["status"] not in active_statuses:
            continue
        if job.get("assigned_printer_id") == printer_id:
            session.post(f"{THEMIS_URL}/api/v1/jobs/{job['id']}/cancel", timeout=10)
            continue
        detail = session.get(f"{THEMIS_URL}/api/v1/jobs/{job['id']}/details", timeout=10)
        if detail.ok:
            cfgs = detail.json().get("printer_configs", [])
            if any(c.get("printer_id") == printer_id for c in cfgs):
                session.post(f"{THEMIS_URL}/api/v1/jobs/{job['id']}/cancel", timeout=10)


# -- fixtures -----------------------------------------------------------------

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
def library_item(ordinus: requests.Session) -> tuple[str, dict[str, Any]]:
    return _first_library_item(ordinus)


# -- tests --------------------------------------------------------------------

@pytest.mark.integration
def test_send_to_themis_source_fields_and_bidirectional_link(
    ordinus: requests.Session,
    themis: requests.Session,
    library_item: tuple[str, dict[str, Any]],
) -> None:
    """Themis project carries Ordinus source fields; themisProjectId stored back in Ordinus."""
    lib_id, item = library_item

    layout_id = _create_layout(ordinus, "E2E: source fields test", lib_id, item, quantity=2)
    _generate_and_wait(ordinus, layout_id, lib_id, item, quantity=2)
    themis_pid = _send_to_themis(ordinus, layout_id)

    # Bidirectional link: themisProjectId written back to bomGenerations
    gen = ordinus.get(f"{ORDINUS_URL}/api/v1/bom/generation/{layout_id}",
                      timeout=10).json()["data"]
    assert gen["themisProjectId"] == themis_pid, (
        f"Expected themisProjectId={themis_pid}, got {gen['themisProjectId']}"
    )

    # Source fields on Themis project
    proj = _themis_project(themis_pid)
    assert proj["source_app"] == "ordinus"
    assert proj["source_layout_id"] == layout_id


@pytest.mark.integration
def test_resend_same_layout_reuses_file_ids(
    ordinus: requests.Session,
    themis: requests.Session,
    library_item: tuple[str, dict[str, Any]],
) -> None:
    """Resending the same layout to Themis reuses the same library file IDs (per-layout dedup)."""
    lib_id, item = library_item

    layout_id = _create_layout(ordinus, "E2E: resend dedup test", lib_id, item, quantity=1)
    _generate_and_wait(ordinus, layout_id, lib_id, item, quantity=1)

    pid_first  = _send_to_themis(ordinus, layout_id)
    pid_second = _send_to_themis(ordinus, layout_id)

    assert pid_first != pid_second, "Each send should create a new Themis project"
    assert _themis_project_file_ids(pid_first) == _themis_project_file_ids(pid_second), (
        "Resending the same layout must reuse Themis library file IDs -- no duplicates"
    )


@pytest.mark.integration
def test_shared_bin_model_across_layouts_reuses_file_ids(
    ordinus: requests.Session,
    themis: requests.Session,
    library_item: tuple[str, dict[str, Any]],
) -> None:
    """Two layouts containing the same bin model share Themis library file IDs.

    Layout A and layout B both contain bin-1x1 (same STL content).  Sending
    each layout to Themis must not create duplicate library items -- the file
    uploaded for layout A must be reused when layout B is sent.
    """
    lib_id, item = library_item

    layout_a = _create_layout(ordinus, "E2E: shared bin A", lib_id, item, quantity=2)
    layout_b = _create_layout(ordinus, "E2E: shared bin B", lib_id, item, quantity=3)

    _generate_and_wait(ordinus, layout_a, lib_id, item, quantity=2)
    _generate_and_wait(ordinus, layout_b, lib_id, item, quantity=3)

    pid_a = _send_to_themis(ordinus, layout_a)
    pid_b = _send_to_themis(ordinus, layout_b)

    file_ids_a = _themis_project_file_ids(pid_a)
    file_ids_b = _themis_project_file_ids(pid_b)

    assert file_ids_a, "Project A should have at least one item"
    assert file_ids_a == file_ids_b, (
        f"Both layouts use the same bin model but got different Themis file IDs.\n"
        f"  Layout A (project {pid_a}) file_ids: {file_ids_a}\n"
        f"  Layout B (project {pid_b}) file_ids: {file_ids_b}\n"
        "The dedup check must match by content hash within the target folder."
    )


@pytest.mark.integration
def test_ordinus_layout_slices_on_elegoo_centauri(
    ordinus: requests.Session,
    themis: requests.Session,
    library_item: tuple[str, dict[str, Any]],
) -> None:
    """End-to-end: Ordinus layout -> Themis project -> slice on Elegoo Centauri Carbon.

    Verifies the full print-prep pipeline:
      1. BOM generation produces STLs for the layout
      2. send-to-themis uploads those STLs to the Themis library
      3. The Themis library file can be used as the source for a print job
      4. The Orca sidecar slices the gridfinity STL with Elegoo Centauri Carbon
         profiles and the job reaches "sliced" status
    """
    lib_id, item = library_item

    # Build a fresh layout and send it to Themis
    layout_id = _create_layout(ordinus, "E2E: Elegoo Centauri slice test",
                               lib_id, item, quantity=1)
    _generate_and_wait(ordinus, layout_id, lib_id, item, quantity=1)
    themis_pid = _send_to_themis(ordinus, layout_id)

    # Use the first STL file uploaded to Themis as our print job source
    file_ids = _themis_project_file_ids(themis_pid)
    assert file_ids, (
        f"Themis project {themis_pid} has no files after send-to-themis "
        f"for layout {layout_id}"
    )
    uploaded_file_id = file_ids[0]

    # Find the Elegoo Centauri Carbon placeholder printer in Themis
    printer_id = _find_centauri_placeholder_printer_id(themis)

    # Clear any leftover active jobs so the queue engine can slice our new job
    _drain_active_jobs_for_printer(themis, printer_id)

    # Confirm Orca serves the Centauri Carbon profiles for this printer
    resp = themis.get(f"{THEMIS_URL}/api/v1/printers/{printer_id}/profiles", timeout=15)
    resp.raise_for_status()
    profiles = resp.json()
    assert PROCESS_PROFILE in profiles["print_profiles"], (
        f"{PROCESS_PROFILE!r} not available for printer {printer_id}. "
        f"Available: {profiles['print_profiles']}"
    )
    assert FILAMENT_PROFILE in profiles["filament_profiles"], (
        f"{FILAMENT_PROFILE!r} not available for printer {printer_id}. "
        f"Available: {profiles['filament_profiles']}"
    )

    # Queue the job targeting the Elegoo Centauri Carbon placeholder
    resp = themis.post(
        f"{THEMIS_URL}/api/v1/jobs",
        json={
            "uploaded_file_id": uploaded_file_id,
            "plate_number": 1,
            "printer_configs": [
                {
                    "printer_id": printer_id,
                    "print_profile": PROCESS_PROFILE,
                    "filament_profile": FILAMENT_PROFILE,
                }
            ],
        },
        timeout=15,
    )
    resp.raise_for_status()
    job_id = resp.json()["id"]

    # Poll until sliced (Orca processes the gridfinity STL)
    deadline = time.monotonic() + SLICE_TIMEOUT_S
    status = None
    while time.monotonic() < deadline:
        r = themis.get(f"{THEMIS_URL}/api/v1/jobs/{job_id}", timeout=10)
        r.raise_for_status()
        status = r.json()["status"]
        if status in ("sliced", "uploading", "printing", "complete", "failed"):
            break
        time.sleep(POLL_INTERVAL_S)

    # Best-effort cleanup regardless of test outcome
    try:
        themis.post(f"{THEMIS_URL}/api/v1/jobs/{job_id}/cancel", timeout=10)
    except Exception:
        pass

    assert status == "sliced", (
        f"Expected job {job_id} to reach 'sliced', got {status!r}.\n"
        f"  Ordinus layout: {layout_id}, Themis project: {themis_pid}, "
        f"file_id: {uploaded_file_id}\n"
        "Check logs: docker compose logs themis | grep -E 'ERROR|slice'"
    )
