"""历史卡片 → 统一文字卡的逐卡迁移（契约 §1.1 / §2.2）。

契约：docs/superpowers/plans/2026-10-12-unified-cards-media-preview-contract.md。

三条不可动摇的性质：

1. **逐卡迁移**：一张旧卡变成一张文字卡，id 不变 —— 绝不把历史上多张独立卡片合并成一张；
2. **内容保真**：代码块逐字（缩进 / 换行 / 行尾空白 / 语言）、链接保留地址与标题；
3. **幂等**：已经是文字卡（含 meta.doc）时原样返回 (card, False)，第二次加载不再写板面。

迁移只发生在**板面状态的加载 / 保存**这一层（board_store），不改历史快照表、不改草稿。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from agent.interactive import text_card

#: 迁移保存使用的 reason（契约 §2.2 冻结字面量）
MIGRATION_REASON = "text_card_migration"


def _meta_of(card: dict) -> dict:
    meta = card.get("meta")
    return dict(meta) if isinstance(meta, dict) else {}


def _stable_block_id(index: int, block: dict) -> str:
    """迁移产出的块 id 必须**由内容决定**，不能随机。

    两张内容相同的卡（例如「撤回后重新加回同样内容」）迁移后必须得到同一份 doc：
    随机 id 会让去重指纹（kind + content + meta）把它们判成两次不同的提交，
    也会让同一张旧卡反复迁移时产生假的内容变化。
    """
    payload = json.dumps(
        {
            "index": index,
            "kind": block.get("kind"),
            "text": block.get("text") or "",
            "level": block.get("level"),
            "ordered": block.get("ordered"),
            "attrs": block.get("attrs") or {},
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return "b_" + hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def _stabilize_ids(doc: dict) -> dict:
    for index, block in enumerate(doc.get("blocks") or []):
        if isinstance(block, dict):
            block["id"] = _stable_block_id(index, block)
    return doc


def migrate_card(card: dict) -> tuple[dict, bool]:
    """把一张旧 code / url 卡迁成文字卡；缺 meta.doc 的文字卡补齐内容模型。

    返回 (新卡, 是否发生了迁移)。**幂等**：已经迁移过（文字卡且有 meta.doc）时
    原样返回 (card, False)。

    不改 id / x / y / w / h / groups / links / 草稿 / 书签 / createdAt / hidden / deleted；
    旧 code / url 卡迁移后 checked=True（它们过去默认就在材料范围内，迁移不缩小范围）。
    """
    if not isinstance(card, dict):
        return card, False
    kind = str(card.get("kind") or "")
    if kind not in ("code", "url", "text"):
        # 其他 kind（file / reply / 未知）原样不动
        return card, False

    meta = _meta_of(card)
    content = str(card.get("content") or "")

    if kind == "text":
        if text_card.is_text_doc(meta.get("doc")):
            return card, False
        # 缺 meta.doc 的旧注释：补齐 doc / docText；checked 不变（未许可的仍然不可见）
        doc = _stabilize_ids(text_card.parse_doc_text(content))
        work = dict(card)
        work["meta"] = {
            **meta,
            "doc": doc,
            "docText": text_card.serialize_doc_text(doc),
        }
        return work, True

    if kind == "code":
        block = text_card.new_block(
            "code", content, attrs={"language": str(meta.get("language") or "")}
        )
    else:  # url
        url_meta = meta.get("url") if isinstance(meta.get("url"), dict) else {}
        href = str(url_meta.get("href") or content)
        title = str(url_meta.get("title") or meta.get("title") or "")
        block = text_card.new_block("link", title, attrs={"href": href})

    doc = _stabilize_ids({"schema": text_card.TEXT_DOC_SCHEMA, "blocks": [block]})
    fields = text_card.card_text_fields(doc)
    work = dict(card)
    work["kind"] = "text"
    work["content"] = fields["content"]
    work["checked"] = True
    work["meta"] = {**meta, "doc": fields["doc"], "docText": fields["docText"]}
    return work, True


def migrate_state(state: dict) -> tuple[dict, bool]:
    """逐卡迁移整块板面状态。返回 (state, 是否发生了迁移)。

    卡片顺序、分组、关系、选择全部原样保留；只有**发生迁移的卡片**被替换。
    """
    if not isinstance(state, dict):
        return state, False
    cards = state.get("cards")
    if not isinstance(cards, list):
        return state, False
    migrated = False
    migrated_cards: list[Any] = []
    for card in cards:
        new_card, changed = migrate_card(card)
        migrated_cards.append(new_card)
        migrated = migrated or changed
    if migrated:
        state["cards"] = migrated_cards
    return state, migrated
