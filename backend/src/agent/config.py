"""Application configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

APP_NAME = "smart-agent"
DEFAULT_PORT = 8734


def default_data_dir() -> Path:
    base = os.environ.get("APPDATA")
    if base:
        return Path(base) / APP_NAME
    return Path.home() / f".{APP_NAME}"


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    return Path(raw) if raw else default


@dataclass
class Settings:
    """Runtime settings. Data lives in APPDATA/smart-agent by default."""

    data_dir: Path = field(default_factory=default_data_dir)
    host: str = field(default_factory=lambda: os.environ.get("SMART_AGENT_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(os.environ.get("SMART_AGENT_PORT", DEFAULT_PORT)))
    log_level: str = field(default_factory=lambda: os.environ.get("SMART_AGENT_LOG", "INFO"))

    def __post_init__(self) -> None:
        override = os.environ.get("SMART_AGENT_DATA_DIR")
        if override:
            self.data_dir = Path(override)

    @property
    def db_path(self) -> Path:
        return _env_path("SMART_AGENT_DB", self.data_dir / "app.db")

    @property
    def archive_dir(self) -> Path:
        return _env_path("SMART_AGENT_ARCHIVE", self.data_dir / "archive")

    @property
    def config_path(self) -> Path:
        return self.data_dir / "config.toml"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    def ensure_dirs(self) -> None:
        for p in (self.data_dir, self.archive_dir, self.log_dir):
            p.mkdir(parents=True, exist_ok=True)