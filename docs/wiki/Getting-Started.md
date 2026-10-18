# Getting Started

## Prerequisites

- Docker with Compose v2 for the recommended container deployment
- Python 3.11 and Node.js 22 for local development, matching CI

## Run with Docker

```bash
git clone https://github.com/laanito/OpenTerminalUI.git
cd OpenTerminalUI
cp .env.example .env
docker compose up --build
```

Use the repo-root `.env` as the canonical local config file. `backend/.env` is still read for backward compatibility, but root `.env` wins if both exist.

App/API are served from backend container:

- UI/API: `http://127.0.0.1:8000`

## Run Locally (without Docker)

### Backend

On macOS/Linux, run `make setup` and copy `.env.example` to the root `.env`.
Its database hostname `postgres` is for Compose; a host process needs a reachable
PostgreSQL URL or a writable local SQLite URL such as
`DATABASE_URL=sqlite+aiosqlite:///./openterminal.db`. Then run:

```bash
PYTHONPATH=. backend/.venv/bin/python -m uvicorn backend.main:app --reload --port 8000
```

On Windows, create and activate a Python 3.11 virtual environment, install
`backend/requirements.txt`, and run `python -m uvicorn backend.main:app --reload
--port 8000` from the repository root.

### Frontend

```bash
npm ci --prefix frontend
npm run dev --prefix frontend
```

Frontend dev server:

- `http://127.0.0.1:5173`

## CI-equivalent Validation

```bash
python -m compileall backend
PYTHONPATH=. python scripts/check_no_production_mocks.py
PYTHONPATH=. python scripts/check_surface_inventory.py
PYTHONPATH=. python scripts/generate_api_reference.py --check
PYTHONPATH=. pytest backend/tests -q --cov=backend --cov-fail-under=45
npm ci --prefix frontend
npm run build --prefix frontend
npm run test --prefix frontend
```

`make gate` runs local integrity/API guards, backend coverage tests, frontend
build and Vitest. Run browser smoke separately with
`npm run test:e2e:smoke --prefix frontend`; use an installed Chrome through
`PLAYWRIGHT_EXECUTABLE_PATH` or install Playwright Chromium if needed. See the
[browser guide](../../frontend/tests/e2e/README.md) for setup and legacy tests.
CI runs deterministic smoke on every PR, plus disposable PostgreSQL and
fresh-install jobs. The clean-install teardown script is CI-only, not a host
deployment check.

## Start researching

Follow [Research Workflows](Research-Workflows.md) for cross-market context,
SEC filing checks, dated Notes, Second Brain and Brain Log. SEC needs
`SEC_USER_AGENT`, FRED panels need `FRED_API_KEY`, and AI needs a running model.
Configure these on the host rather than in browser automation-key settings.
The API explorer is `/docs`; its complete schema is `/openapi.json`.
