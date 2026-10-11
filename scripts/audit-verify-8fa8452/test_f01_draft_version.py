r"""E 路独立核验探针 · F01 草稿版本协议与逐键更新（基线 8fa845255c118a7104b1de8e37298ca745924482）。

作者：agent-e-verify。**只新增，不改产品代码、不改既有测试。**

规格来源：D:\qio-dev\_audit-findings-8fa8452.md 的 F01 段（任务书原文抄录）。
F01 要求（逐条对应下面的用例）：
  - 区分「逐键更新 / 明确删除 / 未修改」，携带并校验保存基线或逐键版本；
  - 旧快照不能隐式删除另一个页面的新草稿；
  - 同键相撞保留双方文字与明确冲突，不能按最后到达静默覆盖；
  - 不把「未传这个键」继续当作无条件删除；
  - 成功回执只清理已经确实上传并确认的本机副本；
  - 旧格式必须显式兼容（legacyReplace 标记），无法安全协调时保留输入并报告冲突。

探针设计原则：每个用例都是「能区分缺陷与修复」的尖锐反例 ——
它在基线（整集合替换语义）上必须**失败并给出可读的实际输出**，在修复后必须**通过**。
因此本文件是「基线证伪」证据，不是「修复后回归」证据；修复落地后重跑同一文件即回归验证。

运行（只跑本节点，禁止全量）：
  cd D:\qio-dev\qa-e\backend
  $env:PYTHONPATH = 'D:\qio-dev\qa-e\backend\src'
  & 'D:\qio-dev\qa-lead\backend\.venv\Scripts\python.exe' -m pytest ..\scripts\audit-verify-8fa8452\test_f01_draft_version.py -q -p no:cacheprovider

  注：探针在 backend/tests 之外，pytest 的 rootdir 变成探针所在目录，
  pyproject.toml 的 pythonpath=["src"] 不生效，所以必须显式设置 PYTHONPATH
  指向**本 worktree** 的 backend/src（保证测的是当前 worktree 的代码）。
"""

from __future__ import annotations

import inspect
import json
import os
import sqlite3
import tempfile
from pathlib import Path

# 不依赖 backend/tests/conftest.py（本探针在 tests/ 之外，pytest 不会加载它）。
# 显式摘掉 QIO_DATA_DIR，避免开发机落点污染；本探针自己用临时 db 文件。
os.environ.pop("QIO_DATA_DIR", None)
os.environ.setdefault("QIO_DEV_INSECURE", "1")
os.environ.setdefault("QIO_DISABLE_DB_CHECK", "1")

import pytest  # noqa: E402

from agent.interactive import board_store, models  # noqa: E402
from agent.storage.db import connect  # noqa: E402
from agent.storage.migrate import apply_migrations  # noqa: E402

BOARD = "board_e_probe"


def _fresh_conn() -> sqlite3.Connection:
    """迁移到最新 schema 的独立临时库（每个用例一个，互不污染）。"""
    tmpdir = tempfile.mkdtemp(prefix="e-verify-f01-")
    conn = connect(Path(tmpdir) / "qio.db")
    apply_migrations(conn)
    return conn


#: C1 协议字段的两种命名（修复版实现用 snake_case，契约 / HTTP 体用 camelCase）。
#: 探针必须两种都认，否则 baseRev 传不进去会让所有版本门用例变成「假失败」——
#: 这正是 E 核验自己踩过的坑（A 的 base_rev 一开始没被适配，报出 4 个假红）。
_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "baseRev": ("baseRev", "base_rev"),
    "keyVersions": ("keyVersions", "key_versions"),
}


def _save(conn: sqlite3.Connection, drafts: dict, **kwargs) -> dict:
    """调用 save_draft；修复版新增的关键字参数（baseRev/mode/removed/keyVersions）按需传入。

    同时接受 snake_case 与 camelCase 两种拼法，这样探针在基线与各修复版上都能**真实**驱动协议
    （而不是因为参数名没对上就退化成「版本门全拒」的假失败）。
    """
    params = inspect.signature(board_store.save_draft).parameters
    accepted: dict = {}
    for key, value in kwargs.items():
        names = _FIELD_ALIASES.get(key, (key,))
        for name in names:
            if name in params and name not in ("conn", "board_id", "drafts"):
                accepted[name] = value
                break
    return board_store.save_draft(conn, BOARD, drafts, **accepted)


