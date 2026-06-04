"""Target Matrix + Single-flight Execution Guard tests.

Covers:
  1. Single-flight execution guard per target
  2. Target setup fixture matrix (9 server configurations)
  3. Routing conflict validation
  4. Container/network/volume conflict validation
"""

from __future__ import annotations

import asyncio
import json
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest

# Stub jose so orchestrator.api.routes can be imported in envs without it
for _jose_mod in ("jose", "jose.jwe", "jose.jwt", "jose.exceptions"):
    sys.modules.setdefault(_jose_mod, MagicMock())

from orchestrator.docker_deployment_ai.models import (
    ContainerInfo,
    DeploymentManager,
    DetectionResult,
    InventorySnapshot,
    ListeningPort,
    ReverseProxy,
)
from orchestrator.docker_deployment_ai.plan_hash import compute_plan_hash
from orchestrator.docker_deployment_ai.validator import validate_deployment_plan


# ---------------------------------------------------------------------------
# Shared DB / fixture helpers
# ---------------------------------------------------------------------------

async def _make_db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(":memory:")
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
            finished_at TEXT, current_step INTEGER,
            rollback_available INTEGER NOT NULL DEFAULT 0
        )"""
    )
    await db.execute(
        """CREATE TABLE IF NOT EXISTS deployment_targets (
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
            name TEXT NOT NULL, host TEXT NOT NULL, ssh_user TEXT NOT NULL,
            ssh_port INTEGER NOT NULL DEFAULT 22,
            environment TEXT NOT NULL DEFAULT 'production',
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


async def _insert_target(db, target_id="tgt-1", project_id="proj-1"):
    await db.execute(
        """INSERT INTO deployment_targets
        (id, project_id, name, host, ssh_user, app_name, deploy_path,
         created_at, updated_at)
        VALUES (?, ?, 'server', '1.2.3.4', 'deploy', 'app', '/opt/app',
        '2024-01-01', '2024-01-01')""",
        (target_id, project_id),
    )
    await db.commit()


async def _make_ready_plan(
    db,
    plan_id: str = "plan-1",
    target_id: str = "tgt-1",
    project_id: str = "proj-1",
    plan_dict: dict | None = None,
) -> str:
    if plan_dict is None:
        plan_dict = {
            "strategy": "docker_compose_private",
            "risk_level": "low",
            "deployment_actions": [],
        }
    plan_json = json.dumps(plan_dict)
    plan_hash = compute_plan_hash(plan_dict)
    await db.execute(
        """INSERT INTO deployment_plans
        (id, project_id, target_id, status, plan_json, plan_content_hash,
         risk_level, blocking_questions_json, created_at)
        VALUES (?, ?, ?, 'approved', ?, ?, 'low', '[]', '2024-01-01')""",
        (plan_id, project_id, target_id, plan_json, plan_hash),
    )
    await db.execute(
        """INSERT INTO deployment_plan_validations
        (id, plan_id, status, plan_content_hash, checks_json,
         blocking_errors_json, warnings_json, created_at)
        VALUES (?, ?, 'passed', ?, '[]', '[]', '[]', '2024-01-01')""",
        (f"val-{plan_id}", plan_id, plan_hash),
    )
    await db.execute(
        """INSERT INTO deployment_approvals
        (id, plan_id, plan_content_hash, approved_by, approved_at)
        VALUES (?, ?, ?, 'user@test.com', '2024-01-01')""",
        (f"appr-{plan_id}", plan_id, plan_hash),
    )
    await db.commit()
    return plan_hash


async def _insert_run(db, run_id: str, target_id: str, status: str) -> None:
    await db.execute(
        """INSERT INTO deploy_runs
        (id, project_id, status, branch, created_at, target_id, started_at, health_status)
        VALUES (?, 'proj-1', ?, 'main', '2024-01-01', ?, '2024-01-01', 'unknown')""",
        (run_id, status, target_id),
    )
    await db.commit()


# ---------------------------------------------------------------------------
# 1. Single-flight execution guard per target
# ---------------------------------------------------------------------------

class TestSingleFlightGuard:
    """No two active deployment runs may mutate the same target concurrently."""

    @pytest.mark.asyncio
    async def test_first_execute_creates_pending_run(self):
        from orchestrator.docker_deployment_ai.orchestrator import DockerDeploymentOrchestrator
        db = await _make_db()
        orch = DockerDeploymentOrchestrator()
        try:
            await db.execute("INSERT INTO projects (id, name) VALUES ('proj-1','test')")
            await _insert_target(db)
            await _make_ready_plan(db)

            prep = await orch.prepare_run("plan-1", "user@test.com", db)

            assert "error" not in prep, f"prepare_run failed: {prep.get('error')}"
            assert prep["run_id"]
            cursor = await db.execute(
                "SELECT status FROM deploy_runs WHERE id=?", (prep["run_id"],)
            )
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] == "pending"
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_second_execute_same_target_while_first_pending_returns_409(self):
        from orchestrator.docker_deployment_ai.orchestrator import DockerDeploymentOrchestrator
        db = await _make_db()
        orch = DockerDeploymentOrchestrator()
        try:
            await db.execute("INSERT INTO projects (id, name) VALUES ('proj-1','test')")
            await _insert_target(db)
            await _make_ready_plan(db, plan_id="plan-1")
            await _make_ready_plan(db, plan_id="plan-2")

            # First execute succeeds
            prep1 = await orch.prepare_run("plan-1", "user@test.com", db)
            assert "error" not in prep1

            # Second execute for same target → 409
            prep2 = await orch.prepare_run("plan-2", "user@test.com", db)
            assert prep2.get("status") == "rejected"
            assert prep2.get("http_status") == 409
            assert "active" in prep2["error"].lower() or "pending" in prep2["error"].lower()
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_second_execute_same_target_while_first_running_returns_409(self):
        from orchestrator.docker_deployment_ai.orchestrator import DockerDeploymentOrchestrator
        db = await _make_db()
        orch = DockerDeploymentOrchestrator()
        try:
            await db.execute("INSERT INTO projects (id, name) VALUES ('proj-1','test')")
            await _insert_target(db)
            await _make_ready_plan(db, plan_id="plan-2")

            # Simulate a run already in 'running' state
            await _insert_run(db, "existing-run", "tgt-1", "running")

            prep = await orch.prepare_run("plan-2", "user@test.com", db)
            assert prep.get("status") == "rejected"
            assert prep.get("http_status") == 409
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_second_execute_same_target_while_first_deploying_returns_409(self):
        from orchestrator.docker_deployment_ai.orchestrator import DockerDeploymentOrchestrator
        db = await _make_db()
        orch = DockerDeploymentOrchestrator()
        try:
            await db.execute("INSERT INTO projects (id, name) VALUES ('proj-1','test')")
            await _insert_target(db)
            await _make_ready_plan(db, plan_id="plan-2")

            await _insert_run(db, "existing-run", "tgt-1", "deploying")

            prep = await orch.prepare_run("plan-2", "user@test.com", db)
            assert prep.get("status") == "rejected"
            assert prep.get("http_status") == 409
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_second_execute_same_target_while_first_starting_returns_409(self):
        from orchestrator.docker_deployment_ai.orchestrator import DockerDeploymentOrchestrator
        db = await _make_db()
        orch = DockerDeploymentOrchestrator()
        try:
            await db.execute("INSERT INTO projects (id, name) VALUES ('proj-1','test')")
            await _insert_target(db)
            await _make_ready_plan(db, plan_id="plan-2")

            await _insert_run(db, "existing-run", "tgt-1", "starting")

            prep = await orch.prepare_run("plan-2", "user@test.com", db)
            assert prep.get("status") == "rejected"
            assert prep.get("http_status") == 409
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_same_target_different_plan_blocks_if_active_run_exists(self):
        """Active run on target blocks regardless of which plan is being submitted."""
        from orchestrator.docker_deployment_ai.orchestrator import DockerDeploymentOrchestrator
        db = await _make_db()
        orch = DockerDeploymentOrchestrator()
        try:
            await db.execute("INSERT INTO projects (id, name) VALUES ('proj-1','test')")
            await _insert_target(db)
            # A different plan for the same target
            await _make_ready_plan(
                db, plan_id="plan-other",
                plan_dict={"strategy": "docker_compose_with_host_port", "risk_level": "low",
                           "deployment_actions": []}
            )
            await _insert_run(db, "active-run", "tgt-1", "pending")

            prep = await orch.prepare_run("plan-other", "user@test.com", db)
            assert prep.get("status") == "rejected"
            assert prep.get("http_status") == 409
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_different_target_not_blocked_by_active_run(self):
        """Active run on target A must not block a run on target B."""
        from orchestrator.docker_deployment_ai.orchestrator import DockerDeploymentOrchestrator
        db = await _make_db()
        orch = DockerDeploymentOrchestrator()
        try:
            await db.execute("INSERT INTO projects (id, name) VALUES ('proj-1','test')")
            # Two targets
            await db.execute(
                """INSERT INTO deployment_targets
                (id, project_id, name, host, ssh_user, app_name, deploy_path,
                 created_at, updated_at)
                VALUES ('tgt-A','proj-1','server-a','10.0.0.1','deploy','app',
                '/opt/app','2024-01-01','2024-01-01')"""
            )
            await db.execute(
                """INSERT INTO deployment_targets
                (id, project_id, name, host, ssh_user, app_name, deploy_path,
                 created_at, updated_at)
                VALUES ('tgt-B','proj-1','server-b','10.0.0.2','deploy','app',
                '/opt/app','2024-01-01','2024-01-01')"""
            )
            await db.commit()
            # Plan for target B
            await _make_ready_plan(db, plan_id="plan-B", target_id="tgt-B")
            # Active run on target A
            await _insert_run(db, "run-A", "tgt-A", "running")

            prep = await orch.prepare_run("plan-B", "user@test.com", db)
            assert "error" not in prep, (
                f"Different target must not be blocked by sibling run: {prep.get('error')}"
            )
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_completed_run_does_not_block(self):
        from orchestrator.docker_deployment_ai.orchestrator import DockerDeploymentOrchestrator
        db = await _make_db()
        orch = DockerDeploymentOrchestrator()
        try:
            await db.execute("INSERT INTO projects (id, name) VALUES ('proj-1','test')")
            await _insert_target(db)
            await _make_ready_plan(db, plan_id="plan-2")
            await _insert_run(db, "old-run", "tgt-1", "completed")

            prep = await orch.prepare_run("plan-2", "user@test.com", db)
            assert "error" not in prep, (
                f"Completed run must not block a new deploy: {prep.get('error')}"
            )
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_failed_run_does_not_block(self):
        from orchestrator.docker_deployment_ai.orchestrator import DockerDeploymentOrchestrator
        db = await _make_db()
        orch = DockerDeploymentOrchestrator()
        try:
            await db.execute("INSERT INTO projects (id, name) VALUES ('proj-1','test')")
            await _insert_target(db)
            await _make_ready_plan(db, plan_id="plan-2")
            await _insert_run(db, "old-run", "tgt-1", "failed")

            prep = await orch.prepare_run("plan-2", "user@test.com", db)
            assert "error" not in prep, (
                f"Failed run must not block a new deploy: {prep.get('error')}"
            )
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_cancelled_run_does_not_block(self):
        from orchestrator.docker_deployment_ai.orchestrator import DockerDeploymentOrchestrator
        db = await _make_db()
        orch = DockerDeploymentOrchestrator()
        try:
            await db.execute("INSERT INTO projects (id, name) VALUES ('proj-1','test')")
            await _insert_target(db)
            await _make_ready_plan(db, plan_id="plan-2")
            await _insert_run(db, "old-run", "tgt-1", "cancelled")

            prep = await orch.prepare_run("plan-2", "user@test.com", db)
            assert "error" not in prep, (
                f"Cancelled run must not block a new deploy: {prep.get('error')}"
            )
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_rejected_execute_creates_zero_rows(self):
        """When the single-flight guard fires, no deploy_runs row must be created."""
        from orchestrator.docker_deployment_ai.orchestrator import DockerDeploymentOrchestrator
        db = await _make_db()
        orch = DockerDeploymentOrchestrator()
        try:
            await db.execute("INSERT INTO projects (id, name) VALUES ('proj-1','test')")
            await _insert_target(db)
            await _make_ready_plan(db, plan_id="plan-2")
            # Pre-existing active run
            await _insert_run(db, "active-run", "tgt-1", "pending")

            before_count_cursor = await db.execute("SELECT count(*) FROM deploy_runs")
            before_count = (await before_count_cursor.fetchone())[0]

            prep = await orch.prepare_run("plan-2", "user@test.com", db)
            assert prep.get("status") == "rejected"

            after_count_cursor = await db.execute("SELECT count(*) FROM deploy_runs")
            after_count = (await after_count_cursor.fetchone())[0]
            assert after_count == before_count, (
                "No new deploy_runs row must be created when single-flight guard fires"
            )
        finally:
            await db.close()


