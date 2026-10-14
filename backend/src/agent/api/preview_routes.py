"""卡片内预览：HTTP 接口（契约 §3.3）。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | /api/attachments/{id}/preview | render_for 的结果；?refresh=1 绕过缓存重算 |
| GET | /api/interactive/boards/{boardId}/preview | {"state": {"positions": {cardId: {...}}}} |
| PUT | /api/interactive/boards/{boardId}/preview/{cardId} | 写入单项；attachmentId 与当前卡不一致 → 409 |
| DELETE | /api/interactive/boards/{boardId}/preview/{cardId} | {"ok":true,"deleted":bool} |

三条硬规则：

1. **异步 + 工作线程**：转换成数据是纯计算，走 services/preview.render_for_async（内部
   asyncio.to_thread）；缓存读写留在事件循环线程（共享 sqlite 连接只有一条线程）。
2. **位置按 attachmentId + version 归属**：版本变了（文件被改 / 重新关联）→ 位置回默认，
   并**如实返回当前 version** 供前端比较；旧版本的位置绝不写到新版本上。
3. **位置只允许契约里的键**：未知键一律丢弃，不做"差不多就行"的宽容解析。

不新增事件、不改板面状态：读位置只碰 board_preview_state 这一张表。
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from agent.interactive import board_store
from agent.services import preview as preview_service
from agent.storage.db import transaction

logger = logging.getLogger(__name__)

router = APIRouter()

_BOARD_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_CARD_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")

#: 契约 §3.3 冻结的 position 形状：**只允许这些键**，未知键丢弃
POSITION_KEYS = ("mode", "page", "slide", "sheet", "row", "line", "zoom", "scrollTop")
#: mode 的取值就是渲染器名（和契约 §3.5 的 renderer 一致）
POSITION_MODES = ("image", "pdf", "docx", "xlsx", "pptx", "code")
_MAX_INDEX = 1_000_000
_MAX_SHEET_NAME = 200
_MIN_ZOOM = 0.1
_MAX_ZOOM = 20.0


def register_preview_routes(app) -> None:
    """把预览路由挂到 app 上 —— server.py 里只需要这一行。

    调用位置：与其它 include_router 放在一起（server.py 路由定义区末尾、return app 之前），
    例如紧跟 app.include_router(interactive_router) 之后。
    """
    app.include_router(router)


# ---------------------------------------------------------------------------
# 参数与位置
# ---------------------------------------------------------------------------


def _body(value: Any) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象")
    return value


def _board_id(raw: str) -> str:
    value = str(raw or "").strip()
    if not _BOARD_ID_RE.match(value):
        raise HTTPException(status_code=400, detail="boardId 只能包含字母、数字与 _.-: 且长度 1..64")
    return value


def _card_id(raw: str) -> str:
    value = str(raw or "").strip()
    if not _CARD_ID_RE.match(value):
        raise HTTPException(status_code=400, detail="cardId 只能包含字母、数字与 _.-: 且长度 1..128")
    return value


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 0 or value > _MAX_INDEX:
        return None
    return int(value)


def _float_or_none(value: Any, *, low: float, high: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number < low or number > high:
        return None
    return number


def normalize_position(raw: Any) -> dict:
    """把请求里的 position 收敛成契约允许的形状（未知键丢弃，非法值丢弃）。"""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    mode = raw.get("mode")
    if isinstance(mode, str) and mode in POSITION_MODES:
        out["mode"] = mode
    for key in ("page", "slide", "row", "line"):
        number = _int_or_none(raw.get(key))
        if number is not None:
            out[key] = number
    sheet = raw.get("sheet")
    if isinstance(sheet, str) and sheet.strip():
        out["sheet"] = sheet.strip()[:_MAX_SHEET_NAME]
    zoom = _float_or_none(raw.get("zoom"), low=_MIN_ZOOM, high=_MAX_ZOOM)
    if zoom is not None:
        out["zoom"] = zoom
    scroll = _float_or_none(raw.get("scrollTop"), low=0.0, high=float(_MAX_INDEX))
    if scroll is not None:
        out["scrollTop"] = scroll
    return out


def _load_position(raw: Any) -> dict:
    """库里存的是我们自己写的 JSON；坏数据当"没有位置"，不猜。"""
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _current_version(store, attachment_id: str) -> str | None:
    """附件**当前**的版本（附件行不存在时 None：位置也就没有归属可言）。"""
    if not attachment_id:
        return None
    try:
        att = store.get(attachment_id, check=False)
    except Exception:  # noqa: BLE001 - 读附件出问题时不让预览状态接口 500
        return None
    if att is None:
        return None
    return preview_service.attachment_version(att)


def _position_item(
    store, *, attachment_id: str, version: str, position: Any, updated_at: str
) -> dict:
    """单项位置：版本对不上就**回默认**，并把当前版本如实报给前端。"""
    current = _current_version(store, attachment_id)
    stored = _load_position(position)
    if current is None or not version or current == version:
        return {
            "attachmentId": str(attachment_id or ""),
            "version": str(current or version or ""),
            "position": stored,
            "updatedAt": str(updated_at or ""),
        }
    return {
        "attachmentId": str(attachment_id or ""),
        "version": str(current),
        "position": {},
        "updatedAt": str(updated_at or ""),
    }


def _card_attachment_id(card: dict) -> str:
    """卡片当前关联的已保存附件 id（只读 meta.file，与 materials 同一口径）。

    旧卡片把附件登记在 meta.image：那条回退已删除 —— 只认卡片真正在用的那份附件，
    否则「残留的旧字段」会被当成关联，把阅读位置写到不是它现在用的附件上。
    """
    meta = card.get("meta") if isinstance(card.get("meta"), dict) else {}
    block = meta.get("file")
    if isinstance(block, dict):
        for name in ("attachmentId", "attachment_id"):
            value = block.get(name)
            if value:
                return str(value).strip()
    for name in ("attachmentId", "attachment_id"):
        value = meta.get(name)
        if value:
            return str(value).strip()
    return ""


def _find_card(conn: sqlite3.Connection, board_id: str, card_id: str) -> dict:
    loaded = board_store.load_board(conn, board_id)
    for card in loaded["state"].get("cards") or []:
        if isinstance(card, dict) and str(card.get("id")) == card_id:
            return card
    raise HTTPException(status_code=404, detail="这块板面上没有这个卡片")


def _ctx(request: Request):
    return request.app.state.ctx


# ---------------------------------------------------------------------------
# 附件预览
# ---------------------------------------------------------------------------


@router.get("/api/attachments/{attachment_id}/preview")
async def attachment_preview(
    request: Request, attachment_id: str, refresh: int = 0
) -> dict:
    """一个附件的预览数据（契约 §3.2 的 render_for 结果，逐字段原样返回）。

    * **永不 500**：损坏 / 加密 / 旧版 Office / 未支持都是 200 + 真实 status 与 reason；
    * 附件不存在 → 404（与 /content、/relocate 一致）；
    * ?refresh=1 绕过缓存重算（仍然走工作线程）。
    """
    store = _ctx(request).attachments
    if store.get(attachment_id) is None:
        raise HTTPException(status_code=404, detail="没有这个附件")
    try:
        return await preview_service.render_for_async(
            store, attachment_id, refresh=bool(refresh)
        )
    finally:
        # 客户端提前离开时别让工作线程白跑：下次同附件的请求也会取消上一代
        if await _client_gone(request):
            preview_service.cancel_preview(attachment_id)


async def _client_gone(request: Request) -> bool:
    try:
        return await request.is_disconnected()
    except Exception:  # noqa: BLE001 - 判定不了就当作还在
        return False


# ---------------------------------------------------------------------------
# 板面阅读位置
# ---------------------------------------------------------------------------


@router.get("/api/interactive/boards/{board_id}/preview")
async def get_board_preview(request: Request, board_id: str) -> dict:
    conn = _ctx(request).conn
    store = _ctx(request).attachments
    bid = _board_id(board_id)
    rows = conn.execute(
        "SELECT card_id, attachment_id, version, position, updated_at"
        " FROM board_preview_state WHERE board_id = ?",
        (bid,),
    ).fetchall()
    positions: dict[str, dict] = {}
    for row in rows:
        positions[str(row["card_id"])] = _position_item(
            store,
            attachment_id=str(row["attachment_id"] or ""),
            version=str(row["version"] or ""),
            position=row["position"],
            updated_at=str(row["updated_at"] or ""),
        )
    return {"state": {"positions": positions}}


@router.put("/api/interactive/boards/{board_id}/preview/{card_id}")
async def put_board_preview(
    request: Request, board_id: str, card_id: str, body: dict | None = None
) -> dict:
    """写入一张卡片的阅读位置（**只影响显示**：不写板面状态、不产生表达/意图）。"""
    conn = _ctx(request).conn
    store = _ctx(request).attachments
    bid = _board_id(board_id)
    cid = _card_id(card_id)
    payload = _body(body)
    attachment_id = str(payload.get("attachmentId") or "").strip()
    version = str(payload.get("version") or "").strip()
    position = normalize_position(payload.get("position"))
    if not attachment_id:
        raise HTTPException(status_code=400, detail="attachmentId 不能为空")

    card = _find_card(conn, bid, cid)
    on_card = _card_attachment_id(card)
    if on_card != attachment_id:
        raise HTTPException(
            status_code=409,
            detail=(
                "这张卡片当前关联的附件与请求里的 attachmentId 不一致"
                "（不把阅读位置写到别的附件上）"
            ),
        )

    existing = conn.execute(
        "SELECT attachment_id, version, position, updated_at FROM board_preview_state"
        " WHERE board_id = ? AND card_id = ?",
        (bid, cid),
    ).fetchone()
    current = _current_version(store, attachment_id)
    if version and current and version != current:
        # 旧版本的位置绝不写到新版本上：不写库，回默认并如实报当前版本
        return _position_item(
            store,
            attachment_id=attachment_id,
            version=current,
            position={},
            updated_at=str(existing["updated_at"]) if existing is not None else "",
        )

    updated_at = _now_iso()
    with transaction(conn):
        conn.execute(
            "INSERT OR REPLACE INTO board_preview_state"
            " (board_id, card_id, attachment_id, version, position, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                bid,
                cid,
                attachment_id,
                version or (current or ""),
                json.dumps(position, ensure_ascii=False, separators=(",", ":")),
                updated_at,
            ),
        )
    return {
        "attachmentId": attachment_id,
        "version": version or (current or ""),
        "position": position,
        "updatedAt": updated_at,
    }


@router.delete("/api/interactive/boards/{board_id}/preview/{card_id}")
async def delete_board_preview(request: Request, board_id: str, card_id: str) -> dict:
    """删除一张卡片的阅读位置（收起卡片 / 换附件时前端调用）。"""
    conn = _ctx(request).conn
    bid = _board_id(board_id)
    cid = _card_id(card_id)
    with transaction(conn):
        cursor = conn.execute(
            "DELETE FROM board_preview_state WHERE board_id = ? AND card_id = ?",
            (bid, cid),
        )
    return {"ok": True, "deleted": bool(cursor.rowcount)}


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
