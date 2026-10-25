"""本次可见范围里的材料解析（契约 docs/interactive-mode-contract.md §14.1 / §14.3）。

职责：把**已经允许查看**的卡片解析成可以交给模型理解的条目，并如实记录每一条
到底有没有读到正文（`models.MATERIAL_SOURCES` 是唯一口径）：

- 统一文字卡（含迁移后的旧 code / url）：**整卡许可**为真时，把块序列的纯文本
  （`text_card.blocks_to_text`，含链接地址与代码正文）作为**一条** inline 材料交付；
  未许可 = 根本不进列表（卡内文字、链接、代码一律不交付，契约 §2.2）；
- 文件卡片：只认卡片 meta 里登记的**已保存附件 id**（`meta.file.attachmentId`），
  用 `AttachmentService.content_target` 拿到真实路径，复用
  `agent/tools/attachment_tools.py` 的既有解析（文本 / CSV / HTML / DOCX / XLSX）；
  文件名与说明文字**不是正文**，绝不拿它们冒充内容；
- 图片（**文件卡的附件**）：本次链路有视觉能力就交付真实字节（`image_delivered`），
  没有视觉能力就如实标 `image_unsupported`，绝不声称看懂；
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
from typing import Any, Callable, Iterable

from agent.interactive import models, text_card
from agent.services import attachment_access, media
from agent.services.attachments import (
    Attachment,
    AttachmentContentError,
    AttachmentService,
    classify_media_kind,
    classify_readability,
    human_size,
)
from agent.tools import attachment_tools
from agent.trace.redact import redact_text

#: 单条材料交付给模型的字符上限（契约 §14.3 的默认值）
DEFAULT_MAX_CHARS = 40000

#: 解析器单次交付的字符预算（与 tools/attachment_tools.py 的 MAX_CHARS 对齐）。
#: 请求的「段」数刻意给得比默认大：真正的约束是字符预算，段数只是窗口。
PARSER_CHAR_BUDGET = attachment_tools.MAX_CHARS
_MAX_UNITS = 20000

#: 文件卡里登记附件 id 的字段名（图片是文件卡的附件，没有独立 kind）
_ATTACHMENT_ID_KEYS = ("attachmentId", "attachment_id")

#: 单张图片**交给模型**的字节上限。超过就不交付字节（如实说明原因、并给出尺寸与上限），
#: 绝不悄悄截断成半张图 —— 图片是有损内容，截一半等于给了错的画面。
MAX_IMAGE_BYTES = 8 * 1024 * 1024
#: 视频抽样帧数的上限（契约 §4.3 的 MAX_SAMPLE_FRAMES；材料层再兜一道）
MAX_MEDIA_SAMPLE_FRAMES = media.MAX_SAMPLE_FRAMES
#: 一次抽样默认取几帧
DEFAULT_MEDIA_SAMPLE_FRAMES = 6
#: 抽样帧的长边像素上限（与 media.DEFAULT_MAX_EDGE 同一口径）
MEDIA_SAMPLE_MAX_EDGE = media.DEFAULT_MAX_EDGE
#: 单帧交付给模型的字节上限：一张 768px JPEG 通常远小于它，超过就如实说明
MAX_FRAME_BYTES = 2 * 1024 * 1024

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
    处理 `kind="file"` 且附件是图片的卡片（用户在板面上用「文件」入口加一张 .png 也是图片；
    图片没有独立 kind —— 图片就是文件卡的附件），且 meta.file 里登记了**真实已保存附件**：
    返回的每一条都是「读了字节、能交给视觉模型」的条目 —— 读不到 / 类型不支持 / 超过上限的**不在这里**，
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
        # 只处理**文件卡**：`_deliverable_image` 只认「可交付的图片类型」，
        # 所以对 docx / pdf 这类文件卡它天然返回 None，不会误交付。
        if str(card.get("kind") or "") != "file":
            continue
        item = _deliverable_image(card, service=service, max_bytes=limit_bytes)
        if item is not None:
            out.append(item)
    return out


def collected_media_candidates(
    attachments: Any,
    turns: Iterable[str],
    *,
    max_bytes: int = media.MAX_MEDIA_BYTES,
) -> list[dict]:
    """本轮附件 → 音视频候选清单（**纯数据**，只含路径/名字/大小/版本）。

    调用方（services/app.py）在**事件循环线程**上做这一步（附件服务与数据库连接都
    不是线程安全的），然后把清单交给工作线程跑 ffmpeg —— 派生处理里就只剩纯文件
    I/O 与子进程，没有任何数据库访问（与项目既有的线程纪律一致）。
    """
    if attachments is None:
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for turn in turns:
        if not turn:
            continue
        try:
            items = attachments.list(turn_id=str(turn), limit=50, check=True)
        except Exception:  # noqa: BLE001 - 清单是旁路，拿不到就没有候选
            continue
        for att in items or []:
            identifier = str(getattr(att, "id", "") or "")
            if not identifier or identifier in seen:
                continue
            # 「作为互动材料」开关：关掉就不**主动**纳入上下文（不是 OS 访问权限）
            if not bool(getattr(att, "material_enabled", 1)):
                continue
            state = str(getattr(att, "state", "") or "")
            name = str(getattr(att, "original_name", "") or "")
            # include_images=True：图片也要进候选清单 —— 图片交付靠的就是这里产出的候选
            # （没有候选就没有内容块，模型只能看到「附加了文件」而拿不到画面）。
            kind = classify_media_kind(name, include_images=True)
            if not kind:
                continue
            # 取不到路径也要**如实说**，不许静默丢弃：
            # 静默丢弃的后果是模型连「没读到」这句事实都拿不到（复审用 100MB+ 的引用型 .jpeg 实测：
            # candidates=[]，没有任何说明）。现在把它降级成一条带可读原因的 *_unsupported 条目。
            # 状态不是 ready 同样**不许静默丢弃**：候选为空意味着模型连「没读到」都不知道
            # （复审实测：把副本删掉后 candidates=0，既没有字节也没有说明）。
            unreadable = ""
            if state != "ready":
                unreadable = (
                    f"这个附件当前不可用（{state or '未知状态'}）：没有可交付的字节，"
                    "本次不声称看过它"
                )
            if not attachment_access.read_allowed(att.id):
                path = None
                unreadable = "这条附件没有通过本轮读取授权，本次没有读取任何内容"
            else:
                try:
                    path, _origin = _resolve_source_path(attachments, att)
                except Exception as exc:  # noqa: BLE001 - 读不到字节是要交付的事实，不是异常
                    path = None
                    if not unreadable:
                        unreadable = str(exc).strip() or f"读取附件字节失败：{type(exc).__name__}"
            if path is None and not unreadable:
                unreadable = "读不到这份附件的字节：没有可用的副本路径"
            seen.add(identifier)
            size = _int_bytes(att)
            if path is not None:
                try:
                    size = int(Path(path).stat().st_size) or size
                except OSError:
                    pass
            out.append(
                {
                    "attachmentId": identifier,
                    "card_id": identifier,
                    "name": name,
                    "kind": kind,
                    "path": Path(path) if path is not None else None,
                    "bytes": size,
                    "version": _attachment_version(att),
                    "unreadable_reason": unreadable,
                }
            )
    return out


def resolve_media(
    conn: sqlite3.Connection | None = None,
    *,
    state: dict | None = None,
    cards: Iterable[dict] | None = None,
    attachments: Any = None,
    candidates: Iterable[dict] | None = None,
    supports_images: bool = False,
    supports_audio: bool = False,
    supports_video: bool = False,
    audio_transcription: str | None = None,
    transcriber: Callable[..., dict] | None = None,
    work_dir: Path | str | None = None,
    max_frames: int = DEFAULT_MEDIA_SAMPLE_FRAMES,
    max_bytes: int = media.MAX_MEDIA_BYTES,
) -> dict:
    """本次可见范围里的**音频 / 视频**材料：诚实事实 + 真正能交付的内容块。

    能力协商（契约 §4.2）：这里的四个能力位**必须**来自本次真实使用的适配器声明
    （含凭据回落之后的那一个），调用方不许按「协议能接受什么」推测。

    返回 `{"entries": [...], "parts": [...], "work_dir": Path|None}`：

    * `entries`：每条音视频材料一条（`source` 取 §4.4 的八种之一），字段含
      `read` / `reason` / `range` / `frames` / `engine`；图片（文件卡的附件）的条目仍由
      `resolve_materials` 负责，这里不重复报；
    * `parts`：**真的交付**的内容块（图片帧 → `ImagePart`，音频字节 → `AudioPart`）；
      没有能力 / 读不到字节时是空列表 —— 调用方据此区分「交付了」与「没交付」；
    * `work_dir`：本轮抽样帧所在的目录（调用方用 `cleanup_media` 收尾）；
      一件都没派生时是 `None`（没有目录要清）。
    """
    service = attachments if attachments is not None else _derive_service(conn)
    if candidates is not None:
        items: list[Any] = list(candidates)
    else:
        items = [
            _card_media_candidate(card, service)
            for card in _iter_cards(state or {}, cards)
        ]
        items = [item for item in items if item is not None]
    max_frames_limit = int(max_frames) if int(max_frames) > 0 else 0
    frames_wanted = min(max_frames_limit, MAX_MEDIA_SAMPLE_FRAMES)
    notes_about_cap: list[str] = []
    if max_frames_limit > MAX_MEDIA_SAMPLE_FRAMES:
        notes_about_cap.append(
            f"请求抽样 {max_frames_limit} 帧，超上限 MAX_MEDIA_SAMPLE_FRAMES="
            f"{MAX_MEDIA_SAMPLE_FRAMES}：本次按上限取 {frames_wanted} 帧"
            "（抽样只代表这些时间点，不代表逐帧读完）"
        )
    limit_bytes = int(max_bytes) if int(max_bytes) > 0 else media.MAX_MEDIA_BYTES

    entries: list[dict] = []
    parts: list[Any] = []
    work_root = Path(work_dir) if work_dir is not None else None
    used_work: Path | None = None

    for item in items:
        candidate = dict(item)
        card_id = str(candidate.get("card_id") or candidate.get("attachmentId") or "")
        card = {"id": card_id, "kind": "file", "content": ""}
        name = str(candidate.get("name") or "")
        kind = str(candidate.get("kind") or classify_media_kind(name))
        entry, blocks, sampled_dir = _media_candidate_entry(
            card,
            candidate,
            kind=kind,
            shown=_label({"kind": "file", "meta": {"name": name}, "content": ""}) or name,
            supports_images=bool(supports_images),
            supports_audio=bool(supports_audio),
            supports_video=bool(supports_video),
            audio_transcription=audio_transcription,
            transcriber=transcriber,
            version=str(candidate.get("version") or ""),
            frames_wanted=frames_wanted,
            cap_notes=notes_about_cap,
            max_edge=MEDIA_SAMPLE_MAX_EDGE,
            limit_bytes=limit_bytes,
            work_dir=(work_root / _safe_dir_name(card)) if work_root is not None else None,
        )
        if sampled_dir is not None:
            used_work = sampled_dir
        entries.append(entry)
        parts.extend(blocks)

    return {"entries": entries, "parts": parts, "work_dir": used_work}


def _card_media_candidate(card: dict, service: Any) -> dict | None:
    """可见范围里的卡片 → 音视频候选（`resolve_media` 走附件服务的那条路）。"""
    meta = card.get("meta") if isinstance(card.get("meta"), dict) else {}
    name = _media_name(card, meta)
    kind = classify_media_kind(name)
    if not kind:
        return None
    att = _media_attachment(card, meta, service)
    if att is None:
        return {
            "attachmentId": "",
            "card_id": str(card.get("id") or ""),
            "name": name,
            "kind": kind,
            "path": None,
            "bytes": 0,
            "version": _content_version(card),
        }
    path, _origin = service_for_content(service, att) if service is not None else (None, "none")
    return {
        "attachmentId": att.id,
        "card_id": str(card.get("id") or ""),
        "name": str(getattr(att, "original_name", "") or name),
        "kind": kind,
        "path": Path(path) if path is not None else None,
        "bytes": _int_bytes(att),
        "version": _attachment_version(att),
    }


def _media_candidate_entry(
    card: dict,
    candidate: dict,
    *,
    kind: str,
    shown: str,
    supports_images: bool,
    supports_audio: bool,
    supports_video: bool,
    audio_transcription: str | None,
    transcriber: Callable[..., dict] | None,
    version: str,
    frames_wanted: int,
    cap_notes: list[str],
    max_edge: int,
    limit_bytes: int,
    work_dir: Path | None,
) -> tuple[dict, list[Any], Path | None]:
    """一条**候选**（已解析好路径）→ 材料条目 + 可交付内容块。"""
    path = candidate.get("path")
    path = Path(path) if path is not None else None
    bytes_ = _to_int_bytes(candidate.get("bytes"))
    attachment_id = str(candidate.get("attachmentId") or "") or None
    if path is None:
        # 候选带来的原因更具体（例如「引用本地文件的附件：QIO 没有保存副本」），优先用它。
        reason = str(candidate.get("unreadable_reason") or "") or (
            "这张材料还没有关联已保存的附件（meta 里没有 attachmentId）：读不到字节"
            if not attachment_id
            else f"读不到附件「{shown}」的字节：文件不在原来登记的位置（可以重新定位后再试）"
        )
        return (
            _media_entry(
                card,
                source={
                    "audio": "audio_unsupported",
                    "video": "video_unsupported",
                    "image": "image_unsupported",
                }.get(kind, "unreadable"),
                label=shown, version=version, read=False, bytes_=bytes_, reason=reason,
                notes=["没有可读的字节就没有交付通道：本次没有读到任何内容"],
                attachment_id=attachment_id,
            ),
            [],
            None,
        )

    # 媒体类型必须按**真实文件名**判断：shown 是展示标签（_snippet 只留 24 字符），
    # 长文件名会被截断成「…」，扩展名跟着丢 —— 图片会因此被判成「无法确认媒体类型」而
    # 不交付；音频/视频只是被通配类型兜底救了（2026-10-13 用真实附件实测发现）。
    media_name = str(candidate.get("name") or shown)
    if kind == "audio":
        entry, blocks = _audio_entry(
            card, path, bytes_=bytes_, attachment_id=attachment_id, shown=shown,
            media_name=media_name,
            supports_audio=bool(supports_audio), audio_transcription=audio_transcription,
            transcriber=transcriber, version=version, limit_bytes=limit_bytes,
            work_dir=work_dir,
        )
        return entry, blocks, None
    if kind == "image":
        return _image_candidate_entry(
            card, path, bytes_=bytes_, attachment_id=attachment_id, shown=shown,
            media_name=media_name,
            supports_images=bool(supports_images), version=version,
        )
    if kind == "video":
        return _video_entry(
            card, path, bytes_=bytes_, attachment_id=attachment_id, shown=shown,
            media_name=media_name,
            supports_images=bool(supports_images), supports_video=bool(supports_video),
            version=version, frames_wanted=frames_wanted, cap_notes=cap_notes,
            max_edge=max_edge, limit_bytes=limit_bytes, work_dir=work_dir,
        )
    # 不是音频/视频/图片却走到媒体通道：如实说不处理。
    # 历史反例：这里曾经无条件落到 _video_entry，于是「图片」会被当成视频去 ffmpeg 抽帧，
    # 回执写成 video_unsupported「没有抽到任何一帧」（2026-10-13 实测）。
    return (
        _media_entry(
            card, source="unreadable", label=shown, version=version, read=False, bytes_=bytes_,
            reason=f"「{shown}」不是音频 / 视频 / 图片：媒体通道不处理它（按其它读取路径的结论为准）",
            notes=["媒体通道只交付音视频与图片的真实字节，不猜测其它类型的内容"],
            attachment_id=attachment_id,
        ),
        [],
        None,
    )


def _to_int_bytes(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _media_name(card: dict, meta: dict) -> str:
    block = meta.get("file") if isinstance(meta.get("file"), dict) else {}
    return str(
        block.get("name") or meta.get("name") or block.get("title") or meta.get("title") or ""
    ).strip()


def _media_label(card: dict, att: Any, kind: str) -> str:
    name = str(getattr(att, "original_name", "") or "") if att is not None else ""
    return _label(card) or name or ("（音频）" if kind == "audio" else "（视频）")


def _media_attachment(card: dict, meta: dict, service: Any) -> Any:
    raw_id = _attachment_id(meta, "file")
    if not raw_id or service is None:
        return None
    return service.get(raw_id, check=False)


def _safe_dir_name(card: dict) -> str:
    raw = str(card.get("id") or "card")
    return "".join(ch for ch in raw if ch.isalnum() or ch in "-_") or "card"


def _audio_entry(
    card: dict,
    path: Path,
    *,
    bytes_: int,
    attachment_id: str | None,
    shown: str,
    media_name: str = "",
    supports_audio: bool,
    audio_transcription: str | None,
    transcriber: Callable[..., dict] | None,
    version: str,
    limit_bytes: int,
    work_dir: Path | None = None,
) -> tuple[dict, list[Any]]:
    """音频：交付字节 / 派生转写 / 如实说不可用（三选一，不许含糊）。"""
    duration: float | None = None
    probe: dict = media.probe_media(path, max_bytes=limit_bytes)
    duration = probe.get("duration")
    if not probe.get("hasAudio") and not probe.get("reason"):
        return (
            _media_entry(
                card,
                source="audio_unsupported",
                label=shown,
                version=version,
                read=False,
                bytes_=bytes_,
                reason="这份文件里没有音频轨道：没有可以转写的语音内容"
                "（画面不会被当成声音，也不推断画面里的对话）",
                notes=["只按真实轨道判断：文件里没有声音，就没有可读的音频事实"],
                attachment_id=attachment_id,
            ),
            [],
        )

    if supports_audio:
        if bytes_ <= 0 or bytes_ > limit_bytes:
            return (
                _media_entry(
                    card, source="audio_unsupported", label=shown, version=version, read=False,
                    bytes_=bytes_,
                    reason=f"音频 {human_size(bytes_)} 超过交付上限 {human_size(limit_bytes)}："
                    "本次没有把音频字节交给模型（不截取半段声音，因为半段会给出错的结论）",
                    notes=["要么压缩后再发，要么把结论写在注释里让 QIO 读文字"],
                    attachment_id=attachment_id,
                ),
                [],
            )
        media_type = _audio_media_type(media_name or shown)
        data_url = _file_data_url(path, media_type)
        if data_url is None:
            return (
                _media_entry(
                    card, source="audio_unsupported", label=shown, version=version, read=False,
                    bytes_=bytes_, reason="读不到这份音频的字节：本次没有把它交给模型",
                    notes=["清单失败也会走到这里：不伪造一个空音频块"],
                    attachment_id=attachment_id,
                ),
                [],
            )
        from agent.adapters.base import AudioPart

        return (
            _media_entry(
                card, source="audio_delivered", label=shown, version=version, read=True,
                bytes_=bytes_,
                reason="",
                notes=[
                    f"本次把这份音频的真实字节（{shown}，{human_size(bytes_)}）"
                    "作为音频内容块交给了模型，由模型自己听；QIO 不另外编造音频描述"
                ],
                attachment_id=attachment_id,
                frame_range={"start": None, "end": duration},
            ),
            [
                AudioPart(
                    data_url=data_url,
                    label=str(card.get("id") or ""),
                    media_type=media_type,
                    bytes=bytes_,
                )
            ],
        )

    if transcriber is not None:
        return _audio_transcribed_entry(
            card, path, probe=probe, duration=duration, transcriber=transcriber,
            bytes_=bytes_, attachment_id=attachment_id, shown=shown, version=version,
            channel=audio_transcription or "", limit_bytes=limit_bytes, work_dir=work_dir,
        )

    if audio_transcription:
        return (
            _media_entry(
                card, source="audio_unsupported", label=shown, version=version, read=False,
                bytes_=bytes_,
                reason=_transcription_blocked_reason(
                    probe=probe, duration=duration, channel=audio_transcription
                ),
                notes=["转写通道没跑成就不编造文字：本次没有得到任何转写文本"],
                attachment_id=attachment_id,
                engine={"provider": audio_transcription, "model": ""},
            ),
            [],
        )

    return (
        _media_entry(
            card, source="audio_unsupported", label=shown, version=version, read=False,
            bytes_=bytes_,
            reason=_no_audio_channel_reason(probe),
            notes=[
                "要读音频内容：给凭据打 transcription 用途标签（或让当前链路凭据本身可转写），"
                "或者把结论写在注释里让 QIO 读文字",
                "本机若没有可用的转换程序，转写所需的音频片段同样抽不出来",
            ],
            attachment_id=attachment_id,
        ),
        [],
    )


def _transcription_blocked_reason(
    *,
    probe: dict,
    duration: float | None,
    channel: str,
) -> str:
    """有转写通道但这次没转成：原因必须具体（缺哪一环就说哪一环）。"""
    if probe.get("reason"):
        return f"有转写通道（{channel}），但读不出媒体元数据：{probe['reason']}"
    ffmpeg, origin = media.ffmpeg_path()
    if not ffmpeg:
        return (
            f"有转写通道（{channel}），但音频抽取需要本机的转换程序：{origin}"
            "—— 没有它就拿不到转写所需的音频片段"
        )
    if duration is not None and duration > media.MAX_TRANSCRIBE_SECONDS:
        return (
            f"有转写通道（{channel}），但这份音频 {duration:g}s 超过转写时长上限 "
            f"MAX_TRANSCRIBE_SECONDS={media.MAX_TRANSCRIBE_SECONDS:g}s："
            "本次没有转写（不截一半就当成完整内容）"
        )
    return f"有转写通道（{channel}），但这次没有得到转写文本：不编造文字"


def _no_audio_channel_reason(probe: dict) -> str:
    """没有音频能力、也没有**解析到凭据的**转写通道：原因必须点名「转写凭据」。"""
    return (
        "当前模型链路既没有音频能力、也没有可用的转写通道"
        "（没有转写凭据：给凭据打 transcription 用途标签，或让当前链路凭据本身可转写）："
        "本次没有把音频交给模型，也没有得到任何转写文字"
        + (f"（另外：{probe['reason']}）" if probe.get("reason") else "")
    )


def _audio_transcribed_entry(
    card: dict,
    path: Path,
    *,
    probe: dict,
    duration: float | None,
    transcriber: Callable[..., dict],
    bytes_: int,
    attachment_id: str | None,
    shown: str,
    version: str,
    channel: str,
    limit_bytes: int,
    work_dir: Path | None,
) -> tuple[dict, list[Any]]:
    """真的走转写通道：抽出音频片段 → 交给端点 → **只回传真的拿到的文字**。

    失败路径分得很清楚：

    * 抽不出音频片段 / 读不出片段 → 说明缺的是哪一环（没有转换程序、读不到文件）；
    * 有凭据但调用失败 → 带上端点的真实原因；
    * 端点回了空文字 → 如实说「没有得到文字」。

    任何一条失败路径都**不写** ``transcript``（不编造文字）。
    """
    if bytes_ <= 0 or bytes_ > limit_bytes:
        return (
            _media_entry(
                card, source="audio_unsupported", label=shown, version=version, read=False,
                bytes_=bytes_,
                reason=f"音频 {human_size(bytes_)} 超过交付上限 {human_size(limit_bytes)}："
                "本次没有抽取音频片段，也没有转写（不截取半段声音，因为半段会给出错的结论）",
                notes=["要么压缩后再发，要么把结论写在注释里让 QIO 读文字"],
                attachment_id=attachment_id,
                engine={"provider": channel, "model": ""},
            ),
            [],
        )
    work = Path(work_dir) if work_dir is not None else _fallback_work_dir(card)
    fragment = work / "transcribe.wav"
    extracted = media.extract_audio(path, fragment, max_bytes=limit_bytes)
    if not extracted.get("path"):
        return (
            _media_entry(
                card, source="audio_unsupported", label=shown, version=version, read=False,
                bytes_=bytes_,
                reason="有转写凭据，但这台机器上没有可用的转换程序，抽不出转写所需的音频片段："
                + str(extracted.get("reason") or "没有说明"),
                notes=["转写失败就不编造文字：本次没有得到任何转写文本"],
                attachment_id=attachment_id,
                engine={"provider": channel, "model": ""},
            ),
            [],
        )
    try:
        raw = Path(extracted["path"]).read_bytes()
    except OSError as exc:
        return (
            _media_entry(
                card, source="audio_unsupported", label=shown, version=version, read=False,
                bytes_=bytes_,
                reason=f"读不出抽好的音频片段（{type(exc).__name__}）：本次没有调用转写端点，"
                "也没有得到任何转写文字",
                notes=["转写失败就不编造文字：本次没有得到任何转写文本"],
                attachment_id=attachment_id,
                engine={"provider": channel, "model": ""},
            ),
            [],
        )
    try:
        result = transcriber(
            raw, filename=fragment.name, duration=float(extracted.get("seconds") or 0.0)
        )
    except Exception as exc:  # noqa: BLE001 - 调用失败必须如实带原因，绝不编造文字
        return (
            _media_entry(
                card, source="audio_unsupported", label=shown, version=version, read=False,
                bytes_=bytes_,
                reason=f"有转写凭据，但这次调用没有成功：{redact_text(str(exc))}",
                notes=["转写失败就不编造文字：本次没有得到任何转写文本"],
                attachment_id=attachment_id,
                engine={"provider": channel, "model": ""},
            ),
            [],
        )

    transcript = str((result or {}).get("text") or "")
    engine = dict((result or {}).get("engine") or {})
    if not transcript.strip():
        return (
            _media_entry(
                card, source="audio_unsupported", label=shown, version=version, read=False,
                bytes_=bytes_,
                reason="转写通道这次没有给出任何文字：本次没有得到转写文本（不编造内容）",
                notes=["转写失败就不编造文字：本次没有得到任何转写文本"],
                attachment_id=attachment_id,
                engine=engine or {"provider": channel, "model": ""},
            ),
            [],
        )

    start, end, notes = _transcription_range(probe=probe, duration=duration, extracted=extracted)
    notes.append(
        f"转写文字只代表这次真的从音频里得到的文字（{shown}）："
        "不代表语气 / 音乐 / 全部环境声音都被读到了"
    )
    if extracted.get("reason"):
        notes.append(str(extracted["reason"]))
    return (
        _media_entry(
            card, source="audio_transcribed", label=shown, version=version, read=True,
            bytes_=bytes_, reason="",
            notes=notes,
            attachment_id=attachment_id,
            engine=engine,
            frame_range={"start": start, "end": end},
            transcript=transcript,
        ),
        [],
    )


def _transcription_range(
    *, probe: dict, duration: float | None, extracted: dict
) -> tuple[float | None, float | None, list[str]]:
    """转写覆盖的时段**如实**给出：超过上限时**不静默截断**，必须写明只转了前 N 秒。"""
    notes: list[str] = []
    seconds = float(extracted.get("seconds") or 0.0)
    start = 0.0 if seconds > 0 else None
    end = seconds if seconds > 0 else None
    cap = float(media.MAX_TRANSCRIBE_SECONDS)
    if duration is not None and duration > cap:
        notes.append(
            f"这份音频 {duration:g}s 超过转写时长上限 MAX_TRANSCRIBE_SECONDS={cap:g}s："
            f"本次只转写了前 {cap:g}s（后面的内容没有转写，不做静默截断）"
        )
        end = min(cap, seconds) if seconds > 0 else cap
    elif probe.get("reason"):
        notes.append(f"元数据读不全（{probe['reason']}）：时段按实际抽到的片段记，不猜")
    return start, end, notes


def transcriber_for_adapter(adapter: Any, *, model: str | None = None) -> Callable[..., dict] | None:
    """按**本次运行**解析出来的适配器能力，给出一个同步的转写调用器（没有就返回 None）。

    * ``adapter.audio_transcription`` 为空 → None：调用方据此如实降级成
      「没有转写凭据」，绝不静默换成用户没配置的外部服务；
    * 有通道 → 返回 ``transcriber(bytes, filename=..., duration=...) -> dict``，
      内部把适配器的 async 入口在**工作线程**里跑完（材料解析本来就在工作线程，
      那里没有事件循环在跑，asyncio.run 是安全的）。

    ``model`` 只是调用方拿到的凭据模型名（**不硬编码**）：适配器自己按路由里的模型发请求。
    """
    if not getattr(adapter, "audio_transcription", None):
        return None
    transcribe = getattr(adapter, "transcribe_audio", None)
    if not callable(transcribe):
        return None

    def _call(data: bytes, *, filename: str = "audio.wav", duration: float = 0.0) -> dict:
        import asyncio

        result = asyncio.run(transcribe(bytes(data), filename=str(filename or "audio.wav")))
        payload = dict(result or {})
        engine = dict(payload.get("engine") or {})
        if not engine.get("model") and model:
            # 适配器没报模型名时才用调用方拿到的凭据模型名补上（仍然不是硬编码）。
            engine["model"] = str(model)
        payload["engine"] = engine
        payload["duration"] = float(duration or 0.0)
        return payload

    return _call


def attach_transcripts(messages: list[Any], entries: Iterable[dict]) -> list[Any]:
    """把**真的拿到**的转写文字挂到本轮的模型请求上（契约 §4.4）。

    只处理 source == audio_transcribed 且 transcript 非空的条目：失败路径
    （audio_unsupported）一个字都不加 —— 不编造语音内容。文字放在首条 user 消息里，
    与「音频没有字节能力时走派生转写」这条口径一致。
    """
    texts: list[str] = []
    for entry in entries or []:
        if str(entry.get("source") or "") != "audio_transcribed":
            continue
        transcript = str(entry.get("transcript") or "").strip()
        if not transcript:
            continue
        label = str(entry.get("label") or "音频")
        engine = entry.get("engine") if isinstance(entry.get("engine"), dict) else {}
        provider = str((engine or {}).get("provider") or "")
        model = str((engine or {}).get("model") or "")
        who = "/".join([part for part in (provider, model) if part])
        range_ = entry.get("range") if isinstance(entry.get("range"), dict) else {}
        start = range_.get("start") if isinstance(range_, dict) else None
        end = range_.get("end") if isinstance(range_, dict) else None
        where = ""
        if start is not None or end is not None:
            head_start = start if start is not None else "开头"
            head_end = end if end is not None else "结尾"
            where = "（时段 " + str(head_start) + "–" + str(head_end) + " 秒）"
        head = "【音频转写】" + label + where
        if who:
            head += " · 转写引擎：" + who
        texts.append(head + chr(10) + transcript)
    if not texts:
        return messages
    block = (chr(10) + chr(10)).join(texts) + chr(10) + "（以上是转写通道真的返回的文字，只代表实际得到的语音内容）"
    for message in messages:
        if getattr(message, "role", "") == "user":
            content = getattr(message, "content", "") or ""
            message.content = (content + chr(10) + chr(10) + block) if content else block
            return messages
    return messages


def _image_candidate_entry(
    card: dict,
    path: Path,
    *,
    bytes_: int,
    attachment_id: str | None,
    shown: str,
    media_name: str = "",
    supports_images: bool,
    version: str,
    max_image_bytes: int = MAX_IMAGE_BYTES,
) -> tuple[dict, list[Any], Path | None]:
    """图片候选：按能力位交付**真实字节**，不能交付就如实说为什么。

    与音频 / 视频同一条规则：能力位是唯一事实来源 —— 没有视觉能力就 `image_unsupported`
    并点明「当前模型链路没有视觉能力」，绝不猜画面；超过上限也不截断（半张图会给出错的画面）。
    """
    media_type = _image_media_type(media_name or shown)
    if not media_type:
        return (
            _media_entry(
                card, source="image_unsupported", label=shown, version=version, read=False,
                bytes_=bytes_,
                reason=f"这种图片格式（{Path(shown).suffix or '无扩展名'}）不作为多模态图片交付："
                "当前只交付 png / jpg / jpeg / gif / webp；其余按既有读取结论处理",
                notes=["不把无法确认媒体类型的字节当图片发给模型"],
                attachment_id=attachment_id,
            ),
            [],
            None,
        )
    if not supports_images:
        return (
            _media_entry(
                card, source="image_unsupported", label=shown, version=version, read=False,
                bytes_=bytes_,
                reason="当前模型链路没有视觉能力：只登记图片元数据，不声称看懂图片内容",
                notes=["当前模型链路没有视觉能力：不读取图片像素、不编造画面描述"],
                attachment_id=attachment_id,
            ),
            [],
            None,
        )
    limit = int(max_image_bytes) if int(max_image_bytes) > 0 else MAX_IMAGE_BYTES
    if bytes_ <= 0 or bytes_ > limit:
        return (
            _media_entry(
                card, source="image_unsupported", label=shown, version=version, read=False,
                bytes_=bytes_,
                reason=f"图片 {human_size(bytes_)} 超过交付上限 {human_size(limit)}："
                "本次没有把像素交给模型（不截断成半张图，因为半张图会给出错的画面）",
                notes=["要么压缩后再发，要么把结论写在注释里让 QIO 读文字"],
                attachment_id=attachment_id,
            ),
            [],
            None,
        )
    data_url = _file_data_url(path, media_type)
    if data_url is None:
        return (
            _media_entry(
                card, source="image_unsupported", label=shown, version=version, read=False,
                bytes_=bytes_, reason="读不到这份图片的字节：本次没有把它交给模型",
                notes=["不伪造一个空图片块"],
                attachment_id=attachment_id,
            ),
            [],
            None,
        )
    from agent.adapters.base import ImagePart

    return (
        _media_entry(
            card, source="image_delivered", label=shown, version=version, read=True,
            bytes_=bytes_, reason="",
            notes=[
                f"本次把这张图片的真实字节（{shown}，{human_size(bytes_)}）作为多模态内容"
                "交给了模型，由模型自己看；QIO 不另外编造画面描述"
            ],
            attachment_id=attachment_id,
        ),
        [
            ImagePart(
                data_url=data_url,
                label=str(card.get("id") or ""),
                media_type=media_type,
                bytes=bytes_,
            )
        ],
        None,
    )


def _video_entry(
    card: dict,
    path: Path,
    *,
    bytes_: int,
    attachment_id: str | None,
    shown: str,
    media_name: str = "",
    supports_images: bool,
    supports_video: bool,
    version: str,
    frames_wanted: int,
    cap_notes: list[str],
    max_edge: int,
    limit_bytes: int,
    work_dir: Path | None,
) -> tuple[dict, list[Any], Path | None]:
    """视频：交付字节 / 抽样帧交付 / 如实说不可用（三选一，不许含糊）。"""
    probe = media.probe_media(path, max_bytes=limit_bytes)

    if supports_video:
        if bytes_ <= 0 or bytes_ > limit_bytes:
            return (
                _media_entry(
                    card, source="video_unsupported", label=shown, version=version, read=False,
                    bytes_=bytes_,
                    reason=f"视频 {human_size(bytes_)} 超过交付上限 "
                    f"{human_size(limit_bytes)}：本次没有把视频字节交给模型",
                    notes=["链路有视频能力，但这份字节超出交付边界"],
                    attachment_id=attachment_id,
                ),
                [],
                None,
            )
        from agent.adapters.base import VideoPart

        media_type = _video_media_type(media_name or shown)
        data_url = _file_data_url(path, media_type)
        if data_url is None:
            return (
                _media_entry(
                    card, source="video_unsupported", label=shown, version=version, read=False,
                    bytes_=bytes_, reason="读不到这份视频的字节：本次没有把它交给模型",
                    notes=["不伪造一个空视频块"],
                    attachment_id=attachment_id,
                ),
                [],
                None,
            )
        return (
            _media_entry(
                card, source="video_delivered", label=shown, version=version, read=True,
                bytes_=bytes_, reason="",
                notes=[
                    f"本次把这份视频的真实字节（{human_size(bytes_)}）作为视频内容块交给了模型"
                ],
                attachment_id=attachment_id,
                frame_range={"start": 0.0, "end": probe.get("duration")},
            ),
            [
                VideoPart(
                    data_url=data_url,
                    label=str(card.get("id") or ""),
                    media_type=media_type,
                    bytes=bytes_,
                )
            ],
            None,
        )

    # 没有视频字节能力 → 走抽样帧（抽帧是派生处理，必须说清用了谁）。
    if frames_wanted <= 0:
        return (
            _media_entry(
                card, source="video_unsupported", label=shown, version=version, read=False,
                bytes_=bytes_, reason="本次允许抽取的帧数是 0：没有可交付的画面依据",
                notes=list(cap_notes),
                attachment_id=attachment_id,
            ),
            [],
            None,
        )

    out_dir = (work_dir if work_dir is not None else _fallback_work_dir(card)) / "frames"
    sample = media.sample_video_frames(
        path, out_dir, count=frames_wanted, max_edge=max_edge, max_bytes=limit_bytes
    )
    notes = [*cap_notes, *(sample.reason.split("；") if sample.reason else [])]
    notes = [n for n in notes if n]
    frame_facts = [{"at": float(frame["at"])} for frame in sample.frames]
    frame_range = {
        "start": float(min((f["at"] for f in frame_facts), default=0.0)),
        "end": float(max((f["at"] for f in frame_facts), default=0.0)) or None,
    }
    engine = dict(sample.engine) if sample.engine else {}

    if not sample.frames:
        return (
            _media_entry(
                card, source="video_unsupported", label=shown, version=version, read=False,
                bytes_=bytes_, reason=notes[0] if notes else "没有抽到任何一帧",
                notes=notes[1:],
                attachment_id=attachment_id,
                engine=engine,
                frame_range={"start": None, "end": probe.get("duration")},
            ),
            [],
            None,
        )

    if not supports_images:
        return (
            _media_entry(
                card, source="video_unsupported", label=shown, version=version, read=False,
                bytes_=bytes_,
                reason="这份视频需要抽样帧交付，但当前模型链路没有图片能力："
                "本次没有把任何画面交给模型（不假装看过这段视频）",
                notes=[
                    f"本机转换程序抽到了 {len(sample.frames)} 帧（时间点："
                    + "、".join(f"{f['at']:g}s" for f in sample.frames)
                    + "），但链路交不出去，所以本次结论里没有画面依据",
                    *notes,
                ],
                attachment_id=attachment_id,
                engine=engine,
                frame_range=frame_range,
                frames=frame_facts,
            ),
            [],
            None,
        )

    from agent.adapters.base import ImagePart

    parts: list[Any] = []
    delivered_frames: list[dict] = []
    skipped: list[str] = []
    for frame in sample.frames:
        data_url = _file_data_url(Path(frame["path"]), "image/jpeg")
        if data_url is None or int(frame["bytes"]) > MAX_FRAME_BYTES:
            skipped.append(f"{float(frame['at']):g}s")
            continue
        parts.append(
            ImagePart(
                data_url=data_url,
                label=str(card.get("id") or ""),
                media_type="image/jpeg",
                bytes=int(frame["bytes"]),
            )
        )
        delivered_frames.append({"at": float(frame["at"])})
    if skipped:
        notes.append(
            f"有 {len(skipped)} 帧读不出字节或超过单帧上限，未交付：" + "、".join(skipped)
        )
    if not parts:
        return (
            _media_entry(
                card, source="video_unsupported", label=shown, version=version, read=False,
                bytes_=bytes_, reason="抽到了帧，但一帧都读不出字节：本次没有把画面交给模型",
                notes=notes,
                attachment_id=attachment_id,
                engine=engine,
            ),
            [],
            None,
        )

    times = "、".join(f"{frame['at']:g}s" for frame in delivered_frames)
    return (
        _media_entry(
            card, source="video_sampled", label=shown, version=version, read=True,
            bytes_=bytes_, reason="",
            notes=[
                f"本次按时间点抽取了 {len(parts)} 帧交付给模型（时间点：{times}）："
                "这不是逐帧读完整段视频，只代表这些时间点上的画面"
            ]
            + notes,
            attachment_id=attachment_id,
            engine=engine,
            frame_range=frame_range,
            frames=delivered_frames,
        ),
        parts,
        out_dir,
    )


def _media_entry(
    card: dict,
    *,
    source: str,
    label: str,
    version: str,
    read: bool,
    bytes_: int,
    reason: str,
    notes: list[str] | None = None,
    attachment_id: str | None = None,
    engine: dict | None = None,
    frame_range: dict | None = None,
    frames: list[dict] | None = None,
    transcript: str | None = None,
) -> dict:
    """音视频材料的统一回执：`read` / `reason` / `range` / `frames` / `engine` 都在。

    `transcript` **只有真的拿到转写文字时**才带值（`audio_transcribed`）；其余情况是 None。
    """
    return _entry(
        card,
        source=source,
        label=label,
        version=version,
        read=read,
        bytes=bytes_,
        characters=0,
        reason=reason,
        attachment_id=attachment_id,
        notes=notes or [],
        engine=dict(engine or {}),
        range=dict(frame_range or {"start": None, "end": None}),
        frames=list(frames or []),
        transcript=transcript,
    )


def _audio_media_type(name: str) -> str:
    return {
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".m4a": "audio/mp4",
        ".aac": "audio/aac",
        ".flac": "audio/flac",
        ".ogg": "audio/ogg",
        ".oga": "audio/ogg",
        ".opus": "audio/opus",
        ".wma": "audio/x-ms-wma",
        ".amr": "audio/amr",
        ".aiff": "audio/aiff",
    }.get(Path(str(name or "")).suffix.lower(), "audio/*")


def _video_media_type(name: str) -> str:
    return {
        ".mp4": "video/mp4",
        ".m4v": "video/mp4",
        ".mov": "video/quicktime",
        ".avi": "video/x-msvideo",
        ".mkv": "video/x-matroska",
        ".webm": "video/webm",
        ".wmv": "video/x-ms-wmv",
        ".flv": "video/x-flv",
        ".mpeg": "video/mpeg",
        ".mpg": "video/mpeg",
        ".ts": "video/mp2t",
        ".3gp": "video/3gpp",
    }.get(Path(str(name or "")).suffix.lower(), "video/*")


def _file_data_url(path: Path | None, media_type: str) -> str | None:
    """真实字节 → data URL；读不到就返回 None（绝不返回一个空壳）。"""
    if path is None:
        return None
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return None
    if not raw:
        return None
    return "data:" + media_type + ";base64," + base64.b64encode(raw).decode("ascii")


def _fallback_work_dir(card: dict) -> Path:
    """调用方没给本轮工作目录时的兜底：系统临时目录里的卡片专属目录。

    只用于「调用方确实没给目录」的情况；正常链路由调用方给本轮工作目录，
    并由它负责 `cleanup_media` 收尾。
    """
    import tempfile

    return Path(tempfile.mkdtemp(prefix=f"qio-media-{_safe_dir_name(card)}-"))


def _deliverable_image(card: dict, *, service: Any, max_bytes: int) -> dict | None:
    """这张卡的**图片附件**能不能真的交付字节；不能就返回 None（原因由材料条目如实说明）。

    只读 `meta.file`（旧 `meta.image` 回退已删除）：图片是文件卡的附件，没有独立 kind。
    """
    meta = card.get("meta") if isinstance(card.get("meta"), dict) else {}
    raw_id = _attachment_id(meta, "file")
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


def _resolve_source_path(service: Any, att: Any) -> tuple[Path | None, str]:
    """读取路径的唯一解析：新语义走 resolve_source，历史记录回退旧口径。

    `services/attachments.py` 的 resolve_source 是读取/预览/媒体解析的唯一入口；
    本函数据此返回 (path, origin)，读不到返回 (None, "")，不复制判断。
    """
    resolver = getattr(service, "resolve_source", None)
    if callable(resolver):
        try:
            resolved = resolver(att.id)
        except Exception:  # noqa: BLE001 - 解析失败按读不到处理
            resolved = None
        if resolved is not None and not getattr(resolved, "legacy", False):
            if resolved.path is None:
                return None, ""
            return Path(resolved.path), "source"
    try:
        target = service.content_target(att.id)
    except Exception:  # noqa: BLE001 - 历史记录没有副本/源文件
        return None, ""
    if not target:
        return None, ""
    path, origin = target
    return Path(path), str(origin)


def service_for_content(service: Any, att: Any) -> tuple[Path | None, str]:
    """这条附件真正可读的路径（源文件引用 / 历史副本回放），并服从读取授权。"""
    if not attachment_access.read_allowed(getattr(att, "id", "")):
        return None, ""
    return _resolve_source_path(service, att)


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
    if kind == "text":
        # 统一文字卡（契约 §2.1 / §2.2）：整卡许可为真时，卡内文字 / 链接 / 代码
        # 合并成一条 inline 材料；未许可的卡在上层可见性判断里已被拦住、根本不进列表。
        return _inline_entry(card, label, max_chars=max_chars)
    if kind == "file":
        return _file_entry(
            card, label, meta, service=service, tool=tool, max_chars=max_chars, vision=vision
        )
    # 图片没有独立 kind（它是文件卡的附件）：kind="file" 在上一支里已经按附件类型分流。
    # 遗留 code / url 没有材料通道：迁移完成前整体不可见（models.visible_except_deleted）。
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
    """统一文字卡：正文 = 块序列的纯文本（含链接地址与代码正文），整体一条 inline 材料。

    同一张卡的多个块**合并为一条**（标注「同一张文字卡的正文」），不拆成多条以免重复。
    """
    doc = text_card.doc_from_card(card)
    raw = text_card.blocks_to_text(doc)
    delivered, cut = _cut(raw, max_chars)
    read = bool(delivered)
    reason = "" if read else "卡片正文为空：这张材料没有可交付的文字"
    notes = ["同一张文字卡的正文（卡内文字、链接、代码都在这一条里，不拆成多条以免重复）"]
    blocks: list[str] = []
    for block in doc.get("blocks") or []:
        block_kind = str(block.get("kind") or "")
        attrs = block.get("attrs") if isinstance(block.get("attrs"), dict) else {}
        if block_kind == "code":
            language = str(attrs.get("language") or "").strip()
            blocks.append("代码" + (f"（{language}）" if language else ""))
        elif block_kind == "link":
            href = str(attrs.get("href") or "")
            blocks.append("链接" + (str(block.get("text") or "") or href))
    if blocks:
        notes.append("这张卡的正文包含：" + "、".join(blocks))
    if cut:
        notes.append(
            f"截断：已解析 {len(raw)} 字符，本次只交付前 {len(delivered)} 字符（上限 {max_chars}）"
        )
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
    vision: bool = False,
) -> dict:
    raw_id = _attachment_id(meta, "file")
    shown_name = label or str(meta.get("name") or "").strip() or "（未命名文件）"
    # 互动上下文必须暴露 §6.4 的引用事实 + 用户说明（见 _attach_file_ref）。
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
    entry = _attachment_entry(
        card, att, label=label or att.original_name, tool=tool, max_chars=max_chars,
        vision=vision,
    )
    entry = _attach_file_ref(entry, service, att)
    return entry


def _attach_file_ref(entry: dict, service: Any, att: Any) -> dict:
    """把 §6.4 源引用事实附加到互动材料条目（不改变正文与 read 语义）。"""
    try:
        if hasattr(service, "file_ref"):
            ref = service.file_ref(att, check=False)
        else:
            ref = {}
    except Exception:  # noqa: BLE001 - 引用事实是旁路，读不到不影响材料正文
        ref = {}
    entry = dict(entry or {})
    entry["attachment_id"] = str(getattr(att, "id", "") or "")
    entry["file_ref"] = ref
    entry["user_note"] = str(getattr(att, "user_note", "") or "")
    entry["material_enabled"] = bool(getattr(att, "material_enabled", 1))
    entry["origin"] = getattr(att, "origin", None)
    entry["source_kind"] = getattr(att, "source_kind", None)
    return entry


def _attachment_entry(
    card: dict,
    att: Attachment,
    *,
    label: str,
    tool: attachment_tools.ReadAttachmentTool | None,
    max_chars: int,
    vision: bool = False,
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
        # 图片走「文件」卡（扩展名是图片）：**只有这一份交付事实**，含 vision 判断。
        # 旧实现硬编码「当前模型链路没有视觉能力」且从不读字节 —— 链路其实有视觉能力时，
        # 那句话是假话，用户也永远拿不到画面（2026-10-13 复审指出）。
        return _image_fact(card, att, raw_id=att.id, label=label, vision=vision)
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
        elif mode == "pptx":
            content = attachment_tools._extract_pptx(att, path, 0, _MAX_UNITS, 0, None)
        elif mode == "pdf":
            # PDF 走**文字层**解析（不渲染版面）：加密 / 没有文字层 / 损坏各自给不同原因。
            content = attachment_tools._extract_pdf(att, path, 0, _MAX_UNITS, 0, None)
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


def _image_fact(
    card: dict,
    att: Attachment | None,
    *,
    raw_id: str | None,
    label: str,
    vision: bool,
    max_image_bytes: int = MAX_IMAGE_BYTES,
) -> dict:
    """图片附件的交付事实：**文件卡里的图片只有这一份判断与措辞**。

    拆出来是为了不再出现「同一个事实两套说法」：文件卡里的图片以前是硬编码的
    「当前模型链路没有视觉能力」——在链路其实有视觉能力时，那是一句假话（2026-10-13 复审指出）。
    """
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

    # 后面的三种原因都基于 raw_id / att（调用方已经决定要读哪份附件），不依赖调用方的其余入参。
    # 没有视觉能力时的**主原因**必须点明「视觉能力」；具体是哪一种拿不到字节写进 notes。
    # （这样既保持诚实口径可机械核对，又不会把「还没有附件」笼统说成「模型看不见」）
    if not raw_id:
        reason = "当前模型链路没有视觉能力：只登记图片元数据，不声称看懂图片内容"
        notes = [
            "这张卡片还没有关联已保存的附件（meta.file.attachmentId 缺失）："
            "没有可读的字节；文件名不是内容",
        ]
    elif att is None:
        reason = "当前模型链路没有视觉能力：只登记图片元数据，不声称看懂图片内容"
        notes = [f"登记的附件 id 当前取不到：{raw_id}（可能已被删除或不属于这台设备）"]
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
    if not attachment_access.read_allowed(getattr(att, "id", "")):
        return None, "none"
    if service is not None:
        resolved = getattr(service, "resolve_source", None)
        if callable(resolved):
            try:
                found = resolved(att.id)
                if found.path is not None and not getattr(found, "legacy", False):
                    return Path(found.path), "source"
            except Exception:  # noqa: BLE001 - 解析失败按读不到处理
                pass
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
    """从卡片 meta 的指定块里取已保存附件 id（文件卡就是 `meta.file.attachmentId`）。

    只认传进来的块名（旧 `meta.image` 回退已删除）：缺块时如实返回空串，
    由调用方给出「没有关联已保存的附件」的真实原因。
    """
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
    """材料语义版本：kind + 正文 + meta（**排除纯展示的 meta.display**）。

    Phase 2：图片显示比例（width/height/aspectRatio）只影响渲染，不改变材料语义；
    附件版本仍由 _attachment_version 参与（见文件卡条目），参与状态 / 关系 / 组成员
    分别在 card / link / group 语义里参与，不在这里冒充材料内容。
    """
    meta = card.get("meta") if isinstance(card.get("meta"), dict) else {}
    semantic_meta = {key: value for key, value in meta.items() if key != "display"}
    payload = json.dumps(
        {
            "kind": card.get("kind"),
            "content": str(card.get("content") or ""),
            "meta": semantic_meta,
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
    if kind == "file":
        block = meta.get("file") if isinstance(meta.get("file"), dict) else {}
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
    # 唯一口径就是 models.MATERIAL_SOURCES（音频/视频的六个取值已在那里）。
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
    # 转写文字只代表**实际得到的文字**（契约 §4.4）：没有就是 None，不写空壳。
    entry.setdefault("transcript", None)
    return entry
