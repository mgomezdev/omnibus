# Background Estimation — Design Spec

**Date:** 2026-07-13
**Revised:** 2026-07-13 (post Fable review + product decisions)
**Status:** Ready for implementation
**Repos:** `themis`

---

## 1. Overview

When the "Enable estimate generation" setting is on, Themis immediately queues a lightweight test slice through the Laminus sidecar after a job is created. The resulting gcode is parsed for time and per-filament material usage, then discarded. Estimates are stored directly on the `Job` row so users can see projected print time and filament consumption before the job is promoted to a real slice.

Once the job completes its actual print, the same `Job` row captures real time and real material from the production slice — before the `GcodeFile` row is deleted — giving a complete before/after record. Projects roll up both sets of values across their jobs.

**Relationship to the Filament Drift / Laminus Health spec:**
The `2026-07-13-filament-drift-health-design.md` spec's Feature 1 (Filament Consumption Tracking, originally migration v008) is superseded by this spec. This spec owns the final shape of `handle_print_complete` and migration v008. Features 2 (Profile Drift) and 3 (Laminus Health Chip) from that spec are unchanged and may be implemented independently.

---

## 2. Settings — `QueueConfig` Extension

### No separate `EstimateConfig` table

Add `estimates_enabled` directly to the existing `QueueConfig` singleton. This avoids a new table, new endpoints, and a new frontend settings section.

**Migration addition (part of v008):**

```sql
ALTER TABLE queue_config ADD COLUMN estimates_enabled BOOLEAN NOT NULL DEFAULT 0;
```

Applied with an idempotent PRAGMA guard (see Section 4).

**SQLAlchemy model addition:**

```python
# On QueueConfig:
estimates_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
```

**Pydantic schema additions:**

```python
class QueueConfigOut(BaseModel):
    check_interval_minutes: int
    operator_name: str | None
    snapshot_interval_seconds: int
    estimates_enabled: bool          # NEW

class QueueConfigIn(BaseModel):
    check_interval_minutes: int | None = None
    operator_name: str | None = None
    snapshot_interval_seconds: int | None = None
    estimates_enabled: bool | None = None  # NEW
```

`PUT /api/v1/settings/queue` already handles partial updates. Add:

```python
if body.estimates_enabled is not None:
    row.estimates_enabled = body.estimates_enabled
```

No new endpoints. The toggle lives in the existing Queue settings card on the frontend.

---

## 3. Themis-Side Slicing Queue Architecture

### Priority queue replaces semaphore and `ThreadPoolExecutor` for slicing

`QueueEngine` gains a single `asyncio.PriorityQueue` that serialises all Laminus slice operations — both production and estimate — from the Themis side. A single worker coroutine consumes it. This guarantees at-most-1 concurrent Laminus slice and gives production work inherent priority over estimates.

**Priority levels:**

| Priority | Type |
|---|---|
| `0` | Production slices (`_run_slice_and_print` path) |
| `1` | Estimate slices (`run_estimate` path) |

**Queue item shape:** `(priority: int, seq: int, coro: Coroutine)` — the `seq` field is a monotonically increasing integer tiebreaker. `asyncio.PriorityQueue` is heapq-backed; without a tiebreaker, equal-priority items (two simultaneous estimates, or two simultaneous production slices) would compare the coroutine objects, which are not orderable, raising `TypeError`. The tiebreaker guarantees deterministic FIFO ordering within each priority tier.

**`QueueEngine.__init__` additions:**

```python
self._slice_queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
self._slice_seq: itertools.count = itertools.count()
self._slice_worker_task: asyncio.Task | None = None
self._estimate_tasks: set[asyncio.Task] = set()
```

(`itertools` must be imported at top of `queue_engine.py`.)

The existing `ThreadPoolExecutor` (`self._executor`) is retained for non-slice blocking calls (upload, start_print). The executor is no longer used for the Laminus `slice_start + poll_status + download` calls — those move into the queue.

**`_slice_worker` coroutine:**

```python
async def _slice_worker(self) -> None:
    while True:
        priority, _seq, coro = await self._slice_queue.get()
        try:
            await coro
        except Exception:
            logger.exception("Slice worker: unhandled exception in queued coro")
        finally:
            self._slice_queue.task_done()
```

**`start()` addition:**

```python
self._slice_worker_task = asyncio.create_task(
    self._slice_worker(), name="slice_worker"
)
```

**`stop()` additions:**

```python
if self._slice_worker_task:
    self._slice_worker_task.cancel()
    await asyncio.gather(self._slice_worker_task, return_exceptions=True)

for t in list(self._estimate_tasks):
    t.cancel()
await asyncio.gather(*self._estimate_tasks, return_exceptions=True)
```

**How production slices use the queue:**

Inside `_run_slice_and_print`, instead of:

```python
gcode_path = await loop.run_in_executor(self._executor, self._slicer.slice, req)
```

The actual Laminus HTTP calls (inside `SlicerService.slice`) are placed in the queue at priority 0. The simplest approach: wrap the `asyncio.to_thread(self._slicer.slice, req)` call in a coroutine and enqueue it.

```python
fut: asyncio.Future = asyncio.get_running_loop().create_future()

async def _do_slice():
    try:
        result = await asyncio.to_thread(self._slicer.slice, req)
        fut.set_result(result)
    except Exception as exc:
        fut.set_exception(exc)

await self._slice_queue.put((0, next(self._slice_seq), _do_slice()))
gcode_path = await fut
```

The `_run_slice_and_print` coroutine suspends at `await fut` until the worker picks up and completes the slice.

### `SlicerService.slice()` — output directory parameter

Add an optional `output_dir: Path | None = None` parameter to `SlicerService.slice()`. When `None`, the existing `{data_dir}/gcode/{job_id}/` is used (production default). When provided (estimate path), the caller passes `{data_dir}/gcode_estimates/{job_id}/`.

