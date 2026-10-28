/**
 * 设置搜索索引（静态、离线、只读）。
 *
 * 索引里**只有设置名称、说明、用途名称与高级参数名**：
 * 不包含任何 Key 明文、Key 片段、服务商返回的数据，也不包含后端运行时值。
 * 这条不是「注意一下」而是硬约束 —— 搜索面板会把命中的文字原样显示出来，
 * 一旦把凭据写进索引，它就等于一个可搜索的密钥泄露面。
 *
 * 每个条目对应一个 `data-settings-key`（见 _STG-MIGRATION-A.md）：
 * 点击结果时 shell 按 key 定位到控件；`fold` 指向必须自动展开的折叠组 id。
 */
import type { SettingsCategoryId } from "./types";

export interface SettingsSearchEntry {
  /** 稳定 key：与 DOM 上的 data-settings-key 完全一致 */
  key: string;
  title: string;
  category: SettingsCategoryId;
  description: string;
  /** 同义词/英文名，让「theme」「cps」这类输入也能命中 */
  keywords?: string[];
  /** 目标在折叠组里时必须先展开的组 id（AdvGroup 的 data-settings-fold） */
  fold?: string | null;
}

/**
 * 用途目录（与后端 `backend/src/agent/credentials/usages.py` 同口径）。
 *
 * 为什么这里也写一份：搜索索引必须能在没有加载模型目录（B 的面板尚未取数）时
 * 也能搜到「记忆整理」这类用途名。key 与 label 来自共享契约 §6.2，
 * 只做展示用途；任何执行路径都不从这里读（A 不直接调用模型 API）。
 */
export const SETTINGS_USAGE_ENTRIES: { key: string; label: string; group: string; note: string }[] = [
  { key: "main-chat", label: "主对话", group: "primary", note: "日常对话与工具调用使用这一档" },
  { key: "general-chat", label: "通用对话", group: "primary", note: "与主对话共用同一条调用路径" },
  { key: "code", label: "写代码", group: "primary", note: "当前没有独立代码模型路径，跟随主对话" },
  { key: "subagent", label: "子任务", group: "primary", note: "子智能体使用这一档" },
  { key: "research", label: "研究", group: "primary", note: "研究模式的子任务使用这一档" },
  { key: "vision", label: "看图 / 视觉", group: "special", note: "只有真能读图的模型才该放这里" },
  { key: "video", label: "视频", group: "special", note: "沿用现有处理方式，不继承默认对话模型" },
  { key: "audio", label: "语音", group: "special", note: "沿用现有处理方式，不继承默认对话模型" },
  { key: "embedding", label: "向量 / 嵌入", group: "special", note: "更换外部嵌入模型会影响既有索引维度" },
  { key: "memory-organize", label: "记忆整理", group: "background", note: "后台整理记忆分段时使用" },
  { key: "knowledge-distill", label: "知识提炼", group: "background", note: "从对话里提炼知识候选时使用" },
  { key: "entity-extract", label: "实体提炼", group: "background", note: "抽取实体与关系时使用" },
];

const USAGE_ENTRIES: SettingsSearchEntry[] = SETTINGS_USAGE_ENTRIES.map((usage) => ({
  key: `models.usage.${usage.key}`,
  title: `用途：${usage.label}`,
  category: "models",
  description: usage.note,
  keywords: ["用途", "分配", usage.key, usage.label],
  // 后台用途默认折叠在「后台用途」分组里，定位前要先展开
  fold: usage.group === "background" ? "models.usage.background" : null,
}));

