"""Tests for the CI/CD Workflow Specialist.

Acceptance coverage:
  1.  Node app — project type detection, test/build script presence
  2.  Dockerfile app — project type detection
  3.  Compose app — project type detection
  4.  Existing workflow — picked up by context collector
  5.  Missing test script — has_test_script is False for node-only without test key
  6.  Production deploy requires environment gate — validator blocks missing environment:
  7.  Secrets never inlined — validator blocks plaintext credentials and private keys
  8.  Unsafe shell rejected — validator blocks curl|bash and wget|sh
  9.  Generated YAML parses — yaml.safe_load succeeds on valid content, fails blocked on bad YAML
  10. Deploy job depends on test/build — validator blocks missing needs:
  11. Approval hash invalidated after mutation — hash changes, verify_approval returns False
  12. Commit creates branch/PR not direct main push — commit_workflow_candidate returns branch + pr_url
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from orchestrator.cicd_specialist.approval import compute_candidate_hash, verify_approval
from orchestrator.cicd_specialist.models import (
    RiskLevel,
    WorkflowCandidate,
    WorkflowFile,
)
from orchestrator.cicd_specialist.repo_context import collect_repo_context
from orchestrator.cicd_specialist.schema import validate_candidate_schema
from orchestrator.cicd_specialist.validator import validate_workflow_candidate


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _candidate(*files: tuple[str, str], **kw: Any) -> WorkflowCandidate:
    return WorkflowCandidate(
        files=[WorkflowFile(path=p, content=c) for p, c in files],
        required_secrets=kw.get("required_secrets", []),
    )


VALID_CI = """\
name: CI
on:
  push:
    branches: [main]
  workflow_dispatch:
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: npm test
"""

VALID_DEPLOY = """\
name: Deploy
on:
  push:
    branches: [main]
  workflow_dispatch:
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: npm test
  deploy:
    needs: [test]
    runs-on: ubuntu-latest
    environment: production
    steps:
      - uses: actions/checkout@v4
      - name: Deploy
        env:
          DEPLOY_KEY: ${{ secrets.DEPLOY_KEY }}
        run: ./deploy.sh
"""


# ---------------------------------------------------------------------------
# 1. Node app context
# ---------------------------------------------------------------------------

def test_node_app_context():
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "package.json").write_text(json.dumps({
            "scripts": {"test": "vitest run", "build": "vite build", "lint": "eslint ."},
        }))
        ctx = collect_repo_context(tmp)

    assert "node" in ctx.project_types
    assert ctx.has_package_json is True
    assert ctx.has_test_script is True
    assert ctx.has_build_script is True
    assert ctx.package_scripts["test"] == "vitest run"


# ---------------------------------------------------------------------------
# 2. Dockerfile app context
# ---------------------------------------------------------------------------

def test_dockerfile_app_context():
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "Dockerfile").write_text("FROM node:22\n")
        ctx = collect_repo_context(tmp)

    assert "docker" in ctx.project_types
    assert ctx.has_dockerfile is True


# ---------------------------------------------------------------------------
# 3. Compose app context
# ---------------------------------------------------------------------------

def test_compose_app_context():
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "docker-compose.yml").write_text(
            "version: '3'\nservices:\n  app:\n    image: node:22\n"
        )
        ctx = collect_repo_context(tmp)

    assert "compose" in ctx.project_types
    assert ctx.has_compose is True


# ---------------------------------------------------------------------------
# 4. Existing workflow files detected
# ---------------------------------------------------------------------------

def test_existing_workflow_detected():
    with tempfile.TemporaryDirectory() as tmp:
        wf_dir = Path(tmp) / ".github" / "workflows"
        wf_dir.mkdir(parents=True)
        (wf_dir / "ci.yml").write_text(VALID_CI)
        ctx = collect_repo_context(tmp)

    assert ".github/workflows/ci.yml" in ctx.existing_workflow_paths
    assert ".github/workflows/ci.yml" in ctx.existing_workflow_contents
    assert "name: CI" in ctx.existing_workflow_contents[".github/workflows/ci.yml"]


# ---------------------------------------------------------------------------
# 5. Missing test script
# ---------------------------------------------------------------------------

def test_missing_test_script_for_node_only():
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "package.json").write_text(json.dumps({
            "scripts": {"build": "webpack"},
        }))
        ctx = collect_repo_context(tmp)

    assert ctx.has_package_json is True
    assert ctx.has_test_script is False
    assert ctx.has_build_script is True


# ---------------------------------------------------------------------------
# 6. Production deploy requires environment gate
# ---------------------------------------------------------------------------

def test_deploy_without_environment_blocked():
    deploy_no_env = """\
