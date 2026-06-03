"""API-level regression tests for P0 blockers.

Covers:
  P0-1  DB path persistence — _get_deployment_db fails fast on empty _db_path.
  P0-3  Validation before execute — execute rejects missing/blocked validation.
  P0-4  Approval plan hash — execute rejects stale approval after plan mutation.
  P0-6  SSH mutation mode — ExecutorSSHClient has no generic passthrough; typed
        builders work; unimplemented actions error without calling SSH.
  P0-7  Rollback disabled MVP — execute_rollback returns not_implemented;
        rollback_available is never set to 1.

Tests are unit / integration tests against the Python layer directly.
No HTTP server is spun up (that requires jose + full app stack).
"""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest

# Stub jose so orchestrator.api.routes can be imported in envs without it
for _jose_mod in ("jose", "jose.jwe", "jose.jwt", "jose.exceptions"):
    sys.modules.setdefault(_jose_mod, MagicMock())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _make_db() -> aiosqlite.Connection:
    """Open a fresh in-memory SQLite DB with the deployment schema."""
    db = await aiosqlite.connect(":memory:")
    # Minimal tables needed by the orchestrator tests
    await db.execute(
        """CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active'
        )"""
    )
    await db.execute(
        """CREATE TABLE IF NOT EXISTS deploy_runs (
            id TEXT PRIMARY KEY, project_id TEXT, status TEXT, branch TEXT,
            created_at TEXT, plan_id TEXT, target_id TEXT, started_by TEXT,
            started_at TEXT, health_status TEXT NOT NULL DEFAULT 'unknown',
            finished_at TEXT, current_step INTEGER, rollback_available INTEGER NOT NULL DEFAULT 0
        )"""
    )
    await db.execute(
        """CREATE TABLE IF NOT EXISTS deployment_targets (
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
            name TEXT NOT NULL, host TEXT NOT NULL, ssh_user TEXT NOT NULL,
            ssh_port INTEGER NOT NULL DEFAULT 22, environment TEXT NOT NULL DEFAULT 'production',
            domain TEXT, app_name TEXT NOT NULL, deploy_path TEXT NOT NULL,
            preferred_strategy TEXT NOT NULL DEFAULT 'auto_detect',
            auth_mode TEXT NOT NULL DEFAULT 'ssh_key', auth_reference TEXT,
            created_by TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            last_inventory_snapshot_id TEXT
        )"""
    )
    await db.execute(
        """CREATE TABLE IF NOT EXISTS deployment_plans (
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL, target_id TEXT NOT NULL,
            inventory_snapshot_id TEXT, docker_ai_analysis_id TEXT,
            status TEXT NOT NULL DEFAULT 'draft', plan_json TEXT NOT NULL DEFAULT '{}',
            plan_content_hash TEXT NOT NULL DEFAULT '',
            summary_markdown TEXT, risk_level TEXT NOT NULL DEFAULT 'medium',
            blocking_questions_json TEXT NOT NULL DEFAULT '[]',
            created_by TEXT, created_at TEXT NOT NULL
        )"""
    )
    await db.execute(
        """CREATE TABLE IF NOT EXISTS deployment_plan_validations (
            id TEXT PRIMARY KEY, plan_id TEXT NOT NULL,
            status TEXT NOT NULL, plan_content_hash TEXT NOT NULL DEFAULT '',
            checks_json TEXT NOT NULL DEFAULT '[]',
            blocking_errors_json TEXT NOT NULL DEFAULT '[]',
            warnings_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL
        )"""
    )
    await db.execute(
        """CREATE TABLE IF NOT EXISTS deployment_approvals (
            id TEXT PRIMARY KEY, plan_id TEXT NOT NULL,
            plan_content_hash TEXT NOT NULL DEFAULT '',
            approved_by TEXT NOT NULL, approved_at TEXT NOT NULL, approval_note TEXT
        )"""
    )
    await db.execute(
        """CREATE TABLE IF NOT EXISTS deployment_rollbacks (
            id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
            rollback_status TEXT NOT NULL DEFAULT 'pending',
            previous_release_ref TEXT, rollback_plan_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL, executed_at TEXT
        )"""
    )
    await db.execute(
        """CREATE TABLE IF NOT EXISTS deployment_run_steps (
            id TEXT PRIMARY KEY, run_id TEXT NOT NULL, step_index INTEGER NOT NULL,
            action_type TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
            started_at TEXT, finished_at TEXT,
            stdout_excerpt TEXT, stderr_excerpt TEXT, error_message TEXT
        )"""
    )
    await db.commit()
    return db


def _make_target_row(
    db,
    target_id: str = "tgt-1",
    project_id: str = "proj-1",
    host: str = "1.2.3.4",
    deploy_path: str = "/opt/app",
):
    return asyncio.get_event_loop().run_until_complete(
        db.execute(
            """INSERT INTO deployment_targets
            (id, project_id, name, host, ssh_user, app_name, deploy_path, created_at, updated_at)
            VALUES (?, ?, 'server', ?, 'deploy', 'app', ?, '2024-01-01', '2024-01-01')""",
            (target_id, project_id, host, deploy_path),
        )
    )


# ---------------------------------------------------------------------------
# P0-1  DB path persistence
# ---------------------------------------------------------------------------

