"""MCP tool policy — allowlist/denylist for Docker MCP Gateway tool access.

This is a hardcoded policy file. It is intentionally NOT configurable per
project — allowing operators to weaken the policy per-project would be a
privilege escalation path. Changes to this policy require a code review and
deployment cycle.

Allowed tool categories (read-only, inspection, analysis):
  - docker:inspect         — container/image/network/volume metadata
  - docker:logs            — container log reading (no write, no exec)
  - filesystem:read        — read Compose/Dockerfile/config files
  - network:inspect        — port and network metadata
  - compose:analyze        — static analysis of Compose files

Forbidden tool categories (write, execution, secrets, destructive):
  - shell / exec / bash / sh / run_command / custom_command
  - ssh / remote_exec
  - secrets:read / secrets:write / vault
  - docker:run / docker:exec / docker:kill / docker:rm / docker:rmi
  - docker:compose:up / docker:compose:down / docker:compose:restart
  - filesystem:write / filesystem:delete
  - network:create / network:delete
  - volume:create / volume:delete / volume:mount
  - Any tool not explicitly in the allowlist
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_ALLOWED_TOOLS: frozenset[str] = frozenset(
    [
        "docker:inspect",
        "docker:logs",
        "docker:image:inspect",
        "docker:network:inspect",
        "docker:volume:inspect",
        "docker:compose:config",
        "filesystem:read",
        "network:inspect",
        "compose:analyze",
        "dockerfile:analyze",
    ]
)

_DENIED_TOOLS: frozenset[str] = frozenset(
    [
        "shell", "exec", "bash", "sh", "run_command", "custom_command",
        "ssh", "remote_exec", "remote_shell",
        "secrets:read", "secrets:write", "vault",
        "docker:run", "docker:exec", "docker:kill", "docker:rm", "docker:rmi",
        "docker:compose:up", "docker:compose:down", "docker:compose:restart",
        "docker:compose:pull", "docker:compose:build",
        "filesystem:write", "filesystem:delete", "filesystem:create",
        "network:create", "network:delete", "network:connect", "network:disconnect",
        "volume:create", "volume:delete", "volume:mount",
        "docker:build", "docker:push", "docker:pull",
        "docker:swarm:join", "docker:swarm:leave",
    ]
)

MAX_EXECUTION_TIME_SECONDS: int = 60
MAX_OUTPUT_BYTES: int = 131072  # 128 KB


class MCPToolDenied(ValueError):
    """Raised when a requested tool is on the denylist or not on the allowlist."""


def validate_tool_call(tool_name: str, arguments: dict[str, Any] | None = None) -> None:
    """Raise `MCPToolDenied` if *tool_name* is not permitted.

    Called before any MCP tool invocation. This is the policy enforcement
    point — do not bypass it with try/except.
    """
    normalized = tool_name.strip().lower()

    if normalized in _DENIED_TOOLS:
        logger.warning("MCP tool DENIED (on denylist): %s", tool_name)
        raise MCPToolDenied(
            f"MCP tool '{tool_name}' is explicitly forbidden by Automatron's deployment "
            f"security policy. Only read-only inspection tools are allowed."
        )

    if normalized not in _ALLOWED_TOOLS:
        logger.warning("MCP tool DENIED (not in allowlist): %s", tool_name)
        raise MCPToolDenied(
            f"MCP tool '{tool_name}' is not on the allowed tool list. "
            f"Allowed tools: {sorted(_ALLOWED_TOOLS)}"
        )

    logger.debug("MCP tool permitted: %s", tool_name)


def is_tool_allowed(tool_name: str) -> bool:
    """Return True if *tool_name* is permitted, False otherwise (no exception)."""
    try:
        validate_tool_call(tool_name)
        return True
    except MCPToolDenied:
        return False
