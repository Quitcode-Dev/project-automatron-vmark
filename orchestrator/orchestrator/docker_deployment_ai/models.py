"""Pydantic models for the Docker deployment intelligence layer.

These are the shared data structures used across inventory, planner,
validator, executor, and API layers. They are not ORM models — persistence
uses raw aiosqlite with JSON columns.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class DeploymentEnvironment(str, Enum):
    preview = "preview"
    staging = "staging"
    production = "production"


class PreferredStrategy(str, Enum):
    auto_detect = "auto_detect"
    docker_compose_private = "docker_compose_private"
    docker_compose_with_host_port = "docker_compose_with_host_port"
    existing_traefik = "existing_traefik"
    kamal_v1 = "kamal_v1"
    kamal_v2 = "kamal_v2"
    existing_nginx = "existing_nginx"
    existing_caddy = "existing_caddy"
    no_public_exposure = "no_public_exposure"


class DeploymentManager(str, Enum):
    none = "none"
    plain_docker = "plain_docker"
    docker_compose = "docker_compose"
    traefik = "traefik"
    kamal_v1 = "kamal_v1"
    kamal_v2 = "kamal_v2"
    nginx = "nginx"
    caddy = "caddy"
    mixed = "mixed"
    unknown = "unknown"


class ReverseProxy(str, Enum):
    none = "none"
    traefik = "traefik"
    kamal_proxy = "kamal_proxy"
    nginx = "nginx"
    caddy = "caddy"
    unknown = "unknown"


class DeploymentStrategy(str, Enum):
    docker_compose_private = "docker_compose_private"
    docker_compose_with_host_port = "docker_compose_with_host_port"
    reuse_existing_traefik = "reuse_existing_traefik"
    kamal_v1_compatible = "kamal_v1_compatible"
    kamal_v2_compatible = "kamal_v2_compatible"
    behind_existing_nginx = "behind_existing_nginx"
    behind_existing_caddy = "behind_existing_caddy"
    no_public_exposure = "no_public_exposure"
    manual_required = "manual_required"
    abort = "abort"


class RiskLevel(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"
    blocked = "blocked"


class HealthStatus(str, Enum):
    unknown = "unknown"
    starting = "starting"
    healthy = "healthy"
    unhealthy = "unhealthy"
    degraded = "degraded"
    failed = "failed"


# ---------------------------------------------------------------------------
# Deployment target
# ---------------------------------------------------------------------------


class DeploymentTargetCreate(BaseModel):
    project_id: str
    name: str
    host: str
    ssh_user: str
    ssh_port: int = 22
    environment: DeploymentEnvironment = DeploymentEnvironment.production
    domain: str | None = None
    app_name: str
    deploy_path: str
    preferred_strategy: PreferredStrategy = PreferredStrategy.auto_detect
    auth_mode: str = "ssh_key"
    auth_reference: str | None = None


class DeploymentTarget(DeploymentTargetCreate):
    id: str
    created_by: str | None = None
    created_at: str = Field(default_factory=_now_iso)
    updated_at: str = Field(default_factory=_now_iso)
    last_inventory_snapshot_id: str | None = None


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------


class ContainerInfo(BaseModel):
    id: str
    name: str
    image: str
    status: str
    labels: dict[str, str] = Field(default_factory=dict)
    ports: list[dict[str, Any]] = Field(default_factory=list)
    mounts: list[dict[str, Any]] = Field(default_factory=list)
    env_keys: list[str] = Field(default_factory=list)
    networks: list[str] = Field(default_factory=list)


class ListeningPort(BaseModel):
    port: int
    proto: str = "tcp"
    process: str | None = None
    address: str = "0.0.0.0"


class DetectionResult(BaseModel):
    deployment_manager: DeploymentManager = DeploymentManager.unknown
    reverse_proxy: ReverseProxy = ReverseProxy.unknown
    confidence: float = 0.0
    evidence: list[str] = Field(default_factory=list)


class InventorySnapshot(BaseModel):
    target_id: str
    project_id: str
    host_info: dict[str, Any] = Field(default_factory=dict)
    docker_info: dict[str, Any] = Field(default_factory=dict)
    containers: list[ContainerInfo] = Field(default_factory=list)
    images: list[dict[str, Any]] = Field(default_factory=list)
    networks: list[dict[str, Any]] = Field(default_factory=list)
    volumes: list[dict[str, Any]] = Field(default_factory=list)
    listening_ports: list[ListeningPort] = Field(default_factory=list)
    detection: DetectionResult = Field(default_factory=DetectionResult)
    filesystem_hints: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


# ---------------------------------------------------------------------------
# Deployment plan (strict schema — see plan_schema.py for JSON schema)
# ---------------------------------------------------------------------------


class DeploymentPlanSummary(BaseModel):
    id: str
    project_id: str
    target_id: str
    status: str
    strategy: str
    risk_level: RiskLevel
    summary: str | None = None
    created_at: str


# ---------------------------------------------------------------------------
# Approval
# ---------------------------------------------------------------------------


class DeploymentApproval(BaseModel):
    id: str
    plan_id: str
    approved_by: str
    approved_at: str
    approval_note: str | None = None


# ---------------------------------------------------------------------------
# Run / step
# ---------------------------------------------------------------------------


class DeploymentRunSummary(BaseModel):
    id: str
    plan_id: str
    project_id: str
    target_id: str
    status: str
    started_by: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    current_step: int | None = None
    health_status: HealthStatus = HealthStatus.unknown
    rollback_available: bool = False
