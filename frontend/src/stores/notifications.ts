/**
 * 应用级短通知调度（B · 公共接口，契约 §3.3）。
 *
 * 只负责**调度**，不负责生成文案，也不负责业务校验：
 * - 同一时间只显示一条（不纵向堆叠）；
 * - 同一事件身份（key）只进一次；任务失败默认合并，500ms 窗口内的多次失败合成为
 *   「N 项任务失败了」并更新同一条；
 * - 5000ms 从**真正开始显示**时计时（不是从入队），悬停 / 键盘聚焦暂停、离开恢复剩余；
 * - 中央弹窗打开期间不开始显示（排队），弹窗结束后**先过滤**已恢复/已处理/过期的成员，
 *   再显示剩下的；
 * - 关闭按钮只关本次通知，不影响任何业务状态；
 * - 历史失败不重放由调用方（useTaskFailureNotices）用「首份快照基线」保证，本 store 不猜。
 */
import { defineStore } from "pinia";
import { useModalArbiterStore } from "./modalArbiter";

/** 任务失败合并窗口（集中配置，不在调用方散落）。 */
export const NOTICE_MERGE_WINDOW_MS = 500;
/** 通知真正显示后的展示时长。 */
export const NOTICE_VISIBLE_MS = 5000;

export type AppNoticeKind = "task_failed" | "setup_incomplete";

export interface AppNoticeInput {
  /** 事件身份（任务身份 + 失败事件身份；不用文案判断）。 */
  key: string;
  kind: AppNoticeKind;
  /** 显示文案（由调用方按规则生成；任务失败合并时本 store 会改写为「N 项任务失败了」）。 */
  text: string;
  taskIds?: string[];
  /** 任务失败默认 true；其他类型默认 false。 */
  mergeable?: boolean;
  /** 点击通知是否定位任务（任务失败类）。 */
  focus?: boolean;
  /**
   * 点击 / 键盘激活通知时执行的动作（例如「初始设置尚未完成」点击接续引导）。
   * 与 focus 分开：有可定位任务时 focus 优先；没有任务时用这个激活动作。
   */
  onActivate?: () => void;
  /**
   * 弹窗结束、真正要显示之前的最后一道校验：返回 false 的成员视为已经恢复/已处理/过期。
   * 不满足时整条通知被丢弃（不显示过期事实）。
   */
  stillRelevant?: () => boolean;
}

export interface AppNoticeMember {
  /** 事件身份。 */
  key: string;
  /** 这条事件对应的任务身份（没有任务时为 null）。 */
  taskId: string;
}

export interface AppNotice {
  /** 主身份（合并后的第一条事件身份）。 */
  key: string;
  kind: AppNoticeKind;
  /** 调用方给的最新一句话（未合并时直接显示）。 */
  text: string;
  mergeable: boolean;
  focus: boolean;
  createdAt: number;
  updatedAt: number;
  members: AppNoticeMember[];
}

/** 事件身份 → 最后一道校验（不入 state：函数不是可序列化状态）。 */
const relevanceChecks = new Map<string, () => boolean>();

/** 事件身份 → 点击激活动作（同样不入 state）。 */
const activationHandlers = new Map<string, () => void>();

/** 这条通知的激活动作（没有可定位任务时使用；合并通知取第一个成员的动作）。 */
export function noticeActivation(notice: AppNotice | null | undefined): (() => void) | null {
  if (!notice) return null;
  const direct = activationHandlers.get(notice.key);
  if (direct) return direct;
  for (const member of notice.members) {
    const handler = activationHandlers.get(member.key);
    if (handler) return handler;
  }
  return null;
}

/** 这条通知是否可以点击（定位任务或执行激活动作）。 */
export function noticeIsActivatable(notice: AppNotice | null | undefined): boolean {
  if (!notice) return false;
  return Boolean(notice.focus && noticeTaskIds(notice).length) || Boolean(noticeActivation(notice));
}

