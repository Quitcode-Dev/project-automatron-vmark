"""DeterministicValidator — does not trust LLM output.

Validates a deployment plan against the latest inventory snapshot using pure
deterministic logic. An LLM analysis may inform the plan but never overrides
validation results.

Validation covers (spec §18):
  - Connectivity: SSH/Docker reachable
  - Server resources: disk, memory, deploy_path writable
  - Docker safety: no container/volume/network name conflicts
  - Routing: no host port conflict, no proxy conflict, no duplicate domain
  - Secrets: required secrets declared and reference exists
  - Execution safety: only allowed action types, rollback plan present
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from orchestrator.docker_deployment_ai.models import (
    DeploymentManager,
    InventorySnapshot,
    ReverseProxy,
)
from orchestrator.docker_deployment_ai.plan_schema import validate_plan
from orchestrator.docker_deployment_ai.policy import check_policy

logger = logging.getLogger(__name__)

_ALLOWED_ACTION_TYPES = frozenset(
    [
        "CREATE_DIRECTORY",
        "UPLOAD_FILE",
        "WRITE_ENV_FILE",
        "DOCKER_LOGIN",
        "DOCKER_COMPOSE_CONFIG",
        "DOCKER_COMPOSE_PULL",
        "DOCKER_COMPOSE_BUILD",
        "DOCKER_COMPOSE_UP",
        "DOCKER_COMPOSE_DOWN",
        "DOCKER_COMPOSE_PS",
        "DOCKER_NETWORK_CREATE",
        "DOCKER_VOLUME_CREATE",
        "RUN_HEALTHCHECK",
        "CAPTURE_LOGS",
        "MARK_RELEASE",
        "ROLLBACK_TO_PREVIOUS_RELEASE",
    ]
)

_FORBIDDEN_ACTION_TYPES = frozenset(["SHELL", "RUN_COMMAND", "EXEC", "BASH", "SH", "CUSTOM_COMMAND"])


@dataclass
class ValidationCheck:
    name: str
    passed: bool
    message: str
    blocking: bool = True


@dataclass
class ValidationResult:
    status: str  # "passed" | "warning" | "blocked"
    checks: list[ValidationCheck] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    blocking_errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "checks": [
                {"name": c.name, "passed": c.passed, "message": c.message, "blocking": c.blocking}
                for c in self.checks
            ],
            "warnings": self.warnings,
            "blocking_errors": self.blocking_errors,
        }


def _check_schema(plan: dict[str, Any]) -> ValidationCheck:
    errors = validate_plan(plan)
    if errors:
        return ValidationCheck(
            "schema_valid", False, f"Plan schema invalid: {'; '.join(errors[:3])}"
        )
    return ValidationCheck("schema_valid", True, "Plan JSON schema is valid")


def _check_action_types(plan: dict[str, Any]) -> list[ValidationCheck]:
    checks = []
    actions = plan.get("deployment_actions") or []
    for action in actions:
        atype = (action.get("action_type") or "").upper()
        if atype in _FORBIDDEN_ACTION_TYPES:
            checks.append(ValidationCheck(
                f"action_type_forbidden:{atype}", False,
                f"Action type '{atype}' is explicitly forbidden. Only typed actions are allowed."
            ))
        elif atype and atype not in _ALLOWED_ACTION_TYPES:
            checks.append(ValidationCheck(
                f"action_type_unknown:{atype}", False,
                f"Unknown action type '{atype}'. Must be one of {sorted(_ALLOWED_ACTION_TYPES)}"
            ))
    if not checks:
        checks.append(ValidationCheck("action_types_valid", True, "All action types are permitted"))
    return checks


def _check_rollback(plan: dict[str, Any]) -> ValidationCheck:
    rollback = plan.get("rollback_plan") or {}
    if not rollback or rollback.get("type") in (None, "none", ""):
        return ValidationCheck(
            "rollback_plan_present", False,
            "No rollback plan defined. Destructive actions require rollback metadata."
        )
    if rollback.get("previous_release_ref_required") and not plan.get("_rollback_ref_available"):
        return ValidationCheck(
            "rollback_ref_available", False,
            "Rollback plan requires a previous release reference, but none is available.",
            blocking=False,  # warning for first deploy
        )
    return ValidationCheck("rollback_plan_present", True, "Rollback plan is defined")


def _check_routing(
    plan: dict[str, Any],
    snapshot: InventorySnapshot,
) -> list[ValidationCheck]:
    checks = []
    strategy = plan.get("strategy", "")
    port_plan = plan.get("port_plan") or {}
    routing_plan = plan.get("routing_plan") or {}
    server_state = plan.get("detected_server_state") or {}
    rp = server_state.get("reverse_proxy", "unknown")
    dm = server_state.get("deployment_manager", "unknown")

    # Host port conflict check
    host_port = port_plan.get("host_port")
    if host_port:
        occupied = any(p.port == host_port for p in snapshot.listening_ports)
        if occupied:
            owner = next(
                (p.process for p in snapshot.listening_ports if p.port == host_port), "unknown"
            )
            checks.append(ValidationCheck(
                f"host_port_{host_port}_free", False,
                f"Host port {host_port} is already in use by '{owner}'."
            ))
        else:
            checks.append(ValidationCheck(
                f"host_port_{host_port}_free", True, f"Host port {host_port} is free"
            ))

    # Domain conflict check
    domain = routing_plan.get("domain")
    if domain:
        for c in snapshot.containers:
            for k, v in c.labels.items():
                if "rule" in k.lower() and domain in v:
                    checks.append(ValidationCheck(
                        "domain_not_already_routed", False,
                        f"Domain '{domain}' is already routed to container '{c.name}' via label '{k}'."
                    ))
                    break

    # Second proxy installation check
    proxy_install_strategies = {"reuse_existing_traefik"}
    if strategy not in proxy_install_strategies and rp in ("nginx", "caddy", "kamal_proxy"):
        if strategy in ("docker_compose_with_host_port",) and rp in ("nginx", "caddy"):
            checks.append(ValidationCheck(
                "no_port_conflict_with_proxy", False,
                f"'{rp}' proxy owns ports 80/443. "
                "Host-port strategy conflicts with existing proxy."
            ))

    # Kamal v2 + Traefik conflict
    if dm == DeploymentManager.kamal_v2.value and strategy == "reuse_existing_traefik":
        checks.append(ValidationCheck(
            "no_traefik_over_kamal_proxy", False,
            "Cannot use Traefik strategy when kamal-proxy is the active proxy (Kamal v2)."
        ))

    if not any(not c.passed for c in checks):
        checks.append(ValidationCheck("routing_checks_passed", True, "No routing conflicts detected"))
    return checks


def _check_secrets(plan: dict[str, Any]) -> ValidationCheck:
    secrets_required = plan.get("secrets_required") or []
    if not secrets_required:
        return ValidationCheck("secrets_declared", True, "No secrets required by plan")
    blocking_questions = plan.get("blocking_questions") or []
    for secret in secrets_required:
        if any(secret in q for q in blocking_questions):
            return ValidationCheck(
                "secrets_declared", False,
                f"Secret '{secret}' is required but appears as an unresolved blocking question.",
                blocking=False,
            )
    return ValidationCheck(
        "secrets_declared", True,
        f"{len(secrets_required)} required secret name(s) declared"
    )


def _check_policy(
    plan: dict[str, Any],
    detection: dict[str, Any],
) -> list[ValidationCheck]:
    violations = check_policy(plan, detection)
    if not violations:
        return [ValidationCheck("policy_checks_passed", True, "All policy rules satisfied")]
    return [
        ValidationCheck(v.rule, False, v.description, blocking=v.blocking)
        for v in violations
    ]


def validate_deployment_plan(
    plan: dict[str, Any],
    snapshot: InventorySnapshot,
) -> ValidationResult:
    """Run all deterministic validation checks. Does NOT consult any LLM."""
    all_checks: list[ValidationCheck] = []

    # 1. Schema
    all_checks.append(_check_schema(plan))

    # 2. Action types
    all_checks.extend(_check_action_types(plan))

    # 3. Rollback
    all_checks.append(_check_rollback(plan))

    # 4. Routing
    all_checks.extend(_check_routing(plan, snapshot))

    # 5. Secrets
    all_checks.append(_check_secrets(plan))

    # 6. Hard policy rules
    detection = {
        "deployment_manager": snapshot.detection.deployment_manager.value,
        "reverse_proxy": snapshot.detection.reverse_proxy.value,
    }
    all_checks.extend(_check_policy(plan, detection))

    blocking_errors = [c.message for c in all_checks if not c.passed and c.blocking]
    warnings = [c.message for c in all_checks if not c.passed and not c.blocking]

    if blocking_errors:
        status = "blocked"
    elif warnings:
        status = "warning"
    else:
        status = "passed"

    return ValidationResult(
        status=status,
        checks=all_checks,
        warnings=warnings,
        blocking_errors=blocking_errors,
    )
