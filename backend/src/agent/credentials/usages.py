"""用途目录：**唯一来源**（后端与前端共用同一份口径）。

为什么单独一个模块
------------------
基线把"这条凭据用来干什么"塞在 `credentials.tags` 里（`main-loop` / `vision` …），
于是一把 Key 只能整体被某个用途占用；同一个 Key 下不同模型无法分给不同用途，
同名模型来自不同 Key 时也无法区分。本轮改成：

    连接（credentials，一行一把 Key）
        └── 模型（credential_models，一把 Key 多个模型）
                └── 用途（model_usages，一行一个用途 → (credential_id, model_id|null)）

`model_id` 为空 = **跟随默认模型**；非空 = 用户显式指定，换默认模型不动它。

这里的常量是**纯数据**，不导入 storage，避免任何循环依赖；迁移、API、运行时
（`services/app.py` 的用途解析）与前端 `frontend/src/services/modelCatalog.ts`
都从这里取同一份 key 与顺序。

三条硬语义（不是文案问题）
---------------------------
1. **用途标签不等于已验证的能力**。`vision` 用途能被分配，不等于某个模型真能读图；
   是否可用由适配器能力探测与按模型记录的验证结果决定（见 `credential_models`）。
2. **`video` / `audio` / `embedding` 不继承默认对话模型**。把对话模型塞给视频/语音/嵌入
   会直接调用失败或产生错维度向量，所以它们的 `inherits_default` 是 False，
   未显式指定时如实报"沿用现有处理方式"。
3. **自定义标签不产生新执行能力**。`custom:<tag>` 只承载历史标签的可见性与映射，
   不会被任何执行路径拿去做"多出来的一档"。
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "UsageSpec",
    "USAGES",
    "USAGE_KEYS",
    "USAGE_BY_KEY",
    "CUSTOM_USAGE_PREFIX",
    "LEGACY_TAG_TO_USAGE",
    "PRESET_USAGE_TAGS",
    "SYSTEM_PROMPT_USAGES",
    "DELIVERY_MODEL",
    "DELIVERY_EXISTING",
    "DELIVERY_UNSUPPORTED",
    "DELIVERIES",
    "list_usages",
    "find_usage",
    "usage_key_for_legacy_tag",
    "legacy_tags_for_usage",
    "is_custom_usage",
    "custom_usage_key",
    "custom_tag_of",
    "normalize_usage_key",
]

#: 自定义用途 key 前缀：`custom:写作助手` 这种。历史标签原文保留在前缀之后。
CUSTOM_USAGE_PREFIX = "custom:"

#: `UsageSpec.delivery` 的三种取值。
DELIVERY_MODEL = "model"          # 真正走模型调用：分配会真的生效
DELIVERY_EXISTING = "existing"    # 沿用现有处理方式（视频 / 语音 / 嵌入）
DELIVERY_UNSUPPORTED = "unsupported"  # 当前没有独立调用路径（研究 / 自定义标签）
DELIVERIES = (DELIVERY_MODEL, DELIVERY_EXISTING, DELIVERY_UNSUPPORTED)

#: 用途分组，决定界面上的排列顺序与折叠方式。
GROUP_PRIMARY = "primary"      # 日常对话路径
GROUP_SPECIAL = "special"      # 需要专门能力的用途
GROUP_BACKGROUND = "background"  # 后台处理
GROUP_LEGACY = "legacy"        # 历史自定义标签


@dataclass(frozen=True)
class UsageSpec:
    """一个用途的静态定义（不含任何运行时/密钥信息）。"""

    key: str
    label: str
    group: str
    #: 未显式指定时是否跟随默认模型。
    inherits_default: bool
    #: 基线 `credentials.tags` 里对应这个用途的标签（迁移与兼容回退用）。
    legacy_tags: tuple[str, ...] = ()
    #: 真实调用入口的一句话说明；**只有列在这里的入口才算接通**。
    call_path: str = ""
    #: 该用途对模型能力的最低要求（仅用于界面提示与候选过滤，不冒称已验证）。
    requires_capabilities: tuple[str, ...] = ()
    #: 给用户看的说明（一句话，不含实现细节）。
    note: str = ""
    #: 是否属于"日常入口"（决定它出现在默认可见区域还是折叠区域）。
    daily: bool = True
    #: 这个用途到底怎么被用（见 DELIVERY_* 常量）：
    #: `model` 分配会真的生效；`existing` 沿用现有处理方式、不继承默认对话模型；
    #: `unsupported` 没有独立调用路径 —— 界面不得把它画成已生效的选择器。
    delivery: str = DELIVERY_MODEL

    def to_dict(self) -> dict:
        return {
            "usage_key": self.key,
            "label": self.label,
            "group": self.group,
            "inherits_default": self.inherits_default,
            "legacy_tags": list(self.legacy_tags),
            "call_path": self.call_path,
            "requires_capabilities": list(self.requires_capabilities),
            "note": self.note,
            "daily": self.daily,
            "delivery": self.delivery,
        }


# 顺序即界面顺序；daily=True 的在主区域，其余折叠。
USAGES: tuple[UsageSpec, ...] = (
    UsageSpec(
        "main-chat",
        "主对话",
        GROUP_PRIMARY,
        True,
        legacy_tags=("main-loop",),
        call_path="services/app.py::resolve_main_target → turn_orchestrator 主循环",
        note="日常对话与工具调用使用这一档",
    ),
    # 基线只有一条主循环解析路径（`resolve_main_ref()` 用 `MAIN_LOOP_USAGE_TAGS`），
    # 主对话与通用对话在真实调用上**完全等价**，因此日常入口合并展示为一行；
    # 但两个 key 都保留在数据里，历史映射不丢（见 LEGACY_TAG_TO_USAGE）。
    UsageSpec(
        "general-chat",
        "通用对话",
        GROUP_PRIMARY,
        True,
        legacy_tags=(),
        call_path="与主对话同一路径（resolve_main_target）",
        note="与主对话共用同一条调用路径，日常入口已合并；单独指定时优先于主对话",
    ),
    UsageSpec(
        "code",
        "写代码",
        GROUP_PRIMARY,
        True,
        legacy_tags=(),
        call_path="与主对话同一路径（工具调用由 loop 决定）",
        note="当前没有独立的代码模型调用路径，跟随主对话",
    ),
    UsageSpec(
        "subagent",
        "子任务",
        GROUP_PRIMARY,
        True,
        legacy_tags=("subagent",),
        call_path="tools/subagent_tool.py::SubagentTool（adapter_factory）",
    ),
    UsageSpec(
        "research",
        "研究",
        GROUP_PRIMARY,
        True,
        legacy_tags=("research",),
        call_path="（未接通：无独立调用路径，仅 policy 回退标签）",
        note="当前没有独立的研究模型调用路径：只是历史标签与 policy 回退，选择器不得表现为已生效",
        delivery=DELIVERY_UNSUPPORTED,
        daily=False,
    ),
    UsageSpec(
        "vision",
        "看图 / 视觉",
        GROUP_SPECIAL,
        False,
        legacy_tags=("vision",),
        call_path="services/app.py::build_vision_adapter → 图片理解",
        requires_capabilities=("vision",),
        note="只有真能读图的模型才该放这里；未指定时回落主对话并如实记录",
        daily=False,
    ),
    UsageSpec(
        "video",
        "视频",
        GROUP_SPECIAL,
        False,
        legacy_tags=("video",),
        call_path="（未接通：沿用现有处理方式）",
        note="沿用现有处理方式；不会继承默认对话模型",
        delivery=DELIVERY_EXISTING,
        daily=False,
    ),
    UsageSpec(
        "audio",
        "语音",
        GROUP_SPECIAL,
        False,
        legacy_tags=("audio",),
        call_path="（未接通：沿用现有处理方式）",
        note="沿用现有处理方式；不会继承默认对话模型",
        delivery=DELIVERY_EXISTING,
        daily=False,
    ),
    UsageSpec(
        "embedding",
        "向量 / 嵌入",
        GROUP_SPECIAL,
        False,
        legacy_tags=("embedding",),
        call_path="services/retrieval.py + selector（沿用现有本地/远程方案）",
        requires_capabilities=("embedding",),
        note="更换外部嵌入模型会影响既有索引维度，需要重建确认",
        delivery=DELIVERY_EXISTING,
        daily=False,
    ),
    UsageSpec(
        "memory-organize",
        "记忆整理",
        GROUP_BACKGROUND,
        True,
        legacy_tags=(),
        call_path="services/memory_lifecycle.py::consolidate + maintenance.py",
        note="后台整理记忆分段时使用",
        daily=False,
    ),
    UsageSpec(
        "knowledge-distill",
        "知识提炼",
        GROUP_BACKGROUND,
        True,
        legacy_tags=(),
        call_path="services/derived_tasks.py::知识提炼任务",
        note="从对话里提炼知识候选时使用",
        daily=False,
    ),
    UsageSpec(
        "entity-extract",
        "实体提炼",
        GROUP_BACKGROUND,
        True,
        legacy_tags=(),
        call_path="entities/extract.py::run_entity_extraction",
        note="抽取实体与关系时使用",
        daily=False,
    ),
)

USAGE_KEYS: tuple[str, ...] = tuple(u.key for u in USAGES)

USAGE_BY_KEY: dict[str, UsageSpec] = {u.key: u for u in USAGES}

#: 基线 `credentials.tags` 里的预设标签 → 用途 key。
#: `main-loop` 同时映射到 main-chat 与 general-chat（同一调用路径，历史映射不丢）。
LEGACY_TAG_TO_USAGE: dict[str, tuple[str, ...]] = {
    "main-loop": ("main-chat", "general-chat"),
    "subagent": ("subagent",),
    "vision": ("vision",),
    "video": ("video",),
    "audio": ("audio",),
    "research": ("research",),
    "embedding": ("embedding",),
}

#: 属于"预设"的标签：其余标签视为历史自定义标签（保留可见，不产生新能力）。
PRESET_USAGE_TAGS: tuple[str, ...] = tuple(LEGACY_TAG_TO_USAGE)

#: 有真实系统提示词/工具集合的用途 —— 目前只有主对话一族。
SYSTEM_PROMPT_USAGES: tuple[str, ...] = ("main-chat", "general-chat", "code", "subagent")


def list_usages() -> list[UsageSpec]:
    return list(USAGES)


def find_usage(key: str | None) -> UsageSpec | None:
    text = str(key or "").strip()
    if not text:
        return None
    return USAGE_BY_KEY.get(text)


def is_custom_usage(key: str | None) -> bool:
    return str(key or "").startswith(CUSTOM_USAGE_PREFIX)


def custom_usage_key(tag: str) -> str:
    text = str(tag or "").strip()
    if not text:
        raise ValueError("自定义用途标签不能为空")
    return CUSTOM_USAGE_PREFIX + text


def custom_tag_of(key: str | None) -> str:
    text = str(key or "")
    return text[len(CUSTOM_USAGE_PREFIX):] if is_custom_usage(text) else ""


def normalize_usage_key(key: str | None) -> str:
    """把用途 key 归一化：预设 key 原样返回，自定义标签补前缀。

    不做模糊匹配（`main` 不会被猜成 `main-chat`）：用途决定请求发给哪家哪模型，
    猜错就是把内容发给用户没选过的地方。
    """
    text = str(key or "").strip()
    if not text:
        raise ValueError("usage_key 不能为空")
    if find_usage(text) is not None:
        return text
    if is_custom_usage(text):
        return custom_usage_key(custom_tag_of(text))
    return custom_usage_key(text)


def usage_key_for_legacy_tag(tag: str) -> tuple[str, ...]:
    """历史标签 → 用途 key 列表（未知标签变成 `custom:<tag>`）。"""
    text = str(tag or "").strip()
    if not text:
        return ()
    mapped = LEGACY_TAG_TO_USAGE.get(text)
    if mapped:
        return mapped
    return (custom_usage_key(text),)


def legacy_tags_for_usage(key: str) -> tuple[str, ...]:
    """用途 key → 历史 `credentials.tags` 里的标签（兼容回退用）。"""
    text = str(key or "").strip()
    spec = find_usage(text)
    if spec is not None:
        return spec.legacy_tags
    tag = custom_tag_of(text)
    return (tag,) if tag else ()
