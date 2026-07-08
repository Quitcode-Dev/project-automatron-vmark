"""WebSocket (Socket.IO) event handlers and graph emit helpers."""

from __future__ import annotations

import asyncio
import logging
import uuid
from urllib.parse import parse_qs

from orchestrator.api.socket_server import sio
from orchestrator.models.project import get_project, save_chat_message
from orchestrator.observability import trace_event

logger = logging.getLogger(__name__)

# Stages where chat is unavailable — there is no plan to act on yet, so the UI
# disables the chat input and the backend refuses to route these to the issue
# classifier. "intake" covers the pre-plan window (project_stage stays "intake"
# during the architect run and only promotes to "awaiting_plan_approval" after it
# finishes). "planning" is reserved: the frontend uses it optimistically and the
# backend may persist it mid-run in future — routing must stay inert if so.
_PLANNING_STAGES = {"intake", "planning"}


def _project_room(project_id: str) -> str:
    return f"project:{project_id}"


@sio.on("connect")
async def on_connect(sid: str, environ: dict) -> bool | None:
    # Validate the Auth.js session cookie before accepting the socket. Returning
    # False causes socket.io to refuse the connection. The web-ui sends the
    # cookie automatically since the socket origin matches the page origin.
    from orchestrator.auth import authenticate_socketio_environ
    session = authenticate_socketio_environ(environ)
    if session is None:
        logger.warning("Socket connect rejected (sid=%s): no/invalid auth cookie", sid)
        return False

    query = environ.get("QUERY_STRING", "")
    params = parse_qs(query)
    project_id = (params.get("projectId") or params.get("project_id") or [None])[0]
    if project_id:
        await sio.enter_room(sid, _project_room(project_id))
    logger.info("Client connected: %s (user=%s)", sid, session.get("email", "anon"))
    return None


@sio.on("disconnect")
async def on_disconnect(sid: str) -> None:
    logger.info("Client disconnected: %s", sid)


@sio.on("join")
async def on_join(sid: str, data: dict) -> None:
    project_id = data.get("project_id") or data.get("projectId")
    if project_id:
        await sio.enter_room(sid, _project_room(project_id))


@sio.on("leave")
async def on_leave(sid: str, data: dict) -> None:
    project_id = data.get("project_id") or data.get("projectId")
    if project_id:
        await sio.leave_room(sid, _project_room(project_id))


@sio.on("chat:message")
async def on_chat_message(sid: str, data: dict) -> None:
    project_id = data.get("project_id") or data.get("projectId")
    text = data.get("message") or data.get("text") or ""
    if not project_id or not text:
        return

    # Resolve the project BEFORE persisting or routing. A missing or soft-deleted
    # project must not spawn orphan chat rows or reach the issue-creating classifier
    # (the UI "delete" is a soft delete: status="deleted", project_stage="error").
    project = await get_project(project_id)
    if project is None or project.get("status") == "deleted":
        logger.info("chat: message rejected — project missing/deleted (%s)", project_id)
        await emit_error(project_id, "This project no longer exists. Reload the page.")
        return

    stage = project.get("project_stage", "intake")

    await save_chat_message(str(uuid.uuid4()), project_id, "user", text)
    await trace_event(
        project_id,
        "operator",
        "chat.message.received",
        {"text": text},
    )

    # Pre-planning (intake/planning): chat is unavailable until a plan exists. The UI
    # disables the input in these stages; this branch is a defensive fallback and
    # deliberately does NOT feed the message into planning (the planner reads the repo
    # README/docs, not chat).
    if stage in _PLANNING_STAGES:
        logger.info("chat: ignored — chat unavailable pre-plan (stage=%s)", stage)
        await emit_architect_message(
            project_id,
            "Chat opens once your plan is ready to review. I'm assembling the plan "
            "from your repo's README and /docs — check the Plan tab shortly.",
        )
        return

    # Plan generated, awaiting your approval: guide the user, but do NOT create issues
    # yet — issue creation is what approving the plan triggers.
    if stage == "awaiting_plan_approval":
        logger.info("chat: held — awaiting plan approval (stage=%s)", stage)
        await emit_architect_message(
            project_id,
            "Your plan is ready for review. Open the Plan tab to edit it, or approve "
            "it to create GitHub issues. Once approved, message me here and I'll turn "
            "requests like this into issues.",
        )
        return

    # Post-approval (building, etc.): route to the feedback classifier. Show a
    # "thinking" indicator while it runs in the background so the socket handler
    # returns immediately; the classifier clears the indicator on reply.
    await emit_architect_thinking(project_id, True)
    from orchestrator.orchestrator import GitHubOrchestrator
    logger.info("chat: routing message to feedback classifier (stage=%s)", stage)

    async def _run_feedback() -> None:
        try:
            await GitHubOrchestrator(project_id).process_feedback_message(text)
        except Exception:
            logger.exception("feedback classifier failed for %s", project_id)
            await emit_architect_message(
                project_id,
                "Sorry — I hit an error handling that. Please try rephrasing, or check the Activity tab.",
            )
        finally:
            await emit_architect_thinking(project_id, False)

    asyncio.create_task(_run_feedback())


