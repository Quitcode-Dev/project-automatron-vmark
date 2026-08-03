"""Auth.js (NextAuth v5) session JWT verification for FastAPI.

Auth.js issues encrypted JWTs (JWE, A256CBC-HS512) keyed by AUTH_SECRET via HKDF
with the salt `"Auth.js Generated Encryption Key (authjs.session-token)"`. We
decrypt the same way and validate `exp` + the email allowlist.

The web-ui (Next.js + next-auth@beta) and this module MUST share AUTH_SECRET and
AUTOMATRON_ALLOWED_EMAILS via environment variables.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
from typing import Any

from fastapi import Depends, HTTPException, Request, status
from jose import jwe

from orchestrator.config import settings

logger = logging.getLogger(__name__)

# Auth.js v5 cookie names — production has __Secure- prefix, dev does not
_COOKIE_NAMES = (
    "__Secure-authjs.session-token",
    "authjs.session-token",
    # NextAuth v4 names for backwards compat if someone hasn't upgraded
    "__Secure-next-auth.session-token",
    "next-auth.session-token",
)


def _hkdf_sha256(secret: bytes, salt: bytes, info: bytes, length: int = 64) -> bytes:
    """RFC 5869 HKDF-Extract + HKDF-Expand with SHA-256, returning `length` bytes.

    Auth.js uses HKDF-SHA256 → 64 bytes for A256CBC-HS512 (32-byte HMAC key +
    32-byte AES-256-CBC key concatenated).
    """
    # Extract
    prk = hmac.new(salt, secret, hashlib.sha256).digest()
    # Expand
    output = b""
    last_block = b""
    counter = 1
    while len(output) < length:
        last_block = hmac.new(
            prk, last_block + info + bytes([counter]), hashlib.sha256
        ).digest()
        output += last_block
        counter += 1
    return output[:length]


def _derive_key(secret: str, cookie_name: str) -> bytes:
    """Match Auth.js v5's getDerivedEncryptionKey: HKDF-SHA256 with
        salt = cookie_name
        info = f"Auth.js Generated Encryption Key ({cookie_name})"
    See packages/core/src/jwt.ts in next-auth.
    """
    salt = cookie_name.encode()
    info = f"Auth.js Generated Encryption Key ({cookie_name})".encode()
    return _hkdf_sha256(secret.encode(), salt, info, length=64)


def decode_authjs_jwt(token: str, cookie_name: str) -> dict[str, Any] | None:
    """Decrypt the Auth.js session JWE. Returns the payload dict, or None on failure.

    The cookie name is part of the HKDF salt — `__Secure-` prefix in production
    means a different key, so the caller must pass the actual cookie name.
    """
    if not token or not settings.auth_secret:
        return None
    try:
        key = _derive_key(settings.auth_secret, cookie_name)
        decrypted = jwe.decrypt(token, key)
        if decrypted is None:
            return None
        return json.loads(decrypted)
    except Exception as exc:
        logger.debug("auth: JWE decrypt failed for cookie %s: %s", cookie_name, exc)
        return None


def _extract_token_from_cookies(cookies: dict[str, str]) -> tuple[str, str] | None:
    """Return (cookie_name, token_value) for the first matching session cookie."""
    for name in _COOKIE_NAMES:
        v = cookies.get(name)
        if v:
            return name, v
    return None


def _allowlisted(email: str) -> bool:
    """Check the email against AUTOMATRON_ALLOWED_EMAILS rules.

    Each comma-separated rule is either:
      - A full email address (exact match), e.g. `dev@quitcode.com`
      - A domain pattern starting with `@`, e.g. `@quitcode.com` matches any
        email at that domain.

    Empty list means the gate is delegated to Google (OAuth consent screen →
    Internal Workspace app, or Test users list).
    """
    rules = [r.strip().lower() for r in settings.automatron_allowed_emails.split(",") if r.strip()]
    if not rules:
        return True
    email_lower = (email or "").lower()
    for rule in rules:
        if rule.startswith("@"):
            if email_lower.endswith(rule):
                return True
        elif email_lower == rule:
            return True
    return False


def email_can_sign_in(email: str) -> bool:
    """Public view of the sign-in allowlist — used to reject sharing a project
    with someone who could never sign in to see it."""
    return _allowlisted(email)


def _is_auth_configured() -> bool:
    """Auth is wired up as soon as we have a secret to decrypt the JWT. The
    email allowlist is optional — Google Cloud Console can gate sign-in itself."""
    return bool(settings.auth_secret)


def is_admin(email: str) -> bool:
    """Admins see and act on every project. Matching follows the same rules as
    the sign-in allowlist: a full email, or a `@domain` pattern."""
    if settings.automatron_dev_no_auth:
        return True
    rules = [r.strip().lower() for r in settings.automatron_admin_emails.split(",") if r.strip()]
    email_lower = (email or "").lower()
    for rule in rules:
        if rule.startswith("@"):
            if email_lower.endswith(rule):
                return True
        elif email_lower == rule:
            return True
    return False


async def require_auth(request: Request) -> dict[str, Any]:
    """FastAPI dependency. Validate the Auth.js session cookie or raise 401.

    Returns the session payload (typically `{"sub": ..., "email": ..., "name": ..., "exp": ...}`).
    """
    if settings.automatron_dev_no_auth:
        return {"email": "dev@localhost", "name": "Dev User", "sub": "dev"}

    if not _is_auth_configured():
        # Auth is not configured yet — fail closed in prod, pass in dev
        if settings.debug:
            logger.warning("auth: AUTH_SECRET / AUTOMATRON_ALLOWED_EMAILS not set; allowing request in debug mode")
            return {"email": "unconfigured@localhost", "name": "Unconfigured", "sub": "anon"}
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Auth not configured")

    cookie_pair = _extract_token_from_cookies(request.cookies)
    if not cookie_pair:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    cookie_name, token = cookie_pair

    payload = decode_authjs_jwt(token, cookie_name)
    if not payload:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid session")

    email = payload.get("email", "")
    if not _allowlisted(email):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Email not allowlisted")

    return payload


async def current_user(request: Request) -> dict[str, Any]:
    """FastAPI dependency. The signed-in user as a local `users` row.

    Upserts on every request so a first-time signer-in gets a row (and an
    invited placeholder row gets linked to their account) without a separate
    registration step. Cached on `request.state` — the router-level access check
    and the route body both want it.
    """
    cached = getattr(request.state, "automatron_user", None)
    if cached is not None:
        return cached  # type: ignore[no-any-return]

    session = await require_auth(request)
    email = session.get("email") or ""
    if not email:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session has no email")

    from orchestrator.models.user import upsert_user

    user = await upsert_user(
        sub=session.get("sub"),
        email=email,
        name=session.get("name"),
        image=session.get("picture") or session.get("image"),
    )
    user = {**user, "is_admin": is_admin(email)}
    request.state.automatron_user = user
    return user


# Path params that identify a resource *belonging* to a project, and the table to
# resolve them through. Without these, `/deployment-targets/{target_id}` and friends
# would be a way around the `{project_id}` check — same hole, different id.
_PROJECT_SCOPED_PARAMS: tuple[tuple[str, str], ...] = (
    ("target_id", "deployment_targets"),
    ("plan_id", "deployment_plans"),
    ("run_id", "deploy_runs"),
)


async def _resolve_project_id(request: Request) -> str | None:
    """The project this request is about, from whichever id the path carries."""
    project_id = request.path_params.get("project_id")
    if project_id:
        return str(project_id)

    import aiosqlite

    from orchestrator.models import project as project_model

    for param, table in _PROJECT_SCOPED_PARAMS:
        resource_id = request.path_params.get(param)
        if not resource_id:
            continue
        if not project_model._db_path:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Database not initialized. Server startup has not completed.",
            )
        async with aiosqlite.connect(project_model._db_path) as db:
            cursor = await db.execute(
                f"SELECT project_id FROM {table} WHERE id = ?",  # table name is from the tuple above
                (resource_id,),
            )
            row = await cursor.fetchone()
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        return str(row[0])

    return None


async def require_project_access(request: Request) -> None:
    """Router-level dependency: enforce ownership on every project-scoped route.

    Registered once on `include_router`, so it covers all ~35 `{project_id}`
    endpoints plus the deployment resources that hang off a project — including
    any added later — instead of relying on each route to remember. It runs after
    routing, so `path_params` is populated; requests that name no project (list,
    create, LLM catalog) pass through.

    Unauthorized access returns 404, not 403: whether a project id exists is
    itself information the requester is not entitled to.
    """
    project_id = await _resolve_project_id(request)
    if not project_id:
        return

    from orchestrator.models.project import get_project
    from orchestrator.models.user import is_project_member

    user = await current_user(request)
    project = await get_project(project_id)
    if not project:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    if user.get("is_admin") or project.get("owner_id") == user["id"]:
        return
    if await is_project_member(project_id, user["id"]):
        return

    logger.warning(
        "auth: %s denied access to project %s (owner=%s)",
        user.get("email"),
        project_id,
        project.get("owner_id"),
    )
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")


async def require_project_owner(request: Request, project_id: str) -> dict[str, Any]:
    """Stricter check for destructive / sharing operations: owner or admin only.

    Collaborators can drive a shared project but cannot delete it or change who
    else it is shared with. Returns the project.
    """
    from orchestrator.models.project import get_project

    user = await current_user(request)
    project = await get_project(project_id)
    if not project:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    if user.get("is_admin") or project.get("owner_id") == user["id"]:
        return project
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the project owner can do this")


async def require_owned_project(request: Request) -> dict[str, Any]:
    """Dependency form of `require_project_owner`, keyed off the `{project_id}` path param."""
    project_id = request.path_params.get("project_id")
    if not project_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return await require_project_owner(request, project_id)


def viewer_role(project: dict[str, Any], user: dict[str, Any]) -> str:
    """How the requesting user relates to this project — drives what the UI offers."""
    from orchestrator.models.user import ROLE_ADMIN, ROLE_COLLABORATOR, ROLE_OWNER

    if project.get("owner_id") == user.get("id"):
        return ROLE_OWNER
    if user.get("is_admin"):
        return ROLE_ADMIN
    return ROLE_COLLABORATOR


def authenticate_socketio_environ(environ: dict[str, Any]) -> dict[str, Any] | None:
    """Validate the cookie on a Socket.IO connect handshake. Returns None to reject.

    Used in api/websocket.py's on_connect handler. Returning None tells Socket.IO
    to refuse the connection.
    """
    if settings.automatron_dev_no_auth:
        return {"email": "dev@localhost", "sub": "dev"}
    if not _is_auth_configured():
        if settings.debug:
            return {"email": "unconfigured@localhost", "sub": "anon"}
        return None

    # Parse cookies out of the WSGI/ASGI environ
    raw_cookie = environ.get("HTTP_COOKIE", "")
    cookies: dict[str, str] = {}
    for pair in raw_cookie.split(";"):
        pair = pair.strip()
        if "=" in pair:
            k, v = pair.split("=", 1)
            cookies[k.strip()] = v.strip()

    cookie_pair = _extract_token_from_cookies(cookies)
    if not cookie_pair:
        return None
    cookie_name, token = cookie_pair
    payload = decode_authjs_jwt(token, cookie_name)
    if not payload:
        return None
    if not _allowlisted(payload.get("email", "")):
        return None
    return payload
