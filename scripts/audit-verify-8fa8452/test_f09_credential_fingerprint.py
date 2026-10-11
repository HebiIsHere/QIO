r"""E 路独立核验探针 · F09 旧凭据验证覆盖新配置
基线 8fa845255c118a7104b1de8e37298ca745924482

规格来源：D:\qio-dev\_audit-findings-8fa8452.md 的 F09 段 + 冻结契约 C5：
  - 验证开始时捕获完整输入指纹：密钥版本 + endpoint + default_model + kind；
  - set_verified(..., input_fingerprint=...) 条件写入：当前配置指纹 != 快照指纹 →
    不改变可用性 / 默认项 / 自动候选，只记录「历史结果」；
  - 临时网络错误不得永久禁用凭据。

「迟到的验证结果」的可观测后果有三个，本探针都覆盖：
  1) 数据库里的 verify_state（可用性的唯一事实源）；
  2) 自动候选 / 默认项（promote_default 会不会把这条挑成主对话凭据）；
  3) 界面文案（用 metadata 里的 reason/verify_error 区分「当前状态」与「历史结果」）。

反例设计（区分缺陷与修复的关键）：
  F09-a 旧配置**成功**晚到、新配置已经**失败** → 不得把新配置报成 verified。
  F09-b 旧配置**失败**晚到、新配置已经**成功** → 不得把新配置打成 failed（临时故障不永久禁用）。

运行（只跑本节点，禁止全量）：
  cd D:\qio-dev\qa-e\backend
  $env:PYTHONPATH = 'D:\qio-dev\qa-e\backend\src'
  & 'D:\qio-dev\qa-lead\backend\.venv\Scripts\python.exe' -m pytest ..\scripts\audit-verify-8fa8452\test_f09_credential_fingerprint.py -q -p no:cacheprovider -s

注：探针在 backend/tests 之外 → pyproject 的 pythonpath 不生效，必须显式设置 PYTHONPATH；
本探针自己建临时库 + MemoryKeyring，不依赖 backend/tests/conftest.py。
"""

from __future__ import annotations

import inspect
import os
from pathlib import Path

os.environ.pop("QIO_DATA_DIR", None)
os.environ.setdefault("QIO_DEV_INSECURE", "1")
os.environ.setdefault("QIO_DISABLE_DB_CHECK", "1")

import pytest  # noqa: E402

from agent.credentials.store import (  # noqa: E402
    CredentialStore,
    MemoryKeyring,
    VERIFY_STATE_FAILED,
    VERIFY_STATE_UNVERIFIED,
    VERIFY_STATE_VERIFIED,
)
from agent.storage.db import connect  # noqa: E402
from agent.storage.migrate import apply_migrations  # noqa: E402

KEY = "key_e_f09"


def _store(tmp_path: Path) -> CredentialStore:
    conn = connect(tmp_path / "test.db")
    apply_migrations(conn)
    return CredentialStore(conn, MemoryKeyring())


def _fingerprint_of(store: CredentialStore, key_id: str):
    """尽力取「输入指纹」：修复版若提供公开取指纹的方法就按它构造快照。

    基线没有这个概念 —— 这正是缺陷本身；返回 None 让调用退化成「没有指纹可核」。
    """
    for name in ("input_fingerprint", "fingerprint", "config_fingerprint"):
        getter = getattr(store, name, None)
        if callable(getter):
            try:
                return getter(key_id)
            except Exception:  # noqa: BLE001
                return None
    return None


def _set_verified(store: CredentialStore, key_id: str, ok: bool, reason: str | None, fingerprint) -> dict:
    """条件写入：修复版接受 input_fingerprint 关键字；基线只接受 (key_id, ok, reason)。"""
    params = inspect.signature(store.set_verified).parameters
    kwargs: dict = {}
    if fingerprint is not None:
        if "input_fingerprint" in params:
            kwargs["input_fingerprint"] = fingerprint
        elif "fingerprint" in params:
            kwargs["fingerprint"] = fingerprint
    return store.set_verified(key_id, ok, reason, **kwargs)


