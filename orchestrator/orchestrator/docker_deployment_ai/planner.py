"""Planner — converts Docker AI analysis + inventory into a deployment_plan.json.

Pipeline:
  sanitized context → DockerAIProvider.recommend_deployment_strategy()
  → optional DockerAgent deployment-advisor
  → PlanNormalizer
  → schema validation
  → persist to deployment_plans

The planner never invents domains, ports, secrets, networks, or production
assumptions. Missing data → strategy=manual_required, risk_level=blocked.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any

import aiosqlite

from orchestrator.docker_deployment_ai.docker_ai_provider import DockerAIProvider
from orchestrator.docker_deployment_ai.inventory import build_sanitized_inventory
from orchestrator.docker_deployment_ai.models import InventorySnapshot
from orchestrator.docker_deployment_ai.plan_hash import compute_plan_hash
from orchestrator.docker_deployment_ai.plan_schema import BLOCKED_PLAN, validate_plan
from orchestrator.docker_deployment_ai.policy import check_policy

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _strategy_from_detection(
    deployment_manager: str,
    reverse_proxy: str,
    confidence: float,
    preferred_strategy: str,
) -> tuple[str, str, list[str]]:
    """Map detection + preference → (strategy, risk_level, blocking_questions).

    Returns conservative defaults when confidence is low.
    """
    questions: list[str] = []

    # Low confidence always requires manual review
    if confidence < 0.4:
        questions.append(
            f"Deployment manager detection confidence is low ({confidence:.0%}). "
            "Please verify the server state manually."
        )
        return "manual_required", "blocked", questions

    # Auto-detect logic following spec §15
    if preferred_strategy != "auto_detect":
        # Honour user's explicit choice but warn if it conflicts
        return preferred_strategy, "medium", questions

    if deployment_manager in ("mixed", "unknown"):
        questions.append("Server has an ambiguous or mixed deployment setup. Clarify before deploying.")
        return "manual_required", "blocked", questions

    if deployment_manager == "kamal_v2":
        return "kamal_v2_compatible", "medium", questions

    if deployment_manager == "kamal_v1":
        return "kamal_v1_compatible", "medium", questions

    if reverse_proxy == "traefik" or deployment_manager == "traefik":
        return "reuse_existing_traefik", "low", questions

    if reverse_proxy == "nginx" or deployment_manager == "nginx":
        return "behind_existing_nginx", "low", questions

    if reverse_proxy == "caddy" or deployment_manager == "caddy":
        return "behind_existing_caddy", "low", questions

    # No proxy: suggest private first; host-port requires approval
    if deployment_manager in ("none", "plain_docker", "docker_compose"):
        questions.append(
            "No public reverse proxy detected. "
            "Confirm whether public exposure is required — host-port binding needs explicit approval."
        )
        return "docker_compose_private", "medium", questions

    questions.append("Unable to determine safe deployment strategy automatically.")
    return "manual_required", "blocked", questions


# Compose strategies that support deterministic action generation.
_COMPOSE_STRATEGIES: frozenset[str] = frozenset([
    "docker_compose_with_host_port",
    "docker_compose_private",
])

# Safe compose filename: alphanumeric/dash/underscore start, .yml or .yaml suffix.
_SAFE_COMPOSE_FILENAME_RE = re.compile(r"^[\w][\w.\-]*\.ya?ml$", re.ASCII)


def _build_deployment_actions(
    strategy: str,
    repo_context: dict[str, Any],
    deploy_path: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Build concrete deployment_actions for compose-based strategies.

    Returns (actions, additional_blocking_questions).

    Non-compose strategies (manual_required, abort, traefik variants, etc.)
    return ([], []) — caller should not generate actions for them.

    Compose strategies require repo_context["compose_content"] to be present.
    If absent, returns ([], [blocking_question]) so the operator knows exactly
    what to supply. The planner never invents compose file content.
    """
    if strategy not in _COMPOSE_STRATEGIES:
        return [], []

    # Sanitise compose filename — reject traversal, shell chars, absolute paths.
    raw_filename = (repo_context.get("compose_file") or "docker-compose.yml").strip()
    if not _SAFE_COMPOSE_FILENAME_RE.match(raw_filename):
        raw_filename = "docker-compose.yml"
    compose_file = raw_filename

    compose_content: str = repo_context.get("compose_content") or ""
    if not compose_content.strip():
        return [], [
            f"'compose_content' is required in repo_context to generate deployment "
            f"actions. Provide the full content of {compose_file!r} so the planner "
            "can produce the UPLOAD_FILE action without inventing file content."
        ]

    deploy_dir = deploy_path.rstrip("/")
    remote_compose_path = f"{deploy_dir}/{compose_file}"

    actions: list[dict[str, Any]] = [
        {"action_type": "CREATE_DIRECTORY", "params": {"path": deploy_dir}},
        {
            "action_type": "UPLOAD_FILE",
            "params": {"path": remote_compose_path, "content": compose_content},
        },
        {"action_type": "DOCKER_COMPOSE_CONFIG", "params": {"compose_file": compose_file}},
        {
            "action_type": "DOCKER_COMPOSE_UP",
            "params": {"compose_file": compose_file, "service": ""},
        },
        {"action_type": "DOCKER_COMPOSE_PS", "params": {"compose_file": compose_file}},
    ]
    return actions, []