async def emit_architect_message(project_id: str, content: str, streaming: bool = False) -> None:
    # Persist complete (non-streaming) architect replies so the conversation
    # survives a page reload. Streaming chunks are transient and not saved.
    if not streaming and content.strip():
        await save_chat_message(str(uuid.uuid4()), project_id, "architect", content)
    await sio.emit(
        "architect:message",
        {
            "project_id": project_id,
            "content": content,
            "is_streaming": streaming,
        },
        room=_project_room(project_id),
    )


async def emit_architect_thinking(project_id: str, thinking: bool) -> None:
    """Tell the UI the Architect is (or is no longer) working on a reply."""
    await sio.emit(
        "architect:thinking",
        {"project_id": project_id, "thinking": thinking},
        room=_project_room(project_id),
    )


async def emit_architect_chunk(project_id: str, chunk: str) -> None:
    """Emit a single streaming token from the architect for real-time UI updates."""
    await sio.emit(
        "architect:message",
        {
            "project_id": project_id,
            "content": chunk,
            "is_streaming": True,
        },
        room=_project_room(project_id),
    )


async def emit_builder_log(
    project_id: str,
    *,
    task_index: int,
    task_text: str,
    output: str,
    status: str,
) -> None:
    await sio.emit(
        "builder:log",
        {
            "project_id": project_id,
            "task_index": task_index,
            "task_text": task_text,
            "output": output,
            "status": status,
        },
        room=_project_room(project_id),
    )


async def emit_status_update(
    project_id: str,
    *,
    status: str,
    stage: str,
    progress: dict,
    preview_url: str | None = None,
) -> None:
    payload = {
        "project_id": project_id,
        "status": status,
        "stage": stage,
        "progress": progress,
    }
    if preview_url:
        payload["preview_url"] = preview_url
    await sio.emit("status:update", payload, room=_project_room(project_id))


async def emit_human_required(project_id: str, reason: str, *, stage: str | None = None) -> None:
    payload = {"project_id": project_id, "reason": reason}
    if stage:
        payload["stage"] = stage
    await sio.emit("human:required", payload, room=_project_room(project_id))


async def emit_error(project_id: str, message: str, *, stage: str = "error") -> None:
    await sio.emit(
        "run:error",
        {"project_id": project_id, "message": message, "stage": stage},
        room=_project_room(project_id),
    )


async def emit_plan_updated(project_id: str, plan_md: str) -> None:
    await sio.emit(
        "plan:updated",
        {"project_id": project_id, "plan_md": plan_md},
        room=_project_room(project_id),
    )


async def emit_issues_updated(project_id: str, issues: list) -> None:
    """Fires after apply_plan and sync_issues with the current issue list."""
    await sio.emit(
        "issues:updated",
        {"project_id": project_id, "issues": issues},
        room=_project_room(project_id),
    )


async def emit_pr_review_ready(
    project_id: str,
    issue_number: int,
    pr_number: int,
    passed: bool,
    summary: str,
) -> None:
    """Fires after an AI PR review completes."""
    await sio.emit(
        "pr:review_ready",
        {
            "project_id": project_id,
            "issue_number": issue_number,
            "pr_number": pr_number,
            "passed": passed,
            "summary": summary,
        },
        room=_project_room(project_id),
    )


async def emit_build_failed(project_id: str, error_summary: str, default_branch: str) -> None:
    """Fires when a post-merge build check fails — lets the frontend ask the user before creating an issue."""
    await sio.emit(
        "build:failed",
        {
            "project_id": project_id,
            "error_summary": error_summary,
            "default_branch": default_branch,
        },
        room=_project_room(project_id),
    )


async def emit_build_passed(project_id: str, default_branch: str) -> None:
    """Fires when a post-merge build check passes."""
    await sio.emit(
        "build:passed",
        {"project_id": project_id, "default_branch": default_branch},
        room=_project_room(project_id),
    )
