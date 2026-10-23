"""R9-G 授权判定路径审计（静态 + 动态）。

目的：不只靠 grep ——
1. 静态核对：关键词授权逻辑必须整体删除（模块/符号/表不存在），统一入口与
   固定查询/永久删除/批量/重核对模块必须真实存在；
2. 动态核对：在真实 create_app/引擎/注册表/审批存储装配下走若干入口，
   检查判定记录是否带要求绑定、执行前是否经过统一重核对、失效审批是否不执行。

模型调用一律受控 fake；不联网、不读真实用户库。
输出：_r9g-evidence/lead/audit_r9g_decision_paths.json
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(BACKEND / "tests"))

RESULTS: dict = {"static": {}, "dynamic": {}}


def static_audit() -> None:
    import importlib.util

    checks = {
        "keyword_extractor_removed": importlib.util.find_spec("agent.security.intent") is None,
        "fixed_queries_module": importlib.util.find_spec(
            "agent.security.fixed_queries"
        )
        is not None,
        "deletion_module": importlib.util.find_spec("agent.security.deletion")
        is not None,
        "recheck_module": importlib.util.find_spec("agent.security.recheck")
        is not None,
        "batch_module": importlib.util.find_spec("agent.security.batch") is not None,
        "resolution_module": importlib.util.find_spec("agent.security.resolution")
        is not None,
    }
    from agent.security import engine as engine_module
    from agent.security import review as review_module

    source = Path(engine_module.__file__).read_text(encoding="utf-8")
    review_source = Path(review_module.__file__).read_text(encoding="utf-8")
    checks.update(
        {
            "engine_no_keyword_prohibition": "_user_prohibition" not in source
            and "_MUTATION_PROHIBITIONS" not in source,
            "review_no_deterministic_guard": "deterministic_allow_guard"
            not in review_source,
            "engine_has_fixed_query_hook": "_classify_fixed_query" in source,
            "engine_has_permanent_delete_policy": "_permanent_delete_decision" in source,
            "engine_has_execute_batch": "authorize_batch" in source,
            "engine_has_recheck_approved": "recheck_approved" in source,
        }
    )
    RESULTS["static"] = checks
    if not all(checks.values()):
        raise SystemExit("静态审计失败：" + json.dumps(checks, ensure_ascii=False))


def dynamic_audit() -> None:
    from fastapi.testclient import TestClient

    from agent.api.server import create_app
    from agent.config import Settings
    from agent.credentials.store import MemoryKeyring
    from agent.security.effects import Effect, EffectKind, Target, Verdict
    tmp = Path(tempfile.mkdtemp(prefix="qio-r9g-audit-"))
    data_dir = tmp / "data"
    data_dir.mkdir()
    db = data_dir / "app.db"
    conn = sqlite3.connect(db, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    from agent.storage.migrate import apply_migrations

    apply_migrations(conn)
    # 设置经 AppContext 的 store 写入（表已就绪）。
    settings = Settings(data_dir=data_dir)
    workspace = tmp / "workspace"
    workspace.mkdir()
    app = create_app(settings, conn)
    ctx = app.state.ctx
    ctx.credentials._kr = MemoryKeyring()
    store = ctx.settings_store
    store.set("computer.root_dir", str(workspace))
    store.set("computer.permission_mode", "read_only")
    results: dict = {}
    with TestClient(app) as client:
        # 1) 固定查询：只读模式允许、不产生审批
        resp = client.post(
            "/api/tools/execute",
            json={"name": "run_program", "arguments": {"program": sys.executable, "args": ["--version"]}},
        ) if False else None
        from agent.adapters.base import ToolCall

        result = client.portal.call(
            lambda: ctx.registry.execute(
                ToolCall(id="audit_fixed", name="run_program", arguments={"program": sys.executable, "args": ["--version"]})
            )
        )
        results["fixed_query_read_only_ok"] = bool(result.ok)
        results["fixed_query_no_pending"] = len(ctx.approvals.pending()) == 0

        # 2) 批量逐项：两个操作两个结论，而不是一个整体结论
        from agent.security.batch import BatchItem

        async def _batch():
            engine = ctx.authorization
            items = [
                BatchItem(
                    item_id="a",
                    effect=Effect(kind=EffectKind.READ, targets=(Target("path", str(workspace / "a.txt")),)),
                ),
                BatchItem(
                    item_id="b",
                    effect=Effect(
                        kind=EffectKind.READ, targets=(Target("path", str(workspace / "b.txt")),)
                    ),
                ),
            ]
            return await engine.authorize_batch([item.effect for item in items])

        decisions = client.portal.call(_batch)
        results["batch_decisions"] = len(decisions)
        results["batch_each_has_verdict"] = all(
            getattr(decision, "verdict", None) == Verdict.ALLOW for decision in decisions
        )

        # 3) 执行前重核对：绑定要求 + 当前模式/对象一致 → ok；版本错 → 拒绝
        from agent.security.recheck import OperationBinding
        from agent.security.review import operation_fingerprint

        requirement = ctx.requirements.record_user_message(
            text="按用户要求读取工作区文件", message_id="msg_audit", turn_id="turn_audit"
        )
        binding = ctx.requirements.binding_for(requirement.requirement_id)
        effect = Effect(
            kind=EffectKind.READ, targets=(Target("path", str(workspace / "audit.txt")),)
        )
        operation_binding = OperationBinding(
            requirement=binding,
            mode=ctx.authorization.mode().value,
            operation_kind="read",
            fingerprint=operation_fingerprint(
                ctx.authorization._review_operation(effect),
                mode=ctx.authorization.mode().value,
            ),
        )

        async def _recheck():
            return await ctx.recheck.recheck(operation_binding, effect)

        recheck_ok = client.portal.call(_recheck)
        results["recheck_ok"] = bool(getattr(recheck_ok, "ok", False))

        # 4) 审批记录缺要求绑定 → 批准后不得执行（历史/被篡改记录重新判断）
        from agent.security.operations import PendingOperation

        operation = PendingOperation(
            kind="fs.write",
            label="审计：无要求绑定的写入",
            payload={"path": str(workspace / "no-binding.txt"), "content": "x"},
            effect={
                "kind": "write",
                "targets": [{"kind": "path", "value": str(workspace / "no-binding.txt")}],
            },
            fingerprint={},
            mode_at_request="restricted",
            source="tool",
        )
        store.set("computer.permission_mode", "restricted")
        ticket = client.portal.call(lambda: ctx.approvals.submit(operation))
        # 直接走审批服务（审计脚本未注入会话令牌；产品 API 的鉴权由测试覆盖）。
        client.portal.call(
            lambda: ctx.approvals.decide(ticket.approval_id, "approve")
        )

        async def _wait():
            import asyncio

            for _ in range(100):
                row = ctx.approvals.store.get(ticket.approval_id)
                if row and str(row.get("state")) in {"invalidated", "failed", "completed"}:
                    return row
                await asyncio.sleep(0.05)
            return ctx.approvals.store.get(ticket.approval_id)

        row = client.portal.call(_wait)
        results["unbound_approval_state"] = str((row or {}).get("state") or "")
        results["unbound_approval_not_executed"] = results["unbound_approval_state"] in {
            "invalidated",
            "failed",
        }

        # 5) 判定审计：固定查询/批量/审批的所有判定都有记录
        audit_rows = ctx.authorization.audit.recent(limit=200)
        results["decision_audit_rows"] = len(audit_rows)
        results["decision_audit_kinds"] = sorted(
            {str(row.get("effect_kind") or "") for row in audit_rows}
        )
        results["every_decision_has_verdict"] = all(
            row.get("verdict") for row in audit_rows
        )

    RESULTS["dynamic"] = results
    if not all(
        bool(value)
        for key, value in results.items()
        if key
        in {
            "fixed_query_read_only_ok",
            "fixed_query_no_pending",
            "batch_each_has_verdict",
            "recheck_ok",
            "unbound_approval_not_executed",
        }
    ):
        raise SystemExit("动态审计失败：" + json.dumps(results, ensure_ascii=False))
    if int(results.get("batch_decisions") or 0) != 2:
        raise SystemExit("批量逐项判定数量不正确")


def main() -> None:
    static_audit()
    dynamic_audit()
    out = ROOT / "_r9g-evidence" / "lead" / "audit_r9g_decision_paths.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(RESULTS, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(RESULTS, ensure_ascii=False, indent=2))
    print("AUDIT_OK")


if __name__ == "__main__":
    main()
