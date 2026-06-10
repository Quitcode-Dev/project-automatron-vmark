"""Tests for the Docker Deployment Intelligence Layer.

Covers per-spec acceptance criteria:
  - Provider chain falls through on Gordon unavailable
  - Redaction strips secrets before LLM calls
  - Gordon output parsed into normalized analysis
  - Detection from fixture JSON (Traefik, Kamal v1/v2, Nginx, Caddy, mixed)
  - Plan schema validation
  - Validator blocks unsafe scenarios
  - Executor rejects forbidden action types
  - Rollback metadata required before execution
  - MCP tool policy enforces allowlist/denylist
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.docker_deployment_ai.executor import (
    ActionTypeRejected,
    _ALLOWED_ACTION_TYPES,
    _FORBIDDEN_ACTION_TYPES,
    _validate_actions,
)
from orchestrator.docker_deployment_ai.gordon_result_parser import (
    normalize_deployment_strategy,
    normalize_inventory_analysis,
    parse_gordon_output,
)
from orchestrator.docker_deployment_ai.models import (
    ContainerInfo,
    DeploymentManager,
    DetectionResult,
    InventorySnapshot,
    ListeningPort,
    ReverseProxy,
)
from orchestrator.docker_deployment_ai.mcp_tool_policy import (
    MCPToolDenied,
    is_tool_allowed,
    validate_tool_call,
)
from orchestrator.docker_deployment_ai.plan_schema import validate_plan
from orchestrator.docker_deployment_ai.policy import PolicyViolation, check_policy
from orchestrator.docker_deployment_ai.reverse_proxy_detector import detect_reverse_proxy
from orchestrator.docker_deployment_ai.deployment_detector import refine_with_kamal_signals
from orchestrator.docker_deployment_ai.port_scanner import parse_ss_output, is_port_occupied
from orchestrator.docker_deployment_ai.secrets import redact_env_dict, sanitize_context
from orchestrator.docker_deployment_ai.validator import validate_deployment_plan


# ---------------------------------------------------------------------------
# Secrets / redaction
# ---------------------------------------------------------------------------


def test_redact_env_dict_strips_values():
    env = {"SOME_KEY": "secret123", "ANOTHER": "not_a_secret"}
    result = redact_env_dict(env)
    assert result == {"SOME_KEY": "[REDACTED]", "ANOTHER": "[REDACTED]"}
    assert set(result.keys()) == {"SOME_KEY", "ANOTHER"}


def test_sanitize_context_redacts_api_key():
    real_key = "sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdefghijklmnopqrstuvwxyz"
    ctx = {"token": real_key, "name": "project"}
    result = sanitize_context(ctx)
    assert real_key not in str(result)
    assert result["name"] == "project"


def test_sanitize_context_handles_nested():
    ctx = {"outer": {"inner": "ghp_ABCDEFGHIJ12345678901234567890"}}
    result = sanitize_context(ctx)
    assert "ghp_" not in str(result["outer"]["inner"])


# ---------------------------------------------------------------------------
# Gordon result parser
# ---------------------------------------------------------------------------


def test_parse_gordon_output_valid_json():
    raw = '{"deployment_manager": "traefik", "confidence": 0.9}'
    result = parse_gordon_output(raw, analysis_type="test")
    assert result["deployment_manager"] == "traefik"
    assert "parse_error" not in result


def test_parse_gordon_output_json_in_prose():
    raw = 'Here is the result:\n{"deployment_manager": "nginx", "confidence": 0.8}\nDone.'
    result = parse_gordon_output(raw, analysis_type="test")
    assert result["deployment_manager"] == "nginx"


def test_parse_gordon_output_empty():
    result = parse_gordon_output("", analysis_type="test")
    assert "parse_error" in result


def test_parse_gordon_output_no_json_block():
    result = parse_gordon_output("No JSON here at all.", analysis_type="test")
    assert result["parse_error"] == "no JSON block found"


def test_normalize_inventory_analysis():
    raw = {"deployment_manager": "kamal_v2", "reverse_proxy": "kamal_proxy", "confidence": 0.95}
    result = normalize_inventory_analysis(raw)
    assert result["deployment_manager"] == "kamal_v2"
    assert result["confidence"] == 0.95


def test_normalize_deployment_strategy_defaults():
    result = normalize_deployment_strategy({})
    assert result["recommended_strategy"] == "manual_required"
    assert result["risk_level"] == "high"


# ---------------------------------------------------------------------------
# Provider chain: Gordon unavailable → fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_chain_falls_through_on_gordon_unavailable():
    """With Gordon unavailable, DockerAIProvider should reach litellm as fallback."""
    import orchestrator.docker_deployment_ai.docker_ai_provider as dap_module
    import orchestrator.docker_deployment_ai.gordon_client as gc_module

    async def fake_gordon(method, kwargs):
        return {"error": "gordon unavailable"}

    async def fake_agent(method, kwargs):
        return {"error": "agent unavailable"}

    async def fake_model_runner(method, kwargs):
        return {"error": "model runner unavailable"}

    async def fake_litellm(method, kwargs):
        return {
            "provider": "litellm",
            "analysis_type": method,
            "raw_output": "{}",
            "normalized": {"deployment_manager": "none", "confidence": 0.5, "evidence": [], "risks": [], "notes": ""},
            "error": None,
        }

    class _FakeSettings:
        docker_ai_require_gordon = False
        docker_ai_provider_priority = "gordon,docker_agent,model_runner,litellm"
        docker_ai_enable_agent = True
        docker_ai_enable_model_runner = True

    original_gordon = dap_module._call_gordon
    original_agent = dap_module._call_docker_agent
    original_runner = dap_module._call_model_runner
    original_litellm = dap_module._call_litellm
    original_settings = dap_module.settings
    original_gc_available = gc_module._available

    try:
        gc_module._available = False
        dap_module._call_gordon = fake_gordon
        dap_module._call_docker_agent = fake_agent
        dap_module._call_model_runner = fake_model_runner
        dap_module._call_litellm = fake_litellm
        # _PROVIDER_CALLERS dict is built at import time — update it too
        dap_module._PROVIDER_CALLERS["gordon"] = fake_gordon
        dap_module._PROVIDER_CALLERS["docker_agent"] = fake_agent
        dap_module._PROVIDER_CALLERS["model_runner"] = fake_model_runner
        dap_module._PROVIDER_CALLERS["litellm"] = fake_litellm
        dap_module.settings = _FakeSettings()

        from orchestrator.docker_deployment_ai.docker_ai_provider import DockerAIProvider
        provider = DockerAIProvider()

        mock_db = AsyncMock()

        result = await provider.run_analysis(
            method="analyze_inventory",
            kwargs={"sanitized_inventory": {}},
            project_id="proj_test",
            db=mock_db,
        )
        assert result.get("provider_used") == "litellm", f"Got: {result}"
        assert result.get("error") is None
    finally:
        dap_module._call_gordon = original_gordon
        dap_module._call_docker_agent = original_agent
        dap_module._call_model_runner = original_runner
        dap_module._call_litellm = original_litellm
        dap_module._PROVIDER_CALLERS["gordon"] = original_gordon
        dap_module._PROVIDER_CALLERS["docker_agent"] = original_agent
        dap_module._PROVIDER_CALLERS["model_runner"] = original_runner
        dap_module._PROVIDER_CALLERS["litellm"] = original_litellm
        dap_module.settings = original_settings
        gc_module._available = original_gc_available


@pytest.mark.asyncio
async def test_gordon_require_raises_when_unavailable(monkeypatch):
    """DOCKER_AI_REQUIRE_GORDON=true + unavailable → GordonRequiredError."""
    import orchestrator.docker_deployment_ai.gordon_client as gc_module
    from orchestrator.docker_deployment_ai.gordon_client import GordonClient
    from orchestrator.docker_deployment_ai.docker_ai_provider import DockerAIProvider, GordonRequiredError

    gc_module._available = None
    monkeypatch.setattr(GordonClient, "is_available", AsyncMock(return_value=False))

    with patch("orchestrator.docker_deployment_ai.docker_ai_provider.settings") as mock_settings:
        mock_settings.docker_ai_require_gordon = True
        mock_settings.docker_ai_provider_priority = "gordon,litellm"
        mock_settings.docker_ai_enable_agent = False
        mock_settings.docker_ai_enable_model_runner = False

        provider = DockerAIProvider()
        mock_db = AsyncMock()

        with pytest.raises(GordonRequiredError):
            await provider.run_analysis(
                method="analyze_inventory",
                kwargs={},
                project_id="proj_test",
                db=mock_db,
            )


# ---------------------------------------------------------------------------
# Reverse proxy detection from fixture JSON
# ---------------------------------------------------------------------------


def _make_container(name: str, image: str = "", labels: dict | None = None) -> ContainerInfo:
    return ContainerInfo(
        id="abc12345",
        name=name,
        image=image or name,
        status="running",
        labels=labels or {},
        ports=[],
        mounts=[],
        env_keys=[],
        networks=[],
    )


def test_detect_traefik_by_container_name():
    containers = [_make_container("traefik")]
    result = detect_reverse_proxy(containers, [])
    assert result.reverse_proxy == ReverseProxy.traefik
    assert result.confidence >= 0.8


def test_detect_traefik_by_label():
    containers = [
        _make_container(
            "myapp",
            labels={"traefik.http.routers.myapp.rule": "Host(`example.com`)"},
        )
    ]
    result = detect_reverse_proxy(containers, [])
    assert result.reverse_proxy == ReverseProxy.traefik


def test_detect_kamal_proxy():
    containers = [_make_container("kamal-proxy", image="basecamp/kamal-proxy:latest")]
    result = detect_reverse_proxy(containers, [])
    assert result.deployment_manager == DeploymentManager.kamal_v2
    assert result.reverse_proxy == ReverseProxy.kamal_proxy


def test_detect_nginx():
    containers = [_make_container("nginx")]
    result = detect_reverse_proxy(containers, [])
    assert result.reverse_proxy == ReverseProxy.nginx


def test_detect_caddy():
    containers = [_make_container("caddy")]
    result = detect_reverse_proxy(containers, [])
    assert result.reverse_proxy == ReverseProxy.caddy


def test_detect_mixed():
    containers = [_make_container("traefik"), _make_container("nginx")]
    result = detect_reverse_proxy(containers, [])
    assert result.deployment_manager == DeploymentManager.mixed


def test_detect_none():
    result = detect_reverse_proxy([], [])
    assert result.deployment_manager == DeploymentManager.none
    assert result.reverse_proxy == ReverseProxy.none


def test_kamal_v1_detection_via_refine():
    """Kamal v1: Traefik base + kamal-style containers + deploy.yml traefik block."""
    traefik_detection = DetectionResult(
        deployment_manager=DeploymentManager.traefik,
        reverse_proxy=ReverseProxy.traefik,
        confidence=0.9,
        evidence=["traefik container found"],
    )
    containers = [
        _make_container("myapp-web-abcdef12"),  # Kamal v1 naming pattern
    ]
    networks: list[dict] = [{"name": "kamal-myapp", "driver": "bridge"}]
    deploy_yml = "traefik:\n  options:\n    publish: 443:443"

    result = refine_with_kamal_signals(traefik_detection, containers, networks, deploy_yml)
    assert result.deployment_manager == DeploymentManager.kamal_v1


def test_kamal_v2_detection_via_deploy_yml():
    traefik_detection = DetectionResult(
        deployment_manager=DeploymentManager.traefik,
        reverse_proxy=ReverseProxy.traefik,
        confidence=0.9,
        evidence=[],
    )
    deploy_yml = "proxy:\n  host: example.com"
    result = refine_with_kamal_signals(traefik_detection, [], [], deploy_yml)
    assert result.deployment_manager == DeploymentManager.kamal_v2
    assert result.reverse_proxy == ReverseProxy.kamal_proxy


# ---------------------------------------------------------------------------
# Port scanner
# ---------------------------------------------------------------------------


def test_parse_ss_output():
    raw = (
        "State  Recv-Q Send-Q Local Address:Port Peer Address:Port Process\n"
        'LISTEN 0      128    0.0.0.0:80        0.0.0.0:*         users:(("nginx",pid=123,fd=6))\n'
        'LISTEN 0      128    0.0.0.0:443       0.0.0.0:*         users:(("nginx",pid=123,fd=7))\n'
    )
    ports = parse_ss_output(raw)
    assert len(ports) == 2
    assert ports[0].port == 80
    assert ports[0].process == "nginx"
    assert ports[1].port == 443


def test_is_port_occupied():
    from orchestrator.docker_deployment_ai.port_scanner import is_port_occupied
    ports = parse_ss_output(
        'LISTEN 0 128 0.0.0.0:8080 0.0.0.0:* users:(("app",pid=1,fd=3))\n'
    )
    assert is_port_occupied(ports, 8080) is True
    assert is_port_occupied(ports, 3000) is False


# ---------------------------------------------------------------------------
# Plan schema validation
# ---------------------------------------------------------------------------

_VALID_PLAN: dict[str, Any] = {
    "strategy": "docker_compose_private",
    "risk_level": "low",
    "summary": "Test plan",
    "docker_ai": {"provider": "litellm", "analysis_id": None, "reasoning_summary": "", "warnings": []},
    "detected_server_state": {
        "deployment_manager": "none",
        "reverse_proxy": "none",
        "confidence": 0.7,
        "evidence": [],
    },
    "deployment_actions": [],
    "rollback_plan": {
        "type": "compose_snapshot",
        "previous_release_ref_required": False,
        "steps": [],
    },
    "blocking_questions": [],
}


def test_plan_schema_valid():
    errors = validate_plan(_VALID_PLAN)
    assert errors == []


def test_plan_schema_missing_required():
    plan = {"strategy": "docker_compose_private"}
    errors = validate_plan(plan)
    assert errors  # missing risk_level, summary, docker_ai, detected_server_state


def test_plan_schema_invalid_strategy():
    plan = dict(_VALID_PLAN)
    plan["strategy"] = "fly_over_the_moon"
    errors = validate_plan(plan)
    assert errors


def test_plan_schema_invalid_risk_level():
    plan = dict(_VALID_PLAN)
    plan["risk_level"] = "catastrophic"
    errors = validate_plan(plan)
    assert errors


# ---------------------------------------------------------------------------
# Deterministic validator
# ---------------------------------------------------------------------------


def _make_snapshot(
    dm: str = "none",
    rp: str = "none",
    ports: list[int] | None = None,
    containers: list[ContainerInfo] | None = None,
) -> InventorySnapshot:
    listening = [ListeningPort(port=p, proto="tcp", process="existing") for p in (ports or [])]
    return InventorySnapshot(
        target_id="t1",
        project_id="p1",
        listening_ports=listening,
        containers=containers or [],
        detection=DetectionResult(
            deployment_manager=DeploymentManager(dm),
            reverse_proxy=ReverseProxy(rp),
            confidence=0.9,
            evidence=[],
        ),
    )


def test_validator_passes_clean_plan():
    result = validate_deployment_plan(_VALID_PLAN, _make_snapshot())
    assert result.status in ("passed", "warning")


def test_validator_blocks_forbidden_action():
    plan = dict(_VALID_PLAN)
    plan["deployment_actions"] = [{"action_type": "SHELL", "params": {}}]
    result = validate_deployment_plan(plan, _make_snapshot())
    assert result.status == "blocked"
    assert any("SHELL" in e for e in result.blocking_errors)


def test_validator_blocks_port_conflict():
    plan = dict(_VALID_PLAN)
    plan["port_plan"] = {"host_port": 8080, "internal_app_port": 3000, "uses_reverse_proxy": False, "reverse_proxy_type": ""}
    snapshot = _make_snapshot(ports=[8080])
    result = validate_deployment_plan(plan, snapshot)
    assert result.status == "blocked"
    assert any("8080" in e for e in result.blocking_errors)


def test_validator_blocks_kamal_v2_with_traefik_strategy():
    plan = dict(_VALID_PLAN)
    plan["strategy"] = "reuse_existing_traefik"
    plan["detected_server_state"] = {
        "deployment_manager": "kamal_v2",
        "reverse_proxy": "kamal_proxy",
        "confidence": 0.95,
        "evidence": [],
    }
    snapshot = _make_snapshot(dm="kamal_v2", rp="kamal_proxy")
    result = validate_deployment_plan(plan, snapshot)
    assert result.status == "blocked"


def test_validator_blocks_mixed_setup():
    plan = dict(_VALID_PLAN)
    plan["strategy"] = "docker_compose_private"
    plan["detected_server_state"] = {
        "deployment_manager": "mixed",
        "reverse_proxy": "unknown",
        "confidence": 0.9,
        "evidence": [],
    }
    snapshot = _make_snapshot(dm="mixed", rp="unknown")
    result = validate_deployment_plan(plan, snapshot)
    assert result.status == "blocked"


# ---------------------------------------------------------------------------
# Typed executor: action type allowlist/denylist
# ---------------------------------------------------------------------------


def test_validate_actions_accepts_allowed():
    actions = [{"action_type": "DOCKER_COMPOSE_UP", "params": {}}]
    _validate_actions(actions)  # should not raise


def test_validate_actions_rejects_shell():
    with pytest.raises(ActionTypeRejected, match="SHELL"):
        _validate_actions([{"action_type": "SHELL", "params": {}}])


def test_validate_actions_rejects_exec():
    with pytest.raises(ActionTypeRejected, match="EXEC"):
        _validate_actions([{"action_type": "EXEC", "params": {}}])


def test_validate_actions_rejects_bash():
    with pytest.raises(ActionTypeRejected, match="BASH"):
        _validate_actions([{"action_type": "BASH", "params": {}}])


def test_validate_actions_rejects_custom_command():
    with pytest.raises(ActionTypeRejected, match="CUSTOM_COMMAND"):
        _validate_actions([{"action_type": "CUSTOM_COMMAND", "params": {}}])


def test_validate_actions_rejects_unknown():
    with pytest.raises(ActionTypeRejected):
        _validate_actions([{"action_type": "DEPLOY_EVERYTHING", "params": {}}])


# ---------------------------------------------------------------------------
# MCP tool policy
# ---------------------------------------------------------------------------


def test_mcp_policy_allows_docker_inspect():
    assert is_tool_allowed("docker:inspect") is True


def test_mcp_policy_allows_filesystem_read():
    assert is_tool_allowed("filesystem:read") is True


def test_mcp_policy_denies_shell():
    with pytest.raises(MCPToolDenied):
        validate_tool_call("shell")


def test_mcp_policy_denies_docker_exec():
    with pytest.raises(MCPToolDenied):
        validate_tool_call("docker:exec")


def test_mcp_policy_denies_docker_compose_up():
    with pytest.raises(MCPToolDenied):
        validate_tool_call("docker:compose:up")


def test_mcp_policy_denies_arbitrary():
    with pytest.raises(MCPToolDenied):
        validate_tool_call("some:random:tool")


# ---------------------------------------------------------------------------
# Policy hard rules
# ---------------------------------------------------------------------------


def test_policy_blocks_mixed_non_manual():
    plan = dict(_VALID_PLAN)
    plan["strategy"] = "docker_compose_private"
    plan["detected_server_state"] = {
        "deployment_manager": "mixed", "reverse_proxy": "unknown",
        "confidence": 0.9, "evidence": [],
    }
    violations = check_policy(plan, {"deployment_manager": "mixed", "reverse_proxy": "unknown"})
    assert any(v.blocking for v in violations)


def test_policy_blocks_kamal_proxy_with_traefik_strategy():
    plan = dict(_VALID_PLAN)
    plan["strategy"] = "reuse_existing_traefik"
    plan["detected_server_state"] = {
        "deployment_manager": "kamal_v2", "reverse_proxy": "kamal_proxy",
        "confidence": 0.9, "evidence": [],
    }
    violations = check_policy(plan, {"deployment_manager": "kamal_v2", "reverse_proxy": "kamal_proxy"})
    assert any("kamal" in v.description.lower() for v in violations)


def test_policy_blocks_abort_strategy():
    plan = dict(_VALID_PLAN)
    plan["strategy"] = "abort"
    violations = check_policy(plan, {"deployment_manager": "none", "reverse_proxy": "none"})
    assert any(v.rule == "strategy_abort" for v in violations)


def test_policy_no_violations_clean_plan():
    plan = dict(_VALID_PLAN)
    plan["strategy"] = "docker_compose_private"
    plan["detected_server_state"] = {
        "deployment_manager": "none", "reverse_proxy": "none",
        "confidence": 0.9, "evidence": [],
    }
    violations = check_policy(plan, {"deployment_manager": "none", "reverse_proxy": "none"})
    assert all(not v.blocking for v in violations)
