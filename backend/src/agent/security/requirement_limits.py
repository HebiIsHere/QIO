"""结构化用户限制：校验 / 序列化 / 合并 / 从要求读取（R9-H 契约 §6）。

为什么需要独立模块：
* 限制只约束、不授权：它必须能被引擎用于拒绝，但**永远不能**产生许可；
* 旧版 user_requirements.limits 是展平文字，无法可靠判断操作/对象；R9-H 起把
  结构化限制（JSON 数组）持久化到 user_requirements.structured_limits；
* 历史行（state=legacy，列 NULL）表示"未知"，不是"明确无限制"；无法可靠恢复时
  标 unresolved，引擎按未解决处理（只对受影响操作要求确认，不静默清空授权）；
* 补充/修正：只补充一句话不得清空父限制；明确解除（removed）才从父集合移除。

限制条目结构（冻结，契约 §6）：
    {
      "operations": ["read" | "write" | "create" | "delete" | "execute" |
                     "install" | "system_change" | "egress", ...],
      "objects": [{"kind": "path" | "path_prefix" | "directory" |
                           "program" | "service", "value": "..."}],
      "reason": "简短原因（不存模型推理全文）"
    }

本模块不做任何名称/正则/关键词判断：对象匹配由引擎按结构化 kind + 路径规范化完成。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Iterable

logger = logging.getLogger(__name__)

#: 结构化限制状态（持久化到 user_requirements.structured_limits_state）。
STATE_LEGACY = "legacy"
STATE_STRUCTURED = "structured"
STATE_UNRESOLVED = "unresolved"
STRUCTURED_LIMIT_STATES = frozenset({STATE_LEGACY, STATE_STRUCTURED, STATE_UNRESOLVED})


class LimitRemovalAmbiguous(ValueError):
    """removed_limits 无法精确落到已有结构化限制上（不能猜测/不能表达剩余集合）。

    调用方必须保留原已知限制并把状态标 unresolved；不得静默忽略解除、也不得清空
    全部限制（R9-H4 §4）。
    """

#: 允许出现在限制条目里的操作名（与引擎 EffectKind 的语义名对应）。
LIMIT_OPERATIONS = frozenset(
    {
        # R9-H4 §2：规范操作名；read 必须能从提示词/解析/保存到执行一致。
        "read",
        "write",
        "create",
        "delete",
        "execute",
        "install",
        "system_change",
        # 本轮排除 egress 的新语义，但保留既有条目可解析/持久化。
        "egress",
    }
)

#: 允许的对象类别；value 只做结构化保存，匹配由引擎按路径关系完成。
LIMIT_OBJECT_KINDS = frozenset(
    {"path", "path_prefix", "directory", "program", "program_name", "service"}
)

#: 防御性上限：模型输出再离谱也不能把台账/内存撑爆。
MAX_LIMIT_ENTRIES = 200
MAX_OBJECTS_PER_LIMIT = 100
MAX_REASON_CHARS = 500
MAX_VALUE_CHARS = 4096


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _normalize_operations(value: Any) -> list[str]:
    if isinstance(value, (str, bytes)) or value is None:
        raise ValueError("限制条目的 operations 必须是数组")
    if not isinstance(value, (list, tuple)):
        raise ValueError("限制条目的 operations 必须是数组")
    operations: list[str] = []
    for raw in value:
        item = _clean_text(raw).lower()
        if not item:
            continue
        if item not in LIMIT_OPERATIONS:
            raise ValueError("不支持的限制操作：" + item)
        if item not in operations:
            operations.append(item)
    return operations


def _normalize_objects(value: Any) -> list[dict]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise ValueError("限制条目的 objects 必须是数组")
    objects: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for raw in value:
        if not isinstance(raw, dict):
            raise ValueError("限制条目的 objects 元素必须是对象")
        kind = _clean_text(raw.get("kind") or "path").lower()
        if kind not in LIMIT_OBJECT_KINDS:
            raise ValueError("不支持的限制对象类别：" + kind)
        text = _clean_text(raw.get("value"))
        if not text:
            raise ValueError("限制对象必须给出 value")
        if len(text) > MAX_VALUE_CHARS:
            raise ValueError("限制对象 value 过长")
        if kind == "program" and "/" not in text and "\\" not in text:
            # R9-H6 §4：历史/模型保存的裸程序名不是路径。转成明确的
            # program_name 结构化类型；不再拿它当 cwd 下的路径静默忽略。
            kind = "program_name"
        if kind == "program_name" and ("/" in text or "\\" in text):
            raise ValueError("program_name 限制只能是程序名称，不能包含路径分隔符")
        key = (kind, text)
        if key in seen:
            continue
        seen.add(key)
        # 类型已转成明确的 program_name；不额外写回 legacy_kind，避免
        # 规范化/反序列化产生两个形状（来源由升级转换本身说明）。
        objects.append({"kind": kind, "value": text})
        if len(objects) > MAX_OBJECTS_PER_LIMIT:
            raise ValueError("单个限制条目的对象数过多")
    return objects


def _canonical_entry(entry: dict, *, include_reason: bool = True) -> tuple:
    operations = tuple(sorted(str(item) for item in (entry.get("operations") or ())))
    objects = tuple(
        sorted(
            (str(item.get("kind")), str(item.get("value")))
            for item in (entry.get("objects") or ())
            if isinstance(item, dict)
        )
    )
    if include_reason:
        return (operations, objects, str(entry.get("reason") or ""))
    return (operations, objects)


def normalize_limits(value: object) -> tuple[dict, ...]:
    """校验/去重/规范化结构化限制；非法抛 ValueError。

    接受 list/tuple（元素为 dict）。空数组是合法的"明确无限制"；None 不合法
    （NULL 只出现在历史/未知场景，调用方应先用 deserialize_limits 解析）。
    """
    if value is None:
        raise ValueError("结构化限制不能为 None（NULL 表示历史未知，请用空数组表示无限制）")
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise ValueError("结构化限制必须是数组")
    if len(value) > MAX_LIMIT_ENTRIES:
        raise ValueError("结构化限制条目过多")
    result: list[dict] = []
    seen: set[tuple] = set()
    for raw in value:
        if not isinstance(raw, dict):
            raise ValueError("结构化限制条目必须是对象")
        operations = _normalize_operations(raw.get("operations") or [])
        objects = _normalize_objects(raw.get("objects") or [])
        if not operations and not objects:
            raise ValueError("限制条目必须至少给出 operations 或 objects")
        reason = _clean_text(raw.get("reason"))[:MAX_REASON_CHARS]
        entry = {
            "operations": operations,
            "objects": objects,
            "reason": reason,
        }
        key = _canonical_entry(entry, include_reason=False)
        if key in seen:
            continue
        seen.add(key)
        result.append(entry)
    return tuple(result)


def serialize_limits(limits: Iterable[dict]) -> str:
    """规范化为 JSON 文本（ensure_ascii=False；不含推理全文/密钥原文）。"""
    normalized = normalize_limits(list(limits or ()))
    return json.dumps(
        [dict(item) for item in normalized], ensure_ascii=False, sort_keys=True
    )


def deserialize_limits(raw: object) -> tuple[dict, ...] | None:
    """持久化列 -> 结构化限制；NULL/空串/非法 JSON/非法结构 -> None（历史未知）。"""
    if raw is None:
        return None
    if isinstance(raw, (list, tuple)):
        value: object = list(raw)
    else:
        text = str(raw).strip()
        if not text:
            return None
        try:
            value = json.loads(text)
        except (TypeError, ValueError):
            logger.warning("结构化限制 JSON 损坏，按历史未知处理")
            return None
    if value is None:
        return None
    if not isinstance(value, (list, tuple)):
        logger.warning("结构化限制不是数组，按历史未知处理")
        return None
    try:
        return normalize_limits(list(value))
    except ValueError:
        logger.warning("结构化限制结构非法，按历史未知处理")
        return None


def _entry_with(
    operations: Iterable[str], objects: Iterable[tuple[str, str]], reason: str
) -> dict:
    return {
        "operations": sorted({str(item) for item in operations}),
        "objects": [{"kind": kind, "value": value} for kind, value in sorted(objects)],
        "reason": reason,
    }


def remove_limits_precise(
    parent: Iterable[dict], removed: Iterable[dict]
) -> tuple[dict, ...]:
    """从已知限制中**精确**移除 removed_limits 覆盖的 (操作, 对象) 组合。

    语义（契约 §4）：
    * removed 有 operations 无 objects = 解除这些操作在**所有对象**上的限制；
    * removed 有 objects 无 operations = 解除这些对象上**所有操作**的限制；
    * 两者都有 = 只解除交集（解除 B 的 write 不清除 A，也不误删 B 的 delete）；
    * 条目 operations/objects 为空表示"全部"；无法表达的剩余集合（例如从
      "所有操作"中只移除 write）抛 LimitRemovalAmbiguous，调用方保留原限制并标
      unresolved，不得猜测。
    * 只做结构化集合运算，不解析文字/名称/关键词。
    """
    result = [dict(item) for item in normalize_limits(list(parent or ()))]
    removals = normalize_limits(list(removed or ()))
    for removal in removals:
        rem_ops = {str(item) for item in (removal.get("operations") or ())}
        rem_objs = {
            (str(item.get("kind")), str(item.get("value")))
            for item in (removal.get("objects") or ())
            if isinstance(item, dict)
        }
        if not rem_ops and not rem_objs:
            raise LimitRemovalAmbiguous("解除限制条目既没有 operations 也没有 objects")
        rem_ops_all = not rem_ops
        rem_objs_all = not rem_objs
        touched = False
        next_result: list[dict] = []
        for entry in result:
            e_ops = {str(item) for item in (entry.get("operations") or ())}
            e_objs = {
                (str(item.get("kind")), str(item.get("value")))
                for item in (entry.get("objects") or ())
                if isinstance(item, dict)
            }
            e_ops_all = not e_ops
            e_objs_all = not e_objs
            # 交集是否存在（空维度=全部，不能用空集合表示"无交集"）
            ops_match = rem_ops_all or e_ops_all or bool(e_ops & rem_ops)
            objs_match = rem_objs_all or e_objs_all or bool(e_objs & rem_objs)
            if not (ops_match and objs_match):
                next_result.append(entry)
                continue
            if e_ops_all and not rem_ops_all:
                # 条目是"所有操作"；只解除部分操作后无法表达"除 write 外的所有操作"。
                raise LimitRemovalAmbiguous("无法精确表达剩余的操作集合（条目为所有操作）")
            if e_objs_all and not rem_objs_all:
                # 条目是"所有对象"；只解除部分对象后无法表达"除 B 外的所有对象"。
                raise LimitRemovalAmbiguous("无法精确表达剩余的对象集合（条目为所有对象）")
            touched = True
            reason = str(entry.get("reason") or "")
            if rem_ops_all:
                hit_ops = set(e_ops)  # e_ops_all 时为空=全部，left_ops 也为空
            elif e_ops_all:  # pragma: no cover - 上面的 ambiguous 分支已覆盖
                hit_ops = set(rem_ops)
            else:
                hit_ops = e_ops & rem_ops
            if rem_objs_all:
                hit_objs = set(e_objs)
            elif e_objs_all:  # pragma: no cover - 上面的 ambiguous 分支已覆盖
                hit_objs = set(rem_objs)
            else:
                hit_objs = e_objs & rem_objs
            left_ops = (e_ops - hit_ops) if not e_ops_all else set()
            left_objs = (e_objs - hit_objs) if not e_objs_all else set()
            if left_ops:
                next_result.append(_entry_with(left_ops, e_objs, reason))
            if left_objs:
                next_result.append(_entry_with(e_ops, left_objs, reason))
        if not touched:
            raise LimitRemovalAmbiguous(
                "解除限制无法精确匹配已有条目（未指定到具体 operations/objects）"
            )
        result = next_result
    return normalize_limits(result)


def merge_limits(
    parent: Iterable[dict] | None,
    *,
    added: Iterable[dict] = (),
    removed: Iterable[dict] = (),
) -> tuple[dict, ...]:
    """在已知父限制上追加/精确移除（amend 用）。

    * parent 必须是**已知**的结构化限制（tuple/list）；None 不是可合并状态，
      调用方应先处理 legacy/unresolved（保留未解决状态，不声称完整）；
    * added 去重后追加；removed 按 (operations, objects) 精确集合差移除（reason 不参与）；
    * removed 无法精确表达时抛 LimitRemovalAmbiguous（调用方保留原限制并标 unresolved）；
    * 只做集合运算，不解析文字、不做关键词判断。
    """
    if parent is None:
        raise ValueError("父限制未知（legacy/unresolved）时不能声称合并后的完整限制")
    result = list(normalize_limits(list(parent)))
    existing = {_canonical_entry(item, include_reason=False) for item in result}
    for entry in normalize_limits(list(added or ())):
        key = _canonical_entry(entry, include_reason=False)
        if key in existing:
            continue
        existing.add(key)
        result.append(entry)
    if removed:
        result = list(remove_limits_precise(result, removed))
    return normalize_limits(result)


def limits_from_requirement(requirement: object) -> tuple[dict, ...] | None:
    """从 UserRequirement 读取结构化限制。

    返回：
    * tuple（可能为空 = 明确无限制）：state=structured；
    * None：state legacy/unresolved、列缺失/NULL/非法（都表示"未知"，不是无限制）。
    """
    if requirement is None:
        return None
    state = _clean_text(getattr(requirement, "structured_limits_state", STATE_LEGACY))
    if state not in STRUCTURED_LIMIT_STATES:
        state = STATE_LEGACY
    if state != STATE_STRUCTURED:
        return None
    raw = getattr(requirement, "structured_limits", None)
    if raw is None:
        return None
    if isinstance(raw, (list, tuple)):
        try:
            return normalize_limits(list(raw))
        except ValueError:
            return None
    return deserialize_limits(raw)


def known_limits_from_requirement(requirement: object) -> tuple[dict, ...] | None:
    """读取该要求**已保存的已知**结构化限制（不因 state 不是 structured 而丢弃）。

    * state=structured：返回完整已知集合（可能空=明确无限制）；
    * state=legacy/unresolved：返回已保存的**部分**已知限制（tuple，可能空）；
      调用方不能把它当"完整集合"，只能用于继承/保留 known + 保持 unresolved；
    * 列 NULL/缺失/JSON 非法：None（确实未知，不是空限制）。
    """
    if requirement is None:
        return None
    raw = getattr(requirement, "structured_limits", None)
    if raw is None:
        return None
    if isinstance(raw, (list, tuple)):
        try:
            return normalize_limits(list(raw))
        except ValueError:
            return None
    return deserialize_limits(raw)


def state_is_known(state: object) -> bool:
    """该状态是否可以被当作"限制已知"（只有 structured 是）。"""
    return _clean_text(state) == STATE_STRUCTURED


__all__ = [
    "LIMIT_OBJECT_KINDS",
    "LIMIT_OPERATIONS",
    "MAX_LIMIT_ENTRIES",
    "MAX_OBJECTS_PER_LIMIT",
    "MAX_REASON_CHARS",
    "STATE_LEGACY",
    "STATE_STRUCTURED",
    "STATE_UNRESOLVED",
    "STRUCTURED_LIMIT_STATES",
    "LimitRemovalAmbiguous",
    "deserialize_limits",
    "known_limits_from_requirement",
    "limits_from_requirement",
    "merge_limits",
    "normalize_limits",
    "remove_limits_precise",
    "serialize_limits",
    "state_is_known",
]
