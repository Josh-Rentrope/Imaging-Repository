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

## The private core

The reconstruction lives in a separate private distribution, not here.
`app/pipeline/server.py` and `app/pipeline/solvers.py` are the extension points:
this side defines the contracts and reports what is installed, and the private
package publishes entry points in `bone_viewer.backends` and
`bone_viewer.solvers`.

Nothing is registered in a checkout of this repo, which is the normal case. Four
states that used to look identical are now reported separately:

| what you see | what it means |
|---|---|
| `not installed in this deployment` | no private core here |
| `installed but failed to load: …` | present and broken |
| registered with `available: false` | package installed, binary it drives missing |
| `server_backend_error` | the same for the backend entry point |

The startup banner reports both on every boot.

A named backend or solver that cannot run is refused, never substituted. Auto
mode does fall back to the fixture, which is what lets a deployment without the
private core work; a caller who named one gets that one or an error.

### Building with it

```sh
cd apps/api
uv run python tools/make_stub_core.py            # throwaway stub, to test the path
# or, for the real thing: tools/build_wheel.py in the private core repo

docker build --build-arg WITH_PRIVATE_CORE=1 -t bone-viewer-api apps/api
```

The wheel goes in `apps/api/vendor/`, which `.gitignore` refuses — and a test
asserts that rule holds.

`WITH_PRIVATE_CORE=1` installs Python glue only. A `colmap` binary, weights and
scratch disk are provisioned separately, and the banner reports what resolved.

The Dockerfile installs this project into the venv, so the private package can
import the public contracts instead of relying on uvicorn adding the working
directory to `sys.path`.

## Layout

```
contracts/          CaptureBundle + results JSON schemas — the shell⇄core interface
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

