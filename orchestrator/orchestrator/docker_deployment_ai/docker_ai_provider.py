"""DockerAIProvider — orchestrates the provider priority chain.

Priority order (configurable via DOCKER_AI_PROVIDER_PRIORITY env var):
  1. gordon       — docker ai subprocess (Docker Desktop feature)
  2. docker_agent — cagent YAML-defined agent workflows
  3. model_runner — Docker Model Runner / Model Gateway (local inference)
  4. litellm      — existing litellm-based LLM provider (fallback)

Each provider is tried in order. A provider is "available" if its
`is_available()` probe returns True. The first available provider is used
for the requested analysis type; if it returns an error, the next provider
is tried. If all providers fail, a structured error result is returned.

If DOCKER_AI_REQUIRE_GORDON=true and Gordon is unavailable, a
`GordonRequiredError` is raised before any fallback is attempted.

This module owns persistence of `docker_ai_analyses` rows via
`_save_analysis`. Callers receive the DB row id in the result so they can
link it to plans.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

import aiosqlite

from orchestrator.config import settings
from orchestrator.docker_deployment_ai.gordon_client import GordonClient

logger = logging.getLogger(__name__)


class GordonRequiredError(RuntimeError):
    """Raised when DOCKER_AI_REQUIRE_GORDON=true and Gordon is unavailable."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _provider_order() -> list[str]:
    raw = settings.docker_ai_provider_priority or "gordon,docker_agent,model_runner,litellm"
    return [p.strip().lower() for p in raw.split(",") if p.strip()]


async def _is_provider_enabled(provider: str) -> bool:
    """Return True if the provider is both configured-on and available."""
    if provider == "gordon":
        return await GordonClient().is_available()
    if provider == "docker_agent":
        return settings.docker_ai_enable_agent
    if provider == "model_runner":
        return settings.docker_ai_enable_model_runner
    if provider == "litellm":
        return True
    return False


async def _call_gordon(method: str, kwargs: dict[str, Any]) -> dict[str, Any]:
    client = GordonClient()
    fn = getattr(client, method, None)
    if fn is None:
        return {"error": f"GordonClient has no method {method}"}
    return await fn(**kwargs)


async def _call_docker_agent(method: str, kwargs: dict[str, Any]) -> dict[str, Any]:
    from orchestrator.docker_deployment_ai.docker_agent_client import DockerAgentClient  # noqa: PLC0415
    client = DockerAgentClient()
    fn = getattr(client, method, None)
    if fn is None:
        return {"error": f"DockerAgentClient has no method {method}"}
    return await fn(**kwargs)


async def _call_model_runner(method: str, kwargs: dict[str, Any]) -> dict[str, Any]:
    from orchestrator.docker_deployment_ai.model_runner_client import ModelRunnerClient  # noqa: PLC0415
    client = ModelRunnerClient()
    fn = getattr(client, method, None)
    if fn is None:
        return {"error": f"ModelRunnerClient has no method {method}"}
    return await fn(**kwargs)


async def _call_litellm(method: str, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Litellm fallback using the existing llm/provider.py wrapper."""
    try:
        from orchestrator.docker_deployment_ai.litellm_adapter import call_litellm_for_docker_ai  # noqa: PLC0415
        return await call_litellm_for_docker_ai(method, kwargs)
    except ImportError:
        return {"error": "litellm_adapter not yet available"}


_PROVIDER_CALLERS = {
    "gordon": _call_gordon,
    "docker_agent": _call_docker_agent,
    "model_runner": _call_model_runner,
    "litellm": _call_litellm,
}


async def _save_analysis(
    db: aiosqlite.Connection,
    *,
    project_id: str,
    target_id: str | None,
    inventory_snapshot_id: str | None,
    provider: str,
    analysis_type: str,
    raw_output: str,
    normalized_json: str,
    status: str,
    error_message: str | None,
) -> str:
    import json as _json
    row_id = str(uuid.uuid4())
    await db.execute(
        """
        INSERT INTO docker_ai_analyses
        (id, project_id, target_id, inventory_snapshot_id, provider,
         analysis_type, raw_output, normalized_json, status, error_message, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row_id, project_id, target_id, inventory_snapshot_id, provider,
            analysis_type, raw_output, normalized_json, status, error_message, _now(),
        ),
    )
    await db.commit()
    return row_id


