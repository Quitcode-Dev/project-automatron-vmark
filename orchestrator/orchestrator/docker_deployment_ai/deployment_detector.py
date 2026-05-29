"""Refine raw `reverse_proxy_detector` results with Kamal v1 detection.

`reverse_proxy_detector` tells us what proxy is running; `deployment_detector`
adds the management layer (Kamal v1 vs plain Traefik, Kamal v2 vs plain
kamal-proxy) by examining additional signals like container naming patterns and
filesystem hints.
"""

from __future__ import annotations

import re

from orchestrator.docker_deployment_ai.models import (
    ContainerInfo,
    DeploymentManager,
    DetectionResult,
    ReverseProxy,
)

# Kamal v1 app container names follow the pattern: <app>-<role>-<hex>
# e.g. myapp-web-12ab34cd
_KAMAL_V1_CONTAINER_RE = re.compile(r"^[a-zA-Z0-9_-]+-\w+-[0-9a-f]{8,}$")

# Kamal v1 deploy.yml has a `traefik:` block at the top level
_KAMAL_V1_DEPLOY_YML_SIGNAL = "traefik:"

# Kamal v2 deploy.yml has a `proxy:` block instead of `traefik:`
_KAMAL_V2_DEPLOY_YML_SIGNAL = "proxy:"

# Kamal v2 Docker network pattern
_KAMAL_V2_NETWORK_RE = re.compile(r"kamal|^deploy-", re.IGNORECASE)


def _looks_like_kamal_v1_container(c: ContainerInfo) -> bool:
    return bool(_KAMAL_V1_CONTAINER_RE.match(c.name))


def _has_kamal_network(networks: list[dict[str, str | None]]) -> bool:
    return any(
        _KAMAL_V2_NETWORK_RE.search(n.get("name") or "") for n in networks
    )


def refine_with_kamal_signals(
    base: DetectionResult,
    containers: list[ContainerInfo],
    networks: list[dict[str, str | None]],
    deploy_yml_content: str = "",
) -> DetectionResult:
    """Return a (possibly upgraded) `DetectionResult` after Kamal-specific checks.

    If the base result already says kamal_v2, nothing changes.
    If Traefik is the proxy and Kamal v1 signals are present, upgrade to kamal_v1.
    """
    if base.deployment_manager == DeploymentManager.kamal_v2:
        return base

    evidence = list(base.evidence)

    # Kamal v2 signals in deploy.yml
    if _KAMAL_V2_DEPLOY_YML_SIGNAL in deploy_yml_content:
        evidence.append("deploy.yml contains 'proxy:' block (Kamal v2 signature)")
        return DetectionResult(
            deployment_manager=DeploymentManager.kamal_v2,
            reverse_proxy=ReverseProxy.kamal_proxy,
            confidence=min(base.confidence + 0.1, 1.0),
            evidence=evidence,
        )

    if base.reverse_proxy != ReverseProxy.traefik:
        return base

    # Kamal v1 signals: Traefik + Kamal-style containers + deploy.yml traefik block
    kamal_v1_containers = [c for c in containers if _looks_like_kamal_v1_container(c)]
    if kamal_v1_containers:
        evidence.append(
            f"Kamal v1-style containers: {[c.name for c in kamal_v1_containers[:3]]}"
        )

    has_kamal_network = _has_kamal_network(networks)
    if has_kamal_network:
        evidence.append("Kamal-named Docker network found")

    has_traefik_block = _KAMAL_V1_DEPLOY_YML_SIGNAL in deploy_yml_content
    if has_traefik_block:
        evidence.append("deploy.yml contains 'traefik:' block (Kamal v1 signature)")

    kamal_v1_score = (
        (1 if kamal_v1_containers else 0)
        + (1 if has_kamal_network else 0)
        + (1 if has_traefik_block else 0)
    )

    if kamal_v1_score >= 2:
        return DetectionResult(
            deployment_manager=DeploymentManager.kamal_v1,
            reverse_proxy=ReverseProxy.traefik,
            confidence=min(base.confidence + 0.05 * kamal_v1_score, 1.0),
            evidence=evidence,
        )

    return DetectionResult(
        deployment_manager=base.deployment_manager,
        reverse_proxy=base.reverse_proxy,
        confidence=base.confidence,
        evidence=evidence,
    )
