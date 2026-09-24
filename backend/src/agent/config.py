"""Application configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

APP_NAME = "qio"
DEFAULT_PORT = 8734

# 只有 QIO 自己的 WebView 才算「正式 origin」。
# Tauri v2 在 Windows 用 http://tauri.localhost，在 macOS/Linux 用 tauri://localhost。
PRODUCTION_ORIGINS = (
    "tauri://localhost",
    "http://tauri.localhost",
    "https://tauri.localhost",
)
# 开发模式额外允许本机 dev server（127.0.0.1 / localhost 的任意端口）。
DEV_ORIGINS = (
    "http://localhost:1420",
    "http://127.0.0.1:1420",
    "http://localhost:5199",
    "http://127.0.0.1:5199",
)


def default_data_dir() -> Path:
    base = os.environ.get("APPDATA")
    if base:
        return Path(base) / APP_NAME
    return Path.home() / f".{APP_NAME}"


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    return Path(raw) if raw else default


def _env_path_or_none(name: str) -> Path | None:
    raw = os.environ.get(name)
    return Path(raw) if raw else None


def _extra_origins() -> tuple[str, ...]:
    raw = os.environ.get("QIO_ALLOWED_ORIGINS", "")
    return tuple(o.strip() for o in raw.split(",") if o.strip())


@dataclass
class Settings:
    """Runtime settings. Data lives in APPDATA/qio by default."""

    data_dir: Path = field(default_factory=default_data_dir)
    host: str = field(default_factory=lambda: os.environ.get("QIO_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(os.environ.get("QIO_PORT", DEFAULT_PORT)))
    log_level: str = field(default_factory=lambda: os.environ.get("QIO_LOG", "INFO"))
    # 会话令牌：Tauri 壳（或 dev 脚本）生成并注入。256 bit 熵，不持久化。
    session_token: str = field(default_factory=lambda: os.environ.get("QIO_SESSION_TOKEN", ""))
    # 由 Tauri 壳指定：后端把最终使用的令牌写到这里，壳读出来交给 WebView。
    session_token_file: Path | None = field(
        default_factory=lambda: _env_path_or_none("QIO_SESSION_TOKEN_FILE")
    )
    # 显式开发豁免：只有明确设置 QIO_DEV_INSECURE=1 才允许无令牌访问本机 API。
    dev_insecure: bool = field(
        default_factory=lambda: os.environ.get("QIO_DEV_INSECURE", "") == "1"
    )
    # 测试事件注入口（QA 脚本用）。默认关闭 → 路由根本不注册，而不是运行期判断。
    test_events: bool = field(
        default_factory=lambda: os.environ.get("QIO_ENABLE_TEST_EVENTS", "") == "1"
    )
    extra_origins: tuple[str, ...] = field(default_factory=_extra_origins)

    def __post_init__(self) -> None:
        override = os.environ.get("QIO_DATA_DIR")
        if override:
            self.data_dir = Path(override)

    @property
    def allowed_origins(self) -> tuple[str, ...]:
        """允许的 WebView origin：正式 Tauri origin + 显式配置 + （仅开发）本机 dev server。"""
        origins = list(PRODUCTION_ORIGINS)
        origins.extend(self.extra_origins)
        if self.dev_insecure:
            origins.extend(DEV_ORIGINS)
        return tuple(dict.fromkeys(origins))

    @property
    def auth_required(self) -> bool:
        """令牌存在 → 强制；否则只有显式 dev 豁免才关闭（fail-closed）。"""
        return bool(self.session_token) or not self.dev_insecure

    @property
    def db_path(self) -> Path:
        return _env_path("QIO_DB", self.data_dir / "app.db")

    @property
    def archive_dir(self) -> Path:
        return _env_path("QIO_ARCHIVE", self.data_dir / "archive")

    @property
    def config_path(self) -> Path:
        return self.data_dir / "config.toml"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def workspace_dir(self) -> Path:
        """电脑操控的默认工作区根目录（`computer.root_dir` 留空时用它）。"""
        return self.data_dir / "workspace"

    def ensure_dirs(self) -> None:
        # 工作区根目录必须一起建：设置页写着「留空则用默认工作区」，而文件工具
        # 的相对路径都以它为准 —— 它不存在时所有相对路径的调用都会报路径错误。
        for p in (self.data_dir, self.archive_dir, self.log_dir, self.workspace_dir):
            p.mkdir(parents=True, exist_ok=True)
