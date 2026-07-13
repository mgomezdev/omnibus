# Filament Consumption, Profile Drift & Laminus Health — Design Spec

**Date:** 2026-07-13
**Status:** Approved
**Repos:** `themis`

---

## Overview

Three self-contained enhancements to the Themis/Laminus stack:

1. **Filament consumption tracking** — surface per-job weight in the history view and auto-deduct from Spoolman spools when a job completes.
2. **Profile drift detection** — on-demand report of stale OrcaSlicer profile references across printers, jobs, project items, and Spoolman filaments.
3. **Laminus health chip** — a sidebar status indicator showing whether the sidecar is up, building, or offline, with catalog counts in a detail popover.

None of these features require DB schema changes or migrations. All new backend state is either computed on-the-fly or held in module-level memory caches.

---

## Feature 1 — Filament Consumption Tracking

### Background

`GcodeFile.filament_grams` is already extracted from each sliced gcode file and stored in SQLite. The job-detail endpoint (`GET /api/v1/jobs/{id}/details`) and project-detail endpoint (`GET /api/v1/projects/{id}`) already return this field and aggregate it respectively. `JobDetailScreen` and `ProjectDetailScreen` already render it.

Two gaps remain:

- The **history list** (`GET /api/v1/jobs/history`) does not join `gcode_files`, so `HistoryJob` carries no filament data.
- **Spoolman is never notified** when plastic is consumed; its `remaining_weight` / `used_weight` drifts out of sync after every print.

### Backend — `jobs.py › list_history`

Add a join against `gcode_files` keyed on `job_id`. Include `filament_grams` (float or null) in each row of the history response. Use the first `GcodeFile` row per job (same `.limit(1)` pattern as `get_job_details`).

### Backend — `spoolman_service.py`

Add:

```python
async def record_spool_use(
    url: str, api_key: str | None, spool_id: int, grams: float
) -> None:
    """PUT /api/v1/spool/{spool_id}/use — records filament consumption in Spoolman."""
```

Calls Spoolman's standard use endpoint with `{"use_weight": grams}`. Raises `httpx.HTTPStatusError` on failure (callers log and swallow).

### Backend — `queue_engine.py`

After the job moves to `complete` status (existing completion callback), add a fire-and-forget coroutine:

1. Load `SpoolmanConfig` (id=1); skip if not enabled.
2. Query `GcodeFile` for the job; skip if `filament_grams` is null.
3. Query `JobPrinterConfig` rows for the job; for each config where `filament_id` is not null, call `record_spool_use(config.filament_id, gcode.filament_grams)`.
4. Log warning on any HTTP error; never raise (must not affect job state machine).

`filament_id` on `JobPrinterConfig` is the Spoolman **spool** ID (the physical roll), not the filament-type ID.

### Frontend — `HistoryScreen.tsx`

- Add `filament_grams: number | null` to the `HistoryJob` interface.
- Add a `"Filament"` column header to the history table.
- Render each row's `filament_grams` as `"{n} g"` (one decimal place), or `"—"` if null.

---

## Feature 2 — Profile Drift Detection + Spoolman Sync Check

### Goal

One endpoint that computes, on demand, a structured report of every stale OrcaSlicer profile reference in the system — across four object types. "Stale" means a profile name or UUID that no longer exists in the current Laminus catalog.

### New endpoint — `GET /api/v1/drift`

**New file:** `backend/app/api/routes/drift.py`  
Registered in `main.py` alongside other routers.

**Algorithm:**

1. Load the cached catalog via `get_cached_catalog()` from `routes/laminus.py`. This never hits the sidecar if the cache is warm.
2. Build lookup structures:
   - `machine_names: set[str]` — all machine profile names
   - `process_names: set[str]` — all process profile names
   - `filament_names: set[str]` — all filament profile names
   - `filament_uuids: set[str]` — all filament profile UUIDs
3. **Printers check:** Load all `Printer` rows. For each printer, flag `current_orca_printer_profile` if not in `machine_names`. For each entry in `loaded_filaments` (JSON list), flag `filament_profile` (if present) if not in `filament_names`.
4. **Jobs check:** Load all `Job` rows with `status IN ('queued', 'blocked')`. For each, load its `JobPrinterConfig` rows. Flag `print_profile` if not in `process_names`; flag `filament_profile` if not null and not in `filament_names`.
5. **Project items check:** Load all `ProjectItem` rows where `filament_profile_uuid != ""`. Flag rows whose UUID is not in `filament_uuids`.
6. **Spoolman filaments check** (skip if `SpoolmanConfig` not enabled): Fetch all filaments via `spoolman_service.fetch_filaments()`. For each filament, decode `extra.orca_profiles` (double-JSON-encoded per existing convention). Flag any UUID key not found in `filament_uuids`.

**Response shape:**

```json
{
  "checked_at": "2026-07-13T10:00:00Z",
  "catalog_age_seconds": 42,
  "all_clear": false,
  "printers": [
    {"id": 1, "name": "Centauri", "stale": ["Elegoo Centauri Carbon 0.4 nozzle (old)"]}
  ],
  "jobs": [
    {"id": 5, "stale": ["PLA @0.20mm Standard"]}
  ],
  "project_items": [
    {"id": 12, "project_id": 3, "project_name": "Gridfinity Layout A", "stale_uuid": "abc-123-..."}
  ],
  "spoolman_filaments": [
    {"id": 7, "name": "Polymaker PLA+ Black", "stale_uuids": ["def-456-..."]}
  ]
}
```