def _supports_patch_protocol() -> bool:
    params = inspect.signature(board_store.save_draft).parameters
    return params.get("mode") is not None or params.get("baseRev") is not None


def _supports_removed() -> bool:
    params = inspect.signature(board_store.save_draft).parameters
    return params.get("removed") is not None or params.get("keyVersions") is not None


# ---------------------------------------------------------------------------
# F01-a 两页改不同卡片：B 页只上传 card:b，绝不能删掉 A 页已保存的 card:a
# ---------------------------------------------------------------------------
def test_f01_other_page_patch_must_not_delete_saved_draft() -> None:
    conn = _fresh_conn()
    first = _save(conn, {"card:a": "A 页写的文字"})
    assert first["drafts"] == {"card:a": "A 页写的文字"}

    # B 页只保存自己的 card:b（它没有 card:a 的任何信息）
    second = _save(conn, {"card:b": "B 页写的文字"})

    actual = board_store.get_draft(conn, BOARD)
    print("\n[F01-a] B 页 PUT 后服务端草稿实际内容 =", json.dumps(actual, ensure_ascii=False))
    print("[F01-a] 本次 PUT 回执 =", json.dumps(second, ensure_ascii=False))
    assert actual["drafts"] == {
        "card:a": "A 页写的文字",
        "card:b": "B 页写的文字",
    }, (
        "F01 缺陷复现：另一窗口只保存自己的键，却把对方已保存的草稿删掉了"
        "（整集合替换语义把『未传这个键』当成无条件删除）。actual="
        + json.dumps(actual["drafts"], ensure_ascii=False)
    )


# ---------------------------------------------------------------------------
# F01-b 未修改的键：patch 里没出现、也不在 removed 里 → 服务端绝不能删
# ---------------------------------------------------------------------------
def test_f01_untouched_keys_are_not_deleted_by_patch() -> None:
    conn = _fresh_conn()
    _save(conn, {"card:a": "A", "card:b": "B"})
    rev = board_store.get_draft(conn, BOARD)["rev"]

    _save(conn, {"card:a": "A2"}, mode="patch", baseRev=rev, removed=[])

    actual = board_store.get_draft(conn, BOARD)
    print("\n[F01-b] patch 后草稿 =", json.dumps(actual, ensure_ascii=False))
    assert actual["drafts"] == {"card:a": "A2", "card:b": "B"}, (
        "F01 缺陷复现：patch 里未出现的 card:b 被服务端删除。actual="
        + json.dumps(actual["drafts"], ensure_ascii=False)
    )


# ---------------------------------------------------------------------------
# F01-c 明确删除：removed 里的键必须真的删掉，并且回执如实报告 cleared
# ---------------------------------------------------------------------------
def test_f01_explicit_removed_is_honoured_and_reported() -> None:
    conn = _fresh_conn()
    _save(conn, {"card:a": "A", "card:b": "B"})
    rev = board_store.get_draft(conn, BOARD)["rev"]

    resp = _save(conn, {}, mode="patch", baseRev=rev, removed=["card:b"])

    actual = board_store.get_draft(conn, BOARD)
    print("\n[F01-c] 删除回执 =", json.dumps(resp, ensure_ascii=False))
    print("[F01-c] 删除后草稿 =", json.dumps(actual, ensure_ascii=False))
    assert actual["drafts"] == {"card:a": "A"}, (
        "F01 缺陷复现：明确删除的 card:b 没被删掉。actual="
        + json.dumps(actual["drafts"], ensure_ascii=False)
    )
    assert "card:b" in (resp.get("cleared") or []), (
        "F01：删除回执必须如实列出真实清掉的键（不能只靠请求发出去了推断）。resp="
        + json.dumps(resp, ensure_ascii=False)
    )


