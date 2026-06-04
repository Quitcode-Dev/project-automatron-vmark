"""JSON schema for workflow candidate validation."""

from __future__ import annotations

from typing import Any

import jsonschema

WORKFLOW_CANDIDATE_SCHEMA: dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "WorkflowCandidate",
    "type": "object",
    "required": ["provider", "ci_platform", "summary", "risk_level", "files"],
    "additionalProperties": False,
    "properties": {
        "provider": {"type": "string"},
        "ci_platform": {"type": "string"},
        "summary": {"type": "string"},
        "risk_level": {"type": "string", "enum": ["low", "medium", "high", "blocked"]},
        "files": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["path", "content"],
                "additionalProperties": False,
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
            },
        },
        "jobs": {"type": "array", "items": {"type": "string"}},
        "triggers": {"type": "array", "items": {"type": "string"}},
        "required_secrets": {"type": "array", "items": {"type": "string"}},
        "required_variables": {"type": "array", "items": {"type": "string"}},
        "deployment_assumptions": {"type": "array", "items": {"type": "string"}},
        "blocking_questions": {"type": "array", "items": {"type": "string"}},
        "warnings": {"type": "array", "items": {"type": "string"}},
    },
}


def validate_candidate_schema(data: dict[str, Any]) -> list[str]:
    """Validate a workflow candidate dict against the JSON schema.

    Returns a list of error message strings (empty = valid).
    """
    validator = jsonschema.Draft7Validator(WORKFLOW_CANDIDATE_SCHEMA)
    return [e.message for e in validator.iter_errors(data)]
