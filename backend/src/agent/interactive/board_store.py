"""板面与草稿的持久化（子智能体 B 负责实现）。

契约：docs/interactive-mode-contract.md §1.5 / §3。

职责边界（三条不许绕过的规则）：

1. 每完成一次操作保存一次：最新状态写 board_states，**同时追加一条快照**到
   board_state_snapshots（撤销 / 重做与「撤回任务改动」的依据）。
2. 保存路径**绝不调用 QIO**：本模块（以及它调用的 submission.refresh_pending）
   只做本地计算与落库，没有任何模型 / 网络调用，也不允许后来加。
3. 草稿（board_drafts）是**未提交的文字输入**，与提交内容分开：草稿不进提交载荷，
   也不代表用户已经确认了这段文字（契约 §6.4：意外退出只保证已保存内容）。

save_board 返回 {"seq", "savedAt", "state", "pending"}：
- seq 是本次保存序号（每次保存 +1，快照与之对应）；
- pending 是「相对上次成功提交基准、但尚未提交」的有效改动，供界面显示；它**不进 QIO**。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from agent.interactive import board, models

#: 单块板面保留的快照条数上限（防止长期使用后表无限增长；历史接口最多取 200 条）
SNAPSHOT_KEEP = 200
#: 草稿限制：键数量与单个值长度（草稿只是未提交的文字输入，不该被当成存储用）
DRAFT_MAX_KEYS = 200
DRAFT_MAX_CHARS = 20000


class DraftTooLong(ValueError):
    """草稿正文超过上限：必须明确拒绝，**不允许截短后返回成功**（契约 M6）。

    attributes：keys（超限的键）、limit（上限字符数）。
    """

    def __init__(self, keys: list[str], limit: int) -> None:
        self.keys = keys
        self.limit = limit
        shown = "、".join(keys[:3]) + ("等" if len(keys) > 3 else "")
        super().__init__(f"草稿过长（最多 {limit} 字）：{shown}。已拒绝保存，未截短、未写入。")


def _now() -> str:
    return models.now_iso()


def _clip(value: Any, *, limit: int = 200) -> str:
    return str(value)[:limit]


def ensure_board(
    conn: sqlite3.Connection, *, board_id: str | None = None, title: str | None = None
) -> dict:
    """确保板面与其最新状态行存在；已存在时**不改动**已有内容（含用户改过的标题）。"""
    resolved = _clip(board_id or models.DEFAULT_BOARD_ID).strip() or models.DEFAULT_BOARD_ID
    stamp = _now()
    conn.execute(
        "INSERT OR IGNORE INTO boards (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (
            resolved,
            _clip(title or models.DEFAULT_BOARD_TITLE, limit=120) or models.DEFAULT_BOARD_TITLE,
            stamp,
            stamp,
        ),
    )
    conn.execute(
        "INSERT OR IGNORE INTO board_states (board_id, seq, state, reason, updated_at)"
        " VALUES (?, 0, ?, 'created', ?)",
        (resolved, models.dumps(models.empty_state(resolved)), stamp),
    )
    row = conn.execute(
        "SELECT id, title, created_at, updated_at FROM boards WHERE id = ?", (resolved,)
    ).fetchone()
    return {
        "id": row["id"],
        "title": row["title"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def load_board(conn: sqlite3.Connection, board_id: str) -> dict:
    """读取最新板面状态。未知 id 会建一块空板面（保证界面不白屏；新建请用 POST /boards）。"""
    info = ensure_board(conn, board_id=board_id)
    row = conn.execute(
        "SELECT seq, state, reason, updated_at FROM board_states WHERE board_id = ?",
        (info["id"],),
    ).fetchone()
    loaded = models.loads(row["state"] if row else None, None)
    state = loaded if isinstance(loaded, dict) else models.empty_state(info["id"])
    state["boardId"] = info["id"]
    for key in ("cards", "groups", "links", "selection"):
        if not isinstance(state.get(key), list):
            state[key] = []
    if not isinstance(state.get("seq"), int):
        state["seq"] = int(row["seq"]) if row else 0
    return {
        "board": info,
        "state": state,
        "seq": int(row["seq"]) if row else 0,
        "savedAt": row["updated_at"] if row else None,
    }


def saved_seq(conn: sqlite3.Connection, board_id: str) -> int | None:
    """当前**已保存**的板面版本（契约 §1.4 的唯一版本事实）。

    板面还不存在时返回 None：读版本**不建行、不改任何内容**——被拒绝的写入
    不应该因为一次版本检查就在数据库里留下痕迹。
    """
    row = conn.execute("SELECT seq FROM board_states WHERE board_id = ?", (board_id,)).fetchone()
    if row is None:
        return None
    try:
        return int(row["seq"])
    except (TypeError, ValueError):
        return None


def parse_state_seq(state: Any) -> int | None:
    """从候选板面里取出「这次写入所依据的已保存版本」。

    能证明是版本的只有整数（含整数值的浮点与数字字符串）；缺失 / 非数字 / 布尔
    一律按**未知版本**返回 None，不猜成某个已保存版本。
    """
    data = state if isinstance(state, dict) else {}
    raw = data.get("seq")
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return int(raw) if raw.is_integer() else None
    if isinstance(raw, str):
        try:
            return int(raw.strip())
        except ValueError:
            return None
    return None


def state_version_error(*, claimed: int | None, current: int) -> str | None:
    """普通整板写入的版本门（契约 §1.4）：通过返回 None，否则返回可读中文原因。

    - 声明版本与当前已保存版本一致 → 放行；
    - 新板（还没有任何已保存版本，current == 0）：缺失版本也放行——没有旧版本可被覆盖，
      这是「新板第一次保存必须允许」的兼容口径；
    - 一旦有已保存版本：旧版 / 未知版本（缺失、非数字、比服务端更新）都必须被拒绝——
      错误前端或跨页面旧请求不得静默回退新版。
    """
    if claimed == current:
        return None
    if claimed is None and current <= 0:
        return None
    if claimed is None:
        return (
            f"这次保存没有带上它所依据的板面版本（seq）：服务器已有已保存版本 {current}，"
            "无法证明这次写入基于它，已拒绝（没有写入任何内容）。"
            "请先读取最新板面，再用最新版本重新保存。"
        )
    if claimed > current:
        return (
            f"这次保存声明的板面版本 {claimed} 比服务器已保存版本 {current} 更新（未知版本）："
            "已拒绝（没有写入任何内容）。请先读取最新板面，再用最新版本重新保存。"
        )
    return (
        f"这次保存基于旧版本 {claimed}，服务器已经保存到版本 {current}："
        "为避免这次写入覆盖更新的内容，已拒绝（没有写入任何内容）。"
        "请先读取最新板面，再用最新版本重新保存。"
    )


def _refresh_pending(conn: sqlite3.Connection, board_id: str) -> dict:
    """重算「未提交的有效改动」（纯本地求差，不调用 QIO）。

    保存不允许因为这个重算失败而失败：出错时退化成空列表 + error 说明，
    界面显示文字而不是白屏。
    """
    try:
        from agent.interactive import submission  # 延迟 import：避免模块级循环依赖

        return submission.refresh_pending(conn, board_id)
    except Exception as exc:  # noqa: BLE001 - 保存必须成功，重算失败只降级
        return {
            "baselineSeq": None,
            "expressions": [],
            "updatedAt": _now(),
            "error": f"未提交改动重算失败：{exc}",
        }


def save_board(
    conn: sqlite3.Connection, board_id: str, state: dict, *, reason: str = "op"
) -> dict:
    """保存最新状态并追加快照。**这里不调用 QIO，也不做任何模型 / 网络调用。**"""
    if not isinstance(state, dict):
        raise ValueError("state 必须是 JSON 对象")
    info = ensure_board(conn, board_id=board_id)
    payload = board.normalize_state({**state, "boardId": info["id"]})
    row = conn.execute("SELECT seq FROM board_states WHERE board_id = ?", (info["id"],)).fetchone()
    seq = (int(row["seq"]) if row else 0) + 1
    stamp = _now()
    payload["seq"] = seq
    payload["updatedAt"] = stamp
    dumped = models.dumps(payload)
    label = _clip(reason or "op", limit=64)
    from agent.storage.db import transaction

    with transaction(conn):
        conn.execute(
            "UPDATE board_states SET seq = ?, state = ?, reason = ?, updated_at = ?"
            " WHERE board_id = ?",
            (seq, dumped, label, stamp, info["id"]),
        )
        conn.execute(
            "INSERT INTO board_state_snapshots (id, board_id, seq, state, reason, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (models.new_id("snap"), info["id"], seq, dumped, label, stamp),
        )
        conn.execute(
            "DELETE FROM board_state_snapshots WHERE board_id = ? AND seq <= ?",
            (info["id"], seq - SNAPSHOT_KEEP),
        )
    pending = _refresh_pending(conn, info["id"])
    return {"seq": seq, "savedAt": stamp, "state": payload, "pending": pending}


def list_board_states(
    conn: sqlite3.Connection, board_id: str, *, limit: int = 50
) -> list[dict]:
    """最近的状态快照（新的在前），撤销 / 重做与撤回保护的依据。"""
    info = ensure_board(conn, board_id=board_id)
    count = max(1, min(int(limit or 50), 200))
    rows = conn.execute(
        "SELECT seq, state, reason, created_at FROM board_state_snapshots"
        " WHERE board_id = ? ORDER BY seq DESC LIMIT ?",
        (info["id"], count),
    ).fetchall()
    return [
        {
            "seq": int(row["seq"]),
            "state": models.loads(row["state"], {}),
            "reason": row["reason"],
            "createdAt": row["created_at"],
        }
        for row in rows
    ]


def load_pending(conn: sqlite3.Connection, board_id: str) -> dict:
    """读取「未提交的有效改动」（界面显示用；没有记录时返回空列表）。"""
    info = ensure_board(conn, board_id=board_id)
    row = conn.execute(
        "SELECT baseline_seq, expressions, updated_at FROM board_pending WHERE board_id = ?",
        (info["id"],),
    ).fetchone()
    if row is None:
        return {"baselineSeq": None, "expressions": [], "updatedAt": None}
    return {
        "baselineSeq": int(row["baseline_seq"]) if row["baseline_seq"] is not None else None,
        "expressions": models.loads(row["expressions"], []),
        "updatedAt": row["updated_at"],
    }


def _read_stored_drafts(conn: sqlite3.Connection, board_id: str) -> tuple[dict[str, str], int]:
    """读取真实存储的草稿与其版本号。

    新格式是 {"rev": int, "drafts": {key: text}}；旧格式是直接的 {key: text}。
    旧格式没有版本信息：rev 如实返回 0，不编造「已保存过一次」的版本。
    """
    row = conn.execute(
        "SELECT drafts FROM board_drafts WHERE board_id = ?", (board_id,)
    ).fetchone()
    stored = models.loads(row["drafts"] if row else None, None)
    if not isinstance(stored, dict):
        return {}, 0
    inner = stored.get("drafts")
    if isinstance(inner, dict) and "rev" in stored:
        return {str(k): str(v) for k, v in inner.items()}, max(0, int(stored.get("rev") or 0))
    # 旧格式（plain dict）：按原样兼容，绝不自动覆盖或删除
    return {str(k): str(v) for k, v in stored.items()}, 0


def get_draft(conn: sqlite3.Connection, board_id: str) -> dict:
    """读取草稿。草稿不是提交内容，也不会交给 QIO。

    除草稿本体与更新时间外，返回单调存簿版本号 rev：区分「客户端计划清除的版本」
    与「服务端里真正存下的版本」就靠它（契约 M1 草稿 draftRev 的服务端事实源）。
    """
    info = ensure_board(conn, board_id=board_id)
    row = conn.execute(
        "SELECT updated_at FROM board_drafts WHERE board_id = ?", (info["id"],)
    ).fetchone()
    drafts, rev = _read_stored_drafts(conn, info["id"])
    return {"drafts": drafts, "updatedAt": row["updated_at"] if row else None, "rev": rev}


def save_draft(conn: sqlite3.Connection, board_id: str, drafts: dict) -> dict:
    """保存草稿（未提交的文字输入）。**不调用 QIO**，也不写入板面状态。

    - 超过 DRAFT_MAX_CHARS 的正文：整体拒绝（DraftTooLong，附带键名与上限），
      不截短、不部分写入；上限内的正文**完整**落库。
    - 响应 cleared 列出这次保存**真实清掉**的键，rev 单调递增：清除结果的
      真实性由调用方逐一核对，而不是靠「请求发出去了」推断。
    """
    if drafts is None:
        drafts = {}
    if not isinstance(drafts, dict):
        raise ValueError("drafts 必须是 JSON 对象")
    if len(drafts) > DRAFT_MAX_KEYS:
        raise ValueError(f"草稿条目过多（最多 {DRAFT_MAX_KEYS} 条）")
    clean: dict[str, str] = {}
    for key, value in drafts.items():
        label = str(key)
        if len(label) > 120:
            # 键名静默截短会让两个话题共用一个键：宁可明确拒绝
            raise ValueError(f"草稿键名过长（最多 120 字符）：{label[:24]}…")
        text = "" if value is None else str(value)
        if len(text) > DRAFT_MAX_CHARS:
            raise DraftTooLong([label], DRAFT_MAX_CHARS)
        clean[label] = text
    info = ensure_board(conn, board_id=board_id)
    prev_drafts, prev_rev = _read_stored_drafts(conn, info["id"])
    cleared = sorted(set(prev_drafts) - set(clean))
    new_rev = prev_rev + 1
    stamp = _now()
    conn.execute(
        "INSERT INTO board_drafts (board_id, drafts, updated_at) VALUES (?, ?, ?)"
        " ON CONFLICT(board_id) DO UPDATE SET drafts = excluded.drafts,"
        " updated_at = excluded.updated_at",
        (info["id"], models.dumps({"rev": new_rev, "drafts": clean}), stamp),
    )
    return {"drafts": clean, "updatedAt": stamp, "rev": new_rev, "cleared": cleared}
