"""Staging readiness regression tests — P1 pass.

Covers:
  1. New param validators: validate_path_under_deploy_dir, validate_env_file_path,
     validate_file_content.
  2. ExecutorSSHClient: run_upload_file / run_write_env_file use stdin (no shell
     interpretation of content); no public generic run() method.
  3. Executor: UPLOAD_FILE and WRITE_ENV_FILE are now implemented; DOCKER_LOGIN
     and ROLLBACK_TO_PREVIOUS_RELEASE remain in _NOT_IMPLEMENTED_ACTIONS.
  4. Validator: plans containing DOCKER_LOGIN or ROLLBACK_TO_PREVIOUS_RELEASE
     are blocked before execution.
  5. Integration sequence: CREATE_DIRECTORY → UPLOAD_FILE compose →
     DOCKER_COMPOSE_CONFIG → DOCKER_COMPOSE_UP → DOCKER_COMPOSE_PS,
     all with SSH mocked, verifying the full executor path end-to-end.
"""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

# Stub jose so orchestrator.api.routes can be imported if needed
for _jose_mod in ("jose", "jose.jwe", "jose.jwt", "jose.exceptions"):
    sys.modules.setdefault(_jose_mod, MagicMock())


# ---------------------------------------------------------------------------
# 1. New param validators
# ---------------------------------------------------------------------------

class TestValidatePathUnderDeployDir:
    def test_path_inside_dir_passes(self):
        from orchestrator.docker_deployment_ai.param_validators import validate_path_under_deploy_dir
        result = validate_path_under_deploy_dir("/opt/app/docker-compose.yml", "/opt/app")
        assert result == "/opt/app/docker-compose.yml"

    def test_nested_path_passes(self):
        from orchestrator.docker_deployment_ai.param_validators import validate_path_under_deploy_dir
        result = validate_path_under_deploy_dir("/opt/app/config/nginx.conf", "/opt/app")
        assert result == "/opt/app/config/nginx.conf"

    def test_path_outside_dir_rejected(self):
        from orchestrator.docker_deployment_ai.param_validators import (
            validate_path_under_deploy_dir, ParameterValidationError,
        )
        with pytest.raises(ParameterValidationError, match="outside deploy directory"):
            validate_path_under_deploy_dir("/etc/cron.d/evil", "/opt/app")

    def test_root_escape_rejected(self):
        from orchestrator.docker_deployment_ai.param_validators import (
            validate_path_under_deploy_dir, ParameterValidationError,
        )
        with pytest.raises(ParameterValidationError):
            validate_path_under_deploy_dir("/root/.ssh/authorized_keys", "/opt/app")

    def test_sibling_dir_rejected(self):
        from orchestrator.docker_deployment_ai.param_validators import (
            validate_path_under_deploy_dir, ParameterValidationError,
        )
        # /opt/app2 is not under /opt/app
        with pytest.raises(ParameterValidationError, match="outside deploy directory"):
            validate_path_under_deploy_dir("/opt/app2/file", "/opt/app")

    def test_traversal_in_path_rejected(self):
        from orchestrator.docker_deployment_ai.param_validators import (
            validate_path_under_deploy_dir, ParameterValidationError,
        )
        with pytest.raises(ParameterValidationError, match="traversal"):
            validate_path_under_deploy_dir("/opt/app/../../etc/shadow", "/opt/app")

    def test_deploy_path_itself_rejected(self):
        """Bare deploy_path with no filename component is not a valid file destination."""
        from orchestrator.docker_deployment_ai.param_validators import (
            validate_path_under_deploy_dir, ParameterValidationError,
        )
        with pytest.raises(ParameterValidationError, match="outside deploy directory"):
            validate_path_under_deploy_dir("/opt/app", "/opt/app")

    def test_trailing_slash_in_deploy_path_handled(self):
        from orchestrator.docker_deployment_ai.param_validators import validate_path_under_deploy_dir
        # deploy_path with trailing slash should still work
        result = validate_path_under_deploy_dir("/opt/app/docker-compose.yml", "/opt/app/")
        assert result == "/opt/app/docker-compose.yml"


class TestValidateEnvFilePath:
    def test_dotenv_passes(self):
        from orchestrator.docker_deployment_ai.param_validators import validate_env_file_path
        assert validate_env_file_path("/opt/app/.env", "/opt/app") == "/opt/app/.env"

    def test_dotenv_production_passes(self):
        from orchestrator.docker_deployment_ai.param_validators import validate_env_file_path
        assert validate_env_file_path("/opt/app/.env.production", "/opt/app") == "/opt/app/.env.production"

    def test_app_env_suffix_passes(self):
        from orchestrator.docker_deployment_ai.param_validators import validate_env_file_path
        assert validate_env_file_path("/opt/app/app.env", "/opt/app") == "/opt/app/app.env"

    def test_non_env_file_rejected(self):
        from orchestrator.docker_deployment_ai.param_validators import (
            validate_env_file_path, ParameterValidationError,
        )
        with pytest.raises(ParameterValidationError, match=r"\.env"):
            validate_env_file_path("/opt/app/docker-compose.yml", "/opt/app")

    def test_outside_deploy_dir_rejected(self):
        from orchestrator.docker_deployment_ai.param_validators import (
            validate_env_file_path, ParameterValidationError,
        )
        with pytest.raises(ParameterValidationError, match="outside deploy directory"):
            validate_env_file_path("/etc/.env", "/opt/app")

    def test_traversal_rejected(self):
        from orchestrator.docker_deployment_ai.param_validators import (
            validate_env_file_path, ParameterValidationError,
        )
        with pytest.raises(ParameterValidationError):
            validate_env_file_path("/opt/app/../../.env", "/opt/app")


class TestValidateFileContent:
    def test_valid_content_passes(self):
        from orchestrator.docker_deployment_ai.param_validators import validate_file_content
        content = "version: '3'\nservices:\n  web:\n    image: nginx\n"
        assert validate_file_content(content) == content

    def test_empty_content_passes(self):
        from orchestrator.docker_deployment_ai.param_validators import validate_file_content
        assert validate_file_content("") == ""

    def test_oversized_content_rejected(self):
        from orchestrator.docker_deployment_ai.param_validators import (
            validate_file_content, ParameterValidationError,
        )
        big = "x" * (600 * 1024)  # 600 KB > 512 KB limit
        with pytest.raises(ParameterValidationError, match="maximum allowed size"):
            validate_file_content(big)

    def test_non_string_rejected(self):
        from orchestrator.docker_deployment_ai.param_validators import (
            validate_file_content, ParameterValidationError,
        )
        with pytest.raises(ParameterValidationError, match="must be a string"):
            validate_file_content(123)  # type: ignore[arg-type]

    def test_shell_metacharacters_in_content_pass(self):
        """Shell metachars in content are safe — content goes via stdin, not shell."""
        from orchestrator.docker_deployment_ai.param_validators import validate_file_content
        content = "API_KEY=abc;def\nSECRET=$(id)\nPASSWORD=`whoami`\n"
        # Must NOT raise — these are valid env file values delivered via stdin
        result = validate_file_content(content)
        assert result == content


# ---------------------------------------------------------------------------
# 2. ExecutorSSHClient — stdin-based file upload
# ---------------------------------------------------------------------------

