/**
 * 审批相关的纯函数（子智能体 C）。
 *
 * 契约：docs/interactive-mode-contract.md §1.6 / §4.4；批次判定与批量列表资格以 §9.2 / §9.3 为准
 * （覆盖 §8.5 的「创建时间相近就是同批」与「剩余待审批 ≥4」两种写法）。
 *
 * 这里**不做审批判定**：冲突、依赖等待、材料变化都由服务端决定，前端只显示结果
 * 并把用户的决定发给服务端。这里只负责三件可以离线验证的事：
 *
 * 1. 预览语义比较：只改位置 vs 改语义（决定「能否直接批准」的显示）；
 * 2. 状态文案：每个状态都要有文字说明与下一步，不能只靠颜色；
 * 3. 批量选择：同一批**产生**的意图达到 4 项时的选择 / 拆分逻辑；
 *    批次只能来自可证明的来源（本地按次记录 / 真实 submissionId），绝不按时间猜。
 */
import { ref } from "vue";
import type {
  BoardCard,
  BoardGroup,
  BoardLink,
  Intent,
  IntentImpact,
  IntentPreview,
  IntentStatus,
  RevertReport,
} from "./types";

/** 同一批达到这个数量时，界面提供简洁列表（与服务端 intents.BATCH_MIN 一致） */
export const BATCH_MIN = 4;

/** 仍然等待审批、可以进入批量列表的状态 */
export const DECIDABLE_STATUSES: IntentStatus[] = [
  "pending",
  "needs_update",
  "waiting_dependency",
  "waiting_confirm",
];

export const INTENT_STATUS_LABELS: Record<IntentStatus, string> = {
  pending: "等待审批",
  needs_update: "需要更新",
  rejected: "已拒绝",
  waiting_dependency: "等待前项",
  waiting_confirm: "等待再次确认",
  running: "执行中",
  paused: "已暂停",
  done: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

const KIND_LABELS: Record<string, string> = {
  text: "注释",
  file: "文件",
  image: "图片",
  code: "代码",
  url: "链接",
  reply: "QIO 结果",
};

export function isDecidable(status: IntentStatus): boolean {
  return DECIDABLE_STATUSES.includes(status);
}

export function cardLabel(card: BoardCard): string {
  const content = String(card?.content ?? "").replace(/\s+/g, " ").trim();
  const short = content.length > 16 ? content.slice(0, 16) + "…" : content;
  const kind = KIND_LABELS[card?.kind ?? ""] ?? String(card?.kind ?? "卡片");
  return short ? kind + "「" + short + "」" : kind + " " + (card?.id ?? "");
}

function round(value: unknown): number {
  const number = typeof value === "number" ? value : Number(value);
  return Number.isFinite(number) ? Math.round(number * 1000) / 1000 : 0;
}

function stable(value: unknown): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value ?? null);
  if (Array.isArray(value)) return JSON.stringify(value.map((item) => stable(item)));
  const entries = Object.entries(value as Record<string, unknown>)
    .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
    .map(([key, item]) => [key, stable(item)]);
  return JSON.stringify(entries);
}

function byId<T extends { id: string }>(a: T, b: T): number {
  return a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
}

function cardSemanticKey(card: BoardCard): string {
  return stable({
    kind: card?.kind,
    content: card?.content ?? "",
    meta: card?.meta ?? {},
    checked: Boolean(card?.checked),
    hidden: Boolean(card?.hidden),
    deleted: Boolean(card?.deleted),
  });
}

function cardKey(card: BoardCard, withLayout: boolean): unknown {
  const base = {
    id: card?.id,
    kind: card?.kind,
    content: card?.content ?? "",
    meta: stable(card?.meta ?? {}),
    checked: Boolean(card?.checked),
    hidden: Boolean(card?.hidden),
    deleted: Boolean(card?.deleted),
  };
  return withLayout
    ? { ...base, x: round(card?.x), y: round(card?.y), w: round(card?.w), h: round(card?.h) }
    : base;
}

function groupSemanticKey(group: BoardGroup): string {
  return stable({
    name: group?.name ?? "",
    defaultName: Boolean(group?.defaultName),
    ordered: Boolean(group?.ordered),
    members: [...(group?.members ?? [])],
    deleted: Boolean(group?.deleted),
  });
}

function groupKey(group: BoardGroup, withLayout: boolean): unknown {
  const base = {
    id: group?.id,
    name: group?.name ?? "",
    defaultName: Boolean(group?.defaultName),
    ordered: Boolean(group?.ordered),
    members: [...(group?.members ?? [])],
    deleted: Boolean(group?.deleted),
  };
  return withLayout
    ? { ...base, x: round(group?.x), y: round(group?.y), w: round(group?.w), h: round(group?.h) }
    : base;
}

