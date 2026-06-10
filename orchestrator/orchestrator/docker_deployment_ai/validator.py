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

# Actions that are schema-valid and allowed by the allowlist but not yet
# implemented by the executor. The validator blocks these BEFORE approval so
# the user gets a clear error rather than a runtime failure after execution starts.
#
# Keep this list in sync with executor._NOT_IMPLEMENTED_ACTIONS.
_EXECUTOR_NOT_IMPLEMENTED: frozenset[str] = frozenset([
    "DOCKER_LOGIN",
    "ROLLBACK_TO_PREVIOUS_RELEASE",
])


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


_PROXY_OWNS_80_443: frozenset[str] = frozenset(["traefik", "kamal_proxy", "nginx", "caddy"])
_HOST_BIND_STRATEGIES: frozenset[str] = frozenset(["docker_compose_with_host_port"])
_TRAEFIK_STRATEGIES: frozenset[str] = frozenset(["reuse_existing_traefik"])


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

    # Host port conflict: plan requests a port that is already listening on the host.
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

    # Domain conflict: check Traefik Host() router rules across all container labels.
    domain = routing_plan.get("domain")
    if domain:
        for c in snapshot.containers:
            for k, v in c.labels.items():
                # Match traefik.http.routers.*.rule labels containing Host(`domain`)
                if "rule" in k.lower() and f"host(`{domain}`)" in v.lower():
                    checks.append(ValidationCheck(
                        "domain_not_already_routed", False,
                        f"Domain '{domain}' already exists in Traefik Host() rule "
                        f"on container '{c.name}' (label: {k})."
                    ))
                    break
                # Fallback: older label format without Host() syntax
                elif "rule" in k.lower() and domain in v and "host(" not in v.lower():
                    checks.append(ValidationCheck(
                        "domain_not_already_routed", False,
                        f"Domain '{domain}' is already routed to container '{c.name}' "
                        f"via label '{k}'."
                    ))
                    break

    # Published Docker port conflict: check if any container already publishes
    # the same host port the plan requests.
    if host_port:
        for c in snapshot.containers:
            for port_entry in c.ports:
                existing_host_port = port_entry.get("host_port") or port_entry.get("HostPort")
                if existing_host_port and int(existing_host_port) == int(host_port):
                    checks.append(ValidationCheck(
                        f"docker_published_port_{host_port}_free", False,
                        f"Docker port {host_port} is already published by container '{c.name}'."
                    ))
                    break

    # Direct bind to 80/443 when a proxy already owns those ports.
    # Strategies that try to bind a host port will conflict with proxy ownership.
    _PORT_80_443 = {80, 443}
    plan_binds_privileged_port = (
        strategy in _HOST_BIND_STRATEGIES
        and host_port is not None
        and int(host_port) in _PORT_80_443
    )
    proxy_owns_ports = rp in _PROXY_OWNS_80_443
    if plan_binds_privileged_port and proxy_owns_ports:
        checks.append(ValidationCheck(
            "no_direct_bind_80_443_with_proxy", False,
            f"'{rp}' owns ports 80/443. Plan cannot bind directly to port {host_port}. "
            "Use a strategy compatible with the existing proxy instead."
        ))
    elif proxy_owns_ports and strategy in _HOST_BIND_STRATEGIES:
        # Any host-port strategy conflicts when a proxy owns the routing layer,
        # even if the specific port is not 80/443.
        checks.append(ValidationCheck(
            "no_port_conflict_with_proxy", False,
            f"'{rp}' proxy owns ports 80/443. "
            "Host-port strategy conflicts with the existing proxy."
        ))

    # Kamal v2 / kamal-proxy + Traefik strategy conflict.
    if rp == ReverseProxy.kamal_proxy.value and strategy in _TRAEFIK_STRATEGIES:
        checks.append(ValidationCheck(
            "no_traefik_over_kamal_proxy", False,
            "Cannot use Traefik strategy when kamal-proxy is the active proxy (Kamal v2). "
            "Use 'kamal_v2_compatible' or 'manual_required' instead."
        ))

    # Kamal v2 deployment manager + Traefik strategy (redundant defence).
    if dm == DeploymentManager.kamal_v2.value and strategy in _TRAEFIK_STRATEGIES:
        checks.append(ValidationCheck(
            "no_traefik_strategy_on_kamal_v2", False,
            "Cannot use Traefik strategy when Kamal v2 manages this host."
        ))

    # Mixed routing ownership → deployment cannot proceed automatically.
    if dm == DeploymentManager.mixed.value and strategy not in ("manual_required", "abort"):
        checks.append(ValidationCheck(
            "mixed_routing_blocks_deploy", False,
            "Multiple conflicting reverse proxies detected (mixed setup). "
            "Strategy must be 'manual_required' or 'abort' until routing ownership is clarified."
        ))

    # Unknown reverse proxy owns 80/443 → cannot deploy safely.
    port_80_listening = any(p.port in (80, 443) for p in snapshot.listening_ports)
    if rp == ReverseProxy.unknown.value and port_80_listening and strategy not in ("manual_required", "abort"):
        checks.append(ValidationCheck(
            "unknown_owner_80_443_blocks_deploy", False,
            "An unknown process owns port 80 or 443. "
            "Routing ownership must be established before deploying. "
            "Use 'manual_required' or identify the process first."
        ))

    if not any(not c.passed for c in checks):
        checks.append(ValidationCheck("routing_checks_passed", True, "No routing conflicts detected"))
    return checks


