"""Local preview runner — clones a GitHub repo and runs it in Docker."""
from __future__ import annotations

import asyncio
import json
import logging
import socket
import subprocess
from pathlib import Path

import httpx

from orchestrator.config import settings

logger = logging.getLogger(__name__)


# ── Port helpers ──────────────────────────────────────────────────────────────

def _get_used_host_ports() -> set[int]:
    """Return set of host ports currently bound by any Docker container."""
    try:
        import docker as docker_sdk
        client = docker_sdk.from_env()
        used: set[int] = set()
        for container in client.containers.list():
            for bindings in (container.ports or {}).values():
                for b in (bindings or []):
                    try:
                        used.add(int(b["HostPort"]))
                    except (KeyError, ValueError, TypeError):
                        pass
        client.close()
        return used
    except Exception:
        return set()


def _find_free_port() -> int:
    used = _get_used_host_ports()
    for port in range(settings.port_range_start, settings.port_range_end + 1):
        if port not in used:
            return port
    raise RuntimeError("No free port available in configured range")


def _run(cmd: list[str], cwd: Path | None = None) -> tuple[int, str]:
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
    return result.returncode, (result.stdout or "") + (result.stderr or "")


# ── Env var loading ───────────────────────────────────────────────────────────

def _parse_env_content(env_content: str) -> dict[str, str]:
    """Parse a .env-style blob into a dict.

    Handles `KEY=VALUE`, optional surrounding quotes, # comments, and blank
    lines. No dotenv interpolation — that's a runtime concern for the app.
    """
    result: dict[str, str] = {}
    if not env_content:
        return result
    for raw in env_content.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if (len(value) >= 2) and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        result[key] = value
    return result


def _materialize_env_files(repo_dir: Path, env: dict[str, str]) -> None:
    """Write env to .env.production and .env.local in the build context.

    Next.js auto-loads both at build time AND runtime, which means NEXT_PUBLIC_*
    get baked into the client bundle correctly without needing Docker buildargs.
    Writes both names because some projects only check one. Overwrites any
    existing file (preview values trump committed-but-empty examples).
    """
    if not env:
        return
    lines = [f"{k}={v}" for k, v in env.items()]
    body = "\n".join(lines) + "\n"
    for name in (".env.production", ".env.local", ".env"):
        (repo_dir / name).write_text(body)


# ── Project type detection ────────────────────────────────────────────────────

def _detect_project_type(repo_dir: Path) -> str:
    pkg = repo_dir / "package.json"
    if pkg.exists():
        try:
            data = json.loads(pkg.read_text())
            deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
            if "next" in deps:
                return "nextjs"
            if "vite" in deps or "@vitejs/plugin-react" in deps:
                return "vite"
        except Exception:
            pass
        return "node"
    if (repo_dir / "pyproject.toml").exists() or (repo_dir / "requirements.txt").exists():
        return "python"
    return "unknown"


def _ensure_dockerfile(repo_dir: Path, project_type: str) -> None:
    """Write a minimal Dockerfile if one doesn't already exist."""
    dockerfile = repo_dir / "Dockerfile"
    if dockerfile.exists():
        return

    if project_type == "nextjs":
        dockerfile.write_text(
            "FROM node:22-alpine\n"
            "WORKDIR /app\n"
            "COPY . .\n"
            "RUN npm install\n"
            "RUN npm run build\n"
            "EXPOSE 3000\n"
            'CMD ["npm", "start"]\n'
        )
    elif project_type == "vite":
        dockerfile.write_text(
            "FROM node:22-alpine\n"
            "WORKDIR /app\n"
            "COPY . .\n"
            "RUN npm install\n"
            "RUN npm run build\n"
            "RUN npm install -g serve\n"
            "EXPOSE 3000\n"
            'CMD ["serve", "-s", "dist", "-l", "3000"]\n'
        )
    elif project_type == "node":
        dockerfile.write_text(
            "FROM node:22-alpine\n"
            "WORKDIR /app\n"
            "COPY . .\n"
            "RUN npm install\n"
            "EXPOSE 3000\n"
            'CMD ["npm", "start"]\n'
        )
    elif project_type == "python":
        dockerfile.write_text(
            "FROM python:3.12-slim\n"
            "WORKDIR /app\n"
            "COPY . .\n"
            "RUN pip install --no-cache-dir -r requirements.txt 2>/dev/null || "
            "pip install --no-cache-dir -e . 2>/dev/null || true\n"
            "EXPOSE 8000\n"
            'CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]\n'
        )