function linkSemanticKey(link: BoardLink): string {
  return stable({
    src: link?.src ?? "",
    dst: link?.dst ?? "",
    direction: Boolean(link?.direction),
    meaning: link?.meaning ?? "",
    deleted: Boolean(link?.deleted),
  });
}

function linkKey(link: BoardLink): unknown {
  return {
    id: link?.id,
    src: link?.src ?? "",
    dst: link?.dst ?? "",
    direction: Boolean(link?.direction),
    meaning: link?.meaning ?? "",
    deleted: Boolean(link?.deleted),
  };
}

/** 预览的「含义」键：工作内容、材料范围、结果关系（不含位置与大小） */
export function previewSemantics(preview: IntentPreview): string {
  return stable({
    cards: [...(preview?.cards ?? [])].sort(byId).map((card) => cardKey(card, false)),
    groups: [...(preview?.groups ?? [])].sort(byId).map((group) => groupKey(group, false)),
    links: [...(preview?.links ?? [])].sort(byId).map((link) => linkKey(link)),
    note: preview?.note ?? "",
  });
}

/** 预览的完整键：含义 + 位置与大小 */
export function previewWithLayout(preview: IntentPreview): string {
  return stable({
    cards: [...(preview?.cards ?? [])].sort(byId).map((card) => cardKey(card, true)),
    groups: [...(preview?.groups ?? [])].sort(byId).map((group) => groupKey(group, true)),
    links: [...(preview?.links ?? [])].sort(byId).map((link) => linkKey(link)),
    note: preview?.note ?? "",
  });
}

export interface PreviewBounds {
  x: number;
  y: number;
  w: number;
  h: number;
}

/** 预览在板面上占据的范围：用于「定位到板面」；空预览返回 null。 */
export function previewBounds(preview: IntentPreview): PreviewBounds | null {
  const boxes = [
    ...(preview?.groups ?? []).map((group) => ({
      x: round(group?.x),
      y: round(group?.y),
      w: round(group?.w),
      h: round(group?.h),
    })),
    ...(preview?.cards ?? []).map((card) => ({
      x: round(card?.x),
      y: round(card?.y),
      w: round(card?.w),
      h: round(card?.h),
    })),
  ];
  if (!boxes.length) return null;
  const minX = Math.min(...boxes.map((box) => box.x));
  const minY = Math.min(...boxes.map((box) => box.y));
  const maxX = Math.max(...boxes.map((box) => box.x + Math.max(box.w, 1)));
  const maxY = Math.max(...boxes.map((box) => box.y + Math.max(box.h, 1)));
  return { x: minX, y: minY, w: maxX - minX, h: maxY - minY };
}

export type PreviewChangeLevel = "same" | "layout_only" | "semantic";

export interface PreviewComparison {
  level: PreviewChangeLevel;
  changes: string[];
  text: string;
  /** 只改位置（或没有变化）时可以直接批准；改语义必须先提交更新 */
  canApprove: boolean;
}

export function describePreviewChanges(before: IntentPreview, after: IntentPreview): string[] {
  const changes: string[] = [];
  const beforeCards = new Map((before?.cards ?? []).map((card) => [card.id, card]));
  const afterCards = new Map((after?.cards ?? []).map((card) => [card.id, card]));
  for (const [id, card] of afterCards) {
    const previous = beforeCards.get(id);
    if (!previous) changes.push("新增结果卡片：" + cardLabel(card));
    else if (cardSemanticKey(previous) !== cardSemanticKey(card)) {
      changes.push("修改结果卡片：" + cardLabel(card));
    }
  }
  for (const [id, card] of beforeCards) {
    if (!afterCards.has(id)) changes.push("移除结果卡片：" + cardLabel(card));
  }

  const beforeGroups = new Map((before?.groups ?? []).map((group) => [group.id, group]));
  const afterGroups = new Map((after?.groups ?? []).map((group) => [group.id, group]));
  for (const [id, group] of afterGroups) {
    const previous = beforeGroups.get(id);
    if (!previous) changes.push("新增组「" + group.name + "」");
    else if (groupSemanticKey(previous) !== groupSemanticKey(group)) {
      changes.push("修改组「" + group.name + "」的成员或名称");
    }
  }
  for (const [id, group] of beforeGroups) {
    if (!afterGroups.has(id)) changes.push("移除组「" + group.name + "」");
  }

  const beforeLinks = new Map((before?.links ?? []).map((link) => [link.id, link]));
  const afterLinks = new Map((after?.links ?? []).map((link) => [link.id, link]));
  for (const [id, link] of afterLinks) {
    const previous = beforeLinks.get(id);
    if (!previous) changes.push("新增关系「" + (link.meaning || id) + "」");
    else if (linkSemanticKey(previous) !== linkSemanticKey(link)) {
      changes.push("修改关系「" + (link.meaning || id) + "」");
    }
  }
  for (const [id, link] of beforeLinks) {
    if (!afterLinks.has(id)) changes.push("移除关系「" + (link.meaning || id) + "」");
  }

  if ((before?.note ?? "") !== (after?.note ?? "")) changes.push("修改了预览说明");
  return changes;
}

