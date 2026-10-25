"""QIO 内部运行数据：受保护路径与「连续三次确认」（契约 §4）。

范围：%APPDATA%/qio 下的数据库、内部配置、凭据存储、实例注册表等（由 AppContext
提供路径清单）。正常应用功能（设置保存、记忆整理、审批落库等）走各自受控接口，
不经过文件工具边界，不要求三次确认；只有**工具/脚本/自建工具/子智能体直接以
文件方式维护内部数据**时才要求：

1. 审批单写明维护内容与范围；
2. 鲜明警告；
3. 用户连续三次确认（后端 CAS：confirmed_stage 0->1->2->3），每次确认不新开请求；
4. 任一步取消即停止；对象/内容/范围变化 -> 重置为 0 并重新确认；
5. 确认状态持久化，后端校验。

适用于全部模式（含 unrestricted），覆盖脚本/自建工具/子智能体。

R9-H4（契约 §8）：内部数据的识别依据是「真实注册路径 + 真实对象身份 + 生命周期」。
文件真正删除后卷序列号/file_id 可能被新文件复用，所以只凭 identity 不够 ——
调用方可以传 `identity_lookup`（如 InternalDataIdentityStore.match_identity），
由登记层同时核对 birth_key。本模块不自己猜生命周期：
* same → 是内部对象；
* different（编号复用）→ 不是旧内部对象；
* uncertain / 读取失败 → 按路径与历史保守处理，并明确按「不确定」对待；
* 旧的 identity_keys 纯键集合继续兼容（旧调用方/旧库 birth 未知时保守保护）。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterable

from agent.security.effects import Effect, EffectKind
from agent.services import file_identity

#: 内部运行数据必须连续确认的次数（0 -> 1 -> 2 -> 3）。
CONFIRMATIONS_REQUIRED = 3

#: 鲜明警告（前端只呈现，后端校验状态）。
INTERNAL_DATA_WARNING = "这是 QIO 内部运行数据，直接改写可能损坏应用，请确认你知道自己在做什么"

# 重要（R9-H 契约 §5）：这里**没有**按目录名/文件名/后缀判定敏感的词表。
# 普通 .key/.pem/.crt、普通 credentials/.config 同名目录不是内部数据；只有 AppContext
# 真实注册的路径（+ 路径规范化）才受内部运行数据规则保护。名称/关键词/正则一律
# 不得单独产生允许/拒绝/确认。


def normalize_path(path: object) -> Path | None:
    text = str(path or "").strip()
    if not text:
        return None
    try:
        return Path(text).expanduser().resolve()
    except (OSError, ValueError):  # pragma: no cover - 极端输入
        return None


def protected_list(paths: list[object] | tuple[object, ...] | None) -> list[Path]:
    out: list[Path] = []
    for raw in paths or ():
        candidate = normalize_path(raw)
        if candidate is not None:
            out.append(candidate)
    return out


def is_internal_data_path(path: object, protected: list[Path] | None) -> bool:
    """path 是否命中 AppContext 提供的内部运行数据路径（含子路径）。"""
    candidate = normalize_path(path)
    if candidate is None:
        return False
    for root in protected or ():
        try:
            if candidate == root or candidate.is_relative_to(root):
                return True
        except (OSError, ValueError):  # pragma: no cover
            continue
    return False


def _identity_key_of(path: object, identity_of: Any = None) -> str | None:
    """路径当前身份键；身份读不到 / 文件系统不支持 → None（不猜）。"""
    finder = identity_of if callable(identity_of) else file_identity.identity_of
    try:
        found = finder(path)
    except Exception:  # noqa: BLE001 - 身份读取失败不能把对象当普通文件
        return None
    if found is None:
        return None
    return str(getattr(found, "key", "") or "") or None


def _identity_object_of(path: object, identity_of: Any = None) -> Any:
    """读取路径当前 FileIdentity（读不到/不支持 → None，不猜）。"""
    finder = identity_of if callable(identity_of) else file_identity.identity_of
    try:
        return finder(path)
    except Exception:  # noqa: BLE001 - 读失败按不可确认处理（调用方决定保守方向）
        return None


def _identity_from_target_value(value: object) -> Any:
    """把 Target.identity（key 字符串 / token / dict）还原成 FileIdentity 形态。

    返回的对象只需要有 .key/.birth_key 属性；无法解析返回 None。
    保守：stat: 回退指纹不还原成权威身份。
    """
    if value is None:
        return None
    if isinstance(value, dict):
        volume = str(value.get("volume") or "")
        file_id = str(value.get("file_id") or "")
        if not volume or not file_id:
            return None
        from agent.services.file_identity import FileIdentity

        return FileIdentity(
            volume=volume,
            file_id=file_id,
            path=str(value.get("path") or ""),
            size=int(value.get("size") or 0),
            mtime_ns=int(value.get("mtime_ns") or 0),
            birth_key=str(value.get("birth_key") or "") or None,
            link_count=value.get("link_count"),
        )
    text = str(value or "").strip()
    if not text or text.startswith("stat:"):
        return None
    birth: str | None = None
    key = text
    if "#" in text:
        key, _, raw_birth = text.partition("#")
        birth = raw_birth or None
    if ":" not in key:
        return None
    volume, file_id = key.split(":", 1)
    if not volume or not file_id:
        return None
    from agent.services.file_identity import FileIdentity

    return FileIdentity(
        volume=volume,
        file_id=file_id,
        path="",
        size=0,
        mtime_ns=0,
        birth_key=birth,
    )


def _token_of(identity: Any) -> str | None:
    try:
        return file_identity.lifecycle_token(identity)
    except Exception:  # noqa: BLE001 - 生命周期依据不可得按未知
        return None


def _state_text(result: object) -> str:
    """identity_lookup 返回值归一：字符串或 {state, ...} 字典。"""
    if isinstance(result, dict):
        return str(result.get("state") or "").strip().lower()
    return str(result or "").strip().lower()


def _registered_identity_match(identity: Any, identity_keys: Iterable[str]) -> bool:
    """登记集合匹配：优先生命周期 token（identity+birth），再退回纯身份键。

    纯身份键只用于旧登记（birth 未知/平台不支持）与旧调用方兼容 —— 这也是
    「旧 birth NULL 视为不确定、按路径/历史保守」的兜底保护。
    """
    keys = identity_keys or ()
    token = _token_of(identity)
    if token and token in keys:
        return True
    key = str(getattr(identity, "key", "") or "")
    return bool(key) and key in keys


def _known_path_matches(candidate: Path, known_paths: Iterable[object]) -> bool:
    wanted = str(candidate)
    for raw in known_paths or ():
        text = str(raw or "").strip()
        if not text:
            continue
        if text == wanted:
            return True
        normalized = normalize_path(text)
        if normalized is not None and str(normalized) == wanted:
            return True
    return False


def is_internal_data_object(
    path: object,
    protected: list[Path] | None,
    *,
    identity_key: str | None = None,
    identity_keys: Iterable[str] = (),
    known_paths: Iterable[object] = (),
    identity_of: Any = None,
    identity: Any = None,
    identity_lookup: Any = None,
) -> bool:
    """某个路径当前指向的对象是否属于内部运行数据（R9-H2 §5.1 / R9-H4 §8）。

    判定顺序（只按真实事实，不按名称/后缀/内容）：
    1. 路径命中 AppContext 真实注册范围（含子路径）→ True；
    2. 生命周期核对（identity_lookup 存在时）：identity+birth 同时一致才认同一对象；
       编号复用（identity 同 birth 不同）→ 不是旧内部对象；
       uncertain → 只有历史登记路径命中才保守保护（不声称同一文件）；
    3. 登记集合匹配：优先 identity+birth token，再退回纯身份键（兼容旧登记）；
    4. 身份读不到且路径在历史登记路径里 → True（不静默降级）；
    5. 否则 False —— 同内容不同身份的复制件不是原对象。
    """
    candidate = normalize_path(path)
    if candidate is None:
        return False
    if is_internal_data_path(candidate, protected):
        return True
    keys = identity_keys or ()
    current = identity if identity is not None else _identity_from_target_value(identity_key)
    if current is None:
        current = _identity_object_of(candidate, identity_of)
    if identity_lookup is not None and current is not None:
        try:
            state = _state_text(identity_lookup(current))
        except Exception:  # noqa: BLE001 - 登记读失败不能静默降级为“普通文件”
            return True
        if state in ("same", "match", "protected"):
            return True
        if state in ("different", "reuse", "reused"):
            return False
        if state in ("uncertain", "unknown_birth"):
            if _registered_identity_match(current, keys):
                return True
            return _known_path_matches(candidate, known_paths)
        if state in ("retired", "deleted"):
            return _known_path_matches(candidate, known_paths)
    if current is not None:
        if _registered_identity_match(current, keys):
            return True
    else:
        key = str(identity_key or "").strip() or _identity_key_of(candidate, identity_of)
        if key and key in keys:
            return True
        if key is None and _known_path_matches(candidate, known_paths):
            return True
        return False
    return False


def effect_targets_internal_data(
    effect: Effect,
    protected: list[Path] | None,
    *,
    identity_keys: Iterable[str] = (),
    known_paths: Iterable[object] = (),
    identity_of: Any = None,
    identity_lookup: Any = None,
) -> bool:
    """effect 的目标是否涉及内部运行数据（路径注册 + 真实对象身份，契约 §5.1）。

    旧调用签名保持兼容（新增参数都有默认值）；EffectKind.INTERNAL_DATA 仍直接为 True。
    """
    if effect.kind == EffectKind.INTERNAL_DATA:
        return True
    for target in effect.targets:
        if target.kind != "path":
            continue
        if is_internal_data_object(
            target.value,
            protected,
            identity_key=getattr(target, "identity", None),
            identity_keys=identity_keys,
            known_paths=known_paths,
            identity_of=identity_of,
            identity_lookup=identity_lookup,
        ):
            return True
    return False


def requires_three_confirmations(
    effect: Effect,
    protected: list[Path] | None,
    *,
    identity_keys: Iterable[str] = (),
    known_paths: Iterable[object] = (),
    identity_of: Any = None,
    identity_lookup: Any = None,
) -> bool:
    """该 effect 是否需要内部数据三次确认（写入/删除/脚本维护等）。"""
    return effect_targets_internal_data(
        effect,
        protected,
        identity_keys=identity_keys,
        known_paths=known_paths,
        identity_of=identity_of,
        identity_lookup=identity_lookup,
    )


def confirmations_for(
    effect: Effect,
    protected: list[Path] | None,
    *,
    identity_keys: Iterable[str] = (),
    known_paths: Iterable[object] = (),
    identity_of: Any = None,
    identity_lookup: Any = None,
) -> int:
    return (
        CONFIRMATIONS_REQUIRED
        if requires_three_confirmations(
            effect,
            protected,
            identity_keys=identity_keys,
            known_paths=known_paths,
            identity_of=identity_of,
            identity_lookup=identity_lookup,
        )
        else 1
    )


def describe_scope(effect: Effect) -> str:
    """审批单/警告里的一句范围说明。"""
    paths = [target.value for target in effect.targets if target.kind == "path"]
    if paths:
        return "；".join(paths[:4]) + ("…" if len(paths) > 4 else "")
    return "（未指明具体文件）"