_AUTOMATRON_LABEL = "automatron.owned"


def _check_docker_safety(
    plan: dict[str, Any],
    snapshot: InventorySnapshot,
) -> list[ValidationCheck]:
    """Block plans that would silently overwrite existing Docker resources.

    Checks container, network, and volume name conflicts against the live
    inventory. Resources tagged with Automatron ownership metadata are treated
    as managed and may proceed (with a warning); unowned resources are blocked.
    """
    checks: list[ValidationCheck] = []
    actions = plan.get("deployment_actions") or []

    existing_container_names = {c.name for c in snapshot.containers}
    existing_network_names = {
        (n.get("Name") or n.get("name") or "") for n in snapshot.networks
    }
    existing_volume_names = {
        (v.get("Name") or v.get("name") or "") for v in snapshot.volumes
    }

    def _network_is_owned(name: str) -> bool:
        for n in snapshot.networks:
            n_name = n.get("Name") or n.get("name") or ""
            if n_name == name:
                labels = n.get("Labels") or n.get("labels") or {}
                if isinstance(labels, str):
                    return _AUTOMATRON_LABEL in labels
                return bool(labels.get(_AUTOMATRON_LABEL))
        return False

    def _volume_is_owned(name: str) -> bool:
        for v in snapshot.volumes:
            v_name = v.get("Name") or v.get("name") or ""
            if v_name == name:
                labels = v.get("Labels") or v.get("labels") or {}
                if isinstance(labels, str):
                    return _AUTOMATRON_LABEL in labels
                return bool(labels.get(_AUTOMATRON_LABEL))
        return False

    network_found = False
    volume_found = False

    for action in actions:
        atype = (action.get("action_type") or "").upper()
        params = action.get("params") or {}

        if atype == "DOCKER_NETWORK_CREATE":
            network_name = params.get("network_name") or params.get("name") or ""
            if network_name and network_name in existing_network_names:
                network_found = True
                if _network_is_owned(network_name):
                    checks.append(ValidationCheck(
                        f"network_owned_exists:{network_name}", True,
                        f"Network '{network_name}' already exists and is Automatron-managed — "
                        "will reuse.",
                        blocking=False,
                    ))
                else:
                    checks.append(ValidationCheck(
                        f"network_conflict:{network_name}", False,
                        f"Docker network '{network_name}' already exists without Automatron "
                        "ownership metadata. Cannot overwrite an unmanaged network."
                    ))

        elif atype == "DOCKER_VOLUME_CREATE":
            volume_name = params.get("volume_name") or params.get("name") or ""
            if volume_name and volume_name in existing_volume_names:
                volume_found = True
                if _volume_is_owned(volume_name):
                    checks.append(ValidationCheck(
                        f"volume_owned_exists:{volume_name}", True,
                        f"Volume '{volume_name}' already exists and is Automatron-managed — "
                        "will reuse.",
                        blocking=False,
                    ))
                else:
                    checks.append(ValidationCheck(
                        f"volume_conflict:{volume_name}", False,
                        f"Docker volume '{volume_name}' already exists without Automatron "
                        "ownership metadata. Cannot silently overwrite an unmanaged volume."
                    ))

        elif atype in ("DOCKER_COMPOSE_UP", "DOCKER_COMPOSE_DOWN", "DOCKER_COMPOSE_CONFIG"):
            # Check for compose project name conflict with existing containers.
            project_name = (
                params.get("project_name")
                or params.get("compose_project")
                or plan.get("compose_project_name")
                or ""
            )
            if project_name:
                # Existing containers whose name starts with the project name
                # indicate a compose project is already running under that name.
                conflict_containers = [
                    c.name for c in snapshot.containers
                    if c.name.startswith(f"{project_name}_")
                    or c.name.startswith(f"{project_name}-")
                ]
                if conflict_containers:
                    # If ANY existing container carries Automatron ownership, allow it.
                    owned = any(
                        snapshot.containers[i].labels.get(_AUTOMATRON_LABEL)
                        for i, c in enumerate(snapshot.containers)
                        if c.name in conflict_containers
                    )
                    if not owned:
                        checks.append(ValidationCheck(
                            f"compose_project_conflict:{project_name}", False,
                            f"Compose project '{project_name}' conflicts with existing "
                            f"containers: {conflict_containers[:3]}. "
                            "This would overwrite a running stack not managed by Automatron."
                        ))

    # Direct container name conflicts from resource_names in plan.
    resource_names = plan.get("resource_names") or {}
    container_name = resource_names.get("container_name") or ""
    if container_name and container_name in existing_container_names:
        checks.append(ValidationCheck(
            f"container_name_conflict:{container_name}", False,
            f"Container '{container_name}' already exists. "
            "Cannot deploy with a conflicting container name."
        ))

    if not any(not c.passed for c in checks):
        checks.append(ValidationCheck(
            "docker_safety_checks_passed", True,
            "No container/network/volume name conflicts detected"
        ))
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


