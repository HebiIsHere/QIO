"""一 Key 多模型 / 用途分配 / 默认模型（credential_models + model_usages）。

为什么单独一层
--------------
基线的 credentials 表一行 = 一把 Key，而「用哪个模型」只存在 default_model 一列里，
于是「同一把 Key 下不同模型分给不同用途」根本表达不出来；用途又塞在 tags 里，
一把 Key 整体被某个用途占用。本轮把数据拆成三层（见 usages.py 顶部）：

    连接（credentials，一行一把 Key）
        └── 模型（credential_models，一把 Key 多个模型）
                └── 用途（model_usages，一行一个用途 → (credential_id, model_id|null)）

默认模型存在 settings 的 model.default 键（JSON）：它是「日常对话用哪一家的哪个模型」，
与「哪条连接是默认连接」（credentials.is_default）是两件事，不能合并。

三条硬语义（写在这里，是为了让 API / 运行时 / 界面都引用同一份判断）
-------------------------------------------------------------------
1. 不可用必须说出来：连接停用 / 撤销 / 过期 / 预算耗尽 / **未通过验证** /
   **密钥在本机不可读**，或这个模型在当前配置上验证失败，usable=False 且带
   unavailable_reason。连接级判断与真实调用（services.usage_target.credential_problem）
   逐项同序，两处不能各说各话：界面可选的模型必须真的发得出去。绝不把不可用模型
   伪装成可用，也绝不因为一个模型失败就判定整把 Key 的所有模型失效。
2. 验证结论跟着配置走：每行模型记录 verify_fingerprint（密钥版本 + 地址 + 协议 +
   模型名）。配置变了就 applies_to_current_config=False，旧结论不覆盖新配置；迟到的
   验证结果只能留痕，不能改写当前状态（与 CredentialStore.set_verified 同一口径）。
3. 不生成假事实：没有发现到模型就返回空列表 + 真实错误；legacy 来源的模型照抄旧结论
   但不伪造指纹；能力未声明时如实标注「能力未知」，不猜它支持什么。
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from agent.credentials.providers import (
    find_preset,
    preset_for_endpoint,
    resolve_model,
)
from agent.credentials.store import (
    USABLE_VERIFY_STATES,
    compute_discovery_signature,
    VERIFY_STATE_FAILED,
    VERIFY_STATE_UNVERIFIED,
    VERIFY_STATE_VERIFIED,
    CredentialStore,
)
from agent.credentials.usages import (
    CUSTOM_USAGE_PREFIX,
    DELIVERY_MODEL,
    DELIVERY_UNSUPPORTED,
    USAGE_BY_KEY,
    USAGES,
    UsageNotEffective,
    UsageSpec,
    UsageTargetNotUsable,
    custom_tag_of,
    find_usage,
    is_custom_usage,
    normalize_usage_key,
    usage_is_effective,
)
from agent.storage.db import transaction

logger = logging.getLogger(__name__)

# 模型来源：
# - discovered：用户提交连接后从服务商 /models 取回；
# - manual：用户在高级入口手动填写（必须明确是他的选择，不是我们猜的）；
# - legacy：迁移回填（来自旧 credentials.default_model，结论只属于旧配置）。
MODEL_SOURCE_DISCOVERED = "discovered"
MODEL_SOURCE_MANUAL = "manual"
MODEL_SOURCE_LEGACY = "legacy"

MODEL_SOURCES = (MODEL_SOURCE_DISCOVERED, MODEL_SOURCE_MANUAL, MODEL_SOURCE_LEGACY)

#: 默认模型设置键：值是 JSON {"credential_id": "...", "model_id": "..."}，空 = 未设置。
DEFAULT_MODEL_KEY = "model.default"

#: 最近一次发现结果（按连接）：checked_at / error / source / provider_id / model_count。
DISCOVERY_KEY_PREFIX = "model.discovery."

#: 当前配置下「这次真实获取到的目录」（按连接）：
#: {"config_signature": "...", "model_ids": [...]}。用来把「当前目录」与旧配置留下的
#: 历史模型行分开：新地址只返回一个模型时，旧地址的模型仍在库里（历史 / 手动 / 用途引用），
#: 但不再算成本次获取结果。空 = 还没有按配置记录过（旧数据按「当前」兼容处理）。
CURRENT_SET_KEY_PREFIX = "model.current_set."

#: 用途 kind：运行时按它决定用哪种调用路径。显式写明而不是从名字猜，
#: C 的用途解析与界面文案都读这一份。
USAGE_KIND_CHAT = "chat"
USAGE_KIND_VISION = "vision"
USAGE_KIND_VIDEO = "video"
USAGE_KIND_AUDIO = "audio"
USAGE_KIND_EMBEDDING = "embedding"
USAGE_KIND_BACKGROUND = "background"
USAGE_KIND_CUSTOM = "custom"

USAGE_KIND_BY_KEY: dict[str, str] = {
    "main-chat": USAGE_KIND_CHAT,
    "general-chat": USAGE_KIND_CHAT,
    "code": USAGE_KIND_CHAT,
    "subagent": USAGE_KIND_CHAT,
    "research": USAGE_KIND_CHAT,
    "vision": USAGE_KIND_VISION,
    "video": USAGE_KIND_VIDEO,
    "audio": USAGE_KIND_AUDIO,
    "embedding": USAGE_KIND_EMBEDDING,
    "memory-organize": USAGE_KIND_BACKGROUND,
    "knowledge-distill": USAGE_KIND_BACKGROUND,
    "entity-extract": USAGE_KIND_BACKGROUND,
}

#: 不继承默认对话模型的用途：把对话模型塞给视频 / 语音 / 嵌入会直接失败或产生错维度向量。
NON_INHERITING_KINDS = frozenset({USAGE_KIND_VIDEO, USAGE_KIND_AUDIO, USAGE_KIND_EMBEDDING})

#: 模型级验证指纹：覆盖「这把 Key 发给谁、用哪个模型」四样输入。
#: 与 store.compute_input_fingerprint 同算法、不同前缀，避免两类结论混用。
MODEL_FINGERPRINT_VERSION = "mfp1"

#: 连接级不可用原因（顺序即优先级：先说的就是界面该显示的那条）。
REASON_CREDENTIAL_MISSING = "连接不存在（可能已删除）"
REASON_CREDENTIAL_REVOKED = "连接已撤销：本机已移除密钥"
REASON_CREDENTIAL_EXPIRED = "连接已过期"
REASON_CREDENTIAL_DISABLED = "连接已停用"
REASON_BUDGET_EXHAUSTED = "这条连接的累计用量上限已用完"
REASON_MODEL_VERIFY_FAILED = "这个模型在当前配置上验证没通过"
REASON_CAPABILITY_MISSING = "该模型没有声明这个用途需要的能力"
#: 连接级「还没通过验证」（含上次验证失败）。真实调用以 credential_unverified 拒绝，
#: 目录必须说同一件事，否则未验证连接会被展示成可选、一发就失败。
REASON_CREDENTIAL_UNVERIFIED = "连接还没有通过验证"
#: 元数据看起来正常（active / enabled / verified），但本机已经读不到密钥 —— 撤销会删
#: 密钥，keyring 后端丢失或密钥被外部清掉同样如此：这条连接发不出真实请求。
REASON_SECRET_MISSING = "这条连接的密钥在本机不可读"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _norm(value: Any) -> str:
    return str(value if value is not None else "").strip()


#: 没有独立调用路径的用途，给用户一句话原因（按 delivery 区分）。
_NO_CALL_PATH_PHRASE = {
    "existing": "沿用现有处理方式，没有独立的模型调用路径",
    "unsupported": "当前没有独立的模型调用路径",
}


def _no_call_path_phrase(spec: UsageSpec) -> str:
    return _NO_CALL_PATH_PHRASE.get(spec.delivery, _NO_CALL_PATH_PHRASE["unsupported"])


def compute_model_fingerprint(
    *,
    version: int | str | None,
    endpoint: str | None,
    kind: str | None,
    model_id: str | None,
) -> str:
    """把「一次模型验证的完整输入」压成可比较的指纹（不含任何密钥材料）。

    只做规范化 + 摘要，可以被 API 层 / 测试独立复算：指纹一样 = 这四样输入一个字
    都没变；不一样 = 那条结论属于另一份配置，不能拿来判断当前可用性。
    """
    try:
        normalized_version = int(version or 0)
    except (TypeError, ValueError):
        normalized_version = 0
    canonical = json.dumps(
        {
            "v": MODEL_FINGERPRINT_VERSION,
            "version": normalized_version,
            "endpoint": _norm(endpoint),
            "kind": _norm(kind),
            "model_id": _norm(model_id),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"{MODEL_FINGERPRINT_VERSION}:{digest[:32]}"


def usage_kind(usage_key: str | None) -> str:
    """用途 → kind；自定义标签是 custom（只保留可见性，不产生新执行能力）。"""
    key = _norm(usage_key)
    if key in USAGE_KIND_BY_KEY:
        return USAGE_KIND_BY_KEY[key]
    return USAGE_KIND_CUSTOM if is_custom_usage(key) else USAGE_KIND_CHAT


def _parse_json_list(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(item) for item in raw]
    try:
        data = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [str(item) for item in data if str(item).strip()]


def _decode_model_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["capabilities"] = _parse_json_list(data.get("capabilities"))
    data["verify_state"] = str(data.get("verify_state") or VERIFY_STATE_UNVERIFIED)
    return data


class ModelCatalogStore:
    """模型目录 / 用途分配 / 默认模型的唯一读写入口。

    与 CredentialStore 共用一个连接：连接的生命周期（创建 / 改配置 / 撤销 / 删除）
    仍由它负责；这里只读写「模型」与「用途 → 连接+模型」两层，不自带密钥后端。
    """

    def __init__(self, conn: sqlite3.Connection, credentials: CredentialStore) -> None:
        self.conn = conn
        self.credentials = credentials

    # ------------------------------------------------------------------
    # 内部：连接元数据 / 指纹 / 设置
    # ------------------------------------------------------------------

    def _credential_rows(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM credentials ORDER BY created_at, id"
        ).fetchall()

    def _credential_row(self, credential_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM credentials WHERE id = ?", (_norm(credential_id),)
        ).fetchone()

    def _credential_meta(self, credential_id: str) -> dict[str, Any] | None:
        return self.credentials.get_metadata(_norm(credential_id))

    def _model_fingerprint_for_row(self, row: sqlite3.Row, model_id: str) -> str:
        meta = self.credentials._serialize(row)  # noqa: SLF001 - 同一模块族的内部协议
        kind = self.credentials._resolved_kind(row)  # noqa: SLF001
        return compute_model_fingerprint(
            version=meta.get("version"),
            endpoint=meta.get("endpoint"),
            kind=kind,
            model_id=model_id,
        )

    def model_fingerprint(self, credential_id: str, model_id: str) -> str | None:
        """这个模型在当前配置下的验证输入指纹；连接不存在返回 None。"""
        row = self._credential_row(credential_id)
        if row is None:
            return None
        return self._model_fingerprint_for_row(row, _norm(model_id))

    def _settings_get(self, key: str) -> str | None:
        row = self.conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        return str(row["value"]) if row is not None else None

    def _settings_set(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, value, _now()),
        )

    def _settings_delete(self, key: str) -> None:
        self.conn.execute("DELETE FROM settings WHERE key = ?", (key,))

    # ------------------------------------------------------------------
    # 模型目录
    # ------------------------------------------------------------------

    def list_models(
        self, credential_id: str, *, secret_present: bool | None = None
    ) -> list[dict[str, Any]]:
        """一条连接下的模型（带当前配置的可用性判定）。

        排序：来源（手动 > 发现 > 历史）→ 最近更新 → 模型名，保证界面稳定。

        可用性判定要看「密钥还在不在」，但同一条连接的所有模型共享同一个答案：
        这里只读一次 keyring，再把结论传给每个模型（secret_present），避免一屏
        N 个模型读 N 次密钥后端。
        """
        row = self._credential_row(credential_id)
        if row is None:
            return []
        rows = self.conn.execute(
            "SELECT * FROM credential_models WHERE credential_id = ?", (_norm(credential_id),)
        ).fetchall()
        if not rows:
            return []
        meta = self.credentials._serialize(row)  # noqa: SLF001
        if secret_present is None:
            secret_present = self._secret_present(_norm(credential_id))
        out: list[dict[str, Any]] = []
        for model_row in rows:
            out.append(
                self._decorate(
                    model_row, meta, credential_row=row, secret_present=secret_present
                )
            )
        out.sort(key=lambda item: (item["_sort_source"], item["updated_at"], item["model_id"]))
        for item in out:
            item.pop("_sort_source", None)
        return out

    def _decorate(
        self,
        model_row: sqlite3.Row,
        meta: dict[str, Any],
        *,
        credential_row: sqlite3.Row | None = None,
        secret_present: bool | None = None,
    ) -> dict[str, Any]:
        data = _decode_model_row(model_row)
        # 先算「这条验证结论说的是不是当前配置」，再判可用性：顺序反过来会让
        # 当前配置上验证失败的模型被当成可用（verify_applies 还没算出来）。
        row = credential_row or self._credential_row(str(data["credential_id"]))
        if row is not None:
            current = self._model_fingerprint_for_row(row, str(data["model_id"]))
            data["verify_applies"] = bool(
                data.get("verify_fingerprint") and data["verify_fingerprint"] == current
            )
        else:
            data["verify_applies"] = False
        data["usable"], data["unavailable_reason"] = self.model_usable(
            data, meta, secret_present=secret_present
        )
        # 来源排序权重：手动 < 发现 < 历史（用户明确选过的优先展示）。
        data["_sort_source"] = {
            MODEL_SOURCE_MANUAL: "0",
            MODEL_SOURCE_DISCOVERED: "1",
            MODEL_SOURCE_LEGACY: "2",
        }.get(str(data.get("source")), "3")
        return data

    def get_model(
        self,
        credential_id: str,
        model_id: str,
        *,
        secret_present: bool | None = None,
    ) -> dict[str, Any] | None:
        row = self._credential_row(credential_id)
        if row is None:
            return None
        model_row = self.conn.execute(
            "SELECT * FROM credential_models WHERE credential_id = ? AND model_id = ?",
            (_norm(credential_id), _norm(model_id)),
        ).fetchone()
        if model_row is None:
            return None
        meta = self.credentials._serialize(row)  # noqa: SLF001
        data = self._decorate(
            model_row, meta, credential_row=row, secret_present=secret_present
        )
        data.pop("_sort_source", None)
        return data

    def upsert_model(
        self,
        credential_id: str,
        model_id: str,
        *,
        display_name: str | None = None,
        source: str = MODEL_SOURCE_MANUAL,
        capabilities: list[str] | None = None,
        context_window: int | None = None,
        replace_metadata: bool = False,
    ) -> dict[str, Any]:
        """写入/更新一个模型。

        replace_metadata=False（默认）表示「只补空字段」：重复发现不会把用户手动
        设置的显示名/能力降级成服务商返回的空值。source=legacy 的行一旦被重新发现，
        来源升级为 discovered（旧结论仍留在 verify 列里，靠指纹判断是否适用）。
        """
        credential_id = _norm(credential_id)
        if not credential_id:
            raise ValueError("credential_id 不能为空")
        model_id = _norm(model_id)
        if not model_id:
            raise ValueError("model_id 不能为空")
        if source not in MODEL_SOURCES:
            raise ValueError(f"未知的模型来源：{source}")
        row = self._credential_row(credential_id)
        if row is None:
            raise KeyError(f"credential not found: {credential_id}")
        existing = self.conn.execute(
            "SELECT * FROM credential_models WHERE credential_id = ? AND model_id = ?",
            (credential_id, model_id),
        ).fetchone()
        caps_json = (
            json.dumps(list(capabilities), ensure_ascii=False) if capabilities is not None else None
        )
        now = _now()
        if existing is None:
            self.conn.execute(
                "INSERT INTO credential_models ("
                " id, credential_id, model_id, display_name, source, capabilities,"
                " verify_state, verified_at, verify_error, verify_fingerprint,"
                " context_window, discovered_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, 'unverified', NULL, NULL, NULL, ?, ?, ?)",
                (
                    _new_id("cm"),
                    credential_id,
                    model_id,
                    (display_name or "").strip() or None,
                    source,
                    caps_json if caps_json is not None else "[]",
                    context_window,
                    now,
                    now,
                ),
            )
        else:
            sets: list[str] = ["updated_at = ?"]
            params: list[Any] = [now]
            if display_name and (replace_metadata or not (existing["display_name"] or "").strip()):
                sets.append("display_name = ?")
                params.append(display_name.strip())
            if replace_metadata and caps_json is not None:
                sets.append("capabilities = ?")
                params.append(caps_json)
            elif caps_json is not None and not _parse_json_list(existing["capabilities"]):
                sets.append("capabilities = ?")
                params.append(caps_json)
            if replace_metadata and context_window is not None:
                sets.append("context_window = ?")
                params.append(int(context_window))
            elif context_window is not None and existing["context_window"] is None:
                sets.append("context_window = ?")
                params.append(int(context_window))
            # 重新发现把 legacy 行升级为 discovered：来源只升不降（manual 不会被覆盖）。
            if source == MODEL_SOURCE_DISCOVERED and str(existing["source"]) == MODEL_SOURCE_LEGACY:
                sets.append("source = ?")
                params.append(MODEL_SOURCE_DISCOVERED)
            elif source == MODEL_SOURCE_MANUAL and str(existing["source"]) != MODEL_SOURCE_MANUAL:
                sets.append("source = ?")
                params.append(MODEL_SOURCE_MANUAL)
            params.extend([credential_id, model_id])
            self.conn.execute(
                f"UPDATE credential_models SET {', '.join(sets)}"
                " WHERE credential_id = ? AND model_id = ?",
                params,
            )
        return self.get_model(credential_id, model_id) or {}

    def upsert_discovered(
        self, credential_id: str, models: list[dict[str, Any]]
    ) -> dict[str, int]:
        """批量写入一次发现结果（只做 upsert，不删除用户手动添加的模型）。

        发现结果里没有的旧模型不删：服务商列表可能临时不完整 / 分页没取全，
        删掉会让已分配的用途突然指向不存在的模型。删除必须由用户显式操作。
        """
        added = 0
        updated = 0
        seen: set[str] = set()
        for item in models:
            model_id = _norm(item.get("model_id") or item.get("id"))
            if not model_id or model_id in seen:
                continue
            seen.add(model_id)
            existed = (
                self.conn.execute(
                    "SELECT 1 FROM credential_models WHERE credential_id = ? AND model_id = ?",
                    (_norm(credential_id), model_id),
                ).fetchone()
                is not None
            )
            self.upsert_model(
                credential_id,
                model_id,
                display_name=item.get("display_name") or item.get("name"),
                source=MODEL_SOURCE_DISCOVERED,
                capabilities=item.get("capabilities"),
                context_window=item.get("context_window"),
            )
            if existed:
                updated += 1
            else:
                added += 1
        return {"added": added, "updated": updated, "total": len(seen)}

    def delete_model(self, credential_id: str, model_id: str) -> bool:
        """删除一个模型记录；仍被用途引用时拒绝（先让用户处理引用）。"""
        refs = [
            item
            for item in self.list_usages()
            if item["credential_id"] == _norm(credential_id)
            and item["model_id"] == _norm(model_id)
        ]
        if refs:
            labels = "、".join(item["label"] for item in refs)
            raise ValueError(f"这个模型还被这些用途引用：{labels}；请先把它们改成别的模型")
        cur = self.conn.execute(
            "DELETE FROM credential_models WHERE credential_id = ? AND model_id = ?",
            (_norm(credential_id), _norm(model_id)),
        )
        return cur.rowcount > 0

    def record_discovery(
        self,
        credential_id: str,
        *,
        source: str = "provider",
        error: str | None = None,
        reason_code: str | None = None,
        provider_id: str | None = None,
        model_count: int = 0,
        config_signature: str = "",
        source_signature: str = "",
    ) -> dict[str, Any]:
        """记住最近一次发现的时间与错误（界面据此说「上次检查」而不是假装刚刚成功）。

        config_signature 记录最近一次**尝试**属于哪一份配置；source_signature 记录
        「这些模型是从哪份配置真正取回来的」——失败尝试不能改写来源事实。
        """
        # 失败尝试只更新「最近一次尝试」，不改变模型来源：沿用上一次成功记录的来源签名；
        # 旧格式没有 source_signature 时，用「上一次状态没有错误」的 config_signature 兜底。
        preserved = self.discovery_state(credential_id)
        effective_source = str(source_signature or "")
        if not effective_source:
            effective_source = str(preserved.get("source_signature") or "")
        if not effective_source and preserved.get("error") is None:
            effective_source = str(preserved.get("config_signature") or "")
        payload = {
            "checked_at": _now(),
            "error": error or None,
            "reason_code": reason_code or None,
            "source": source,
            "provider_id": provider_id,
            "model_count": int(model_count),
            "config_signature": str(config_signature or ""),
            "source_signature": effective_source,
        }
        self._settings_set(
            DISCOVERY_KEY_PREFIX + _norm(credential_id),
            json.dumps(payload, ensure_ascii=False),
        )
        return payload

    def discovery_state(self, credential_id: str) -> dict[str, Any]:
        raw = self._settings_get(DISCOVERY_KEY_PREFIX + _norm(credential_id))
        empty = {
            "checked_at": None,
            "error": None,
            "reason_code": None,
            "source": None,
            "provider_id": None,
            "model_count": 0,
            "config_signature": "",
            "source_signature": "",
        }
        if not raw:
            return empty
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return empty
        if not isinstance(data, dict):
            return empty
        return {
            "checked_at": data.get("checked_at"),
            "error": data.get("error"),
            "reason_code": data.get("reason_code"),
            "source": data.get("source"),
            "provider_id": data.get("provider_id"),
            "model_count": int(data.get("model_count") or 0),
            "config_signature": str(data.get("config_signature") or ""),
            "source_signature": str(data.get("source_signature") or ""),
        }

    def record_current_set(
        self, credential_id: str, *, config_signature: str, model_ids: list[str]
    ) -> dict[str, Any]:
        """记住「当前配置下这次真正返回的模型集合」（不删除任何历史行）。"""
        payload = {
            "config_signature": str(config_signature or ""),
            "model_ids": [_norm(item) for item in model_ids if _norm(item)],
        }
        self._settings_set(
            CURRENT_SET_KEY_PREFIX + _norm(credential_id),
            json.dumps(payload, ensure_ascii=False),
        )
        return payload

    def current_set(self, credential_id: str) -> dict[str, Any]:
        """当前配置记录过的模型集合；没有记录时返回空签名 + 空列表（旧数据兼容）。"""
        raw = self._settings_get(CURRENT_SET_KEY_PREFIX + _norm(credential_id))
        empty: dict[str, Any] = {"config_signature": "", "model_ids": []}
        if not raw:
            return empty
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return empty
        if not isinstance(data, dict):
            return empty
        ids = data.get("model_ids")
        return {
            "config_signature": str(data.get("config_signature") or ""),
            "model_ids": [_norm(item) for item in ids] if isinstance(ids, list) else [],
        }

    # ------------------------------------------------------------------
    # 默认模型
    # ------------------------------------------------------------------

    def get_default(self) -> dict[str, str] | None:
        """默认模型 {"credential_id", "model_id"}；未设置 / 连接已删 / 模型已删 → None。"""
        raw = self._settings_get(DEFAULT_MODEL_KEY)
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        credential_id = _norm(data.get("credential_id"))
        model_id = _norm(data.get("model_id"))
        if not credential_id or not model_id:
            return None
        if self._credential_row(credential_id) is None:
            return None
        exists = self.conn.execute(
            "SELECT 1 FROM credential_models WHERE credential_id = ? AND model_id = ?",
            (credential_id, model_id),
        ).fetchone()
        if exists is None:
            return None
        return {"credential_id": credential_id, "model_id": model_id}

    def set_default(self, credential_id: str, model_id: str) -> dict[str, str]:
        """设置默认模型；连接/模型不存在时先补一条 manual 模型（用户显式指定即事实）。"""
        credential_id = _norm(credential_id)
        model_id = _norm(model_id)
        if not credential_id or not model_id:
            raise ValueError("请选择默认模型所在的连接与模型")
        if self._credential_row(credential_id) is None:
            raise KeyError(f"credential not found: {credential_id}")
        if self.get_model(credential_id, model_id) is None:
            self.upsert_model(credential_id, model_id, source=MODEL_SOURCE_MANUAL)
        self._settings_set(
            DEFAULT_MODEL_KEY,
            json.dumps(
                {"credential_id": credential_id, "model_id": model_id}, ensure_ascii=False
            ),
        )
        return {"credential_id": credential_id, "model_id": model_id}

    def clear_default(self) -> None:
        self._settings_delete(DEFAULT_MODEL_KEY)

    # ------------------------------------------------------------------
    # 用途分配
    # ------------------------------------------------------------------

    def _usage_row(self, usage_key: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM model_usages WHERE usage_key = ?", (_norm(usage_key),)
        ).fetchone()

    def get_usage(self, usage_key: str) -> dict[str, Any] | None:
        key = normalize_usage_key(usage_key)
        row = self._usage_row(key)
        if row is None:
            return None
        return {
            "usage_key": key,
            "credential_id": row["credential_id"],
            "model_id": row["model_id"],
            "mode": str(row["mode"]),
            "updated_at": row["updated_at"],
        }

    def set_usage(
        self,
        usage_key: str,
        credential_id: str | None,
        model_id: str | None,
    ) -> dict[str, Any]:
        """设置用途分配；两者都为空 = 恢复跟随默认（删掉覆盖行）。

        两条硬规则（B/C）：
        1. **没有独立调用路径的用途不接受新分配**：写下去也不会被任何真实调用读取，
           只能谎报「已生效」；清除历史覆盖仍然允许（清除不是新分配）。
        2. **分配的目标必须现在真的可用**：与真实调用同一个
           `model_usable(..., usage_key=...)`（连接状态 / 验证 / 预算 / 密钥 / 模型验证 /
           用途所需能力）；能力未声明按「未知」放行，不按模型名猜、不联网验证。
        """
        raw_key = str(usage_key or "").strip()
        key = normalize_usage_key(raw_key)
        spec = find_usage(key)
        credential_id = _norm(credential_id) or None
        model_id = _norm(model_id) or None
        # 返修：只有**显式 custom:<tag>** 才是合法历史标签；自由文本仍按未知用途拒绝
        # （保留第一轮 400 语义），否则「清除历史覆盖」对自定义标签不可用。
        if spec is None and not (
            raw_key.startswith(CUSTOM_USAGE_PREFIX) and is_custom_usage(key)
        ):
            raise ValueError(f"未知用途：{usage_key}")
        if credential_id is None:
            if model_id is not None:
                raise ValueError("只给模型不给连接会指到一个不存在的发送目标")
            with transaction(self.conn):
                self.conn.execute("DELETE FROM model_usages WHERE usage_key = ?", (key,))
            return {"usage_key": key, "credential_id": None, "model_id": None, "mode": "inherit"}
        if model_id is None:
            raise ValueError("指定了连接就必须指定模型；要跟随默认请把两者都留空")
        if self._credential_row(credential_id) is None:
            raise KeyError(f"credential not found: {credential_id}")
        # 先整条校验（有没有独立调用路径 + 目标现在是否真的可用），再写任何记录：
        # 被拒绝的分配不留下模型行 / 用途行。
        self.check_assignment(key, credential_id, model_id)
        if self.get_model(credential_id, model_id) is None:
            # 高级入口允许手动模型名：用户明确写了它，就记为 manual（不是我们猜的）。
            self.upsert_model(credential_id, model_id, source=MODEL_SOURCE_MANUAL)
        now = _now()
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO model_usages (id, usage_key, credential_id, model_id, mode, updated_at)"
                " VALUES (?, ?, ?, ?, 'explicit', ?)"
                " ON CONFLICT(usage_key) DO UPDATE SET"
                " credential_id = excluded.credential_id,"
                " model_id = excluded.model_id,"
                " mode = 'explicit',"
                " updated_at = excluded.updated_at",
                (_new_id("mu"), key, credential_id, model_id, now),
            )
        return {
            "usage_key": key,
            "credential_id": credential_id,
            "model_id": model_id,
            "mode": "explicit",
        }

    def check_assignment(
        self,
        usage_key: str,
        credential_id: str,
        model_id: str,
    ) -> None:
        """一条用途分配现在能不能真的发出去；不能就抛 UsageTargetNotUsable。

        与真实调用同一个 `model_usable(..., usage_key=...)`：连接级事实（状态 / 启用 /
        验证 / 预算 / 密钥）→ 模型级验证失败 → 用途要求的能力是否声明。
        能力未声明 = 未知，按现有运行规则放行（与候选展示一致）。

        批量保存在**开启事务前**逐项调用它，保证「整批先校验、再原子写」。
        """
        key = normalize_usage_key(usage_key)
        spec = find_usage(key)
        credential_id = _norm(credential_id)
        model_id = _norm(model_id)
        if spec is None:
            if is_custom_usage(key):
                raise UsageNotEffective(
                    key,
                    f"「{custom_tag_of(key)}」是历史自定义标签：只保留可见性与映射，"
                    "不产生新的执行能力，这里的模型分配不会影响真实请求",
                    credential_id=credential_id,
                    model_id=model_id,
                )
            raise ValueError(f"未知用途：{usage_key}")
        if self._credential_row(credential_id) is None:
            raise KeyError(f"credential not found: {credential_id}")
        if not usage_is_effective(key):
            # 没有独立调用路径：写下去不会被任何真实调用读取，只能谎报「已生效」。
            raise UsageNotEffective(
                key,
                f"「{spec.label}」{_no_call_path_phrase(spec)}："
                "这里的模型分配不会影响真实请求，请改为跟随默认或清除历史覆盖",
                credential_id=credential_id,
                model_id=model_id,
            )
        meta = self._credential_meta(credential_id)
        model = self.get_model(credential_id, model_id)
        if model is None:
            # 还没写进目录的手动模型名：按「未验证、能力未知」判断，与保存后的结论一致。
            model = {
                "verify_state": VERIFY_STATE_UNVERIFIED,
                "verify_applies": False,
                "capabilities": [],
            }
        usable, reason = self.model_usable(model, meta, usage_key=key)
        if not usable:
            raise UsageTargetNotUsable(
                key,
                f"「{spec.label}」的目标当前不可用：{reason}",
                reason=reason,
                credential_id=credential_id,
                model_id=model_id,
            )

    def clear_usage(self, usage_key: str) -> None:
        self.set_usage(usage_key, None, None)

    def references_to_credential(self, credential_id: str) -> list[dict[str, Any]]:
        """仍引用这条连接的用途（删除连接前必须为空，否则先让用户处理引用）。"""
        target = _norm(credential_id)
        return [
            item
            for item in self.list_usages()
            if item["credential_id"] == target and item["model_id"] is not None
        ]

    # ------------------------------------------------------------------
    # 可用性判定
    # ------------------------------------------------------------------

    def _secret_present(self, credential_id: str) -> bool:
        """这条连接的密钥在本机还能不能读到（只读 keyring，不联网、不发起验证）。

        读不到 = 这条连接现在发不出真实请求：撤销会删密钥，keyring 后端丢失或密钥被
        外部清掉同样如此。真实调用那一侧（services.usage_target.credential_problem）
        用的是同一条本地事实，目录不能在这里给出相反结论。

        拿不到 get_secret 的存储（只给元数据的 fake）按「不知道」处理：查不了不等于
        不可用，不能因此误禁；真实调用那一侧仍然自己判断。
        """
        getter = getattr(self.credentials, "get_secret", None)
        if not callable(getter):
            return True
        try:
            return getter(_norm(credential_id)) is not None
        except Exception:  # noqa: BLE001 - 读不到密钥 = 这条连接发不出请求
            return False

    def connection_unusable_reason(
        self,
        meta: dict[str, Any] | None,
        *,
        secret_present: bool | None = None,
    ) -> str:
        """连接级不可用原因；空串 = 连接本身可用。

        判断项与真实调用那一侧（services.usage_target.credential_problem）逐项同序：
        状态 → 启用 → 验证状态 → 预算 → 密钥是否还可读。顺序本身就是语义：两边都按
        这个顺序说话，「同时坏了两样」时给出的原因也不会互相打架。

        secret_present 用于一次列出多条模型时避免重复读 keyring；传 None 表示现读。
        """
        if not meta:
            return REASON_CREDENTIAL_MISSING
        status = str(meta.get("status") or "")
        if status == "revoked":
            return REASON_CREDENTIAL_REVOKED
        if status == "expired":
            return REASON_CREDENTIAL_EXPIRED
        if status != "active":
            return f"连接状态为 {status or '未知'}"
        if not meta.get("enabled", True):
            return REASON_CREDENTIAL_DISABLED
        state = str(meta.get("verify_state") or VERIFY_STATE_UNVERIFIED)
        if state not in USABLE_VERIFY_STATES:
            # 连接没通过验证（含上次验证失败）：真实调用会拒绝，目录也必须拒绝。
            # legacy 在 USABLE_VERIFY_STATES 里：历史连接不因此被误禁。
            if state == VERIFY_STATE_FAILED:
                return REASON_CREDENTIAL_UNVERIFIED + "（上次验证没通过）"
            return REASON_CREDENTIAL_UNVERIFIED
        budget = meta.get("budget")
        if budget is not None:
            try:
                left = float(budget) - float(meta.get("budget_used") or 0)
            except (TypeError, ValueError):
                left = 0.0
            if left <= 0:
                return REASON_BUDGET_EXHAUSTED
        credential_id = _norm(meta.get("id"))
        if credential_id:
            if secret_present is None:
                secret_present = self._secret_present(credential_id)
            if not secret_present:
                return REASON_SECRET_MISSING
        return ""

    def model_usable(
        self,
        model: dict[str, Any],
        meta: dict[str, Any] | None,
        *,
        usage_key: str | None = None,
        secret_present: bool | None = None,
    ) -> tuple[bool, str]:
        """模型可用性判定（连接可用 + 当前配置上验证未失败 + 用途需要的能力已声明）。

        注意：usage_key 为空时只做前两项（目录页展示「这个模型本身可不可用」）；
        指定用途时才叠加能力检查 —— 同一个模型对「主对话」可用、对「看图」可能不可用，
        这是两条不同的事实，不能混成一个布尔值。

        连接级判断复用 connection_unusable_reason（与真实调用同一套顺序与原因），
        不另写一份：两套逻辑迟早会漂移成「界面说可用、一发就失败」。
        """
        reason = self.connection_unusable_reason(meta, secret_present=secret_present)
        if reason:
            return False, reason
        state = str(model.get("verify_state") or VERIFY_STATE_UNVERIFIED)
        if state == VERIFY_STATE_FAILED and model.get("verify_applies"):
            return False, REASON_MODEL_VERIFY_FAILED
        if usage_key:
            key = normalize_usage_key(usage_key)
            spec = find_usage(key)
            if spec is not None and spec.requires_capabilities:
                declared = list(model.get("capabilities") or [])
                if declared:
                    missing = [cap for cap in spec.requires_capabilities if cap not in declared]
                    if missing:
                        return False, REASON_CAPABILITY_MISSING + f"（缺少 {', '.join(missing)}）"
        return True, ""

    # ------------------------------------------------------------------
    # 目录快照（GET /api/models/catalog 的形状）
    # ------------------------------------------------------------------

    def discovery_signature(self, credential_id: str) -> str:
        """这条连接**当前**的发现配置签名（Key 版本 + 地址 + 协议）。"""
        row = self._credential_row(_norm(credential_id))
        if row is None:
            return ""
        meta = self.credentials._serialize(row)  # noqa: SLF001
        return compute_discovery_signature(
            version=meta.get("version"),
            endpoint=meta.get("endpoint"),
            kind=meta.get("kind"),
        )

    def current_model_view(self, credential_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """把一条连接的模型分成（当前目录, 旧配置历史）。

        当前目录 = 手动 / 历史兼容行 + **当前配置**这次真实获取到的模型；
        旧配置发现过的模型仍在库里（历史 / 用途引用 / 手动模型都不删除），但不再算进
        本次获取结果 —— 新地址只返回一个模型时，旧地址的模型不能一起被计数。
        兼容旧格式：没有集合字段、但发现状态已经记录了来源签名（上一轮写下的
        discovery config_signature）时，按那个签名判断归属；只有连来源签名都没有的
        真正老数据才全部按当前目录兼容（第一次成功获取后写入集合）。
        """
        signature = self.discovery_signature(credential_id)
        info = self.current_set(credential_id)
        stored_signature = str(info.get("config_signature") or "")
        owned = bool(stored_signature) and stored_signature == signature
        current_ids = set(info["model_ids"]) if owned else set()
        has_set = bool(stored_signature)
        # 旧格式兼容：集合字段缺失时，来源签名仍然明确说明这些模型属于哪份配置。
        legacy_signature = ""
        legacy_owned = False
        if not has_set:
            state = self.discovery_state(credential_id)
            # 来源事实优先：失败尝试写下的 config_signature 不能替代「模型从哪来」。
            legacy_signature = str(state.get("source_signature") or "")
            if not legacy_signature and not state.get("error"):
                legacy_signature = str(state.get("config_signature") or "")
            legacy_owned = bool(legacy_signature) and legacy_signature == signature
        current: list[dict[str, Any]] = []
        historical: list[dict[str, Any]] = []
        for item in self.list_models(credential_id):
            if str(item.get("source")) == MODEL_SOURCE_DISCOVERED:
                if has_set:
                    is_current = owned and str(item["model_id"]) in current_ids
                elif legacy_signature:
                    # 有明确来源签名：换地址/Key 后旧来源的模型一律算历史，不能全列当前。
                    is_current = legacy_owned
                else:
                    is_current = True
            else:
                is_current = True
            (current if is_current else historical).append(item)
        return current, historical

    def catalog(self, *, usage_key: str | None = None) -> dict[str, Any]:
        """目录快照；带 usage_key 时额外给出「候选是否适用于该用途」的服务端结论。

        用途维度只来自**现有模型能力与用途要求**（不按模型名猜、不联网验证）：
        * `usage_usable`：与真实保存 / 调用同一个 `model_usable(..., usage_key=...)`；
        * `capability_state`：supported / missing / unknown / not_required；
        * 一般可用性 `usable` 保持与用途无关 —— 同一个模型可能「一般可用」但
          「不适用于看图」，这是两条不同事实，界面两条都要显示。
        """
        spec = find_usage(usage_key) if usage_key else None
        rows = self._credential_rows()
        connections: list[dict[str, Any]] = []
        discovery: dict[str, Any] = {}
        for row in rows:
            meta = self.credentials._serialize(row)  # noqa: SLF001
            credential_id = str(meta["id"])
            preset = preset_for_endpoint(meta.get("endpoint")) or find_preset(None)
            # 每条连接只读一次密钥：一般可用性与用途维度判断共用同一份本地快照。
            secret_present = self._secret_present(credential_id)
            # 当前目录与旧配置历史分开：历史行保留（手动 / 用途引用 / 审计），但不计入本次获取。
            models, historical_models = self.current_model_view(credential_id)
            budget = meta.get("budget")
            try:
                budget_left = (
                    None if budget is None else float(budget) - float(meta.get("budget_used") or 0)
                )
            except (TypeError, ValueError):
                budget_left = 0.0
            connections.append(
                {
                    "credential_id": credential_id,
                    "name": self._connection_name(meta, preset),
                    "endpoint": meta.get("endpoint"),
                    "kind": self.credentials._resolved_kind(row),  # noqa: SLF001
                    "provider_id": preset.id if preset else None,
                    "provider_name": preset.name if preset else None,
                    "enabled": bool(meta.get("enabled", True)),
                    "status": str(meta.get("status") or "active"),
                    "verify_state": str(meta.get("verify_state") or VERIFY_STATE_UNVERIFIED),
                    "budget": budget,
                    "budget_used": float(meta.get("budget_used") or 0),
                    "budget_left": budget_left,
                    "models": [
                        self._catalog_model_view(
                            item,
                            meta,
                            spec,
                            secret_present=secret_present,
                        )
                        for item in models
                    ],
                    "historical_models": [
                        self._catalog_model_view(
                            item,
                            meta,
                            spec,
                            secret_present=secret_present,
                        )
                        for item in historical_models
                    ],
                }
            )
            discovery[credential_id] = self.discovery_state(credential_id)
        result: dict[str, Any] = {
            "connections": connections,
            "default": self.get_default(),
            "usages": self.list_usages(),
            "discovery": discovery,
        }
        if spec is not None:
            result["usage"] = {
                "usage_key": spec.key,
                "label": spec.label,
                "delivery": spec.delivery,
                "effective": spec.delivery == DELIVERY_MODEL,
                "requires_capabilities": list(spec.requires_capabilities),
                "note": spec.note,
            }
        return result

    def _catalog_model_view(
        self,
        item: dict[str, Any],
        meta: dict[str, Any],
        spec: UsageSpec | None,
        *,
        secret_present: bool | None,
    ) -> dict[str, Any]:
        """目录里的一行模型：一般可用性 +（带用途时）用途维度结论。"""
        payload = {
            "model_id": item["model_id"],
            "display_name": item["display_name"] or item["model_id"],
            "source": item["source"],
            "capabilities": item["capabilities"],
            "verify_state": item["verify_state"],
            "verified_at": item["verified_at"],
            "verify_error": item["verify_error"],
            "usable": item["usable"],
            "unavailable_reason": item["unavailable_reason"],
        }
        if spec is None:
            return payload
        usable, reason = self.model_usable(
            item, meta, usage_key=spec.key, secret_present=secret_present
        )
        payload.update(
            {
                "usage_key": spec.key,
                "usage_usable": usable,
                "usage_reason": reason,
                "capability_state": self.capability_state(
                    spec, item.get("capabilities")
                ),
                "usage_requires_capabilities": list(spec.requires_capabilities),
            }
        )
        return payload

    @staticmethod
    def capability_state(spec: UsageSpec, capabilities: Any) -> str:
        """能力声明的三类事实；未声明 = unknown（不能判为不支持）。"""
        required = list(spec.requires_capabilities)
        if not required:
            return "not_required"
        declared = [str(item) for item in (capabilities or []) if str(item).strip()]
        if not declared:
            return "unknown"
        return (
            "supported"
            if all(item in declared for item in required)
            else "missing"
        )

    @staticmethod
    def _connection_name(meta: dict[str, Any], preset: Any) -> str:
        note = _norm(meta.get("note"))
        if note:
            return note
        if preset is not None:
            return preset.name
        endpoint = _norm(meta.get("endpoint"))
        if endpoint:
            return endpoint
        return str(meta.get("id") or "连接")

    # ------------------------------------------------------------------
    # 用途列表与解析
    # ------------------------------------------------------------------

    @staticmethod
    def _is_explicit(row: dict[str, Any] | None) -> bool:
        """这一行是不是**用户显式指定**（而不是迁移回填的兼容映射）。

        `mode='explicit'` 才算。迁移回填写的是 `mode='legacy'`：历史 tags 在基线里
        只参与「选哪把 Key」的政策，从不锁定模型；把它当显式指定会让老用户那把它已
        停用/欠费的 Key 把主对话直接打成硬失败，违反「保留既有凭据选择政策」。
        """
        if not isinstance(row, dict):
            return False
        if not _norm(row.get("model_id")):
            return False
        return str(row.get("mode") or "").strip().lower() == "explicit"

    def _assignment(self, spec: UsageSpec) -> dict[str, Any] | None:
        row = self._usage_row(spec.key)
        if row is None:
            return None
        return {
            "usage_key": spec.key,
            "credential_id": row["credential_id"],
            "model_id": row["model_id"],
            "mode": str(row["mode"]),
            "updated_at": row["updated_at"],
        }

    def list_usages(self) -> list[dict[str, Any]]:
        """用途分配视图（GET /api/models/catalog 的 usages 字段）。

        每个用途都给出：显式指定值、是否跟随默认、解析后的实际目标、以及这个目标
        现在可不可用。界面显示的是解析结果，不是分配表里的原始值 —— 否则「跟随默认」
        会显示成空，用户看不出实际用的是谁。
        """
        default = self.get_default()
        specs: list[UsageSpec] = list(USAGES)
        # 历史自定义标签：分配表里出现过的 custom:<tag> 补进列表（usages.py 不会静态枚举历史标签）。
        custom_keys = [
            str(row["usage_key"])
            for row in self.conn.execute(
                "SELECT usage_key FROM model_usages WHERE usage_key LIKE 'custom:%' ORDER BY usage_key"
            )
        ]
        custom_specs: list[UsageSpec] = []
        for key in custom_keys:
            if key in USAGE_BY_KEY:
                continue
            custom_specs.append(
                UsageSpec(
                    key,
                    custom_tag_of(key) or key,
                    "legacy",
                    False,
                    note="历史自定义标签：只保留可见性与映射，不产生新的执行能力",
                    delivery=DELIVERY_UNSUPPORTED,
                    daily=False,
                )
            )
        out: list[dict[str, Any]] = []
        for spec in specs + custom_specs:
            out.append(self.describe_usage(spec, default=default))
        return out

    def describe_usage(
        self, spec: UsageSpec, *, default: dict[str, str] | None = None
    ) -> dict[str, Any]:
        """一个用途的完整视图：分配、是否跟随默认、当前实际目标、是否真的生效。

        `assignment_effective` 是 B 的核心事实：只有存在真实调用读取点的用途才为
        True。没有读取点的用途不解析「当前生效目标」（resolved=None），历史覆盖放进
        `historical_target` 并标「历史配置（未生效）」，界面据此只读展示 + 清除入口。
        """
        key = spec.key
        kind = usage_kind(key)
        own = self._assignment(spec)
        explicit = self._is_explicit(own)
        assignment_effective = spec.delivery == DELIVERY_MODEL
        historical = bool(
            not assignment_effective and own is not None and _norm(own.get("model_id"))
        )
        target: dict[str, Any] | None = None
        source = "none"
        resolved = None
        historical_target: dict[str, Any] | None = None
        follows_default = False
        inherits_ok = False
        note = spec.note or ""
        if assignment_effective:
            if explicit:
                target = {
                    "credential_id": own["credential_id"],
                    "model_id": own["model_id"],
                }
                source = "explicit"
            elif kind in NON_INHERITING_KINDS:
                # vision：不继承默认对话模型；未显式指定时如实为空，由调用方回落。
                target = None
                source = "none"
            elif default is not None:
                target = dict(default)
                source = "default"
            resolved = (
                self._resolved_target(target, source=source, usage_key=key)
                if target
                else None
            )
            follows_default = not explicit
            inherits_ok = bool(resolved and resolved.get("usable"))
            if own is not None and not explicit and _norm(own.get("model_id")):
                # 迁移回填的历史标签映射：保留可见，但**不是**用户显式指定，
                # 运行时按默认/政策选择（基线里 tags 也从不锁定模型）。
                note = "历史标签映射：保留用途关联与可见性，不锁定模型；实际按默认模型或既有选择政策"
            elif kind in NON_INHERITING_KINDS and not explicit:
                note = "沿用现有处理方式：这个用途不继承默认对话模型，需要单独指定才生效"
            elif follows_default and default is None:
                note = "还没有设置默认模型；日常对话会回退到兼容的旧凭据选择规则"
            elif explicit and resolved is not None and not resolved.get("usable"):
                note = "已明确指定，但当前不可用：" + str(
                    resolved.get("unavailable_reason") or ""
                )
        elif historical:
            # 没有独立调用路径：不给出「当前生效目标」，历史覆盖单独可读并标未生效。
            historical_target = self._resolved_target(
                {
                    "credential_id": own["credential_id"],
                    "model_id": own["model_id"],
                },
                source="historical",
            )
            note = (
                "历史配置（未生效）：这个用途没有独立的模型调用路径，"
                "历史覆盖只保留可见性与清除入口，不会影响真实请求"
            )
        return {
            "usage_key": key,
            "label": spec.label,
            "kind": kind,
            "group": spec.group,
            "model_id": own["model_id"] if own else None,
            "credential_id": own["credential_id"] if own else None,
            "follows_default": follows_default,
            "inherits_ok": inherits_ok,
            "resolved": resolved,
            "note": note,
            # 原始分配行的来源（explicit / legacy / inherit）：界面据此区分
            # 「用户显式指定」与「迁移回填的兼容映射」，不把后者说成前者。
            "mode": str(own.get("mode")) if own else None,
            "daily": spec.daily,
            # 这个用途到底怎么被用（model / existing / unsupported）：界面据此区分
            # 「分配会生效」「沿用现有处理方式」「当前未接通」，不把占位画成可用。
            "delivery": spec.delivery,
            # **是否真的生效**（唯一事实，来自上级的真实读取点核对结果）。
            "assignment_effective": assignment_effective,
            # 存在历史覆盖但不会生效：可读、标未生效、可清除，不静默删除。
            "historical": historical,
            "historical_target": historical_target,
            "requires_capabilities": list(spec.requires_capabilities),
        }

    def _resolved_target(
        self,
        target: dict[str, Any],
        *,
        source: str,
        usage_key: str | None = None,
    ) -> dict[str, Any]:
        credential_id = _norm(target.get("credential_id"))
        model_id = _norm(target.get("model_id"))
        meta = self._credential_meta(credential_id)
        # 同一个目标只读一次密钥：模型行的可用性与「这个模型是否适用」必须基于同一次
        # 本地快照，也避免一次解析里重复读 keyring。
        secret_present = self._secret_present(credential_id) if meta is not None else None
        model = (
            self.get_model(credential_id, model_id, secret_present=secret_present)
            if meta is not None
            else None
        )
        preset = preset_for_endpoint((meta or {}).get("endpoint"))
        name = self._connection_name(meta or {}, preset)
        if meta is None or model is None:
            return {
                "credential_id": credential_id or None,
                "model_id": model_id or None,
                "source": source,
                "usable": False,
                "unavailable_reason": REASON_CREDENTIAL_MISSING
                if meta is None
                else "这个连接下没有记录这个模型",
                "credential_name": name,
            }
        # usage_key 一起传：同一个模型对「主对话」可用、对「看图」可能不可用
        # （用途要求的能力不在模型声明里就如实判不可用，不靠名字猜）。
        usable, reason = self.model_usable(
            model, meta, usage_key=usage_key, secret_present=secret_present
        )
        return {
            "credential_id": credential_id,
            "model_id": model_id,
            "source": source,
            "usable": usable,
            "unavailable_reason": reason,
            "credential_name": name,
        }

    def resolve_usage(self, usage_key: str) -> dict[str, Any] | None:
        """运行时解析：显式分配 → 默认模型 → None（交给 C 的兼容回退）。

        返回的 usable / unavailable_reason 是给调用方的事实：不可用时必须如实失败
        或明确说明，不允许悄悄换成别的模型。
        """
        key = normalize_usage_key(usage_key)
        spec = find_usage(key)
        if spec is None:
            # 自定义标签不产生执行能力：运行时没有对应调用路径。
            return None
        if spec.delivery != DELIVERY_MODEL:
            # 没有独立调用路径（沿用现有处理方式 / 未接通）：历史分配不会改变真实请求。
            return None
        own = self._assignment(spec)
        if self._is_explicit(own):
            return self._resolved_target(
                {"credential_id": own["credential_id"], "model_id": own["model_id"]},
                source="explicit",
                usage_key=key,
            )
        if usage_kind(key) in NON_INHERITING_KINDS:
            # 不继承默认对话模型：没有显式指定就没有目标（调用方沿用现有处理方式）。
            return None
        default = self.get_default()
        if default is not None:
            return self._resolved_target(default, source="default", usage_key=key)
        return None

    def legacy_fallback(self, usage_key: str) -> dict[str, Any] | None:
        """兼容回退：没有显式分配、也没有默认模型时，按基线 tags/policy 找一条连接。

        只用于 main-chat / general-chat / subagent / research 这类原本就有 tags 的用途；
        video / audio / embedding 不继承（调用方应如实报「沿用现有处理方式」）。
        模型名用 providers.resolve_model（与基线完全同一套判断）。
        """
        key = normalize_usage_key(usage_key)
        spec = find_usage(key)
        if spec is None:
            return None
        if spec.delivery != DELIVERY_MODEL:
            # 没有独立调用路径：不让调试接口把一个「实际不会被打到」的目标说成当前目标。
            return None
        if usage_kind(key) in NON_INHERITING_KINDS:
            return None
        tags = list(spec.legacy_tags) or ["main-loop"]
        for tag in tags:
            for meta in self.credentials.list_tagged(tag):
                model_id = _norm(meta.get("default_model")) or _norm(resolve_model(meta))
                if not model_id:
                    continue
                usable, reason = self.model_usable(
                    {
                        "verify_state": meta.get("verify_state"),
                        "verify_applies": False,
                        "capabilities": [],
                    },
                    meta,
                )
                return {
                    "credential_id": str(meta.get("id")),
                    "model_id": model_id,
                    "source": "legacy_tags",
                    "usable": usable,
                    "unavailable_reason": reason,
                    "credential_name": self._connection_name(
                        meta, preset_for_endpoint(meta.get("endpoint"))
                    ),
                }
        return None

    # ------------------------------------------------------------------
    # 模型级验证
    # ------------------------------------------------------------------

    def record_verification(
        self,
        credential_id: str,
        model_id: str,
        *,
        ok: bool,
        reason_code: str | None = None,
        fingerprint: str | None = None,
        transient: bool = False,
    ) -> dict[str, Any]:
        """记录一次针对这个模型的验证结果（不改 Key 级 verify_state）。

        三条规则与 CredentialStore.set_verified 完全同源：
        1. 指纹对不上 = 结论属于另一份配置 → 只留痕（applied=False），不改当前状态；
        2. 临时故障（断网/超时/限流）且当前可用 → 保持原状态（state_kept=True）；
        3. 明确的失败只影响这一个模型，绝不连带判定整把 Key 的所有模型失效。
        """
        credential_id = _norm(credential_id)
        model_id = _norm(model_id)
        if not credential_id or not model_id:
            raise ValueError("credential_id / model_id 不能为空")
        row = self._credential_row(credential_id)
        if row is None:
            raise KeyError(f"credential not found: {credential_id}")
        model = self.get_model(credential_id, model_id)
        if model is None:
            model = self.upsert_model(credential_id, model_id, source=MODEL_SOURCE_MANUAL)
        current = self._model_fingerprint_for_row(row, model_id)
        previous = str(model.get("verify_state") or VERIFY_STATE_UNVERIFIED)
        stale = fingerprint is not None and str(fingerprint) != current
        if stale:
            return {
                "applied": False,
                "state_kept": False,
                "result_state": "historical",
                "state": previous,
                "previous_state": previous,
                "input_fingerprint": fingerprint,
                "current_fingerprint": current,
            }
        if (not ok) and transient and previous in USABLE_VERIFY_STATES:
            return {
                "applied": False,
                "state_kept": True,
                "result_state": "current",
                "state": previous,
                "previous_state": previous,
                "input_fingerprint": fingerprint,
                "current_fingerprint": current,
            }
        state = VERIFY_STATE_VERIFIED if ok else VERIFY_STATE_FAILED
        self.conn.execute(
            "UPDATE credential_models SET verify_state = ?, verified_at = ?, verify_error = ?,"
            " verify_fingerprint = ?, updated_at = ?"
            " WHERE credential_id = ? AND model_id = ?",
            (
                state,
                _now() if ok else None,
                None if ok else (reason_code or ""),
                current,
                _now(),
                credential_id,
                model_id,
            ),
        )
        return {
            "applied": True,
            "state_kept": False,
            "result_state": "current",
            "state": state,
            "previous_state": previous,
            "input_fingerprint": fingerprint,
            "current_fingerprint": current,
        }


__all__ = [
    "ModelCatalogStore",
    "MODEL_SOURCE_DISCOVERED",
    "MODEL_SOURCE_MANUAL",
    "MODEL_SOURCE_LEGACY",
    "MODEL_SOURCES",
    "DEFAULT_MODEL_KEY",
    "DISCOVERY_KEY_PREFIX",
    "MODEL_FINGERPRINT_VERSION",
    "USAGE_KIND_CHAT",
    "USAGE_KIND_VISION",
    "USAGE_KIND_VIDEO",
    "USAGE_KIND_AUDIO",
    "USAGE_KIND_EMBEDDING",
    "USAGE_KIND_BACKGROUND",
    "USAGE_KIND_CUSTOM",
    "USAGE_KIND_BY_KEY",
    "NON_INHERITING_KINDS",
    "compute_model_fingerprint",
    "usage_kind",
]
