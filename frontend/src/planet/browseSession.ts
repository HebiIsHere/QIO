/**
 * Planet 浏览会话（第二阶段的核心：流动话题窗口）。
 *
 * 三个概念在这里必须分清楚：
 * - **总话题数**：后端数据层有多少就有多少（无上限）；
 * - **可见容量**：同屏最多出现多少个话题（视觉层，固定）；
 * - **展示窗口**：此刻窗口里具体是哪几个话题 —— 本模块维护的东西。
 *
 * 关键设计：**话题在槽位里保持不动，只有转到背面（用户看不见）的槽位才会被
 * 回收成下一个话题**。因此：
 * - 前进一次只换掉一个槽位，其它话题不会跳位或互换；
 * - 短距离反向浏览能把刚离开的话题放回原来的槽位（exitStack）；
 * - 一个话题至少要展示 minLifetimeMs 才可能被换走（节奏慢，不是老虎机）；
 * - 被选中 / 被搜索注入的话题在查看期间锁定，不会被回收；
 * - 近期展示过的话题进入冷却，不会立刻从另一侧再次出现（但没有永久去重：
 *   长时间浏览后允许重新遇见）。
 *
 * 本模块不依赖 Three.js，也不依赖 Vue：渲染层只负责「哪个槽位在背面」。
 */

/** 视觉层容量：与后端 services/planet.py 的 VISIBLE_CAPACITY 保持一致。 */
export const VISIBLE_CAPACITY = 12;
/** 一个话题至少被展示多久才可能被回收（毫秒）。 */
export const DEFAULT_MIN_LIFETIME_MS = 1200;
/** 近期展示记录长度：太长会让小数据集转不动，太短会察觉重复。 */
export const DEFAULT_COOLDOWN = 24;

export interface BrowseTopic {
  topic_id: string;
  title: string;
  fragment_count: number;
  last_activity: string | null;
  summary_preview?: string | null;
  visual_seed?: number;
}

export interface SwapPlan {
  slot: number;
  topic: BrowseTopic | null;
}

export interface BrowseSessionOptions {
  capacity?: number;
  minLifetimeMs?: number;
  cooldownSize?: number;
}

interface ExitEntry {
  slot: number;
  topic: BrowseTopic | null;
}

export class PlanetBrowseSession {
  readonly capacity: number;

  private readonly minLifetimeMs: number;
  private readonly cooldownSize: number;
  /** 还没进过窗口的话题（来自后端浏览序列的分页结果）。 */
  private queue: BrowseTopic[] = [];
  /** 已经离开窗口、可以再次被取用的话题（FIFO，保证重新遇见有间隔）。 */
  private pool: BrowseTopic[] = [];
  private slots: (BrowseTopic | null)[];
  /** 最近几次「槽位被换掉」的记录，供反向浏览原样还原。 */
  private exitStack: ExitEntry[] = [];
  /** 近期展示过的 topic_id（冻结窗口里的旧记录）。 */
  private recent: string[] = [];
  private shownAt = new Map<string, number>();
  private protectedIds = new Set<string>();
  private lockedId: string | null = null;
  private lastSwapAt = Number.NEGATIVE_INFINITY;

  constructor(options: BrowseSessionOptions = {}) {
    this.capacity = Math.max(1, options.capacity ?? VISIBLE_CAPACITY);
    this.minLifetimeMs = Math.max(0, options.minLifetimeMs ?? DEFAULT_MIN_LIFETIME_MS);
    this.cooldownSize = Math.max(0, options.cooldownSize ?? DEFAULT_COOLDOWN);
    this.slots = new Array(this.capacity).fill(null);
  }

  // -- 序列 -------------------------------------------------------------

  /** 新一批浏览序列（打开星球的第一页，或者跨圈后的新一页）。 */
  setSequence(items: BrowseTopic[]): void {
    this.queue = this.dedupe(items);
  }