/**
 * 比较两份预览（语义与位置分开比较），与服务端 intents.compare_preview 一致：
 * - same：没有变化；
 * - layout_only：只改了位置 / 大小 → 仍可直接批准；
 * - semantic：工作内容 / 材料范围 / 结果关系变化 → 需要更新。
 */
export function comparePreview(before: IntentPreview, after: IntentPreview): PreviewComparison {
  const level: PreviewChangeLevel =
    previewWithLayout(before) === previewWithLayout(after)
      ? "same"
      : previewSemantics(before) === previewSemantics(after)
        ? "layout_only"
        : "semantic";
  const changes = describePreviewChanges(before, after);
  let text: string;
  if (level === "same") text = "预览没有变化。";
  else if (level === "layout_only") {
    text = "只调整了位置：工作内容、材料范围与结果关系都没有变，可以直接批准。";
  } else {
    const detail = changes.length ? changes.join("；") : "需要更新预览";
    text = "工作要求或关系含义发生了变化：" + detail + "。需要提交并由 QIO 更新后才能批准。";
  }
  return { level, changes, text, canApprove: level !== "semantic" };
}

export type StatusTone = "open" | "waiting" | "active" | "settled" | "problem";

export interface StatusText {
  label: string;
  detail: string;
  tone: StatusTone;
}

/** 状态文案：每个状态都要有文字说明（颜色只是辅助）。 */
export function statusText(intent: Intent): StatusText {
  const label = INTENT_STATUS_LABELS[intent?.status] ?? String(intent?.status ?? "");
  const reason = String(intent?.reason ?? "").trim();
  switch (intent?.status) {
    case "pending":
      return {
        label,
        detail: "批准后才会成为任务；拒绝则预览消失、板面原内容保留。",
        tone: "open",
      };
    case "needs_update":
      return {
        label,
        detail: reason || "相关材料已变化：不能批准，需要提交并由 QIO 更新预览。",
        tone: "problem",
      };
    case "rejected":
      return { label, detail: reason || "预览已消失，板面原内容保留。", tone: "settled" };
    case "waiting_dependency":
      return {
        label,
        detail: "已批准，但前项还没有成功完成：不会自动开始，会一直等待。",
        tone: "waiting",
      };
    case "waiting_confirm":
      return {
        label,
        detail: "前项已经完成：需要你再次确认才会开始，不会自动开始。",
        tone: "waiting",
      };
    case "running":
      return {
        label,
        detail: "任务正在执行；第一阶段没有真实执行，推进来自演示入口。",
        tone: "active",
      };
    case "paused":
      return {
        label,
        detail:
          reason ||
          "已暂停：不会自动继续；确认后会按当前材料继续，不会重试、也不会重新执行已完成的部分。",
        tone: "waiting",
      };
    case "done":
      return { label, detail: "结果已经成为板面正式内容（实线）。", tone: "settled" };
    case "failed":
      return {
        label,
        detail: reason || "本任务造成的改动已撤回，你后来的修改保留；不会自动重试。",
        tone: "problem",
      };
    case "cancelled":
      return { label, detail: reason || "任务已取消，本任务造成的改动已撤回。", tone: "problem" };
    default:
      return { label, detail: reason, tone: "settled" };
  }
}

export interface ApproveAvailability {
  allowed: boolean;
  /** true = 需要带上 confirmDependency（前项已完成，等待再次确认） */
  needsConfirm: boolean;
  text: string;
}

