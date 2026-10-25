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
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

logger = logging.getLogger(__name__)

#: 结构化限制状态（持久化到 user_requirements.structured_limits_state）。
STATE_LEGACY = "legacy"
STATE_STRUCTURED = "structured"
STATE_UNRESOLVED = "unresolved"
STRUCTURED_LIMIT_STATES = frozenset({STATE_LEGACY, STATE_STRUCTURED, STATE_UNRESOLVED})

#: 限制对象条目上的程序绑定状态（R9-H11 §1/§4）。
#: * bound：已保存 program_binding（path + identity），执行时不再重新查找；
#: * unresolved：**保存时**后端候选事实不足（名称查不到/身份不可读），显式记录，
#:   不得当成"无绑定=无关"；需要专用恢复/澄清；
#: * missing：历史数据（R9-H10 之前保存）从未尝试绑定。
BINDING_STATE_BOUND = "bound"
BINDING_STATE_UNRESOLVED = "unresolved"
BINDING_STATE_MISSING = "missing"

#: 保存时解析失败的稳定依据码（有界字符串，写入 JSON 供审计/恢复重试）。
BINDING_FAILURE_LOOKUP_FAILED = "lookup_failed"
BINDING_FAILURE_IDENTITY_UNAVAILABLE = "identity_unavailable"
BINDING_FAILURE_RESOLVER_ERROR = "resolver_error"
BINDING_FAILURE_UNKNOWN = "unknown"
BINDING_FAILURES = frozenset(
    {
        BINDING_FAILURE_LOOKUP_FAILED,
        BINDING_FAILURE_IDENTITY_UNAVAILABLE,
        BINDING_FAILURE_RESOLVER_ERROR,
        BINDING_FAILURE_UNKNOWN,
    }
)
#: 候选事实只为审计保留有界路径（不存模型推理/密钥）。
MAX_BINDING_CANDIDATES = 4

#: 恢复采纳来源（R9-H12 §4）：只表示"采纳的是保存时的候选，且已被后端重新核验"。
#: 与保存时路径查找来源（path_lookup）严格区分，审计能看出绑定不是按当前 PATH 转移的。
BINDING_SOURCE_SAVED_CANDIDATE_VERIFIED = "saved_candidate_verified"

#: 恢复失败/歧义的稳定依据码（有界字符串，写入返回事实与日志，不落库）。
RECOVERY_REASON_NO_SAVED_CANDIDATES = "no_saved_candidates"
RECOVERY_REASON_CANDIDATE_UNVERIFIABLE = "saved_candidate_unverifiable"
RECOVERY_REASON_CANDIDATE_CONFLICT = "saved_candidate_conflict"


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


def _resolve_program_binding(name: str) -> dict | None:
    """把程序名称稳定绑定到保存时的真实程序对象（路径 + 文件身份）。

    R9-H9 §3：查找环境只提供候选事实；绑定在保存限制时固化，执行时不再用
    当前 PATH 重解析，避免同名程序随 PATH 顺序自动转移禁止对象。
    """
    import os
    import shutil

    try:
        found = shutil.which(name)
    except Exception:  # noqa: BLE001 - 查找失败按未绑定处理
        found = None
    if not found:
        return None
    try:
        from agent.services.file_identity import identity_of
        from pathlib import Path

        resolved = os.path.realpath(found)
        identity = identity_of(Path(resolved))
        if identity is None:
            return None
        return {
            "path": resolved,
            "identity": identity.key,
            "birth_key": identity.birth_key,
            "source": "path_lookup",
        }
    except Exception:  # noqa: BLE001 - 身份不可读按未绑定（执行时按 A 状态处理）
        return None


def _probe_program_binding(name: str) -> dict:
    """保存/恢复时的**后端只读候选事实**（唯一查找入口，不猜名称）。

    返回 {"binding": dict | None, "failure": 依据码, "candidates": (候选事实, ...)}：
    * 名称在当前 PATH 解析到对象且文件身份可读 → binding（path/identity/birth_key/source）；
    * 查不到 → failure=lookup_failed（没有候选事实）；
    * 查到了但身份不可读 → failure=identity_unavailable + 有界候选路径（审计用）。
    不写库、不改历史 JSON；是否落库/是否算恢复成功由调用方决定。
    """
    text = _clean_text(name)
    if not text:
        return {
            "binding": None,
            "failure": BINDING_FAILURE_LOOKUP_FAILED,
            "candidates": (),
        }
    try:
        binding = _resolve_program_binding(text)
    except Exception:  # noqa: BLE001 - 查找异常按未绑定事实（恢复失败，不猜测）
        binding = None
    if isinstance(binding, dict):
        path = _clean_text(binding.get("path"))
        identity = _clean_text(binding.get("identity"))
        if path and identity:
            return {
                "binding": {
                    "path": path,
                    "identity": identity,
                    "birth_key": _clean_text(binding.get("birth_key")) or None,
                    "source": _clean_text(binding.get("source")) or "path_lookup",
                },
                "failure": "",
                "candidates": (),
            }
    import os
    import shutil

    found = None
    try:
        found = shutil.which(text)
    except Exception:  # noqa: BLE001 - 查找失败按未绑定事实
        found = None
    if found:
        try:
            candidate_path = os.path.realpath(found)
        except Exception:  # noqa: BLE001
            candidate_path = str(found)
        return {
            "binding": None,
            "failure": BINDING_FAILURE_IDENTITY_UNAVAILABLE,
            "candidates": (
                {
                    "path": candidate_path[:MAX_VALUE_CHARS],
                    "source": "path_lookup",
                    "state": BINDING_STATE_UNRESOLVED,
                },
            ),
        }
    return {
        "binding": None,
        "failure": BINDING_FAILURE_LOOKUP_FAILED,
        "candidates": (),
    }


