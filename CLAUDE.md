# CLAUDE.md

Guidance for working in this repo. **The README and `orchestrator/pyproject.toml` description are stale** — they still describe a LangGraph + Docker + Cline architecture that was fully removed. Trust this file and the code, not those.

## What this is

Automatron is a **GitHub-native project planning & orchestration tool**. It reads a GitHub repo (README + docs), generates Epics → Stories → Tasks via LLM, creates GitHub Milestones + Issues, and delegates implementation to the GitHub Copilot coding agent or Aider. It also has CI/CD and Docker-deployment AI subsystems.

- **Canonical repo:** `https://github.com/Quitcode-Dev/project-automatron-vmark` (`origin`). Ignore any other `*/Project-Automatron` repos.
- **Production:** https://automatron.quitcode.com — deploys from `origin/main` via GitHub Actions.
- LangGraph is **not** imported anywhere (verified: 0 imports, no `graph/` dir). `langgraph` remains listed as a dep in `pyproject.toml` but is unused.

## Layout

- `orchestrator/` — Python 3.12 backend. Package lives at `orchestrator/orchestrator/`.
  - `main.py` — builds `app`: a Socket.IO ASGI app wrapping FastAPI (`combined_app = socketio.ASGIApp(sio, other_asgi_app=fastapi_app)`). Uvicorn entry point is `orchestrator.main:app`, **port 8000**.
  - `orchestrator.py` — `GitHubOrchestrator` (analyze/plan/apply/review). `github/issues.py` — `GitHubClient` REST wrapper.
  - `api/` — `routes.py`, `webhook_github.py` (`POST /api/webhooks/github`, auto-detects Copilot PRs → AI review), `socket_server.py`/`websocket.py` (WS events: `issues:updated`, `pr:review_ready`, `architect:chunk`, `human_required`).
  - `llm/` (provider/catalog/prompts), `cicd_specialist/`, `github_actions/`, `docker_deployment_ai/`, `deployment/`, `scaffolding/`, `validation/`, `plan_parser/`, `preview.py`, `aider_agent.py`.
  - `tests/` — 21 pytest files.
- `web-ui/` — Next.js **15** (App Router), React 19, Zustand, Socket.IO client, Tailwind + shadcn/ui, port 3000. Uses **npm** (`package-lock.json`) — the Makefile's `pnpm` is wrong. Tests via vitest.

## Commands

Backend (from `orchestrator/`, venv at project-root `.venv/`):
- Run: `make dev` → `uvicorn orchestrator.main:app --reload --port 8000`
- Test: `python -m pytest tests/ -v` (asyncio_mode=auto)
- Lint/format: `ruff check orchestrator/` (line-length 100), `mypy` (strict)

Web UI (from `web-ui/`):
- `npm run dev` · `npm run build` · `npm test` (vitest) · `npm run lint`

## Config & gotchas

- Env in `.env` at project root: `GITHUB_TOKEN`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GITHUB_WEBHOOK_SECRET`. `pydantic-settings` reads `.env` from **CWD** — symlink `.env` if running from a subdirectory.
- DB: SQLite, `config.py` default `./data/automatron.db` (relative to CWD).
- **FastAPI `BackgroundTasks` silently swallows exceptions** — when "nothing happens," check `activity_logs` in SQLite.
- `call_llm()` takes a `[SystemMessage(...), HumanMessage(...)]` list, not a `system=` kwarg (verify against current `llm/` code before relying on this — memory note).
- Commit/push only when asked; branch first if on `main`.
