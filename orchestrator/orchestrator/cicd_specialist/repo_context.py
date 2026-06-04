"""Repo context collector for CI/CD workflow generation."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from orchestrator.cicd_specialist.models import RepoContext

logger = logging.getLogger(__name__)


def collect_repo_context(repo_path: str) -> RepoContext:
    """Scan a local repository and return signals needed for workflow generation."""
    root = Path(repo_path)
    project_types: list[str] = []

    has_dockerfile = (root / "Dockerfile").exists() or bool(list(root.glob("Dockerfile.*")))
    has_compose = (
        (root / "docker-compose.yml").exists()
        or (root / "docker-compose.yaml").exists()
        or bool(list(root.glob("docker-compose.*.yml")))
        or bool(list(root.glob("docker-compose.*.yaml")))
    )
    has_package_json = (root / "package.json").exists()
    has_requirements_txt = (root / "requirements.txt").exists()
    has_pyproject_toml = (root / "pyproject.toml").exists()

    if has_dockerfile:
        project_types.append("docker")
    if has_compose:
        project_types.append("compose")
    if has_package_json:
        project_types.append("node")
    if has_requirements_txt or has_pyproject_toml:
        project_types.append("python")

    package_scripts: dict[str, str] = {}
    if has_package_json:
        try:
            pkg = json.loads((root / "package.json").read_text(encoding="utf-8"))
            package_scripts = pkg.get("scripts") or {}
        except Exception:
            logger.debug("Failed to parse package.json at %s", root)

    has_test_script = bool(package_scripts.get("test"))
    has_build_script = bool(package_scripts.get("build"))

    # Python projects have pytest available — treat as having a test command.
    if has_requirements_txt or has_pyproject_toml:
        has_test_script = True

    workflow_dir = root / ".github" / "workflows"
    existing_workflow_paths: list[str] = []
    existing_workflow_contents: dict[str, str] = {}
    if workflow_dir.exists():
        all_wf = sorted(workflow_dir.glob("*.yml")) + sorted(workflow_dir.glob("*.yaml"))
        for wf in all_wf:
            rel = wf.relative_to(root).as_posix()
            existing_workflow_paths.append(rel)
            try:
                existing_workflow_contents[rel] = wf.read_text(encoding="utf-8")
            except Exception:
                pass

    env_var_names: list[str] = []
    for candidate in [root / ".env.example", root / ".env"]:
        if candidate.exists():
            try:
                for line in candidate.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        env_var_names.append(line.split("=", 1)[0].strip())
            except Exception:
                pass
            break

    return RepoContext(
        repo_path=repo_path,
        project_types=project_types,
        has_dockerfile=has_dockerfile,
        has_compose=has_compose,
        has_package_json=has_package_json,
        package_scripts=package_scripts,
        has_test_script=has_test_script,
        has_build_script=has_build_script,
        existing_workflow_paths=existing_workflow_paths,
        existing_workflow_contents=existing_workflow_contents,
        has_requirements_txt=has_requirements_txt,
        has_pyproject_toml=has_pyproject_toml,
        env_var_names=env_var_names,
    )
