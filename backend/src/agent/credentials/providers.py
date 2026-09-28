"""厂商预设：**唯一来源**。

为什么单独一个模块：以前这份列表长在 `services/identify.py`（自动识别候选表）里，
前端又各写一份；两边的 base_url / 模型名一旦漂移，用户就会看到「界面写着 A 家、
请求实际打到 B 家」。现在定义一次，后端 API（`/api/credentials/providers`）与界面
都从这里取。

三条硬约束（产品要求）：

1. 厂商由用户明确选择；预设只提供「选完之后的默认值」，不做自动识别；
2. `suggested_model` 只是**推荐值**，不是可用性证明 —— 它是否真的能调用，由
   `services/verify.py` 的一次真实调用决定；
3. `key_prefixes` 只用于**本地提示**（「看起来像 DeepSeek 的 Key，要不要选它？」），
   不触发任何网络请求，也不会把 Key 发给别的厂商。
"""

from __future__ import annotations

from dataclasses import dataclass

# 用户需要一眼看出 Key 会被发到哪里：官方端点 / 第三方转发 / 自定义地址。
CATEGORY_OFFICIAL = "official"
CATEGORY_AGGREGATOR = "aggregator"
CATEGORY_CUSTOM = "custom"

CATEGORY_LABELS = {
    CATEGORY_OFFICIAL: "官方服务",
    CATEGORY_AGGREGATOR: "第三方转发",
    CATEGORY_CUSTOM: "自定义服务",
}

KIND_OPENAI = "openai"
KIND_ANTHROPIC = "anthropic"
KINDS = (KIND_OPENAI, KIND_ANTHROPIC)

CUSTOM_PROVIDER_ID = "custom"


@dataclass(frozen=True)
class ProviderPreset:
    id: str
    name: str
    kind: str
    base_url: str
    suggested_model: str
    key_prefixes: tuple[str, ...] = ()
    # 用于核对 Key 是否被接受（auth-sensitive）的路径；有的服务 /models 是公开目录
    auth_path: str = "/models"
    category: str = CATEGORY_OFFICIAL
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "base_url": self.base_url,
            "suggested_model": self.suggested_model,
            "category": self.category,
            "category_label": CATEGORY_LABELS.get(self.category, ""),
            "note": self.note,
        }


# 顺序即界面上的展示顺序：先用得多的排前面，聚合/转发类排在官方之后。
PRESETS: tuple[ProviderPreset, ...] = (
    ProviderPreset(
        "deepseek", "DeepSeek", KIND_OPENAI, "https://api.deepseek.com/v1",
        "deepseek-v4-flash", ("sk-",),
    ),
    ProviderPreset(
        "openai", "OpenAI", KIND_OPENAI, "https://api.openai.com/v1",
        "gpt-5.6-luna", ("sk-proj-", "sk-"),
    ),
    ProviderPreset(
        "anthropic", "Anthropic", KIND_ANTHROPIC, "https://api.anthropic.com/v1",
        "claude-sonnet-4", ("sk-ant-",),
    ),
    ProviderPreset(
        "glm", "GLM（智谱）", KIND_OPENAI, "https://open.bigmodel.cn/api/paas/v4",
        "glm-5.2",
    ),
    ProviderPreset(
        "kimi", "Kimi（月之暗面）", KIND_OPENAI, "https://api.moonshot.cn/v1",
        "kimi-k2",
    ),
    ProviderPreset(
        "minimax", "MiniMax", KIND_OPENAI, "https://api.minimax.chat/v1",
        "minimax-m3",
    ),
    ProviderPreset(
        "mimo", "MiMo（小米）", KIND_OPENAI, "https://api.xiaomimimo.com/v1",
        "mimo-v2.5",
    ),
    ProviderPreset(
        "gemini", "Google Gemini", KIND_OPENAI,
        "https://generativelanguage.googleapis.com/v1beta/openai/",
        "gemini-2.5-pro", ("AIza",),
    ),
    ProviderPreset(
        "qwen", "Qwen（DashScope）", KIND_OPENAI,
        "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen3-max",
    ),
    ProviderPreset(
        "groq", "Groq", KIND_OPENAI, "https://api.groq.com/openai/v1",
        "llama-4", ("gsk_",),
    ),
    ProviderPreset(
        "xai", "xAI Grok", KIND_OPENAI, "https://api.x.ai/v1",
        "grok-4", ("xai-",),
    ),
    ProviderPreset(
        "mistral", "Mistral", KIND_OPENAI, "https://api.mistral.ai/v1",
        "mistral-large",
    ),
    ProviderPreset(
        "openrouter", "OpenRouter", KIND_OPENAI, "https://openrouter.ai/api/v1",
        "deepseek/deepseek-v4-flash", ("sk-or-",),
        auth_path="/key", category=CATEGORY_AGGREGATOR,
        note="/models 是公开目录，核对 Key 走 /key",
    ),
    ProviderPreset(
        "siliconflow", "SiliconFlow（硅基流动）", KIND_OPENAI,
        "https://api.siliconflow.cn/v1", "Qwen/Qwen3-32B",
        category=CATEGORY_AGGREGATOR, note="同一个地址转发多家模型",
    ),
    ProviderPreset(
        "aihubmix", "AIHubMix", KIND_OPENAI, "https://api.aihubmix.com/v1",
        "", category=CATEGORY_AGGREGATOR, note="没有可靠的默认模型，需要自己选",
    ),
    ProviderPreset(
        "deepbricks", "DeepBricks", KIND_OPENAI, "https://api.deepbricks.ai/v1",
        "", category=CATEGORY_AGGREGATOR, note="没有可靠的默认模型，需要自己选",
    ),
)

