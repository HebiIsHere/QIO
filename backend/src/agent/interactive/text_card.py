"""统一文字卡的内容模型（块序列 ↔ 编辑区文本）——契约实现（Lead 维护）。

契约：docs/superpowers/plans/2026-10-12-unified-cards-media-preview-contract.md 第 1 节。
本模块是**纯函数**：不碰数据库、不发事件 —— board / board_store / materials / submission
都只消费它的结果，「什么是文字卡的内容」只有一份判断。

三条不可动摇的性质（测试锁死）：

1. **代码保真**：code 块的 text 逐字保存缩进 / 换行 / 行尾空白 / 语言信息；
2. serialize_doc_text(parse_doc_text(t)) == t（规范写法）；
3. parse_doc_text(serialize_doc_text(doc)) 的块序与 code / list 文本不变。
"""

from __future__ import annotations

import re
import uuid
from typing import Any

TEXT_DOC_SCHEMA = "qio.textcard/1"

BLOCK_KINDS = ("paragraph", "heading", "list", "quote", "code", "link")

MAX_HEADING_LEVEL = 3

FENCE = "```"

#: 旧卡片类型（迁移前）。迁移后一律变成 "text"；这里只用于识别历史数据。
LEGACY_TEXT_KINDS = ("code", "url")


def new_block_id() -> str:
    return "b_" + uuid.uuid4().hex[:12]


def empty_doc() -> dict:
    return {"schema": TEXT_DOC_SCHEMA, "blocks": []}


def _clamp_level(level: Any) -> int:
    try:
        value = int(level)
    except (TypeError, ValueError):
        return 1
    return max(1, min(MAX_HEADING_LEVEL, value))


def new_block(kind: str, text: str = "", **extra: Any) -> dict:
    if kind not in BLOCK_KINDS:
        raise ValueError("unknown block kind: " + str(kind))
    block: dict = {"id": new_block_id(), "kind": kind, "text": str(text)}
    if kind == "heading":
        block["level"] = _clamp_level(extra.get("level", 1))
    elif kind == "list":
        block["ordered"] = bool(extra.get("ordered", False))
    elif kind == "code":
        attrs = extra.get("attrs") or {}
        block["attrs"] = {"language": str(attrs.get("language") or ""), "fence": FENCE}
    elif kind == "link":
        attrs = extra.get("attrs") or {}
        block["attrs"] = {"href": str(attrs.get("href") or "")}
    return block


def _lines(text: str) -> list[str]:
    raw = str(text or "").split("\n")
    return [line[:-1] if line.endswith("\r") else line for line in raw]


def _match_heading(line: str) -> tuple[int, str] | None:
    stripped = line.lstrip("#")
    level = len(line) - len(stripped)
    if 1 <= level <= MAX_HEADING_LEVEL and stripped.startswith(" "):
        return level, stripped[1:]
    return None


def _match_ul(line: str) -> str | None:
    if len(line) >= 2 and line[0] in "-*+" and line[1] == " ":
        return line[2:]
    return None


def _match_ol(line: str) -> str | None:
    digits = 0
    while digits < len(line) and line[digits].isdigit():
        digits += 1
    if 0 < digits <= 9 and line[digits:digits + 2] == ". ":
        return line[digits + 2:]
    return None


def _match_quote(line: str) -> str | None:
    if line.startswith("> "):
        return line[2:]
    if line == ">":
        return ""
    return None


def _match_link(line: str) -> tuple[str, str] | None:
    if not (line.startswith("[") and line.endswith(")")):
        return None
    close = line.find("](")
    if close <= 0:
        return None
    label = line[1:close]
    href = line[close + 2:-1]
    if "]" in href:
        return None
    return label, href


