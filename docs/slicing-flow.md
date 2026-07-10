# Slicing Flow: Themis + Laminus Sidecar

This document describes how a print job moves from the Themis queue through the Laminus sidecar to a sliced G-code file, including profile resolution and the check-overrides inspection workflow.

---

## Architecture Overview

```mermaid
graph LR
    subgraph Browser
        UI[Themis Frontend]
    end

    subgraph Docker["Docker Compose"]
        TB[Themis Backend\nFastAPI · port 8000]
        LS[Laminus Sidecar\nFastAPI · port 5000]
        CLI[OrcaSlicer CLI\nspawned by sidecar]
    end

    subgraph Storage["Filesystem Volumes"]
        SP[System Profiles\n/opt/orcaslicer/resources/profiles]
        UP[User Profiles\n/config/user]
        JD[Job Data\n/tmp/jobs]
    end

    UI -->|HTTP| TB
    TB -->|HTTP via LaminusSidecarClient| LS
    LS -->|subprocess| CLI
    LS -->|scans on startup| SP
    LS -->|scans on startup| UP
    CLI -->|writes gcode| JD
    LS -->|serves download| TB
    TB -->|stores on disk| JD
```

**Themis Backend** owns the print queue, job lifecycle, and printer communication.  
**Laminus Sidecar** owns everything slicing-related: the profile catalog, 3MF assembly, and OrcaSlicer invocation.  
**Themis never reads profile files directly.** It holds only the names users see (e.g. `"Elegoo Centauri Carbon"`) and the stable UUIDs it discovers from the sidecar catalog.

---

## 1. Laminus Startup — Building the Profile Catalog

On container start, the sidecar scans both the system profiles directory and the user config directory to build an in-memory `ProfileCatalog`. Each profile's inheritance chain is resolved once and cached under a `_resolved` key.

```mermaid
sequenceDiagram
    participant Boot as Sidecar Startup
    participant FS as Profile Files
    participant Cat as ProfileCatalog

    Boot->>FS: scan /opt/orcaslicer/resources/profiles
    Boot->>FS: scan /config/user
    FS-->>Boot: machine / process / filament JSON files

    Boot->>Cat: build catalog
    note over Cat: For each profile, walk the<br/>"inherits" chain and merge<br/>parent → child (child wins)

    Cat->>Cat: assign stable UUIDs<br/>machine → UUID5(mfr|model|nozzle)<br/>process/filament → UUID5(source\0rel_path)

    note over Cat: Public GET /api/profiles<br/>strips _resolved — callers<br/>see names + UUIDs only.<br/>Internal catalog keeps full<br/>_resolved data for slice calls.
```

The sidecar returns a **503** on any slice or profile endpoint until this scan completes.

---

## 2. Profile Discovery — Themis Fetches the Catalog

When a user opens the profile selector in the UI, or when the queue engine needs UUIDs for slicing, Themis fetches the catalog from the sidecar. Results are cached for **300 seconds** to avoid repeated round-trips.

```mermaid
sequenceDiagram
    participant UI as Frontend
    participant TB as Themis Backend
    participant SC as LaminusSidecarClient
    participant LS as Laminus Sidecar

    UI->>TB: GET /api/v1/printers/{id}/profiles
    TB->>SC: get_catalog()
    SC->>LS: GET /api/profiles
    LS-->>SC: { machine: [...], process: [...], filament: [...] }
    SC-->>TB: catalog dict (names + UUIDs + compatible_printers)

    note over TB: filter process[] and filament[] where<br/>machine_name ∈ compatible_printers

    TB-->>UI: { print_profiles: ["0.16mm Optimal", ...],<br/>             filament_profiles: ["Elegoo PLA Basic", ...] }
```

The printer's `current_orca_printer_profile` (e.g. `"Elegoo Centauri Carbon 0.4 nozzle"`) is the key used to filter compatible process and filament profiles (field named for the OrcaSlicer profile system).

