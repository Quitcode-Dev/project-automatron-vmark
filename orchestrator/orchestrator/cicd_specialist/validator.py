"""GitHub Actions workflow file safety validator.

Checks each file in a WorkflowCandidate against a deterministic rule set.
No LLM involved — all decisions are pattern-based.
"""

from __future__ import annotations

import re
from typing import Any

import yaml

from orchestrator.cicd_specialist.models import (
    WorkflowCandidate,
    WorkflowValidationError,
    WorkflowValidationResult,
)

# ---------------------------------------------------------------------------
# Compiled patterns
# ---------------------------------------------------------------------------

_PRIVATE_KEY = re.compile(
    r"-----BEGIN\s+(?:RSA\s+|EC\s+|DSA\s+|OPENSSH\s+)?PRIVATE KEY-----"
)
_CURL_PIPE_BASH = re.compile(
    r"curl\b[^|\n]*\|\s*(?:sudo\s+)?(?:ba)?sh\b", re.IGNORECASE
)
_WGET_PIPE_SH = re.compile(
    r"wget\b[^|\n]*\|\s*(?:sudo\s+)?(?:ba)?sh\b", re.IGNORECASE
)
_ECHO_SECRET = re.compile(r"echo\b[^\n]*\$\{\{\s*secrets\.", re.IGNORECASE)
_SECRET_REF = re.compile(r"\$\{\{\s*secrets\.(\w+)\s*\}\}")
# Matches lines like `password: somevalue` where value is not a template expression.
_PLAINTEXT_CRED = re.compile(
    r"(?m)(?:^|(?<=\s))(?:password|passwd|secret|api[_-]?key|access[_-]?key|token|credential)\s*:\s+"
    r"(?!\$\{\{)(?!secrets\.)(?!vars\.)(?!\"\")(?!'')([^\n\r\"'\$]{6,})",
    re.IGNORECASE,
)
_DEPLOY_JOB = re.compile(r"\b(?:deploy|release|publish|rollout|ship)\b", re.IGNORECASE)
_TEST_BUILD_JOB = re.compile(
    r"\b(?:test|tests|build|lint|check|verify|validate|ci)\b", re.IGNORECASE
)


# ---------------------------------------------------------------------------
# Individual check functions
# ---------------------------------------------------------------------------


def _err(rule: str, msg: str, path: str) -> WorkflowValidationError:
    return WorkflowValidationError(rule=rule, message=msg, file_path=path)


def _check_path(path: str) -> list[WorkflowValidationError]:
    if not path.startswith(".github/workflows/"):
        return [_err(
            "invalid_path",
            f"Workflow file must be under .github/workflows/, got: {path}",
            path,
        )]
    return []


def _check_yaml_valid(path: str, content: str) -> list[WorkflowValidationError]:
    try:
        yaml.safe_load(content)
        return []
    except yaml.YAMLError as exc:
        return [_err("invalid_yaml", f"YAML parse error: {exc}", path)]


def _check_private_keys(path: str, content: str) -> list[WorkflowValidationError]:
    if _PRIVATE_KEY.search(content):
        return [_err("private_key_detected", "Private key material detected in workflow file", path)]
    return []


def _check_plaintext_secrets(path: str, content: str) -> list[WorkflowValidationError]:
    errors: list[WorkflowValidationError] = []
    for match in _PLAINTEXT_CRED.finditer(content):
        value = match.group(1).strip()
        if not value:
            continue
        errors.append(_err(
            "plaintext_secret",
            f"Possible plaintext credential value: '{value[:20]}...'",
            path,
        ))
    return errors


def _check_echo_secrets(path: str, content: str) -> list[WorkflowValidationError]:
    if _ECHO_SECRET.search(content):
        return [_err("echo_secret", "Secret echoed to stdout — use masked env vars or avoid echo", path)]
    return []


def _check_unsafe_shell(path: str, content: str) -> list[WorkflowValidationError]:
    errors: list[WorkflowValidationError] = []
    if _CURL_PIPE_BASH.search(content):
        errors.append(_err("unsafe_shell", "curl | bash detected — never pipe curl to shell", path))
    if _WGET_PIPE_SH.search(content):
        errors.append(_err("unsafe_shell", "wget | sh detected — never pipe wget to shell", path))
    return errors


def _get_triggers(parsed: Any) -> list[str]:
    """Extract trigger names from a parsed workflow dict.

    PyYAML converts the 'on:' key to True (YAML boolean), so check both.
    """
    on = parsed.get("on") if isinstance(parsed, dict) else None
    if on is None and isinstance(parsed, dict):
        on = parsed.get(True)
    if isinstance(on, dict):
        return list(on.keys())
    if isinstance(on, list):
        return [str(t) for t in on]
    if isinstance(on, str):
        return [on]
    return []


