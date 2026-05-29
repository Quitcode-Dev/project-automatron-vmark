"""ExecutorSSHClient — typed command builders for deployment mutations.

This client is the ONLY SSH surface available to the typed executor.
It does NOT expose a generic run(command_string) method. Every deployment
action is a named builder method that:

  1. Receives pre-validated parameters (callers must run param_validators first).
  2. Applies shlex.quote() to every variable part of the command string.
  3. Constructs and executes a single known, safe SSH command.

This prevents remote shell injection even if plan parameters are malicious:
the remote SSH server executes the command through a shell, so quoting is
mandatory for all variable parts.

Security contract:
  - No generic command passthrough.
  - No eval / exec / arbitrary string execution.
  - All params quoted with shlex.quote() before interpolation.
  - Pre-validation (param_validators) is a separate layer; this client
    trusts that callers have already validated, and quotes as defence-in-depth.
"""

from __future__ import annotations

import logging
import shlex
import subprocess
from pathlib import Path

from orchestrator.config import settings
from orchestrator.logsafe import redact

logger = logging.getLogger(__name__)


class ExecutorSSHError(RuntimeError):
    pass


class ExecutorSSHClient:
    """SSH client exposing only typed deployment mutation command builders."""

    def __init__(
        self,
        host: str,
        user: str,
        port: int = 22,
        ssh_key_path: str | None = None,
    ) -> None:
        self.host = host
        self.user = user
        self.port = port
        self._key_path = ssh_key_path or settings.deploy_ssh_key_path or None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ssh_prefix(self) -> list[str]:
        cmd: list[str] = ["ssh", "-p", str(self.port)]
        cmd += ["-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes"]
        cmd += ["-o", "ConnectTimeout=15"]
        if self._key_path:
            cmd += ["-i", str(Path(self._key_path))]
        cmd.append(f"{self.user}@{self.host}")
        return cmd

    def _run(self, remote_cmd: str, *, timeout: int = 60) -> tuple[int, str, str]:
        """Execute *remote_cmd* on the remote host. Returns (rc, stdout, stderr)."""
        full_cmd = self._ssh_prefix()
        full_cmd.append(remote_cmd)
        logger.debug("ExecutorSSH [%s@%s]: %s", self.user, self.host, remote_cmd)
        try:
            result = subprocess.run(
                full_cmd,
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return 1, "", f"timed out after {timeout}s"
        except FileNotFoundError:
            raise ExecutorSSHError("ssh binary not found — is OpenSSH installed?")
        except OSError as exc:
            raise ExecutorSSHError(str(exc)) from exc
        if result.returncode != 0:
            logger.debug(
                "ExecutorSSH failed (rc=%d): %s",
                result.returncode,
                redact(result.stderr[:500]),
            )
        return result.returncode, result.stdout, result.stderr

    # ------------------------------------------------------------------
    # Typed command builders — each quotes ALL variable parts
    # ------------------------------------------------------------------

    def run_mkdir(self, path: str, *, timeout: int = 15) -> tuple[int, str, str]:
        return self._run(f"mkdir -p {shlex.quote(path)}", timeout=timeout)

    def run_docker_compose_config(
        self, deploy_path: str, compose_file: str, *, timeout: int = 30
    ) -> tuple[int, str, str]:
        return self._run(
            f"docker compose -f {shlex.quote(deploy_path)}/{shlex.quote(compose_file)} config",
            timeout=timeout,
        )

    def run_docker_compose_pull(
        self, deploy_path: str, compose_file: str, *, timeout: int = 180
    ) -> tuple[int, str, str]:
        return self._run(
            f"docker compose -f {shlex.quote(deploy_path)}/{shlex.quote(compose_file)} pull",
            timeout=timeout,
        )

    def run_docker_compose_build(
        self, deploy_path: str, compose_file: str, *, timeout: int = 300
    ) -> tuple[int, str, str]:
        return self._run(
            f"docker compose -f {shlex.quote(deploy_path)}/{shlex.quote(compose_file)} build",
            timeout=timeout,
        )

    def run_docker_compose_up(
        self,
        deploy_path: str,
        compose_file: str,
        service: str = "",
        *,
        timeout: int = 120,
    ) -> tuple[int, str, str]:
        base = (
            f"docker compose -f {shlex.quote(deploy_path)}/{shlex.quote(compose_file)} up -d"
        )
        cmd = f"{base} {shlex.quote(service)}" if service else base
        return self._run(cmd, timeout=timeout)

    def run_docker_compose_down(
        self, deploy_path: str, compose_file: str, *, timeout: int = 60
    ) -> tuple[int, str, str]:
        return self._run(
            f"docker compose -f {shlex.quote(deploy_path)}/{shlex.quote(compose_file)} down",
            timeout=timeout,
        )

    def run_docker_compose_ps(
        self, deploy_path: str, compose_file: str, *, timeout: int = 30
    ) -> tuple[int, str, str]:
        return self._run(
            f"docker compose -f {shlex.quote(deploy_path)}/{shlex.quote(compose_file)} ps --format json",
            timeout=timeout,
        )

    def run_docker_compose_logs(
        self,
        deploy_path: str,
        compose_file: str,
        service: str,
        lines: int,
        *,
        timeout: int = 30,
    ) -> tuple[int, str, str]:
        base = (
            f"docker compose -f {shlex.quote(deploy_path)}/{shlex.quote(compose_file)}"
            f" logs --tail {int(lines)}"
        )
        cmd = f"{base} {shlex.quote(service)}" if service else base
        return self._run(cmd, timeout=timeout)

    def run_docker_network_create(
        self, network_name: str, driver: str = "bridge", *, timeout: int = 30
    ) -> tuple[int, str, str]:
        return self._run(
            f"docker network create --driver {shlex.quote(driver)} {shlex.quote(network_name)}",
            timeout=timeout,
        )

    def run_docker_volume_create(
        self, volume_name: str, *, timeout: int = 30
    ) -> tuple[int, str, str]:
        return self._run(
            f"docker volume create {shlex.quote(volume_name)}",
            timeout=timeout,
        )