/** 合并后的任务数量（按任务身份去重；没有任务身份时按事件身份数量）。 */
export function noticeTaskCount(notice: AppNotice | null | undefined): number {
  if (!notice) return 0;
  const ids = new Set(notice.members.map((member) => member.taskId).filter(Boolean));
  return ids.size || notice.members.length;
}

/** 真正显示的文案：合并过的任务失败说「N 项任务失败了」，其余用调用方文案。 */
export function noticeDisplayText(notice: AppNotice | null | undefined): string {
  if (!notice) return "";
  const count = noticeTaskCount(notice);
  if (notice.kind === "task_failed" && notice.mergeable && count > 1) {
    return count + " 项任务失败了";
  }
  return notice.text;
}

/** 这条通知涉及的任务身份（去重、保序）。 */
export function noticeTaskIds(notice: AppNotice | null | undefined): string[] {
  if (!notice) return [];
  const out: string[] = [];
  for (const member of notice.members) {
    if (member.taskId && !out.includes(member.taskId)) out.push(member.taskId);
  }
  return out;
}

/** 通知里作为点击目标的完整读屏文案（不截断；可见文本可以省略）。 */
export function noticeFullText(notice: AppNotice | null | undefined): string {
  if (!notice) return "";
  const count = noticeTaskCount(notice);
  if (notice.kind === "task_failed" && notice.mergeable && count > 1) {
    return count + " 项任务失败了";
  }
  return notice.text;
}

type TimerHandle = ReturnType<typeof setTimeout> | 0;

function setTimer(handler: () => void, ms: number): TimerHandle {
  const g = globalThis as { setTimeout?: (fn: () => void, delay: number) => unknown };
  if (typeof g.setTimeout !== "function") return 0;
  return g.setTimeout(handler, Math.max(0, ms)) as TimerHandle;
}

function clearTimer(handle: TimerHandle): void {
  if (!handle) return;
  const g = globalThis as { clearTimeout?: (id: unknown) => void };
  if (typeof g.clearTimeout === "function") g.clearTimeout(handle);
}

