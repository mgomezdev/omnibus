# Concordia — Contributor Reference

Concordia (this repo, `omnibus` on disk) is the orchestration layer for the full 3D printing stack. It owns `docker-compose.yml` and the cross-service E2E test suite. It contains no application code.

---

## What's in This Repo

```
omnibus/
├── docker-compose.yml          # Production: pull images from Docker Hub
├── docker-compose.local.yml    # Dev override: build from local sibling repos
├── .env                        # Active env overrides (not committed)
├── .env.example                # Reference env vars
├── CLAUDE.md                   # Claude Code project instructions
├── CONTRIBUTING.md             # This file
├── docs/
│   ├── slicing-flow.md         # Full slicing pipeline (Mermaid diagrams)
│   └── superpowers/specs/      # Feature design documents
└── tests/
    ├── conftest.py
    └── e2e/
        ├── test_centauri_slice.py
        └── test_ordinus_themis_integration.py
```

---

## The Stack

Three services, all managed by Docker Compose:

| Service | Repo | Role | Port |
|---|---|---|---|
| `laminus` | `../orca` | OrcaSlicer sidecar — profile catalog, slicing, 3MF packing | Internal :5000 only |
| `themis` | `../themis` | Print queue, job lifecycle, printer management, REST API + React frontend | `HOST_PORT` (default 8001) |
| `ordinus` | `../gridfinity-customizer` | Gridfinity bin layout tool, BOM generation, Themis integration | `ORDINUS_PORT` (default 3002) |

### Dependency chain

```
laminus (healthcheck: GET /api/health)
  └─ themis (depends_on: laminus healthy) (healthcheck: GET /api/v1/health)
       └─ ordinus (depends_on: themis healthy)
```

Laminus must pass its healthcheck before Themis starts. Themis must pass its healthcheck before Ordinus starts. This ordering ensures the profile catalog is warm and Themis is ready before either downstream service tries to reach them.

---

## Running the Stack

### Docker Hub images (production / quick start)

```bash
cp .env.example .env
# Edit .env if you need different ports
docker compose up
```

Themis: `http://localhost:8001`
Ordinus: `http://localhost:3002`

### Local source builds (development)

The `docker-compose.local.yml` file overrides the image pulls with local builds from sibling repos. The sibling repos must exist at the same directory level:

```
parent-dir/
├── omnibus/          ← this repo (Concordia)
├── themis/
├── orca/             ← Laminus source (note: dir is named "orca")
└── gridfinity-customizer/  ← Ordinus source
```

```bash
docker compose -f docker-compose.yml -f docker-compose.local.yml up --build
```

To rebuild a single service after code changes:
```bash
docker compose -f docker-compose.yml -f docker-compose.local.yml up --build themis
```

---

## Environment Variables

Defined in `.env` (copy from `.env.example`):

| Variable | Default | Purpose |
|---|---|---|
| `HOST_PORT` | `8001` | Host port for Themis |
| `ORDINUS_PORT` | `3002` | Host port for Ordinus |
| `ORCA_VERSION` | `2.4.1` | OrcaSlicer AppImage version for Laminus |

Laminus passes `ORCA_VERSION` to its entrypoint script, which downloads the matching AppImage if not already in the `laminus-slicer` named volume.

---

## Named Volumes

| Volume | Mounted in | Purpose |
|---|---|---|
| `themis-data` | `/data` in themis | Themis DB, library files, gcode cache |
| `laminus-config` | `/config` in laminus | User OrcaSlicer profiles |
| `laminus-data` | `/data` in laminus | Catalog cache, job temp files |
| `laminus-slicer` | `/opt/orcaslicer` in laminus | Extracted OrcaSlicer AppImage (downloaded once) |
| `ordinus-config` | `/config` in ordinus | Ordinus SQLite DB |
| `ordinus-data` | `/data` in ordinus | Generated STLs, images, thumbnails |

---

## E2E Tests

E2E tests target a running stack. Run them with:

```bash
# Stack must be running first
pytest tests/e2e/test_centauri_slice.py --integration
pytest tests/e2e/test_ordinus_themis_integration.py --integration

# Override service URLs (defaults match .env):
THEMIS_URL=http://localhost:8001 ORDINUS_URL=http://localhost:3002 pytest tests/e2e/ --integration
```

### test_centauri_slice.py

Tests the Themis slicing pipeline end-to-end:
1. Upload a minimal binary STL to Themis files
2. Find the placeholder Elegoo Centauri Carbon printer
3. Confirm profile catalog is populated
4. Create a job with Elegoo process + filament profiles
5. Call `POST /api/v1/jobs/{id}/verify-slice`
6. Assert `ok=true`

Profiles used: `Elegoo Centauri Carbon 0.4 nozzle`, `0.16mm Optimal @Elegoo CC 0.4 nozzle`, `Elegoo PLA @ECC`

### test_ordinus_themis_integration.py

Tests the Ordinus → Themis bidirectional integration:
1. **Send layout** — sends a gridfinity layout to Themis; asserts `source_app=ordinus` and `source_layout_id` on the created project; asserts `themisProjectId` written back to Ordinus BOM generation row
2. **Resend dedup** — resending the same layout creates a new project but reuses the same Themis file IDs (per-layout content-hash dedup)
3. **Cross-layout dedup** — two layouts sharing the same bin model get the same Themis file IDs
4. **Full pipeline** — Ordinus BOM → send to Themis → verify-slice on Elegoo Centauri Carbon → `ok=true`

---

## Adding a New Service

1. Add the service to `docker-compose.yml` with:
   - A `healthcheck` (other services that depend on it need this)
   - `depends_on` referencing upstream services with `condition: service_healthy`
   - Named volumes for persistent data

2. Add a local build override in `docker-compose.local.yml`:
```yaml
services:
  myservice:
    build: ../my-repo
    image: ""
```

3. Add E2E tests in `tests/e2e/test_myservice_integration.py`.

---

## Docs

`docs/slicing-flow.md` — authoritative Mermaid diagram sequence for the full slicing pipeline (Laminus startup, profile discovery, job lifecycle, error paths). Update this when changing the slicing architecture.

`docs/superpowers/specs/` — feature design documents. Create a new file here when designing a non-trivial feature before implementing it.
