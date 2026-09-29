# -*- coding: utf-8 -*-
"""真实语料层：只测离线可确定的部分。

真实模型调用不进 pytest（仓库约定：测试不得以真实 API Key 或联网为前提），
所以这里守的是筛选、去重、缓存幂等与预算守卫 —— 它们错了会让整套实验数字失真或超支。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.eval import real_corpus
from agent.eval.real_corpus import BudgetExceeded, BudgetGuard, read_jsonl, select_docs, write_jsonl


def test_select_docs_filters_by_length_and_dedups():
    rows = [
        ("标题A", "太短了"),
        ("标题B", "文" * 500),
        ("标题B", "文" * 500),
        ("标题C", "文" * 2500),
        ("标题D", "好" * 800),
    ]
    docs = select_docs(rows, n=10, min_len=400, max_len=2000)
    assert [d.title for d in docs] == ["标题B", "标题D"]


def test_select_docs_respects_limit():
    rows = [(f"T{i}", "文" * 500) for i in range(10)]
    assert len(select_docs(rows, n=3)) == 3


def test_budget_guard_stops_at_threshold_and_reports_spend():
    guard = BudgetGuard(max_usd=1.0, stop_at=0.9)
    guard.add(0.5)
    guard.add(0.3)
    assert guard.exhausted is False
    assert guard.spent == pytest.approx(0.8)
    with pytest.raises(BudgetExceeded):
        guard.add(0.2)
    assert guard.exhausted is True


def test_jsonl_roundtrip_is_stable(tmp_path: Path):
    path = tmp_path / "docs.jsonl"
    rows = [{"title": "甲", "text": "内容"}, {"title": "乙", "text": "内容2"}]
    write_jsonl(path, rows)
    assert read_jsonl(path) == rows
    write_jsonl(path, rows)
    assert len(path.read_text(encoding="utf-8").strip().splitlines()) == 2


def test_load_docs_uses_cache_without_network(tmp_path: Path, monkeypatch):
    cache = tmp_path / "wiki_docs.jsonl"
    write_jsonl(
        cache,
        [{"title": f"T{i}", "text": "文" * 500} for i in range(5)],
    )

    def _boom(*args, **kwargs):
        raise AssertionError("命中缓存时不应再联网")

    monkeypatch.setattr(real_corpus, "download_wiki_shard", _boom)
    docs = real_corpus.load_wiki_docs(3, cache_dir=tmp_path)
    assert len(docs) == 3
    assert docs[0].title == "T0"
    assert json.loads(json.dumps({"ok": True}))["ok"] is True
