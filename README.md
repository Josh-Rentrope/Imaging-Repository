# Bone Viewer

**Working name.** This is a Dental/Orthodontic Imaging SaaS demo, not a full product.

## Running it

```sh
# Backend  →  http://127.0.0.1:8787  (docs at /docs)
cd apps/api && uv sync && uv run uvicorn app.main:app --reload --port 8787

# Frontend →  http://localhost:5273  (proxies /api to the backend)
cd apps/web && npm install && npm run dev

# Tests
cd apps/api && uv run python -m pytest -q
cd apps/web && npm run typecheck
```

> On Windows, Vite binds to `[::1]` — use `http://localhost:5273`, not `127.0.0.1`.

## Layout

```
contracts/          CaptureBundle + results JSON schemas — the shell⇄core seam
apps/api/           FastAPI. InferenceBackend protocol + implementations
  app/pipeline/     interfaces.py (Stage/Op), fixture.py, ondevice.py, server.py (loader)
  app/dicom/        minimal.py — narrow, dependency-free DICOM header reader
  recordings/       optional per-stage responses, replayed in place of generated output
  tests/            smoke tests asserting the safety-critical behaviours
apps/web/           Vite + React + TS. Transition layer in src/transitions/
  src/transitions/  named page-transition presets (fade, dissolve, wipes, rise)
  src/features/     dicom dropzone + upload, arch viewport (react-three-fiber)
Notes/              planning docs — GITIGNORED, read from disk
```

