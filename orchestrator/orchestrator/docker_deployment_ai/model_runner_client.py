"""ModelRunnerClient — optional local/private model backend via Docker Model Runner.

Uses the OpenAI-compatible HTTP API exposed by Docker Model Runner at
`DOCKER_MODEL_RUNNER_BASE_URL` (default: http://model-runner.docker.internal:12434).

This is an optional provider — only active when `DOCKER_AI_ENABLE_MODEL_RUNNER=true`.
It reuses the same prompt-builder + normalizer pipeline as GordonClient and the
litellm fallback, so output format is consistent across all providers.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from orchestrator.config import settings
from orchestrator.docker_deployment_ai.gordon_prompts import (
    analyze_inventory_prompt,
    explain_build_failure_prompt,
    explain_runtime_failure_prompt,
    recommend_deployment_strategy_prompt,
    review_compose_prompt,
    review_dockerfile_prompt,
)
from orchestrator.docker_deployment_ai.gordon_result_parser import (
    normalize_compose_review,
    normalize_deployment_strategy,
    normalize_dockerfile_review,
    normalize_failure_explanation,
    normalize_inventory_analysis,
    parse_gordon_output,
)
from orchestrator.docker_deployment_ai.secrets import sanitize_context, sanitize_text

logger = logging.getLogger(__name__)

_SYSTEM = (
    "You are a Docker infrastructure expert. "
    "Respond ONLY with valid JSON as instructed — no markdown, no prose."
)


class ModelRunnerClient:
    """Adapter for Docker Model Runner / Model Gateway local inference."""

    def __init__(self) -> None:
        # Prefer Model Runner; fall back to Model Gateway URL
        self._base_url = (
            settings.docker_model_runner_base_url.rstrip("/")
            or settings.docker_model_gateway_base_url.rstrip("/")
        )
        self._model = settings.docker_ai_model or "ai/llama3.2"

    async def is_available(self) -> bool:
        if not settings.docker_ai_enable_model_runner:
            return False
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{self._base_url}/v1/models")
                return resp.status_code == 200
        except Exception:
            return False

    async def _chat(self, prompt: str) -> tuple[str, str | None]:
        """Run a chat completion. Returns (response_text, error_or_None)."""
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
            "max_tokens": 4096,
        }
        try:
            async with httpx.AsyncClient(timeout=settings.docker_ai_timeout_seconds) as client:
                resp = await client.post(
                    f"{self._base_url}/v1/chat/completions",
                    json=payload,
                )
                resp.raise_for_status()
                data = resp.json()
                content = (
                    data.get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                )
                return sanitize_text(content), None
        except httpx.HTTPStatusError as exc:
            return "", f"Model Runner HTTP {exc.response.status_code}"
        except Exception as exc:
            return "", str(exc)

    def _wrap(self, raw: str, normalized: dict[str, Any], analysis_type: str, error: str | None) -> dict[str, Any]:
        return {
            "provider": "model_runner",
            "analysis_type": analysis_type,
            "raw_output": raw,
            "normalized": normalized,
            "error": error,
        }

    async def analyze_inventory(self, sanitized_inventory: dict[str, Any], **_: Any) -> dict[str, Any]:
        prompt = analyze_inventory_prompt(sanitize_context(sanitized_inventory))
        raw, err = await self._chat(prompt)
        if err:
            return self._wrap("", {"parse_error": err}, "analyze_inventory", err)
        parsed = parse_gordon_output(raw, analysis_type="analyze_inventory")
        return self._wrap(raw, normalize_inventory_analysis(parsed), "analyze_inventory", None)

    async def recommend_deployment_strategy(
        self,
        repo_context: dict[str, Any],
        sanitized_inventory: dict[str, Any],
        desired_domain: str | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        prompt = recommend_deployment_strategy_prompt(
            sanitize_context(repo_context), sanitize_context(sanitized_inventory), desired_domain
        )
        raw, err = await self._chat(prompt)
        if err:
            return self._wrap("", {"parse_error": err}, "recommend_deployment_strategy", err)
        parsed = parse_gordon_output(raw, analysis_type="recommend_deployment_strategy")
        return self._wrap(raw, normalize_deployment_strategy(parsed), "recommend_deployment_strategy", None)

    async def review_dockerfile(self, dockerfile: str, repo_context: dict[str, Any], **_: Any) -> dict[str, Any]:
        prompt = review_dockerfile_prompt(sanitize_text(dockerfile), sanitize_context(repo_context))
        raw, err = await self._chat(prompt)
        if err:
            return self._wrap("", {"parse_error": err}, "review_dockerfile", err)
        parsed = parse_gordon_output(raw, analysis_type="review_dockerfile")
        return self._wrap(raw, normalize_dockerfile_review(parsed), "review_dockerfile", None)

    async def explain_build_failure(
        self, logs: str, dockerfile: str | None = None, compose_yaml: str | None = None, **_: Any
    ) -> dict[str, Any]:
        prompt = explain_build_failure_prompt(
            sanitize_text(logs),
            sanitize_text(dockerfile) if dockerfile else None,
            sanitize_text(compose_yaml) if compose_yaml else None,
        )
        raw, err = await self._chat(prompt)
        if err:
            return self._wrap("", {"parse_error": err}, "explain_build_failure", err)
        parsed = parse_gordon_output(raw, analysis_type="explain_build_failure")
        return self._wrap(raw, normalize_failure_explanation(parsed), "explain_build_failure", None)

    async def explain_runtime_failure(
        self, logs: str, inspect_json: dict[str, Any], **_: Any
    ) -> dict[str, Any]:
        prompt = explain_runtime_failure_prompt(sanitize_text(logs), sanitize_context(inspect_json))
        raw, err = await self._chat(prompt)
        if err:
            return self._wrap("", {"parse_error": err}, "explain_runtime_failure", err)
        parsed = parse_gordon_output(raw, analysis_type="explain_runtime_failure")
        return self._wrap(raw, normalize_failure_explanation(parsed), "explain_runtime_failure", None)
