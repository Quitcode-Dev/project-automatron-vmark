"""Anthropic Agent SDK builder — experimental alternative to Aider.

Drop-in replacement for `aider_agent.implement_issue`. Same input/output
signature so it can be swapped in via `implement_with_aider(builder_engine=...)`.

Why try this:
- Aider's whole/diff edit formats produce parser failures (bogus root files,
  missed deletions, route collisions) that we keep writing sanitisers for.
- Aider's prompt caching is all-or-nothing per CLI invocation; the Agent SDK
  lets us cache per tool-call so context survives across architect→build→retry.
- Tool-use forces the model to declare a target path before writing; no
  silent ".gitignore housekeeping" when it doesn't know what to do.

Tool surface (intentionally small — every tool added is a failure mode added):
  read_file, write_file, edit_file, list_dir, glob_files, grep_files,
  delete_file, run_build, done.

Token tracking returns (input, output, cache_creation, cache_read) at the end
so the comparison harness can compute real $/run.
"""
from __future__ import annotations

import asyncio
import fnmatch
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orchestrator.aider_agent import _run  # reuse shell helper
from orchestrator.build_check import run_build_in_docker
from orchestrator.config import settings
from orchestrator.logsafe import redact

logger = logging.getLogger(__name__)


# ── Token usage tracking ─────────────────────────────────────────────────────

@dataclass
class UsageTotals:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    tool_calls: int = 0
    iterations: int = 0
    tool_call_breakdown: dict[str, int] = field(default_factory=dict)

    def merge_response(self, usage: Any) -> None:
        self.input_tokens += getattr(usage, "input_tokens", 0) or 0
        self.output_tokens += getattr(usage, "output_tokens", 0) or 0
        self.cache_creation_input_tokens += getattr(usage, "cache_creation_input_tokens", 0) or 0
        self.cache_read_input_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0

    def estimated_cost_usd(self, model: str) -> float:
        # Approximate Anthropic pricing per million tokens (as of late 2026).
        # Sonnet 4.x and Opus 4.x ratios; override if you ship at a different rate.
        if "opus" in model.lower():
            rate_in, rate_out, rate_cache_w, rate_cache_r = 15.0, 75.0, 18.75, 1.5
        else:  # default to sonnet pricing
            rate_in, rate_out, rate_cache_w, rate_cache_r = 3.0, 15.0, 3.75, 0.30
        return (
            (self.input_tokens * rate_in)
            + (self.output_tokens * rate_out)
            + (self.cache_creation_input_tokens * rate_cache_w)
            + (self.cache_read_input_tokens * rate_cache_r)
        ) / 1_000_000


# ── Tool implementations ─────────────────────────────────────────────────────

_READ_BUDGET = 80_000  # max chars per read — protect the context window


def _safe_path(repo_dir: Path, rel: str) -> Path | None:
    """Resolve a relative path inside repo_dir; reject escapes."""
    try:
        target = (repo_dir / rel).resolve()
        if not str(target).startswith(str(repo_dir.resolve())):
            return None
        return target
    except Exception:
        return None


def _tool_read_file(repo_dir: Path, args: dict) -> str:
    p = _safe_path(repo_dir, args["path"])
    if p is None:
        return f"ERROR: path '{args['path']}' is outside the repo"
    if not p.exists():
        return f"ERROR: file '{args['path']}' does not exist"
    if not p.is_file():
        return f"ERROR: '{args['path']}' is not a regular file"
    try:
        text = p.read_text(errors="replace")
    except Exception as exc:
        return f"ERROR reading file: {exc!r}"
    if len(text) > _READ_BUDGET:
        return text[:_READ_BUDGET] + f"\n[TRUNCATED — file is {len(text)} chars, only first {_READ_BUDGET} returned]"
    return text


def _tool_write_file(repo_dir: Path, args: dict) -> str:
    p = _safe_path(repo_dir, args["path"])
    if p is None:
        return f"ERROR: path '{args['path']}' is outside the repo"
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        p.write_text(args["content"])
    except Exception as exc:
        return f"ERROR writing file: {exc!r}"
    return f"OK: wrote {len(args['content'])} chars to {args['path']}"


