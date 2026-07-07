# Architecture — Project Automatron

## System Overview

Automatron is a **GitHub-native** planning and orchestration system. It turns a
product idea (or an existing repo) into a structured plan, provisions a GitHub
repository with Milestones and Issues, and delegates the actual coding to the
**GitHub Copilot coding agent** or its built-in **Agent SDK builder**. It then reviews the resulting pull
requests with an LLM, manages CI/CD via GitHub Actions, and runs AI-assisted
deployments to a target host.

> **Note:** An earlier design orchestrated LLM "builder" agents (Cline) inside
> per-task Docker containers with LangGraph. That was fully removed. LangGraph is
> no longer imported anywhere. Docker is now used only for **local previews** and
> the **Docker-AI deployment** subsystem.

## Architecture Diagram

```
┌────────────────────────────────────────────────────────────────┐
│                      Web UI (Next.js 15, port 3000)            │
│   Projects · Plan editor · Issues/PRs · Logs · Deploy panel    │
│                Google OAuth (Auth.js v5)                       │
└───────────────────────────┬────────────────────────────────────┘
                            │  Socket.IO + REST
┌───────────────────────────┴────────────────────────────────────┐
│           Orchestrator — FastAPI + Socket.IO (port 8000)       │
│   app = socketio.ASGIApp(sio, other_asgi_app=fastapi_app)      │
│                                                                │
│  ┌──────────────────┐   ┌──────────────────┐   ┌────────────┐  │
│  │ GitHubOrchestrator│  │   GitHubClient    │  │  LLM       │  │
│  │ analyze / plan /  │  │  repos · issues · │  │  provider  │  │
│  │ apply / review    │  │  milestones · PRs │  │ (LiteLLM)  │  │
│  └────────┬─────────┘   └─────────┬────────┘   └────────────┘  │
│           │                       │                            │
│  ┌────────▼─────────┐   ┌─────────▼────────┐   ┌────────────┐  │
│  │ cicd_specialist  │   │ webhook_github   │   │ Docker AI  │  │
│  │ github_actions   │   │ (Copilot PR →    │   │ deployment │  │
│  │ (CI/CD workflows)│   │  AI review)      │   │ subsystem  │  │
│  └──────────────────┘   └──────────────────┘   └─────┬──────┘  │
│                                                       │        │
│              SQLite (data/automatron.db)              │        │
└───────────────────────────────────────────────────────┼────────┘
                                                        │ SSH
        GitHub (repos, issues, PRs, Actions)     Deployment target host
        Copilot coding agent / Agent SDK builder (docker compose)
```

## Component Details

### 1. Web UI (Next.js 15)
- App Router, React 19, Tailwind + shadcn/ui, dark mode.
- **State:** Zustand stores. **Real-time:** Socket.IO client.
- **Auth:** Google OAuth via Auth.js v5 (`web-ui/src/auth.ts`, `middleware.ts`),
  gated by an email allowlist shared with the backend.

### 2. Orchestrator (FastAPI + Socket.IO)
- Entry point `orchestrator.main:app` — a `socketio.ASGIApp` wrapping the FastAPI
  app so REST and WebSocket share one ASGI server (uvicorn, port 8000).
- **REST** (`api/routes.py`): project CRUD, plan management, issue/PR operations,
  Copilot delegation, previews, deployment targets/plans/runs.
- **WebSocket** (`api/socket_server.py`, `api/websocket.py`): streams progress
  (e.g. `status:update`) and human-in-the-loop prompts (`human:required`).
- **Webhooks** (`api/webhook_github.py`): `POST /api/webhooks/github` verifies the
  HMAC signature, dedupes deliveries, auto-detects Copilot PRs, and triggers AI review.

### 3. Core services

