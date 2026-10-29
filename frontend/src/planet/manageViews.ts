/**
 * 记忆星球页「管理区」的纯逻辑（无副作用、无 DOM）。
 *
 * 为什么单独放一个模块：视图状态机、话题分组、全局搜索分组、分页拼接这些规则
 * 是本次改版里最容易悄悄退化的部分（比如「旧响应覆盖新关键词」「已结束话题混进主列表」）。
 * 把它们做成纯函数，单测直接验规则，不必挂整棵组件树。
 */
import type { EntityCard, KnowledgeItem, TopicFingerprint } from "../services/api";

/** 管理区三标签页（契约 §3.5：不是三栏同时展开） */
export type ManageTab = "memory" | "knowledge" | "entity";
/** 管理内容区当前承载的视图：三标签 / 待处理 / 整理记录 / 全局搜索 */
export type ManageView = "tabs" | "pending" | "records" | "search";
/** 页面工具行 + 外部入口能请求的落点 */
export type PlanetInitialTab = "topic" | "knowledge" | "entity" | "memory";

export interface ManageRequest {
  view: ManageView;
  tab: ManageTab;
}

/**
 * 外部（对话页记忆提醒 / 深链）给的 initialTab → 管理区落点。
 *
 * 注意：`memory` 在这里沿用**外部契约**的含义（= 待处理），
 * 而管理区自己的 `memory` 标签是「记忆（话题）」—— 两者名字相同、语义不同，
 * 这个映射就是唯一的翻译点（改这里会同时影响深链与提醒入口）。
 */
export function resolveInitialRequest(tab: PlanetInitialTab | null | undefined): ManageRequest {
  switch (tab) {
    case "knowledge":
      return { view: "tabs", tab: "knowledge" };
    case "entity":
      return { view: "tabs", tab: "entity" };
    case "memory":
      return { view: "pending", tab: "memory" };
    case "topic":
    default:
      return { view: "tabs", tab: "memory" };
  }
}

/** 话题是否命中关键词（标题 + 关键词；空关键词一律命中） */
export function matchesTopic(topic: TopicFingerprint, query: string): boolean {
  const q = query.trim().toLowerCase();
  if (!q) return true;
  if (topic.title.toLowerCase().includes(q)) return true;
  if (topic.keywords.some((k) => k.toLowerCase().includes(q))) return true;
  // 摘要预览也参与：用户常常记得说过什么，而不是话题叫什么
  return (topic.summary_preview ?? "").toLowerCase().includes(q);
}

export interface TopicGroups {
  active: TopicFingerprint[];
  ended: TopicFingerprint[];
}

/**
 * 主列表只放「还在进行」的话题；已结束的单独成组（结束不是删掉）。
 * 两组都受同一个搜索词约束 —— 已结束的话题依然可以被搜到、被点到。
 */
export function groupTopics(list: TopicFingerprint[], query = ""): TopicGroups {
  const groups: TopicGroups = { active: [], ended: [] };
  for (const topic of list) {
    if (!matchesTopic(topic, query)) continue;
    if (topic.ended) groups.ended.push(topic);
    else groups.active.push(topic);
  }
  return groups;
}

export type SearchKind = "topic" | "knowledge" | "entity";

export interface SearchHit {
  kind: SearchKind;
  /** 真实 id：话题 id / 知识 id / 实体 id（点击后用它定位条目） */
  id: string;
  title: string;
  /** 一行补充（时间、状态、别名等），没有就不显示 */
  note: string;
}

export interface SearchGroups {
  /** 每类命中的**完整**列表（真实顺序；展示层再分批，不在这里截断） */
  topics: SearchHit[];
  knowledge: SearchHit[];
  entities: SearchHit[];
  /** 三类真实命中总数（用于「共 N 项」与空状态判定） */
  total: number;
  /** 每类真实命中数（分组标题展示真实总数，不是当前显示条数） */
  counts: { topics: number; knowledge: number; entities: number };
}

export interface SearchSource {
  topics: TopicFingerprint[];
  knowledge: KnowledgeItem[];
  entities: EntityCard[];
}

/**
 * 每组**首批**展示多少条（F11）：搜索是「找到并打开」，不是把整库平铺出来；
 * 但命中总数必须是真的，其余结果通过「查看更多」分批展开 —— 不能靠一个固定截断数
 * 假装那就是全部（原先每组只留 8 条且把 8 当总数，第九条以后根本看不到）。
 */
export const SEARCH_GROUP_LIMIT = 8;

const KNOWLEDGE_CATEGORY_LABELS: Record<string, string> = {
  user_profile: "用户画像",
  agent_self: "自我",
  goal: "目标",
  general_fact: "常识",
  tool_experience: "工具经验",
};
const KNOWLEDGE_STATE_LABELS: Record<string, string> = {
  draft: "草稿",
  pending_review: "待审核",
  verified: "已验证",
  active: "生效",
  expired: "过期",
  revoked: "已归档",
};