class TestExecutorSSHClientFileUpload:
    def test_no_public_run_method(self):
        """ExecutorSSHClient must not expose a generic public run()."""
        from orchestrator.docker_deployment_ai.executor_ssh_client import ExecutorSSHClient
        assert not hasattr(ExecutorSSHClient, "run"), (
            "Public run() would allow arbitrary SSH commands"
        )

    def test_run_upload_file_method_exists(self):
        from orchestrator.docker_deployment_ai.executor_ssh_client import ExecutorSSHClient
        assert hasattr(ExecutorSSHClient, "run_upload_file")

    def test_run_write_env_file_method_exists(self):
        from orchestrator.docker_deployment_ai.executor_ssh_client import ExecutorSSHClient
        assert hasattr(ExecutorSSHClient, "run_write_env_file")

    def test_run_upload_file_uses_stdin(self):
        """run_upload_file must pipe content via stdin, not via command string."""
        import subprocess
        from orchestrator.docker_deployment_ai.executor_ssh_client import ExecutorSSHClient

        client = ExecutorSSHClient(host="1.2.3.4", user="deploy")
        content = "version: '3'\nservices:\n  web:\n    image: nginx\n"

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            client.run_upload_file("/opt/app/docker-compose.yml", content)

        mock_run.assert_called_once()
        kwargs = mock_run.call_args
        # content must appear as the `input` kwarg, not in the command list
        assert kwargs.kwargs.get("input") == content or (
            len(kwargs.args) > 1 and kwargs.args[1] == content
        ), "Content must be passed as stdin input, not in the command string"
        # The command itself must not contain the content
        cmd = kwargs.args[0] if kwargs.args else kwargs.kwargs.get("args", [])
        cmd_str = " ".join(str(c) for c in cmd)
        assert "version: '3'" not in cmd_str

    def test_run_upload_file_quotes_remote_path(self):
        """remote_path must be shlex-quoted in the SSH command string."""
        from orchestrator.docker_deployment_ai.executor_ssh_client import ExecutorSSHClient

        client = ExecutorSSHClient(host="1.2.3.4", user="deploy")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            client.run_upload_file("/opt/my app/docker-compose.yml", "content")

        cmd = mock_run.call_args.args[0]
        remote_part = cmd[-1]  # last element is the remote command string
        # Path with space must be quoted
        assert "'/opt/my app/docker-compose.yml'" in remote_part or \
               '"/opt/my app/docker-compose.yml"' in remote_part, \
               f"Path not quoted in remote command: {remote_part!r}"

    def test_run_write_env_file_uses_stdin(self):
        """run_write_env_file must also use stdin."""
        from orchestrator.docker_deployment_ai.executor_ssh_client import ExecutorSSHClient

        client = ExecutorSSHClient(host="1.2.3.4", user="deploy")
        env_content = "API_KEY=secret\nDATABASE_URL=postgres://localhost/db\n"

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            client.run_write_env_file("/opt/app/.env", env_content)

        kwargs = mock_run.call_args
        stdin_input = kwargs.kwargs.get("input")
        assert stdin_input == env_content, "Env content must be passed via stdin"


# ---------------------------------------------------------------------------
# 3. Executor — UPLOAD_FILE and WRITE_ENV_FILE are now implemented
# ---------------------------------------------------------------------------

class TestExecutorUploadFileImplemented:
    @pytest.mark.asyncio
    async def test_upload_file_not_in_not_implemented(self):
        """UPLOAD_FILE must no longer be in _NOT_IMPLEMENTED_ACTIONS."""
        from orchestrator.docker_deployment_ai.executor import _NOT_IMPLEMENTED_ACTIONS
        assert "UPLOAD_FILE" not in _NOT_IMPLEMENTED_ACTIONS

    @pytest.mark.asyncio
    async def test_write_env_file_not_in_not_implemented(self):
        """WRITE_ENV_FILE must no longer be in _NOT_IMPLEMENTED_ACTIONS."""
        from orchestrator.docker_deployment_ai.executor import _NOT_IMPLEMENTED_ACTIONS
        assert "WRITE_ENV_FILE" not in _NOT_IMPLEMENTED_ACTIONS

    @pytest.mark.asyncio
    async def test_docker_login_still_not_implemented(self):
        from orchestrator.docker_deployment_ai.executor import _NOT_IMPLEMENTED_ACTIONS
        assert "DOCKER_LOGIN" in _NOT_IMPLEMENTED_ACTIONS

    @pytest.mark.asyncio
    async def test_rollback_to_previous_still_not_implemented(self):
        from orchestrator.docker_deployment_ai.executor import _NOT_IMPLEMENTED_ACTIONS
        assert "ROLLBACK_TO_PREVIOUS_RELEASE" in _NOT_IMPLEMENTED_ACTIONS

    @pytest.mark.asyncio
    async def test_upload_file_calls_run_upload_file(self):
        """UPLOAD_FILE action calls ssh.run_upload_file with validated params."""
        from orchestrator.docker_deployment_ai.executor import _execute_action, ExecutorSSHClient

        fake_ssh = MagicMock(spec=ExecutorSSHClient)
        fake_ssh.run_upload_file.return_value = (0, "", "")

        _, _, error = await _execute_action(
            {
                "action_type": "UPLOAD_FILE",
                "params": {
                    "path": "/opt/app/docker-compose.yml",
                    "content": "version: '3'\nservices:\n  web:\n    image: nginx\n",
                },
            },
            fake_ssh,
            deploy_path="/opt/app",
        )
        assert error is None
        fake_ssh.run_upload_file.assert_called_once_with(
            "/opt/app/docker-compose.yml",
            "version: '3'\nservices:\n  web:\n    image: nginx\n",
        )

    @pytest.mark.asyncio
    async def test_upload_file_path_outside_deploy_dir_rejected(self):
        """UPLOAD_FILE with path outside deploy_path returns error without SSH call."""
        from orchestrator.docker_deployment_ai.executor import _execute_action, ExecutorSSHClient

        fake_ssh = MagicMock(spec=ExecutorSSHClient)
        _, _, error = await _execute_action(
            {
                "action_type": "UPLOAD_FILE",
                "params": {
                    "path": "/etc/cron.d/evil",
                    "content": "* * * * * root id > /tmp/pwned",
                },
            },
            fake_ssh,
            deploy_path="/opt/app",
        )
        assert error is not None
        assert "outside deploy directory" in error or "validation" in error.lower()
        fake_ssh.run_upload_file.assert_not_called()

    @pytest.mark.asyncio
    async def test_write_env_file_calls_run_write_env_file(self):
        """WRITE_ENV_FILE action calls ssh.run_write_env_file with validated params."""
        from orchestrator.docker_deployment_ai.executor import _execute_action, ExecutorSSHClient

        fake_ssh = MagicMock(spec=ExecutorSSHClient)
        fake_ssh.run_write_env_file.return_value = (0, "", "")

        env_content = "DATABASE_URL=postgres://db/app\nREDIS_URL=redis://localhost\n"
        _, _, error = await _execute_action(
            {
                "action_type": "WRITE_ENV_FILE",
                "params": {"path": "/opt/app/.env", "content": env_content},
            },
            fake_ssh,
            deploy_path="/opt/app",
        )
        assert error is None
        fake_ssh.run_write_env_file.assert_called_once_with("/opt/app/.env", env_content)

    @pytest.mark.asyncio
    async def test_write_env_file_non_env_path_rejected(self):
        """WRITE_ENV_FILE rejects paths that don't look like .env files."""
        from orchestrator.docker_deployment_ai.executor import _execute_action, ExecutorSSHClient

        fake_ssh = MagicMock(spec=ExecutorSSHClient)
        _, _, error = await _execute_action(
            {
                "action_type": "WRITE_ENV_FILE",
                "params": {"path": "/opt/app/docker-compose.yml", "content": "x=y"},
            },
            fake_ssh,
            deploy_path="/opt/app",
        )
        assert error is not None
        fake_ssh.run_write_env_file.assert_not_called()

    @pytest.mark.asyncio
    async def test_docker_login_returns_not_implemented_error(self):
        """DOCKER_LOGIN still returns explicit error, never calls SSH."""
        from orchestrator.docker_deployment_ai.executor import _execute_action, ExecutorSSHClient

        fake_ssh = MagicMock(spec=ExecutorSSHClient)
        _, _, error = await _execute_action(
            {"action_type": "DOCKER_LOGIN", "params": {}},
            fake_ssh,
            deploy_path="/opt/app",
        )
        assert error is not None
        assert "not implemented" in error.lower()


