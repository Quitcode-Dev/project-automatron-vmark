"""Decide whether a project's intake implies it needs a database.

Only consulted when the project has no Supabase credentials, to choose between
two paths that are both correct for different projects:

  - no database needed  -> scaffold strips Supabase, planning proceeds
  - database needed     -> Automatron provisions one

Deliberately conservative, in the same spirit as
`scaffolding._detect_framework`: when the signals are absent we prefer NOT
provisioning. A project that turns out to need a database fails with a clear,
actionable planning error, whereas one that gets a database it never wanted
silently burns a container and a schema nobody asked for.
"""

from __future__ import annotations

import re

# Nouns that only appear when something is being stored and read back.
_PERSISTENCE_SIGNALS = (
    "database",
    "postgres",
    "supabase",
    "sql",
    "schema",
    "migration",
    "persist",
    "store data",
    "stored data",
    "data model",
    "crud",
    "user accounts",
    "user account",
    "sign up",
    "signup",
    "sign-up",
    "log in",
    "login",
    "authentication",
    "auth",
    "dashboard",
    "admin panel",
    "records",
    "inventory",
    "orders",
    "bookings",
    "appointments",
    "submissions",
    "comments",
    "profiles",
)

# Phrases that positively indicate no data layer. Checked first: a landing page
# that mentions a "login link to our other product" must not trip the signals
# above.
_STATIC_SIGNALS = (
    "static site",
    "static website",
    "landing page",
    "brochure site",
    "marketing site",
    "no database",
    "no backend",
    "purely frontend",
    "front-end only",
    "frontend only",
)


def _contains(haystack: str, needles: tuple[str, ...]) -> list[str]:
    found = []
    for needle in needles:
        # Word-boundary match so "auth" doesn't fire on "author" and "sql"
        # doesn't fire on "mysqldump" mentioned in passing.
        if re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", haystack):
            found.append(needle)
    return found


def needs_database(intake_text: str, readme: str = "") -> tuple[bool, str]:
    """Return `(needs_db, reason)` for a project with no Supabase credentials."""
    haystack = f"{intake_text}\n{readme}".lower()

    static_hits = _contains(haystack, _STATIC_SIGNALS)
    if static_hits:
        return False, f"intake describes a site with no data layer ({', '.join(static_hits)})"

    hits = _contains(haystack, _PERSISTENCE_SIGNALS)
    if hits:
        shown = ", ".join(hits[:5])
        return True, f"intake mentions persistence ({shown})"

    return False, "no persistence signals found in intake or README"
