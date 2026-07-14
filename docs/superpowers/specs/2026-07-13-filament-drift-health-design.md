# Filament Consumption, Profile Drift & Laminus Health — Design Spec

**Date:** 2026-07-13  
**Revised:** 2026-07-14 (Feature 2 redesigned: drift check integrated into catalog sync; Spoolman online sanity check added; deduplication of remap prompts)  
**Status:** Approved  
**Repos:** `themis`

---

## Overview

Three self-contained enhancements to the Themis/Laminus stack:

1. **Filament consumption tracking** — persist per-job actual grams on the `Job` row; surface in history view; auto-deduct from the correct Spoolman spool on completion.
2. **Profile drift remap on catalog sync** — catalog refresh/rescan pre-scans the incoming catalog for removed profiles; if live data references them, the sync pauses and the user resolves each stale reference via a remap modal before the new catalog is committed.
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

## Feature 2 — Profile Drift Remap on Catalog Sync

### Goal

Catalog refresh/rescan must not silently orphan live profile references. Before Themis commits an incoming catalog to its cache, it diffs the incoming catalog against the current one. If profiles that live data references would disappear, the sync pauses and returns a structured pending-remaps payload; the user resolves each stale reference in a modal, and a confirm call applies the DB updates and completes the swap.

### Flow overview

**Happy path (no drift):** User clicks Refresh/Rescan → Themis fetches the new catalog from Laminus into a local variable → `compute_drift(old, new, ...)` finds no removed profiles referenced by live data → cache is committed immediately → response `{"status": "ok", ...}`. Identical behavior to today, plus the status field.

**Drift path:** Same fetch → `compute_drift` finds stale references → the new catalog is parked in a module-level pending-sync slot (the cache still serves the **old** catalog) → response `{"status": "pending_remaps", ...}` → UI opens the RemapModal → user picks replacements → `POST /catalog/confirm-remap` → Themis applies DB/Spoolman updates, commits the parked catalog to the cache, clears the pending slot → `{"status": "ok"}`.

If the current cache is cold (first sync ever), there is no old catalog to diff against — commit directly, no drift check.

### Backend — `services/catalog_utils.py`

Keep the shared name-set helper (extracted from `settings.py` lines 272–276; update `settings.py` to use it):

```python
def catalog_name_sets(catalog: dict) -> tuple[set[str], set[str], set[str], set[str]]:
    """Returns (machine_names, process_names, filament_names, filament_uuids)."""
```

Add the drift computation:

```python
async def compute_drift(
    old_catalog: dict, new_catalog: dict,
    session: AsyncSession, spoolman_cfg: SpoolmanConfig | None,
) -> dict | None:
    """Returns a pending-remaps payload, or None if no live data references removed profiles."""
```

**Algorithm:**

1. `removed_* = old_sets - new_sets` for machine, process, filament names, and filament UUIDs. If all four removal sets are empty, return `None` without touching the DB — the common case costs four set subtractions.
2. **Collect raw hits:**
   - **Printers:** for each `Printer` row, record a hit if `current_orca_printer_profile` is in `removed_machine`; for each slot in `loaded_filaments`, record a hit if its `filament_profile` is non-null and in `removed_filament_names`.
   - **Jobs:** for each `Job` with `status IN ('queued', 'blocked')`, for each of its `JobPrinterConfig` rows, record a hit if `print_profile` is in `removed_process` or `filament_profile` is non-null and in `removed_filament_names`. Track the config's `file_name` (from `UploadedFile.original_filename`).
   - **Spoolman** (only if `spoolman_cfg` is enabled): `fetch_filaments()`, decode each filament's `extra.orca_profiles` (double-JSON-encoded dict UUID → name), record a hit per UUID key in `removed_filament_uuids`. On HTTP error, set `spoolman_error` in the payload and skip this section — a Spoolman outage must not block a catalog sync.
3. **Deduplicate into grouped entries** (one user prompt per unique stale value, not per affected object):
   - **Spoolman:** group by `stale_uuid`. Each entry carries `affected_filament_ids: [int, ...]` and `affected_filament_names: [str, ...]`.
   - **Jobs:** group by `(field, stale_value)`. Each entry carries `affected_config_ids: [int, ...]` and `affected_file_names: [str, ...]`.
   - **Printers:** group by `(field, stale_value)`. Each entry carries `affected_printer_ids: [int, ...]`, `affected_printer_names: [str, ...]`, and `affected_slots: [int | null, ...]` (null for `current_orca_printer_profile`, slot index for `loaded_filaments.filament_profile`). Keep `required: true`.
