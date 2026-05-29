"""Docker-native Deployment Intelligence Layer.

Provides Gordon (docker ai) and Docker Agent (cagent) backed advisors for
Dockerfile/Compose review, build/runtime failure explanation, and deployment
strategy recommendation, wrapped in deterministic safety rails: read-only
remote inventory, strict plan validation, human approval, typed action
executor, and rollback metadata.

This module is intentionally a sibling of orchestrator.py — never merge its
logic into the GitHubOrchestrator god-module. Public entry point is
`DockerDeploymentOrchestrator` (added in a later scope); internal classes
stay internal.
"""

from __future__ import annotations

__all__: list[str] = []