def _check_pull_request_target_secrets(
    path: str, parsed: Any
) -> list[WorkflowValidationError]:
    triggers = _get_triggers(parsed)
    if "pull_request_target" not in triggers:
        return []
    jobs = parsed.get("jobs", {}) if isinstance(parsed, dict) else {}
    if not isinstance(jobs, dict):
        return []
    for job in jobs.values():
        if not isinstance(job, dict):
            continue
        for step in (job.get("steps") or []):
            if not isinstance(step, dict):
                continue
            env = step.get("env") or {}
            if isinstance(env, dict) and any(
                isinstance(v, str) and "${{ secrets." in v for v in env.values()
            ):
                return [_err(
                    "pull_request_target_secrets",
                    "pull_request_target trigger combined with secrets access is a security risk",
                    path,
                )]
    return []


def _check_production_deploy_without_environment(
    path: str, parsed: Any
) -> list[WorkflowValidationError]:
    if not isinstance(parsed, dict):
        return []
    jobs = parsed.get("jobs", {}) or {}
    errors: list[WorkflowValidationError] = []
    for job_name, job in (jobs.items() if isinstance(jobs, dict) else []):
        if not isinstance(job, dict):
            continue
        if _DEPLOY_JOB.search(str(job_name)) and not job.get("environment"):
            errors.append(_err(
                "deploy_without_environment_gate",
                f"Deploy job '{job_name}' requires an environment: gate for production safety",
                path,
            ))
    return errors


def _check_deploy_needs_test_build(
    path: str, parsed: Any
) -> list[WorkflowValidationError]:
    if not isinstance(parsed, dict):
        return []
    jobs = parsed.get("jobs", {}) or {}
    errors: list[WorkflowValidationError] = []
    for job_name, job in (jobs.items() if isinstance(jobs, dict) else []):
        if not isinstance(job, dict):
            continue
        if not _DEPLOY_JOB.search(str(job_name)):
            continue
        needs = job.get("needs") or []
        if isinstance(needs, str):
            needs = [needs]
        if not any(_TEST_BUILD_JOB.search(str(n)) for n in needs):
            errors.append(_err(
                "deploy_missing_needs",
                f"Deploy job '{job_name}' must declare needs: pointing to a test or build job",
                path,
            ))
    return errors


def _check_workflow_dispatch(path: str, parsed: Any) -> list[WorkflowValidationError]:
    triggers = _get_triggers(parsed)
    if "workflow_dispatch" not in triggers:
        return [_err(
            "missing_workflow_dispatch",
            "Workflow must include workflow_dispatch trigger to allow manual execution",
            path,
        )]
    return []


def _check_undeclared_secrets(
    path: str, content: str, declared: list[str]
) -> list[WorkflowValidationError]:
    found = set(_SECRET_REF.findall(content))
    undeclared = found - set(declared)
    if undeclared:
        return [_err(
            "undeclared_secrets",
            f"Secrets used but not listed in required_secrets: {', '.join(sorted(undeclared))}",
            path,
        )]
    return []


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_workflow_candidate(candidate: WorkflowCandidate) -> WorkflowValidationResult:
    """Run all safety checks on a WorkflowCandidate.

    Checks are applied per file. Returns immediately after collecting all errors
    (not fail-fast) so the caller sees the complete picture.
    """
    errors: list[WorkflowValidationError] = []
    warnings: list[str] = []

    for wf in candidate.files:
        path, content = wf.path, wf.content

        errors.extend(_check_path(path))

        yaml_errors = _check_yaml_valid(path, content)
        errors.extend(yaml_errors)
        if yaml_errors:
            # Can't run structural checks on unparseable YAML
            continue

        parsed = yaml.safe_load(content)

        errors.extend(_check_private_keys(path, content))
        errors.extend(_check_plaintext_secrets(path, content))
        errors.extend(_check_echo_secrets(path, content))
        errors.extend(_check_unsafe_shell(path, content))
        errors.extend(_check_pull_request_target_secrets(path, parsed))
        errors.extend(_check_production_deploy_without_environment(path, parsed))
        errors.extend(_check_deploy_needs_test_build(path, parsed))
        errors.extend(_check_workflow_dispatch(path, parsed))
        errors.extend(_check_undeclared_secrets(path, content, candidate.required_secrets))

    return WorkflowValidationResult(
        passed=len(errors) == 0,
        errors=errors,
        warnings=warnings,
    )
