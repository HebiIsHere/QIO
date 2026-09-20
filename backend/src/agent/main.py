"""Backend entrypoint: builds the HTTP + SSE app and serves it."""

from __future__ import annotations

import logging

import uvicorn
from fastapi import FastAPI

from agent.api.server import create_app as build_app
from agent.config import Settings
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations


def create_app() -> FastAPI:
    """Factory for `uvicorn agent.main:create_app --factory`."""
    settings = Settings()
    settings.ensure_dirs()
    conn = connect(settings.db_path)
    apply_migrations(conn)
    app = build_app(settings, conn)
    app.state.settings = settings
    _announce_auth(app.state.auth, settings)
    return app


def _announce_auth(auth, settings: Settings) -> None:
    """把安全模式讲清楚（绝不打印令牌本身）。"""
    logger = logging.getLogger("agent.main")
    if not auth.enabled:
        logger.warning(
            "local API authentication is DISABLED (QIO_DEV_INSECURE=1): "
            "any local process can call the QIO API. Development only."
        )
        return
    if auth.generated:
        where = f"; token written to {settings.session_token_file}" if settings.session_token_file else ""
        logger.warning(
            "no QIO_SESSION_TOKEN configured: generated a per-process session token%s "
            "(set QIO_SESSION_TOKEN, or run the dev script, to connect a frontend)",
            where,
        )


def main() -> None:
    settings = Settings()
    logging.basicConfig(level=settings.log_level)
    uvicorn.run(create_app(), host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