---

## 3. Slicing — The Main Flow

This is the complete path from a queued job to a G-code file on disk.

### 3a. Queue Engine — Job Claim and SliceRequest Assembly

```mermaid
sequenceDiagram
    participant QE as QueueEngine
    participant DB as Database
    participant PM as PrinterManager

    QE->>DB: select queued/blocked jobs for this printer
    DB-->>QE: job + JobPrinterConfig

    QE->>QE: check filament loaded on printer
    alt filament mismatch
        QE->>DB: job.status = "blocked"
    else
        QE->>DB: job.status = "slicing"
        QE->>DB: commit

        note over QE: Build SliceRequest:<br/>• source_3mf (uploaded file path)<br/>• machine_preset (printer's orca profile name)<br/>• process_preset (print profile name)<br/>• filament_presets (filament profile name(s))<br/>• plate_number<br/>• extra_config { curr_bed_type, job_overrides }<br/>• export_args (printer-specific, e.g. --export-3mf)

        QE->>QE: asyncio.get_running_loop().run_in_executor(<br/>  ThreadPoolExecutor, slicer.slice, req<br/>)
    end
```

### 3b. SlicerService — UUID Resolution and Sidecar Dispatch

```mermaid
sequenceDiagram
    participant QE as QueueEngine
    participant SS as SlicerService
    participant SC as LaminusSidecarClient
    participant LS as Laminus Sidecar

    QE->>SS: slice(SliceRequest)

    SS->>SS: check LAMINUS_SIDECAR_URL configured
    note over SS: raises SliceError if not set

    SS->>SS: check prepare_hook is None
    note over SS: raises SliceError if set<br/>(multi-extruder remapping<br/>not yet supported in sidecar mode)


    alt catalog cache stale (>300s)
        SS->>SC: get_catalog()
        SC->>LS: GET /api/profiles
        LS-->>SC: catalog dict
        SC-->>SS: catalog dict
        SS->>SS: cache catalog + timestamp
    end

    SS->>SS: _resolve_uuids(req)
    note over SS: Build name→UUID maps from cache.<br/>Look up machine_preset, process_preset,<br/>each filament_preset by name.

    alt any name not found in catalog
        SS-->>QE: raise SliceError("Profile not found in Laminus sidecar catalog — machine=... process=... filaments=...")
    end

    SS->>SC: slice_start(<br/>  source_file,<br/>  machine_uuid, process_uuid, filament_uuids,<br/>  plate, export_3mf,<br/>  extra_config<br/>)
    SC->>LS: POST /api/slice/start (multipart)
    LS-->>SC: { "job_id": "abc-123" }
    SC-->>SS: "abc-123"

    loop poll every 2 seconds
        SS->>SC: poll_status("abc-123")
        SC->>LS: GET /api/slice/status/abc-123
        LS-->>SC: { "status": "slicing" | "completed" | "failed" }
    end

    alt status = "failed"
        SC-->>SS: raise SidecarError
        SS-->>QE: raise SliceError
    end

    SS->>SC: download("abc-123", dest_path)
    SC->>LS: GET /api/slice/download/abc-123
    LS-->>SC: gcode bytes
    SC->>SC: write to dest_path
    SC-->>SS: Path(dest_path)

    SS->>SS: _inject_thumbnail(gcode, source_3mf, plate)
    note over SS: Extracts Metadata/plate_N.png from<br/>the original 3MF. Prepends it as<br/>"; thumbnail begin ..." base64 comments<br/>so Elegoo/Snapmaker displays a preview.

    SS-->>QE: gcode_path
```

### 3c. Laminus Sidecar — POST /api/slice/start Internals

