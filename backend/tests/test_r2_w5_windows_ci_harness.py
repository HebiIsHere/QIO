"""W5 装置回归：Windows 后端 CI 两类缺陷的确定性反例（基线应红）。

本文件只验证**测试装置**（不碰产品代码），对应 run 38033354514 的 6 条红：

1. 子进程编码：run_probe() 的父进程用 locale 默认编码解码、子进程继承 runner 默认
   stdio 编码（windows-latest = cp1252），而探针用 ensure_ascii=False 打印中文 →
   子进程 UnicodeEncodeError（F01/F02 的 4 条红）。
   复现手法：把环境里的 PYTHONIOENCODING 置成 cp1252（runner 同款），装置必须自己
   明确子进程 UTF-8、父进程按 UTF-8 解码，不得依赖运行环境。
2. 拒读/恢复装置：/inheritance:r + /grant:r + /deny 会让 icacls 把 grant 改写成
   去掉 R 的 (W,D,WDAC,WO,X,DC)；恢复时只 /remove:d 仍读不了，而"再 grant 回来"在部分
   runner 上没有生效（check=False 吞掉）→ F17/F18 的 2 条红。
   复现手法（受控、无 sleep）：
   * icacls <file> /reset 造出与 CI tmp 文件同形的「纯继承 ACL」；
   * 把 /grant:r 打成「静默不生效」（返回码 0、ACL 不变），等价于 runner 上的实测现象。
   基线装置依赖 /grant:r 恢复 → 恢复后仍不可读。
   另外冻结三条硬要求：不动继承、恢复只做 /remove:d、每次 icacls 都检查返回码并把
   stdout/stderr 带进错误、恢复要复核可读且失败必须大声失败。

运行：cd backend; uv run --frozen --extra dev pytest tests/test_r2_w5_windows_ci_harness.py -q
"""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
import types
from pathlib import Path

import pytest

from _acc_a_probe import run_probe

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

# 四个带拒读装置的文件（契约 W5：f17/f18 修，f15/fb_a_r2 一并修）
DEVICE_MODULES = (
    "test_acc_e_f17_readability",
    "test_acc_e_f18_reference_retry",
    "test_acc_e_f15_binding_boundary",
    "test_fb_a_r2_group_recheck",
)

# 装置对外必须提供的表面（不绑定内部有没有 _icacls 这个中间层）
HELPER_NAMES = ("_whoami", "_readable", "_deny_read", "_allow_read")


def _load_device_module(name: str):
    module = importlib.import_module(name)
    missing = [attr for attr in HELPER_NAMES if not hasattr(module, attr)]
    assert not missing, (
        f"{name} 缺少加固后的拒读装置 helper {missing}："
        "四个装置必须共用同一套「不动继承、恢复只 /remove:d、复核可读、检查返回码」的实现"
    )
    return module


def _readable(path: Path) -> bool:
    try:
        with open(path, "rb") as handle:
            handle.read(1)
    except OSError:
        return False
    return True


def _used(calls: list, flag: str) -> bool:
    """某个 icacls 开关是否真的出现在调用里（子串匹配，不是列表精确成员）。"""
    return any(flag in part for call in calls for part in call)


def _real_icacls(path: Path, *args: str, allow_failure: bool = False) -> None:
    """测试自身用的真 icacls（不受被测模块 module.subprocess 补丁影响）。"""
    done = subprocess.run(["icacls", str(path), *args], capture_output=True)
    if done.returncode != 0 and not allow_failure:
        raise AssertionError(
            "测试装置自身 icacls 调用失败："
            f"{args} rc={done.returncode} out={done.stdout!r} err={done.stderr!r}"
        )


def _patch_icacls(
    monkeypatch,
    module,
    *,
    ineffective: frozenset = frozenset(),
    failing: frozenset = frozenset(),
):
    """给被测模块换一个受控的 subprocess.run（只拦 icacls，其余委托真身）。

    * ineffective 里的开关：**静默不生效**（返回码 0，却一个 ACL 都不改）；
    * failing 里的开关：返回码 5，并带 MARKER stdout/stderr（模拟 icacls 真的失败）。
    """
    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        parts = [str(part) for part in cmd] if isinstance(cmd, (list, tuple)) else []
        if parts and parts[0].lower() == "icacls":
            if any(flag in parts for flag in failing):
                out, err = b"MARKER-ICACLS-OUT", b"MARKER-ICACLS-ERR"
                if kwargs.get("check"):
                    raise subprocess.CalledProcessError(5, cmd, output=out, stderr=err)
                return subprocess.CompletedProcess(cmd, 5, out, err)
            if any(flag in parts for flag in ineffective):
                out, err = b"INERT-ICACLS-OUT", b"INERT-ICACLS-ERR"
                return subprocess.CompletedProcess(cmd, 0, out, err)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(module, "subprocess", types.SimpleNamespace(run=fake_run))


# ---- 1. 子进程编码：不得依赖 runner 默认（windows-latest = cp1252） -------------