def parse_doc_text(text: str) -> dict:
    """编辑区文本 → 块序列。围栏内逐字；未闭合围栏记 issue 并读到末尾。"""
    lines = _lines(text)
    blocks: list[dict] = []
    issues: list[str] = []
    i = 0
    total = len(lines)
    while i < total:
        line = lines[i]
        if line.strip() == "":
            i += 1
            continue
        if line.startswith(FENCE):
            language = line[len(FENCE):].strip()
            body: list[str] = []
            i += 1
            closed = False
            while i < total:
                if lines[i].startswith(FENCE):
                    closed = True
                    i += 1
                    break
                body.append(lines[i])
                i += 1
            if not closed:
                issues.append("代码围栏未闭合，已按代码块读到末尾")
            value = ("\n".join(body) + "\n") if body else ""
            blocks.append(new_block("code", value, attrs={"language": language}))
            continue
        link = _match_link(line)
        if link is not None:
            blocks.append(new_block("link", link[0], attrs={"href": link[1]}))
            i += 1
            continue
        heading = _match_heading(line)
        if heading is not None:
            blocks.append(new_block("heading", heading[1], level=heading[0]))
            i += 1
            continue
        if _match_quote(line) is not None:
            quote_parts: list[str] = []
            while i < total and lines[i].strip() != "" and _match_quote(lines[i]) is not None:
                quote_parts.append(_match_quote(lines[i]) or "")
                i += 1
            blocks.append(new_block("quote", "\n".join(quote_parts)))
            continue
        ordered = _match_ol(line) is not None
        if ordered or _match_ul(line) is not None:
            items: list[str] = []
            while i < total and lines[i].strip() != "":
                item = _match_ol(lines[i]) if ordered else _match_ul(lines[i])
                if item is None:
                    break
                items.append(item)
                i += 1
            blocks.append(new_block("list", "\n".join(items), ordered=ordered))
            continue
        para: list[str] = []
        while i < total and lines[i].strip() != "":
            para.append(lines[i])
            i += 1
        blocks.append(new_block("paragraph", "\n".join(para)))
    doc: dict = {"schema": TEXT_DOC_SCHEMA, "blocks": blocks}
    if issues:
        doc["issues"] = issues
    return doc


def _text_lines(text: str) -> list[str]:
    raw = str(text or "").split("\n")
    if raw and raw[-1] == "":
        raw.pop()
    return raw


def _serialize_block(block: dict) -> list[str]:
    kind = block.get("kind")
    text = str(block.get("text") or "")
    if kind == "heading":
        return ["#" * _clamp_level(block.get("level", 1)) + " " + text]
    if kind == "list":
        prefix = "1. " if block.get("ordered") else "- "
        return [prefix + line for line in _text_lines(text)]
    if kind == "quote":
        return ["> " + line for line in _text_lines(text)]
    if kind == "code":
        attrs = block.get("attrs") or {}
        language = str(attrs.get("language") or "")
        body = _text_lines(text)
        if not body and text.endswith("\n"):
            body = [""]
        return [FENCE + language, *body, FENCE]
    if kind == "link":
        attrs = block.get("attrs") or {}
        return ["[" + text + "](" + str(attrs.get("href") or "") + ")"]
    return _text_lines(text)


def serialize_doc_text(doc: dict | None) -> str:
    blocks = (doc or {}).get("blocks") or []
    chunks = ["\n".join(_serialize_block(block)) for block in blocks]
    if not chunks:
        return ""
    return "\n\n".join(chunks) + "\n"


def inline_plain(text: str) -> str:
    """去掉行内格式标记。返回空串时回退原文（绝不因为格式标记丢掉整段内容）。"""
    source = str(text or "")
    if not source:
        return ""
    out = re.sub(r"\*\*(.+?)\*\*", r"\1", source)
    out = re.sub(r"__(.+?)__", r"\1", out)
    out = re.sub(r"~~(.+?)~~", r"\1", out)
    out = re.sub(r"(?<![\w\u4e00-\u9fff])`([^`]+)`", r"\1", out)
    out = re.sub(r"\*([^*\n]+)\*", r"\1", out)
    return out


