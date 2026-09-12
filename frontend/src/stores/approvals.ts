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
  }),
  getters: {
    current: (state) => state.queue[0] ?? null,
  },
  actions: {
    enqueue(approvalId: string, kind: string, payload: Record<string, unknown>) {
      if (this.queue.some((a) => a.approval_id === approvalId)) return;
      this.queue.push({ approval_id: approvalId, kind, payload });
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
      } catch (e) {
        // 失败保留审批项：UI 上「看起来批准了、后端没批准」是绝不允许的状态
        this.error = `审批请求失败：${(e as Error).message}（未做出任何授权，可重试）`;
      } finally {
        this.responding = null;
      }
    },
  },
});
