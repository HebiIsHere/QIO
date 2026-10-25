/**
 * 意图显示位置的**权威状态**（A 组 F10）。
 *
 * 三类位置必须分开，不能混成一个「覆盖值」：
 * 1. temporary：拖动中的预览（只存在于 IntentLayer 的手势里，本模块不保存）；
 * 2. pending：松手后正在保存的位置（等服务器回执）；
 * 3. confirmed：服务器读取到 / 保存回执确认的位置。
 *
 * 规则（对应 F10 的 6 条要求）：
 * - 保存成功只认**回执里的位置**（没有就用这次提交的位置），并写成本机缓存；
 * - 保存失败回退到**这次操作前**的位置（confirmed，没有才用本机缓存），并留下原因；
 * - 按对象 + 操作版本归属：旧操作的回执（成功或失败）不能改写更新的成功结果；
 * - 一张卡失败不影响另一张；
 * - 服务器读取到的 display 优先于本机缓存（缓存只能填服务器没给的位置）；
 * - 操作结束后清掉临时覆盖，后续服务器读取 / 父级更新能生效；
 * - 意图不在清单里时清理本地记录，避免旧位置套到别的新意图上。
 */
export interface IntentDisplayPoint {
  x: number;
  y: number;
}

export type DisplayPhase = "pending" | "failed";

export interface DisplayStateEntry {
  phase: DisplayPhase;
  error: string | null;
}

export interface DisplayFailure {
  intentId: string;
  error: string;
}

export interface DisplaySnapshot {
  /** 交给 IntentLayer 的位置覆盖（等待保存 / 已保存 / 已确认 / 本机缓存） */
  overrides: Record<string, IntentDisplayPoint>;
  /** 仍在保存 / 保存失败的状态（界面据此标注，不把未保存的位置说成已保存） */
  states: Record<string, DisplayStateEntry>;
  failures: DisplayFailure[];
}

interface PendingSave {
  op: number;
  point: IntentDisplayPoint;
  /** 这次操作前的位置（失败要回到它，而不是更早的默认位置） */
  previous: IntentDisplayPoint | null;
}

function finite(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function readPoint(raw: unknown): IntentDisplayPoint | null {
  if (!raw || typeof raw !== "object") return null;
  const record = raw as { x?: unknown; y?: unknown };
  const x = finite(record.x);
  const y = finite(record.y);
  if (x === null || y === null) return null;
  return { x, y };
}

export class IntentDisplayAuthority {
  private readonly confirmed = new Map<string, IntentDisplayPoint>();
  private readonly cache = new Map<string, IntentDisplayPoint>();
  private readonly pending = new Map<string, PendingSave>();
  private readonly failed = new Map<string, string>();
  private readonly opSeq = new Map<string, number>();

  constructor(cache?: Record<string, IntentDisplayPoint> | null) {
    for (const [intentId, point] of Object.entries(cache ?? {})) {
      const safe = readPoint(point);
      if (intentId && safe) this.cache.set(intentId, safe);
    }
  }

  /**
   * 服务器读取到的新事实：给了 display 就以它为准（覆盖本机缓存）；
   * 没给（旧映射没有该字段）就保留已知位置，不退回旧缓存。
   * 已经不在清单里的意图：清掉本地记录。
   */
  applyServerViews(views: readonly { intent_id?: string; display?: unknown }[] | null | undefined): void {
    const seen = new Set<string>();
    for (const view of views ?? []) {
      const intentId = typeof view?.intent_id === "string" ? view.intent_id : "";
      if (!intentId) continue;
      seen.add(intentId);
      const point = readPoint(view?.display);
      if (!point) continue;
      if (this.pending.has(intentId)) continue; // 在飞保存的位置更晚，等它落地
      this.confirmed.set(intentId, point);
      this.cache.set(intentId, point);
      this.failed.delete(intentId);
    }
    for (const intentId of [...this.confirmed.keys()]) {
      if (!seen.has(intentId)) this.confirmed.delete(intentId);
    }
    for (const intentId of [...this.cache.keys()]) {
      if (!seen.has(intentId)) this.cache.delete(intentId);
    }
    for (const intentId of [...this.failed.keys()]) {
      if (!seen.has(intentId)) this.failed.delete(intentId);
    }
    for (const intentId of [...this.pending.keys()]) {
      if (!seen.has(intentId)) {
        this.pending.delete(intentId);
        this.failed.delete(intentId);
      }
    }
  }

  /** 开始一次保存：返回这次操作版本（同一意图单调递增）。 */
  begin(intentId: string, point: IntentDisplayPoint): number {
    const safe = readPoint(point) ?? { x: 0, y: 0 };
    const op = (this.opSeq.get(intentId) ?? 0) + 1;
    this.opSeq.set(intentId, op);
    this.pending.set(intentId, {
      op,
      point: safe,
      previous: this.confirmed.get(intentId) ?? this.cache.get(intentId) ?? null,
    });
    this.failed.delete(intentId);
    return op;
  }

  /** 保存成功：只认这次操作（更旧的 op 一律忽略）。返回 false = 这次回执已经过期。 */
  settle(intentId: string, op: number, serverPoint?: IntentDisplayPoint | null): boolean {
    const current = this.pending.get(intentId);
    if (!current || current.op !== op) return false;
    this.pending.delete(intentId);
    const accepted = readPoint(serverPoint) ?? current.point;
    this.confirmed.set(intentId, accepted);
    this.cache.set(intentId, accepted);
    this.failed.delete(intentId);
    return true;
  }

  /** 保存失败：回退到这次操作前的位置并记录原因（更旧的 op 不能撤销更新的成功）。 */
  fail(intentId: string, op: number, reason: string): boolean {
    const current = this.pending.get(intentId);
    if (!current || current.op !== op) return false;
    this.pending.delete(intentId);
    this.failed.set(intentId, String(reason ?? "") || "原因未知");
    return true;
  }

  /** 用户关掉失败说明后：只清提示，不改任何位置。 */
  clearFailures(): void {
    this.failed.clear();
  }

  /** 这一帧每张卡画在哪、处于什么保存状态。 */
  snapshot(): DisplaySnapshot {
    const overrides: Record<string, IntentDisplayPoint> = {};
    const ids = new Set<string>([
      ...this.confirmed.keys(),
      ...this.cache.keys(),
      ...this.pending.keys(),
    ]);
    for (const intentId of ids) {
      const pending = this.pending.get(intentId);
      const point = pending?.point ?? this.confirmed.get(intentId) ?? this.cache.get(intentId);
      if (point) overrides[intentId] = { x: point.x, y: point.y };
    }
    const states: Record<string, DisplayStateEntry> = {};
    for (const [intentId, pending] of this.pending) {
      if (pending) states[intentId] = { phase: "pending", error: null };
    }
    for (const [intentId, error] of this.failed) {
      states[intentId] = { phase: "failed", error };
    }
    const failures = [...this.failed].map(([intentId, error]) => ({ intentId, error }));
    return { overrides, states, failures };
  }

  /** 需要写回本机缓存的那一份（只包含已确认 / 已保存的位置，不含临时与失败位置）。 */
  cacheSnapshot(): Record<string, IntentDisplayPoint> {
    const out: Record<string, IntentDisplayPoint> = {};
    for (const [intentId, point] of this.cache) out[intentId] = { x: point.x, y: point.y };
    return out;
  }

  /** 位置状态（测试与外部排查用） */
  stateOf(intentId: string): DisplayStateEntry | undefined {
    return this.snapshot().states[intentId];
  }
}
