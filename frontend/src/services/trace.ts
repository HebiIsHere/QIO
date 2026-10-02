/**
 * 一轮的「时间花在哪」：把后端 Trace 的 phases 账本换算成用户读得懂的分类。
 *
 * 只做换算与格式化，不发请求、不碰 DOM（组件与 composable 各自负责那一层）。
 *
 * 三条边界（诚实优先）：
 * 1. **只消费 phases**（阶段名 + 毫秒 + notes）；不读 injection / tool_runs /
 *    final_preview —— 那些字段里有用户原文、记忆预览和文件路径，展示它们不是这个
 *    面板的目的，也等于把 trace 里的隐私面再暴露一次。
 * 2. 分类耗时之和 + 其它(residual) ≈ 执行时长；residual 一律如实显示成「其它」，
 *    不假装 QIO 能解释所有耗时。
 * 3. 老数据（没有 phases 列 / 旧版本留下的行）优雅降级：只给总耗时 + 一句说明，
 *    不显示 0，也不留空白。
 */

import { api } from "./api";

/** 后端 phases.spans 的一项（顶层阶段铺满时间轴，depth>1 只作细分）。 */
export interface PhaseSpan {
  name: string;
  ms: number;
  depth: number;
  detail?: string | null;
  start_ms?: number;
}

/** 后端 phases.notes：与时间轴无关的事实（排队等待在这里）。 */
export interface PhaseNotes {
  queue_wait_ms?: number;
  [key: string]: unknown;
}

/** after_turn：turn 结束之后仍在跑的后台工作（不计入执行时长）。 */
export interface PhaseAfterTurn {
  name: string;
  ms: number;
  detail?: string | null;
}

/** 后端 phases 账本。旧行是 {}，字段都可能缺 —— 读取端必须容忍。 */
export interface PhaseLedger {
  version?: number;
  total_ms?: number;
  sum_ms?: number;
  residual_ms?: number | null;
  notes?: PhaseNotes | null;
  spans?: PhaseSpan[] | null;
  after_turn?: PhaseAfterTurn[] | null;
}

/**
 * 展示只需要这几个字段。
 *
 * `api.getTrace` 的 `TraceDetail` 目前没有声明 `phases`（那是 Agent B 的文件，
 * 本任务不改它），所以这里自己声明并用一次断言接上；后端确实返回 phases。
 */
export interface TraceTimingSource {
  turn_id: string;
  status?: string;
  duration_ms?: number | null;
  phases?: PhaseLedger | null;
}

/**
 * 面向用户的分类（固定顺序，便于跨轮比较）。
 * 展示的是这些中文名，不是内部阶段名 —— 内部名只在开发者模式里出现。
 */
export const TIMING_CATEGORY_ORDER = [
  "queue",
  "prepare",
  "context",
  "memory",
  "model",
  "tool",
  "approval",
  "save",
  "organize",
  "other",
] as const;

export type TimingCategoryKey = (typeof TIMING_CATEGORY_ORDER)[number];

export const TIMING_CATEGORY_LABEL: Record<TimingCategoryKey, string> = {
  queue: "排队",
  prepare: "准备环境",
  context: "上下文准备",
  memory: "记忆检索",
  model: "模型",
  tool: "工具",
  approval: "等待确认",
  save: "保存",
  organize: "记忆整理",
  other: "其它",
};

/**
 * 内部阶段名 → 用户分类。
 *
 * 映射原则：用户关心的是「卡在哪一类事情上」，不是代码里的函数名。
 * 未列出的阶段**不会消失**：先落进「其它」，同时把原名记进 unknownStages，
 * 开发者模式下能看到（这样后端将来加阶段也不会被静默吞掉）。
 */
const STAGE_CATEGORY: Record<string, TimingCategoryKey> = {
  adapter_setup: "prepare",
  turn_setup: "context",
  turn_binding: "context",
  context_assembly: "context",
  topic_prediction: "context",
  retrieval: "memory",
  model_wait: "model",
  tool_wait: "tool",
  tool_routing: "tool",
  approval_wait: "approval",
  persistence: "save",
  finalize: "save",
  memory_post: "organize",
  narrative: "other",
  agent_loop: "other",
  other: "other",
};

