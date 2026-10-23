"""Filesystem tools for computer control (patch-style edits, redline guard).

统一路径收口：`resolve → normalize → containment → 权限判定 → 执行`。

* 相对路径（含省略 dir）一律以 ComputerSandbox.root() 为基准，
  **不用** process.cwd()；
* containment 用 resolve 之后的真实路径判断，所以 ..、绝对路径、
  指向根外的 symlink（含嵌套 symlink）都逃不出去；
* 所有权限判定都委派给注入的 ComputerSandbox；工具只执行判定结果
  （'auto' 直接执行、'approve' 走 ApprovalService、'deny' 直接拒绝）。

契约 4（响应性）：实际文件 I/O（open/read/write/replace/stat/iterdir/
scandir/_find_files）经 run_in_executor 移出事件循环（共享有界执行器，
见 agent/tools/blocking.py）；resolve → 权限判定 → 审批的顺序与语义
留在事件循环上，不变 —— 审批（人机交互）绝不进线程。

取消语义（fs_write / fs_patch）：用 asyncio.shield 等价机制等待/检查底层
future —— **不得宣布「没有执行」**。底层写入不可中断时返回 ok=False 且
描述准确（「已取消，但写入可能已在后台完成（状态未确认）」）；确认完成
时如实说明写入实际已完成；不误报成功、不漏报。只读工具（read/list/
find/info）取消时 CancelledError 照常传播（幂等、可安全重试）。
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from agent import prompts
from agent.security.review import object_state_digest, precondition_digest
from agent.services import file_origins
from agent.tools import security_gate
from agent.tools.base import Tool, ToolResult
from agent.tools.blocking import get_blocking_executor

FIND_LIMIT = 50

# 取消但写入状态未确认时的准确文案（契约 4 规定不得漏报/误报）
CANCELLED_WRITE_UNCONFIRMED = "已取消，但写入可能已在后台完成（状态未确认）"

# ==========================================================================
# R9-H4：实际影响（limit_impacts）/ 目录影响依据 / 批量路径去重（契约 §3、§6、§7）
# ==========================================================================

LIMIT_IMPACTS_KEY = "limit_impacts"
LIMIT_IMPACTS_COMPLETE_KEY = "limit_impacts_complete"
LIMIT_IMPACTS_REASON_KEY = "limit_impacts_reason"
DIRECTORY_IMPACT_KEY = "directory_impact"
DIRECTORY_IMPACT_DIGEST_KEY = "directory_impact_digest"
INTERNAL_DATA_ENTRIES_KEY = "internal_data_entries"

#: 目录影响枚举上限（有界；达到上限必须明确"不确定"，不能假装完整）。
DIRECTORY_IMPACT_MAX_ENTRIES = 500
DIRECTORY_IMPACT_MAX_DEPTH = 8
#: 只对不超过该大小的文件算内容摘要；更大的文件用（大小,mtime）参与条目摘要，
#: 避免为了审批去读整份大文件（有界，不做全内容扫描）。
DIRECTORY_CONTENT_HASH_MAX_BYTES = 512 * 1024


def path_dedupe_key(path: object, *, windows: bool | None = None) -> str:
    """按平台文件系统语义的去重键：Windows 规范化（大小写/分隔符不敏感），POSIX 原样。"""
    text = os.path.normpath(os.fspath(path))
    if windows is None:
        windows = os.name == "nt"
    return os.path.normcase(text) if windows else text


def make_limit_impact(operation: str, objects: Sequence[dict]) -> dict:
    """契约 §2：{operation, objects:[{kind,value}]}，objects 去重且保持顺序。"""
    seen: set[tuple[str, str]] = set()
    unique: list[dict] = []
    for item in objects or ():
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "").strip()
        value = str(item.get("value") or "")
        if not kind or not value:
            continue
        key = (kind, value)
        if key in seen:
            continue
        seen.add(key)
        unique.append({"kind": kind, "value": value})
    return {"operation": str(operation or "").strip(), "objects": unique}


def _impact_objects(paths: Sequence[object], *, kind: str = "path") -> list[dict]:
    return [{"kind": kind, "value": str(path)} for path in paths or ()]


def read_limit_impacts(path: object) -> list[dict]:
    return [make_limit_impact("read", _impact_objects([path]))]


def write_limit_impacts(
    path: object, *, created: bool = False, operation: str | None = None
) -> list[dict]:
    op = operation or ("create" if created else "write")
    return [make_limit_impact(op, _impact_objects([path]))]


def delete_limit_impacts(
    path: object,
    *,
    directory_impact_data: dict | None = None,
) -> list[dict]:
    """删除影响：普通文件 = 该 path；目录 = 目录自身 + 有界枚举到的所有子对象。"""
    if directory_impact_data is not None:
        return directory_limit_impacts(directory_impact_data)
    return [make_limit_impact("delete", _impact_objects([path]))]


def attach_limit_impacts(
    effect: Any,
    impacts: Sequence[dict],
    *,
    complete: bool = True,
    reason: str | None = None,
) -> None:
    """把实际影响写进 effect.detail（供引擎/审查/审批/执行前核对使用）。

    只向已存在的 detail dict 追加（Effect 本身是不可变 dataclass）；detail 不存在时无操作。
    """
    detail = getattr(effect, "detail", None)
    if not isinstance(detail, dict):
        return
    merged: list[dict] = []
    existing = detail.get(LIMIT_IMPACTS_KEY)
    if isinstance(existing, (list, tuple)):
        merged.extend(item for item in existing if isinstance(item, dict))
    merged.extend(item for item in impacts or () if isinstance(item, dict))
    detail[LIMIT_IMPACTS_KEY] = merged
    detail[LIMIT_IMPACTS_COMPLETE_KEY] = bool(complete)
    if not complete:
        detail[LIMIT_IMPACTS_REASON_KEY] = reason or "影响范围达到有界枚举上限，剩余影响未知"
    elif reason:
        detail[LIMIT_IMPACTS_REASON_KEY] = str(reason)


def aggregate_limit_impacts(impacts: Sequence[Sequence[dict]]) -> list[dict]:
    """批量合并：同 operation 的对象合并去重（汇总不漏项）。"""
    merged: dict[str, dict] = {}
    order: list[str] = []
    for group in impacts or ():
        for item in group or ():
            if not isinstance(item, dict):
                continue
            operation = str(item.get("operation") or "")
            objects = [entry for entry in (item.get("objects") or []) if isinstance(entry, dict)]
            if operation not in merged:
                merged[operation] = {"operation": operation, "objects": []}
                order.append(operation)
            candidate = make_limit_impact(operation, merged[operation]["objects"] + objects)
            merged[operation]["objects"] = candidate["objects"]
    return [merged[name] for name in order]


def _bounded_identity(path: Path) -> str | None:
    try:
        return security_gate.identity_key(security_gate.file_identity(path))
    except Exception:  # noqa: BLE001 - 身份读不到按不可确认
        return None


def _bounded_content_sha(path: Path) -> str | None:
    try:
        if path.stat().st_size > DIRECTORY_CONTENT_HASH_MAX_BYTES:
            return None
        return security_gate.content_sha256(path)
    except OSError:
        return None


def _entry_record(path: Path, *, is_protected: Any = None) -> dict:
    try:
        st = path.stat()
    except OSError:
        st = None
    try:
        is_dir = path.is_dir() and not path.is_symlink()
    except OSError:
        is_dir = False
    content_sha = None if is_dir else _bounded_content_sha(path)
    record = {
        "path": str(path),
        "kind": "directory" if is_dir else "file",
        "identity": _bounded_identity(path),
        # 内容摘要只对小文件计算；大文件用 size+mtime 参与条目摘要（有界）。
        "content_sha256": content_sha,
        "size": int(getattr(st, "st_size", 0) or 0) if st is not None else None,
        "mtime_ns": int(getattr(st, "st_mtime_ns", 0) or 0) if st is not None else None,
    }
    if callable(is_protected):
        try:
            record["protected"] = bool(is_protected(path))
        except Exception:  # noqa: BLE001 - 保护判定失败按未知（不冒充已检查）
            record["protected"] = None
    return record


def _impact_digest(impact: dict) -> str:
    payload = {
        "operation": impact.get("operation"),
        "root": impact.get("root"),
        "complete": bool(impact.get("complete")),
        "entries": sorted(
            [
                [
                    path_dedupe_key(entry.get("path")),
                    entry.get("kind"),
                    entry.get("identity"),
                    entry.get("content_sha256"),
                    entry.get("size"),
                    entry.get("mtime_ns"),
                    entry.get("protected"),
                ]
                for entry in (impact.get("entries") or [])
                if isinstance(entry, dict)
            ]
        ),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def directory_impact(
    root: object,
    *,
    operation: str = "delete",
    is_protected: Any = None,
    max_entries: int | None = None,
    max_depth: int | None = None,
) -> dict:
    """对目录做**有界**影响枚举：目录自身 + 子文件/子目录（含受保护/内部数据子对象）。

    * 不跟随目录符号链接（避免环/逃逸）；
    * 达到 max_entries / max_depth 时 complete=False、reason 明确，digest 仍可比较前缀，
      但调用方/执行前核对必须把它当"影响不确定"，不能假装完整；
    * 同一路径按平台语义排序，保证审批与执行比较一致。
    """
    base = Path(root)
    entries: list[dict] = []
    complete = True
    reason = ""
    max_entries = int(
        max_entries if max_entries is not None else DIRECTORY_IMPACT_MAX_ENTRIES
    )
    max_depth = int(
        max_depth if max_depth is not None else DIRECTORY_IMPACT_MAX_DEPTH
    )
    try:
        root_entry = _entry_record(base, is_protected=is_protected)
        root_entry["kind"] = "directory"
        entries.append(root_entry)
    except OSError:  # pragma: no cover - 极端路径
        pass
    stack: list[tuple[Path, int]] = [(base, 0)]
    seen_dirs: set[str] = set()
    while stack:
        current, depth = stack.pop(0)
        try:
            key = path_dedupe_key(current)
            if key in seen_dirs:
                continue
            seen_dirs.add(key)
            children = sorted(os.scandir(current), key=lambda item: path_dedupe_key(item.name))
        except OSError:
            complete = False
            reason = reason or f"目录无法完整读取：{current}"
            continue
        for entry in children:
            try:
                entry_path = Path(entry.path)
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                complete = False
                reason = reason or f"目录项状态无法读取：{entry.path}"
                continue
            if len(entries) >= max(1, int(max_entries)):
                complete = False
                reason = (
                    f"影响枚举达到上限 {max_entries} 项：剩余对象未枚举，影响不确定"
                )
                break
            entries.append(_entry_record(entry_path, is_protected=is_protected))
            if is_dir:
                if depth + 1 > max(0, int(max_depth)):
                    complete = False
                    reason = (
                        f"目录层级超过上限 {max_depth}：更深层对象未枚举，影响不确定"
                    )
                    continue
                stack.append((entry_path, depth + 1))
        if not complete and len(entries) >= max(1, int(max_entries)):
            break
    entries.sort(key=lambda item: (item.get("kind") != "directory", path_dedupe_key(item.get("path"))))
    impact = {
        "operation": str(operation or "delete"),
        "root": str(base),
        "complete": complete,
        "truncated": not complete,
        "reason": reason,
        "entries": entries,
        "entry_count": len(entries),
        "max_entries": int(max_entries),
        "max_depth": int(max_depth),
    }
    impact["digest"] = _impact_digest(impact)
    impact["internal_data_entries"] = [
        entry.get("path")
        for entry in entries
        if entry.get("protected") is True
    ]
    return impact


def directory_limit_impacts(impact: dict) -> list[dict]:
    """目录影响 → limit_impacts：目录对象 + 每个枚举子对象；不完整时补 path_prefix。"""
    objects: list[dict] = [{"kind": "directory", "value": str(impact.get("root") or "")}]
    for entry in impact.get("entries") or ():
        if not isinstance(entry, dict):
            continue
        value = str(entry.get("path") or "")
        if not value:
            continue
        if str(entry.get("path")) == str(impact.get("root")):
            continue
        kind = "directory" if entry.get("kind") == "directory" else "path"
        objects.append({"kind": kind, "value": value})
    if not impact.get("complete", True):
        prefix = os.path.join(str(impact.get("root") or ""), "")
        objects.append({"kind": "path_prefix", "value": prefix})
    return [make_limit_impact(impact.get("operation") or "delete", objects)]


def verify_directory_impact(
    expected: dict | None,
    root: object,
    *,
    is_protected: Any = None,
    max_entries: int | None = None,
    max_depth: int | None = None,
) -> dict:
    """执行前比较目录影响：新增/移除/替换（身份/内容/条目）→ 旧批准失效。

    * 审批时影响不完整（达到上限）→ 明确返不通过（不能假装没变化）；
    * 只做同一目录的有界重算，不扫描其它路径。
    """
    expected = dict(expected or {})
    if not expected:
        return {
            "ok": False,
            "code": "directory_impact_missing",
            "reason": "审批记录缺少目录影响依据：无法可靠比较，需重新确认",
            "added": [],
            "removed": [],
            "replaced": [],
        }
    current = directory_impact(
        root,
        operation=str(expected.get("operation") or "delete"),
        is_protected=is_protected,
        max_entries=int(max_entries or expected.get("max_entries") or DIRECTORY_IMPACT_MAX_ENTRIES),
        max_depth=int(max_depth or expected.get("max_depth") or DIRECTORY_IMPACT_MAX_DEPTH),
    )
    if not expected.get("complete", True):
        return {
            "ok": False,
            "code": "directory_impact_incomplete",
            "reason": str(
                expected.get("reason")
                or "审批时目录影响未完整枚举（达到上限）：无法确认没有新增影响，需重新确认"
            ),
            "added": [],
            "removed": [],
            "replaced": [],
            "expected_digest": expected.get("digest"),
            "current_digest": current.get("digest"),
        }
    expected_entries = {
        path_dedupe_key(entry.get("path")): entry
        for entry in expected.get("entries") or ()
        if isinstance(entry, dict)
    }
    current_entries = {
        path_dedupe_key(entry.get("path")): entry
        for entry in current.get("entries") or ()
        if isinstance(entry, dict)
    }
    added = sorted(
        str(current_entries[key].get("path"))
        for key in current_entries
        if key not in expected_entries
    )
    removed = sorted(
        str(expected_entries[key].get("path"))
        for key in expected_entries
        if key not in current_entries
    )
    replaced: list[str] = []
    for key in expected_entries:
        if key not in current_entries:
            continue
        before = expected_entries[key]
        after = current_entries[key]
        if (
            before.get("kind") != after.get("kind")
            or before.get("identity") != after.get("identity")
            or before.get("content_sha256") != after.get("content_sha256")
            or int(before.get("size") or 0) != int(after.get("size") or 0)
            or int(before.get("mtime_ns") or 0) != int(after.get("mtime_ns") or 0)
            or bool(before.get("protected")) != bool(after.get("protected"))
        ):
            replaced.append(str(after.get("path")))
    if not current.get("complete", True):
        return {
            "ok": False,
            "code": "directory_impact_incomplete_now",
            "reason": str(
                current.get("reason") or "执行前目录影响无法完整枚举：需重新确认"
            ),
            "added": added,
            "removed": removed,
            "replaced": replaced,
            "expected_digest": expected.get("digest"),
            "current_digest": current.get("digest"),
        }
    changed = bool(added or removed or replaced)
    return {
        "ok": not changed,
        "code": "" if not changed else "directory_impact_changed",
        "reason": (
            ""
            if not changed
            else "目录内容在审批/执行前发生变化（新增/移除/替换），旧批准失效：需重新确认"
        ),
        "added": added,
        "removed": removed,
        "replaced": replaced,
        "expected_digest": expected.get("digest"),
        "current_digest": current.get("digest"),
    }

# ==========================================================================
# R9-G：操作绑定 / 批量逐项 / 执行前重核对（契约 §3、§6、§7；W3）
# ==========================================================================

#: effect.detail 中的后端核实要求绑定键（模型参数无法写入这里）。
REQUIREMENT_BINDING_KEY = "requirement_binding"
REQUIREMENT_BINDING_SOURCE_KEY = "requirement_binding_source"

#: 批次项状态（契约 §6）：allowed / denied / waiting / invalidated / completed / failed。
BATCH_STATUS_ALLOWED = "allowed"
BATCH_STATUS_DENIED = "denied"
BATCH_STATUS_WAITING = "waiting"
BATCH_STATUS_INVALIDATED = "invalidated"
BATCH_STATUS_COMPLETED = "completed"
BATCH_STATUS_FAILED = "failed"


def _binding_payload(value: Any) -> dict | None:
    """RequirementBinding（dataclass/对象/dict）→ 可序列化 {requirement_id, version}。"""
    if value is None:
        return None
    if isinstance(value, dict):
        requirement_id = value.get("requirement_id") or value.get("id")
        version = value.get("version", 1)
    else:
        requirement_id = getattr(value, "requirement_id", None)
        version = getattr(value, "version", 1)
    if not requirement_id:
        return None
    try:
        version = int(version or 1)
    except (TypeError, ValueError):
        version = 1
    return {"requirement_id": str(requirement_id), "version": version}


def requirement_binding_from_context(gateway: Any = None, owner: Any = None) -> dict | None:
    """读取**后端注入**的当前用户要求绑定；绝不调用 requirements.current()。

    Lead 在 security/context.py 增加 RequirementBinding contextvar 后自动生效；
    也兼容 gateway 暴露的只读读取器和 app 注入到工具实例上的字段。
    读不到 → None（引擎按「没有可对照要求」走人工确认，工具不自行判定）。
    """
    candidates: list[Any] = []
    if owner is not None:
        for name in ("requirement_binding", "current_requirement_binding", "binding"):
            value = getattr(owner, name, None)
            getter = getattr(value, "__call__", None)
            candidates.append(getter() if callable(value) else value)
    if gateway is not None:
        for name in ("requirement_binding", "current_requirement_binding", "binding"):
            value = getattr(gateway, name, None)
            if value is not None:
                candidates.append(value() if callable(value) else value)
    try:
        from agent.security import context as security_context  # type: ignore

        for name in (
            "current_requirement_binding",
            "requirement_binding",
            "current_requirement",
        ):
            holder = getattr(security_context, name, None)
            getter = getattr(holder, "get", None)
            if callable(getter):
                try:
                    candidates.append(getter())
                except Exception:  # noqa: BLE001 - contextvar 未初始化
                    continue
    except Exception:  # noqa: BLE001 - Lead 的改动尚未合入
        pass
    for value in candidates:
        binding = _binding_payload(value)
        if binding is not None:
            return binding
    return None


def attach_requirement_binding(
    effect: Any, binding: dict | None, *, source: str | None = None
) -> dict | None:
    """把后端核实过的要求绑定放进 effect.detail（供审查/审批/执行前重核对）。"""
    detail = getattr(effect, "detail", None)
    if not isinstance(detail, dict):
        return binding
    detail[REQUIREMENT_BINDING_KEY] = binding
    detail[REQUIREMENT_BINDING_SOURCE_KEY] = (
        source or ("security.context" if binding is not None else "unavailable")
    )
    return binding


@dataclass
class BatchItem:
    """批量入口的统一展开项（契约 §6）。"""

    item_id: str
    effect: Any
    label: str = ""
    payload: dict = field(default_factory=dict)
    fingerprint: dict = field(default_factory=dict)
    status: str = "pending"
    reason: str = ""
    result: dict = field(default_factory=dict)

    def as_status(self) -> dict:
        data = {
            "item_id": self.item_id,
            "label": self.label,
            "path": self.payload.get("path"),
            "status": self.status,
            "reason": self.reason,
            "payload": dict(self.payload),
        }
        if self.result:
            data["result"] = dict(self.result)
        return data


@dataclass(frozen=True)
class RecheckOutcome:
    """执行前重核对结果（Lead 的 security/recheck.py 未合入时的接口同形状回退）。"""

    ok: bool
    code: str = ""
    reason: str = ""
    verdict: str = ""
    needs_user_decision: bool = False


def _recheck_service(gateway: Any) -> Any | None:
    """找 Lead 的 RecheckService：gateway 属性 → security.recheck 模块单例。"""
    for name in ("recheck", "recheck_service"):
        candidate = getattr(gateway, name, None)
        if candidate is not None and callable(getattr(candidate, "recheck", None)):
            return candidate
    try:
        from agent.security import recheck as recheck_module  # type: ignore
    except Exception:  # noqa: BLE001 - Lead 尚未合入 recheck.py
        return None
    for name in ("get_recheck_service", "default_recheck_service", "recheck_service"):
        candidate = getattr(recheck_module, name, None)
        if callable(candidate):
            try:
                candidate = candidate()
            except Exception:  # noqa: BLE001 - 单例初始化失败按不可用
                continue
        if candidate is not None and callable(getattr(candidate, "recheck", None)):
            return candidate
    return None


def _operation_fingerprint(
    effect: Any, binding: dict | None, mode: str, gateway: Any = None
) -> str:
    # Lead 的 security/recheck.py 已提供统一指纹（含对象身份/程序/脚本摘要/接收方/
    # 模式）时优先用它；两边必须用同一个函数，否则执行前重核对会误判内容变化。
    if gateway is not None and callable(getattr(gateway, "_review_operation", None)):
        try:
            from agent.security.review import operation_fingerprint as _lead_fingerprint

            return str(_lead_fingerprint(gateway._review_operation(effect), mode=mode))
        except Exception:  # noqa: BLE001 - 老分支/自定义网关退回本地实现
            pass
    payload = {
        "kind": str(getattr(getattr(effect, "kind", None), "value", getattr(effect, "kind", ""))),
        "targets": [
            {
                "kind": getattr(target, "kind", ""),
                "value": getattr(target, "value", ""),
                "identity": getattr(target, "identity", None),
            }
            for target in getattr(effect, "targets", ()) or ()
        ],
        "detail": {
            key: value
            for key, value in (getattr(effect, "detail", {}) or {}).items()
            if key != REQUIREMENT_BINDING_SOURCE_KEY
        },
        "requirement": binding,
        "mode": mode,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _build_operation_binding(
    *, effect: Any, binding: dict | None, gateway: Any, authorization_id: str | None = None
) -> Any | None:
    """按 Lead 的 security/recheck.py OperationBinding 构造（字段按 dataclass 过滤）。"""
    try:
        from agent.security.recheck import OperationBinding  # type: ignore
        from agent.security.requirements import RequirementBinding  # type: ignore
    except Exception:  # noqa: BLE001 - Lead 尚未合入
        return None
    mode = security_gate.mode_of(gateway)
    requirement = None
    if binding:
        try:
            requirement = RequirementBinding(
                requirement_id=str(binding["requirement_id"]),
                version=int(binding["version"]),
            )
        except Exception:  # noqa: BLE001 - 类型不一致时退回 dict
            requirement = binding
    # 内容/前置状态由本工具在 recheck_before_execute 里用 expected_identity /
    # expected_sha256 精确核对；对象身份已进统一指纹。这里不把 identity 字典
    # 误当内容摘要传给 RecheckService（否则新文件/身份字符串会被误判变化）。
    content_digests: list[tuple[str, str]] = []
    values = {
        "requirement": requirement,
        "mode": mode,
        "operation_kind": str(
            getattr(getattr(effect, "kind", None), "value", getattr(effect, "kind", ""))
        ),
        "fingerprint": _operation_fingerprint(effect, binding, mode, gateway),
        "spec_digest": None,
        "content_digests": tuple(content_digests),
        "conditions": (),
        "authorization_id": authorization_id,
        "delegation": None,
    }
    try:
        names = set(getattr(OperationBinding, "__dataclass_fields__", {}) or {})
        kwargs = {key: value for key, value in values.items() if key in names}
        return OperationBinding(**kwargs)
    except Exception:  # noqa: BLE001 - 构造函数差异按不可构造处理
        return None


async def _authorize_once(gateway: Any, effect: Any) -> Any:
    """调用网关 authorize；Lead 的 AuthContext 增加 requirement 字段后自动带上绑定。"""
    ctx = None
    binding = (getattr(effect, "detail", {}) or {}).get(REQUIREMENT_BINDING_KEY)
    if binding:
        try:
            from agent.security.gateway import AuthContext  # type: ignore

            names = set(getattr(AuthContext, "__dataclass_fields__", {}) or {})
            kwargs: dict = {}
            if "requirement" in names:
                try:
                    from agent.security.requirements import RequirementBinding  # type: ignore

                    kwargs["requirement"] = RequirementBinding(
                        requirement_id=str(binding["requirement_id"]),
                        version=int(binding["version"]),
                    )
                except Exception:  # noqa: BLE001
                    kwargs["requirement"] = binding
            if kwargs:
                ctx = AuthContext(**kwargs)
        except Exception:  # noqa: BLE001 - AuthContext 尚未扩展
            ctx = None
    if ctx is not None:
        try:
            return await gateway.authorize(effect, ctx=ctx)
        except TypeError:  # 旧 fake gateway 不接受 ctx
            pass
    return await gateway.authorize(effect)


async def authorize_item(gateway: Any, effect: Any) -> dict:
    """逐项判定（契约 §6：不得用一个对象的允许覆盖整批）。"""
    if gateway is None or not callable(getattr(gateway, "authorize", None)):
        return {
            "verdict": "approve",
            "reason": "统一授权网关未接入：需要人工确认本次操作",
            "approval_id": None,
            "decision": None,
        }
    try:
        decision = await _authorize_once(gateway, effect)
    except Exception as exc:  # noqa: BLE001 - 网关异常按拒绝（失败关闭）
        return {
            "verdict": "deny",
            "reason": f"授权网关调用失败：{type(exc).__name__}（本次没有执行）",
            "approval_id": None,
            "decision": None,
        }
    return {
        "verdict": security_gate.verdict_of(decision),
        "reason": security_gate.reason_of(decision, "统一授权网关没有给出原因"),
        "approval_id": security_gate.approval_id_of(decision),
        "decision": decision,
    }


async def recheck_before_execute(
    *,
    gateway: Any,
    effect: Any,
    mode_at_request: str = "",
    expected_identity: dict | None = None,
    expected_sha256: str | None = None,
) -> RecheckOutcome:
    """每个实际开始的文件操作执行前重核对（契约 §7）。

    优先调用 Lead 的 security/recheck.py::RecheckService；未合入时按同一接口语义回退：
    用**当前**模式重新走一次 gateway.authorize（模式/要求/授权/审批是否仍覆盖）
    + 校验要求绑定仍有效（requirements.validate_binding）+ 对象身份/内容摘要未变。
    """
    binding = (getattr(effect, "detail", {}) or {}).get(REQUIREMENT_BINDING_KEY)
    service = _recheck_service(gateway)
    if service is not None:
        operation_binding = _build_operation_binding(
            effect=effect, binding=binding, gateway=gateway
        )
        if operation_binding is None:
            return RecheckOutcome(
                False,
                "recheck_binding_unbuildable",
                "执行前无法构造操作绑定（security/recheck.py 接口不匹配）：本次未执行",
            )
        try:
            result = await service.recheck(operation_binding, effect=effect)
        except Exception as exc:  # noqa: BLE001 - 重核对异常按未通过（失败关闭）
            return RecheckOutcome(
                False,
                "recheck_service_error",
                f"执行前重核对失败（{type(exc).__name__}）：本次未执行",
            )
        ok = bool(getattr(result, "ok", False))
        verdict = str(getattr(result, "verdict", "") or "")
        return RecheckOutcome(
            ok,
            str(getattr(result, "code", "") or ("ok" if ok else "recheck_failed")),
            str(getattr(result, "reason", "") or ""),
            verdict=verdict,
            needs_user_decision=verdict == "approve",
        )
    # ---- 回退实现（与契约 §7 同语义；Lead 合入后自动改走上面） ----------------
    if gateway is not None and callable(getattr(gateway, "authorize", None)):
        decision = await authorize_item(gateway, effect)
        if decision["verdict"] == "deny":
            return RecheckOutcome(
                False,
                "recheck_denied",
                decision["reason"] or "当前授权已失效：本次未执行",
                verdict="deny",
            )
        if decision["verdict"] == "approve":
            return RecheckOutcome(
                False,
                "recheck_needs_user_decision",
                decision["reason"] or "当前状态需要用户确认后才能继续",
                verdict="approve",
                needs_user_decision=True,
            )
    if binding:
        ledger = getattr(gateway, "requirements", None) if gateway is not None else None
        validate = getattr(ledger, "validate_binding", None)
        if callable(validate):
            try:
                try:
                    from agent.security.requirements import RequirementBinding  # type: ignore

                    target_binding: Any = RequirementBinding(
                        requirement_id=str(binding["requirement_id"]),
                        version=int(binding["version"]),
                    )
                except Exception:  # noqa: BLE001
                    target_binding = binding
                valid = bool(validate(target_binding))
            except TypeError:
                try:
                    valid = bool(
                        validate(
                            requirement_id=binding["requirement_id"],
                            version=binding["version"],
                        )
                    )
                except Exception:  # noqa: BLE001 - 无法核实按失效处理
                    valid = False
            except Exception:  # noqa: BLE001
                valid = False
            if not valid:
                return RecheckOutcome(
                    False,
                    "recheck_requirement_invalid",
                    "对应的用户要求已被修正/撤回或版本不符：本次未执行",
                )
    path_targets = tuple(
        target
        for target in (getattr(effect, "targets", ()) or ())
        if getattr(target, "kind", "") == "path"
    )
    if path_targets:
        target_path = str(getattr(path_targets[0], "value", ""))
        if expected_identity:
            current = security_gate.file_identity(target_path)
            current_key = security_gate.identity_key(current)
            expected_key = security_gate.identity_key(expected_identity)
            if expected_key is not None and current_key != expected_key:
                return RecheckOutcome(
                    False,
                    "recheck_identity_changed",
                    "目标文件已不是原来的那一个（身份变化）：本次未执行",
                )
        if expected_sha256:
            current_sha = security_gate.content_sha256(target_path)
            if current_sha != expected_sha256:
                return RecheckOutcome(
                    False,
                    "recheck_content_changed",
                    "目标内容在执行前发生了变化：本次未执行",
                )
    return RecheckOutcome(True, "ok", "")



# -- 同步 I/O helper（在线程里执行；模块级以便测试替换/记录线程） ------------


def _read_file_text(path: Path) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _write_file(path: Path, content: str) -> None:
    """写入文件（原子：同目录临时文件 + os.replace；失败时原文件不变）。

    模块级函数是**测试缝**：取消/线程语义用例用闸门替换它来观察真实 I/O 线程。
    """
    security_gate.atomic_write_text(path, content)


def _write_file_text(path: Path, text: str) -> None:
    """写入文本（同 _write_file，保留旧名字作为测试缝）。"""
    security_gate.atomic_write_text(path, text)


def _list_dir(path: Path) -> list[str]:
    return [p.name for p in path.iterdir()]


def _stat_info(path: Path) -> os.stat_result:
    return path.stat()


class _FsTool(Tool):
    # security = 统一授权网关（契约 §2 的 AuthorizationGateway，W1 实现）。
    # origins = W3 的 FileOriginStore（可选注入；未注入时用进程默认 store，见
    # services/file_origins.py）。注册时由 ServiceRegistry.attach 注入；为 None 时
    # 只用于旧接线/单测回退。
    inject = ["computer", "approvals", "security", "origins"]

    def __init__(self, computer=None, approvals=None, security=None, origins=None) -> None:
        self.computer = computer
        self.approvals = approvals
        self.security = security
        self.origins = origins

    # -- 文件来源（契约 §3.4：后端核实后才给授权引擎，不信任模型传入值） ------

    def _origin_store(self):
        return self.origins if self.origins is not None else file_origins.default_store()

    def _origin_record(self, target: Path):
        store = self._origin_store()
        if store is None:
            return None
        try:
            return store.lookup(target)
        except Exception:  # noqa: BLE001 - 来源查询失败按未知处理，不打断文件操作
            return None

    def _file_origin(self, target: Path) -> str | None:
        record = self._origin_record(target)
        return record.origin if record is not None else None

    def _note_after_write(self, target: Path, pre_record, existed_before: bool, *, source: str) -> None:
        """写入落定后记账（在事件循环线程调用）。

        * 已记录的生成物/用户文件：原子替换会改变 OS 身份，更新身份但**保留来源**；
        * 写前不存在（本次创建）：记 qio_generated —— 必须在真实创建成功后；
        * 写前已存在但没有记录：观测为用户文件（user_original），绝不冒充生成物。
        """
        store = self._origin_store()
        if store is None:
            return
        try:
            if pre_record is not None:
                store.update_after_replace(pre_record, target)
            elif existed_before:
                store.record_existing_file(
                    target,
                    origin=file_origins.ORIGIN_USER_ORIGINAL,
                    source=source + ".observed_existing",
                )
            else:
                store.record_created(
                    target,
                    origin=file_origins.ORIGIN_QIO_GENERATED,
                    source=source,
                )
        except Exception:  # noqa: BLE001 - 记账失败不改变写入结果（安全侧按未知/用户文件）
            return

    def _operation_binding_record(self, effect) -> dict:
        """审查/授权/审批/重核对共用同一份操作绑定（R9-H §4）。

        指纹用 review.operation_fingerprint（引擎从真实 effect.detail 组装同一份
        ReviewOperation）；内容/前置状态/spec/条件/删除方式一并记录。
        引擎不可用时指纹为空字符串，审批/执行器按“无法核对绑定”显式处理。
        """
        mode = security_gate.mode_of(self.security, self.computer)
        review_operation = None
        getter = getattr(self.security, "_review_operation", None)
        if callable(getter):
            try:
                review_operation = getter(effect)
            except Exception:  # noqa: BLE001 - 组装失败按无指纹
                review_operation = None
        fingerprint = ""
        if review_operation is not None:
            try:
                from agent.security.review import operation_fingerprint

                fingerprint = str(operation_fingerprint(review_operation, mode=mode))
            except Exception:  # noqa: BLE001
                fingerprint = ""
        detail = dict(getattr(effect, "detail", {}) or {})
        execution = dict(detail.get("execution") or {})
        return {
            "fingerprint": fingerprint,
            "mode": mode,
            "content_digest": detail.get("content_digest")
            or detail.get("content_sha256"),
            "precondition_digest": detail.get("precondition_digest")
            or detail.get("base_content_sha256"),
            "spec_digest": detail.get("spec_digest")
            or execution.get("spec_digest"),
            "conditions": [dict(item) for item in (detail.get("conditions") or ()) if isinstance(item, dict)],
            "delete_mode": detail.get("delete_mode"),
        }

    def _resolve(self, raw: str) -> Path:
        """相对路径以工作区根为基准解析，返回规范化后的绝对路径。"""
        return self.computer.resolve_in_root(raw)

    async def _legacy_approve(self, payload: dict, denied: str) -> ToolResult | None:
        """旧审批通道（同一 ApprovalService）：网关不可用 / 网关不提供 submit 时使用。"""
        if self.approvals is None:
            return ToolResult(
                ok=False,
                error="这次操作需要你的批准，但当前没有可用的审批服务：本次没有执行。",
                category="permission",
                recoverable=False,
            )
        # 审批内容必须让普通用户看懂（spec 第 66~70 条）：把这次具体操作
        # 翻译成「想做什么 / 会访问什么 / 一次性还是长期」，原始 action
        # 仍然保留（高级详情用）。
        from agent.tools.approval_present import describe_computer_action

        described = dict(payload)
        described.update(describe_computer_action(payload))
        r = await self.approvals.request("computer", described)
        if r.decision != "approved":
            from agent.tools.approval import refusal_reason

            return ToolResult(
                ok=False,
                error=f"{payload.get('action')} 未获批准：{refusal_reason(r.decision)}",
            )
        return None

    async def _gateway_permitted(
        self,
        effect,
        payload: dict,
        denied: str,
        pending: tuple | None = None,
    ) -> ToolResult | None:
        """统一授权入口：allow 继续；deny 中文拒绝；approve 提交待审批操作（非阻塞）。"""
        decision = await security_gate.authorize(self.security, effect)
        verdict = security_gate.verdict_of(decision)
        if verdict == "allow":
            return None
        if verdict == "deny":
            return security_gate.denied_result(decision, denied)
        # approve：按契约 §2.1/§5.3 交给 submit()，工具立即返回 pending 结果。
        confirmations = (
            security_gate.INTERNAL_DATA_CONFIRMATIONS
            if effect.kind == security_gate.EffectKind.INTERNAL_DATA
            else 1
        )
        if pending is not None:
            kind, label, pending_payload, fingerprint = pending
            operation = security_gate.build_pending_operation(
                kind=kind,
                label=label,
                payload=pending_payload,
                effect=effect,
                fingerprint=fingerprint,
                purpose=effect.purpose,
                mode_at_request=security_gate.mode_of(self.security, self.computer),
                confirmations_required=confirmations,
                instruction=security_gate.instruction_snapshot(self.security),
            )
            try:
                ticket = await security_gate.submit_pending(self.security, operation)
            except Exception as exc:  # noqa: BLE001 - 提交失败要如实说，不能当作已提交
                return ToolResult(
                    ok=False,
                    error=f"提交审批失败：{type(exc).__name__}: {exc}（本次没有执行）",
                    category="permission",
                    recoverable=False,
                )
            if ticket is not None:
                return security_gate.pending_result(
                    ticket,
                    operation_label=label,
                    confirmations_required=confirmations,
                    extra={"path": payload.get("path"), "effect": security_gate.effect_to_dict(effect)},
                )
        # 网关要求批准但没有可提交的操作（或没有 submit）：退回同一个 ApprovalService，
        # 绝不自行执行；没有审批通道就明确说这次没执行。
        if self.approvals is not None:
            return await self._legacy_approve(payload, denied)
        return ToolResult(
            ok=False,
            error="这次操作需要你的批准，但当前没有可用的审批执行器：本次没有执行（执行边界暂不可用）。",
            category="permission",
            recoverable=False,
            code="approval_unavailable",
        )

    async def _permitted(
        self,
        verdict: str,
        payload: dict,
        denied: str,
        *,
        effect=None,
        pending: tuple | None = None,
    ) -> ToolResult | None:
        """把判定翻译成工具行为；返回非 None 表示应当直接返回。

        网关存在时一切以网关为准（ComputerSandbox 判定只作为旧接线回退，不再
        单独决定权限）；网关不存在时才走旧的 computer → approvals 路径。
        """
        if self.security is not None and effect is not None:
            return await self._gateway_permitted(effect, payload, denied, pending)
        if verdict == "deny":
            return ToolResult(ok=False, error=denied)
        if verdict == "approve":
            return await self._legacy_approve(payload, denied)
        return None

    # -- R9-G：批量 / 重核对（契约 §6、§7） ------------------------------------

    def _requirement_binding(self) -> dict | None:
        return requirement_binding_from_context(self.security, self)

    def _mode_at_request(self) -> str:
        return security_gate.mode_of(self.security, self.computer)

    async def _resubmit_pending(self, pending: tuple, effect) -> ToolResult | None:
        """重核对发现需要用户决策：把同一操作（已绑定要求/对象/内容）重新提交审批。"""
        if self.security is None or pending is None:
            return None
        kind, label, pending_payload, fingerprint = pending
        operation = security_gate.build_pending_operation(
            kind=kind,
            label=label,
            payload=pending_payload,
            effect=effect,
            fingerprint=fingerprint,
            purpose=effect.purpose,
            mode_at_request=self._mode_at_request(),
            confirmations_required=1,
            instruction=security_gate.instruction_snapshot(self.security),
        )
        try:
            ticket = await security_gate.submit_pending(self.security, operation)
        except Exception:  # noqa: BLE001 - 提交失败不能当作已提交
            return None
        if ticket is None:
            return None
        return security_gate.pending_result(
            ticket,
            operation_label=label,
            confirmations_required=1,
            extra={
                "recheck_requires_user_decision": True,
                "effect": security_gate.effect_to_dict(effect),
            },
        )

    async def _recheck_then_continue(
        self,
        effect,
        pending: tuple | None,
        *,
        mode_at_request: str,
        identity: dict | None = None,
        sha256: str | None = None,
    ) -> ToolResult | None:
        """每个实际开始的文件操作执行前重核对（契约 §7）；失败保留进度并明确状态。"""
        outcome = await recheck_before_execute(
            gateway=self.security,
            effect=effect,
            mode_at_request=mode_at_request,
            expected_identity=identity,
            expected_sha256=sha256,
        )
        if outcome.ok:
            return None
        if outcome.needs_user_decision and pending is not None:
            resubmitted = await self._resubmit_pending(pending, effect)
            if resubmitted is not None:
                return resubmitted
        category = "conflict" if outcome.code in (
            "recheck_identity_changed",
            "recheck_content_changed",
        ) else "permission"
        return ToolResult(
            ok=False,
            error=f"执行前重核对未通过：{outcome.reason or outcome.code}（本次没有执行）",
            code=outcome.code or "recheck_failed",
            category=category,
            recoverable=bool(outcome.needs_user_decision),
        )

    async def _submit_batch_pending(self, pending: tuple, effect) -> ToolResult | None:
        """一次明确批量授权 → **一个**审批操作（payload 逐项列明对象/动作/内容摘要）。"""
        kind, label, pending_payload, fingerprint = pending
        operation = security_gate.build_pending_operation(
            kind=kind,
            label=label,
            payload=pending_payload,
            effect=effect,
            fingerprint=fingerprint,
            purpose=effect.purpose,
            mode_at_request=self._mode_at_request(),
            confirmations_required=1,
            instruction=security_gate.instruction_snapshot(self.security),
        )
        try:
            ticket = await security_gate.submit_pending(self.security, operation)
        except Exception as exc:  # noqa: BLE001 - 提交失败要如实说
            return ToolResult(
                ok=False,
                error=f"提交批量审批失败：{type(exc).__name__}: {exc}（本次没有执行）",
                category="permission",
                recoverable=False,
            )
        if ticket is None:
            return None
        return security_gate.pending_result(
            ticket,
            operation_label=label,
            confirmations_required=1,
            extra={
                "batch": True,
                "effect": security_gate.effect_to_dict(effect),
            },
        )

    @staticmethod
    async def _run_blocking(fn, *args: Any, **kwargs: Any) -> Any:
        """把一次纯 I/O 调用交给共享有界执行器（线程身份由调用方断言）。"""
        loop = asyncio.get_running_loop()
        if kwargs:
            import functools

            fn = functools.partial(fn, **kwargs)
        return await loop.run_in_executor(get_blocking_executor(), fn, *args)

    @staticmethod
    async def _await_write_settled(fut: Any, target: Path, ok_message: str) -> ToolResult:
        """等待写入落定，并给出准确的结局（含取消语义）。

        正常完成 → ok=True；取消 → 等待/检查底层 future：
        - 写入实际完成 → ok=False 且说明「写入实际已完成」；
        - 等待也被二次取消 → ok=False +「已取消，但写入可能已在后台完成（状态未确认）」；
        - 底层写入失败 → ok=False + 失败原因。
        """
        try:
            await asyncio.shield(fut)
        except asyncio.CancelledError:
            # 取消：底层写入不可中断。shield 防止二次取消跳过状态检查。
            try:
                await asyncio.shield(fut)
            except asyncio.CancelledError:
                return ToolResult(ok=False, error=CANCELLED_WRITE_UNCONFIRMED)
            except OSError as exc:
                return ToolResult(ok=False, error=f"本轮已取消，且底层写入失败：{exc}")
            return ToolResult(ok=False, error=f"本轮已取消，且写入实际已完成：{target}")
        except OSError as exc:
            return ToolResult(ok=False, error=f"写入失败：{exc}")
        return ToolResult(ok=True, content=ok_message)


class FsReadTool(_FsTool):
    name = "fs_read"
    description = prompts.TOOL_DESCRIPTIONS["fs_read"]
    parameters = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
    is_concurrency_safe = True

    async def run(self, **kwargs: Any) -> ToolResult:
        path = str(kwargs.get("path") or "").strip()
        if not path:
            return ToolResult(ok=False, error="path 必填")
        target = self._resolve(path)
        effect = security_gate.read_effect("fs_read", target, purpose="读取文件内容")
        attach_limit_impacts(effect, read_limit_impacts(target))
        blocked = await self._permitted(
            self.computer.read_verdict(str(target)),
            {"action": "read", "path": str(target)},
            "拒绝访问该路径（敏感路径）",
            effect=effect,
        )
        if blocked is not None:
            return blocked
        try:
            text = await self._run_blocking(_read_file_text, target)
        except OSError as exc:
            return ToolResult(ok=False, error=f"读取失败：{exc}")
        return ToolResult(ok=True, content=text)


class FsWriteTool(_FsTool):
    name = "fs_write"
    description = prompts.TOOL_DESCRIPTIONS["fs_write"]
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
        "required": ["path", "content"],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        path = str(kwargs.get("path") or "").strip()
        content = str(kwargs.get("content") or "")
        if not path:
            return ToolResult(ok=False, error="path 必填")
        target = self._resolve(path)
        identity = security_gate.file_identity(target)
        existed_before = bool(identity.get("exists"))
        pre_record = self._origin_record(target)
        binding = self._requirement_binding()
        mode_at_request = self._mode_at_request()
        digest = security_gate.text_sha256(content)
        identity_key = security_gate.identity_key(identity)
        # R9-H §4：前置状态（覆盖前的对象身份 + 内容；新文件用 absent 标记）
        # 由真实工具写入 effect.detail，引擎从同一事实组装审查对象与复用键。
        base_content_sha = (
            security_gate.content_sha256(target) if existed_before else None
        )
        precondition = object_state_digest(
            path=target,
            exists=existed_before,
            identity=identity_key,
            content_sha256=base_content_sha,
            is_directory=False,
        )
        internal_root = security_gate.match_internal_data(self.security, target)
        effect = security_gate.write_effect(
            "fs_write",
            target,
            purpose="写入文件内容",
            content_sha256=digest,
            identity=identity_key,
            exists=bool(identity.get("exists")),
            detail={
                "action": "write",
                "internal_data_root": str(internal_root) if internal_root else None,
                # 契约 §3.4：后端核实过的来源（新文件在创建前未知 → None）。
                # 模型传入的 origin 字段不参与判定。
                "file_origin": pre_record.origin if pre_record is not None else None,
                "file_exists": existed_before,
                # R9-H §4 字段映射：拟写入内容摘要 + 覆盖前对象状态摘要。
                "content_digest": digest,
                "precondition_digest": precondition,
                "identity": identity_key,
            },
            internal=internal_root is not None,
        )
        attach_limit_impacts(
            effect, write_limit_impacts(target, created=not existed_before)
        )
        attach_requirement_binding(effect, binding)
        pending = (
            "fs.write",
            f"写入文件 {target}",
            {
                "path": str(target),
                "content": content,
                "content_sha256": digest,
                "requirement_binding": binding,
            },
            {
                "kind": "fs.write",
                "path": str(target),
                "identity": identity,
                # 提交审批时目标文件的内容摘要：执行前核对「审批期间内容有没有被改」。
                # 文件身份 volume+file_id 在编辑后不变，光靠身份挡不住覆盖别人刚改的内容。
                "base_content_sha256": base_content_sha,
                "content_sha256": digest,
                "precondition_digest": precondition,
                "identity_key": identity_key,
                "limit_impacts": write_limit_impacts(target, created=not existed_before),
                "limit_impacts_complete": True,
                "operation_binding": self._operation_binding_record(effect),
                "internal_data_root": str(internal_root) if internal_root else None,
                "requirement_binding": binding,
            },
        )
        blocked = await self._permitted(
            self.computer.write_verdict(str(target)),
            {"action": "write", "path": str(target)},
            "拒绝访问该路径（敏感路径）",
            effect=effect,
            pending=pending,
        )
        if blocked is not None:
            return blocked
        # 执行前重核对（契约 §7）：模式/要求/授权/审批是否仍覆盖 + 对象身份未变；
        # 授权失效时保留进度并给出明确状态，绝不按旧允许直接写。
        blocked_by_recheck = await self._recheck_then_continue(
            effect, pending, mode_at_request=mode_at_request, identity=identity
        )
        if blocked_by_recheck is not None:
            return blocked_by_recheck
        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(get_blocking_executor(), _write_file, target, content)
        result = await self._await_write_settled(fut, target, f"已写入 {target}")
        if result.ok:
            # 记账在真实创建/替换成功之后：创建前不存在 → generated；
            # 已有文件 → 保留原来源；没有记录 → user_original（不冒充生成物）。
            self._note_after_write(target, pre_record, existed_before, source="fs_write")
        return result


class FsPatchTool(_FsTool):
    name = "fs_patch"
    description = prompts.TOOL_DESCRIPTIONS["fs_patch"]
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "old": {"type": "string"},
            "new": {"type": "string"},
        },
        "required": ["path", "old", "new"],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        path = str(kwargs.get("path") or "").strip()
        old = str(kwargs.get("old") or "")
        new = str(kwargs.get("new") or "")
        if not path or not old:
            return ToolResult(ok=False, error="path 与 old 必填")
        target = self._resolve(path)
        identity = security_gate.file_identity(target)
        pre_record = self._origin_record(target)
        binding = self._requirement_binding()
        mode_at_request = self._mode_at_request()
        try:
            text = await self._run_blocking(_read_file_text, target)
        except OSError as exc:
            return ToolResult(ok=False, error=f"读取失败：{exc}")
        if old not in text:
            return ToolResult(ok=False, error="未找到待替换的内容 old")
        text = text.replace(old, new, 1)
        digest = security_gate.text_sha256(text)
        identity_key = security_gate.identity_key(identity)
        # R9-H §4：补丁内容的摘要 + 打补丁前（当前磁盘内容）的前置状态。
        base_content_sha = security_gate.content_sha256(target)
        precondition = object_state_digest(
            path=target,
            exists=True,
            identity=identity_key,
            content_sha256=base_content_sha,
            is_directory=False,
        )
        internal_root = security_gate.match_internal_data(self.security, target)
        effect = security_gate.write_effect(
            "fs_patch",
            target,
            purpose="修改文件内容",
            content_sha256=digest,
            identity=identity_key,
            exists=True,
            detail={
                "action": "patch",
                "old": old,
                "internal_data_root": str(internal_root) if internal_root else None,
                # 编辑不改变归属：后端核实过的来源随 effect 给授权引擎。
                "file_origin": pre_record.origin if pre_record is not None else None,
                "file_exists": True,
                "content_digest": digest,
                "precondition_digest": precondition,
                "identity": identity_key,
            },
            internal=internal_root is not None,
        )
        attach_limit_impacts(effect, write_limit_impacts(target))
        attach_requirement_binding(effect, binding)
        pending = (
            "fs.patch",
            f"修改文件 {target}",
            {
                "path": str(target),
                "old": old,
                "new": new,
                "expected_identity": identity,
                "content_sha256": digest,
                "requirement_binding": binding,
            },
            {
                "kind": "fs.patch",
                "path": str(target),
                "identity": identity,
                "base_content_sha256": base_content_sha,
                "content_sha256": digest,
                "precondition_digest": precondition,
                "identity_key": identity_key,
                "limit_impacts": write_limit_impacts(target),
                "limit_impacts_complete": True,
                "operation_binding": self._operation_binding_record(effect),
                "internal_data_root": str(internal_root) if internal_root else None,
                "requirement_binding": binding,
            },
        )
        blocked = await self._permitted(
            self.computer.write_verdict(str(target)),
            {"action": "patch", "path": str(target), "old": old},
            "拒绝访问该路径（敏感路径）",
            effect=effect,
            pending=pending,
        )
        if blocked is not None:
            return blocked
        # 执行前重核对（契约 §7）：模式/要求/授权仍覆盖 + 对象身份未变。
        base_sha = (pending[3] or {}).get("base_content_sha256")
        blocked_by_recheck = await self._recheck_then_continue(
            effect,
            pending,
            mode_at_request=mode_at_request,
            identity=identity,
            sha256=str(base_sha) if base_sha else None,
        )
        if blocked_by_recheck is not None:
            return blocked_by_recheck
        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(
            get_blocking_executor(), _write_file_text, target, text
        )
        result = await self._await_write_settled(fut, target, f"已编辑 {target}")
        if result.ok:
            # 编辑保留原来源；原子替换换掉的是 OS 身份，不是归属。
            self._note_after_write(target, pre_record, True, source="fs_patch")
        return result


class FsListTool(_FsTool):
    name = "fs_list"
    description = prompts.TOOL_DESCRIPTIONS["fs_list"]
    parameters = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
    is_concurrency_safe = True

    async def run(self, **kwargs: Any) -> ToolResult:
        path = str(kwargs.get("path") or "").strip()
        if not path:
            return ToolResult(ok=False, error="path 必填")
        target = self._resolve(path)
        effect = security_gate.read_effect("fs_list", target, purpose="列出目录内容")
        attach_limit_impacts(effect, read_limit_impacts(target))
        blocked = await self._permitted(
            self.computer.read_verdict(str(target)),
            {"action": "list", "path": str(target)},
            "拒绝访问该路径（敏感路径）",
            effect=effect,
        )
        if blocked is not None:
            return blocked
        try:
            entries = sorted(await self._run_blocking(_list_dir, target))
        except OSError as exc:
            return ToolResult(ok=False, error=f"列目录失败：{exc}")
        return ToolResult(ok=True, content="\n".join(entries) or "(空目录)")


class FsFindTool(_FsTool):
    name = "fs_find"
    description = prompts.TOOL_DESCRIPTIONS["fs_find"]
    parameters = {
        "type": "object",
        "properties": {"query": {"type": "string"}, "dir": {"type": "string"}},
        "required": ["query"],
    }
    is_concurrency_safe = True

    async def run(self, **kwargs: Any) -> ToolResult:
        query = str(kwargs.get("query") or "").strip()
        if not query:
            return ToolResult(ok=False, error="query 必填")
        directory = str(kwargs.get("dir") or "").strip()
        # 默认（以及相对路径）都以工作区根为基准，而不是后端进程的 cwd
        root = self._resolve(directory) if directory else self.computer.root()
        effect = security_gate.read_effect("fs_find", root, purpose="按文件名查找")
        attach_limit_impacts(effect, read_limit_impacts(root))
        blocked = await self._permitted(
            self.computer.read_verdict(str(root)),
            {"action": "find", "path": str(root)},
            "拒绝访问该路径（敏感路径）",
            effect=effect,
        )
        if blocked is not None:
            return blocked
        if not root.exists():
            return ToolResult(ok=False, error=f"目录不存在：{root}")
        try:
            matches = await self._run_blocking(_find_files, root, query, limit=FIND_LIMIT)
        except OSError as exc:
            return ToolResult(ok=False, error=f"查找失败：{exc}")
        return ToolResult(ok=True, content="\n".join(matches) or "(无匹配)")


class FsInfoTool(_FsTool):
    name = "fs_info"
    description = prompts.TOOL_DESCRIPTIONS["fs_info"]
    parameters = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
    is_concurrency_safe = True

    async def run(self, **kwargs: Any) -> ToolResult:
        path = str(kwargs.get("path") or "").strip()
        if not path:
            return ToolResult(ok=False, error="path 必填")
        target = self._resolve(path)
        effect = security_gate.read_effect("fs_info", target, purpose="读取文件信息")
        attach_limit_impacts(effect, read_limit_impacts(target))
        blocked = await self._permitted(
            self.computer.read_verdict(str(target)),
            {"action": "info", "path": str(target)},
            "拒绝访问该路径（敏感路径）",
            effect=effect,
        )
        if blocked is not None:
            return blocked
        try:
            st = await self._run_blocking(_stat_info, target)
        except OSError as exc:
            return ToolResult(ok=False, error=f"读取信息失败：{exc}")
        lines = [
            f"name: {target.name}",
            f"size: {st.st_size}",
            f"mtime: {int(st.st_mtime)}",
            f"path: {target}",
        ]
        return ToolResult(ok=True, content="\n".join(lines))


class FsDeleteTool(_FsTool):
    """删除用户文件：**唯一**入口进系统回收站（契约 §0.8/§3.1）。

    * 每个 path 逐条 resolve 成真实路径，构造一个 DELETE effect（多目标一次判定）；
    * ALLOW（指令明确删除授权 / QIO 生成文件）→ 直接走
      agent/services/recycle_bin 逐条移入系统回收站；
    * APPROVE → 提交 `fs.delete` 待审批操作（含每条的身份/存在性指纹），
      返回「已提交待批准」；批准后由审批执行器进回收站；
    * DENY → 中文拒绝；
    * 失败/回收站不可用 → 文件保留并如实报告，绝不 unlink。
    """

    name = "fs_delete"
    description = prompts.TOOL_DESCRIPTIONS["fs_delete"]
    parameters = {
        "type": "object",
        "properties": {
            "paths": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "description": prompts.argument_description("fs_delete", "paths"),
            }
        },
        "required": ["paths"],
    }

    def _legacy_delete_verdict(self, targets: list[Path]) -> str:
        """旧接线（没有网关）时的删除判定：红线 deny；其余按写权限语义。"""
        if self.computer is None:
            return "approve"
        worst = "auto"
        for target in targets:
            verdict = self.computer.write_verdict(str(target))
            if verdict == "deny":
                return "deny"
            if verdict == "approve":
                worst = "approve"
        return worst

    def _note_recycled(self, target: Path) -> None:
        store = self._origin_store()
        if store is None:
            return
        try:
            store.mark_missing(target)
        except Exception:  # noqa: BLE001 - 记账失败不改变删除结果
            return

    async def _recycle_each(self, targets: list[Path]) -> ToolResult:
        """逐条移入系统回收站；失败保留文件并如实报告（绝不永久删除）。"""
        mover = None
        try:
            from agent.services import recycle_bin

            candidate = getattr(recycle_bin, "send_to_recycle_bin", None)
            if callable(candidate):
                mover = candidate
        except Exception:  # noqa: BLE001 - 模块不可用按「回收站不可用」处理
            mover = None
        results: list[dict] = []
        failed: list[dict] = []
        for target in targets:
            if mover is None:
                item = {
                    "path": str(target),
                    "ok": False,
                    "reason": "回收站模块暂不可用：文件已保留，未删除（不会直接 unlink）",
                    "deleted_permanently": False,
                }
            else:
                try:
                    outcome = await asyncio.to_thread(mover, str(target))
                except Exception as exc:  # noqa: BLE001 - 失败保留文件
                    outcome = {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
                outcome = dict(outcome or {})
                item = {
                    "path": str(target),
                    "ok": bool(outcome.get("ok")),
                    "mechanism": outcome.get("mechanism"),
                    "reason": str(outcome.get("reason") or ""),
                    "trashed_path": outcome.get("trashed_path"),
                    "deleted_permanently": bool(outcome.get("deleted_permanently", False)),
                }
            results.append(item)
            if item["ok"]:
                # 已移入回收站：保留来源事实，只标记 missing（重启后仍可查历史归属）。
                self._note_recycled(Path(item["path"]))
            else:
                failed.append(item)
        lines = [
            f"{item['path']}："
            + ("已移入系统回收站" if item["ok"] else "未删除（" + (item["reason"] or "失败") + "）")
            for item in results
        ]
        data = {"results": results, "deleted_permanently": False}
        if failed:
            return ToolResult(
                ok=False,
                error=(
                    "部分文件没有移入系统回收站：失败项已保留文件（未做永久删除）。"
                    + "；".join(
                        item["path"] + "（" + (item["reason"] or "失败") + "）"
                        for item in failed
                    )
                ),
                content="\n".join(lines),
                data=data,
                code="recycle_partial_failure",
                recoverable=True,
            )
        return ToolResult(
            ok=True,
            content="已把 %d 个文件移入系统回收站：\n%s"
            % (len(results), "\n".join(lines)),
            data=data,
        )

    async def run(self, **kwargs: Any) -> ToolResult:
        raw_paths = kwargs.get("paths")
        if not isinstance(raw_paths, (list, tuple)) or not raw_paths:
            return ToolResult(ok=False, error="paths 必填（至少一个路径）")
        targets: list[Path] = []
        skipped: list[str] = []
        seen: set[str] = set()
        for raw in raw_paths:
            text = str(raw or "").strip()
            if not text:
                skipped.append("空路径")
                continue
            try:
                target = self._resolve(text)
            except Exception as exc:  # noqa: BLE001 - 无法定位的路径不能猜着删
                skipped.append(f"{text}（路径无法解析：{exc}）")
                continue
            # 平台语义去重：Windows 规范化（大小写/分隔符不敏感）；POSIX 大小写敏感。
            key = path_dedupe_key(target)
            if key in seen:
                continue
            seen.add(key)
            targets.append(target)
        if not targets:
            return ToolResult(
                ok=False,
                error="没有可删除的有效路径：" + "；".join(skipped),
            )

        binding = self._requirement_binding()
        mode_at_request = self._mode_at_request()

        # -- 逐项展开（契约 §6）：每个 path 独立 effect，逐项核对，不得互相覆盖 ------
        items: list[BatchItem] = []
        for index, target in enumerate(targets):
            identity = security_gate.file_identity(target)
            exists = bool(identity.get("exists"))
            identity_key = security_gate.identity_key(identity)
            origin_record = self._origin_record(target)
            origin = origin_record.origin if origin_record is not None else None
            is_directory = target.is_dir() if exists else False
            # R9-H4 §7：删除目录不能只查目录本身 —— 有界枚举子对象（含受保护/内部
            # 数据子对象）作为真实影响；达到上限时 complete=False（影响不确定）。
            directory_impact_data: dict | None = None
            if is_directory:
                try:
                    directory_impact_data = directory_impact(
                        target,
                        operation="delete",
                        is_protected=lambda path: (
                            security_gate.match_internal_data(self.security, path)
                            is not None
                        ),
                    )
                except Exception:  # noqa: BLE001 - 枚举失败按影响不确定处理
                    directory_impact_data = {
                        "operation": "delete",
                        "root": str(target),
                        "complete": False,
                        "truncated": True,
                        "reason": "目录影响枚举失败：影响不确定，需重新确认",
                        "entries": [],
                        "entry_count": 0,
                        "digest": "",
                    }
            item_content_sha = (
                security_gate.content_sha256(target)
                if exists and not is_directory
                else None
            )
            directory_digest = (
                str(directory_impact_data.get("digest") or "")
                if directory_impact_data is not None
                else None
            )
            limit_impacts = delete_limit_impacts(
                target, directory_impact_data=directory_impact_data
            )
            impacts_complete = (
                bool(directory_impact_data.get("complete", True))
                if directory_impact_data is not None
                else True
            )
            impacts_reason = (
                str(directory_impact_data.get("reason") or "")
                if directory_impact_data is not None
                else ""
            )
            # R9-H §4：删除前的对象状态摘要（身份 + 内容/目录影响摘要；不存在用 absent）。
            item_precondition = object_state_digest(
                path=target,
                exists=exists,
                identity=identity_key,
                content_sha256=(
                    directory_digest if is_directory else item_content_sha
                ),
                is_directory=is_directory,
            )
            found_root = security_gate.match_internal_data(self.security, target)
            internal_entries = (
                directory_impact_data.get("internal_data_entries") or []
                if directory_impact_data is not None
                else []
            )
            if found_root is None and internal_entries:
                # 目录本身不在注册路径下，但枚举到受保护/内部数据子对象：
                # 删除父目录同样属于内部数据维护（不能只查目录本身）。
                found_root = Path(str(internal_entries[0]))
            item_kind = (
                security_gate.EffectKind.INTERNAL_DATA
                if found_root is not None
                else security_gate.EffectKind.DELETE
            )
            item_id = f"item_{index + 1}"
            item_effect = security_gate.make_effect(
                item_kind,
                [security_gate.path_target(target, identity=identity_key, exists=exists)],
                purpose=f"把文件移入系统回收站：{target}",
                tool=self.name,
                detail={
                    "paths": [str(target)],
                    "batch_item_id": item_id,
                    "deleted_to_recycle_bin": True,
                    "delete_mode": "recycle_bin",
                    "file_origin": origin,
                    "file_origins": [{"path": str(target), "file_origin": origin}],
                    "internal_data_root": str(found_root) if found_root else None,
                    # R9-H §4：删除前状态进入审查对象/复用键。
                    "precondition_digest": item_precondition,
                    # R9-H4 §3/§7：真实删除影响（目录为枚举子对象；不完整明确标记）。
                    LIMIT_IMPACTS_KEY: limit_impacts,
                    LIMIT_IMPACTS_COMPLETE_KEY: impacts_complete,
                    LIMIT_IMPACTS_REASON_KEY: impacts_reason or None,
                    DIRECTORY_IMPACT_KEY: directory_impact_data,
                    DIRECTORY_IMPACT_DIGEST_KEY: directory_digest,
                    INTERNAL_DATA_ENTRIES_KEY: (
                        directory_impact_data.get("internal_data_entries") or []
                        if directory_impact_data is not None
                        else []
                    ),
                    "identity": identity_key,
                },
            )
            attach_requirement_binding(item_effect, binding)
            items.append(
                BatchItem(
                    item_id=item_id,
                    effect=item_effect,
                    label=str(target),
                    payload={
                        "path": str(target),
                        "action": "delete",
                        "identity": identity_key,
                        "exists": exists,
                        "file_origin": origin,
                        "directory_impact_digest": directory_digest,
                        "limit_impacts_complete": impacts_complete,
                    },
                    fingerprint={
                        "path": str(target),
                        "action": "delete",
                        "identity": identity,
                        "exists": exists,
                        "content_sha256": item_content_sha,
                        "is_directory": is_directory,
                        "precondition_digest": item_precondition,
                        "delete_mode": "recycle_bin",
                        "identity_key": identity_key,
                        "limit_impacts": limit_impacts,
                        "limit_impacts_complete": impacts_complete,
                        "limit_impacts_reason": impacts_reason or None,
                        "directory_impact": directory_impact_data,
                        "directory_impact_digest": directory_digest,
                        "internal_data_entries": (
                            directory_impact_data.get("internal_data_entries") or []
                            if directory_impact_data is not None
                            else []
                        ),
                        "operation_binding": self._operation_binding_record(item_effect),
                        "file_origin": origin,
                    },
                )
            )

        # -- 逐项判定：一个对象的允许绝不覆盖整批 ---------------------------------
        verdict_to_status = {
            "allow": BATCH_STATUS_ALLOWED,
            "approve": BATCH_STATUS_WAITING,
            "deny": BATCH_STATUS_DENIED,
        }
        for item in items:
            decision = await authorize_item(self.security, item.effect)
            item.status = verdict_to_status.get(decision["verdict"], BATCH_STATUS_DENIED)
            item.reason = decision["reason"]

        # -- 已允许项：逐项执行前重核对（模式/要求/授权/身份）---------------------
        for item in [entry for entry in items if entry.status == BATCH_STATUS_ALLOWED]:
            target = Path(str(item.payload.get("path")))
            recheck = await recheck_before_execute(
                gateway=self.security,
                effect=item.effect,
                mode_at_request=mode_at_request,
                expected_identity=item.fingerprint.get("identity"),
            )
            if not recheck.ok:
                if recheck.needs_user_decision:
                    item.status = BATCH_STATUS_WAITING
                    item.reason = recheck.reason or "执行前状态变化：需要重新确认"
                else:
                    item.status = BATCH_STATUS_INVALIDATED
                    item.reason = recheck.reason or "执行前重核对未通过"
                    item.result = {"ok": False, "reason": item.reason, "code": recheck.code}
                continue
            result = await self._recycle_each([target])
            item.status = BATCH_STATUS_COMPLETED if result.ok else BATCH_STATUS_FAILED
            item.reason = "" if result.ok else (result.error or "移入回收站失败")
            item.result = dict(result.data or {})

        # -- 需要确认的项合并为**一个**审批操作（payload items 列表，不逐项弹窗） ---
        waiting = [entry for entry in items if entry.status == BATCH_STATUS_WAITING]
        pending_result_obj: ToolResult | None = None
        if waiting:
            batch_effect_targets = [
                security_gate.path_target(
                    entry.payload["path"],
                    identity=entry.payload.get("identity"),
                    exists=bool(entry.payload.get("exists")),
                )
                for entry in waiting
            ]
            batch_kind = security_gate.EffectKind.DELETE
            if any(
                getattr(entry.effect.kind, "value", entry.effect.kind) == "internal_data"
                for entry in waiting
            ):
                batch_kind = security_gate.EffectKind.INTERNAL_DATA
            batch_precondition = precondition_digest(
                [
                    {
                        "path": entry.fingerprint.get("path"),
                        "exists": bool(entry.fingerprint.get("exists")),
                        "identity": security_gate.identity_key(
                            dict(entry.fingerprint.get("identity") or {})
                        ),
                        # 目录用有界影响摘要作为"内容摘要"参与批次前置状态。
                        "content_sha256": entry.fingerprint.get(
                            "directory_impact_digest"
                        )
                        or entry.fingerprint.get("content_sha256"),
                        "is_directory": bool(entry.fingerprint.get("is_directory")),
                    }
                    for entry in waiting
                ]
            )
            batch_limit_impacts = aggregate_limit_impacts(
                [entry.fingerprint.get("limit_impacts") or [] for entry in waiting]
            )
            batch_impacts_complete = all(
                bool(entry.fingerprint.get("limit_impacts_complete", True))
                for entry in waiting
            )
            batch_impacts_reason = "；".join(
                str(entry.fingerprint.get("limit_impacts_reason") or "")
                for entry in waiting
                if entry.fingerprint.get("limit_impacts_reason")
            )
            batch_internal_entries: list[str] = []
            for entry in waiting:
                batch_internal_entries.extend(
                    str(item)
                    for item in (entry.fingerprint.get("internal_data_entries") or [])
                )
            batch_effect = security_gate.make_effect(
                batch_kind,
                batch_effect_targets,
                purpose="批量把用户文件移入系统回收站（一次审批覆盖列明对象）",
                tool=self.name,
                detail={
                    "paths": [entry.payload["path"] for entry in waiting],
                    "items": [entry.payload for entry in waiting],
                    "deleted_to_recycle_bin": True,
                    "delete_mode": "recycle_bin",
                    # R9-H §4：批量前置状态摘要（任一对象变化 → 旧记录不命中）。
                    "precondition_digest": batch_precondition,
                    # R9-H4：批量真实影响汇总（逐项目标；不完整明确标记）。
                    LIMIT_IMPACTS_KEY: batch_limit_impacts,
                    LIMIT_IMPACTS_COMPLETE_KEY: batch_impacts_complete,
                    LIMIT_IMPACTS_REASON_KEY: batch_impacts_reason or None,
                    INTERNAL_DATA_ENTRIES_KEY: batch_internal_entries,
                    "requirement_binding": binding,
                },
            )
            label = (
                f"把 {len(waiting)} 个文件移入系统回收站（一次批量审批）"
                if len(waiting) > 1
                else f"把文件移入系统回收站：{waiting[0].payload['path']}"
            )
            batch_payload = {
                "paths": [entry.payload for entry in waiting],
                "items": [dict(entry.payload, item_id=entry.item_id) for entry in waiting],
                "reason": "删除用户文件（进系统回收站，不永久删除）",
                "requirement_binding": binding,
            }
            batch_fingerprint = {
                "kind": "fs.delete",
                "paths": [entry.fingerprint for entry in waiting],
                "items": [entry.fingerprint for entry in waiting],
                "deleted_to_recycle_bin": True,
                "delete_mode": "recycle_bin",
                "precondition_digest": batch_precondition,
                "identity_key": [entry.payload.get("identity") for entry in waiting],
                "limit_impacts": batch_limit_impacts,
                "limit_impacts_complete": batch_impacts_complete,
                "limit_impacts_reason": batch_impacts_reason or None,
                "directory_impacts": [
                    {
                        "path": entry.payload.get("path"),
                        "digest": entry.fingerprint.get("directory_impact_digest"),
                        "complete": entry.fingerprint.get(
                            "limit_impacts_complete", True
                        ),
                    }
                    for entry in waiting
                ],
                "internal_data_entries": batch_internal_entries,
                "operation_binding": self._operation_binding_record(batch_effect),
                "requirement_binding": binding,
            }
            submitted = await self._submit_batch_pending(
                ("fs.delete", label, batch_payload, batch_fingerprint), batch_effect
            )
            if submitted is None:
                return ToolResult(
                    ok=False,
                    error="这次批量删除需要你的批准，但当前没有可用的审批执行器：本次没有删除任何文件。",
                    code="approval_unavailable",
                    category="permission",
                    recoverable=False,
                )
            if submitted.code != "pending_approval":
                return submitted
            submitted.data = dict(submitted.data or {})
            submitted.data["batch"] = True
            submitted.data["items"] = [entry.as_status() for entry in items]
            submitted.data["results"] = [
                {
                    "path": entry.payload.get("path"),
                    "ok": entry.status == BATCH_STATUS_COMPLETED,
                    "status": entry.status,
                    "reason": entry.reason,
                }
                for entry in items
            ]
            pending_result_obj = submitted

        completed = [entry for entry in items if entry.status == BATCH_STATUS_COMPLETED]
        failed = [
            entry
            for entry in items
            if entry.status in (BATCH_STATUS_FAILED, BATCH_STATUS_INVALIDATED)
        ]
        denied = [entry for entry in items if entry.status == BATCH_STATUS_DENIED]
        statuses = [entry.as_status() for entry in items]
        results = [
            {
                "path": entry.payload.get("path"),
                "ok": entry.status == BATCH_STATUS_COMPLETED,
                "status": entry.status,
                "reason": entry.reason,
                **(entry.result or {}),
            }
            for entry in items
        ]
        data = {
            "batch": True,
            "items": statuses,
            "results": results,
            "pending_approval": pending_result_obj is not None,
            "deleted_permanently": False,
        }
        if pending_result_obj is not None:
            return pending_result_obj
        lines = []
        for entry in items:
            if entry.status == BATCH_STATUS_COMPLETED:
                lines.append(str(entry.payload.get("path")) + "：已移入系统回收站")
            elif entry.status == BATCH_STATUS_DENIED:
                lines.append(
                    str(entry.payload.get("path"))
                    + "：拒绝（"
                    + (entry.reason or "未获授权")
                    + "）"
                )
            else:
                lines.append(
                    str(entry.payload.get("path"))
                    + "：未删除（"
                    + (entry.reason or "失败")
                    + "）"
                )
        if failed or denied:
            return ToolResult(
                ok=False,
                error=(
                    "批量删除未全部完成：拒绝/失效/失败的项已保留文件（未做永久删除）。"
                    + "；".join(
                        str(entry.payload.get("path"))
                        + "（"
                        + (entry.reason or entry.status)
                        + "）"
                        for entry in (failed + denied)
                    )
                ),
                content="\n".join(lines),
                data=data,
                code="recycle_partial_failure",
                recoverable=True,
            )
        return ToolResult(
            ok=True,
            content="已把 %d 个文件移入系统回收站：\n%s" % (len(completed), "\n".join(lines)),
            data=data,
        )
def _find_files(root: Path, query: str, limit: int = FIND_LIMIT) -> list[str]:
    """不跟随符号链接的广度优先查找，且只返回仍在 root 内的真实文件。

    Path.rglob 在 Windows 上会沿着 symlink 目录走到根外（实测），所以这里
    用 os.scandir + follow_symlinks=False 自己走，并逐个结果做 containment。
    """
    base = Path(root).resolve()
    found: list[str] = []
    queue: list[Path] = [base]
    while queue and len(found) < limit:
        current = queue.pop(0)
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    queue.append(Path(entry.path))
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue  # symlink 文件也不返回：读它可能落在根外
            except OSError:
                continue
            if query in entry.name:
                resolved = Path(entry.path).resolve()
                if resolved.is_relative_to(base):
                    found.append(str(resolved))
                    if len(found) >= limit:
                        break
    return sorted(found)