# ---------------------------------------------------------------------------
# 2. Target setup fixture matrix
# ---------------------------------------------------------------------------

def _make_snapshot(
    *,
    reverse_proxy: ReverseProxy = ReverseProxy.none,
    deployment_manager: DeploymentManager = DeploymentManager.none,
    confidence: float = 0.9,
    evidence: list[str] | None = None,
    containers: list[ContainerInfo] | None = None,
    listening_ports: list[ListeningPort] | None = None,
    networks: list[dict] | None = None,
    volumes: list[dict] | None = None,
) -> InventorySnapshot:
    return InventorySnapshot(
        target_id="tgt-fixture",
        project_id="proj-fixture",
        detection=DetectionResult(
            deployment_manager=deployment_manager,
            reverse_proxy=reverse_proxy,
            confidence=confidence,
            evidence=evidence or [],
        ),
        containers=containers or [],
        listening_ports=listening_ports or [],
        networks=networks or [],
        volumes=volumes or [],
        docker_binary_info={"docker_binary_present": True, "docker_compose_v2_available": True},
    )


def _minimal_plan(strategy: str, **extra) -> dict:
    """Build a minimal schema-passing plan (schema validation is off in unit tests)."""
    plan = {
        "strategy": strategy,
        "risk_level": "low",
        "deployment_actions": [],
        "detected_server_state": {
            "deployment_manager": extra.pop("detected_dm", "none"),
            "reverse_proxy": extra.pop("detected_rp", "none"),
            "confidence": 0.9,
            "evidence": [],
        },
    }
    plan.update(extra)
    return plan


