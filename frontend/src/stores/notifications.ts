/**
 * 应用级短通知调度（B · 公共接口，契约 §3.3；A · F03—F06 修复）。
 *
 * 只负责**调度**，不负责生成业务状态，也不负责猜任务是否仍需处理：
 * - 同一时间只显示一条（不纵向堆叠）；
 * - 同一事件身份（key）只进一次；任务失败默认合并，500ms 窗口内的多次失败合成为
 *   「N 项任务失败了」并更新同一条；
 * - **每个成员保留自己的文案来源（AppNoticeMember.text）**：合并/去重/过滤/挂起恢复后，
 *   显示文案、数量、完整读屏文案、点击目标一律从剩余有效成员重新生成 ——
 *   不再用“最后一个成员的文本”覆盖整条通知（F03）；
 * - 5000ms 从**真正开始显示**时计时（不是从入队），悬停 / 键盘聚焦**分别记录暂停原因**，
 *   全部解除才恢复剩余时长（F06）；
 * - 中央弹窗打开期间不开始显示（排队），弹窗结束后**先过滤**已恢复/已处理/过期的成员
 *   （恢复路径与首次显示走同一套核对，F04），再把弹窗期间分时到达的失败**重新合并**
 *   成一条汇总（F05）；
 * - 旧通知合入新的未显示失败时，更新后的汇总获得一次完整展示机会；重复旧事件不重置计时；
 * - 关闭过（或自动收起过）的任务失败事件身份不会复活（F03）；
 * - 关闭按钮只关本次通知，不影响任何业务状态；
 * - 历史失败不重放由调用方（useTaskFailureNotices）用「首份快照基线」保证，本 store 不猜。
 */
import { defineStore } from "pinia";
import { useModalArbiterStore } from "./modalArbiter";

/** 任务失败合并窗口（集中配置，不在调用方散落）。 */
export const NOTICE_MERGE_WINDOW_MS = 500;
/** 通知真正显示后的展示时长。 */
export const NOTICE_VISIBLE_MS = 5000;
/** 已收口（关闭/自动收起）的任务失败事件身份上限（有界，避免长会话无限增长）。 */
export const NOTICE_SETTLED_LIMIT = 200;

export type AppNoticeKind = "task_failed" | "setup_incomplete";

export interface AppNoticeInput {
  /** 事件身份（任务身份 + 失败事件身份；不用文案判断）。 */
  key: string;
  kind: AppNoticeKind;
  /** 这条事件的文案来源；任务失败合并时每个成员各自保留自己的这一份。 */
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
   * 不满足时整条通知被丢弃（不显示过期事实）。恢复被挂起的通知前也会重新核对。
   */
  stillRelevant?: () => boolean;
}

export interface AppNoticeMember {
  /** 事件身份。 */
  key: string;
  /** 这条事件对应的任务身份（没有任务时为 null）。 */
  taskId: string;
  /** 这个成员自己的文案来源（显示时按剩余有效成员重新生成，不互相覆盖）。 */
  text: string;
}

export interface AppNotice {
  /** 主身份（合并后的第一条事件身份）。 */
  key: string;
  kind: AppNoticeKind;
  /** 最近一次入队给的一句话（仅作兜底；显示优先用成员各自的 text）。 */
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
  if (!notice || !Array.isArray(notice.members)) return 0;
  const ids = new Set(notice.members.map((member) => member.taskId).filter(Boolean));
  return ids.size || notice.members.length;
}

function memberTextOf(member: AppNoticeMember | null | undefined): string {
  if (!member) return "";
  return typeof member.text === "string" ? member.text : "";
}

/**
 * 真正显示的文案：**从剩余有效成员重新生成**。
 *
 * - 去重后的任务数 ≥ 2：合成「N 项任务失败了」（N = 任务身份去重后的数量）；
 * - 只剩一个任务：用该任务最新成员自己的文案（不是最后一个入队成员的文案）；
 * - 没有任务身份（例如引导提醒）：用最后成员自己的文案；
 * - 没有成员：空串（调用方应已丢弃整条通知）。
 */
