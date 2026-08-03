# Deployment Guide — Project Automatron

Production runs at **https://automatron.quitcode.com** and deploys automatically
from `origin/main` via GitHub Actions.

## Prerequisites

- **OS:** Linux host with Docker Engine 24.0+ and Docker Compose v2.
- **Reverse proxy:** [Traefik](https://traefik.io) attached to an external Docker
  network named `proxy` (TLS via Let's Encrypt, cert resolver `le`). The base
  `docker-compose.yml` also ships an `nginx` service, but the production overlay
  disables it in favour of Traefik.
- **Ports:** 8000 (API/WS), 3000 (UI). Both are exposed to Traefik over the
  `proxy` network rather than published directly in production.
- API keys and GitHub credentials (see Environment variables below).

## Local development

```bash
cp .env.example .env        # fill in the values (see below)

# Backend — port 8000
cd orchestrator
pip install -e ".[dev]"
uvicorn orchestrator.main:app --reload --host 0.0.0.0 --port 8000   # or: make dev

# Frontend — port 3000 (npm)
cd web-ui
npm install
npm run dev
```

Set `AUTOMATRON_DEV_NO_AUTH=true` to bypass Google OAuth locally.

### Run the full stack in Docker (dev)

```bash
docker compose up -d --build     # or: make build && make up
docker compose logs -f           # or: make logs
```

## Production deployment (automated)

`.github/workflows/deploy.yml` runs on every push to `main` (and via
`workflow_dispatch`). It:

1. Validates the `AUTOMATRON_DEPLOY_KEY` secret and configures SSH.
2. SSHes into the production host and, in `/root/app/automatron-vmark`, fetches
   and **hard-resets** to `origin/main` (preserving the server's `.env`).
3. Rebuilds and restarts containers:
   ```bash
   docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build --remove-orphans
   ```
4. Posts a status webhook notification.

The overlay wires both services into Traefik on `automatron.quitcode.com`:

- `/api/*`, `/health`, `/socket.io/*` → `orchestrator:8000`
- `/api/auth/*` → `web-ui:3000` (Auth.js routes live in Next.js, higher priority)
- everything else → `web-ui:3000`

### Required GitHub Actions secrets

| Secret | Purpose |
|--------|---------|
| `AUTOMATRON_DEPLOY_KEY` | SSH private key for the deploy user on the host |
| `AUTOMATRON_WEBHOOK_URL` | Optional — deploy status notification endpoint |

The host also needs `/root/.ssh/automatron_deploy` (a key with read access to the
repo) so it can `git fetch` over SSH.

## Environment variables

Configuration is loaded from `.env` (see `.env.example`). `pydantic-settings`
reads it from the current working directory. Key variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `GITHUB_TOKEN` | — | GitHub PAT for repo/issue/PR automation |
| `GITHUB_OWNER` / `GITHUB_OWNER_TYPE` | — / `user` | Target owner and whether it's a `user` or `org` |
| `GITHUB_WEBHOOK_SECRET` | — | HMAC secret verifying inbound GitHub webhooks |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `GOOGLE_API_KEY` | — | LLM providers (via LiteLLM) |
| `ARCHITECT_MODEL` | `anthropic/claude-opus-4-6` | Planning model |
| `BUILDER_MODEL` | `anthropic/claude-sonnet-4-6` | Agent SDK builder model |
| `REVIEWER_MODEL` | `anthropic/claude-sonnet-4-6` | PR review model |
| `SQLITE_DB_PATH` | `./data/automatron.db` | Application database |
| `AUTH_SECRET` | — | Shared JWT secret (also used by web-ui NextAuth) |
| `AUTH_URL` | — | Public URL for NextAuth (e.g. the production URL) |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | — | Google OAuth 2.0 web client |
| `AUTOMATRON_ALLOWED_EMAILS` | — | Sign-in allowlist (`dev@x.com` or `@x.com` domain rules) |
| `AUTOMATRON_ADMIN_EMAILS` | — | Admins see every project; the **first** entry inherits projects created before per-user ownership |
| `AUTOMATRON_DEV_NO_AUTH` | `false` | Local escape hatch — **never** set in production |
| `AUTOMATRON_PUBLIC_URL` | — | Public URL; used to auto-register GitHub webhooks |
| `DOCKER_AI_PROVIDER_PRIORITY` | `gordon,docker_agent,model_runner,litellm` | Deployment-AI backend chain (first available wins) |
| `DOCKER_AI_MODEL` | `gpt-5.3-codex` | Model for the DevOps/deployment agent |
| `PORT_RANGE_START` / `PORT_RANGE_END` | `7000` / `7999` | Local preview port range |

> `gpt-5*` models require `temperature=1`; the LLM provider sets this automatically.

## Data persistence

- `orchestrator/data/` — SQLite databases. Back up by copying the directory.
- The server's `.env` is preserved across deploys (never checked into git).

## Monitoring

- Health endpoint: `GET /health` → `{"status": "ok"}` (also used by the compose
  healthcheck).
- Logs: `docker compose logs -f orchestrator` (or `make logs-api` / `make logs-ui`).

## Makefile reference

| Command | Description |
|---------|-------------|
| `make dev` / `make dev-ui` | Run orchestrator / Next.js with hot-reload |
| `make build` / `make up` / `make down` | Build / start / stop the Docker stack |
| `make logs` / `make logs-api` / `make logs-ui` | Tail logs |
| `make test` / `make test-cov` | Run tests (with coverage) |
| `make lint` / `make format` / `make typecheck` | ruff lint / ruff format / mypy |
| `make install` / `make install-ui` | Install backend / frontend deps |
| `make clean` / `make clean-docker` | Remove build artifacts / containers & images |

> The `make golden` / `make secrets` targets belong to the removed Docker-builder
> design and are not part of the current deployment path.

## Troubleshooting

### "Nothing happens" after starting a project
FastAPI `BackgroundTasks` swallows exceptions silently. Inspect the SQLite
`activity_logs` and `trace_events` tables:
```bash
sqlite3 orchestrator/data/automatron.db "SELECT created_at, status, task_text FROM activity_logs ORDER BY seq DESC LIMIT 20;"
```

### Webhook not firing
Confirm `GITHUB_WEBHOOK_SECRET` matches the webhook config and that
`AUTOMATRON_PUBLIC_URL` is reachable. Deliveries are deduped in
`webhook_deliveries`.

### Preview container issues
The local preview runner uses the Docker Python SDK. Ensure the Docker socket is
accessible and that ports `7000–7999` are free.
