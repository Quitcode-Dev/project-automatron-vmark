"""Tests for the Next.js scaffolder's Supabase handling.

The scaffold fixture ships `@supabase/*` deps unconditionally. For a project
with no Supabase credentials those deps are dead weight AND actively harmful:
the architect reads `package.json` back after scaffolding and aborts planning
when Supabase is a dependency it can't introspect. These tests pin the strip
behaviour and, just as importantly, pin the case where the deps must be KEPT.
"""

from __future__ import annotations

import json

import pytest

from orchestrator.scaffolding import maybe_scaffold_repo
from orchestrator.scaffolding.nextjs import (
    _iter_fixture_files,
    _strip_supabase,
    _without_supabase_deps,
    scaffold_nextjs,
)


class RecordingGitHub:
    """Minimal `GitHubClient` double capturing what the scaffolder pushes."""

    def __init__(self, existing_package_json: str | None = None) -> None:
        self.pushed: list[tuple[str, str]] = []
        self._existing_package_json = existing_package_json

    async def read_file(self, owner: str, repo: str, path: str) -> str | None:
        if path == "package.json":
            return self._existing_package_json
        return None

    async def push_file(
        self,
        owner: str,
        repo: str,
        path: str,
        content: str,
        commit_message: str,
        branch: str = "main",
    ) -> None:
        self.pushed.append((path, content))

    def pushed_dict(self) -> dict[str, str]:
        return dict(self.pushed)


def _fixture_package_json() -> str:
    return dict(_iter_fixture_files())["package.json"]


# --- Fixture guard ---------------------------------------------------------
# If someone regenerates the fixture with a newer create-next-app and these
# assumptions change, every other test here becomes vacuous. Fail loudly here
# instead of silently passing everywhere else.


def test_fixture_still_ships_supabase() -> None:
    files = dict(_iter_fixture_files())

    assert "src/lib/supabase/database.types.ts" in files
    deps = json.loads(files["package.json"])["dependencies"]
    assert any(k.startswith("@supabase/") for k in deps), (
        "fixture no longer ships @supabase/* deps — the strip logic is now moot"
    )


def test_fixture_source_does_not_import_supabase() -> None:
    """The premise of stripping: nothing in the scaffold actually uses Supabase."""
    for rel, content in _iter_fixture_files():
        if rel.startswith("src/app/") or rel.startswith("src/components/"):
            assert "supabase" not in content.lower(), f"{rel} imports Supabase"


# --- Strip helpers ---------------------------------------------------------


def test_without_supabase_deps_removes_only_supabase() -> None:
    result = json.loads(_without_supabase_deps(_fixture_package_json()))

    assert not [k for k in result["dependencies"] if k.startswith("@supabase/")]
    # The rest of the stack must survive untouched.
    for kept in ("next", "react", "react-dom", "tailwind-merge"):
        assert kept in result["dependencies"]
    assert "tailwindcss" in result["devDependencies"]
    assert result["scripts"]["build"] == "next build"


def test_without_supabase_deps_survives_bad_json() -> None:
    """A fixture formatting problem must not take the whole scaffold down."""
    assert _without_supabase_deps("{not json") == "{not json"


def test_strip_supabase_drops_client_files_and_deps() -> None:
    stripped = _strip_supabase(_iter_fixture_files())
    paths = [rel for rel, _ in stripped]

    assert not [p for p in paths if p.startswith("src/lib/supabase/")]
    # Non-Supabase files under src/lib survive.
    assert "src/lib/utils.ts" in paths
    assert "@supabase/" not in dict(stripped)["package.json"]


# --- scaffold_nextjs end to end --------------------------------------------


async def test_scaffold_without_creds_pushes_no_supabase() -> None:
    gh = RecordingGitHub()

    await scaffold_nextjs(gh, "o", "r", include_supabase=False)

    pushed = gh.pushed_dict()
    assert pushed, "scaffold pushed nothing"
    assert not [p for p in pushed if p.startswith("src/lib/supabase/")]
    for path, content in pushed.items():
        assert "@supabase/" not in content, f"{path} still references @supabase/"


async def test_scaffold_with_creds_is_unchanged() -> None:
    """Regression guard for the credentialed path — must match the fixture."""
    gh = RecordingGitHub()

    await scaffold_nextjs(gh, "o", "r", include_supabase=True)

    assert gh.pushed_dict() == dict(_iter_fixture_files())


async def test_scaffold_default_includes_supabase() -> None:
    gh = RecordingGitHub()

    await scaffold_nextjs(gh, "o", "r")

    assert "src/lib/supabase/database.types.ts" in gh.pushed_dict()


async def test_generated_types_override_still_applies() -> None:
    gh = RecordingGitHub()
    generated = "// real generated types\nexport type Database = {};\n"

    await scaffold_nextjs(gh, "o", "r", database_types_ts=generated, include_supabase=True)

    assert gh.pushed_dict()["src/lib/supabase/database.types.ts"] == generated


# --- Flag threading --------------------------------------------------------


@pytest.mark.parametrize("has_creds", [True, False])
async def test_maybe_scaffold_threads_creds_flag(
    monkeypatch: pytest.MonkeyPatch, has_creds: bool
) -> None:
    seen: dict[str, object] = {}

    async def fake_scaffold(gh, owner, repo, **kwargs):  # type: ignore[no-untyped-def]
        seen.update(kwargs)

    monkeypatch.setattr("orchestrator.scaffolding.nextjs.scaffold_nextjs", fake_scaffold)

    result = await maybe_scaffold_repo(
        RecordingGitHub(),
        "o",
        "r",
        intake_text="A Next.js app",
        readme="",
        has_supabase_creds=has_creds,
    )

    assert result == "nextjs"
    assert seen["include_supabase"] is has_creds


async def test_maybe_scaffold_skips_when_package_json_exists() -> None:
    gh = RecordingGitHub(existing_package_json='{"name": "already-here"}')

    result = await maybe_scaffold_repo(
        gh, "o", "r", intake_text="A Next.js app", readme="", has_supabase_creds=False
    )

    assert result is None
    assert gh.pushed == []
