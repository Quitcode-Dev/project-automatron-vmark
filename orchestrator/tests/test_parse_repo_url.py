"""Repo URL parsing — regression cover for the `.rstrip(".git")` truncation.

`_parse_repo_url` used `rstrip(".git")` to drop a `.git` suffix. `str.rstrip`
takes a SET OF CHARACTERS, not a suffix, so it ate any trailing '.', 'g', 'i' or
't': `barthu-method-ylehenkyi-training` became `barthu-method-ylehenkyi-trainin`
and every GitHub API call for that project 404'd. The wrong name was persisted to
`projects.github_repo_name` at creation, so the damage outlived the request.

Note ruff's B005 does NOT catch this — it only fires when the strip argument has
duplicate characters (`.rstrip(".gitt")`), so these tests are the guard.
"""

from __future__ import annotations

import aiosqlite
import pytest

from orchestrator.config import settings
from orchestrator.orchestrator import _parse_repo_url
from tests.test_project_ownership import as_user, client  # noqa: F401  (fixture)


@pytest.mark.parametrize(
    "url,expected",
    [
        # The production regression that surfaced this.
        (
            "https://github.com/ylehenkyi-qc/barthu-method-ylehenkyi-training",
            ("ylehenkyi-qc", "barthu-method-ylehenkyi-training"),
        ),
        # One per character in the old strip set, in trailing position.
        ("https://github.com/acme/toolkit", ("acme", "toolkit")),  # t -> was "toolk"
        ("https://github.com/acme/orbit", ("acme", "orbit")),      # t -> was "orb"
        ("https://github.com/acme/summit", ("acme", "summit")),    # t -> was "summ"
        ("https://github.com/acme/digit", ("acme", "digit")),      # t -> was "d"
        ("https://github.com/acme/log", ("acme", "log")),          # g -> was "lo"
        ("https://github.com/acme/api", ("acme", "api")),          # i -> was "ap"
        ("https://github.com/acme/git", ("acme", "git")),          # all -> was ""
        ("https://github.com/acme/v1.", ("acme", "v1.")),          # . -> was "v1"
        # A real `.git` suffix is still removed — exactly once.
        ("https://github.com/acme/toolkit.git", ("acme", "toolkit")),
        ("https://github.com/acme/toolkit.git.git", ("acme", "toolkit.git")),
        # Scheme-less and shorthand forms.
        ("github.com/acme/toolkit", ("acme", "toolkit")),
        ("acme/toolkit", ("acme", "toolkit")),
        ("acme/toolkit.git", ("acme", "toolkit")),
        # Trailing path/query must not be absorbed into the name.
        ("https://github.com/acme/toolkit/issues", ("acme", "toolkit")),
        ("https://github.com/acme/toolkit?tab=readme", ("acme", "toolkit")),
        # Degenerate: a bare ".git" name would have built /repos/acme//issues.
        ("https://github.com/acme/.git", None),
        # Non-matches.
        ("toolkit", None),
        ("", None),
    ],
)
def test_parse_repo_url(url: str, expected: tuple[str, str] | None) -> None:
    assert _parse_repo_url(url) == expected


@pytest.mark.parametrize(
    "name",
    ["toolkit", "orbit", "digit", "summit", "log", "api", "git", "training",
     "a", "t", "gg", "x.y", "repo-1"],
)
def test_repo_name_round_trips(name: str) -> None:
    """The generic invariant that would have caught this without anyone thinking
    of `.git`: a name with no `.git` suffix must survive parsing unchanged."""
    assert _parse_repo_url(f"https://github.com/acme/{name}") == ("acme", name)
    assert _parse_repo_url(f"https://github.com/acme/{name}.git") == ("acme", name)


async def test_created_project_persists_the_full_repo_name(client) -> None:  # noqa: ANN001, F811
    """The parser is pure but the damage was durable — assert the stored column,
    not the response body. `POST /api/projects` is the only writer of
    `projects.github_repo_name`, and nothing re-derives it afterwards."""
    as_user("alice@example.com", "sub-alice")
    res = client.post(
        "/api/projects",
        json={
            "name": "Training",
            "repo_url": "https://github.com/ylehenkyi-qc/barthu-method-ylehenkyi-training",
        },
    )
    assert res.status_code == 200, res.text

    async with aiosqlite.connect(settings.sqlite_db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT github_repo_owner, github_repo_name FROM projects WHERE id = ?",
            (res.json()["id"],),
        )
        row = await cursor.fetchone()

    assert row is not None
    assert row["github_repo_owner"] == "ylehenkyi-qc"
    assert row["github_repo_name"] == "barthu-method-ylehenkyi-training"
