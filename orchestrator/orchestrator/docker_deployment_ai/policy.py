"""Hard deployment policy rules — spec §30.

These are non-negotiable and are enforced in code. Any of these violations
blocks deployment regardless of Docker AI confidence or user intent.
`check_policy` returns a list of `PolicyViolation` objects. An empty list
means no hard violations were found.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from orchestrator.docker_deployment_ai.models import DeploymentManager, ReverseProxy


@dataclass
class PolicyViolation:
    rule: str
    description: str
    blocking: bool = True


def check_policy(plan: dict[str, Any], inventory_detection: dict[str, Any]) -> list[PolicyViolation]:
    """Evaluate hard policy rules against *plan* and *inventory_detection*.

    *inventory_detection* must contain at minimum:
      - deployment_manager: str
      - reverse_proxy: str
    *plan* must be a dict following the deployment_plan.json schema.

    Returns a list of `PolicyViolation`. If any have `blocking=True` the
    plan must not be executed.
    """
    violations: list[PolicyViolation] = []
    strategy = plan.get("strategy", "")
    server_state = plan.get("detected_server_state", {})
    dm = server_state.get("deployment_manager") or inventory_detection.get("deployment_manager", "unknown")
    rp = server_state.get("reverse_proxy") or inventory_detection.get("reverse_proxy", "unknown")

    # Rule 1: Gordon / Docker AI recommendations never bypass validator or approval.
    # (Enforced architecturally — this check catches plans that somehow lack server state.)
    if dm == "unknown" and strategy not in ("manual_required", "abort"):
        violations.append(PolicyViolation(
            rule="unknown_ownership_blocks_deploy",
            description=(
                f"Deployment manager is 'unknown' but strategy is '{strategy}'. "
                "Routing ownership must be established before deploying."
            ),
        ))

    # Rule 2: Never install a second reverse proxy when 80/443 are owned.
    proxy_install_strategies = {
        "reuse_existing_traefik",  # reuses, not installs — OK
    }
    traefik_install_strategies = {
        "reuse_existing_traefik",  # reuses existing — OK if Traefik is present
    }
    if rp in ("nginx", "caddy", "kamal_proxy") and strategy == "reuse_existing_traefik":
        violations.append(PolicyViolation(
            rule="no_second_proxy_on_owned_ports",
            description=(
                f"Existing proxy '{rp}' owns routing. Strategy 'reuse_existing_traefik' "
                "cannot be used when Traefik is not the active proxy."
            ),
        ))

    # Rule 3: Never install Traefik on top of Kamal v2 / kamal-proxy.
    if rp == "kamal_proxy" and strategy in (
        "reuse_existing_traefik", "docker_compose_with_host_port"
    ):
        violations.append(PolicyViolation(
            rule="no_traefik_over_kamal_v2",
            description=(
                "kamal-proxy is present. Cannot use Traefik-based or host-port strategies "
                "that would conflict with kamal-proxy routing."
            ),
        ))

    # Rule 4: If Kamal v2/kamal-proxy exists, do not allow strategies that bind 80/443.
    if dm in (DeploymentManager.kamal_v2.value,) and strategy == "docker_compose_with_host_port":
        violations.append(PolicyViolation(
            rule="no_host_port_over_kamal_v2",
            description=(
                "Kamal v2 manages this host. 'docker_compose_with_host_port' would conflict "
                "with kamal-proxy port ownership."
            ),
        ))

    # Rule 5: Mixed setup must block unless strategy is manual_required.
    if dm == DeploymentManager.mixed.value and strategy not in ("manual_required", "abort"):
        violations.append(PolicyViolation(
            rule="mixed_setup_blocks_auto_deploy",
            description=(
                "Multiple conflicting reverse proxies detected. Strategy must be "
                "'manual_required' or 'abort' until the setup is clarified."
            ),
        ))

    # Rule 6: Secrets must be declared (blocking_questions must be empty for execution).
    blocking_questions = plan.get("blocking_questions") or []
    if blocking_questions:
        violations.append(PolicyViolation(
            rule="blocking_questions_unresolved",
            description=(
                f"{len(blocking_questions)} blocking question(s) must be resolved before "
                "deployment: " + "; ".join(blocking_questions[:3])
            ),
        ))

    # Rule 7: abort strategy always blocks.
    if strategy == "abort":
        violations.append(PolicyViolation(
            rule="strategy_abort",
            description="Deployment strategy is 'abort' — plan explicitly requires manual intervention.",
        ))

    return violations