class DockerAIProvider:
    """Tries providers in priority order and persists the analysis result."""

    async def run_analysis(
        self,
        *,
        method: str,
        kwargs: dict[str, Any],
        project_id: str,
        target_id: str | None = None,
        inventory_snapshot_id: str | None = None,
        db: aiosqlite.Connection,
    ) -> dict[str, Any]:
        """Run *method* against the first available provider.

        Returns the provider result enriched with:
          - ``analysis_id`` — the DB row id for linkage to plans
          - ``provider_used`` — which provider ultimately responded
        """
        import json as _json

        order = _provider_order()
        if settings.docker_ai_require_gordon and "gordon" not in order:
            raise GordonRequiredError(
                "DOCKER_AI_REQUIRE_GORDON=true but 'gordon' not in DOCKER_AI_PROVIDER_PRIORITY"
            )

        # Availability probe for Gordon first, before we iterate
        gordon_available = await GordonClient().is_available()
        if settings.docker_ai_require_gordon and not gordon_available:
            raise GordonRequiredError(
                "DOCKER_AI_REQUIRE_GORDON=true but `docker ai` is not available. "
                "Install Docker Desktop with the AI features enabled, or set "
                "DOCKER_AI_REQUIRE_GORDON=false to allow fallback providers."
            )

        last_error: str = "no providers available"
        for provider in order:
            if not await _is_provider_enabled(provider):
                logger.debug("Provider %s skipped (disabled or unavailable)", provider)
                continue

            caller = _PROVIDER_CALLERS.get(provider)
            if caller is None:
                continue

            logger.info("Docker AI: trying provider=%s method=%s", provider, method)
            try:
                result = await caller(method, kwargs)
            except Exception as exc:
                logger.warning("Provider %s raised: %s", provider, exc)
                result = {"error": str(exc)}

            error = result.get("error")
            if error:
                logger.warning("Provider %s returned error: %s", provider, error)
                last_error = str(error)
                continue

            # Success — persist and return
            raw_out = result.get("raw_output", "")
            normalized = result.get("normalized", {})
            analysis_id = await _save_analysis(
                db,
                project_id=project_id,
                target_id=target_id,
                inventory_snapshot_id=inventory_snapshot_id,
                provider=provider,
                analysis_type=method,
                raw_output=raw_out,
                normalized_json=_json.dumps(normalized),
                status="ok",
                error_message=None,
            )
            result["analysis_id"] = analysis_id
            result["provider_used"] = provider
            return result

        # All providers failed — persist failure record
        analysis_id = await _save_analysis(
            db,
            project_id=project_id,
            target_id=target_id,
            inventory_snapshot_id=inventory_snapshot_id,
            provider="none",
            analysis_type=method,
            raw_output="",
            normalized_json="{}",
            status="error",
            error_message=last_error,
        )
        return {
            "error": last_error,
            "analysis_id": analysis_id,
            "provider_used": None,
        }

    # ---- convenience wrappers ----

    async def analyze_inventory(
        self,
        sanitized_inventory: dict[str, Any],
        *,
        project_id: str,
        target_id: str | None = None,
        inventory_snapshot_id: str | None = None,
        db: aiosqlite.Connection,
    ) -> dict[str, Any]:
        return await self.run_analysis(
            method="analyze_inventory",
            kwargs={"sanitized_inventory": sanitized_inventory},
            project_id=project_id,
            target_id=target_id,
            inventory_snapshot_id=inventory_snapshot_id,
            db=db,
        )

    async def recommend_deployment_strategy(
        self,
        repo_context: dict[str, Any],
        sanitized_inventory: dict[str, Any],
        desired_domain: str | None,
        *,
        project_id: str,
        target_id: str | None = None,
        inventory_snapshot_id: str | None = None,
        db: aiosqlite.Connection,
    ) -> dict[str, Any]:
        return await self.run_analysis(
            method="recommend_deployment_strategy",
            kwargs={
                "repo_context": repo_context,
                "sanitized_inventory": sanitized_inventory,
                "desired_domain": desired_domain,
            },
            project_id=project_id,
            target_id=target_id,
            inventory_snapshot_id=inventory_snapshot_id,
            db=db,
        )

    async def review_dockerfile(
        self,
        dockerfile: str,
        repo_context: dict[str, Any],
        *,
        project_id: str,
        target_id: str | None = None,
        db: aiosqlite.Connection,
    ) -> dict[str, Any]:
        return await self.run_analysis(
            method="review_dockerfile",
            kwargs={"dockerfile": dockerfile, "repo_context": repo_context},
            project_id=project_id,
            target_id=target_id,
            db=db,
        )

    async def explain_build_failure(
        self,
        logs: str,
        dockerfile: str | None,
        compose_yaml: str | None,
        *,
        project_id: str,
        target_id: str | None = None,
        db: aiosqlite.Connection,
    ) -> dict[str, Any]:
        return await self.run_analysis(
            method="explain_build_failure",
            kwargs={"logs": logs, "dockerfile": dockerfile, "compose_yaml": compose_yaml},
            project_id=project_id,
            target_id=target_id,
            db=db,
        )

    async def explain_runtime_failure(
        self,
        logs: str,
        inspect_json: dict[str, Any],
        *,
        project_id: str,
        target_id: str | None = None,
        db: aiosqlite.Connection,
    ) -> dict[str, Any]:
        return await self.run_analysis(
            method="explain_runtime_failure",
            kwargs={"logs": logs, "inspect_json": inspect_json},
            project_id=project_id,
            target_id=target_id,
            db=db,
        )