export const SETTINGS_SEARCH_INDEX: SettingsSearchEntry[] = [
  /* ---------------- 常规 ---------------- */
  {
    key: "regular.theme",
    title: "主题",
    category: "regular",
    description: "跟随系统，或固定暗紫晶 / 净白",
    keywords: ["theme", "外观", "暗色", "亮色", "深色", "浅色"],
  },
  {
    key: "regular.motion",
    title: "动画",
    category: "regular",
    description: "跟随系统，或固定标准 / 减少动画",
    keywords: ["motion", "减少动画", "reduced", "过渡"],
  },
  {
    key: "regular.scrollbar",
    title: "滚动指示条",
    category: "regular",
    description: "自动显示 / 始终显示；只作用于允许显示且实际溢出的区域",
    keywords: ["scrollbar", "滚动条", "指示条"],
  },
  {
    key: "regular.speed",
    title: "输出速度",
    category: "regular",
    description: "助手回答的打字机显示速度（25 / 50 / 75 字/秒）",
    keywords: ["cps", "打字机", "typewriter", "速度"],
  },
  {
    key: "regular.onboarding",
    title: "设置助手",
    category: "regular",
    description: "重新运行首次引导（已设置项会预填，不产生重复记录）",
    keywords: ["引导", "onboarding", "向导", "首次设置"],
  },
  {
    key: "regular.window.planet-hide",
    title: "星球入口贴靠后自动隐藏",
    category: "regular",
    description: "关闭＝入口常显（不是禁用）",
    keywords: ["浮动", "贴边", "隐藏", "planet"],
  },
  {
    key: "regular.window.settings-hide",
    title: "设置入口贴靠后自动隐藏",
    category: "regular",
    description: "关闭＝入口常显（不是禁用）",
    keywords: ["浮动", "贴角", "隐藏", "settings"],
  },
  {
    key: "regular.window.reset-layout",
    title: "还原默认布局",
    category: "regular",
    description: "把所有浮动组件还原到默认位置并关闭贴靠隐藏",
    keywords: ["重置", "布局", "浮动", "reset"],
  },
  {
    key: "regular.window.close-behavior",
    title: "关闭主窗口时",
    category: "regular",
    description: "每次询问 / 最小化到托盘 / 直接退出",
    keywords: ["关闭", "托盘", "退出", "close", "quit", "tray"],
  },
  {
    key: "regular.advanced.diagnostics",
    title: "诊断（开发者模式）",
    category: "regular",
    description: "显示 FPS / WebGL / 视角等图形诊断与每条消息的 token 明细",
    keywords: ["开发者", "developer", "诊断", "token", "fps"],
    fold: "regular.advanced",
  },

  /* ---------------- 模型与服务（B 的面板 + 搜索配置） ---------------- */
  {
    key: "models.key.create",
    title: "新建 Key / 接入服务商",
    category: "models",
    description: "选择服务商 → 填写 Key → 自动获取模型 → 分配用途",
    keywords: ["凭据", "密钥", "credential", "新增", "key"],
  },
  {
    key: "models.key.list",
    title: "Key 列表与筛选",
    category: "models",
    description: "全部 / 已启用 / 已停用 / 已撤销 / 已过期",
    keywords: ["凭据", "筛选", "状态", "credential"],
  },
  {
    key: "models.key.detail",
    title: "Key 管理（编辑 / 换钥 / 启停 / 撤销 / 删除）",
    category: "models",
    description: "编辑连接信息、更换 Key、启用停用、撤销、删除；撤销与删除不可恢复",
    keywords: ["凭据", "管理", "撤销", "删除", "停用"],
  },
  {
    key: "models.key.models",
    title: "Key 详情·模型目录",
    category: "models",
    description: "一把 Key 下已知的模型、分页刷新与按模型验证",
    keywords: ["模型", "目录", "catalog", "发现", "discover"],
  },
  {
    key: "models.default-model",
    title: "默认模型",
    category: "models",
    description: "默认使用哪个 Key 下的哪个模型；未单独分配的用途跟随它",
    keywords: ["默认", "模型", "default", "model"],
  },
  {
    key: "models.usage.background",
    title: "后台用途（记忆整理 / 知识提炼 / 实体提炼）",
    category: "models",
    description: "后台处理使用哪一档模型；默认跟随默认模型",
    keywords: ["后台", "折叠", "用途", "memory-organize"],
    fold: "models.usage.background",
  },
  ...USAGE_ENTRIES,
  {
    key: "models.search.status",
    title: "联网搜索状态",
    category: "models",
    description: "区分「已配置」「未检查」「检查通过 / 检查失败」，不把已配置说成可用",
    keywords: ["搜索", "状态", "连通性", "search"],
  },
  {
    key: "models.search.keyless",
    title: "免密钥联网搜索",
    category: "models",
    description: "Exa / Parallel 免费搜索服务，失败回退 DuckDuckGo；不是联网搜索总开关",
    keywords: ["免密钥", "keyless", "exa", "parallel", "duckduckgo"],
  },
  {
    key: "models.search.bocha",
    title: "博查 API Key",
    category: "models",
    description: "配置 / 替换 / 清除；留空保存不会清除，清除必须显式操作",
    keywords: ["博查", "bocha", "key", "搜索密钥"],
  },
  {
    key: "models.search.check",
    title: "联网搜索连接检查",
    category: "models",
    description: "真实打到端点的低影响检查；检查通过才显示通过",
    keywords: ["检查", "连接", "测试", "连通性"],
  },
  {
    key: "models.search.advanced.searxng",
    title: "自建 SearXNG 地址",
    category: "models",
    description: "可选；需要有可达的自建或自备实例",
    keywords: ["searxng", "地址", "搜索"],
    fold: "models.search.advanced",
  },
  {
    key: "models.search.advanced.top-k",
    title: "默认返回条数",
    category: "models",
    description: "每次搜索返回的结果数（1-20）",
    keywords: ["top_k", "条数", "结果数", "搜索"],
    fold: "models.search.advanced",
  },
  {
    key: "models.search.advanced.max-fetch-chars",
    title: "读正文字符预算",
    category: "models",
    description: "抓取网页正文的字符上限（1000-40000）",
    keywords: ["正文", "字符", "预算", "max_fetch_chars"],
    fold: "models.search.advanced",
  },

  /* ---------------- 记忆与整理 ---------------- */
  {
    key: "memory.fragment.tier",
    title: "记忆封块分档",
    category: "memory",
    description: "短 5 / 标准 10 / 长 15 / 自定义 1-30 轮",
    keywords: ["记忆", "分档", "封块", "轮数", "fragment"],
  },
  {
    key: "memory.fragment.custom-turns",
    title: "记忆封块自定义轮数",
    category: "memory",
    description: "自定义分档下的单段轮数（1-30）",
    keywords: ["自定义", "轮数", "记忆"],
  },
  {
    key: "memory.fragment.max-tokens",
    title: "单段长度目标",
    category: "memory",
    description: "内容长度兜底上限（2000-200000）；到点分段不代表阶段完成",
    keywords: ["长度", "token", "分段", "记忆"],
  },
  {
    key: "memory.maintenance.enabled",
    title: "后台整理开关",
    category: "memory",
    description: "启用后台离线整理记忆与知识",
    keywords: ["维护", "整理", "maintenance", "离线"],
  },
  {
    key: "memory.maintenance.interval",
    title: "后台整理间隔",
    category: "memory",
    description: "每次后台整理的间隔（1-720 小时）",
    keywords: ["间隔", "小时", "维护"],
  },
  {
    key: "memory.maintenance.run",
    title: "立即整理",
    category: "memory",
    description: "现在启动一次后台整理（危险操作需要确认）",
    keywords: ["立即", "运行", "整理", "维护", "run"],
  },
  {
    key: "memory.advanced.max-iterations",
    title: "单轮迭代上限",
    category: "memory",
    description: "每轮任务最多重新决策的次数（1-1000）",
    keywords: ["迭代", "深度", "iterations", "对话深度"],
    fold: "memory.advanced",
  },
  {
    key: "memory.advanced.output-budget",
    title: "输出 token 预算",
    category: "memory",
    description: "单轮模型输出累计上限（0 表示不限）",
    keywords: ["输出", "预算", "token", "budget"],
    fold: "memory.advanced",
  },

  /* ---------------- 工具与权限 ---------------- */
  {
    key: "tools.permission",
    title: "权限模式",
    category: "tools",
    description: "只读 / 受限 / 完全无限制；只有你的选择能修改",
    keywords: ["权限", "审批", "permission", "安全"],
  },
  {
    key: "tools.workspace",
    title: "工作区根目录",
    category: "tools",
    description: "QIO 默认可访问的目录；之外默认要审批",
    keywords: ["工作区", "目录", "根目录", "workspace"],
  },
  {
    key: "tools.records.record-outputs",
    title: "保存工具输出全文",
    category: "tools",
    description: "关闭后仍保留参数、状态、失败原因与耗时",
    keywords: ["工具", "记录", "输出", "history"],
  },
  {
    key: "tools.records.output-retention",
    title: "输出保留天数",
    category: "tools",
    description: "到期只清空输出全文；0 表示永久保留",
    keywords: ["保留", "天数", "输出", "retention"],
  },
  {
    key: "tools.records.record-retention",
    title: "记录保留天数",
    category: "tools",
    description: "到期删除整条工具记录；0 表示永久保留（默认）",
    keywords: ["保留", "天数", "记录", "retention"],
  },
  {
    key: "tools.effective",
    title: "当前生效参数",
    category: "tools",
    description: "只读展示已保存且真实生效的值，便于排查",
    keywords: ["生效", "参数", "只读", "effective"],
  },

  /* ---------------- 关于与更新 ---------------- */
  {
    key: "about.version",
    title: "版本与数据说明",
    category: "about",
    description: "应用版本、BYOK 与本机数据说明",
    keywords: ["版本", "version", "byok", "隐私"],
  },
  {
    key: "about.update",
    title: "更新",
    category: "about",
    description: "检查、下载与安装更新；何时重启由你决定",
    keywords: ["更新", "升级", "update"],
  },
  {
    key: "about.license",
    title: "许可证（MIT）",
    category: "about",
    description: "QIO 完整许可全文（仓库 LICENSE，离线内联）",
    keywords: ["许可", "license", "mit", "授权"],
    fold: "about.license",
  },
  {
    key: "about.third-party",
    title: "第三方开源组件声明",
    category: "about",
    description: "组件清单与每个许可证全文（离线内联，生成物）",
    keywords: ["第三方", "开源", "声明", "notices", "组件"],
    fold: "about.third-party",
  },
];