# ---------------------------------------------------------------------------
# F01-d 同键相撞：服务端已被「别人」改成不同文本 → 不静默覆盖，返回 conflict 且保留双方文字
# ---------------------------------------------------------------------------
def test_f01_same_key_collision_reports_conflict_and_keeps_both() -> None:
    conn = _fresh_conn()
    base = _save(conn, {"card:a": "共同基线"})
    base_rev = base["rev"]
    # 别人（另一页）先把同一个键改成别的文字
    _save(conn, {"card:a": "别人改的文字"}, baseRev=base_rev, mode="patch")

    # 我基于旧 baseRev 提交我的版本
    resp = _save(
        conn,
        {"card:a": "我改的文字"},
        mode="patch",
        baseRev=base_rev,
    )

    print("\n[F01-d] 冲突回执 =", json.dumps(resp, ensure_ascii=False))
    print("[F01-d] 服务端草稿 =", json.dumps(board_store.get_draft(conn, BOARD), ensure_ascii=False))
    conflicts = resp.get("conflicts") or []
    assert conflicts, (
        "F01 缺陷复现：同键被并发改成不同文本，服务端静默按最后到达覆盖，没有返回 conflict。resp="
        + json.dumps(resp, ensure_ascii=False)
    )
    texts = {c.get("local") for c in conflicts} | {c.get("server") for c in conflicts}
    assert {"我改的文字", "别人改的文字"} <= texts, (
        "F01：冲突必须保留双方文字（local 与 server 都要在冲突里可见）。resp="
        + json.dumps(resp, ensure_ascii=False)
    )
    # 可判定的最小要求：**不得静默覆盖** —— 服务端文本不能被我的旧写入单方改写掉。
    # （「本地文字是否也必须持久化在服务端」C1 正文没有明说；本探针不作超出契约的强断言，
    #  只要求它出现在冲突回执里、可由调用方交给用户决定。此点作为风险记入交付报告。）
    surviving = board_store.get_draft(conn, BOARD)["drafts"]
    assert surviving.get("card:a") == "别人改的文字", (
        "F01：冲突未解决前不得用旧写入静默覆盖服务端正文。actual="
        + json.dumps(surviving, ensure_ascii=False)
    )


# ---------------------------------------------------------------------------
# F01-e 迟到 baseRev：基于旧版本、但服务端已前进且内容不同 → 必须拒绝/报冲突（静默覆盖的可观测后果）
# ---------------------------------------------------------------------------
def test_f01_stale_baserev_is_not_silently_applied() -> None:
    conn = _fresh_conn()
    r1 = _save(conn, {"card:a": "v1"})
    _save(conn, {"card:a": "v2"}, mode="patch", baseRev=r1["rev"])

    # 一个还拿着 v1 版本号的旧请求姗姗来迟
    resp = _save(conn, {"card:a": "来自旧版本的写入"}, mode="patch", baseRev=r1["rev"])

    actual = board_store.get_draft(conn, BOARD)
    print("\n[F01-e] 旧 baseRev PUT 回执 =", json.dumps(resp, ensure_ascii=False))
    print("[F01-e] 服务端草稿 =", json.dumps(actual, ensure_ascii=False))
    assert actual["drafts"].get("card:a") == "v2", (
        "F01 缺陷复现：基于旧版本（baseRev 落后）的写入被静默接受并覆盖了更新的文字。actual="
        + json.dumps(actual["drafts"], ensure_ascii=False)
    )


# ---------------------------------------------------------------------------
# F01-f 协议缺失时的降级：旧请求体（无 mode/baseRev/removed）不得静默删除未传键
# ---------------------------------------------------------------------------
def test_f01_legacy_body_never_silently_deletes() -> None:
    conn = _fresh_conn()
    _save(conn, {"card:a": "A 页", "card:b": "B 页"})
    resp = _save(conn, {"card:a": "A 页 v2"})  # 旧客户端整集合请求体

    actual = board_store.get_draft(conn, BOARD)
    print("\n[F01-f] 旧体 PUT 回执 =", json.dumps(resp, ensure_ascii=False))
    print("[F01-f] 服务端草稿 =", json.dumps(actual, ensure_ascii=False))
    assert actual["drafts"].get("card:b") == "B 页", (
        "F01 缺陷复现：旧请求体（无 mode/baseRev）触发了无条件整集合替换，删掉了未传的 card:b。"
        "诚实的兼容行为应当保留 card:b 并在回执里说明 legacyReplace/冲突。actual="
        + json.dumps(actual["drafts"], ensure_ascii=False)
    )


def test_f01_probe_declares_protocol_support() -> None:
    """把基线协议能力打印出来，供交付报告引用（不是断言，只是事实）。"""
    print("\n[F01-meta] save_draft 参数 =", list(inspect.signature(board_store.save_draft).parameters))
    print("[F01-meta] patch 协议可用 =", _supports_patch_protocol())
    print("[F01-meta] removed/keyVersions 可用 =", _supports_removed())
    print("[F01-meta] 存储格式 = 见 get_draft 返回：", json.dumps(board_store.get_draft(_fresh_conn(), BOARD), ensure_ascii=False))
