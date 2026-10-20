# -*- coding: utf-8 -*-
"""阶段三冻结：互动模式的可信运行上下文（程序绑定，模型不可覆盖）。

契约：docs/qio-refactor-contract.md §13.2 / §13.4。

主循环在每一轮开始/结束时由 Lead 的接线设置本上下文；板面工具只从这里取
当前板面、提交、意图与授权范围，**不接受模型自报 board_id / 模式 / 权限**。
普通模式没有本上下文 → 板面工具一律拒绝（normal_mode 不可用）。

本模块不读数据库、不调用模型；只保存本轮程序已经验证过的绑定事实。
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any

MODE_NORMAL = "normal"
MODE_INTERACTIVE = "interactive"


@dataclass(frozen=True)
class BoardTurnContext:
    """一轮互动请求的可信绑定（全部由程序在受理 / 续作时验证后写入）。"""

    turn_id: str
    mode: str
    board_id: str = ""
    submission_id: str = ""
    #: 原提交的话题 / 续作身份（由程序从提交记录与 turn 绑定恢复）。
    topic_id: str = ""
    attempt: int = 1
    #: 本次续作要执行的已确认意图；空表示本轮是提案 / 澄清轮。
    intent_ids: tuple[str, ...] = ()
    #: 已确认意图的授权范围快照（intent_id -> scope 字典），仅供程序核对展示，
    #: 工具不得据此放宽服务层自己的授权检查。
    authorized_scopes: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def interactive(self) -> bool:
        return self.mode == MODE_INTERACTIVE and bool(self.board_id)


class BoardToolUnavailable(RuntimeError):
    """当前轮没有可信互动绑定（普通模式 / 绑定缺失 / 板面不可用）。"""

    def __init__(self, message: str, *, code: str = "interactive_unavailable") -> None:
        super().__init__(message)
        self.code = code


_current: ContextVar[BoardTurnContext | None] = ContextVar(
    "qio_board_turn_context", default=None
)

#: 本轮已通过 board_wait_for_input 交付给用户的问题（Lead 的接线在轮末统一
#: 作为正常对话呈现一次；模型不需要再次生成同一问题）。
_delivered_questions: ContextVar[list[dict[str, Any]]] = ContextVar(
    "qio_board_delivered_questions", default=[]
)


def set_board_turn(context: BoardTurnContext) -> Token:
    """设置本轮绑定；返回的 token 交给 reset_board_turn 恢复。"""
    return _current.set(context)


def reset_board_turn(token: Token) -> None:
    _current.reset(token)


def current_board_turn() -> BoardTurnContext | None:
    return _current.get()


def require_board_turn() -> BoardTurnContext:
    """工具入口用：没有可信绑定就拒绝，绝不接受模型自报的板面身份。"""
    context = _current.get()
    if context is None:
        raise BoardToolUnavailable(
            "当前轮没有可信的互动板面绑定：普通对话模式或绑定缺失，本次板面操作未执行。"
        )
    if not context.interactive:
        raise BoardToolUnavailable(
            "当前轮不是互动模式或缺少板面身份，本次板面操作未执行。"
        )
    return context


def record_delivered_question(question: str, *, missing_fields: list[str] | None = None) -> None:
    """记录一条已交付的问题（由 board_wait_for_input 调用）。"""
    items = list(_delivered_questions.get() or [])
    items.append(
        {
            "question": str(question or "").strip(),
            "missing_fields": list(missing_fields or []),
        }
    )
    _delivered_questions.set(items)


def take_delivered_questions() -> list[dict[str, Any]]:
    """轮末取出并清空已交付问题（Lead 接线的持久化 / 呈现路径使用）。"""
    items = list(_delivered_questions.get() or [])
    _delivered_questions.set([])
    return items


def clear_delivered_questions() -> None:
    _delivered_questions.set([])