`all_clear` is `true` only when all four lists are empty.

**Caching:** Module-level `_drift_cache: dict | None` and `_drift_cached_at: float | None` in `drift.py`. TTL = 300 s. The cache is invalidated (set to `None`) when `POST /api/v1/laminus/catalog/refresh` or `POST /api/v1/laminus/catalog/rescan` runs — add a `invalidate_drift_cache()` helper that `laminus.py` calls at the end of both those handlers.

**Spoolman error handling:** If `fetch_filaments` raises, return the drift report without the `spoolman_filaments` section and include `"spoolman_error": "..."` at the top level.

### Frontend — `SettingsScreen.tsx`

Add a **"Profile Drift"** section to the Settings page (below the existing Spoolman section):

- On mount, `GET /api/v1/drift` and render the result.
- If `all_clear: true`, show a green "All profiles current" line.
- If not all clear, render one collapsible accordion per non-empty category (Printers / Jobs / Project Items / Spoolman Filaments), each showing the count in the header and the affected names/IDs in the body.
- A "Re-check" button re-fetches (bypasses client-side cache by just refetching; the server cache handles deduplication).

### New frontend API function — `api/drift.ts`

```typescript
export interface DriftReport { /* matches response shape above */ }
export async function fetchDriftReport(): Promise<DriftReport>
```

---

## Feature 5 — Laminus Health Chip

### Backend — enhance `GET /api/v1/laminus/catalog/status`

The existing endpoint already proxies `GET /api/health` from Laminus (extracting `catalog_loaded`, `catalog_building`, `profile_count`). Extend it to also:

- Compute per-type counts from the in-memory `_catalog_dict` (no sidecar round-trip):
  ```python
  catalog_counts = {
      "machine": len(_catalog_dict.get("machine", [])),
      "process": len(_catalog_dict.get("process", [])),
      "filament": len(_catalog_dict.get("filament", [])),
  } if _catalog_dict else None
  ```
- Add a derived `status: str` field:
  - `"unconfigured"` — `LAMINUS_SIDECAR_URL` not set
  - `"offline"` — health check request failed or non-200
  - `"building"` — `catalog_building: true`
  - `"online"` — `catalog_loaded: true`

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

### Frontend — `LaminusStatusChip` component

**New file:** `frontend/src/components/LaminusStatusChip.tsx`

Props: none (fetches internally).

Behaviour:
- On mount, fetch `GET /api/v1/laminus/catalog/status`.
- Poll every 60 s via `setInterval`.
- Render a small row: `●  Laminus  [status label]` where the dot color maps to status:
  - `online` → green (`var(--ok)`)
  - `building` → amber (`var(--warn)` or `#f59e0b`)
  - `offline` → red (`var(--err)`)
  - `unconfigured` → grey (`var(--text-3)`)
- On click, toggle an inline detail block showing machine/process/filament counts and `fetched_at` formatted as a relative time ("3 min ago").
- If `laminus_configured: false`, suppress the chip entirely (don't show a broken indicator when the feature isn't set up).

### Integration — `Sidebar.tsx`

Add `<LaminusStatusChip />` to the Account `nav-section`, below the Settings `NavLink`. No props required; the chip manages its own data lifecycle.

---

## File Changelist

| Repo | File | Change |
|---|---|---|
| `themis` | `backend/app/api/routes/jobs.py` | `list_history`: join `GcodeFile`, add `filament_grams` to each history row |
| `themis` | `backend/app/services/spoolman_service.py` | Add `record_spool_use(url, api_key, spool_id, grams)` |
| `themis` | `backend/app/services/queue_engine.py` | Post-completion hook: fire-and-forget `record_spool_use` per `JobPrinterConfig` |
| `themis` | `backend/app/api/routes/drift.py` | **New** — `GET /api/v1/drift` with module-level cache |
| `themis` | `backend/app/api/routes/laminus.py` | Call `invalidate_drift_cache()` at end of `/catalog/refresh` and `/catalog/rescan` |
| `themis` | `backend/app/main.py` | Register `drift` router |
| `themis` | `backend/app/api/routes/laminus.py` | Extend `catalog/status` response with `catalog_counts` and `status` |
| `themis` | `frontend/src/screens/HistoryScreen.tsx` | Add `filament_grams` to type + table column |
| `themis` | `frontend/src/api/drift.ts` | **New** — `fetchDriftReport()` |
| `themis` | `frontend/src/screens/SettingsScreen.tsx` | Add "Profile Drift" section |
| `themis` | `frontend/src/components/LaminusStatusChip.tsx` | **New** — polling status chip |
| `themis` | `frontend/src/components/Sidebar.tsx` | Add `<LaminusStatusChip />` to Account section |

## Tests

| File | What to test |
|---|---|
| `backend/tests/api/test_jobs_api.py` | `GET /jobs/history` includes `filament_grams` when a `GcodeFile` row exists |
| `backend/tests/services/test_spoolman_service.py` | `record_spool_use` calls correct Spoolman URL with correct payload |
| `backend/tests/api/test_drift_api.py` | **New** — stale printer profile flagged; all-clear when catalog matches; Spoolman check skipped when not enabled; 5-min cache honoured |
| `backend/tests/api/test_laminus_api.py` | `catalog/status` returns `catalog_counts` and correct `status` string |
