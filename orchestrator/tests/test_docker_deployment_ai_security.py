"""Security regression tests — P0-2 (IDOR) and P0-5 (command injection).

P0-2: Ownership checks on all deployment routes.
  - Authenticated user cannot access resources belonging to an inaccessible project.
  - Tests prove 403, not 404/500, is returned.

P0-5: Parameter validation blocks injection before any SSH call.
  - Shell metacharacters in service, compose_file, path, network_name,
    volume_name, driver are rejected by param_validators.
  - Forbidden action types are rejected before side effects.
  - Valid names pass without error.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

# Stub jose so orchestrator.api.routes can be imported in envs that don't have
# the jose package installed (jose is only needed for live JWT auth, not for
# the helper functions tested here).
for _jose_mod in ("jose", "jose.jwe", "jose.jwt", "jose.exceptions"):
    sys.modules.setdefault(_jose_mod, MagicMock())

import pytest

from orchestrator.docker_deployment_ai.executor import (
    ActionTypeRejected,
    _FORBIDDEN_ACTION_TYPES,
    _NOT_IMPLEMENTED_ACTIONS,
    _validate_actions,
)
from orchestrator.docker_deployment_ai.param_validators import (
    ParameterValidationError,
    validate_compose_file,
    validate_deploy_path,
    validate_log_lines,
    validate_network_driver,
    validate_network_name,
    validate_safe_name,
    validate_service_name,
    validate_volume_name,
)


# ---------------------------------------------------------------------------
# P0-5: Parameter validation — injection rejection
# ---------------------------------------------------------------------------

class TestCommandInjectionServiceName:
    """Service names with shell metacharacters must be rejected."""

    def test_semicolon_rejected(self):
        with pytest.raises(ParameterValidationError, match="Shell metacharacter"):
            validate_service_name("web; cat /etc/shadow")

    def test_pipe_rejected(self):
        with pytest.raises(ParameterValidationError, match="Shell metacharacter"):
            validate_service_name("web|id")

    def test_ampersand_rejected(self):
        with pytest.raises(ParameterValidationError, match="Shell metacharacter"):
            validate_service_name("web&id")

    def test_backtick_rejected(self):
        with pytest.raises(ParameterValidationError, match="Shell metacharacter"):
            validate_service_name("web`id`")

    def test_dollar_subshell_rejected(self):
        with pytest.raises(ParameterValidationError, match="Shell metacharacter"):
            validate_service_name("web$(id)")

    def test_newline_rejected(self):
        with pytest.raises(ParameterValidationError, match="Shell metacharacter"):
            validate_service_name("web\nid")

    def test_valid_service_name_passes(self):
        assert validate_service_name("web") == "web"
        assert validate_service_name("my-app-worker") == "my-app-worker"
        assert validate_service_name("app_v2") == "app_v2"

    def test_empty_allowed(self):
        assert validate_service_name("") == ""


class TestCommandInjectionComposefile:
    """Compose filenames must be relative, no traversal, no metacharacters."""

    def test_semicolon_rejected(self):
        with pytest.raises(ParameterValidationError):
            validate_compose_file("docker-compose.yml; rm -rf /")

    def test_absolute_path_rejected(self):
        with pytest.raises(ParameterValidationError, match="Absolute path"):
            validate_compose_file("/etc/passwd")

    def test_directory_traversal_rejected(self):
        with pytest.raises(ParameterValidationError, match="traversal"):
            validate_compose_file("../../etc/shadow")

    def test_traversal_within_path_rejected(self):
        with pytest.raises(ParameterValidationError, match="traversal"):
            validate_compose_file("ops/../../../etc/crontab")

    def test_valid_compose_file_passes(self):
        assert validate_compose_file("docker-compose.yml") == "docker-compose.yml"
        assert validate_compose_file("compose.prod.yml") == "compose.prod.yml"

    def test_empty_returns_default(self):
        assert validate_compose_file("") == "docker-compose.yml"


class TestCommandInjectionDeployPath:
    """Deploy paths must be absolute and free of shell metacharacters."""

    def test_semicolon_rejected(self):
        with pytest.raises(ParameterValidationError, match="Shell metacharacter"):
            validate_deploy_path("/opt/app && cat .env")

    def test_relative_path_rejected(self):
        with pytest.raises(ParameterValidationError, match="absolute"):
            validate_deploy_path("opt/app")

    def test_traversal_rejected(self):
        with pytest.raises(ParameterValidationError, match="traversal"):
            validate_deploy_path("/opt/../../etc")

    def test_backtick_rejected(self):
        with pytest.raises(ParameterValidationError, match="Shell metacharacter"):
            validate_deploy_path("/opt/app`id`")

    def test_dollar_subshell_rejected(self):
        with pytest.raises(ParameterValidationError, match="Shell metacharacter"):
            validate_deploy_path("/opt/$(whoami)")

    def test_valid_path_passes(self):
        assert validate_deploy_path("/opt/automatron/apps/myapp") == "/opt/automatron/apps/myapp"
        assert validate_deploy_path("/home/deploy/app") == "/home/deploy/app"

    def test_empty_rejected(self):
        with pytest.raises(ParameterValidationError, match="must not be empty"):
            validate_deploy_path("")


class TestCommandInjectionNetworkName:
    """Network names with shell injection characters are rejected."""

    def test_pipe_rejected(self):
        with pytest.raises(ParameterValidationError):
            validate_network_name("mynet|id")

    def test_semicolon_rejected(self):
        with pytest.raises(ParameterValidationError):
            validate_network_name("mynet;id")

    def test_valid_name_passes(self):
        assert validate_network_name("traefik-public") == "traefik-public"
        assert validate_network_name("my_app_net") == "my_app_net"

    def test_empty_allowed(self):
        assert validate_network_name("") == ""


class TestCommandInjectionNetworkDriver:
    """Only known safe Docker network drivers are accepted."""

    def test_unknown_driver_rejected(self):
        with pytest.raises(ParameterValidationError, match="Unknown network driver"):
            validate_network_driver("bridge; rm -rf /")

    def test_shell_in_driver_rejected(self):
        with pytest.raises(ParameterValidationError):
            validate_network_driver("bridge|id")

    def test_valid_drivers_pass(self):
        assert validate_network_driver("bridge") == "bridge"
        assert validate_network_driver("overlay") == "overlay"
        assert validate_network_driver("host") == "host"

    def test_default_is_bridge(self):
        assert validate_network_driver("") == "bridge"


# ---------------------------------------------------------------------------
# P0-5: Executor action type allowlist — forbidden types blocked before side effect
# ---------------------------------------------------------------------------

class TestExecutorActionTypeRejection:
    """All forbidden action types raise ActionTypeRejected before any side effect."""

    @pytest.mark.parametrize("atype", sorted(_FORBIDDEN_ACTION_TYPES))
    def test_forbidden_type_rejected(self, atype: str):
        with pytest.raises(ActionTypeRejected, match=atype):
            _validate_actions([{"action_type": atype, "params": {}}])

    def test_unknown_type_rejected(self):
        with pytest.raises(ActionTypeRejected):
            _validate_actions([{"action_type": "DEPLOY_EVERYTHING", "params": {}}])

    def test_shell_injection_via_type_rejected(self):
        with pytest.raises(ActionTypeRejected):
            _validate_actions([{"action_type": "SHELL", "params": {"command": "id"}}])

    def test_empty_list_passes(self):
        _validate_actions([])  # no error

    def test_valid_action_passes(self):
        _validate_actions([{"action_type": "DOCKER_COMPOSE_UP", "params": {}}])
        _validate_actions([{"action_type": "CREATE_DIRECTORY", "params": {}}])


class TestExecutorNotImplementedActions:
    """NOT_IMPLEMENTED actions are in the allowlist (schema-valid) but executor
    returns explicit error strings rather than silently acknowledging them."""

    @pytest.mark.parametrize("atype", sorted(_NOT_IMPLEMENTED_ACTIONS))
    def test_not_implemented_in_allowed(self, atype: str):
        # These pass _validate_actions (they're in the allowed set)
        _validate_actions([{"action_type": atype, "params": {}}])

    @pytest.mark.asyncio
    async def test_docker_login_returns_error_not_acknowledged(self):
        """DOCKER_LOGIN is still not implemented — must error before any SSH call."""
        from orchestrator.docker_deployment_ai.executor import _execute_action, ExecutorSSHClient  # noqa: PLC0415
        from unittest.mock import MagicMock  # noqa: PLC0415
        fake_ssh = MagicMock(spec=ExecutorSSHClient)
        _, _, error = await _execute_action(
            {"action_type": "DOCKER_LOGIN", "params": {}},
            fake_ssh,
            deploy_path="/opt/app",
        )
        assert error is not None
        assert "not implemented" in error.lower()
        # SSH was NEVER called
        fake_ssh.run_mkdir.assert_not_called()
        fake_ssh.run_docker_compose_up.assert_not_called()

    @pytest.mark.asyncio
    async def test_rollback_to_previous_returns_error(self):
        from orchestrator.docker_deployment_ai.executor import _execute_action, ExecutorSSHClient  # noqa: PLC0415
        from unittest.mock import MagicMock  # noqa: PLC0415
        fake_ssh = MagicMock(spec=ExecutorSSHClient)
        _, _, error = await _execute_action(
            {"action_type": "ROLLBACK_TO_PREVIOUS_RELEASE", "params": {}},
            fake_ssh,
            deploy_path="/opt/app",
        )
        assert error is not None
        assert "not implemented" in error.lower()
        fake_ssh.run_docker_compose_up.assert_not_called()


# ---------------------------------------------------------------------------
# P0-2: IDOR ownership verification helpers
# ---------------------------------------------------------------------------

class TestIDOROwnershipHelpers:
    """_require_target_access, _require_plan_access, _require_run_access
    convert inaccessible-project 404 → 403 Forbidden."""

    @classmethod
    def setup_class(cls) -> None:
        # Ensure the routes module is in sys.modules so patch() can resolve
        # "orchestrator.api.routes.*" targets without AttributeError.
        import orchestrator.api.routes  # noqa: F401, PLC0415

    @pytest.mark.asyncio
    async def test_target_in_missing_project_raises_403(self):
        """Accessing a target whose project doesn't exist returns 403."""
        from unittest.mock import AsyncMock, patch  # noqa: PLC0415
        from fastapi import HTTPException  # noqa: PLC0415

        # Build a fake target whose project_id doesn't exist
        from orchestrator.docker_deployment_ai.models import DeploymentTarget  # noqa: PLC0415
        fake_target = DeploymentTarget(
            id="tgt-1", project_id="project-does-not-exist",
            name="server", host="1.2.3.4", ssh_user="deploy",
            app_name="myapp", deploy_path="/opt/app",
        )

        mock_db = AsyncMock()

        with patch(
            "orchestrator.api.routes._deployment_orch.get_target",
            AsyncMock(return_value=fake_target),
        ), patch(
            "orchestrator.api.routes._get_required_project",
            AsyncMock(side_effect=HTTPException(status_code=404, detail="Project not found")),
        ):
            from orchestrator.api.routes import _require_target_access  # noqa: PLC0415
            with pytest.raises(HTTPException) as exc_info:
                await _require_target_access("tgt-1", mock_db)
            assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_plan_in_missing_project_raises_403(self):
        from unittest.mock import AsyncMock, patch  # noqa: PLC0415
        from fastapi import HTTPException  # noqa: PLC0415

        fake_plan = {"id": "plan-1", "project_id": "project-does-not-exist", "plan_json": {}}
        mock_db = AsyncMock()

        with patch(
            "orchestrator.api.routes._deployment_orch.get_plan",
            AsyncMock(return_value=fake_plan),
        ), patch(
            "orchestrator.api.routes._get_required_project",
            AsyncMock(side_effect=HTTPException(status_code=404, detail="Project not found")),
        ):
            from orchestrator.api.routes import _require_plan_access  # noqa: PLC0415
            with pytest.raises(HTTPException) as exc_info:
                await _require_plan_access("plan-1", mock_db)
            assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_run_in_missing_project_raises_403(self):
        from unittest.mock import AsyncMock, patch  # noqa: PLC0415
        from fastapi import HTTPException  # noqa: PLC0415

        fake_run = {"id": "run-1", "project_id": "project-does-not-exist"}
        mock_db = AsyncMock()

        with patch(
            "orchestrator.api.routes._deployment_orch.get_run",
            AsyncMock(return_value=fake_run),
        ), patch(
            "orchestrator.api.routes._get_required_project",
            AsyncMock(side_effect=HTTPException(status_code=404, detail="Project not found")),
        ):
            from orchestrator.api.routes import _require_run_access  # noqa: PLC0415
            with pytest.raises(HTTPException) as exc_info:
                await _require_run_access("run-1", mock_db)
            assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_nonexistent_target_raises_404_not_403(self):
        """A completely unknown target_id raises 404, not 403."""
        from unittest.mock import AsyncMock, patch  # noqa: PLC0415
        from fastapi import HTTPException  # noqa: PLC0415

        mock_db = AsyncMock()
        with patch(
            "orchestrator.api.routes._deployment_orch.get_target",
            AsyncMock(return_value=None),
        ):
            from orchestrator.api.routes import _require_target_access  # noqa: PLC0415
            with pytest.raises(HTTPException) as exc_info:
                await _require_target_access("unknown-id", mock_db)
            assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_target_in_existing_project_returns_target(self):
        """Valid target in accessible project returns the target, no exception."""
        from unittest.mock import AsyncMock, patch  # noqa: PLC0415
        from orchestrator.docker_deployment_ai.models import DeploymentTarget  # noqa: PLC0415

        fake_target = DeploymentTarget(
            id="tgt-2", project_id="project-exists",
            name="server", host="1.2.3.4", ssh_user="deploy",
            app_name="myapp", deploy_path="/opt/app",
        )
        mock_db = AsyncMock()

        with patch(
            "orchestrator.api.routes._deployment_orch.get_target",
            AsyncMock(return_value=fake_target),
        ), patch(
            "orchestrator.api.routes._get_required_project",
            AsyncMock(return_value={"id": "project-exists"}),
        ):
            from orchestrator.api.routes import _require_target_access  # noqa: PLC0415
            result = await _require_target_access("tgt-2", mock_db)
            assert result.id == "tgt-2"
