"""Detect reverse proxy type from inventory data.

All detection is pure — takes parsed containers, ports, labels, and processes;
returns a `DetectionResult`. No network I/O, no LLM.

Detection priority / precedence:
  Kamal v2  > Kamal v1  > Traefik  > Nginx  > Caddy  > plain Compose  > mixed  > none
"""

from __future__ import annotations

from orchestrator.docker_deployment_ai.models import (
    ContainerInfo,
    DeploymentManager,
    DetectionResult,
    ListeningPort,
    ReverseProxy,
)
from orchestrator.docker_deployment_ai.port_scanner import port_owner
from orchestrator.docker_deployment_ai.service_scanner import (
    detect_caddy_via_process,
    detect_nginx_via_process,
    detect_traefik_via_process,
    is_systemd_active,
)

# Label prefixes that confirm Traefik is managing a container
_TRAEFIK_LABEL_PREFIXES = (
    "traefik.enable",
    "traefik.http.routers.",
    "traefik.http.services.",
    "traefik.http.middlewares.",
)


def _container_is_traefik(c: ContainerInfo) -> bool:
    name_lower = c.name.lower()
    image_lower = c.image.lower()
    if "traefik" in name_lower or "traefik" in image_lower:
        return True
    return any(
        any(k.startswith(prefix) for prefix in _TRAEFIK_LABEL_PREFIXES)
        for k in c.labels
    )


def _owns_public_port(c: ContainerInfo) -> bool:
    """True if the container publishes host port 80 or 443.

    This is the edge-proxy signal: a containerised nginx/caddy *app* listening
    only on an internal port (behind Traefik) does not own the public ports and
    must not be mistaken for the reverse proxy.
    """
    for p in c.ports:
        hp = str(p.get("host_port") or "")
        if hp in ("80", "443"):
            return True
    return False


def _container_is_kamal_proxy(c: ContainerInfo) -> bool:
    name_lower = c.name.lower()
    image_lower = c.image.lower()
    return "kamal-proxy" in name_lower or "kamal-proxy" in image_lower


def _container_is_nginx(c: ContainerInfo) -> bool:
    return "nginx" in c.name.lower() or "nginx" in c.image.lower()


def _container_is_caddy(c: ContainerInfo) -> bool:
    return "caddy" in c.name.lower() or "caddy" in c.image.lower()


def _extract_traefik_routes(containers: list[ContainerInfo]) -> list[str]:
    """Return all Traefik Host() rule values from container labels."""
    rules = []
    for c in containers:
        for k, v in c.labels.items():
            if "rule" in k.lower() and "Host" in v:
                rules.append(v)
    return rules