```python
def slice(self, req: SliceRequest, output_dir: Path | None = None) -> str:
    ...
    out_dir = output_dir if output_dir is not None else (self._data_dir / "gcode" / str(req.job_id))
    out_dir.mkdir(parents=True, exist_ok=True)
    ...
```

Estimate slices pass `output_dir=data_dir / "gcode_estimates" / str(job_id)`. Because the estimate gcode never enters the `gcode_files` table and is deleted immediately after parsing, there is no directory collision with production gcode.

### Startup sweep for orphaned estimate gcode

In `QueueEngine.start()`, before starting the worker:

```python
import shutil
estimate_dir = self._slicer._data_dir / "gcode_estimates"
shutil.rmtree(estimate_dir, ignore_errors=True)
```

This cleans any estimate gcode left on disk from a prior unclean restart.

### `max_concurrent` removed

There is no `max_concurrent` configuration for estimates. The single-worker queue is the concurrency limit for all slices.

### Production slice throughput change (intentional)

Today production slices run concurrently (up to 4) via `ThreadPoolExecutor` — multiple printers can slice simultaneously. The single-worker queue serialises all Laminus slice calls to 1. This is intentional: Laminus is a single-process OrcaSlicer CLI runner and cannot meaningfully parallelize slices. The executor concurrency was providing false parallelism. The behavioral change: if two printers are both ready to slice at the same instant, the second slice waits for the first to complete. Given slice times of 1–10 minutes, this has negligible real-world impact on queue throughput. Also note: the queue is priority-ordered at dequeue, not preemptive — a production slice cannot interrupt an in-flight estimate. Estimate slices should be short (most jobs are fast) and the priority ordering prevents new estimates from jumping ahead of queued production work.

### `spawn_estimate` helper

To avoid duplicating trigger logic across `create_job`, `update_job_configs`, and `generate_project`, extract a `QueueEngine` method:

```python
def spawn_estimate(self, job_id: int) -> None:
    """Create and track a background estimate task for job_id."""
    task = asyncio.create_task(
        self.run_estimate(job_id), name=f"estimate-{job_id}"
    )
    self._estimate_tasks.add(task)
    task.add_done_callback(self._estimate_tasks.discard)
```

All three call sites (`create_job`, `update_job_configs`, `generate_project`) call `queue_engine.spawn_estimate(job.id)` after setting `estimate_status = "pending"` and committing.

---

## 4. Data Model

### Migration v008 — `v008_job_estimates_and_queue_config.py`

This migration owns all new columns for this feature. There is no v008 from any prior spec (v007 is `printer_bed_size`). All additions use idempotent PRAGMA guards.

```python
"""Add estimate/actual columns to jobs and estimates_enabled to queue_config."""
from __future__ import annotations
from sqlalchemy import text

version = 8
name = "job_estimates_and_queue_config"


async def up(conn) -> None:
    job_cols = {r[1] for r in (await conn.execute(text("PRAGMA table_info(jobs)"))).fetchall()}
    qc_cols  = {r[1] for r in (await conn.execute(text("PRAGMA table_info(queue_config)"))).fetchall()}

    # Actual values (captured at production slice time)
    if "actual_filament_grams" not in job_cols:
        await conn.execute(text("ALTER TABLE jobs ADD COLUMN actual_filament_grams REAL"))
    if "actual_seconds" not in job_cols:
        await conn.execute(text("ALTER TABLE jobs ADD COLUMN actual_seconds INTEGER"))
    if "actual_filament_breakdown" not in job_cols:
        await conn.execute(text("ALTER TABLE jobs ADD COLUMN actual_filament_breakdown JSON"))
    if "deduction_skipped" not in job_cols:
        await conn.execute(text("ALTER TABLE jobs ADD COLUMN deduction_skipped BOOLEAN"))

    # Estimate values (captured after background test slice)
    if "estimate_token" not in job_cols:
        await conn.execute(text("ALTER TABLE jobs ADD COLUMN estimate_token INTEGER NOT NULL DEFAULT 0"))
    if "estimate_status" not in job_cols:
        await conn.execute(text("ALTER TABLE jobs ADD COLUMN estimate_status TEXT"))
    if "estimate_seconds" not in job_cols:
        await conn.execute(text("ALTER TABLE jobs ADD COLUMN estimate_seconds INTEGER"))
    if "estimate_filament_grams" not in job_cols:
        await conn.execute(text("ALTER TABLE jobs ADD COLUMN estimate_filament_grams REAL"))
    if "estimate_filament_breakdown" not in job_cols:
        await conn.execute(text("ALTER TABLE jobs ADD COLUMN estimate_filament_breakdown JSON"))
    if "estimate_preset_label" not in job_cols:
        await conn.execute(text("ALTER TABLE jobs ADD COLUMN estimate_preset_label JSON"))

    # QueueConfig extension
    if "estimates_enabled" not in qc_cols:
        await conn.execute(text(
            "ALTER TABLE queue_config ADD COLUMN estimates_enabled BOOLEAN NOT NULL DEFAULT 0"
        ))


async def down(conn) -> None:
    # SQLite does not support DROP COLUMN before 3.35; recreate tables.
    # This down() exists only to satisfy rollback_last() — it is not intended
    # for production use. Re-create jobs without the new columns.
    await conn.execute(text("""
        CREATE TABLE jobs_new AS
        SELECT id, project_id, status, assigned_printer_id, block_reason,
               filament_map, created_at, updated_at
        FROM jobs
    """))
    await conn.execute(text("DROP TABLE jobs"))
    await conn.execute(text("ALTER TABLE jobs_new RENAME TO jobs"))
    # queue_config: no equivalent for DROP COLUMN; leave estimates_enabled in place.
    # (It will simply default to 0 on re-up.)
```

All new `jobs` columns are nullable with no default (existing rows get `NULL`). `deduction_skipped` defaults to `NULL` (falsy); it is only set to `True` on the reconcile-FAILED path.

### Updated `Job` model (additions only)

