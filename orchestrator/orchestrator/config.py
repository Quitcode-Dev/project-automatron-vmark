"""Application configuration via Pydantic Settings."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Global application settings loaded from environment variables."""

    # --- LLM Providers ---
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    google_api_key: str = ""
    github_token: str = ""
    github_webhook_secret: str = ""
    automatron_public_url: str = ""  # e.g. https://automatron.example.com — used for auto-registering webhooks
    figma_access_token: str = ""    # Figma personal access token for reading design context
    github_owner: str = ""
    github_owner_type: str = "user"
    github_default_org: str = ""
    github_api_url: str = "https://api.github.com"
    github_repo_visibility: str = "private"
    github_environment_name: str = "production"
    github_actions_ci_workflow_name: str = "CI"
    github_actions_deploy_workflow_name: str = "Deploy"
    git_author_name: str = "Automatron Bot"
    git_author_email: str = "automatron@example.local"

    # --- Architect ---
    # Architect benefits most from a larger model — planning quality drives
    # every downstream issue, so the marginal cost is worth it.
    architect_model: str = "anthropic/claude-opus-4-6"
    architect_prompt_version: str = "v1"

    # --- Builder ---
    # Defaults to Sonnet (~5× cheaper than Opus); its tool-use edit quality is
    # nearly indistinguishable from Opus for the patch-style edits the Agent SDK
    # builder makes. Override per-project via llm_config if a task truly needs
    # Opus-level reasoning.
    builder_model: str = "anthropic/claude-sonnet-4-6"
    # Deprecated/unused: the Aider builder (which honoured this) was removed. Kept
    # so deployments whose .env still sets BUILDER_CLINE_TIMEOUT don't trip the
    # extra_forbidden validation on startup. Safe to drop once no .env sets it.
    builder_cline_timeout: int = 900
    # Reviewer reads PR diffs and emits a short structured summary — Sonnet
    # is more than enough.
    reviewer_model: str = "anthropic/claude-sonnet-4-6"

    # --- Docker ---
    golden_image: str = "automatron/golden:latest"
    workspace_base_path: str = "/var/automatron/workspaces"
    port_range_start: int = 7000
    port_range_end: int = 7999

    # --- Preview exposure ---
    # When preview_base_domain is set, preview containers are published through
    # Traefik as HTTPS subdomains (https://<slug>.<preview_base_domain>) instead
    # of http://<host>:<port>. The raw-port form is unreachable behind the TLS
    # reverse proxy AND blocked as mixed content inside the HTTPS app, so it must
    # be set in any deployment that runs behind Traefik. Requires wildcard DNS
    # (*.<preview_base_domain> → this host) so Traefik's cert resolver can issue
    # a cert per preview host. Leave empty for local dev (falls back to
    # http://localhost:<port>).
    preview_base_domain: str = ""
    preview_traefik_network: str = "proxy"
    preview_traefik_certresolver: str = "le"
    preview_traefik_entrypoint: str = "websecure"

    # --- Deploy ---
    deploy_ssh_key_path: str = ""
    deploy_ssh_options: str = ""

    # --- Docker AI / Deployment Intelligence ---
    # Provider priority chain (comma-separated, order matters). First available wins.
    # Allowed tokens: gordon, docker_agent, model_runner, litellm
    docker_ai_provider_priority: str = "gordon,docker_agent,model_runner,litellm"
    # When true, fail loudly if Gordon (`docker ai`) is unavailable instead of
    # falling through the chain. Use this on workstations where Docker Desktop is
    # required; leave false in containerized deployments where Gordon is usually
    # not present.
    docker_ai_require_gordon: bool = False
    docker_ai_enable_agent: bool = True
    docker_ai_enable_mcp: bool = True
    docker_ai_enable_model_runner: bool = False
    docker_model_runner_base_url: str = "http://model-runner.docker.internal:12434"
    docker_model_gateway_base_url: str = "http://localhost:12434"
    # Subprocess hard limits for docker ai / cagent invocations. Keeps a stuck
    # LLM call from blocking the event loop indefinitely or filling memory.
    docker_ai_timeout_seconds: int = 120
    docker_ai_max_output_bytes: int = 262144
    # Model for the DevOps/deployment agent. Passed to docker ai / cagent /
    # model runner when the backend supports a `--model` flag, and used directly
    # by the litellm fallback (the production path inside the Linux container,
    # where Gordon is unavailable). Codex rejects non-default temperature — the
    # llm provider sets temperature=1 for any `gpt-5*` model.
    docker_ai_model: str = "gpt-5.3-codex"

    # --- Database ---
    sqlite_db_path: str = "./data/automatron.db"
    checkpoint_db_path: str = "./data/checkpoints.db"

    # --- Server ---
    host: str = "0.0.0.0"
    port: int = 8000
    debug: bool = False
    # Extra CORS origins (comma-separated). The browser rejects a wildcard
    # origin on credentialed requests, so we echo concrete origins instead:
    # any localhost/127.0.0.1 port (dev) is allowed via regex, AUTOMATRON_PUBLIC_URL
    # is added automatically, and anything here is appended for other deployments.
    cors_allow_origins: str = ""

    # --- Auth (Google OAuth via Auth.js v5) ---
    # AUTH_SECRET is shared with the web-ui's NextAuth config. Used to verify the
    # session JWT cookie. Generate with: openssl rand -base64 32
    auth_secret: str = ""
    # Comma-separated allowlist of email addresses that can sign in. Leave empty
    # to disable auth entirely (dev mode / pre-OAuth deployments).
    automatron_allowed_emails: str = ""
    # Local-dev escape hatch: when true, require_auth always returns a fake user.
    # NEVER set in production.
    automatron_dev_no_auth: bool = False

    @field_validator("debug", mode="before")
    @classmethod
    def _parse_debug_flag(cls, value: object) -> object:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes", "on", "debug", "dev", "development"}:
                return True
            if normalized in {"0", "false", "no", "off", "release", "prod", "production"}:
                return False
        return value

    @property
    def sqlite_db_dir(self) -> Path:
        path = Path(self.sqlite_db_path).parent
        path.mkdir(parents=True, exist_ok=True)
        return path

    @classmethod
    def _project_root(cls) -> Path:
        return Path(__file__).resolve().parents[1]

    @field_validator("sqlite_db_path", "checkpoint_db_path", mode="before")
    @classmethod
    def _normalize_sqlite_paths(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        raw = value.strip()
        if not raw:
            return value
        path = Path(raw)
        if path.is_absolute():
            path.parent.mkdir(parents=True, exist_ok=True)
            return str(path)
        normalized = (cls._project_root() / path).resolve()
        normalized.parent.mkdir(parents=True, exist_ok=True)
        return str(normalized)

    @property
    def workspace_base_dir(self) -> Path:
        raw_path = self.workspace_base_path.strip()
        if os.name == "nt" and raw_path.startswith("/"):
            path = Path.cwd() / "workspaces"
        else:
            path = Path(raw_path)
        path.mkdir(parents=True, exist_ok=True)
        return path

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