4. If no grouped entries exist (removed profiles exist but nothing references them), return `None`.

As in the previous revision, `ProjectItem.filament_profile_uuid` is not checked — legacy field, no UI path populates it.

**Pending-remaps payload structure** (also the drift-path HTTP response body):

```json
{
  "status": "pending_remaps",
  "sync_id": "b3f1c9e0-…",
  "pending": {
    "printers": [
      {
        "field": "current_orca_printer_profile",
        "stale_value": "Bambu X1C 0.4 nozzle",
        "options_kind": "machine",
        "required": true,
        "affected_printer_ids": [1, 3],
        "affected_printer_names": ["X1C-left", "X1C-right"],
        "affected_slots": [null, null]
      },
      {
        "field": "loaded_filaments.filament_profile",
        "stale_value": "Generic PLA Old",
        "options_kind": "filament",
        "required": true,
        "affected_printer_ids": [1],
        "affected_printer_names": ["X1C-left"],
        "affected_slots": [0]
      }
    ],
    "jobs": [
      {
        "field": "print_profile",
        "stale_value": "0.20mm Standard Old",
        "options_kind": "process",
        "required": false,
        "affected_config_ids": [12, 17],
        "affected_file_names": ["bracket.3mf", "clip.3mf"]
      }
    ],
    "spoolman_filaments": [
      {
        "stale_uuid": "a1b2…",
        "stale_name": "PolyLite PLA @X1C",
        "options_kind": "filament_uuid",
        "required": false,
        "affected_filament_ids": [9, 14, 22],
        "affected_filament_names": ["PolyLite PLA Red", "PolyLite PLA Blue", "PolyLite PLA White"]
      }
    ]
  },
  "options": {
    "machine": ["Bambu X1C 0.4 nozzle (new)", "…"],
    "process": ["…"],
    "filament": ["…"],
    "filament_uuids": [{"uuid": "c3d4…", "name": "PolyLite PLA @X1C v2"}]
  },
  "spoolman_error": null
}
```

`options` carries the **incoming** catalog's valid values — the client cannot get them from `GET /catalog`, which still serves the old catalog while the sync is pending.

### Backend — modified refresh and rescan endpoints (`routes/laminus.py`)

Refactor `_fetch_and_cache()` into two steps so the fetch can happen without the commit:

```python
async def _fetch_catalog() -> tuple[bytes, dict]:   # pull from Laminus, parse, no side effects
def _commit_catalog(raw: bytes, parsed: dict) -> None:  # write module-level cache + fetched_at
```

Both **refresh** and **rescan** end with the same gate (rescan after its existing rebuild-trigger + health poll; refresh immediately):

```python
raw, new_catalog = await _fetch_catalog()
old_catalog = _catalog_dict
drift = await compute_drift(old_catalog, new_catalog, session, spoolman_cfg) if old_catalog else None
if drift is None:
    _commit_catalog(raw, new_catalog)
    return {"status": "ok", **existing_response_fields}
_pending_sync = {"sync_id": uuid4(), "raw": raw, "catalog": new_catalog,
                 "pending": drift["pending"], "created_at": time.time()}
return drift   # 200, status: "pending_remaps"
```

Note: refresh no longer clears the cache before fetching — the old catalog must survive as the diff baseline and keep serving reads until the swap commits.

`_pending_sync` is a single module-level slot next to the existing catalog globals. A new refresh/rescan overwrites it (the newer fetch wins; a confirm against the overwritten sync fails cleanly on `sync_id`). In-memory is acceptable for the same reason the catalog cache is: Themis is single-process, and losing the slot on restart just means the user re-triggers the sync — no data loss, no corruption.

Drift detection returns HTTP 200 (the pre-scan succeeded); the `status` field discriminates. Errors from Laminus keep their existing 502/503 behavior.

### Backend — `POST /api/v1/laminus/catalog/confirm-remap`

Request body: resolutions are keyed by stale value/UUID (one resolution per grouped entry). The apply step fans out each resolution to all affected object IDs from the pending payload.

