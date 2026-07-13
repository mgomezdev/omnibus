# Filament Consumption, Profile Drift & Laminus Health — Design Spec

**Date:** 2026-07-13  
**Revised:** 2026-07-13 (post Fable review)  
**Status:** Approved  
**Repos:** `themis`

---

## Overview

Three self-contained enhancements to the Themis/Laminus stack:

1. **Filament consumption tracking** — persist per-job actual grams on the `Job` row; surface in history view; auto-deduct from the correct Spoolman spool on completion.
2. **Profile drift detection** — on-demand report of stale OrcaSlicer profile references across printers, jobs, and Spoolman filaments.
3. **Laminus health chip** — a sidebar status indicator showing whether the sidecar is up, building, or offline, with catalog counts in a detail panel.

Feature 1 requires one migration (v008). Features 2 and 3 require no schema changes.

---

## Feature 1 — Filament Consumption Tracking

### Background

`GcodeFile.filament_grams` is extracted at slice time and stored, but the `GcodeFile` row is **deleted at job completion** inside `handle_print_complete` (and the reconcile path). This means:

- A post-completion hook cannot query `GcodeFile` — it is gone.
- A history join on `gcode_files` returns null for every completed job.

The fix is to persist grams on the `Job` row before the delete.

### Migration v008 — add `filament_grams` to `jobs`

```sql
ALTER TABLE jobs ADD COLUMN filament_grams REAL;
```

Standard idempotent migration file: `backend/app/migrations/v008_job_filament_grams.py`.  
Existing completed jobs get `NULL` (correct — grams were not captured for them).

### Backend — `queue_engine.py › handle_print_complete`

Inside the existing session block, **before** `session.delete(gcode)`:

```python
if gcode and gcode.filament_grams is not None:
    job.filament_grams = gcode.filament_grams
```

After `session.commit()`, if `job.filament_grams` is not null, resolve the Spoolman spool and fire the deduction (see below).

**Idempotency:** `handle_print_complete` already guards on `job.status == "printing"` which is set to `"complete"` before commit. The reconcile path calls the same function and hits the same guard — double-deduction is structurally prevented.

### Backend — Spoolman spool resolution

The Spoolman spool ID lives in `Printer.loaded_filaments[slot]["spoolman_spool_id"]`, **not** in `JobPrinterConfig.filament_id` (which is the Spoolman filament-type ID).

Resolution in `handle_print_complete`, after commit (printer_id is already known):

1. Load `Printer` for `printer_id` → `loaded = printer.loaded_filaments`
2. Load `JobPrinterConfig` where `job_id = job.id AND printer_id = printer_id`
3. Call `_slot_for_config(config, loaded)` — already imported in queue_engine — to get the matched slot dict
4. Read `slot.get("spoolman_spool_id")` → skip deduction if None

**Multi-tool policy:** A single `GcodeFile.filament_grams` value represents the total for all tools on that printer. Deduct the full total against slot 0 (the primary slot) only. Multi-tool deduction per-slot is out of scope — OrcaSlicer's gcode header does not break grams out per tool.

### Backend — `spoolman_service.py`

Add:

```python
async def record_spool_use(
    url: str, api_key: str | None, spool_id: int, grams: float
) -> None:
    """PUT /api/v1/spool/{spool_id}/use — records filament consumption."""
    headers = _headers(api_key)
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.put(
            f"{url.rstrip('/')}/api/v1/spool/{spool_id}/use",
            json={"use_weight": grams},
            headers=headers,
        )
        resp.raise_for_status()
```

### Backend — fire-and-forget deduction in `handle_print_complete`

After commit, in a `try/except`:

```python
asyncio.create_task(_deduct_spool(spoolman_cfg, spool_id, job.filament_grams))
```

`_deduct_spool` is a module-level async helper that calls `record_spool_use` and logs a warning on failure. It never raises — print completion must not be affected by Spoolman availability.

### Backend — `jobs.py › list_history`

`Job.filament_grams` is now a direct column — no join required. Add it to `_to_dict(job)` and to the history response. The existing `HistoryJob` interface on the frontend needs `filament_grams: number | null`.

### Frontend — `HistoryScreen.tsx`

- Add `filament_grams: number | null` to the `HistoryJob` interface.
- Add a `"Filament"` column to the history table.
- Render `"{n.toFixed(1)} g"` or `"—"` when null.

---

## Feature 2 — Profile Drift Detection

### Goal

One endpoint that computes, on demand, a structured report of stale OrcaSlicer profile references across three object types. No server-side cache — the check is fast (small-table SELECTs + set lookups) and runs only when a user visits the Settings page.

