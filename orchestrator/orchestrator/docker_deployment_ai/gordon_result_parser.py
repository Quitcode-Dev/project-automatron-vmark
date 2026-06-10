"""Parse raw Gordon (`docker ai`) text output into structured dicts.

Gordon responds to prompts with free text that typically contains a JSON
block. This parser does a best-effort extraction so callers always get a
dict — never a parse exception that bubbles up to the API layer.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# Match the first {...} or [...] block, including nested braces/brackets.
_JSON_BLOCK_RE = re.compile(r"(\{.*\}|\[.*\])", re.DOTALL)


def _extract_json(text: str) -> str | None:
    """Return the first JSON object/array found in *text*, or None."""
    # Strip markdown code fences first
    cleaned = re.sub(r"```(?:json)?\s*", "", text)
    cleaned = re.sub(r"```", "", cleaned)
    match = _JSON_BLOCK_RE.search(cleaned)
    return match.group(1) if match else None


def parse_gordon_output(raw: str, *, analysis_type: str = "unknown") -> dict[str, Any]:
    """Return a normalized dict from *raw* Gordon output.

    On parse failure returns a dict with ``parse_error`` set and ``raw``
    preserved so callers can inspect or retry.
    """
    if not raw or not raw.strip():
        return {"parse_error": "empty output", "raw": raw, "analysis_type": analysis_type}

    json_str = _extract_json(raw)
    if json_str is None:
        logger.warning("Gordon output for %s had no JSON block", analysis_type)
        return {"parse_error": "no JSON block found", "raw": raw[:2000], "analysis_type": analysis_type}

    try:
        parsed = json.loads(json_str)
    except json.JSONDecodeError as exc:
        logger.warning("Gordon JSON parse error for %s: %s", analysis_type, exc)
        return {"parse_error": str(exc), "raw": raw[:2000], "analysis_type": analysis_type}

    if not isinstance(parsed, dict):
        return {"parse_error": "expected object, got array/scalar", "raw": raw[:2000], "analysis_type": analysis_type}

    parsed.setdefault("analysis_type", analysis_type)
    return parsed


def normalize_inventory_analysis(raw_parsed: dict[str, Any]) -> dict[str, Any]:
    return {
        "deployment_manager": raw_parsed.get("deployment_manager", "unknown"),
        "reverse_proxy": raw_parsed.get("reverse_proxy", "unknown"),
        "confidence": float(raw_parsed.get("confidence") or 0.0),
        "evidence": raw_parsed.get("evidence") or [],
        "risks": raw_parsed.get("risks") or [],
        "notes": raw_parsed.get("notes") or "",
        "parse_error": raw_parsed.get("parse_error"),
    }


def normalize_deployment_strategy(raw_parsed: dict[str, Any]) -> dict[str, Any]:
    return {
        "recommended_strategy": raw_parsed.get("recommended_strategy", "manual_required"),
        "risk_level": raw_parsed.get("risk_level", "high"),
        "reasoning_summary": raw_parsed.get("reasoning_summary") or "",
        "blocking_questions": raw_parsed.get("blocking_questions") or [],
        "required_files": raw_parsed.get("required_files") or [],
        "warnings": raw_parsed.get("warnings") or [],
        "parse_error": raw_parsed.get("parse_error"),
    }


def normalize_dockerfile_review(raw_parsed: dict[str, Any]) -> dict[str, Any]:
    return {
        "quality_score": raw_parsed.get("quality_score"),
        "issues": raw_parsed.get("issues") or [],
        "suggestions": raw_parsed.get("suggestions") or [],
        "healthcheck_recommendation": raw_parsed.get("healthcheck_recommendation"),
        "base_image_assessment": raw_parsed.get("base_image_assessment") or "",
        "parse_error": raw_parsed.get("parse_error"),
    }


def normalize_compose_review(raw_parsed: dict[str, Any]) -> dict[str, Any]:
    return {
        "compatible": raw_parsed.get("compatible"),
        "conflicts": raw_parsed.get("conflicts") or [],
        "missing_labels": raw_parsed.get("missing_labels") or [],
        "networking_issues": raw_parsed.get("networking_issues") or [],
        "suggestions": raw_parsed.get("suggestions") or [],
        "proxy_compatibility": raw_parsed.get("proxy_compatibility") or {},
        "parse_error": raw_parsed.get("parse_error"),
    }


def normalize_failure_explanation(raw_parsed: dict[str, Any]) -> dict[str, Any]:
    return {
        "root_cause": raw_parsed.get("root_cause") or "",
        "error_category": raw_parsed.get("error_category") or raw_parsed.get("failure_type") or "other",
        "remediation_steps": raw_parsed.get("remediation_steps") or [],
        "relevant_log_lines": raw_parsed.get("relevant_log_lines") or [],
        "estimated_fix_complexity": raw_parsed.get("estimated_fix_complexity"),
        "healthcheck_suggestion": raw_parsed.get("healthcheck_suggestion"),
        "parse_error": raw_parsed.get("parse_error"),
    }