class TestDbPathPersistence:
    """_get_deployment_db must not connect to empty string."""

    @pytest.mark.asyncio
    async def test_empty_db_path_raises_503(self):
        import orchestrator.models.project as pm  # noqa: PLC0415
        import orchestrator.api.routes as routes  # noqa: PLC0415
        original = pm._db_path
        try:
            pm._db_path = ""
            with pytest.raises(Exception) as exc_info:
                await routes._get_deployment_db()
            # Must be an HTTPException(503) — not connect to ""
            assert exc_info.value.status_code == 503
        finally:
            pm._db_path = original

    @pytest.mark.asyncio
    async def test_nonempty_db_path_returns_connection(self, tmp_path):
        """With a valid path, _get_deployment_db returns an aiosqlite connection."""
        import orchestrator.models.project as pm  # noqa: PLC0415
        import orchestrator.api.routes as routes  # noqa: PLC0415
        db_file = str(tmp_path / "test.db")
        original = pm._db_path
        try:
            pm._db_path = db_file
            conn = await routes._get_deployment_db()
            assert conn is not None
            await conn.close()
        finally:
            pm._db_path = original


# ---------------------------------------------------------------------------
# P0-3  Validation before execute
# ---------------------------------------------------------------------------

class TestValidationBeforeExecute:
    """execute_plan must reject runs that lack a passed validation."""

    @pytest.fixture
    def loop(self):
        return asyncio.new_event_loop()

    @pytest.mark.asyncio
    async def test_execute_without_validation_returns_rejected(self):
        """No validation row → execute_plan returns rejected."""
        from orchestrator.docker_deployment_ai.orchestrator import DockerDeploymentOrchestrator  # noqa: PLC0415
        db = await _make_db()
        orch = DockerDeploymentOrchestrator()
        try:
            # Insert a target and an approved plan, but no validation
            await db.execute(
                """INSERT INTO deployment_targets
                (id, project_id, name, host, ssh_user, app_name, deploy_path, created_at, updated_at)
                VALUES ('tgt-1','proj-1','server','1.2.3.4','deploy','app','/opt/app','2024-01-01','2024-01-01')"""
            )
            import json  # noqa: PLC0415
            plan_json = json.dumps({"strategy": "docker_compose_private", "risk_level": "low",
                                    "deployment_actions": []})
            from orchestrator.docker_deployment_ai.plan_hash import compute_plan_hash  # noqa: PLC0415
            plan_hash = compute_plan_hash(json.loads(plan_json))
            await db.execute(
                """INSERT INTO deployment_plans
                (id, project_id, target_id, status, plan_json, plan_content_hash,
                 risk_level, blocking_questions_json, created_at)
                VALUES ('plan-1','proj-1','tgt-1','approved',?,?,'low','[]','2024-01-01')""",
                (plan_json, plan_hash),
            )
            # Approval exists but NO validation row
            await db.execute(
                """INSERT INTO deployment_approvals
                (id, plan_id, plan_content_hash, approved_by, approved_at)
                VALUES ('appr-1','plan-1',?,'user@test.com','2024-01-01')""",
                (plan_hash,),
            )
            await db.commit()
            result = await orch.execute_plan("plan-1", "user@test.com", db)
            assert result["status"] == "rejected"
            assert "validation" in result["error"].lower() or "validate" in result["error"].lower()
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_execute_with_blocked_validation_returns_rejected(self):
        """Blocked validation → execute_plan returns rejected."""
        from orchestrator.docker_deployment_ai.orchestrator import DockerDeploymentOrchestrator  # noqa: PLC0415
        import json  # noqa: PLC0415
        from orchestrator.docker_deployment_ai.plan_hash import compute_plan_hash  # noqa: PLC0415
        db = await _make_db()
        orch = DockerDeploymentOrchestrator()
        try:
            await db.execute(
                """INSERT INTO deployment_targets
                (id, project_id, name, host, ssh_user, app_name, deploy_path, created_at, updated_at)
                VALUES ('tgt-1','proj-1','server','1.2.3.4','deploy','app','/opt/app','2024-01-01','2024-01-01')"""
            )
            plan_json = json.dumps({"strategy": "docker_compose_private", "risk_level": "low",
                                    "deployment_actions": []})
            plan_hash = compute_plan_hash(json.loads(plan_json))
            await db.execute(
                """INSERT INTO deployment_plans
                (id, project_id, target_id, status, plan_json, plan_content_hash,
                 risk_level, blocking_questions_json, created_at)
                VALUES ('plan-1','proj-1','tgt-1','approved',?,?,'low','[]','2024-01-01')""",
                (plan_json, plan_hash),
            )
            await db.execute(
                """INSERT INTO deployment_approvals
                (id, plan_id, plan_content_hash, approved_by, approved_at)
                VALUES ('appr-1','plan-1',?,'user@test.com','2024-01-01')""",
                (plan_hash,),
            )
            # Validation row with status=blocked
            await db.execute(
                """INSERT INTO deployment_plan_validations
                (id, plan_id, status, plan_content_hash, checks_json,
                 blocking_errors_json, warnings_json, created_at)
                VALUES ('val-1','plan-1','blocked',?,'[]','["port conflict"]','[]','2024-01-01')""",
                (plan_hash,),
            )
            await db.commit()
            result = await orch.execute_plan("plan-1", "user@test.com", db)
            assert result["status"] == "rejected"
            assert "blocked" in result["error"].lower() or "validation" in result["error"].lower()
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_execute_with_stale_validation_returns_rejected(self):
        """Validation hash mismatch (plan changed after validation) → rejected."""
        from orchestrator.docker_deployment_ai.orchestrator import DockerDeploymentOrchestrator  # noqa: PLC0415
        import json  # noqa: PLC0415
        from orchestrator.docker_deployment_ai.plan_hash import compute_plan_hash  # noqa: PLC0415
        db = await _make_db()
        orch = DockerDeploymentOrchestrator()
        try:
            await db.execute(
                """INSERT INTO deployment_targets
                (id, project_id, name, host, ssh_user, app_name, deploy_path, created_at, updated_at)
                VALUES ('tgt-1','proj-1','server','1.2.3.4','deploy','app','/opt/app','2024-01-01','2024-01-01')"""
            )
            # Current plan content
            plan_dict = {"strategy": "docker_compose_private", "risk_level": "low",
                         "deployment_actions": []}
            plan_json = json.dumps(plan_dict)
            current_hash = compute_plan_hash(plan_dict)
            # Stale validation used a different (old) plan hash
            stale_hash = "aaaaaaaabbbbbbbbccccccccdddddddd" * 2
            await db.execute(
                """INSERT INTO deployment_plans
                (id, project_id, target_id, status, plan_json, plan_content_hash,
                 risk_level, blocking_questions_json, created_at)
                VALUES ('plan-1','proj-1','tgt-1','approved',?,?,'low','[]','2024-01-01')""",
                (plan_json, current_hash),
            )
            await db.execute(
                """INSERT INTO deployment_approvals
                (id, plan_id, plan_content_hash, approved_by, approved_at)
                VALUES ('appr-1','plan-1',?,'user@test.com','2024-01-01')""",
                (current_hash,),
            )
            await db.execute(
                """INSERT INTO deployment_plan_validations
                (id, plan_id, status, plan_content_hash, checks_json,
                 blocking_errors_json, warnings_json, created_at)
                VALUES ('val-1','plan-1','passed',?,
                        '[]','[]','[]','2024-01-01')""",
                (stale_hash,),  # hash mismatch — stale
            )
            await db.commit()
            result = await orch.execute_plan("plan-1", "user@test.com", db)
            assert result["status"] == "rejected"
            assert "changed" in result["error"].lower() or "stale" in result["error"].lower() or "hash" in result["error"].lower()
        finally:
            await db.close()


