# Project Automatron

> GitHub-native project planning & orchestration. Automatron reads a product idea (or an existing repo), plans it into Epics → Stories → Tasks with an LLM, provisions a GitHub repository with Milestones and Issues, and delegates implementation to the **GitHub Copilot coding agent** or its **built-in Agent SDK builder**. It reviews the resulting PRs, drives CI/CD, and ships AI-assisted deployments.

**Production:** https://automatron.quitcode.com

## Architecture

```
Human ─▶ Web UI (Next.js 15) ─ Socket.IO + REST ─▶ Orchestrator (FastAPI + Socket.IO)
                                                        │
                        ┌───────────────────────────────┼───────────────────────────────┐
                        ▼                                ▼                                ▼
                  Architect (LLM)              GitHubClient (REST)               Docker AI (DevOps)
              plan → Epics/Stories/Tasks   repo · milestones · issues · PRs   inventory · plan · deploy
                                                        │
                                                        ▼
                              Copilot coding agent / Agent SDK builder  ──▶  PRs
                                                        │
                                          Webhook ▶ AI review ▶ CI/CD (GitHub Actions)
```

There is **no** LangGraph, Cline, or per-task build container — that earlier design was removed. Docker remains only for **local previews** (via the Docker Python SDK) and the Docker-AI deployment subsystem.

## Stack

| Component | Technology |
|-----------|-----------|
| Orchestrator | Python 3.12, FastAPI + Socket.IO (uvicorn, port 8000) |
| Web UI | Next.js 15 (App Router), React 19, Zustand, Socket.IO client, Tailwind + shadcn/ui (port 3000) |
| LLM | LiteLLM (Claude / GPT / Gemini); Architect defaults to Opus, Builder/Reviewer to Sonnet |
| Code agent | GitHub Copilot coding agent (issues assigned to `copilot-swe-agent[bot]`) or the built-in Agent SDK builder |
| State | SQLite (`orchestrator/data/automatron.db`) |
| Auth | Google OAuth via Auth.js v5 (email allowlist) |
| Deploy | GitHub Actions → SSH → `docker compose` behind Traefik |

## Quick start (local dev)

```bash
# 1. Configure
cp .env.example .env        # fill in GITHUB_TOKEN, ANTHROPIC_API_KEY / OPENAI_API_KEY, GITHUB_OWNER, ...

# 2. Backend (port 8000)
cd orchestrator
pip install -e ".[dev]"     # venv lives at project-root .venv/
uvicorn orchestrator.main:app --reload --host 0.0.0.0 --port 8000
#   or, from the repo root:  make dev

# 3. Web UI (port 3000) — uses npm
cd web-ui
npm install
npm run dev

# 4. Open http://localhost:3000
```

Set `AUTOMATRON_DEV_NO_AUTH=true` in `.env` to bypass Google OAuth during local development.

## Project structure

```
orchestrator/            # FastAPI + Socket.IO backend (package at orchestrator/orchestrator/)
  orchestrator.py        #   GitHubOrchestrator: analyze / plan / apply / review
  github/issues.py       #   GitHubClient: repo, milestone, issue, PR REST calls
  api/                   #   routes.py, webhook_github.py, socket_server.py, websocket.py
  llm/, cicd_specialist/, github_actions/, docker_deployment_ai/, deployment/, preview.py
  tests/                 #   pytest suite
web-ui/                  # Next.js 15 frontend (npm)
docs/                    # ARCHITECTURE.md, DEPLOYMENT.md, and historical reports
docker-compose*.yml      # base + prod (Traefik) overlays
Makefile                 # dev / test / lint helpers
```

## Documentation

- [Architecture](docs/ARCHITECTURE.md)
- [Deployment](docs/DEPLOYMENT.md)
- `CLAUDE.md` — working notes for AI agents in this repo

## License

Private / Proprietary
