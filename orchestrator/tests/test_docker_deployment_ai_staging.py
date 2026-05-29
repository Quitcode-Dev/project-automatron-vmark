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