```mermaid
sequenceDiagram
    participant SC as LaminusSidecarClient
    participant LS as Laminus Sidecar
    participant Cat as ProfileCatalog
    participant CLI as OrcaSlicer CLI

    SC->>LS: POST /api/slice/start\nmultipart: file + UUIDs + plate + extra_config

    LS->>Cat: get_by_uuid(machine_uuid)
    LS->>Cat: get_by_uuid(process_uuid)
    LS->>Cat: get_by_uuid(filament_uuid) ×N
    Cat-->>LS: profile entries (with _resolved data)

    LS->>LS: validate compatible_printers

    alt STL uploaded
        LS->>LS: stl_to_3mf(raw_path, base_3mf)
    end

    LS->>LS: build_project_settings(\n  machine._resolved,\n  process._resolved,\n  [filament._resolved, ...]\n)
    note over LS: Merges machine + process + filament<br/>settings into a single flat dict.<br/>Precedence: filament > process > machine.

    alt extra_config provided
        LS->>LS: project_cfg.update(json.loads(extra_config))
        note over LS: Applies runtime overrides on top:<br/>curr_bed_type, layer_height, etc.
    end

    LS->>LS: embed_project_settings(\n  base_3mf,\n  project_cfg,\n  prepared.3mf\n)
    note over LS: Writes Metadata/project_settings.config<br/>into the 3MF ZIP.

    LS->>LS: jobs[job_id] = { status: "pending", ... }
    LS-->>SC: { "job_id": "abc-123" }

    LS->>CLI: xvfb-run -a orcaslicer\n  --slice {plate}\n  --outputdir {dir}\n  --arrange 1\n  [--export-3mf {name}]\n  prepared.3mf

    alt slice fails AND geometry_only_retry=true
        LS->>LS: strip Metadata/model_settings.config\nfrom 3MF → write geo.3mf
        LS->>CLI: retry with geo.3mf
    end

    CLI-->>LS: plate_1.gcode [+ .gcode.3mf]
    LS->>LS: jobs[job_id].status = "completed"\njobs[job_id].sliced_file = "plate_1.gcode"
```

### 3d. Queue Engine — After Slicing

```mermaid
sequenceDiagram
    participant QE as QueueEngine
    participant DB as Database
    participant PM as PrinterManager
    participant PR as Printer

    QE->>DB: INSERT GcodeFile(job_id, printer_id, path=gcode_path)

    alt printer not ready to receive
        QE->>DB: job.status = "sliced"
        note over QE: G-code waits on disk.<br/>Next queue cycle will send<br/>when printer becomes ready.
    else printer ready
        QE->>DB: job.status = "uploading"
        QE->>PM: get_client(printer_id)
        PM-->>QE: AbstractPrinterClient
        QE->>PR: upload_and_print(gcode_path)
        PR-->>QE: print started
        QE->>DB: job.status = "printing"
    end
```

---

## 4. Check-Overrides — Inspecting a 3MF Against Canonical Settings

When the user uploads a 3MF with baked-in slicer settings, Themis can diff those settings against the canonical profile to surface any non-default values.

```mermaid
sequenceDiagram
    participant UI as Frontend
    participant TB as Themis Backend
    participant SC as LaminusSidecarClient
    participant LS as Laminus Sidecar

    UI->>TB: POST /api/v1/jobs/{id}/check-overrides\n(multipart: machine, print_profile, filament_profile, file)

    TB->>SC: get_catalog()
    SC->>LS: GET /api/profiles
    LS-->>SC: catalog dict
    SC-->>TB: catalog dict

    TB->>TB: build name→UUID maps
    TB->>TB: machine_uuid = machine_map[machine_name]
    TB->>TB: process_uuid = process_map[print_profile]
    TB->>TB: filament_uuid = filament_map[filament_profile]

    alt filament not found by name
        TB->>TB: pick first compatible filament\n(curated diff, filament content irrelevant)
    end

    TB->>SC: get_merged_config(machine_uuid, process_uuid, [filament_uuid])
    SC->>LS: POST /api/profiles/merged-config\n{ machine_uuid, process_uuid, filament_uuids }
    LS->>LS: build_project_settings(_resolved data)
    LS-->>SC: canonical_config dict
    SC-->>TB: canonical_config dict

    TB->>TB: inspect_overrides(\n  uploaded_3mf_path,\n  canonical_config,\n  filament_slots\n)
    note over TB: Reads Metadata/project_settings.config<br/>from the uploaded 3MF.<br/>Computes diff: values in the 3MF that<br/>differ from canonical_config.

    TB-->>UI: { overrides: { layer_height: "0.12", ... },\n             has_embedded_settings: true }
```

