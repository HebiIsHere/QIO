/**
 * 左侧任务栏的状态（A 维护）。
 *
 * 数据纪律（契约 §4.1 / §5）：
 * - 权威快照只有一处：GET /api/tasks（经 services/taskFeedApi.ts），当前/历史分开取；
 * - 增量事件只用于**触发刷新**，不在前端拼状态；重复/乱序/迟到回调按
 *   id + serverTime/serverTime + instanceId 丢弃，断线重连后整份重新对齐；
 * - 历史不落 localStorage，也不从布局偏好恢复业务状态；
 * - 失败状态如实保留，只有「知道了」才把失败项移入历史（并继续保留原因与部分结果）。
 *
 * 展开集合、滚动位置、详情缓存都只在内存里：它们属于界面状态，不写服务端偏好。
 */
import { defineStore } from "pinia";
import { watch, type WatchStopHandle } from "vue";
import type {
  TaskDetail,
  TaskFeedItem,
  TaskFeedPage,
  TaskCounts,
  TaskView,
} from "../conversation/contracts";
import { useApprovalsStore } from "./approvals";
import { useConversationShellStore } from "./conversationShell";
import { useEventStore } from "./events";
import { useSessionStore } from "./session";
import {
  acknowledgeTask,
  fetchTaskDetail,
  fetchTaskFeed,
  requestTaskCancel,
} from "../services/taskFeedApi";
import {
  ACTIVE_POLL_MS,
  COMPLETION_GRACE_MS,
  REFRESH_DEBOUNCE_MS,
  TASK_FEED_PAGE_SIZE,
  activeCountOf,
  attentionCountOf,
  buildContinueDevInstruction,
  dedupeTasks,
  isExpandedFlag,
  mergeRetainedTasks,
  recoveryCountOf,
  recoveryRecordToTaskItem,
  requestTaskPreview,
  resolveRemovedTask,
  sortTasks,
  taskDisplayStatus,
  type RemovalResolution,
} from "../services/conversationTasks";

/** 恢复事项的定位事件（B 的中央恢复区接收；Lead 集成时统一）。 */
const OPEN_RECOVERY_EVENT = "qio:open-recovery";

/** 会改变任务集合的真实事件：收到只做一次防抖刷新，不在前端猜状态。 */
const REFRESH_ON_EVENTS = new Set<string>([
  "APPROVAL_REQUIRED",
  "APPROVAL_RESULT",
  "SUBAGENT_STATUS",
  "TOOL_CREATE_STATUS",
  "TOOL_START",
  "TOOL_END",
  "TURN_START",
  "TURN_END",
  "RESYNC",
]);

interface TaskViewState {
  items: TaskFeedItem[];
  counts: TaskCounts | null;
  nextCursor: string | null;
  loaded: boolean;
  loading: boolean;
  loadingMore: boolean;
  error: string;
  moreError: string;
  serverTime: string;
  instanceId: string | null;
  loadedAt: number;
}

interface RetainedEntry {
  item: TaskFeedItem;
  verified: boolean;
  expiresAt: number;
}

function emptyView(): TaskViewState {
  return {
    items: [],
    counts: null,
    nextCursor: null,
    loaded: false,
    loading: false,
    loadingMore: false,
    error: "",
    moreError: "",
    serverTime: "",
    instanceId: null,
    loadedAt: 0,
  };
}

function errorText(error: unknown): string {
  if (!error) return "未知原因";
  const message = (error as Error).message;
  return message && message.trim() ? message : "未知原因";
}

function describeFeedError(error: unknown): string {
  const status = (error as { status?: number } | null)?.status;
  if (status === 404) return "任务列表暂不可用：后端还没有提供任务接口（可以稍后重试）。";
  if (status === 401 || status === 403) {
    return "任务列表暂不可用：本机 API 拒绝了这次请求（会话令牌可能失效）。";
  }
  return "任务列表暂时无法读取：" + errorText(error) + "（可以重试）";
}

function normalizeCounts(raw: unknown): TaskCounts | null {
  if (!raw || typeof raw !== "object") return null;
  const value = raw as Record<string, unknown>;
  const num = (key: string): number => {
    const n = Number(value[key]);
    return Number.isFinite(n) && n > 0 ? Math.floor(n) : 0;
  };
  return {
    queued: num("queued"),
    running: num("running"),
    waitingApproval: num("waitingApproval"),
    waitingUser: num("waitingUser"),
    completed: num("completed"),
    partial: num("partial"),
    failed: num("failed"),
    cancelled: num("cancelled"),
    budgetExhausted: num("budgetExhausted"),
    unknown: num("unknown"),
  };
}

