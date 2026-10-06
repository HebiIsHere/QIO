import { defineStore } from "pinia";
import { api } from "../services/api";

export interface ApprovalItem {
  approval_id: string;
  kind: string;
  payload: Record<string, unknown>;
  /**
   * 这次审批原本属于哪一轮 / 哪个会话 / 哪个请求摘要。
   * 应答时原样回传，后端据此确认「批准的就是这一次请求」——
   * 用户操作方式不变，只有审批与请求不匹配时才会被拒。
   */
  turnId?: string | null;
  sessionId?: string | null;
  requestDigest?: string | null;
  /**
   * 后端已经没有这条审批（别处已应答 / 已过期）。
   * 这时批准与拒绝都会 404，继续保留成一个「可重试」的待办只会把用户卡死 ——
   * 所以标记为失效：界面明确说清楚，并给一个「知道了」把它清掉。
   */
  stale?: boolean;
}

/**
 * 「它想做什么」：审批里最该先读的一句人话。
 *
 * 与 ApprovalModal 的 intent 共用这一份取值顺序
 * （description → tool_name → name → explanation → reason）——
 * 内联卡与弹窗必须说同一句话，不能各写一套。
 */
export function approvalIntent(payload: Record<string, unknown>): string {
  const first = [payload.description, payload.tool_name, payload.name, payload.explanation, payload.reason]
    .map((v) => (typeof v === "string" ? v.trim() : ""))
    .find((v) => v.length > 0);
  return first ?? "该操作需要你的授权";
}

/** 后端 policy.describe() 的能力清单（人话）；没有就不显示，不编造。 */
export function approvalCapabilities(payload: Record<string, unknown>): string[] {
  const raw = payload.capabilities;
  return Array.isArray(raw) ? raw.map((x) => String(x)) : [];
}

