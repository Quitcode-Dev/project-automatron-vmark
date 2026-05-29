"""Read-only SSH client for remote host inventory.

Uses `subprocess` (not paramiko) to mirror the existing project pattern.
Only commands on the explicit ALLOWLIST may be executed. Any attempt to run
an unlisted command raises `SSHCommandNotAllowed`, ensuring the inventory
layer cannot be weaponised into an unrestricted remote shell.

Key-based auth is preferred. Password auth is intentionally unsupported here
— operators should configure their key references in the deployment target's
`auth_reference` field and let ssh-agent or an explicit key file handle auth.
"""

from __future__ import annotations

import logging
import shlex
import subprocess
from pathlib import Path
from typing import Any

from orchestrator.config import settings
from orchestrator.logsafe import redact

logger = logging.getLogger(__name__)

# Read-only commands permitted during inventory collection. Anything not in
# this set is rejected before the subprocess is spawned.
_ALLOWED_COMMANDS: frozenset[str] = frozenset(
    [
        # Host info
        "hostname", "uname -a", "cat /etc/os-release", "nproc",
        "free -b", "df -B1", "whoami", "id",
        # Docker
        "docker version --format json", "docker info --format json",
        "docker ps -a --format json", "docker ps --format json",
        "docker images --format json",
        "docker network ls --format json",
        "docker volume ls --format json",
        "docker system df --format json",
        # Ports / services
        "ss -tlnp", "ss -ulnp", "netstat -tlnp",
        # Process / service detection
        "systemctl is-active traefik", "systemctl is-active nginx",
        "systemctl is-active caddy", "systemctl is-active docker",
        "ps aux",
        # Filesystem hints (safe paths only)
        "ls -la /opt", "ls -la /opt/apps", "ls -la /var/www",
        "ls -la /home/deploy", "ls -la /opt/automatron",
        # Kamal / deploy.yml detection
        "test -f /etc/kamal/deploy.yml && echo found || echo not_found",
    ]
)

# Commands whose prefix matches any entry here are allowed (for docker inspect)
_ALLOWED_PREFIXES: tuple[str, ...] = (
    "docker inspect ",
    "docker compose -f ",
    "docker logs --tail ",
    "cat /opt/",
    "cat /var/www/",
    "cat /home/deploy/",
)


class SSHCommandNotAllowed(ValueError):
    pass


class SSHError(RuntimeError):
    pass


def _is_command_allowed(cmd: str) -> bool:
    cmd_stripped = cmd.strip()
    if cmd_stripped in _ALLOWED_COMMANDS:
        return True
    return any(cmd_stripped.startswith(prefix) for prefix in _ALLOWED_PREFIXES)


class SSHClient:
    """Thin SSH client for read-only inventory operations."""

    def __init__(
        self,
        host: str,
        user: str,
        port: int = 22,
        ssh_key_path: str | None = None,
        ssh_options: str | None = None,
    ) -> None:
        self.host = host
        self.user = user
        self.port = port
        self._key_path = ssh_key_path or settings.deploy_ssh_key_path or None
        self._extra_opts: list[str] = (
            shlex.split(ssh_options or settings.deploy_ssh_options or "")
        )

    def _build_cmd(self, remote_command: str) -> list[str]:
        cmd: list[str] = ["ssh", "-p", str(self.port)]
        # Never do interactive host-key prompts in automated inventory
        cmd += ["-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes"]
        # Short connect timeout to fail fast on unreachable hosts
        cmd += ["-o", "ConnectTimeout=10"]
        if self._key_path:
            cmd += ["-i", str(Path(self._key_path))]
        cmd += self._extra_opts
        cmd.append(f"{self.user}@{self.host}")
        cmd.append(remote_command)
        return cmd

    def run(self, remote_command: str, *, timeout: int = 30) -> tuple[int, str, str]:
        """Run *remote_command* over SSH. Returns (returncode, stdout, stderr).

        Raises `SSHCommandNotAllowed` if the command is not on the allowlist.
        Raises `SSHError` on subprocess-level failures (binary not found, etc).
        """
        if not _is_command_allowed(remote_command):
            raise SSHCommandNotAllowed(
                f"SSH command not on inventory allowlist: {remote_command!r}"
            )

        cmd = self._build_cmd(remote_command)
        logger.debug("SSH [%s@%s]: %s", self.user, self.host, remote_command)

        try:
            result = subprocess.run(
                cmd,
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return 1, "", f"timed out after {timeout}s"
        except FileNotFoundError:
            raise SSHError("ssh binary not found — is OpenSSH installed?")
        except OSError as exc:
            raise SSHError(str(exc)) from exc

        if result.returncode != 0:
            logger.debug(
                "SSH command returned %d: %s",
                result.returncode,
                redact(result.stderr[:500]),
            )
        return result.returncode, result.stdout, result.stderr

    def run_or_empty(self, remote_command: str, *, timeout: int = 30) -> str:
        """Like `run` but returns empty string on any error instead of raising."""
        try:
            rc, stdout, _ = self.run(remote_command, timeout=timeout)
            return stdout if rc == 0 else ""
        except (SSHCommandNotAllowed, SSHError):
            return ""