# ---------------------------------------------------------------------------
# 4. Validator — blocks plans with unimplemented executor actions
# ---------------------------------------------------------------------------

class TestValidatorBlocksUnimplementedActions:
    def _make_base_plan(self) -> dict:
        return {
            "strategy": "docker_compose_private",
            "risk_level": "low",
            "deployment_actions": [],
            "rollback_plan": {"type": "compose_snapshot"},
        }

    def test_plan_with_docker_login_is_blocked(self):
        from orchestrator.docker_deployment_ai.validator import validate_deployment_plan
        from orchestrator.docker_deployment_ai.models import InventorySnapshot
        plan = self._make_base_plan()
        plan["deployment_actions"] = [{"action_type": "DOCKER_LOGIN", "params": {}}]
        snapshot = InventorySnapshot(target_id="t1", project_id="p1")
        result = validate_deployment_plan(plan, snapshot)
        assert result.status == "blocked"
        assert any("DOCKER_LOGIN" in e for e in result.blocking_errors)

    def test_plan_with_rollback_action_is_blocked(self):
        from orchestrator.docker_deployment_ai.validator import validate_deployment_plan
        from orchestrator.docker_deployment_ai.models import InventorySnapshot
        plan = self._make_base_plan()
        plan["deployment_actions"] = [
            {"action_type": "ROLLBACK_TO_PREVIOUS_RELEASE", "params": {}}
        ]
        snapshot = InventorySnapshot(target_id="t1", project_id="p1")
        result = validate_deployment_plan(plan, snapshot)
        assert result.status == "blocked"
        assert any("ROLLBACK_TO_PREVIOUS_RELEASE" in e for e in result.blocking_errors)

    def test_plan_without_unimplemented_actions_passes_capability_check(self):
        from orchestrator.docker_deployment_ai.validator import (
            _check_unimplemented_executor_actions,
        )
        plan = {
            "deployment_actions": [
                {"action_type": "CREATE_DIRECTORY", "params": {}},
                {"action_type": "UPLOAD_FILE", "params": {}},
                {"action_type": "DOCKER_COMPOSE_UP", "params": {}},
            ]
        }
        checks = _check_unimplemented_executor_actions(plan)
        assert all(c.passed for c in checks)

    def test_plan_with_upload_file_passes_capability_check(self):
        """UPLOAD_FILE is now implemented — it must NOT block the capability check."""
        from orchestrator.docker_deployment_ai.validator import (
            _check_unimplemented_executor_actions,
        )
        plan = {"deployment_actions": [{"action_type": "UPLOAD_FILE", "params": {}}]}
        checks = _check_unimplemented_executor_actions(plan)
        assert all(c.passed for c in checks)

    def test_plan_with_write_env_file_passes_capability_check(self):
        """WRITE_ENV_FILE is now implemented — it must NOT block the capability check."""
        from orchestrator.docker_deployment_ai.validator import (
            _check_unimplemented_executor_actions,
        )
        plan = {"deployment_actions": [{"action_type": "WRITE_ENV_FILE", "params": {}}]}
        checks = _check_unimplemented_executor_actions(plan)
        assert all(c.passed for c in checks)

    def test_multiple_unimplemented_each_generate_check(self):
        from orchestrator.docker_deployment_ai.validator import (
            _check_unimplemented_executor_actions,
        )
        plan = {
            "deployment_actions": [
                {"action_type": "DOCKER_LOGIN", "params": {}},
                {"action_type": "ROLLBACK_TO_PREVIOUS_RELEASE", "params": {}},
            ]
        }
        checks = _check_unimplemented_executor_actions(plan)
        blocked = [c for c in checks if not c.passed]
        assert len(blocked) == 2
        names = {c.name for c in blocked}
        assert "executor_unimplemented:DOCKER_LOGIN" in names
        assert "executor_unimplemented:ROLLBACK_TO_PREVIOUS_RELEASE" in names


# ---------------------------------------------------------------------------
# 5. Integration sequence — full mock end-to-end
#    CREATE_DIRECTORY → UPLOAD_FILE → DOCKER_COMPOSE_CONFIG →
#    DOCKER_COMPOSE_UP → DOCKER_COMPOSE_PS
# ---------------------------------------------------------------------------

