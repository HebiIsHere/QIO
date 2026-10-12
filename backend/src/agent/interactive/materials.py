"""本次可见范围里的材料解析（契约 docs/interactive-mode-contract.md §14.1 / §14.3）。

职责：把**已经允许查看**的卡片解析成可以交给模型理解的条目，并如实记录每一条
到底有没有读到正文（`models.MATERIAL_SOURCES` 是唯一口径）：

- 文字注释 / 代码卡片：正文就在卡片里（inline）；
- 文件卡片：只认卡片 meta 里登记的**已保存附件 id**（`meta.file.attachmentId`），
  用 `AttachmentService.content_target` 拿到真实路径，复用
  `agent/tools/attachment_tools.py` 的既有解析（文本 / CSV / HTML / DOCX / XLSX）；
  文件名与说明文字**不是正文**，绝不拿它们冒充内容；
- 图片卡片：当前模型链路没有视觉能力 → `image_unsupported`，绝不声称看懂；
- URL 卡片：只有真的已经有正文才 `url_fetched`，只有地址一律 `url_address_only`；
- 格式未实现 / 读取失败 → `unreadable` + 可读原因；
- `max_chars` 截断如实置 `truncated=true`，并记录截断前已解析到的原始 `characters`。

边界：

- 只读**已保存的附件**与已保存的板面状态，不抓取网络内容、不执行文件里的任何东西；
- 条目里的 `text` 只放真实读到的正文；没读到就是空串，配 `read=false` 与 `reason`；
- 本函数是**同步**的：调用方必须在事件循环线程上调用（数据库连接与附件服务都不是
  线程安全的，见 services/attachments.py 的线程纪律）。
"""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import zipfile
from pathlib import Path
from typing import Any, Iterable

from agent.interactive import models
from agent.services.attachments import (
    Attachment,
    AttachmentContentError,
    AttachmentService,
    classify_readability,
)
from agent.tools import attachment_tools
from agent.trace.redact import redact_text

#: 单条材料交付给模型的字符上限（契约 §14.3 的默认值）
DEFAULT_MAX_CHARS = 40000

#: 解析器单次交付的字符预算（与 tools/attachment_tools.py 的 MAX_CHARS 对齐）。
#: 请求的「段」数刻意给得比默认大：真正的约束是字符预算，段数只是窗口。
PARSER_CHAR_BUDGET = attachment_tools.MAX_CHARS
_MAX_UNITS = 20000

#: URL 卡片里代表「已经抓到的正文」的 meta 字段名（只认显式登记的正文）
_URL_BODY_KEYS = ("content", "text", "excerpt", "fetchedText", "fetched_text")
#: 文件 / 图片卡片里登记附件 id 的字段名
_ATTACHMENT_ID_KEYS = ("attachmentId", "attachment_id")

#: 单张图片**交给模型**的字节上限。超过就不交付字节（如实说明原因、并给出尺寸与上限），
#: 绝不悄悄截断成半张图 —— 图片是有损内容，截一半等于给了错的画面。
MAX_IMAGE_BYTES = 8 * 1024 * 1024
#: 允许作为多模态图片下发的媒体类型（其它类型如 svg/tiff 交给既有文本读取结论，不猜）。
DELIVERABLE_IMAGE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def resolve_materials(
    conn: sqlite3.Connection,
    *,
    state: dict,
    cards: Iterable[dict] | None,
    attachments: Any = None,
    max_chars: int = DEFAULT_MAX_CHARS,
    vision: bool = False,
) -> list[dict]:
    """把本次可见范围里的材料解析成条目（字段见 §14.3 与 §14.1 的来源口径）。

    - `state`：已保存的板面状态（`cards` 里给 id 时从这里取卡片；也给防御性可见性过滤）；
    - `cards`：本次允许查看的卡片（dict），给 None 时退回 models.selectable_cards(state)；
    - `attachments`：AttachmentService；None 时按连接的数据库位置尽力推导一份
      （推不出来就如实把附件类材料记成 unreadable，而不是假装读到了）；
    - `max_chars`：单条材料的字符上限，超出的部分不交付并记 truncated=true。
    """
    state = state if isinstance(state, dict) else {}
    limit_chars = _char_limit(max_chars)
    service = attachments if attachments is not None else _derive_service(conn)
    tool = attachment_tools.ReadAttachmentTool(service) if service is not None else None

    entries: list[dict] = []
    for card in _iter_cards(state, cards):
        entry = _resolve_card(
            card, service=service, tool=tool, max_chars=limit_chars, vision=bool(vision)
        )
        if entry is not None:
            entries.append(entry)
    return entries