```json
{
  "sync_id": "b3f1c9e0-…",
  "resolutions": {
    "printers": [
      {"field": "current_orca_printer_profile", "stale_value": "Bambu X1C 0.4 nozzle", "new_value": "Bambu X1C 0.4 nozzle (new)"},
      {"field": "loaded_filaments.filament_profile", "stale_value": "Generic PLA Old", "new_value": "Generic PLA @0.4 nozzle"}
    ],
    "jobs": [
      {"field": "print_profile", "stale_value": "0.20mm Standard Old", "new_value": null}
    ],
    "spoolman_filaments": [
      {"stale_uuid": "a1b2…", "new_uuid": "c3d4…"}
    ]
  }
}
```

**Validation (all before any write):**

- `_pending_sync` is None or `sync_id` mismatch → **409** ("sync superseded or expired — re-run the catalog sync").
- Every `pending.printers` grouped entry must have a matching resolution (matched on `field` + `stale_value`) with a non-null `new_value` present in the pending catalog's corresponding name set → **422** listing the unresolved entries.
- Job/Spoolman resolutions: `new_value`/`new_uuid` may be null (clear/drop) or must be valid in the pending catalog → **422** if invalid. Missing resolution entries are treated as null.

**Apply (single DB transaction):**

- Printers: for each printer resolution, look up the grouped entry's `affected_printer_ids` and `affected_slots` from `_pending_sync`. For each (printer_id, slot) pair: if slot is null, set `current_orca_printer_profile` to `new_value`; if slot is an index, rewrite `loaded_filaments[slot].filament_profile` — reassign the whole list so SQLAlchemy detects the JSON mutation.
- Jobs: for each job resolution, iterate its grouped entry's `affected_config_ids` and set `JobPrinterConfig.print_profile` / `filament_profile` to the new value or `NULL`.
- Spoolman (after commit, best-effort per filament): for each Spoolman resolution, iterate its grouped entry's `affected_filament_ids`. For each: decode `extra.orca_profiles`, delete the `stale_uuid` key, insert `new_uuid → name` (name looked up from the pending catalog) if `new_uuid` is non-null, re-encode, and PATCH via `spoolman_service.update_filament_extra(url, api_key, filament_id, extra)`. A per-filament HTTP failure is logged and reported in the response's `spoolman_failures` list but does not abort the swap.

**Complete:** `_commit_catalog(pending.raw, pending.catalog)`, clear `_pending_sync`, return `{"status": "ok", "applied": {"printers": n, "jobs": n, "spoolman_filaments": n}, "spoolman_failures": []}`.

### Frontend — `api/laminus.ts`

```typescript
export interface SyncOk { status: "ok"; /* existing refresh/rescan fields */ }
export interface PendingEntry { /* per-type fields as in payload above */ required: boolean; }
export interface PendingRemaps {
  status: "pending_remaps";
  sync_id: string;
  pending: { printers: PrinterEntry[]; jobs: JobEntry[]; spoolman_filaments: SpoolmanEntry[] };
  options: { machine: string[]; process: string[]; filament: string[];
             filament_uuids: { uuid: string; name: string }[] };
  spoolman_error: string | null;
}
export type SyncResponse = SyncOk | PendingRemaps;

export async function refreshCatalog(): Promise<SyncResponse>
export async function rescanCatalog(): Promise<SyncResponse>
export async function confirmRemap(syncId: string, resolutions: Resolutions): Promise<ConfirmResult>
```

### Frontend — `RemapModal.tsx`

**New file:** `frontend/src/components/RemapModal.tsx`. Props: `payload: PendingRemaps`, `onDone(result)`, `onCancel()`.

- Three groups, rendered only when non-empty: **Printers**, **Queued Jobs**, **Spoolman Filaments**. Each row represents one deduplicated grouped entry (one unique stale value), not one affected object.
- Each row shows: the stale value struck through, a count badge ("affects 2 printers", "affects 3 jobs", "affects 15 filaments"), and a dropdown. When the count is 1, show the single object name instead of the badge (e.g. "X1C-left", "bracket.3mf", "PolyLite PLA Red").
- Printer dropdowns: options from `options.machine` or `options.filament` per `options_kind`; no empty choice; unselected state blocks confirm.
- Job and Spoolman dropdowns: same option lists (Spoolman uses `options.filament_uuids`, displaying names, submitting UUIDs) plus a leading **"— clear —"** option, preselected. "Clear" submits `null`.
- If `spoolman_error` is set, show a warning banner: Spoolman references could not be checked this sync.
- **Confirm** button disabled until every `required: true` row has a selection. On click: `confirmRemap(sync_id, resolutions)`; on 409, show "Sync superseded — run the catalog sync again" and close.
- **Cancel** closes the modal without calling the API; the old catalog simply remains active (the pending slot server-side is inert and will be overwritten by the next sync).