```python
# --- Actual values (set at production slice time, persisted before GcodeFile deleted) ---
actual_filament_grams: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
actual_seconds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
actual_filament_breakdown: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
# Set True on reconcile-FAILED path; NULL/False otherwise
deduction_skipped: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)

# --- Estimate values (set after background test slice) ---
estimate_token: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
estimate_status: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
estimate_seconds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
estimate_filament_grams: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
estimate_filament_breakdown: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
estimate_preset_label: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
```

**Note on naming:** The existing `GcodeFile.filament_grams` / `GcodeFile.estimated_seconds` are the source columns on the live gcode row (present while a job is slicing/uploading/printing). The new `Job.actual_filament_grams` and `Job.actual_seconds` are the persisted copies captured before the `GcodeFile` row is deleted. These are different column names on different tables — there is no ambiguity. The filament-drift spec's `job.filament_grams` column name is superseded; this spec uses `actual_filament_grams` for clarity.

### `estimate_status` values (four states)

| Value | Meaning |
|---|---|
| `NULL` | Not started — feature was disabled at job creation, or pre-v008 row, or was reset by cancellation/startup recovery |
| `"pending"` | Queued in the priority queue or currently being sliced (indistinguishable in practice — see O2) |
| `"done"` | Slice completed; all `estimate_*` fields populated |
| `"failed"` | Slice failed; estimate fields remain `NULL`; `block_reason` is not affected |

### `estimate_preset_label` JSON shape

```json
{
  "printer_name": "Bambu X1C (left)",
  "machine_profile": "Bambu Lab X1 Carbon 0.4 nozzle",
  "process_profile": "0.20mm Standard @BBL X1C",
  "filament_profiles": ["Bambu PLA Basic @BBL X1C", "Bambu PETG Basic @BBL X1C"]
}
```

Stored as structured JSON. Frontend renders however it likes.

### `estimate_filament_breakdown` / `actual_filament_breakdown` JSON shape

```json
[
  {"extruder_index": 0, "filament_profile": "Bambu PLA Basic @BBL X1C", "grams": 15.23},
  {"extruder_index": 1, "filament_profile": "Bambu PETG Basic @BBL X1C", "grams": 8.45}
]
```

For single-extruder jobs the list has exactly one entry. `filament_profile` is the OrcaSlicer preset name used for that extruder. For actual breakdown, it is the preset name passed to the production slice.

### No `JobEstimate` table

Inline columns on `Job`. There is exactly one estimate per job, the data is small, and all estimate reads occur alongside the job row anyway.

---

## 5. Estimation Flow

### Trigger — `create_job` in `jobs.py`

After `await session.commit()` and after `await session.refresh(job)`, before `queue_engine.wake()`:

```python
async with self._factory() as session:
    cfg = await session.get(QueueConfig, 1)
    estimates_enabled = cfg is not None and cfg.estimates_enabled

if estimates_enabled:
    job.estimate_token = (job.estimate_token or 0) + 1
    job.estimate_status = "pending"
    await session.commit()
    queue_engine.spawn_estimate(job.id)
```

The task is fire-and-forget; `create_job` still returns immediately with `estimate_status = "pending"`. Incrementing `estimate_token` at trigger time prevents a stale estimate (still in-flight from a previous config) from writing its results: `run_estimate` captures the token at load time and uses it in the final `WHERE` clause.

The same trigger pattern applies in `update_job_configs` and `generate_project` (see Section 10).

### `run_estimate` — step-by-step

```python
async def run_estimate(self, job_id: int) -> None:
```

**Step 1 — Load job and resolve config**

Open a session and load the `Job`. If the job is gone or already in a terminal status, return. Capture `token = job.estimate_token` before the session closes — this is the generation counter set at trigger time and used in the final WHERE clause to discard stale writes. Then load the `JobPrinterConfig` (lowest `id`) for this job, the associated `Printer`, and the `UploadedFile`. Capture all scalar values before the session closes.

Filament profile resolution (no "v1 single-extruder only" restriction — multi-extruder is fully supported):

- If `JobPrinterConfig.filament_map` is set: walk entries sorted by `tool_index`, read `filament_profile` directly from the entry if it has one, or fall back to `Printer.loaded_filaments[tool_index]["filament_profile"]` for slot-assigned entries.
- Otherwise (single config row): call `_slot_for_config(config, loaded)` to get the matched slot, then use `config.filament_profile or slot.get("filament_profile")`.

Build `preset_label` dict while all data is in scope.

The `estimate_status` is written to `"pending"` at trigger time and stays `"pending"` throughout. There is no `"running"` state transition (O2 decision).

**Step 2 — Pre-flight validation**

If `machine_preset` is empty, `stored_path` is missing, or `filament_profiles` is empty — call `_fail_estimate(job_id, token, reason)` and return.

**Step 3 — Enqueue slice**

Build a `SliceRequest` with:
- `filament_colours=[]` (cosmetic; skip for estimation)
- `export_args=[]` (raw gcode; no 3MF wrapper needed)
- `prepare_hook=None` (no AMS remapping for estimates)

Compute the output directory: `self._slicer._data_dir / "gcode_estimates" / str(job_id)`.

Create a coroutine that runs the slice and resolves a future:

```python
fut: asyncio.Future = asyncio.get_running_loop().create_future()

async def _do_estimate_slice():
    try:
        # Cancellation check inside the worker context
        async with self._factory() as s:
            j = await s.get(Job, job_id)
            if j is None or j.status in ("cancelled", "complete", "failed"):
                fut.cancel()
                return
            if j.estimate_status != "pending":
                fut.cancel()
                return
        result = await asyncio.to_thread(
            self._slicer.slice, req, output_dir
        )
        if not fut.cancelled():
            fut.set_result(result)
    except Exception as exc:
        if not fut.cancelled():
            fut.set_exception(exc)

await self._slice_queue.put((1, next(self._slice_seq), _do_estimate_slice()))
```

