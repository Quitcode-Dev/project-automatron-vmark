"""Deployment event emitters — thin wrappers around the existing Socket.IO pattern.

All payloads are passed through `logsafe.redact` before emission to ensure
secrets never leak over WebSocket connections.

Event naming follows the spec §24 schema: `deployment.<area>.<action>`.
Callers import and call the individual `emit_*` helpers — no raw sio.emit
should be used directly from the docker_deployment_ai module.
"""

from __future__ import annotations

import logging
from typing import Any

from orchestrator.logsafe import redact

logger = logging.getLogger(__name__)


def _project_room(project_id: str) -> str:
    return f"project:{project_id}"


def _safe(payload: dict[str, Any]) -> dict[str, Any]:
    """Recursively redact string values in payload."""
    result: dict[str, Any] = {}
    for k, v in payload.items():
        if isinstance(v, str):
            result[k] = redact(v)
        elif isinstance(v, dict):
            result[k] = _safe(v)
        elif isinstance(v, list):
            result[k] = [redact(i) if isinstance(i, str) else i for i in v]
        else:
            result[k] = v
    return result


async def _emit(event: str, payload: dict[str, Any], project_id: str) -> None:
    try:
        from orchestrator.api.socket_server import sio  # noqa: PLC0415
        await sio.emit(event, _safe(payload), room=_project_room(project_id))
    except Exception as exc:
        logger.debug("Event emit failed (%s): %s", event, exc)


# ---- Inventory ----

async def emit_inventory_started(project_id: str, target_id: str) -> None:
    await _emit("deployment.inventory.started", {"target_id": target_id}, project_id)


async def emit_inventory_completed(project_id: str, target_id: str, snapshot_id: str, detection: dict[str, Any]) -> None:
    await _emit("deployment.inventory.completed", {
        "target_id": target_id, "snapshot_id": snapshot_id, "detection": detection,
    }, project_id)


async def emit_inventory_failed(project_id: str, target_id: str, error: str) -> None:
    await _emit("deployment.inventory.failed", {"target_id": target_id, "error": redact(error)}, project_id)


# ---- Docker AI ----

async def emit_docker_ai_started(project_id: str, analysis_type: str) -> None:
    await _emit("deployment.docker_ai.started", {"analysis_type": analysis_type}, project_id)


async def emit_docker_ai_completed(project_id: str, provider: str, analysis_id: str, analysis_type: str) -> None:
    await _emit("deployment.docker_ai.completed", {
        "provider": provider, "analysis_id": analysis_id, "analysis_type": analysis_type,
    }, project_id)


async def emit_docker_ai_failed(project_id: str, error: str) -> None:
    await _emit("deployment.docker_ai.failed", {"error": redact(error)}, project_id)


async def emit_docker_ai_unavailable(project_id: str, provider: str) -> None:
    await _emit("deployment.docker_ai.unavailable", {"provider": provider}, project_id)


# ---- Plan ----

async def emit_plan_started(project_id: str, target_id: str) -> None:
    await _emit("deployment.plan.started", {"target_id": target_id}, project_id)


async def emit_plan_completed(project_id: str, plan_id: str, strategy: str, risk_level: str) -> None:
    await _emit("deployment.plan.completed", {
        "plan_id": plan_id, "strategy": strategy, "risk_level": risk_level,
    }, project_id)


async def emit_plan_blocked(project_id: str, plan_id: str, blocking_questions: list[str]) -> None:
    await _emit("deployment.plan.blocked", {
        "plan_id": plan_id, "blocking_questions": blocking_questions,
    }, project_id)


async def emit_plan_failed(project_id: str, error: str) -> None:
    await _emit("deployment.plan.failed", {"error": redact(error)}, project_id)


# ---- Validation ----

async def emit_validation_started(project_id: str, plan_id: str) -> None:
    await _emit("deployment.validation.started", {"plan_id": plan_id}, project_id)


async def emit_validation_completed(project_id: str, plan_id: str, status: str) -> None:
    await _emit("deployment.validation.completed", {"plan_id": plan_id, "status": status}, project_id)


async def emit_validation_blocked(project_id: str, plan_id: str, blocking_errors: list[str]) -> None:
    await _emit("deployment.validation.blocked", {
        "plan_id": plan_id, "blocking_errors": blocking_errors,
    }, project_id)


# ---- Approval ----

async def emit_approval_required(project_id: str, plan_id: str) -> None:
    await _emit("deployment.approval.required", {"plan_id": plan_id}, project_id)


async def emit_approved(project_id: str, plan_id: str, approved_by: str) -> None:
    await _emit("deployment.approved", {"plan_id": plan_id, "approved_by": approved_by}, project_id)


# ---- Run ----

async def emit_run_started(project_id: str, run_id: str, plan_id: str) -> None:
    await _emit("deployment.run.started", {"run_id": run_id, "plan_id": plan_id}, project_id)


async def emit_run_step_started(project_id: str, run_id: str, step_index: int, action_type: str) -> None:
    await _emit("deployment.run.step.started", {
        "run_id": run_id, "step_index": step_index, "action_type": action_type,
    }, project_id)


async def emit_run_step_completed(project_id: str, run_id: str, step_index: int) -> None:
    await _emit("deployment.run.step.completed", {"run_id": run_id, "step_index": step_index}, project_id)


async def emit_run_step_failed(project_id: str, run_id: str, step_index: int, error: str) -> None:
    await _emit("deployment.run.step.failed", {
        "run_id": run_id, "step_index": step_index, "error": redact(error),
    }, project_id)


async def emit_healthcheck_started(project_id: str, run_id: str) -> None:
    await _emit("deployment.run.healthcheck.started", {"run_id": run_id}, project_id)


async def emit_healthcheck_completed(project_id: str, run_id: str, status: str) -> None:
    await _emit("deployment.run.healthcheck.completed", {"run_id": run_id, "status": status}, project_id)


async def emit_run_completed(project_id: str, run_id: str, health_status: str) -> None:
    await _emit("deployment.run.completed", {"run_id": run_id, "health_status": health_status}, project_id)


async def emit_run_failed(project_id: str, run_id: str, error: str) -> None:
    await _emit("deployment.run.failed", {"run_id": run_id, "error": redact(error)}, project_id)


# ---- Rollback ----

async def emit_rollback_started(project_id: str, run_id: str) -> None:
    await _emit("deployment.rollback.started", {"run_id": run_id}, project_id)


async def emit_rollback_completed(project_id: str, run_id: str) -> None:
    await _emit("deployment.rollback.completed", {"run_id": run_id}, project_id)


async def emit_rollback_failed(project_id: str, run_id: str, error: str) -> None:
    await _emit("deployment.rollback.failed", {"run_id": run_id, "error": redact(error)}, project_id)