def _tool_edit_file(repo_dir: Path, args: dict) -> str:
    p = _safe_path(repo_dir, args["path"])
    if p is None:
        return f"ERROR: path '{args['path']}' is outside the repo"
    if not p.exists():
        return f"ERROR: file '{args['path']}' does not exist — use write_file to create"
    try:
        original = p.read_text(errors="replace")
    except Exception as exc:
        return f"ERROR reading file: {exc!r}"
    old = args["old_string"]
    new = args["new_string"]
    if args.get("replace_all"):
        if old not in original:
            return f"ERROR: old_string not found in {args['path']}"
        edited = original.replace(old, new)
    else:
        occurrences = original.count(old)
        if occurrences == 0:
            return f"ERROR: old_string not found in {args['path']}"
        if occurrences > 1:
            return (
                f"ERROR: old_string occurs {occurrences} times in {args['path']}. "
                f"Add more context to make it unique, or pass replace_all=true."
            )
        edited = original.replace(old, new, 1)
    p.write_text(edited)
    return f"OK: edited {args['path']} ({len(original)} → {len(edited)} chars)"


def _tool_list_dir(repo_dir: Path, args: dict) -> str:
    p = _safe_path(repo_dir, args.get("path", "."))
    if p is None or not p.exists():
        return f"ERROR: '{args.get('path', '.')}' does not exist"
    if not p.is_dir():
        return f"ERROR: '{args.get('path', '.')}' is not a directory"
    entries = []
    for child in sorted(p.iterdir()):
        if child.name.startswith(".git"):
            continue
        marker = "/" if child.is_dir() else ""
        entries.append(f"{child.name}{marker}")
    return "\n".join(entries) if entries else "(empty)"


def _tool_glob_files(repo_dir: Path, args: dict) -> str:
    pattern = args["pattern"]
    matches: list[str] = []
    for f in repo_dir.rglob("*"):
        if ".git" in f.parts:
            continue
        rel = str(f.relative_to(repo_dir))
        if fnmatch.fnmatch(rel, pattern):
            matches.append(rel)
        if len(matches) >= 200:
            matches.append("[...truncated at 200]")
            break
    return "\n".join(matches) if matches else "(no matches)"


def _tool_grep_files(repo_dir: Path, args: dict) -> str:
    pattern = args["pattern"]
    path_arg = args.get("path", ".")
    p = _safe_path(repo_dir, path_arg)
    if p is None or not p.exists():
        return f"ERROR: '{path_arg}' does not exist"
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        return f"ERROR: invalid regex: {exc}"
    matches: list[str] = []
    targets = p.rglob("*") if p.is_dir() else [p]
    for f in targets:
        if not f.is_file() or ".git" in f.parts:
            continue
        try:
            for i, line in enumerate(f.read_text(errors="replace").splitlines(), start=1):
                if regex.search(line):
                    matches.append(f"{f.relative_to(repo_dir)}:{i}: {line[:200]}")
                    if len(matches) >= 100:
                        matches.append("[...truncated at 100]")
                        return "\n".join(matches)
        except Exception:
            continue
    return "\n".join(matches) if matches else "(no matches)"


def _tool_delete_file(repo_dir: Path, args: dict) -> str:
    p = _safe_path(repo_dir, args["path"])
    if p is None:
        return f"ERROR: path '{args['path']}' is outside the repo"
    if not p.exists():
        return f"ERROR: '{args['path']}' does not exist"
    try:
        if p.is_dir():
            return f"ERROR: '{args['path']}' is a directory — refuse to delete recursively"
        p.unlink()
    except Exception as exc:
        return f"ERROR deleting: {exc!r}"
    return f"OK: deleted {args['path']}"


async def _tool_run_build(repo_dir: Path, _args: dict) -> str:
    passed, output = await run_build_in_docker(repo_dir)
    if passed:
        return f"BUILD PASSED\n\n{output[-2000:]}"
    return f"BUILD FAILED\n\n{output[-4000:]}"


