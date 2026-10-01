# -*- coding: utf-8 -*-
"""统一「模型输出可修正层」。

为什么需要它：派生数据（摘要、索引、实体卡、知识）由主模型产出，而模型的输出
天然会「太多 / 太长 / 重复 / 多余空白」。这些都**属于可修正输出** —— 本地按契约
收敛后继续派生，而不是让第 51 个实体名、超长标题、或者一条坏条目把整条链
（摘要 → 索引 → 实体卡 → 知识）一起变成 0。

**只有下面四类才算 schema failure**，并且失败原因必须可读、必须落在
trace / 派生任务状态里：

1. **根本无法解析**：不是 JSON、顶层不是 JSON 对象；
2. **类型完全错误**：字符串字段拿到非字符串；列表字段拿到非列表；
   列表里**没有任何一条**符合声明类型；
3. **必需结构缺失**：必需字段缺失，或修正后为空；
4. **无法安全恢复**：列表里本来有内容，但每一条都无效，没有可保留项。

判定顺序固定：解析 → 字段类型 → 逐条修正（strip / 去空 / 去重 / 截断）
→ 逐条领域校验（无效条目**丢弃并记 note**，不整批失败）→ 组装。

每一次修正与丢弃都产生 `RepairNote`；调用方必须把 note 写进 trace / 诊断。
**修正必须是可见的** —— 不允许把结构问题悄悄吞掉，也不允许把「模型给多了」
升级成整条派生失败。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Generic, Mapping, Sequence, TypeVar

_JSON_BLOCK = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)

# 模型把 JSON null 写成字符串的常见形态（提示词里写的是 "user|topic|entity|null"）。
_NULL_LITERALS = {"null", "none", "nil", "undefined", "-"}

# 修正类型：写进 trace 的稳定 code，便于检索与断言。
NOTE_STRIPPED = "stripped"
NOTE_TRUNCATED_TEXT = "truncated_text"
NOTE_TRUNCATED_ITEMS = "truncated_items"
NOTE_DEDUPED = "deduped"
NOTE_DROPPED_BLANK = "dropped_blank"
NOTE_DROPPED_INVALID_ITEM = "dropped_invalid_item"
NOTE_NULL_LITERAL = "null_literal"

_MISSING = object()


@dataclass(frozen=True)
class RepairNote:
    """一次本地修正 / 丢弃的留痕。detail 只放计数与原因，不放模型原文。"""

    field: str
    code: str
    detail: str

    def render(self) -> str:
        return f"{self.field}:{self.code}({self.detail})"


@dataclass(frozen=True)
class TextField:
    """字符串字段契约。

    * `required=True`：缺失或修正后为空 → schema failure；
    * `max_length`：超长按上限截断（可修正）；
    * `allow_null_literal=True`：把模型写出来的 "null" / "none" / "" 当空值
      （仅用于本来就可空的字段，见 KnowledgeCandidate.attach / entity）。
    """

    name: str
    max_length: int
    required: bool = True
    allow_null_literal: bool = False


@dataclass(frozen=True)
class TextListField:
    """字符串数组字段契约：截断 / 去重 / 去空 / strip，都由本地兜底。

    `strict=False` 用于**条目内部的子字段**（例如实体卡的 aliases）：这里坏掉
    只把子值降级为空 + 记 note，不让承载它的那条主条目一起被丢掉。
    """

    name: str
    max_items: int
    max_item_length: int | None = None
    required: bool = False
    dedupe: bool = True
    strict: bool = True


@dataclass(frozen=True)
class ObjectListField:
    """对象数组字段契约（例如知识候选）。

    `item_validator` 是领域校验：返回非空字符串表示该条无效，只丢这一条。
    「列表非空但一条都没保住」才算无法安全恢复。

    `fields` 允许嵌套（子字段也可以是 TextListField / ObjectListField）；
    `strict=False` 的嵌套字段只做局部降级（空数组 + note），不让父条目失败。
    """

    name: str
    max_items: int
    fields: tuple[Field, ...] = ()
    required: bool = False
    dedupe: bool = False
    strict: bool = True
    item_validator: Callable[[dict[str, Any]], str | None] | None = None


Field = TextField | TextListField | ObjectListField


@dataclass(frozen=True)
class RepairResult:
    """一次「解析 + 修正」的结果。data 非空且 error 为空才算可用。"""

    data: dict[str, Any] | None
    error: str | None = None
    notes: tuple[RepairNote, ...] = ()

    @property
    def ok(self) -> bool:
        return self.data is not None and self.error is None


T = TypeVar("T")


@dataclass(frozen=True)
class Outcome(Generic[T]):
    """派生契约的统一产出：值 / 可读失败原因 / 修正留痕。"""

    value: T | None
    error: str | None = None
    notes: tuple[RepairNote, ...] = ()

    @property
    def ok(self) -> bool:
        return self.value is not None

    def repair_line(self) -> str:
        return render_notes(self.notes)


def render_notes(notes: Sequence[RepairNote]) -> str:
    return "; ".join(note.render() for note in notes)


def parse_json_object(text: str) -> tuple[Any | None, str | None]:
    """取出并解析模型返回的 JSON（容忍 Markdown 代码围栏）。"""
    raw = (text or "").strip()
    if not raw:
        return None, "模型输出为空"
    match = _JSON_BLOCK.search(raw)
    candidate = match.group(1) if match else raw
    try:
        return json.loads(candidate), None
    except json.JSONDecodeError as exc:
        return None, f"无法解析 JSON：{exc}"


def repair_output(
    payload: Any,
    fields: Sequence[Field],
    *,
    contract: str,
) -> RepairResult:
    """按契约修正一份已解析的模型输出。

    `contract` 只用于拼可读的失败原因（例如「摘要输出」「知识抽取输出」）。
    """
    if not isinstance(payload, Mapping):
        got = type(payload).__name__
        return RepairResult(None, f"{contract}顶层不是 JSON 对象（拿到 {got}）")

    data: dict[str, Any] = dict(payload)
    notes: list[RepairNote] = []

    for spec in fields:
        error = _apply_field(data, spec, notes)
        if error is not None:
            return RepairResult(None, f"{contract}：{error}", tuple(notes))

    return RepairResult(data, None, tuple(notes))


# ---------------------------------------------------------------------------
# 单个字段的修正
# ---------------------------------------------------------------------------


def _apply_field(
    data: dict[str, Any],
    spec: Field,
    notes: list[RepairNote],
    *,
    prefix: str = "",
) -> str | None:
    """按字段类型分派修正。prefix 只影响留痕 / 失败原因里的可读标签。"""
    if isinstance(spec, TextField):
        return _apply_text_field(data, spec, notes, prefix=prefix)
    if isinstance(spec, TextListField):
        return _apply_text_list_field(data, spec, notes, prefix=prefix)
    return _apply_object_list_field(data, spec, notes, prefix=prefix)


def _apply_text_field(
    data: dict[str, Any],
    spec: TextField,
    notes: list[RepairNote],
    *,
    prefix: str = "",
) -> str | None:
    label = f"{prefix}{spec.name}"
    if spec.name not in data:
        if spec.required:
            return f"缺少必需字段 {label}"
        return None
    value, error = _repair_text_value(
        data[spec.name], spec, label=label, notes=notes
    )
    if error is not None:
        return error
    if value is _MISSING:
        data.pop(spec.name, None)
    else:
        data[spec.name] = value
    return None


def _apply_text_list_field(
    data: dict[str, Any],
    spec: TextListField,
    notes: list[RepairNote],
    *,
    prefix: str = "",
) -> str | None:
    label = f"{prefix}{spec.name}"
    if spec.name not in data:
        if spec.required:
            return f"缺少必需字段 {label}"
        return None
    raw = data[spec.name]
    if not isinstance(raw, list):
        if spec.strict:
            return f"字段 {label} 类型错误（期望数组，拿到 {type(raw).__name__}）"
        notes.append(
            RepairNote(
                label,
                NOTE_DROPPED_INVALID_ITEM,
                f"类型不是数组（{type(raw).__name__}），按空处理",
            )
        )
        data[spec.name] = []
        return None

    kept: list[str] = []
    seen: set[str] = set()
    type_invalid = 0
    blank = 0
    deduped = 0
    stripped = 0
    truncated = 0
    for item in raw:
        # 「列表里一条都不是声明类型」= 类型完全错误，交给调用方判失败。
        if not isinstance(item, str):
            type_invalid += 1
            continue
        value = item.strip()
        if value != item:
            stripped += 1
        if not value:
            blank += 1
            continue
        if spec.max_item_length is not None and len(value) > spec.max_item_length:
            value = value[: spec.max_item_length]
            truncated += 1
        if spec.dedupe and value in seen:
            deduped += 1
            continue
        seen.add(value)
        kept.append(value)

    if raw and type_invalid == len(raw):
        if spec.strict:
            return f"字段 {label} 类型完全错误（{len(raw)} 项都不是字符串）"
        notes.append(
            RepairNote(
                label,
                NOTE_DROPPED_INVALID_ITEM,
                f"{len(raw)} 项都不是字符串，按空处理",
            )
        )
        kept = []
    if stripped:
        notes.append(RepairNote(label, NOTE_STRIPPED, f"{stripped} 项去掉首尾空白"))
    if blank:
        notes.append(RepairNote(label, NOTE_DROPPED_BLANK, f"丢掉 {blank} 项空白值"))
    if truncated:
        notes.append(
            RepairNote(label, NOTE_TRUNCATED_TEXT, f"{truncated} 项按上限截断")
        )
    all_type_invalid = bool(raw) and type_invalid == len(raw)
    if type_invalid and not all_type_invalid:
        notes.append(
            RepairNote(
                label, NOTE_DROPPED_INVALID_ITEM, f"丢掉 {type_invalid} 项非字符串"
            )
        )
    if deduped:
        notes.append(RepairNote(label, NOTE_DEDUPED, f"去掉 {deduped} 项重复"))
    if len(kept) > spec.max_items:
        notes.append(
            RepairNote(
                label,
                NOTE_TRUNCATED_ITEMS,
                f"保留 {spec.max_items}/{len(kept)} 项",
            )
        )
        kept = kept[: spec.max_items]
    data[spec.name] = kept
    return None


def _apply_object_list_field(
    data: dict[str, Any],
    spec: ObjectListField,
    notes: list[RepairNote],
    *,
    prefix: str = "",
) -> str | None:
    label = f"{prefix}{spec.name}"
    if spec.name not in data:
        if spec.required:
            return f"缺少必需字段 {label}"
        return None
    raw = data[spec.name]
    if not isinstance(raw, list):
        if spec.strict:
            return f"字段 {label} 类型错误（期望数组，拿到 {type(raw).__name__}）"
        notes.append(
            RepairNote(
                label,
                NOTE_DROPPED_INVALID_ITEM,
                f"类型不是数组（{type(raw).__name__}），按空处理",
            )
        )
        data[spec.name] = []
        return None

    kept: list[dict[str, Any]] = []
    seen: set[str] = set()
    type_invalid = 0
    invalid: list[str] = []
    deduped = 0
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            type_invalid += 1
            continue
        item_label = f"{label}[{index}]"
        repaired: dict[str, Any] = dict(item)
        item_notes: list[RepairNote] = []
        reason: str | None = None
        for field_spec in spec.fields:
            error = _apply_field(
                repaired, field_spec, item_notes, prefix=f"{item_label}."
            )
            if error is not None:
                reason = error
                break
        if reason is None and spec.item_validator is not None:
            reason = spec.item_validator(repaired)
        if reason is not None:
            invalid.append(reason)
            continue
        if spec.dedupe:
            key = json.dumps(repaired, sort_keys=True, ensure_ascii=False, default=str)
            if key in seen:
                deduped += 1
                continue
            seen.add(key)
        # 条目自身的修正（strip / 截断）在条目通过校验后才计入留痕
        notes.extend(item_notes)
        kept.append(repaired)

    all_type_invalid = bool(raw) and type_invalid == len(raw)
    all_invalid = bool(raw) and not kept
    if all_type_invalid and spec.strict:
        return f"字段 {label} 类型完全错误（{len(raw)} 项都不是对象）"
    if all_invalid and not all_type_invalid and spec.strict:
        return (
            f"字段 {label} 无法安全恢复（{len(raw)} 项全部无效："
            f"{invalid[0] if invalid else '类型无效'}）"
        )
    if all_type_invalid:
        notes.append(
            RepairNote(
                label,
                NOTE_DROPPED_INVALID_ITEM,
                f"{len(raw)} 项都不是对象，按空处理",
            )
        )
    elif type_invalid:
        notes.append(
            RepairNote(
                label, NOTE_DROPPED_INVALID_ITEM, f"丢掉 {type_invalid} 项非对象"
            )
        )
    if invalid:
        notes.append(
            RepairNote(
                label,
                NOTE_DROPPED_INVALID_ITEM,
                f"丢掉 {len(invalid)} 项无效条目（{invalid[0]}）",
            )
        )
    if deduped:
        notes.append(RepairNote(label, NOTE_DEDUPED, f"去掉 {deduped} 项重复"))
    if len(kept) > spec.max_items:
        notes.append(
            RepairNote(
                label,
                NOTE_TRUNCATED_ITEMS,
                f"保留 {spec.max_items}/{len(kept)} 项",
            )
        )
        kept = kept[: spec.max_items]
    data[spec.name] = kept
    return None


def _repair_text_value(
    raw: Any,
    spec: TextField,
    *,
    label: str,
    notes: list[RepairNote],
) -> tuple[Any, str | None]:
    """返回 (修正后的值 / _MISSING, 错误)。错误非空 = schema failure。"""
    if raw is _MISSING:
        if spec.required:
            return _MISSING, f"缺少必需字段 {label}"
        return _MISSING, None
    if spec.allow_null_literal and not spec.required and isinstance(raw, str):
        if raw.strip().lower() in _NULL_LITERALS or not raw.strip():
            notes.append(RepairNote(label, NOTE_NULL_LITERAL, "按空值处理"))
            return _MISSING, None
    if raw is None and not spec.required:
        # 可空字段的 JSON null 本来就是契约允许的形态，不算一次「修正」
        return _MISSING, None
    if not isinstance(raw, str):
        return _MISSING, f"{label} 类型错误（期望字符串，拿到 {type(raw).__name__}）"
    value = raw.strip()
    if value != raw:
        notes.append(RepairNote(label, NOTE_STRIPPED, "去掉首尾空白"))
    if not value:
        if spec.required:
            return _MISSING, f"{label} 修正后为空（必需字段）"
        notes.append(RepairNote(label, NOTE_DROPPED_BLANK, "空白值"))
        return _MISSING, None
    if len(value) > spec.max_length:
        notes.append(
            RepairNote(label, NOTE_TRUNCATED_TEXT, f"{len(value)}→{spec.max_length} 字符")
        )
        value = value[: spec.max_length]
    return value, None