| Module | Responsibility |
|--------|----------------|
| `orchestrator.py` (`GitHubOrchestrator`) | Top-level flow: analyze → plan → apply → review |
| `github/issues.py` (`GitHubClient`) | All GitHub REST calls (repos, milestones, issues, PRs, assignment) |
| `llm/` | LiteLLM provider, model catalog, prompt templates |
| `plan_parser/` | Parse the LLM plan into Epics / Stories / Tasks |
| `cicd_specialist/`, `github_actions/` | Generate & sync CI/CD workflows and read run status |
| `preview.py` | Local preview runner via the Docker Python SDK (ports 7000–7999) |
| `docker_deployment_ai/` | Deployment intelligence — inventory, plan, validate, execute, rollback |
| `deployment/`, `scaffolding/`, `validation/`, `execution_contract.py` | Deploy execution, repo scaffolding, plan/task validation, the execution contract |

### 4. Planning & delegation flow

1. **Intake** — a project is created from a prompt or an existing repo/README.
2. **Architect** (`architect_model`, default `anthropic/claude-opus-4-6`) generates
   a plan: Epics → Stories → Tasks, plus a stack/execution contract.
3. **Human review** — the plan is approved in the UI (`approve-plan`).
4. **Apply** — `GitHubClient` provisions the repo, Milestones, and Issues.
5. **Delegate** — issues are assigned to `copilot-swe-agent[bot]` (Copilot coding
   agent) or handed to the Agent SDK builder (`builder_model`, default
   `anthropic/claude-sonnet-4-6`), which pushes to `agent-sdk/fix-<n>` and opens a PR.
6. **Review** — a Copilot PR fires the webhook; the reviewer LLM
   (`reviewer_model`) posts a structured review (`review-pr`).
7. **CI/CD** — GitHub Actions run; the orchestrator syncs status onto the project.
8. **Deploy** — the Docker-AI subsystem inventories the target, drafts a plan,
   validates it, and (after approval) executes over SSH.

### 5. LLM configuration
- Per-role models resolved from settings, overridable per project via
  `llm_config_json`. Defaults live in `config.py`; `.env` overrides them.
- Providers routed through **LiteLLM** (Anthropic / OpenAI / Google). Note:
  `gpt-5*` models require `temperature=1` (Codex rejects other values).

### 6. Data model (SQLite)

Core tables (`models/project.py`):

```
projects          → id, name, status, project_stage, plan_md, stack_config_json,
                     llm_config_json, execution_contract_json, repo_url,
                     default/develop/feature_branch, ci_status, deploy_status,
                     preview_url, plan_approved, preview_approved, timestamps, ...
sessions          → id, project_id, thread_id, phase, started_at, ended_at
task_logs         → id, session_id, task_index, task_text, status, output, duration_s
chat_messages     → id, project_id, role, content, created_at
github_issues     → id, project_id, issue_number, title, epic, story, status,
                     pr_number, pr_url, pr_review_json, build_status
activity_logs     → id, project_id, seq, task_text, output, status, created_at
trace_events      → id, project_id, actor, event_type, stage, payload_json
deploy_runs       → id, project_id, status, branch, output, summary_json
webhook_deliveries→ delivery_id, received_at   (idempotency dedupe)
```

The Docker-AI subsystem adds `deployment_targets`, `deployment_inventory_snapshots`,
`docker_ai_analyses`, `deployment_plans`, `deployment_run_steps`, `deployment_secrets`,
and related tables (`docker_deployment_ai/schema.py`).

> **Debugging tip:** FastAPI `BackgroundTasks` swallows exceptions silently. When
> "nothing happens," inspect the `activity_logs` and `trace_events` tables.

## Technology Stack

| Layer | Technology |
|-------|-----------|
| Frontend | Next.js 15, React 19, Tailwind + shadcn/ui, Zustand, Socket.IO, Auth.js v5 |
| Backend | FastAPI + Socket.IO, Python 3.12, uvicorn |
| LLM | LiteLLM (Claude / GPT / Gemini) |
| Code agent | GitHub Copilot coding agent, built-in Agent SDK builder |
| Deployment AI | Docker AI (`docker ai` / cagent / model runner / LiteLLM fallback) |
| Database | SQLite |
| Reverse proxy | Traefik (production, TLS via Let's Encrypt) |
