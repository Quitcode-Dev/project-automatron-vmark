"""InventoryCollector — read-only remote server inspection.

Orchestrates SSH commands → parsers → detectors to produce an immutable
`InventorySnapshot`. The snapshot is then stored in `deployment_inventory_snapshots`
and fed (sanitized) to Docker AI providers.

All SSH commands used here must be on the allowlist in `ssh_client.py`.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

import aiosqlite

from orchestrator.docker_deployment_ai.deployment_detector import refine_with_kamal_signals
from orchestrator.docker_deployment_ai.docker_remote import (
    enrich_container_from_inspect,
    parse_containers,
    parse_docker_info,
    parse_images,
    parse_networks,
    parse_volumes,
)
from orchestrator.docker_deployment_ai.models import (
    ContainerInfo,
    DeploymentTarget,
    InventorySnapshot,
    ListeningPort,
)
from orchestrator.docker_deployment_ai.port_scanner import parse_netstat_output, parse_ss_output
from orchestrator.docker_deployment_ai.reverse_proxy_detector import detect_reverse_proxy
from orchestrator.docker_deployment_ai.ssh_client import SSHClient, SSHError
from orchestrator.docker_deployment_ai.target_store import update_last_snapshot

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_run(client: SSHClient, cmd: str, timeout: int = 20) -> str:
    """Run *cmd* via SSH, return stdout on success or "" on error."""
    try:
        rc, stdout, _ = client.run(cmd, timeout=timeout)
        return stdout if rc == 0 else ""
    except Exception as exc:
        logger.debug("SSH command failed (%s): %s", cmd, exc)
        return ""


def _collect_host_info(client: SSHClient) -> dict[str, Any]:
    hostname = _safe_run(client, "hostname").strip()
    uname = _safe_run(client, "uname -a").strip()
    os_release_raw = _safe_run(client, "cat /etc/os-release")
    os_info: dict[str, str] = {}
    for line in os_release_raw.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            os_info[k.strip()] = v.strip().strip('"')
    nproc = _safe_run(client, "nproc").strip()
    free_raw = _safe_run(client, "free -b")
    mem: dict[str, int] = {}
    for line in free_raw.splitlines():
        if line.startswith("Mem:"):
            parts = line.split()
            if len(parts) >= 3:
                try:
                    mem = {"total": int(parts[1]), "available": int(parts[6]) if len(parts) > 6 else 0}
                except (ValueError, IndexError):
                    pass
    whoami = _safe_run(client, "whoami").strip()
    return {
        "hostname": hostname,
        "uname": uname,
        "os": os_info,
        "cpu_count": int(nproc) if nproc.isdigit() else None,
        "memory": mem,
        "current_user": whoami,
    }


def _collect_listening_ports(client: SSHClient) -> list[ListeningPort]:
    ss_tcp = _safe_run(client, "ss -tlnp")
    ss_udp = _safe_run(client, "ss -ulnp")
    ports = parse_ss_output(ss_tcp, proto="tcp") + parse_ss_output(ss_udp, proto="udp")
    if not ports:
        netstat = _safe_run(client, "netstat -tlnp")
        if netstat:
            ports = parse_netstat_output(netstat)
    return ports


def _collect_service_info(client: SSHClient) -> dict[str, str]:
    return {
        "traefik_systemd": _safe_run(client, "systemctl is-active traefik"),
        "nginx_systemd": _safe_run(client, "systemctl is-active nginx"),
        "caddy_systemd": _safe_run(client, "systemctl is-active caddy"),
        "ps_output": _safe_run(client, "ps aux", timeout=10),
    }


def _collect_containers(client: SSHClient) -> list[ContainerInfo]:
    raw_ps = _safe_run(client, "docker ps -a --format json", timeout=30)
    containers = parse_containers(raw_ps)
    # Enrich first 20 containers with inspect data (avoid huge payloads)
    enriched = []
    for c in containers[:20]:
        raw_inspect = _safe_run(client, f"docker inspect {c.id}", timeout=15)
        if raw_inspect:
            c = enrich_container_from_inspect(c, raw_inspect)
        enriched.append(c)
    enriched.extend(containers[20:])
    return enriched


def _collect_filesystem_hints(client: SSHClient, deploy_path: str) -> dict[str, Any]:
    hints: dict[str, Any] = {}
    for path in ["/opt", "/opt/apps", "/var/www", "/home/deploy", deploy_path]:
        listing = _safe_run(client, f"ls -la {path}")
        if listing:
            hints[path] = listing[:2000]
    # Look for deploy.yml (Kamal)
    deploy_yml = _safe_run(
        client,
        "test -f /etc/kamal/deploy.yml && echo found || echo not_found",
    )
    hints["kamal_deploy_yml"] = deploy_yml.strip()
    return hints


async def collect_inventory(
    target: DeploymentTarget,
    db: aiosqlite.Connection,
) -> tuple[str, InventorySnapshot]:
    """Run read-only inventory on *target*, persist snapshot, return (id, snapshot).

    The snapshot is stored immutably — never updated after creation.
    """
    snapshot_id = str(uuid.uuid4())
    client = SSHClient(
        host=target.host,
        user=target.ssh_user,
        port=target.ssh_port,
        ssh_key_path=target.auth_reference if target.auth_mode == "ssh_key" else None,
    )

    error: str | None = None
    snapshot: InventorySnapshot

    try:
        logger.info("Collecting inventory for target %s (%s@%s)", target.id, target.ssh_user, target.host)

        host_info = _collect_host_info(client)
        listening_ports = _collect_listening_ports(client)
        service_info = _collect_service_info(client)
        containers = _collect_containers(client)
        filesystem_hints = _collect_filesystem_hints(client, target.deploy_path)

        raw_docker_info = _safe_run(client, "docker info --format json", timeout=30)
        docker_info = parse_docker_info(raw_docker_info)

        raw_images = _safe_run(client, "docker images --format json", timeout=20)
        images = parse_images(raw_images)

        raw_networks = _safe_run(client, "docker network ls --format json", timeout=20)
        networks = parse_networks(raw_networks)

        raw_volumes = _safe_run(client, "docker volume ls --format json", timeout=20)
        volumes = parse_volumes(raw_volumes)

        # Detect reverse proxy and deployment manager
        base_detection = detect_reverse_proxy(
            containers,
            listening_ports,
            systemctl_traefik=service_info.get("traefik_systemd", ""),
            systemctl_nginx=service_info.get("nginx_systemd", ""),
            systemctl_caddy=service_info.get("caddy_systemd", ""),
            ps_output=service_info.get("ps_output", ""),
        )
        deploy_yml = filesystem_hints.get("kamal_deploy_yml", "")
        detection = refine_with_kamal_signals(
            base_detection,
            containers,
            networks,
            deploy_yml_content=deploy_yml,
        )

        snapshot = InventorySnapshot(
            target_id=target.id,
            project_id=target.project_id,
            host_info=host_info,
            docker_info=docker_info,
            containers=containers,
            images=images,
            networks=networks,
            volumes=volumes,
            listening_ports=listening_ports,
            detection=detection,
            filesystem_hints=filesystem_hints,
        )

    except SSHError as exc:
        error = str(exc)
        logger.error("SSH error during inventory for target %s: %s", target.id, error)
        snapshot = InventorySnapshot(
            target_id=target.id,
            project_id=target.project_id,
            error=error,
        )

    # Serialize for DB storage
    raw_json = snapshot.model_dump_json()
    summary: dict[str, Any] = {
        "detected_reverse_proxy": snapshot.detection.reverse_proxy.value,
        "detected_deployment_manager": snapshot.detection.deployment_manager.value,
        "confidence": snapshot.detection.confidence,
        "container_count": len(snapshot.containers),
        "error": error,
    }
    summary_json = json.dumps(summary)

    await db.execute(
        """
        INSERT INTO deployment_inventory_snapshots
        (id, target_id, project_id, status, raw_json, summary_json,
         detected_reverse_proxy, detected_deployment_manager, confidence,
         error_message, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            snapshot_id, target.id, target.project_id,
            "error" if error else "ok",
            raw_json, summary_json,
            snapshot.detection.reverse_proxy.value,
            snapshot.detection.deployment_manager.value,
            snapshot.detection.confidence,
            error, _now(),
        ),
    )
    await db.commit()
    await update_last_snapshot(db, target.id, snapshot_id)

    logger.info(
        "Inventory complete for target %s: manager=%s proxy=%s confidence=%.2f",
        target.id,
        snapshot.detection.deployment_manager.value,
        snapshot.detection.reverse_proxy.value,
        snapshot.detection.confidence,
    )
    return snapshot_id, snapshot