# ---------------------------------------------------------------------------
# P0-4  Approval plan hash
# ---------------------------------------------------------------------------

class TestApprovalPlanHash:
    """Execute must reject when the plan changed after approval."""

    @pytest.mark.asyncio
    async def test_execute_after_stale_approval_returns_rejected(self):
        """Approval hash != current plan hash → rejected."""
        from orchestrator.docker_deployment_ai.orchestrator import DockerDeploymentOrchestrator  # noqa: PLC0415
        import json  # noqa: PLC0415
        from orchestrator.docker_deployment_ai.plan_hash import compute_plan_hash  # noqa: PLC0415
        db = await _make_db()
        orch = DockerDeploymentOrchestrator()
        try:
            await db.execute(
                """INSERT INTO deployment_targets
                (id, project_id, name, host, ssh_user, app_name, deploy_path, created_at, updated_at)
                VALUES ('tgt-1','proj-1','server','1.2.3.4','deploy','app','/opt/app','2024-01-01','2024-01-01')"""
            )
            # Current plan
            plan_dict = {"strategy": "docker_compose_private", "risk_level": "low",
                         "deployment_actions": [], "note": "v2"}
            plan_json = json.dumps(plan_dict)
            current_hash = compute_plan_hash(plan_dict)
            # Old plan that was approved
            old_hash = compute_plan_hash({"strategy": "docker_compose_private",
                                          "risk_level": "low", "deployment_actions": []})
            await db.execute(
                """INSERT INTO deployment_plans
                (id, project_id, target_id, status, plan_json, plan_content_hash,
                 risk_level, blocking_questions_json, created_at)
                VALUES ('plan-1','proj-1','tgt-1','approved',?,?,'low','[]','2024-01-01')""",
                (plan_json, current_hash),
            )
            # Validation for CURRENT hash (passed)
            await db.execute(
                """INSERT INTO deployment_plan_validations
                (id, plan_id, status, plan_content_hash, checks_json,
                 blocking_errors_json, warnings_json, created_at)
                VALUES ('val-1','plan-1','passed',?,'[]','[]','[]','2024-01-01')""",
                (current_hash,),
            )
            # Approval for OLD hash
            await db.execute(
                """INSERT INTO deployment_approvals
                (id, plan_id, plan_content_hash, approved_by, approved_at)
                VALUES ('appr-1','plan-1',?,'user@test.com','2024-01-01')""",
                (old_hash,),
            )
            await db.commit()
            result = await orch.execute_plan("plan-1", "user@test.com", db)
            assert result["status"] == "rejected"
            # Error must mention approval/hash/changed
            err = result["error"].lower()
            assert "approval" in err or "hash" in err or "changed" in err
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_approve_stores_plan_content_hash(self):
        """approve_plan must record the current plan_content_hash in deployment_approvals."""
        from orchestrator.docker_deployment_ai.orchestrator import DockerDeploymentOrchestrator  # noqa: PLC0415
        import json  # noqa: PLC0415
        from orchestrator.docker_deployment_ai.plan_hash import compute_plan_hash  # noqa: PLC0415
        db = await _make_db()
        orch = DockerDeploymentOrchestrator()
        try:
            plan_dict = {"strategy": "docker_compose_private", "risk_level": "low",
                         "deployment_actions": []}
            plan_json = json.dumps(plan_dict)
            expected_hash = compute_plan_hash(plan_dict)
            await db.execute(
                """INSERT INTO deployment_plans
                (id, project_id, target_id, status, plan_json, plan_content_hash,
                 risk_level, blocking_questions_json, created_at)
                VALUES ('plan-1','proj-1','tgt-1','draft',?,?,'low','[]','2024-01-01')""",
                (plan_json, expected_hash),
            )
            await db.commit()
            result = await orch.approve_plan("plan-1", "approver@test.com", None, db)
            assert "approval_id" in result

            cursor = await db.execute(
                "SELECT plan_content_hash FROM deployment_approvals WHERE plan_id='plan-1'"
            )
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] == expected_hash
        finally:
            await db.close()


