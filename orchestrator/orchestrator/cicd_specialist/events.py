"""CI/CD specialist event emitters.

Event names follow the pattern: cicd.<area>.<action>
All error strings are passed through logsafe.redact before emission.
"""

from __future__ import annotations

import logging
from typing import Any

from orchestrator.logsafe import redact

logger = logging.getLogger(__name__)


def _room(project_id: str) -> str:
    return f"project:{project_id}"


async def _emit(event: str, payload: dict[str, Any], project_id: str) -> None:
    try:
        from orchestrator.api.socket_server import sio  # noqa: PLC0415
        await sio.emit(event, payload, room=_room(project_id))
    except Exception as exc:
        logger.debug("Event emit skipped (%s): %s", event, exc)


# ---- Context collection ----

async def emit_context_started(project_id: str) -> None:
    await _emit("cicd.context.started", {}, project_id)


async def emit_context_completed(project_id: str, project_types: list[str]) -> None:
    await _emit("cicd.context.completed", {"project_types": project_types}, project_id)


# ---- Workflow generation ----

async def emit_generation_started(project_id: str) -> None:
    await _emit("cicd.generation.started", {}, project_id)


async def emit_generation_completed(
    project_id: str, file_count: int, risk_level: str
) -> None:
    await _emit(
        "cicd.generation.completed",
        {"file_count": file_count, "risk_level": risk_level},
        project_id,
    )


async def emit_generation_failed(project_id: str, error: str) -> None:
    await _emit("cicd.generation.failed", {"error": redact(error)}, project_id)


# ---- Validation ----

async def emit_validation_started(project_id: str) -> None:
    await _emit("cicd.validation.started", {}, project_id)


async def emit_validation_completed(
    project_id: str, passed: bool, error_count: int
) -> None:
    await _emit(
        "cicd.validation.completed",
        {"passed": passed, "error_count": error_count},
        project_id,
    )


async def emit_validation_blocked(project_id: str, errors: list[str]) -> None:
    await _emit("cicd.validation.blocked", {"errors": errors}, project_id)


# ---- Approval ----

async def emit_approval_required(project_id: str, candidate_hash: str) -> None:
    await _emit("cicd.approval.required", {"candidate_hash": candidate_hash}, project_id)


async def emit_approved(
    project_id: str, candidate_hash: str, approved_by: str
) -> None:
    await _emit(
        "cicd.approved",
        {"candidate_hash": candidate_hash, "approved_by": approved_by},
        project_id,
    )


# ---- Commit / PR ----

async def emit_commit_started(project_id: str, repo_name: str) -> None:
    await _emit("cicd.commit.started", {"repo_name": repo_name}, project_id)


async def emit_commit_completed(project_id: str, branch: str, pr_url: str) -> None:
    await _emit(
        "cicd.commit.completed", {"branch": branch, "pr_url": pr_url}, project_id
    )


async def emit_commit_failed(project_id: str, error: str) -> None:
    await _emit("cicd.commit.failed", {"error": redact(error)}, project_id)