class TestMinimalDeploymentSequence:
    """Verifies the full executor path for a minimal real deployment sequence."""

    @pytest.mark.asyncio
    async def test_five_step_sequence_completes(self):
        """All five steps complete successfully with SSH mocked."""
        import asyncio
        import aiosqlite
        from unittest.mock import patch as _patch
        from orchestrator.docker_deployment_ai.executor import execute_plan, ExecutorSSHClient

        # Build in-memory DB with required tables
        db = await aiosqlite.connect(":memory:")
        await db.execute(
            """CREATE TABLE deploy_runs (
                id TEXT PRIMARY KEY, project_id TEXT, status TEXT, branch TEXT,
                created_at TEXT, plan_id TEXT, target_id TEXT, started_by TEXT,
                started_at TEXT, health_status TEXT NOT NULL DEFAULT 'unknown',
                finished_at TEXT, current_step INTEGER,
                rollback_available INTEGER NOT NULL DEFAULT 0
            )"""
        )
        await db.execute(
            """CREATE TABLE deployment_run_steps (
                id TEXT PRIMARY KEY, run_id TEXT NOT NULL, step_index INTEGER NOT NULL,
                action_type TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                started_at TEXT, finished_at TEXT,
                stdout_excerpt TEXT, stderr_excerpt TEXT, error_message TEXT
            )"""
        )
        await db.execute(
            """CREATE TABLE deployment_approvals (
                id TEXT PRIMARY KEY, plan_id TEXT NOT NULL,
                plan_content_hash TEXT NOT NULL DEFAULT '',
                approved_by TEXT NOT NULL, approved_at TEXT NOT NULL,
                approval_note TEXT
            )"""
        )
        await db.execute(
            """INSERT INTO deploy_runs
            (id, project_id, status, branch, created_at, plan_id, target_id,
             started_by, started_at, health_status)
            VALUES ('run-1', 'proj-1', 'pending', 'main', '2024-01-01',
                    'plan-1', 'tgt-1', 'deploy@test.com', '2024-01-01', 'unknown')"""
        )
        await db.execute(
            """INSERT INTO deployment_approvals
            (id, plan_id, plan_content_hash, approved_by, approved_at)
            VALUES ('appr-1', 'plan-1', '', 'approver@test.com', '2024-01-01')"""
        )
        await db.commit()

        # The plan with the five-step sequence
        plan = {
            "strategy": "docker_compose_private",
            "risk_level": "low",
            "deployment_actions": [
                {
                    "action_type": "CREATE_DIRECTORY",
                    "params": {"path": "/opt/app"},
                },
                {
                    "action_type": "UPLOAD_FILE",
                    "params": {
                        "path": "/opt/app/docker-compose.yml",
                        "content": "version: '3'\nservices:\n  web:\n    image: nginx\n",
                    },
                },
                {
                    "action_type": "DOCKER_COMPOSE_CONFIG",
                    "params": {"compose_file": "docker-compose.yml"},
                },
                {
                    "action_type": "DOCKER_COMPOSE_UP",
                    "params": {
                        "compose_file": "docker-compose.yml",
                        "service": "",
                    },
                },
                {
                    "action_type": "DOCKER_COMPOSE_PS",
                    "params": {"compose_file": "docker-compose.yml"},
                },
            ],
        }

        # Fake target
        target = MagicMock()
        target.host = "1.2.3.4"
        target.ssh_user = "deploy"
        target.ssh_port = 22
        target.deploy_path = "/opt/app"
        target.auth_mode = "ssh_key"
        target.auth_reference = "/home/ci/.ssh/id_rsa"

        # Mock all SSH calls to succeed
        with patch.object(
            ExecutorSSHClient, "run_mkdir", return_value=(0, "", "")
        ), patch.object(
            ExecutorSSHClient, "run_upload_file", return_value=(0, "", "")
        ), patch.object(
            ExecutorSSHClient, "run_docker_compose_config", return_value=(0, "services ok", "")
        ), patch.object(
            ExecutorSSHClient, "run_docker_compose_up", return_value=(0, "started", "")
        ), patch.object(
            ExecutorSSHClient, "run_docker_compose_ps", return_value=(0, "[]", "")
        ):
            result = await execute_plan(
                plan_id="plan-1",
                plan=plan,
                target=target,
                run_id="run-1",
                project_id="proj-1",
                started_by="deploy@test.com",
                db=db,
            )

        await db.close()

        assert result["status"] == "completed", f"Unexpected result: {result}"
        assert result["steps_completed"] == 5
        assert result["steps_failed"] == 0

    @pytest.mark.asyncio
    async def test_sequence_aborts_on_upload_failure(self):
        """If UPLOAD_FILE fails, executor stops and returns failed status."""
        import aiosqlite
        from orchestrator.docker_deployment_ai.executor import execute_plan, ExecutorSSHClient

        db = await aiosqlite.connect(":memory:")
        await db.execute(
            """CREATE TABLE deploy_runs (
                id TEXT PRIMARY KEY, project_id TEXT, status TEXT, branch TEXT,
                created_at TEXT, plan_id TEXT, target_id TEXT, started_by TEXT,
                started_at TEXT, health_status TEXT NOT NULL DEFAULT 'unknown',
                finished_at TEXT, current_step INTEGER,
                rollback_available INTEGER NOT NULL DEFAULT 0
            )"""
        )
        await db.execute(
            """CREATE TABLE deployment_run_steps (
                id TEXT PRIMARY KEY, run_id TEXT NOT NULL, step_index INTEGER NOT NULL,
                action_type TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                started_at TEXT, finished_at TEXT,
                stdout_excerpt TEXT, stderr_excerpt TEXT, error_message TEXT
            )"""
        )
        await db.execute(
            """CREATE TABLE deployment_approvals (
                id TEXT PRIMARY KEY, plan_id TEXT NOT NULL,
                plan_content_hash TEXT NOT NULL DEFAULT '',
                approved_by TEXT NOT NULL, approved_at TEXT NOT NULL,
                approval_note TEXT
            )"""
        )
        await db.execute(
            """INSERT INTO deploy_runs
            (id, project_id, status, branch, created_at, plan_id, target_id,
             started_by, started_at, health_status)
            VALUES ('run-2', 'proj-1', 'pending', 'main', '2024-01-01',
                    'plan-2', 'tgt-1', 'deploy@test.com', '2024-01-01', 'unknown')"""
        )
        await db.execute(
            """INSERT INTO deployment_approvals
            (id, plan_id, plan_content_hash, approved_by, approved_at)
            VALUES ('appr-2', 'plan-2', '', 'approver@test.com', '2024-01-01')"""
        )
        await db.commit()

        plan = {
            "strategy": "docker_compose_private",
            "risk_level": "low",
            "deployment_actions": [
                {"action_type": "CREATE_DIRECTORY", "params": {"path": "/opt/app"}},
                {
                    "action_type": "UPLOAD_FILE",
                    "params": {
                        "path": "/opt/app/docker-compose.yml",
                        "content": "version: '3'\n",
                    },
                },
                # This step should NEVER run if UPLOAD_FILE fails
                {"action_type": "DOCKER_COMPOSE_UP", "params": {}},
            ],
        }

        target = MagicMock()
        target.host = "1.2.3.4"
        target.ssh_user = "deploy"
        target.ssh_port = 22
        target.deploy_path = "/opt/app"
        target.auth_mode = "ssh_key"
        target.auth_reference = None

        compose_up_mock = MagicMock(return_value=(0, "", ""))

        with patch.object(
            ExecutorSSHClient, "run_mkdir", return_value=(0, "", "")
        ), patch.object(
            ExecutorSSHClient, "run_upload_file",
            return_value=(1, "", "permission denied"),  # ← FAILURE
        ), patch.object(
            ExecutorSSHClient, "run_docker_compose_up", compose_up_mock
        ):
            result = await execute_plan(
                plan_id="plan-2",
                plan=plan,
                target=target,
                run_id="run-2",
                project_id="proj-1",
                started_by="deploy@test.com",
                db=db,
            )

        await db.close()

        assert result["status"] == "failed"
        assert result["steps_completed"] == 1  # only CREATE_DIRECTORY completed
        assert result["steps_failed"] == 1
        compose_up_mock.assert_not_called()  # step 3 never reached

    @pytest.mark.asyncio
    async def test_sequence_rejects_docker_login_action(self):
        """A plan containing DOCKER_LOGIN is rejected before any SSH call."""
        import aiosqlite
        from orchestrator.docker_deployment_ai.executor import execute_plan, ExecutorSSHClient

        db = await aiosqlite.connect(":memory:")
        await db.execute(
            """CREATE TABLE deploy_runs (
                id TEXT PRIMARY KEY, project_id TEXT, status TEXT, branch TEXT,
                created_at TEXT, plan_id TEXT, target_id TEXT, started_by TEXT,
                started_at TEXT, health_status TEXT NOT NULL DEFAULT 'unknown',
                finished_at TEXT, current_step INTEGER,
                rollback_available INTEGER NOT NULL DEFAULT 0
            )"""
        )
        await db.execute(
            """CREATE TABLE deployment_run_steps (
                id TEXT PRIMARY KEY, run_id TEXT NOT NULL, step_index INTEGER NOT NULL,
                action_type TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                started_at TEXT, finished_at TEXT,
                stdout_excerpt TEXT, stderr_excerpt TEXT, error_message TEXT
            )"""
        )
        await db.execute(
            """CREATE TABLE deployment_approvals (
                id TEXT PRIMARY KEY, plan_id TEXT NOT NULL,
                plan_content_hash TEXT NOT NULL DEFAULT '',
                approved_by TEXT NOT NULL, approved_at TEXT NOT NULL,
                approval_note TEXT
            )"""
        )
        await db.execute(
            """INSERT INTO deploy_runs
            (id, project_id, status, branch, created_at, plan_id, target_id,
             started_by, started_at, health_status)
            VALUES ('run-3', 'proj-1', 'pending', 'main', '2024-01-01',
                    'plan-3', 'tgt-1', 'deploy@test.com', '2024-01-01', 'unknown')"""
        )
        await db.execute(
            """INSERT INTO deployment_approvals
            (id, plan_id, plan_content_hash, approved_by, approved_at)
            VALUES ('appr-3', 'plan-3', '', 'approver@test.com', '2024-01-01')"""
        )
        await db.commit()

        plan = {
            "strategy": "docker_compose_private",
            "risk_level": "low",
            "deployment_actions": [
                {"action_type": "DOCKER_LOGIN", "params": {"registry": "ghcr.io"}},
                {"action_type": "DOCKER_COMPOSE_UP", "params": {}},
            ],
        }

        target = MagicMock()
        target.host = "1.2.3.4"
        target.ssh_user = "deploy"
        target.ssh_port = 22
        target.deploy_path = "/opt/app"
        target.auth_mode = "ssh_key"
        target.auth_reference = None

        compose_up_mock = MagicMock(return_value=(0, "", ""))

        with patch.object(ExecutorSSHClient, "run_docker_compose_up", compose_up_mock):
            result = await execute_plan(
                plan_id="plan-3",
                plan=plan,
                target=target,
                run_id="run-3",
                project_id="proj-1",
                started_by="deploy@test.com",
                db=db,
            )

        await db.close()

        # DOCKER_LOGIN is in _NOT_IMPLEMENTED_ACTIONS — action is executed and returns error
        # OR validator blocked it — either way, docker compose up must not have run
        compose_up_mock.assert_not_called()
        # The run should not have completed successfully
        assert result.get("status") in ("failed", "rejected")