def resolve_images(
    conn: sqlite3.Connection,
    *,
    state: dict,
    cards: Iterable[dict] | None,
    attachments: Any = None,
    max_bytes: int = MAX_IMAGE_BYTES,
) -> list[dict]:
    """本次可见范围里**可以真正交给模型看**的图片（多模态交付）。

    与 `resolve_materials` 同一套可见性纪律（未勾选的注释 / 隐藏 / 已删除一律不读），
    只处理 `kind="image"` 且 meta 里登记了**真实已保存附件**的卡片：返回的每一条都是
    「读了字节、能交给视觉模型」的条目 —— 读不到 / 类型不支持 / 超过上限的**不在这里**，
    而是在 `resolve_materials` 的条目里如实说明原因（调用方据此区分「没交付」与「交付了」）。

    返回条目字段：
    - `cardId`：哪张卡片（模型引用它时用得上）；
    - `attachmentId` / `name` / `bytes`：来自哪份已保存附件、多大；
    - `mediaType`：`image/png` 这类真实媒体类型（按扩展名，与 data URL 一致）；
    - `dataUrl`：`data:<mediaType>;base64,<…>` —— 交付给适配器的就是它。
    """
    limit_bytes = int(max_bytes) if int(max_bytes) > 0 else MAX_IMAGE_BYTES
    state = state if isinstance(state, dict) else {}
    service = attachments if attachments is not None else _derive_service(conn)
    if service is None:
        return []
    out: list[dict] = []
    for card in _iter_cards(state, cards):
        if str(card.get("kind") or "") != "image":
            continue
        item = _deliverable_image(card, service=service, max_bytes=limit_bytes)
        if item is not None:
            out.append(item)
    return out


def _deliverable_image(card: dict, *, service: Any, max_bytes: int) -> dict | None:
    """这张图片卡能不能真的交付字节；不能就返回 None（原因由材料条目如实说明）。"""
    meta = card.get("meta") if isinstance(card.get("meta"), dict) else {}
    raw_id = _attachment_id(meta, "image") or _attachment_id(meta, "file")
    if not raw_id:
        return None
    att = service.get(raw_id, check=False)
    if att is None or str(getattr(att, "state", "")) != "ready":
        return None
    name = str(getattr(att, "original_name", "") or "")
    media_type = _image_media_type(name)
    if not media_type:
        return None
    path, _origin = service_for_content(service, att)
    if path is None:
        return None
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size <= 0 or size > max_bytes:
        return None
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if not raw:
        return None
    label = _label(card) or name or "（图片）"
    return {
        "cardId": card.get("id"),
        "attachmentId": att.id,
        "name": name,
        "bytes": len(raw),
        "mediaType": media_type,
        "label": label,
        "dataUrl": "data:" + media_type + ";base64," + base64.b64encode(raw).decode("ascii"),
    }


def _image_media_type(name: str) -> str:
    suffix = Path(str(name or "")).suffix.lower()
    return DELIVERABLE_IMAGE_TYPES.get(suffix, "")


def service_for_content(service: Any, att: Any) -> tuple[Path | None, str]:
    """这条附件真正可读的路径（有副本读副本，没有副本读原文件）。

    与 `_content_path` 同一口径（`services/attachments.py` 的 content_target 是唯一实现），
    这里只做「拿不到就返回 (None, reason)」的收口，不复制判断。
    """
    try:
        target = service.content_target(att.id)
    except Exception:  # noqa: BLE001 - 读不到就是读不到，调用方会如实标注
        return None, ""
    if not target:
        return None, ""
    path, origin = target
    return Path(path), str(origin)


def _char_limit(value: Any) -> int:
    if value is None:
        return DEFAULT_MAX_CHARS
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("max_chars 必须是整数（材料交付给模型的字符上限）") from exc
    if parsed < 0:
        raise ValueError("max_chars 不能是负数")
    return parsed