/** 同一实例的旧快照整份丢弃：服务端时间更早 = 迟到响应，不得覆盖更新的状态。 */
function acceptsPage(previous: TaskViewState, page: TaskFeedPage): boolean {
  const nextTime = Date.parse(String(page?.serverTime ?? ""));
  const prevTime = Date.parse(previous.serverTime);
  if (!Number.isFinite(nextTime) || !Number.isFinite(prevTime)) return true;
  if (nextTime >= prevTime) return true;
  const nextInstance = page?.instanceId ?? null;
  const prevInstance = previous.instanceId ?? null;
  return nextInstance !== null && prevInstance !== null && nextInstance !== prevInstance;
}

function sameInstance(previous: TaskViewState, page: TaskFeedPage): boolean {
  const nextInstance = page?.instanceId ?? null;
  const prevInstance = previous.instanceId ?? null;
  return nextInstance === null || prevInstance === null || nextInstance === prevInstance;
}

export const useConversationTasksStore = defineStore("conversationTasks", {
  state: () => ({
    current: emptyView(),
    history: emptyView(),
    /** 展开集合按视图分开保留（切换当前/历史不互相清空）。 */
    expanded: { current: [] as string[], history: [] as string[] },
    /** 当前/历史各自的滚动位置（内存，不写服务端偏好）。 */
    scrollTop: { current: 0, history: 0 },
    /** 完成项短暂停留（当前视图），值为后端最终事实或最后一次已知状态。 */
    retained: [] as RetainedEntry[],
    /** 未拿到最终结果时的说明（不伪造结局）。 */
    retainedNotes: {} as Record<string, string>,
    details: {} as Record<string, TaskDetail>,
    detailLoading: [] as string[],
    detailErrors: {} as Record<string, string>,
    /** 一条动作请求在飞：itemId:kind */
    actionBusy: "",
    /** 按任务显示的最近一次动作结果/失败原因 */
    notices: {} as Record<string, string>,
    /** 看不清是哪一行发生的结果（例如放弃成功后该行已经移除） */
    feedback: "",
    focusTarget: null as { id: string; seq: number } | null,
    /** 一次定位的一组任务（全部目标；单目标时只有一项）。 */
    focusTargets: [] as string[],
    focusError: "",
    /** 应用级引用计数：GlobalFeedbackHost 挂载/卸载唯一的生命周期开关。 */
    _globalRefs: 0,
    /** 与事件流的一致性：unknown 初始 / online / offline（断线时才提示可能不是最新）。 */
    connection: "unknown" as "unknown" | "online" | "offline",
    _seq: { current: 0, history: 0 } as Record<TaskView, number>,
    _focusSeq: 0,
    _bound: false,
    _stops: [] as WatchStopHandle[],
    _refreshTimer: 0 as ReturnType<typeof setTimeout> | 0,
    _retainTimer: 0 as ReturnType<typeof setTimeout> | 0,
    _pollTimer: 0 as ReturnType<typeof setInterval> | 0,
    _feedbackTimer: 0 as ReturnType<typeof setTimeout> | 0,
  }),
  getters: {
    /** 应用级任务数据生命周期的引用计数（>0 = 事件订阅/轮询已建立）。 */
    globalRefCount(state): number {
      return state._globalRefs;
    },
    view(state) {
      return (view: TaskView): TaskViewState => (view === "history" ? state.history : state.current);
    },
    displayItems(state) {
      return (view: TaskView): TaskFeedItem[] => {
        if (view === "history") return sortTasks(dedupeTasks(state.history.items), { pinAttention: false });
        const merged = mergeRetainedTasks(
          state.current.items,
          state.retained.map((entry) => entry.item),
        );
        // 恢复事项来自现有收件箱（后端任务接口不含它们）；按 id 与任务行去重。
        const session = useSessionStore();
        const recovery = (session.recoveryRecords ?? []).map(recoveryRecordToTaskItem);
        return sortTasks(dedupeTasks([...merged, ...recovery]), { pinAttention: true });
      };
    },
    /** 任务行里需要用户处理的数量（服务端计数 + 恢复事项）。 */
    taskAttentionCount(state): number {
      const session = useSessionStore();
      return (
        attentionCountOf(state.current.counts, state.current.items) +
        recoveryCountOf(session.recoveryRecords)
      );
    },
    /** 待审批申请数量（收起入口的需要处理数量要把它们算进去）。 */
    approvalCount(): number {
      return useApprovalsStore().pendingCount;
    },
    /** 收起入口的数量：需要处理（任务 + 待审批）优先；没有才显示运行/排队。 */
    attentionCount(): number {
      const session = useSessionStore();
      return (
        attentionCountOf(this.current.counts, this.current.items) +
        recoveryCountOf(session.recoveryRecords) +
        useApprovalsStore().pendingCount
      );
    },
    activeCount(state): number {
      return activeCountOf(state.current.counts, state.current.items);
    },
    badgeCount(): number {
      const attention = this.attentionCount;
      if (attention > 0) return attention;
      return this.activeCount;
    },
    badgeTone(): "attention" | "active" {
      return this.attentionCount > 0 ? "attention" : "active";
    },
    isExpanded(state) {
      return (view: TaskView, id: string): boolean =>
        isExpandedFlag(state.expanded[view] ?? [], id);
    },
    detailFor(state) {
      return (id: string): TaskDetail | null => state.details[id] ?? null;
    },
    detailLoadingFor(state) {
      return (id: string): boolean => state.detailLoading.includes(id);
    },
    detailErrorFor(state) {
      return (id: string): string => state.detailErrors[id] ?? "";
    },
    noticeFor(state) {
      return (id: string): string => state.notices[id] ?? "";
    },
    retainedNoteFor(state) {
      return (id: string): string => state.retainedNotes[id] ?? "";
    },
    actionBusyFor(state) {
      return (id: string): boolean => state.actionBusy.indexOf(id + ":") === 0;
    },
    connectionHint(state): string {
      if (state.connection !== "offline") return "";
      if (!state.current.loaded && !state.history.loaded) return "";
      return "与后端的实时连接已断开：下面显示的是上次读取结果，状态可能已经变化。";
    },
  },
  actions: {
    // ── 取数 ────────────────────────────────────────────────────────────

    /** 首屏按当前 tab 幂等加载；已加载过返回就不重复请求（页面切换不清空）。 */
    async ensureLoaded(): Promise<void> {
      const shell = useConversationShellStore();
      if (!this.current.loaded && !this.current.loading) void this.refresh("current");
      if (shell.prefs.taskTab === "history" && !this.history.loaded && !this.history.loading) {
        void this.refresh("history");
      }
    },

    /** 切换当前/历史：恢复各自滚动位置由组件负责，这里只保证该视图有真实数据。 */
    async switchView(view: TaskView): Promise<void> {
      const shell = useConversationShellStore();
      shell.setTaskTab(view);
      const state = this[view];
      if (!state.loaded && !state.loading) {
        await this.refresh(view);
        return;
      }
      if (Date.now() - state.loadedAt > 5000 && !state.loading) {
        void this.refresh(view, { silent: true });
      }
    },

    async refresh(view: TaskView, opts: { silent?: boolean } = {}): Promise<void> {
      const state = this[view];
      const seq = ++this._seq[view];
      state.loading = true;
      if (!opts.silent) state.error = "";
      try {
        const page = await fetchTaskFeed({ view, limit: TASK_FEED_PAGE_SIZE });
        if (seq !== this._seq[view]) return;
        if (!page || !Array.isArray(page.items)) {
          throw new Error("后端返回的任务列表格式不正确");
        }
        if (!acceptsPage(state, page)) return;
        const instanceChanged =
          !sameInstance(state, page) && state.instanceId !== null && page.instanceId !== null;
        const previousItems = state.items.slice();
        state.items = dedupeTasks(page.items);
        state.counts = normalizeCounts(page.counts);
        state.nextCursor = typeof page.nextCursor === "string" ? page.nextCursor : null;
        state.serverTime = String(page.serverTime ?? "");
        state.instanceId = page.instanceId ?? null;
        state.loaded = true;
        state.error = "";
        state.moreError = "";
        state.loadedAt = Date.now();
        if (instanceChanged) this.resetTransient("后端实例已经更换");
        if (view === "current") await this.handleRemoved(previousItems);
      } catch (error) {
        if (seq !== this._seq[view]) return;
        state.error = describeFeedError(error);
      } finally {
        if (seq === this._seq[view]) state.loading = false;
      }
    },

    async loadMore(view: TaskView): Promise<void> {
      const state = this[view];
      const cursor = state.nextCursor;
      if (!cursor || state.loadingMore) return;
      state.loadingMore = true;
      state.moreError = "";
      try {
        const page = await fetchTaskFeed({ view, cursor, limit: TASK_FEED_PAGE_SIZE });
        if (!page || !Array.isArray(page.items)) throw new Error("后端返回的任务列表格式不正确");
        state.items = dedupeTasks([...state.items, ...page.items]);
        state.nextCursor = typeof page.nextCursor === "string" ? page.nextCursor : null;
        state.counts = normalizeCounts(page.counts) ?? state.counts;
      } catch (error) {
        state.moreError = "更早的任务没有加载出来：" + errorText(error) + "（可以重试）";
      } finally {
        state.loadingMore = false;
      }
    },

    /** 后端换了实例：旧实例的完成停留、展开、详情都不再可信，收口但保留用户视图。 */
    resetTransient(reason: string): void {
      this.retained = [];
      this.retainedNotes = {};
      this.details = {};
      this.detailErrors = {};
      this.detailLoading = [];
      this.notices = {};
      this.focusTarget = null;
      this.focusTargets = [];
      this.expanded.current = [];
      this.expanded.history = [];
      if (reason) this.setFeedback(reason + "：已按最新后端状态重新对齐。");
    },

    // ── 完成项短暂停留 ──────────────────────────────────────────────────

    async handleRemoved(previousItems: readonly TaskFeedItem[]): Promise<void> {
      const known = new Set(this.current.items.map((item) => item.id));
      const retainedIds = new Set(this.retained.map((entry) => entry.item.id));
      const removed = previousItems.filter((item) => !known.has(item.id) && !retainedIds.has(item.id));
      if (!removed.length) return;
      const candidates = removed.filter((item) => {
        const status = taskDisplayStatus(item);
        return (
          status === "running" ||
          status === "queued" ||
          status === "waiting_approval" ||
          status === "waiting_user"
        );
      });
      if (!candidates.length) {
        // 失败/已完成项从当前视图消失：后端已经把它归档，不需要伪造「完成」。
        return;
      }
      const historyItems = await this.historyForLookup();
      for (const item of candidates) {
        const expanded = this.isExpanded("current", item.id);
        const resolution: RemovalResolution = resolveRemovedTask(item, historyItems, expanded);
        if (resolution.action === "retain" && resolution.item) {
          this.retainItem(resolution.item, resolution.verified);
        }
      }
    },

    /** 只在历史视图尚未加载时补一份做对照；否则用已有历史（不动用户的分页）。 */
    async historyForLookup(): Promise<TaskFeedItem[] | null> {
      if (this.history.loaded) return this.history.items;
      try {
        const page = await fetchTaskFeed({ view: "history", limit: TASK_FEED_PAGE_SIZE });
        if (!page || !Array.isArray(page.items)) return null;
        const items = dedupeTasks(page.items);
        this.history.items = items;
        this.history.counts = normalizeCounts(page.counts);
        this.history.nextCursor = typeof page.nextCursor === "string" ? page.nextCursor : null;
        this.history.serverTime = String(page.serverTime ?? "");
        this.history.instanceId = page.instanceId ?? null;
        this.history.loaded = true;
        this.history.loadedAt = Date.now();
        return items;
      } catch {
        return null;
      }
    },

    retainItem(item: TaskFeedItem, verified: boolean): void {
      const entry: RetainedEntry = {
        item,
        verified,
        expiresAt: Date.now() + COMPLETION_GRACE_MS,
      };
      const index = this.retained.findIndex((existing) => existing.item.id === item.id);
      if (index >= 0) this.retained[index] = entry;
      else this.retained.push(entry);
      if (!verified) {
        this.retainedNotes = {
          ...this.retainedNotes,
          [item.id]: "已不在当前任务列表，最终结果未确认（历史记录里也没有找到结局）。",
        };
      }
      // 进入历史后仍然保持展开态，切换过去能直接定位到这条。
      if (
        verified &&
        this.isExpanded("current", item.id) &&
        this.history.items.some((row) => row.id === item.id) &&
        !this.isExpanded("history", item.id)
      ) {
        this.expanded.history.push(item.id);
      }
      this.scheduleRetainPrune();
    },

    scheduleRetainPrune(): void {
      if (this._retainTimer) clearTimeout(this._retainTimer);
      if (!this.retained.length) {
        this._retainTimer = 0;
        return;
      }
      this._retainTimer = setTimeout(() => {
        this._retainTimer = 0;
        this.pruneRetained();
      }, COMPLETION_GRACE_MS + 20);
    },

    /** 过期且没有被展开阅读的完成项离开当前视图（自动归档只改位置，不删结果）。 */
    pruneRetained(now: number = Date.now()): void {
      const keep: RetainedEntry[] = [];
      const expiredNotes: string[] = [];
      for (const entry of this.retained) {
        const expired = entry.expiresAt <= now;
        if (expired && !this.isExpanded("current", entry.item.id)) {
          expiredNotes.push(entry.item.id);
          continue;
        }
        keep.push(entry);
      }
      if (expiredNotes.length) {
        const notes = { ...this.retainedNotes };
        for (const id of expiredNotes) delete notes[id];
        this.retainedNotes = notes;
        this.retained = keep;
        this.scheduleRetainPrune();
      }
    },

    // ── 展开 / 滚动 / 详情 ─────────────────────────────────────────────

    expand(view: TaskView, id: string): void {
      if (!id) return;
      if (this.expanded[view].includes(id)) return;
      this.expanded[view].push(id);
    },

    collapse(view: TaskView, id: string): void {
      this.expanded[view] = this.expanded[view].filter((entry) => entry !== id);
      if (view !== "current") return;
      const before = this.retained.length;
      this.retained = this.retained.filter((entry) => entry.item.id !== id);
      if (this.retained.length !== before) {
        const notes = { ...this.retainedNotes };
        delete notes[id];
        this.retainedNotes = notes;
      }
    },

    toggleExpanded(view: TaskView, id: string): void {
      if (this.expanded[view].includes(id)) this.collapse(view, id);
      else this.expand(view, id);
    },

    setScroll(view: TaskView, top: number): void {
      this.scrollTop[view] = Number.isFinite(top) ? Math.max(0, Math.round(top)) : 0;
    },

    async loadDetail(id: string, opts: { force?: boolean } = {}): Promise<void> {
      if (!id) return;
      if (!opts.force && this.details[id]) return;
      if (this.detailLoading.includes(id)) return;
      this.detailLoading = [...this.detailLoading, id];
      const errors = { ...this.detailErrors };
      delete errors[id];
      this.detailErrors = errors;
      try {
        const detail = await fetchTaskDetail(id);
        if (!detail || !detail.item) throw new Error("后端返回的任务详情不完整");
        this.details = { ...this.details, [id]: detail };
      } catch (error) {
        this.detailErrors = {
          ...this.detailErrors,
          [id]: "完整内容读取失败：" + errorText(error) + "（可以重试）",
        };
      } finally {
        this.detailLoading = this.detailLoading.filter((entry) => entry !== id);
      }
    },

    // ── 动作 ────────────────────────────────────────────────────────────

    setNotice(id: string, text: string): void {
      const next = { ...this.notices };
      if (text) next[id] = text;
      else delete next[id];
      this.notices = next;
    },

    setFeedback(text: string): void {
      this.feedback = text;
      if (this._feedbackTimer) clearTimeout(this._feedbackTimer);
      this._feedbackTimer = 0;
      if (!text) return;
      this._feedbackTimer = setTimeout(() => {
        this._feedbackTimer = 0;
        this.feedback = "";
      }, 6000);
    },

    /** 等待审批的定位入口：只打开统一审批弹窗，左侧不出现批准/拒绝按钮。 */
    openApprovalFor(item?: TaskFeedItem): void {
      const approvals = useApprovalsStore();
      if (!approvals.pendingCount) {
        if (item) this.setNotice(item.id, "现在没有待处理的审批申请（可能已经处理，或正在统一审批弹窗里）。");
        return;
      }
      approvals.openNow();
      if (item) this.setNotice(item.id, "");
    },

    /** 只有后端 supported 且本端已接线的动作才会走到这里（界面同样先过滤）。 */
    async runAction(item: TaskFeedItem, kind: string): Promise<void> {
      if (!item || !kind) return;
      if (this.actionBusy) return;
      const action = (item.actions ?? []).find(
        (entry) => entry.kind === kind && entry.supported === true,
      );
      if (!action && kind !== "read_more") return;
      const busyKey = item.id + ":" + kind;
      this.actionBusy = busyKey;
      this.setNotice(item.id, "");
      try {
        await this.dispatchAction(item, kind, action?.target);
      } catch (error) {
        this.setNotice(item.id, "操作没有完成：" + errorText(error) + "（可以重试）");
      } finally {
        this.actionBusy = "";
      }
    },

    async dispatchAction(item: TaskFeedItem, kind: string, target?: string): Promise<void> {
      if (kind === "read_more") {
        await this.loadDetail(item.id, { force: true });
        return;
      }
      if (kind === "open_preview") {
        // C 的预览入口：尚未合并时由 window 事件接手（Lead 集成时统一成 open(target)）。
        requestTaskPreview(item, target);
        return;
      }
      if (kind === "open_approval") {
        this.openApprovalFor(item);
        return;
      }
      if (kind === "open_recovery") {
        if (typeof window !== "undefined") {
          window.dispatchEvent(
            new CustomEvent(OPEN_RECOVERY_EVENT, {
              detail: { taskId: item.id, recordId: target ?? "" },
            }),
          );
        }
        this.setNotice(item.id, "恢复事项在中央对话区的恢复提示里处理；这里保留它的记录。");
        return;
      }
      if (kind === "ack") {
        await acknowledgeTask(item.id);
        this.removeItem(item.id);
        this.setFeedback("已标记为知道了；这条记录移入历史记录，失败原因与部分结果继续保留。");
        await this.refresh("current", { silent: true });
        await this.refresh("history", { silent: true });
        return;
      }
      if (kind === "cancel") {
        const result = await requestTaskCancel(item.id);
        this.setNotice(
          item.id,
          result.accepted
            ? "取消请求已提交：" + result.detail + "。底层执行单元可能仍在停止，稍后刷新查看真实状态。"
            : "后端没有接受这次取消：" + result.detail,
        );
        await this.refresh("current", { silent: true });
        return;
      }
      if (kind === "continue_dev") {
        const session = useSessionStore();
        const ok = await session.send(buildContinueDevInstruction(item));
        if (ok) {
          this.setFeedback("已把这项开发交给模型继续，新的一轮会出现在中央对话区。");
        } else {
          this.setNotice(
            item.id,
            "没能把这项开发交给模型" +
              (session.lastError ? "：" + session.lastError : "") +
              "。可以再点一次。",
          );
        }
        return;
      }
      if (kind === "revoke_auth") {
        const session = useSessionStore();
        const taskId = target || item.sourceId;
        const ok = await session.revokeDevAuthorization(taskId);
        this.setNotice(
          item.id,
          ok
            ? "执行授权已经收回；这项任务本身没有结束，下次测试会重新征求确认。"
            : "没能撤销这次授权（后端没有确认），它现在可能仍然有效。可以再试一次。",
        );
        return;
      }
      if (kind === "abandon_dev") {
        const session = useSessionStore();
        const taskId = target || item.sourceId;
        const result = await session.abandonDevTask(taskId);
        if (!result.ok) {
          this.setNotice(
            item.id,
            "这项开发没有被放弃：" + result.message + "。它仍然在列表里，可以再试一次。",
          );
          return;
        }
        this.removeItem(item.id);
        this.setFeedback("已放弃这项开发（终态，不是完成）；工作区文件与记录保留。");
        await this.refresh("current", { silent: true });
        await this.refresh("history", { silent: true });
        return;
      }
    },

    removeItem(id: string): void {
      this.current.items = this.current.items.filter((item) => item.id !== id);
      this.retained = this.retained.filter((entry) => entry.item.id !== id);
      this.expanded.current = this.expanded.current.filter((entry) => entry !== id);
      const notes = { ...this.retainedNotes };
      delete notes[id];
      this.retainedNotes = notes;
      this.setNotice(id, "");
    },

    // ── 定位（中央执行记录 / 通知 → 左侧任务） ────────────────────────

    /**
     * 展开并定位**一组**任务（通知合并了多个失败任务时用；单个任务也走这里）。
     *
     * - 去空/去重/保序，不截断任何目标；
     * - 当前/历史两个视图里命中的目标都展开（切换视图后仍是展开态）；
     * - 可见列表优先当前视图（失败通知基本都在当前），否则历史；
     * - 失败原因写进 focusError（可直接展示），绝不静默成功。
     */
    async focusTasks(taskIds: string[]): Promise<void> {
      const requested: string[] = [];
      for (const raw of Array.isArray(taskIds) ? taskIds : []) {
        const id = String(raw ?? "").trim();
        if (id && !requested.includes(id)) requested.push(id);
      }
      if (!requested.length) return;
      // 恢复事项的 id 是 "recovery:<record_id>"：中央按 record_id 发过来时也要能定位。
      const candidates = (id: string): string[] =>
        id.indexOf(":") >= 0 ? [id] : [id, "recovery:" + id];
      const resolveIn = (items: readonly TaskFeedItem[], out: string[]): void => {
        for (const id of requested) {
          const hit = items.find((item) => candidates(id).includes(item.id));
          if (hit && !out.includes(hit.id)) out.push(hit.id);
        }
      };
      const collect = (): { current: string[]; history: string[] } => {
        const current: string[] = [];
        const history: string[] = [];
        resolveIn(this.displayItems("current"), current);
        resolveIn(this.history.items, history);
        return { current, history };
      };

      this.focusError = "";
      let hits = collect();
      const foundCount = (target: { current: string[]; history: string[] }): number =>
        requested.filter((id) =>
          [...target.current, ...target.history].some((actual) => candidates(id).includes(actual)),
        ).length;
      // 目标可能来自陈旧快照（全部或部分没有）：刷新一次权威快照再找。
      // 历史只在尚未加载时补一份做对照，不动用户已加载的分页。
      if (foundCount(hits) < requested.length) {
        await Promise.all([
          this.refresh("current", { silent: true }),
          this.history.loaded ? Promise.resolve() : this.refresh("history", { silent: true }),
        ]);
        hits = collect();
      }

      if (!hits.current.length && !hits.history.length) {
        this.focusTarget = null;
        this.focusTargets = [];
        this.focusError =
          !this.current.loaded && !this.history.loaded
            ? "任务列表暂不可用：现在无法定位这 " + requested.length + " 项任务。"
            : "没有找到这 " + requested.length + " 项任务：它们可能已经被清理，或不在当前后端实例的记录里。仍可在左侧任务列表里逐条查看。";
        return;
      }

      // 全部目标都展开：当前/历史分别记账，切换视图后不会丢失展开态。
      for (const id of hits.current) this.expand("current", id);
      for (const id of hits.history) this.expand("history", id);

      const primaryView: TaskView = hits.current.length ? "current" : "history";
      useConversationShellStore().setTaskTab(primaryView);
      const visibleHits = primaryView === "current" ? hits.current : hits.history;
      const visibleOrdered: string[] = [];
      for (const id of requested) {
        const actual = visibleHits.find((entry) => candidates(id).includes(entry));
        if (actual && !visibleOrdered.includes(actual)) visibleOrdered.push(actual);
      }
      this.focusTargets = visibleOrdered;
      this.focusTarget = { id: visibleOrdered[0], seq: ++this._focusSeq };

      const matchedRequested = foundCount(hits);
      const missing = requested.length - matchedRequested;
      if (missing > 0) {
        this.focusError =
          "有 " + missing + " 项任务没有找到（可能已经被清理，或不在当前后端实例的记录里）；其余 " + visibleOrdered.length + " 项已经在列表里展开定位。仍可在左侧任务列表里逐条查看。";
      }
    },

    /** 兼容：单目标定位等价 focusTasks([id])。 */
    async focusTask(taskId: string): Promise<void> {
      await this.focusTasks([typeof taskId === "string" ? taskId : String(taskId ?? "")]);
    },

    /** 供 B / Lead 在中央执行记录里直接调用的别名。 */
    async focus(taskId: string): Promise<void> {
      await this.focusTask(taskId);
    },

    // ── 应用级生命周期（GlobalFeedbackHost 挂载/卸载） ──────────────────

    /**
     * 应用级挂载：第一次 acquire 才建立事件订阅 + 轮询 + 首份权威快照；
     * 重复 acquire 只加计数，不重复订阅/轮询（页面来回切换不累加）。
     */
    acquireGlobal(): void {
      const refs = Math.max(0, Number(this._globalRefs) || 0);
      this._globalRefs = refs + 1;
      if (this._globalRefs > 1) return;
      this.bindEvents();
      this.startPolling();
      // 直接进入设置/互动/调试页也要有任务基线：首份快照由应用级挂载建立。
      void this.ensureLoaded();
    },

    /**
     * 应用级卸载：最后一个 release 才真正 dispose（取消订阅/轮询、清理计时器）。
     * 多余的 release（计数已经是 0）是安全 no-op，不会误停其它持有者。
     */
    releaseGlobal(): void {
      const refs = Math.max(0, Number(this._globalRefs) || 0);
      if (refs <= 0) return;
      this._globalRefs = refs - 1;
      if (this._globalRefs > 0) return;
      this.dispose();
    },

    // ── 事件与轮询 ──────────────────────────────────────────────────────

    bindEvents(): void {
      if (this._bound) return;
      this._bound = true;
      const events = useEventStore();
      this.connection = events.connected ? "online" : "unknown";
      const stops: WatchStopHandle[] = [];
      stops.push(
        watch(
          () => events.connected,
          (connected, previous) => {
            this.connection = connected ? "online" : previous ? "offline" : "unknown";
            if (connected && previous === false) {
              // 断线重连 / 后端重启：旧快照的时间戳作废，整份重新对齐。
              this.current.serverTime = "";
              this.history.serverTime = "";
              void this.refresh("current", { silent: true });
              if (this.history.loaded) void this.refresh("history", { silent: true });
            }
          },
        ),
      );
      // 事件数组有上限（到顶会 shift 最旧的一条，长度不再变化）：盯最后一条的 id。
      // 某些测试/降级环境注入的是精简 events store（没有 events 数组）：此时跳过这条订阅，
      // 不因为「读不到事件列表」让整个页面绑定失败。
      const eventList = () => {
        const raw = (events as { events?: unknown }).events;
        return Array.isArray(raw) ? raw : [];
      };
      const lastEventId = () => {
        const list = eventList();
        return list.length ? list[list.length - 1]?.id ?? "" : "";
      };
      stops.push(
        watch(
          lastEventId,
          () => {
            const list = eventList();
            const last = list[list.length - 1];
            if (!last || !REFRESH_ON_EVENTS.has(last.type)) return;
            this.scheduleRefresh();
          },
        ),
      );
      this._stops = stops;
      // 页面切换期间完成停留的定时器被收口过：回来时补一次，避免旧完成项一直挂在当前视图。
      if (this.retained.length) this.scheduleRetainPrune();
    },

    scheduleRefresh(): void {
      if (this._refreshTimer) clearTimeout(this._refreshTimer);
      this._refreshTimer = setTimeout(() => {
        this._refreshTimer = 0;
        void this.refresh("current", { silent: true });
        const shell = useConversationShellStore();
        if (shell.prefs.taskTab === "history" && this.history.loaded) {
          void this.refresh("history", { silent: true });
        }
      }, REFRESH_DEBOUNCE_MS);
    },

    startPolling(): void {
      if (this._pollTimer) return;
      if (typeof window === "undefined") return;
      this._pollTimer = setInterval(() => {
        this.pollTick();
      }, ACTIVE_POLL_MS);
    },

    stopPolling(): void {
      if (this._pollTimer) clearInterval(this._pollTimer);
      this._pollTimer = 0;
    },

    /** 只在「有真实活动/待处理」且页面可见时兜底刷新（避免空列表也一直打后端）。 */
    pollTick(): void {
      if (typeof document !== "undefined" && document.hidden) return;
      if (!this.current.loaded || this.current.loading) return;
      const attention = this.attentionCount;
      const active = this.activeCount;
      if (attention <= 0 && active <= 0 && this.retained.length === 0) return;
      void this.refresh("current", { silent: true });
      const shell = useConversationShellStore();
      if (shell.prefs.taskTab === "history" && this.history.loaded) {
        void this.refresh("history", { silent: true });
      }
    },

    unbindEvents(): void {
      for (const stop of this._stops) stop();
      this._stops = [];
      this._bound = false;
      if (this._refreshTimer) clearTimeout(this._refreshTimer);
      this._refreshTimer = 0;
    },

    /** 应用级收口：取消订阅/轮询与所有计时器（store 可复用，下次 acquire 会重新 bind）。 */
    dispose(): void {
      // 外部直接 dispose（例如测试收口）也应让引用计数归零，避免之后 acquire 不复绑。
      this._globalRefs = 0;
      this.unbindEvents();
      this.stopPolling();
      if (this._retainTimer) clearTimeout(this._retainTimer);
      this._retainTimer = 0;
      if (this._feedbackTimer) clearTimeout(this._feedbackTimer);
      this._feedbackTimer = 0;
    },
  },
});
