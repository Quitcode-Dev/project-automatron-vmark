"""Stable content hash for deployment plans.

The hash is computed from the canonical JSON of the plan dict so that
approval and validation can be tied to an exact plan content snapshot.
If plan_json changes after approval, the hash mismatch is caught before
execution.

Volatile metadata (run-time keys that must not affect the hash):
  _rollback_ref_available  — set transiently by validator
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

# Keys that must be excluded from the hash because they are volatile
# runtime annotations, not plan content.
_EXCLUDE_KEYS: frozenset[str] = frozenset({"_rollback_ref_available"})


def compute_plan_hash(plan: dict[str, Any]) -> str:
    """Return the SHA-256 hex digest of the canonical plan JSON.

    Keys are sorted and volatile metadata fields are excluded so the hash
    is stable across re-reads of the same logical plan.
    """
    plan_copy = {k: v for k, v in plan.items() if k not in _EXCLUDE_KEYS}
    canonical = json.dumps(plan_copy, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode()).hexdigest()