# ---------------------------------------------------------------------------
# Hardening — Docker Compose v2 prerequisite validation
# ---------------------------------------------------------------------------

def _make_snapshot_with_binary_info(
    compose_v2: bool,
    binary_present: bool = True,
    daemon_reachable: bool = True,
    has_compose_actions: bool = True,
) -> tuple[dict, object]:
    """Return (plan_dict, InventorySnapshot) for compose v2 validation tests."""
    from orchestrator.docker_deployment_ai.models import InventorySnapshot, DetectionResult, DeploymentManager, ReverseProxy  # noqa: PLC0415

    actions = (
        [
            {"action_type": "CREATE_DIRECTORY", "params": {}},
            {"action_type": "DOCKER_COMPOSE_UP", "params": {}},
        ]
        if has_compose_actions
        else [{"action_type": "CREATE_DIRECTORY", "params": {}}]
    )
    plan = {
        "strategy": "docker_compose_with_host_port",
        "risk_level": "low",
        "deployment_actions": actions,
    }
    snapshot = InventorySnapshot(
        target_id="tgt-1",
        project_id="proj-1",
        docker_binary_info={
            "docker_binary_present": binary_present,
            "docker_daemon_reachable": daemon_reachable,
            "docker_compose_v2_available": compose_v2,
            "docker_info_error": None if daemon_reachable else "connection refused",
        },
        detection=DetectionResult(
            deployment_manager=DeploymentManager.none,
            reverse_proxy=ReverseProxy.none,
            confidence=0.5,
        ),
    )
    return plan, snapshot


class TestDockerComposeV2Validation:
    """Compose-based plans are blocked when Docker Compose v2 is absent.

    Covers:
      - compose v2 available → validation passes
      - only docker-compose v1 (binary present but plugin absent) → validation blocked
      - docker binary missing entirely → validation blocked
      - plan without compose actions → check passes even when v2 absent
      - snapshot without docker_binary_info (old format) → non-blocking pass
    """

    def test_compose_v2_available_passes(self):
        from orchestrator.docker_deployment_ai.validator import _check_docker_compose_v2  # noqa: PLC0415
        plan, snapshot = _make_snapshot_with_binary_info(compose_v2=True)
        check = _check_docker_compose_v2(plan, snapshot)
        assert check.passed, f"Expected pass, got: {check.message}"
        assert check.name == "docker_compose_v2_check"

    def test_compose_v1_only_blocked(self):
        """Binary present, daemon reachable, but compose plugin absent → blocked."""
        from orchestrator.docker_deployment_ai.validator import _check_docker_compose_v2  # noqa: PLC0415
        plan, snapshot = _make_snapshot_with_binary_info(
            compose_v2=False, binary_present=True, daemon_reachable=True
        )
        check = _check_docker_compose_v2(plan, snapshot)
        assert not check.passed
        assert check.blocking
        assert "compose v2" in check.message.lower() or "plugin" in check.message.lower()

    def test_docker_binary_missing_blocked(self):
        """No docker binary at all → blocked."""
        from orchestrator.docker_deployment_ai.validator import _check_docker_compose_v2  # noqa: PLC0415
        plan, snapshot = _make_snapshot_with_binary_info(
            compose_v2=False, binary_present=False
        )
        check = _check_docker_compose_v2(plan, snapshot)
        assert not check.passed
        assert check.blocking
        assert "docker" in check.message.lower()

    def test_no_compose_actions_skips_check(self):
        """Plans without compose actions pass regardless of v2 availability."""
        from orchestrator.docker_deployment_ai.validator import _check_docker_compose_v2  # noqa: PLC0415
        plan, snapshot = _make_snapshot_with_binary_info(
            compose_v2=False, has_compose_actions=False
        )
        check = _check_docker_compose_v2(plan, snapshot)
        assert check.passed

    def test_old_snapshot_without_binary_info_passes_nonblocking(self):
        """Snapshot without docker_binary_info (pre-hardening) must not block."""
        from orchestrator.docker_deployment_ai.validator import _check_docker_compose_v2  # noqa: PLC0415
        from orchestrator.docker_deployment_ai.models import InventorySnapshot, DetectionResult  # noqa: PLC0415
        plan = {
            "strategy": "docker_compose_with_host_port",
            "risk_level": "low",
            "deployment_actions": [{"action_type": "DOCKER_COMPOSE_UP", "params": {}}],
        }
        # Empty docker_binary_info (default) simulates old inventory
        snapshot = InventorySnapshot(
            target_id="tgt-x", project_id="proj-x",
            detection=DetectionResult(),
        )
        check = _check_docker_compose_v2(plan, snapshot)
        # Must pass (non-blocking warning, not a hard block)
        assert check.passed

    def test_full_validation_blocked_on_v1_host(self):
        """validate_deployment_plan returns blocked status when compose v2 absent."""
        from orchestrator.docker_deployment_ai.validator import validate_deployment_plan  # noqa: PLC0415
        plan, snapshot = _make_snapshot_with_binary_info(compose_v2=False)
        # Build a schema-valid plan
        plan["summary"] = "test"
        plan["docker_ai"] = {"provider": "litellm"}
        plan["detected_server_state"] = {"reverse_proxy": "none", "deployment_manager": "none"}
        plan["rollback_plan"] = {
            "type": "compose_snapshot",
            "previous_release_ref_required": False,
            "steps": [],
        }
        plan["secrets_required"] = []
        plan["blocking_questions"] = []
        plan["port_plan"] = {}
        plan["routing_plan"] = {}
        result = validate_deployment_plan(plan, snapshot)
        assert result.status == "blocked"
        assert any("compose_v2" in c.name for c in result.checks if not c.passed)

    def test_full_validation_passes_on_v2_host_with_no_containers(self):
        """Fresh clean host with docker binary + compose v2 + zero containers passes."""
        from orchestrator.docker_deployment_ai.validator import validate_deployment_plan  # noqa: PLC0415
        plan, snapshot = _make_snapshot_with_binary_info(compose_v2=True)
        plan["summary"] = "test"
        plan["docker_ai"] = {"provider": "litellm"}
        plan["detected_server_state"] = {"reverse_proxy": "none", "deployment_manager": "none"}
        plan["rollback_plan"] = {
            "type": "compose_snapshot",
            "previous_release_ref_required": False,
            "steps": [],
        }
        plan["secrets_required"] = []
        plan["blocking_questions"] = []
        plan["port_plan"] = {"host_port": 8080}
        plan["routing_plan"] = {}
        result = validate_deployment_plan(plan, snapshot)
        compose_check = next(
            (c for c in result.checks if c.name == "docker_compose_v2_check"), None
        )
        assert compose_check is not None
        assert compose_check.passed