def _derive_service(conn: sqlite3.Connection) -> AttachmentService | None:
    """没有传 attachments 时，按这条连接真正打开的数据库位置推导附件服务。

    生产库里数据库就在 data_dir 之下（`<data_dir>/app.db`），所以
    `AttachmentService(conn, Path(db).parent)` 与 create_app 里建的是同一个根目录。
    推导不出来就返回 None —— 附件类材料会如实记成不可读，不假装读到了内容。
    """
    if conn is None:
        return None
    try:
        from agent.storage.db_identity import connection_db_path

        path = connection_db_path(conn)
        if not path:
            return None
        return AttachmentService(conn, Path(path).parent)
    except Exception:  # noqa: BLE001 - 推导失败是「没有服务」，不是致命错误
        return None


def _iter_cards(state: dict, cards: Iterable[dict] | None):
    """按可见范围口径过滤输入卡片：范围外的卡片（未勾选注释 / 隐藏 / 已删除）绝不读。"""
    source: Iterable[Any]
    if cards is None:
        source = models.selectable_cards(state)
    else:
        source = list(cards)
    for item in source:
        card = models.card_by_id(state, item) if isinstance(item, str) else item
        if not isinstance(card, dict) or not card.get("id"):
            continue
        # 防御：即使调用方把范围外的卡片塞进来，也不读它的正文
        if not models.is_live(card) or not models.visible_except_deleted(card):
            continue
        yield card


# ---------------------------------------------------------------------------
# 单个卡片
# ---------------------------------------------------------------------------


def _resolve_card(
    card: dict,
    *,
    service: AttachmentService | None,
    tool: attachment_tools.ReadAttachmentTool | None,
    max_chars: int,
    vision: bool = False,
) -> dict | None:
    kind = str(card.get("kind") or "")
    meta = card.get("meta") if isinstance(card.get("meta"), dict) else {}
    label = _label(card)

    if kind == models.REPLY_KIND:
        # QIO 自己的结果卡片不是用户材料，不进理解上下文
        return None
    if kind in ("text", "code"):
        return _inline_entry(card, label, max_chars=max_chars)
    if kind == "file":
        return _file_entry(card, label, meta, service=service, tool=tool, max_chars=max_chars)
    if kind == "image":
        return _image_entry(card, label, meta, service=service, vision=vision)
    if kind == "url":
        return _url_entry(card, label, meta, max_chars=max_chars)
    return _entry(
        card,
        source="unsupported",
        label=label,
        version=_content_version(card),
        read=False,
        bytes=0,
        characters=0,
        reason=f"不支持的材料类型：{kind or '未知'}（本次没有把它当成正文交给模型）",
    )


def _inline_entry(card: dict, label: str, *, max_chars: int) -> dict:
    """文字注释 / 代码卡片：content 就是正文。"""
    raw = str(card.get("content") or "")
    delivered, cut = _cut(raw, max_chars)
    read = bool(delivered)
    reason = "" if read else "卡片正文为空：这张材料没有可交付的文字"
    notes = []
    if cut:
        notes.append(
            f"截断：已解析 {len(raw)} 字符，本次只交付前 {len(delivered)} 字符（上限 {max_chars}）"
        )
    if str(card.get("kind")) == "code" and isinstance(card.get("meta"), dict):
        language = str(card["meta"].get("language") or "").strip()
        if language:
            notes.append(f"代码语言：{language}")
    return _entry(
        card,
        source="inline",
        label=label,
        version=_content_version(card),
        read=read,
        truncated=cut,
        bytes=len(raw.encode("utf-8")),
        characters=len(raw),
        reason=reason,
        text=delivered if read else "",
        notes=notes,
    )