export interface TimingRow {
  key: TimingCategoryKey;
  label: string;
  ms: number;
  /** 占总耗时的百分比（0–100，保留一位小数） */
  percent: number;
}

export interface RawStage {
  name: string;
  ms: number;
  count: number;
}

export interface TurnTiming {
  turnId: string;
  /** 用户感知总耗时 = 排队 + 执行；都没有时为 null（界面要说「没有记录」，不能说 0） */
  totalMs: number | null;
  /** 后端 duration_ms（不含排队） */
  executionMs: number | null;
  queueMs: number | null;
  rows: TimingRow[];
  residualMs: number;
  /** turn 之后的后台整理耗时（不计入 totalMs） */
  afterTurnMs: number;
  /** 没被映射表认识的阶段名（开发者模式展示；它们的时间已并入「其它」） */
  unknownStages: string[];
  /** 开发模式：内部阶段合计（同名的合并） */
  rawStages: RawStage[];
  /** 老数据：没有 phases 账本 */
  legacy: boolean;
}

interface OwnedSpan {
  name: string;
  /** 独占时间（自己减去子阶段）——顶层与嵌套不会重复计入 */
  ms: number;
}

function spanStart(span: PhaseSpan): number {
  const value = Number(span.start_ms ?? 0);
  return Number.isFinite(value) ? value : 0;
}

function spanMs(span: PhaseSpan): number {
  const value = Number(span.ms ?? 0);
  return Number.isFinite(value) && value > 0 ? value : 0;
}

function contains(outer: PhaseSpan, inner: PhaseSpan): boolean {
  const start = spanStart(outer);
  return (
    inner.depth > outer.depth &&
    spanStart(inner) >= start &&
    spanStart(inner) < start + Math.max(spanMs(outer), 1)
  );
}

/**
 * 把 span 列表折成「独占时间」：父阶段的时长减去子阶段，避免父子重复计入。
 *
 * 后端的计时器是栈式的，spans 按 start_ms 递增；depth>1 落在包含它的 depth-1 里。
 * 并行工具只记批次墙钟（一次 tool_wait），所以这里不会因为并行把时间算多。
 */
export function attributeSpans(spans: PhaseSpan[]): OwnedSpan[] {
  const sorted = [...spans].sort(
    (a, b) => spanStart(a) - spanStart(b) || (a.depth ?? 1) - (b.depth ?? 1),
  );
  const stack: { span: PhaseSpan; childrenMs: number }[] = [];
  const out: OwnedSpan[] = [];

  const closeTop = () => {
    const top = stack.pop();
    if (!top) return;
    const own = Math.max(0, spanMs(top.span) - top.childrenMs);
    out.push({ name: String(top.span.name || "other"), ms: own });
    if (stack.length) stack[stack.length - 1].childrenMs += spanMs(top.span);
  };

  for (const span of sorted) {
    while (stack.length && !contains(stack[stack.length - 1].span, span)) closeTop();
    stack.push({ span, childrenMs: 0 });
  }
  while (stack.length) closeTop();
  return out;
}

function percentOf(ms: number, total: number): number {
  if (!total || total <= 0) return 0;
  return Math.round((ms / total) * 1000) / 10;
}

/**
 * 账本 → 用户可读的耗时结构。没有 trace 时返回 null（调用方决定怎么措辞）。
 */
