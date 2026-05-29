"""Secret redaction helpers for Docker AI context building.

All context sent to Gordon, Docker Agent, Model Runner, or the litellm fallback
MUST pass through `sanitize_context` before leaving this process. This module
wraps `logsafe.redact` and adds env-dict sanitization (keeps key names, strips
values) so callers don't need to think about which redaction to apply.
"""

from __future__ import annotations

import re
from typing import Any

from orchestrator.logsafe import redact

# Patterns whose presence in a value suggests a secret. When any pattern
# matches we replace the value unconditionally — false positives are fine,
# false negatives (leaked secrets) are not.
_VALUE_SECRET_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"sk-ant-", re.IGNORECASE),
    re.compile(r"sk-proj-", re.IGNORECASE),
    re.compile(r"^sk-[A-Za-z0-9]", re.IGNORECASE),
    re.compile(r"AIza", re.IGNORECASE),
    re.compile(r"ghp_|gho_|ghs_|ghu_|github_pat_", re.IGNORECASE),
    re.compile(r"eyJ[A-Za-z0-9_\-]{10,}"),  # JWTs
    re.compile(r"-----BEGIN", re.IGNORECASE),  # PEM private keys
    re.compile(r"password|passwd|secret|token|api[_-]?key|private[_-]?key", re.IGNORECASE),
]

_REDACTED = "[REDACTED]"


def redact_env_dict(env: dict[str, str] | None) -> dict[str, str]:
    """Return a copy of *env* with all values replaced by [REDACTED].

    Key names are preserved so callers can still see which env vars are set
    (useful for deployment planning) without leaking their values.
    """
    if not env:
        return {}
    return {k: _REDACTED for k in env}


def redact_value(value: str) -> str:
    """Redact *value* if it looks like a secret; return as-is otherwise."""
    if not isinstance(value, str):
        return str(value)
    for pattern in _VALUE_SECRET_PATTERNS:
        if pattern.search(value):
            return _REDACTED
    return value


def sanitize_text(text: str | None) -> str:
    """Apply logsafe.redact to *text*. Safe on None."""
    if text is None:
        return ""
    return redact(text)


def sanitize_context(obj: Any, *, depth: int = 0) -> Any:
    """Recursively sanitize *obj* for inclusion in an LLM context payload.

    - dicts: values are sanitized; keys are preserved.
    - lists/tuples: items are sanitized.
    - str: logsafe.redact applied.
    - other: returned as-is.

    Depth limit (64) prevents infinite recursion on pathological input.
    """
    if depth > 64:
        return "[DEPTH_LIMIT]"
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        return {k: sanitize_context(v, depth=depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [sanitize_context(item, depth=depth + 1) for item in obj]
    return obj