/** 能不能点「批准」——只依据服务端给出的状态，不改判冲突与依赖。 */
export function approveAvailability(intent: Intent): ApproveAvailability {
  switch (intent?.status) {
    case "pending":
      return { allowed: true, needsConfirm: false, text: "批准后才会成为任务。" };
    case "waiting_confirm":
      return {
        allowed: true,
        needsConfirm: true,
        text: "前项已完成：需要你再次确认才会开始。",
      };
    case "waiting_dependency":
      return {
        allowed: false,
        needsConfirm: false,
        text: "已批准，正在等待前项成功完成；不会自动开始。",
      };
    case "needs_update":
      return {
        allowed: false,
        needsConfirm: false,
        text: intent.reason || "相关材料已变化：不能批准，需要提交后由 QIO 更新预览。",
      };
    case "paused":
      // 暂停后可以「按当前材料继续」：必须由用户确认，服务端才会重新开始
      return {
        allowed: true,
        needsConfirm: true,
        text: "暂停后可以按当前材料继续：需要你确认；不会自动重试，也不会重新执行已完成的部分。",
      };
    case "running":
      return { allowed: false, needsConfirm: false, text: "任务正在执行中。" };
    case "done":
      return { allowed: false, needsConfirm: false, text: "任务已经完成。" };
    default:
      return { allowed: false, needsConfirm: false, text: "这项意图已经结束。" };
  }
}

export function rejectAvailability(intent: Intent): { allowed: boolean; text: string } {
  if (isDecidable(intent?.status)) {
    return { allowed: true, text: "拒绝后预览消失，板面原内容保留。" };
  }
  if (intent?.status === "running" || intent?.status === "paused") {
    return { allowed: false, text: "执行中的任务请先暂停或取消。" };
  }
  return { allowed: false, text: "这项意图已经结束。" };
}

// --- 影响说明 -----------------------------------------------------------

export interface ImpactSection {
  key: "objects" | "tasks" | "consequences";
  label: string;
  items: string[];
}

export function impactSections(impact: IntentImpact | undefined): ImpactSection[] {
  return [
    { key: "objects", label: "对象", items: [...(impact?.objects ?? [])] },
    { key: "tasks", label: "任务", items: [...(impact?.tasks ?? [])] },
    { key: "consequences", label: "后果", items: [...(impact?.consequences ?? [])] },
  ];
}

export function revertSections(revert: RevertReport | undefined): ImpactSection[] {
  const pending = (revert?.pendingDecision ?? []).map(
    (item) => item.id + "：" + item.reason + "（影响：" + item.impact + "）",
  );
  return [
    { key: "objects", label: "已撤回", items: [...(revert?.reverted ?? [])] },
    { key: "tasks", label: "已保留（你后来的修改）", items: [...(revert?.kept ?? [])] },
    { key: "consequences", label: "等待你决定", items: pending },
  ];
}

/** 等待依赖时，把前项 id 换成可读标题；找不到时退回 id。 */
export function waitingForLabels(intent: Intent, all: Intent[]): string[] {
  const ids = intent?.waitingFor ?? intent?.dependsOn ?? [];
  return ids.map((id) => all.find((item) => item.id === id)?.title ?? id);
}

// --- 批量选择 -----------------------------------------------------------

export function batchCandidates(intents: Intent[]): Intent[] {
  return (intents ?? []).filter((intent) => isDecidable(intent.status));
}

/** 默认选中所有可以批准的项：needs_update 的不能批准，但可以被拒绝。 */
export function defaultBatchSelection(intents: Intent[]): string[] {
  return batchCandidates(intents)
    .filter((intent) => intent.status !== "needs_update")
    .map((intent) => intent.id);
}

export function toggleBatchSelection(selected: string[], id: string): string[] {
  return selected.includes(id) ? selected.filter((item) => item !== id) : [...selected, id];
}

export function selectAllBatch(intents: Intent[]): string[] {
  return batchCandidates(intents).map((intent) => intent.id);
}

export function clearBatchSelection(): string[] {
  return [];
}

export interface BatchSplit {
  ids: string[];
  blocked: { id: string; reason: string }[];
}

/**
 * 把选中的项拆成「可以提交给服务端的」与「本批不能这样处理的」。
 * 例如 needs_update 的项不能批准（要等 QIO 更新预览），但仍可以被拒绝。
 */
export function splitBatchDecision(
  intents: Intent[],
  selectedIds: string[],
  decision: "approve" | "reject",
): BatchSplit {
  const candidates = new Map(batchCandidates(intents).map((intent) => [intent.id, intent]));
  const ids: string[] = [];
  const blocked: { id: string; reason: string }[] = [];
  for (const id of selectedIds ?? []) {
    const intent = candidates.get(id);
    if (!intent) {
      blocked.push({ id, reason: "这项已经不在等待审批的列表里。" });
      continue;
    }
    const availability =
      decision === "approve" ? approveAvailability(intent) : rejectAvailability(intent);
    if (availability.allowed) ids.push(id);
    else blocked.push({ id, reason: availability.text });
  }
  return { ids, blocked };
}