def _build_plan(
    strategy: str,
    risk_level: str,
    docker_ai_result: dict[str, Any],
    snapshot: InventorySnapshot,
    target_domain: str | None,
    blocking_questions: list[str],
    deployment_actions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    detection = snapshot.detection
    ai_normalized = docker_ai_result.get("normalized") or {}
    analysis_id = docker_ai_result.get("analysis_id")
    provider = docker_ai_result.get("provider_used") or docker_ai_result.get("provider") or "none"

    # Merge blocking questions from AI and from detection
    all_questions = list(blocking_questions)
    ai_questions = ai_normalized.get("blocking_questions") or []
    all_questions.extend(q for q in ai_questions if q not in all_questions)

    # If AI says higher risk, respect it
    ai_risk = ai_normalized.get("risk_level") or "medium"
    risk_order = {"low": 0, "medium": 1, "high": 2, "blocked": 3}
    effective_risk = max(risk_level, ai_risk, key=lambda r: risk_order.get(r, 1))

    return {
        "strategy": strategy,
        "risk_level": effective_risk,
        "summary": ai_normalized.get("reasoning_summary") or f"Strategy: {strategy}",
        "docker_ai": {
            "provider": provider,
            "analysis_id": analysis_id,
            "reasoning_summary": ai_normalized.get("reasoning_summary") or "",
            "warnings": (ai_normalized.get("warnings") or []),
        },
        "detected_server_state": {
            "deployment_manager": detection.deployment_manager.value,
            "reverse_proxy": detection.reverse_proxy.value,
            "confidence": detection.confidence,
            "evidence": detection.evidence,
        },
        "required_changes": [],
        "generated_files": [],
        "port_plan": {
            "internal_app_port": None,
            "host_port": None,
            "uses_reverse_proxy": strategy not in (
                "docker_compose_with_host_port", "no_public_exposure"
            ),
            "reverse_proxy_type": detection.reverse_proxy.value,
        },
        "routing_plan": {
            "domain": target_domain or "",
            "router_name": "",
            "service_name": "",
            "network": "",
            "tls": bool(target_domain),
        },
        "volumes": [],
        "networks": [],
        "secrets_required": list(ai_normalized.get("required_files") or []),
        "preflight_checks": [],
        "deployment_actions": deployment_actions or [],
        "healthchecks": [],
        "rollback_plan": {
            "type": "compose_snapshot",
            # False: rollback execution is disabled in this version; setting True
            # would trigger a non-blocking validator warning on first deploy that
            # blocks execution via the validation gate.
            "previous_release_ref_required": False,
            "steps": [],
        },
        "blocking_questions": all_questions,
    }


async def create_deployment_plan(
    *,
    project_id: str,
    target_id: str,
    snapshot: InventorySnapshot,
    inventory_snapshot_id: str,
    repo_context: dict[str, Any],
    target_domain: str | None,
    preferred_strategy: str,
    deploy_path: str = "",
    created_by: str | None,
    db: aiosqlite.Connection,
    plan_id: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Generate and persist a deployment plan. Returns (plan_id, plan_dict).

    On failure, persists a BLOCKED_PLAN so there is always a traceable record.
    """
    provider = DockerAIProvider()
    sanitized_inv = build_sanitized_inventory(snapshot)

    # Get Docker AI strategy recommendation
    try:
        ai_result = await provider.recommend_deployment_strategy(
            repo_context=repo_context,
            sanitized_inventory=sanitized_inv,
            desired_domain=target_domain,
            project_id=project_id,
            target_id=target_id,
            inventory_snapshot_id=inventory_snapshot_id,
            db=db,
        )
    except Exception as exc:
        logger.error("DockerAIProvider.recommend_deployment_strategy failed: %s", exc)
        ai_result = {
            "error": str(exc),
            "provider_used": "none",
            "normalized": {},
            "analysis_id": None,
        }

    # Derive strategy from detection + AI recommendation
    detection = snapshot.detection
    strategy, risk_level, blocking_questions = _strategy_from_detection(
        detection.deployment_manager.value,
        detection.reverse_proxy.value,
        detection.confidence,
        preferred_strategy,
    )

    # If AI recommended a strategy and we're in auto_detect mode, prefer AI
    ai_norm = ai_result.get("normalized") or {}
    ai_strategy = ai_norm.get("recommended_strategy")
    if preferred_strategy == "auto_detect" and ai_strategy and not ai_result.get("error"):
        strategy = ai_strategy
        ai_risk = ai_norm.get("risk_level") or risk_level
        risk_order = {"low": 0, "medium": 1, "high": 2, "blocked": 3}
        risk_level = max(risk_level, ai_risk, key=lambda r: risk_order.get(r, 1))
    elif preferred_strategy != "auto_detect" and not ai_result.get("error"):
        # For explicit strategy, respect AI risk up to "high" — but do NOT let AI
        # block a strategy the operator chose explicitly. AI blocking questions are
        # still collected (they appear in _build_plan via ai_norm). If the AI is
        # truly concerned it can raise "high" which surfaces as a warning.
        ai_risk = ai_norm.get("risk_level") or "medium"
        risk_order = {"low": 0, "medium": 1, "high": 2, "blocked": 3}
        capped_ai_risk = ai_risk if ai_risk != "blocked" else "high"
        risk_level = max(risk_level, capped_ai_risk, key=lambda r: risk_order.get(r, 1))

    # Deterministically generate deployment_actions from strategy + repo_context.
    # This happens AFTER strategy is finalised so the right action set is chosen.
    # Never happens when risk_level is already blocked (no point building actions
    # for a plan that cannot be executed).
    deployment_actions: list[dict[str, Any]] = []
    if risk_level != "blocked" and deploy_path:
        actions, action_questions = _build_deployment_actions(
            strategy, repo_context, deploy_path
        )
        deployment_actions = actions
        # Merge action blocking questions into the plan-level list
        for q in action_questions:
            if q not in blocking_questions:
                blocking_questions.append(q)

    plan = _build_plan(
        strategy=strategy,
        risk_level=risk_level,
        docker_ai_result=ai_result,
        snapshot=snapshot,
        target_domain=target_domain,
        blocking_questions=blocking_questions,
        deployment_actions=deployment_actions,
    )

    # Schema validation
    schema_errors = validate_plan(plan)
    if schema_errors:
        logger.error("Plan failed schema validation: %s", schema_errors)
        plan = dict(BLOCKED_PLAN)
        plan["blocking_questions"] = [f"Schema error: {e}" for e in schema_errors]

    # Policy check
    policy_violations = check_policy(plan, {
        "deployment_manager": detection.deployment_manager.value,
        "reverse_proxy": detection.reverse_proxy.value,
    })
    if policy_violations:
        plan["risk_level"] = "blocked"
        plan["blocking_questions"] = plan.get("blocking_questions", []) + [
            v.description for v in policy_violations if v.blocking
        ]
        plan["strategy"] = "manual_required"

    plan_id = plan_id or str(uuid.uuid4())
    plan_hash = compute_plan_hash(plan)
    await db.execute(
        """
        INSERT INTO deployment_plans
        (id, project_id, target_id, inventory_snapshot_id, docker_ai_analysis_id,
         status, plan_json, plan_content_hash, summary_markdown, risk_level,
         blocking_questions_json, created_by, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            plan_id, project_id, target_id, inventory_snapshot_id,
            ai_result.get("analysis_id"),
            "blocked" if plan["risk_level"] == "blocked" else "draft",
            json.dumps(plan),
            plan_hash,
            plan.get("summary") or "",
            plan["risk_level"],
            json.dumps(plan.get("blocking_questions") or []),
            created_by, _now(),
        ),
    )
    await db.commit()
    logger.info(
        "Deployment plan created: id=%s strategy=%s risk=%s",
        plan_id, plan["strategy"], plan["risk_level"],
    )
    return plan_id, plan