# ---------------------------------------------------------------------------
# Hardening — SSH allowlist includes docker compose version
# ---------------------------------------------------------------------------

class TestSSHAllowlistDockerComposeVersion:
    """docker compose version and which docker must be on the inventory SSH allowlist."""

    def test_docker_compose_version_on_allowlist(self):
        from orchestrator.docker_deployment_ai.ssh_client import _ALLOWED_COMMANDS  # noqa: PLC0415
        assert "docker compose version" in _ALLOWED_COMMANDS, (
            "'docker compose version' must be on the SSH allowlist "
            "so inventory can detect Compose v2 availability."
        )

    def test_which_docker_on_allowlist(self):
        from orchestrator.docker_deployment_ai.ssh_client import _ALLOWED_COMMANDS  # noqa: PLC0415
        assert "which docker" in _ALLOWED_COMMANDS


# ---------------------------------------------------------------------------
# Hardening — fresh clean host with Docker available and zero containers
# ---------------------------------------------------------------------------

class TestFreshHostWithDockerAvailable:
    """A fresh host with Docker + Compose v2 and zero containers must not block
    the planner strategy detection."""

    def test_strategy_detection_none_manager_returns_valid_strategy(self):
        """detection manager=none, proxy=none, confidence=0.5 must not return blocked."""
        from orchestrator.docker_deployment_ai.planner import _strategy_from_detection  # noqa: PLC0415
        strategy, risk, questions = _strategy_from_detection(
            deployment_manager="none",
            reverse_proxy="none",
            confidence=0.5,
            preferred_strategy="docker_compose_with_host_port",
        )
        # Explicit preferred_strategy must be honoured; risk should not escalate to blocked
        assert strategy == "docker_compose_with_host_port"
        assert risk != "blocked"

    def test_strategy_detection_auto_detect_clean_host(self):
        """auto_detect on a clean host (no proxy, no manager) suggests compose_private."""
        from orchestrator.docker_deployment_ai.planner import _strategy_from_detection  # noqa: PLC0415
        strategy, risk, _ = _strategy_from_detection(
            deployment_manager="none",
            reverse_proxy="none",
            confidence=0.6,
            preferred_strategy="auto_detect",
        )
        assert strategy == "docker_compose_private"
        assert risk in ("low", "medium")

    def test_compose_v2_check_passes_for_fresh_host(self):
        """Fresh host with docker_compose_v2_available=True and zero containers passes."""
        from orchestrator.docker_deployment_ai.validator import _check_docker_compose_v2  # noqa: PLC0415
        plan = {
            "deployment_actions": [
                {"action_type": "DOCKER_COMPOSE_UP", "params": {}},
                {"action_type": "DOCKER_COMPOSE_PS", "params": {}},
            ]
        }
        from orchestrator.docker_deployment_ai.models import InventorySnapshot, DetectionResult, DeploymentManager, ReverseProxy  # noqa: PLC0415
        snapshot = InventorySnapshot(
            target_id="t", project_id="p",
            docker_binary_info={
                "docker_binary_present": True,
                "docker_daemon_reachable": True,
                "docker_compose_v2_available": True,
                "docker_info_error": None,
            },
            containers=[],  # zero containers — must not trigger blocking
            detection=DetectionResult(
                deployment_manager=DeploymentManager.none,
                reverse_proxy=ReverseProxy.none,
                confidence=0.6,
            ),
        )
        check = _check_docker_compose_v2(plan, snapshot)
        assert check.passed


# ---------------------------------------------------------------------------
# Planner action generation — _build_deployment_actions unit tests
# ---------------------------------------------------------------------------

_HELLO_COMPOSE = (
    "version: \"3.8\"\n"
    "services:\n"
    "  hello:\n"
    "    image: nginxdemos/hello:plain-text\n"
    "    ports:\n"
    "      - \"8080:80\"\n"
    "    restart: unless-stopped\n"
)
_DEPLOY_PATH = "/opt/deploy/testapp"


