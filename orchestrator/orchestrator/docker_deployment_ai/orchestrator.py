"""DockerDeploymentOrchestrator — product-level coordinator.

Wires together the individual Scope 1-6 components into the end-to-end flow:
  target registration
  → read-only inventory
  → Docker AI / Gordon analysis
  → strict deployment_plan.json
  → deterministic validation
  → human approval (enforced in execute)
  → typed execution
  → healthcheck
  → rollback metadata

This is the only class that API routes should call. Internal components
(GordonClient, InventoryCollector, Planner, etc.) stay internal.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

import aiosqlite

from orchestrator.docker_deployment_ai.docker_ai_provider import DockerAIProvider
from orchestrator.docker_deployment_ai.plan_hash import compute_plan_hash
from orchestrator.docker_deployment_ai.events import (
    emit_approval_required,
    emit_approved,
    emit_docker_ai_completed,
    emit_docker_ai_failed,
    emit_docker_ai_started,
    emit_inventory_completed,
    emit_inventory_failed,
    emit_inventory_started,
    emit_plan_blocked,
    emit_plan_completed,
    emit_plan_failed,
    emit_plan_started,
    emit_validation_blocked,
    emit_validation_completed,
    emit_validation_started,
)
from orchestrator.docker_deployment_ai.executor import execute_plan
from orchestrator.docker_deployment_ai.gordon_client import GordonClient
from orchestrator.docker_deployment_ai.healthcheck import run_healthcheck
from orchestrator.docker_deployment_ai.inventory import build_sanitized_inventory, collect_inventory
from orchestrator.docker_deployment_ai.models import DeploymentTarget, DeploymentTargetCreate
from orchestrator.docker_deployment_ai.planner import create_deployment_plan
from orchestrator.docker_deployment_ai.rollback import capture_rollback_metadata, execute_rollback
from orchestrator.docker_deployment_ai.target_store import (
    create_target,
    get_target,
    list_targets,
)
from orchestrator.docker_deployment_ai.validator import validate_deployment_plan

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DockerDeploymentOrchestrator:
    """Single entry-point for all Docker deployment intelligence operations."""

    # ---- Target management ----

    async def register_target(
        self,
        data: DeploymentTargetCreate,
        created_by: str | None,
        db: aiosqlite.Connection,
    ) -> DeploymentTarget:
        return await create_target(db, data, created_by=created_by)

    async def get_target(self, target_id: str, db: aiosqlite.Connection) -> DeploymentTarget | None:
        return await get_target(db, target_id)

    async def list_targets(self, project_id: str, db: aiosqlite.Connection) -> list[DeploymentTarget]:
        return await list_targets(db, project_id)

    # ---- Inventory ----

    async def run_inventory(
        self,
        target_id: str,
        project_id: str,
        db: aiosqlite.Connection,
    ) -> dict[str, Any]:
        target = await get_target(db, target_id)
        if not target:
            return {"error": f"Target {target_id} not found"}
        await emit_inventory_started(project_id, target_id)
        try:
            snapshot_id, snapshot = await collect_inventory(target, db)
            detection = {
                "deployment_manager": snapshot.detection.deployment_manager.value,
                "reverse_proxy": snapshot.detection.reverse_proxy.value,
                "confidence": snapshot.detection.confidence,
                "evidence": snapshot.detection.evidence,
            }
            await emit_inventory_completed(project_id, target_id, snapshot_id, detection)
            return {"snapshot_id": snapshot_id, "detection": detection, "error": snapshot.error}
        except Exception as exc:
            error = str(exc)
            await emit_inventory_failed(project_id, target_id, error)
            return {"error": error}

    async def get_latest_inventory(self, target_id: str, db: aiosqlite.Connection) -> dict[str, Any] | None:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM deployment_inventory_snapshots WHERE target_id = ? ORDER BY created_at DESC LIMIT 1",
            (target_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        data = dict(row)
        try:
            data["raw_json"] = json.loads(data.get("raw_json") or "{}")
            data["summary_json"] = json.loads(data.get("summary_json") or "{}")
        except json.JSONDecodeError:
            pass
        return data

    async def get_inventory_snapshot(self, snapshot_id: str, db: aiosqlite.Connection) -> dict[str, Any] | None:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM deployment_inventory_snapshots WHERE id = ?", (snapshot_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return None
        data = dict(row)
        try:
            data["raw_json"] = json.loads(data.get("raw_json") or "{}")
        except json.JSONDecodeError:
            pass
        return data

    # ---- Docker AI analysis ----

    async def run_docker_ai_analysis(
        self,
        target_id: str,
        project_id: str,
        analysis_type: str,
        db: aiosqlite.Connection,
    ) -> dict[str, Any]:
        target = await get_target(db, target_id)
        if not target:
            return {"error": f"Target {target_id} not found"}

        snapshot_data = await self.get_latest_inventory(target_id, db)
        if not snapshot_data:
            return {"error": "No inventory snapshot found. Run inventory first."}

        await emit_docker_ai_started(project_id, analysis_type)
        try:
            from orchestrator.docker_deployment_ai.models import InventorySnapshot  # noqa: PLC0415
            raw_json = snapshot_data.get("raw_json") or {}
            snapshot = InventorySnapshot.model_validate(raw_json)
            sanitized = build_sanitized_inventory(snapshot)

            provider = DockerAIProvider()
            result = await provider.analyze_inventory(
                sanitized_inventory=sanitized,
                project_id=project_id,
                target_id=target_id,
                inventory_snapshot_id=snapshot_data.get("id"),
                db=db,
            )
            if result.get("error"):
                await emit_docker_ai_failed(project_id, result["error"])
            else:
                await emit_docker_ai_completed(
                    project_id,
                    result.get("provider_used", "unknown"),
                    result.get("analysis_id", ""),
                    analysis_type,
                )
            return result
        except Exception as exc:
            await emit_docker_ai_failed(project_id, str(exc))
            return {"error": str(exc)}

    async def list_docker_ai_analyses(self, target_id: str, db: aiosqlite.Connection) -> list[dict[str, Any]]:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, provider, analysis_type, status, error_message, created_at "
            "FROM docker_ai_analyses WHERE target_id = ? ORDER BY created_at DESC LIMIT 20",
            (target_id,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    # ---- Plan ----

    async def create_plan(
        self,
        project_id: str,
        target_id: str,
        repo_context: dict[str, Any],
        desired_domain: str | None,
        preferred_strategy: str,
        created_by: str | None,
        db: aiosqlite.Connection,
    ) -> dict[str, Any]:
        target = await get_target(db, target_id)
        if not target:
            return {"error": f"Target {target_id} not found"}

        snapshot_data = await self.get_latest_inventory(target_id, db)
        if not snapshot_data:
            return {"error": "No inventory snapshot found. Run inventory first."}

        await emit_plan_started(project_id, target_id)
        try:
            from orchestrator.docker_deployment_ai.models import InventorySnapshot  # noqa: PLC0415
            raw_json = snapshot_data.get("raw_json") or {}
            snapshot = InventorySnapshot.model_validate(raw_json)

            plan_id, plan = await create_deployment_plan(
                project_id=project_id,
                target_id=target_id,
                snapshot=snapshot,
                inventory_snapshot_id=snapshot_data["id"],
                repo_context=repo_context,
                target_domain=desired_domain,
                preferred_strategy=preferred_strategy,
                deploy_path=target.deploy_path,
                created_by=created_by,
                db=db,
            )
            if plan.get("risk_level") == "blocked":
                await emit_plan_blocked(project_id, plan_id, plan.get("blocking_questions") or [])
            else:
                await emit_plan_completed(project_id, plan_id, plan["strategy"], plan["risk_level"])
            return {"plan_id": plan_id, "plan": plan}
        except Exception as exc:
            await emit_plan_failed(project_id, str(exc))
            return {"error": str(exc)}

    async def get_plan(self, plan_id: str, db: aiosqlite.Connection) -> dict[str, Any] | None:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM deployment_plans WHERE id = ?", (plan_id,))
        row = await cursor.fetchone()
        if not row:
            return None
        data = dict(row)
        try:
            data["plan_json"] = json.loads(data.get("plan_json") or "{}")
            data["blocking_questions_json"] = json.loads(data.get("blocking_questions_json") or "[]")
        except json.JSONDecodeError:
            pass
        return data

    # ---- Validation ----

    async def validate_plan(self, plan_id: str, db: aiosqlite.Connection) -> dict[str, Any]:
        plan_data = await self.get_plan(plan_id, db)
        if not plan_data:
            return {"error": f"Plan {plan_id} not found"}

        plan = plan_data.get("plan_json") or {}
        target_id = plan_data.get("target_id")
        snapshot_data = await self.get_latest_inventory(target_id, db) if target_id else None

        await emit_validation_started(plan_data.get("project_id", ""), plan_id)

        from orchestrator.docker_deployment_ai.models import InventorySnapshot  # noqa: PLC0415
        if snapshot_data:
            raw_json = snapshot_data.get("raw_json") or {}
            snapshot = InventorySnapshot.model_validate(raw_json)
        else:
            snapshot = InventorySnapshot(target_id=target_id or "", project_id="")

        result = validate_deployment_plan(plan, snapshot)
        result_dict = result.to_dict()

        # P0-4: store plan_content_hash alongside validation result
        plan_hash = compute_plan_hash(plan)
        validation_id = str(uuid.uuid4())
        project_id = plan_data.get("project_id", "")
        await db.execute(
            """
            INSERT INTO deployment_plan_validations
            (id, plan_id, status, plan_content_hash, checks_json,
             blocking_errors_json, warnings_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                validation_id, plan_id, result.status, plan_hash,
                json.dumps(result_dict["checks"]),
                json.dumps(result.blocking_errors),
                json.dumps(result.warnings),
                _now(),
            ),
        )
        await db.commit()

        if result.status == "blocked":
            await emit_validation_blocked(project_id, plan_id, result.blocking_errors)
        else:
            await emit_validation_completed(project_id, plan_id, result.status)

        return result_dict

    # ---- Approval ----

    async def _check_validation_allows_execution(
        self, plan_id: str, plan_hash: str, db: aiosqlite.Connection
    ) -> tuple[bool, str]:
        """Return (ok, error_msg).

        Execution is allowed when:
          - A validation result exists for this plan_id.
          - The latest result has status 'passed' OR status 'warning' with no
            blocking_errors (warnings are non-blocking issues only).
          - The plan_content_hash matches the current plan (no mutation since validation).

        Status 'blocked' always refuses execution regardless of blocking_errors content.
        Status 'warning' with any blocking_errors also refuses — that state should not
        occur by design (validator sets 'blocked' when blocking_errors is non-empty)
        but we guard defensively.
        """
        cursor = await db.execute(
            """
            SELECT status, plan_content_hash, blocking_errors_json
            FROM deployment_plan_validations
            WHERE plan_id = ?
            ORDER BY created_at DESC LIMIT 1
            """,
            (plan_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return False, (
                "No validation result found for this plan. "
                "Call POST /deployment-plans/{plan_id}/validate before executing."
            )
        status, validated_hash, blocking_errors_json = row

        if status == "blocked":
            return False, (
                "Latest validation status is 'blocked' — execution requires 'passed' "
                "or 'warning' (with no blocking errors). Resolve all blocking errors "
                "and re-validate."
            )

        if status == "warning":
            import json as _json  # noqa: PLC0415
            blocking_errors: list[str] = _json.loads(blocking_errors_json or "[]")
            if blocking_errors:
                # Defensive: warning + blocking_errors is an inconsistent state.
                # Refuse execution so the operator re-validates with a clean result.
                return False, (
                    "Validation status is 'warning' but blocking errors are present. "
                    "Re-validate to obtain a consistent result before executing."
                )

        if status not in ("passed", "warning"):
            return False, (
                f"Latest validation status is '{status}' — execution requires "
                "'passed' or 'warning' with no blocking errors."
            )

        if validated_hash and validated_hash != plan_hash:
            return False, (
                "Plan content has changed since validation was run. "
                "Re-validate the current plan before executing."
            )
        return True, ""

    # Keep old name as alias so any existing callers (tests) still work.
    _check_validation_passed = _check_validation_allows_execution

    async def approve_plan(
        self,
        plan_id: str,
        approved_by: str,
        approval_note: str | None,
        db: aiosqlite.Connection,
    ) -> dict[str, Any]:
        plan_data = await self.get_plan(plan_id, db)
        if not plan_data:
            return {"error": f"Plan {plan_id} not found"}

        if plan_data.get("risk_level") == "blocked":
            return {"error": "Cannot approve a blocked plan. Resolve all blocking issues first."}

        # P0-4: bind approval to current plan content hash
        plan = plan_data.get("plan_json") or {}
        plan_hash = compute_plan_hash(plan)

        approval_id = str(uuid.uuid4())
        now = _now()
        await db.execute(
            """
            INSERT INTO deployment_approvals
            (id, plan_id, plan_content_hash, approved_by, approved_at, approval_note)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (approval_id, plan_id, plan_hash, approved_by, now, approval_note),
        )
        await db.execute(
            "UPDATE deployment_plans SET status = 'approved' WHERE id = ?", (plan_id,)
        )
        await db.commit()
        project_id = plan_data.get("project_id", "")
        await emit_approved(project_id, plan_id, approved_by)
        return {"approval_id": approval_id, "plan_id": plan_id, "approved_by": approved_by}

    # ---- Execute ----

    async def _check_approved_hash(self, plan_id: str, plan_hash: str, db: aiosqlite.Connection) -> tuple[bool, str]:
        """Return (ok, error_msg). Verifies approval exists and hash matches current plan."""
        cursor = await db.execute(
            """
            SELECT plan_content_hash
            FROM deployment_approvals
            WHERE plan_id = ?
            ORDER BY approved_at DESC LIMIT 1
            """,
            (plan_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return False, (
                "Deployment approval is required before execution. "
                "Call POST /deployment-plans/{plan_id}/approve."
            )
        approved_hash = row[0]
        if approved_hash and approved_hash != plan_hash:
            return False, (
                "Plan content has changed since approval. "
                "Re-approve the current plan before executing."
            )
        return True, ""

    async def prepare_run(
        self,
        plan_id: str,
        started_by: str,
        db: aiosqlite.Connection,
    ) -> dict[str, Any]:
        """Run all pre-execution gates and create the deploy_runs row.

        This is the SYNCHRONOUS part of execution — it must complete before the
        API response is sent so the returned run_id is immediately readable.

        Returns one of:
          {"run_id": ..., "plan": ..., "target": ..., "project_id": ..., "plan_id": ...}
            — gates passed, run row committed to DB.
          {"status": "rejected", "error": ..., "http_status": 409}
            — a gate failed; no row was created.
        """
        plan_data = await self.get_plan(plan_id, db)
        if not plan_data:
            return {"status": "rejected", "error": f"Plan {plan_id} not found",
                    "http_status": 404}

        plan = plan_data.get("plan_json") or {}
        target_id = plan_data.get("target_id")
        project_id = plan_data.get("project_id", "")
        target = await get_target(db, target_id) if target_id else None
        if not target:
            return {"status": "rejected", "error": "Deployment target not found",
                    "http_status": 404}

        # P0-3: validation gate
        plan_hash = compute_plan_hash(plan)
        valid_ok, valid_err = await self._check_validation_allows_execution(plan_id, plan_hash, db)
        if not valid_ok:
            return {"status": "rejected", "error": valid_err, "http_status": 409}

        # P0-4: approval hash gate
        approved_ok, approved_err = await self._check_approved_hash(plan_id, plan_hash, db)
        if not approved_ok:
            return {"status": "rejected", "error": approved_err, "http_status": 409}

        # Risk level gate
        if plan.get("risk_level") == "blocked":
            return {"status": "rejected",
                    "error": "Plan has risk_level=blocked and cannot be executed.",
                    "http_status": 409}

        # Create run row — committed before this method returns so the run_id
        # is readable by any concurrent GET before execution starts.
        run_id = str(uuid.uuid4())
        now = _now()
        await db.execute(
            """
            INSERT INTO deploy_runs (id, project_id, status, branch, created_at,
              plan_id, target_id, started_by, started_at, health_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (run_id, project_id, "pending", "main", now,
             plan_id, target_id, started_by, now, "unknown"),
        )
        await db.commit()
        logger.info(
            "Execution prepared: run_id=%s plan_id=%s started_by=%s",
            run_id, plan_id, started_by,
        )
        return {
            "run_id": run_id,
            "plan": plan,
            "plan_id": plan_id,
            "target": target,
            "project_id": project_id,
        }

    async def execute_prepared_run(
        self,
        run_id: str,
        plan: dict[str, Any],
        plan_id: str,
        target: Any,
        project_id: str,
        started_by: str,
        db: aiosqlite.Connection,
    ) -> dict[str, Any]:
        """Execute a run whose row was already created by prepare_run.

        This is the BACKGROUND part of execution — SSH calls, rollback capture,
        healthcheck. The run row already exists in DB when this method starts.
        """
        # Capture rollback metadata before execution (best-effort; non-blocking)
        try:
            await capture_rollback_metadata(
                run_id=run_id, plan=plan, target=target, db=db
            )
        except Exception as exc:
            logger.warning("Rollback metadata capture failed: %s", exc)

        result = await execute_plan(
            plan_id=plan_id,
            plan=plan,
            target=target,
            run_id=run_id,
            project_id=project_id,
            started_by=started_by,
            db=db,
        )

        # Healthcheck after successful execution
        if result.get("status") == "completed":
            try:
                await run_healthcheck(
                    run_id=run_id,
                    project_id=project_id,
                    plan=plan,
                    target=target,
                    db=db,
                )
            except Exception as exc:
                logger.warning("Healthcheck failed: %s", exc)

        return {"run_id": run_id, **result}

    async def execute_plan(
        self,
        plan_id: str,
        started_by: str,
        db: aiosqlite.Connection,
        *,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """Legacy single-method execute — kept for backwards compatibility with tests.

        New callers should use prepare_run + execute_prepared_run.
        """
        prep = await self.prepare_run(plan_id, started_by, db)
        if "error" in prep:
            return {"status": prep.get("status", "rejected"), "error": prep["error"]}
        # If an external run_id was supplied (old API path), ignore it and use the
        # one created by prepare_run — the row already exists at that id.
        return await self.execute_prepared_run(
            run_id=prep["run_id"],
            plan=prep["plan"],
            plan_id=prep["plan_id"],
            target=prep["target"],
            project_id=prep["project_id"],
            started_by=started_by,
            db=db,
        )

    # ---- Run queries ----

    async def get_run(self, run_id: str, db: aiosqlite.Connection) -> dict[str, Any] | None:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM deploy_runs WHERE id = ?", (run_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_run_steps(self, run_id: str, db: aiosqlite.Connection) -> list[dict[str, Any]]:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM deployment_run_steps WHERE run_id = ? ORDER BY step_index",
            (run_id,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    # ---- Rollback ----

    async def rollback_run(
        self,
        run_id: str,
        project_id: str,
        db: aiosqlite.Connection,
    ) -> dict[str, Any]:
        run = await self.get_run(run_id, db)
        if not run:
            return {"error": f"Run {run_id} not found"}
        target_id = run.get("target_id")
        target = await get_target(db, target_id) if target_id else None
        if not target:
            return {"error": "Deployment target not found"}
        return await execute_rollback(
            run_id=run_id, project_id=project_id, target=target, db=db
        )

    # ---- Gordon availability probe ----

    async def gordon_status(self) -> dict[str, Any]:
        client = GordonClient()
        available = await client.is_available()
        return {"available": available, "provider": "gordon" if available else None}
