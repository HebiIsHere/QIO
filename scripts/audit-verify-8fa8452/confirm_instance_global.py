r"""E 路独立核验 · 派生任务恢复的模块级全局顺序依赖（Lead 提出的基线既有缺陷）的独立确认。

只做事实实验，不改产品代码。运行：
  cd <wt>\backend
  $env:PYTHONPATH='<wt>\backend\src'
  & 'D:\qio-dev\qa-lead\backend\.venv\Scripts\python.exe' <本文件>
"""

from __future__ import annotations

import pathlib
import tempfile

from agent.services import derived_tasks as dt
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations


def _fresh():
    d = pathlib.Path(tempfile.mkdtemp())
    c = connect(d / "x.db")
    apply_migrations(c)
    return c


def main() -> None:
    c = _fresh()
    tid, _ = dt.enqueue(c, dt.KIND_SUMMARY, "frag_1", 1)
    print("bound_instance_id（未绑定）= ", dt.bound_instance_id())
    dt.claim_due(c)
    row = c.execute("SELECT owner_instance_id, state FROM derived_tasks WHERE id = ?", (tid,)).fetchone()
    print("认领后 owner_instance_id =", repr(row["owner_instance_id"]), " state =", row["state"])

    # 把 updated_at 推到很久以前（模拟「进程被杀」）
    c.execute("UPDATE derived_tasks SET updated_at = ? WHERE id = ?", ("2000-01-01T00:00:00+00:00", tid))
    recovered = dt.recover_stale(c)
    state_after = c.execute("SELECT state FROM derived_tasks WHERE id = ?", (tid,)).fetchone()["state"]
    print("未绑定实例时 recover_stale =", recovered, " state 保持 =", state_after)

    print("--- 绑定实例身份后重做同一实验 ---")
    c2 = _fresh()
    dt.bind_instance("inst_X")
    try:
        tid2, _ = dt.enqueue(c2, dt.KIND_SUMMARY, "frag_1", 1)
        dt.claim_due(c2)
        row2 = c2.execute(
            "SELECT owner_instance_id, state FROM derived_tasks WHERE id = ?", (tid2,)
        ).fetchone()
        print("认领后 owner_instance_id =", repr(row2["owner_instance_id"]), " state =", row2["state"])
        c2.execute(
            "UPDATE derived_tasks SET updated_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", tid2),
        )
        print("绑定实例时 recover_stale =", dt.recover_stale(c2))
    finally:
        dt.bind_instance(None, None)

    print("--- 无归属 + 归属者已死（registry.owner_alive=False）---")

    class _Dead:
        def owner_alive(self, instance_id):  # noqa: ANN001
            return False

    c3 = _fresh()
    dt.bind_instance("inst_Y", _Dead())
    try:
        tid3, _ = dt.enqueue(c3, dt.KIND_SUMMARY, "frag_1", 1)
        dt.claim_due(c3)
        c3.execute("UPDATE derived_tasks SET updated_at = ? WHERE id = ?", ("2000-01-01T00:00:00+00:00", tid3))
        print("归属已死时 recover_stale（且已超期）=", dt.recover_stale(c3))
    finally:
        dt.bind_instance(None, None)


if __name__ == "__main__":
    main()