export const useApprovalsStore = defineStore("approvals", {
  state: () => ({
    queue: [] as ApprovalItem[],
    responding: null as string | null,
    /** 最近一次 respond 失败原因（成功后清空）；用于「失败 ≠ 已授权」的可见反馈 */
    error: null as string | null,
    /**
     * 用户已经「稍后处理」：窗口收起但待审批项保留。
     * 直接把窗口藏起来而不做这个标记，会留下一个用户找不到的待审批项；反之
     * 无条件弹出，会在用户正编辑表单时抢走焦点（历史录像：工具确认盖住凭据编辑）。
     */
    deferred: false,
    /**
     * 过程区里已经**内联显示按钮**的那条审批 id。
     *
     * 同一时刻只允许一套按钮：内联卡在显示时，全局 ApprovalEntry / ApprovalModal
     * 不再对同一个 approval_id 显示按钮（按 id 门控，不是按「有没有审批」）。
     */
    inlineId: null as string | null,
  }),
  getters: {
    current: (state) => state.queue[0] ?? null,
    /** 队列里有待办、且用户没有选择稍后 → 窗口应当显示 */
    visible: (state) => state.queue.length > 0 && !state.deferred,
    /** 待确认数量（入口文案用） */
    pendingCount: (state) => state.queue.length,
    /** 内联卡正在显示的就是当前这条审批 */
    inlineClaimed: (state) =>
      !!state.inlineId && state.queue.length > 0 && state.queue[0]?.approval_id === state.inlineId,
  },
  actions: {
    enqueue(
      approvalId: string,
      kind: string,
      payload: Record<string, unknown>,
      opts: {
        autoOpen?: boolean;
        turnId?: string | null;
        sessionId?: string | null;
        requestDigest?: string | null;
      } = {},
    ) {
      if (this.queue.some((a) => a.approval_id === approvalId)) return;
      const first = this.queue.length === 0;
      this.queue.push({
        approval_id: approvalId,
        kind,
        payload,
        turnId: opts.turnId ?? null,
        sessionId: opts.sessionId ?? null,
        requestDigest: opts.requestDigest ?? null,
      });
      // 编辑中到达的确认不抢焦点：保留待办并亮出可发现的入口，由用户主动打开。
      if (first) this.deferred = opts.autoOpen === false;
      else if (opts.autoOpen !== false) this.deferred = false;
    },
    /** 过程区内联卡声明「这条审批的按钮由我显示」（按 approval_id） */
    claimInline(approvalId: string) {
      if (approvalId) this.inlineId = approvalId;
    },
    /** 释放内联声明（内联卡卸载 / 轮次结束）：全局入口恢复显示 */
    releaseInline(approvalId?: string | null) {
      if (!approvalId || this.inlineId === approvalId) this.inlineId = null;
    },
    /** 用户主动打开（入口点击 / 直接相关的确认） */
    openNow() {
      if (this.queue.length) this.deferred = false;
    },
    /** 稍后处理：只收起窗口，待审批项与失败提示都保留 */
    defer() {
      if (this.queue.length) this.deferred = true;
    },
    /**
     * 后端报告某项审批已经有结局（APPROVAL_RESULT）。
     *
     * 本地通常已经处理过（用户点了批准/拒绝），但同一审批也可能由别处应答、
     * 或本地状态与后端不一致。按 id 收敛是幂等的：不在队列里就什么都不做；
     * 这样不会留下「看不见的待审批项」，也不会把已经处理过的项重复移除。
     */
    resolve(approvalId: string) {
      if (!approvalId) return;
      const before = this.queue.length;
      this.queue = this.queue.filter((a) => a.approval_id !== approvalId);
      if (this.queue.length !== before) this.error = null;
      if (!this.queue.length) this.deferred = false;
    },
    /**
     * 用服务端的权威快照**替换**待办集合。
     *
     * 服务器没有列出来的审批 = 已经不再 pending（被批准/拒绝/过期/取消），
     * 本地必须移除，否则会出现「后端早已有结局，界面还留着 Allow / Reject」。
     * 只做移除、不在这里添加：添加要按 kind 决定走哪条 UI（见 events store 的统一入口）。
     */
    reconcile(keepIds: string[]) {
      const keep = new Set(keepIds);
      const before = this.queue.length;
      this.queue = this.queue.filter((a) => keep.has(a.approval_id));
      if (this.queue.length !== before) this.error = null;
      if (!this.queue.length) this.deferred = false;
    },
    async respond(decision: "approved" | "rejected", overrides?: Record<string, unknown>) {
      const item = this.current;
      if (!item) return;
      await this._respondItem(item, decision, overrides);
    },
    /**
     * 按 approval_id 应答（过程区内联卡用）。
     *
     * 为什么不能直接用 respond()：内联卡上的按钮属于**那一条**审批；
     * 请求在飞或审批已被别处处理时 current 可能已经换成下一条 ——
     * 按 id 定位，绝不误伤下一项；重复点击也只会命中同一条，被 responding 挡住。
     */
    async respondById(
      approvalId: string,
      decision: "approved" | "rejected",
      overrides?: Record<string, unknown>,
    ) {
      const item = this.queue.find((a) => a.approval_id === approvalId);
      if (!item) return;
      await this._respondItem(item, decision, overrides);
    },
    /** 一条审批的应答（respond / respondById 共用，语义完全一致） */
    async _respondItem(
      item: ApprovalItem,
      decision: "approved" | "rejected",
      overrides?: Record<string, unknown>,
    ) {
      // 双提交防护：请求进行中忽略后续点击（按钮同时 disabled）
      if (this.responding) return;
      // 已经判定失效的项不再发请求：后端已经没有它了，重试只会一直 404
      if (item.stale) return;
      this.responding = item.approval_id;
      this.error = null;
      try {
        await api.respondApproval(item.approval_id, decision, overrides, {
          turnId: item.turnId,
          sessionId: item.sessionId,
          requestDigest: item.requestDigest,
        });
        // 成功才出队（按 id 过滤，避免并发事件让 shift 移除错项）
        this.queue = this.queue.filter((a) => a.approval_id !== item.approval_id);
        if (!this.queue.length) this.deferred = false;
        // 这条已经没有待办了：内联声明一起释放，避免门控残留
        if (this.inlineId === item.approval_id) this.inlineId = null;
      } catch (e) {
        const status = (e as { status?: number }).status;
        if (status === 404) {
          // 「没有这条审批 / 已经有结局」：它已经被处理过。不静默移除（用户得知道发生了什么），
          // 但也不留成一个永远点不掉的待办 —— 标记失效，界面只给一个「知道了」。
          item.stale = true;
          this.error = `这项确认已经有结局（可能已在别处处理或已过期），不会再等待你的授权。未做出任何授权：${(e as Error).message}`;
          return;
        }
        // 失败保留审批项：UI 上「看起来批准了、后端没批准」是绝不允许的状态
        // 文案必须先说结论：失败 ≠ 已授权，再给原因与退路。
        this.error = `审批请求失败，未做出任何授权：${(e as Error).message}（可重试）`;
      } finally {
        this.responding = null;
      }
    },
  },
});
