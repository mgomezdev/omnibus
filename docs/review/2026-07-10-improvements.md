# Print Farm Improvements — Ranked by Value

**Date:** 2026-07-10  
**Basis:** Fable advisory review + technical research pass

Ranked by impact × urgency for the target use case: personal farm, <10 printers, multi-part project tracking (cosplay, gridfinity, modular systems).

---

## Ranked List

| # | Title | Category | Effort |
|---|---|---|---|
| 1 | [Fix Ordinus→Themis 422 on project generation](#1-fix-ordinusthemis-422-on-project-generation) ✅ | Bug | S |
| 2 | [Add project tracking: project_id on jobs + progress view](#2-add-project-tracking-project_id-on-jobs--progress-view) ✅ | Feature | M |
| 3 | [Fix check_overrides NameError](#3-fix-check_overrides-nameerror) ✅ | Bug | XS |
| 4 | [Add Spoolman to compose stack](#4-add-spoolman-to-compose-stack) | DevOps | S |
| 5 | [Persist Laminus job state across restarts](#5-persist-laminus-job-state-across-restarts) ✅ | Reliability | M |
| 6 | [Fix CORS_ORIGIN for LAN access](#6-fix-cors_origin-for-lan-access) | Bug | XS |
| 7 | [Webhook notifications on job state change](#7-webhook-notifications-on-job-state-change) | Feature | S |
| 8 | [Print history view](#8-print-history-view) ✅ | Feature | S |
| 9 | [Surface filament estimates + Spoolman inventory check](#9-surface-filament-estimates--spoolman-inventory-check) | Feature | M |
| 10 | [Cache Laminus profile catalog to disk](#10-cache-laminus-profile-catalog-to-disk) ✅ | Reliability | S |
| 11 | [Unify Projects and Orders into one concept](#11-unify-projects-and-orders-into-one-concept) | Refactor | L |
| 12 | [Add Themis healthcheck to compose](#12-add-themis-healthcheck-to-compose) ✅ | DevOps | XS |
| 13 | [Fix compose bind-mount paths for Laminus](#13-fix-compose-bind-mount-paths-for-laminus) ✅ | Bug | XS |
| 14 | [Remove dead auth plumbing in Ordinus](#14-remove-dead-auth-plumbing-in-ordinus) ✅ | Cleanup | S |
| 15 | [Add themis-data backup documentation](#15-add-themis-data-backup-documentation) ~~won't implement~~ | Docs | XS |
| 16 | [Fix documentation drift (data-model.md, Ordinus README)](#16-fix-documentation-drift) | Docs | S |
| 17 | [Profile onboarding UI in Themis settings](#17-profile-onboarding-ui-in-themis-settings) ~~won't implement~~ | UX | M |
| 18 | [Mobile-responsive UI](#18-mobile-responsive-ui) | UX | L |
| 19 | [Manual job outcome marking + per-item failure tracking](#19-manual-job-outcome-marking--per-item-failure-tracking) ✅ | Feature | M |

---

## Story Cards

---

### 1. Fix Ordinus→Themis 422 on project generation ✅

**Category:** Bug  
**Effort:** S (1 day)  
**Repos:** `ordinus`, `themis`

**Problem**  
When a user clicks "Send to Themis" in Ordinus, the Express server POSTs project items with `filament_profile_uuid: ''` (empty string) because Ordinus has no concept of Themis filament profiles. Themis's `generate_project` endpoint raises HTTP 422 when any item is missing a filament profile UUID. The user lands in Themis with a project they cannot generate without first manually assigning machine, process, and filament profile to every part — a requirement never communicated in the UI. This breaks the primary advertised integration path for every new user.

**What to do**

*Themis (`backend/app/api/routes/projects.py`):*
- In `generate_project`, distinguish items where `filament_profile_uuid` is empty/null from items where it's set to an invalid UUID. Treat empty as "defer" — skip filament filtering for that item and allow plate-packing to proceed without a profile lock. The job should be created in `queued` state but marked to require profile assignment before slicing begins (a `needs_profile` boolean or a dedicated `draft` status).
- Return a clear JSON response body explaining what manual steps remain, not just a 422 status code.

*Ordinus (`server/src/controllers/themis.controller.ts`):*
- After creating the project in Themis, check the response for any `needs_profile` items and surface a UI banner: "Project created in Themis. Assign filament profiles to [N] parts before generating."
- Include the Themis project URL in the banner so the user can navigate directly.

**Acceptance criteria**
- "Send to Themis" succeeds (2xx) regardless of whether filament profiles have been assigned in Ordinus.
- Themis shows the project with a visible indicator on items that still need a profile.
- `generate_project` can be triggered on a project with unassigned profiles and either skips those items or queues them as draft jobs, with a clear UI explanation.
- A user who assigns profiles in Themis and then generates gets normal behavior.

**Files likely touched**
- `themis/backend/app/api/routes/projects.py`
- `themis/backend/app/models.py` (possibly — `needs_profile` flag or status enum addition)
- `ordinus/server/src/controllers/themis.controller.ts`
- Themis frontend: project detail view to show incomplete-profile state

---

### 2. Add project tracking: `project_id` on jobs + progress view

**Category:** Feature  
**Effort:** M (2–3 days)  
**Repos:** `themis`

**Problem**  
`generate_project` creates `Job` rows with no `project_id`. Once jobs are queued, there is no database-backed way to answer "which jobs belong to this project?" or "6 of 14 parts done." Project items carry `quantity` fields at generation time — that data is used to determine how many jobs to create but is otherwise discarded. The system can execute a 30-part cosplay build but cannot tell you how much of it is done. This is the central gap for the stated use case.

**What to do**

*Data layer (`themis/backend/app/models.py`, `database.py`):*
- Add `project_id: Optional[int]` FK to the `jobs` table referencing `projects.id`.
- Add to `_ALTERS`: `ALTER TABLE jobs ADD COLUMN project_id INTEGER REFERENCES projects(id)`.
- Add `quantity_needed: int` and `quantity_completed: int` to `ProjectItem` (or track at the `Project` level via a computed view of linked jobs).

*`generate_project` (`projects.py`):*
- Set `job.project_id = project.id` on every created job.
- Store `quantity_needed` from the project item on the job or on a new `ProjectItem` aggregate.

*Queue engine (`queue_engine.py`):*
- When a job transitions to `complete`, increment `quantity_completed` on the parent `ProjectItem` (or update a denormalized counter on `Project`).

*WebSocket broadcast:*
- Include `project_id` in job state change messages so the frontend can update project progress in real time.

*Frontend:*
- Add a progress bar or part counter to the project detail view: "8 / 14 parts complete."
- Show per-plate job status (queued / slicing / printing / complete / failed) grouped under the project.

**Acceptance criteria**
- Every job created by `generate_project` has a non-null `project_id`.
- Project detail view shows a live progress indicator updated via WebSocket.
- Completing a job increments the project's completed count; failing a job is visually distinct from completing one.
- Querying `SELECT * FROM jobs WHERE project_id = ?` returns exactly the jobs for that project.

**Files likely touched**
- `themis/backend/app/models.py`
- `themis/backend/app/database.py` (`_ALTERS`)
- `themis/backend/app/api/routes/projects.py`
- `themis/backend/app/services/queue_engine.py`
- `themis/backend/app/api/routes/jobs.py` (WebSocket broadcast payload)
- Themis frontend: project detail screen

---

### 3. Fix `check_overrides` NameError

**Category:** Bug  
**Effort:** XS (~1 hour)  
**Repos:** `themis`

**Problem**  
`POST /api/v1/jobs/check-overrides` at `jobs.py:233` references `loop` and `client` that are never defined in scope. `LaminusSidecarClient` is imported at the top of the file but never instantiated inside the function. Any request to this endpoint raises `NameError` and returns a 500. This is the endpoint used in the New Job flow to warn users when embedded 3MF settings will override the canonical profile — a genuine safety feature that is completely non-functional.

**What to do**

In `themis/backend/app/api/routes/jobs.py`, in the `check_overrides` async endpoint function:

1. Instantiate `LaminusSidecarClient` using the sidecar URL from config:
   ```python
   client = LaminusSidecarClient(get_laminus_sidecar_url())
   ```
2. Replace `loop.run_in_executor(...)` with `asyncio.get_running_loop().run_in_executor(...)`, consistent with the pattern used elsewhere in the file.
3. Ensure the client is used (or properly awaited) and that its response is returned correctly.

**Acceptance criteria**
- `POST /api/v1/jobs/check-overrides` with a valid 3MF body returns 200 with a diff payload (or empty diff if no overrides).
- No `NameError` or 500 on any call to this endpoint.
- Existing tests for this route pass; add one if none exists.

**Files likely touched**
- `themis/backend/app/api/routes/jobs.py` (lines ~225–250)

---

### 4. Add Spoolman to compose stack

**Category:** DevOps  
**Effort:** S (2–3 hours)  
**Repos:** `omnibus` (Concordia)

**Problem**  
Spoolman is prominently documented as a core feature — filament inventory, spool tracking, and the filament-match blocking that is one of the queue engine's strongest features all depend on it. It is not in `docker-compose.yml`. A user who runs `docker compose up` gets a stack with no filament tracking. They must separately find the Spoolman image, stand it up, discover its URL format, and configure it in Themis settings. The "one command and it works" story is broken for a major advertised feature.

**What to do**

In `omnibus/docker-compose.yml`:
- Add a `spoolman` service using the official image (`ghcr.io/donkie/spoolman:latest`).
- Mount a named volume for its SQLite data (`spoolman-data:/home/app/.local/share/spoolman`).
- Add `SPOOLMAN_URL=http://spoolman:7912` to Themis's environment block.
- Add `spoolman-data` to the top-level `volumes` section.
- Make Themis `depends_on: spoolman: condition: service_started` (Spoolman has no health endpoint by default; `service_started` is appropriate).

In `.env.example`:
- Document the `SPOOLMAN_URL` variable with a note that it defaults to the compose service.

In `CLAUDE.md` / user-facing docs:
- Note that Spoolman is included and available at `http://localhost:7912` by default.

**Acceptance criteria**
- `docker compose up` starts Themis, Laminus, Ordinus, and Spoolman.
- Themis's filament-match blocking works out of the box without any manual Spoolman configuration.
- Spoolman UI is accessible at `http://localhost:7912`.
- Spoolman data persists across restarts via the named volume.

**Files likely touched**
- `omnibus/docker-compose.yml`
- `omnibus/.env.example`
- `omnibus/CLAUDE.md`

---

### 5. Persist Laminus job state across restarts

**Category:** Reliability  
**Effort:** M (1–2 days)  
**Repos:** `laminus` (orca)

**Problem**  
All active slice job state in Laminus lives in a Python `dict` (`jobs: dict[str, Job]`) in memory. Any container restart — OOM kill, Docker update, host reboot — silently loses every in-flight job. Themis has no way to distinguish "Laminus restarted and forgot me" from "still slicing"; it polls for up to 620 seconds before timing out and marking the job failed. For a farm running a 60-part overnight batch, this is a realistic failure path with a bad user experience: wake up to a dozen failed jobs and no explanation.

**What to do**

In `laminus/app/main.py`:

1. On job creation (`POST /api/slice/start`), write the job record to a small SQLite DB or JSON file at `/data/jobs.db` (or `/data/jobs.json`). Include: `job_id`, `status`, `created_at`, `config` (the slice config used), `output_path`.
2. On job status change (pending → running → complete/failed), update the record.
3. On startup, read any existing job records. For jobs in `running` state at startup time, transition them to `failed` with `error: "Laminus restarted during slicing"` — Themis will pick these up on its next poll.
4. On successful download (`GET /api/slice/download/{job_id}`), mark the job `downloaded` and clean up the working directory. Remove the record from the DB after a configurable TTL (e.g., 24h).

Separately: cache the resolved profile catalog to `/data/catalog_cache.json` keyed on a hash of the config directory mtime. On startup, load the cache if it's valid; fall back to full rebuild if not. This reduces the post-restart unavailability window from ~60 seconds to near-zero for normal restarts.

**Acceptance criteria**
- Restarting the Laminus container while a slice is in progress transitions the job to `failed` on next Themis poll, with an error message that explains the restart.
- Restarting Laminus when no jobs are active has no user-visible effect on the queue.
- Profile catalog is available within 5 seconds of Laminus start on a warm restart (cached).
- Cold start (first boot or `ORCA_VERSION` change) still does full catalog rebuild.

**Files likely touched**
- `orca/app/main.py` (Laminus)
- `orca/docker-compose.yml` (ensure `/data` volume covers the new persistence path)

---

### 6. Fix CORS_ORIGIN for LAN access

**Category:** Bug  
**Effort:** XS (~30 minutes)  
**Repos:** `omnibus` (Concordia)

**Problem**  
`CORS_ORIGIN` in `docker-compose.yml` (or `.env.example`) is set to `http://localhost:${ORDINUS_PORT}`. This restricts cross-origin API calls to requests originating from `localhost` only. Any user accessing Ordinus from another machine on the LAN (a laptop, a phone, a PC next to the printers) gets CORS failures on every API call. For a self-hosted farm tool, "usable from another machine on the network" is a basic expectation.

**What to do**

Option A (recommended for a trusted LAN tool): Set `CORS_ORIGIN=*` in `docker-compose.yml` for Ordinus (and confirm Themis's CORS config also allows `*`).

Option B: Set `CORS_ORIGIN` to a comma-separated list of expected origins and document how to add entries in `.env.example`. More correct for a shared environment; more friction for a personal one.

Either way, update `.env.example` with a comment explaining the tradeoff and the default.

**Acceptance criteria**
- Ordinus UI is fully functional when accessed from a different machine on the same LAN.
- No CORS errors appear in the browser console when using the app from a non-`localhost` origin.

**Files likely touched**
- `omnibus/docker-compose.yml` (Ordinus environment block)
- `omnibus/.env.example`
- Possibly `ordinus/server/src/index.ts` or wherever Ordinus configures Express CORS middleware

---

### 7. Webhook notifications on job state change

**Category:** Feature  
**Effort:** S (1 day)  
**Repos:** `themis`

**Problem**  
There is no way to know a print completed (or failed) without manually checking the Themis UI. For a farm running overnight or unattended, this means either staying up or checking in the morning. A simple webhook on job state transitions would allow routing to ntfy, Discord, Home Assistant, or any other notification system without building notification delivery into Themis itself.

**What to do**

*Data layer:*
- Add a `WebhookConfig` model (or reuse/extend `QueueConfig`): `url: str`, `secret: Optional[str]` (for HMAC signing), `events: List[str]` (e.g., `['job.complete', 'job.failed', 'job.blocked']`).
- Add a settings UI page in Themis to configure the webhook URL and select which events to subscribe to.

*Queue engine / job state transitions:*
- After each job state change, if a webhook URL is configured and the event matches, fire an async `httpx` POST to the webhook URL with a JSON body:
  ```json
  {
    "event": "job.complete",
    "job_id": "...",
    "job_name": "...",
    "printer": "...",
    "project_id": "...",
    "timestamp": "..."
  }
  ```
- Sign the payload with HMAC-SHA256 using the secret (if configured) in an `X-Webhook-Signature` header.
- Fire-and-forget with a 5-second timeout; log failures but don't affect job state.

**Acceptance criteria**
- A configured webhook URL receives a POST within 5 seconds of a job transitioning to `complete`, `failed`, or `blocked`.
- Payload includes enough information to construct a useful notification (job name, printer, project, status).
- Webhook failures are logged but do not affect queue operation.
- Settings UI allows configuring URL, secret, and event filter.

**Files likely touched**
- `themis/backend/app/models.py` (`WebhookConfig`)
- `themis/backend/app/services/queue_engine.py`
- `themis/backend/app/api/routes/settings.py`
- Themis frontend: settings screen

---

### 8. Print history view

**Category:** Feature  
**Effort:** S (1 day)  
**Repos:** `themis`

**Problem**  
Completed jobs are excluded from the queue WebSocket broadcast and the Themis frontend has no history screen. G-code files are deleted on completion. After a 20-plate batch run, there is no record of what printed, when, on which printer, or how long it took. For tracking a multi-session cosplay build or gridfinity system over weeks, this is a meaningful gap.

**What to do**

*Data layer:*
- Ensure `jobs` rows are never deleted — only their status changes. (Verify this is already the case.)
- Add `completed_at: Optional[datetime]` and `print_duration_seconds: Optional[int]` to the `Job` model if not already present.
- Add to `_ALTERS` as needed.

*API:*
- Add `GET /api/v1/jobs/history` with query params: `?status=complete,failed,cancelled&project_id=&page=&page_size=`. Returns paginated job rows including printer name and project name via JOIN.

*Frontend:*
- Add a "History" screen (new route) showing a table of completed/failed/cancelled jobs: date, job name, printer, duration, project (linked), status.
- Basic filters: date range, project, printer, status.
- No need to store or re-display G-code — the record is sufficient.

**Acceptance criteria**
- History screen shows all completed, failed, and cancelled jobs with timestamps and printer assignment.
- Jobs are filterable by project, date range, and status.
- A job that failed shows its failure reason.
- History persists across Themis restarts (it's backed by the existing SQLite rows).

**Files likely touched**
- `themis/backend/app/models.py` (add `completed_at`, `print_duration_seconds` if missing)
- `themis/backend/app/database.py` (`_ALTERS`)
- `themis/backend/app/api/routes/jobs.py`
- Themis frontend: new History screen + nav link

---

### 9. Surface filament estimates + Spoolman inventory check

**Category:** Feature  
**Effort:** M (2 days)  
**Repos:** `themis`

**Problem**  
The slicer computes filament usage (grams) and estimated print time per plate. Spoolman knows remaining spool weights. Neither is surfaced anywhere in the Themis UI. The question a batch-printing user asks before starting a 30-bin overnight run — "do I have enough filament?" — cannot be answered in the app. The data already exists in the system; it just isn't connected or displayed.

**What to do**

*Slice output:*
- After a successful slice, parse the gcode or Laminus's slice result for filament usage (grams per tool) and estimated print time. Store these on the `GcodeFile` model: `filament_grams: Optional[float]`, `estimated_seconds: Optional[int]`.

*Project view:*
- On the project detail screen, after generation, show per-plate estimates and a project total: "~340g PLA / ~14h 20m total."

*Spoolman check (requires #4 to be done first):*
- When the user is about to generate a project (or views a generated project), query Spoolman for remaining weight on the loaded spools for each required filament profile.
- If total required grams > available grams, show a warning: "Project needs ~340g Generic PLA. Loaded spool has ~220g remaining."
- This is advisory — don't block generation.

**Acceptance criteria**
- After slicing, filament grams and estimated print time are stored and shown on the job/plate card.
- Project detail shows totals across all plates.
- If Spoolman is configured, a per-filament inventory check is run when viewing a project and a warning is shown when inventory is insufficient.
- Warning is non-blocking (user can dismiss and proceed).

**Files likely touched**
- `themis/backend/app/models.py` (`GcodeFile`)
- `themis/backend/app/services/slicer_service.py` (parse slice result)
- `themis/backend/app/api/routes/projects.py`
- `themis/backend/app/services/spoolman_service.py`
- Themis frontend: project detail, job cards

---

### 10. Cache Laminus profile catalog to disk

**Category:** Reliability  
**Effort:** S (half day)  
**Repos:** `laminus` (orca)

**Problem**  
On every Laminus startup, `warm_catalog_cache()` rebuilds the profile catalog from scratch by walking the `/config` directory tree, resolving OrcaSlicer inheritance chains, and building UUID5-keyed index structures. This takes up to 5 minutes on a large profile set and blocks all slicing during that window. The UUID5 scheme already makes catalog entries deterministic from their inputs — the catalog is cacheable to disk without any correctness risk.

**What to do**

In `laminus/app/main.py` (or wherever `warm_catalog_cache` lives):

1. On startup, compute a cache key: hash of the `/config` directory tree (e.g., sorted `(path, mtime, size)` tuples). A recursive `os.walk` is fast enough.
2. If a cache file at `/data/catalog_cache.json` exists and its stored key matches the current key, load the catalog from cache and skip the rebuild. Log "catalog loaded from cache."
3. If the key doesn't match (config changed) or no cache exists, do the full rebuild and write the result to `/data/catalog_cache.json` with the new key.
4. On cache load, validate that the JSON deserializes correctly before trusting it; fall back to rebuild if it's corrupted.

**Acceptance criteria**
- Laminus health endpoint reports `catalog_loaded: true` within 5 seconds of container start on a warm restart (config unchanged).
- If `/config` contents change between restarts, the catalog is rebuilt correctly on next start.
- Cache file corruption (truncated, invalid JSON) triggers a graceful fallback to full rebuild, not a crash.

**Files likely touched**
- `orca/app/main.py` (Laminus)

---

### 11. Unify Projects and Orders into one concept

**Category:** Refactor  
**Effort:** L (3–5 days)  
**Repos:** `themis`

**Problem**  
There are two overlapping grouping concepts in Themis:
- **Projects**: machine + process selection, STL files with per-part quantities and filament profiles, auto plate-packing, job generation. No progress tracking.
- **Orders**: a checklist/grouping with a progress bar driven by `jobs.order_id`. No automated generation.

Project-generated jobs don't set `order_id`, so they never appear in Orders. Orders have the progress bar; Projects don't. A user starting a gridfinity project uses Projects and gets no completion tracking. A user tracking a build manually uses Orders and gets no auto plate-packing. The two features exist for the same purpose without connecting.

This is a larger refactor and should follow story #2 (project_id on jobs), which partially addresses the gap with less disruption.

**What to do**

*Option A (recommended):* Make Orders the top-level concept; fold Projects into Orders as a generation mode.
- An Order can have a "generate from STLs" workflow (current Projects flow) or be composed manually.
- Jobs carry `order_id` regardless of how they were created.
- The Order detail view shows per-plate/per-job status (from story #2's progress tracking), a BOM, and the existing checklist.
- Deprecate the standalone Projects screens once the Order flow covers the same ground.

*Option B (lighter):* Keep both but wire them: when `generate_project` creates jobs, also create or link an Order and set `job.order_id`. The Order view then shows project progress. Projects and Orders remain separate concepts with a persistent link.

Option B is lower risk and can be done after story #2.

**Acceptance criteria**
- A gridfinity project generated via "Send to Themis" → generate has visible progress tracking (number of jobs complete) in the same view.
- The user does not need to understand two different "grouping" concepts to track a project.
- Existing data (Projects and Orders) migrates cleanly.

**Files likely touched**
- `themis/backend/app/models.py` (merge or link models)
- `themis/backend/app/database.py` (migration)
- `themis/backend/app/api/routes/projects.py`, `orders.py`
- Themis frontend: significant screen consolidation

---

### 12. Add Themis healthcheck to compose

**Category:** DevOps  
**Effort:** XS (~30 minutes)  
**Repos:** `omnibus` (Concordia), `themis`

**Problem**  
Themis has no `HEALTHCHECK` in its Dockerfile and no `healthcheck` in `docker-compose.yml`. Ordinus uses `depends_on: themis: condition: service_started` — meaning Ordinus may attempt to register or send data to Themis before Themis is ready to accept connections. This is inconsistent with the care taken for the Laminus→Themis dependency (which uses `service_healthy`).

**What to do**

In `omnibus/docker-compose.yml`, add under the `themis` service:
```yaml
healthcheck:
  test: ["CMD", "curl", "-f", "http://localhost:8000/api/health"]
  interval: 10s
  timeout: 5s
  retries: 5
  start_period: 15s
```
(Confirm that Themis exposes `/api/health` or equivalent; add the endpoint if not.)

Update Ordinus's `depends_on` to `condition: service_healthy`.

**Acceptance criteria**
- `docker compose ps` shows Themis as `healthy` once it's accepting requests.
- Ordinus does not start until Themis is healthy.
- `docker compose up` with a slow Themis start does not result in Ordinus connection errors on startup.

**Files likely touched**
- `omnibus/docker-compose.yml`
- `themis/backend/app/main.py` or router (confirm `/api/health` exists)

---

### 13. Fix compose bind-mount paths for Laminus

**Category:** Bug  
**Effort:** XS (~15 minutes)  
**Repos:** `omnibus` (Concordia)

**Problem**  
`docker-compose.local.yml` mounts `../laminus/config` and `../laminus/data` into the Laminus container. The Laminus source directory on disk is still named `orca/` (the rename was never applied to the filesystem). On a fresh clone, Docker auto-creates `../laminus/` as empty root-owned directories, and the container starts with an empty profile catalog. The Docker-Hub-pull story (no local Laminus source needed) also fails silently because the bind mount path doesn't exist.

**What to do**

Two parts, depending on whether the directory rename happens:

*If `orca/` is renamed to `laminus/`:*  
No code change needed — mounts will resolve correctly.

*If `orca/` stays as `orca/`:*  
Update `docker-compose.local.yml` to reference `../orca/config` and `../orca/data`, matching the actual directory name.

In `docker-compose.yml` (the production/pull version), if these are bind mounts rather than named volumes, convert them to named volumes so the pull story doesn't depend on a sibling directory at all.

**Acceptance criteria**
- `docker compose up` (pull mode) starts with a functional Laminus that can receive profile files via the API.
- `docker compose -f docker-compose.yml -f docker-compose.local.yml up` (local build mode) correctly mounts the local config and data directories.
- No empty root-owned directories auto-created by Docker on fresh clone.

**Files likely touched**
- `omnibus/docker-compose.yml`
- `omnibus/docker-compose.local.yml`

---

### 14. Remove dead auth plumbing in Ordinus ✅

**Category:** Cleanup  
**Effort:** S (half day)  
**Repos:** `ordinus`

**Problem**  
Authentication was removed from Ordinus for single-user use, but its remnants are scattered throughout the codebase: `accessToken` references in `BomGenerationPanel.tsx`, a `users` table in the schema kept "for backward compat," login account references in the README. These create confusion for anyone reading the code and slightly bloat every component they touch.

**What to do**

- Remove `accessToken` state, props, and usage from all frontend components. Verify the API calls they were passing to still work without auth headers.
- Remove or clearly comment-out the `users` table from the Drizzle schema. If it's kept for compat, add a comment explaining why.
- Remove login account documentation from the README.
- Do a project-wide search for `accessToken`, `token`, `auth`, `login` and remove or comment out anything that's vestigial.

**Acceptance criteria**
- `accessToken` does not appear as a prop or state variable in any React component.
- The app builds and all existing functionality works without auth-related props being threaded through.
- README does not reference login accounts that don't exist.

**Files likely touched**
- `ordinus/app/src/components/BomGenerationPanel.tsx` (and likely others)
- `ordinus/server/src/db/schema.ts`
- `ordinus/README.md`

---

### 15. Add themis-data backup documentation

**Category:** Docs  
**Effort:** XS (~1 hour)  
**Repos:** `omnibus` (Concordia)

**Problem**  
`themis-data` is a Docker named volume containing the SQLite database and all uploaded file library content. It is one `docker volume rm themis-data` away from losing the entire print history, project records, and file library. There is no documentation telling users how to back it up or restore it. For a tool that accumulates project history over weeks and months, this is a meaningful risk.

**What to do**

Add a "Backups" section to the main README (or `CLAUDE.md`) with:
- The command to snapshot the volume: `docker run --rm -v themis-data:/data -v $(pwd):/backup alpine tar czf /backup/themis-data-$(date +%Y%m%d).tar.gz /data`
- The command to restore: extract the archive and copy files back into a fresh volume.
- A recommendation to run the backup command before upgrading Themis.
- Optionally: a one-liner cron job example for automated daily backups.

**Acceptance criteria**
- A user following the README can back up and restore their data without prior Docker volume knowledge.
- The backup command is copy-pasteable and tested.

**Files likely touched**
- `omnibus/README.md` (create if doesn't exist) or `omnibus/CLAUDE.md`

---

### 16. Fix documentation drift

**Category:** Docs  
**Effort:** S (2–3 hours)  
**Repos:** `themis`, `ordinus`

**Problem**  
Several documentation files actively mislead:

- `themis/docs/agent/data-model.md` states the `projects` table was "removed" — Projects is an actively used feature with two dedicated frontend screens.
- `ordinus/README.md` describes a `packages/` monorepo layout; the actual layout is `app/`, `server/`, `shared/`, `generator/`.
- `ordinus/README.md` lists dev login accounts that no longer exist.

**What to do**

- Update `data-model.md` to reflect the current schema accurately. Remove the "projects removed" claim; document the actual `projects` and `project_items` tables.
- Update `ordinus/README.md` with the correct directory structure.
- Remove the login account section from the Ordinus README (or note that auth was removed and the app is single-user with no login required).
- Do a pass over all docs for any other `orca`/`omnibus` references that should be `laminus`/`concordia`.

**Acceptance criteria**
- `data-model.md` accurately reflects the current SQLAlchemy models.
- `ordinus/README.md` directory structure matches the actual repo layout.
- No documentation references features (login, auth) that have been removed.

**Files likely touched**
- `themis/docs/agent/data-model.md`
- `ordinus/README.md`

---

### 17. Profile onboarding UI in Themis settings

**Category:** UX  
**Effort:** M (2 days)  
**Repos:** `themis`

**Problem**  
Setting up OrcaSlicer profiles requires the user to know the JSON structure, understand the inheritance chain, and either place files manually in the `config/` directory or run `flatten_profiles.py` via `docker exec`. There is no in-app guidance. This is a significant onboarding wall for any user who didn't write the system.

**What to do**

Add a "Profiles" section to Themis settings:

1. **Profile browser**: List currently detected machine, process, and filament profiles from Laminus's catalog endpoint. Show which profiles are loaded and healthy.
2. **Import UI**: Allow uploading a profile JSON file directly from the browser (Laminus already has `POST /api/profiles/upload`). Validate and show the result.
3. **Flatten helper**: Surface the `flatten_profiles.py` command with a copy button, parameterized for the selected built-in profile. Show the list of available built-in profiles (from Laminus) so the user can pick without knowing the internal path.
4. **(Optional)** Show a "getting started" card when no user profiles are detected: "No profiles found. Here's how to add your first one."

**Acceptance criteria**
- A user can see all currently loaded profiles in the Themis UI without using the terminal.
- A user can upload a profile JSON file from the browser.
- The flatten command for any built-in profile is available as copy-pasteable text from the settings UI.
- When zero profiles are configured, a visible prompt explains how to add one.

**Files likely touched**
- `themis/backend/app/api/routes/settings.py` (or new `profiles.py` route)
- Laminus API (confirm profile list endpoint is exposed)
- Themis frontend: settings screen

---

### 18. Mobile-responsive UI

**Category:** UX  
**Effort:** L (3–5 days)  
**Repos:** `themis`

**Problem**  
The realistic operating posture for a farm manager is standing at the printer rack with a phone: check the next job, clear the plate, unblock a filament. The Themis UI is not documented as responsive and the React components are likely sized for desktop. This isn't a blocking issue on day one, but it becomes daily friction once the system is running regularly.

**What to do**

Audit the Themis React frontend for responsive layout issues. Priority views for mobile:
1. **Queue screen**: shows the active and queued jobs, printer status, and the plate-clear button.
2. **Printer fleet screen**: shows each printer's current job, status, and the "resume" / "clear plate" actions.
3. **Job detail**: filament match status, override warnings, slice status.

Lower priority for mobile: project builder, file library, settings.

Use CSS media queries or Tailwind responsive utilities (check which CSS framework is in use). The goal is "usable on a phone," not "pixel-perfect mobile app."

**Acceptance criteria**
- Queue screen and printer fleet screen are usable on a 390px-wide viewport without horizontal scroll.
- Plate-clear and filament-unblock actions are reachable with one thumb.
- No critical information is obscured by overflow at mobile widths.

**Files likely touched**
- Themis frontend: queue, printer fleet, and job detail screen components
- Global CSS / layout components

---

### 19. Manual job outcome marking + per-item failure tracking ✅

**Category:** Feature
**Effort:** M (2 days)
**Repos:** `themis`

**Problem**
Once a print job completes, there was no way for the user to record whether it succeeded or which parts failed. Over multiple plates of a multi-part project, failures accumulated silently — the user had no reliable way to know how many of each part had been successfully printed vs. needed to be reprinted.

**What was built**

- `JobItemFailure` table — per-job, per-item audit records with `quantity_failed` and `quantity_on_plate` snapshot (enables re-marking without data corruption).
- `Job.outcome` — null (unreviewed) or "reviewed". `Job.project_item_quantities` — JSON dict `{item_id: qty_on_this_plate}` stored at job creation time, distributing group quantities evenly across plates (remainder to first N plates).
- `ProjectItem.quantity_failed` and `ProjectItem.quantity_completed` — denormalized counters updated atomically when outcome is marked. Clamp-safe reversal on re-mark.
- `PUT /api/v1/jobs/{id}/outcome` — accepts per-item failure list; reverses previous increments, applies new ones, returns updated job.
- **History screen**: "Outcome" column with "✓ Reviewed" badge or "Mark" button for unreviewed complete project jobs.
- **OutcomeModal**: fetches project item names, shows per-plate quantities, per-item failure count inputs, "Mark All Good" and "Mark All Failed" convenience buttons.
- **Project builder**: shows `N/total ok · M failed` per part when counts are non-zero.

**Design decisions**
- No auto-requeue — failures are tracked so the user can consolidate plates and requeue manually.
- Per-plate quantities estimated by even distribution (slicer doesn't expose a manifest); user can adjust in the modal.
- `quantity_failed` on `ProjectItem` is denormalized for easy per-item success rate reporting.
- Outcome is re-markable — the endpoint reverses prior increments using the `quantity_on_plate` snapshot before applying new values.

**Files touched**
- `themis/backend/app/models.py`
- `themis/backend/app/database.py`
- `themis/backend/app/api/routes/jobs.py`
- `themis/backend/app/api/routes/projects.py`
- `themis/frontend/src/components/OutcomeModal.tsx` (new)
- `themis/frontend/src/screens/HistoryScreen.tsx`
- `themis/frontend/src/screens/ProjectBuilderScreen.tsx`
- `themis/frontend/src/api/projects.ts`
- `themis/frontend/src/api/queue.ts`
