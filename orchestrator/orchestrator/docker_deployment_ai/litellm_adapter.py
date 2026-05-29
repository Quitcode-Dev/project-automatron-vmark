"""Litellm fallback adapter for Docker AI methods.

Routes Docker-AI-style method calls through the existing `llm/provider.py`
`call_llm` wrapper, using the same prompt-builder functions as the Gordon
client so the output format is consistent. This is the last resort in the
provider chain — it uses whatever LLM the operator has configured globally.
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

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
from orchestrator.llm.provider import call_llm

logger = logging.getLogger(__name__)

_SYSTEM = (
    "You are a Docker infrastructure expert integrated into Automatron. "
    "Respond ONLY with valid JSON as instructed — no markdown, no prose."
)


async def _run_prompt(prompt: str, analysis_type: str) -> tuple[str, dict[str, Any]]:
    """Call litellm and return (raw_output, normalized_dict)."""
    model = settings.docker_ai_model or settings.architect_model
    try:
        raw = await call_llm(
            [SystemMessage(content=_SYSTEM), HumanMessage(content=prompt)],
            model=model,
            temperature=0.1,
            max_tokens=4096,
        )
    except Exception as exc:
        logger.warning("litellm_adapter: %s call failed: %s", analysis_type, exc)
        return "", {"parse_error": str(exc)}
    safe_raw = sanitize_text(raw)
    parsed = parse_gordon_output(safe_raw, analysis_type=analysis_type)
    return safe_raw, parsed


def _wrap(raw: str, normalized: dict[str, Any], analysis_type: str) -> dict[str, Any]:
    return {
        "provider": "litellm",
        "analysis_type": analysis_type,
        "raw_output": raw,
        "normalized": normalized,
        "error": normalized.get("parse_error") if normalized.get("parse_error") else None,
    }


async def call_litellm_for_docker_ai(method: str, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Dispatch *method* to the appropriate prompt builder and litellm."""
    if method == "analyze_inventory":
        inv = sanitize_context(kwargs.get("sanitized_inventory", {}))
        prompt = analyze_inventory_prompt(inv)
        raw, parsed = await _run_prompt(prompt, "analyze_inventory")
        return _wrap(raw, normalize_inventory_analysis(parsed), "analyze_inventory")

    if method == "recommend_deployment_strategy":
        ctx = sanitize_context(kwargs.get("repo_context", {}))
        inv = sanitize_context(kwargs.get("sanitized_inventory", {}))
        domain = kwargs.get("desired_domain")
        prompt = recommend_deployment_strategy_prompt(ctx, inv, domain)
        raw, parsed = await _run_prompt(prompt, "recommend_deployment_strategy")
        return _wrap(raw, normalize_deployment_strategy(parsed), "recommend_deployment_strategy")

    if method == "review_dockerfile":
        df = sanitize_text(kwargs.get("dockerfile", ""))
        ctx = sanitize_context(kwargs.get("repo_context", {}))
        prompt = review_dockerfile_prompt(df, ctx)
        raw, parsed = await _run_prompt(prompt, "review_dockerfile")
        return _wrap(raw, normalize_dockerfile_review(parsed), "review_dockerfile")

    if method == "review_compose":
        compose = sanitize_text(kwargs.get("compose_yaml", ""))
        inv = sanitize_context(kwargs.get("sanitized_inventory", {}))
        prompt = review_compose_prompt(compose, inv)
        raw, parsed = await _run_prompt(prompt, "review_compose")
        return _wrap(raw, normalize_compose_review(parsed), "review_compose")

    if method == "explain_build_failure":
        logs = sanitize_text(kwargs.get("logs", ""))
        df = sanitize_text(kwargs.get("dockerfile")) if kwargs.get("dockerfile") else None
        cy = sanitize_text(kwargs.get("compose_yaml")) if kwargs.get("compose_yaml") else None
        prompt = explain_build_failure_prompt(logs, df, cy)
        raw, parsed = await _run_prompt(prompt, "explain_build_failure")
        return _wrap(raw, normalize_failure_explanation(parsed), "explain_build_failure")

    if method == "explain_runtime_failure":
        logs = sanitize_text(kwargs.get("logs", ""))
        inspect = sanitize_context(kwargs.get("inspect_json", {}))
        prompt = explain_runtime_failure_prompt(logs, inspect)
        raw, parsed = await _run_prompt(prompt, "explain_runtime_failure")
        return _wrap(raw, normalize_failure_explanation(parsed), "explain_runtime_failure")

    return {"error": f"litellm_adapter: unknown method '{method}'"}
