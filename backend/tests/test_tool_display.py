"""工具展示名与用户可见文案：界面里出现的中文，不出现英文工具名。"""

from __future__ import annotations

import pytest

from agent.adapters.base import ToolCall
from agent.api.bus import EventBus
from agent.config import Settings
from agent.services.app import AppContext
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations
from agent.tools.base import ToolResult
from agent.tools.display import TOOL_LABELS, tool_label


@pytest.fixture()
def ctx(tmp_path) -> AppContext:
    conn = connect(tmp_path / "app.db")
    apply_migrations(conn)
    return AppContext(Settings(data_dir=tmp_path), conn, EventBus())


def test_every_registered_builtin_tool_has_chinese_label(ctx: AppContext):
    names = {s.name for s in ctx.registry.specs()}
    missing = sorted(n for n in names if n not in TOOL_LABELS)
    assert missing == [], f"这些内置工具还没有中文展示名：{missing}"


def test_tool_label_maps_web_search_to_chinese():
    assert tool_label("web_search") == "网络搜索"
    assert tool_label("memory_search") == "检索记忆"
    assert tool_label("fs_read") == "读取文件"
    # 未登记（例如 Agent 自建工具）回落原始名，不编造
    assert tool_label("my_custom_tool") == "my_custom_tool"


def test_presentation_title_is_chinese_and_keeps_raw_name(ctx: AppContext):
    tool = ctx.registry.get("web_search")
    assert tool is not None
    presentation = ctx.registry._present(
        tool,
        ToolCall(id="c1", name="web_search", arguments={"query": "x"}),
        ToolResult(ok=False, error="联网搜索暂不可用"),
    )
    assert presentation is not None
    assert presentation["title"] == "网络搜索"
    assert presentation["tool"] == "web_search"
    assert "web_search" not in presentation["title"]
