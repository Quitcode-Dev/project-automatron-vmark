"""GordonClient — adapter for Docker AI / Gordon (`docker ai`).

Gordon is a Docker Desktop feature. Inside a Docker container it will usually
be absent; `is_available()` returns False and the caller falls through to the
next provider. That is the expected production code path — do not treat an
unavailable Gordon as an error unless `DOCKER_AI_REQUIRE_GORDON=true`.

All context sent to Gordon passes through `secrets.sanitize_context` before
the subprocess is spawned. Raw stdout is stored alongside the normalized parse
result so operators can inspect Gordon's raw reasoning if needed.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from pathlib import Path
from typing import Any

from orchestrator.config import settings
from orchestrator.docker_deployment_ai.gordon_prompts import (
    analyze_inventory_prompt,
    explain_build_failure_prompt,
    explain_runtime_failure_prompt,
    recommend_deployment_strategy_prompt,
    review_compose_prompt,
    review_dockerfile_prompt,
)
from orchestrator.docker_deployment_ai.gordon_result_parser import (
    normalize_compose_review,
    normalize_deployment_strategy,
    normalize_dockerfile_review,
    normalize_failure_explanation,
    normalize_inventory_analysis,
    parse_gordon_output,
)
from orchestrator.docker_deployment_ai.secrets import sanitize_context, sanitize_text

logger = logging.getLogger(__name__)

# Cached result of availability probe — probed once per process lifetime.
_available: bool | None = None


async def _run_gordon(prompt: str) -> tuple[int, str, str]:
    """Run `docker ai --stdin` with *prompt* on stdin.

    Returns (returncode, stdout, stderr). Respects `docker_ai_timeout_seconds`
    and truncates combined output to `docker_ai_max_output_bytes`.
    """
    cmd = ["docker", "ai", "--stdin"]
    if settings.docker_ai_model:
        cmd += ["--model", settings.docker_ai_model]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return 127, "", "docker: command not found"
    except OSError as exc:
        return 1, "", str(exc)

    encoded_prompt = prompt.encode()
    max_bytes = settings.docker_ai_max_output_bytes

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(input=encoded_prompt),
            timeout=settings.docker_ai_timeout_seconds,
        )
    except asyncio.TimeoutError:
        proc.kill()
        return 1, "", f"gordon timed out after {settings.docker_ai_timeout_seconds}s"

    stdout = stdout_bytes[:max_bytes].decode(errors="replace")
    stderr = stderr_bytes[:max_bytes].decode(errors="replace")
    return proc.returncode or 0, stdout, stderr


def _make_result(
    raw_stdout: str,
    raw_stderr: str,
    returncode: int,
    analysis_type: str,
    normalizer: Any,
) -> dict[str, Any]:
    """Build the standard result envelope returned by all GordonClient methods."""
    if returncode != 0 or not raw_stdout.strip():
        error_msg = sanitize_text(raw_stderr or f"gordon exited {returncode}")
        return {
            "provider": "gordon",
            "analysis_type": analysis_type,
            "raw_output": sanitize_text(raw_stdout),
            "normalized": {"parse_error": error_msg},
            "error": error_msg,
        }
    raw_parsed = parse_gordon_output(raw_stdout, analysis_type=analysis_type)
    return {
        "provider": "gordon",
        "analysis_type": analysis_type,
        "raw_output": sanitize_text(raw_stdout),
        "normalized": normalizer(raw_parsed),
        "error": None,
    }


class GordonClient:
    """Async adapter for Gordon (`docker ai`).

    All methods are safe to call when Gordon is unavailable — they return a
    result dict with `error` set rather than raising. Callers check `error` to
    decide whether to fall through the provider chain.
    """

    async def is_available(self) -> bool:
        """Return True if `docker ai` is installed and responds to --version.

        Result is cached for the process lifetime. Gordon is a Docker Desktop
        feature and will typically be absent inside a container — that is fine
        and expected.
        """
        global _available
        if _available is not None:
            return _available

        if shutil.which("docker") is None:
            logger.info("Gordon unavailable: docker binary not found")
            _available = False
            return False

        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "ai", "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=5)
            _available = (proc.returncode == 0)
        except (OSError, asyncio.TimeoutError):
            _available = False

        if _available:
            logger.info("Gordon available (docker ai found)")
        else:
            logger.info("Gordon unavailable (docker ai not responding)")
        return _available

    async def get_version_info(self) -> dict[str, Any]:
        if not await self.is_available():
            return {"available": False}
        _, stdout, stderr = await _run_gordon("What version of docker ai are you?")
        return {"available": True, "raw": sanitize_text(stdout or stderr)}

    async def analyze_inventory(self, sanitized_inventory: dict[str, Any]) -> dict[str, Any]:
        prompt = analyze_inventory_prompt(sanitize_context(sanitized_inventory))
        rc, stdout, stderr = await _run_gordon(prompt)
        return _make_result(stdout, stderr, rc, "analyze_inventory", normalize_inventory_analysis)

    async def review_dockerfile(self, dockerfile: str, repo_context: dict[str, Any]) -> dict[str, Any]:
        safe_ctx = sanitize_context(repo_context)
        safe_df = sanitize_text(dockerfile)
        prompt = review_dockerfile_prompt(safe_df, safe_ctx)
        rc, stdout, stderr = await _run_gordon(prompt)
        return _make_result(stdout, stderr, rc, "review_dockerfile", normalize_dockerfile_review)

    async def review_compose(
        self,
        compose_yaml: str,
        sanitized_inventory: dict[str, Any],
    ) -> dict[str, Any]:
        safe_inv = sanitize_context(sanitized_inventory)
        safe_compose = sanitize_text(compose_yaml)
        prompt = review_compose_prompt(safe_compose, safe_inv)
        rc, stdout, stderr = await _run_gordon(prompt)
        return _make_result(stdout, stderr, rc, "review_compose", normalize_compose_review)

    async def explain_build_failure(
        self,
        logs: str,
        dockerfile: str | None = None,
        compose_yaml: str | None = None,
    ) -> dict[str, Any]:
        safe_logs = sanitize_text(logs)
        safe_df = sanitize_text(dockerfile) if dockerfile else None
        safe_compose = sanitize_text(compose_yaml) if compose_yaml else None
        prompt = explain_build_failure_prompt(safe_logs, safe_df, safe_compose)
        rc, stdout, stderr = await _run_gordon(prompt)
        return _make_result(stdout, stderr, rc, "explain_build_failure", normalize_failure_explanation)

    async def explain_runtime_failure(
        self,
        logs: str,
        inspect_json: dict[str, Any],
    ) -> dict[str, Any]:
        safe_logs = sanitize_text(logs)
        safe_inspect = sanitize_context(inspect_json)
        prompt = explain_runtime_failure_prompt(safe_logs, safe_inspect)
        rc, stdout, stderr = await _run_gordon(prompt)
        return _make_result(stdout, stderr, rc, "explain_runtime_failure", normalize_failure_explanation)

    async def recommend_deployment_strategy(
        self,
        repo_context: dict[str, Any],
        sanitized_inventory: dict[str, Any],
        desired_domain: str | None = None,
    ) -> dict[str, Any]:
        safe_ctx = sanitize_context(repo_context)
        safe_inv = sanitize_context(sanitized_inventory)
        prompt = recommend_deployment_strategy_prompt(safe_ctx, safe_inv, desired_domain)
        rc, stdout, stderr = await _run_gordon(prompt)
        return _make_result(stdout, stderr, rc, "recommend_deployment_strategy", normalize_deployment_strategy)

    @staticmethod
    def _write_temp_file(content: str, suffix: str) -> Path:
        """Write *content* to a named temp file, return its Path."""
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=suffix, delete=False, encoding="utf-8"
        )
        tmp.write(content)
        tmp.close()
        return Path(tmp.name)
