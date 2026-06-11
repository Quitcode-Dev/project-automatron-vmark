"""Regression tests for the inventory connectivity gate.

Bug: every SSH command in inventory.py goes through `_safe_run`, which swallows
errors and returns "". A fully failed connection (auth rejected, host
unreachable, host-key mismatch) therefore produced a deceptively "ok" snapshot
with 0 containers and a spurious ~0.5 detection confidence, instead of a visible
error. `_probe_connectivity` gates the collection on one real command so the
failure surfaces as status="error" with a meaningful message.
"""

from __future__ import annotations

from unittest.mock import patch

import aiosqlite
import pytest

from orchestrator.docker_deployment_ai import inventory as inv
from orchestrator.docker_deployment_ai.inventory import _probe_connectivity, collect_inventory
from orchestrator.docker_deployment_ai.models import DeploymentTarget
from orchestrator.docker_deployment_ai.schema import init_deployment_schema
from orchestrator.docker_deployment_ai.ssh_client import SSHError


class _FakeClient:
    """Stand-in for SSHClient with a scripted `run` result."""

    def __init__(self, result=None, *, raise_exc=None, **_kwargs):
        self._result = result or (0, "root\n", "")
        self._raise = raise_exc

    def run(self, _cmd, *, timeout=30):  # noqa: ARG002 - signature parity
        if self._raise:
            raise self._raise
        return self._result


# ---------------------------------------------------------------------------
# Unit: _probe_connectivity
# ---------------------------------------------------------------------------

class TestProbeConnectivity:
    def test_ok_connection_returns_none(self):
        assert _probe_connectivity(_FakeClient((0, "root\n", ""))) is None

    def test_ssh_level_failure_255_is_reported(self):
        err = _probe_connectivity(
            _FakeClient((255, "", "Permission denied (publickey)."))
        )
        assert err is not None
        assert "SSH connection failed" in err
        assert "Permission denied" in err

    def test_255_without_stderr_falls_back_to_exit_code(self):
        err = _probe_connectivity(_FakeClient((255, "", "")))
        assert err == "SSH connection failed: exit code 255"

    def test_nonzero_with_no_output_is_reported(self):
        err = _probe_connectivity(_FakeClient((127, "", "command not found")))
        assert err is not None
        assert "SSH probe failed" in err

    def test_ssh_error_is_wrapped(self):
        err = _probe_connectivity(
            _FakeClient(raise_exc=SSHError("ssh binary not found"))
        )
        assert err is not None
        assert "SSH unavailable" in err

    def test_daemon_down_but_host_reachable_passes(self):
        # whoami succeeds (rc 0) even if Docker is later unreachable — that is a
        # valid inventory, not a connection error.
        assert _probe_connectivity(_FakeClient((0, "deploy\n", ""))) is None


# ---------------------------------------------------------------------------
# Integration: collect_inventory persists an error snapshot on failed connect
# ---------------------------------------------------------------------------

async def _make_db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(":memory:")
    # deploy_runs must exist before init_deployment_schema extends it
    await db.execute(
        "CREATE TABLE IF NOT EXISTS deploy_runs (id TEXT PRIMARY KEY, status TEXT)"
    )
    await init_deployment_schema(db)
    await db.commit()
    db.row_factory = aiosqlite.Row
    return db


def _target() -> DeploymentTarget:
    return DeploymentTarget(
        id="t-1",
        project_id="p-1",
        name="lukasz",
        host="204.168.188.5",
        ssh_user="root",
        app_name="automatron-tes",
        deploy_path="/opt/automatron/apps",
        auth_reference="/root/.ssh/automatron_deploy",
    )


@pytest.mark.asyncio
async def test_failed_connection_yields_error_snapshot():
    db = await _make_db()
    try:
        fake = _FakeClient((255, "", "Permission denied (publickey)."))
        with patch.object(inv, "SSHClient", return_value=fake):
            snap_id, snapshot = await collect_inventory(_target(), db)

        assert snapshot.error is not None
        assert "Permission denied" in snapshot.error

        cur = await db.execute(
            "SELECT status, error_message, confidence "
            "FROM deployment_inventory_snapshots WHERE id = ?",
            (snap_id,),
        )
        row = await cur.fetchone()
        assert row["status"] == "error"
        assert "Permission denied" in row["error_message"]
        # No misleading ~0.5 confidence on a dead connection
        assert row["confidence"] == 0.0
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_reachable_host_proceeds_to_ok_snapshot():
    db = await _make_db()
    try:
        # whoami passes; every other command returns empty (daemon down etc.)
        fake = _FakeClient((0, "root\n", ""))
        with patch.object(inv, "SSHClient", return_value=fake):
            snap_id, snapshot = await collect_inventory(_target(), db)

        assert snapshot.error is None
        cur = await db.execute(
            "SELECT status FROM deployment_inventory_snapshots WHERE id = ?",
            (snap_id,),
        )
        row = await cur.fetchone()
        assert row["status"] == "ok"
    finally:
        await db.close()