  /** 追加预取的一页。 */
  appendSequence(items: BrowseTopic[]): void {
    this.queue.push(...this.dedupe(items));
  }

  private dedupe(items: BrowseTopic[]): BrowseTopic[] {
    const known = new Set<string>([
      ...this.queue.map((t) => t.topic_id),
      ...this.pool.map((t) => t.topic_id),
      ...this.slots.filter((t): t is BrowseTopic => t !== null).map((t) => t.topic_id),
    ]);
    const out: BrowseTopic[] = [];
    for (const item of items) {
      if (!item || known.has(item.topic_id)) continue;
      known.add(item.topic_id);
      out.push(item);
    }
    return out;
  }

  /** 队列快见底时调用方应该继续向后端要下一页。 */
  needsMore(): boolean {
    return this.queue.length < this.capacity;
  }

  // -- 窗口 -------------------------------------------------------------

  /** 当前展示窗口（长度固定 = capacity，空位为 null）。 */
  windowSlots(): (BrowseTopic | null)[] {
    return [...this.slots];
  }

  /** 初始填充：把第一批话题放进空槽位。 */
  fill(nowMs: number): void {
    for (let slot = 0; slot < this.capacity; slot++) {
      if (this.slots[slot]) continue;
      const next = this.queue.shift();
      if (!next) break;
      this.slots[slot] = next;
      this.markShown(next.topic_id, nowMs);
    }
  }

  /**
   * 前进 / 后退一格。
   *
   * `backSlots` 是渲染层给出的「当前在星球背面（用户看不见）」的槽位顺序：
   * 数据替换只发生在低感知区域，用户看到的是话题自然地从远处进入。
   */
  takeSwap(direction: 1 | -1, backSlots: number[], nowMs: number): SwapPlan | null {
    if (direction === 1) return this.swapForward(backSlots, nowMs);
    return this.swapBackward(nowMs);
  }

  private swapForward(backSlots: number[], nowMs: number): SwapPlan | null {
    if (nowMs - this.lastSwapAt < this.minLifetimeMs) return null;
    const slot = this.pickRecycleSlot(backSlots, nowMs);
    if (slot === null) return null;
    const next = this.pickNext();
    if (!next) return null;

    const displaced = this.slots[slot];
    this.slots[slot] = next;
    if (displaced) {
      this.exitStack.push({ slot, topic: displaced });
      this.pool.push(displaced);
      if (this.exitStack.length > this.capacity * 4) this.exitStack.shift();
      if (this.pool.length > this.capacity * 4) this.pool.shift();
    }
    this.markShown(next.topic_id, nowMs);
    this.lastSwapAt = nowMs;
    return { slot, topic: next };
  }

  private swapBackward(nowMs: number): SwapPlan | null {
    while (this.exitStack.length) {
      const entry = this.exitStack.pop() as ExitEntry;
      if (!entry.topic) continue;
      if (this.isInWindow(entry.topic.topic_id)) continue; // 已经在别的槽位显示中，跳过
      this.removeFromPool(entry.topic.topic_id);
      const current = this.slots[entry.slot];
      this.slots[entry.slot] = entry.topic;
      if (current) this.queue.unshift(current);
      this.markShown(entry.topic.topic_id, nowMs);
      this.lastSwapAt = nowMs;
      return { slot: entry.slot, topic: entry.topic };
    }
    return null;
  }

  /**
   * 回收哪个槽位：优先背面、已展示够久、且没有被用户锁定的槽位。
   * 找不到就这一轮不换（视觉连续性优先于数据顺序）。
   */
  private pickRecycleSlot(backSlots: number[], nowMs: number): number | null {
    for (const slot of backSlots) {
      if (!Number.isInteger(slot) || slot < 0 || slot >= this.capacity) continue;
      const occupant = this.slots[slot];
      if (!occupant) return slot;
      if (this.isProtected(occupant.topic_id)) continue;
      if (this.isInWindow(occupant.topic_id) === false) continue;
      const shown = this.shownAt.get(occupant.topic_id) ?? 0;
      if (nowMs - shown < this.minLifetimeMs) continue;
      return slot;
    }
    return null;
  }

