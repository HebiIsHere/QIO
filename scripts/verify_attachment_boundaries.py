"""附件边界的手工验收：真实 100MB 复制 + 引用型文件的生命周期（不进 CI，不拖慢常规套件）。

为什么是脚本而不是用例：精确等于阈值的「真实复制」要写 100MB 到磁盘，
放进 pytest 会让每次全量都变慢、并在磁盘紧张时假红。阈值分类与复制失败路径
已由 backend/tests/test_attachments_service.py 用稀疏文件与注入失败覆盖；
这里只在需要**真机取证**时手工跑一次。

用法（仓库根或 backend 目录都可以）：
    cd backend
    uv run --frozen python ../scripts/verify_attachment_boundaries.py
退出码：0 = 全部符合预期；1 = 有不符合的项。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QIO_DEV_INSECURE", "1")
os.environ.setdefault("QIO_DISABLE_DB_CHECK", "1")
os.environ.pop("QIO_DATA_DIR", None)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

from agent.services.attachments import COPY_MAX_BYTES, AttachmentService  # noqa: E402
from agent.storage.db import connect  # noqa: E402
from agent.storage.migrate import apply_migrations  # noqa: E402
from agent.tools.attachment_tools import ReadAttachmentTool  # noqa: E402

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f" —— {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)


def main() -> int:
    import asyncio

    work = Path(tempfile.mkdtemp(prefix="qio-attach-verify-"))
    try:
        conn = connect(work / "app.db")
        apply_migrations(conn)
        svc = AttachmentService(conn, work / "data")

        check("阈值是十进制 100,000,000 字节", COPY_MAX_BYTES == 100_000_000, str(COPY_MAX_BYTES))

        # 1) 小文件：真实副本，内容一致，原文件不动
        small = work / "笔记.txt"
        body = "真实读入的内容：附件验收。\n第二行。\n"
        small.write_text(body, encoding="utf-8")
        mtime_before = small.stat().st_mtime
        att = svc.prepare(str(small))
        svc.run_prepare(att.id)
        done = svc.get(att.id)
        stored = Path(done.stored_path or "")
        check("小文件存独立副本并标注「已保存副本」",
              done.kind == "copy" and done.state == "ready" and "副本" in svc.payload(done, check=False)["display"],
              f"kind={done.kind} state={done.state}")
        check("副本内容与原文一致",
              stored.exists() and stored.read_text(encoding="utf-8") == body)
        check("保存副本不改动用户原文件",
              small.exists() and small.stat().st_mtime == mtime_before)

        # 2) 精确等于阈值：真实复制 100MB
        exact = work / "exact.bin"
        with open(exact, "wb") as handle:
            handle.truncate(COPY_MAX_BYTES)
        att_exact = svc.prepare(str(exact))
        check("取等号仍判为副本", att_exact.kind == "copy", att_exact.kind)
        started = time.perf_counter()
        svc.run_prepare(att_exact.id)
        elapsed = time.perf_counter() - started
        done_exact = svc.get(att_exact.id)
        size = Path(done_exact.stored_path).stat().st_size if done_exact.stored_path else -1
        check("真实复制 100MB 成功且大小精确",
              done_exact.state == "ready" and size == COPY_MAX_BYTES,
              f"state={done_exact.state} size={size} 用时={elapsed:.2f}s")

        # 3) 超过阈值：只记引用，不复制
        big = work / "big.bin"
        with open(big, "wb") as handle:
            handle.truncate(COPY_MAX_BYTES + 1)
        att_big = svc.prepare(str(big))
        svc.run_prepare(att_big.id)
        done_big = svc.get(att_big.id)
        copied = [p for p in svc.root.rglob("*") if p.is_file()] if svc.root.is_dir() else []
        check("超过阈值判为引用且标注「引用本地文件」",
              att_big.kind == "reference" and "引用" in svc.payload(done_big, check=False)["display"],
              att_big.kind)
        check("超过阈值不产生任何副本", len(copied) == 2, f"副本文件数={len(copied)}")

        # 4) 工具真实读回内容 + 分页
        lines = work / "多行.txt"
        lines.write_text("\n".join(f"第{i:03d}行内容" for i in range(1, 121)), encoding="utf-8")
        att_lines = svc.prepare(str(lines), turn_id="turn_verify")
        svc.run_prepare(att_lines.id)
        tool = ReadAttachmentTool(svc, active_turn_id=lambda: "turn_verify")

        async def read(**kwargs):
            return await tool.run(**kwargs)

        first = asyncio.run(read(attachment_id=att_lines.id, offset=0, limit=10))
        second = asyncio.run(read(attachment_id=att_lines.id, offset=10, limit=5))
        check("工具读回真实内容且分页只给这一段",
              first.ok and "第001行内容" in first.content and "第011行内容" not in first.content,
              (first.error or "")[:60])
        check("第二页起点正确",
              second.ok and "第011行内容" in second.content and "第016行内容" not in second.content)

        # 5) 引用文件消失：状态变 missing，工具如实报错
        ref = work / "引用大文件.bin"
        with open(ref, "wb") as handle:
            handle.write(b"REFERENCE-HEAD")
            handle.truncate(COPY_MAX_BYTES + 10)
        att_ref = svc.prepare(str(ref))
        svc.run_prepare(att_ref.id)
        head = asyncio.run(read(attachment_id=att_ref.id, offset=0, limit=1))
        check("引用型文件可读", head.ok, (head.error or "")[:60])
        ref.unlink()
        gone = svc.get(att_ref.id)
        after = asyncio.run(read(attachment_id=att_ref.id, offset=0, limit=1))
        check("引用文件消失 → state=missing", gone.state == "missing", gone.state)
        check("引用文件消失 → 工具如实报错、不编造内容",
              (not after.ok) and bool(after.error))
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} 项不符合 —— " + "、".join(FAILURES))
        return 1
    print("全部符合预期（含真实 100MB 复制）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