class TestBuildDeploymentActions:
    """_build_deployment_actions deterministically builds the 5-step sequence."""

    def test_compose_strategy_with_content_returns_five_actions(self):
        from orchestrator.docker_deployment_ai.planner import _build_deployment_actions  # noqa: PLC0415
        actions, questions = _build_deployment_actions(
            "docker_compose_with_host_port",
            {"compose_content": _HELLO_COMPOSE},
            _DEPLOY_PATH,
        )
        assert len(actions) == 5
        assert not questions
        types = [a["action_type"] for a in actions]
        assert types == [
            "CREATE_DIRECTORY",
            "UPLOAD_FILE",
            "DOCKER_COMPOSE_CONFIG",
            "DOCKER_COMPOSE_UP",
            "DOCKER_COMPOSE_PS",
        ]

    def test_private_strategy_also_generates_actions(self):
        from orchestrator.docker_deployment_ai.planner import _build_deployment_actions  # noqa: PLC0415
        actions, questions = _build_deployment_actions(
            "docker_compose_private",
            {"compose_content": _HELLO_COMPOSE},
            _DEPLOY_PATH,
        )
        assert len(actions) == 5
        assert not questions

    def test_non_compose_strategy_returns_empty(self):
        from orchestrator.docker_deployment_ai.planner import _build_deployment_actions  # noqa: PLC0415
        for strategy in ("manual_required", "reuse_existing_traefik",
                         "kamal_v2_compatible", "no_public_exposure"):
            actions, questions = _build_deployment_actions(
                strategy, {"compose_content": _HELLO_COMPOSE}, _DEPLOY_PATH
            )
            assert actions == [], f"Expected [] for strategy={strategy}"
            assert questions == []

    def test_missing_compose_content_returns_blocking_question(self):
        from orchestrator.docker_deployment_ai.planner import _build_deployment_actions  # noqa: PLC0415
        actions, questions = _build_deployment_actions(
            "docker_compose_with_host_port",
            {},
            _DEPLOY_PATH,
        )
        assert actions == []
        assert len(questions) == 1
        assert "compose_content" in questions[0]

    def test_empty_compose_content_returns_blocking_question(self):
        from orchestrator.docker_deployment_ai.planner import _build_deployment_actions  # noqa: PLC0415
        actions, questions = _build_deployment_actions(
            "docker_compose_with_host_port",
            {"compose_content": "   "},
            _DEPLOY_PATH,
        )
        assert actions == []
        assert questions

    def test_upload_file_path_is_under_deploy_path(self):
        from orchestrator.docker_deployment_ai.planner import _build_deployment_actions  # noqa: PLC0415
        actions, _ = _build_deployment_actions(
            "docker_compose_with_host_port",
            {"compose_content": _HELLO_COMPOSE},
            _DEPLOY_PATH,
        )
        upload = next(a for a in actions if a["action_type"] == "UPLOAD_FILE")
        path = upload["params"]["path"]
        assert path.startswith(_DEPLOY_PATH + "/")

    def test_upload_file_content_matches_input(self):
        from orchestrator.docker_deployment_ai.planner import _build_deployment_actions  # noqa: PLC0415
        actions, _ = _build_deployment_actions(
            "docker_compose_with_host_port",
            {"compose_content": _HELLO_COMPOSE},
            _DEPLOY_PATH,
        )
        upload = next(a for a in actions if a["action_type"] == "UPLOAD_FILE")
        assert upload["params"]["content"] == _HELLO_COMPOSE

    def test_custom_compose_filename_used(self):
        from orchestrator.docker_deployment_ai.planner import _build_deployment_actions  # noqa: PLC0415
        actions, _ = _build_deployment_actions(
            "docker_compose_with_host_port",
            {"compose_content": _HELLO_COMPOSE, "compose_file": "compose.prod.yml"},
            _DEPLOY_PATH,
        )
        upload = next(a for a in actions if a["action_type"] == "UPLOAD_FILE")
        assert "compose.prod.yml" in upload["params"]["path"]
        config = next(a for a in actions if a["action_type"] == "DOCKER_COMPOSE_CONFIG")
        assert config["params"]["compose_file"] == "compose.prod.yml"

    def test_unsafe_compose_filename_falls_back_to_default(self):
        from orchestrator.docker_deployment_ai.planner import _build_deployment_actions  # noqa: PLC0415
        for bad_name in ("../etc/cron", "../../shadow", "/etc/passwd", "a; rm -rf /"):
            actions, _ = _build_deployment_actions(
                "docker_compose_with_host_port",
                {"compose_content": _HELLO_COMPOSE, "compose_file": bad_name},
                _DEPLOY_PATH,
            )
            upload = next(a for a in actions if a["action_type"] == "UPLOAD_FILE")
            assert bad_name not in upload["params"]["path"], f"Unsafe name leaked: {bad_name}"
            assert "docker-compose.yml" in upload["params"]["path"]

    def test_create_directory_uses_deploy_path(self):
        from orchestrator.docker_deployment_ai.planner import _build_deployment_actions  # noqa: PLC0415
        actions, _ = _build_deployment_actions(
            "docker_compose_with_host_port",
            {"compose_content": _HELLO_COMPOSE},
            _DEPLOY_PATH,
        )
        mkdir = next(a for a in actions if a["action_type"] == "CREATE_DIRECTORY")
        assert mkdir["params"]["path"] == _DEPLOY_PATH


class TestGeneratedActionsPassValidator:
    """Actions generated by _build_deployment_actions must pass the validator."""

    def _make_compose_plan(self, strategy: str = "docker_compose_with_host_port") -> dict:
        from orchestrator.docker_deployment_ai.planner import _build_deployment_actions  # noqa: PLC0415
        actions, _ = _build_deployment_actions(
            strategy, {"compose_content": _HELLO_COMPOSE}, _DEPLOY_PATH
        )
        return {
            "strategy": strategy,
            "risk_level": "low",
            "summary": "test",
            "docker_ai": {"provider": "litellm"},
            "detected_server_state": {"reverse_proxy": "none", "deployment_manager": "none"},
            "rollback_plan": {
                "type": "compose_snapshot",
                "previous_release_ref_required": False,
                "steps": [],
            },
            "secrets_required": [],
            "blocking_questions": [],
            "port_plan": {"host_port": 8080},
            "routing_plan": {},
            "deployment_actions": actions,
        }

    def test_action_types_valid(self):
        from orchestrator.docker_deployment_ai.executor import _validate_actions  # noqa: PLC0415
        from orchestrator.docker_deployment_ai.planner import _build_deployment_actions  # noqa: PLC0415
        actions, _ = _build_deployment_actions(
            "docker_compose_with_host_port",
            {"compose_content": _HELLO_COMPOSE},
            _DEPLOY_PATH,
        )
        _validate_actions(actions)  # must not raise

    def test_full_validation_passes_with_compose_v2_snapshot(self):
        from orchestrator.docker_deployment_ai.validator import validate_deployment_plan  # noqa: PLC0415
        from orchestrator.docker_deployment_ai.models import (  # noqa: PLC0415
            InventorySnapshot, DetectionResult, DeploymentManager, ReverseProxy
        )
        plan = self._make_compose_plan()
        snapshot = InventorySnapshot(
            target_id="t", project_id="p",
            docker_binary_info={
                "docker_binary_present": True,
                "docker_daemon_reachable": True,
                "docker_compose_v2_available": True,
                "docker_info_error": None,
            },
            containers=[],
            detection=DetectionResult(
                deployment_manager=DeploymentManager.none,
                reverse_proxy=ReverseProxy.none,
                confidence=0.6,
            ),
        )
        result = validate_deployment_plan(plan, snapshot)
        assert result.status in ("passed", "warning"), (
            f"Unexpected status={result.status}: {result.blocking_errors}"
        )

    def test_no_forbidden_action_types_generated(self):
        from orchestrator.docker_deployment_ai.planner import _build_deployment_actions  # noqa: PLC0415
        from orchestrator.docker_deployment_ai.executor import _FORBIDDEN_ACTION_TYPES, _NOT_IMPLEMENTED_ACTIONS  # noqa: PLC0415
        actions, _ = _build_deployment_actions(
            "docker_compose_with_host_port",
            {"compose_content": _HELLO_COMPOSE},
            _DEPLOY_PATH,
        )
        for a in actions:
            atype = a["action_type"]
            assert atype not in _FORBIDDEN_ACTION_TYPES, f"Forbidden action generated: {atype}"
            assert atype not in _NOT_IMPLEMENTED_ACTIONS, f"Not-implemented action generated: {atype}"