Then await the future:

```python
try:
    gcode_path = await fut
except asyncio.CancelledError:
    return  # job moved on while waiting in queue
except Exception as exc:
    logger.warning("Estimate slice failed for job %s: %s", job_id, exc)
    await self._fail_estimate(job_id, token, str(exc))
    return
```

**Step 4 — Parse, discard gcode, write results**

```python
grams, secs, extruder_grams = _parse_gcode_estimates(gcode_path)

try:
    os.remove(gcode_path)
except OSError:
    pass

breakdown = None
if extruder_grams is not None:
    breakdown = [
        {
            "extruder_index": i,
            "filament_profile": filament_profiles[i] if i < len(filament_profiles) else None,
            "grams": g,
        }
        for i, g in enumerate(extruder_grams)
    ]
```

Write results with a conditional UPDATE that guards against both cancellation and retrigger races. The `estimate_token` must match the value captured at Step 1 — if configs were updated (which increments the token) while this estimate was in-flight, the UPDATE finds no matching row and the stale results are discarded:

```python
async with self._factory() as session:
    result = await session.execute(
        text(
            "UPDATE jobs SET estimate_status='done', estimate_seconds=:secs, "
            "estimate_filament_grams=:grams, estimate_filament_breakdown=:bd, "
            "estimate_preset_label=:label, updated_at=:now "
            "WHERE id=:id AND estimate_status='pending' AND estimate_token=:token"
        ),
        {"secs": secs, "grams": grams, "bd": json.dumps(breakdown),
         "label": json.dumps(preset_label), "now": _now(), "id": job_id,
         "token": token}
    )
    if result.rowcount == 0:
        # Job was cancelled, re-triggered, or estimate_status was reset — discard
        return
    await session.commit()

await self._broadcast_job(job_id)
```

### `_fail_estimate` helper

Uses the same conditional UPDATE pattern. `token` is the generation counter captured at Step 1:

```python
async def _fail_estimate(self, job_id: int, token: int, reason: str) -> None:
    async with self._factory() as session:
        result = await session.execute(
            text(
                "UPDATE jobs SET estimate_status='failed', updated_at=:now "
                "WHERE id=:id AND estimate_status='pending' AND estimate_token=:token"
            ),
            {"now": _now(), "id": job_id, "token": token}
        )
        if result.rowcount > 0:
            await session.commit()
    logger.warning("Estimate failed for job %s: %s", job_id, reason)
    await self._broadcast_job(job_id)
```

Never touches `job.status` or `job.block_reason`.

### Cancellation handling

**Cancel path (`cancel_job` in `jobs.py`):**

In addition to the existing cancellation logic, set `estimate_status = NULL` if it is `'pending'`:

```python
if job.estimate_status == "pending":
    job.estimate_status = None
```

This happens in the same session block before `commit()`. The estimate task itself will either: (a) find the job is cancelled in its pre-flight check and return cleanly, or (b) find `estimate_status != 'pending'` in its conditional UPDATE and discard silently.

**`handle_print_complete` does not touch `estimate_status`.** A completed job's estimate_status is irrelevant post-completion.

**Startup recovery:**

In `QueueEngine.start()`, after resetting `slicing`/`uploading` jobs:

```python
async with self._factory() as session:
    await session.execute(
        text("UPDATE jobs SET estimate_status=NULL WHERE estimate_status='pending'")
    )
    await session.commit()
```

Orphaned `"pending"` rows are reset to `NULL`. Re-triggering estimates on startup is not required — the estimate is a nice-to-have and a missing one is not an error. The estimate gcode directory sweep (Section 3) ensures no stale files remain.

---

## 6. Actual Value Capture

### Changes to `_run_slice_and_print` — at production slice time

The actual values are captured immediately after the production slice succeeds, in the same session block that creates the `GcodeFile` row (current code lines 542–558 in `queue_engine.py`). This is before the `GcodeFile` is ever deleted, so the data is always present on the `Job` row by the time `handle_print_complete` runs.

```python
# Inside the session block after a successful slice:
grams, secs, extruder_grams = _parse_gcode_estimates(gcode_path)
gcode_rec = GcodeFile(
    job_id=job_id, printer_id=printer_id, path=gcode_path,
    filament_grams=grams, estimated_seconds=secs,
)
session.add(gcode_rec)

# Persist actuals on the Job row NOW (before gcode can be deleted)
job.actual_filament_grams = grams
job.actual_seconds = secs
if extruder_grams is not None:
    job.actual_filament_breakdown = [
        {
            "extruder_index": i,
            "filament_profile": filament_profiles[i] if i < len(filament_profiles) else None,
            "grams": g,
        }
        for i, g in enumerate(extruder_grams)
    ]
```

`filament_profiles` here is the same list already resolved earlier in `_run_slice_and_print` for the `SliceRequest`. No additional DB query is needed.

### Changes to `handle_print_complete`

`handle_print_complete` reads `job.actual_filament_grams` directly from the `Job` row — it no longer needs to access the `GcodeFile` row for this data. The `GcodeFile` is still deleted as before.

Spoolman deduction fires from `handle_print_complete` using `job.actual_filament_grams` (see Section 7).

---

## 7. Spoolman Deduction Policy

### Three distinct cases

**Case 1 — Normal completion (`handle_print_complete`):**
Deduct `job.actual_filament_grams` from Spoolman. Always fires regardless of what the operator later marks as the outcome. The printer consumed the plastic.

**Case 2 — Operator marks failed outcome (OutcomeModal / `PUT /{job_id}/outcome`):**
Still deduct. The operator marking failures is about counting good/bad parts — the filament was consumed regardless. The Spoolman deduction has already fired at completion time (Case 1); no second deduction is needed in the outcome path.