### Shared catalog helper — `services/catalog_utils.py`

`settings.py` (lines 272–276) already builds `machine_names` and `filament_names` sets from the catalog. Extract this logic into a shared function rather than duplicating it in `drift.py`:

```python
def catalog_name_sets(catalog: dict) -> tuple[set[str], set[str], set[str], set[str]]:
    """Returns (machine_names, process_names, filament_names, filament_uuids)."""
```

Update `settings.py` to use this helper. `drift.py` uses it too.

### New endpoint — `GET /api/v1/drift`

**New file:** `backend/app/api/routes/drift.py`. Registered in `main.py`.

**Algorithm:**

1. Call `get_cached_catalog()` from `routes/laminus.py`. If the catalog is cold and Laminus is unreachable, this raises HTTPException — let it propagate. The Settings UI shows the standard 502/503 error.
2. Call `catalog_name_sets(catalog)` → four sets.
3. Load `SpoolmanConfig` (id=1).

**Printers check:**  
For each `Printer` row:
- Skip `current_orca_printer_profile` if null (printer not yet configured).
- Flag if machine profile name not in `machine_names`.
- For each entry in `loaded_filaments`, flag `filament_profile` if not null and not in `filament_names`.

**Jobs check:**  
For each `Job` where `status IN ('queued', 'blocked')`:
- Load its `JobPrinterConfig` rows.
- Flag `print_profile` if not in `process_names`; flag `filament_profile` if not null and not in `filament_names`.

**Spoolman filaments check** (skip if `SpoolmanConfig` not enabled):  
Call `fetch_filaments()`. For each filament, decode `extra.orca_profiles` (double-JSON-encoded). Flag any UUID key not in `filament_uuids`. On HTTP error, set `spoolman_error` and omit this section.

**Dropped from v1:** The `ProjectItem.filament_profile_uuid` check. That field is a legacy artifact (`models.py:186-187`) that no current UI path populates. Flagging it produces alarms with no actionable remediation.

**Response shape:**

```json
{
  "checked_at": "2026-07-13T10:00:00Z",
  "catalog_age_seconds": 42,
  "all_clear": true,
  "printers": [],
  "jobs": [
    {"id": 5, "file_name": "bracket.3mf", "stale": ["PLA @0.20mm Standard"]}
  ],
  "spoolman_filaments": [],
  "spoolman_error": null
}
```

`all_clear` is `true` only when all three lists are empty and `spoolman_error` is null (or Spoolman is disabled).

Include `file_name` in job entries (from `UploadedFile.original_filename`) so the report is immediately actionable.

### Frontend — `api/drift.ts`

New file:
```typescript
export interface DriftReport { /* matches response shape */ }
export async function fetchDriftReport(): Promise<DriftReport>
```

### Frontend — `SettingsScreen.tsx`

Add a **"Profile Drift"** section below the Spoolman section:

- Fetch on mount via `fetchDriftReport()`.
- Loading state: spinner.
- Error state (502/503): "Could not check — Laminus sidecar unreachable."
- `all_clear: true`: green "All profiles current" row.
- Otherwise: one collapsible accordion per non-empty category (Printers / Jobs / Spoolman Filaments), count in header, affected names in body.
- "Re-check" button: clears local state and re-fetches.

---

## Feature 3 — Laminus Health Chip

*(Renumbered from 5 — features 3 and 4 were not implemented.)*

### Backend — enhance `GET /api/v1/laminus/catalog/status`

The existing endpoint already hits Laminus `GET /api/health` (5 s timeout, in a thread). Two additions:

**1. Add `catalog_counts` from the in-memory dict (zero sidecar cost):**

```python
catalog_counts = {
    "machine": len(_catalog_dict.get("machine", [])),
    "process": len(_catalog_dict.get("process", [])),
    "filament": len(_catalog_dict.get("filament", [])),
} if _catalog_dict else None
```

**2. Add derived `status` string:**

| Condition | `status` |
|---|---|
| `LAMINUS_SIDECAR_URL` not set | `"unconfigured"` |
| Health request raised / non-200 and non-503 | `"offline"` |
| Health returned 503, or 200 with `catalog_building: true` | `"building"` |
| Health returned 200 with `catalog_loaded: true` | `"online"` |
| Health returned 200 with `catalog_loaded: false, catalog_building: false` | `"offline"` |

503 during catalog rebuild is treated as `"building"` not `"offline"` — Laminus returns 503 while `warm_catalog_cache` polls (`laminus.py:77`).