def _build_tool_schemas() -> list[dict[str, Any]]:
    return [
        {
            "name": "read_file",
            "description": "Read a file at the given path relative to the repo root.",
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
        {
            "name": "write_file",
            "description": (
                "Create or fully overwrite a file. Use this for new files. "
                "For modifying existing files, prefer edit_file."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
        {
            "name": "edit_file",
            "description": (
                "Replace an exact substring in a file. old_string must be unique "
                "unless replace_all=true. Errors loudly if old_string isn't found "
                "or isn't unique — encouraging targeted, intentional edits."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"},
                    "replace_all": {"type": "boolean", "default": False},
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
        {
            "name": "list_dir",
            "description": "List entries in a directory. Directories end with '/'.",
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string", "default": "."}},
            },
        },
        {
            "name": "glob_files",
            "description": "Find files matching a glob pattern (e.g. 'src/**/*.tsx').",
            "input_schema": {
                "type": "object",
                "properties": {"pattern": {"type": "string"}},
                "required": ["pattern"],
            },
        },
        {
            "name": "grep_files",
            "description": "Search for a regex pattern in files. Returns 'path:line: match'.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string", "default": "."},
                },
                "required": ["pattern"],
            },
        },
        {
            "name": "delete_file",
            "description": "Delete a single file. Refuses to delete directories.",
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
        {
            "name": "run_build",
            "description": (
                "Run `npm run build` (or equivalent) in Docker against the current "
                "workspace. Use to verify your changes compile before signalling done. "
                "Returns BUILD PASSED / BUILD FAILED with output tail."
            ),
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "name": "done",
            "description": (
                "Signal that all required changes are complete and committed. "
                "Call this ONLY after run_build returns PASSED. "
                "Include a 1-3 sentence summary of what was changed."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"summary": {"type": "string"}},
                "required": ["summary"],
            },
        },
    ]


# ── Tool dispatch ────────────────────────────────────────────────────────────

async def _dispatch_tool(name: str, args: dict, repo_dir: Path) -> str:
    if name == "read_file":
        return _tool_read_file(repo_dir, args)
    if name == "write_file":
        return _tool_write_file(repo_dir, args)
    if name == "edit_file":
        return _tool_edit_file(repo_dir, args)
    if name == "list_dir":
        return _tool_list_dir(repo_dir, args)
    if name == "glob_files":
        return _tool_glob_files(repo_dir, args)
    if name == "grep_files":
        return _tool_grep_files(repo_dir, args)
    if name == "delete_file":
        return _tool_delete_file(repo_dir, args)
    if name == "run_build":
        return await _tool_run_build(repo_dir, args)
    return f"ERROR: unknown tool '{name}'"


# ── System prompt ────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """You are a senior software engineer working on a single, focused GitHub issue. \
You have a small toolset for reading and modifying files in a git working tree. \
The working directory is the repo root.

Workflow:
1. Start by reading the issue spec and exploring the workspace with list_dir / glob_files / read_file.
2. Make targeted edits with edit_file (preferred) or write_file (for new files).
3. Delete files with delete_file ONLY when the spec explicitly requires it. Do not delete files \
the spec doesn't mention.
4. Call run_build to verify your changes compile.
5. If the build fails, read the error, fix it, and run_build again. Limit to 3 fix attempts.
6. Call the `done` tool with a 1-3 sentence summary once the build passes.

CRITICAL — HOW TO FINISH:
- You MUST call the `done` tool to complete your work. A text-only message at the end is NOT \
completion. If you stop your turn without calling `done`, your changes may be discarded. \
"The build passes and I'm finished" written as text is not enough — that exact thought must \
be passed as the `summary` parameter to the `done` tool.
- Every turn must end with EITHER a tool call (any tool, including `done`) OR you must \
continue working with more tool calls. Never reply with text alone.

Hard rules:
- NEVER write a file whose name contains '=', whitespace, or starts with a shell command \
prefix (npm, yarn, pnpm, pip, python, node, bun, deno). Those are documentation snippets, not files.
- NEVER create new files at paths the spec doesn't explicitly name. If you need a helper, \
inline it in an existing file or ask via grep_files first whether something similar already exists.
- Stay focused on the issue's scope. If you notice an unrelated bug, leave it.
- Real filenames end in known extensions (.tsx .ts .js .jsx .json .md .css .mjs .cjs .yml .yaml \
.toml .sql .env) or match known config roots (Dockerfile, Makefile, LICENSE, README).

When in doubt, read more before writing. Edits should be small and intentional."""


# ── Main entry — mirrors aider_agent.implement_issue ─────────────────────────

async def implement_issue_via_agent_sdk(
    project_id: str,
    owner: str,
    repo: str,
    issue_number: int,
    issue_title: str,
    issue_body: str,
    default_branch: str = "main",
    model: str = "claude-sonnet-4-5",
    is_reimplementation: bool = False,
    extra_context: str | None = None,
    max_iterations: int = 40,
) -> tuple[str | None, str | None, UsageTotals]:
    """Run the Agent SDK tool loop and push a branch. Returns (branch, failure, usage).

    NOTE — extra args vs aider_agent.implement_issue:
      - extra_context: same purpose as the prepended PRIMARY DIRECTIVE
      - max_iterations: hard cap on tool-loop iterations (Aider has its own cap)
      - returns UsageTotals as third element for the comparison harness
    """
    try:
        from anthropic import AsyncAnthropic
    except ImportError:
        return None, "anthropic SDK not installed (pip install anthropic)", UsageTotals()

    if not settings.anthropic_api_key:
        return None, "ANTHROPIC_API_KEY not set", UsageTotals()

    # Workspace setup — same shape as aider_agent, just inlined here so the A/B
    # is a fair fight (no dependency on Aider-specific git-state assumptions).
    workspace = settings.workspace_base_dir / str(project_id)
    workspace.mkdir(parents=True, exist_ok=True)
    repo_dir = workspace / "repo-agent-sdk"
    token = settings.github_token
    authed_url = (
        f"https://x-access-token:{token}@github.com/{owner}/{repo}.git"
        if token
        else f"https://github.com/{owner}/{repo}.git"
    )
    branch = f"agent-sdk/fix-{issue_number}"

    if (repo_dir / ".git").exists():
        await _run(["git", "remote", "set-url", "origin", authed_url], cwd=repo_dir)
        await _run(["git", "fetch", "origin"], cwd=repo_dir, timeout=60)
    else:
        rc, out = await _run(["git", "clone", authed_url, str(repo_dir)], timeout=120)
        if rc != 0:
            return None, f"clone failed: {out[-500:]}", UsageTotals()
        await _run(["git", "fetch", "origin"], cwd=repo_dir, timeout=60)

    _, remote_branches = await _run(["git", "ls-remote", "--heads", "origin", branch], cwd=repo_dir)
    branch_on_remote = branch in remote_branches

    if is_reimplementation and branch_on_remote:
        await _run(["git", "merge", "--abort"], cwd=repo_dir)
        await _run(["git", "checkout", "-B", branch, f"origin/{branch}"], cwd=repo_dir)
        await _run(["git", "reset", "--hard", f"origin/{branch}"], cwd=repo_dir)
        await _run(["git", "clean", "-fd"], cwd=repo_dir)
        merge_rc, _ = await _run(
            [
                "git",
                "-c", "user.email=agent@automatron.local",
                "-c", "user.name=Automatron Agent SDK",
                "merge", "--no-edit", f"origin/{default_branch}",
            ],
            cwd=repo_dir,
        )
        if merge_rc != 0:
            await _run(["git", "merge", "--abort"], cwd=repo_dir)
            return None, f"merge from {default_branch} conflicted on re-impl", UsageTotals()
    else:
        await _run(["git", "checkout", default_branch], cwd=repo_dir)
        await _run(["git", "reset", "--hard", f"origin/{default_branch}"], cwd=repo_dir)
        await _run(["git", "checkout", "-B", branch], cwd=repo_dir)
        await _run(["git", "clean", "-fd"], cwd=repo_dir)

    # Build the user message — issue body + optional primary-directive framing
    if extra_context:
        user_message = (
            "# PRIMARY DIRECTIVE — Fix this error\n\n"
            "This issue was previously implemented. A build/runtime error was "
            "reported on the branch. Your PRIMARY task is to fix the error below. "
            "The original spec is preserved further down as reference only.\n\n"
            "## Error to fix\n\n" + extra_context + "\n\n"
            "---\n\n"
            f"## Original issue spec (reference)\n\n# {issue_title}\n\n{issue_body}"
        )
    else:
        user_message = f"# {issue_title}\n\n{issue_body}"

    # Tool loop
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    tools = _build_tool_schemas()
    messages: list[dict[str, Any]] = [{"role": "user", "content": user_message}]
    usage = UsageTotals()
    done_summary: str | None = None
    # Track whether the agent actually made any modifications. Used to reject
    # premature done calls — the agent must call write_file / edit_file /
    # delete_file at least once before done is honoured. Without this guard the
    # agent can call done after pure exploration (we observed this on issue #49
    # which was already merged — agent read 24 files, ran build, called done,
    # produced zero changes).
    _WRITE_TOOLS = {"write_file", "edit_file", "delete_file"}
    write_tool_calls = 0

    for iteration in range(max_iterations):
        usage.iterations += 1
        try:
            response = await client.messages.create(
                model=model,
                max_tokens=8192,
                system=[{
                    "type": "text",
                    "text": _SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }],
                tools=tools,
                messages=messages,
            )
        except Exception as exc:
            logger.exception("Agent SDK: API call failed at iteration %d", iteration)
            return None, f"API call failed: {exc!r}", usage

        usage.merge_response(response.usage)

        if response.stop_reason == "end_turn":
            # The model stopped talking without calling `done`. Two cases:
            #   1. It made real changes (write/edit/delete) and just summarised
            #      in text instead of using the `done` tool — protocol slip, but
            #      the actual work succeeded. Promote the text to a done summary
            #      and fall through to commit + push.
            #   2. It made zero changes — pure exploration / chat. That's a real
            #      failure; surface it.
            text_blocks = [b.text for b in response.content if getattr(b, "type", None) == "text"]
            final_text = " ".join(text_blocks).strip()
            if write_tool_calls > 0:
                logger.warning(
                    "Agent SDK: end_turn without `done` call but %d write op(s) occurred — "
                    "promoting text to done summary for issue #%d",
                    write_tool_calls, issue_number,
                )
                done_summary = final_text[:800] if final_text else "(no summary — text was empty)"
                break
            return None, f"agent ended without calling done and made no changes: {final_text[:500]}", usage

        # Append assistant turn
        messages.append({"role": "assistant", "content": [b.model_dump() for b in response.content]})

        # Process tool calls
        tool_results: list[dict[str, Any]] = []
        for block in response.content:
            if getattr(block, "type", None) != "tool_use":
                continue
            usage.tool_calls += 1
            usage.tool_call_breakdown[block.name] = usage.tool_call_breakdown.get(block.name, 0) + 1
            if block.name in _WRITE_TOOLS:
                write_tool_calls += 1
            if block.name == "done":
                # Reject premature done: the agent must have made at least one
                # write/edit/delete before completion. Surface the rejection as
                # a tool result so the loop continues and the model can correct.
                if write_tool_calls == 0:
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": (
                            "REJECTED: you called done without making any changes "
                            "(no write_file, edit_file, or delete_file calls). "
                            "Re-read the issue's implementation_notes and "
                            "acceptance_criteria, then USE THE TOOLS to make the "
                            "changes the spec requires. Calling done again without "
                            "first writing code will be rejected again."
                        ),
                        "is_error": True,
                    })
                    continue
                done_summary = block.input.get("summary", "(no summary)")
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": "OK — done signal received",
                })
                continue
            result = await _dispatch_tool(block.name, block.input, repo_dir)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result,
            })

        if done_summary is not None:
            break

        if not tool_results:
            return None, "agent produced no tool calls in a tool_use turn", usage

        messages.append({"role": "user", "content": tool_results})

    else:
        return None, f"agent exceeded max_iterations ({max_iterations})", usage

    # Skip commit + push if the only diff vs default_branch is build artifacts.
    # Running `next build` / `npm install` inside run_build regenerates
    # next-env.d.ts, package-lock.json, .next/, etc. If those are the ONLY
    # changes (because the agent did exploration + build but no real edits),
    # committing them creates a misleading branch that suggests progress when
    # there was none. Return early so the caller sees no branch was created.
    _BUILD_ARTIFACTS = {
        "next-env.d.ts", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
        "bun.lockb", "tsconfig.tsbuildinfo",
    }
    _, status_out = await _run(["git", "status", "--porcelain"], cwd=repo_dir)
    changed_paths: list[str] = []
    for line in status_out.splitlines():
        line = line.strip()
        if not line:
            continue
        # `git status --porcelain` format: "XY path" where XY is the status code
        # (e.g. " M", "??"). Path can contain spaces. Split on first run of spaces.
        parts = line.split(None, 1)
        if len(parts) == 2:
            changed_paths.append(parts[1])
    meaningful = [p for p in changed_paths if p not in _BUILD_ARTIFACTS and not p.startswith(".next/")]
    if not meaningful:
        logger.warning(
            "Agent SDK: only build artifacts changed for issue #%d — skipping commit/push. Artifacts: %s",
            issue_number, changed_paths,
        )
        return None, (
            f"Agent called done but only build artifacts changed: {changed_paths}. "
            f"No real code was written. The spec may already be implemented on the base branch, "
            f"or the agent prematurely declared completion."
        ), usage

    # Commit + push whatever the agent changed (excluding build artifacts via
    # selective add — `git add` only meaningful paths, leave artifacts dirty).
    rc, _ = await _run(["git", "add", "--", *meaningful], cwd=repo_dir)
    if rc != 0:
        return None, f"git add failed for paths: {meaningful}", usage
    rc, out = await _run(
        ["git",
         "-c", "user.email=agent@automatron.local",
         "-c", "user.name=Automatron Agent SDK",
         "commit", "-m", f"feat: implement #{issue_number} via Agent SDK\n\n{done_summary or ''}"],
        cwd=repo_dir,
    )
    if rc != 0 and "nothing to commit" not in out.lower():
        return None, f"commit failed: {out[-500:]}", usage

    rc, out = await _run(
        ["git", "push", "-u", "origin", branch, "--force-with-lease"],
        cwd=repo_dir,
        timeout=60,
    )
    if rc != 0:
        return None, f"push failed: {redact(out)[-500:]}", usage

    return branch, None, usage