name: Deploy
on:
  workflow_dispatch:
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: npm test
  deploy:
    needs: [test]
    runs-on: ubuntu-latest
    steps:
      - run: ./deploy.sh
"""
    result = validate_workflow_candidate(
        _candidate((".github/workflows/deploy.yml", deploy_no_env))
    )
    assert result.passed is False
    assert any(e.rule == "deploy_without_environment_gate" for e in result.errors)


def test_deploy_with_environment_gate_passes():
    result = validate_workflow_candidate(
        _candidate(
            (".github/workflows/deploy.yml", VALID_DEPLOY),
            required_secrets=["DEPLOY_KEY"],
        )
    )
    env_gate_errors = [e for e in result.errors if e.rule == "deploy_without_environment_gate"]
    assert not env_gate_errors


# ---------------------------------------------------------------------------
# 7a. Plaintext secrets blocked
# ---------------------------------------------------------------------------

def test_plaintext_secret_blocked():
    workflow = """\
name: CI
on:
  workflow_dispatch:
jobs:
  test:
    runs-on: ubuntu-latest
    env:
      password: supersecretvalue
    steps:
      - run: npm test
"""
    result = validate_workflow_candidate(
        _candidate((".github/workflows/ci.yml", workflow))
    )
    assert result.passed is False
    assert any(e.rule == "plaintext_secret" for e in result.errors)


# ---------------------------------------------------------------------------
# 7b. Private key blocked
# ---------------------------------------------------------------------------

def test_private_key_blocked():
    # Embed RSA key header directly in a run step
    workflow = VALID_CI + "      - run: echo '-----BEGIN RSA PRIVATE KEY-----'\n"
    result = validate_workflow_candidate(
        _candidate((".github/workflows/ci.yml", workflow))
    )
    assert result.passed is False
    assert any(e.rule == "private_key_detected" for e in result.errors)


# ---------------------------------------------------------------------------
# 8a. curl | bash rejected
# ---------------------------------------------------------------------------

def test_curl_pipe_bash_blocked():
    workflow = """\
name: CI
on:
  workflow_dispatch:
jobs:
  install:
    runs-on: ubuntu-latest
    steps:
      - run: curl -sL https://example.com/install.sh | bash
"""
    result = validate_workflow_candidate(
        _candidate((".github/workflows/ci.yml", workflow))
    )
    assert result.passed is False
    assert any(e.rule == "unsafe_shell" for e in result.errors)


# ---------------------------------------------------------------------------
# 8b. wget | sh rejected
# ---------------------------------------------------------------------------

def test_wget_pipe_sh_blocked():
    workflow = """\
name: CI
on:
  workflow_dispatch:
jobs:
  install:
    runs-on: ubuntu-latest
    steps:
      - run: wget -qO- https://example.com/install.sh | sh
"""
    result = validate_workflow_candidate(
        _candidate((".github/workflows/ci.yml", workflow))
    )
    assert result.passed is False
    assert any(e.rule == "unsafe_shell" for e in result.errors)


# ---------------------------------------------------------------------------
# 9a. Valid YAML parses
# ---------------------------------------------------------------------------

def test_valid_yaml_not_blocked_as_invalid():
    yaml.safe_load(VALID_CI)   # must not raise
    yaml.safe_load(VALID_DEPLOY)

    result = validate_workflow_candidate(
        _candidate(
            (".github/workflows/ci.yml", VALID_CI),
            (".github/workflows/deploy.yml", VALID_DEPLOY),
            required_secrets=["DEPLOY_KEY"],
        )
    )
    yaml_errors = [e for e in result.errors if e.rule == "invalid_yaml"]
    assert not yaml_errors


# ---------------------------------------------------------------------------
# 9b. Invalid YAML is blocked
# ---------------------------------------------------------------------------

def test_invalid_yaml_blocked():
    bad = "name: CI\non: [\nbroken"
    result = validate_workflow_candidate(
        _candidate((".github/workflows/ci.yml", bad))
    )
    assert result.passed is False
    assert any(e.rule == "invalid_yaml" for e in result.errors)


# ---------------------------------------------------------------------------
# 10a. Deploy job without needs: blocked
# ---------------------------------------------------------------------------

def test_deploy_without_needs_blocked():
    workflow = """\
name: Deploy
on:
  workflow_dispatch:
jobs:
  deploy:
    runs-on: ubuntu-latest
    environment: production
    steps:
      - run: ./deploy.sh
