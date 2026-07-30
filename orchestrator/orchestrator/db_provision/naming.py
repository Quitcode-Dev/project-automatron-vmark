"""Derived names for a project's provisioned database resources.

Every name here ends up inside a SQL identifier, a Docker container name, or a
Traefik router key — none of which can be parameterized. Sanitizing centrally
means the SQL builders in `postgres.py` can treat these as trusted.
"""

from __future__ import annotations

import re

_UNSAFE = re.compile(r"[^a-z0-9]")

# Postgres truncates identifiers at 63 bytes. Keeping the slug well under that
# leaves room for the prefixes below without silent collisions from truncation.
_SLUG_MAX = 32


def project_slug(project_id: str) -> str:
    """Reduce a project id to `[a-z0-9]+`, safe in every context we use.

    Raises ValueError on input that reduces to nothing — a caller passing an
    empty or entirely punctuation id is a bug, and silently inventing a name
    would provision resources nobody can trace back to a project.
    """
    slug = _UNSAFE.sub("", project_id.lower())[:_SLUG_MAX]
    if not slug:
        raise ValueError(f"project_id {project_id!r} has no usable identifier characters")
    return slug


def database_name(project_id: str) -> str:
    return f"proj_{project_slug(project_id)}"


def authenticator_role(project_id: str) -> str:
    """PostgREST's login role — one per project, so CONNECT can be scoped."""
    return f"auth_{project_slug(project_id)}"


def container_name(project_id: str) -> str:
    return f"automatron-db-{project_slug(project_id)}"


def route_slug(project_id: str) -> str:
    """Traefik router/middleware/service key for this project's API origin."""
    return f"db-{project_slug(project_id)}"


def api_host(project_id: str, base_domain: str) -> str:
    return f"{route_slug(project_id)}.{base_domain}"