# ---------------------------------------------------------------------------
# P0-6  SSH mutation mode
# ---------------------------------------------------------------------------

class TestSSHMutationMode:
    """ExecutorSSHClient has no generic passthrough; typed builders work."""

    def test_executor_ssh_client_has_no_generic_run_method_exposed(self):
        """ExecutorSSHClient should not expose a public `run()` method
        (that would be a generic passthrough to the shell)."""
        from orchestrator.docker_deployment_ai.executor_ssh_client import ExecutorSSHClient  # noqa: PLC0415
        # The internal _run is acceptable (private); a public run() is not.
        assert not hasattr(ExecutorSSHClient, "run"), (
            "ExecutorSSHClient must not have a public run() method — "
            "that would allow arbitrary shell execution. Use typed builders only."
        )

    def test_executor_ssh_client_has_required_typed_builders(self):
        """Typed builder methods must exist."""
        from orchestrator.docker_deployment_ai.executor_ssh_client import ExecutorSSHClient  # noqa: PLC0415
        for method in (
            "run_mkdir",
            "run_docker_compose_up",
            "run_docker_compose_down",
            "run_docker_network_create",
            "run_docker_volume_create",
        ):
            assert hasattr(ExecutorSSHClient, method), f"Missing typed builder: {method}"

    @pytest.mark.asyncio
    async def test_unimplemented_action_docker_login_errors_before_ssh(self):
        """DOCKER_LOGIN (still not implemented) must return an error without SSH call."""
        from orchestrator.docker_deployment_ai.executor import _execute_action, ExecutorSSHClient  # noqa: PLC0415
        fake_ssh = MagicMock(spec=ExecutorSSHClient)
        _, _, error = await _execute_action(
            {"action_type": "DOCKER_LOGIN", "params": {}},
            fake_ssh,
            deploy_path="/opt/app",
        )
        assert error is not None
        assert "not implemented" in error.lower()
        # No SSH method should have been called
        for method in ("run_mkdir", "run_docker_compose_up", "run_docker_compose_down"):
            getattr(fake_ssh, method).assert_not_called()

    @pytest.mark.asyncio
    async def test_unimplemented_action_rollback_to_previous_errors_before_ssh(self):
        """ROLLBACK_TO_PREVIOUS_RELEASE (still not implemented) must error without SSH call."""
        from orchestrator.docker_deployment_ai.executor import _execute_action, ExecutorSSHClient  # noqa: PLC0415
        fake_ssh = MagicMock(spec=ExecutorSSHClient)
        _, _, error = await _execute_action(
            {"action_type": "ROLLBACK_TO_PREVIOUS_RELEASE", "params": {}},
            fake_ssh,
            deploy_path="/opt/app",
        )
        assert error is not None
        assert "not implemented" in error.lower()

    @pytest.mark.asyncio
    async def test_create_directory_calls_run_mkdir(self):
        """CREATE_DIRECTORY calls run_mkdir with the validated path."""
        from orchestrator.docker_deployment_ai.executor import _execute_action, ExecutorSSHClient  # noqa: PLC0415
        fake_ssh = MagicMock(spec=ExecutorSSHClient)
        fake_ssh.run_mkdir.return_value = (0, "", "")
        _, _, error = await _execute_action(
            {"action_type": "CREATE_DIRECTORY", "params": {"path": "/opt/myapp"}},
            fake_ssh,
            deploy_path="/opt/myapp",
        )
        assert error is None
        fake_ssh.run_mkdir.assert_called_once_with("/opt/myapp")


# ---------------------------------------------------------------------------
# P0-7  Rollback disabled MVP
# ---------------------------------------------------------------------------