class TestPlannerE2EWithMockedAI:
    """Full create_deployment_plan pipeline with mocked AI and in-memory DB."""

    @pytest.mark.asyncio
    async def test_compose_content_produces_executable_plan(self):
        """Given compose_content, planner produces a plan with 5 concrete actions."""
        import json  # noqa: PLC0415
        import aiosqlite  # noqa: PLC0415
        from unittest.mock import AsyncMock, patch  # noqa: PLC0415
        from orchestrator.docker_deployment_ai.planner import create_deployment_plan  # noqa: PLC0415
        from orchestrator.docker_deployment_ai.models import (  # noqa: PLC0415
            InventorySnapshot, DetectionResult, DeploymentManager, ReverseProxy
        )

        snapshot = InventorySnapshot(
            target_id="tgt-e2e", project_id="proj-e2e",
            docker_binary_info={
                "docker_binary_present": True,
                "docker_daemon_reachable": True,
                "docker_compose_v2_available": True,
                "docker_info_error": None,
            },
            containers=[],
            detection=DetectionResult(
                deployment_manager=DeploymentManager.none,
                reverse_proxy=ReverseProxy.none,
                confidence=0.6,
            ),
        )
        mock_ai = {
            "provider_used": "litellm",
            "analysis_id": "analysis-mock",
            "normalized": {
                "recommended_strategy": "docker_compose_with_host_port",
                "risk_level": "low",
                "reasoning_summary": "Fresh host, compose v2 available.",
                "blocking_questions": [],
                "warnings": [],
            },
        }

        db = await aiosqlite.connect(":memory:")
        await db.execute(
            """CREATE TABLE deployment_plans (
                id TEXT PRIMARY KEY, project_id TEXT, target_id TEXT,
                inventory_snapshot_id TEXT, docker_ai_analysis_id TEXT,
                status TEXT, plan_json TEXT, plan_content_hash TEXT,
                summary_markdown TEXT, risk_level TEXT,
                blocking_questions_json TEXT, created_by TEXT, created_at TEXT
            )"""
        )
        await db.execute(
            """CREATE TABLE docker_ai_analyses (
                id TEXT PRIMARY KEY, project_id TEXT, target_id TEXT,
                inventory_snapshot_id TEXT, provider TEXT, analysis_type TEXT,
                raw_output TEXT, normalized_json TEXT, status TEXT,
                error_message TEXT, created_at TEXT
            )"""
        )
        await db.commit()

        with patch("orchestrator.docker_deployment_ai.planner.DockerAIProvider") as MP:
            MP.return_value.recommend_deployment_strategy = AsyncMock(return_value=mock_ai)
            plan_id, plan = await create_deployment_plan(
                project_id="proj-e2e",
                target_id="tgt-e2e",
                snapshot=snapshot,
                inventory_snapshot_id="snap-e2e",
                repo_context={
                    "app_name": "testapp",
                    "compose_file": "docker-compose.yml",
                    "compose_content": _HELLO_COMPOSE,
                },
                target_domain=None,
                preferred_strategy="auto_detect",
                deploy_path=_DEPLOY_PATH,
                created_by="test",
                db=db,
            )

        assert plan_id
        actions = plan.get("deployment_actions", [])
        assert len(actions) == 5, f"Expected 5 actions, got {len(actions)}"
        types = [a["action_type"] for a in actions]
        assert types == [
            "CREATE_DIRECTORY", "UPLOAD_FILE",
            "DOCKER_COMPOSE_CONFIG", "DOCKER_COMPOSE_UP", "DOCKER_COMPOSE_PS",
        ]
        upload = next(a for a in actions if a["action_type"] == "UPLOAD_FILE")
        assert upload["params"]["content"] == _HELLO_COMPOSE
        assert upload["params"]["path"].startswith(_DEPLOY_PATH + "/")

        # Verify plan persisted to DB with actions
        cursor = await db.execute(
            "SELECT plan_json, risk_level FROM deployment_plans WHERE id=?", (plan_id,)
        )
        row = await cursor.fetchone()
        assert row is not None
        db_plan = json.loads(row[0])
        assert len(db_plan.get("deployment_actions", [])) == 5
        await db.close()

    @pytest.mark.asyncio
    async def test_missing_compose_content_produces_blocked_plan(self):
        """When compose_content is absent, plan has empty actions and a blocking question."""
        import aiosqlite  # noqa: PLC0415
        from unittest.mock import AsyncMock, patch  # noqa: PLC0415
        from orchestrator.docker_deployment_ai.planner import create_deployment_plan  # noqa: PLC0415
        from orchestrator.docker_deployment_ai.models import (  # noqa: PLC0415
            InventorySnapshot, DetectionResult, DeploymentManager, ReverseProxy
        )

        snapshot = InventorySnapshot(
            target_id="tgt-e2e", project_id="proj-e2e",
            docker_binary_info={
                "docker_binary_present": True,
                "docker_compose_v2_available": True,
            },
            containers=[],
            detection=DetectionResult(
                deployment_manager=DeploymentManager.none,
                reverse_proxy=ReverseProxy.none,
                confidence=0.6,
            ),
        )
        mock_ai = {
            "provider_used": "litellm", "analysis_id": None,
            "normalized": {
                "recommended_strategy": "docker_compose_with_host_port",
                "risk_level": "low", "reasoning_summary": "Fresh host.",
                "blocking_questions": [], "warnings": [],
            },
        }

        db = await aiosqlite.connect(":memory:")
        await db.execute(
            """CREATE TABLE deployment_plans (
                id TEXT PRIMARY KEY, project_id TEXT, target_id TEXT,
                inventory_snapshot_id TEXT, docker_ai_analysis_id TEXT,
                status TEXT, plan_json TEXT, plan_content_hash TEXT,
                summary_markdown TEXT, risk_level TEXT,
                blocking_questions_json TEXT, created_by TEXT, created_at TEXT
            )"""
        )
        await db.execute(
            """CREATE TABLE docker_ai_analyses (
                id TEXT PRIMARY KEY, project_id TEXT, target_id TEXT,
                inventory_snapshot_id TEXT, provider TEXT, analysis_type TEXT,
                raw_output TEXT, normalized_json TEXT, status TEXT,
                error_message TEXT, created_at TEXT
            )"""
        )
        await db.commit()

        with patch("orchestrator.docker_deployment_ai.planner.DockerAIProvider") as MP:
            MP.return_value.recommend_deployment_strategy = AsyncMock(return_value=mock_ai)
            plan_id, plan = await create_deployment_plan(
                project_id="proj-e2e",
                target_id="tgt-e2e",
                snapshot=snapshot,
                inventory_snapshot_id="snap-e2e",
                repo_context={"app_name": "testapp"},  # no compose_content
                target_domain=None,
                preferred_strategy="auto_detect",
                deploy_path=_DEPLOY_PATH,
                created_by="test",
                db=db,
            )

        assert plan.get("deployment_actions", []) == [], (
            "Actions must be empty when compose_content missing"
        )
        questions = plan.get("blocking_questions", [])
        assert any("compose_content" in q for q in questions), (
            f"Expected blocking question, got: {questions}"
        )
        await db.close()