def _file_entry(
    card: dict,
    label: str,
    meta: dict,
    *,
    service: AttachmentService | None,
    tool: attachment_tools.ReadAttachmentTool | None,
    max_chars: int,
) -> dict:
    raw_id = _attachment_id(meta, "file")
    shown_name = label or str(meta.get("name") or "").strip() or "（未命名文件）"
    if not raw_id:
        return _entry(
            card,
            source="unreadable",
            label=shown_name,
            version=_content_version(card),
            read=False,
            bytes=0,
            characters=0,
            reason=(
                f"这张文件卡片还没有关联已保存的附件（meta.file.attachmentId 缺失）："
                f"只有文件名「{shown_name}」，文件名不是正文，未读取正文"
            ),
        )
    if service is None:
        return _entry(
            card,
            source="unreadable",
            label=shown_name,
            version=_content_version(card),
            read=False,
            bytes=0,
            characters=0,
            attachment_id=raw_id,
            reason="本次没有可用的附件服务：读不到已保存附件的正文（不能凭文件名或说明推断内容）",
        )
    att = service.get(raw_id, check=False)
    if att is None:
        return _entry(
            card,
            source="unreadable",
            label=shown_name,
            version=_content_version(card),
            read=False,
            bytes=0,
            characters=0,
            attachment_id=raw_id,
            reason=f"找不到这个附件 id：{raw_id}（可能已被删除或不属于这台设备）",
        )
    return _attachment_entry(
        card, att, label=label or att.original_name, tool=tool, max_chars=max_chars
    )


def _attachment_entry(
    card: dict,
    att: Attachment,
    *,
    label: str,
    tool: attachment_tools.ReadAttachmentTool | None,
    max_chars: int,
) -> dict:
    """真实读取已保存附件的正文（复用既有解析，不重写）。"""
    mode, mode_label, mode_note = classify_readability(att.original_name)
    version = _attachment_version(att)

    def unreadable(reason: str, *, bytes_: int = 0, notes: list[str] | None = None) -> dict:
        return _entry(
            card,
            source="unreadable",
            label=label,
            version=version,
            read=False,
            bytes=bytes_,
            characters=0,
            attachment_id=att.id,
            reason=reason,
            notes=notes or [],
        )

    if mode == "image":
        # 图片走 file 卡片（扩展名是图片）：同样没有视觉能力，如实标注
        return _entry(
            card,
            source="image_unsupported",
            label=label,
            version=version,
            read=False,
            bytes=_int_bytes(att),
            characters=0,
            attachment_id=att.id,
            reason="当前模型链路没有视觉能力：只登记图片元数据，不声称看懂图片内容",
            notes=[f"附件类型：{mode_label}（{mode_note}）"],
        )
    if att.state == "prepared":
        return unreadable(f"附件「{att.original_name}」还在准备中（{att.state}），现在没有可读的内容")
    if att.state == "cancelled":
        return unreadable(f"附件「{att.original_name}」的准备已被取消，没有可读的内容")
    if mode == "unsupported":
        return unreadable(f"当前不可读取：{mode_note}", bytes_=_int_bytes(att))

    service = service_for(tool)
    path, origin = _content_path(service, att, tool)
    if path is None:
        fresh = service.get(att.id) if service is not None else None
        current = fresh or att
        return unreadable(
            f"读不到附件「{current.original_name}」的正文："
            f"{current.error or '文件不在原来登记的位置'}（状态 {current.state}）"
        )

    notes: list[str] = []
    if origin == "source":
        notes.append(
            "读的是用户本地原文件（引用方式保存）：内容可能已经被改动；"
            "历史保留的是位置，不保证内容仍然存在"
        )

    try:
        if mode in ("text", "csv"):
            content = attachment_tools._extract_text(
                att, path, 0, _MAX_UNITS, 0, mode_label, None
            )
        elif mode == "html":
            content = attachment_tools._extract_html(att, path, 0, _MAX_UNITS, 0, None)
        elif mode == "docx":
            content = attachment_tools._extract_docx(att, path, 0, _MAX_UNITS, 0, None)
        elif mode == "xlsx":
            content = attachment_tools._extract_xlsx(att, path, 0, _MAX_UNITS, 0, None)
        else:  # sniff：扩展名不认识，按内容嗅探
            content = attachment_tools._extract_unknown(att, path, 0, _MAX_UNITS, 0, None)
    except attachment_tools.AttachmentLimitExceeded as exc:
        return unreadable(str(exc), bytes_=_file_bytes(path, att))
    except zipfile.BadZipFile:
        return unreadable(
            f"附件「{att.original_name}」看起来不是有效的 {mode_label} 文件"
            "（压缩结构损坏或被改名），当前读不到内容",
            bytes_=_file_bytes(path, att),
        )
    except OSError as exc:
        return unreadable(
            f"读取附件「{att.original_name}」失败：{type(exc).__name__}: {redact_text(str(exc))}",
            bytes_=_file_bytes(path, att),
        )
    except Exception as exc:  # noqa: BLE001 - 解析异常一律如实记成不可读，不伪装成已读
        return unreadable(
            f"解析附件「{att.original_name}」失败：{type(exc).__name__}: {redact_text(str(exc))}",
            bytes_=_file_bytes(path, att),
        )

    if not content.readable:
        return unreadable(
            content.reason or "当前不可读取（解析器没有给出内容）",
            bytes_=_file_bytes(path, att),
            notes=list(content.notes),
        )

    raw_text = content.content or ""
    delivered, cut = _cut(raw_text, max_chars)
    parser_truncated = bool(content.truncated or content.partial) or (
        content.next_offset is not None
    )
    truncated = bool(parser_truncated or cut)
    characters = len(raw_text)
    notes.extend(content.notes)
    if truncated:
        notes.append(
            f"截断：已解析 {characters} 字符，本次只交付前 {len(delivered)} 字符"
            f"（解析单次上限 {PARSER_CHAR_BUDGET} 字符）：没有读全，不得当作完整内容"
        )
    # 诚实口径：只有真的读到正文才允许 attachment_text（读了但正文为空 → unreadable）
    read = bool(delivered)
    reason = "" if read else "附件读取成功但正文为空：这份材料没有可交付的文字，未读取到正文"
    return _entry(
        card,
        source="attachment_text" if read else "unreadable",
        label=label,
        version=version,
        read=read,
        truncated=truncated,
        bytes=_file_bytes(path, att),
        characters=characters,
        reason=reason,
        attachment_id=att.id,
        text=delivered if read else "",
        notes=notes,
    )


