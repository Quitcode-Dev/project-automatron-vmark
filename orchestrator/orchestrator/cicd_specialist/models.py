"""Pydantic models for the CI/CD workflow specialist."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class RiskLevel(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"
    blocked = "blocked"


class WorkflowFile(BaseModel):
    path: str
    content: str


class WorkflowCandidate(BaseModel):
    provider: str = "litellm"
    ci_platform: str = "github_actions"
    summary: str = ""
    risk_level: RiskLevel = RiskLevel.low
    files: list[WorkflowFile] = Field(default_factory=list)
    jobs: list[str] = Field(default_factory=list)
    triggers: list[str] = Field(default_factory=list)
    required_secrets: list[str] = Field(default_factory=list)
    required_variables: list[str] = Field(default_factory=list)
    deployment_assumptions: list[str] = Field(default_factory=list)
    blocking_questions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class WorkflowValidationError(BaseModel):
    rule: str
    message: str
    file_path: str | None = None


class WorkflowValidationResult(BaseModel):
    passed: bool
    errors: list[WorkflowValidationError] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class RepoContext(BaseModel):
    repo_path: str
    project_types: list[str] = Field(default_factory=list)
    has_dockerfile: bool = False
    has_compose: bool = False
    has_package_json: bool = False
    package_scripts: dict[str, str] = Field(default_factory=dict)
    has_test_script: bool = False
    has_build_script: bool = False
    existing_workflow_paths: list[str] = Field(default_factory=list)
    existing_workflow_contents: dict[str, str] = Field(default_factory=dict)
    has_requirements_txt: bool = False
    has_pyproject_toml: bool = False
    env_var_names: list[str] = Field(default_factory=list)


class CICDSpecialistResult(BaseModel):
    project_id: str
    candidate: WorkflowCandidate
    validation: WorkflowValidationResult
    candidate_hash: str
    approved: bool = False
    pr_url: str | None = None
    branch: str | None = None
    created_at: str = Field(default_factory=_now_iso)