**Case 3 — Reconcile FAILED path (`_reconcile_printing_jobs` → `ended_in_failure == True`):**
No deduction. The job was physically aborted on the printer; total consumption is unknown or zero. Set `job.deduction_skipped = True` on the `Job` row so the frontend can surface a notice. The frontend should display: "Print was aborted — please manually update your Spoolman inventory."

### Spool ID resolution (S7)

In `handle_print_complete`, resolve the Spoolman spool ID from `Printer.loaded_filaments`, not from `JobPrinterConfig.filament_id` (which is a Spoolman filament-type ID, not a spool ID). All scalar values must be captured **inside the session block** before it closes, because the async deduction task runs after the session is gone:

```python
# Inside the existing session block in handle_print_complete:
printer = await session.get(Printer, job.assigned_printer_id)
config = (await session.execute(
    select(JobPrinterConfig)
    .where(JobPrinterConfig.job_id == job.id,
           JobPrinterConfig.printer_id == job.assigned_printer_id)
)).scalar_one_or_none()
spoolman_cfg = await session.get(SpoolmanConfig, 1)

# Capture all scalars before session closes
spool_id: int | None = None
spoolman_url: str | None = None
spoolman_key: str | None = None
grams_to_deduct: float | None = job.actual_filament_grams

if (
    spoolman_cfg and spoolman_cfg.enabled
    and grams_to_deduct is not None
    and printer and config
):
    slot = _slot_for_config(config, printer.loaded_filaments or {})
    if slot:
        spool_id = slot.get("spoolman_spool_id")
    spoolman_url = spoolman_cfg.url
    spoolman_key = spoolman_cfg.api_key
```

After `session.commit()`, fire the deduction:

```python
if spool_id is not None and spoolman_url and grams_to_deduct is not None:
    task = asyncio.create_task(
        _deduct_spool(spoolman_url, spoolman_key, spool_id, grams_to_deduct)
    )
    self._estimate_tasks.add(task)   # reuse the same tracking set
    task.add_done_callback(self._estimate_tasks.discard)
```

Three guard conditions that skip deduction gracefully:
- `spoolman_cfg` is disabled or missing → skip
- `job.actual_filament_grams is None` → skip (production slice did not parse grams)
- `slot.get("spoolman_spool_id") is None` → skip (printer slot has no spool assigned)

`_deduct_spool` is a module-level async helper (calls `spoolman_service.record_spool_use`; logs warning on failure; never raises). The deduction never blocks print completion.

**Multi-tool deduction:** Deduct the full `actual_filament_grams` total against the primary matched slot only. Per-slot deduction is out of scope in v1.

### Reconcile FAILED path changes

In the `ended_in_failure` branch of `_reconcile_printing_jobs`, add before `session.commit()`:

```python
job.deduction_skipped = True
```

Do not call `handle_print_complete` from this branch — the failure path is handled inline and must not deduct.

---

## 8. Parsing Changes — `_parse_gcode_estimates`

### Extended signature

```python
def _parse_gcode_estimates(path: str) -> tuple[float | None, int | None, list[float] | None]:
    """Extract filament_grams (total), estimated_seconds, and per-extruder grams.

    Returns (total_grams, seconds, extruder_grams_list).
    extruder_grams_list is a list of floats, one per comma-separated value in the
    'filament used [g]' header line. total_grams is the sum of that list.
    Returns None for each field independently if parsing fails.
    """
```

### Parsing change for `filament used [g]`

OrcaSlicer gcode headers:

```
; filament used [g] = 15.23, 8.45    # multi-extruder
; filament used [g] = 15.23           # single-extruder
```

New parsing (replaces the existing scalar parse):

```python
if "filament used [g]" in line.lower():
    raw_val = line.split("=")[-1].strip()
    parts = [p.strip() for p in raw_val.split(",")]
    try:
        extruder_grams = [float(p) for p in parts if p]
        grams = sum(extruder_grams)
    except ValueError:
        extruder_grams = None
        grams = None
```

For single-extruder: `extruder_grams = [15.23]`, `grams = 15.23`. No behavior change for single-extruder callers. The function returns `(grams, seconds, extruder_grams)` as a 3-tuple.

### All callers must unpack three values

**Existing call in `_run_slice_and_print` (line 546):**

```python
# Before:
grams, secs = _parse_gcode_estimates(gcode_path)

# After:
grams, secs, extruder_grams = _parse_gcode_estimates(gcode_path)
```

The `estimate_dir` cleanup in `run_estimate` and any other call sites must also be updated.

---

## 9. Project Rollup

### Three-value pattern (O4)

`_project_dict` in `projects.py` currently joins `GcodeFile` to sum `filament_grams` and `estimated_seconds`. Since `GcodeFile` rows are deleted on completion, this sum is zero for completed jobs. Replace with a direct query on `Job`:

**Remove:**

```python
gcode_rows = (await session.execute(
    select(GcodeFile).join(Job, Job.id == GcodeFile.job_id).where(Job.project_id == project.id)
)).scalars().all()
total_grams = sum(g.filament_grams for g in gcode_rows if g.filament_grams is not None) or None
total_seconds = sum(g.estimated_seconds for g in gcode_rows if g.estimated_seconds is not None) or None
```

**Replace with:**

```python
job_rows = (await session.execute(
    select(Job).where(Job.project_id == project.id)
)).scalars().all()

_TERMINAL = {"complete", "failed", "cancelled"}

# Estimate total — original projection, all jobs
estimate_filament_grams_total = (
    sum(j.estimate_filament_grams for j in job_rows if j.estimate_filament_grams is not None) or None
)
estimate_seconds_total = (
    sum(j.estimate_seconds for j in job_rows if j.estimate_seconds is not None) or None
)

# Estimate remaining — jobs not yet terminal
estimate_filament_grams_remaining = (
    sum(
        j.estimate_filament_grams for j in job_rows
        if j.estimate_filament_grams is not None and j.status not in _TERMINAL
    ) or None
)
estimate_seconds_remaining = (
    sum(
        j.estimate_seconds for j in job_rows
        if j.estimate_seconds is not None and j.status not in _TERMINAL
    ) or None
)

# Actual — only jobs where actuals were captured
actual_filament_grams = (
    sum(j.actual_filament_grams for j in job_rows if j.actual_filament_grams is not None) or None
)
actual_seconds = (
    sum(j.actual_seconds for j in job_rows if j.actual_seconds is not None) or None
)
```