def _image_entry(
    card: dict,
    label: str,
    meta: dict,
    *,
    service: AttachmentService | None,
    vision: bool = False,
    max_image_bytes: int = MAX_IMAGE_BYTES,
) -> dict:
    """图片卡片：能交付就如实说「字节已交给模型」，不能就如实说为什么不能。

    `vision=True` 表示**本次模型链路支持多模态**（适配器声明）；此时真正读了字节的图片
    记成 `image_delivered`（read=True，说明模型自己看），其余情况一律 `image_unsupported`
    并带上可读原因（没有登记附件 / 附件不在 / 类型不支持 / 超过字节上限）。
    """
    raw_id = _attachment_id(meta, "image") or _attachment_id(meta, "file")
    att = service.get(raw_id, check=False) if (service is not None and raw_id) else None
    bytes_ = _int_bytes(att) if att is not None else 0
    version = _attachment_version(att) if att is not None else _content_version(card)
    name = str(getattr(att, "original_name", "") or "") if att is not None else ""
    shown = label or "（图片）"

    if vision and att is not None:
        if str(getattr(att, "state", "")) != "ready":
            return _image_unsupported(
                card,
                shown,
                version,
                bytes_,
                att.id,
                f"图片附件当前状态是 {getattr(att, 'state', '未知')}：没有可交付的字节，"
                "本次不声称看过这张图",
                ["视觉理解需要附件处于 ready 状态"],
            )
        media_type = _image_media_type(name)
        if not media_type:
            return _image_unsupported(
                card,
                shown,
                version,
                bytes_,
                att.id,
                f"这种图片格式（{Path(name).suffix or '无扩展名'}）不作为多模态图片交付："
                "当前只交付 png / jpg / gif / webp；其余按既有读取结论处理",
                ["不把无法确认媒体类型的字节当图片发给模型"],
            )
        limit = int(max_image_bytes) if int(max_image_bytes) > 0 else MAX_IMAGE_BYTES
        if bytes_ > limit:
            return _image_unsupported(
                card,
                shown,
                version,
                bytes_,
                att.id,
                f"图片 {bytes_} 字节超过交付上限 {limit} 字节：本次没有把像素交给模型"
                "（不截断成半张图，因为半张图会给出错的画面）",
                ["要么压缩后再发，要么把结论写在注释里让 QIO 读文字"],
            )
        return _entry(
            card,
            source="image_delivered",
            label=shown,
            version=version,
            read=True,
            bytes=bytes_,
            characters=0,
            attachment_id=att.id,
            reason="",
            notes=[
                f"本次把这张图片的真实字节（{name or '图片'}，{bytes_} 字节）作为多模态内容交给了模型，"
                "由模型自己看；QIO 不另外编造画面描述",
            ],
        )

    if not raw_id:
        reason = (
            "这张图片卡片还没有关联已保存的附件（meta.image.attachmentId 缺失）："
            "本次没有可读的字节，也未读取图片内容"
        )
        notes = ["没有附件就没有像素可读；文件名不是内容"]
    elif att is None:
        reason = f"登记的附件 id 当前取不到：{raw_id}（可能已被删除或不属于这台设备）"
        notes = ["取不到附件时不猜图片内容"]
    elif vision:
        reason = "这张图片没有可交付的字节（读取失败或为空）：本次未把它交给模型"
        notes = ["视觉链路可用，但这份附件读不出字节"]
    else:
        reason = "当前模型链路没有视觉能力：只登记图片元数据，不声称看懂图片内容"
        notes = ["当前模型链路没有视觉能力：不读取图片像素、不编造画面描述"]
    return _image_unsupported(card, shown, version, bytes_, att.id if att is not None else (raw_id or None), reason, notes)


