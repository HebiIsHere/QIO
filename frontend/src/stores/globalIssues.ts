/**
 * 全局异常（「独立问题」）集合（B · 公共接口，契约 §3.4）。
 *
 * 设计口径（对应用户任务书「整体异常只进顶部全局栏一个入口」）：
 * - 顶部全局栏只显示**一个入口**与独立问题的数量，不把错误散落到各个页面；
 * - 同一根因（id）只计一次：重复上报只更新原因/诊断，不重复计数；
 * - 部分恢复只移除对应那一项；全部恢复后入口整体消失（面板随之收起）；
 * - 每项都带真实处理动作（指向设置页真实分区 / 真实重试入口），不造假按钮；
 * - 只收「持续/整体」的异常：单条回复未完成、用户主动停止、发送结果未知都不进这里。
 */
import { defineStore } from "pinia";

export type GlobalIssueKind =
  | "sse_disconnected"
  | "model_unavailable"
  | "model_capability_fallback"
  | "no_credentials"
  | "credential_paused"
  | "credential_revoked"
  | "restore_failed"
  | "credential_state"
  | "other";

/** 处理动作必须指向真实位置：设置页真实分区 id / 真实路由名 / 真实重试入口。 */
export interface GlobalIssueAction {
  label: string;
  kind: "settings" | "route" | "retry-connection" | "retry-restore";
  /** 设置页真实分区 id（appearance/chat/model/tools/data/cred/advanced/about）。 */
  section?: string;
  /** 真实路由名（settings / debug）。 */
  route?: string;
}

export interface GlobalIssue {
  /** 根因身份：同 id 只计一次（去重键）。 */
  id: string;
  kind: GlobalIssueKind;
  /** 问题（一句话，结论先行）。 */
  title: string;
  /** 简短原因（后端原文优先，不编造）。 */
  reason: string;
  actions: GlobalIssueAction[];
  /** 按需展开的诊断（技术细节；默认折叠，不显示密钥原文）。 */
  diagnostics: string[];
  severity: "error" | "warning";
  /** 最近一次上报时间（毫秒）。调用方可以不传：upsert 内部补 Date.now()（契约 §3.4 未列该字段）。 */
  at?: number;
}

function normalizeIssue(issue: GlobalIssue): GlobalIssue {
  return {
    id: String(issue.id ?? "").trim(),
    kind: issue.kind ?? "other",
    title: String(issue.title ?? "").trim() || "有一项需要处理的问题",
    reason: String(issue.reason ?? "").trim(),
    actions: Array.isArray(issue.actions)
      ? issue.actions.filter((action) => action && action.label && action.kind)
      : [],
    diagnostics: Array.isArray(issue.diagnostics)
      ? issue.diagnostics.map((line) => String(line ?? "")).filter((line) => line.trim())
      : [],
    severity: issue.severity === "warning" ? "warning" : "error",
    at: Number.isFinite(Number(issue.at)) ? Number(issue.at) : Date.now(),
  };
}

export const useGlobalIssuesStore = defineStore("globalIssues", {
  state: () => ({
    issues: [] as GlobalIssue[],
    /** 面板是否展开（入口与面板是两个组件，共享同一份状态）。 */
    panelOpen: false,
  }),
  getters: {
    /** 独立问题数量（同根因已去重）。 */
    count(state): number {
      return state.issues.length;
    },
    hasIssues(state): boolean {
      return state.issues.length > 0;
    },
    /** 需要立即处理的数量（错误级；warning 也计入入口数量，但语气不同）。 */
    errorCount(state): number {
      return state.issues.filter((issue) => issue.severity === "error").length;
    },
    /** 入口的读屏文案（数量 + 是否包含错误）。 */
    summary(state): string {
      const total = state.issues.length;
      if (!total) return "";
      const errors = state.issues.filter((issue) => issue.severity === "error").length;
      if (errors > 0) return `${total} 个独立问题（${errors} 个需要处理）`;
      return `${total} 个独立问题`;
    },
    issueById(state) {
      return (id: string): GlobalIssue | null =>
        state.issues.find((issue) => issue.id === id) ?? null;
    },
  },
  actions: {
    /**
     * 上报一个问题（按 id 去重）。
     *
     * 重复上报只更新原因/诊断/动作（不新增条目、不重复计数）；恢复由 resolve 单独表达。
     */
    upsert(input: GlobalIssue): void {
      const issue = normalizeIssue(input);
      if (!issue.id) return;
      const index = this.issues.findIndex((existing) => existing.id === issue.id);
      if (index >= 0) {
        const existing = this.issues[index];
        this.issues[index] = {
          ...existing,
          ...issue,
          at: Date.now(),
        };
        return;
      }
      this.issues.push({ ...issue, at: Date.now() });
    },

    /** 某一项已经恢复：只移除这一项。 */
    resolve(id: string): void {
      const target = String(id ?? "").trim();
      if (!target) return;
      const before = this.issues.length;
      this.issues = this.issues.filter((issue) => issue.id !== target);
      if (this.issues.length !== before && !this.issues.length) this.panelOpen = false;
    },

    /** 一类问题全部恢复（例如凭据恢复正常后清掉所有凭据类问题）。 */
    resolveKind(kind: GlobalIssueKind): void {
      const before = this.issues.length;
      this.issues = this.issues.filter((issue) => issue.kind !== kind);
      if (this.issues.length !== before && !this.issues.length) this.panelOpen = false;
    },

    /** 保留清单之外的全部清除（例如一次来源核对后只留下真实存在的问题）。 */
    retain(ids: readonly string[]): void {
      const keep = new Set(ids);
      const next = this.issues.filter((issue) => keep.has(issue.id));
      if (next.length !== this.issues.length) {
        this.issues = next;
        if (!this.issues.length) this.panelOpen = false;
      }
    },

    /** 全部恢复：入口整体消失。 */
    clearAll(): void {
      this.issues = [];
      this.panelOpen = false;
    },

    openPanel(): void {
      if (!this.issues.length) {
        this.panelOpen = false;
        return;
      }
      this.panelOpen = true;
    },
    closePanel(): void {
      this.panelOpen = false;
    },
    togglePanel(): void {
      if (this.panelOpen) this.closePanel();
      else this.openPanel();
    },

    reset(): void {
      this.issues = [];
      this.panelOpen = false;
    },
  },
});