### Updated `_project_dict` response keys

The existing keys `filament_grams` and `estimated_seconds` are removed. New keys:

```python
return {
    ...existing fields...,
    # Estimate rollup (from background test slices)
    "estimate_filament_grams_total": round(estimate_filament_grams_total, 2) if estimate_filament_grams_total else None,
    "estimate_seconds_total": estimate_seconds_total,
    "estimate_filament_grams_remaining": round(estimate_filament_grams_remaining, 2) if estimate_filament_grams_remaining else None,
    "estimate_seconds_remaining": estimate_seconds_remaining,
    # Actual rollup (from completed production prints)
    "actual_filament_grams": round(actual_filament_grams, 2) if actual_filament_grams else None,
    "actual_seconds": actual_seconds,
}
```

The old `filament_grams` and `estimated_seconds` keys are removed from the project response. This is a breaking change — the frontend must update all project detail displays.

---

## 10. API Changes

### `_to_dict(job)` in `jobs.py`

Add all new `Job` fields. This function is used by every job endpoint (`GET /jobs`, `GET /jobs/{id}`, `POST /jobs`, `PATCH /jobs/{id}/configs`, `POST /jobs/{id}/cancel`, `GET /jobs/history`), so all inherit the new fields automatically:

```python
def _to_dict(j: Job) -> dict:
    return {
        ...existing fields...,
        # Actual values (populated at production slice time)
        "actual_filament_grams": j.actual_filament_grams,
        "actual_seconds": j.actual_seconds,
        "actual_filament_breakdown": j.actual_filament_breakdown,
        "deduction_skipped": j.deduction_skipped,
        # Estimate values (populated after background test slice)
        "estimate_status": j.estimate_status,
        "estimate_seconds": j.estimate_seconds,
        "estimate_filament_grams": j.estimate_filament_grams,
        "estimate_filament_breakdown": j.estimate_filament_breakdown,
        "estimate_preset_label": j.estimate_preset_label,
    }
```

### `GET /api/v1/jobs/{job_id}/details` — job detail endpoint (S6)

Currently (lines 379–393 in `jobs.py`), this endpoint surfaces `filament_grams` and `estimated_seconds` sourced from the live `GcodeFile` row. These are the from-the-slice-file values available while the job is in `slicing/sliced/uploading/printing` states. They must remain available from the detail endpoint during those states, but must not shadow the persisted `Job` columns.

Rename the live keys to avoid collision:

```python
return {
    **_to_dict(job),                    # includes all new Job fields
    "block_reason": job.block_reason,
    "file": file_info,
    "plate": plate_info,
    "printer_configs": printer_configs,
    "assigned_printer": assigned_printer,
    # Live slice data — present only while GcodeFile row exists (slicing→printing)
    "filament_grams_live": gcode_rec.filament_grams if gcode_rec else None,
    "estimated_seconds_live": gcode_rec.estimated_seconds if gcode_rec else None,
}
```

The old `"filament_grams"` and `"estimated_seconds"` keys in the detail response are replaced by `"filament_grams_live"` / `"estimated_seconds_live"` (live) and `"actual_filament_grams"` / `"actual_seconds"` (persisted, from `_to_dict`). This is a breaking change — all frontend callers of `estimated_seconds` in the job detail response must be audited.

### `PATCH /api/v1/jobs/{job_id}/configs` — config update

When configs are replaced, clear the estimate state and re-trigger:

```python
# After deleting old configs, before adding new ones:
job.estimate_status = None
job.estimate_seconds = None
job.estimate_filament_grams = None
job.estimate_filament_breakdown = None
job.estimate_preset_label = None
job.updated_at = _now()
```

After committing the new configs:

```python
cfg = await session.get(QueueConfig, 1)
if cfg is not None and cfg.estimates_enabled:
    job.estimate_token = (job.estimate_token or 0) + 1
    job.estimate_status = "pending"
    await session.commit()
    queue_engine.spawn_estimate(job.id)
```

### `POST /api/v1/projects/{id}/generate` — generate_project

`generate_project` creates `Job` rows directly (bypassing `create_job`) and must trigger estimates for each. After each `Job` is inserted and committed:

```python
cfg = await session.get(QueueConfig, 1)
if cfg is not None and cfg.estimates_enabled:
    job.estimate_token = (job.estimate_token or 0) + 1
    job.estimate_status = "pending"
    await session.commit()
    queue_engine.spawn_estimate(job.id)
```

This loop runs for each job created by the generate call. If the project has many items, estimates are queued sequentially at priority 1 and processed one at a time by the worker — no special batch handling is needed.

### `POST /api/v1/jobs/{job_id}/cancel` — cancel

Add to the cancel block (before `session.commit()`):

```python
if job.estimate_status == "pending":
    job.estimate_status = None
```

### `GET/PUT /api/v1/settings/queue`

Extended with `estimates_enabled` — see Section 2. No new endpoints.

### `GET /api/v1/projects/{id}` and `GET /api/v1/projects`

Remove `filament_grams` and `estimated_seconds`. Add the six new rollup keys — see Section 9. Both `get_project` and `list_projects` use `_project_dict`, so both are updated automatically.

---

## 11. Frontend Surface

### Job Detail screen

**Estimate section** — shown when `estimate_status` is not `null`:

| State | Display |
|---|---|
| `"pending"` | "Estimating…" with a subtle spinner |
| `"done"` | Estimate card: time (h m), total grams, per-filament table (profile name + grams per extruder), preset label chips |
| `"failed"` | "Estimate unavailable" in muted text |
| `null` | Hidden entirely |