class TestFixtureMatrix:
    """Verify classification and validator behaviour for each server fixture."""

    # --- clean_plain_docker ---

    def test_clean_plain_docker_classification(self):
        snap = _make_snapshot(
            reverse_proxy=ReverseProxy.none,
            deployment_manager=DeploymentManager.plain_docker,
            confidence=0.95,
            evidence=["no reverse proxy detected", "docker daemon running"],
        )
        assert snap.detection.reverse_proxy == ReverseProxy.none
        assert snap.detection.deployment_manager in (
            DeploymentManager.none, DeploymentManager.plain_docker
        )
        assert snap.detection.confidence >= 0.8

    def test_clean_plain_docker_host_port_allowed_if_port_free(self):
        snap = _make_snapshot(
            reverse_proxy=ReverseProxy.none,
            deployment_manager=DeploymentManager.plain_docker,
            listening_ports=[],  # port 8080 is free
        )
        plan = _minimal_plan(
            "docker_compose_with_host_port",
            detected_dm="plain_docker",
            detected_rp="none",
            port_plan={"host_port": 8080},
        )
        result = validate_deployment_plan(plan, snap)
        port_checks = [c for c in result.checks if "8080" in c.name]
        assert all(c.passed for c in port_checks), (
            "Port 8080 is free — host-port bind must be allowed"
        )

    # --- docker_compose_stack_no_proxy ---

    def test_docker_compose_stack_no_proxy_classification(self):
        snap = _make_snapshot(
            deployment_manager=DeploymentManager.docker_compose,
            reverse_proxy=ReverseProxy.none,
            confidence=0.88,
            evidence=["compose project labels detected"],
            containers=[
                ContainerInfo(
                    id="abc1", name="myapp_web_1", image="nginx:latest",
                    status="Up", labels={"com.docker.compose.project": "myapp"}
                )
            ],
        )
        assert snap.detection.deployment_manager in (
            DeploymentManager.docker_compose, DeploymentManager.plain_docker
        )
        assert snap.detection.reverse_proxy == ReverseProxy.none

    def test_docker_compose_stack_no_proxy_compose_conflict_blocked(self):
        """Cannot overwrite existing compose stack containers without ownership."""
        snap = _make_snapshot(
            deployment_manager=DeploymentManager.docker_compose,
            reverse_proxy=ReverseProxy.none,
            containers=[
                ContainerInfo(
                    id="abc1", name="existingapp_web_1", image="nginx:latest",
                    status="Up", labels={"com.docker.compose.project": "existingapp"}
                )
            ],
        )
        plan = _minimal_plan(
            "docker_compose_private",
            detected_dm="docker_compose",
            detected_rp="none",
            compose_project_name="existingapp",
            deployment_actions=[
                {"action_type": "DOCKER_COMPOSE_UP",
                 "params": {"project_name": "existingapp",
                            "compose_file": "/opt/app/docker-compose.yml"}}
            ],
        )
        result = validate_deployment_plan(plan, snap)
        conflict_checks = [c for c in result.checks
                           if "compose_project_conflict" in c.name and not c.passed]
        assert conflict_checks, (
            "Existing compose project without Automatron ownership must be blocked"
        )

    # --- traefik_existing_routers ---

    def test_traefik_existing_routers_classification(self):
        snap = _make_snapshot(
            reverse_proxy=ReverseProxy.traefik,
            deployment_manager=DeploymentManager.traefik,
            confidence=0.95,
            evidence=["traefik container detected", "traefik labels on containers"],
            containers=[
                ContainerInfo(
                    id="tr1", name="traefik", image="traefik:v2.10",
                    status="Up",
                    labels={"traefik.enable": "true"},
                    ports=[{"host_port": 80}, {"host_port": 443}],
                )
            ],
            listening_ports=[
                ListeningPort(port=80, process="traefik"),
                ListeningPort(port=443, process="traefik"),
            ],
        )
        assert snap.detection.reverse_proxy == ReverseProxy.traefik
        assert snap.detection.confidence >= 0.8

    def test_traefik_direct_bind_80_443_blocked(self):
        snap = _make_snapshot(
            reverse_proxy=ReverseProxy.traefik,
            deployment_manager=DeploymentManager.traefik,
            listening_ports=[
                ListeningPort(port=80, process="traefik"),
                ListeningPort(port=443, process="traefik"),
            ],
        )
        plan = _minimal_plan(
            "docker_compose_with_host_port",
            detected_dm="traefik",
            detected_rp="traefik",
            port_plan={"host_port": 80},
        )
        result = validate_deployment_plan(plan, snap)
        blocking = [c for c in result.checks if not c.passed and c.blocking]
        assert blocking, "Direct bind to 80/443 owned by Traefik must be blocked"

    def test_traefik_reuse_existing_strategy_preferred(self):
        """reuse_existing_traefik strategy must not trigger proxy conflict check."""
        snap = _make_snapshot(
            reverse_proxy=ReverseProxy.traefik,
            deployment_manager=DeploymentManager.traefik,
            listening_ports=[
                ListeningPort(port=80, process="traefik"),
            ],
        )
        plan = _minimal_plan(
            "reuse_existing_traefik",
            detected_dm="traefik",
            detected_rp="traefik",
        )
        result = validate_deployment_plan(plan, snap)
        proxy_conflict = [c for c in result.checks
                          if "no_port_conflict_with_proxy" in c.name and not c.passed]
        assert not proxy_conflict, (
            "reuse_existing_traefik must not trigger proxy port conflict"
        )

    # --- kamal_v1_traefik ---

    def test_kamal_v1_traefik_classification(self):
        snap = _make_snapshot(
            deployment_manager=DeploymentManager.kamal_v1,
            reverse_proxy=ReverseProxy.traefik,
            confidence=0.9,
            evidence=["kamal v1 labels", "traefik container"],
            containers=[
                ContainerInfo(
                    id="tr1", name="traefik", image="traefik:v2",
                    status="Up", labels={}
                ),
                ContainerInfo(
                    id="ka1", name="myapp-web-1", image="myapp:latest",
                    status="Up",
                    labels={"sh.kamal.role": "web", "sh.kamal.version": "1"},
                ),
            ],
        )
        assert snap.detection.deployment_manager == DeploymentManager.kamal_v1
        assert snap.detection.reverse_proxy == ReverseProxy.traefik

    def test_kamal_v1_generic_traefik_install_blocked(self):
        """Can't install/use Traefik when kamal_v1 manages the host — use kamal_v1_compatible."""
        snap = _make_snapshot(
            deployment_manager=DeploymentManager.kamal_v1,
            reverse_proxy=ReverseProxy.traefik,
            listening_ports=[ListeningPort(port=80, process="traefik")],
        )
        plan = _minimal_plan(
            "docker_compose_with_host_port",
            detected_dm="kamal_v1",
            detected_rp="traefik",
            port_plan={"host_port": 80},
        )
        result = validate_deployment_plan(plan, snap)
        blocking = [c for c in result.checks if not c.passed and c.blocking]
        assert blocking, "host-port strategy on kamal_v1/traefik host must be blocked"

    # --- kamal_v2_kamal_proxy ---

    def test_kamal_v2_kamal_proxy_classification(self):
        snap = _make_snapshot(
            deployment_manager=DeploymentManager.kamal_v2,
            reverse_proxy=ReverseProxy.kamal_proxy,
            confidence=0.92,
            evidence=["kamal-proxy container", "kamal v2 labels"],
            containers=[
                ContainerInfo(
                    id="kp1", name="kamal-proxy", image="basecamp/kamal-proxy:latest",
                    status="Up",
                    labels={"sh.kamal.role": "proxy"},
                    ports=[{"host_port": 80}, {"host_port": 443}],
                )
            ],
            listening_ports=[
                ListeningPort(port=80, process="kamal-proxy"),
                ListeningPort(port=443, process="kamal-proxy"),
            ],
        )
        assert snap.detection.deployment_manager == DeploymentManager.kamal_v2
        assert snap.detection.reverse_proxy == ReverseProxy.kamal_proxy

    def test_kamal_v2_traefik_strategy_blocked(self):
        snap = _make_snapshot(
            deployment_manager=DeploymentManager.kamal_v2,
            reverse_proxy=ReverseProxy.kamal_proxy,
            listening_ports=[ListeningPort(port=80, process="kamal-proxy")],
        )
        plan = _minimal_plan(
            "reuse_existing_traefik",
            detected_dm="kamal_v2",
            detected_rp="kamal_proxy",
        )
        result = validate_deployment_plan(plan, snap)
        blocking = [c for c in result.checks if not c.passed and c.blocking]
        assert blocking, "Traefik strategy on kamal_v2/kamal-proxy host must be blocked"

    def test_kamal_v2_direct_bind_80_443_blocked(self):
        snap = _make_snapshot(
            deployment_manager=DeploymentManager.kamal_v2,
            reverse_proxy=ReverseProxy.kamal_proxy,
            listening_ports=[ListeningPort(port=80, process="kamal-proxy")],
        )
        plan = _minimal_plan(
            "docker_compose_with_host_port",
            detected_dm="kamal_v2",
            detected_rp="kamal_proxy",
            port_plan={"host_port": 80},
        )
        result = validate_deployment_plan(plan, snap)
        blocking = [c for c in result.checks if not c.passed and c.blocking]
        assert blocking, "Direct bind to 80/443 on kamal_v2 host must be blocked"

    # --- nginx_owns_80_443 ---

    def test_nginx_owns_80_443_classification(self):
        snap = _make_snapshot(
            reverse_proxy=ReverseProxy.nginx,
            deployment_manager=DeploymentManager.none,
            confidence=0.93,
            evidence=["nginx process on port 80", "nginx process on port 443"],
            listening_ports=[
                ListeningPort(port=80, process="nginx"),
                ListeningPort(port=443, process="nginx"),
            ],
        )
        assert snap.detection.reverse_proxy == ReverseProxy.nginx

    def test_nginx_direct_bind_80_443_blocked(self):
        snap = _make_snapshot(
            reverse_proxy=ReverseProxy.nginx,
            listening_ports=[
                ListeningPort(port=80, process="nginx"),
                ListeningPort(port=443, process="nginx"),
            ],
        )
        plan = _minimal_plan(
            "docker_compose_with_host_port",
            detected_dm="none",
            detected_rp="nginx",
            port_plan={"host_port": 80},
        )
        result = validate_deployment_plan(plan, snap)
        blocking = [c for c in result.checks if not c.passed and c.blocking]
        assert blocking, "Direct bind to 80 owned by Nginx must be blocked"

    def test_nginx_strategy_should_be_behind_existing_nginx(self):
        """Strategy 'behind_existing_nginx' must not trigger proxy conflict."""
        snap = _make_snapshot(
            reverse_proxy=ReverseProxy.nginx,
            listening_ports=[ListeningPort(port=80, process="nginx")],
        )
        plan = _minimal_plan(
            "behind_existing_nginx",
            detected_dm="none",
            detected_rp="nginx",
        )
        result = validate_deployment_plan(plan, snap)
        # behind_existing_nginx is not in _HOST_BIND_STRATEGIES so no proxy conflict
        proxy_conflict = [c for c in result.checks
                          if "no_port_conflict_with_proxy" in c.name and not c.passed]
        assert not proxy_conflict, (
            "behind_existing_nginx strategy must not trigger proxy port conflict"
        )

    # --- caddy_owns_80_443 ---

    def test_caddy_owns_80_443_classification(self):
        snap = _make_snapshot(
            reverse_proxy=ReverseProxy.caddy,
            deployment_manager=DeploymentManager.none,
            confidence=0.91,
            evidence=["caddy process on port 80"],
            listening_ports=[
                ListeningPort(port=80, process="caddy"),
                ListeningPort(port=443, process="caddy"),
            ],
        )
        assert snap.detection.reverse_proxy == ReverseProxy.caddy

    def test_caddy_direct_bind_80_443_blocked(self):
        snap = _make_snapshot(
            reverse_proxy=ReverseProxy.caddy,
            listening_ports=[
                ListeningPort(port=80, process="caddy"),
                ListeningPort(port=443, process="caddy"),
            ],
        )
        plan = _minimal_plan(
            "docker_compose_with_host_port",
            detected_dm="none",
            detected_rp="caddy",
            port_plan={"host_port": 80},
        )
        result = validate_deployment_plan(plan, snap)
        blocking = [c for c in result.checks if not c.passed and c.blocking]
        assert blocking, "Direct bind to 80 owned by Caddy must be blocked"

    # --- mixed_traefik_nginx_conflict ---

    def test_mixed_traefik_nginx_conflict_classification(self):
        snap = _make_snapshot(
            deployment_manager=DeploymentManager.mixed,
            reverse_proxy=ReverseProxy.unknown,
            confidence=0.4,
            evidence=["traefik container detected", "nginx process on port 80 detected",
                      "conflicting ownership"],
            containers=[
                ContainerInfo(
                    id="tr1", name="traefik", image="traefik:v2", status="Up", labels={}
                )
            ],
            listening_ports=[
                ListeningPort(port=80, process="nginx"),
            ],
        )
        assert snap.detection.deployment_manager == DeploymentManager.mixed

    def test_mixed_routing_setup_blocked(self):
        snap = _make_snapshot(
            deployment_manager=DeploymentManager.mixed,
            reverse_proxy=ReverseProxy.unknown,
            listening_ports=[ListeningPort(port=80, process="nginx")],
        )
        plan = _minimal_plan(
            "docker_compose_private",
            detected_dm="mixed",
            detected_rp="unknown",
        )
        result = validate_deployment_plan(plan, snap)
        blocking = [c for c in result.checks if not c.passed and c.blocking]
        assert blocking, "Mixed routing setup must block automatic deploy"

    def test_mixed_routing_manual_required_is_allowed(self):
        """manual_required strategy must not be blocked by the mixed routing check."""
        snap = _make_snapshot(
            deployment_manager=DeploymentManager.mixed,
            reverse_proxy=ReverseProxy.unknown,
        )
        plan = _minimal_plan(
            "manual_required",
            detected_dm="mixed",
            detected_rp="unknown",
        )
        result = validate_deployment_plan(plan, snap)
        mixed_blocks = [c for c in result.checks
                        if "mixed_routing_blocks_deploy" in c.name and not c.passed]
        assert not mixed_blocks, (
            "manual_required strategy must not be blocked by mixed routing check"
        )

    # --- unknown_process_owns_80_443 ---

    def test_unknown_process_owns_80_443_classification(self):
        snap = _make_snapshot(
            deployment_manager=DeploymentManager.unknown,
            reverse_proxy=ReverseProxy.unknown,
            confidence=0.15,
            evidence=["unknown process on port 80"],
            listening_ports=[
                ListeningPort(port=80, process="unknown"),
            ],
        )
        assert snap.detection.reverse_proxy == ReverseProxy.unknown

    def test_unknown_owner_80_443_blocks_deploy(self):
        snap = _make_snapshot(
            reverse_proxy=ReverseProxy.unknown,
            listening_ports=[ListeningPort(port=80, process="unknown_process")],
        )
        plan = _minimal_plan(
            "docker_compose_private",
            detected_dm="unknown",
            detected_rp="unknown",
        )
        result = validate_deployment_plan(plan, snap)
        blocking = [c for c in result.checks if not c.passed and c.blocking]
        assert blocking, (
            "Unknown owner of port 80/443 must block automatic deployment"
        )


