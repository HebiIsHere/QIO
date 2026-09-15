import { defineStore } from "pinia";
import { api } from "../services/api";

export interface ApprovalItem {
  approval_id: string;
  kind: string;
  payload: Record<string, unknown>;
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
    async respond(decision: "approved" | "rejected", overrides?: Record<string, unknown>) {
      const item = this.current;
      // 双提交防护：请求进行中忽略后续点击（按钮同时 disabled）
      if (!item || this.responding) return;
      this.responding = item.approval_id;
      this.error = null;
      try {
        await api.respondApproval(item.approval_id, decision, overrides);
        // 成功才出队（按 id 过滤，避免并发事件让 shift 移除错项）
        this.queue = this.queue.filter((a) => a.approval_id !== item.approval_id);
        if (!this.queue.length) this.deferred = false;
      } catch (e) {
        // 失败保留审批项：UI 上「看起来批准了、后端没批准」是绝不允许的状态
        // 文案必须先说结论：失败 ≠ 已授权，再给原因与退路。
        this.error = `审批请求失败，未做出任何授权：${(e as Error).message}（可重试）`;
      } finally {
        this.responding = null;
      }
    },
  },
});
