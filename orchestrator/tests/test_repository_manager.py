"""Tests for repository manager helpers."""

from __future__ import annotations

from orchestrator.repository.manager import RepositoryManager


def test_create_feature_branch_name_slugifies_project_name():
    manager = RepositoryManager()

    branch = manager.create_feature_branch_name("Client Portal MVP", run_seq=3)

    assert branch == "feature/3-client-portal-mvp"


def test_ensure_deploy_supporting_docs_creates_expected_files(tmp_path):
    manager = RepositoryManager()
    manager.workspace_root = tmp_path

    manager.ensure_deploy_supporting_docs("project-1", "Client Portal MVP")
    workspace = manager.workspace_path("project-1")

    assert (workspace / ".env.example").read_text(encoding="utf-8") == "APP_PORT=3000\n"
    assert "Client Portal MVP" in (workspace / "DEPLOY.md").read_text(encoding="utf-8")
    assert (workspace / "deploy" / "docker-compose.yml").exists()
    assert (workspace / ".github" / "workflows" / "ci.yml").exists()
    assert (workspace / ".github" / "workflows" / "deploy.yml").exists()
