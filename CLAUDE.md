# CLAUDE.md

This file provides guidance to Claude Code when working with code in this repository.

## Commands

```bash
# Start the full stack (Laminus sidecar + Themis backend)
docker compose up

# Run the E2E integration test (stack must be running)
pytest tests/e2e/test_centauri_slice.py --integration

# Override the Themis URL (default matches HOST_PORT in .env)
THEMIS_URL=http://localhost:8001 pytest tests/e2e/ --integration
```

## Architecture

Concordia is the **orchestration repo** — it owns `docker-compose.yml` and cross-service E2E tests. The actual application code lives in two sibling repos:

| Repo | Role | Default internal port |
|---|---|---|
| `../themis` | Print queue, job lifecycle, frontend, API | 8000 (host: `HOST_PORT` from `.env`) |
| `../laminus` | OrcaSlicer sidecar — profile catalog, slicing, packing | 5000 (internal only) |

Both services build from their own repos (`build: ../themis`, `build: ../laminus`). Themis talks to Laminus at `http://laminus:5000` via `LAMINUS_SIDECAR_URL`. Laminus must pass its healthcheck before Themis starts (`depends_on: condition: service_healthy`).

See `docs/slicing-flow.md` for the full slicing pipeline.

## Design Specs
- `docs/superpowers/specs/` — feature design documents