/** 批量列表顶部的一句话说明：选中了多少、未选中的会怎样。 */
export function batchSummary(intents: Intent[], selectedIds: string[]): string {
  const candidates = batchCandidates(intents);
  const selected = candidates.filter((intent) => selectedIds.includes(intent.id));
  const approvable = selected.filter((intent) => approveAvailability(intent).allowed);
  const waiting = candidates.length - selected.length;
  const parts = ["已选 " + selected.length + " 项"];
  if (selected.length) parts.push("其中可直接批准 " + approvable.length + " 项");
  parts.push(waiting > 0 ? "未选中的 " + waiting + " 项继续等待" : "全部已选中");
  return parts.join("；") + "。";
}
// --- 批次判定（契约 §9.2 / §9.3，覆盖 §8.5） -------------------------------
//
// 一个批次只能来自**可证明的同一次产生过程**：
//   ① 前端按次记录的创建批次：调用方在「一次创建动作」之后调 recordIntentBatch 记下来
//      （演示入口一次四项、提交后一次生成的多项）；
//   ② 真实的 submissionId：服务端同一次提交产生的多项共用它（submission.py 写入），
//      只在字段真的存在时使用，不因为「字段存在」就扩大含义。
// 两个来源都拿不到时（含本地记录被清除 / 写入失败 / 被禁用 / 达到容量上限 / 损坏），
// 该意图**各自成批**（intent:<id>）：宁可不出批量列表，也**绝不误合并**。
//
// **已删除的推断**：不再按 createdAt 相近 / 同一秒归批（§9.2）。同一秒内产生的两批各两项，
// 在没有可证明来源时必须仍然是两批，不能变成四项一批。
//
// 这一层只做「哪些意图属于同一批」，不做任何审批判定；冲突、依赖、材料变化仍然由服务端决定。

/** 同一批**产生**的意图达到这个数量，才提供该批的批量列表（§9.3：按总量，不按剩余待审批数） */
export const BATCH_LIST_MIN = BATCH_MIN;

/** 本次会话的创建批次记录（意图 id → 批次键）；解析失败或写满时静默降级为「不成批」 */
export const INTENT_BATCH_STORAGE_KEY = "qio.interactive.intentBatches";

/**
 * 本地批次记录的**版本号**（存储本身不是响应式来源）。
 *
 * 为什么需要它：写入记录与「意图列表更新」可能落在不同微任务里。实测演示入口
 * 一次生成四项时，组件先因为 intents 更新而重算过一次（那时记录还没写进去），
 * 记录随后写入却没有任何响应式变化 → 批量入口要等下一次刷新才出现。
 * 这里在写入成功后 +1，并让读取路径 touch 它：组件里基于 batchesWithList 的
 * computed 会因此被正确重新计算，不依赖调用方的调用顺序。
 */
const batchRevision = ref(0);

/** 条数上限：只保留最近的若干批，避免存储无限增长（超出时丢最旧的批，同样静默降级） */
const BATCH_MAX_GROUPS = 60;
const BATCH_MAX_IDS = 400;
/**
 * 时间上限：超过这个时长的本地记录不再参与归批（有界 + 过期清理）。
 * 过期只影响**本地记录**；服务端 submissionId 是持久来源，不受影响。
 */
export const BATCH_RECORD_TTL_MS = 7 * 24 * 60 * 60 * 1000;

/** 服务端来源的键前缀（直观区分一个键是怎么来的） */
const SUBMISSION_BATCH_PREFIX = "submission:";
/** 自成一批：键里带意图 id，保证不同意图一定不同批 */
const SOLO_BATCH_PREFIX = "intent:";

interface BatchRecord {
  id: string;
  key: string;
  /** 记录时间（毫秒）；旧格式没有这个字段（undefined）时按「时间未知」保留 */
  at?: number;
}

interface BatchStore {
  groups: { key: string; ids: string[] }[];
  byId: Map<string, string>;
}

/** 能拿到哪个 Web Storage；都拿不到（或访问就抛错）时返回 null */
function batchStorage(): Storage | null {
  for (const name of ["localStorage", "sessionStorage"] as const) {
    try {
      const store = (globalThis as Record<string, unknown>)[name] as Storage | undefined;
      if (store && typeof store.getItem === "function" && typeof store.setItem === "function") {
        return store;
      }
    } catch {
      // 隐私模式 / 被策略禁用：换下一个，拿不到就降级
    }
  }
  return null;
}

