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
from agent.services import file_origins
from agent.tools import security_gate
from agent.tools.base import Tool, ToolResult
from agent.tools.blocking import get_blocking_executor

FIND_LIMIT = 50

# 取消但写入状态未确认时的准确文案（契约 4 规定不得漏报/误报）
CANCELLED_WRITE_UNCONFIRMED = "已取消，但写入可能已在后台完成（状态未确认）"

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
        internal_root = security_gate.match_internal_data(self.security, target)
        effect = security_gate.write_effect(
            "fs_write",
            target,
            purpose="写入文件内容",
            content_sha256=digest,
            identity=security_gate.identity_key(identity),
            exists=bool(identity.get("exists")),
            detail={
                "action": "write",
                "internal_data_root": str(internal_root) if internal_root else None,
                # 契约 §3.4：后端核实过的来源（新文件在创建前未知 → None）。
                # 模型传入的 origin 字段不参与判定。
                "file_origin": pre_record.origin if pre_record is not None else None,
                "file_exists": existed_before,
            },
            internal=internal_root is not None,
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
                "base_content_sha256": (
                    security_gate.content_sha256(target)
                    if identity.get("exists")
                    else None
                ),
                "content_sha256": digest,
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
        internal_root = security_gate.match_internal_data(self.security, target)
        effect = security_gate.write_effect(
            "fs_patch",
            target,
            purpose="修改文件内容",
            content_sha256=digest,
            identity=security_gate.identity_key(identity),
            exists=True,
            detail={
                "action": "patch",
                "old": old,
                "internal_data_root": str(internal_root) if internal_root else None,
                # 编辑不改变归属：后端核实过的来源随 effect 给授权引擎。
                "file_origin": pre_record.origin if pre_record is not None else None,
                "file_exists": True,
            },
            internal=internal_root is not None,
        )
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
                "base_content_sha256": security_gate.content_sha256(target),
                "content_sha256": digest,
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
            key = str(target).lower()
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
            found_root = security_gate.match_internal_data(self.security, target)
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
                    "file_origin": origin,
                    "file_origins": [{"path": str(target), "file_origin": origin}],
                    "internal_data_root": str(found_root) if found_root else None,
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
                    },
                    fingerprint={
                        "path": str(target),
                        "action": "delete",
                        "identity": identity,
                        "exists": exists,
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
            batch_effect = security_gate.make_effect(
                batch_kind,
                batch_effect_targets,
                purpose="批量把用户文件移入系统回收站（一次审批覆盖列明对象）",
                tool=self.name,
                detail={
                    "paths": [entry.payload["path"] for entry in waiting],
                    "items": [entry.payload for entry in waiting],
                    "deleted_to_recycle_bin": True,
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
