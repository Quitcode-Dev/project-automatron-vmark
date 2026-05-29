"""Prompt builders for Gordon (`docker ai`) invocations.

Each function returns a list of strings that will be joined with a newline and
piped to `docker ai --stdin` (or equivalent). Prompts are designed to elicit
structured JSON output so `gordon_result_parser` can parse them reliably.

All parameters are expected to have been sanitized by `secrets.sanitize_context`
before reaching these functions.
"""

from __future__ import annotations

import json
from typing import Any

_JSON_FOOTER = (
    "\n\nRespond ONLY with a single valid JSON object — no markdown, no explanation, "
    "no code fences. If you cannot determine a value, use null."
)


def analyze_inventory_prompt(sanitized_inventory: dict[str, Any]) -> str:
    inventory_json = json.dumps(sanitized_inventory, indent=2)
    return (
        "You are a Docker infrastructure expert. Analyze the following server inventory "
        "snapshot and identify the current deployment setup, reverse proxy configuration, "
        "potential conflicts, and any risks.\n\n"
        f"INVENTORY:\n{inventory_json}\n\n"
        "Return a JSON object with these keys:\n"
        '  "deployment_manager": one of none|plain_docker|docker_compose|traefik|kamal_v1|kamal_v2|nginx|caddy|mixed|unknown\n'
        '  "reverse_proxy": one of none|traefik|kamal_proxy|nginx|caddy|unknown\n'
        '  "confidence": float 0.0-1.0\n'
        '  "evidence": list of strings explaining what was detected\n'
        '  "risks": list of strings describing potential deployment risks\n'
        '  "notes": string with any additional observations'
        + _JSON_FOOTER
    )


def review_dockerfile_prompt(dockerfile: str, repo_context: dict[str, Any]) -> str:
    context_json = json.dumps(repo_context, indent=2)
    return (
        "You are a Docker expert. Review the following Dockerfile for quality, security, "
        "and best practices. Consider the repository context provided.\n\n"
        f"DOCKERFILE:\n{dockerfile}\n\n"
        f"REPOSITORY CONTEXT:\n{context_json}\n\n"
        "Return a JSON object with these keys:\n"
        '  "quality_score": integer 1-10\n'
        '  "issues": list of {"severity": "error|warning|info", "description": string}\n'
        '  "suggestions": list of strings\n'
        '  "healthcheck_recommendation": string or null\n'
        '  "base_image_assessment": string'
        + _JSON_FOOTER
    )


def review_compose_prompt(compose_yaml: str, sanitized_inventory: dict[str, Any]) -> str:
    inventory_json = json.dumps(sanitized_inventory, indent=2)
    return (
        "You are a Docker Compose expert. Review the following docker-compose.yml against "
        "the current server state. Identify conflicts, missing labels, networking issues, "
        "and compatibility with the detected reverse proxy.\n\n"
        f"COMPOSE FILE:\n{compose_yaml}\n\n"
        f"SERVER INVENTORY:\n{inventory_json}\n\n"
        "Return a JSON object with these keys:\n"
        '  "compatible": boolean\n'
        '  "conflicts": list of {"type": string, "description": string}\n'
        '  "missing_labels": list of strings\n'
        '  "networking_issues": list of strings\n'
        '  "suggestions": list of strings\n'
        '  "proxy_compatibility": {"proxy": string, "compatible": boolean, "notes": string}'
        + _JSON_FOOTER
    )


def explain_build_failure_prompt(
    logs: str,
    dockerfile: str | None,
    compose_yaml: str | None,
) -> str:
    parts = [
        "You are a Docker build expert. Explain the following Docker build failure and "
        "provide actionable remediation steps.\n\n"
        f"BUILD LOGS:\n{logs}"
    ]
    if dockerfile:
        parts.append(f"\nDOCKERFILE:\n{dockerfile}")
    if compose_yaml:
        parts.append(f"\nCOMPOSE FILE:\n{compose_yaml}")
    parts.append(
        "\n\nReturn a JSON object with these keys:\n"
        '  "root_cause": string\n'
        '  "error_category": one of missing_dependency|base_image|syntax|permissions|network|disk_space|other\n'
        '  "remediation_steps": list of strings (ordered, actionable)\n'
        '  "relevant_log_lines": list of strings (the key error lines)\n'
        '  "estimated_fix_complexity": one of trivial|moderate|complex'
        + _JSON_FOOTER
    )
    return "\n".join(parts)


def explain_runtime_failure_prompt(logs: str, inspect_json: dict[str, Any]) -> str:
    inspect_str = json.dumps(inspect_json, indent=2)
    return (
        "You are a Docker runtime expert. Analyze the following container failure. "
        "The container exited unexpectedly or is not healthy.\n\n"
        f"CONTAINER LOGS:\n{logs}\n\n"
        f"CONTAINER INSPECT:\n{inspect_str}\n\n"
        "Return a JSON object with these keys:\n"
        '  "failure_type": one of crash_loop|oom_killed|exit_code_nonzero|healthcheck_failed|port_conflict|startup_error|other\n'
        '  "root_cause": string\n'
        '  "remediation_steps": list of strings\n'
        '  "relevant_log_lines": list of strings\n'
        '  "healthcheck_suggestion": string or null'
        + _JSON_FOOTER
    )


def recommend_deployment_strategy_prompt(
    repo_context: dict[str, Any],
    sanitized_inventory: dict[str, Any],
    desired_domain: str | None,
) -> str:
    repo_json = json.dumps(repo_context, indent=2)
    inv_json = json.dumps(sanitized_inventory, indent=2)
    domain_line = f"DESIRED DOMAIN: {desired_domain}" if desired_domain else "DESIRED DOMAIN: not specified"
    return (
        "You are a Docker deployment expert. Based on the repository metadata and current "
        "server state, recommend the safest deployment strategy. Consider existing reverse "
        "proxies, port ownership, and Kamal/Traefik/Nginx/Caddy configurations carefully.\n\n"
        f"REPOSITORY CONTEXT:\n{repo_json}\n\n"
        f"SERVER INVENTORY:\n{inv_json}\n\n"
        f"{domain_line}\n\n"
        "Return a JSON object with these keys:\n"
        '  "recommended_strategy": one of '
        "reuse_existing_traefik|kamal_v1_compatible|kamal_v2_compatible|"
        "docker_compose_private|docker_compose_with_host_port|behind_existing_nginx|"
        'behind_existing_caddy|no_public_exposure|manual_required|abort\n'
        '  "risk_level": one of low|medium|high|blocked\n'
        '  "reasoning_summary": string\n'
        '  "blocking_questions": list of strings (questions that must be answered before deploying)\n'
        '  "required_files": list of strings (file names the deployment will need)\n'
        '  "warnings": list of strings'
        + _JSON_FOOTER
    )