function isBatchRecord(value: unknown): value is BatchRecord {
  if (!value || typeof value !== "object") return false;
  const item = value as { id?: unknown; key?: unknown; at?: unknown };
  return (
    typeof item.id === "string" &&
    item.id.length > 0 &&
    typeof item.key === "string" &&
    item.key.length > 0 &&
    (item.at === undefined || typeof item.at === "number")
  );
}

/**
 * 记录是否已过期：只有**带时间戳且超出 TTL**的记录才算过期。
 * 旧格式（没有时间戳）是旧版本写下的有效记录，按「时间未知」保留 ——
 * 下一次写入时会补上时间戳，之后同样按 TTL 过期；条数上限始终有效。
 */
function isExpired(record: BatchRecord, now: number): boolean {
  if (typeof record.at !== "number" || !Number.isFinite(record.at) || record.at <= 0) return false;
  return now - record.at > BATCH_RECORD_TTL_MS;
}

/** 读会话批次记录。任何异常（解析失败 / 结构损坏 / 存储不可用）都当「没有记录」，不抛错。 */
function readBatchStore(now = Date.now()): BatchStore {
  // 让调用方的 computed / watch 依赖「本地记录已变化」这件事（见 batchRevision）
  void batchRevision.value;
  const empty: BatchStore = { groups: [], byId: new Map() };
  const store = batchStorage();
  if (!store) return empty;
  let raw: string | null = null;
  try {
    raw = store.getItem(INTENT_BATCH_STORAGE_KEY);
  } catch {
    return empty;
  }
  if (!raw) return empty;
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return empty;
  }
  const source = Array.isArray(parsed)
    ? parsed
    : parsed && typeof parsed === "object" && Array.isArray((parsed as { records?: unknown }).records)
      ? (parsed as { records: unknown[] }).records
      : [];
  const groups: { key: string; ids: string[] }[] = [];
  const byId = new Map<string, string>();
  for (const entry of source) {
    if (!isBatchRecord(entry)) continue;
    if (isExpired(entry, now)) continue; // 过期清理：旧记录不参与归批，也不写回
    if (byId.has(entry.id)) continue;
    // 读侧也设上限：即使存储里被塞进超大内容，也只读最近的上限条数
    if (byId.size >= BATCH_MAX_IDS) break;
    byId.set(entry.id, entry.key);
    const group = groups.find((item) => item.key === entry.key);
    if (group) group.ids.push(entry.id);
    else groups.push({ key: entry.key, ids: [entry.id] });
  }
  return { groups, byId };
}

function writeBatchStore(store: BatchStore): boolean {
  const target = batchStorage();
  if (!target) return false;
  const now = Date.now();
  const payload = JSON.stringify({
    version: 2,
    updatedAt: new Date(now).toISOString(),
    records: store.groups.flatMap((group) => group.ids.map((id) => ({ id, key: group.key, at: now }))),
  });
  try {
    target.setItem(INTENT_BATCH_STORAGE_KEY, payload);
    return true;
  } catch {
    // 写满 / 被禁用：降级为「本次会话的批次没有记全」，下次调用再试，绝不抛错
    return false;
  }
}

/**
 * 一个意图能不能拿到**可证明**的批次键。
 * 拿不到时返回 null，由调用方给 `intent:<id>`（各自成批）。
 */
function batchKeyFromIntent(intent: Intent): string | null {
  const id = intent?.id;
  if (!id) return null;
  const bySession = readBatchStore().byId.get(id);
  if (bySession) return bySession; // ① 本次记录的创建批次（有界、会过期、可损坏降级）
  const submissionId = typeof intent.submissionId === "string" ? intent.submissionId.trim() : "";
  if (submissionId) return SUBMISSION_BATCH_PREFIX + submissionId; // ② 服务端同一次提交
  return null; // 没有可证明的来源
}

/**
 * 这一项有没有可证明的批次来源。false 时界面要**如实说明「批次不可恢复」**，
 * 不能靠创建时间之类的近似去猜（契约 §9.2）。
 */
export function hasRecoverableBatch(intent: Intent): boolean {
  return batchKeyFromIntent(intent) !== null;
}

/**
 * 一个意图属于哪一批。优先级：① 本地创建批次 > ② 真实 submissionId；
 * 都拿不到时返回带意图 id 的独立键：**不同批次绝不合并**（契约 §9.2）。
 */