def _freeze(value: Any) -> Any:
    """把纯数据递归冻结为可哈希/可比较结构（不改变语义、不做任何查找）。"""
    if isinstance(value, dict):
        return tuple(sorted((str(key), _freeze(item)) for key, item in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _object_identity(obj: Mapping[str, Any]) -> tuple:
    """对象条目的**完整数据身份**（去重/作用域键；纯数据）。

    R9-H13 §3.A3：只按 (kind, value) 去重会把“同名同动作、分别绑定不同程序对象”
    的两条限制静默合并成一条（第二个对象从限制中消失、实际可执行）。身份键必须
    包含绑定对象/身份/生命周期、未解决事实、候选及来源含义。
    """
    payload = {
        str(key): _freeze(item)
        for key, item in obj.items()
        if key is not None and str(key) != "observed_at"
    }
    return (str(obj.get("kind") or ""), str(obj.get("value") or ""), _freeze(payload))


def _entry_data_key(entry: Mapping[str, Any]) -> tuple:
    """限制条目的完整数据键（含对象绑定/未解决事实；reason 不参与）。

    用途：amend 时区分“本次新增限制/明确修改对象”与“原样继承条目”——
    只有数据身份确实匹配本次新增内容的条目才允许重新探测绑定。
    """
    operations = tuple(sorted(str(item) for item in (entry.get("operations") or ())))
    objects = tuple(
        sorted(
            (_object_identity(item) for item in (entry.get("objects") or ()) if isinstance(item, Mapping)),
            key=repr,
        )
    )
    return (operations, objects)


def _has_saved_binding_facts(item: Mapping[str, Any]) -> bool:
    """对象条目是否已携带保存时的绑定/未解决事实（纯数据判定，不查找程序）。"""
    if not isinstance(item, Mapping):
        return False
    if program_binding_state(dict(item)) == BINDING_STATE_BOUND:
        return True
    for key in ("binding_state", "binding_failure", "binding_candidates"):
        if item.get(key):
            return True
    return False


def _normalize_binding_candidates(value: Any) -> list[dict]:
    """规范化对象条目上的**候选事实**（纯数据：不查找、不采集身份）。"""
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        return []
    candidates: list[dict] = []
    seen: set[tuple] = set()
    for raw in value:
        if not isinstance(raw, dict):
            continue
        path = _clean_text(raw.get("path"))[:MAX_VALUE_CHARS]
        if not path:
            continue
        item = {
            "path": path,
            "source": (_clean_text(raw.get("source")) or "path_lookup")[:80],
        }
        state = _clean_text(raw.get("state")).lower()
        if state in (BINDING_STATE_UNRESOLVED, BINDING_STATE_BOUND):
            item["state"] = state
        # R9-H12 §4：候选若已携带身份/生命周期事实，必须原样保留（纯数据）；
        # 恢复时用它们核验"保存候选仍是同一对象"，缺失时按路径当前身份采纳。
        identity = _clean_text(raw.get("identity"))[:MAX_VALUE_CHARS]
        if identity:
            item["identity"] = identity
        birth_key = _clean_text(raw.get("birth_key"))[:MAX_VALUE_CHARS]
        if birth_key:
            item["birth_key"] = birth_key
        candidate_key = _freeze(item)
        if candidate_key in seen:
            continue
        seen.add(candidate_key)
        candidates.append(item)
        if len(candidates) >= MAX_BINDING_CANDIDATES:
            break
    return candidates


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
        entry = {"kind": kind, "value": text}
        if kind == "program_name":
            binding = raw.get("program_binding")
            clean_binding = None
            if isinstance(binding, dict):
                bound_path = _clean_text(binding.get("path"))
                bound_identity = _clean_text(binding.get("identity"))
                if bound_path and bound_identity:
                    clean_binding = {
                        "path": bound_path,
                        "identity": bound_identity,
                        "birth_key": _clean_text(binding.get("birth_key")) or None,
                        "source": _clean_text(binding.get("source")) or "path_lookup",
                    }
                    for key in ("requirement_id", "requirement_version", "requirement_status"):
                        value = binding.get(key)
                        if value is not None:
                            clean_binding[key] = value
            # 规范化/反序列化只保留已保存绑定，绝不查找程序/采集身份。
            if clean_binding is not None:
                entry["program_binding"] = clean_binding
            else:
                # R9-H11 §1 / R9-H12 §4：显式保留"保存时解析失败"的事实
                # （binding_state / binding_failure / 候选）。读取/反序列化/
                # 合并/精确解除都只搬运纯数据，绝不重新查找或补建绑定。
                state = _clean_text(raw.get("binding_state")).lower()
                if state == BINDING_STATE_UNRESOLVED:
                    entry["binding_state"] = BINDING_STATE_UNRESOLVED
                failure = _clean_text(raw.get("binding_failure")).lower()
                if failure or state == BINDING_STATE_UNRESOLVED:
                    entry["binding_failure"] = (
                        failure if failure in BINDING_FAILURES else BINDING_FAILURE_UNKNOWN
                    )
                candidates = _normalize_binding_candidates(
                    raw.get("binding_candidates")
                )
                if candidates:
                    entry["binding_candidates"] = candidates
        objects.append(entry)
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


def attach_program_bindings(
    limits: Iterable[dict],
    *,
    requirement_id: str,
    version: int,
    status: str,
    fresh_limits: Iterable[dict] | None = None,
    preserve_saved_facts: bool = False,
) -> tuple[dict, ...]:
    """可信绑定创建入口：仅在要求保存/恢复流程显式调用（R9-H10 §1 / R9-H11 §1）。

    规范化/序列化/反序列化不调用本函数；读历史数据只能保留已有绑定。

    R9-H13 §3.A1（普通补充不得重新绑定旧禁止）：
    * ``fresh_limits`` 不是 None：**只有**数据身份匹配本次新增/明确修改条目的条目
      才允许探测创建绑定；其余条目视为**原样继承**——逐字段原样保留（包括
      program_binding / binding_state / binding_failure / binding_candidates 与
      候选来源），不重新查找、不重新绑定、不改写；
    * ``preserve_saved_facts``：对已携带绑定/未解决事实的对象原样保留（用于低层
      重写入口不重新探测历史事实）；
    * 两者都不给（默认）：保持 H11 的显式“创建绑定”语义（对所有未绑定对象探测），
      只供首次保存新要求/测试装配使用。恢复历史未解决绑定必须用
      ``recover_program_bindings``（保存候选优先），不得复用本函数。

    绑定事实：
    * 已有有效绑定：原样保留（含创建时的 requirement_* 事实），**不**按当前环境
      重新解析——同名程序不能随 PATH 变化自动转移禁止对象；
    * 本次首次提交：用后端只读候选事实（当前 PATH 查找 + 文件身份）创建绑定，记录
      requirement_id/version/status/path/identity/birth_key/source；
    * 候选事实不足：在对象条目上**显式**记录 binding_state=unresolved +
      binding_failure（lookup_failed / identity_unavailable / resolver_error）+
      有界候选路径，供专用恢复/澄清与审计使用；绝不伪造绑定。
    """
    normalized = normalize_limits(list(limits or ()))
    fresh_keys: set[tuple] | None = None
    if fresh_limits is not None:
        fresh_keys = {
            _entry_data_key(entry) for entry in normalize_limits(list(fresh_limits))
        }
    result: list[dict] = []
    for entry in normalized:
        copied = dict(entry)
        if fresh_keys is not None and _entry_data_key(copied) not in fresh_keys:
            # 原样继承：不得重新查找/绑定继承限制（H13 §3.A1）。
            result.append(copied)
            continue
        objects: list[dict] = []
        for obj in copied.get("objects") or ():
            item = dict(obj)
            if str(item.get("kind") or "").strip().lower() == "program_name":
                if preserve_saved_facts and _has_saved_binding_facts(item):
                    objects.append(item)
                    continue
                if program_binding_state(item) != BINDING_STATE_BOUND:
                    item.pop("program_binding", None)
                    try:
                        probe = _probe_program_binding(str(item.get("value") or ""))
                    except Exception:  # noqa: BLE001 - 查找异常=未绑定事实，不猜测
                        probe = {
                            "binding": None,
                            "failure": BINDING_FAILURE_RESOLVER_ERROR,
                            "candidates": (),
                        }
                    binding = probe.get("binding") if isinstance(probe, dict) else None
                    if (
                        isinstance(binding, dict)
                        and _clean_text(binding.get("path"))
                        and _clean_text(binding.get("identity"))
                    ):
                        clean_binding = {
                            "path": _clean_text(binding.get("path")),
                            "identity": _clean_text(binding.get("identity")),
                            "birth_key": _clean_text(binding.get("birth_key")) or None,
                            "source": _clean_text(binding.get("source"))
                            or "path_lookup",
                            "requirement_id": str(requirement_id),
                            "requirement_version": int(version),
                            "requirement_status": str(status),
                        }
                        item["program_binding"] = clean_binding
                        item.pop("binding_state", None)
                        item.pop("binding_failure", None)
                        item.pop("binding_candidates", None)
                    else:
                        failure = str(
                            (probe or {}).get("failure") or BINDING_FAILURE_UNKNOWN
                        )
                        item["binding_state"] = BINDING_STATE_UNRESOLVED
                        item["binding_failure"] = (
                            failure if failure in BINDING_FAILURES else BINDING_FAILURE_UNKNOWN
                        )
                        candidates = _normalize_binding_candidates(
                            (probe or {}).get("candidates")
                        )
                        if candidates:
                            item["binding_candidates"] = candidates
                        else:
                            item.pop("binding_candidates", None)
            objects.append(item)
        copied["objects"] = objects
        result.append(copied)
    return tuple(result)


def program_binding_state(item: object) -> str:
    """对象条目的绑定状态（纯数据判定）：bound / unresolved / missing。"""
    if not isinstance(item, dict):
        return BINDING_STATE_MISSING
    binding = item.get("program_binding")
    if (
        isinstance(binding, dict)
        and _clean_text(binding.get("path"))
        and _clean_text(binding.get("identity"))
    ):
        return BINDING_STATE_BOUND
    if _clean_text(item.get("binding_state")).lower() == BINDING_STATE_UNRESOLVED:
        return BINDING_STATE_UNRESOLVED
    return BINDING_STATE_MISSING


def unbound_program_objects(limits: Iterable[dict]) -> tuple[dict, ...]:
    """列出所有尚未绑定的 program_name 对象（只读，不查找程序）。

    结构化-but-unbound（R9-H11 问题一）由本函数显式识别：它们不是"无关"，而是
    需要专用恢复/澄清的对象。
    """
    result: list[dict] = []
    for entry_index, entry in enumerate(limits or ()):
        if not isinstance(entry, dict):
            continue
        for object_index, item in enumerate(entry.get("objects") or ()):
            if not isinstance(item, dict):
                continue
            if str(item.get("kind") or "").strip().lower() != "program_name":
                continue
            state = program_binding_state(item)
            if state == BINDING_STATE_BOUND:
                continue
            result.append(
                {
                    "entry_index": int(entry_index),
                    "object_index": int(object_index),
                    "value": str(item.get("value") or ""),
                    "state": state,
                    "failure": _clean_text(item.get("binding_failure")),
                }
            )
    return tuple(result)


def needs_binding_recovery(limits: Iterable[dict] | None) -> bool:
    """是否存在需要专用恢复/澄清的程序对象（缺失或保存时解析失败）。"""
    return bool(unbound_program_objects(limits or ()))


def binding_recovery_complete(limits: Iterable[dict] | None) -> bool:
    """限制集合里的 program_name 是否都已绑定到明确对象。"""
    return not unbound_program_objects(limits or ())


def binding_facts(limits: Iterable[dict] | None) -> dict:
    """绑定事实摘要（审计/日志用；不查找程序）。"""
    facts: dict = {
        "program_name": 0,
        "bound": 0,
        "unresolved": 0,
        "missing": 0,
        "sources": [],
    }
    for entry in limits or ():
        if not isinstance(entry, dict):
            continue
        for item in entry.get("objects") or ():
            if not isinstance(item, dict):
                continue
            if str(item.get("kind") or "").strip().lower() != "program_name":
                continue
            facts["program_name"] += 1
            state = program_binding_state(item)
            facts[state] = int(facts.get(state, 0)) + 1
            binding = item.get("program_binding")
            source = _clean_text(binding.get("source")) if isinstance(binding, dict) else ""
            if source and source not in facts["sources"]:
                facts["sources"].append(source)
    facts["sources"] = sorted(facts["sources"])
    return facts



# ---------------------------------------------------------------------------
# R9-H12 §4：历史未解决绑定的专用恢复（保存候选优先，绝不随 PATH 转移）
# ---------------------------------------------------------------------------


def _path_token(value: object) -> str:
    """路径的规范化比较 token（realpath + normcase）；不可解析返回空串。"""
    import os

    text = _clean_text(value)
    if not text:
        return ""
    try:
        absolute = os.path.abspath(text)
        resolved = os.path.realpath(absolute)
    except (OSError, ValueError):  # pragma: no cover - 极端路径
        return ""
    return os.path.normcase(os.path.normpath(resolved))


def _verify_saved_binding_candidate(candidate: object) -> dict:
    """后端核验**保存时的候选事实**是否仍有效（恢复采纳的唯一依据）。

    要求（R9-H12 §4）：候选路径现在存在、是普通文件、路径没有被换成别的位置、
    文件身份可读；候选若带 identity/birth 事实，必须与当前观测一致。
    调用方/模型自报不能替代本函数：只接受候选 dict，不执行程序、不读内容。
    """
    result: dict = {
        "verified": False,
        "path": "",
        "identity": None,
        "birth_key": None,
        "failure": "",
        "candidate_source": "path_lookup",
    }
    if not isinstance(candidate, dict):
        result["failure"] = "candidate_invalid"
        return result
    result["candidate_source"] = _clean_text(candidate.get("source"))[:80] or "path_lookup"
    path_text = _clean_text(candidate.get("path"))
    result["path"] = path_text
    if not path_text:
        result["failure"] = "candidate_path_missing"
        return result
    import os
    from pathlib import Path

    try:
        target = Path(os.path.abspath(path_text))
    except (OSError, ValueError):  # pragma: no cover - 极端路径
        result["failure"] = "candidate_path_missing"
        return result
    try:
        if not target.exists():
            result["failure"] = "candidate_path_missing"
            return result
        if not target.is_file():
            result["failure"] = "candidate_not_file"
            return result
    except OSError:  # pragma: no cover - 权限/平台异常按不可验证
        result["failure"] = "candidate_path_unreadable"
        return result
    # 保存的路径当时是 realpath；现在 realpath 变了说明路径指向被改（符号链接换目标）。
    stored_token = os.path.normcase(os.path.normpath(path_text))
    current_token = _path_token(path_text)
    if not current_token or stored_token != current_token:
        result["failure"] = "candidate_path_changed"
        return result
    try:
        from agent.services.file_identity import identity_of

        observed = identity_of(target)
    except Exception:  # noqa: BLE001 - 身份提供器异常=不可核验（失败关闭）
        observed = None
    if observed is None:
        result["failure"] = "candidate_identity_unavailable"
        return result
    recorded_identity = _clean_text(candidate.get("identity"))
    if recorded_identity and recorded_identity != str(observed.key):
        result["failure"] = "candidate_identity_mismatch"
        return result
    recorded_birth = _clean_text(candidate.get("birth_key"))
    observed_birth = _clean_text(observed.birth_key)
    if recorded_birth:
        if not observed_birth:
            result["failure"] = "candidate_lifecycle_unverifiable"
            return result
        if recorded_birth != observed_birth:
            result["failure"] = "candidate_lifecycle_mismatch"
            return result
    result.update(
        verified=True,
        path=os.path.realpath(path_text),
        identity=str(observed.key),
        birth_key=observed.birth_key,
    )
    return result


def _verify_adopted_binding(binding: object) -> str:
    """写回前重新核验已采纳绑定（path 未变、identity/birth 一致）；失败返回依据码。"""
    if not isinstance(binding, dict):
        return "binding_missing"
    path_text = _clean_text(binding.get("path"))
    identity_text = _clean_text(binding.get("identity"))
    if not path_text or not identity_text:
        return "binding_incomplete"
    import os
    from pathlib import Path

    try:
        target = Path(os.path.abspath(path_text))
        if not target.exists() or not target.is_file():
            return "binding_path_missing"
    except OSError:  # pragma: no cover - 极端路径
        return "binding_path_missing"
    if os.path.normcase(os.path.normpath(os.path.realpath(path_text))) != os.path.normcase(
        os.path.normpath(path_text)
    ):
        return "binding_path_changed"
    try:
        from agent.services.file_identity import identity_of

        observed = identity_of(target)
    except Exception:  # noqa: BLE001 - 身份提供器异常=无法确认
        observed = None
    if observed is None:
        return "binding_identity_unavailable"
    if str(observed.key) != identity_text:
        return "binding_identity_mismatch"
    recorded_birth = _clean_text(binding.get("birth_key"))
    if recorded_birth:
        observed_birth = _clean_text(observed.birth_key)
        if not observed_birth:
            return "binding_lifecycle_unverifiable"
        if recorded_birth != observed_birth:
            return "binding_lifecycle_mismatch"
    return ""


def _normalize_current_candidate_facts(value: object) -> tuple[dict, ...]:
    """把调用方/后端提供的**当前候选事实**规范化为审计用的有界纯数据。

    这些事实只用于审计与返回，**绝不**用于创建绑定（R9-H12 §4：无保存候选时
    不得用当前 PATH 结果直接绑定）。接受 {名称: 探测结果} 映射或事实序列。
    """

    def _one(name: str, raw: object) -> list[dict]:
        facts: list[dict] = []
        if not isinstance(raw, Mapping):
            return facts
        binding = raw.get("binding")
        if isinstance(binding, Mapping) and _clean_text(binding.get("path")):
            facts.append(
                {
                    "name": name,
                    "path": _clean_text(binding.get("path"))[:MAX_VALUE_CHARS],
                    "identity": _clean_text(binding.get("identity"))[:MAX_VALUE_CHARS]
                    or None,
                    "birth_key": _clean_text(binding.get("birth_key"))[:MAX_VALUE_CHARS]
                    or None,
                    "source": (_clean_text(binding.get("source")) or "path_lookup")[:80],
                    "failure": "",
                    "verified": False,
                    "audit_only": True,
                }
            )
            return facts
        candidates = raw.get("candidates")
        if isinstance(candidates, (list, tuple)) and candidates:
            for item in candidates:
                if not isinstance(item, Mapping):
                    continue
                facts.append(
                    {
                        "name": name,
                        "path": _clean_text(item.get("path"))[:MAX_VALUE_CHARS],
                        "identity": _clean_text(item.get("identity"))[:MAX_VALUE_CHARS]
                        or None,
                        "birth_key": _clean_text(item.get("birth_key"))[
                            :MAX_VALUE_CHARS
                        ]
                        or None,
                        "source": (_clean_text(item.get("source")) or "path_lookup")[:80],
                        "failure": _clean_text(raw.get("failure"))[:80],
                        "verified": False,
                        "audit_only": True,
                    }
                )
                if len(facts) >= MAX_BINDING_CANDIDATES:
                    return facts
            return facts
        if _clean_text(raw.get("path")) or _clean_text(raw.get("failure")):
            facts.append(
                {
                    "name": name,
                    "path": _clean_text(raw.get("path"))[:MAX_VALUE_CHARS],
                    "identity": _clean_text(raw.get("identity"))[:MAX_VALUE_CHARS] or None,
                    "birth_key": _clean_text(raw.get("birth_key"))[:MAX_VALUE_CHARS]
                    or None,
                    "source": (_clean_text(raw.get("source")) or "path_lookup")[:80],
                    "failure": _clean_text(raw.get("failure"))[:80],
                    "verified": False,
                    "audit_only": True,
                }
            )
        return facts

    result: list[dict] = []
    if value is None:
        return ()
    if isinstance(value, Mapping):
        for raw_name, raw in value.items():
            result.extend(_one(_clean_text(raw_name), raw))
    elif isinstance(value, (list, tuple)):
        for raw in value:
            if not isinstance(raw, Mapping):
                continue
            name = _clean_text(raw.get("name") or raw.get("value"))
            result.extend(_one(name, raw))
    return tuple(result)


def current_binding_candidates(limits: Iterable[dict] | None) -> tuple[dict, ...]:
    """当前 PATH 的只读候选事实（**审计专用**，绝不用于创建绑定）。

    只探测没有保存候选的未绑定 program_name：保存候选优先，恢复不得借当前 PATH
    把禁止对象转移到另一个同名程序（R9-H12 §4）。
    """
    facts: list[dict] = []
    for entry in normalize_limits(list(limits or ())):
        if not isinstance(entry, dict):
            continue
        for item in entry.get("objects") or ():
            if not isinstance(item, dict):
                continue
            if str(item.get("kind") or "").strip().lower() != "program_name":
                continue
            if program_binding_state(item) == BINDING_STATE_BOUND:
                continue
            if _normalize_binding_candidates(item.get("binding_candidates")):
                continue
            name = _clean_text(item.get("value"))
            try:
                probe = _probe_program_binding(name)
            except Exception:  # noqa: BLE001 - 查找异常=无候选事实
                probe = {
                    "binding": None,
                    "failure": BINDING_FAILURE_RESOLVER_ERROR,
                    "candidates": (),
                }
            if not isinstance(probe, dict):
                probe = {
                    "binding": None,
                    "failure": BINDING_FAILURE_RESOLVER_ERROR,
                    "candidates": (),
                }
            normalized = _normalize_current_candidate_facts({name: probe})
            if normalized:
                facts.extend(normalized)
            else:
                facts.append(
                    {
                        "name": name,
                        "path": "",
                        "identity": None,
                        "birth_key": None,
                        "source": "path_lookup",
                        "failure": _clean_text(probe.get("failure"))
                        or BINDING_FAILURE_LOOKUP_FAILED,
                        "verified": False,
                        "audit_only": True,
                    }
                )
    return tuple(facts)


@dataclass(frozen=True)
class BindingRecoveryOutcome:
    """专用绑定恢复的结果（结构化返回事实；limits 未采纳的对象保持原样）。

    * limits：恢复后的限制集合；只有 adopted 非空时才与输入不同；
    * adopted：真正采纳并核验过的对象事实；
    * unresolved：仍无法可靠绑定的对象事实（含原因与保存/当前候选事实）；
    * current_candidates：当前 PATH 候选事实（审计专用，未落库）；
    * sources：采纳来源集合（saved_candidate_verified + 原候选来源）；
    * complete：所有 program_name 是否都已绑定；changed：是否有采纳；
    * reason：未完成时的稳定原因码。
    """

    limits: tuple[dict, ...] = ()
    adopted: tuple[dict, ...] = ()
    unresolved: tuple[dict, ...] = ()
    current_candidates: tuple[dict, ...] = ()
    sources: tuple[str, ...] = ()
    reason: str = ""
    complete: bool = False
    changed: bool = False

    def as_dict(self) -> dict:
        return {
            "limits": [dict(item) for item in self.limits],
            "adopted": [dict(item) for item in self.adopted],
            "unresolved": [dict(item) for item in self.unresolved],
            "current_candidates": [dict(item) for item in self.current_candidates],
            "sources": list(self.sources),
            "reason": self.reason,
            "complete": bool(self.complete),
            "changed": bool(self.changed),
        }


def recover_program_bindings(
    saved_limits: Iterable[dict] | None,
    *,
    requirement_id: object,
    version: object,
    status: object,
    now_candidates: object = None,
) -> BindingRecoveryOutcome:
    """恢复历史未解决绑定：**保存候选优先**，不得按当前 PATH 转移禁止对象。

    R9-H12 §4：
    * 只处理无 binding 的 program_name；已有有效绑定原样保留（不随 PATH 转移）；
    * 保存候选（binding_candidates）中 path 现在存在且身份可读 -> 采纳该路径/
      identity/birth，source=saved_candidate_verified，记录要求 id/version/status；
    * 保存候选都不可验证 -> 不写绑定（limits 保持原样），记录 unresolved 事实；
    * 多个保存候选核验到不同对象 -> 冲突，不写绑定；
    * 无保存候选 -> 不用当前 PATH 结果绑定；now_candidates 只作为审计事实返回；
    * 不会修改传入对象；normalize/serialize 保持纯数据。
    """
    normalized = normalize_limits(list(saved_limits or ()))
    current = _normalize_current_candidate_facts(now_candidates)
    limits_out: list[dict] = []
    adopted: list[dict] = []
    unresolved: list[dict] = []
    for entry_index, entry in enumerate(normalized):
        copied = dict(entry)
        objects: list[dict] = []
        for object_index, obj in enumerate(copied.get("objects") or ()):
            item = dict(obj) if isinstance(obj, dict) else dict(obj or {})
            kind = str(item.get("kind") or "").strip().lower()
            value = _clean_text(item.get("value"))
            if kind != "program_name" or program_binding_state(item) == BINDING_STATE_BOUND:
                objects.append(item)
                continue
            candidates = _normalize_binding_candidates(item.get("binding_candidates"))
            matching_current = [
                dict(fact) for fact in current if _clean_text(fact.get("name")) == value
            ]
            if not candidates:
                unresolved.append(
                    {
                        "entry_index": int(entry_index),
                        "object_index": int(object_index),
                        "value": value,
                        "reason": RECOVERY_REASON_NO_SAVED_CANDIDATES,
                        "failure": _clean_text(item.get("binding_failure"))
                        or "no_saved_candidate",
                        "saved_candidates": [],
                        "current_candidates": matching_current,
                    }
                )
                objects.append(item)
                continue
            verified = [
                _verify_saved_binding_candidate(candidate) for candidate in candidates
            ]
            good = [fact for fact in verified if fact["verified"]]
            identities = {str(fact.get("identity")) for fact in good}
            if len(identities) > 1:
                unresolved.append(
                    {
                        "entry_index": int(entry_index),
                        "object_index": int(object_index),
                        "value": value,
                        "reason": RECOVERY_REASON_CANDIDATE_CONFLICT,
                        "failure": "candidate_conflict",
                        "saved_candidates": [
                            _clean_text(candidate.get("path")) for candidate in candidates
                        ],
                        "current_candidates": matching_current,
                    }
                )
                objects.append(item)
                continue
            if not good:
                failure = next(
                    (fact["failure"] for fact in verified if fact.get("failure")),
                    "candidate_unverifiable",
                )
                unresolved.append(
                    {
                        "entry_index": int(entry_index),
                        "object_index": int(object_index),
                        "value": value,
                        "reason": RECOVERY_REASON_CANDIDATE_UNVERIFIABLE,
                        "failure": failure,
                        "saved_candidates": [
                            _clean_text(candidate.get("path")) for candidate in candidates
                        ],
                        "current_candidates": matching_current,
                    }
                )
                objects.append(item)
                continue
            chosen = good[0]
            binding = {
                "path": _clean_text(chosen.get("path")),
                "identity": _clean_text(chosen.get("identity")),
                "birth_key": _clean_text(chosen.get("birth_key")) or None,
                "source": BINDING_SOURCE_SAVED_CANDIDATE_VERIFIED,
                "requirement_id": str(requirement_id),
                "requirement_version": int(version),
                "requirement_status": str(status),
            }
            item["program_binding"] = binding
            item.pop("binding_state", None)
            item.pop("binding_failure", None)
            item.pop("binding_candidates", None)
            adopted.append(
                {
                    "entry_index": int(entry_index),
                    "object_index": int(object_index),
                    "value": value,
                    "path": binding["path"],
                    "identity": binding["identity"],
                    "birth_key": binding["birth_key"],
                    "source": BINDING_SOURCE_SAVED_CANDIDATE_VERIFIED,
                    "candidate_source": _clean_text(chosen.get("candidate_source"))
                    or "path_lookup",
                    "requirement_id": binding["requirement_id"],
                    "requirement_version": binding["requirement_version"],
                    "requirement_status": binding["requirement_status"],
                }
            )
            objects.append(item)
        copied["objects"] = objects
        limits_out.append(copied)
    limits = normalize_limits(limits_out)
    complete = binding_recovery_complete(limits)
    reason = ""
    if not complete:
        reason = unresolved[0]["reason"] if unresolved else "recovery_incomplete"
    sources: list[str] = []
    if adopted:
        sources.append(BINDING_SOURCE_SAVED_CANDIDATE_VERIFIED)
        for fact in adopted:
            candidate_source = _clean_text(fact.get("candidate_source"))
            if candidate_source and candidate_source not in sources:
                sources.append(candidate_source)
    return BindingRecoveryOutcome(
        limits=limits,
        adopted=tuple(adopted),
        unresolved=tuple(unresolved),
        current_candidates=current,
        sources=tuple(sorted(sources)),
        reason=reason,
        complete=bool(complete),
        changed=bool(adopted),
    )


def verify_recovered_bindings(limits: Iterable[dict] | None) -> tuple[dict, ...]:
    """写回前重新核验本轮恢复采纳的绑定（path 未变、identity/birth 一致）。

    只核验 source=saved_candidate_verified 的绑定；返回无效事实（空=全部有效）。
    失败必须不写回（调用方保留原限制/known/候选/来源）。
    """
    invalid: list[dict] = []
    for entry_index, entry in enumerate(normalize_limits(list(limits or ()))):
        if not isinstance(entry, dict):
            continue
        for object_index, item in enumerate(entry.get("objects") or ()):
            if not isinstance(item, dict):
                continue
            if str(item.get("kind") or "").strip().lower() != "program_name":
                continue
            binding = item.get("program_binding")
            if not isinstance(binding, dict):
                continue
            if _clean_text(binding.get("source")) != BINDING_SOURCE_SAVED_CANDIDATE_VERIFIED:
                continue
            failure = _verify_adopted_binding(binding)
            if failure:
                invalid.append(
                    {
                        "entry_index": int(entry_index),
                        "object_index": int(object_index),
                        "value": _clean_text(item.get("value")),
                        "path": _clean_text(binding.get("path")),
                        "failure": failure,
                    }
                )
    return tuple(invalid)


def build_binding_recovery_source(
    *,
    requirement_text: object,
    chain_digest: object,
    version: object,
    sources: Iterable[object] = ("path_lookup",),
    origin: object = "",
) -> str:
    """专用恢复写回的来源（有界、可审计）：真实要求原话摘要 + 链 + 版本 + 候选来源。

    origin 保留恢复前已有的来源（例如历史保存依据），便于审计追溯。
    """
    import hashlib

    text = "" if requirement_text is None else str(requirement_text)
    text_digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    picked = sorted(
        {str(item) for item in (sources or ()) if str(item or "").strip()}
    ) or ["path_lookup"]
    parts = ["binding_recovery:" + "+".join(picked)]
    digest_text = str(chain_digest or "").strip()
    if digest_text:
        parts.append("chain=" + digest_text)
    parts.append("version=" + str(int(version)))
    origin_text = str(origin or "").strip()
    if origin_text:
        parts.append("origin=" + origin_text[:120])
    parts.append("text=" + text_digest)
    return ";".join(parts)[:300]


def _entry_with_objects(
    operations: Iterable[str], objects: Iterable[dict], reason: str
) -> dict:
    """保留对象原始字段（含 program_binding），避免精确解除时丢失稳定绑定。"""
    return {
        "operations": sorted({str(item) for item in operations}),
        "objects": [dict(item) for item in objects],
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
            e_obj_map = {
                (str(item.get("kind")), str(item.get("value"))): dict(item)
                for item in (entry.get("objects") or ())
                if isinstance(item, dict)
            }
            e_objs = set(e_obj_map)
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
                next_result.append(
                    _entry_with_objects(left_ops, e_obj_map.values(), reason)
                )
            if left_objs:
                next_result.append(
                    _entry_with_objects(
                        e_ops, [e_obj_map[key] for key in sorted(left_objs)], reason
                    )
                )
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
    "BINDING_FAILURES",
    "BINDING_SOURCE_SAVED_CANDIDATE_VERIFIED",
    "BINDING_FAILURE_IDENTITY_UNAVAILABLE",
    "BINDING_FAILURE_LOOKUP_FAILED",
    "BINDING_FAILURE_RESOLVER_ERROR",
    "BINDING_FAILURE_UNKNOWN",
    "BINDING_STATE_BOUND",
    "BINDING_STATE_MISSING",
    "BINDING_STATE_UNRESOLVED",
    "BindingRecoveryOutcome",
    "LIMIT_OBJECT_KINDS",
    "LIMIT_OPERATIONS",
    "MAX_BINDING_CANDIDATES",
    "MAX_LIMIT_ENTRIES",
    "MAX_OBJECTS_PER_LIMIT",
    "MAX_REASON_CHARS",
    "RECOVERY_REASON_CANDIDATE_CONFLICT",
    "RECOVERY_REASON_CANDIDATE_UNVERIFIABLE",
    "RECOVERY_REASON_NO_SAVED_CANDIDATES",
    "STATE_LEGACY",
    "STATE_STRUCTURED",
    "STATE_UNRESOLVED",
    "STRUCTURED_LIMIT_STATES",
    "LimitRemovalAmbiguous",
    "attach_program_bindings",
    "binding_facts",
    "binding_recovery_complete",
    "build_binding_recovery_source",
    "current_binding_candidates",
    "deserialize_limits",
    "known_limits_from_requirement",
    "limits_from_requirement",
    "merge_limits",
    "needs_binding_recovery",
    "normalize_limits",
    "program_binding_state",
    "recover_program_bindings",
    "remove_limits_precise",
    "serialize_limits",
    "state_is_known",
    "unbound_program_objects",
    "verify_recovered_bindings",
]
