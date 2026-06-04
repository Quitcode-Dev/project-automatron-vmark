"""Approval hash gate for workflow candidates.

The hash is computed over the complete serialised candidate so any mutation
after approval — even whitespace in a workflow file — invalidates it.
"""

from __future__ import annotations

import hashlib
import json
import logging

from orchestrator.cicd_specialist.models import WorkflowCandidate

logger = logging.getLogger(__name__)


def compute_candidate_hash(candidate: WorkflowCandidate) -> str:
    """Return a deterministic SHA-256 hex digest for a WorkflowCandidate."""
    data = candidate.model_dump()
    # Normalise file order so the hash is stable regardless of insertion order.
    data["files"] = sorted(data["files"], key=lambda f: f["path"])
    serialised = json.dumps(data, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(serialised.encode("utf-8")).hexdigest()


def verify_approval(candidate: WorkflowCandidate, approved_hash: str) -> bool:
    """Return True iff the candidate still matches its approved hash."""
    current = compute_candidate_hash(candidate)
    if current != approved_hash:
        logger.warning(
            "Approval hash mismatch: approved=%s…, current=%s…",
            approved_hash[:12],
            current[:12],
        )
        return False
    return True