export const useNotificationsStore = defineStore("notifications", {
  state: () => ({
    /** 排队 + 正在显示的通知（activeKey 指的那条也在其中）。 */
    notices: [] as AppNotice[],
    activeKey: null as string | null,
    /** 当前被悬停/键盘聚焦暂停的通知（只用 key 记录，避免引用失效）。 */
    pausedKeys: [] as string[],
    /** 每条通知**真正开始显示**的时间（用于计算剩余时长）。 */
    displayStartedAt: {} as Record<string, number>,
    /** 暂停时剩下的展示时长。 */
    remainingMs: {} as Record<string, number>,
    /** 合并窗口截止时间；0 = 没有合并在进行。 */
    mergeUntil: 0,
    /**
     * 中央弹窗期间挂起：正在显示的通知先撤下（DOM 不显示），保留在队列里，
     * 记录剩余展示时间；弹窗结束后恢复同一条并继续剩余时长。
     */
    suspendedForModal: false,
    /** 被挂起时正在显示的那条（key + 剩余毫秒）；null = 没有需要恢复的。 */
    suspendedActive: null as { key: string; remaining: number } | null,
    /** 自动隐藏计时器（真正显示 5s / 恢复剩余时长）。 */
    _hideTimer: 0 as TimerHandle,
    /** 合并窗口结束后触发显示的计时器。 */
    _mergeTimer: 0 as TimerHandle,
  }),
  getters: {
    active(state): AppNotice | null {
      if (!state.activeKey) return null;
      return state.notices.find((notice) => notice.key === state.activeKey) ?? null;
    },
    hasActive(state): boolean {
      return Boolean(state.activeKey) && state.notices.some((n) => n.key === state.activeKey);
    },
    queuedCount(state): number {
      return state.notices.filter((notice) => notice.key !== state.activeKey).length;
    },
    /** 这条通知当前是否被暂停（悬停/聚焦）。 */
    isPaused(state) {
      return (key: string): boolean => Boolean(key) && state.pausedKeys.includes(key);
    },
    /** 这条通知剩下的展示毫秒（正在显示且暂停时用；其余返回完整时长）。 */
    remainingFor(state) {
      return (key: string): number => {
        if (typeof state.remainingMs[key] === "number") return state.remainingMs[key];
        const started = state.displayStartedAt[key];
        if (!started) return NOTICE_VISIBLE_MS;
        return Math.max(0, NOTICE_VISIBLE_MS - (Date.now() - started));
      };
    },
  },
  actions: {
    /** 记下「这条事件结束前仍须成立」的校验。 */
    registerRelevance(key: string, check: (() => boolean) | null | undefined): void {
      if (!key) return;
      if (check) relevanceChecks.set(key, check);
      else relevanceChecks.delete(key);
    },

    /**
     * 入队一条通知。
     *
     * 三种走向：同事件身份 → 更新同一条；500ms 合并窗口内的同类任务失败 → 合并成员；
     * 其余 → 新通知排队。合并/入队都不抢焦点，也不改变任何业务状态。
     */
    enqueue(input: AppNoticeInput): void {
      const key = String(input?.key ?? "").trim();
      if (!key) return;
      const kind = input.kind;
      const mergeable = input.mergeable ?? kind === "task_failed";
      const now = Date.now();
      const taskIds = Array.isArray(input.taskIds)
        ? input.taskIds.map((id) => String(id ?? "").trim()).filter(Boolean)
        : [];
      const members: AppNoticeMember[] = taskIds.length
        ? taskIds.map((taskId) => ({ key, taskId }))
        : [{ key, taskId: "" }];
      this.registerRelevance(key, input.stillRelevant);
      if (input.onActivate) activationHandlers.set(key, input.onActivate);
      else activationHandlers.delete(key);

      // 1) 同一事件身份：只更新同一条（幂等；会员按任务身份去重）
      const sameKey = this.notices.find((notice) =>
        notice.members.some((member) => member.key === key),
      );
      if (sameKey) {
        for (const member of members) this._addMember(sameKey, member);
        if (input.text) sameKey.text = input.text;
        sameKey.updatedAt = now;
        if (this.activeKey === sameKey.key) this._restartDisplayTimer(sameKey.key);
        return;
      }

      // 2) 合并窗口内的同类任务失败：并入同一条，不产生第二条
      if (mergeable) {
        const candidate = this.notices.find(
          (notice) =>
            notice.mergeable &&
            notice.kind === kind &&
            now - notice.updatedAt <= NOTICE_MERGE_WINDOW_MS,
        );
        if (candidate) {
          for (const member of members) this._addMember(candidate, member);
          if (input.text) candidate.text = input.text;
          candidate.updatedAt = now;
          if (this.activeKey === candidate.key) this._restartDisplayTimer(candidate.key);
          return;
        }
      }

      // 3) 新通知
      const notice: AppNotice = {
        key,
        kind,
        text: String(input.text ?? ""),
        mergeable,
        focus: input.focus ?? kind === "task_failed",
        createdAt: now,
        updatedAt: now,
        members,
      };
      this.notices.push(notice);

      if (mergeable) {
        // 合并窗口内先不显示：等窗口结束再一次性显示「N 项任务失败了」
        if (!this.mergeUntil) {
          this.mergeUntil = now + NOTICE_MERGE_WINDOW_MS;
          this._armMergeTimer(this.mergeUntil - now);
        }
        return;
      }
      this.flushQueue();
    },

    /** 把一条事件身份并入通知（同任务身份不重复计数）。 */
    _addMember(notice: AppNotice, member: AppNoticeMember): void {
      const exists = notice.members.some(
        (existing) => existing.key === member.key && existing.taskId === member.taskId,
      );
      if (exists) return;
      // 同一任务身份由新身份再次报告（重试后失败）：保留两个事件身份，但任务去重
      notice.members.push({ ...member });
    },

    _armMergeTimer(delayMs: number): void {
      this._mergeTimer = setTimer(() => {
        this._mergeTimer = 0;
        this.mergeUntil = 0;
        this.flushQueue();
      }, delayMs);
    },

    _armHideTimer(key: string, delayMs: number): void {
      this._hideTimer = setTimer(() => {
        this._hideTimer = 0;
        if (this.activeKey === key) this.dismiss(key);
      }, delayMs);
    },

    _restartDisplayTimer(key: string): void {
      if (this.activeKey !== key) return;
      this.displayStartedAt[key] = Date.now();
      if (this.pausedKeys.includes(key)) {
        this.remainingMs[key] = NOTICE_VISIBLE_MS;
        return;
      }
      clearTimer(this._hideTimer);
      this._hideTimer = 0;
      this._armHideTimer(key, NOTICE_VISIBLE_MS);
    },

    /**
     * 尝试显示下一条。
     *
     * 必须同时满足：没有正在显示的、合并窗口已结束、没有中央弹窗/挂起。
     * 显示前先过滤已恢复/已处理/过期的成员；全部失效的整条丢弃（不显示过期事实）。
     */
    flushQueue(): boolean {
      if (this.activeKey) return false;
      // 中央弹窗期间：已显示的先撤下、未显示的继续排队，等弹窗结束再统一放行
      if (this.suspendedForModal) return false;
      if (this.mergeUntil && Date.now() < this.mergeUntil) return false;
      const arbiter = useModalArbiterStore();
      if (arbiter.blocksNotices) return false;
      this._pruneNotices();
      const next = this.notices[0];
      if (!next) return false;
      this.activeKey = next.key;
      this.displayStartedAt = { ...this.displayStartedAt, [next.key]: Date.now() };
      delete this.remainingMs[next.key];
      clearTimer(this._hideTimer);
      this._hideTimer = 0;
      this._armHideTimer(next.key, NOTICE_VISIBLE_MS);
      return true;
    },

    /** 过滤掉已经恢复/已处理/过期的成员；全部成员失效的通知整条移除。 */
    _pruneNotices(): void {
      const kept: AppNotice[] = [];
      for (const notice of this.notices) {
        notice.members = notice.members.filter((member) => {
          const check = relevanceChecks.get(member.key);
          if (!check) return true;
          try {
            return check() !== false;
          } catch {
            return true;
          }
        });
        if (!notice.members.length) {
          relevanceChecks.delete(notice.key);
          activationHandlers.delete(notice.key);
          if (this.activeKey === notice.key) this.activeKey = null;
          delete this.displayStartedAt[notice.key];
          delete this.remainingMs[notice.key];
          continue;
        }
        kept.push(notice);
      }
      this.notices = kept;
    },

    /**
     * 中央弹窗打开：撤下正在显示的通知（但保留在队列、记录剩余时长）。
     *
     * 任务书要求「弹窗打开前已显示的通知暂停并撤下，保留待处理事件；弹窗结束后再显示」。
     * 这里不删通知、不重置计时，也不改变任何业务状态。
     */
    suspendForModal(): void {
      if (this.suspendedForModal) return;
      this.suspendedForModal = true;
      const key = this.activeKey;
      if (!key) {
        clearTimer(this._hideTimer);
        this._hideTimer = 0;
        return;
      }
      const remaining =
        typeof this.remainingMs[key] === "number"
          ? this.remainingMs[key]
          : Math.max(0, NOTICE_VISIBLE_MS - (Date.now() - (this.displayStartedAt[key] ?? Date.now())));
      this.suspendedActive = { key, remaining };
      clearTimer(this._hideTimer);
      this._hideTimer = 0;
      delete this.remainingMs[key];
      // 撤下显示：activeKey 置空 → 组件不再渲染；通知仍留在 notices 队列里
      this.activeKey = null;
    },

    /**
     * 中央弹窗关闭：恢复被挂起的那条（按剩余时长继续），或显示弹窗期间新入队的。
     *
     * 「弹窗期间新入队的」在真正开始显示时起算 5s（由 flushQueue 内的显示逻辑保证）。
     */
    resumeFromModal(): void {
      if (!this.suspendedForModal && !this.suspendedActive) {
        this.flushQueue();
        return;
      }
      this.suspendedForModal = false;
      const suspended = this.suspendedActive;
      this.suspendedActive = null;
      if (suspended && this.notices.some((notice) => notice.key === suspended.key)) {
        const remaining = Math.max(0, Math.min(NOTICE_VISIBLE_MS, suspended.remaining));
        this.activeKey = suspended.key;
        this.pausedKeys = this.pausedKeys.filter((entry) => entry !== suspended.key);
        this.displayStartedAt[suspended.key] = Date.now() - (NOTICE_VISIBLE_MS - remaining);
        delete this.remainingMs[suspended.key];
        clearTimer(this._hideTimer);
        this._hideTimer = 0;
        this._armHideTimer(suspended.key, remaining);
        return;
      }
      // 被挂起的那条已经不在队列里：显示下一条
      this.flushQueue();
    },

    /** 关闭一条通知（只关这次显示，不影响业务；成员校验一并清理）。 */
    dismiss(key: string): void {
      const target = String(key ?? "").trim();
      if (!target) return;
      const notice = this.notices.find((item) =>
        item.key === target || item.members.some((member) => member.key === target),
      );
      if (!notice) return;
      const wasActive = this.activeKey === notice.key;
      this.notices = this.notices.filter((item) => item.key !== notice.key);
      for (const member of notice.members) {
        relevanceChecks.delete(member.key);
        activationHandlers.delete(member.key);
      }
      relevanceChecks.delete(notice.key);
      activationHandlers.delete(notice.key);
      this.pausedKeys = this.pausedKeys.filter((entry) => entry !== notice.key);
      delete this.displayStartedAt[notice.key];
      delete this.remainingMs[notice.key];
      if (this.suspendedActive?.key === notice.key) this.suspendedActive = null;
      if (wasActive) {
        this.activeKey = null;
        clearTimer(this._hideTimer);
        this._hideTimer = 0;
        this.flushQueue();
      }
    },

    /** 悬停 / 键盘聚焦：暂停倒计时（记录剩余时长）。 */
    pause(key: string): void {
      const target = String(key ?? "").trim();
      if (!target || this.pausedKeys.includes(target)) return;
      if (this.activeKey !== target) return;
      this.pausedKeys = [...this.pausedKeys, target];
      const started = this.displayStartedAt[target] ?? Date.now();
      this.remainingMs[target] = Math.max(0, NOTICE_VISIBLE_MS - (Date.now() - started));
      clearTimer(this._hideTimer);
      this._hideTimer = 0;
    },

    /** 移开 / 失焦：按剩余时长继续。 */
    resume(key: string): void {
      const target = String(key ?? "").trim();
      if (!target) return;
      this.pausedKeys = this.pausedKeys.filter((entry) => entry !== target);
      if (this.activeKey !== target) {
        delete this.remainingMs[target];
        return;
      }
      const remaining =
        typeof this.remainingMs[target] === "number" ? this.remainingMs[target] : NOTICE_VISIBLE_MS;
      delete this.remainingMs[target];
      const used = NOTICE_VISIBLE_MS - remaining;
      this.displayStartedAt[target] = Date.now() - used;
      clearTimer(this._hideTimer);
      this._hideTimer = 0;
      this._armHideTimer(target, remaining);
    },

    /** 清空（重连基线 / 页面卸载 / 测试收口）。 */
    reset(): void {
      clearTimer(this._hideTimer);
      clearTimer(this._mergeTimer);
      this._hideTimer = 0;
      this._mergeTimer = 0;
      this.notices = [];
      this.activeKey = null;
      this.pausedKeys = [];
      this.displayStartedAt = {};
      this.remainingMs = {};
      this.mergeUntil = 0;
      this.suspendedForModal = false;
      this.suspendedActive = null;
      relevanceChecks.clear();
      activationHandlers.clear();
    },
  },
});
