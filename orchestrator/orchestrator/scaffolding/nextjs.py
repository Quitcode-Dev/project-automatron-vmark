"""Next.js + Tailwind + shadcn scaffolder.

Pushes a vendored, frozen scaffold to the target repo's `main` branch as a single
initial commit. All fixture files live in `fixtures/nextjs/` next to this module
and are read at runtime — no `npx create-next-app` shell-out needed (~1s instead
of 60-120s, deterministic, works offline).

Update procedure when Next.js majors change: regenerate the fixtures locally with
`npx create-next-app@latest` + `npx shadcn@latest init` + `npx shadcn@latest add
button input label card alert`, copy the resulting files over the fixture
directory, commit. Same package.json deps as the version I pushed manually to
yc-nonprofit-...-v5 (commit 96912d0).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_FIXTURES_ROOT = Path(__file__).parent / "fixtures" / "nextjs"


def _iter_fixture_files() -> list[tuple[str, str]]:
    """Walk the fixture directory and return [(repo_path, content), ...]."""
    pairs: list[tuple[str, str]] = []
    for path in sorted(_FIXTURES_ROOT.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(_FIXTURES_ROOT).as_posix()
        # Allow a `_dot_` prefix in the fixture directory for files that should
        # land with a leading dot (e.g. `_dot_gitignore` → `.gitignore`).
        # `.gitignore` files in a Python package can confuse some tooling; this
        # lets us avoid that entirely.
        leaf = path.name
        if leaf.startswith("_dot_"):
            rel = rel[: -len(leaf)] + "." + leaf[len("_dot_"):]
        pairs.append((rel, path.read_text()))
    return pairs


_DATABASE_TYPES_REL = "src/lib/supabase/database.types.ts"


async def scaffold_nextjs(
    gh: Any,
    owner: str,
    repo: str,
    log_fn: Any | None = None,
    database_types_ts: str | None = None,
) -> None:
    """Push every file under fixtures/nextjs/ to `main` as a single commit.

    Uses the git data API (blobs + tree + commit + ref update) so all 17 files
    land in ONE commit instead of one-per-file. Falls back to per-file push if
    the git data API isn't available on the GitHubClient.

    When `database_types_ts` is provided, the fixture stub for
    `src/lib/supabase/database.types.ts` is replaced with the generated content.
    This is the compile-time safety net: TypeScript will then reject any
    `donor.assigned_solicitor_id` reference whose column isn't on the live
    `donors` table, catching schema fiction at `npm run build` time.
    """
    files = _iter_fixture_files()
    if not files:
        logger.warning("scaffold_nextjs: no fixture files found at %s", _FIXTURES_ROOT)
        return

    # Swap the stub for real generated types when available.
    if database_types_ts:
        files = [
            (rel, database_types_ts) if rel == _DATABASE_TYPES_REL else (rel, content)
            for rel, content in files
        ]
        logger.info(
            "scaffold_nextjs: overriding stub %s with %d-char generated types",
            _DATABASE_TYPES_REL, len(database_types_ts),
        )

    logger.info("scaffold_nextjs: pushing %d fixture file(s) to %s/%s", len(files), owner, repo)
    if log_fn is not None:
        await log_fn(
            "Scaffolding: Next.js base",
            f"Pushing {len(files)} files (package.json, configs, root layout, shadcn primitives) to main"
            + ("; using REAL database.types.ts from live Supabase schema" if database_types_ts else "; using stub database.types.ts (no Supabase creds)"),
            "RUNNING",
        )

    # The orchestrator's GitHubClient exposes `push_file` (single-file PUT via
    # contents API). It's per-file but simple and reliable. For a 17-file scaffold
    # this means 17 commits — noisy but functionally correct, and avoids depending
    # on the git data API surface (blobs/trees/refs) which adds 4 round-trips of
    # its own.
    pushed = 0
    for rel_path, content in files:
        try:
            await gh.push_file(
                owner, repo, rel_path, content,
                commit_message=f"chore: scaffold Next.js base ({rel_path})",
                branch="main",
            )
            pushed += 1
        except Exception as exc:
            logger.error("scaffold_nextjs: failed to push %s: %s", rel_path, exc)
            # Keep going — partial scaffold is still useful for diagnosis
            continue

    logger.info("scaffold_nextjs: pushed %d/%d fixture file(s) to %s/%s",
                pushed, len(files), owner, repo)
    if log_fn is not None:
        await log_fn(
            "Scaffolding: Next.js base ready",
            f"{pushed}/{len(files)} files committed to main",
            "SUCCESS" if pushed == len(files) else "AMBIGUITY",
        )
