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


def _read_stored_drafts(
    conn: sqlite3.Connection, board_id: str
) -> tuple[dict[str, str], int, dict[str, dict]]:
    """读取真实存储的草稿、存簿版本与逐键版本索引（契约 C1）。

    存储格式沿革（**只追加，不重写历史数据**）：

    - v2（本契约）：`{"rev": int, "drafts": {key: text}, "keys": {key: {...}}}`，
      其中 `keys[key] = {"version": int, "deleted": bool, "text": str}`；
      `deleted=True` 的条目是**删除墓碑**：它证明「这个键被显式删过一次」，
      于是不带身份（版本）的迟到更新不许把它复活（F01 的删除身份保护）。
    - v1：`{"rev": int, "drafts": {key: text}}`（缺 keys）：正文照读，逐键版本如实记 0。
    - v0：直接的 `{key: text}`：正文照读、rev 记 0，绝不自动覆盖或删除。

    返回 `(drafts, rev, index)`，index[key] = {"version": int, "deleted": bool, "text": str}。
    旧格式没有版本信息时**如实返回 0**，不编造「已保存过一次」的版本。
    """
    row = conn.execute(
        "SELECT drafts FROM board_drafts WHERE board_id = ?", (board_id,)
    ).fetchone()
    stored = models.loads(row["drafts"] if row else None, None)
    if not isinstance(stored, dict):
        return {}, 0, {}
    inner = stored.get("drafts")
    if isinstance(inner, dict) and "rev" in stored:
        texts = {str(k): str(v) for k, v in inner.items()}
        rev = max(0, int(stored.get("rev") or 0))
        raw_keys = stored.get("keys") if isinstance(stored.get("keys"), dict) else {}
        index: dict[str, dict] = {}
        for key, text in texts.items():
            meta = raw_keys.get(key)
            version = meta.get("version") if isinstance(meta, dict) else None
            index[key] = {
                "version": int(version) if isinstance(version, int) and not isinstance(version, bool) else 0,
                "deleted": False,
                "text": text,
            }
        # 墓碑（已删除、正文不在 drafts 里）也要读回来：删除身份保护的唯一依据
        for raw_key, meta in raw_keys.items():
            key = str(raw_key)
            if key in index or not isinstance(meta, dict) or not meta.get("deleted"):
                continue
            version = meta.get("version")
            index[key] = {
                "version": int(version) if isinstance(version, int) and not isinstance(version, bool) else 0,
                "deleted": True,
                "text": str(meta.get("text") or ""),
            }
        return texts, rev, index
    # 旧格式（v1 缺 keys / v0 plain dict）：按原样兼容，绝不自动覆盖或删除
    return {str(k): str(v) for k, v in stored.items()}, 0, {
        str(k): {"version": 0, "deleted": False, "text": str(v)} for k, v in stored.items()
    }


def _stored_payload(rev: int, drafts: dict[str, str], index: dict[str, dict]) -> str:
    """把内存里的草稿集合与逐键索引写回 v2 存储形状（只追加字段，兼容旧读法）。"""
    keys: dict[str, dict] = {}
    for key, entry in index.items():
        item: dict[str, Any] = {
            "version": int(entry.get("version") or 0),
            "deleted": bool(entry.get("deleted")),
        }
        if entry.get("deleted"):
            # 墓碑保留最后一份正文：迟到的更新相撞时要能「保留双方文字」
            item["text"] = str(entry.get("text") or "")
        keys[key] = item
    return models.dumps({"rev": int(rev), "drafts": dict(drafts), "keys": keys})


def get_draft(conn: sqlite3.Connection, board_id: str) -> dict:
    """读取草稿。草稿不是提交内容，也不会交给 QIO。

    除草稿本体与更新时间外，返回：

    - 单调存簿版本号 rev（区分「客户端计划清除的版本」与「服务端里真正存下的版本」）；
    - 逐键版本 keyVersions（C1）：客户端据此在保存时证明「这次更新/删除基于哪一版」。
    """
    info = ensure_board(conn, board_id=board_id)
    row = conn.execute(
        "SELECT updated_at FROM board_drafts WHERE board_id = ?", (info["id"],)
    ).fetchone()
    drafts, rev, index = _read_stored_drafts(conn, info["id"])
    return {
        "drafts": drafts,
        "updatedAt": row["updated_at"] if row else None,
        "rev": rev,
        "keyVersions": {
            key: int(entry["version"]) for key, entry in index.items() if not entry["deleted"]
        },
    }


def _clean_draft_entries(drafts: Any) -> dict[str, str]:
    """校验并规范化逐键正文（超限整体拒绝，绝不截短、绝不部分写入）。"""
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
    return clean


def _clean_removed_keys(removed: Any, *, updates: dict[str, str]) -> list[str]:
    if removed is None:
        return []
    if not isinstance(removed, list):
        raise ValueError("removed 必须是草稿键数组")
    keys: list[str] = []
    for item in removed:
        if not isinstance(item, str) or not item:
            raise ValueError("removed 里的每一项都必须是非空字符串键")
        if item in updates:
            raise ValueError(f"同一个键不能既更新又删除：{item}")
        if item not in keys:
            keys.append(item)
    return keys


def _clean_key_versions(raw: Any) -> dict[str, int]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("keyVersions 必须是 {键: 版本号} 对象")
    versions: dict[str, int] = {}
    for key, value in raw.items():
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("keyVersions 里的版本号必须是整数")
        versions[str(key)] = int(value)
    return versions


