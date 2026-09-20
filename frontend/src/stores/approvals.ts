import { defineStore } from "pinia";
import { api } from "../services/api";

export interface ApprovalItem {
  approval_id: string;
  kind: string;
  payload: Record<string, unknown>;
  /**
   * 后端已经没有这条审批（别处已应答 / 已过期）。
   * 这时批准与拒绝都会 404，继续保留成一个「可重试」的待办只会把用户卡死 ——
   * 所以标记为失效：界面明确说清楚，并给一个「知道了」把它清掉。
   */
  stale?: boolean;
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
  }),
  getters: {
    current: (state) => state.queue[0] ?? null,
    /** 队列里有待办、且用户没有选择稍后 → 窗口应当显示 */
    visible: (state) => state.queue.length > 0 && !state.deferred,
    /** 待确认数量（入口文案用） */
    pendingCount: (state) => state.queue.length,
  },
  actions: {
    enqueue(
      approvalId: string,
      kind: string,
      payload: Record<string, unknown>,
      opts: { autoOpen?: boolean } = {},
    ) {
      if (this.queue.some((a) => a.approval_id === approvalId)) return;
      const first = this.queue.length === 0;
      this.queue.push({ approval_id: approvalId, kind, payload });
      // 编辑中到达的确认不抢焦点：保留待办并亮出可发现的入口，由用户主动打开。
      if (first) this.deferred = opts.autoOpen === false;
      else if (opts.autoOpen !== false) this.deferred = false;
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
    async respond(decision: "approved" | "rejected", overrides?: Record<string, unknown>) {
      const item = this.current;
      // 双提交防护：请求进行中忽略后续点击（按钮同时 disabled）
      if (!item || this.responding) return;
      // 已经判定失效的项不再发请求：后端已经没有它了，重试只会一直 404
      if (item.stale) return;
      this.responding = item.approval_id;
      this.error = null;
      try {
        await api.respondApproval(item.approval_id, decision, overrides);
        // 成功才出队（按 id 过滤，避免并发事件让 shift 移除错项）
        this.queue = this.queue.filter((a) => a.approval_id !== item.approval_id);
        if (!this.queue.length) this.deferred = false;
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