# ---------------------------------------------------------------------------
# 3. Routing conflict validation
# ---------------------------------------------------------------------------

class TestRoutingConflictValidation:
    """Explicit validator tests for routing conflict scenarios."""

    def _base_snapshot(self, **kwargs) -> InventorySnapshot:
        return _make_snapshot(**kwargs)

    def test_domain_in_traefik_host_rule_blocked(self):
        """Domain already present in a Traefik Host() router rule must be blocked."""
        snap = _make_snapshot(
            reverse_proxy=ReverseProxy.traefik,
            containers=[
                ContainerInfo(
                    id="app1", name="existing_app", image="app:latest",
                    status="Up",
                    labels={
                        "traefik.http.routers.app.rule": "Host(`myapp.example.com`)",
                    },
                )
            ],
        )
        plan = _minimal_plan(
            "reuse_existing_traefik",
            detected_dm="traefik",
            detected_rp="traefik",
            routing_plan={"domain": "myapp.example.com"},
        )
        result = validate_deployment_plan(plan, snap)
        domain_checks = [c for c in result.checks
                         if "domain_not_already_routed" in c.name and not c.passed]
        assert domain_checks, (
            "Domain already in Traefik Host() rule must be blocked"
        )

    def test_different_domain_in_traefik_host_rule_allowed(self):
        """Routing a new domain must not be blocked by an existing rule for a different domain."""
        snap = _make_snapshot(
            reverse_proxy=ReverseProxy.traefik,
            containers=[
                ContainerInfo(
                    id="app1", name="existing_app", image="app:latest",
                    status="Up",
                    labels={
                        "traefik.http.routers.app.rule": "Host(`other.example.com`)",
                    },
                )
            ],
        )
        plan = _minimal_plan(
            "reuse_existing_traefik",
            detected_dm="traefik",
            detected_rp="traefik",
            routing_plan={"domain": "myapp.example.com"},
        )
        result = validate_deployment_plan(plan, snap)
        domain_blocks = [c for c in result.checks
                         if "domain_not_already_routed" in c.name and not c.passed]
        assert not domain_blocks, "Different domain in Host() rule must not block new domain"

    def test_host_port_already_listening_blocked(self):
        snap = _make_snapshot(
            listening_ports=[ListeningPort(port=3000, process="node")],
        )
        plan = _minimal_plan(
            "docker_compose_with_host_port",
            detected_dm="none",
            detected_rp="none",
            port_plan={"host_port": 3000},
        )
        result = validate_deployment_plan(plan, snap)
        port_checks = [c for c in result.checks if "3000" in c.name and not c.passed]
        assert port_checks, "Port 3000 already in use must be blocked"

    def test_free_host_port_allowed(self):
        snap = _make_snapshot(
            listening_ports=[ListeningPort(port=8080, process="other")],
        )
        plan = _minimal_plan(
            "docker_compose_with_host_port",
            detected_dm="none",
            detected_rp="none",
            port_plan={"host_port": 9000},
        )
        result = validate_deployment_plan(plan, snap)
        port_blocks = [c for c in result.checks if "9000" in c.name and not c.passed]
        assert not port_blocks, "Free port 9000 must not be blocked"

    def test_docker_published_port_already_in_use_blocked(self):
        """Plan requests host port 8080 that another container already publishes."""
        snap = _make_snapshot(
            containers=[
                ContainerInfo(
                    id="running", name="existing_app", image="app:latest",
                    status="Up", labels={},
                    ports=[{"host_port": 8080, "container_port": 80}],
                )
            ],
        )
        plan = _minimal_plan(
            "docker_compose_with_host_port",
            detected_dm="none",
            detected_rp="none",
            port_plan={"host_port": 8080},
        )
        result = validate_deployment_plan(plan, snap)
        published_checks = [c for c in result.checks
                            if "docker_published_port" in c.name and not c.passed]
        assert published_checks, (
            "Docker published port already in use by container must be blocked"
        )

    def test_port_80_owned_by_nginx_direct_bind_blocked(self):
        snap = _make_snapshot(
            reverse_proxy=ReverseProxy.nginx,
            listening_ports=[ListeningPort(port=80, process="nginx")],
        )
        plan = _minimal_plan(
            "docker_compose_with_host_port",
            detected_dm="none",
            detected_rp="nginx",
            port_plan={"host_port": 80},
        )
        result = validate_deployment_plan(plan, snap)
        blocking = [c for c in result.checks if not c.passed and c.blocking]
        assert blocking, "Port 80 owned by nginx — direct bind must be blocked"

    def test_port_443_owned_by_caddy_direct_bind_blocked(self):
        snap = _make_snapshot(
            reverse_proxy=ReverseProxy.caddy,
            listening_ports=[ListeningPort(port=443, process="caddy")],
        )
        plan = _minimal_plan(
            "docker_compose_with_host_port",
            detected_dm="none",
            detected_rp="caddy",
            port_plan={"host_port": 443},
        )
        result = validate_deployment_plan(plan, snap)
        blocking = [c for c in result.checks if not c.passed and c.blocking]
        assert blocking, "Port 443 owned by caddy — direct bind must be blocked"

    def test_port_80_owned_by_traefik_direct_bind_blocked(self):
        snap = _make_snapshot(
            reverse_proxy=ReverseProxy.traefik,
            listening_ports=[ListeningPort(port=80, process="traefik")],
        )
        plan = _minimal_plan(
            "docker_compose_with_host_port",
            detected_dm="traefik",
            detected_rp="traefik",
            port_plan={"host_port": 80},
        )
        result = validate_deployment_plan(plan, snap)
        blocking = [c for c in result.checks if not c.passed and c.blocking]
        assert blocking, "Port 80 owned by traefik — direct bind must be blocked"

    def test_port_80_owned_by_kamal_proxy_direct_bind_blocked(self):
        snap = _make_snapshot(
            reverse_proxy=ReverseProxy.kamal_proxy,
            listening_ports=[ListeningPort(port=80, process="kamal-proxy")],
        )
        plan = _minimal_plan(
            "docker_compose_with_host_port",
            detected_dm="kamal_v2",
            detected_rp="kamal_proxy",
            port_plan={"host_port": 80},
        )
        result = validate_deployment_plan(plan, snap)
        blocking = [c for c in result.checks if not c.passed and c.blocking]
        assert blocking, "Port 80 owned by kamal-proxy — direct bind must be blocked"

    def test_kamal_v2_plan_uses_traefik_strategy_blocked(self):
        snap = _make_snapshot(
            deployment_manager=DeploymentManager.kamal_v2,
            reverse_proxy=ReverseProxy.kamal_proxy,
            listening_ports=[ListeningPort(port=80, process="kamal-proxy")],
        )
        plan = _minimal_plan(
            "reuse_existing_traefik",
            detected_dm="kamal_v2",
            detected_rp="kamal_proxy",
        )
        result = validate_deployment_plan(plan, snap)
        blocking = [c for c in result.checks if not c.passed and c.blocking]
        assert blocking, "Traefik strategy on kamal-proxy host must be blocked"

    def test_mixed_routing_setup_blocked(self):
        snap = _make_snapshot(
            deployment_manager=DeploymentManager.mixed,
            reverse_proxy=ReverseProxy.unknown,
        )
        plan = _minimal_plan(
            "docker_compose_private",
            detected_dm="mixed",
            detected_rp="unknown",
        )
        result = validate_deployment_plan(plan, snap)
        blocking = [c for c in result.checks if not c.passed and c.blocking]
        assert blocking, "Mixed routing setup must be blocked"

    def test_unknown_owner_of_80_443_blocked(self):
        snap = _make_snapshot(
            reverse_proxy=ReverseProxy.unknown,
            listening_ports=[ListeningPort(port=80, process="mystery_process")],
        )
        plan = _minimal_plan(
            "docker_compose_private",
            detected_dm="unknown",
            detected_rp="unknown",
        )
        result = validate_deployment_plan(plan, snap)
        blocking = [c for c in result.checks if not c.passed and c.blocking]
        assert blocking, "Unknown owner of port 80 must be blocked"


