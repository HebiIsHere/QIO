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
    return app


def main() -> None:
    settings = Settings()
    logging.basicConfig(level=settings.log_level)
    uvicorn.run(create_app(), host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()