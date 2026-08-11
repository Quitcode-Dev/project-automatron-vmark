"""Builder git hygiene and GitHub error surfacing.

Three production failures on Quitcode-Dev/pando-vsemerenko-training, one root
cause each:

  #51  push rejected — `next-swc.linux-arm64-musl.node` is 133.84 MB, over
       GitHub's 100 MB file limit. The repo ships no .gitignore (spec-only: docs
       and a README), and the builder clones directly rather than through
       RepositoryManager, so `_ensure_gitignore` never ran on its working copy.
       `npm install` left node_modules/ untracked and staging swept it in.

  #50  PR creation 422. The pushed branch was byte-identical to main
       (ahead_by=0, total_commits=0): `git commit` had said "nothing to commit",
       which the code swallowed before pushing anyway and reporting success. The
       422 surfaced two steps later with the real cause nowhere in the log.

  both The reason was unreadable, because the GitHub client called bare
       `raise_for_status()` and discarded the response body — where GitHub
       states "No commits between main and <branch>" and, for the earlier 410,
       "Issues are disabled for this repo".

Coverage:
  1.  Artifact directories are excluded from staging by path prefix
  2.  The collapsed untracked-directory form ("node_modules/") is caught
  3.  Real source paths are never excluded
  4.  .gitignore is created when absent, and extended additively when present
  5.  create_pull_request / create_issue / create_milestone surface the body
"""

from __future__ import annotations

import httpx
import pytest

from orchestrator.builder.agent_sdk import _ARTIFACT_DIRS, _ensure_builder_gitignore
from orchestrator.github.issues import GitHubClient


def _meaningful(changed_paths: list[str]) -> list[str]:
    """Mirror of the staging filter in run_agent_sdk (same predicate)."""
    build_artifacts = {
        "next-env.d.ts", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
        "bun.lockb", "tsconfig.tsbuildinfo",
    }
    return [
        p for p in changed_paths
        if p not in build_artifacts and not any(p.startswith(d) for d in _ARTIFACT_DIRS)
    ]


# ---------------------------------------------------------------------------
# 1-3. Staging filter
# ---------------------------------------------------------------------------


def test_untracked_node_modules_directory_is_never_staged() -> None:
    """The exact shape that broke #51: git collapses the untracked dir to one entry."""
    assert _meaningful(["node_modules/"]) == []


@pytest.mark.parametrize(
    "path",
    [
        "node_modules/@next/swc-linux-arm64-musl/next-swc.linux-arm64-musl.node",
        "node_modules/react/index.js",
        ".next/static/chunks/main.js",
        "dist/bundle.js",
        "build/index.html",
        ".venv/lib/python3.12/site-packages/x.py",
        "__pycache__/mod.cpython-312.pyc",
        ".pytest_cache/v/cache/lastfailed",
    ],
)
def test_artifact_paths_are_excluded(path: str) -> None:
    assert _meaningful([path]) == []


@pytest.mark.parametrize(
    "path",
    [
        "src/app/page.tsx",
        "src/lib/node_modules_helper.ts",  # substring, not a directory prefix
        "docs/PRD.md",
        "package.json",
        "app/api/route.ts",
        ".github/workflows/ci.yml",
    ],
)
def test_real_source_paths_survive(path: str) -> None:
    assert _meaningful([path]) == [path]


def test_mixed_changeset_keeps_only_source() -> None:
    changed = [
        "node_modules/",
        "package-lock.json",
        ".next/build-manifest.json",
        "src/app/page.tsx",
        "src/lib/auth.ts",
    ]

    assert _meaningful(changed) == ["src/app/page.tsx", "src/lib/auth.ts"]


# ---------------------------------------------------------------------------
# 4. .gitignore on the builder's clone
# ---------------------------------------------------------------------------


def test_gitignore_is_created_when_the_repo_has_none(tmp_path) -> None:
    """The spec-only-repo case — docs and a README, no .gitignore."""
    _ensure_builder_gitignore(tmp_path)

    written = (tmp_path / ".gitignore").read_text().splitlines()
    assert "node_modules/" in written
    for d in _ARTIFACT_DIRS:
        assert d in written


def test_existing_gitignore_is_extended_not_replaced(tmp_path) -> None:
    (tmp_path / ".gitignore").write_text("# project rules\nsecrets.json\nnode_modules/\n")

    _ensure_builder_gitignore(tmp_path)

    written = (tmp_path / ".gitignore").read_text().splitlines()
    assert "secrets.json" in written          # pre-existing entry preserved
    assert written.count("node_modules/") == 1  # no duplicate
    assert ".next/" in written                # missing entry added


def test_gitignore_write_failure_does_not_raise(tmp_path) -> None:
    """The prefix filter is the load-bearing guard; the file is convenience."""
    (tmp_path / ".gitignore").mkdir()  # a directory where a file belongs

    _ensure_builder_gitignore(tmp_path)  # must not raise


# ---------------------------------------------------------------------------
# 5. GitHub error bodies reach the caller
# ---------------------------------------------------------------------------


def _client_returning(status: int, body: str) -> GitHubClient:
    gh = GitHubClient()
    transport = httpx.MockTransport(lambda request: httpx.Response(status, text=body))

    def _fake_client(*args, **kwargs):
        return httpx.AsyncClient(transport=transport, base_url="https://api.github.com")

    gh._client = _fake_client  # type: ignore[method-assign]
    return gh


async def test_pr_422_body_reaches_the_caller() -> None:
    """The #50 failure: without the body, 'No commits between' is invisible."""
    gh = _client_returning(422, '{"message":"Validation Failed","errors":[{"message":'
                                '"No commits between main and agent-sdk/fix-50"}]}')

    with pytest.raises(RuntimeError) as exc:
        await gh.create_pull_request(
            "Quitcode-Dev", "pando-vsemerenko-training",
            title="t", body="b", head="agent-sdk/fix-50", base="main",
        )

    assert "No commits between main and agent-sdk/fix-50" in str(exc.value)
    assert "422" in str(exc.value)


async def test_issue_410_body_reaches_the_caller() -> None:
    """The earlier fork failure: 'Issues are disabled for this repo'."""
    gh = _client_returning(410, '{"message":"Issues are disabled for this repo"}')

    with pytest.raises(RuntimeError) as exc:
        await gh.create_issue("ylehenkyi-qc", "some-fork", title="t", body="b")

    assert "Issues are disabled for this repo" in str(exc.value)
    assert "410" in str(exc.value)


async def test_milestone_error_body_reaches_the_caller() -> None:
    gh = _client_returning(404, '{"message":"Not Found"}')

    with pytest.raises(RuntimeError) as exc:
        await gh.create_milestone("owner", "repo", "E-008: Platform Administration")

    assert "Not Found" in str(exc.value)
    assert "E-008: Platform Administration" in str(exc.value)
