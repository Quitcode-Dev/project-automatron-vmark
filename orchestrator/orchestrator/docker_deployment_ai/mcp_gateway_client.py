"""MCP Gateway client — controlled tool access via Docker MCP Gateway.

All tool calls pass through `mcp_tool_policy.validate_tool_call` before
execution. If the MCP Gateway binary/service is unavailable, methods return
an error dict — never raise.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from typing import Any

from orchestrator.config import settings
from orchestrator.docker_deployment_ai.mcp_tool_policy import MCPToolDenied, validate_tool_call
from orchestrator.docker_deployment_ai.secrets import sanitize_context

logger = logging.getLogger(__name__)


class MCPGatewayClient:
    """Thin client around the Docker MCP Gateway service."""

    async def is_available(self) -> bool:
        return shutil.which("docker") is not None

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """Invoke *tool_name* via MCP Gateway after policy check."""
        try:
            validate_tool_call(tool_name, arguments)
        except MCPToolDenied as exc:
            return {"error": str(exc), "tool": tool_name}

        safe_args = sanitize_context(arguments)

        cmd = [
            "docker", "mcp", "call", tool_name,
            "--args", json.dumps(safe_args),
            "--timeout", str(settings.docker_ai_timeout_seconds),
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=settings.docker_ai_timeout_seconds + 5
            )
        except FileNotFoundError:
            return {"error": "docker mcp: command not found", "tool": tool_name}
        except asyncio.TimeoutError:
            return {"error": f"MCP tool timed out after {settings.docker_ai_timeout_seconds}s", "tool": tool_name}

        max_bytes = settings.docker_ai_max_output_bytes
        stdout = stdout_b[:max_bytes].decode(errors="replace")
        stderr = stderr_b[:max_bytes].decode(errors="replace")

        if proc.returncode != 0:
            return {"error": stderr or f"mcp exited {proc.returncode}", "tool": tool_name}

        try:
            return {"result": json.loads(stdout), "tool": tool_name, "error": None}
        except json.JSONDecodeError:
            return {"result": stdout, "tool": tool_name, "error": None}
