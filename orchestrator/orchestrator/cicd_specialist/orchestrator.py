"""CICDSpecialistOrchestrator — product-level coordinator.

Pipeline:
  repo context collection
  → CI/CD specialist prompt over LiteLLM
  → strict JSON schema validation
  → GitHub Actions deterministic validator
  → approval hash gate
  → commit / PR flow
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from orchestrator.cicd_specialist.approval import compute_candidate_hash, verify_approval
from orchestrator.cicd_specialist.commit_pr import WorkflowCommitError, commit_workflow_candidate
from orchestrator.cicd_specialist.events import (
    emit_approval_required,
    emit_approved,
    emit_commit_completed,
    emit_commit_failed,
    emit_commit_started,
    emit_context_completed,
    emit_context_started,
    emit_generation_completed,
    emit_generation_failed,
    emit_generation_started,
    emit_validation_blocked,
    emit_validation_completed,
    emit_validation_started,
)
from orchestrator.cicd_specialist.models import (
    CICDSpecialistResult,
    RepoContext,
    WorkflowCandidate,
)
from orchestrator.cicd_specialist.prompt import build_cicd_messages
from orchestrator.cicd_specialist.repo_context import collect_repo_context
from orchestrator.cicd_specialist.schema import validate_candidate_schema
from orchestrator.cicd_specialist.validator import validate_workflow_candidate
from orchestrator.config import settings
from orchestrator.llm.provider import call_llm

logger = logging.getLogger(__name__)


class CICDSpecialistOrchestrator:
    """Single entry-point for CI/CD workflow generation and commit operations."""

    async def generate(
        self,
        project_id: str,
        repo_path: str,
        *,
        model: str | None = None,
    ) -> CICDSpecialistResult:
        """Collect context, generate via LiteLLM, validate, return result ready for approval."""

        # 1. Repo context
        await emit_context_started(project_id)
        context = collect_repo_context(repo_path)
        await emit_context_completed(project_id, context.project_types)

        # 2. LiteLLM generation
        await emit_generation_started(project_id)
        try:
            candidate = await self._generate_candidate(context, model=model)
        except Exception as exc:
            await emit_generation_failed(project_id, str(exc))
            raise

        await emit_generation_completed(project_id, len(candidate.files), candidate.risk_level.value)

        # 3. Deterministic validation
        await emit_validation_started(project_id)
        validation = validate_workflow_candidate(candidate)

        if validation.passed:
            await emit_validation_completed(project_id, True, 0)
        else:
            error_msgs = [e.message for e in validation.errors]
            await emit_validation_blocked(project_id, error_msgs)
            await emit_validation_completed(project_id, False, len(validation.errors))

        # 4. Approval hash gate
        candidate_hash = compute_candidate_hash(candidate)
        await emit_approval_required(project_id, candidate_hash)

        return CICDSpecialistResult(
            project_id=project_id,
            candidate=candidate,
            validation=validation,
            candidate_hash=candidate_hash,
        )

    async def approve_and_commit(
        self,
        project_id: str,
        repo_name: str,
        result: CICDSpecialistResult,
        *,
        approved_by: str,
    ) -> CICDSpecialistResult:
        """Verify approval, commit files to a new branch, open a PR."""
        if not result.validation.passed:
            raise ValueError("Cannot commit a candidate that failed validation")

        if not verify_approval(result.candidate, result.candidate_hash):
            raise ValueError("Candidate hash mismatch — the candidate was mutated after generation")

        await emit_approved(project_id, result.candidate_hash, approved_by)
        await emit_commit_started(project_id, repo_name)

        try:
            commit_result = await commit_workflow_candidate(
                repo_name,
                result.candidate,
                result.candidate_hash,
                actor=approved_by,
            )
        except WorkflowCommitError as exc:
            await emit_commit_failed(project_id, str(exc))
            raise

        await emit_commit_completed(project_id, commit_result["branch"], commit_result["pr_url"])

        return result.model_copy(update={
            "approved": True,
            "pr_url": commit_result["pr_url"],
            "branch": commit_result["branch"],
        })

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _generate_candidate(
        self,
        context: RepoContext,
        *,
        model: str | None = None,
    ) -> WorkflowCandidate:
        messages = build_cicd_messages(context)
        raw = await call_llm(
            messages,
            model=model or settings.architect_model,
            temperature=0.2,
            max_tokens=8192,
        )

        json_str = _extract_json(raw)
        try:
            data: dict[str, Any] = json.loads(json_str)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"LLM returned invalid JSON: {exc}\n--- raw (first 500) ---\n{raw[:500]}"
            ) from exc

        schema_errors = validate_candidate_schema(data)
        if schema_errors:
            raise ValueError(f"Candidate schema errors: {'; '.join(schema_errors)}")

        return WorkflowCandidate.model_validate(data)


def _extract_json(text: str) -> str:
    """Strip markdown code fences and extract the outermost JSON object."""
    text = text.strip()
    # Handle ```json ... ``` or ``` ... ``` fences
    if text.startswith("```"):
        lines = text.splitlines()
        lines = lines[1:]  # drop opening fence line
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        return text[start : end + 1]
    return text
