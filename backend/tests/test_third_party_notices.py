# -*- coding: utf-8 -*-
"""第三方组件声明覆盖率检查器自身的回归。

为什么值得单独测：这份检查是「装进安装包的第三方代码一定带着许可证与版权声明」的**唯一执行者**。
它自己坏掉是静默的 —— 匹配不到就当作没问题，于是声明可以永远停在旧版本，而 CI 依然全绿。
真实缺口形状（2026-10-17 本轮之前）：仓库里根本没有 `LICENSE` 与 `THIRD-PARTY-NOTICES.md`，
安装包里也只有内置模型那一份声明。

这里做三件事：
1. 用真实仓库跑一遍完整校验（少一个组件、正文与清单脱钩都当场红）；
2. 跑检查器自带的 `--selftest`，证明每一项检查**既会绿也会红**，而不是永远返回「没问题」；
3. 端到端反例：锁文件多了一个组件而清单没跟上，必须被点名。
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CHECKER = REPO / 'scripts' / 'licenses' / 'check_third_party.py'


def _module():
    spec = importlib.util.spec_from_file_location('qio_check_third_party', CHECKER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_repository_notices_are_complete():
    """当前仓库必须通过：锁文件里的组件一个不漏，许可证全文与声明正文同步。"""
    check = _module()
    problems = check.run_checks(REPO)
    assert problems == [], '第三方声明不合规：' + '；'.join(problems)


def test_selftest_proves_every_check_can_fail():
    """自检必须过：证明这些检查真的会把坏数据判红，而不是永远说「没问题」。"""
    check = _module()
    assert check.selftest() == 0


def test_a_missing_component_is_reported():
    """端到端反例：锁文件多了一个组件、清单没跟上 —— 必须点名它。"""
    check = _module()
    root = Path(tempfile.mkdtemp(prefix='qio-third-party-'))
    try:
        check._write_fixture(root)
        uv_lock = root / check.UV_LOCK
        uv_lock.write_text(
            uv_lock.read_text(encoding='utf-8') + '[[package]]' + chr(10) + 'name = "delta"' + chr(10) + 'version = "4.0.0"' + chr(10),
            encoding='utf-8',
        )
        problems = check.run_checks(root)
        assert any('delta@4.0.0' in problem for problem in problems), problems
    finally:
        shutil.rmtree(root, ignore_errors=True)
