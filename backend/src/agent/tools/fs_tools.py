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
import os
from pathlib import Path
from typing import Any

from agent import prompts
from agent.tools import security_gate
from agent.tools.base import Tool, ToolResult
from agent.tools.blocking import get_blocking_executor

FIND_LIMIT = 50

# 取消但写入状态未确认时的准确文案（契约 4 规定不得漏报/误报）
CANCELLED_WRITE_UNCONFIRMED = "已取消，但写入可能已在后台完成（状态未确认）"


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
    # 注册时由 ServiceRegistry.attach 注入；为 None 时只用于旧接线/单测回退。
    inject = ["computer", "approvals", "security"]

    def __init__(self, computer=None, approvals=None, security=None) -> None:
        self.computer = computer
        self.approvals = approvals
        self.security = security

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
            },
            internal=internal_root is not None,
        )
        pending = (
            "fs.write",
            f"写入文件 {target}",
            {"path": str(target), "content": content, "content_sha256": digest},
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
        # 执行前重核对对象身份（契约 §3.1/§7）：审批期间文件被改过就不覆盖。
        same, reason = security_gate.recheck_identity(target, identity)
        if not same:
            return ToolResult(ok=False, error=reason, category="conflict", recoverable=False)
        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(get_blocking_executor(), _write_file, target, content)
        return await self._await_write_settled(fut, target, f"已写入 {target}")


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
            },
            internal=internal_root is not None,
        )
        pending = (
            "fs.patch",
            f"修改文件 {target}",
            {
                "path": str(target),
                "old": old,
                "new": new,
                "expected_identity": identity,
                "content_sha256": digest,
            },
            {
                "kind": "fs.patch",
                "path": str(target),
                "identity": identity,
                "base_content_sha256": security_gate.content_sha256(target),
                "content_sha256": digest,
                "internal_data_root": str(internal_root) if internal_root else None,
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
        # 读取与写入之间文件可能被改过：执行前重核对（同 fs_write）。
        same, reason = security_gate.recheck_identity(target, identity)
        if not same:
            return ToolResult(ok=False, error=reason, category="conflict", recoverable=False)
        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(
            get_blocking_executor(), _write_file_text, target, text
        )
        return await self._await_write_settled(fut, target, f"已编辑 {target}")


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
            if not item["ok"]:
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

        effect_targets: list[Any] = []
        payload_paths: list[dict] = []
        fingerprint_paths: list[dict] = []
        internal_root = None
        for target in targets:
            identity = security_gate.file_identity(target)
            exists = bool(identity.get("exists"))
            identity_key = security_gate.identity_key(identity)
            effect_targets.append(
                security_gate.path_target(target, identity=identity_key, exists=exists)
            )
            payload_paths.append(
                {"path": str(target), "identity": identity_key, "exists": exists}
            )
            fingerprint_paths.append(
                {"path": str(target), "identity": identity, "exists": exists}
            )
            found_root = security_gate.match_internal_data(self.security, target)
            if found_root is not None and internal_root is None:
                internal_root = found_root
        kind = (
            security_gate.EffectKind.INTERNAL_DATA
            if internal_root is not None
            else security_gate.EffectKind.DELETE
        )
        effect = security_gate.make_effect(
            kind,
            effect_targets,
            purpose="把用户文件移入系统回收站（不永久删除）",
            tool=self.name,
            detail={
                "paths": [str(target) for target in targets],
                "internal_data_root": str(internal_root) if internal_root else None,
                "deleted_to_recycle_bin": True,
            },
        )
        fingerprint = {
            "kind": "fs.delete",
            "paths": fingerprint_paths,
            "deleted_to_recycle_bin": True,
        }
        if internal_root is not None:
            fingerprint["internal_data_root"] = str(internal_root)
        label = (
            f"把 {len(targets)} 个文件移入系统回收站"
            if len(targets) > 1
            else f"把文件移入系统回收站：{targets[0]}"
        )
        pending = (
            "fs.delete",
            label,
            {
                "paths": payload_paths,
                "reason": "删除用户文件（进系统回收站，不永久删除）",
            },
            fingerprint,
        )
        blocked = await self._permitted(
            self._legacy_delete_verdict(targets),
            {"action": "fs_delete", "paths": [str(target) for target in targets]},
            "拒绝删除该路径（敏感路径）",
            effect=effect,
            pending=pending,
        )
        if blocked is not None:
            return blocked
        return await self._recycle_each(targets)


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
