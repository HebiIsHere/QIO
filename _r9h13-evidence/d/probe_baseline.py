# -*- coding: utf-8 -*-
"""R9-H13 D 基线行为探测（证据附件；不属于 pytest 收集）。

运行：cd backend; $env:PYTHONPATH="src;tests"; .\.venv\Scripts\python.exe ..\_r9h13-evidence\d\probe_baseline.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(os.environ.get("R9D_ROOT") or (Path(__file__).resolve().parents[2] / "backend"))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
os.environ.setdefault("QIO_DEV_INSECURE", "1")
os.environ.setdefault("QIO_DISABLE_DB_CHECK", "1")
os.environ.pop("QIO_DATA_DIR", None)

from agent.security.context import current_requirement  # noqa: E402
from agent.services.attachments import AttachmentService  # noqa: E402
from agent.tools.approved_operations import register_approved_operations  # noqa: E402
from _r9_auth_support import make_security  # noqa: E402

import tempfile  # noqa: E402
from agent.storage.db import connect  # noqa: E402
from agent.storage.migrate import apply_migrations  # noqa: E402


def fresh(tmp: Path):
    conn = connect(tmp / "test.db")
    apply_migrations(conn)
    return conn


def add_req(security, text="按你的要求执行"):
    ledger = security.requirements
    req = ledger.record_user_message(text=text, message_id="m1", turn_id="t1")
    binding = ledger.binding_for(req.requirement_id)
    return req, binding


def set_limits(security, req, limits, state="structured"):
    return security.requirements.set_structured_limits(
        req.requirement_id, limits, state=state, source="probe"
    )


def attach(security, tmp, conn):
    # 生产装配：RecheckService 挂在引擎上（app.py 同款），执行前重核对使用真实服务。
    from agent.security.recheck import RecheckService
    from agent.security.requirements import AuthorizationLedger

    if getattr(security.engine, "authorizations", None) is None:
        security.engine.authorizations = AuthorizationLedger(conn)
    service = AttachmentService(conn, tmp / "data", workspace_dir=tmp / "workspace")
    register_approved_operations(security.operations, attachments_getter=lambda: service)
    security.recheck = RecheckService(
        engine=security.engine,
        requirements=security.requirements,
        approvals=security.approvals,
        authorizations=security.engine.authorizations,
    )
    security.engine.recheck = security.recheck
    return service


def describe_pending(result):
    return {
        "ok": result.get("ok"),
        "pending": result.get("pending_approval"),
        "approval": result.get("approval"),
        "reason": result.get("reason"),
    }


async def run(title, fn):
    print("=" * 70)
    print("SCENARIO:", title)
    try:
        await fn()
    except Exception as exc:  # noqa: BLE001
        print("EXC:", type(exc).__name__, str(exc)[:400])


async def s1_unrestricted_normal(tmp, conn):
    security = make_security(tmp, conn, mode="unrestricted")
    req, binding = add_req(security)
    service = attach(security, tmp, conn)
    src = tmp / "report.txt"
    src.write_text("old", encoding="utf-8")
    att = service.register_source(path=str(src), source_kind="desktop_path")
    token = current_requirement.set(binding)
    try:
        res = await service.write_back_source(att.id, b"new", gateway=security.engine)
    finally:
        current_requirement.reset(token)
    print("result:", res)
    print("file:", src.read_text(encoding="utf-8"))


async def s2_restricted_pending_execute(tmp, conn):
    security = make_security(tmp, conn, mode="restricted")
    security.approvals._schedule_execute = lambda aid: False
    req, binding = add_req(security)
    service = attach(security, tmp, conn)
    src = tmp / "report.txt"
    src.write_text("old", encoding="utf-8")
    att = service.register_source(path=str(src), source_kind="desktop_path")
    token = current_requirement.set(binding)
    try:
        res = await service.write_back_source(att.id, b"new", gateway=security.engine)
        print("result:", describe_pending(res))
        aid = res["approval"]["approval_id"]
        row = security.approvals.store.get(aid)
        print("row requires_confirmations:", row["requires_confirmations"])
        op = row["operation_detail"]
        print("op confirmations_required:", op.get("confirmations_required"))
        print("fingerprint keys:", sorted((row["fingerprint"] or {}).keys()))
        await security.approvals.decide(aid, "approve")
        await security.approvals._execute_approved(aid)
        row = security.approvals.store.get(aid)
        print("state:", row["state"], "result:", (row.get("result") or {}).get("summary"))
        print(
            "result detail keys:",
            sorted(((row.get("result") or {}).get("detail") or {}).keys()),
        )
        print("file:", src.read_text(encoding="utf-8"))
    finally:
        current_requirement.reset(token)


async def s3_internal_attachment_legacy_row(tmp, conn):
    """历史错误行：operation.confirmations_required 写成 1，真实对象是内部日志。"""
    internal = tmp / "qio-internal"
    internal.mkdir()
    security = make_security(tmp, conn, mode="restricted", protected=[internal])
    security.approvals._schedule_execute = lambda aid: False
    log = internal / "app.log"
    log.write_text("old-log", encoding="utf-8")
    req, binding = add_req(security)
    service = attach(security, tmp, conn)
    att = service.register_source(path=str(log), source_kind="desktop_path")
    token = current_requirement.set(binding)
    try:
        res = await service.write_back_source(att.id, b"new-log", gateway=security.engine)
        print("result:", describe_pending(res))
        aid = (res.get("approval") or {}).get("approval_id")
        if not aid:
            print("no approval; direct result:", res)
            return
        row = security.approvals.store.get(aid)
        print("row requires_confirmations:", row["requires_confirmations"])
        raw = row["operation_detail"]
        raw["confirmations_required"] = 1
        conn.execute(
            "UPDATE pending_approvals SET operation = ? WHERE approval_id = ?",
            (json.dumps(raw, ensure_ascii=False), aid),
        )
        conn.commit()
        try:
            await security.approvals.decide(aid, "approve")
            print("DECIDE APPROVED (bug)")
        except Exception as exc:  # noqa: BLE001
            print("decide exc:", type(exc).__name__, str(exc)[:200])
        await security.approvals._execute_approved(aid)
        row = security.approvals.store.get(aid)
        print("state:", row["state"], "result:", (row.get("result") or {}).get("summary"))
        print("log:", log.read_text(encoding="utf-8"))
    finally:
        current_requirement.reset(token)


async def s4_read_ban_write(tmp, conn):
    security = make_security(tmp, conn, mode="unrestricted")
    req, binding = add_req(security)
    service = attach(security, tmp, conn)
    src = tmp / "secret.txt"
    src.write_text("secret-old", encoding="utf-8")
    att = service.register_source(path=str(src), source_kind="desktop_path")
    set_limits(
        security,
        req,
        [{"operation": "read", "objects": [{"kind": "path", "value": str(src)}]}],
    )
    req2 = security.requirements.by_id(req.requirement_id)
    print("limits state:", getattr(req2, "structured_limits_state", None))
    import agent.services.attachments as am

    real = am.content_sha256
    reads = []

    def spy(path):
        reads.append(str(path))
        return real(Path(path))

    am.content_sha256 = spy
    token = current_requirement.set(binding)
    try:
        try:
            res = await service.write_back_source(att.id, b"new", gateway=security.engine)
            print("result:", res if not isinstance(res, dict) else describe_pending(res))
        except Exception as exc:  # noqa: BLE001
            print("exception:", type(exc).__name__, str(exc)[:300])
        print("read calls:", len(reads))
        print("file:", src.read_text(encoding="utf-8"))
    finally:
        current_requirement.reset(token)
        am.content_sha256 = real


async def s5_e1_relocate_hardlink(tmp, conn):
    security = make_security(tmp, conn, mode="restricted")
    security.approvals._schedule_execute = lambda aid: False
    req, binding = add_req(security)
    service = attach(security, tmp, conn)
    allowed = tmp / "allowed"
    banned = tmp / "banned"
    allowed.mkdir()
    banned.mkdir()
    src = allowed / "doc.txt"
    src.write_text("old", encoding="utf-8")
    att = service.register_source(path=str(src), source_kind="desktop_path")
    set_limits(
        security,
        req,
        [
            {
                "operation": "write",
                "objects": [{"kind": "path_prefix", "value": str(banned)}],
            }
        ],
    )
    token = current_requirement.set(binding)
    try:
        res = await service.write_back_source(att.id, b"new", gateway=security.engine)
        print("result:", describe_pending(res))
        aid = (res.get("approval") or {}).get("approval_id")
        if not aid:
            return
        try:
            os.link(src, banned / "doc-link.txt")
            print("hardlink created")
        except OSError as exc:
            print("hardlink failed:", exc)
            return
        moved = service.relocate_reference(att.id, str(banned / "doc-link.txt"))
        print("relocated source_path:", moved.source_path)
        await security.approvals.decide(aid, "approve")
        await security.approvals._execute_approved(aid)
        row = security.approvals.store.get(aid)
        print("state:", row["state"])
        print("summary:", (row.get("result") or {}).get("summary"))
        detail = (row.get("result") or {}).get("detail") or {}
        print("detail path:", detail.get("path"))
        print("src content:", src.read_text(encoding="utf-8"))
        print("banned hardlink content:", (banned / "doc-link.txt").read_text(encoding="utf-8"))
    finally:
        current_requirement.reset(token)


async def s6_recycle(tmp, conn):
    import agent.services.recycle_bin as rb

    calls = []

    def fake_mover(path):
        calls.append(str(path))
        os.unlink(path)
        return {
            "ok": True,
            "mechanism": "fake_trash",
            "trashed_path": str(path) + ".trash",
        }

    real = getattr(rb, "send_to_recycle_bin", None)
    rb.send_to_recycle_bin = fake_mover
    try:
        security = make_security(tmp, conn, mode="restricted")
        security.approvals._schedule_execute = lambda aid: False
        req, binding = add_req(security)
        service = attach(security, tmp, conn)
        a = tmp / "a.txt"
        a.write_text("A-content", encoding="utf-8")
        att = service.register_source(path=str(a), source_kind="desktop_path")
        token = current_requirement.set(binding)
        try:
            res = await service.recycle_material_files(
                [{"attachment_id": att.id, "path": str(a), "reason": "probe"}],
                gateway=security.engine,
            )
            print("result:", describe_pending(res))
            aid = (res.get("approval") or {}).get("approval_id")
            if not aid:
                print("calls:", calls)
                return
            row = security.approvals.store.get(aid)
            print("fingerprint items:", row["fingerprint"].get("items"))
            await security.approvals.decide(aid, "approve")
            await security.approvals._execute_approved(aid)
            row = security.approvals.store.get(aid)
            print("state:", row["state"])
            print("summary:", (row.get("result") or {}).get("summary"))
            print("detail:", (row.get("result") or {}).get("detail"))
            print("mover calls:", calls)
        finally:
            current_requirement.reset(token)
    finally:
        if real is not None:
            rb.send_to_recycle_bin = real


async def s9_recycle_unrestricted(tmp, conn):
    import agent.services.recycle_bin as rb

    calls = []

    def fake_mover(path):
        calls.append(str(path))
        os.unlink(path)
        return {"ok": True, "mechanism": "fake_trash", "trashed_path": str(path) + ".trash"}

    real = getattr(rb, "send_to_recycle_bin", None)
    rb.send_to_recycle_bin = fake_mover
    try:
        security = make_security(tmp, conn, mode="unrestricted")
        req, binding = add_req(security)
        service = attach(security, tmp, conn)
        a = tmp / "a.txt"
        a.write_text("A-content", encoding="utf-8")
        att = service.register_source(path=str(a), source_kind="desktop_path")
        token = current_requirement.set(binding)
        try:
            res = await service.recycle_material_files(
                [{"attachment_id": att.id, "path": str(a), "reason": "probe"}],
                gateway=security.engine,
            )
            print("result:", res if "mover calls" not in str(res) else describe_pending(res))
            print("mover calls:", calls)
            print("exists:", a.exists())
        finally:
            current_requirement.reset(token)
    finally:
        if real is not None:
            rb.send_to_recycle_bin = real


async def s7_batch_write_back(tmp, conn):
    security = make_security(tmp, conn, mode="restricted")
    security.approvals._schedule_execute = lambda aid: False
    req, binding = add_req(security)
    service = attach(security, tmp, conn)
    a = tmp / "a.txt"
    b = tmp / "b.txt"
    a.write_text("A-old", encoding="utf-8")
    b.write_text("B-old", encoding="utf-8")
    att_a = service.register_source(path=str(a), source_kind="desktop_path")
    att_b = service.register_source(path=str(b), source_kind="desktop_path")
    token = current_requirement.set(binding)
    try:
        res = await service.write_back_many(
            [
                {"attachment_id": att_a.id, "data": b"A-new"},
                {"attachment_id": att_b.id, "data": b"B-new"},
            ],
            gateway=security.engine,
        )
        print("result:", describe_pending(res))
        aid = (res.get("approval") or {}).get("approval_id")
        if not aid:
            print("items:", res.get("items"))
            print("a:", a.read_text(encoding="utf-8"), "b:", b.read_text(encoding="utf-8"))
            return
        row = security.approvals.store.get(aid)
        print(
            "fingerprint item keys:",
            sorted((row["fingerprint"].get("items") or [{}])[0].keys()),
        )
        set_limits(
            security,
            req,
            [{"operation": "write", "objects": [{"kind": "path", "value": str(b)}]}],
            state="structured",
        )
        await security.approvals.decide(aid, "approve")
        await security.approvals._execute_approved(aid)
        row = security.approvals.store.get(aid)
        print("state:", row["state"])
        print("summary:", (row.get("result") or {}).get("summary"))
        detail = (row.get("result") or {}).get("detail") or {}
        print("item results:", detail.get("results"))
        print("a:", a.read_text(encoding="utf-8"), "b:", b.read_text(encoding="utf-8"))
    finally:
        current_requirement.reset(token)


async def s8_mode_change_batch(tmp, conn):
    security = make_security(tmp, conn, mode="restricted")
    security.approvals._schedule_execute = lambda aid: False
    req, binding = add_req(security)
    service = attach(security, tmp, conn)
    a = tmp / "a.txt"
    b = tmp / "b.txt"
    a.write_text("A-old", encoding="utf-8")
    b.write_text("B-old", encoding="utf-8")
    att_a = service.register_source(path=str(a), source_kind="desktop_path")
    att_b = service.register_source(path=str(b), source_kind="desktop_path")
    token = current_requirement.set(binding)
    try:
        res = await service.write_back_many(
            [
                {"attachment_id": att_a.id, "data": b"A-new"},
                {"attachment_id": att_b.id, "data": b"B-new"},
            ],
            gateway=security.engine,
        )
        aid = (res.get("approval") or {}).get("approval_id")
        print("result:", describe_pending(res))
        if not aid:
            return
        security.store.set("computer.permission_mode", "unrestricted")
        await security.approvals.decide(aid, "approve")
        await security.approvals._execute_approved(aid)
        row = security.approvals.store.get(aid)
        print("state:", row["state"], "summary:", (row.get("result") or {}).get("summary"))
        print("a:", a.read_text(encoding="utf-8"), "b:", b.read_text(encoding="utf-8"))
    finally:
        current_requirement.reset(token)


async def main():
    names = sys.argv[1:] or ["all"]
    for name in names:
        tmp = Path(tempfile.mkdtemp(prefix="r9dprobe_"))
        conn = fresh(tmp)
        try:
            if name in ("all", "s1"):
                await run("S1 unrestricted normal writeback", lambda: s1_unrestricted_normal(tmp, conn))
            if name in ("all", "s2"):
                await run("S2 restricted pending -> decide -> execute", lambda: s2_restricted_pending_execute(tmp, conn))
            if name in ("all", "s3"):
                await run("S3 internal attachment legacy row=1", lambda: s3_internal_attachment_legacy_row(tmp, conn))
            if name in ("all", "s4"):
                await run("S4 read ban vs writeback", lambda: s4_read_ban_write(tmp, conn))
            if name in ("all", "s5"):
                await run("S5 E1 relocate hardlink banned dir", lambda: s5_e1_relocate_hardlink(tmp, conn))
            if name in ("all", "s6"):
                await run("S6 recycle approval flow", lambda: s6_recycle(tmp, conn))
            if name in ("all", "s7"):
                await run("S7 batch writeback B ban", lambda: s7_batch_write_back(tmp, conn))
            if name in ("all", "s8"):
                await run("S8 batch mode change restricted->unrestricted", lambda: s8_mode_change_batch(tmp, conn))
            if name in ("all", "s9"):
                await run("S9 recycle unrestricted (direct allow)", lambda: s9_recycle_unrestricted(tmp, conn))
        finally:
            conn.close()


if __name__ == "__main__":
    asyncio.run(main())