def _image_unsupported(
    card: dict,
    label: str,
    version: str,
    bytes_: int,
    attachment_id: str | None,
    reason: str,
    notes: list[str],
) -> dict:
    """图片**没有**交付给模型时的统一回执（原因必须可读，不许含糊）。"""
    return _entry(
        card,
        source="image_unsupported",
        label=label,
        version=version,
        read=False,
        bytes=bytes_,
        characters=0,
        attachment_id=attachment_id,
        reason=reason,
        notes=notes,
    )


def _url_entry(card: dict, label: str, meta: dict, *, max_chars: int) -> dict:
    """URL 卡片：只有真的抓到正文才 url_fetched；只有地址一律 url_address_only。"""
    url_meta = meta.get("url") if isinstance(meta.get("url"), dict) else {}
    address = str(url_meta.get("href") or card.get("content") or "").strip()
    body = ""
    for key in _URL_BODY_KEYS:
        value = url_meta.get(key)
        if isinstance(value, str) and value.strip():
            body = value
            break
    notes = ["只按卡片上已经登记的正文判断；本次不抓取网络内容"]
    if not body:
        return _entry(
            card,
            source="url_address_only",
            label=label,
            version=_content_version(card),
            read=False,
            bytes=len(address.encode("utf-8")),
            characters=0,
            reason="只有网址，没有抓到网页正文：本次理解不凭地址推断内容，也不假装读过",
            notes=notes + ["要正文需要在获准后真实抓取页面；地址本身不是正文"],
            address=address,
        )
    delivered, cut = _cut(body, max_chars)
    if cut:
        notes.append(
            f"截断：已抓到 {len(body)} 字符，本次只交付前 {len(delivered)} 字符（上限 {max_chars}）"
        )
    return _entry(
        card,
        source="url_fetched",
        label=label,
        version=_content_version(card) + f":{hashlib.sha256(body.encode('utf-8')).hexdigest()}",
        read=True,
        truncated=cut,
        bytes=len(body.encode("utf-8")),
        characters=len(body),
        reason="",
        text=delivered,
        notes=notes + ["正文来自卡片上已登记的抓取结果（不是本次联网抓取）"],
        address=address,
    )


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def service_for(tool: attachment_tools.ReadAttachmentTool | None) -> AttachmentService | None:
    """从既有工具上取回附件服务（工具与解析器是同一份事实源）。"""
    return getattr(tool, "attachments", None) if tool is not None else None


def _content_path(
    service: AttachmentService | None,
    att: Attachment,
    tool: attachment_tools.ReadAttachmentTool | None,
) -> tuple[Path | None, str]:
    """真实要读的文件 + 来源（副本 / 用户原文件）。

    先走契约要求的 `AttachmentService.content_target`（只认 QIO 自己管理的副本）；
    引用型附件与副本丢失的情况退回既有 `ReadAttachmentTool._readable_path` 的口径。
    """
    if service is not None:
        try:
            path, _name = service.content_target(att.id)
            if path is not None and Path(path).is_file():
                return Path(path), "copy"
        except AttachmentContentError:
            pass  # 引用型 / 副本丢失：按既有口径退回原文件
        except Exception:  # noqa: BLE001 - 取路径失败按「读不到」处理，不抛给调用方
            pass
    if tool is not None:
        path, origin = tool._readable_path(att)  # 复用既有口径（副本优先，其次原文件）
        if path is not None:
            return path, origin
    return None, "none"


