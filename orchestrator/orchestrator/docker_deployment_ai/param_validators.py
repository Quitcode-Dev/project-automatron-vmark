"""Strict parameter validators for deployment executor actions.

All user/LLM-controlled values that appear in SSH command strings MUST pass
through these validators before being forwarded to ExecutorSSHClient.

Why: SSH runs the remote_command through a shell on the remote host.
Even though the local subprocess call is NOT shell=True, the remote side
IS a shell. Injecting "web; cat /etc/shadow" as the service name would
execute both commands on the remote host.

Validation strategy: reject any value that cannot match a narrow allowlist
pattern. False positives (valid names rejected) are acceptable. False
negatives (injected commands accepted) are not.
"""

from __future__ import annotations

import re

# Docker name rules: alphanumeric + [._-], max 128, must start alphanumeric.
# We also allow "/" for image tags (e.g. service name like "app/worker")
# but NOT leading/trailing slashes or double-slashes.
_SAFE_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_./-]{0,127}$")

# Shell metacharacters — reject if found anywhere in a value.
_SHELL_METACHARS_RE = re.compile(r"""[;&|`$\\><()\n\r\t!{}*?'"#~^]""")

# Compose file: relative path, no directory traversal, safe chars only.
# Allow e.g. "docker-compose.yml", "compose.prod.yml", "ops/deploy.yml"
_SAFE_FILENAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.\-/]{0,127}$")

_SAFE_DRIVERS = frozenset({"bridge", "host", "overlay", "macvlan", "ipvlan", "none"})


class ParameterValidationError(ValueError):
    """Raised when a plan parameter fails pre-command validation.

    Callers should treat this as a plan execution error and abort the run
    before any SSH call is made.
    """


def _reject_metachars(value: str, param_name: str) -> None:
    m = _SHELL_METACHARS_RE.search(value)
    if m:
        raise ParameterValidationError(
            f"Shell metacharacter {m.group(0)!r} rejected in {param_name}={value!r}"
        )


def validate_safe_name(value: str, param_name: str = "name") -> str:
    """Validate a Docker container / service / network / volume name.

    Empty string is allowed and means "omit from command" — callers decide
    whether empty is acceptable for their context.
    """
    if not value:
        return value
    # Metachar check runs first so the error message names the offending char
    _reject_metachars(value, param_name)
    if not _SAFE_NAME_RE.match(value):
        raise ParameterValidationError(
            f"Invalid {param_name}={value!r}: "
            "must start with alphanumeric, contain only [a-zA-Z0-9._/-], max 128 chars"
        )
    return value


def validate_compose_file(value: str) -> str:
    """Validate a compose filename: relative, no traversal, safe chars."""
    if not value:
        return "docker-compose.yml"
    if ".." in value:
        raise ParameterValidationError(
            f"Directory traversal rejected in compose_file={value!r}"
        )
    if value.startswith("/"):
        raise ParameterValidationError(
            f"Absolute path rejected in compose_file={value!r}: must be relative filename"
        )
    if not _SAFE_FILENAME_RE.match(value):
        raise ParameterValidationError(
            f"Invalid compose_file={value!r}: "
            "only alphanumeric, dots, dashes, underscores, forward slashes allowed"
        )
    _reject_metachars(value, "compose_file")
    return value


def validate_deploy_path(value: str) -> str:
    """Validate an absolute deploy path: must start with '/', no traversal, no metachars."""
    if not value:
        raise ParameterValidationError("deploy_path must not be empty")
    if not value.startswith("/"):
        raise ParameterValidationError(
            f"deploy_path must be absolute (start with /), got {value!r}"
        )
    if ".." in value:
        raise ParameterValidationError(
            f"Directory traversal (..) rejected in deploy_path={value!r}"
        )
    _reject_metachars(value, "deploy_path")
    return value


def validate_service_name(value: str) -> str:
    """Validate a docker compose service name (may be empty = all services)."""
    return validate_safe_name(value, "service")


def validate_network_name(value: str) -> str:
    return validate_safe_name(value, "network_name")


def validate_volume_name(value: str) -> str:
    return validate_safe_name(value, "volume_name")


def validate_network_driver(value: str) -> str:
    """Validate a Docker network driver. Only known safe drivers are allowed."""
    clean = (value or "bridge").lower().strip()
    if clean not in _SAFE_DRIVERS:
        raise ParameterValidationError(
            f"Unknown network driver={clean!r}. Allowed: {sorted(_SAFE_DRIVERS)}"
        )
    return clean


def validate_log_lines(value: object) -> int:
    """Coerce and clamp a log-line count to [1, 10000]."""
    try:
        n = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 100
    return max(1, min(n, 10_000))


_MAX_FILE_CONTENT_BYTES = 512 * 1024  # 512 KB hard cap for uploaded file content


def validate_file_content(content: str, param_name: str = "content") -> str:
    """Validate text file content for UPLOAD_FILE / WRITE_ENV_FILE.

    The content is delivered to the remote host via subprocess stdin — it is
    never shell-interpolated. The only constraint here is a size cap to prevent
    very large payloads from being embedded in plan JSON.
    """
    if not isinstance(content, str):
        raise ParameterValidationError(
            f"{param_name} must be a string, got {type(content).__name__}"
        )
    if len(content.encode()) > _MAX_FILE_CONTENT_BYTES:
        raise ParameterValidationError(
            f"{param_name} exceeds maximum allowed size of "
            f"{_MAX_FILE_CONTENT_BYTES // 1024} KB"
        )
    return content


def validate_path_under_deploy_dir(path: str, deploy_path: str) -> str:
    """Validate that *path* is within *deploy_path*.

    Prevents UPLOAD_FILE / WRITE_ENV_FILE from writing outside the deploy
    directory (e.g. /etc/cron.d or /root/.ssh/authorized_keys).

    *path* must be an absolute path under *deploy_path* (not equal to it —
    a bare directory is not a valid file destination).
    """
    path = validate_deploy_path(path)
    deploy_dir = deploy_path.rstrip("/")
    if not path.startswith(deploy_dir + "/"):
        raise ParameterValidationError(
            f"Upload path {path!r} is outside deploy directory {deploy_dir!r}. "
            "Files may only be written within the deploy directory."
        )
    return path


def validate_env_file_path(path: str, deploy_path: str) -> str:
    """Validate a .env file destination path.

    Extends validate_path_under_deploy_dir with a check that the basename
    looks like an env file (starts or ends with '.env'), preventing
    WRITE_ENV_FILE from masquerading as a generic file-write.
    """
    path = validate_path_under_deploy_dir(path, deploy_path)
    basename = path.rsplit("/", 1)[-1]
    if not (basename.startswith(".env") or basename.endswith(".env")):
        raise ParameterValidationError(
            f"WRITE_ENV_FILE path must be a .env file (basename starts or ends "
            f"with '.env'), got {basename!r}"
        )
    return path