/**
 * 结果行的第二行：**不再重复分组标题里的类型名**（分组已经是「话题（8）」）。
 * 这一行留给真正有用的信息：
 * - 话题 → 「N 片段 · 最近活动」；
 * - 知识 → 分类 / 状态 + 关联话题（单行，界面上省略超长）；
 * - 实体 → 「N 属性 · M 关系」。
 */
function hitTopic(topic: TopicFingerprint): SearchHit {
  const fragments = `${topic.fragment_count} 片段`;
  const recent = topic.last_activity ? ` · 最近活动 ${formatWhen(topic.last_activity)}` : "";
  return { kind: "topic", id: topic.topic_id, title: topic.title, note: `${fragments}${recent}` };
}

function hitKnowledge(item: KnowledgeItem): SearchHit {
  const parts = [
    KNOWLEDGE_CATEGORY_LABELS[item.category] ?? item.category,
    KNOWLEDGE_STATE_LABELS[item.state] ?? item.state,
    item.topic_name ?? "",
  ].filter(Boolean);
  return { kind: "knowledge", id: item.id, title: item.content, note: parts.join(" · ") };
}

function hitEntity(card: EntityCard): SearchHit {
  const attributes = card.attributes?.length ?? 0;
  const relations = card.relations?.length ?? 0;
  return { kind: "entity", id: card.id, title: card.name, note: `${attributes} 属性 · ${relations} 关系` };
}

/**
 * 全局搜索：一次搜索同时覆盖话题 / 知识 / 实体（与当前标签页无关），结果按三类分组。
 * 纯函数 —— 数据由调用方并行取好再传进来，这里只做匹配与截断（顺序保持后端顺序）。
 */
export function buildSearchGroups(source: SearchSource, query: string): SearchGroups {
  const q = query.trim().toLowerCase();
  const empty: SearchGroups = {
    topics: [],
    knowledge: [],
    entities: [],
    total: 0,
    counts: { topics: 0, knowledge: 0, entities: 0 },
  };
  if (!q) return empty;
  const has = (text: string | null | undefined) => (text ?? "").toLowerCase().includes(q);

  // 全量命中：展示层的分批只影响「显示多少条」，不影响总数与后续可达性。
  const topics = source.topics.filter((t) => matchesTopic(t, q)).map(hitTopic);
  const knowledge = source.knowledge
    .filter((k) => has(k.content) || has(k.topic_name))
    .map(hitKnowledge);
  const entities = source.entities
    .filter((e) => has(e.name) || (e.aliases ?? []).some((a) => has(a)) || has(e.summary))
    .map(hitEntity);

  return {
    topics,
    knowledge,
    entities,
    total: topics.length + knowledge.length + entities.length,
    counts: { topics: topics.length, knowledge: knowledge.length, entities: entities.length },
  };
}

/** 结果分组为空 = 真的没有命中（与「还没搜索」「加载中」区分开） */
export function searchGroupsEmpty(groups: SearchGroups): boolean {
  return groups.total === 0;
}

export interface PagedMessages<T> {
  items: T[];
  total: number;
}

/**
 * 分页拼接：offset === 0 是换页（替换），否则是继续读取（追加）。
 * 期间若用户切了片段，调用方用请求序号丢弃过期响应（见 MemoryTab 的 loadPage）。
 */
export function mergeMessagePage<T>(
  previous: PagedMessages<T>,
  page: { messages: T[]; total: number },
  offset: number,
): PagedMessages<T> {
  const items = offset === 0 ? [...page.messages] : [...previous.items, ...page.messages];
  return { items, total: Math.max(0, page.total) };
}

/** 还有没有下一页（空页保护：total 说还有、但服务器没给新消息时不再无限读） */
export function hasMoreMessages(state: PagedMessages<unknown>): boolean {
  return state.items.length < state.total;
}

/** 「正在浏览的话题」是否就是「对话当前的话题」——只有两者不同才需要「继续讨论」 */
export function isCurrentTopic(browsedId: string | null | undefined, currentId: string | null | undefined): boolean {
  return Boolean(browsedId) && browsedId === currentId;
}

/** 时间用相对说法更像人话；拿不到就退回日期（等宽声部承载的数据） */
export function formatWhen(iso?: string | null, now = Date.now()): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  const days = Math.floor((now - d.getTime()) / 86400000);
  const p = (n: number) => String(n).padStart(2, "0");
  const clock = `${p(d.getHours())}:${p(d.getMinutes())}`;
  if (days <= 0) return `今天 ${clock}`;
  if (days === 1) return `昨天 ${clock}`;
  if (days < 30) return `${days} 天前`;
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}
