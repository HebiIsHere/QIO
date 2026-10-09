"""E 组 · M08 受控验收：`agent/services/settings_service.py` 的整套原子性。

验收对应（契约 §2 E / task-5）：
* 合法前字段 + 非法后字段 → 校验异常，**数据库与运行时都不变**；
* 中途写库失败 → 同样回滚，数据库保持整套旧值；
* 合法请求整套生效，且**提交成功之后**才应用运行时；
* 保留期限校验失败 → 不触发任何清理。

这些用例只依赖可观察行为：settings 表的行、异常类型、以及运行时钩子的调用次数与
调用时看到的值。不联网、不用真实 Key。
"""

from __future__ import annotations

import sqlite3

import pytest

from agent.services.settings_service import (
    SettingsService,
    SettingsValidationError,
    SettingsWriteError,
)
from agent.storage.settings import SettingsStore


class _CtxStub:
    """`SettingsService` 需要的最小上下文（与 `AppContext` 的同名能力一致）。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.settings_store = SettingsStore(conn)
        self.search_applied = 0
        self.prune_outputs_calls = 0
        self.prune_records_calls = 0
        #: 每次清理时看到的保留期（用来证明清理发生在提交之后）
        self.seen_output_days: list[int] = []
        self.seen_record_days: list[int] = []

    def apply_search_settings(self) -> None:
        self.search_applied += 1

    def prune_tool_outputs(self) -> int:
        self.prune_outputs_calls += 1
        self.seen_output_days.append(
            self.settings_store.get_int("tools.output_retention_days", -1)
        )
        return 3

    def prune_tool_records(self) -> int:
        self.prune_records_calls += 1
        self.seen_record_days.append(
            self.settings_store.get_int("tools.record_retention_days", -1)
        )
        return 1


@pytest.fixture()
def ctx(db_conn: sqlite3.Connection) -> _CtxStub:
    return _CtxStub(db_conn)


def _rows(conn: sqlite3.Connection) -> dict[str, str]:
    return {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM settings")}


def test_valid_prefix_plus_invalid_field_keeps_everything_unchanged(
    ctx: _CtxStub, db_conn: sqlite3.Connection
) -> None:
    """合法前字段 + 非法后字段：400 语义，库与运行时都不许动。"""
    before = _rows(db_conn)
    service = SettingsService(ctx)

    with pytest.raises(SettingsValidationError) as err:
        service.apply(
            {
                # 前面的 section 全部合法
                "memory": {"fragment_max_turns": 12},
                "loop": {"max_iterations": 64},
                "search": {"top_k_default": 8},
                # 最后一个字段非法：max_iterations 越界
                "maintenance": {"enabled": True, "interval_hours": 0},
            }
        )

    assert err.value.field == "maintenance.interval_hours"
    assert _rows(db_conn) == before  # 一个字节都没写
    assert ctx.search_applied == 0  # 运行时也没被应用
    assert ctx.prune_outputs_calls == 0
    assert ctx.prune_records_calls == 0


def test_unknown_section_and_unknown_field_are_rejected_before_writing(
    ctx: _CtxStub, db_conn: sqlite3.Connection
) -> None:
    service = SettingsService(ctx)
    before = _rows(db_conn)

    with pytest.raises(SettingsValidationError):
        service.apply({"memory": {"fragment_max_turns": 5}, "typo": {"x": 1}})
    with pytest.raises(SettingsValidationError):
        service.apply({"memory": {"fragment_max_turns": 5, "nope": 1}})

    assert _rows(db_conn) == before


def test_write_failure_mid_transaction_rolls_back_the_whole_patch(
    ctx: _CtxStub, db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """中途写库失败：前面的字段也必须回滚（不是半套新设置）。"""
    service = SettingsService(ctx)
    service.apply({"memory": {"fragment_max_turns": 7}})
    before = _rows(db_conn)

    real_set = SettingsStore.set
    calls = {"n": 0}

    def flaky_set(self: SettingsStore, key: str, value: str) -> None:
        calls["n"] += 1
        if calls["n"] == 2:  # 第二个字段写失败
            raise sqlite3.OperationalError("disk I/O error")
        real_set(self, key, value)

    monkeypatch.setattr(SettingsStore, "set", flaky_set)

    with pytest.raises(SettingsWriteError):
        service.apply(
            {
                "memory": {"fragment_max_turns": 9},
                "loop": {"max_iterations": 33},
                "maintenance": {"interval_hours": 6},
            }
        )

    assert calls["n"] == 2
    assert _rows(db_conn) == before  # 第一个字段也回滚了
    assert ctx.search_applied == 0
    assert ctx.prune_outputs_calls == 0


def test_valid_whole_patch_takes_effect_and_applies_runtime_after_commit(
    ctx: _CtxStub, db_conn: sqlite3.Connection
) -> None:
    """合法请求整套生效；搜索运行时与清理都发生在提交成功之后。"""
    service = SettingsService(ctx)

    result = service.apply(
        {
            "memory": {"fragment_max_turns": 4, "fragment_max_tokens": 5000},
            "loop": {"max_iterations": 20, "output_token_budget": 0},
            "maintenance": {"enabled": False, "interval_hours": 48},
            "tools": {"record_outputs": False, "output_retention_days": 7, "record_retention_days": 30},
            "search": {
                "searxng_url": "http://127.0.0.1:8888",
                "keyless_fallback": False,
                "top_k_default": 9,
                "max_fetch_chars": 20000,
            },
        }
    )

    rows = _rows(db_conn)
    assert rows["fragment.max_turns"] == "4"
    assert rows["fragment.max_tokens"] == "5000"
    assert rows["loop.max_iterations"] == "20"
    assert rows["loop.output_token_budget"] == "0"
    assert rows["maintenance.enabled"] == "false"
    assert rows["maintenance.interval_hours"] == "48"
    assert rows["tools.record_outputs"] == "0"
    assert rows["tools.output_retention_days"] == "7"
    assert rows["tools.record_retention_days"] == "30"
    assert rows["search.searxng_url"] == "http://127.0.0.1:8888"
    assert rows["search.top_k_default"] == "9"
    assert rows["search.max_fetch_chars"] == "20000"

    assert set(result.sections) == {"memory", "loop", "maintenance", "tools", "search"}
    assert result.sections["loop"] == {"max_iterations": 20, "output_token_budget": 0}
    assert result.sections["maintenance"] == {"enabled": False, "interval_hours": 48}
    assert result.sections["memory"] == {"fragment_max_turns": 4, "fragment_max_tokens": 5000}
    assert result.sections["tools"]["record_outputs"] is False
    assert result.sections["search"]["keyless_fallback"] is False

    # 运行时应用：搜索设置被套用；清理只做一次，且看到的是**提交之后**的保留期
    assert ctx.search_applied == 1
    assert ctx.prune_outputs_calls == 1
    assert ctx.prune_records_calls == 1
    assert ctx.seen_output_days == [7]
    assert ctx.seen_record_days == [30]

    # changed 只列真正变化的键
    assert "fragment.max_turns" in result.changed
    assert "search.max_fetch_chars" in result.changed


def test_partial_update_keeps_other_sections_and_reports_no_change_when_idempotent(
    ctx: _CtxStub, db_conn: sqlite3.Connection
) -> None:
    """部分更新：只改给定字段；重复提交同一值 = 没有实际改动。"""
    service = SettingsService(ctx)
    service.apply({"loop": {"max_iterations": 30}, "tools": {"record_retention_days": 14}})

    result = service.apply({"loop": {"max_iterations": 30}})
    assert result.changed == ()
    assert result.sections["loop"]["max_iterations"] == 30
    # 未提到的 section 不受影响
    assert _rows(db_conn)["tools.record_retention_days"] == "14"
    assert ctx.prune_records_calls == 1  # 只有第一次 tools 补丁触发了清理


def test_retention_validation_failure_never_triggers_cleanup(
    ctx: _CtxStub, db_conn: sqlite3.Connection
) -> None:
    """保留期限校验失败（非法类型 / 同批里有别的非法字段）：不清理。"""
    service = SettingsService(ctx)
    service.apply({"tools": {"record_retention_days": 9}})
    before = _rows(db_conn)
    assert ctx.prune_records_calls == 1
    assert ctx.prune_outputs_calls == 1  # 与既有端点一致：tools 补丁会顺带清过期输出

    with pytest.raises(SettingsValidationError):
        service.apply({"tools": {"record_retention_days": "很久"}})

    with pytest.raises(SettingsValidationError):
        service.apply(
            {
                "tools": {"record_retention_days": 1},
                "memory": {"fragment_max_tokens": 10},
            }
        )

    assert _rows(db_conn) == before
    assert ctx.prune_records_calls == 1  # 没有新增清理
    assert ctx.prune_outputs_calls == 1


def test_non_retention_section_does_not_prune(ctx: _CtxStub) -> None:
    """只改 memory / loop：不触发工具历史清理。"""
    SettingsService(ctx).apply({"memory": {"fragment_max_turns": 3}})
    assert ctx.prune_outputs_calls == 0
    assert ctx.prune_records_calls == 0


def test_retention_days_clamp_to_boundaries_like_existing_endpoints(ctx: _CtxStub) -> None:
    """tools 的保留期沿用既有「按边界收敛」语义。"""
    result = SettingsService(ctx).apply(
        {"tools": {"output_retention_days": 10**9, "record_retention_days": -5}}
    )
    assert result.sections["tools"]["output_retention_days"] == 3650
    assert result.sections["tools"]["record_retention_days"] == 0


def test_api_key_is_write_only_and_never_echoed(
    ctx: _CtxStub, db_conn: sqlite3.Connection
) -> None:
    """`bocha_api_key` 只写不读：库里存了真值，返回值 / 错误信息里都没有原文。"""
    secret = "sk-rm-e-secret-value"
    result = SettingsService(ctx).apply({"search": {"bocha_api_key": secret}})

    assert _rows(db_conn)["search.bocha_api_key"] == secret
    assert result.sections["search"]["bocha_has_key"] is True
    assert secret not in repr(result.as_dict())
    assert secret not in repr(result.sections)

    # 超长密钥：报错只说「太长」，不回显
    with pytest.raises(SettingsValidationError) as err:
        SettingsService(ctx).apply({"search": {"bocha_api_key": "x" * 4096}})
    assert secret not in str(err.value)
    assert "x" * 64 not in str(err.value)

    # 空串 = 显式清除
    cleared = SettingsService(ctx).apply({"search": {"bocha_api_key": ""}})
    assert cleared.sections["search"]["bocha_has_key"] is False


def test_string_numbers_and_booleans_are_accepted_like_existing_endpoints(
    ctx: _CtxStub,
) -> None:
    """既有端点接受 "1"/"true" 这类写法与字符串数字，这里保持一致。"""
    result = SettingsService(ctx).apply(
        {
            "maintenance": {"enabled": "false"},
            "tools": {"record_outputs": "0", "output_retention_days": "12"},
            "loop": {"max_iterations": "5"},
        }
    )
    assert result.sections["maintenance"]["enabled"] is False
    assert result.sections["tools"]["record_outputs"] is False
    assert result.sections["tools"]["output_retention_days"] == 12
    assert result.sections["loop"]["max_iterations"] == 5


def test_apply_settings_module_helper_matches_service(
    ctx: _CtxStub, db_conn: sqlite3.Connection
) -> None:
    from agent.services.settings_service import apply_settings

    result = apply_settings(ctx, {"memory": {"fragment_max_turns": 6}})
    assert result.sections["memory"]["fragment_max_turns"] == 6
    assert _rows(db_conn)["fragment.max_turns"] == "6"


def test_bad_section_body_type_is_rejected(ctx: _CtxStub) -> None:
    service = SettingsService(ctx)
    with pytest.raises(SettingsValidationError):
        service.apply({"loop": 5})  # type: ignore[dict-item]
    with pytest.raises(SettingsValidationError):
        service.apply({"loop": {"max_iterations": True}})  # bool 不是合法数字
    with pytest.raises(SettingsValidationError):
        service.apply({"loop": {"max_iterations": 0}})
    with pytest.raises(SettingsValidationError):
        service.apply({"loop": {"output_token_budget": -1}})


def test_nothing_written_when_validation_error_on_later_section_after_runtime_hook(
    ctx: _CtxStub, db_conn: sqlite3.Connection
) -> None:
    """同一批里前面是 search（会触发运行时应用）后面非法：运行时也不能动。"""
    before = _rows(db_conn)
    with pytest.raises(SettingsValidationError):
        SettingsService(ctx).apply(
            {
                "search": {"searxng_url": "http://x"},
                "memory": {"fragment_max_turns": 999},
            }
        )
    assert _rows(db_conn) == before
    assert ctx.search_applied == 0