**Actual section** — shown after completion (`status === "complete"` or `"failed"`):

- Print time from `actual_seconds`
- Total filament from `actual_filament_grams`
- Per-filament table from `actual_filament_breakdown`
- If `deduction_skipped === true`: warning notice "Print was aborted — please manually update your Spoolman inventory."

**Live slice data** — shown when job is in `slicing / sliced / uploading / printing`:

- Use `filament_grams_live` and `estimated_seconds_live` from the detail endpoint, labeled "From current slice"

### Job History screen

- Add `Estimate` column group: `estimate_filament_grams` (g) and `estimate_seconds` (formatted)
- Add `Actual` column group: `actual_filament_grams` (g) and `actual_seconds` (formatted)
- Both groups show "—" when `null`
- Remove `filament_grams` column if it was previously wired to `GcodeFile`

### Project Detail screen

Replace the old single `estimated_seconds` / `filament_grams` display with:

- "Estimated total": `estimate_filament_grams_total` + `estimate_seconds_total` — the sum of all original estimates across all jobs
- "Estimated remaining": `estimate_filament_grams_remaining` + `estimate_seconds_remaining` — estimates for jobs not yet complete/failed/cancelled
- "Actual total": `actual_filament_grams` + `actual_seconds` — from completed prints only
- All rows show "—" when `null`

### Settings screen — Queue settings card

Add to the existing Queue settings card (no new section):

- Toggle: "Enable estimate generation for queued jobs" — bound to `estimates_enabled`
- Caption: "When enabled, a test slice runs immediately after job creation to estimate print time and filament use. The gcode is discarded — only time and grams are stored."
- `PUT /api/v1/settings/queue` with `{ "estimates_enabled": true/false }`

---

## 12. File Changelist

| Repo | File | Change |
|---|---|---|
| `themis` | `backend/app/migrations/v008_job_estimates_and_queue_config.py` | **New** — all new `jobs` columns + `queue_config.estimates_enabled`; full idempotent PRAGMA guard; `down()` for rollback support |
| `themis` | `backend/app/migrations/runner.py` | Register `v008_job_estimates_and_queue_config` in `_MIGRATIONS` list |
| `themis` | `backend/app/models.py` | Add `actual_filament_grams`, `actual_seconds`, `actual_filament_breakdown`, `deduction_skipped`, `estimate_token`, `estimate_status`, `estimate_seconds`, `estimate_filament_grams`, `estimate_filament_breakdown`, `estimate_preset_label` to `Job`; add `estimates_enabled` to `QueueConfig` |
| `themis` | `backend/app/services/slicer_service.py` | Add `output_dir: Path \| None = None` parameter to `slice()`; use it when provided |
| `themis` | `backend/app/services/spoolman_service.py` | Add `record_spool_use(url, api_key, spool_id, grams)` function |
| `themis` | `backend/app/services/queue_engine.py` | Extend `_parse_gcode_estimates` → 3-tuple; add `_slice_queue`, `_slice_seq`, `_slice_worker_task`, `_estimate_tasks` to `__init__`; add `_slice_worker`, `spawn_estimate`, `run_estimate`, `_fail_estimate`, `_deduct_spool`; update `start()` (startup recovery + worker start + dir sweep); update `stop()` (cancel worker + estimate tasks); update `_run_slice_and_print` to enqueue slice at priority 0, capture `actual_*` on Job in the post-slice session block, unpack 3-tuple; update `handle_print_complete` to resolve spool ID and fire deduction task using `_estimate_tasks` set; update `_reconcile_printing_jobs` FAILED branch to set `job.deduction_skipped = True` and skip deduction; remove `ThreadPoolExecutor` usage for Laminus calls |
| `themis` | `backend/app/api/routes/jobs.py` | `_to_dict`: add all new Job fields; `create_job`: increment `estimate_token`, set `estimate_status='pending'`, call `spawn_estimate`; `cancel_job`: clear `estimate_status` if `'pending'`; `update_job_configs`: clear estimate fields, increment token, re-trigger; `get_job_details`: add `filament_grams_live` + `estimated_seconds_live` (renamed); `generate_project`: trigger `spawn_estimate` per job when enabled; import `QueueConfig` |
| `themis` | `backend/app/api/routes/projects.py` | `_project_dict`: remove `gcode_rows` join; add 6-key rollup from `job_rows`; remove old `filament_grams` + `estimated_seconds` keys |
| `themis` | `backend/app/api/routes/settings.py` | Add `estimates_enabled` to `QueueConfigOut` and `QueueConfigIn`; update `update_queue_config` handler |
| `themis` | `frontend/src/api/queue.ts` | Add all new fields to `ApiJobDetails` interface; rename `estimated_seconds` → `estimated_seconds_live` in detail response type |
| `themis` | `frontend/src/api/projects.ts` | Remove `filament_grams` + `estimated_seconds`; add 6 new rollup keys |
| `themis` | `frontend/src/api/settings.ts` | Add `estimates_enabled: boolean` to queue config type |
| `themis` | `frontend/src/screens/JobDetailScreen.tsx` | Add estimate card (pending/done/failed); add actual section with `deduction_skipped` notice; update live-slice display to use `filament_grams_live` / `estimated_seconds_live` |
| `themis` | `frontend/src/screens/HistoryScreen.tsx` | Add `Estimate` and `Actual` column groups |
| `themis` | `frontend/src/screens/ProjectDetailScreen.tsx` | Replace single `estimated_seconds` row with Estimated Total / Estimated Remaining / Actual Total rows |
| `themis` | `frontend/src/screens/SettingsScreen.tsx` | Add `estimates_enabled` toggle to Queue settings card |
| `themis` | `frontend/src/screens/__tests__/EditJobScreen.test.tsx` | Update `filament_grams: null` fixture → `actual_filament_grams: null`; add `estimated_seconds_live: null` fixture |