def build_sanitized_inventory(snapshot: InventorySnapshot) -> dict[str, Any]:
    """Return a dict safe to send to Docker AI providers (no secret values)."""
    from orchestrator.docker_deployment_ai.secrets import redact_env_dict

    containers_safe = []
    for c in snapshot.containers:
        containers_safe.append({
            "name": c.name,
            "image": c.image,
            "status": c.status,
            "labels": c.labels,
            "ports": c.ports,
            "networks": c.networks,
            "env_keys": c.env_keys,
            # mounts included for compose/volume conflict detection
            "mounts": [
                {k: v for k, v in m.items() if k != "source" or not str(v).startswith("/run/secrets")}
                for m in c.mounts
            ],
        })

    return {
        "host": {
            "hostname": snapshot.host_info.get("hostname"),
            "os": snapshot.host_info.get("os", {}).get("PRETTY_NAME"),
            "architecture": snapshot.docker_info.get("architecture"),
            "cpu_count": snapshot.host_info.get("cpu_count"),
            "memory_bytes": snapshot.host_info.get("memory", {}).get("total"),
        },
        "docker": {
            "version": snapshot.docker_info.get("version"),
            "containers_running": snapshot.docker_info.get("containers_running"),
        },
        "containers": containers_safe,
        "networks": [{"name": n.get("name"), "driver": n.get("driver")} for n in snapshot.networks],
        "volumes": [{"name": v.get("name"), "driver": v.get("driver")} for v in snapshot.volumes],
        "listening_ports": [
            {"port": p.port, "proto": p.proto, "process": p.process}
            for p in snapshot.listening_ports
        ],
        "detection": {
            "deployment_manager": snapshot.detection.deployment_manager.value,
            "reverse_proxy": snapshot.detection.reverse_proxy.value,
            "confidence": snapshot.detection.confidence,
            "evidence": snapshot.detection.evidence,
        },
    }