---

## 5. Data Flow Summary

```mermaid
flowchart TD
    A[Job enters queue\nwith profile names] --> B{Filament\nloaded?}
    B -- No --> C[job.status = blocked]
    B -- Yes --> D[job.status = slicing]

    D --> E[SlicerService.slice]

    E --> F{LAMINUS_SIDECAR_URL\nconfigured?}
    F -- No --> FAIL1[SliceError:\nURL not configured]

    F -- Yes --> G{prepare_hook\nset?}
    G -- Yes --> FAIL2[SliceError:\nprepare_hook not\nsupported in sidecar mode]

    G -- No --> H[_resolve_uuids\nfetch catalog if stale\nmap names → UUIDs]

    H --> I{All UUIDs\nfound?}
    I -- No --> FAIL3[SliceError:\nprofile not found\nin sidecar catalog]

    I -- Yes --> J[LaminusSidecarClient.slice_start\nPOST /api/slice/start\nmachine + process + filament UUIDs\nplate + extra_config]

    J --> K[Sidecar: resolve _resolved data\nbuild_project_settings\nmerge extra_config\nembed into 3MF\nlaunch OrcaSlicer CLI]

    K --> L{Slice\nsucceeded?}
    L -- No, retry --> M[Sidecar: strip model_settings\ngeometry-only retry]
    M --> L
    L -- Failed --> FAIL4[SidecarError →\nSliceError]
    L -- Yes --> N[poll_status until completed\ndownload gcode\ninject thumbnail]

    N --> O[job.status = sliced / uploading\ngcode stored on disk]
```

---

## 6. Key Invariants

| Invariant | Where enforced |
|---|---|
| Themis never reads profile JSON files | `slicer_service.py` — no file imports |
| All profile data comes from `GET /api/profiles` | `LaminusSidecarClient.get_catalog()` |
| Profile catalog cached 300 s | `SlicerService._CATALOG_TTL = 300.0` |
| `extra_config` (bed type, job overrides) applied **after** profile resolution | Sidecar `start_slice` handler |
| Inheritance flattened once at catalog build time | `ProfileCatalog` on sidecar startup |
| UUIDs are stable across restarts | UUID5 from deterministic inputs |
| Sidecar unavailable → hard failure, no local fallback | `SlicerService.slice()` line 1 |

---

## 7. Error Paths at a Glance

| Condition | Error | Source |
|---|---|---|
| `LAMINUS_SIDECAR_URL` not set | `SliceError: LAMINUS_SIDECAR_URL is not configured` | `SlicerService.slice` |
| `prepare_hook` is set (multi-extruder job) | `SliceError: prepare_hook not supported` | `SlicerService.slice` |
| Catalog fetch fails (sidecar down) | `SliceError: Profile not found…` | `SlicerService._resolve_uuids` |
| Profile name not in catalog | `SliceError: Profile not found…` | `SlicerService._resolve_uuids` |
| OrcaSlicer CLI returns non-zero (both attempts) | `SidecarError` → `SliceError` | Sidecar `run_orcaslicer_task` |
| Sidecar poll timeout (>620 s) | `SidecarError: sidecar poll timed out` | `LaminusSidecarClient.poll_status` |
| `extra_config` is not a JSON object | HTTP 422 | Sidecar `start_slice` |
