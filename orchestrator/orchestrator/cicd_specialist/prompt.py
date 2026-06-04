"""CI/CD specialist prompt builder for LiteLLM."""

from __future__ import annotations

import json

from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage

from orchestrator.cicd_specialist.models import RepoContext

_SYSTEM = """\
You are a CI/CD workflow specialist. Generate safe, minimal GitHub Actions workflow files for the provided repository.

MANDATORY RULES (violations will be rejected automatically):
1. Output ONLY valid JSON — no markdown fences, no text outside the JSON object.
2. Never inline secrets. All secrets must use ${{ secrets.SECRET_NAME }} syntax.
3. Include workflow_dispatch trigger in every workflow file.
4. Any job named deploy/release/publish/rollout/ship MUST have environment: set.
5. Any deploy/release/publish job MUST declare needs: pointing to a test or build job.
6. Never use curl | bash or wget | sh patterns.
7. Never echo ${{ secrets.* }} values.
8. All files MUST be under .github/workflows/.
9. List every ${{ secrets.X }} name used in required_secrets.
10. If the project has no test script, add a warning and include a placeholder test step.
11. Add unclear assumptions to blocking_questions rather than guessing.

OUTPUT FORMAT (strict JSON, no extra keys):
{
  "provider": "litellm",
  "ci_platform": "github_actions",
  "summary": "<1-2 sentence description>",
  "risk_level": "low|medium|high|blocked",
  "files": [{"path": ".github/workflows/ci.yml", "content": "<yaml as escaped string>"}],
  "jobs": ["<job names>"],
  "triggers": ["<trigger names>"],
  "required_secrets": ["<every secret name used>"],
  "required_variables": ["<every var name used>"],
  "deployment_assumptions": ["<any assumption made>"],
  "blocking_questions": ["<question that blocks safe generation>"],
  "warnings": ["<non-blocking warning>"]
}"""


def build_cicd_messages(context: RepoContext) -> list[AnyMessage]:
    """Build LiteLLM messages for the CI/CD specialist."""
    lines = [
        f"Repository: {context.repo_path}",
        f"Project types: {', '.join(context.project_types) or 'unknown'}",
        f"Dockerfile: {context.has_dockerfile}",
        f"docker-compose: {context.has_compose}",
        f"package.json: {context.has_package_json}",
        f"requirements.txt: {context.has_requirements_txt}",
        f"pyproject.toml: {context.has_pyproject_toml}",
        f"Has test script: {context.has_test_script}",
        f"Has build script: {context.has_build_script}",
    ]

    if context.package_scripts:
        lines.append(f"package.json scripts:\n{json.dumps(context.package_scripts, indent=2)}")

    if context.env_var_names:
        lines.append(f"Env var names (from .env.example): {', '.join(context.env_var_names)}")

    if context.existing_workflow_paths:
        lines.append(f"\nExisting workflows: {', '.join(context.existing_workflow_paths)}")
        for wf_path, wf_content in context.existing_workflow_contents.items():
            lines.append(f"\n{wf_path}:\n```yaml\n{wf_content}\n```")

    human = (
        "Generate GitHub Actions workflow files for this repository.\n\n"
        "Repository context:\n" + "\n".join(lines)
    )
    return [SystemMessage(content=_SYSTEM), HumanMessage(content=human)]