"""
    result = validate_workflow_candidate(
        _candidate((".github/workflows/deploy.yml", workflow))
    )
    assert result.passed is False
    assert any(e.rule == "deploy_missing_needs" for e in result.errors)


# ---------------------------------------------------------------------------
# 10b. Deploy with needs: test passes
# ---------------------------------------------------------------------------

def test_deploy_with_needs_test_passes():
    result = validate_workflow_candidate(
        _candidate(
            (".github/workflows/deploy.yml", VALID_DEPLOY),
            required_secrets=["DEPLOY_KEY"],
        )
    )
    needs_errors = [e for e in result.errors if e.rule == "deploy_missing_needs"]
    assert not needs_errors


# ---------------------------------------------------------------------------
# 11. Approval hash invalidated after mutation
# ---------------------------------------------------------------------------

def test_approval_hash_is_stable():
    c = _candidate((".github/workflows/ci.yml", VALID_CI))
    assert compute_candidate_hash(c) == compute_candidate_hash(c)


def test_approval_hash_changes_on_content_mutation():
    original = _candidate((".github/workflows/ci.yml", VALID_CI))
    mutated = _candidate((".github/workflows/ci.yml", VALID_CI + "\n# mutated"))

    h_orig = compute_candidate_hash(original)
    h_mut = compute_candidate_hash(mutated)

    assert h_orig != h_mut
    assert verify_approval(original, h_orig) is True
    assert verify_approval(mutated, h_orig) is False


def test_approval_hash_changes_on_path_mutation():
    original = _candidate((".github/workflows/ci.yml", VALID_CI))
    mutated = _candidate((".github/workflows/ci-changed.yml", VALID_CI))

    assert compute_candidate_hash(original) != compute_candidate_hash(mutated)


def test_approval_hash_stable_regardless_of_file_order():
    c1 = WorkflowCandidate(
        files=[
            WorkflowFile(path=".github/workflows/ci.yml", content=VALID_CI),
            WorkflowFile(path=".github/workflows/deploy.yml", content=VALID_DEPLOY),
        ]
    )
    c2 = WorkflowCandidate(
        files=[
            WorkflowFile(path=".github/workflows/deploy.yml", content=VALID_DEPLOY),
            WorkflowFile(path=".github/workflows/ci.yml", content=VALID_CI),
        ]
    )
    assert compute_candidate_hash(c1) == compute_candidate_hash(c2)


# ---------------------------------------------------------------------------
# 12. Commit creates branch/PR, not direct push to main
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_commit_creates_branch_and_pr_not_direct_push():
    from orchestrator.cicd_specialist.commit_pr import commit_workflow_candidate

    candidate = _candidate(
        (".github/workflows/ci.yml", VALID_CI),
        required_secrets=[],
    )
    approved_hash = compute_candidate_hash(candidate)

    # Build ordered mock responses matching the exact API call sequence:
    # GET refs/heads/main, POST git/refs (branch), POST git/blobs (×n files),
    # GET git/commits/{sha}, POST git/trees, POST git/commits,
    # PATCH git/refs/heads/{branch}, POST pulls
    def _resp(data: Any) -> MagicMock:
        r = MagicMock()
        r.raise_for_status = MagicMock()
        r.json.return_value = data
        r.content = b"{}"
        return r

    get_queue = [
        _resp({"object": {"sha": "basesha123"}}),         # GET refs/heads/main
        _resp({"tree": {"sha": "basetreesha"}}),           # GET git/commits/basesha123
    ]
    post_queue = [
        _resp({"ref": "refs/heads/automatron/cicd-x"}),   # POST git/refs (create branch)
        _resp({"sha": "blobsha1"}),                        # POST git/blobs
        _resp({"sha": "newtreesha"}),                      # POST git/trees
        _resp({"sha": "newcommitsha"}),                    # POST git/commits
        _resp({"html_url": "https://github.com/owner/repo/pull/99", "number": 99}),  # POST pulls
    ]
    patch_queue = [_resp({})]                              # PATCH git/refs (update branch)

    get_iter = iter(get_queue)
    post_iter = iter(post_queue)
    patch_iter = iter(patch_queue)

    async def _get(url: str, **_: Any) -> Any:
        return next(get_iter)

    async def _post(url: str, **_: Any) -> Any:
        return next(post_iter)

    async def _patch(url: str, **_: Any) -> Any:
        return next(patch_iter)

    mock_client = MagicMock()
    mock_client.get = _get
    mock_client.post = _post
    mock_client.patch = _patch

    with (
        patch("orchestrator.cicd_specialist.commit_pr.settings") as mock_cfg,
        patch("orchestrator.cicd_specialist.commit_pr.httpx.AsyncClient") as mock_cls,
    ):
        mock_cfg.github_token = "fake-token"
        mock_cfg.github_owner = "owner"
        mock_cfg.github_api_url = "https://api.github.com"
        mock_cfg.git_author_name = "Bot"
        mock_cfg.git_author_email = "bot@example.local"

        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)

        result = await commit_workflow_candidate("my-repo", candidate, approved_hash)

    # Branch is automatron/cicd-... (not main)
    assert result["branch"].startswith("automatron/cicd-")
    assert result["branch"] != "main"
    # A PR was opened
    assert "pr_url" in result
    assert "pull" in result["pr_url"]
    assert result["pr_number"] == 99


@pytest.mark.asyncio
async def test_commit_blocked_on_mutated_candidate():
    from orchestrator.cicd_specialist.commit_pr import WorkflowCommitError, commit_workflow_candidate

    candidate = _candidate((".github/workflows/ci.yml", VALID_CI))
    approved_hash = compute_candidate_hash(candidate)

    mutated = _candidate((".github/workflows/ci.yml", VALID_CI + "\n# tampered"))

    with pytest.raises(WorkflowCommitError, match="hash mismatch"):
        await commit_workflow_candidate("my-repo", mutated, approved_hash)


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------

def test_schema_validates_complete_candidate():
    data = {
        "provider": "litellm",
        "ci_platform": "github_actions",
        "summary": "CI pipeline for Node app",
        "risk_level": "low",
        "files": [{"path": ".github/workflows/ci.yml", "content": "name: CI"}],
        "jobs": ["test"],
        "triggers": ["push", "workflow_dispatch"],
        "required_secrets": [],
        "required_variables": [],
        "deployment_assumptions": [],
        "blocking_questions": [],
        "warnings": [],
    }
    assert validate_candidate_schema(data) == []


def test_schema_rejects_missing_required_fields():
    errors = validate_candidate_schema({"provider": "litellm"})
    assert len(errors) > 0


def test_schema_rejects_invalid_risk_level():
    data = {
        "provider": "litellm",
        "ci_platform": "github_actions",
        "summary": "Test",
        "risk_level": "catastrophic",
        "files": [],
    }
    assert len(validate_candidate_schema(data)) > 0


# ---------------------------------------------------------------------------
# pull_request_target with secrets
# ---------------------------------------------------------------------------

def test_pull_request_target_with_secrets_blocked():
    workflow = """\
