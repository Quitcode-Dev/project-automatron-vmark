"""FastAPI application entry point."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

logging.getLogger("orchestrator").setLevel(logging.INFO)

import socketio
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from orchestrator.api.routes import router as api_router
from orchestrator.api.webhook_github import router as webhook_router
from orchestrator.api.socket_server import sio
from orchestrator.auth import require_auth
from orchestrator.config import settings
from orchestrator.models.project import init_db

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application startup / shutdown lifecycle."""
    logger.info("Automatron Orchestrator starting up...")
    await init_db(settings.sqlite_db_path)
    logger.info("Database initialized at %s", settings.sqlite_db_path)
    yield
    logger.info("Automatron Orchestrator shutting down...")


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="Automatron Orchestrator",
        version="0.1.0",
        description="Autonomous software development engine",
        lifespan=lifespan,
    )

    # CORS. The frontend calls the API with credentials (the Auth.js cookie), and
    # browsers reject a wildcard `Access-Control-Allow-Origin` on credentialed
    # requests — so we echo concrete origins instead of "*". Localhost (any port)
    # is allowed via regex for cross-origin local dev; the public URL and any
    # configured extras are allowed explicitly. Production is same-origin behind
    # Traefik, so CORS mostly matters for local dev.
    _cors_origins = [o.strip() for o in settings.cors_allow_origins.split(",") if o.strip()]
    if settings.automatron_public_url:
        _cors_origins.append(settings.automatron_public_url.rstrip("/"))
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # REST API routes — auth required for everything in api_router; webhook_router
    # uses its own HMAC signature check (skip OAuth there) and must stay reachable
    # for GitHub-originated traffic with no cookies.
    app.include_router(api_router, prefix="/api", dependencies=[Depends(require_auth)])
    app.include_router(webhook_router, prefix="/api")

    # Health endpoint
    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


# --- ASGI app with Socket.IO ---
fastapi_app = create_app()
combined_app = socketio.ASGIApp(sio, other_asgi_app=fastapi_app)
app = combined_app

# Register Socket.IO event handlers.
from orchestrator.api import websocket as _websocket  # noqa: F401,E402

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "orchestrator.main:combined_app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
    )
