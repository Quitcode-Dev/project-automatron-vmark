"""The per-project PostgREST container and its Traefik route.

PostgREST serves its OpenAPI description and tables at the root path, while both
`@supabase/supabase-js` and `_introspect_supabase_schema` address them under
`/rest/v1/`. Traefik's stripprefix middleware is what reconciles the two, and it
is the reason a gateway is mandatory rather than a nicety: without it the origin
we hand back would 404 on every request the rest of the system makes.
"""

from __future__ import annotations

import logging
from typing import Any

from orchestrator.config import settings
from orchestrator.db_provision.naming import api_host, container_name, route_slug

logger = logging.getLogger(__name__)

# PostgREST's built-in listen port.
POSTGREST_PORT = 3000
# The path prefix Supabase clients use; stripped before reaching PostgREST.
REST_PREFIX = "/rest/v1"


def postgrest_environment(db_uri: str, jwt_secret: str) -> dict[str, str]:
    return {
        "PGRST_DB_URI": db_uri,
        "PGRST_DB_SCHEMAS": "public",
        "PGRST_DB_ANON_ROLE": "anon",
        "PGRST_JWT_SECRET": jwt_secret,
        # Report only what the requesting role may touch, so the OpenAPI
        # `definitions` that introspection parses match real privileges.
        "PGRST_OPENAPI_MODE": "follow-privileges",
        "PGRST_DB_USE_LEGACY_GUCS": "false",
    }


def traefik_labels(project_id: str, base_domain: str) -> dict[str, str]:
    slug = route_slug(project_id)
    host = api_host(project_id, base_domain)
    labels = {
        "traefik.enable": "true",
        f"traefik.http.routers.{slug}.rule": f"Host(`{host}`)",
        f"traefik.http.routers.{slug}.entrypoints": settings.preview_traefik_entrypoint,
        f"traefik.http.routers.{slug}.middlewares": f"{slug}-strip",
        f"traefik.http.middlewares.{slug}-strip.stripprefix.prefixes": REST_PREFIX,
        f"traefik.http.services.{slug}.loadbalancer.server.port": str(POSTGREST_PORT),
    }
    # An empty certresolver means the entrypoint terminates TLS elsewhere (or
    # not at all, on a plain-HTTP dev proxy). Emitting the label empty makes
    # Traefik reject the router outright, so omit it instead.
    if settings.preview_traefik_certresolver:
        labels[f"traefik.http.routers.{slug}.tls.certresolver"] = (
            settings.preview_traefik_certresolver
        )
    return labels


def _client() -> Any:
    import docker as docker_sdk

    return docker_sdk.from_env()


def stop_postgrest(project_id: str) -> None:
    """Remove the project's PostgREST container if it exists."""
    name = container_name(project_id)
    client = _client()
    try:
        try:
            existing = client.containers.get(name)
        except Exception:
            return
        existing.remove(force=True)
        logger.info("db_provision: removed container %s", name)
    finally:
        client.close()


def start_postgrest(project_id: str, db_uri: str, jwt_secret: str) -> str:
    """(Re)start this project's PostgREST container. Returns the container name."""
    name = container_name(project_id)
    stop_postgrest(project_id)

    client = _client()
    try:
        client.containers.run(
            settings.db_postgrest_image,
            detach=True,
            name=name,
            restart_policy={"Name": "unless-stopped"},
            environment=postgrest_environment(db_uri, jwt_secret),
            network=settings.preview_traefik_network,
            labels=traefik_labels(project_id, settings.db_base_domain),
        )
    finally:
        client.close()
    logger.info("db_provision: started PostgREST container %s", name)
    return name