class TestRollbackDisabledMVP:
    """execute_rollback must return not_implemented; rollback_available stays 0."""

    @pytest.mark.asyncio
    async def test_execute_rollback_returns_not_implemented(self):
        from orchestrator.docker_deployment_ai.rollback import execute_rollback  # noqa: PLC0415
        db = await _make_db()
        target = MagicMock()
        target.host = "1.2.3.4"
        target.ssh_user = "deploy"
        target.ssh_port = 22
        target.auth_mode = "ssh_key"
        target.auth_reference = None
        try:
            result = await execute_rollback(
                run_id="run-1",
                project_id="proj-1",
                target=target,
                db=db,
            )
            assert result["status"] == "not_implemented"
            assert "not" in result["error"].lower()
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_rollback_available_not_set_after_capture(self):
        """capture_rollback_metadata must NOT set rollback_available=1."""
        from orchestrator.docker_deployment_ai.rollback import capture_rollback_metadata  # noqa: PLC0415
        db = await _make_db()
        # Create a run row
        await db.execute(
            """INSERT INTO deploy_runs
            (id, project_id, status, branch, created_at, health_status)
            VALUES ('run-1','proj-1','completed','main','2024-01-01','ok')"""
        )
        await db.commit()

        target = MagicMock()
        target.host = "1.2.3.4"
        target.ssh_user = "deploy"
        target.ssh_port = 22
        target.deploy_path = "/opt/app"
        target.auth_mode = "ssh_key"
        target.auth_reference = None

        # Patch SSHClient so capture doesn't try real SSH
        with patch(
            "orchestrator.docker_deployment_ai.rollback.SSHClient"
        ) as mock_ssh_cls:
            mock_ssh = MagicMock()
            mock_ssh.run.return_value = (1, "", "connection refused")
            mock_ssh_cls.return_value = mock_ssh

            await capture_rollback_metadata(
                run_id="run-1",
                plan={},
                target=target,
                db=db,
            )

        # rollback_available must still be 0
        cursor = await db.execute(
            "SELECT rollback_available FROM deploy_runs WHERE id='run-1'"
        )
        row = await cursor.fetchone()
        assert row is not None
        assert int(row[0]) == 0, "rollback_available must stay 0 until rollback is implemented"
        await db.close()

    @pytest.mark.asyncio
    async def test_rollback_record_has_metadata_only_status(self):
        """The rollback record in deployment_rollbacks must say 'metadata_only'."""
        from orchestrator.docker_deployment_ai.rollback import capture_rollback_metadata  # noqa: PLC0415
        db = await _make_db()
        await db.execute(
            """INSERT INTO deploy_runs
            (id, project_id, status, branch, created_at, health_status)
            VALUES ('run-1','proj-1','completed','main','2024-01-01','ok')"""
        )
        await db.commit()

        target = MagicMock()
        target.host = "1.2.3.4"
        target.ssh_user = "deploy"
        target.ssh_port = 22
        target.deploy_path = "/opt/app"
        target.auth_mode = "ssh_key"
        target.auth_reference = None

        with patch(
            "orchestrator.docker_deployment_ai.rollback.SSHClient"
        ) as mock_ssh_cls:
            mock_ssh = MagicMock()
            mock_ssh.run.return_value = (1, "", "connection refused")
            mock_ssh_cls.return_value = mock_ssh
            rollback_id = await capture_rollback_metadata(
                run_id="run-1", plan={}, target=target, db=db
            )

        cursor = await db.execute(
            "SELECT rollback_status FROM deployment_rollbacks WHERE id = ?",
            (rollback_id,),
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "metadata_only"
        await db.close()


# ---------------------------------------------------------------------------
# P0-1  plan_hash determinism (unit)
# ---------------------------------------------------------------------------

class TestPlanHashDeterminism:
    """compute_plan_hash must be stable and exclude volatile keys."""

    def test_same_plan_same_hash(self):
        from orchestrator.docker_deployment_ai.plan_hash import compute_plan_hash  # noqa: PLC0415
        plan = {"strategy": "kamal_v2_compatible", "risk_level": "medium",
                "deployment_actions": []}
        assert compute_plan_hash(plan) == compute_plan_hash(plan)

    def test_different_plan_different_hash(self):
        from orchestrator.docker_deployment_ai.plan_hash import compute_plan_hash  # noqa: PLC0415
        plan_a = {"strategy": "kamal_v2_compatible", "risk_level": "medium"}
        plan_b = {"strategy": "docker_compose_private", "risk_level": "low"}
        assert compute_plan_hash(plan_a) != compute_plan_hash(plan_b)

    def test_volatile_key_excluded(self):
        from orchestrator.docker_deployment_ai.plan_hash import compute_plan_hash  # noqa: PLC0415
        plan_base = {"strategy": "kamal_v2_compatible", "risk_level": "medium"}
        plan_with_volatile = {**plan_base, "_rollback_ref_available": True}
        assert compute_plan_hash(plan_base) == compute_plan_hash(plan_with_volatile)

    def test_key_order_does_not_affect_hash(self):
        from orchestrator.docker_deployment_ai.plan_hash import compute_plan_hash  # noqa: PLC0415
        plan_a = {"risk_level": "medium", "strategy": "kamal_v2_compatible"}
        plan_b = {"strategy": "kamal_v2_compatible", "risk_level": "medium"}
        assert compute_plan_hash(plan_a) == compute_plan_hash(plan_b)


# ---------------------------------------------------------------------------
# Hardening — execute returns run_id
# ---------------------------------------------------------------------------

class TestExecuteReturnsRunId:
    """POST /execute must return a run_id so the client can poll status."""

    @pytest.mark.asyncio
    async def test_execute_plan_returns_run_id(self):
        """DockerDeploymentOrchestrator.execute_plan must include run_id in result."""
        import json  # noqa: PLC0415
        from orchestrator.docker_deployment_ai.orchestrator import DockerDeploymentOrchestrator  # noqa: PLC0415
        from orchestrator.docker_deployment_ai.plan_hash import compute_plan_hash  # noqa: PLC0415

        db = await _make_db()
        orch = DockerDeploymentOrchestrator()

        # Insert project, target, plan, validation, approval
        await db.execute(
            "INSERT INTO projects (id, name) VALUES ('proj-1','test')"
        )
        await db.execute(
            """INSERT INTO deployment_targets
            (id, project_id, name, host, ssh_user, app_name, deploy_path, created_at, updated_at)
            VALUES ('tgt-1','proj-1','server','1.2.3.4','deploy','app','/opt/app','2024-01-01','2024-01-01')"""
        )
        plan_dict = {
            "strategy": "docker_compose_with_host_port",
            "risk_level": "low",
            "deployment_actions": [],
        }
        plan_json = json.dumps(plan_dict)
        plan_hash = compute_plan_hash(plan_dict)
        await db.execute(
            """INSERT INTO deployment_plans
            (id, project_id, target_id, status, plan_json, plan_content_hash,
             risk_level, blocking_questions_json, created_at)
            VALUES ('plan-1','proj-1','tgt-1','approved',?,?,'low','[]','2024-01-01')""",
            (plan_json, plan_hash),
        )
        await db.execute(
            """INSERT INTO deployment_plan_validations
            (id, plan_id, status, plan_content_hash, checks_json,
             blocking_errors_json, warnings_json, created_at)
            VALUES ('val-1','plan-1','passed',?,
                    '[]','[]','[]','2024-01-01')""",
            (plan_hash,),
        )
        await db.execute(
            """INSERT INTO deployment_approvals
            (id, plan_id, plan_content_hash, approved_by, approved_at)
            VALUES ('appr-1','plan-1',?,'tester@test.com','2024-01-01')""",
            (plan_hash,),
        )
        await db.commit()

        with patch(
            "orchestrator.docker_deployment_ai.orchestrator.execute_plan",
            AsyncMock(return_value={"status": "completed", "steps_completed": 0}),
        ), patch(
            "orchestrator.docker_deployment_ai.orchestrator.run_healthcheck",
            AsyncMock(),
        ), patch(
            "orchestrator.docker_deployment_ai.orchestrator.capture_rollback_metadata",
            AsyncMock(),
        ), patch(
            "orchestrator.docker_deployment_ai.orchestrator.emit_approved",
            AsyncMock(),
        ):
            result = await orch.execute_plan("plan-1", "tester@test.com", db)

        assert "run_id" in result, "execute_plan must return run_id"
        assert result["run_id"]  # must be non-empty string
        assert result["status"] == "completed"
        await db.close()

    @pytest.mark.asyncio
    async def test_prepare_run_creates_row_before_returning(self):
        """prepare_run must commit the run row to DB before it returns, so the
        run_id is immediately readable — no background task required."""
        import json  # noqa: PLC0415
        from orchestrator.docker_deployment_ai.orchestrator import DockerDeploymentOrchestrator  # noqa: PLC0415
        from orchestrator.docker_deployment_ai.plan_hash import compute_plan_hash  # noqa: PLC0415

        db = await _make_db()
        orch = DockerDeploymentOrchestrator()

        await db.execute("INSERT INTO projects (id, name) VALUES ('proj-1','test')")
        await db.execute(
            """INSERT INTO deployment_targets
            (id, project_id, name, host, ssh_user, app_name, deploy_path, created_at, updated_at)
            VALUES ('tgt-1','proj-1','server','1.2.3.4','deploy','app','/opt/app','2024-01-01','2024-01-01')"""
        )
        plan_dict = {"strategy": "docker_compose_with_host_port", "risk_level": "low",
                     "deployment_actions": []}
        plan_json = json.dumps(plan_dict)
        plan_hash = compute_plan_hash(plan_dict)
        await db.execute(
            """INSERT INTO deployment_plans
            (id, project_id, target_id, status, plan_json, plan_content_hash,
             risk_level, blocking_questions_json, created_at)
            VALUES ('plan-2','proj-1','tgt-1','approved',?,?,'low','[]','2024-01-01')""",
            (plan_json, plan_hash),
        )
        await db.execute(
            """INSERT INTO deployment_plan_validations
            (id, plan_id, status, plan_content_hash, checks_json,
             blocking_errors_json, warnings_json, created_at)
            VALUES ('val-2','plan-2','passed',?,'[]','[]','[]','2024-01-01')""",
            (plan_hash,),
        )
        await db.execute(
            """INSERT INTO deployment_approvals
            (id, plan_id, plan_content_hash, approved_by, approved_at)
            VALUES ('appr-2','plan-2',?,'tester@test.com','2024-01-01')""",
            (plan_hash,),
        )
        await db.commit()

        prep = await orch.prepare_run("plan-2", "tester@test.com", db)

        # prepare_run must not return an error
        assert "error" not in prep, f"prepare_run failed: {prep.get('error')}"
        run_id = prep["run_id"]
        assert run_id

        # The run row MUST exist in the DB before we even start the background task.
        cursor = await db.execute(
            "SELECT id, status FROM deploy_runs WHERE id=?", (run_id,)
        )
        row = await cursor.fetchone()
        assert row is not None, (
            "deploy_runs row must be committed before prepare_run returns — "
            "client must be able to GET the run immediately after receiving run_id."
        )
        assert row[1] == "pending"
        await db.close()

    @pytest.mark.asyncio
    async def test_prepare_run_missing_approval_returns_error_not_row(self):
        """prepare_run must return an error (not create a row) when approval is absent."""
        import json  # noqa: PLC0415
        from orchestrator.docker_deployment_ai.orchestrator import DockerDeploymentOrchestrator  # noqa: PLC0415
        from orchestrator.docker_deployment_ai.plan_hash import compute_plan_hash  # noqa: PLC0415

        db = await _make_db()
        orch = DockerDeploymentOrchestrator()

        await db.execute("INSERT INTO projects (id, name) VALUES ('proj-1','test')")
        await db.execute(
            """INSERT INTO deployment_targets
            (id, project_id, name, host, ssh_user, app_name, deploy_path, created_at, updated_at)
            VALUES ('tgt-1','proj-1','server','1.2.3.4','deploy','app','/opt/app','2024-01-01','2024-01-01')"""
        )
        plan_dict = {"strategy": "docker_compose_with_host_port", "risk_level": "low",
                     "deployment_actions": []}
        plan_json = json.dumps(plan_dict)
        plan_hash = compute_plan_hash(plan_dict)
        await db.execute(
            """INSERT INTO deployment_plans
            (id, project_id, target_id, status, plan_json, plan_content_hash,
             risk_level, blocking_questions_json, created_at)
            VALUES ('plan-3','proj-1','tgt-1','draft',?,?,'low','[]','2024-01-01')""",
            (plan_json, plan_hash),
        )
        await db.execute(
            """INSERT INTO deployment_plan_validations
            (id, plan_id, status, plan_content_hash, checks_json,
             blocking_errors_json, warnings_json, created_at)
            VALUES ('val-3','plan-3','passed',?,'[]','[]','[]','2024-01-01')""",
            (plan_hash,),
        )
        # No approval row inserted
        await db.commit()

        prep = await orch.prepare_run("plan-3", "tester@test.com", db)

        assert "error" in prep, "prepare_run must return error when approval missing"
        assert prep.get("status") == "rejected"
        assert prep.get("http_status") == 409

        # No run row must have been created
        cursor = await db.execute("SELECT count(*) FROM deploy_runs")
        count = (await cursor.fetchone())[0]
        assert count == 0, "No run row must be created when gate fails"
        await db.close()


# ---------------------------------------------------------------------------
# Hardening — rollback 501 message updated
# ---------------------------------------------------------------------------

class TestRollback501MessageUpdated:
    """The rollback 501 message must no longer reference UPLOAD_FILE/WRITE_ENV_FILE."""

    @pytest.mark.asyncio
    async def test_execute_rollback_message_updated(self):
        from orchestrator.docker_deployment_ai.rollback import execute_rollback  # noqa: PLC0415
        db = await _make_db()
        target = MagicMock()
        target.host = "1.2.3.4"
        target.ssh_user = "deploy"
        target.ssh_port = 22
        target.auth_mode = "ssh_key"
        target.auth_reference = None
        try:
            result = await execute_rollback(
                run_id="run-x", project_id="proj-x", target=target, db=db
            )
            error_msg = result["error"].lower()
            assert "not implemented" in error_msg
            assert "upload_file" not in error_msg, (
                "Rollback error message must not reference UPLOAD_FILE — "
                "that action is now implemented."
            )
            assert "write_env_file" not in error_msg, (
                "Rollback error message must not reference WRITE_ENV_FILE — "
                "that action is now implemented."
            )
            assert "compose" in error_msg or "restoration" in error_msg
        finally:
            await db.close()


# ---------------------------------------------------------------------------
# Production guardrails — validation warning semantics
# ---------------------------------------------------------------------------

class TestValidationWarningSemanticsForExecution:
    """_check_validation_allows_execution must treat 'warning' as executable
    only when blocking_errors is empty.

    Rules:
      status='passed',  blocking_errors=[]  → executable
      status='warning', blocking_errors=[]  → executable
      status='warning', blocking_errors=[…] → NOT executable (inconsistent state)
      status='blocked', blocking_errors=[]  → NOT executable
      status='blocked', blocking_errors=[…] → NOT executable
    """

    async def _make_plan_with_validation(
        self,
        db,
        *,
        val_status: str,
        blocking_errors: list,
    ):
        import json  # noqa: PLC0415
        from orchestrator.docker_deployment_ai.plan_hash import compute_plan_hash  # noqa: PLC0415
        plan_dict = {"strategy": "docker_compose_private", "risk_level": "low",
                     "deployment_actions": []}
        plan_json = json.dumps(plan_dict)
        plan_hash = compute_plan_hash(plan_dict)
        await db.execute(
            """INSERT INTO deployment_plans
            (id, project_id, target_id, status, plan_json, plan_content_hash,
             risk_level, blocking_questions_json, created_at)
            VALUES ('plan-w','proj-w','tgt-w','draft',?,?,'low','[]','2024-01-01')""",
            (plan_json, plan_hash),
        )
        await db.execute(
            """INSERT INTO deployment_plan_validations
            (id, plan_id, status, plan_content_hash, checks_json,
             blocking_errors_json, warnings_json, created_at)
            VALUES ('val-w','plan-w',?,?,'[]',?,?,'2024-01-01')""",
            (val_status, plan_hash, json.dumps(blocking_errors), "[]"),
        )
        await db.commit()
        return plan_hash

    @pytest.mark.asyncio
    async def test_passed_no_blocking_errors_is_executable(self):
        from orchestrator.docker_deployment_ai.orchestrator import DockerDeploymentOrchestrator  # noqa: PLC0415
        from orchestrator.docker_deployment_ai.plan_hash import compute_plan_hash  # noqa: PLC0415
        import json  # noqa: PLC0415
        db = await _make_db()
        plan_dict = {"strategy": "docker_compose_private", "risk_level": "low",
                     "deployment_actions": []}
        plan_hash = await self._make_plan_with_validation(
            db, val_status="passed", blocking_errors=[]
        )
        orch = DockerDeploymentOrchestrator()
        ok, err = await orch._check_validation_allows_execution("plan-w", plan_hash, db)
        assert ok, f"Expected executable for status=passed, got: {err}"
        await db.close()

    @pytest.mark.asyncio
    async def test_warning_no_blocking_errors_is_executable(self):
        """status='warning' with no blocking errors must allow execution."""
        from orchestrator.docker_deployment_ai.orchestrator import DockerDeploymentOrchestrator  # noqa: PLC0415
        db = await _make_db()
        plan_hash = await self._make_plan_with_validation(
            db, val_status="warning", blocking_errors=[]
        )
        orch = DockerDeploymentOrchestrator()
        ok, err = await orch._check_validation_allows_execution("plan-w", plan_hash, db)
        assert ok, f"Expected executable for status=warning+no-errors, got: {err}"
        await db.close()

    @pytest.mark.asyncio
    async def test_warning_with_blocking_errors_is_not_executable(self):
        """status='warning' with blocking_errors present is an inconsistent state
        and must refuse execution."""
        from orchestrator.docker_deployment_ai.orchestrator import DockerDeploymentOrchestrator  # noqa: PLC0415
        db = await _make_db()
        plan_hash = await self._make_plan_with_validation(
            db, val_status="warning",
            blocking_errors=["Some blocking error that should not be here"]
        )
        orch = DockerDeploymentOrchestrator()
        ok, err = await orch._check_validation_allows_execution("plan-w", plan_hash, db)
        assert not ok, "Must refuse when warning+blocking_errors (inconsistent state)"
        assert "blocking error" in err.lower() or "inconsistent" in err.lower()
        await db.close()

    @pytest.mark.asyncio
    async def test_blocked_status_is_never_executable(self):
        """status='blocked' must always refuse execution."""
        from orchestrator.docker_deployment_ai.orchestrator import DockerDeploymentOrchestrator  # noqa: PLC0415
        db = await _make_db()
        plan_hash = await self._make_plan_with_validation(
            db, val_status="blocked", blocking_errors=["Port 80 in use"]
        )
        orch = DockerDeploymentOrchestrator()
        ok, err = await orch._check_validation_allows_execution("plan-w", plan_hash, db)
        assert not ok, "status=blocked must always refuse"
        assert "blocked" in err.lower()
        await db.close()

    @pytest.mark.asyncio
    async def test_blocked_with_no_errors_is_still_not_executable(self):
        """Even if blocking_errors is empty, status='blocked' still refuses."""
        from orchestrator.docker_deployment_ai.orchestrator import DockerDeploymentOrchestrator  # noqa: PLC0415
        db = await _make_db()
        plan_hash = await self._make_plan_with_validation(
            db, val_status="blocked", blocking_errors=[]
        )
        orch = DockerDeploymentOrchestrator()
        ok, _ = await orch._check_validation_allows_execution("plan-w", plan_hash, db)
        assert not ok, "status=blocked must always refuse regardless of blocking_errors"
        await db.close()


# ---------------------------------------------------------------------------
# Production guardrails — run_id immediately readable after execute
# ---------------------------------------------------------------------------

class TestRunIdImmediatelyReadable:
    """After POST execute returns run_id, GET /deployment-runs/{run_id} must
    return 200 immediately — never 404. The run row must exist before execution
    starts, not after it completes."""

    @pytest.mark.asyncio
    async def test_run_row_exists_immediately_after_prepare_run(self):
        """prepare_run commits the row; no background task needed for readability."""
        import json  # noqa: PLC0415
        from orchestrator.docker_deployment_ai.orchestrator import DockerDeploymentOrchestrator  # noqa: PLC0415
        from orchestrator.docker_deployment_ai.plan_hash import compute_plan_hash  # noqa: PLC0415

        db = await _make_db()
        orch = DockerDeploymentOrchestrator()

        await db.execute("INSERT INTO projects (id, name) VALUES ('p','test')")
        await db.execute(
            """INSERT INTO deployment_targets
            (id, project_id, name, host, ssh_user, app_name, deploy_path, created_at, updated_at)
            VALUES ('t','p','s','1.2.3.4','deploy','app','/opt/app','2024-01-01','2024-01-01')"""
        )
        plan_dict = {"strategy": "docker_compose_private", "risk_level": "low",
                     "deployment_actions": []}
        plan_json = json.dumps(plan_dict)
        plan_hash = compute_plan_hash(plan_dict)
        await db.execute(
            """INSERT INTO deployment_plans
            (id, project_id, target_id, status, plan_json, plan_content_hash,
             risk_level, blocking_questions_json, created_at)
            VALUES ('pl','p','t','approved',?,?,'low','[]','2024-01-01')""",
            (plan_json, plan_hash),
        )
        await db.execute(
            """INSERT INTO deployment_plan_validations
            (id, plan_id, status, plan_content_hash, checks_json,
             blocking_errors_json, warnings_json, created_at)
            VALUES ('vl','pl','passed',?,'[]','[]','[]','2024-01-01')""",
            (plan_hash,),
        )
        await db.execute(
            """INSERT INTO deployment_approvals
            (id, plan_id, plan_content_hash, approved_by, approved_at)
            VALUES ('ap','pl',?,'u','2024-01-01')""",
            (plan_hash,),
        )
        await db.commit()

        prep = await orch.prepare_run("pl", "user@test.com", db)
        assert "error" not in prep, f"prepare_run failed: {prep}"

        run_id = prep["run_id"]

        # Simulate what GET /deployment-runs/{run_id} does — must find the row.
        run_data = await orch.get_run(run_id, db)
        assert run_data is not None, (
            f"GET /deployment-runs/{run_id} would return 404 — "
            "run row must exist before background task starts."
        )
        assert run_data["status"] == "pending"
        await db.close()

    @pytest.mark.asyncio
    async def test_gate_failure_returns_no_run_row(self):
        """When gates fail, prepare_run returns an error and creates no DB row."""
        from orchestrator.docker_deployment_ai.orchestrator import DockerDeploymentOrchestrator  # noqa: PLC0415
        db = await _make_db()
        orch = DockerDeploymentOrchestrator()
        # Non-existent plan
        prep = await orch.prepare_run("no-such-plan", "user@test.com", db)
        assert "error" in prep
        cursor = await db.execute("SELECT count(*) FROM deploy_runs")
        assert (await cursor.fetchone())[0] == 0
        await db.close()