  private pickNext(): BrowseTopic | null {
    const fresh = this.takeFromQueue();
    if (fresh) return fresh;
    return this.takeFromPool();
  }

  private takeFromQueue(): BrowseTopic | null {
    let scanned = 0;
    while (this.queue.length && scanned <= this.queue.length) {
      const candidate = this.queue.shift() as BrowseTopic;
      scanned += 1;
      if (this.isInWindow(candidate.topic_id)) continue;
      if (this.recent.includes(candidate.topic_id) && this.queue.length >= scanned) {
        // 冷却中：放到队尾，先去试别人（但不是永久去重）
        this.queue.push(candidate);
        continue;
      }
      return candidate;
    }
    return null;
  }

  private takeFromPool(): BrowseTopic | null {
    let scanned = 0;
    while (this.pool.length && scanned <= this.pool.length) {
      const candidate = this.pool.shift() as BrowseTopic;
      scanned += 1;
      if (this.isInWindow(candidate.topic_id) || this.isProtected(candidate.topic_id)) {
        this.pool.push(candidate);
        continue;
      }
      if (this.recent.includes(candidate.topic_id) && this.pool.length >= scanned) {
        this.pool.push(candidate);
        continue;
      }
      return candidate;
    }
    return null;
  }

  private removeFromPool(topicId: string): void {
    this.pool = this.pool.filter((t) => t.topic_id !== topicId);
  }

  // -- 选择 / 注入 -------------------------------------------------------

  /** 用户选中的话题：查看期间不会被回收（取消选中后重新进入流动）。 */
  lock(topicId: string | null): void {
    this.lockedId = topicId;
  }

  /**
   * 把某个话题（搜索结果 / 当前所在话题）放进窗口并返回槽位。
   * 已经在窗口里就返回原槽位；否则占用一个「没被锁定的最旧槽位」。
   */
  pinTopic(topic: BrowseTopic): number {
    const existing = this.slots.findIndex((t) => t?.topic_id === topic.topic_id);
    if (existing >= 0) return existing;

    this.protectedIds.add(topic.topic_id);
    this.removeFromPool(topic.topic_id);
    this.queue = this.queue.filter((t) => t.topic_id !== topic.topic_id);

    let target = this.slots.findIndex((t) => t === null);
    if (target < 0) {
      let oldest = Number.POSITIVE_INFINITY;
      for (let slot = 0; slot < this.capacity; slot++) {
        const occupant = this.slots[slot];
        if (!occupant || this.isLocked(occupant.topic_id)) continue;
        const shown = this.shownAt.get(occupant.topic_id) ?? 0;
        if (shown < oldest) {
          oldest = shown;
          target = slot;
        }
      }
    }
    if (target < 0) target = 0; // 理论上到不了：容量 ≥ 1 且锁定项最多 1 个

    const displaced = this.slots[target];
    if (displaced && displaced.topic_id !== topic.topic_id) {
      this.exitStack.push({ slot: target, topic: displaced });
      this.pool.push(displaced);
    }
    this.slots[target] = topic;
    this.markShown(topic.topic_id, Date.now());
    return target;
  }

  // -- 内部工具 ---------------------------------------------------------

  private markShown(topicId: string, nowMs: number): void {
    this.shownAt.set(topicId, nowMs);
    this.recent.push(topicId);
    while (this.recent.length > this.cooldownSize) this.recent.shift();
  }

  private isInWindow(topicId: string): boolean {
    return this.slots.some((t) => t?.topic_id === topicId);
  }

  private isLocked(topicId: string): boolean {
    return this.lockedId !== null && this.lockedId === topicId;
  }

  private isProtected(topicId: string): boolean {
    return this.isLocked(topicId) || this.protectedIds.has(topicId);
  }
}