function haystack(entry: SettingsSearchEntry): string {
  return [entry.title, entry.description, entry.key, ...(entry.keywords ?? [])]
    .join("\n")
    .toLowerCase();
}

/** 单个条目与查询的匹配得分；0 表示不匹配。标题命中权重高于说明/关键词。 */
function scoreEntry(entry: SettingsSearchEntry, query: string): number {
  const q = query.trim().toLowerCase();
  if (!q) return 1;
  const title = entry.title.toLowerCase();
  if (title.startsWith(q)) return 100;
  if (title.includes(q)) return 80;
  if ((entry.keywords ?? []).some((word) => word.toLowerCase().includes(q))) return 60;
  if (entry.key.toLowerCase().includes(q)) return 40;
  const text = haystack(entry);
  // 多词查询按「全部出现」判定：搜「搜索 密钥」不该命中无关条目
  const parts = q.split(/\s+/).filter(Boolean);
  if (parts.length > 1) return parts.every((part) => text.includes(part)) ? 20 : 0;
  return text.includes(q) ? 10 : 0;
}

/** 全量搜索（稳定排序：分数降序，同分保持索引里的顺序）。 */
export function searchSettings(query: string): SettingsSearchEntry[] {
  const q = query.trim();
  if (!q) return [];
  return SETTINGS_SEARCH_INDEX.map((entry, index) => ({ entry, index, score: scoreEntry(entry, q) }))
    .filter((item) => item.score > 0)
    .sort((a, b) => b.score - a.score || a.index - b.index)
    .map((item) => item.entry);
}

/** 按分类分组，结果面板按分类给标题（顺序与导航一致）。 */
export function groupSearchResults(
  entries: SettingsSearchEntry[],
): { category: SettingsCategoryId; entries: SettingsSearchEntry[] }[] {
  const order: SettingsCategoryId[] = ["regular", "models", "memory", "tools", "about"];
  const groups = new Map<SettingsCategoryId, SettingsSearchEntry[]>();
  for (const entry of entries) {
    const list = groups.get(entry.category) ?? [];
    list.push(entry);
    groups.set(entry.category, list);
  }
  return order
    .filter((id) => groups.has(id))
    .map((id) => ({ category: id, entries: groups.get(id)! }));
}