**Frontend files to audit for `estimated_seconds` references:**
The field `estimated_seconds` was previously surfaced in:
- `jobs.py:393` (detail endpoint) — renamed to `estimated_seconds_live`
- `projects.py:205` (project response) — removed entirely

Search for `estimated_seconds` across all frontend source files before shipping; any reference must be updated to the appropriate new key.

---

## 13. Test Plan

| File | Scenario |
|---|---|
| `backend/tests/services/test_queue_engine.py` | `_parse_gcode_estimates` — single extruder: returns `(grams, secs, [grams])`; multi-extruder comma-separated: returns correct list and summed total; missing line: returns `(None, None, None)` |
| `backend/tests/services/test_queue_engine.py` | `run_estimate` — enabled: sets `estimate_status="pending"` after create; write guard: conditional UPDATE sets `"done"` only when `estimate_status='pending'`; populates all `estimate_*` fields on success |
| `backend/tests/services/test_queue_engine.py` | `run_estimate` — `SliceError` from slicer: sets `estimate_status="failed"` via conditional UPDATE; does not set `block_reason`; does not change `job.status` |
| `backend/tests/services/test_queue_engine.py` | `run_estimate` — job cancelled before enqueued slice runs: future is cancelled; `run_estimate` returns without writing; `estimate_status` remains `NULL` (set by `cancel_job`) |
| `backend/tests/services/test_queue_engine.py` | `run_estimate` — job cancelled after slice completes but before write: conditional UPDATE finds `estimate_status != 'pending'`, rowcount = 0; results discarded |
| `backend/tests/services/test_queue_engine.py` | `_fail_estimate` — conditional UPDATE: only marks `"failed"` if `estimate_status='pending'`; no-op when already `NULL` |
| `backend/tests/services/test_queue_engine.py` | Priority queue ordering: production slice (priority 0) runs before estimate slice (priority 1) when both are queued simultaneously; two equal-priority items do not raise `TypeError` (seq tiebreaker) |
| `backend/tests/services/test_queue_engine.py` | `handle_print_complete` — reads `job.actual_filament_grams` (set at slice time) for Spoolman deduction; does not access `GcodeFile` for grams |
| `backend/tests/services/test_queue_engine.py` | `_run_slice_and_print` — after successful slice: `job.actual_filament_grams`, `job.actual_seconds`, `job.actual_filament_breakdown` set in same session block as `GcodeFile` insert; single-extruder breakdown has one entry with correct profile name; multi-extruder breakdown has correct count and profile names |
| `backend/tests/services/test_queue_engine.py` | Spoolman deduction — normal completion: fires once using spool_id from `Printer.loaded_filaments` slot (not `filament_id`); skipped when `SpoolmanConfig` disabled; skipped when `spoolman_spool_id` is None; skipped when `actual_filament_grams` is `None`; HTTP error does not affect job completion |
| `backend/tests/services/test_queue_engine.py` | Spoolman deduction — aborted print (reconcile FAILED): `deduction_skipped = True` set on Job; `record_spool_use` is NOT called |
| `backend/tests/services/test_queue_engine.py` | Startup recovery: all jobs with `estimate_status='pending'` are reset to `NULL`; no re-triggering occurs; estimate gcode directory is swept |
| `backend/tests/services/test_spoolman_service.py` | `record_spool_use` calls `PUT /api/v1/spool/{id}/use` with `{"use_weight": grams}`; raises on non-2xx |
| `backend/tests/api/test_jobs_api.py` | `POST /jobs` with estimation enabled: response includes `estimate_status="pending"`; after mock estimate task runs, `GET /jobs/{id}` shows `"done"` with populated fields |
| `backend/tests/api/test_jobs_api.py` | `POST /jobs` with estimation disabled: `estimate_status` is `null` in response |
| `backend/tests/api/test_jobs_api.py` | `POST /jobs/{id}/cancel` — while `estimate_status="pending"`: response has `estimate_status=null`; no DB row with `"pending"` remains |
| `backend/tests/api/test_jobs_api.py` | `GET /jobs/{id}/details` — fields `filament_grams_live` and `estimated_seconds_live` present when `GcodeFile` row exists; `null` when no gcode row; `actual_filament_grams` and `actual_seconds` from Job row in both cases |
| `backend/tests/api/test_jobs_api.py` | `PATCH /jobs/{id}/configs` — clears all `estimate_*` fields; increments `estimate_token`; re-triggers estimation when enabled; does not re-trigger when disabled |
| `backend/tests/api/test_jobs_api.py` | `POST /projects/{id}/generate` — when estimates enabled, each created job has `estimate_status='pending'` and `estimate_token=1`; `spawn_estimate` called once per job |
| `backend/tests/api/test_jobs_api.py` | `GET /jobs/history` — returns `estimate_filament_grams`, `estimate_seconds`, `actual_filament_grams`, `actual_seconds`, `actual_filament_breakdown` for completed jobs; `null` when not set |
| `backend/tests/api/test_projects_api.py` | `GET /projects/{id}` — `estimate_filament_grams_total` is sum across all jobs; `estimate_filament_grams_remaining` excludes terminal jobs; `actual_filament_grams` is sum only for jobs with non-null actual; all three are `null` when no values present |
| `backend/tests/api/test_projects_api.py` | `GET /projects/{id}` — `filament_grams` and `estimated_seconds` keys are absent from response; GcodeFile join is not used |
| `backend/tests/api/test_settings_api.py` | `GET /settings/queue` returns `estimates_enabled: false` by default; `PUT /settings/queue` with `estimates_enabled: true` persists and returns the value; `estimates_enabled` is independent of other queue config fields |
| `backend/tests/migrations/test_v008.py` | Migration applies cleanly on empty DB; applies cleanly on DB that already has any subset of the new columns (full idempotency); all new `jobs` columns are nullable; `queue_config.estimates_enabled` defaults to `0`; v008 is registered in `runner._MIGRATIONS` and applies in the correct order |