export function buildTurnTiming(
  trace: TraceTimingSource | null | undefined,
): TurnTiming | null {
  if (!trace) return null;

  const ledger = trace.phases ?? null;
  const spans = (ledger?.spans ?? []).filter((s) => s && typeof s.name === "string");
  const notes = ledger?.notes ?? null;
  const rawQueue = notes?.queue_wait_ms;
  const queueMs =
    typeof rawQueue === "number" && Number.isFinite(rawQueue) && rawQueue >= 0
      ? Math.round(rawQueue)
      : null;
  const executionMs =
    typeof trace.duration_ms === "number" && Number.isFinite(trace.duration_ms)
      ? Math.round(trace.duration_ms)
      : null;
  const afterTurnMs = Math.round(
    (ledger?.after_turn ?? []).reduce((sum, item) => sum + spanMs({ ...item, depth: 1 }), 0),
  );

  if (!spans.length) {
    // 老数据 / 没有账本：给总耗时 + 明确说明，不给空白也不给 0
    const total = executionMs === null && queueMs === null
      ? null
      : (executionMs ?? 0) + (queueMs ?? 0);
    return {
      turnId: trace.turn_id,
      totalMs: total,
      executionMs,
      queueMs,
      rows: [],
      residualMs: 0,
      afterTurnMs,
      unknownStages: [],
      rawStages: [],
      legacy: true,
    };
  }

  const totals: Record<TimingCategoryKey, number> = {
    queue: queueMs ?? 0,
    prepare: 0,
    context: 0,
    memory: 0,
    model: 0,
    tool: 0,
    approval: 0,
    save: 0,
    organize: 0,
    other: 0,
  };
  const rawMap = new Map<string, RawStage>();
  const unknown: string[] = [];

  for (const owned of attributeSpans(spans)) {
    const existing = rawMap.get(owned.name);
    if (existing) {
      existing.ms += owned.ms;
      existing.count += 1;
    } else {
      rawMap.set(owned.name, { name: owned.name, ms: owned.ms, count: 1 });
    }
    const category = STAGE_CATEGORY[owned.name];
    if (!category) {
      if (!unknown.includes(owned.name)) unknown.push(owned.name);
      totals.other += owned.ms;
      continue;
    }
    totals[category] += owned.ms;
  }

  // residual 是后端算的「没被任何顶层阶段解释的那一点」：如实并入「其它」
  const residualMs =
    typeof ledger?.residual_ms === "number" && Number.isFinite(ledger.residual_ms)
      ? Math.max(0, Math.round(ledger.residual_ms))
      : 0;
  totals.other += residualMs;

  const totalMs =
    executionMs === null && queueMs === null ? null : (executionMs ?? 0) + (queueMs ?? 0);

  const rows: TimingRow[] = [];
  for (const key of TIMING_CATEGORY_ORDER) {
    const ms = Math.round(totals[key]);
    if (ms <= 0) continue;
    rows.push({
      key,
      label: TIMING_CATEGORY_LABEL[key],
      ms,
      percent: percentOf(ms, totalMs ?? 0),
    });
  }

  return {
    turnId: trace.turn_id,
    totalMs,
    executionMs,
    queueMs,
    rows,
    residualMs,
    afterTurnMs,
    unknownStages: unknown,
    rawStages: [...rawMap.values()].sort((a, b) => b.ms - a.ms),
    legacy: false,
  };
}

/** 毫秒 → 用户可读的时间：单位 + 可读小数（读屏也念得顺）。 */
export function formatMs(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return "未知";
  const value = Math.max(0, ms);
  if (value < 1000) return `${Math.round(value)} 毫秒`;
  if (value < 10_000) return `${(value / 1000).toFixed(1)} 秒`;
  if (value < 60_000) return `${Math.round(value / 1000)} 秒`;
  const minutes = Math.floor(value / 60_000);
  const seconds = Math.round((value % 60_000) / 1000);
  return `${minutes} 分 ${seconds} 秒`;
}

/**
 * 读屏用的一句话：总耗时 + 占比最大的几项（含「其它」）。
 * 例：「总耗时 1.7 秒，其中等待确认 0.9 秒、模型 0.5 秒、其它 0.3 秒」。
 */
export function buildTimingSentence(timing: TurnTiming | null): string {
  if (!timing) return "这次没有耗时记录";
  if (timing.totalMs === null) return "这次没有耗时记录";
  const head = `总耗时 ${formatMs(timing.totalMs)}`;
  const top = [...timing.rows]
    .sort((a, b) => b.ms - a.ms)
    .slice(0, 3)
    .map((row) => `${row.label} ${formatMs(row.ms)}`);
  if (!top.length) return head;
  return `${head}，其中${top.join("、")}`;
}

/** 拉一轮的 trace 并换算；任何失败都返回 null（展示层不比对话本身更重要）。 */
export async function fetchTurnTiming(turnId: string): Promise<TurnTiming | null> {
  if (!turnId) return null;
  const detail = (await api.getTrace(turnId)) as unknown as TraceTimingSource;
  return buildTurnTiming(detail);
}