def test_probe_is_utf8_when_ambient_stdio_encoding_is_cp1252(tmp_path: Path, monkeypatch):
    source = tmp_path / "中文探针.txt"
    source.write_text("中文内容第一行\n中文内容第二行\n", encoding="utf-8", newline="")
    # windows-latest 的默认 stdio 编码：探针输出（含中文原因与内容）若继承它就崩
    monkeypatch.setenv("PYTHONIOENCODING", "cp1252")
    monkeypatch.delenv("PYTHONUTF8", raising=False)

    result = run_probe(tmp_path, source, "中文探针.txt", offset=0, limit=2)

    assert result["ok"] is True, result["error"]
    payload = json.loads(result["content"])
    assert "中文内容第一行" in payload["content"], payload["content"]
    assert "\ufffd" not in payload["content"], "父进程不能把中文解成替换字符（这才是诚实交付）"


# ---- 2. 装置形状：不动继承、恢复只 /remove:d、复核可读 --------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ACL 拒读装置")
@pytest.mark.parametrize("module_name", DEVICE_MODULES)
def test_deny_does_not_touch_inheritance_and_allow_only_removes_deny(
    module_name: str, tmp_path: Path, monkeypatch
):
    module = _load_device_module(module_name)
    path = tmp_path / "dev.txt"
    path.write_bytes(b"device")
    calls: list = []
    real_run = subprocess.run

    def recording_run(cmd, *args, **kwargs):
        parts = [str(part) for part in cmd] if isinstance(cmd, (list, tuple)) else []
        if parts and parts[0].lower() == "icacls":
            calls.append(parts)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(module, "subprocess", types.SimpleNamespace(run=recording_run))

    module._deny_read(path)
    deny_calls = list(calls)
    calls.clear()
    assert not _readable(path), "装置自证：/deny 之后必须真的读不了"
    assert _used(deny_calls, "/deny"), deny_calls
    assert not _used(deny_calls, "/inheritance"), (
        "制造拒读不得动继承：/inheritance:r 会让恢复变成「再 grant 回来」，CI 上就是这么红的",
        deny_calls,
    )

    module._allow_read(path)
    allow_calls = list(calls)
    assert _readable(path), "装置自证：恢复之后必须可读"
    assert _used(allow_calls, "/remove:d"), allow_calls
    assert not _used(allow_calls, "/grant"), (
        "恢复不得依赖 /grant:r（部分 runner 上它没有把 R 加回来，check=False 还会吞掉失败）",
        allow_calls,
    )
    assert not _used(allow_calls, "/inheritance"), allow_calls


# ---- 3. CI 实况：/grant:r 静默不生效时，恢复仍必须可读 ---------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ACL 拒读装置")
@pytest.mark.parametrize("module_name", DEVICE_MODULES)
def test_restore_survives_ineffective_regrant(module_name: str, tmp_path: Path, monkeypatch):
    module = _load_device_module(module_name)
    path = tmp_path / "dev.txt"
    path.write_bytes(b"device")
    _real_icacls(path, "/reset")  # 与 CI tmp 文件同形：纯继承 ACL
    _patch_icacls(monkeypatch, module, ineffective=frozenset({"/grant:r"}))
    try:
        module._deny_read(path)
        assert not _readable(path), "装置自证：这一刻引用真的读不了"
        module._allow_read(path)
        restored = _readable(path)  # 必须在 finally 清理之前取值，否则清理会掩盖失败
    finally:
        _real_icacls(path, "/reset", allow_failure=True)

    assert restored, "装置自证：/grant:r 静默不生效（runner 实测）时，恢复之后仍必须可读"


# ---- 4. 恢复不了必须大声失败（带 icacls 输出），绝不静默放过 ---------------------


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ACL 拒读装置")
@pytest.mark.parametrize("module_name", DEVICE_MODULES)
def test_unrestorable_deny_fails_loudly(module_name: str, tmp_path: Path, monkeypatch):
    module = _load_device_module(module_name)
    path = tmp_path / "dev.txt"
    path.write_bytes(b"device")
    _real_icacls(path, "/reset")
    user = module._whoami()
    _real_icacls(path, "/deny", f"{user}:(RD)")
    assert not _readable(path)
    # 恢复路径（/remove:d、/reset、/grant:r）全部静默不生效 → 必须抛错而不是放过
    _patch_icacls(monkeypatch, module, ineffective=frozenset({"/remove:d", "/reset", "/grant:r"}))
    try:
        with pytest.raises(AssertionError) as excinfo:
            module._allow_read(path)
    finally:
        _real_icacls(path, "/reset", allow_failure=True)

    message = str(excinfo.value)
    assert "icacls" in message, message
    assert path.name in message or str(path) in message, message


# ---- 5. 每次 icacls 调用的返回码都必须检查，并把输出带进错误 ---------------------


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ACL 拒读装置")
@pytest.mark.parametrize("module_name", DEVICE_MODULES)
def test_icacls_failure_carries_exit_code_and_output(module_name: str, tmp_path: Path, monkeypatch):
    module = _load_device_module(module_name)
    path = tmp_path / "dev.txt"
    path.write_bytes(b"device")
    _real_icacls(path, "/reset")
    _patch_icacls(monkeypatch, module, failing=frozenset({"/deny"}))
    try:
        with pytest.raises(AssertionError) as excinfo:
            module._deny_read(path)
    finally:
        _real_icacls(path, "/reset", allow_failure=True)

    message = str(excinfo.value)
    assert "MARKER-ICACLS-OUT" in message and "MARKER-ICACLS-ERR" in message, (
        "icacls 失败的 stdout/stderr 必须进错误信息，否则装置故障无法诊断",
        message,
    )
    assert "5" in message, message
