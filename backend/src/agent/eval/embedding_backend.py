"""评测用的真实嵌入后端装配（只服务 agent/eval，不进生产链路）。

评测要回答的问题是「内置模型在现有用例上得几分」，所以这里只做一件事：
按与生产同源的方式把 `OnnxEmbeddingBackend` 装起来。两个刻意的选择：

1. **内存库**：每次装配都用新的 `:memory:` 连接并跑迁移。评测既不碰用户的
   `app.db`，也不让上一次运行留下的向量影响这一次。
2. **每个用例一个新实例**：后端实例内部按话题 id / 文档 id 缓存向量，跨用例
   复用会把 A 用例算出的向量喂给 B 用例（不同用例里话题 id 会重复）。代价是
   每个用例重新加载一次 ONNX 会话 —— 用例只有几十条，这个代价可以接受。

模型找不到时返回 `None`，调用方退回原来的确定性路径，并在结果里说明原因。
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

from agent.config import Settings
from agent.storage.migrate import apply_migrations

MODEL_SUBDIR = "bge-small-zh-v1.5"


def resolve_model_dir(explicit: str | Path | None = None) -> Path:
    """模型目录：显式参数 > `QIO_MODELS_DIR` > 与生产同源的 `data_dir/models`。"""
    if explicit:
        return Path(explicit)
    base = os.environ.get("QIO_MODELS_DIR")
    root = Path(base) if base else Settings().data_dir / "models"
    return root / MODEL_SUBDIR


def _memory_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.isolation_level = None  # autocommit，与 storage/db.connect 一致
    conn.row_factory = sqlite3.Row
    apply_migrations(conn)
    return conn


def build_backend(model_dir: str | Path | None = None) -> tuple[Any | None, str]:
    """返回 `(嵌入后端, 说明)`；模型不可用时后端为 `None`，说明里写清找过哪里。"""
    from agent.selector.onnx import OnnxEmbeddingBackend

    directory = resolve_model_dir(model_dir)
    backend = OnnxEmbeddingBackend(_memory_conn(), model_dir=directory)
    if not backend.available():
        return None, f"嵌入模型不可用：{directory}（缺 model.onnx 或 tokenizer.json）"
    return backend, backend.model_identity