### Frontend — `SettingsScreen.tsx`

The existing "Refresh Catalog" and "Rescan" buttons switch to the new `SyncResponse` type:

- `status: "ok"` → success toast, exactly as today.
- `status: "pending_remaps"` → store the payload in state, open `<RemapModal>`.
- Modal `onDone`: close, success toast ("Catalog updated — N references remapped"); append a warning toast if `spoolman_failures` is non-empty.
- Modal `onCancel`: close, info toast ("Catalog sync cancelled — profiles unchanged").

### Backend — Spoolman online sanity check (`routes/settings.py`)

When `POST /api/v1/settings/spoolman/test` receives a successful response from Spoolman, run a sanity check before returning to the client: compare every filament's `extra.orca_profiles` UUID keys against the Themis cached catalog's filament UUID set.

**This check is Spoolman-vs-catalog, not catalog-vs-catalog.** It fires because Spoolman's filament records may reference profiles that were never in the current Themis catalog (e.g., the catalog was rebuilt from a different OrcaSlicer library while Spoolman was offline).

**Algorithm:**

1. If `_catalog_dict` is None (cold cache), skip the check and return the existing `{"status": "ok", ...}` response — can't validate without a catalog.
2. Call `catalog_name_sets(_catalog_dict)` → `filament_uuids`.
3. `fetch_filaments()` from Spoolman. For each filament, decode `extra.orca_profiles` (double-JSON-encoded). Collect every UUID key not in `filament_uuids`.
4. If no stale UUIDs, return the existing success response unchanged.
5. If stale UUIDs found: group hits by `stale_uuid` (same dedup logic as `compute_drift` — one entry per unique UUID, with `affected_filament_ids` and `affected_filament_names`). Generate a `sync_id`, write `_pending_sync` with `raw=None` and `catalog=None` (no catalog swap will happen on confirm — only Spoolman updates apply), and return `status: "pending_remaps"` with `printers: []`, `jobs: []`, `spoolman_filaments: [deduplicated entries]`, and `options.filament_uuids` from `_catalog_dict`. The `options` other keys (`machine`, `process`, `filament`) are empty lists.

**`confirm-remap` reuse:** No changes to the confirm-remap endpoint. When `_pending_sync.raw` is `None`, the endpoint skips `_commit_catalog` after applying Spoolman updates and just clears the pending slot. `printers` and `jobs` resolution lists will be empty; the existing "missing resolution for required entry" validation trivially passes (no required entries).

**Frontend:** `testSpoolmanConnection()` in `api/settings.ts` changes its return type from a plain success object to `SyncResponse`. The "Test Connection" button handler in `SettingsScreen.tsx` adds the `pending_remaps` branch: store the payload, open `<RemapModal>`. `onDone` shows a success toast ("Spoolman connected — N filament profile references remapped"). `onCancel` shows an info toast ("Connected — stale profile references left unresolved").