def _check_unimplemented_executor_actions(plan: dict[str, Any]) -> list[ValidationCheck]:
    """Block plans containing action types the executor cannot yet run.

    These actions are schema-valid (so they pass _check_action_types) but the
    executor does not have working implementations for them. Blocking here gives
    the user a clear, early error before approval or execution.
    """
    actions = plan.get("deployment_actions") or []
    found: set[str] = set()
    for action in actions:
        atype = (action.get("action_type") or "").upper()
        if atype in _EXECUTOR_NOT_IMPLEMENTED:
            found.add(atype)
    if not found:
        return [ValidationCheck(
            "executor_capabilities_satisfied", True,
            "All plan actions are supported by the executor",
        )]
    return [
        ValidationCheck(
            f"executor_unimplemented:{atype}", False,
            f"Action type '{atype}' is not yet implemented by the executor. "
            "Remove it from the plan or wait for a version that supports it.",
            blocking=True,
        )
        for atype in sorted(found)
    ]


_COMPOSE_ACTION_TYPES: frozenset[str] = frozenset([
    "DOCKER_COMPOSE_CONFIG",
    "DOCKER_COMPOSE_PULL",
    "DOCKER_COMPOSE_BUILD",
    "DOCKER_COMPOSE_UP",
    "DOCKER_COMPOSE_DOWN",
    "DOCKER_COMPOSE_PS",
])


def _check_docker_compose_v2(
    plan: dict[str, Any],
    snapshot: InventorySnapshot,
) -> ValidationCheck:
    """Block compose-based plans when the target lacks Docker Compose v2.

    This check fires only when the plan actually contains compose actions.
    If the snapshot has no docker_binary_info (e.g. from an older inventory
    that predates this field), the check passes with a warning.
    """
    actions = plan.get("deployment_actions") or []
    uses_compose = any(
        (action.get("action_type") or "").upper() in _COMPOSE_ACTION_TYPES
        for action in actions
    )
    if not uses_compose:
        return ValidationCheck(
            "docker_compose_v2_check", True,
            "Plan does not use Docker Compose actions — no v2 check needed",
        )

    info = snapshot.docker_binary_info
    if not info:
        # Old snapshot pre-dates this field — pass non-blocking to avoid
        # blocking plans validated before this feature was added.
        return ValidationCheck(
            "docker_compose_v2_check", True,
            "Docker Compose v2 availability unknown (inventory pre-dates check) — proceeding",
            blocking=False,
        )

    if not info.get("docker_binary_present", False):
        return ValidationCheck(
            "docker_compose_v2_check", False,
            "Docker binary not found on the target host. "
            "Install Docker Engine before deploying.",
        )

    if not info.get("docker_compose_v2_available", False):
        return ValidationCheck(
            "docker_compose_v2_check", False,
            "Docker Compose v2 plugin is not available on the target host "
            "(`docker compose version` failed). "
            "Install the Docker Compose v2 plugin: "
            "https://docs.docker.com/compose/install/",
        )

    return ValidationCheck(
        "docker_compose_v2_check", True,
        "Docker Compose v2 is available on the target host",
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

    # 2. Action types (forbidden / unknown)
    all_checks.extend(_check_action_types(plan))

    # 2b. Executor capability gate — blocks known-unimplemented actions early
    all_checks.extend(_check_unimplemented_executor_actions(plan))

    # 2c. Docker Compose v2 prerequisite — blocks compose plans on v1-only hosts
    all_checks.append(_check_docker_compose_v2(plan, snapshot))

    # 3. Rollback
    all_checks.append(_check_rollback(plan))

    # 4. Routing
    all_checks.extend(_check_routing(plan, snapshot))

    # 4b. Docker resource safety (container/network/volume name conflicts)
    all_checks.extend(_check_docker_safety(plan, snapshot))

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