def detect_reverse_proxy(
    containers: list[ContainerInfo],
    listening_ports: list[ListeningPort],
    *,
    systemctl_traefik: str = "",
    systemctl_nginx: str = "",
    systemctl_caddy: str = "",
    ps_output: str = "",
) -> DetectionResult:
    """Return a `DetectionResult` based on pure analysis of inventory data."""
    evidence: list[str] = []
    proxies_found: set[str] = set()

    # --- Kamal v2 / kamal-proxy ---
    kamal_proxy_containers = [c for c in containers if _container_is_kamal_proxy(c)]
    if kamal_proxy_containers:
        proxies_found.add("kamal_proxy")
        evidence.append(
            f"kamal-proxy container(s) found: {[c.name for c in kamal_proxy_containers]}"
        )
        # Check if kamal-proxy owns 80/443
        for c in kamal_proxy_containers:
            for p in c.ports:
                hp = str(p.get("host_port") or "")
                if hp in ("80", "443"):
                    evidence.append(f"kamal-proxy container owns port {hp}")

    # --- Traefik ---
    traefik_containers = [c for c in containers if _container_is_traefik(c)]
    if traefik_containers:
        proxies_found.add("traefik")
        evidence.append(f"Traefik container(s): {[c.name for c in traefik_containers]}")
        routes = _extract_traefik_routes(containers)
        if routes:
            evidence.append(f"Traefik Host rules: {routes[:5]}")
    if is_systemd_active(systemctl_traefik):
        proxies_found.add("traefik")
        evidence.append("systemd traefik.service is active")
    if ps_output and detect_traefik_via_process(ps_output):
        proxies_found.add("traefik")
        evidence.append("traefik process detected in ps")

    # --- Nginx ---
    nginx_containers = [c for c in containers if _container_is_nginx(c)]
    if nginx_containers:
        proxies_found.add("nginx")
        evidence.append(f"nginx container(s): {[c.name for c in nginx_containers]}")
    if is_systemd_active(systemctl_nginx):
        proxies_found.add("nginx")
        evidence.append("systemd nginx.service is active")
    if ps_output and detect_nginx_via_process(ps_output):
        proxies_found.add("nginx")
        evidence.append("nginx process detected in ps")

    # --- Caddy ---
    caddy_containers = [c for c in containers if _container_is_caddy(c)]
    if caddy_containers:
        proxies_found.add("caddy")
        evidence.append(f"caddy container(s): {[c.name for c in caddy_containers]}")
    if is_systemd_active(systemctl_caddy):
        proxies_found.add("caddy")
        evidence.append("systemd caddy.service is active")
    if ps_output and detect_caddy_via_process(ps_output):
        proxies_found.add("caddy")
        evidence.append("caddy process detected in ps")

    # Port 80/443 ownership cross-reference
    for port_num in (80, 443):
        owner = port_owner(listening_ports, port_num)
        if owner:
            evidence.append(f"Port {port_num} owner: {owner}")

    # --- Classify ---
    if len(proxies_found) > 1:
        # Multiple proxy *signals* often mean one real edge proxy plus backend
        # apps that happen to embed nginx/caddy (e.g. an nginx app container
        # behind Traefik). Disambiguate by host-port 80/443 ownership: if exactly
        # one proxy type owns the public ports, that is the edge proxy and the
        # rest are backend services — not a genuinely mixed setup.
        proxy_containers = {
            "kamal_proxy": kamal_proxy_containers,
            "traefik": traefik_containers,
            "nginx": nginx_containers,
            "caddy": caddy_containers,
        }
        edge_types = {
            t for t in proxies_found
            if any(_owns_public_port(c) for c in proxy_containers.get(t, []))
        }
        if len(edge_types) == 1:
            sole = next(iter(edge_types))
            backends = proxies_found - {sole}
            evidence.append(
                f"Multiple proxy signals {sorted(proxies_found)}, but only {sole!r} "
                f"owns host 80/443; treating {sorted(backends)} as backend services"
            )
            proxies_found = {sole}  # collapse to the edge proxy, fall through
        else:
            return DetectionResult(
                deployment_manager=DeploymentManager.mixed,
                reverse_proxy=ReverseProxy.unknown,
                confidence=0.9,
                evidence=evidence + [f"Multiple proxies detected: {proxies_found}"],
            )

    if "kamal_proxy" in proxies_found:
        return DetectionResult(
            deployment_manager=DeploymentManager.kamal_v2,
            reverse_proxy=ReverseProxy.kamal_proxy,
            confidence=0.95,
            evidence=evidence,
        )

    if "traefik" in proxies_found:
        return DetectionResult(
            deployment_manager=DeploymentManager.traefik,
            reverse_proxy=ReverseProxy.traefik,
            confidence=0.9,
            evidence=evidence,
        )

    if "nginx" in proxies_found:
        return DetectionResult(
            deployment_manager=DeploymentManager.nginx,
            reverse_proxy=ReverseProxy.nginx,
            confidence=0.85,
            evidence=evidence,
        )

    if "caddy" in proxies_found:
        return DetectionResult(
            deployment_manager=DeploymentManager.caddy,
            reverse_proxy=ReverseProxy.caddy,
            confidence=0.85,
            evidence=evidence,
        )

    # No proxy found — check for plain docker / compose
    if containers:
        return DetectionResult(
            deployment_manager=DeploymentManager.plain_docker,
            reverse_proxy=ReverseProxy.none,
            confidence=0.7,
            evidence=evidence + ["containers present, no proxy detected"],
        )

    return DetectionResult(
        deployment_manager=DeploymentManager.none,
        reverse_proxy=ReverseProxy.none,
        confidence=0.5,
        evidence=evidence + ["no containers and no proxy detected"],
    )
