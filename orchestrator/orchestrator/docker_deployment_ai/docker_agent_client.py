"""DockerAgentClient — adapter for Docker Agent (cagent).

Runs YAML-defined agent workflows via the `cagent` CLI. Each agent spec
lives in `agent_specs/`. The client loads the spec at construction time,
validates the output schema with jsonschema, and stores runs in
`docker_agent_runs`.

Like GordonClient, methods return result dicts with `error` set rather
than raising — callers in DockerAIProvider check `error` to fall through
the chain.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from orchestrator.config import settings
from orchestrator.docker_deployment_ai.secrets import sanitize_context, sanitize_text

logger = logging.getLogger(__name__)

_SPECS_DIR = Path(__file__).parent / "agent_specs"
_available: bool | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_spec(name: str) -> dict[str, Any]:
    """Load YAML spec by name (without .yaml extension)."""
    path = _SPECS_DIR / f"{name}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"Agent spec not found: {path}")
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def _validate_output(output: dict[str, Any], spec: dict[str, Any]) -> list[str]:
    """Validate *output* against the spec's output_schema. Returns list of errors."""
    schema = spec.get("output_schema")
    if not schema:
        return []
    try:
        import jsonschema  # noqa: PLC0415
        validator = jsonschema.Draft7Validator(schema)
        return [str(e.message) for e in validator.iter_errors(output)]
    except Exception as exc:
        return [f"schema validation error: {exc}"]


async def _run_cagent(spec_path: Path, input_json: str) -> tuple[int, str, str]:
    """Run cagent with the given spec and JSON input. Returns (rc, stdout, stderr)."""
    cmd = ["cagent", "run", str(spec_path), "--input", input_json]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return 127, "", "cagent: command not found"
    except OSError as exc:
        return 1, "", str(exc)

    max_bytes = settings.docker_ai_max_output_bytes
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=settings.docker_ai_timeout_seconds
        )
    except asyncio.TimeoutError:
        proc.kill()
        return 1, "", f"cagent timed out after {settings.docker_ai_timeout_seconds}s"

    return (
        proc.returncode or 0,
        stdout_b[:max_bytes].decode(errors="replace"),
        stderr_b[:max_bytes].decode(errors="replace"),
    )


class DockerAgentClient:
    """Async adapter for Docker Agent (cagent) YAML-defined workflows."""

    async def is_available(self) -> bool:
        global _available
        if _available is not None:
            return _available
        _available = shutil.which("cagent") is not None
        if _available:
            logger.info("Docker Agent (cagent) available")
        else:
            logger.info("Docker Agent (cagent) unavailable")
        return _available

    async def run_agent(
        self,
        agent_name: str,
        input_data: dict[str, Any],
        *,
        project_id: str,
        target_id: str | None = None,
        db: Any,  # aiosqlite.Connection
    ) -> dict[str, Any]:
        """Run *agent_name* with *input_data*, persist run record, return result."""
        try:
            spec = _load_spec(agent_name)
        except FileNotFoundError as exc:
            return {"error": str(exc)}

        safe_input = sanitize_context(input_data)
        input_json = json.dumps(safe_input)
        run_id = str(uuid.uuid4())
        started_at = _now()

        spec_path = _SPECS_DIR / f"{agent_name}.yaml"
        rc, stdout, stderr = await _run_cagent(spec_path, input_json)

        finished_at = _now()
        error: str | None = None
        output: dict[str, Any] = {}

        if rc != 0 or not stdout.strip():
            error = sanitize_text(stderr or f"cagent exited {rc}")
            output = {"error": error}
        else:
            try:
                raw_output = json.loads(stdout)
                if not isinstance(raw_output, dict):
                    raw_output = {}
            except json.JSONDecodeError:
                # Try to extract first JSON block
                import re  # noqa: PLC0415
                m = re.search(r"\{.*\}", stdout, re.DOTALL)
                raw_output = json.loads(m.group(0)) if m else {}

            validation_errors = _validate_output(raw_output, spec)
            if validation_errors:
                logger.warning("Agent %s output failed schema validation: %s", agent_name, validation_errors)
            output = raw_output
            output["_validation_errors"] = validation_errors

        # Persist run
        await db.execute(
            """
            INSERT INTO docker_agent_runs
            (id, project_id, target_id, agent_name, input_json, output_json,
             status, started_at, finished_at, error_message)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id, project_id, target_id, agent_name,
                input_json, json.dumps(output),
                "error" if error else "ok",
                started_at, finished_at, error,
            ),
        )
        await db.commit()

        return {
            "provider": "docker_agent",
            "agent_name": agent_name,
            "run_id": run_id,
            "raw_output": sanitize_text(stdout),
            "normalized": output,
            "error": error,
        }

    # ---- named wrappers used by DockerAIProvider ----

    async def recommend_deployment_strategy(
        self,
        repo_context: dict[str, Any],
        sanitized_inventory: dict[str, Any],
        desired_domain: str | None = None,
        **_kw: Any,
    ) -> dict[str, Any]:
        return await self.run_agent(
            "deployment-advisor",
            {
                "sanitized_inventory": sanitized_inventory,
                "repo_context": repo_context,
                "desired_domain": desired_domain,
            },
            project_id=_kw.get("project_id", ""),
            target_id=_kw.get("target_id"),
            db=_kw["db"],
        )

    async def analyze_inventory(
        self,
        sanitized_inventory: dict[str, Any],
        **_kw: Any,
    ) -> dict[str, Any]:
        return await self.run_agent(
            "reverse-proxy-detector",
            {
                "containers": sanitized_inventory.get("containers", []),
                "listening_ports": sanitized_inventory.get("listening_ports", []),
                "networks": sanitized_inventory.get("networks", []),
            },
            project_id=_kw.get("project_id", ""),
            target_id=_kw.get("target_id"),
            db=_kw["db"],
        )
