"""Strict JSON schema for deployment_plan.json.

Used by `planner.py` to validate generated plans before persistence, and by
`validator.py` to confirm an incoming plan is well-formed before running
deterministic checks. Validation uses jsonschema Draft7.

If required data is missing the planner returns the sentinel:
  { "strategy": "manual_required", "risk_level": "blocked", "blocking_questions": [...] }
"""

from __future__ import annotations

from typing import Any

import jsonschema

DEPLOYMENT_PLAN_SCHEMA: dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "DeploymentPlan",
    "type": "object",
    "required": ["strategy", "risk_level", "summary", "docker_ai", "detected_server_state"],
    "additionalProperties": True,
    "properties": {
        "strategy": {
            "type": "string",
            "enum": [
                "docker_compose_private",
                "docker_compose_with_host_port",
                "reuse_existing_traefik",
                "kamal_v1_compatible",
                "kamal_v2_compatible",
                "behind_existing_nginx",
                "behind_existing_caddy",
                "no_public_exposure",
                "manual_required",
                "abort",
            ],
        },
        "risk_level": {
            "type": "string",
            "enum": ["low", "medium", "high", "blocked"],
        },
        "summary": {"type": "string"},
        "docker_ai": {
            "type": "object",
            "required": ["provider"],
            "properties": {
                "provider": {"type": "string"},
                "analysis_id": {"type": ["string", "null"]},
                "reasoning_summary": {"type": "string"},
                "warnings": {"type": "array", "items": {"type": "string"}},
            },
        },
        "detected_server_state": {
            "type": "object",
            "required": ["deployment_manager", "reverse_proxy"],
            "properties": {
                "deployment_manager": {
                    "type": "string",
                    "enum": [
                        "none", "plain_docker", "docker_compose", "traefik",
                        "kamal_v1", "kamal_v2", "nginx", "caddy", "mixed", "unknown",
                    ],
                },
                "reverse_proxy": {
                    "type": "string",
                    "enum": ["none", "traefik", "kamal_proxy", "nginx", "caddy", "unknown"],
                },
                "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                "evidence": {"type": "array", "items": {"type": "string"}},
            },
        },
        "required_changes": {"type": "array"},
        "generated_files": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["path", "purpose", "content"],
                "properties": {
                    "path": {"type": "string"},
                    "purpose": {"type": "string"},
                    "content": {"type": "string"},
                },
            },
        },
        "port_plan": {
            "type": "object",
            "properties": {
                "internal_app_port": {"type": ["integer", "null"]},
                "host_port": {"type": ["integer", "null"]},
                "uses_reverse_proxy": {"type": "boolean"},
                "reverse_proxy_type": {"type": "string"},
            },
        },
        "routing_plan": {
            "type": "object",
            "properties": {
                "domain": {"type": "string"},
                "router_name": {"type": "string"},
                "service_name": {"type": "string"},
                "network": {"type": "string"},
                "tls": {"type": "boolean"},
            },
        },
        "volumes": {"type": "array"},
        "networks": {"type": "array"},
        "secrets_required": {"type": "array", "items": {"type": "string"}},
        "preflight_checks": {"type": "array"},
        "deployment_actions": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["action_type"],
                "properties": {
                    "action_type": {"type": "string"},
                    "params": {"type": "object"},
                },
            },
        },
        "healthchecks": {"type": "array"},
        "rollback_plan": {
            "type": "object",
            "properties": {
                "type": {"type": "string"},
                "previous_release_ref_required": {"type": "boolean"},
                "steps": {"type": "array"},
            },
        },
        "blocking_questions": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
}


BLOCKED_PLAN: dict[str, Any] = {
    "strategy": "manual_required",
    "risk_level": "blocked",
    "summary": "Insufficient data to generate a safe deployment plan.",
    "docker_ai": {"provider": "none", "analysis_id": None, "reasoning_summary": "", "warnings": []},
    "detected_server_state": {
        "deployment_manager": "unknown",
        "reverse_proxy": "unknown",
        "confidence": 0.0,
        "evidence": [],
    },
    "required_changes": [],
    "generated_files": [],
    "port_plan": {"internal_app_port": None, "host_port": None, "uses_reverse_proxy": False, "reverse_proxy_type": ""},
    "routing_plan": {"domain": "", "router_name": "", "service_name": "", "network": "", "tls": False},
    "volumes": [],
    "networks": [],
    "secrets_required": [],
    "preflight_checks": [],
    "deployment_actions": [],
    "healthchecks": [],
    "rollback_plan": {"type": "none", "previous_release_ref_required": True, "steps": []},
    "blocking_questions": [],
}


def validate_plan(plan: dict[str, Any]) -> list[str]:
    """Return a list of JSON schema validation errors (empty = valid)."""
    validator = jsonschema.Draft7Validator(DEPLOYMENT_PLAN_SCHEMA)
    return [str(e.message) for e in validator.iter_errors(plan)]
