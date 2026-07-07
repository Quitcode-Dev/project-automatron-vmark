# Automatron Orchestrator

GitHub-native orchestration service for Project Automatron — a FastAPI + Socket.IO
backend that plans projects with an LLM, drives the GitHub REST API (repos,
milestones, issues, PRs), delegates coding to the GitHub Copilot agent or its
built-in Agent SDK builder,
and runs AI-assisted review, CI/CD, and deployment.

The importable package is `orchestrator` (i.e. `orchestrator/orchestrator/`).

## Development

Create and activate a virtual environment, then install the package in editable
mode with dev dependencies:

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

## Running

The ASGI entry point `orchestrator.main:app` is a Socket.IO app wrapping FastAPI:

```bash
uvicorn orchestrator.main:app --reload --host 0.0.0.0 --port 8000
```

Configuration is read from a `.env` file in the current working directory
(`pydantic-settings`); see `../.env.example`. Health check: `GET /health`.

## Testing & linting

```bash
python -m pytest tests/ -v     # asyncio_mode=auto
ruff check orchestrator/       # line-length 100
mypy orchestrator/             # strict
```
