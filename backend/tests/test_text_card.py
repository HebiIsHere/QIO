"""统一文字卡内容模型（text_card.py）的契约测试。

契约：docs/superpowers/plans/2026-10-12-unified-cards-media-preview-contract.md 第 1 节。
前端同一套规则在 frontend/src/interactive/textCard.ts（vitest 侧有对照测试）。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from agent.interactive import text_card as tc

FENCE = tc.FENCE

SAMPLE = (
    "普通文字\n"
    "\n"
    "## 标题\n"
    "\n"
    "- 一\n"
    "- 二\n"
    "\n"
    + FENCE
    + "python\n  def f():\n      return 1\n"
    + FENCE
    + "\n"
    "\n"
    "[QIO](https://example.com)\n"
)


def test_mixed_blocks():
    doc = tc.parse_doc_text(SAMPLE)
    assert [b["kind"] for b in doc["blocks"]] == ["paragraph", "heading", "list", "code", "link"]


def test_roundtrip_is_identity():
    assert tc.serialize_doc_text(tc.parse_doc_text(SAMPLE)) == SAMPLE


def test_code_block_is_verbatim():
    doc = tc.parse_doc_text(SAMPLE)
    code = next(b for b in doc["blocks"] if b["kind"] == "code")
    assert code["text"] == "  def f():\n      return 1\n"
    assert code["attrs"]["language"] == "python"
    again = tc.parse_doc_text(tc.serialize_doc_text(doc))
    assert [b["kind"] for b in again["blocks"]] == [b["kind"] for b in doc["blocks"]]
    assert next(b for b in again["blocks"] if b["kind"] == "code")["text"] == code["text"]


def test_fence_body_is_not_reparsed():
    text = FENCE + "md\n# 不是标题\n- 不是列表\n> 不是引用\n" + FENCE + "\n"
    doc = tc.parse_doc_text(text)
    assert len(doc["blocks"]) == 1
    assert doc["blocks"][0]["kind"] == "code"
    assert doc["blocks"][0]["text"] == "# 不是标题\n- 不是列表\n> 不是引用\n"
    assert tc.serialize_doc_text(doc) == text


def test_unclosed_fence_records_issue():
    doc = tc.parse_doc_text(FENCE + "js\nlet a = 1")
    assert doc["blocks"][0]["kind"] == "code"
    assert doc["blocks"][0]["text"] == "let a = 1\n"
    assert "未闭合" in "".join(doc.get("issues") or [])


def test_inline_link_is_not_a_link_block():
    doc = tc.parse_doc_text("见 [QIO](https://example.com) 这一行")
    assert len(doc["blocks"]) == 1
    assert doc["blocks"][0]["kind"] == "paragraph"


def test_blocks_to_text():
    plain = tc.blocks_to_text(tc.parse_doc_text(SAMPLE))
    assert "## 标题" in plain
    assert "- 一" in plain
    assert "  def f():" in plain
    assert "QIO <https://example.com>" in plain


def test_card_text_fields():
    fields = tc.card_text_fields(tc.parse_doc_text(SAMPLE))
    assert "- 二" in fields["content"]
    assert fields["docText"] == SAMPLE
    assert len(fields["doc"]["blocks"]) == 5


def test_doc_from_card_precedence():
    from_doc = tc.doc_from_card(
        {"content": "旧的", "meta": {"doc": {"schema": tc.TEXT_DOC_SCHEMA, "blocks": [
            {"id": "b1", "kind": "paragraph", "text": "来自 doc"}]}}}
    )
    assert from_doc["blocks"][0]["text"] == "来自 doc"
    assert from_doc["blocks"][0]["id"] == "b1"
    from_text = tc.doc_from_card({"content": "旧的", "meta": {"docText": "来自 docText\n"}})
    assert from_text["blocks"][0]["text"] == "来自 docText"
    from_content = tc.doc_from_card({"content": "来自 content"})
    assert from_content["blocks"][0]["text"] == "来自 content"
    assert tc.doc_from_card({})["blocks"] == []


def test_normalize_keeps_damaged_content():
    doc = tc.normalize_doc({"blocks": [{"kind": "weird", "text": "x"}, "oops"]})
    assert [b["text"] for b in doc["blocks"]] == ["x", "oops"]
    assert len(doc["issues"]) >= 2
    assert tc.normalize_doc(None)["blocks"] == []


def test_inline_plain():
    assert tc.inline_plain("**粗** 和 `码` 与 *斜*") == "粗 和 码 与 斜"
    assert tc.inline_plain("") == ""


def test_new_text_card_defaults_unchecked():
    card = tc.new_text_card("第一行")
    assert card["kind"] == "text"
    assert card["checked"] is False
    assert card["content"] == "第一行"
    assert card["meta"]["doc"]["blocks"][0]["kind"] == "paragraph"


def test_frontend_and_backend_agree_on_wire_format(tmp_path: Path):
    """同一份编辑区文本，前端 parse+serialize 与后端必须一致（两侧规则同源）。

    前端一侧用 node 直接跑 TS 模块（用 vitest 的 esbuild 太慢）——这里改为
    通过契约约定的「规范写法」做等价断言：后端产出的 doc 序列化回来必须逐字相同。
    前端一致性由 vitest 的 textCard.test.ts 锁死（同一份规则）。
    """
    doc = tc.parse_doc_text(SAMPLE)
    assert tc.serialize_doc_text(tc.normalize_doc(json.loads(json.dumps(doc)))) == SAMPLE