No new Spoolman-specific payload shape: the existing `PendingRemaps` interface covers this case exactly.

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
| `themis` | `backend/app/services/catalog_utils.py` | **New** — `catalog_name_sets(catalog)` + `compute_drift(old, new, session, spoolman_cfg)` |
| `themis` | `backend/app/api/routes/settings.py` | Use `catalog_name_sets` instead of inline set-builds; Spoolman sanity check after successful test; return `SyncResponse` |
| `themis` | `backend/app/api/routes/laminus.py` | Split `_fetch_and_cache` into `_fetch_catalog`/`_commit_catalog`; drift gate in refresh + rescan; `_pending_sync` slot (supports `raw=None` for Spoolman-only pending); **new** `POST /catalog/confirm-remap` |
| `themis` | `backend/app/services/spoolman_service.py` | Add `update_filament_extra(url, api_key, filament_id, extra)` (Feature 2) |
| `themis` | `backend/app/api/routes/laminus.py` | Extend `catalog/status`: `catalog_counts`, `status`, 30 s health memo (Feature 3) |
| `themis` | `frontend/src/screens/HistoryScreen.tsx` | Add `filament_grams` to type + table column |
| `themis` | `frontend/src/api/laminus.ts` | `SyncResponse` union types for refresh/rescan; `confirmRemap()` |
| `themis` | `frontend/src/components/RemapModal.tsx` | **New** — stale-reference resolution modal |
| `themis` | `frontend/src/screens/SettingsScreen.tsx` | Refresh/Rescan handlers branch on `status`; mount `<RemapModal>`; Test Connection handler adds `pending_remaps` branch |
| `themis` | `frontend/src/api/settings.ts` | `testSpoolmanConnection()` returns `SyncResponse` instead of plain success object |

| `themis` | `frontend/src/components/LaminusStatusChip.tsx` | **New** — polling status chip |
| `themis` | `frontend/src/components/Sidebar.tsx` | Add `<LaminusStatusChip />` to Account section |

**Dropped from the 2026-07-13 revision** (superseded by the sync-integrated design): `backend/app/api/routes/drift.py`, the `drift` router registration in `main.py`, `frontend/src/api/drift.ts`, and `backend/tests/api/test_drift_api.py`.

---

## Tests

| File | Scenario |
|---|---|
| `backend/tests/api/test_jobs_api.py` | `GET /jobs/history` includes `filament_grams` from `job.filament_grams`; null when not set |
| `backend/tests/services/test_queue_engine.py` | `handle_print_complete` sets `job.filament_grams` from gcode before delete; calls `record_spool_use` once for the assigned printer's matched slot; skips deduction when SpoolmanConfig disabled; skips when grams null; HTTP error in `record_spool_use` does not affect job status |
| `backend/tests/services/test_spoolman_service.py` | `record_spool_use` calls correct Spoolman URL with `{"use_weight": grams}` |
| `backend/tests/api/test_laminus_api.py` | **Feature 2** — refresh with identical catalog completes immediately (`status: "ok"`, cache swapped); refresh with removed profile referenced by a printer returns `status: "pending_remaps"` with correct grouped entries (including `affected_printer_ids`) and options, and `GET /catalog` still serves the old catalog; two queued jobs with the same stale `print_profile` produce **one** `jobs` pending entry with two `affected_config_ids`; confirm-remap resolution keyed by `(field, stale_value)` is applied to all `affected_config_ids`; confirm-remap with valid resolutions updates `Printer` / `JobPrinterConfig` rows, swaps the cache, and clears the pending slot; confirm-remap missing a required printer resolution (or with a value not in the incoming catalog) returns 422 and applies nothing; confirm-remap with stale/unknown `sync_id` returns 409; Spoolman resolution with `new_uuid: null` drops the stale UUID key from all `affected_filament_ids` via `update_filament_extra`; cold cache (first sync) commits without drift check; Spoolman-only confirm (`raw=None`) applies `update_filament_extra` and clears pending slot without calling `_commit_catalog` |
| `backend/tests/api/test_settings_api.py` | **Feature 2 (Spoolman sanity check)** — test-connection success with all Spoolman UUIDs present in catalog returns normal success response; test-connection success where three filaments share one stale UUID returns `status: "pending_remaps"` with one `spoolman_filaments` grouped entry having three `affected_filament_ids`; cold catalog (`_catalog_dict` is None) at test-connection time returns normal success without checking UUIDs |
| `backend/tests/api/test_laminus_api.py` | **Feature 3** — `catalog/status` includes `catalog_counts` and correct `status` for each health state (online, building via 503, building via catalog_building flag, offline) |
| `backend/tests/services/test_catalog_utils.py` | **New** — `catalog_name_sets` returns correct sets for normal catalog; empty catalog; missing keys. `compute_drift`: returns None when no removals; returns None when removals are unreferenced; flags printer machine + slot filament, queued/blocked job configs only, stale Spoolman UUIDs; multiple objects sharing the same stale value produce a single grouped entry with all affected IDs; Spoolman disabled → section skipped; Spoolman fetch failure → `spoolman_error` set, other sections intact |
