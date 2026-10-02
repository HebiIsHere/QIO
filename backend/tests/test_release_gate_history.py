# -*- coding: utf-8 -*-
"""发布闸门的历史落库与回归比较（合成产物，不联网、不安装）。

回归的缺口（2026-10-02 之前）：闸门有 --json，但**没人保存结果**，也没有「这次比上次
好还是差」——一次发布闸门跑完就没了，退化只能靠人回忆。

这里用合成产物跑**真 CLI**（子进程），断言：

* 一次判定后历史里多一条，且字段能对上（时间/commit/版本/逐项状态/安装包 sha256）；
* 第二次判定能明确指出「新失败」是哪一项（而不是只说「1 项不通过」）；
* --history 能把历史读回来；
* 历史文件写不进去时**判定不受影响**（只警告）——判定只能由真实产物决定。
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
GATE = REPO / 'scripts' / 'release_gate.py'
INSTALLER = 'QIO_9.9.9_x64-setup.exe'


def _gate_module():
    """scripts/ 不是包：用显式路径加载（只取 build_fixture，不触发任何副作用）。

    必须先登记进 sys.modules：脚本里用了 `from __future__ import annotations`，
    dataclass 解析字符串注解时要能按 __module__ 找到模块，否则 AttributeError。
    """
    spec = importlib.util.spec_from_file_location('qio_release_gate', GATE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _run_gate(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(GATE), *args],
        capture_output=True, text=True, encoding='utf-8', errors='replace',
        cwd=str(REPO), timeout=300,
    )


def _history(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]


def _gate_args(fixture: Path, history: Path) -> list[str]:
    return [
        '--repo', str(fixture),
        '--dist', str(fixture / 'dist'),
        '--installer', str(fixture / 'dist' / INSTALLER),
        '--history-file', str(history),
    ]


def test_each_run_appends_a_traceable_record(tmp_path):
    gate = _gate_module()
    fixture = tmp_path / 'fixture'
    gate.build_fixture(fixture)
    history = tmp_path / 'history.jsonl'

    first = _run_gate(*_gate_args(fixture, history))
    assert first.returncode == 0, first.stdout + first.stderr

    records = _history(history)
    assert len(records) == 1
    record = records[0]
    assert record['schema'] == 1
    assert record['version'] == '9.9.9'
    assert record['installer'] == INSTALLER
    assert record['installer_sha256'] == gate.sha256_of(fixture / 'dist' / INSTALLER)
    assert record['results']['installer.sha256'] == 'PASS'
    assert record['results']['sha256sums'] == 'PASS'
    assert record['failed'] == []
    assert record['counts']['PASS'] >= 10
    assert record['ts'] and record['commit'] is not None


def test_second_run_reports_what_newly_failed(tmp_path):
    gate = _gate_module()
    fixture = tmp_path / 'fixture'
    gate.build_fixture(fixture)
    history = tmp_path / 'history.jsonl'
    assert _run_gate(*_gate_args(fixture, history)).returncode == 0

    # 只改一件事：让 SHA256SUMS.txt 记一个错的哈希（真实事故形状：发布目录被换过）
    (fixture / 'dist' / 'SHA256SUMS.txt').write_text(
        f'{INSTALLER}  ' + '0' * 64 + '\n', encoding='utf-8'
    )
    second = _run_gate(*_gate_args(fixture, history))
    assert second.returncode == 1
    assert 'sha256sums' in second.stdout
    assert '新失败' in second.stdout, second.stdout
    assert 'sha256sums' in second.stdout.split('新失败', 1)[1].split('\n')[0]

    records = _history(history)
    assert len(records) == 2
    assert records[0]['results']['sha256sums'] == 'PASS'
    assert records[1]['results']['sha256sums'] == 'FAIL'
    diff = gate.compare_records(records[0], records[1])
    assert diff['newly_failed'] == ['sha256sums']
    assert diff['newly_passed'] == []


def test_history_command_reads_back_the_records(tmp_path):
    gate = _gate_module()
    fixture = tmp_path / 'fixture'
    gate.build_fixture(fixture)
    history = tmp_path / 'history.jsonl'
    assert _run_gate(*_gate_args(fixture, history)).returncode == 0
    assert _run_gate(*_gate_args(fixture, history)).returncode == 0

    shown = _run_gate('--history-file', str(history), '--history')
    assert shown.returncode == 0, shown.stdout + shown.stderr
    assert '发布闸门历史' in shown.stdout
    assert shown.stdout.count('9.9.9') >= 2  # 两条记录都在
    assert '与上一条比较' in shown.stdout


def test_history_command_on_an_empty_file_says_so(tmp_path):
    empty = tmp_path / 'empty.jsonl'
    empty.write_text('', encoding='utf-8')
    shown = _run_gate('--history-file', str(empty), '--history')
    assert shown.returncode == 0
    assert '没有历史记录' in shown.stdout


def test_an_unwritable_history_does_not_change_the_verdict(tmp_path):
    gate = _gate_module()
    fixture = tmp_path / 'fixture'
    gate.build_fixture(fixture)
    blocker = tmp_path / 'blocker'
    blocker.write_text('not a directory', encoding='utf-8')

    proc = _run_gate(*_gate_args(fixture, blocker / 'history.jsonl'))
    assert proc.returncode == 0, '历史写不进去不该改变判定'
    assert '[warn]' in proc.stderr
    assert not (blocker / 'history.jsonl').exists()


def test_report_survives_a_non_utf8_console(tmp_path):
    """英文 Windows（cp1252）上报告全是中文：报告层必须降级，不能崩在第一条 print。

    CI 的 windows-latest 抓到过这件事：闸门以 UnicodeEncodeError 收场，判定没跑完，
    历史里当然也没有记录 —— 一个「跑不完」的发布闸门等于没有闸门。
    """
    gate = _gate_module()
    fixture = tmp_path / 'fixture'
    gate.build_fixture(fixture)
    history = tmp_path / 'history.jsonl'
    proc = subprocess.run(
        [sys.executable, str(GATE), *_gate_args(fixture, history)],
        capture_output=True, cwd=str(REPO), timeout=300,
        env={**os.environ, 'PYTHONIOENCODING': 'cp1252', 'PYTHONUTF8': '0'},
    )
    out = proc.stdout.decode('utf-8', 'replace')  # 重定向时报告按 UTF-8 写字节
    err = proc.stderr.decode('utf-8', 'replace')
    assert proc.returncode == 0, out + err
    assert '发布闸门' in out, out
    assert len(_history(history)) == 1


def test_default_history_lives_in_the_repo_not_in_dist():
    """历史是发布决策的一部分：跟 docs/releases 的发布说明放在一起（dist 会被清掉）。"""
    gate = _gate_module()
    assert gate.DEFAULT_HISTORY == REPO / 'docs' / 'releases' / 'release-history.jsonl'
    assert gate.DEFAULT_HISTORY.parent.is_dir(), '默认历史目录必须存在（否则第一次写入会静默失败）'
    assert gate.DEFAULT_HISTORY.name == 'release-history.jsonl'