def blocks_to_text(doc: dict | None) -> str:
    blocks = (doc or {}).get("blocks") or []
    out: list[str] = []
    for block in blocks:
        kind = block.get("kind")
        text = str(block.get("text") or "")
        if kind == "heading":
            out.append("#" * _clamp_level(block.get("level", 1)) + " " + text)
        elif kind == "list":
            prefix = "1. " if block.get("ordered") else "- "
            out.extend(prefix + line for line in _text_lines(text))
        elif kind == "quote":
            out.append(text)
        elif kind == "code":
            out.append(text)
        elif kind == "link":
            href = str((block.get("attrs") or {}).get("href") or "")
            out.append((text + " <" + href + ">") if text else href)
        else:
            out.append(inline_plain(text))
    return "\n".join(out)


def card_text_fields(doc: dict) -> dict:
    safe = doc or empty_doc()
    return {
        "content": blocks_to_text(safe),
        "docText": serialize_doc_text(safe),
        "doc": safe,
    }


def normalize_doc(raw: Any) -> dict:
    """任何输入都返回合法 doc：损坏内容保留为段落并记 issues，绝不静默丢内容。"""
    issues: list[str] = []
    incoming: list = []
    if isinstance(raw, dict):
        if isinstance(raw.get("blocks"), list):
            incoming = list(raw["blocks"])
        elif raw:
            issues.append("内容结构无法识别，已按原文保留")
        if isinstance(raw.get("issues"), list):
            issues.extend(str(item) for item in raw["issues"])
    elif raw:
        issues.append("内容结构无法识别，已按原文保留")
    blocks: list[dict] = []
    for item in incoming:
        if not isinstance(item, dict) or not isinstance(item.get("kind"), str):
            blocks.append(new_block("paragraph", item if isinstance(item, str) else ""))
            # 结构损坏的项本身也是原文：能读出来的字符串照样保留
            issues.append("有一个内容块结构损坏，已按原文保留")
            continue
        kind = str(item["kind"])
        text = str(item.get("text") or "")
        if kind == "heading":
            block = new_block("heading", text, level=item.get("level", 1))
        elif kind == "list":
            block = new_block("list", text, ordered=bool(item.get("ordered")))
        elif kind == "quote":
            block = new_block("quote", text)
        elif kind == "code":
            attrs = item.get("attrs") or {}
            block = new_block("code", text, attrs={"language": attrs.get("language") or ""})
        elif kind == "link":
            attrs = item.get("attrs") or {}
            block = new_block("link", text, attrs={"href": attrs.get("href") or ""})
        else:
            block = new_block("paragraph", text)
            if kind != "paragraph":
                issues.append("有一个内容块的类型无法识别，已按原文保留为普通文字")
        if isinstance(item.get("id"), str) and item["id"]:
            block["id"] = item["id"]
        blocks.append(block)
    doc: dict = {"schema": TEXT_DOC_SCHEMA, "blocks": blocks}
    if issues:
        doc["issues"] = issues
    return doc


def is_text_doc(raw: Any) -> bool:
    return isinstance(raw, dict) and isinstance(raw.get("blocks"), list)


def doc_from_card(card: dict | None) -> dict:
    """文字卡 → 内容模型。meta.doc 是权威；meta.docText 次之；content 兜底。"""
    card = card or {}
    meta = card.get("meta") if isinstance(card.get("meta"), dict) else {}
    if is_text_doc(meta.get("doc")):
        return normalize_doc(meta["doc"])
    doc_text = meta.get("docText")
    if isinstance(doc_text, str) and doc_text:
        return parse_doc_text(doc_text)
    content = str(card.get("content") or "")
    if not content:
        return empty_doc()
    return parse_doc_text(content if content.endswith("\n") else content + "\n")


def new_text_card(content: str = "", **fields: Any) -> dict:
    """新建一张文字卡。默认未许可（checked=False），内容保存在 meta.doc / meta.docText。"""
    from . import models

    doc = parse_doc_text(content) if content else empty_doc()
    payload = card_text_fields(doc)
    card = models.new_card("text", payload["content"], **fields)
    meta = dict(card.get("meta") or {})
    meta["doc"] = payload["doc"]
    meta["docText"] = payload["docText"]
    card["meta"] = meta
    return card
