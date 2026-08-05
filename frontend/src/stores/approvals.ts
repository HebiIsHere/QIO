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
  }),
  getters: {
    current: (state) => state.queue[0] ?? null,
  },
  actions: {
    enqueue(approvalId: string, kind: string, payload: Record<string, unknown>) {
      if (this.queue.some((a) => a.approval_id === approvalId)) return;
      this.queue.push({ approval_id: approvalId, kind, payload });
    },
    async respond(decision: "approved" | "rejected") {
      const item = this.current;
      if (!item) return;
      this.responding = item.approval_id;
      try {
        await api.respondApproval(item.approval_id, decision);
      } finally {
        this.responding = null;
        this.queue.shift();
      }
    },
  },
});