def _detect_internal_port(repo_dir: Path) -> int:
    dockerfile = repo_dir / "Dockerfile"
    if dockerfile.exists():
        for line in dockerfile.read_text().splitlines():
            if line.strip().upper().startswith("EXPOSE"):
                parts = line.split()
                if len(parts) >= 2:
                    try:
                        return int(parts[1])
                    except ValueError:
                        pass
    return 3000


# ── Main entry point ─────────────────────────────────────────────────────────

async def run_preview_locally(
    project_id: str, owner: str, repo: str, default_branch: str = "main",
    branch: str | None = None, issue_number: int | None = None,
) -> str | None:
    """Clone the repo, build, and run it in Docker. Returns the preview URL or None.

    If `branch` is provided, that branch is checked out instead of `default_branch`.
    If `issue_number` is provided, it's appended to every activity_log title so the
    UI's "Send to Aider" button on ERROR rows can extract the right issue.
    """
    from orchestrator.models.project import save_activity_log, get_activity_logs
    from orchestrator.api.websocket import emit_error

    workspace = settings.workspace_base_dir / str(project_id)
    workspace.mkdir(parents=True, exist_ok=True)
    # Use a separate directory from the aider workspace to avoid branch conflicts
    repo_dir = workspace / "preview-repo"

    target_branch = branch or default_branch

    # Sequence counter shared across all activity_log entries for this preview run.
    existing = await get_activity_logs(project_id)
    _seq = [max((r.get("seq", 0) for r in existing), default=0) + 1]

    _issue_suffix = f" (issue #{issue_number})" if issue_number is not None else ""

    async def _log(title: str, body: str = "", level: str = "INFO") -> None:
        await save_activity_log(project_id, _seq[0], f"{title}{_issue_suffix}", body, level)
        _seq[0] += 1

    await _log(
        f"Preview: starting (branch={target_branch})",
        f"Repo: {owner}/{repo} • default: {default_branch}",
    )

    token = settings.github_token
    clone_url = (
        f"https://x-access-token:{token}@github.com/{owner}/{repo}.git"
        if token
        else f"https://github.com/{owner}/{repo}.git"
    )

    if (repo_dir / ".git").exists():
        logger.info("Preview: syncing %s/%s to %s", owner, repo, target_branch)
        await _log(f"Preview: syncing branch `{target_branch}`")
        _run(["git", "remote", "set-url", "origin", clone_url], cwd=repo_dir)
        _run(["git", "fetch", "origin", target_branch], cwd=repo_dir)
        _run(["git", "checkout", "-B", target_branch, f"origin/{target_branch}"], cwd=repo_dir)
        rc, out = _run(["git", "reset", "--hard", f"origin/{target_branch}"], cwd=repo_dir)
    else:
        logger.info("Preview: cloning %s/%s @ %s", owner, repo, target_branch)
        await _log(f"Preview: cloning `{owner}/{repo}` @ `{target_branch}`")
        rc, out = _run(["git", "clone", "--branch", target_branch, clone_url, str(repo_dir)])

    if rc != 0:
        logger.error("Preview: git failed:\n%s", out)
        from orchestrator.logsafe import redact
        await _log(
            "Preview: git checkout failed",
            redact(out)[-1000:],
            "ERROR",
        )
        await emit_error(
            project_id,
            f"Preview: could not check out branch `{target_branch}` — {redact(out)[-200:].strip()}",
        )
        return None

    # Compose: merge default_branch into target_branch so the preview reflects
    # what shipping the PR would actually produce. Without this, an Aider branch
    # cut from an older main misses any scaffolding/config commits that landed
    # on main after the branch was created (e.g. orchestrator-managed scaffold).
    if target_branch != default_branch:
        await _log(f"Preview: merging `{default_branch}` into `{target_branch}` (compose-on-main)")
        _run(["git", "fetch", "origin", default_branch], cwd=repo_dir)
        merge_rc, merge_out = _run(
            [
                "git",
                "-c", "user.email=preview@automatron.local",
                "-c", "user.name=Automatron Preview",
                "merge", "--no-edit", f"origin/{default_branch}",
            ],
            cwd=repo_dir,
        )
        if merge_rc != 0:
            # Conflict — surface the conflicting paths and bail.
            _, conflicts_out = _run(["git", "diff", "--name-only", "--diff-filter=U"], cwd=repo_dir)
            _run(["git", "merge", "--abort"], cwd=repo_dir)
            conflicts = conflicts_out.strip() or "(unknown)"
            logger.error("Preview: merge of %s into %s failed:\n%s", default_branch, target_branch, merge_out)
            await _log(
                f"Preview: merge conflict — `{default_branch}` ↔ `{target_branch}`",
                f"Conflicting paths:\n{conflicts}\n\nGit output:\n{merge_out[-800:]}",
                "ERROR",
            )
            await emit_error(
                project_id,
                f"Preview: merging `{default_branch}` into `{target_branch}` produced conflicts in:\n"
                f"```\n{conflicts}\n```\n"
                f"Resolve them on the PR (rebase or merge `{default_branch}` into the branch) and try again.",
            )
            return None
        logger.info("Preview: merged %s into %s for compose-on-main preview", default_branch, target_branch)

    project_type = _detect_project_type(repo_dir)
    logger.info("Preview: detected project type=%s for %s/%s @ %s", project_type, owner, repo, target_branch)
    await _log(f"Preview: detected project type = `{project_type}`")

    if project_type == "unknown":
        logger.warning("Preview: unrecognised project type for %s/%s @ %s", owner, repo, target_branch)
        if target_branch != default_branch:
            msg = (
                f"Preview cannot start: even after merging `{default_branch}` into `{target_branch}`, "
                f"no `package.json` (or `pyproject.toml`/`requirements.txt`) exists. "
                f"That means `{default_branch}` itself is unscaffolded — the orchestrator's "
                f"scaffolding step did not run (or detected no framework from the intake). "
                f"Re-run planning on this project, or push a scaffold to `{default_branch}` manually."
            )
        else:
            msg = (
                f"Preview cannot start: no `package.json` (or `pyproject.toml`/`requirements.txt`) "
                f"on `{target_branch}`. The orchestrator's scaffolding step did not run. "
                f"Re-run planning, or push a scaffold to `{target_branch}` manually."
            )
        await _log("Preview: aborted — no recognisable project type", msg, "ERROR")
        await emit_error(project_id, msg)
        return None

    # Pull the project's deploy_target.env_content (collected at onboarding,
    # also used for GitHub Actions deploy secrets) and materialise it into the
    # build context so Next.js bakes NEXT_PUBLIC_* at build time AND the
    # server-side process.env sees the rest at runtime. Without this every
    # preview crashes with HTTP 500 the moment a request touches Supabase /
    # Auth.js / any code that reads process.env.
    from orchestrator.models.project import get_project as _get_project_for_env
    project_record = await _get_project_for_env(project_id)
    deploy_target = (project_record or {}).get("deploy_target") or {}
    env_content = str(deploy_target.get("env_content") or "")
    env_vars = _parse_env_content(env_content)
    if env_vars:
        _materialize_env_files(repo_dir, env_vars)
        await _log(
            f"Preview: injected {len(env_vars)} env var(s) from project's deploy_target.env_content",
            "Wrote .env.production, .env.local, .env into the build context — "
            "Next.js auto-loads them at build (for NEXT_PUBLIC_*) and runtime (for server-side).",
        )
    else:
        await _log(
            "Preview: no env vars to inject",
            "deploy_target.env_content is empty. If the app reads process.env at runtime, "
            "expect HTTP 500 once a request reaches that code. Set env_content in the project's "
            "deploy target (same place GitHub Actions secrets come from).",
            "AMBIGUITY",
        )

    _ensure_dockerfile(repo_dir, project_type)

    port = _find_free_port()
    container_name = f"preview-{project_id}"
    image_name = f"automatron-preview-{project_id}"

    import docker as docker_sdk
    try:
        client = docker_sdk.from_env()
    except Exception as exc:
        logger.error("Preview: docker daemon unreachable: %s", exc)
        await _log("Preview: Docker daemon unreachable", str(exc), "ERROR")
        await emit_error(
            project_id,
            f"Preview: Docker daemon is not reachable from the orchestrator — `{exc}`. "
            f"Check that the orchestrator container has `/var/run/docker.sock` mounted.",
        )
        return None

    try:
        # Stop any existing container for this project
        try:
            old = client.containers.get(container_name)
            old.remove(force=True)
            logger.info("Preview: removed old container %s", container_name)
        except docker_sdk.errors.NotFound:
            pass

        # Build
        logger.info("Preview: building image %s", image_name)
        await _log(
            f"Preview: building docker image `{image_name}`",
            "This usually takes 60–120 seconds for a first build (npm install + npm run build).",
        )
        try:
            _, build_logs = client.images.build(path=str(repo_dir), tag=image_name, rm=True)
            for chunk in build_logs:
                if "stream" in chunk:
                    line = chunk["stream"].rstrip()
                    if line:
                        logger.debug("Preview build: %s", line)
        except docker_sdk.errors.BuildError as exc:
            build_output = "\n".join(
                chunk.get("stream", chunk.get("error", "")).rstrip()
                for chunk in exc.build_log
                if chunk.get("stream") or chunk.get("error")
            )
            logger.error("Preview: docker build failed:\n%s", build_output[-3000:])
            from orchestrator.logsafe import redact
            safe = redact(build_output)
            await _log("Preview: docker build FAILED", safe[-3000:], "ERROR")
            await emit_error(
                project_id,
                f"Preview: docker build failed on `{target_branch}`. See activity log for the full output. "
                f"Tail:\n```\n{safe[-600:]}\n```",
            )
            return None

        internal_port = _detect_internal_port(repo_dir)

        # Run — pass env_vars as runtime environment too. Belt-and-braces
        # alongside the .env files; some frameworks (or custom servers) read
        # process.env directly without dotenv loading.
        try:
            client.containers.run(
                image_name,
                detach=True,
                name=container_name,
                ports={f"{internal_port}/tcp": port},
                restart_policy={"Name": "unless-stopped"},
                environment=env_vars or None,
            )
        except Exception as exc:
            logger.error("Preview: docker run failed: %s", exc)
            await _log("Preview: docker run FAILED", str(exc), "ERROR")
            await emit_error(
                project_id,
                f"Preview: container failed to start — `{exc}`",
            )
            return None
        await _log(
            f"Preview: container started on port {port}",
            f"Container `{container_name}` from image `{image_name}` mapped {internal_port} → {port}.",
        )
    finally:
        try:
            client.close()
        except Exception as exc:
            logger.warning("Preview: docker client close failed: %s", exc)

    # Public URL shown to users
    from urllib.parse import urlparse
    public = (settings.automatron_public_url or "").rstrip("/")
    if public:
        host = urlparse(public).hostname or "localhost"
    else:
        host = "localhost"
    preview_url = f"http://{host}:{port}"

    # Health-check using localhost — containers can't reach the public hostname via hairpin NAT
    health_url = f"http://localhost:{port}"
    logger.info("Preview: container started, polling %s", health_url)
    await _log("Preview: waiting for HTTP readiness", f"Polling {health_url} every 3s for up to 60s.")

    for attempt in range(20):
        await asyncio.sleep(3)
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(health_url)
                if resp.status_code < 500:
                    logger.info("Preview: ready at %s (attempt %d)", preview_url, attempt + 1)
                    await _log(
                        f"Preview: READY → {preview_url}",
                        f"Container responded with HTTP {resp.status_code} after {(attempt + 1) * 3}s.",
                        "INFO",
                    )
                    return preview_url
        except Exception:
            pass

    logger.warning("Preview: health check timed out, returning URL anyway: %s", preview_url)
    await _log(
        f"Preview: URL ready but health check timed out → {preview_url}",
        "Container is running but did not respond on the expected port within 60s. "
        "Open the URL anyway — it may just be slow to start.",
        "AMBIGUITY",
    )
    return preview_url