# 「其他 / 自定义服务」：地址、模型、协议都由用户填，必填项不藏起来。
CUSTOM_PRESET = ProviderPreset(
    CUSTOM_PROVIDER_ID, "其他 / 自定义服务", KIND_OPENAI, "", "",
    category=CATEGORY_CUSTOM,
    note="自建网关、公司内网网关、或上面没有列出的服务",
)


def list_presets() -> list[ProviderPreset]:
    return list(PRESETS)


def find_preset(provider_id: str | None) -> ProviderPreset | None:
    if not provider_id:
        return None
    if provider_id == CUSTOM_PRESET.id:
        return CUSTOM_PRESET
    for preset in PRESETS:
        if preset.id == provider_id:
            return preset
    return None


def _norm(url: str | None) -> str:
    return (url or "").strip().rstrip("/").lower()


def preset_for_endpoint(endpoint: str | None) -> ProviderPreset | None:
    """按地址反查预设（历史凭据没有存 kind/模型时用得上）。

    只做精确的 base_url 匹配，不做域名猜测：猜错会把内容发给别家。
    """
    target = _norm(endpoint)
    if not target:
        return None
    for preset in PRESETS:
        if _norm(preset.base_url) == target:
            return preset
    return None


def key_hint(key: str) -> ProviderPreset | None:
    """**纯本地**的 Key 前缀提示；不联网、不证明任何事。

    同一前缀可能对应多家服务（`sk-`），取前缀最长的那条当提示。
    """
    text = (key or "").strip()
    if not text:
        return None
    best: ProviderPreset | None = None
    best_len = 0
    for preset in PRESETS:
        for prefix in preset.key_prefixes:
            if text.startswith(prefix) and len(prefix) > best_len:
                best, best_len = preset, len(prefix)
    return best


MODEL_SUGGESTION_NOTE = "预设里的模型名只是推荐值；是否真的可用，由保存后的一次实际调用决定"

# 历史兼容：本次改动之前建的凭据可能没有地址/模型，老行为是兜底到 OpenAI 官方
# 地址与一个默认模型。只有这类「旧数据」才继续走这个兜底；新建凭据必须带上
# 地址与模型（API 层强制），因此**新配置永远不会猜发送目标**。
LEGACY_ENDPOINT = "https://api.openai.com/v1"
LEGACY_MODEL = "gpt-4o-mini"


def _is_legacy(meta: dict | None) -> bool:
    return bool(meta) and str(meta.get("verify_state") or "") == "legacy"


def resolve_endpoint(meta: dict | None) -> str | None:
    """这条凭据实际要请求的地址；没有就不猜（旧数据除外）。"""
    if not meta:
        return None
    endpoint = str(meta.get("endpoint") or "").strip()
    if endpoint:
        return endpoint
    return LEGACY_ENDPOINT if _is_legacy(meta) else None


def resolve_model(meta: dict | None) -> str | None:
    """这条凭据实际要用的模型；没有就要求用户补一个（旧数据除外）。"""
    if not meta:
        return None
    model = str(meta.get("default_model") or "").strip()
    if model:
        return model
    preset = preset_for_endpoint(meta.get("endpoint"))
    if preset and preset.suggested_model:
        return preset.suggested_model
    return LEGACY_MODEL if _is_legacy(meta) else None


def uses_anthropic(meta: dict | None, endpoint: str | None) -> bool:
    """连接协议：以存下来的 kind 为准，历史数据按地址回退判断。

    「测试」与「正式对话」必须走同一套判断，否则同一把 Key 两边结论会不一样。
    """
    from agent.adapters.anthropic import is_anthropic_endpoint

    kind = str((meta or {}).get("kind") or "").strip()
    if kind:
        return kind == KIND_ANTHROPIC
    return is_anthropic_endpoint(endpoint)