export function noticeDisplayText(notice: AppNotice | null | undefined): string {
  if (!notice || !Array.isArray(notice.members) || !notice.members.length) return "";
  const count = noticeTaskCount(notice);
  if (notice.kind === "task_failed" && notice.mergeable && count > 1) {
    return count + " 项任务失败了";
  }
  const taskIds = notice.members.map((member) => member.taskId).filter(Boolean);
  const taskId = taskIds.length ? taskIds[taskIds.length - 1] : "";
  if (taskId) {
    const sameTask = notice.members.filter((member) => member.taskId === taskId);
    const text = memberTextOf(sameTask[sameTask.length - 1]);
    if (text) return text;
  }
  return memberTextOf(notice.members[notice.members.length - 1]) || notice.text;
}

/** 这条通知涉及的任务身份（去重、保序）。 */
export function noticeTaskIds(notice: AppNotice | null | undefined): string[] {
  if (!notice || !Array.isArray(notice.members)) return [];
  const out: string[] = [];
  for (const member of notice.members) {
    if (member.taskId && !out.includes(member.taskId)) out.push(member.taskId);
  }
  return out;
}

/** 通知里作为点击目标的完整读屏文案（不截断；与显示文案同源，始终一致）。 */
export function noticeFullText(notice: AppNotice | null | undefined): string {
  return noticeDisplayText(notice);
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

interface SuspendedNotice {
  key: string;
  remaining: number;
  /** 挂起那一刻的成员事件身份快照：恢复时用来判断「是否合入了新的失败」。 */
  memberKeys: string[];
}

export const useNotificationsStore = defineStore("notifications", {
  state: () => ({
    /** 排队 + 正在显示的通知（activeKey 指的那条也在其中）。 */
    notices: [] as AppNotice[],
    activeKey: null as string | null,
    /**
     * 每条通知的暂停原因（hover / focus / manual …）。
     * 只有**全部**原因解除才恢复剩余时长（F06）；pausedKeys 是从这里派生的只读视图。
     */
    pauseReasons: {} as Record<string, string[]>,
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
    /** 被挂起时正在显示的那条（key + 剩余毫秒 + 成员身份快照）；null = 没有需要恢复的。 */
    suspendedActive: null as SuspendedNotice | null,
    /** 已收口的任务失败事件身份（关闭/自动收起后不再复活；有界）。 */
    settledKeys: [] as string[],
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
    /** 当前被暂停的通知 key（由 pauseReasons 派生，保留旧字段名兼容既有调用）。 */
    pausedKeys(state): string[] {
      return Object.keys(state.pauseReasons).filter(
        (key) => (state.pauseReasons[key] ?? []).length > 0,
      );
    },
    /** 这条通知当前是否被暂停（悬停/聚焦/手动）。 */
    isPaused(state) {
      return (key: string): boolean => Boolean(key) && (state.pauseReasons[key] ?? []).length > 0;
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
     * 三种走向：同事件身份 → 更新同一条（重复旧事件不重置计时）；500ms 合并窗口内的同类
     * 任务失败 → 合并成员；其余 → 新通知排队。合并/入队都不抢焦点，也不改变任何业务状态。
     */
    enqueue(input: AppNoticeInput): void {
      const key = String(input?.key ?? "").trim();
      if (!key) return;
      // 关闭过/已提醒过的任务失败事件身份不再复活（F03）
      if (this.settledKeys.includes(key)) return;
      const kind = input.kind;
      const mergeable = input.mergeable ?? kind === "task_failed";
      const now = Date.now();
      const text = String(input.text ?? "");
      const taskIds = Array.isArray(input.taskIds)
        ? input.taskIds.map((id) => String(id ?? "").trim()).filter(Boolean)
        : [];
      const members: AppNoticeMember[] = (taskIds.length ? taskIds : [""]).map((taskId) => ({
        key,
        taskId,
        text,
      }));
      this.registerRelevance(key, input.stillRelevant);
      if (input.onActivate) activationHandlers.set(key, input.onActivate);
      else activationHandlers.delete(key);

      // 1) 同一事件身份：更新同一条（幂等；成员按事件身份 + 任务身份去重）
      const sameKey = this.notices.find((notice) =>
        notice.members.some((member) => member.key === key),
      );
      if (sameKey) {
        let changed = false;
        for (const member of sameKey.members) {
          if (member.key === key && text && member.text !== text) {
            member.text = text;
            changed = true;
          }
        }
        for (const member of members) {
          if (this._addMember(sameKey, member)) changed = true;
        }
        if (text && text !== sameKey.text) {
          sameKey.text = text;
          changed = true;
        }
        if (!changed) return;
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
          let changed = false;
          for (const member of members) {
            if (this._addMember(candidate, member)) changed = true;
          }
          if (text && text !== candidate.text) {
            candidate.text = text;
            changed = true;
          }
          if (!changed) return;
          candidate.updatedAt = now;
          // 旧通知合入新的未显示失败：给更新后的汇总一次完整展示机会
          if (this.activeKey === candidate.key) this._restartDisplayTimer(candidate.key);
          return;
        }
      }

      // 3) 新通知
      const notice: AppNotice = {
        key,
        kind,
        text,
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

    /** 把一条事件身份并入通知（同事件身份 + 同任务身份不重复计数）；返回是否新增成员。 */
    _addMember(notice: AppNotice, member: AppNoticeMember): boolean {
      const existing = notice.members.find(
        (candidate) => candidate.key === member.key && candidate.taskId === member.taskId,
      );
      if (existing) {
        if (member.text && existing.text !== member.text) existing.text = member.text;
        return false;
      }
      notice.members.push({ ...member });
      return true;
    },

    _pausedReasonsFor(key: string): string[] {
      const reasons = this.pauseReasons[key];
      return Array.isArray(reasons) ? reasons : [];
    },

    /** 清掉一条通知的暂停原因（关闭/撤下/重新显示时用；不重启计时器）。 */
    _clearPauses(key: string): void {
      if (!this.pauseReasons[key]) return;
      const next = { ...this.pauseReasons };
      delete next[key];
      this.pauseReasons = next;
    },

    /** 判断成员此刻是否仍需要提醒（显示前、恢复前都走这里）。 */
    _memberStillRelevant(member: AppNoticeMember): boolean {
      const check = relevanceChecks.get(member.key);
      if (!check) return true;
      try {
        return check() !== false;
      } catch {
        return true;
      }
    },

    /** 整条通知离开队列：清理校验/激活动作/计时/暂停原因。 */
    _forgetNotice(notice: AppNotice): void {
      for (const member of notice.members) {
        relevanceChecks.delete(member.key);
        activationHandlers.delete(member.key);
      }
      relevanceChecks.delete(notice.key);
      activationHandlers.delete(notice.key);
      if (this.activeKey === notice.key) {
        this.activeKey = null;
        clearTimer(this._hideTimer);
        this._hideTimer = 0;
      }
      delete this.displayStartedAt[notice.key];
      delete this.remainingMs[notice.key];
      this._clearPauses(notice.key);
      if (this.suspendedActive?.key === notice.key) this.suspendedActive = null;
    },

    _rememberSettled(notice: AppNotice): void {
      const keys = [notice.key, ...notice.members.map((member) => member.key)].filter(Boolean);
      const merged = [...this.settledKeys];
      for (const key of keys) if (!merged.includes(key)) merged.push(key);
      this.settledKeys =
        merged.length > NOTICE_SETTLED_LIMIT ? merged.slice(-NOTICE_SETTLED_LIMIT) : merged;
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
      if (this._pausedReasonsFor(key).length) {
        // 暂停中合入新失败：恢复后仍给一次完整展示机会
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
      this._clearPauses(next.key);
      clearTimer(this._hideTimer);
      this._hideTimer = 0;
      this._armHideTimer(next.key, NOTICE_VISIBLE_MS);
      return true;
    },

    /** 过滤掉已经恢复/已处理/过期的成员；全部成员失效的通知整条移除。 */
    _pruneNotices(): void {
      const kept: AppNotice[] = [];
      for (const notice of this.notices) {
        notice.members = notice.members.filter((member) => this._memberStillRelevant(member));
        if (!notice.members.length) {
          this._forgetNotice(notice);
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
     * 这里不删通知、不重置计时，也不改变任何业务状态；暂停原因随 DOM 撤下一起清掉
     * （否则鼠标/焦点事件不会再发生，通知会永远暂停）。
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
      const notice = this.notices.find((item) => item.key === key) ?? null;
      const remaining =
        typeof this.remainingMs[key] === "number"
          ? this.remainingMs[key]
          : Math.max(
              0,
              NOTICE_VISIBLE_MS - (Date.now() - (this.displayStartedAt[key] ?? Date.now())),
            );
      this.suspendedActive = {
        key,
        remaining,
        memberKeys: notice ? notice.members.map((member) => member.key) : [],
      };
      clearTimer(this._hideTimer);
      this._hideTimer = 0;
      delete this.remainingMs[key];
      this._clearPauses(key);
      // 撤下显示：activeKey 置空 → 组件不再渲染；通知仍留在 notices 队列里
      this.activeKey = null;
    },

    /**
     * 弹窗期间分时到达的任务失败：重新去重/合并成一条汇总（F05）。
     *
     * 500ms 合并窗口只用来减少日常抖动；弹窗期间可能持续数秒，多批失败各自成条，
     * 这里按「仍是有效成员」重新汇总，避免结束后依次弹两条。
     * 只合并 mergeable 的 task_failed —— 中性引导提醒（setup_incomplete）不并入失败数。
     */
    _coalesceTaskFailures(): void {
      const failures = this.notices.filter(
        (notice) => notice.kind === "task_failed" && notice.mergeable,
      );
      if (failures.length < 2) return;
      const target = failures[0];
      let gained = false;
      for (const extra of failures.slice(1)) {
        for (const member of extra.members) {
          if (this._addMember(target, member)) gained = true;
        }
      }
      const extraKeys = new Set(failures.slice(1).map((notice) => notice.key));
      this.notices = this.notices.filter((notice) => !extraKeys.has(notice.key));
      for (const key of extraKeys) {
        delete this.displayStartedAt[key];
        delete this.remainingMs[key];
        this._clearPauses(key);
        if (this.activeKey === key) {
          this.activeKey = null;
          clearTimer(this._hideTimer);
          this._hideTimer = 0;
        }
      }
      target.updatedAt = Date.now();
      if (gained) {
        target.text = target.members[target.members.length - 1]?.text ?? target.text;
      }
    },

    /**
     * 中央弹窗关闭：先按与首次显示同一套规则重新过滤（F04），再把弹窗期间的失败重新
     * 合并（F05），最后恢复被挂起的那条（按剩余时长继续）或显示新入队的。
     *
     * 「弹窗期间新入队的」在真正开始显示时起算 5s（由 flushQueue 内的显示逻辑保证）；
     * 被挂起的旧通知若合入了新的未显示失败，则给更新后的汇总一次完整展示机会。
     */
    resumeFromModal(): void {
      const hadSuspension = this.suspendedForModal || Boolean(this.suspendedActive);
      this.suspendedForModal = false;
      const suspended = this.suspendedActive;
      this.suspendedActive = null;
      // 恢复路径与首次显示走同一套过滤：已恢复/已处理/过期的成员不能再露出来
      this._pruneNotices();
      if (!hadSuspension) {
        this.flushQueue();
        return;
      }
      // 弹窗期间分时到达的失败：重新去重/过滤/合并后再显示
      this._coalesceTaskFailures();
      this._pruneNotices();
      if (suspended) {
        const notice = this.notices.find((item) => item.key === suspended.key) ?? null;
        if (notice && notice.members.length) {
          const previousKeys = Array.isArray(suspended.memberKeys) ? suspended.memberKeys : [];
          const gained = notice.members.some((member) => !previousKeys.includes(member.key));
          const remaining = gained
            ? NOTICE_VISIBLE_MS
            : Math.max(0, Math.min(NOTICE_VISIBLE_MS, suspended.remaining));
          this.activeKey = notice.key;
          this.displayStartedAt[notice.key] = Date.now() - (NOTICE_VISIBLE_MS - remaining);
          delete this.remainingMs[notice.key];
          this._clearPauses(notice.key);
          clearTimer(this._hideTimer);
          this._hideTimer = 0;
          this._armHideTimer(notice.key, Math.max(1, remaining));
          return;
        }
      }
      // 被挂起的那条已经不在队列里（或全部成员失效）：显示下一条
      this.flushQueue();
    },

    /** 关闭一条通知（只关这次显示，不影响业务；成员校验一并清理）。 */
    dismiss(key: string): void {
      const target = String(key ?? "").trim();
      if (!target) return;
      const notice = this.notices.find(
        (item) => item.key === target || item.members.some((member) => member.key === target),
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
      this._clearPauses(notice.key);
      delete this.displayStartedAt[notice.key];
      delete this.remainingMs[notice.key];
      if (this.suspendedActive?.key === notice.key) this.suspendedActive = null;
      // 任务失败事件收口后不再复活（引导提醒不在这里记，避免影响它自己的业务判断）
      if (notice.kind === "task_failed") this._rememberSettled(notice);
      if (wasActive) {
        this.activeKey = null;
        clearTimer(this._hideTimer);
        this._hideTimer = 0;
        this.flushQueue();
      }
    },

    /**
     * 暂停倒计时（悬停 / 键盘聚焦 / 旧调用不带原因，都按独立原因记录）。
     * 第一次暂停时记下剩余时长；重复原因幂等；全部原因解除后才继续（F06）。
     */
    pause(key: string, reason: string = "manual"): void {
      const target = String(key ?? "").trim();
      const why = String(reason ?? "").trim() || "manual";
      if (!target) return;
      if (this.activeKey !== target) return;
      const reasons = this._pausedReasonsFor(target);
      if (reasons.includes(why)) return;
      if (!reasons.length) {
        const started = this.displayStartedAt[target] ?? Date.now();
        this.remainingMs = {
          ...this.remainingMs,
          [target]: Math.max(0, NOTICE_VISIBLE_MS - (Date.now() - started)),
        };
        clearTimer(this._hideTimer);
        this._hideTimer = 0;
      }
      this.pauseReasons = { ...this.pauseReasons, [target]: [...reasons, why] };
    },

    /** 解除一个暂停原因；全部解除后才按剩余时长继续。 */
    resume(key: string, reason: string = "manual"): void {
      const target = String(key ?? "").trim();
      const why = String(reason ?? "").trim() || "manual";
      if (!target) return;
      const reasons = this._pausedReasonsFor(target);
      if (!reasons.includes(why)) return;
      const rest = reasons.filter((entry) => entry !== why);
      if (rest.length) {
        this.pauseReasons = { ...this.pauseReasons, [target]: rest };
        return;
      }
      this._clearPauses(target);
      this._resumeRemainingTimer(target);
    },

    /** 解除一条通知的全部暂停原因（组件卸载 / 强制收口用）；仍按剩余时长继续。 */
    releasePauses(key: string): void {
      const target = String(key ?? "").trim();
      if (!target || !this.pauseReasons[target]) return;
      this._clearPauses(target);
      if (typeof this.remainingMs[target] === "number") this._resumeRemainingTimer(target);
    },

    _resumeRemainingTimer(key: string): void {
      const remaining =
        typeof this.remainingMs[key] === "number" ? this.remainingMs[key] : NOTICE_VISIBLE_MS;
      delete this.remainingMs[key];
      if (this.activeKey !== key) return;
      const used = NOTICE_VISIBLE_MS - remaining;
      this.displayStartedAt[key] = Date.now() - used;
      clearTimer(this._hideTimer);
      this._hideTimer = 0;
      this._armHideTimer(key, Math.max(1, remaining));
    },

    /** 清空（重连基线 / 页面卸载 / 测试收口）。 */
    reset(): void {
      clearTimer(this._hideTimer);
      clearTimer(this._mergeTimer);
      this._hideTimer = 0;
      this._mergeTimer = 0;
      this.notices = [];
      this.activeKey = null;
      this.pauseReasons = {};
      this.displayStartedAt = {};
      this.remainingMs = {};
      this.mergeUntil = 0;
      this.suspendedForModal = false;
      this.suspendedActive = null;
      this.settledKeys = [];
      relevanceChecks.clear();
      activationHandlers.clear();
    },
  },
});
