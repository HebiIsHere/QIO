"""独立迁移验证（整合轮 E 的跨模块验收脚本）。

不依赖任何别人的自述，也不依赖仓库里的既有测试：自己建库、自己升级、自己制造缺对象、
自己复核「历史迁移没有被改写」的可静态证明部分。

四个场景（对应 task-5 第 2 条）：
  A. 空库：从零跑完整迁移序列 → 版本到顶、必需对象齐全、真实模型对象存在；
  B. 真实 main 库升级：复制一份真实应用库（默认 %APPDATA%\qio\app.db，可用 --real-db 指定），
     升级前记录各表行数，升级后要求版本到顶、对象齐全、**既有行数不变**；
  C. 缺对象补偿：在已升级的库上删掉必需的表 / 索引 / 列，再走一次启动（apply_migrations），
     要求补偿迁移把它们补回来、复核为空；
  D. 重复启动：连续多次启动同一库，版本稳定、版本行不重复、无异常。

外加静态不变式（不需要数据库）：
  I1. SCHEMA_VERSION == MIGRATIONS 最大编号；
  I2. COMPENSATION_VERSION 在 MIGRATIONS 里；
  I3. 补偿迁移 == 上一条完整迁移 + 本轮新增迁移（MIGRATIONS[c] == MIGRATIONS[c-2] + MIGRATIONS[c-1]）；
  I4. 真实模型对象（board_understanding / board_executions）首次出现于迁移 37（37/38 是已交付历史，禁止改写）；
  I5. REQUIRED_OBJECTS 的每一项都能在补偿迁移里找到对应的建对象语句。

用法（在 worktree 根目录）：
  backend/.venv/Scripts/python.exe scripts/irm-int-verify/verify_migrations.py \
      [--real-db <path>] [--json out.json] [--keep-tmp]
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKEND_SRC = ROOT / "backend" / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

from agent.storage import migrate as M  # noqa: E402
from agent.storage import schema as S  # noqa: E402
from agent.storage.db import connect, transaction  # noqa: E402

RESULTS: list[dict] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append({"check": name, "ok": bool(ok), "detail": detail})
    print(("  [PASS] " if ok else "  [FAIL] ") + name + ((" :: " + detail) if detail else ""))
    return bool(ok)


def _git(*args: str) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(ROOT), *args], capture_output=True, text=True, timeout=60
        )
        return out.stdout.strip() if out.returncode == 0 else f"<git failed: {out.stderr.strip()}>"
    except Exception as exc:  # pragma: no cover - 诊断用
        return f"<git error: {exc}>"


def _table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    names = [
        str(r[0])
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    counts: dict[str, int] = {}
    for name in names:
        try:
            counts[name] = int(conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0])
        except sqlite3.Error as exc:
            counts[name] = -1
            print(f"    (计数失败 {name}: {exc})")
    return counts


_CREATE_TABLE_RE = re.compile(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][A-Za-z0-9_]*)")


def _created_tables(stmt: str) -> set[str]:
    return {m.group(1) for m in _CREATE_TABLE_RE.finditer(str(stmt))}


def _version_rows(conn: sqlite3.Connection) -> list[int]:
    return [int(r[0]) for r in conn.execute("SELECT version FROM schema_version ORDER BY version")]


# ---------------------------------------------------------------- 场景 A：空库
def scenario_empty(tmp: Path) -> None:
    print("\n[A] 空库：从零跑完整迁移序列")
    db = tmp / "empty" / "app.db"
    conn = connect(db)
    try:
        version = M.apply_migrations(conn)
        check("A1 版本到顶", version == S.SCHEMA_VERSION, f"version={version} SCHEMA_VERSION={S.SCHEMA_VERSION}")
        missing = M.missing_objects(conn)
        check("A2 必需对象齐全", not any(missing.values()), M._describe(missing))
        report = M.schema_objects_report(conn)
        check("A3 schema_objects_report.complete", report["complete"] is True)
        for obj, kind in (("board_understanding", "table"), ("board_executions", "table"),
                          ("idx_board_understanding_request", "index"),
                          ("idx_board_executions_board", "index")):
            if kind == "table":
                ok = M._table_exists(conn, obj)
            else:
                ok = M._index_exists(conn, obj)
            check(f"A4 真实模型对象存在（{obj}）", ok, obj)
        for col in ("goal", "expected", "operations", "basis", "intent_key", "understanding_id"):
            check(f"A5 board_intents.{col} 存在", M._column_exists(conn, "board_intents", col))
        rows = _version_rows(conn)
        check("A6 版本行数 == 迁移条数且无重复",
              len(rows) == len(S.MIGRATIONS) and len(set(rows)) == len(rows),
              f"rows={len(rows)} migrations={len(S.MIGRATIONS)}")
    finally:
        conn.close()


# ------------------------------------------------------- 场景 B：真实 main 库升级
def scenario_real(tmp: Path, real_db: Path) -> None:
    print(f"\n[B] 真实库升级：{real_db}")
    if not real_db.exists():
        check("B0 找到真实库", False, f"{real_db} 不存在")
        return
    work = tmp / "real"
    work.mkdir(parents=True, exist_ok=True)
    target = work / "app.db"
    shutil.copy2(real_db, target)
    for suffix in ("-wal", "-shm"):
        src = Path(str(real_db) + suffix)
        if src.exists():
            shutil.copy2(src, Path(str(target) + suffix))

    conn = connect(target)
    try:
        before_version = M.current_version(conn)
        before_counts = _table_counts(conn)
        before_tables = set(before_counts)
        print(f"    升级前 version={before_version} tables={len(before_tables)}")
        version = M.apply_migrations(conn)
        check("B1 升级到最新版本", version == S.SCHEMA_VERSION,
              f"{before_version} -> {version} (SCHEMA_VERSION={S.SCHEMA_VERSION})")
        missing = M.missing_objects(conn)
        check("B2 升级后对象齐全", not any(missing.values()), M._describe(missing))
        after_counts = _table_counts(conn)
        changed = {
            name: (before_counts[name], after_counts.get(name))
            for name in before_tables
            if name != "schema_version" and after_counts.get(name) != before_counts[name]
        }
        check("B3 既有表行数不变（升级不改用户数据）", not changed, json.dumps(changed, ensure_ascii=False))
        created_tables = {
            t
            for target, stmts in S.MIGRATIONS
            if target > before_version
            for s in stmts
            for t in _created_tables(s)
        }
        new_tables = sorted(set(after_counts) - before_tables)
        check("B4 新增表都来自未应用的迁移语句",
              set(new_tables) <= created_tables,
              f"new={new_tables} 不在迁移里的={sorted(set(new_tables) - created_tables)}")
        check("B5 升级后真实模型两张表存在",
              {"board_understanding", "board_executions"} <= set(after_counts))
        print(f"    升级前 {before_version} 版、{len(before_tables)} 张表；"
              f"升级后 {version} 版、{len(after_counts)} 张表；既有数据行数合计 "
              f"{sum(v for k, v in before_counts.items() if k != 'schema_version' and v > 0)}")
    finally:
        conn.close()


# --------------------------------------------------------- 场景 C：缺对象补偿
def scenario_compensation(tmp: Path) -> None:
    print("\n[C] 缺对象补偿：删掉必需对象后再启动")
    db = tmp / "comp" / "app.db"
    conn = connect(db)
    try:
        M.apply_migrations(conn)
        for stmt in ("DROP TABLE board_understanding",
                     "DROP INDEX idx_board_executions_board",
                     "ALTER TABLE board_intents DROP COLUMN goal"):
            try:
                conn.execute(stmt)
            except sqlite3.Error as exc:
                check(f"C0 制造缺口（{stmt}）", False, str(exc))
        missing = M.missing_objects(conn)
        check("C1 缺口被按对象检出",
              "board_understanding" in missing["tables"]
              and "idx_board_executions_board" in missing["indexes"]
              and "board_intents.goal" in missing["columns"],
              M._describe(missing))
        version = M.apply_migrations(conn)  # 版本已到顶 → 必须走补偿复核
        check("C2 版本不变（补偿不是新迁移）", version == S.SCHEMA_VERSION, f"version={version}")
        still = M.missing_objects(conn)
        check("C3 补偿后对象齐全", not any(still.values()), M._describe(still))
        check("C4 被删的表已补回", M._table_exists(conn, "board_understanding"))
        check("C5 被删的索引已补回", M._index_exists(conn, "idx_board_executions_board"))
        check("C6 被删的列已补回", M._column_exists(conn, "board_intents", "goal"))
        row = conn.execute("SELECT COUNT(*) FROM board_understanding").fetchone()
        check("C7 补回的表可用", int(row[0]) == 0, f"rows={row[0] if row else None}")
        # 补偿是可重复的：再删一次、再启动一次
        conn.execute("DROP INDEX idx_board_understanding_request")
        M.apply_migrations(conn)
        check("C8 补偿可重复触发", M._index_exists(conn, "idx_board_understanding_request"))
    finally:
        conn.close()


# ------------------------------------------------------------- 场景 D：重复启动
def scenario_restart(tmp: Path) -> None:
    print("\n[D] 重复启动：同一库连续启动三次")
    db = tmp / "restart" / "app.db"
    conn = connect(db)
    try:
        versions = []
        for i in range(3):
            versions.append(M.apply_migrations(conn))
            rows = _version_rows(conn)
            if len(rows) != len(set(rows)):
                check(f"D2 第 {i + 1} 次启动版本行不重复", False, f"rows={rows}")
                return
        check("D1 三次启动版本稳定", versions == [S.SCHEMA_VERSION] * 3, f"{versions}")
        rows = _version_rows(conn)
        check("D2 版本行不重复、条数稳定",
              len(rows) == len(set(rows)) == len(S.MIGRATIONS), f"rows={len(rows)}")
        check("D3 重复启动无缺对象", not any(M.missing_objects(conn).values()))
    finally:
        conn.close()


# ------------------------------------------- 场景 E：合成存量库（带数据）升级
def scenario_stock_with_data(tmp: Path) -> None:
    """存量库（停在 36，已经有人用）升级到最新：既有数据一行不少、内容不变。"""
    print("\n[E] 合成存量库（36 版 + 真实数据）升级")
    db = tmp / "stock" / "app.db"
    conn = connect(db)
    try:
        M.current_version(conn)  # 建 schema_version 表（与真实启动一致）
        for target, stmts in S.MIGRATIONS:
            if target > 36:
                break
            with transaction(conn):
                M._apply_statements(conn, target, stmts)
                conn.execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                    (target, M._now()),
                )
        check("E1 存量库停在 36", M.current_version(conn) == 36, f"{M.current_version(conn)}")
        conn.execute(
            "INSERT INTO boards (id, title, created_at, updated_at, meta)"
            " VALUES ('b1', '旧板面', 't0', 't0', '{\"k\":1}')"
        )
        conn.execute(
            "INSERT INTO board_states (board_id, seq, state, reason, updated_at)"
            " VALUES ('b1', 7, '{\"cards\":[{\"id\":\"c1\"}]}', 'op', 't0')"
        )
        conn.execute(
            "INSERT INTO board_drafts (board_id, drafts, updated_at)"
            " VALUES ('b1', '{\"c1\":\"旧草稿\"}', 't0')"
        )
        conn.execute(
            "INSERT INTO board_submissions (id, board_id, seq, status, created_at)"
            " VALUES ('s1', 'b1', 7, 'succeeded', 't0')"
        )
        conn.execute(
            "INSERT INTO board_intents (id, board_id, title, status, created_at, updated_at)"
            " VALUES ('i1', 'b1', '旧任务', 'pending', 't0', 't0')"
        )
        version = M.apply_migrations(conn)
        check("E2 升级到最新版本", version == S.SCHEMA_VERSION, f"36 -> {version}")
        drafts = conn.execute("SELECT drafts FROM board_drafts WHERE board_id='b1'").fetchone()[0]
        check("E3 草稿正文不变", drafts == '{"c1":"旧草稿"}', drafts)
        state = conn.execute("SELECT seq, state FROM board_states WHERE board_id='b1'").fetchone()
        check("E4 板面版本与正文不变", state[0] == 7 and "c1" in state[1], f"seq={state[0]}")
        intent = conn.execute(
            "SELECT title, status, goal, expected, operations, basis, intent_key, understanding_id"
            " FROM board_intents WHERE id='i1'"
        ).fetchone()
        check("E5 旧意图行保留且新列为默认值",
              intent[0] == "旧任务" and intent[1] == "pending"
              and tuple(intent[2:]) == ("", "", "[]", "[]", "", ""),
              str(tuple(intent)))
        check("E6 提交记录保留",
              conn.execute("SELECT COUNT(*) FROM board_submissions WHERE id='s1'").fetchone()[0] == 1)
        check("E7 升级后对象齐全", not any(M.missing_objects(conn).values()))
    finally:
        conn.close()


# --------------------------------------------------------------- 静态不变式
def invariants() -> None:
    print("\n[I] 静态不变式（迁移编号与历史迁移）")
    targets = [t for t, _ in S.MIGRATIONS]
    check("I1 SCHEMA_VERSION == 最大迁移编号",
          S.SCHEMA_VERSION == max(targets), f"{S.SCHEMA_VERSION} vs {max(targets)}")
    check("I2 补偿编号在 MIGRATIONS 里", S.COMPENSATION_VERSION in targets,
          f"COMPENSATION_VERSION={S.COMPENSATION_VERSION}")
    by_target = {t: stmts for t, stmts in S.MIGRATIONS}
    c = S.COMPENSATION_VERSION
    check("I3 补偿 == c-2 全部语句 + c-1 新增语句",
          by_target.get(c) == list(by_target.get(c - 2, [])) + list(by_target.get(c - 1, [])),
          f"c={c}")
    first_real = min(
        (t for t, stmts in S.MIGRATIONS if any("board_understanding" in s for s in stmts)),
        default=None,
    )
    check("I4 真实模型对象首次出现在迁移 37（37/38 为已交付历史）",
          first_real == 37, f"first_real={first_real}")
    check("I5 历史迁移（<37）不含真实模型对象",
          all(
              "board_understanding" not in s and "board_executions" not in s
              for t, stmts in S.MIGRATIONS if t < 37 for s in stmts
          ))
    comp_text = "\n".join(by_target.get(c, []))
    uncovered: list[str] = []
    for table in M.REQUIRED_OBJECTS["tables"]:
        if f"CREATE TABLE IF NOT EXISTS {table}" not in comp_text and f"CREATE TABLE {table}" not in comp_text:
            uncovered.append(f"table:{table}")
    for spec in M.REQUIRED_OBJECTS["columns"]:
        _, _, column = str(spec).partition(".")
        if f"ADD COLUMN {column} " not in comp_text:
            uncovered.append(f"column:{spec}")
    for index in M.REQUIRED_OBJECTS["indexes"]:
        if f"INDEX IF NOT EXISTS {index}" not in comp_text and f"INDEX {index}" not in comp_text:
            uncovered.append(f"index:{index}")
    check("I6 REQUIRED_OBJECTS 每项都有补偿语句", not uncovered, ", ".join(uncovered))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--real-db", default=str(Path.home() / "AppData" / "Roaming" / "qio" / "app.db"))
    parser.add_argument("--json", default="")
    parser.add_argument("--keep-tmp", action="store_true")
    args = parser.parse_args()

    head = _git("rev-parse", "HEAD")
    branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    print(f"worktree={ROOT}\nbranch={branch}\nHEAD={head}")
    print(f"SCHEMA_VERSION={S.SCHEMA_VERSION} COMPENSATION_VERSION={S.COMPENSATION_VERSION} "
          f"MIGRATIONS={len(S.MIGRATIONS)}")

    tmp = Path(tempfile.mkdtemp(prefix="irm-int-e-mig-"))
    try:
        scenario_empty(tmp)
        scenario_real(tmp, Path(args.real_db))
        scenario_compensation(tmp)
        scenario_restart(tmp)
        scenario_stock_with_data(tmp)
        invariants()
    finally:
        if args.keep_tmp:
            print(f"\n临时目录保留：{tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)

    failed = [r for r in RESULTS if not r["ok"]]
    summary = {
        "script": "scripts/irm-int-verify/verify_migrations.py",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "worktree": str(ROOT),
        "branch": branch,
        "head": head,
        "schema_version": S.SCHEMA_VERSION,
        "compensation_version": S.COMPENSATION_VERSION,
        "migrations": len(S.MIGRATIONS),
        "real_db": args.real_db,
        "checks_total": len(RESULTS),
        "checks_failed": len(failed),
        "passed": not failed,
        "checks": RESULTS,
    }
    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n证据已写入 {out}")
    print(f"\n结果：{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过"
          + ("" if not failed else "；失败：" + ", ".join(r["check"] for r in failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
