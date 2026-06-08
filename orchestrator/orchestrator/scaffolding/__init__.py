"""Project scaffolding adapters.

Pre-architect step that pushes a buildable framework skeleton to a target repo's
`main` branch *before* the architect plans any issues. Eliminates the
recurring failure mode where Aider's first issue assumes `package.json` and
friends already exist (because the architect's spec assumed it too).

See plan: /Users/qc3/.claude/plans/atomic-fluttering-duckling.md
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _detect_framework(intake_text: str, readme: str) -> str | None:
    """Return the framework key to scaffold, or None to skip.

    Conservative: only scaffold when we have positive evidence of Next.js.
    Wrong framework auto-detect is worse than no auto-detect — better to leave
    the repo empty and let the user scaffold by hand.
    """
    haystack = f"{intake_text}\n{readme}".lower()
    # Next.js signals — explicit mention, or React + Tailwind + App Router phrasing
    nextjs_signals = ("next.js", "nextjs", "next 15", "next 14", "next 13",
                      "app router", "vercel", "react server components")
    if any(s in haystack for s in nextjs_signals):
        return "nextjs"
    return None


async def maybe_scaffold_repo(
    gh: Any,
    owner: str,
    repo: str,
    intake_text: str,
    readme: str,
    log_fn: Any | None = None,
) -> str | None:
    """Detect framework and scaffold the repo if needed.

    Returns the framework key actually scaffolded, or None if scaffolding was
    skipped (repo already has package.json, framework not detected, etc.).

    `log_fn` is an optional async callable matching `Orchestrator._log` so we
    can surface progress in the activity tab.
    """
    # Already initialized? skip.
    existing_pkg = await gh.read_file(owner, repo, "package.json")
    if existing_pkg:
        logger.info("scaffolding: %s/%s already has package.json — skipping", owner, repo)
        return None

    framework = _detect_framework(intake_text, readme)
    if framework is None:
        logger.info("scaffolding: framework not detected from intake/README for %s/%s — skipping", owner, repo)
        if log_fn is not None:
            await log_fn(
                "Scaffolding: skipped",
                "No framework detected from intake/README. The architect will see an empty repo — "
                "expect issues that assume a scaffold exists to fail.",
                "AMBIGUITY",
            )
        return None

    if framework == "nextjs":
        from orchestrator.scaffolding.nextjs import scaffold_nextjs
        await scaffold_nextjs(gh, owner, repo, log_fn=log_fn)
        return "nextjs"

    return None