def save_draft(
    conn: sqlite3.Connection,
    board_id: str,
    drafts: dict,
    *,
    removed: Any = None,
    base_rev: Any = None,
    key_versions: Any = None,
    mode: Any = None,
) -> dict:
    """保存草稿（未提交的文字输入）。**不调用 QIO**，也不写入板面状态。

    契约 C1 的三态语义（`mode="patch"` 或带了 removed / keyVersions 时）：

    - `drafts` = 本次**确实上传**的逐键更新；
    - `removed` = 本次**明确删除**的键；
    - 两者都没出现的键 = **未修改**：服务端绝不因此删除它（F01 的根治点）。

    安全规则：

    - `baseRev` 是客户端读到的服务端 rev；与当前 rev 一致即为「无并发改写」的安全基线；
    - 没有安全基线时，单个键只有带上与服务端一致的**正版本号**（keyVersions）才允许覆盖/删除；
    - 无法证明身份的同键改写 → 冲突：服务端保留自己那份文字，响应里同时给出双方文字；
    - 无法证明身份的删除 → 保留服务器上的文字并列入 blockedRemovals（绝不隐式删除）；
    - 旧请求体（无 mode/baseRev/removed）走旧「整集合替换」语义，但同样不允许在没有安全基线时
      删除未列出的键：响应里显式标记 `legacyReplace=True`。
    """
    clean = _clean_draft_entries(drafts)
    remove_keys = _clean_removed_keys(removed, updates=clean)
    versions = _clean_key_versions(key_versions)
    if base_rev is not None and (isinstance(base_rev, bool) or not isinstance(base_rev, int)):
        raise ValueError("baseRev 必须是整数版本号")
    if mode is not None and mode not in ("patch", "replace"):
        raise ValueError("mode 只能是 patch 或 replace")

    info = ensure_board(conn, board_id=board_id)
    prev, prev_rev, index = _read_stored_drafts(conn, info["id"])
    # patch = 逐键三态；否则是旧「整集合替换」（未出现的键是删除候选，但要过版本门）
    explicit_patch = mode == "patch" or removed is not None or key_versions is not None
    delete_keys = remove_keys if explicit_patch else [key for key in prev if key not in clean]
    safe_base = base_rev is not None and base_rev == prev_rev

    def identity_ok(key: str, entry: dict) -> bool:
        """没有安全基线时，只有明确写上来的**正版本号**与服务端一致才算证明身份。"""
        version = int(entry.get("version") or 0)
        return version > 0 and versions.get(key) == version

    conflicts: list[dict] = []
    blocked: list[str] = []
    applied: dict[str, str] = {}

    for key, text in clean.items():
        entry = index.get(key)
        if entry is None:
            applied[key] = text
            continue
        if entry["deleted"]:
            # 删除与更新相撞：服务器上这个键是被显式删掉的（墓碑）
            if safe_base or identity_ok(key, entry):
                applied[key] = text
            else:
                conflicts.append({"key": key, "local": text, "server": entry["text"]})
            continue
        server_text = prev.get(key, entry["text"])
        if server_text == text:
            # 同内容更新：不是冲突，也不推进版本（客户端手里那一版仍然有效）
            continue
        if not explicit_patch and base_rev is None:
            # 真·旧客户端的整集合断言：逐键覆盖是旧语义，删除另有版本门保护
            applied[key] = text
        elif safe_base or identity_ok(key, entry):
            applied[key] = text
        else:
            conflicts.append({"key": key, "local": text, "server": server_text})

    cleared: list[str] = []
    for key in delete_keys:
        entry = index.get(key)
        if key not in prev or entry is None or entry["deleted"]:
            continue  # 本来就没有：无事可做
        if safe_base or identity_ok(key, entry):
            cleared.append(key)
            continue
        blocked.append(key)
        conflicts.append({"key": key, "local": "", "server": prev.get(key, "")})

    changed = bool(applied) or bool(cleared)
    stamp = _now()
    new_drafts = dict(prev)
    new_index = {
        key: {"version": int(entry["version"]), "deleted": bool(entry["deleted"]), "text": entry["text"]}
        for key, entry in index.items()
    }
    for key, text in applied.items():
        entry = new_index.get(key) or {"version": 0, "deleted": False, "text": ""}
        new_index[key] = {
            "version": int(entry["version"]) + 1,
            "deleted": False,
            "text": text,
        }
        new_drafts[key] = text
    for key in cleared:
        entry = new_index[key]
        new_index[key] = {
            "version": int(entry["version"]) + 1,
            "deleted": True,
            "text": new_drafts.get(key, entry["text"]),
        }
        new_drafts.pop(key, None)

    new_rev = prev_rev + 1 if changed else prev_rev
    row = conn.execute(
        "SELECT updated_at FROM board_drafts WHERE board_id = ?", (info["id"],)
    ).fetchone()
    updated_at = row["updated_at"] if row else None
    if changed:
        conn.execute(
            "INSERT INTO board_drafts (board_id, drafts, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(board_id) DO UPDATE SET drafts = excluded.drafts,"
            " updated_at = excluded.updated_at",
            (info["id"], _stored_payload(new_rev, new_drafts, new_index), stamp),
        )
        updated_at = stamp

    return {
        "drafts": new_drafts,
        "updatedAt": updated_at,
        "rev": new_rev,
        "keyVersions": {
            key: int(entry["version"]) for key, entry in new_index.items() if not entry["deleted"]
        },
        "cleared": sorted(cleared),
        "conflicts": conflicts,
        "blockedRemovals": sorted(blocked),
        "mode": "patch" if explicit_patch else "replace",
        "legacyReplace": not explicit_patch,
    }