export function batchKeyOf(intent: Intent): string {
  const id = intent?.id ?? "";
  return batchKeyFromIntent(intent) ?? SOLO_BATCH_PREFIX + id;
}

export interface IntentBatch {
  /** 批次标识：同一批产生的意图共用一个 key */
  key: string;
  /** 这一批**产生**的全部意图（含已处理的：契约 §9.3 的资格按总量算） */
  intentIds: string[];
  /** 这一批里仍然等待审批、还没处理的意图 */
  pendingIds: string[];
}

/** 按批分组（保持传入顺序，便于界面稳定显示）。 */
export function groupIntentsByBatch(intents: Intent[]): IntentBatch[] {
  const groups = new Map<string, IntentBatch>();
  for (const intent of intents ?? []) {
    if (!intent?.id) continue;
    const key = batchKeyOf(intent);
    let batch = groups.get(key);
    if (!batch) {
      batch = { key, intentIds: [], pendingIds: [] };
      groups.set(key, batch);
    }
    if (batch.intentIds.includes(intent.id)) continue;
    batch.intentIds.push(intent.id);
    if (isDecidable(intent.status)) batch.pendingIds.push(intent.id);
  }
  return [...groups.values()];
}

/**
 * 需要提供批量列表的批次：该批**产生**的意图总量 ≥ 4，且还有未处理项（契约 §9.3）。
 *
 * - 处理掉其中若干项后，只要还有剩余就继续返回（列表保留到处理完）；
 * - 全部处理完（pendingIds 为空）才消失；
 * - 初始只有三项的批次**不会**因为别处另有一项而变成四项列表（不同批次不累加）。
 */
export function batchesWithList(intents: Intent[]): IntentBatch[] {
  return groupIntentsByBatch(intents).filter(
    (batch) => batch.intentIds.length >= BATCH_LIST_MIN && batch.pendingIds.length > 0,
  );
}

/** 这一批还有多少项没处理（入口与列表要显示的剩余数量，契约 §9.3）。 */
export function batchRemainingCount(batch: IntentBatch): number {
  return batch?.pendingIds?.length ?? 0;
}

/** 这一批已经处理掉多少项。 */
export function batchHandledCount(batch: IntentBatch): number {
  const total = batch?.intentIds?.length ?? 0;
  return Math.max(0, total - batchRemainingCount(batch));
}

/**
 * 这一项现在还能不能被选中 / 提交：只有**还没处理**的项可以（契约 §9.3）。
 * 已处理项（已批准、已拒绝、执行中、已完成…）不能再次被选中或再次提交审批。
 */
export function isBatchItemSelectable(batch: IntentBatch, id: string): boolean {
  return Boolean(id) && (batch?.pendingIds ?? []).includes(id);
}

/**
 * 把「某一次创建动作产生的意图」记进本批（演示入口一次四项、提交后一次生成的多项）。
 *
 * - 优先写 localStorage，拿不到就退回 sessionStorage，都拿不到就静默跳过；
 * - 解析失败 / 写满 / 被策略禁用一律静默降级为「这批没有被记下来」（不抛错，
 *   最多只是这批不出现批量列表，绝不会把不同批次错误合并）；
 * - **有界**：条数上限 BATCH_MAX_GROUPS 批 / BATCH_MAX_IDS 条，超出丢最旧的；
 *   时间上限 BATCH_RECORD_TTL_MS，过期记录在读写时都会被清掉。
 */
export function recordIntentBatch(batchKey: string, intentIds: string[]): void {
  const key = typeof batchKey === "string" ? batchKey.trim() : "";
  const ids = [...new Set((intentIds ?? []).filter((id) => typeof id === "string" && id.length > 0))];
  // 没有可用的批次键时不写：写了只会让这些意图被算成「同一批」，不如让它们各自成批
  if (!key || !ids.length) return;
  try {
    const store = readBatchStore(); // 读取时已做过过期清理
    const group = store.groups.find((item) => item.key === key);
    if (group) {
      for (const id of ids) if (!group.ids.includes(id)) group.ids.push(id);
    } else {
      store.groups.push({ key, ids });
    }
    for (const id of ids) store.byId.set(id, key);
    while (store.groups.length > BATCH_MAX_GROUPS) {
      const dropped = store.groups.shift();
      for (const id of dropped?.ids ?? []) store.byId.delete(id);
    }
    let total = store.groups.reduce((sum, item) => sum + item.ids.length, 0);
    while (total > BATCH_MAX_IDS && store.groups.length > 1) {
      const dropped = store.groups.shift();
      const count = dropped?.ids.length ?? 0;
      total -= count;
      for (const id of dropped?.ids ?? []) store.byId.delete(id);
    }
    if (writeBatchStore(store)) batchRevision.value += 1;
  } catch {
    // 兜底：任何意外都不许让调用方的界面崩掉（降级为「这批不成批」）
  }
}

