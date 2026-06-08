#!/usr/bin/env python3
"""A/B compare aider vs agent_sdk on a single GitHub issue.

Runs each engine against the same issue spec from a clean workspace, captures
time, token usage, estimated cost, and the resulting branch diff. Outputs a
JSON summary you can diff yourself.

Usage:
    python -m scripts.compare_builders \\
        --project-id <uuid> --issue-number 49 \\
        [--engines aider,agent_sdk] [--model anthropic/claude-sonnet-4-6]

Reads project/issue from the orchestrator's DB; writes nothing to it. The
agent_sdk branch is `agent-sdk/fix-N`, the aider branch is the usual
`aider/fix-N`. Both are pushed to the remote — diff them on GitHub.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

# Make orchestrator importable when running from the orchestrator/ dir
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.aider_agent import _run, implement_issue as aider_impl  # noqa: E402
from orchestrator.builder.agent_sdk import implement_issue_via_agent_sdk  # noqa: E402
from orchestrator.config import settings  # noqa: E402
from orchestrator.github.issues import GitHubClient  # noqa: E402
from orchestrator.models.project import get_project  # noqa: E402


async def _diff_summary(workspace: Path, branch: str, default_branch: str) -> dict[str, Any]:
    """Return a summary of what changed on `branch` vs `default_branch`."""
    repo_dir = next((workspace / sub for sub in ("repo", "repo-agent-sdk") if (workspace / sub / ".git").exists()), None)
    if repo_dir is None:
        return {"error": "no repo workspace found"}

    # Stat-only diff (file names + line counts)
    rc, out = await _run(
        ["git", "diff", "--stat", f"origin/{default_branch}...origin/{branch}"],
        cwd=repo_dir,
    )
    if rc != 0:
        return {"error": f"git diff failed: {out[-200:]}"}

    # Just names for a quick list
    rc, names_out = await _run(
        ["git", "diff", "--name-status", f"origin/{default_branch}...origin/{branch}"],
        cwd=repo_dir,
    )
    files = [line for line in names_out.splitlines() if line.strip()]

    return {
        "files_changed": len(files),
        "files": files[:50],
        "stat": out.strip().splitlines()[-1] if out.strip() else "(no changes)",
    }


async def _run_aider(project_id: str, owner: str, repo: str, issue_number: int,
                    issue_title: str, issue_body: str, default_branch: str,
                    model: str) -> dict[str, Any]:
    print(f"\n[aider] starting on issue #{issue_number}...", flush=True)
    t0 = time.monotonic()
    try:
        branch, failure = await aider_impl(
            project_id=project_id,
            owner=owner,
            repo=repo,
            issue_number=issue_number,
            issue_title=issue_title,
            issue_body=issue_body,
            default_branch=default_branch,
            model=model,
            is_reimplementation=False,
        )
    except Exception as exc:
        return {"engine": "aider", "error": f"{exc!r}", "elapsed_sec": time.monotonic() - t0}
    elapsed = time.monotonic() - t0

    diff = await _diff_summary(settings.workspace_base_dir / str(project_id), branch or "main", default_branch) if branch else {}
    return {
        "engine": "aider",
        "branch": branch,
        "failure": failure,
        "elapsed_sec": round(elapsed, 1),
        "diff": diff,
        "tokens_note": "Aider does not surface usage directly — check Anthropic console for cost.",
    }


async def _run_agent_sdk(project_id: str, owner: str, repo: str, issue_number: int,
                         issue_title: str, issue_body: str, default_branch: str,
                         model: str) -> dict[str, Any]:
    print(f"\n[agent_sdk] starting on issue #{issue_number}...", flush=True)
    t0 = time.monotonic()
    try:
        branch, failure, usage = await implement_issue_via_agent_sdk(
            project_id=project_id,
            owner=owner,
            repo=repo,
            issue_number=issue_number,
            issue_title=issue_title,
            issue_body=issue_body,
            default_branch=default_branch,
            model=model.replace("anthropic/", ""),  # raw model name for SDK
            is_reimplementation=False,
        )
    except Exception as exc:
        return {"engine": "agent_sdk", "error": f"{exc!r}", "elapsed_sec": time.monotonic() - t0}
    elapsed = time.monotonic() - t0

    diff = await _diff_summary(settings.workspace_base_dir / str(project_id), branch or "main", default_branch) if branch else {}
    return {
        "engine": "agent_sdk",
        "branch": branch,
        "failure": failure,
        "elapsed_sec": round(elapsed, 1),
        "diff": diff,
        "iterations": usage.iterations,
        "tool_calls": usage.tool_calls,
        "tool_call_breakdown": usage.tool_call_breakdown,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_creation_input_tokens": usage.cache_creation_input_tokens,
        "cache_read_input_tokens": usage.cache_read_input_tokens,
        "estimated_cost_usd": round(usage.estimated_cost_usd(model), 4),
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--issue-number", type=int, required=True)
    parser.add_argument("--engines", default="aider,agent_sdk",
                        help="Comma-separated: aider, agent_sdk")
    parser.add_argument("--model", default="anthropic/claude-sonnet-4-6")
    parser.add_argument("--out", help="Write summary JSON to this file (otherwise stdout)")
    args = parser.parse_args()

    project = await get_project(args.project_id)
    if not project:
        print(f"ERROR: project {args.project_id} not found", file=sys.stderr)
        sys.exit(1)

    owner = project.get("github_repo_owner") or ""
    repo = project.get("github_repo_name") or ""
    default_branch = project.get("default_branch") or "main"
    if not owner or not repo:
        print("ERROR: project has no github_repo_owner/name", file=sys.stderr)
        sys.exit(1)

    gh = GitHubClient()
    issue_data = await gh.get_issue(owner, repo, args.issue_number)
    if not issue_data:
        print(f"ERROR: issue #{args.issue_number} not found in {owner}/{repo}", file=sys.stderr)
        sys.exit(1)
    issue_title = issue_data.get("title", f"Issue #{args.issue_number}")
    issue_body = issue_data.get("body", "") or ""

    engines = [e.strip() for e in args.engines.split(",") if e.strip()]
    results: list[dict[str, Any]] = []

    for engine in engines:
        if engine == "aider":
            results.append(await _run_aider(
                args.project_id, owner, repo, args.issue_number,
                issue_title, issue_body, default_branch, args.model,
            ))
        elif engine == "agent_sdk":
            results.append(await _run_agent_sdk(
                args.project_id, owner, repo, args.issue_number,
                issue_title, issue_body, default_branch, args.model,
            ))
        else:
            print(f"WARN: unknown engine '{engine}', skipping", file=sys.stderr)

    summary = {
        "project_id": args.project_id,
        "issue_number": args.issue_number,
        "owner": owner,
        "repo": repo,
        "default_branch": default_branch,
        "model": args.model,
        "results": results,
    }
    serialized = json.dumps(summary, indent=2, default=str)
    if args.out:
        Path(args.out).write_text(serialized)
        print(f"\nSummary written to {args.out}")
    else:
        print("\n" + serialized)


if __name__ == "__main__":
    asyncio.run(main())
