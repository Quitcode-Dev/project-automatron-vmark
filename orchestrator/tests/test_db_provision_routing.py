"""Routing between the two no-credentials outcomes, and the prompt contract.

With no Supabase credentials a project goes down one of two paths — scaffold
without a data layer, or get one provisioned. `needs_database` picks. It is
deliberately biased toward NOT provisioning: a missing database surfaces as a
clear planning error, while an unwanted one silently costs a container and a
schema nobody asked for.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.db_provision.detect import needs_database
from orchestrator.orchestrator import _parse_tagged_block

PROMPT = Path(__file__).parent.parent / "prompts" / "architect_github_v1.txt"


@pytest.mark.parametrize(
    "intake",
    [
        "A CRM where staff log in and track donor records",
        "Needs a Postgres database for orders and inventory",
        "Users sign up, create profiles, and leave comments",
        "Admin panel for managing bookings",
        "Store submissions from the contact form",
    ],
)
def test_persistence_intents_are_detected(intake: str) -> None:
    wants, why = needs_database(intake)

    assert wants is True, why


@pytest.mark.parametrize(
    "intake",
    [
        "A static marketing site for our consultancy",
        "Landing page with a hero, features and a pricing table",
        "Brochure site, no backend needed",
        "A portfolio of past work with an about page",
        "Rebuild our homepage in Next.js with nicer animations",
    ],
)
def test_static_sites_are_not_given_a_database(intake: str) -> None:
    wants, why = needs_database(intake)

    assert wants is False, why


def test_explicit_denial_beats_incidental_keywords() -> None:
    """A landing page that merely mentions a login elsewhere must stay static."""
    wants, _ = needs_database(
        "Static landing page. No database. Has a login link to our main product."
    )

    assert wants is False


def test_word_boundaries_avoid_false_positives() -> None:
    """'auth' must not fire on 'author', 'sql' must not fire on 'mysqldump'."""
    wants, why = needs_database("A blog listing each author's published essays")

    assert wants is False, why


def test_ambiguous_intake_defaults_to_no_database() -> None:
    wants, why = needs_database("Make something nice for our team")

    assert wants is False
    assert "no persistence signals" in why


# --- Prompt contract -------------------------------------------------------


def test_prompt_defines_the_conditional_migration_block() -> None:
    text = PROMPT.read_text()

    assert "```sql:migration" in text
    assert "## Provisioned Database" in text
    # The conditionality has to be explicit, or the architect emits SQL for
    # user-owned Supabase projects where migrations are forbidden.
    assert "CONDITIONAL" in text


def test_prompt_keeps_user_owned_schema_rules_intact() -> None:
    """Rules 15-17 must still bind for projects with user-supplied Supabase."""
    text = PROMPT.read_text()

    assert "Do NOT generate `supabase/migrations/*.sql` files when a live schema is supplied" in text
    assert "User action required: add column X to table Y in Supabase" in text
    # ...and the override must be scoped to the provisioned case only.
    assert "ownership is inverted" in text


def test_migration_block_round_trips_through_the_parser() -> None:
    """The orchestrator extracts Block 4 with the same helper as the others."""
    response = (
        "preamble\n"
        "```json:issue_plan\n{}\n```\n"
        "```sql:migration\nCREATE TABLE donors (id uuid PRIMARY KEY);\n```\n"
    )

    assert _parse_tagged_block(response, "sql:migration") == (
        "CREATE TABLE donors (id uuid PRIMARY KEY);"
    )


def test_absent_migration_block_parses_as_none() -> None:
    """Drives the abort path when a provisioned project gets no schema."""
    assert _parse_tagged_block("```json:issue_plan\n{}\n```", "sql:migration") is None


# --- Recovering a half-provisioned project ---------------------------------
#
# Provisioning persists credentials BEFORE a schema exists. If the run then
# fails (no migration emitted, bad SQL, verification failure), a re-run would
# skip provisioning because credentials are present, fail introspection because
# the database is empty, and abort at the gate — telling the user to fix
# credentials for a database Automatron created. These pin the escape hatch.


def _is_resumable(url: str, base_domain: str, schema_ok: bool | None) -> bool:
    """Mirror of the orchestrator's resume condition, kept in one place."""
    return bool(
        url
        and base_domain
        and url.endswith(f".{base_domain}")
        and schema_ok is False
    )


def test_own_empty_database_is_resumable() -> None:
    assert _is_resumable("https://db-abc.db.example.com", "db.example.com", False) is True


def test_own_populated_database_is_not_resumable() -> None:
    """A working provisioned project must take the ordinary path."""
    assert _is_resumable("https://db-abc.db.example.com", "db.example.com", True) is False


def test_user_supplied_supabase_is_never_resumable() -> None:
    """We must never try to write a schema into a database the user owns."""
    assert _is_resumable("https://xyz.supabase.co", "db.example.com", False) is False


def test_nothing_is_resumable_without_a_base_domain() -> None:
    """With provisioning unconfigured there is no such thing as "our" database."""
    assert _is_resumable("https://xyz.supabase.co", "", False) is False


# --- Post-migration introspection retry ------------------------------------
#
# `NOTIFY pgrst, 'reload schema'` is fire-and-forget. Introspecting immediately
# after a migration sees PostgREST's OLD cache and reports an empty database,
# which aborted the whole planning run. Caught only by an end-to-end run: the
# live test had its wait in the test rather than in the code.


async def test_introspection_retries_until_the_cache_catches_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import orchestrator.orchestrator as orch

    calls = {"n": 0}

    async def flaky(url: str, key: str):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] < 3:
            return orch.SupabaseSchema(ok=False, error="No tables exposed via PostgREST")
        return orch.SupabaseSchema(ok=True, tables={"donors": ["id"]})

    monkeypatch.setattr(orch, "_introspect_supabase_schema", flaky)
    monkeypatch.setattr(orch.asyncio, "sleep", _instant_sleep)

    result = await orch._introspect_after_migration("https://x", "key")

    assert result.ok is True
    assert calls["n"] == 3


async def test_introspection_gives_up_and_reports_the_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A genuinely broken migration must still surface, not hang forever."""
    import orchestrator.orchestrator as orch

    calls = {"n": 0}

    async def always_empty(url: str, key: str):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        return orch.SupabaseSchema(ok=False, error="No tables exposed via PostgREST")

    monkeypatch.setattr(orch, "_introspect_supabase_schema", always_empty)
    monkeypatch.setattr(orch.asyncio, "sleep", _instant_sleep)

    result = await orch._introspect_after_migration("https://x", "key", attempts=4)

    assert result.ok is False
    assert "No tables exposed" in result.error
    assert calls["n"] == 4


async def _instant_sleep(seconds: float) -> None:
    """Keep the retry tests fast without weakening what they assert."""
    return None