def _state(store: CredentialStore, key_id: str) -> str:
    meta = store.get_metadata(key_id) or {}
    return str(meta.get("verify_state") or VERIFY_STATE_UNVERIFIED)


def test_f09_a_late_success_from_old_config_must_not_verify_new_config(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create(
        KEY,
        "sk-old-0000000000000000",
        ["main-loop"],
        endpoint="https://old.example.com/v1",
        default_model="old-model",
    )
    # 用户在这个配置上开始验证：捕获指纹（修复版）
    snapshot = _fingerprint_of(store, KEY)
    print("\n[F09-a] 验证起点捕获到的指纹 =", snapshot)

    # 验证在飞行中，用户改了配置（新 secret 会推进 version 并复位验证状态），
    # 随后新配置的验证**失败**了。
    store.reconfigure(
        KEY,
        secret="sk-new-1111111111111111",
        endpoint="https://new.example.com/v1",
        default_model="new-model",
        confirm_reconfigure=True,
    )
    store.set_verified(KEY, False, "新配置连通性测试失败：401")
    before = _state(store, KEY)
    before_default = store.promote_default()
    print("[F09-a] 新配置验证失败后 state =", before, " promote_default =", before_default)
    assert before == VERIFY_STATE_FAILED, "前置条件：新配置的验证结论是失败"

    # 旧配置的成功结果姗姗来迟
    _set_verified(store, KEY, True, None, snapshot)
    after = _state(store, KEY)
    after_default = store.promote_default()
    meta = store.get_metadata(KEY) or {}
    print("[F09-a] 旧成功晚到后 state =", after, " promote_default =", after_default)
    print("[F09-a] metadata.verify_state/verify_error =", meta.get("verify_state"), "/", meta.get("verify_error"))

    assert after == VERIFY_STATE_FAILED, (
        "F09 缺陷复现：旧配置的成功验证结果晚到，把**已经失败的新配置**覆盖成了 verified"
        "（set_verified 无条件按 key_id 写，没有任何输入指纹校验）。实际 state=" + after
    )
    assert after_default is None, (
        "F09 缺陷复现：旧配置的迟到成功让这条凭据进入了自动候选/默认项，而当前配置其实没通过验证。"
        "实际 promote_default=" + str(after_default)
    )


def test_f09_b_late_failure_from_old_config_must_not_break_new_config(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create(
        KEY,
        "sk-old-2222222222222222",
        ["main-loop"],
        endpoint="https://old.example.com/v1",
        default_model="old-model",
    )
    snapshot = _fingerprint_of(store, KEY)
    print("\n[F09-b] 验证起点捕获到的指纹 =", snapshot)

    store.reconfigure(
        KEY,
        secret="sk-new-3333333333333333",
        endpoint="https://new.example.com/v1",
        default_model="new-model",
        confirm_reconfigure=True,
    )
    store.set_verified(KEY, True, None)
    before = _state(store, KEY)
    before_default = store.promote_default()
    print("[F09-b] 新配置验证成功后 state =", before, " promote_default =", before_default)

    # 旧配置的失败结果姗姗来迟（例如当时的临时网络错误）
    _set_verified(store, KEY, False, "旧配置的临时网络错误：connection reset", snapshot)
    after = _state(store, KEY)
    print("[F09-b] 旧失败晚到后 state =", after, " promote_default =", store.promote_default())

    assert after == VERIFY_STATE_VERIFIED, (
        "F09 缺陷复现：旧配置的失败验证结果晚到，把**已经通过验证的新配置**打成了 failed"
        "（旧配置的临时网络错误永久禁用了新配置）。实际 state=" + after
    )


def test_f09_meta_declares_fingerprint_api(tmp_path: Path) -> None:
    """事实记录：基线 set_verified 的签名里没有任何输入指纹参数。"""
    store = _store(tmp_path)
    params = list(inspect.signature(store.set_verified).parameters)
    print("\n[F09-meta] set_verified 参数 =", params)
    print("[F09-meta] 有指纹参数 =", any("fingerprint" in p for p in params))
    print("[F09-meta] 指纹读取方法 =", _fingerprint_of(store, "nope"))
