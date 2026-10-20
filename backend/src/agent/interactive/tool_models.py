# -*- coding: utf-8 -*-
"""阶段三冻结：互动模式专属工具的参数模型与意图结构（执行模型）。

契约：docs/qio-refactor-contract.md §13.2 / §13.3。

唯一职责：
* 定义 12 个板面工具的**参数模型**（pydantic）与意图 / 范围 / 位置结构；
* 模型可见 schema 由 agent.tools.contracts.json_schema_for 从这些模型生成，
  不另写一份手抄 schema；工具参数说明来自 prompts.ARGUMENT_DESCRIPTIONS；
* 本地校验与展示 schema 同源（Tool.arg_model），越界、类型、枚举由程序执行。

边界：
* 本模块不调用模型、不读写数据库、不做权限判定（授权与账本在 B 的服务层）。
* 字段名与枚举值是第三阶段冻结面；后续阶段确需扩展先更新契约并集中改这里。
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic import ConfigDict

# ---------------------------------------------------------------------------
# 冻结上限（程序执行，不是提示词建议）
# ---------------------------------------------------------------------------

MAX_INTENTS_PER_PROPOSAL = 10
MAX_CARD_IDS = 64
MAX_IDS = 128
MAX_REF_IDS = 64
MAX_OPERATION_KEYS = 200
MAX_NEW_CARDS_PER_INTENT = 40
MAX_NEW_GROUPS_PER_INTENT = 10
MAX_NEW_LINKS_PER_INTENT = 40
MAX_TITLE_CHARS = 120
MAX_GOAL_CHARS = 2000
MAX_EXPECTED_CHARS = 2000
MAX_QUESTION_CHARS = 500
MAX_SUMMARY_CHARS = 2000
MAX_CONTENT_CHARS = 200_000
MAX_COORD = 1_000_000.0
MIN_SIZE = 1.0
MAX_SIZE = 20_000.0
MAX_GAP = 10_000.0
MAX_REF_CHARS = 200

#: 意图允许的板面动作（与 scope.allowed_actions 枚举同源）。
ALLOWED_BOARD_ACTIONS: tuple[str, ...] = (
    "create_card",
    "update_card",
    "move_card",
    "group_cards",
    "order_group",
    "add_link",
)

#: 意图与续作的生命周期状态（程序填写；中文措辞不能代替这些值）。
INTENT_STATUSES: tuple[str, ...] = (
    "proposed",
    "awaiting_confirmation",
    "approved",
    "running",
    "paused",
    "completed",
    "partially_completed",
    "failed",
    "cancelled",
    "rejected",
)


class BoardAction(str, Enum):
    CREATE_CARD = "create_card"
    UPDATE_CARD = "update_card"
    MOVE_CARD = "move_card"
    GROUP_CARDS = "group_cards"
    ORDER_GROUP = "order_group"
    ADD_LINK = "add_link"


def _clean_ids(values: Any, *, limit: int = MAX_IDS) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, (list, tuple)):
        raise ValueError("必须是 id 列表")
    out: list[str] = []
    for raw in values:
        text = str(raw or "").strip()
        if not text:
            raise ValueError("id 不能为空")
        if len(text) > MAX_REF_CHARS:
            raise ValueError("id 过长")
        if text not in out:
            out.append(text)
    if len(out) > limit:
        raise ValueError(f"最多允许 {limit} 个 id")
    return out


def _finite(value: Any, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是有限数字") from exc
    if number != number or number in (float("inf"), float("-inf")):
        raise ValueError(f"{name} 必须是有限数字")
    if abs(number) > MAX_COORD:
        raise ValueError(f"{name} 超出板面坐标允许范围")
    return number


class IntentBasisRef(BaseModel):
    """意图依据：引用的正式卡片、版本及在意图中的角色。"""

    model_config = ConfigDict(extra="forbid")

    card_id: str = Field(min_length=1, max_length=MAX_REF_CHARS)
    version: int = Field(ge=1)
    role: Literal["source", "target", "context"] = "context"


class DependencyRef(BaseModel):
    """依赖 / 冲突引用：同一事务内由程序把 client_ref 解析成正式 intent id。"""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["client_ref", "intent_id"]
    value: str = Field(min_length=1, max_length=MAX_REF_CHARS)


class IntentScope(BaseModel):
    """结构化影响范围；授权对象是目标与范围，不是固定操作文本。"""

    model_config = ConfigDict(extra="forbid")

    read_card_ids: list[str] = Field(default_factory=list, max_length=MAX_CARD_IDS)
    write_card_ids: list[str] = Field(default_factory=list, max_length=MAX_CARD_IDS)
    group_ids: list[str] = Field(default_factory=list, max_length=MAX_CARD_IDS)
    allowed_actions: list[BoardAction] = Field(default_factory=list)
    max_new_cards: int = Field(default=0, ge=0, le=MAX_NEW_CARDS_PER_INTENT)
    max_new_groups: int = Field(default=0, ge=0, le=MAX_NEW_GROUPS_PER_INTENT)
    max_new_links: int = Field(default=0, ge=0, le=MAX_NEW_LINKS_PER_INTENT)

    @field_validator("read_card_ids", "write_card_ids", "group_ids", mode="before")
    @classmethod
    def _clean(cls, value: Any) -> list[str]:
        return _clean_ids(value, limit=MAX_CARD_IDS)

    @field_validator("allowed_actions")
    @classmethod
    def _actions(cls, value: Any) -> list[BoardAction]:
        # pydantic v2 的 after 校验器收到的可能已经是 BoardAction 枚举；
        # str(BoardAction.X) 在 Python 3.11 是 "BoardAction.X"，不能二次解析。
        out: list[BoardAction] = []
        for raw in value or []:
            action = raw if isinstance(raw, BoardAction) else BoardAction(getattr(raw, "value", raw))
            if action not in out:
                out.append(action)
        params = set(out) & {
            BoardAction.GROUP_CARDS,
            BoardAction.ORDER_GROUP,
            BoardAction.ADD_LINK,
        }
        if params and not (out & {BoardAction.CREATE_CARD, BoardAction.MOVE_CARD}):
            # 允许只做结构关系；但任何写入动作都必须明确列出，不做隐式推断。
            pass
        return out

    @model_validator(mode="after")
    def _write_scope_consistent(self) -> "IntentScope":
        overlap = set(self.read_card_ids) & set(self.write_card_ids)
        if overlap:
            # 同一张卡既读又写是常见情形，不禁止；这里只校验写范围非空时必须有写入动作。
            pass
        if self.write_card_ids and not (
            set(self.allowed_actions)
            & {BoardAction.UPDATE_CARD, BoardAction.MOVE_CARD}
        ):
            raise ValueError("write_card_ids 需要同时声明 update_card 或 move_card 动作")
        return self


class AbsolutePosition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["absolute"]
    x: float
    y: float

    @field_validator("x", "y")
    @classmethod
    def _finite(cls, value: Any, info: Any) -> float:
        return _finite(value, "坐标")


class RelativePosition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["relative"]
    relative_to: str = Field(min_length=1, max_length=MAX_REF_CHARS)
    side: Literal["right", "left", "below", "above"] = "right"
    gap: float = Field(default=24.0, ge=0.0, le=MAX_GAP)

    @field_validator("gap")
    @classmethod
    def _gap(cls, value: Any) -> float:
        number = _finite(value, "间距")
        if number < 0:
            raise ValueError("间距不能为负数")
        return number


class Position(BaseModel):
    """位置必须二选一：absolute 或 relative。"""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["absolute", "relative"]
    x: float | None = None
    y: float | None = None
    relative_to: str | None = None
    side: Literal["right", "left", "below", "above"] | None = None
    gap: float | None = None

    @model_validator(mode="after")
    def _check(self) -> "Position":
        if self.kind == "absolute":
            if self.x is None or self.y is None:
                raise ValueError("absolute 位置必须提供 x 和 y")
            _finite(self.x, "x")
            _finite(self.y, "y")
            if self.relative_to or self.side is not None or self.gap is not None:
                raise ValueError("absolute 位置不能同时提供相对位置字段")
        else:
            if not self.relative_to:
                raise ValueError("relative 位置必须提供 relative_to")
            if self.side is None:
                raise ValueError("relative 位置必须提供 side")
            if self.gap is not None and _finite(self.gap, "gap") < 0:
                raise ValueError("间距不能为负数")
            if self.x is not None or self.y is not None:
                raise ValueError("relative 位置不能同时提供 x 和 y")
        return self


class IntentSpec(BaseModel):
    """一次提案中的单个意图（模型只提目标与范围，不发送坐标落点之外的执行细节）。"""

    model_config = ConfigDict(extra="forbid")

    client_ref: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=MAX_TITLE_CHARS)
    goal: str = Field(min_length=1, max_length=MAX_GOAL_CHARS)
    expected: str = Field(min_length=1, max_length=MAX_EXPECTED_CHARS)
    basis: list[IntentBasisRef] = Field(default_factory=list, max_length=MAX_REF_IDS)
    scope: IntentScope = Field(default_factory=IntentScope)
    depends_on: list[DependencyRef] = Field(default_factory=list, max_length=MAX_REF_IDS)
    conflicts_with: list[DependencyRef] = Field(default_factory=list, max_length=MAX_REF_IDS)

    @field_validator("client_ref")
    @classmethod
    def _ref(cls, value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("client_ref 不能为空")
        allowed = set("abcdefghijklmnopqrstuvwxyz0123456789_-")
        if not set(text) <= allowed or not text[0].isalpha():
            raise ValueError("client_ref 只能使用小写字母、数字、下划线和短横线，且以字母开头")
        return text

    @model_validator(mode="after")
    def _refs(self) -> "IntentSpec":
        seen: set[str] = set()
        for dep in self.depends_on:
            if dep.kind == "client_ref":
                if dep.value == self.client_ref:
                    raise ValueError("意图不能依赖自己")
                if dep.value in seen:
                    raise ValueError("重复的依赖引用")
                seen.add(dep.value)
        conflicts: set[str] = set()
        for item in self.conflicts_with:
            if item.kind == "client_ref" and item.value == self.client_ref:
                raise ValueError("意图不能与自己冲突")
            key = (item.kind, item.value)
            if key in conflicts:
                raise ValueError("重复的冲突引用")
            conflicts.add(key)
        return self


# ---------------------------------------------------------------------------
# 12 个工具的冻结参数模型
# ---------------------------------------------------------------------------


class BoardInspectArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BoardReadCardsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    card_ids: list[str] = Field(default_factory=list, max_length=MAX_CARD_IDS)
    source: Literal["latest", "submission"] = "latest"
    offset: int = Field(default=0, ge=0, le=10_000)
    limit: int = Field(default=20, ge=1, le=MAX_CARD_IDS)

    @field_validator("card_ids", mode="before")
    @classmethod
    def _clean(cls, value: Any) -> list[str]:
        return _clean_ids(value, limit=MAX_CARD_IDS)


class BoardProposeIntentsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    submission_id: str = Field(min_length=1, max_length=MAX_REF_CHARS)
    intents: list[IntentSpec] = Field(min_length=1, max_length=MAX_INTENTS_PER_PROPOSAL)
    resolved_question_ids: list[str] = Field(default_factory=list, max_length=MAX_IDS)

    @model_validator(mode="after")
    def _refs(self) -> "BoardProposeIntentsArgs":
        seen = [intent.client_ref for intent in self.intents]
        if len(seen) != len(set(seen)):
            raise ValueError("同一次提案中的 client_ref 必须唯一")
        return self


class BoardUpdateIntentArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intent_id: str = Field(min_length=1, max_length=MAX_REF_CHARS)
    expected_version: int = Field(ge=1)
    changes: dict[str, Any] = Field(default_factory=dict)
    resolved_question_ids: list[str] = Field(default_factory=list, max_length=MAX_IDS)

    @model_validator(mode="after")
    def _changes(self) -> "BoardUpdateIntentArgs":
        allowed = {
            "title",
            "goal",
            "expected",
            "basis",
            "scope",
            "depends_on",
            "conflicts_with",
        }
        unknown = set(self.changes) - allowed
        if unknown:
            raise ValueError(f"不允许修改的意图字段：{sorted(unknown)}")
        if not self.changes and not self.resolved_question_ids:
            raise ValueError("changes 与 resolved_question_ids 至少提供一项")
        return self


class BoardWaitForInputArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    submission_id: str = Field(min_length=1, max_length=MAX_REF_CHARS)
    question: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)
    missing_fields: list[str] = Field(default_factory=list, max_length=MAX_IDS)

    @field_validator("missing_fields", mode="before")
    @classmethod
    def _fields(cls, value: Any) -> list[str]:
        return _clean_ids(value, limit=MAX_IDS)


class BoardCreateCardArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intent_id: str = Field(min_length=1, max_length=MAX_REF_CHARS)
    operation_key: str = Field(min_length=1, max_length=MAX_REF_CHARS)
    content: str = Field(min_length=1, max_length=MAX_CONTENT_CHARS)
    title: str | None = Field(default=None, max_length=MAX_TITLE_CHARS)
    width: float | None = None
    height: float | None = None
    position: Position | None = None

    @model_validator(mode="after")
    def _size(self) -> "BoardCreateCardArgs":
        for name in ("width", "height"):
            value = getattr(self, name)
            if value is not None:
                number = _finite(value, name)
                if number < MIN_SIZE or number > MAX_SIZE:
                    raise ValueError(f"{name} 必须为正数且在允许范围内")
        return self


class BoardUpdateCardArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intent_id: str = Field(min_length=1, max_length=MAX_REF_CHARS)
    operation_key: str = Field(min_length=1, max_length=MAX_REF_CHARS)
    card_id: str = Field(min_length=1, max_length=MAX_REF_CHARS)
    expected_version: int = Field(ge=1)
    patch: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _patch(self) -> "BoardUpdateCardArgs":
        allowed = {"content", "title", "width", "height"}
        unknown = set(self.patch) - allowed
        if unknown:
            raise ValueError(f"update_card 不允许修改字段：{sorted(unknown)}")
        if not self.patch:
            raise ValueError("patch 不能为空")
        if "content" in self.patch:
            text = str(self.patch.get("content") or "")
            if not text.strip() or len(text) > MAX_CONTENT_CHARS:
                raise ValueError("content 必须是完整非空正文")
        for name in ("width", "height"):
            if name in self.patch:
                number = _finite(self.patch[name], name)
                if number < MIN_SIZE or number > MAX_SIZE:
                    raise ValueError(f"{name} 必须为正数")
        return self


class BoardMoveCardArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intent_id: str = Field(min_length=1, max_length=MAX_REF_CHARS)
    operation_key: str = Field(min_length=1, max_length=MAX_REF_CHARS)
    card_id: str = Field(min_length=1, max_length=MAX_REF_CHARS)
    expected_version: int = Field(ge=1)
    position: Position


class BoardGroupCardsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intent_id: str = Field(min_length=1, max_length=MAX_REF_CHARS)
    operation_key: str = Field(min_length=1, max_length=MAX_REF_CHARS)
    card_ids: list[str] = Field(min_length=1, max_length=MAX_CARD_IDS)
    expected_versions: dict[str, int] = Field(default_factory=dict)
    group_id: str | None = Field(default=None, max_length=MAX_REF_CHARS)
    name: str | None = Field(default=None, max_length=MAX_TITLE_CHARS)

    @field_validator("card_ids", mode="before")
    @classmethod
    def _ids(cls, value: Any) -> list[str]:
        return _clean_ids(value, limit=MAX_CARD_IDS)

    @field_validator("expected_versions")
    @classmethod
    def _versions(cls, value: Any) -> dict[str, int]:
        out: dict[str, int] = {}
        for key, raw in (value or {}).items():
            cid = str(key or "").strip()
            if not cid:
                raise ValueError("expected_versions 的 id 不能为空")
            number = int(raw)
            if number < 1:
                raise ValueError("expected_versions 的版本必须 >= 1")
            out[cid] = number
        if len(out) > MAX_CARD_IDS:
            raise ValueError("expected_versions 条目过多")
        return out


class BoardOrderGroupArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intent_id: str = Field(min_length=1, max_length=MAX_REF_CHARS)
    operation_key: str = Field(min_length=1, max_length=MAX_REF_CHARS)
    group_id: str = Field(min_length=1, max_length=MAX_REF_CHARS)
    member_order: list[str] = Field(min_length=1, max_length=MAX_CARD_IDS)
    expected_versions: dict[str, int] = Field(default_factory=dict)
    arrange: Literal["none", "row", "column"] = "none"

    @field_validator("member_order", mode="before")
    @classmethod
    def _ids(cls, value: Any) -> list[str]:
        return _clean_ids(value, limit=MAX_CARD_IDS)

    @field_validator("expected_versions")
    @classmethod
    def _versions(cls, value: Any) -> dict[str, int]:
        return BoardGroupCardsArgs._versions(value)


class BoardAddLinkArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intent_id: str = Field(min_length=1, max_length=MAX_REF_CHARS)
    operation_key: str = Field(min_length=1, max_length=MAX_REF_CHARS)
    src: str = Field(min_length=1, max_length=MAX_REF_CHARS)
    dst: str = Field(min_length=1, max_length=MAX_REF_CHARS)
    direction: bool = False
    meaning: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _self_link(self) -> "BoardAddLinkArgs":
        if self.src == self.dst:
            raise ValueError("不能连接卡片自身")
        return self


class BoardFinishIntentArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intent_id: str = Field(min_length=1, max_length=MAX_REF_CHARS)
    expected_version: int = Field(ge=1)
    outcome: Literal["completed", "partially_completed", "failed"]
    summary: str = Field(min_length=1, max_length=MAX_SUMMARY_CHARS)
    operation_keys: list[str] = Field(default_factory=list, max_length=MAX_OPERATION_KEYS)

    @field_validator("operation_keys", mode="before")
    @classmethod
    def _keys(cls, value: Any) -> list[str]:
        return _clean_ids(value, limit=MAX_OPERATION_KEYS)


#: 工具名 -> 冻结参数模型（注册工具时直接作为 arg_model）。
TOOL_ARG_MODELS: dict[str, type[BaseModel]] = {
    "board_inspect": BoardInspectArgs,
    "board_read_cards": BoardReadCardsArgs,
    "board_propose_intents": BoardProposeIntentsArgs,
    "board_update_intent": BoardUpdateIntentArgs,
    "board_wait_for_input": BoardWaitForInputArgs,
    "board_create_card": BoardCreateCardArgs,
    "board_update_card": BoardUpdateCardArgs,
    "board_move_card": BoardMoveCardArgs,
    "board_group_cards": BoardGroupCardsArgs,
    "board_order_group": BoardOrderGroupArgs,
    "board_add_link": BoardAddLinkArgs,
    "board_finish_intent": BoardFinishIntentArgs,
}

#: 只读工具：不写板面，可与其他只读工具并行。
READ_ONLY_TOOLS: tuple[str, ...] = ("board_inspect", "board_read_cards")


def intent_spec_schema() -> dict[str, Any]:
    """意图结构的模型可见 schema（程序生成，不手写）。"""
    from agent.tools.contracts import json_schema_for

    return json_schema_for(IntentSpec)


def tool_argument_schemas() -> dict[str, dict[str, Any]]:
    """全部板面工具的参数 schema（key 为工具名），供注册与静态核对。"""
    from agent.tools.contracts import json_schema_for

    return {name: json_schema_for(model) for name, model in TOOL_ARG_MODELS.items()}
