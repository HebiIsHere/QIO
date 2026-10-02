# -*- coding: utf-8 -*-
"""文档一致性检查器自身的回归（扩展名匹配顺序 + 整份检查必须通过）。

为什么值得单独测：`check_docs.py` 是「文档引用的路径/命令必须真实存在」这条纪律的唯一
执行者，它自己坏掉是**静默**的 —— 匹配不到就当作没问题。真实事故形状（2026-10-02）：
扩展名候选写成 `json|jsonl`，正则引擎先匹配 `json`，于是 `x.jsonl` 被截成 `x.json`，
所有 `.jsonl` 路径都被报成「不存在」。是 `docs/longterm-testing.md` 把这条暴露出来的。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CHECK_DOCS = REPO / 'scripts' / 'check_docs.py'


def _module():
    spec = importlib.util.spec_from_file_location('qio_check_docs', CHECK_DOCS)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_path_like_keeps_the_full_extension():
    """`.jsonl` 不能被 `json` 前缀截断，否则它永远被报成不存在。"""
    check = _module()
    text = (
        '见 `backend/evals/topic_threshold/cases.jsonl` 与 '
        '`docs/releases/release-history.jsonl`'
    )
    assert check.PATH_LIKE.findall(text) == [
        'backend/evals/topic_threshold/cases.jsonl',
        'docs/releases/release-history.jsonl',
    ]


def test_path_like_still_matches_the_other_extensions():
    check = _module()
    for token in (
        'backend/evals/baseline.json',
        'scripts/release_gate.py',
        'docs/architecture.md',
        'frontend/src-tauri/tauri.conf.json',
    ):
        assert check.PATH_LIKE.findall(f'`{token}`') == [token], token


def test_script_like_matches_script_paths():
    check = _module()
    assert check.SCRIPT_LIKE.findall('`scripts/frozen_worker_smoke.py`') == [
        'scripts/frozen_worker_smoke.py'
    ]


def test_the_repository_docs_are_consistent():
    """整份检查必须通过：引用不存在的路径/命令、里程碑矛盾、会过期的硬编码数字都要当场红。"""
    check = _module()
    assert check.main() == 0