// --- 批量列表的本地视图（选择状态、定位） --------------------------------

/** 已经在等待审批的意图 id（批量列表里可以勾选的项） */
export function decidableIds(intents: Intent[]): string[] {
  return (intents ?? []).filter((intent) => isDecidable(intent?.status)).map((intent) => intent.id);
}

/** 批次里还能处理的项：只保留传进来的范围（未选中的不处理） */
export function inBatchSelection(batch: IntentBatch, ids: string[]): string[] {
  return batch.pendingIds.filter((id) => ids.includes(id));
}

/** 清掉已经不在列表里的选择：数量变化不改变「你选了哪些还在的项」 */
export function pruneBatchSelection(batch: IntentBatch, ids: string[]): string[] {
  return inBatchSelection(batch, ids);
}

/** 选择部分或全部：同一批里勾选 / 取消；未选中的继续等待审批 */
export function toggleBatchSelectionIn(batch: IntentBatch, ids: string[], id: string): string[] {
  const kept = inBatchSelection(batch, ids).filter((item) => item !== id);
  if (!batch.pendingIds.includes(id)) return kept;
  return ids.includes(id) ? kept : [...kept, id];
}

export function selectAllInBatch(batch: IntentBatch): string[] {
  return [...batch.pendingIds];
}

export function clearBatchSelectionIn(): string[] {
  return [];
}

/** 把选中项拆成「可以提交给服务端的」与「本批不能这样处理的」（沿用既有判定） */
export interface BatchDecisionSplit {
  ids: string[];
  blocked: { id: string; reason: string }[];
}

export function splitBatchIn(
  intents: Intent[],
  batch: IntentBatch,
  selectedIds: string[],
  decision: "approve" | "reject",
): BatchDecisionSplit {
  const members = new Set(batch?.intentIds ?? []);
  const pending = new Set(batch?.pendingIds ?? []);
  const inBatch: string[] = [];
  const blocked: { id: string; reason: string }[] = [];
  for (const id of selectedIds ?? []) {
    // 已处理项 / 不属于这一批的项不能提交，并给出真实原因（契约 §9.3）
    if (!members.has(id)) {
      blocked.push({ id, reason: "这一项不在当前这一批里。" });
      continue;
    }
    if (!pending.has(id)) {
      blocked.push({ id, reason: "这一项已经处理过了，不能再次提交审批。" });
      continue;
    }
    inBatch.push(id);
  }
  const decided = splitBatchDecision(intents, inBatch, decision);
  return { ids: decided.ids, blocked: [...blocked, ...decided.blocked] };
}

/** 批量列表顶部的一句话说明：选中了多少、未选中的会怎样 */
export function batchSummaryIn(intents: Intent[], batch: IntentBatch, selectedIds: string[]): string {
  return batchSummary(intents, inBatchSelection(batch, selectedIds));
}

/**
 * 批量入口要显示的文字：这一批**剩余待处理数**（"待审批 N 项"）、总量与已处理数，
 * 以及其他批次还在等的数量（不累加，契约 §9.3）。
 */
export function batchEntryText(batch: IntentBatch, extraPending = 0): string {
  const total = batch?.intentIds?.length ?? 0;
  const remaining = batchRemainingCount(batch);
  const handled = batchHandledCount(batch);
  const detail = ["共 " + total + " 项"];
  if (handled > 0) detail.push("已处理 " + handled + " 项");
  const parts = ["这一批待审批 " + remaining + " 项（" + detail.join("，") + "）"];
  if (extraPending > 0) parts.push("另有 " + extraPending + " 项在其他批次等待");
  return parts.join("；");
}

/** 点击条目 / 在板面上定位这项预览：沿用 window 事件 qio:interactive:locate-preview */
export function locatePreview(intentId: string, bounds: PreviewBounds | null): void {
  if (typeof window === "undefined" || typeof window.dispatchEvent !== "function") return;
  try {
    const event =
      typeof CustomEvent === "function"
        ? new CustomEvent("qio:interactive:locate-preview", { detail: { intentId, bounds } })
        : new Event("qio:interactive:locate-preview");
    window.dispatchEvent(event);
  } catch {
    // 事件派发失败不该影响审批操作本身
  }
}
