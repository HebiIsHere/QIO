"""Backend entrypoint: builds the HTTP + SSE app and serves it.

同一个可执行文件承担两件事，靠命令行参数分流：

* 默认：启动后端服务（uvicorn + 数据库迁移 + 应用维护任务）。
* `--tool-worker`：只执行一个工具请求，然后退出。

分流的**顺序**是这里的关键：worker 分支必须在加载 uvicorn、FastAPI、数据库、
凭据之前返回。所以本模块顶层只 import 标准库，其余全部放进函数 —— 否则
「worker 模式不加载后端服务」这条承诺在冻结产物里就不成立
（`tests/test_entrypoint_split.py` 用真子进程验证这一点）。
"""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 只用于标注：运行期不加载
    from fastapi import FastAPI

    from agent.config import Settings

TOOL_WORKER_FLAG = "--tool-worker"


def is_tool_worker(argv: list[str] | None = None) -> bool:
    """是否进入工具 worker 模式（只有显式传入该参数才算）。"""
    return TOOL_WORKER_FLAG in (argv if argv is not None else sys.argv[1:])


def create_app() -> "FastAPI":
    """Factory for `uvicorn agent.main:create_app --factory`."""
    from agent.api.server import create_app as build_app
    from agent.config import Settings
    from agent.storage.db import connect
    from agent.storage.migrate import apply_migrations

    settings = Settings()
    settings.ensure_dirs()
    conn = connect(settings.db_path)
    apply_migrations(conn)
    # 真实进程入口：由 lifespan 在最后一步关闭 DB（测试自己管 fixture 的连接）。
    app = build_app(settings, conn, close_db_on_shutdown=True)
    app.state.settings = settings
    _announce_auth(app.state.auth, settings)
    return app


def _announce_auth(auth, settings: "Settings") -> None:
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


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    if is_tool_worker(args):
        # 分流点：到这里为止没有加载任何后端服务 / 数据库 / 凭据。
        from agent import tool_worker

        raise SystemExit(tool_worker.main(args))

    import uvicorn

    from agent.config import Settings

    settings = Settings()
    logging.basicConfig(level=settings.log_level)
    uvicorn.run(create_app(), host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