**3. Add a 30 s server-side memo** on the health check result (not the full catalog — just the 5-field health dict). Prevents every open browser tab from holding a thread for 5 s when Laminus is down.

Updated response:
```json
{
  "cached": true,
  "cached_bytes": 18432,
  "fetched_at": 1720123456.7,
  "laminus_configured": true,
  "laminus": {"catalog_loaded": true, "catalog_building": false, "profile_count": 142},
  "catalog_counts": {"machine": 12, "process": 48, "filament": 82},
  "status": "online"
}
```

### Frontend — `LaminusStatusChip.tsx`

**New file:** `frontend/src/components/LaminusStatusChip.tsx`

- Fetches `GET /api/v1/laminus/catalog/status` on mount.
- Polls every 60 s via `setInterval`; clears on unmount (`useEffect` cleanup).
- Hidden entirely when `laminus_configured: false`.
- Renders: colored dot + `"Laminus"` label + status text (e.g. `"online"`, `"building"`, `"offline"`).
- Click toggles an inline detail block: machine / process / filament counts and last-fetched as a relative time ("3 min ago").
- Dot colors: `online` → `var(--ok)` green; `building` → `#f59e0b` amber; `offline` → `var(--err)` red.

### Integration — `Sidebar.tsx`

Add `<LaminusStatusChip />` to the Account `nav-section`, below the Settings `NavLink`. No props — chip manages its own data.

---

## File Changelist

| Repo | File | Change |
|---|---|---|
| `themis` | `backend/app/migrations/v008_job_filament_grams.py` | **New** — `ALTER TABLE jobs ADD COLUMN filament_grams REAL` |
| `themis` | `backend/app/models.py` | Add `filament_grams: Mapped[Optional[float]]` to `Job` |
| `themis` | `backend/app/api/routes/jobs.py` | `_to_dict`: include `job.filament_grams` |
| `themis` | `backend/app/services/queue_engine.py` | Capture grams before gcode delete; fire deduction task |
| `themis` | `backend/app/services/spoolman_service.py` | Add `record_spool_use(url, api_key, spool_id, grams)` |
| `themis` | `backend/app/services/catalog_utils.py` | **New** — `catalog_name_sets(catalog)` shared helper |
| `themis` | `backend/app/api/routes/settings.py` | Use `catalog_name_sets` instead of inline set-builds |
| `themis` | `backend/app/api/routes/drift.py` | **New** — `GET /api/v1/drift` |
| `themis` | `backend/app/main.py` | Register `drift` router |
| `themis` | `backend/app/api/routes/laminus.py` | Extend `catalog/status`: `catalog_counts`, `status`, 30 s health memo |
| `themis` | `frontend/src/screens/HistoryScreen.tsx` | Add `filament_grams` to type + table column |
| `themis` | `frontend/src/api/drift.ts` | **New** — `fetchDriftReport()` |
| `themis` | `frontend/src/screens/SettingsScreen.tsx` | Add "Profile Drift" section |
| `themis` | `frontend/src/components/LaminusStatusChip.tsx` | **New** — polling status chip |
| `themis` | `frontend/src/components/Sidebar.tsx` | Add `<LaminusStatusChip />` to Account section |

---

## Tests

| File | Scenario |
|---|---|
| `backend/tests/api/test_jobs_api.py` | `GET /jobs/history` includes `filament_grams` from `job.filament_grams`; null when not set |
| `backend/tests/services/test_queue_engine.py` | `handle_print_complete` sets `job.filament_grams` from gcode before delete; calls `record_spool_use` once for the assigned printer's matched slot; skips deduction when SpoolmanConfig disabled; skips when grams null; HTTP error in `record_spool_use` does not affect job status |
| `backend/tests/services/test_spoolman_service.py` | `record_spool_use` calls correct Spoolman URL with `{"use_weight": grams}` |
| `backend/tests/api/test_drift_api.py` | **New** — stale machine profile flagged; stale job process profile flagged; null printer profile skipped; all-clear when catalog matches; Spoolman check skipped when not enabled; Spoolman fetch failure sets `spoolman_error`, returns partial result; cold catalog returns 502/503 |
| `backend/tests/api/test_laminus_api.py` | `catalog/status` includes `catalog_counts` and correct `status` for each health state (online, building via 503, building via catalog_building flag, offline) |
| `backend/tests/services/test_catalog_utils.py` | **New** — `catalog_name_sets` returns correct sets for normal catalog; empty catalog; missing keys |
