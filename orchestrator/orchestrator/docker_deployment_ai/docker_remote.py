"""Parse remote Docker state from SSH-collected JSON output.

All functions take raw strings (stdout from docker commands) and return
typed Python objects or empty defaults. No network I/O here — just parsing.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from orchestrator.docker_deployment_ai.models import ContainerInfo
from orchestrator.docker_deployment_ai.secrets import redact_env_dict

logger = logging.getLogger(__name__)


def _load_json_lines(raw: str) -> list[dict[str, Any]]:
    """Parse newline-delimited JSON objects (docker --format json output)."""
    results = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                results.append(obj)
        except json.JSONDecodeError:
            pass
    return results


def _load_json_object(raw: str) -> dict[str, Any]:
    if not raw or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def parse_containers(raw_ps: str) -> list[ContainerInfo]:
    """Parse `docker ps -a --format json` (one JSON object per line) output."""
    rows = _load_json_lines(raw_ps)
    containers = []
    for row in rows:
        # docker ps --format json gives: ID, Image, Names, Status, Labels, Ports, Mounts, Networks
        name = row.get("Names") or row.get("Name") or ""
        # Names may be comma-separated; take first
        if "," in name:
            name = name.split(",")[0].strip()
        name = name.lstrip("/")

        labels_raw = row.get("Labels") or ""
        labels: dict[str, str] = {}
        if isinstance(labels_raw, str):
            for pair in labels_raw.split(","):
                if "=" in pair:
                    k, _, v = pair.partition("=")
                    labels[k.strip()] = v.strip()
        elif isinstance(labels_raw, dict):
            labels = {str(k): str(v) for k, v in labels_raw.items()}

        ports_raw = row.get("Ports") or ""
        ports: list[dict[str, Any]] = []
        if isinstance(ports_raw, str) and ports_raw:
            for p in ports_raw.split(","):
                ports.append({"raw": p.strip()})
        elif isinstance(ports_raw, list):
            ports = ports_raw

        networks_raw = row.get("Networks") or ""
        networks: list[str] = (
            [n.strip() for n in networks_raw.split(",") if n.strip()]
            if isinstance(networks_raw, str)
            else list(networks_raw)
        )

        containers.append(
            ContainerInfo(
                id=str(row.get("ID") or row.get("Id") or "")[:12],
                name=name,
                image=str(row.get("Image") or ""),
                status=str(row.get("Status") or ""),
                labels=labels,
                ports=ports,
                mounts=[],
                env_keys=[],
                networks=networks,
            )
        )
    return containers


def enrich_container_from_inspect(
    container: ContainerInfo,
    raw_inspect: str,
) -> ContainerInfo:
    """Merge `docker inspect <id>` output into *container*.

    Extracts env key names (values stripped), mounts, and more-complete
    port/network data from the inspect JSON.
    """
    try:
        data_list = json.loads(raw_inspect)
        if not data_list or not isinstance(data_list, list):
            return container
        data: dict[str, Any] = data_list[0]
    except (json.JSONDecodeError, IndexError):
        return container

    # Env: keep key names only
    raw_env: list[str] = (data.get("Config") or {}).get("Env") or []
    env_keys = [e.split("=", 1)[0] for e in raw_env if "=" in e]

    # Mounts
    mounts_raw: list[dict[str, Any]] = data.get("Mounts") or []
    mounts = [
        {
            "type": m.get("Type"),
            "source": m.get("Source"),
            "destination": m.get("Destination"),
            "mode": m.get("Mode"),
        }
        for m in mounts_raw
    ]

    # Networks
    networks_data: dict[str, Any] = (
        (data.get("NetworkSettings") or {}).get("Networks") or {}
    )
    networks = list(networks_data.keys())

    # Ports
    ports_data: dict[str, Any] = (
        (data.get("NetworkSettings") or {}).get("Ports") or {}
    )
    ports = []
    for container_port, bindings in ports_data.items():
        if bindings:
            for b in bindings:
                ports.append({
                    "container_port": container_port,
                    "host_ip": b.get("HostIp"),
                    "host_port": b.get("HostPort"),
                })
        else:
            ports.append({"container_port": container_port, "host_port": None})

    return ContainerInfo(
        id=container.id,
        name=container.name,
        image=container.image,
        status=container.status,
        labels=container.labels,
        ports=ports,
        mounts=mounts,
        env_keys=env_keys,
        networks=networks,
    )


def parse_images(raw_images: str) -> list[dict[str, Any]]:
    rows = _load_json_lines(raw_images)
    return [
        {
            "id": str(r.get("ID") or r.get("Id") or "")[:12],
            "repository": r.get("Repository") or "",
            "tag": r.get("Tag") or "",
            "size": r.get("Size") or "",
            "created": r.get("CreatedAt") or r.get("Created") or "",
        }
        for r in rows
    ]


def parse_networks(raw_networks: str) -> list[dict[str, Any]]:
    rows = _load_json_lines(raw_networks)
    return [
        {
            "id": str(r.get("ID") or r.get("Id") or "")[:12],
            "name": r.get("Name") or "",
            "driver": r.get("Driver") or "",
            "scope": r.get("Scope") or "",
        }
        for r in rows
    ]


def parse_volumes(raw_volumes: str) -> list[dict[str, Any]]:
    rows = _load_json_lines(raw_volumes)
    return [
        {
            "name": r.get("Name") or "",
            "driver": r.get("Driver") or "",
            "mountpoint": r.get("Mountpoint") or "",
        }
        for r in rows
    ]


def parse_docker_info(raw_info: str) -> dict[str, Any]:
    data = _load_json_object(raw_info)
    return {
        "version": data.get("ServerVersion") or "",
        "storage_driver": data.get("Driver") or "",
        "containers_running": data.get("ContainersRunning") or 0,
        "containers_paused": data.get("ContainersPaused") or 0,
        "containers_stopped": data.get("ContainersStopped") or 0,
        "images": data.get("Images") or 0,
        "operating_system": data.get("OperatingSystem") or "",
        "architecture": data.get("Architecture") or "",
        "mem_total": data.get("MemTotal") or 0,
        "cpus": data.get("NCPU") or 0,
        "daemon_endpoint": data.get("HttpProxy") or "",
    }