name: PR Check
on:
  pull_request_target:
jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - name: Verify
        env:
          TOKEN: ${{ secrets.SOME_TOKEN }}
        run: ./check.sh
"""
    result = validate_workflow_candidate(
        _candidate(
            (".github/workflows/pr.yml", workflow),
            required_secrets=["SOME_TOKEN"],
        )
    )
    assert result.passed is False
    assert any(e.rule == "pull_request_target_secrets" for e in result.errors)


# ---------------------------------------------------------------------------
# workflow_dispatch required
# ---------------------------------------------------------------------------

def test_missing_workflow_dispatch_blocked():
    workflow = """\
name: CI
on:
  push:
    branches: [main]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: npm test
"""
    result = validate_workflow_candidate(
        _candidate((".github/workflows/ci.yml", workflow))
    )
    assert result.passed is False
    assert any(e.rule == "missing_workflow_dispatch" for e in result.errors)


# ---------------------------------------------------------------------------
# File path outside .github/workflows/
# ---------------------------------------------------------------------------

def test_invalid_workflow_path_blocked():
    result = validate_workflow_candidate(
        _candidate(("workflows/ci.yml", VALID_CI))
    )
    assert result.passed is False
    assert any(e.rule == "invalid_path" for e in result.errors)


# ---------------------------------------------------------------------------
# Undeclared secrets
# ---------------------------------------------------------------------------

def test_undeclared_secrets_blocked():
    workflow = """\
name: Deploy
on:
  workflow_dispatch:
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: echo test
  deploy:
    needs: [test]
    runs-on: ubuntu-latest
    environment: production
    steps:
      - env:
          API_KEY: ${{ secrets.API_KEY }}
          DB_PASS: ${{ secrets.DB_PASS }}
        run: ./deploy.sh
"""
    result = validate_workflow_candidate(
        _candidate(
            (".github/workflows/deploy.yml", workflow),
            required_secrets=["API_KEY"],   # DB_PASS not declared
        )
    )
    assert result.passed is False
    undeclared_errors = [e for e in result.errors if e.rule == "undeclared_secrets"]
    assert undeclared_errors
    assert "DB_PASS" in undeclared_errors[0].message


# ---------------------------------------------------------------------------
# Echo secret blocked
# ---------------------------------------------------------------------------

def test_echo_secret_blocked():
    workflow = """\
name: CI
on:
  workflow_dispatch:
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: echo ${{ secrets.MY_SECRET }}
"""
    result = validate_workflow_candidate(
        _candidate(
            (".github/workflows/ci.yml", workflow),
            required_secrets=["MY_SECRET"],
        )
    )
    assert result.passed is False
    assert any(e.rule == "echo_secret" for e in result.errors)