def _attachment_id(meta: dict, key: str) -> str:
    """从卡片 meta 里取已保存附件 id：`meta.file.attachmentId` / `meta.image.attachmentId`。"""
    block = meta.get(key)
    if isinstance(block, dict):
        for name in _ATTACHMENT_ID_KEYS:
            value = block.get(name)
            if value:
                return str(value).strip()
    for name in _ATTACHMENT_ID_KEYS:
        top = meta.get(name)
        if top and str(top).strip():
            return str(top).strip()
    return ""


def _cut(text: str, max_chars: int) -> tuple[str, bool]:
    if max_chars is None or len(text) <= max_chars:
        return text, False
    return text[:max_chars], True


def _file_bytes(path: Path, att: Attachment) -> int:
    try:
        return int(path.stat().st_size)
    except OSError:
        return _int_bytes(att)


def _int_bytes(att: Attachment | None) -> int:
    if att is None:
        return 0
    try:
        return int(att.size_bytes or 0)
    except (TypeError, ValueError):
        return 0


def _attachment_version(att: Attachment) -> str:
    """附件版本：有 sha256 用 sha256；否则用「大小 + 修改时间 + 更新时间」的自证组合。"""
    if att.sha256:
        return f"sha256:{att.sha256}"
    mtime = "" if att.mtime is None else f"{float(att.mtime):.3f}"
    return f"att:{att.id}:{_int_bytes(att)}:{mtime}:{att.updated_at or ''}"


def _content_version(card: dict, *extra: str) -> str:
    payload = json.dumps(
        {
            "kind": card.get("kind"),
            "content": str(card.get("content") or ""),
            "meta": card.get("meta") or {},
            "extra": list(extra),
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return "c:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _label(card: dict) -> str:
    """展示用标签（与 submission.card_label 同一口径；只做展示，永不当作正文）。"""
    kind = card.get("kind")
    meta = card.get("meta") if isinstance(card.get("meta"), dict) else {}
    if kind == "text":
        return _snippet(card.get("content"))
    if kind in ("file", "image"):
        block = meta.get(kind) if isinstance(meta.get(kind), dict) else {}
        return _snippet(
            block.get("name") or block.get("title") or meta.get("name") or meta.get("title")
            or card.get("content")
        )
    if kind == "code":
        language = str(meta.get("language") or "").strip()
        return f"{language} 代码" if language else _snippet(card.get("content"))
    if kind == "url":
        return _snippet(meta.get("title") or meta.get("href") or card.get("content"))
    return _snippet(card.get("content"))


def _snippet(value: Any, limit: int = 24) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        return "（无文字）"
    return text if len(text) <= limit else text[:limit] + "…"


def _entry(
    card: dict,
    *,
    source: str,
    label: str,
    version: str,
    read: bool,
    bytes: int,
    characters: int,
    reason: str,
    truncated: bool = False,
    attachment_id: str | None = None,
    text: str = "",
    delivered: int | None = None,
    notes: list[str] | None = None,
    **extra: Any,
) -> dict:
    """材料条目（前端 UnderstandingMaterial 的字段 + 交付用的 text / 说明）。"""
    if source not in models.MATERIAL_SOURCES:
        raise ValueError(f"未知的材料来源：{source}（只能取 models.MATERIAL_SOURCES）")
    entry = {
        "cardId": str(card.get("id") or ""),
        "kind": str(card.get("kind") or ""),
        "label": label,
        "source": source,
        "version": version,
        "read": bool(read),
        "truncated": bool(truncated),
        "bytes": int(bytes or 0),
        # 契约口径：characters 是**截断前**已解析到的原始字符数；交付量另记 deliveredCharacters
        "characters": int(characters or 0),
        "reason": reason or "",
        "attachmentId": attachment_id or None,
        # 交付给模型的正文；read=false 时恒为空串（不伪造）
        "text": text if read else "",
        "deliveredCharacters": len(text) if read else 0,
        "notes": list(notes or []),
    }
    if delivered is not None:
        entry["deliveredCharacters"] = int(delivered)
    entry.update(extra)
    return entry
