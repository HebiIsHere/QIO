"""互动理解：受理 / 上下文构建 / 真实模型理解与结构化校验（子智能体 B 负责实现）。

阶段三退役说明（docs/qio-refactor-contract.md §13.3）：新提交流程**不再调用**本模块的
模型链。互动提交已改为主循环的一个 Turn（interactive/submission.py 注入 turn_acceptor），
由板面工具在同一个主循环里提出意图 / 提问 / 执行；本模块保留只为旧 board_understanding
记录的只读兼容、既有 API 读取与旧失败记录的重试入口，不再产生新记录。

契约：docs/interactive-mode-contract.md §14.0 / §14.1 / §14.3 / §14.5。

三个公开入口（冻结签名）：

* request_understanding(conn, *, board_id, submission_id, snapshot=None)
  只做**受理**：落一条 status=accepted 的理解请求（按 request_id 幂等），
  **不调用模型**，也尽量只做短事务写入。受理失败会抛错，让提交方按受理失败处理。
* build_context(conn, *, board_id, submission_id=None, understanding_id=None)
  只从**冻结的提交快照**构建上下文，不读当前板面。
* async run_understanding(conn, understanding_id, *, adapter_provider)
  材料 → 模型 → 结构化校验 → 落库；任何异常都收敛为 status=failed + 可读原因，
  **绝不向外抛**，也绝不让已经受理成功的提交被改判失败。

三条不许绕过的规则：

1. 模型不是权威：结构、对象引用、材料范围、操作类型、依赖与冲突、修改范围
   全部由程序复核；无效输出**不得**落成可执行任务，自动修复至多一次。
2. 材料口径必须诚实：拿到文件名 / 地址不等于读到正文，未读取、截断、不支持都写进
   limits / honesty，禁止声称已完整阅读。
3. 长期记忆只写适合后续合作的目标 / 结论 / 依据 / 成果，区分用户表达、模型推测、
   待审批建议与已完成事实；整块板面与未获准内容不写。所有文本先过 agent.trace.redact。
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, Awaitable, Callable

from agent.interactive import models, placement, prompts
from agent.interactive import submission as submission_module

NL = chr(10)

# --- 上下文与校验预算 -----------------------------------------------------

#: 一次理解允许送入模型的上下文总字符数（超出的材料正文会被截断并如实记录）。
MAX_CONTEXT_CHARS = 12000
#: 单条材料允许送入的正文上限。
MAX_MATERIAL_CHARS = 4000
#: 卡片正文进入上下文的上限。
MAX_CARD_TEXT_CHARS = 600
#: 结果卡（reply）正文进入上下文的上限（F4：结果卡的完整正文要能被后续任务引用）。
MAX_RESULT_TEXT_CHARS = 4000
#: 摘要 / 标题类字段的上限。
MAX_SHORT_CHARS = 200
#: 一次理解最多接受的意图数与每个意图的操作数（修改范围受限）。
MAX_INTENTS = 5
MAX_OPS_PER_INTENT = 12
#: 校验原因最多回传多少条（可读优先，不刷屏）。
MAX_VALIDATION_REASONS = 8
#: 长期记忆：单次理解最多写多少条、单条多少字。
MAX_MEMORY_ENTRIES = 8
MAX_MEMORY_CHARS = 400
#: 原始模型输出保留长度（审计用，超长截断）。
MAX_RAW_CHARS = 4000

#: 真正读到了正文的材料来源；其余来源都要在 honesty 里如实标注未读。
#: 「真的读到了内容」的来源。image_delivered：图片的字节作为多模态内容交付给了模型 ——
#: 内容不在上下文 JSON 里（它在图片块里），但**确实交付了**，所以不算「未读取」。
READABLE_SOURCES = ("inline", "attachment_text", "url_fetched", "image_delivered")

#: 理解阶段（前端统一过程区按这个顺序显示）。
STAGE_ORDER = ("materials", "model", "validation", "persist")
STAGE_LABELS = {
    "materials": "读取材料",
    "model": "理解提交内容",
    "validation": "校验输出",
    "persist": "落库与登记",
}

#: 长期记忆条目的来源语义：用户表达 / 模型推测 / 待审批建议 / 已完成事实。
MEMORY_PROVENANCE = ("user_statement", "model_inference", "pending_suggestion", "completed_fact")
#: 记忆类别 → 既有 knowledge 类别（复用 agent.knowledge 的分类，不新造一套）。
MEMORY_CATEGORY = {
    "goal": "goal",
    "conclusion": "general_fact",
    "basis": "general_fact",
    "outcome": "general_fact",
}

_JSON_COLUMNS = ("progress", "snapshot", "context", "materials", "output", "validation", "memory")


def _now() -> str:
    return models.now_iso()


def _clip(text: Any, limit: int) -> str:
    value = str(text or "")
    return value if len(value) <= limit else value[:limit] + "…"


def _safe_error(exc: BaseException) -> str:
    """错误文本一律过脱敏，绝不把密钥原文写进任何记录。"""
    from agent.trace import redact

    try:
        return redact.sanitize_error_text(str(exc)) or type(exc).__name__
    except Exception:  # noqa: BLE001 - 脱敏本身故障也不能让理解流程炸掉
        return type(exc).__name__


def _redact_text(text: Any) -> str:
    from agent.trace import redact

    try:
        return redact.redact_text(str(text or ""))
    except Exception:  # noqa: BLE001
        return str(text or "")


# --- 行读写 ---------------------------------------------------------------


def _load_row(conn: sqlite3.Connection, understanding_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM board_understanding WHERE id = ?", (understanding_id,)
    ).fetchone()


def _row_dict(row: sqlite3.Row) -> dict:
    data = dict(row)
    return {
        "id": data["id"],
        "boardId": data["board_id"],
        "submissionId": data.get("submission_id"),
        "requestId": data.get("request_id"),
        "status": data.get("status"),
        "stage": data.get("stage") or "",
        "progress": models.loads(data.get("progress"), {}),
        "materials": models.loads(data.get("materials"), []),
        "output": models.loads(data.get("output"), {}),
        "validation": models.loads(data.get("validation"), {}),
        "memory": models.loads(data.get("memory"), []),
        "error": data.get("error"),
        "attempt": int(data.get("attempt") or 1),
        "adapter": data.get("adapter") or "",
        "hasSnapshot": bool(models.loads(data.get("snapshot"), {})),
        "hasContext": bool(models.loads(data.get("context"), {})),
        "createdAt": data.get("created_at"),
        "updatedAt": data.get("updated_at"),
        "startedAt": data.get("started_at"),
        "finishedAt": data.get("finished_at"),
    }


def get_understanding(conn: sqlite3.Connection, understanding_id: str) -> dict | None:
    """读一条理解记录（只读，给接口层与测试用）。"""
    row = _load_row(conn, understanding_id)
    return _row_dict(row) if row is not None else None


def _update(conn: sqlite3.Connection, understanding_id: str, **fields: Any) -> None:
    fields["updated_at"] = _now()
    assignments: list[str] = []
    values: list[Any] = []
    for key, value in fields.items():
        assignments.append(key + " = ?")
        values.append(models.dumps(value) if key in _JSON_COLUMNS else value)
    values.append(understanding_id)
    conn.execute(
        "UPDATE board_understanding SET " + ", ".join(assignments) + " WHERE id = ?",
        values,
    )


def _set_stage(conn: sqlite3.Connection, understanding_id: str, stage: str, detail: str = "") -> None:
    """写阶段与进度：stage 是阶段键，progress 给前端统一过程区显示。"""
    index = STAGE_ORDER.index(stage) if stage in STAGE_ORDER else 0
    progress = {
        "index": index,
        "total": len(STAGE_ORDER),
        "stage": stage,
        "label": STAGE_LABELS.get(stage, stage),
        "detail": _clip(detail, MAX_SHORT_CHARS),
        "updatedAt": _now(),
    }
    _update(conn, understanding_id, stage=stage, progress=progress)


# --- 受理 -----------------------------------------------------------------


def request_id_for(board_id: str, submission_id: str) -> str:
    """受理身份：同一次提交的 request_id 是确定的，重复受理不会新建第二条。"""
    return "sub:" + str(board_id) + ":" + str(submission_id)


def _submission_row(conn: sqlite3.Connection, submission_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT id, board_id, before_state, after_state, expressions, delivery"
        " FROM board_submissions WHERE id = ?",
        (submission_id,),
    ).fetchone()


def _note_from_submission(row: sqlite3.Row) -> str:
    """从提交行里取用户备注。

    A 在受理时只传内存里的 after 投影（之后紧接着要清勾选，重读会污染快照），
    备注不在快照里；这里按 submission_id 从 board_submissions.delivery 补读
    （先认显式 note 字段，再认 detail 里的「用户备注：」）。
    """
    try:
        delivery = models.loads(row["delivery"], {})
    except (KeyError, IndexError):
        return ""
    if not isinstance(delivery, dict):
        return ""
    note = delivery.get("note")
    if isinstance(note, str) and note.strip():
        return _clip(note, MAX_SHORT_CHARS)
    detail = delivery.get("detail")
    if isinstance(detail, str) and "用户备注：" in detail:
        tail = detail.rsplit("用户备注：", 1)[1]
        tail = tail.split("；", 1)[0].strip()
        return _clip(tail, MAX_SHORT_CHARS)
    return ""


def _freeze_snapshot(
    snapshot: Any, *, expressions: Any = None, note: str = "", source: str = "caller"
) -> dict:
    """把提交内容固化成一份**不含未获准内容**的快照。

    复用 submission.project_snapshot（可见范围的唯一实现）：未勾选注释、隐藏卡片、
    一端不可见的链接都不会进入快照。冻结之后重试只读它，不再读当前板面。
    """
    raw = snapshot if isinstance(snapshot, dict) else {}
    projection = submission_module.project_snapshot(
        {
            "cards": [c for c in raw.get("cards", []) if isinstance(c, dict)],
            "groups": [g for g in raw.get("groups", []) if isinstance(g, dict)],
            "links": [l for l in raw.get("links", []) if isinstance(l, dict)],
            "selection": [s for s in raw.get("selection", []) if isinstance(s, str)],
        }
    )
    frozen = dict(projection)
    # F6：冻结视图（客户端可见区域）随快照固化为 null 或 {x,y,w,h,scale}；不编造。
    frozen["view"] = submission_module.normalize_view(raw.get("view"))
    if isinstance(expressions, list):
        frozen["expressions"] = [e for e in expressions if isinstance(e, dict)]
    else:
        frozen["expressions"] = []
    frozen["note"] = _clip(note, MAX_SHORT_CHARS)
    frozen["snapshotSource"] = str(source)
    frozen["frozenAt"] = _now()
    return frozen


def request_understanding(
    conn: sqlite3.Connection,
    *,
    board_id: str,
    submission_id: str,
    snapshot: dict | None = None,
) -> dict:
    """受理：建立一条 accepted 的理解请求（幂等，按 request_id）。

    **不调用模型**。写库失败会抛错，由提交方按受理失败处理（不更新基准）。
    """
    from agent.interactive import board_store

    info = board_store.ensure_board(conn, board_id=board_id)
    bid = info["id"]
    sid = str(submission_id or "").strip()
    if not sid:
        raise ValueError("受理失败：缺少 submissionId，无法建立理解请求。")
    rid = request_id_for(bid, sid)
    existing = conn.execute(
        "SELECT * FROM board_understanding WHERE request_id = ?", (rid,)
    ).fetchone()
    if existing is not None:
        result = _row_dict(existing)
        result["reused"] = True
        return result

    row = _submission_row(conn, sid)
    expressions = models.loads(row["expressions"], []) if row is not None else []
    if snapshot is None:
        after = models.loads(row["after_state"], {}) if row is not None else {}
        source = "submission" if row is not None else "missing"
        # F6：从保存的快照重建时，冻结视图从提交记录的投递事实里取回（缺失 → null）。
        if row is not None:
            try:
                delivery = models.loads(row["delivery"], {})
            except (KeyError, IndexError):
                delivery = {}
            after = {
                **(after if isinstance(after, dict) else {}),
                "view": (delivery or {}).get("view") if isinstance(delivery, dict) else None,
            }
    else:
        after = snapshot
        source = "caller"
    frozen = _freeze_snapshot(after, expressions=expressions, source=source)

    understanding_id = models.new_understanding_id()
    stamp = _now()
    try:
        conn.execute(
            "INSERT INTO board_understanding (id, board_id, submission_id, request_id, status,"
            " stage, progress, snapshot, context, materials, output, validation, memory, error,"
            " attempt, adapter, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                understanding_id,
                bid,
                sid,
                rid,
                "accepted",
                "",
                "{}",
                models.dumps(frozen),
                "{}",
                "[]",
                "{}",
                "{}",
                "[]",
                None,
                1,
                "",
                stamp,
                stamp,
            ),
        )
    except sqlite3.IntegrityError:
        existing = conn.execute(
            "SELECT * FROM board_understanding WHERE request_id = ?", (rid,)
        ).fetchone()
        if existing is None:
            raise RuntimeError("受理失败：理解请求写入冲突且无法读回已有记录。") from None
        result = _row_dict(existing)
        result["reused"] = True
        return result
    except sqlite3.Error as exc:
        raise RuntimeError(
            "受理失败：理解请求未能写入数据库（" + _safe_error(exc) + "）"
        ) from exc

    created = _load_row(conn, understanding_id)
    assert created is not None
    result = _row_dict(created)
    result["reused"] = False
    return result

# --- 材料 -----------------------------------------------------------------

#: 材料来源的诚实说明模板（拿到文件名 / 地址不等于读到正文）。
SOURCE_NOTES = {
    "url_address_only": "只拿到地址，没有读到网页正文",
    "image_unsupported": "是图片：当前模型链路没有视觉能力，内容未读取",
    "unreadable": "读取失败，内容未读取",
    "unsupported": "属于当前不支持的材料类型，内容未读取",
}


#: 材料模块的**静态可见**导入（打包契约，别改回纯动态导入）。
#:
#: `_materials_module()` 走 importlib：PyInstaller 的静态分析看不见这种动态导入。
#: 2026-10-12 的安装包实测：qio-backend.exe 里 agent.interactive.board_store /
#: submission / understanding / execution 都在，唯独没有 agent.interactive.materials
#: ——于是每次理解都返回 materials=[] 与
#: honesty.materialsError="材料解析模块不可用：No module named 'agent.interactive.materials'"
#: （用户库 D:\QIO-data\app.db 的 board_understanding.context.honesty 里有原话）。
#: 修复分两处：这里保留一条**字面 import 语句**（静态分析能看见，冻结产物就会带上
#: 这个模块 + 它的依赖），以及构建侧的 --hidden-import（scripts/build_sidecar.ps1、
#: backend/qio-backend.spec）。
#:
#: 这一行只为打包器而存在：运行期仍然走 _materials_module()（按 sys.modules 取，
#: 单元测试可以替换成假模块），所以对外行为与材料错误的诚实语义完全不变。
#: 导入失败也只是「材料不可用」，绝不让理解模块自己起不来。
try:  # pragma: no cover - 只有材料模块缺失 / 导入失败时才会走到 except
    import agent.interactive.materials as _materials_static  # noqa: F401
except ImportError:  # pragma: no cover - 漏包时如实降级到「材料解析模块不可用」
    _materials_static = None


def _materials_module():
    import importlib

    return importlib.import_module("agent.interactive.materials")


def _normalize_material(item: Any, index: int) -> dict:
    data = dict(item) if isinstance(item, dict) else {"text": str(item or "")}
    card_id = data.get("cardId") or data.get("card_id")
    if card_id is None and isinstance(data.get("id"), str):
        card_id = data.get("id")
    identifier = data.get("id") or card_id or ("m" + str(index))
    # 材料来源**只归一化、不静默降级**（契约 §4.4）：音频 / 视频等诚实事实会持续追加到
    # models.MATERIAL_SOURCES；把「暂时不在清单里」的来源悄悄抹成 unsupported，
    # 等于把「抽样 / 转写 / 未支持」的真实区别丢掉。只有来源确实缺失或不是字符串时
    # 才降级为 unsupported，并如实记下原因。
    raw_source = data.get("source")
    source = ""
    source_note = ""
    if isinstance(raw_source, str) and raw_source.strip():
        source = raw_source.strip()
        if source not in models.MATERIAL_SOURCES:
            source_note = f"材料来源 {source} 不在当前已知清单里：按原值保留，不静默降级"
    else:
        source = "unsupported"
        source_note = "材料来源缺失或不是字符串：已如实记为 unsupported（不猜来源）"
    text = data.get("text")
    if text is None:
        text = data.get("content")
    text = str(text or "")
    return {
        "id": str(identifier),
        "cardId": str(card_id) if card_id else None,
        "kind": data.get("kind"),
        "name": _clip(data.get("name") or data.get("title"), MAX_SHORT_CHARS),
        "source": source,
        "text": text,
        "chars": int(data.get("chars") or len(text)),
        "truncated": bool(data.get("truncated")),
        "reason": _material_reason(data, source_note),
    }


def _material_reason(data: dict, note: str) -> Any:
    """材料原因：原样保留原原因，需要说明来源归一化时如实追加，绝不覆盖真实原因。"""
    original = data.get("reason") or data.get("error")
    if not note:
        return original
    return f"{original}；{note}" if original else note


def _resolve_materials(
    conn: sqlite3.Connection, state: dict, *, vision: bool = False
) -> tuple[list[dict], dict]:
    """调用 A 的 materials.resolve_materials（冻结签名）。

    材料解析不可用（模块缺失 / 调用失败）时不丢上下文：返回空材料并如实记录原因。
    """
    try:
        module = _materials_module()
        resolve = getattr(module, "resolve_materials")
    except Exception as exc:  # noqa: BLE001 - 模块还没就绪也不能让上下文构建崩溃
        return [], {"ok": False, "reason": "材料解析模块不可用：" + _safe_error(exc)}
    try:
        try:
            raw = resolve(
                conn,
                state=state,
                cards=list(state.get("cards") or []),
                max_chars=MAX_MATERIAL_CHARS,
                vision=bool(vision),
            )
        except TypeError:
            # 老版材料模块没有 vision 形参：退回既有签名（图片仍然是 image_unsupported）
            raw = resolve(conn, state=state, cards=list(state.get("cards") or []))
    except Exception as exc:  # noqa: BLE001
        return [], {"ok": False, "reason": "材料解析失败：" + _safe_error(exc)}
    materials = [_normalize_material(item, index) for index, item in enumerate(list(raw or []))]
    return materials, {"ok": True, "reason": ""}


def _resolve_deliverable_images(conn: sqlite3.Connection, state: dict) -> list[dict]:
    """本次能真的交给视觉模型的图片（材料模块的唯一实现；不可用就如实返回空）。

    与材料解析共用同一份可见范围过滤：未勾选的注释、隐藏、已删除的卡片都不会被读。
    """
    try:
        module = _materials_module()
    except Exception:  # noqa: BLE001 - 模块不可用时不猜，返回空并保持材料的诚实结论
        return []
    resolve = getattr(module, "resolve_images", None)
    if not callable(resolve):
        return []
    try:
        raw = resolve(
            conn,
            state=state,
            cards=list(state.get("cards") or []),
        )
    except Exception:  # noqa: BLE001 - 单次读图失败不该让整次理解失败
        return []
    out: list[dict] = []
    for item in raw or []:
        if not isinstance(item, dict) or not item.get("dataUrl"):
            continue
        out.append(
            {
                "cardId": item.get("cardId"),
                "attachmentId": item.get("attachmentId"),
                "name": item.get("name"),
                "bytes": item.get("bytes"),
                "mediaType": item.get("mediaType"),
                "label": item.get("label"),
                "dataUrl": item.get("dataUrl"),
            }
        )
    return out


def _geo_num(value: Any, fallback: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    if number != number or number in (float("inf"), float("-inf")):
        return fallback
    return number


def _card_label(card: dict) -> str:
    try:
        return submission_module.card_label(card)
    except Exception:  # noqa: BLE001
        return _clip(card.get("content"), 24)


def _source_note(source: str, label: str, reason: Any) -> str:
    head = "材料 " + label + "：" + SOURCE_NOTES.get(source, "内容未完整读取")
    if reason:
        head += "（" + _clip(_redact_text(reason), 120) + "）"
    return head


# --- 上下文构建 -----------------------------------------------------------


def build_context(
    conn: sqlite3.Connection,
    *,
    board_id: str,
    submission_id: str | None = None,
    understanding_id: str | None = None,
    vision: bool = False,
) -> dict:
    """从**冻结的提交快照**构建互动上下文；不读当前板面。

    上下文包含用户要求 / 关系 / 有效变化 / 关注范围 / 材料；长度受控，截断与未读取范围
    记录在 limits / honesty 里，不允许任何地方声称已完整阅读。
    """
    row: sqlite3.Row | None = None
    if understanding_id:
        row = _load_row(conn, understanding_id)
        if row is None:
            raise ValueError("理解请求不存在，无法构建上下文：" + str(understanding_id))
    elif submission_id:
        row = conn.execute(
            "SELECT * FROM board_understanding WHERE board_id = ? AND submission_id = ?"
            " ORDER BY created_at DESC LIMIT 1",
            (board_id, submission_id),
        ).fetchone()
    effective_submission_id = str(
        (row["submission_id"] if row is not None else "") or submission_id or ""
    ).strip()
    frozen = models.loads(row["snapshot"], {}) if row is not None else {}
    if not isinstance(frozen, dict) or not frozen:
        frozen = {}
        if effective_submission_id:
            sub = _submission_row(conn, effective_submission_id)
            if sub is not None:
                frozen = _freeze_snapshot(
                    models.loads(sub["after_state"], {}),
                    expressions=models.loads(sub["expressions"], []),
                    note=_note_from_submission(sub),
                    source="submission",
                )
    # 「有效变化 / 用户备注」按集成口径补齐：A 受理时只传内存里的 after 投影，
    # 快照里没有 expressions / note（提交行是受理之后才落库的），这里按 submission_id
    # 从 board_submissions 读该次提交的冻结记录补齐 —— 仍然不读当前板面。
    expressions = [e for e in (frozen.get("expressions") or []) if isinstance(e, dict)]
    note_text_raw = str(frozen.get("note") or "")
    expression_source = "snapshot" if expressions else "none"
    if effective_submission_id and (not expressions or not note_text_raw):
        sub_row = _submission_row(conn, effective_submission_id)
        if sub_row is not None:
            if not expressions:
                loaded = models.loads(sub_row["expressions"], [])
                if isinstance(loaded, list):
                    expressions = [e for e in loaded if isinstance(e, dict)]
                    if expressions:
                        expression_source = "submission"
            if not note_text_raw:
                note_text_raw = _note_from_submission(sub_row)
    state_like = {
        "cards": [c for c in frozen.get("cards", []) if isinstance(c, dict)],
        "groups": [g for g in frozen.get("groups", []) if isinstance(g, dict)],
        "links": [l for l in frozen.get("links", []) if isinstance(l, dict)],
        "selection": [s for s in frozen.get("selection", []) if isinstance(s, str)],
    }
    # 唯一可见范围实现：未勾选注释、隐藏卡片、reply 卡片都不进上下文。
    visible_cards = models.selectable_cards(state_like)
    by_id: dict[str, dict] = {}
    visible_ids: list[str] = []
    for card in visible_cards:
        cid = str(card.get("id") or "")
        if cid:
            by_id[cid] = card
            visible_ids.append(cid)
    visible_id_set = set(visible_ids)

    groups: list[dict] = []
    for group in state_like["groups"]:
        if group.get("deleted"):
            continue
        members = [str(m) for m in (group.get("members") or []) if str(m) in visible_id_set]
        if not members:
            continue
        groups.append({
            "id": str(group.get("id") or ""),
            "name": str(group.get("name") or ""),
            "ordered": bool(group.get("ordered")),
            "members": members,
        })

    relationships: list[dict] = []
    for link in state_like["links"]:
        if link.get("deleted"):
            continue
        src = str(link.get("src") or "")
        dst = str(link.get("dst") or "")
        if src not in visible_id_set or dst not in visible_id_set:
            continue
        relationships.append({
            "id": str(link.get("id") or ""),
            "src": src,
            "dst": dst,
            "direction": bool(link.get("direction")),
            "meaning": _clip(link.get("meaning"), MAX_SHORT_CHARS),
            "srcLabel": _card_label(by_id.get(src, {})),
            "dstLabel": _card_label(by_id.get(dst, {})),
        })

    selection = [str(s) for s in state_like["selection"] if str(s) in visible_id_set]

    cards: list[dict] = []
    group_of: dict[str, str] = {}
    for group in groups:
        for member in group["members"]:
            group_of.setdefault(member, group["id"])
    for cid in visible_ids:
        card = by_id[cid]
        text = str(card.get("content") or "")
        # F4：可见结果卡交付**完整正文**（上限按结果正文口径，仍是诚实截断而不是省略），
        # 并带上来源（meta.reply 的 intentId / understandingId / executionId）与 updatedAt。
        is_result = card.get("kind") == models.REPLY_KIND
        limit = MAX_RESULT_TEXT_CHARS if is_result else MAX_CARD_TEXT_CHARS
        entry = {
            "id": cid,
            "kind": card.get("kind"),
            "label": _card_label(card),
            "text": _clip(text, limit),
            "textTruncated": len(text) > limit,
            "material": card.get("kind") in models.MATERIAL_KINDS,
            "group": group_of.get(cid),
            "updatedAt": card.get("updatedAt"),
        }
        if is_result:
            entry["result"] = True
            meta = card.get("meta") if isinstance(card.get("meta"), dict) else {}
            reply = meta.get("reply") if isinstance(meta.get("reply"), dict) else {}
            entry["source"] = {
                key: reply[key]
                for key in ("intentId", "understandingId", "executionId", "stepIndex")
                if reply.get(key) is not None
            }
        cards.append(entry)

    changes: list[dict] = []
    for expr in expressions:
        change = {
            "id": str(expr.get("id") or ""),
            "kind": str(expr.get("kind") or ""),
            "intentBearing": bool(expr.get("intentBearing")),
            "summary": _clip(expr.get("summary"), MAX_SHORT_CHARS),
            "cardIds": [str(c) for c in (expr.get("cardIds") or []) if str(c) in visible_id_set],
            "groupId": expr.get("groupId"),
            "linkId": expr.get("linkId"),
        }
        # F4：结果新增 / 变化的表达载荷带正文与来源，不能只剩编号或任务标题。
        if expr.get("kind") in ("result_added", "result_updated"):
            change["content"] = _clip(expr.get("content"), MAX_RESULT_TEXT_CHARS)
            source = expr.get("source")
            change["source"] = dict(source) if isinstance(source, dict) else {}
            if expr.get("updatedAt"):
                change["updatedAt"] = str(expr.get("updatedAt"))
        changes.append(change)

    note_ids: list[str] = []
    for expr in expressions:
        if expr.get("kind") not in ("note_added", "note_edited"):
            continue
        for cid in expr.get("cardIds") or []:
            cid = str(cid)
            if cid in visible_id_set and cid not in note_ids:
                note_ids.append(cid)
    note_texts = []
    for cid in note_ids:
        text = str(by_id.get(cid, {}).get("content") or "").strip()
        if text:
            note_texts.append(text)
    requirement = {
        "text": _clip(NL.join(note_texts), MAX_MATERIAL_CHARS),
        "cardIds": note_ids,
        "source": (
            "submission_notes"
            if note_texts
            else ("submission_changes" if expressions else "none")
        ),
        "note": _clip(note_text_raw, MAX_SHORT_CHARS),
        "expressionSource": expression_source,
        "intentBearing": any(bool(e.get("intentBearing")) for e in expressions),
    }

    materials, material_status = _resolve_materials(conn, state_like, vision=bool(vision))
    # 视觉链路可用时，把**这次真的能交付**的图片字节读出来（同一份可见范围、同一份材料模块）。
    # 读进上下文一起落库：事后能从保存的上下文重建「这次到底交付了哪些图片」。
    image_parts: list[dict] = []
    if vision:
        image_parts = _resolve_deliverable_images(conn, state_like)
    material_ids: list[str] = []
    for material in materials:
        for key in ("cardId", "id"):
            value = material.get(key)
            if value and str(value) not in material_ids:
                material_ids.append(str(value))

    # F6：几何上下文 —— **只**覆盖本次获准范围内的对象；未许可对象的身份与几何都不出现。
    visible_group_ids = {g["id"] for g in groups if g["id"]}
    geometry = {
        "unit": "board",
        "unitNote": (
            "板面坐标（board units）：原点在板面左上角，x 向右、y 向下；"
            "与卡片外框使用同一坐标系。"
        ),
        # 客户端冻结的可见区域（含 scale）；客户端没给 → null（不编造）。
        "view": submission_module.normalize_view(
            frozen.get("view") if isinstance(frozen, dict) else None
        ),
        "cards": [
            {
                "id": cid,
                "x": _geo_num(by_id[cid].get("x")),
                "y": _geo_num(by_id[cid].get("y")),
                "w": _geo_num(by_id[cid].get("w")),
                "h": _geo_num(by_id[cid].get("h")),
            }
            for cid in visible_ids
        ],
        "groups": [
            {
                "id": str(group.get("id") or ""),
                "x": _geo_num(group.get("x")),
                "y": _geo_num(group.get("y")),
                "w": _geo_num(group.get("w")),
                "h": _geo_num(group.get("h")),
            }
            for group in state_like["groups"]
            if str(group.get("id") or "") in visible_group_ids
        ],
    }

    context = {
        "contextVersion": 1,
        "boardId": board_id,
        "submissionId": effective_submission_id or None,
        "understandingId": row["id"] if row is not None else understanding_id,
        "requirement": requirement,
        "relationships": relationships,
        "changes": changes,
        "geometry": geometry,
        "focus": {
            "cardIds": selection,
            "labels": [_card_label(by_id.get(cid, {})) for cid in selection],
        },
        "cards": cards,
        "groups": groups,
        "materials": materials,
        "imageParts": image_parts,
        "visibleCardIds": visible_ids,
        "visibleGroupIds": [g["id"] for g in groups if g["id"]],
        "materialIds": material_ids,
        "honesty": {
            "materialsResolved": bool(material_status.get("ok")),
            "materialsError": material_status.get("reason") or "",
            "expressionsSource": expression_source,
            "snapshotSource": str(
                frozen.get("snapshotSource") or ("understanding" if row is not None else "missing")
            ),
            "notes": [],
        },
    }
    _apply_limits(context)
    return context


def image_parts_from_context(context: dict) -> list[Any]:
    """这次真正交付给模型看的图片内容块（`ImagePart`）。

    事实来源只有一个：`build_context` 在**有数据库连接**的时候、按同一份可见范围
    把「已交付」的图片字节读出来，写进 `context["imageParts"]`（随上下文一起落库，
    所以事后可从保存的上下文重建这次的交付事实）。这里只做读取与形状转换，不重新判断；
    上下文里没有（老记录 / 材料模块不可用）就如实返回空。
    """
    raw = context.get("imageParts")
    if not isinstance(raw, list) or not raw:
        return []
    from agent.adapters.base import ImagePart

    parts: list[Any] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        data_url = str(item.get("dataUrl") or "")
        if not data_url:
            continue
        parts.append(
            ImagePart(
                data_url=data_url,
                label=str(item.get("label") or ""),
                media_type=str(item.get("mediaType") or ""),
                bytes=int(item.get("bytes") or 0),
            )
        )
    return parts


def _apply_limits(context: dict) -> None:
    """控制上下文长度：截断材料正文，并如实记录截断与未读取范围。"""
    materials = [m for m in (context.get("materials") or []) if isinstance(m, dict)]
    unread_ranges: list[dict] = []
    not_provided: list[str] = []
    base = dict(context)
    base["materials"] = []
    base["limits"] = {"maxChars": MAX_CONTEXT_CHARS, "usedChars": 0, "truncated": False}
    fixed_chars = len(json.dumps(base, ensure_ascii=False, separators=(",", ":")))
    remaining = max(0, MAX_CONTEXT_CHARS - fixed_chars)
    truncated_any = False
    for material in materials:
        text = str(material.get("text") or "")
        budget = min(MAX_MATERIAL_CHARS, remaining)
        if len(text) > budget:
            kept = text[:budget] if budget > 0 else ""
            material["chars"] = len(text)
            material["providedChars"] = len(kept)
            material["truncated"] = True
            truncated_any = True
            if budget <= 0:
                not_provided.append(material.get("id"))
            else:
                unread_ranges.append({
                    "id": material.get("id"),
                    "from": len(kept),
                    "to": len(text),
                    "reason": "上下文长度预算用尽，这一段正文未读取",
                })
            material["text"] = kept
        remaining = max(0, remaining - len(str(material.get("text") or "")))

    honesty = dict(context.get("honesty") or {})
    notes = list(honesty.get("notes") or [])
    unread_sources: dict[str, int] = {}
    for material in materials:
        source = str(material.get("source") or "unsupported")
        label = str(material.get("name") or material.get("cardId") or material.get("id") or "")
        if source not in READABLE_SOURCES:
            unread_sources[source] = unread_sources.get(source, 0) + 1
            notes.append(_source_note(source, label, material.get("reason")))
        elif material.get("truncated"):
            notes.append(
                "材料 "
                + label
                + " 只提供了前 "
                + str(material.get("providedChars"))
                + " 字，其余未读取"
            )
    if not_provided:
        notes.append(
            "以下材料的正文完全没有送入模型（预算用尽）："
            + "、".join(str(x) for x in not_provided)
        )
    complete = (not truncated_any) and (not not_provided) and (not unread_sources)
    honesty.update({
        "complete": complete,
        "claim": (
            "本上下文的材料已完整读取"
            if complete
            else "本上下文的材料**未**完整读取：来源与截断见 notes 与 limits"
        ),
        "unreadSources": unread_sources,
        "notes": notes,
    })
    context["materials"] = materials
    context["honesty"] = honesty
    limits = {
        "maxChars": MAX_CONTEXT_CHARS,
        "usedChars": 0,
        "truncated": truncated_any,
        "unreadRanges": unread_ranges,
        "notProvided": not_provided,
    }
    context["limits"] = limits
    context["limits"]["usedChars"] = len(
        json.dumps(context, ensure_ascii=False, separators=(",", ":"))
    )

# --- 结构化输出校验（程序复核，不是信任模型） ------------------------------


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _invalid(reasons: list[str]) -> dict:
    return {
        "ok": False,
        "reasons": [str(r) for r in reasons][:MAX_VALIDATION_REASONS],
        "output": None,
    }


#: F5 引用符号语法：card:<symbol> / group:<symbol>；裸符号按卡片引用（向后兼容）。
REF_SYMBOL_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,31}\Z")
REF_PREFIXES = ("card", "group")


def parse_ref(value: Any) -> tuple[str, str]:
    """解析引用：返回 (类型, 符号)。类型是 "card" / "group"；裸符号按卡片处理。

    形状不合法（非字符串 / 空 / 未知前缀）时类型返回 ""，调用方按「不是合法引用」处理。
    """
    if not isinstance(value, str):
        return "", ""
    text = value.strip()
    if not text:
        return "", ""
    if ":" in text:
        prefix, symbol = text.split(":", 1)
        if prefix not in REF_PREFIXES:
            return "", ""
        return prefix, symbol
    return "card", text


def _check_card_ref(
    value: Any,
    visible: set[str],
    earlier: dict[str, str],
    all_declared: dict[str, str],
    reasons: list[str],
    where: str,
) -> str | None:
    """校验一个卡片引用：既有可见对象，或**更早**操作声明的、类型正确的符号。"""
    if not isinstance(value, str) or not value.strip():
        reasons.append(where + "：缺少卡片引用")
        return None
    text = str(value).strip()
    # 1) 已存在的对象仍按当前获准范围（visibleCardIds）校验（F5）。
    if text in visible:
        return text
    prefix, symbol = parse_ref(text)
    if prefix == "group":
        reasons.append(where + "：这里需要卡片引用，收到的是组引用 " + text)
        return None
    if prefix != "card" or not REF_SYMBOL_RE.match(symbol):
        reasons.append(where + "：引用了不在本次可见范围内的对象 " + text)
        return None
    if symbol in earlier:
        if earlier[symbol] != "card":
            reasons.append(where + "：符号 " + symbol + " 已声明为组，类型不匹配")
            return None
        return text
    if symbol in all_declared:
        reasons.append(
            where + "：符号 " + symbol + " 在更晚的操作里才声明（前向引用 / 自引用不允许）"
        )
        return None
    reasons.append(
        where
        + "：引用了不在本次可见范围内的对象 "
        + text
        + "（既没有在本意图更早的操作里声明，也不在可见范围内）"
    )
    return None


def _check_group_ref(
    value: Any,
    group_members: dict[str, list[str]],
    earlier: dict[str, str],
    all_declared: dict[str, str],
    reasons: list[str],
    where: str,
) -> str | None:
    """校验一个组引用：本次可见的组，或**更早**操作声明的组符号。"""
    if not isinstance(value, str) or not value.strip():
        reasons.append(where + "：缺少组引用")
        return None
    text = str(value).strip()
    if text in group_members:
        return text
    prefix, symbol = parse_ref(text)
    if prefix == "card":
        reasons.append(where + "：这里需要组引用，收到的是卡片引用 " + text)
        return None
    if prefix != "group" or not REF_SYMBOL_RE.match(symbol):
        reasons.append(where + "：引用了不在本次可见范围内的组 " + text)
        return None
    if symbol in earlier:
        if earlier[symbol] != "group":
            reasons.append(where + "：符号 " + symbol + " 已声明为卡片，类型不匹配")
            return None
        return text
    if symbol in all_declared:
        reasons.append(
            where + "：符号 " + symbol + " 在更晚的操作里才声明（前向引用 / 自引用不允许）"
        )
        return None
    reasons.append(
        where
        + "：引用了不在本次可见范围内的组 "
        + text
        + "（既没有在本意图更早的操作里声明，也不在可见范围内）"
    )
    return None


def _check_card_id(value: Any, visible: set[str], reasons: list[str], where: str) -> bool:
    if not isinstance(value, str) or not value.strip():
        reasons.append(where + "：缺少卡片 id")
        return False
    if value not in visible:
        reasons.append(where + "：引用了不在本次可见范围内的对象 " + value)
        return False
    return True


def _validate_key_list(value: Any, where: str, reasons: list[str]) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        reasons.append(where + " 必须是字符串数组")
        return []
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            reasons.append(where + " 里出现了空引用")
            continue
        out.append(item.strip())
    return out


def _declared_symbol(
    kind: str, raw_ref: Any, where: str, reasons: list[str]
) -> tuple[str, str]:
    """校验 `ref` 声明：返回 (符号, 类型)。不合法返回 ("","") 并记原因。"""
    if raw_ref is None:
        return "", ""
    if not isinstance(raw_ref, str) or not REF_SYMBOL_RE.match(raw_ref.strip()):
        reasons.append(
            where + " 的 ref 必须是 [A-Za-z][A-Za-z0-9_]{0,31} 的符号"
        )
        return "", ""
    return raw_ref.strip(), ("group" if kind == "group_cards" else "card")


def _validate_operation(
    raw: Any,
    visible: set[str],
    group_members: dict[str, list[str]],
    where: str,
    reasons: list[str],
    *,
    earlier: dict[str, str] | None = None,
    all_declared: dict[str, str] | None = None,
) -> dict | None:
    earlier = earlier if isinstance(earlier, dict) else {}
    all_declared = all_declared if isinstance(all_declared, dict) else {}
    if not isinstance(raw, dict):
        reasons.append(where + " 不是对象")
        return None
    op = raw.get("op")
    if not isinstance(op, str) or op not in models.ALLOWED_INTENT_OPS:
        reasons.append(
            where
            + " 出现了不允许的操作："
            + repr(op)
            + "（允许："
            + ", ".join(models.ALLOWED_INTENT_OPS)
            + "）"
        )
        return None
    shape = prompts.OPERATION_SHAPES.get(op, {})
    required = tuple(shape.get("required", ()))
    allowed = set(required) | set(shape.get("optional", ())) | {"op"}
    label = where + " 的操作 " + op
    before = len(reasons)
    for name in required:
        if raw.get(name) is None:
            reasons.append(label + " 缺少必填参数 " + name)
    for name in raw:
        if name not in allowed:
            reasons.append(label + " 含不支持的参数 " + str(name))
    payload: dict[str, Any] = {"op": op}
    if op in ("add_reply_card", "add_text_card"):
        content = raw.get("content")
        if not isinstance(content, str) or not content.strip():
            reasons.append(label + " 的 content 必须是非空文字")
        else:
            payload["content"] = _clip(content, MAX_MATERIAL_CHARS)
        if op == "add_reply_card":
            title = raw.get("title")
            if title is not None and not isinstance(title, str):
                reasons.append(label + " 的 title 必须是文字")
            elif title:
                payload["title"] = _clip(title, MAX_SHORT_CHARS)
        for key in ("x", "y", "w", "h"):
            value = raw.get(key)
            if value is None:
                continue
            if not _is_number(value):
                reasons.append(label + " 的 " + key + " 必须是数字")
            else:
                payload[key] = float(value)
        # F5：`ref` 声明本批次内的符号（供同意图更早之后的操作引用）。
        raw_ref = raw.get("ref")
        if isinstance(raw_ref, str) and REF_SYMBOL_RE.match(raw_ref.strip()):
            payload["ref"] = raw_ref.strip()
        # F7：`place` 由程序确定性算出 x/y（不依赖模型猜坐标）。
        place = raw.get("place")
        if place is not None:
            if not isinstance(place, dict):
                reasons.append(label + " 的 place 必须是对象")
            else:
                for key in place:
                    if key not in ("relativeTo", "side", "gap"):
                        reasons.append(label + " 的 place 含不支持的参数 " + str(key))
                relative_to = _check_card_ref(
                    place.get("relativeTo"),
                    visible,
                    earlier,
                    all_declared,
                    reasons,
                    label + " 的 place.relativeTo",
                )
                checked_place: dict[str, Any] = {}
                if relative_to:
                    checked_place["relativeTo"] = relative_to
                side = place.get("side")
                if side is not None:
                    if not isinstance(side, str) or side not in placement.SIDES:
                        reasons.append(
                            label + " 的 place.side 必须是 "
                            + " / ".join(placement.SIDES)
                            + " 之一"
                        )
                    else:
                        checked_place["side"] = side
                gap = place.get("gap")
                if gap is not None:
                    if not _is_number(gap):
                        reasons.append(label + " 的 place.gap 必须是数字")
                    else:
                        checked_place["gap"] = float(gap)
                if checked_place:
                    payload["place"] = checked_place
    elif op == "move_card":
        card_ref = _check_card_ref(
            raw.get("cardId"), visible, earlier, all_declared, reasons, label
        )
        if card_ref:
            payload["cardId"] = card_ref
        for key in ("x", "y"):
            value = raw.get(key)
            if not _is_number(value):
                reasons.append(label + " 的 " + key + " 必须是数字")
            else:
                payload[key] = float(value)
    elif op == "group_cards":
        ids = raw.get("cardIds")
        if not isinstance(ids, list) or len([i for i in ids if isinstance(i, str)]) < 2:
            reasons.append(label + " 至少需要 2 个卡片 id（成组才有意义）")
        else:
            clean = []
            for cid in ids:
                checked_ref = _check_card_ref(
                    cid, visible, earlier, all_declared, reasons, label
                )
                if checked_ref:
                    clean.append(checked_ref)
            payload["cardIds"] = clean
        raw_ref = raw.get("ref")
        if isinstance(raw_ref, str) and REF_SYMBOL_RE.match(raw_ref.strip()):
            payload["ref"] = raw_ref.strip()
        ordered = raw.get("ordered")
        if ordered is not None:
            if not isinstance(ordered, bool):
                reasons.append(label + " 的 ordered 必须是布尔值")
            else:
                payload["ordered"] = ordered
        name = raw.get("name")
        if name is not None:
            if not isinstance(name, str):
                reasons.append(label + " 的 name 必须是文字")
            else:
                payload["name"] = _clip(name, MAX_SHORT_CHARS)
    elif op == "order_group":
        gid = _check_group_ref(
            raw.get("groupId"), group_members, earlier, all_declared, reasons, label
        )
        known_group = isinstance(gid, str) and gid in group_members
        if gid:
            payload["groupId"] = gid
        order = raw.get("memberOrder")
        if not isinstance(order, list) or not order:
            reasons.append(label + " 的 memberOrder 必须是卡片 id 数组")
        else:
            members = set(group_members.get(gid, [])) if known_group else set()
            clean_order: list[str] = []
            for cid in order:
                checked_ref = _check_card_ref(
                    cid, visible, earlier, all_declared, reasons, label + " 的 memberOrder"
                )
                if not checked_ref:
                    continue
                if members and str(cid) not in members:
                    reasons.append(label + " 的 memberOrder 含不属于该组的卡片：" + str(cid))
                    continue
                clean_order.append(checked_ref)
            payload["memberOrder"] = clean_order
        # F8：可选 arrange（row / column）；没有它时就是**纯逻辑排序**。
        arrange = raw.get("arrange")
        if arrange is not None:
            if not isinstance(arrange, str) or arrange not in ("row", "column"):
                reasons.append(label + " 的 arrange 必须是 row / column 之一")
            else:
                payload["arrange"] = arrange
    elif op == "add_link":
        for key in ("src", "dst"):
            checked_ref = _check_card_ref(
                raw.get(key), visible, earlier, all_declared, reasons, label
            )
            if checked_ref:
                payload[key] = checked_ref
        src, dst = raw.get("src"), raw.get("dst")
        if isinstance(src, str) and src == dst:
            reasons.append(label + " 的两端不能是同一张卡片")
        direction = raw.get("direction")
        if direction is not None:
            if not isinstance(direction, bool):
                reasons.append(label + " 的 direction 必须是布尔值")
            else:
                payload["direction"] = direction
        meaning = raw.get("meaning")
        if meaning is not None:
            if not isinstance(meaning, str):
                reasons.append(label + " 的 meaning 必须是文字")
            else:
                payload["meaning"] = _clip(meaning, MAX_SHORT_CHARS)
    if len(reasons) > before:
        return None
    return payload


def _validate_memory(raw: Any, reasons: list[str]) -> list[dict]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        reasons.append("memory 必须是数组")
        return []
    out: list[dict] = []
    for item in raw[: MAX_MEMORY_ENTRIES + 2]:
        if not isinstance(item, dict):
            reasons.append("memory 条目不是对象")
            continue
        text = item.get("text")
        kind = item.get("kind")
        if not isinstance(text, str) or not text.strip():
            reasons.append("memory 条目缺少 text")
            continue
        if kind not in prompts.MEMORY_KINDS:
            reasons.append("memory 条目的 kind 必须是 " + " / ".join(prompts.MEMORY_KINDS))
            continue
        if len(text) > MAX_MEMORY_CHARS:
            reasons.append(
                "memory 条目过长（上限 "
                + str(MAX_MEMORY_CHARS)
                + " 字）：长期记忆不该是整块板面"
            )
            continue
        out.append({"text": text.strip(), "kind": kind})
    return out[:MAX_MEMORY_ENTRIES]


def _validate_intents(
    raw_intents: Any,
    visible: set[str],
    group_members: dict[str, list[str]],
    material_ids: set[str],
    reasons: list[str],
) -> list[dict]:
    intents: list[dict] = []
    seen_keys: list[str] = []
    for index, raw in enumerate(raw_intents):
        where = "intent[" + str(index) + "]"
        if not isinstance(raw, dict):
            reasons.append(where + " 不是对象")
            continue
        key = raw.get("intentKey")
        if not isinstance(key, str) or not key.strip():
            reasons.append(where + " 缺少 intentKey（稳定的任务身份）")
            key = ""
        key = str(key).strip()
        if key and key in seen_keys:
            reasons.append(where + " 的 intentKey 重复：" + key)
        seen_keys.append(key)
        title = raw.get("title")
        if not isinstance(title, str) or not title.strip():
            reasons.append(where + " 缺少 title")
        operations = raw.get("operations")
        if not isinstance(operations, list) or not operations:
            reasons.append(where + " 没有任何板面操作")
            operations = []
        elif len(operations) > MAX_OPS_PER_INTENT:
            reasons.append(where + " 的操作数超过上限 " + str(MAX_OPS_PER_INTENT))
            operations = operations[:MAX_OPS_PER_INTENT]
        # F5：**先收集声明、再校验引用** —— 引用只能指向同一意图内更早操作声明的、
        # 类型正确的符号；不存在 / 重复 / 前向 / 跨意图 / 跨批次 / 类型错误 / 循环
        # 都会在这里被拒绝。
        all_declared: dict[str, str] = {}
        for op_index, op in enumerate(operations):
            if not isinstance(op, dict):
                continue
            kind = str(op.get("op") or "")
            if kind not in ("add_reply_card", "add_text_card", "group_cards"):
                continue
            symbol, decl_kind = _declared_symbol(
                kind,
                op.get("ref"),
                where + ".操作[" + str(op_index) + "]",
                reasons,
            )
            if not symbol:
                continue
            if symbol in all_declared:
                reasons.append(
                    where + ".操作[" + str(op_index) + "] 重复声明了符号 " + symbol
                )
                continue
            all_declared[symbol] = decl_kind
        earlier: dict[str, str] = {}
        normalized_ops: list[dict] = []
        for op_index, op in enumerate(operations):
            label = where + ".操作[" + str(op_index) + "]"
            checked = _validate_operation(
                op,
                visible,
                group_members,
                label,
                reasons,
                earlier=dict(earlier),
                all_declared=all_declared,
            )
            if checked is not None:
                normalized_ops.append(checked)
                symbol = str(checked.get("ref") or "")
                if symbol:
                    earlier[symbol] = all_declared.get(symbol) or (
                        "group" if str(checked.get("op")) == "group_cards" else "card"
                    )
        if operations and not normalized_ops:
            reasons.append(where + " 的板面操作全部无效")
        raw_basis = raw.get("basis")
        if raw_basis is None:
            raw_basis = []
        if not isinstance(raw_basis, list):
            reasons.append(where + " 的 basis 必须是数组")
            raw_basis = []
        basis: list[dict] = []
        for item in raw_basis:
            if not isinstance(item, dict):
                reasons.append(where + " 的 basis 条目不是对象")
                continue
            cid = item.get("cardId")
            if not _check_card_id(cid, visible, reasons, where + " 的依据"):
                continue
            role = item.get("role") if isinstance(item.get("role"), str) else "material"
            if role == "material" and str(cid) not in material_ids:
                reasons.append(
                    where
                    + " 的材料依据 "
                    + str(cid)
                    + " 不在本次解析出的材料里"
                )
                continue
            basis.append({
                "cardId": str(cid),
                "role": role,
                "version": _clip(item.get("version"), MAX_SHORT_CHARS),
            })
        impact = raw.get("impact")
        if not isinstance(impact, dict):
            reasons.append(where + " 缺少 impact（影响范围必须由模型说明）")
            impact = {}
        impact_ids = impact.get("cardIds")
        if impact_ids is None:
            impact_ids = []
        if not isinstance(impact_ids, list):
            reasons.append(where + " 的 impact.cardIds 必须是数组")
            impact_ids = []
        clean_impact: list[str] = []
        for cid in impact_ids:
            if _check_card_id(cid, visible, reasons, where + " 的影响范围"):
                clean_impact.append(str(cid))
        depends = _validate_key_list(raw.get("dependsOn"), where + " 的 dependsOn", reasons)
        conflicts = _validate_key_list(raw.get("conflictsWith"), where + " 的 conflictsWith", reasons)
        conflict_key = raw.get("conflictKey")
        if not isinstance(conflict_key, str):
            conflict_key = ""
        if conflicts and not conflict_key.strip():
            reasons.append(where + " 声明了冲突却没有给出 conflictKey（冲突组身份）")
        intents.append({
            "intentKey": key,
            "title": _clip(title, MAX_SHORT_CHARS),
            "goal": _clip(raw.get("goal"), MAX_SHORT_CHARS),
            "summary": _clip(raw.get("summary"), MAX_SHORT_CHARS),
            "basis": basis,
            "expected": _clip(raw.get("expected"), MAX_SHORT_CHARS),
            "operations": normalized_ops,
            "impact": {"cardIds": clean_impact, "scope": _clip(impact.get("scope"), MAX_SHORT_CHARS)},
            "dependsOn": depends,
            "conflictsWith": conflicts,
            "conflictKey": _clip(conflict_key, MAX_SHORT_CHARS),
        })
    known = {i["intentKey"] for i in intents if i["intentKey"]}
    for intent in intents:
        label = intent["intentKey"] or intent["title"] or "（未命名）"
        for dep in intent["dependsOn"]:
            if dep not in known:
                reasons.append("intent " + label + " 依赖了不存在的 intentKey：" + dep)
        for other in intent["conflictsWith"]:
            if other not in known:
                reasons.append("intent " + label + " 的冲突引用无法解析：" + other)
    return intents


def validate_output(payload: Any, *, context: dict) -> dict:
    """复核模型输出。返回 {"ok", "reasons", "output"}；ok=False 时 output=None。

    复核维度：结构、对象引用是否在本轮可见范围、材料 id 是否来自材料解析结果、
    操作类型是否在白名单内且参数形状正确、依赖与冲突能否解析、修改范围是否受限。
    """
    if not isinstance(payload, dict):
        return _invalid(["输出不是 JSON 对象（顶层必须是对象）"])
    visible = {str(c) for c in (context.get("visibleCardIds") or [])}
    group_members: dict[str, list[str]] = {}
    for group in context.get("groups") or []:
        if isinstance(group, dict):
            group_members[str(group.get("id") or "")] = [str(m) for m in (group.get("members") or [])]
    material_ids = {str(m) for m in (context.get("materialIds") or [])}
    reasons: list[str] = []
    kind = payload.get("kind")
    if kind not in models.UNDERSTANDING_KINDS:
        return _invalid([
            "kind 必须是 " + " / ".join(models.UNDERSTANDING_KINDS) + " 之一，实际是：" + repr(kind)
        ])
    summary = payload.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        reasons.append("缺少 summary：需要一句给用户看的中文说明")
    memory = _validate_memory(payload.get("memory"), reasons)
    normalized: dict[str, Any] = {
        "kind": kind,
        "summary": _clip(summary, MAX_SHORT_CHARS),
        "clarification": None,
        "intents": [],
        "memory": memory,
    }
    raw_intents = payload.get("intents")
    if kind == "intent":
        if not isinstance(raw_intents, list) or not raw_intents:
            reasons.append("kind=intent 时必须给出非空的 intents 数组")
        elif len(raw_intents) > MAX_INTENTS:
            reasons.append("意图数量超过上限 " + str(MAX_INTENTS) + "（修改范围必须受限）")
        else:
            normalized["intents"] = _validate_intents(
                raw_intents, visible, group_members, material_ids, reasons
            )
            if not normalized["intents"]:
                reasons.append("kind=intent 但没有一个可执行的意图")
    else:
        if isinstance(raw_intents, list) and raw_intents:
            reasons.append(
                "kind="
                + str(kind)
                + " 不允许产出意图：clarification / no_action 都不创建任何任务"
            )
    if kind == "clarification":
        clarification = payload.get("clarification")
        if not isinstance(clarification, dict):
            reasons.append("kind=clarification 时必须给出 clarification 对象")
        else:
            question = clarification.get("question")
            if not isinstance(question, str) or not question.strip():
                reasons.append("clarification 缺少 question")
            missing = clarification.get("missing")
            if missing is not None and not isinstance(missing, list):
                reasons.append("clarification.missing 必须是字符串数组")
                missing = []
            normalized["clarification"] = {
                "question": _clip(question, MAX_SHORT_CHARS),
                "missing": [
                    _clip(m, MAX_SHORT_CHARS)
                    for m in (missing or [])
                    if isinstance(m, str) and m.strip()
                ][:MAX_VALIDATION_REASONS],
            }
    if reasons:
        return _invalid(reasons)
    return {"ok": True, "reasons": [], "output": normalized}

# --- 模型调用（复用既有凭据 / 用量 / 预算链路） ----------------------------


def _strip_fences(text: str) -> str:
    kept = [line for line in text.splitlines() if not line.strip().startswith("```")]
    return NL.join(kept).strip()


def _parse_json_object(text: Any) -> tuple[dict | None, str]:
    raw = str(text or "").strip()
    if not raw:
        return None, "模型没有返回任何内容"
    if raw.startswith("```"):
        raw = _strip_fences(raw)
    start = raw.find("{")
    if start < 0:
        return None, "模型输出里没有 JSON 对象（顶层必须是对象）"
    try:
        payload, _end = json.JSONDecoder().raw_decode(raw[start:])
    except ValueError as exc:
        return None, "模型输出不是合法 JSON：" + _safe_error(exc)
    if not isinstance(payload, dict):
        return None, "模型输出不是 JSON 对象（顶层必须是对象）"
    return payload, ""


def _completion_text(completion: Any) -> str:
    if completion is None:
        return ""
    if getattr(completion, "stream_incomplete", False):
        raise RuntimeError("模型流没有正常结束，不把半截输出当成理解结果")
    message = getattr(completion, "message", None)
    content = getattr(message, "content", None) if message is not None else None
    return content if isinstance(content, str) else ""


async def _adapter_for_stage(
    adapter_provider: Any, *, images_hint: bool = False, errors: list | None = None
) -> Any:
    """取这次理解要用的适配器。

    `images_hint=True` 表示「本次可能带图」：调用方（路由）据此**优先**选一条视觉用途的
    凭据；老调用方（只接受无参可调用对象）自动退回既有取法，行为逐字不变。
    取不到时把原因追加进 `errors`（调用方用它写出可读的失败原因，不吞掉细节）。
    """

    def _note(exc: BaseException) -> None:
        if errors is not None:
            errors.append(_safe_error(exc))

    if images_hint:
        try:
            return await adapter_provider(images=True)
        except TypeError:
            pass  # 老形状：无参 provider
        except Exception as exc:  # noqa: BLE001 - 取 adapter 失败按「没有适配器」处理
            _note(exc)
            return None
    try:
        return await adapter_provider()
    except Exception as exc:  # noqa: BLE001 - 取 adapter 失败也算理解失败，不是提交失败
        _note(exc)
        return None


def _adapter_supports_images(adapter: Any) -> bool:
    """这条链路能不能真的把图片交给模型（适配器自己声明；缺声明一律按不支持）。"""
    if adapter is None:
        return False
    return bool(getattr(adapter, "supports_images", False))


async def _call_model(adapter: Any, messages: list) -> Any:
    """一次模型请求：先过预算闸，再按既有记账链路归因本次用量。

    真实 adapter 自己按请求记账（adapter_self_accounts），这里不重复记；
    测试里的假 adapter 由这里代记。记账失败不得打断理解。
    """
    from agent.credentials import usage as usage_module

    usage_module.ensure_adapter_request_allowed(adapter)
    try:
        completion = await adapter.complete(messages, [])
    except Exception as exc:
        try:
            if not usage_module.adapter_self_accounts(adapter):
                usage_module.account_adapter_failure(adapter, exc)
        except Exception:  # noqa: BLE001 - 记账失败不得掩盖原始错误
            pass
        raise
    try:
        if not usage_module.adapter_self_accounts(adapter):
            usage_module.account_adapter_request(adapter, getattr(completion, "usage", None))
    except Exception:  # noqa: BLE001 - 记账失败不得打断理解
        pass
    return completion


def _adapter_label(adapter: Any) -> str:
    mode = getattr(adapter, "mode", "")
    mode_value = getattr(mode, "value", mode)
    model = getattr(adapter, "model", "") or ""
    return _clip(str(mode_value or "unknown") + ":" + str(model), 120)


# --- 意图落库（延迟 import C 的实现） --------------------------------------


def _create_intents(
    conn: sqlite3.Connection, *, board_id: str, understanding_id: str, payloads: list[dict]
) -> dict:
    """延迟 import agent.interactive.intents 并落成意图。

    失败只记进理解记录，**不丢理解结果**；clarification / no_action 不调用它。
    """
    try:
        from agent.interactive import intents as intents_module
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "skipped": False,
            "intentIds": [],
            "error": "意图模块不可用：" + _safe_error(exc),
        }
    create = getattr(intents_module, "create_intents_from_understanding", None)
    if create is None:
        return {
            "ok": False,
            "skipped": False,
            "intentIds": [],
            "error": "意图模块还没有 create_intents_from_understanding：理解结果已保留，未落成任务",
        }
    try:
        created = create(
            conn, board_id=board_id, understanding_id=understanding_id, payloads=payloads
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "skipped": False,
            "intentIds": [],
            "error": "意图落库失败（理解结果已保留）：" + _safe_error(exc),
        }
    rows = [r for r in list(created or []) if isinstance(r, dict)]
    return {
        "ok": True,
        "skipped": False,
        "intentIds": [str(r.get("id")) for r in rows if r.get("id")],
        "count": len(rows),
    }


# --- 长期记忆（复用 agent.knowledge / agent.memory 既有机制） --------------


def _memory_candidates(normalized: dict, context: dict) -> list[dict]:
    """只挑适合后续合作的内容：用户表达、模型补充、待审批建议。"""
    candidates: list[dict] = []
    requirement = context.get("requirement") or {}
    text = str(requirement.get("text") or "").strip()
    if text:
        candidates.append({"text": text, "kind": "goal", "provenance": "user_statement"})
    for item in normalized.get("memory") or []:
        candidates.append({
            "text": str(item.get("text") or ""),
            "kind": str(item.get("kind") or ""),
            "provenance": "model_inference",
        })
    if normalized.get("kind") == "intent":
        for intent in normalized.get("intents") or []:
            title = str(intent.get("title") or "").strip()
            goal = str(intent.get("goal") or intent.get("summary") or "").strip()
            if not (title or goal):
                continue
            candidates.append({
                "text": (title + "：" + goal).strip("："),
                "kind": "goal",
                "provenance": "pending_suggestion",
                "intentKey": intent.get("intentKey"),
            })
    return candidates


def _sanitize_memory_entry(entry: dict, visible_ids: tuple[str, ...]) -> tuple[dict | None, str]:
    text = _redact_text(str(entry.get("text") or "")).strip()
    kind = str(entry.get("kind") or "")
    provenance = str(entry.get("provenance") or "model_inference")
    if provenance not in MEMORY_PROVENANCE:
        provenance = "model_inference"
    if kind not in prompts.MEMORY_KINDS:
        return None, "记忆类别不在允许范围（" + " / ".join(prompts.MEMORY_KINDS) + "）"
    if not text:
        return None, "记忆内容为空"
    if text[:1] in ("{", "["):
        try:
            json.loads(text)
            return None, "内容像整块板面 / JSON 数据，不写入长期记忆"
        except ValueError:
            pass
    hits = sum(1 for cid in visible_ids if cid and cid in text)
    if hits > 3:
        return None, "内容堆了多张卡片的 id（疑似整块板面），不写入长期记忆"
    truncated = len(text) > MAX_MEMORY_CHARS
    if truncated:
        text = text[:MAX_MEMORY_CHARS]
    return {
        "text": text,
        "kind": kind,
        "provenance": provenance,
        "truncated": truncated,
    }, ""


def _store_memory_entry(
    conn: sqlite3.Connection, *, entry: dict, board_id: str, understanding_id: str
) -> dict:
    """复用既有 knowledge 生命周期：一律先落 pending_review（待审批），不绕过审批。"""
    from agent.knowledge.lifecycle import KnowledgeService

    service = KnowledgeService(conn)
    category = MEMORY_CATEGORY.get(entry["kind"], "general_fact")
    item = service.create(
        category=category,
        content=entry["text"],
        node_ids=[],
        provenance={
            "source": "interactive_understanding",
            "understanding_id": understanding_id,
            "board_id": board_id,
            "memory_kind": entry["kind"],
            "provenance": entry["provenance"],
        },
    )
    service.submit(item.id)
    record = {"knowledgeId": item.id, "state": "pending_review", "stored": True, "reason": ""}
    if entry["provenance"] == "completed_fact":
        try:
            service.verify(item.id, verified_by="system")
            service.activate(item.id)
            record["state"] = "active"
        except Exception as exc:  # noqa: BLE001 - 高影响类别要用户确认，保留待审批
            record["reason"] = "已完成事实未能激活（保留待审批）：" + _safe_error(exc)
    return record


def write_memory_entries(
    conn: sqlite3.Connection,
    *,
    board_id: str,
    understanding_id: str,
    entries: list[dict],
    visible_ids: tuple[str, ...] = (),
) -> list[dict]:
    """把候选记忆写入长期记忆；每条都如实回报是否写入、为何没写。"""
    records: list[dict] = []
    seen: set[tuple] = set()
    for entry in entries:
        if len(records) >= MAX_MEMORY_ENTRIES:
            records.append({
                "stored": False,
                "text": "",
                "kind": entry.get("kind"),
                "provenance": entry.get("provenance"),
                "reason": "超过单次上限 " + str(MAX_MEMORY_ENTRIES) + " 条，未写入",
            })
            break
        clean, reason = _sanitize_memory_entry(entry, visible_ids)
        if clean is None:
            records.append({
                "stored": False,
                "text": _clip(entry.get("text"), 80),
                "kind": entry.get("kind"),
                "provenance": entry.get("provenance"),
                "reason": reason,
            })
            continue
        key = (clean["kind"], clean["text"])
        if key in seen:
            records.append({
                "stored": False,
                "text": _clip(clean["text"], 80),
                "kind": clean["kind"],
                "provenance": clean["provenance"],
                "reason": "与本次已写入的条目重复",
            })
            continue
        seen.add(key)
        record = dict(clean)
        record["boardId"] = board_id
        record["understandingId"] = understanding_id
        try:
            record.update(
                _store_memory_entry(
                    conn, entry=clean, board_id=board_id, understanding_id=understanding_id
                )
            )
        except Exception as exc:  # noqa: BLE001 - 记忆写入失败不得影响理解结果
            record["stored"] = False
            record["state"] = "none"
            record["reason"] = "长期记忆写入失败：" + _safe_error(exc)
        records.append(record)
    return records


def record_memory(
    conn: sqlite3.Connection,
    *,
    board_id: str,
    understanding_id: str,
    normalized: dict,
    context: dict,
) -> list[dict]:
    """理解通过后的长期记忆入口：只写目标 / 结论 / 依据 / 成果。"""
    candidates = _memory_candidates(normalized, context)
    visible_ids = tuple(str(c) for c in (context.get("visibleCardIds") or []))
    return write_memory_entries(
        conn,
        board_id=board_id,
        understanding_id=understanding_id,
        entries=candidates,
        visible_ids=visible_ids,
    )

# --- 执行一次真实理解 -----------------------------------------------------


def _fail(
    conn: sqlite3.Connection,
    understanding_id: str,
    *,
    stage: str,
    error: str,
    reasons: list[str] | None = None,
) -> dict:
    """把失败如实落库：status=failed + 可读原因 + validation.reasons。"""
    clean = [
        _clip(reason, MAX_SHORT_CHARS)
        for reason in (reasons or [error])
        if str(reason or "").strip()
    ]
    if not clean:
        clean = ["理解失败，但没有可读原因"]
    _set_stage(conn, understanding_id, stage, error)
    _update(
        conn,
        understanding_id,
        status="failed",
        error=_clip(error, 600),
        validation={"ok": False, "reasons": clean[:MAX_VALIDATION_REASONS]},
        finished_at=_now(),
    )
    fresh = _load_row(conn, understanding_id)
    assert fresh is not None
    return _row_dict(fresh)


async def run_understanding(
    conn: sqlite3.Connection,
    understanding_id: str,
    *,
    adapter_provider: Callable[[], Awaitable[Any]],
) -> dict:
    """执行一次真实理解：材料 → 模型 → 结构化校验 → 落库。

    adapter_provider 的形状是 async () -> BaseAdapter | None，由调用方复用 QIO 既有的
    凭据 / 用量 / 预算链路（build_adapter / build_adapter_for_credential）来构造。

    任何异常（没有 adapter、模型报错、输出无法解析）都收敛为 status=failed + 可读原因；
    本函数不向外抛，也绝不让已受理成功的提交被改判失败。
    """
    row = _load_row(conn, understanding_id)
    if row is None:
        return {
            "id": understanding_id,
            "status": "failed",
            "boardId": None,
            "error": "理解请求不存在，无法执行理解。",
            "validation": {"ok": False, "reasons": ["理解请求不存在"]},
        }
    record = _row_dict(row)
    if record["status"] in ("understood", "needs_clarification", "no_action"):
        record["skipped"] = True
        record["reason"] = "这次理解已经完成：不重复调用模型，也不重复创建任务。"
        return record

    board_id = str(row["board_id"])
    attempt = int(row["attempt"]) if row["status"] == "accepted" else int(row["attempt"]) + 1
    started_at = row["started_at"] or _now()
    current_stage = "materials"
    _update(
        conn,
        understanding_id,
        status="understanding",
        error=None,
        attempt=attempt,
        started_at=started_at,
        finished_at=None,
        output={},
        validation={},
        memory=[],
    )
    # 整合轮契约 §2：这一次尝试的身份 = (记录 id, attempt)。写终结状态与创建意图之前
    # 必须核对它没有被新的一次尝试 / 新实例取代（关停中断后重试会 attempt+1）——
    # 旧实例的迟到结果不得写回当前记录，也不得建意图。
    run_attempt = attempt

    def _discard_if_superseded() -> dict | None:
        """已被新的一次尝试取代时返回「丢弃」结果（调用方直接 return），否则 None。

        读不到记录按「已取代」处理：宁可什么都不写，也不能把迟到的结果写进一条
        来路不明的记录。丢弃时如实写日志，不改记录、不建意图。
        """
        fresh = _load_row(conn, understanding_id)
        if fresh is not None and int(fresh["attempt"] or 1) == run_attempt:
            return None
        logger.warning(
            "互动理解已被新的一次尝试取代：这一次的迟到结果不再写回（%s）", understanding_id
        )
        if fresh is None:
            return {
                "id": understanding_id,
                "status": None,
                "boardId": board_id,
                "discarded": True,
                "reason": "这条理解记录已经不存在：这一次的结果不再写回。",
            }
        record_now = _row_dict(fresh)
        record_now["discarded"] = True
        record_now["reason"] = "这次理解已经被更新的一次尝试取代：迟到的结果不再写回。"
        return record_now

    try:
        current_stage = "materials"
        _set_stage(conn, understanding_id, "materials", "正在读取本次允许查看的材料")
        # 视觉能力要在**读材料之前**就问出来：它决定图片是「交付字节」还是「如实说不支持」。
        provider_errors: list[str] = []
        adapter = await _adapter_for_stage(
            adapter_provider, images_hint=True, errors=provider_errors
        )
        vision = _adapter_supports_images(adapter)
        context = build_context(
            conn,
            board_id=board_id,
            understanding_id=understanding_id,
            vision=vision,
        )
        materials = [m for m in (context.get("materials") or []) if isinstance(m, dict)]
        _update(conn, understanding_id, context=context, materials=materials)

        current_stage = "model"
        _set_stage(conn, understanding_id, "model", "正在调用模型理解这次提交")
        if adapter is None:
            reason = "当前没有可用的模型适配器，无法进行真实理解"
            if provider_errors:
                reason += "（" + provider_errors[0] + "）"
            reason += "：受理记录已保留，补充凭据后可以重试。"
            discarded = _discard_if_superseded()
            if discarded is not None:
                return discarded
            return _fail(
                conn, understanding_id, stage="model", error=reason, reasons=[reason]
            )

        raw = ""
        # 真正交付的图片内容块（上下文里记着这次交付了哪些；不是「有图片卡」就算）
        image_parts = image_parts_from_context(context)
        messages = prompts.build_understanding_messages(context, images=image_parts)
        completion = await _call_model(adapter, messages)
        raw = _completion_text(completion)

        current_stage = "validation"
        _set_stage(conn, understanding_id, "validation", "正在校验模型输出的结构、引用与操作")
        payload, parse_reason = _parse_json_object(raw)
        if payload is None:
            validation = {"ok": False, "reasons": [parse_reason], "output": None}
        else:
            validation = validate_output(payload, context=context)
        attempts = 1
        if not validation["ok"]:
            # 自动修复**至多一次**：第二次仍无效就如实失败，不落成可执行任务。
            attempts = 2
            _set_stage(conn, understanding_id, "validation", "输出无效，正在请求模型修复一次")
            repair_messages = prompts.build_repair_messages(
                context, raw, list(validation.get("reasons") or []), images=image_parts
            )
            repair_completion = await _call_model(adapter, repair_messages)
            raw = _completion_text(repair_completion)
            payload, parse_reason = _parse_json_object(raw)
            if payload is None:
                validation = {"ok": False, "reasons": [parse_reason], "output": None}
            else:
                validation = validate_output(payload, context=context)
        if not validation["ok"]:
            reasons = [str(r) for r in (validation.get("reasons") or [])]
            error = (
                "模型输出未通过程序校验（已自动修复 1 次，仍然无效），没有落成任何任务："
                + ("；".join(reasons) if reasons else "原因未知")
            )
            discarded = _discard_if_superseded()
            if discarded is not None:
                return discarded
            _update(
                conn,
                understanding_id,
                adapter=_adapter_label(adapter),
                output={"raw": _clip(_redact_text(raw), MAX_RAW_CHARS)},
            )
            return _fail(
                conn, understanding_id, stage="validation", error=error, reasons=reasons
            )

        normalized = validation["output"]
        kind = str(normalized["kind"])
        status = {
            "intent": "understood",
            "clarification": "needs_clarification",
            "no_action": "no_action",
        }[kind]

        current_stage = "persist"
        _set_stage(conn, understanding_id, "persist", "正在落库并登记本次理解结果")
        output: dict[str, Any] = {
            "kind": kind,
            "summary": normalized["summary"],
            "clarification": normalized["clarification"],
            "intents": normalized["intents"],
            "memory": normalized["memory"],
            "raw": _clip(_redact_text(raw), MAX_RAW_CHARS),
        }
        discarded = _discard_if_superseded()
        if discarded is not None:
            return discarded
        if kind == "intent":
            intent_creation = _create_intents(
                conn,
                board_id=board_id,
                understanding_id=understanding_id,
                payloads=list(normalized["intents"]),
            )
        else:
            intent_creation = {
                "ok": True,
                "skipped": True,
                "intentIds": [],
                "reason": "本次判定为 " + kind + "：不创建任何意图。",
            }
        output["intentCreation"] = intent_creation

        try:
            memory_records = record_memory(
                conn,
                board_id=board_id,
                understanding_id=understanding_id,
                normalized=normalized,
                context=context,
            )
        except Exception as exc:  # noqa: BLE001 - 记忆写入失败绝不改判已经成功的理解
            memory_records = [
                {
                    "stored": False,
                    "text": "",
                    "reason": "长期记忆写入失败：" + _safe_error(exc),
                }
            ]
        _update(
            conn,
            understanding_id,
            status=status,
            adapter=_adapter_label(adapter),
            output=output,
            memory=memory_records,
            context=context,
            materials=materials,
            validation={
                "ok": True,
                "reasons": [],
                "attempts": attempts,
                "kind": kind,
                "checks": [
                    "结构",
                    "对象引用在本轮可见范围",
                    "材料 id 来自材料解析结果",
                    "操作类型与参数形状",
                    "依赖与冲突引用",
                    "修改范围",
                ],
            },
            error=None,
            finished_at=_now(),
        )
        fresh = _load_row(conn, understanding_id)
        assert fresh is not None
        return _row_dict(fresh)
    except Exception as exc:  # noqa: BLE001 - 任何异常都收敛为失败，绝不向外抛
        discarded = _discard_if_superseded()
        if discarded is not None:
            return discarded
        message = "理解过程出现异常（已收敛为失败，可从快照重试）：" + _safe_error(exc)
        return _fail(
            conn,
            understanding_id,
            stage=current_stage,
            error=message,
            reasons=[message],
        )