# ---------------------------------------------------------------------------
# 4. Container/network/volume conflict validation
# ---------------------------------------------------------------------------

class TestDockerSafetyValidation:
    """Validator must block plans that would overwrite unowned Docker resources."""

    def test_container_name_conflict_blocked(self):
        snap = _make_snapshot(
            containers=[
                ContainerInfo(
                    id="c1", name="my-app-container", image="app:latest",
                    status="Up", labels={},
                )
            ],
        )
        plan = _minimal_plan(
            "docker_compose_private",
            detected_dm="none",
            detected_rp="none",
            resource_names={"container_name": "my-app-container"},
        )
        result = validate_deployment_plan(plan, snap)
        conflict_checks = [c for c in result.checks
                           if "container_name_conflict" in c.name and not c.passed]
        assert conflict_checks, "Existing container name conflict must be blocked"

    def test_network_name_conflict_without_ownership_blocked(self):
        snap = _make_snapshot(
            networks=[{"Name": "myapp_network", "Labels": {}}],
        )
        plan = _minimal_plan(
            "docker_compose_private",
            detected_dm="none",
            detected_rp="none",
            deployment_actions=[
                {"action_type": "DOCKER_NETWORK_CREATE",
                 "params": {"network_name": "myapp_network", "driver": "bridge"}}
            ],
        )
        result = validate_deployment_plan(plan, snap)
        conflict_checks = [c for c in result.checks
                           if "network_conflict" in c.name and not c.passed]
        assert conflict_checks, (
            "Docker network without Automatron ownership must be blocked"
        )

    def test_volume_name_conflict_without_ownership_blocked(self):
        snap = _make_snapshot(
            volumes=[{"Name": "myapp_data", "Labels": {}}],
        )
        plan = _minimal_plan(
            "docker_compose_private",
            detected_dm="none",
            detected_rp="none",
            deployment_actions=[
                {"action_type": "DOCKER_VOLUME_CREATE",
                 "params": {"volume_name": "myapp_data"}}
            ],
        )
        result = validate_deployment_plan(plan, snap)
        conflict_checks = [c for c in result.checks
                           if "volume_conflict" in c.name and not c.passed]
        assert conflict_checks, (
            "Docker volume without Automatron ownership must be blocked"
        )

    def test_compose_project_name_conflict_blocked(self):
        """Compose project with same name as existing unowned containers must be blocked."""
        snap = _make_snapshot(
            containers=[
                ContainerInfo(
                    id="c1", name="mystack_web_1", image="web:latest",
                    status="Up", labels={"com.docker.compose.project": "mystack"},
                )
            ],
        )
        plan = _minimal_plan(
            "docker_compose_private",
            detected_dm="docker_compose",
            detected_rp="none",
            deployment_actions=[
                {"action_type": "DOCKER_COMPOSE_UP",
                 "params": {"project_name": "mystack",
                            "compose_file": "/opt/app/docker-compose.yml"}}
            ],
        )
        result = validate_deployment_plan(plan, snap)
        conflict_checks = [c for c in result.checks
                           if "compose_project_conflict" in c.name and not c.passed]
        assert conflict_checks, "Compose project name conflict with unowned containers must be blocked"

    def test_network_with_automatron_ownership_allowed(self):
        """Network with Automatron ownership label must not be blocked."""
        snap = _make_snapshot(
            networks=[{"Name": "myapp_network", "Labels": {"automatron.owned": "true"}}],
        )
        plan = _minimal_plan(
            "docker_compose_private",
            detected_dm="none",
            detected_rp="none",
            deployment_actions=[
                {"action_type": "DOCKER_NETWORK_CREATE",
                 "params": {"network_name": "myapp_network", "driver": "bridge"}}
            ],
        )
        result = validate_deployment_plan(plan, snap)
        hard_conflict = [c for c in result.checks
                         if "network_conflict" in c.name and not c.passed and c.blocking]
        assert not hard_conflict, (
            "Automatron-owned network must not be hard-blocked (may pass or warn)"
        )

    def test_volume_with_automatron_ownership_allowed_or_warning(self):
        """Volume with Automatron ownership label must not be hard-blocked."""
        snap = _make_snapshot(
            volumes=[{"Name": "myapp_data", "Labels": {"automatron.owned": "true"}}],
        )
        plan = _minimal_plan(
            "docker_compose_private",
            detected_dm="none",
            detected_rp="none",
            deployment_actions=[
                {"action_type": "DOCKER_VOLUME_CREATE",
                 "params": {"volume_name": "myapp_data"}}
            ],
        )
        result = validate_deployment_plan(plan, snap)
        hard_conflict = [c for c in result.checks
                         if "volume_conflict" in c.name and not c.passed and c.blocking]
        assert not hard_conflict, (
            "Automatron-owned volume must not be hard-blocked (may pass or warn)"
        )

    def test_no_conflicts_passes_cleanly(self):
        snap = _make_snapshot()
        plan = _minimal_plan(
            "docker_compose_private",
            detected_dm="none",
            detected_rp="none",
            deployment_actions=[
                {"action_type": "DOCKER_NETWORK_CREATE",
                 "params": {"network_name": "brandnew_network", "driver": "bridge"}},
                {"action_type": "DOCKER_VOLUME_CREATE",
                 "params": {"volume_name": "brandnew_volume"}},
            ],
        )
        result = validate_deployment_plan(plan, snap)
        conflict_blocks = [c for c in result.checks
                           if ("network_conflict" in c.name or "volume_conflict" in c.name
                               or "container_name_conflict" in c.name)
                           and not c.passed]
        assert not conflict_blocks, "No conflicts → must pass cleanly"
        safety_passed = [c for c in result.checks
                         if "docker_safety_checks_passed" in c.name and c.passed]
        assert safety_passed, "docker_safety_checks_passed must be present when no conflicts"
