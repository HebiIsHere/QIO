/**
 * 对话页 v2 · 统一审批 store（D 负责）。
 *
 * 把「所有需要用户明确批准/拒绝的单次申请」收进**一个队列**，一次只展示一项：
 *
 * * 工具/权限/凭据申请：身份是 approval_id，真实的提交接口是
 *   `POST /api/approvals/{approval_id}/respond`（复用 stores/approvals 的既有校验、
 *   单次消费、stale 处理与预算 overrides）；
 * * 知识保存申请：身份是 knowledge id（没有 approval_id），提交走知识自己的原子接口
 *   `POST /api/knowledge/{id}/approval`。两类**绝不串线**：分派按结构化字段
 *   （有没有 approval_id），不看标题文案。
 *
 * 队列规则（契约 §4.4）：
 * - 同一审批 id / 同一知识 id 只入队一次；
 * - 新申请不替换正在阅读/编辑的那一项；用户可主动切换；
 * - 关闭/稍后处理只隐藏，不批准也不拒绝；已暂缓的同一项不再自动弹；
 * - 提交期间防重复；失败 / 超时 / 失效 / 已在别处处理分别说明；
 * - 自动弹出：工具/权限类（含后台工作与子智能体）按规则自动；主对话知识保存
 *   从不自动，只更新入口数量；已有其他弹窗/用户正在输入时不叠加。
 */
import { defineStore } from "pinia";
import { request } from "../services/api";
import type { MemoryPendingListing, PendingItemView } from "../services/api";
import { approvalFacts, useApprovalsStore, type ApprovalItem } from "./approvals";
import { useSessionStore } from "./session";

export type UnifiedKind = "tool" | "knowledge";
export type ToolOrigin = "main_agent" | "background" | "subagent" | "unknown";
export type LoadStatus = "idle" | "loading" | "ready" | "error";

/** 来源文案：如实说清是谁提出的（不把内部枚举直接给用户看）。 */
export const TOOL_ORIGIN_LABELS: Record<ToolOrigin, string> = {
  main_agent: "主智能体",
  background: "后台工作",
  subagent: "子智能体",
  unknown: "来源未记录",
};

export const APPROVAL_KIND_TITLES: Record<string, string> = {
  tool_create: "工具创建审批",
  credential_grant: "凭据授权审批",
  high_impact_knowledge: "知识维护确认",
  tool_execution: "需要你确认的操作",
  computer: "电脑操作审批",
  dependency_install: "安装依赖",
  tool_candidate: "工具建议确认",
  create_topic: "新建话题确认",
  continue: "继续本轮",
};

export const KNOWLEDGE_CATEGORY_LABELS: Record<string, string> = {
  user_profile: "关于你",
  agent_self: "QIO 对自己的认识",
  goal: "长期目标",
  general_fact: "一般事实",
  tool_experience: "工具经验",
};

/** 「确定程度」是事实属性，不是审批状态（照实展示，不替用户判断真假）。 */
export const EPISTEMIC_LABELS: Record<string, string> = {
  user_explicit: "你明确说过",
  shared_understanding: "你们已形成的理解",
  qio_observed: "QIO 观察到",
  qio_inference: "QIO 的推断",
};

/** 后台维护类审批：来自后台工作（会自动弹出，但来源必须如实写出来）。 */
const BACKGROUND_APPROVAL_KINDS = new Set([
  "tool_candidate",
  "high_impact_knowledge",
  "maintenance",
  "dependency_install",
]);

/** 后端结构化来源字段 → 展示来源。按字段判定，不猜标题。 */
export function classifyToolOrigin(
  kind: string,
  payload: Record<string, unknown> | null | undefined,
): ToolOrigin {
  const p = (payload ?? {}) as Record<string, unknown>;
  const source = String(p.source ?? p.origin ?? "").trim().toLowerCase();
  if (
    source === "background" ||
    source === "maintenance" ||
    source.startsWith("maintenance_") ||
    BACKGROUND_APPROVAL_KINDS.has(kind)
  ) {
    return "background";
  }
  if (
    p.subagent === true ||
    Object.prototype.hasOwnProperty.call(p, "subagent_id") ||
    Object.prototype.hasOwnProperty.call(p, "agent_id") ||
    String(p.executor ?? "").toLowerCase().includes("subagent")
  ) {
    return "subagent";
  }
  if (!kind) return "unknown";
  return "main_agent";
}

export interface SourceFact {
  label: string;
  value: string;
}

/**
 * 来源 / 任务 / 执行者等事实行：**只从结构化字段取值**，缺失就不显示。
 * 不编造「来自某话题」之类后端没给的归属。
 */
export function toolSourceFacts(item: {
  kind: string;
  origin: ToolOrigin;
  approvalId: string;
  payload: Record<string, unknown> | null | undefined;
}): SourceFact[] {
  const p = (item.payload ?? {}) as Record<string, unknown>;
  const text = (value: unknown): string => (typeof value === "string" ? value.trim() : "");
  const facts: SourceFact[] = [{ label: "来源", value: TOOL_ORIGIN_LABELS[item.origin] }];
  if (item.origin === "background" && item.kind === "high_impact_knowledge") {
    facts.push({ label: "来源说明", value: "后台记忆整理提出的知识调整；不会自动执行" });
  }
  const executor = text(p.executor) || text(p.agent) || text(p.subagent);
  if (executor) facts.push({ label: "执行者", value: executor });
  if (text(p.tool_name)) facts.push({ label: "工具", value: text(p.tool_name) });
  const task = text(p.workspace) || text(p.task_id);
  if (task) facts.push({ label: "来源任务", value: task });
  const topic = text(p.topic_id);
  if (topic) facts.push({ label: "话题", value: topic });
  const knowledgeId = text(p.knowledge_id);
  if (knowledgeId) facts.push({ label: "知识", value: knowledgeId });
  if (text(p.action)) {
    const ACTION_LABELS: Record<string, string> = {
      degrade: "降低使用权重",
      expire: "停用这条知识",
      correct: "修正这条知识",
      merge: "合并这条知识",
      delete: "删除这条知识",
    };
    facts.push({
      label: "建议动作",
      value: ACTION_LABELS[text(p.action)] ?? text(p.action),
    });
  }
  return facts;
}

export interface KnowledgePendingDetail {
  id: string;
  version: number | null;
  content: string;
  category: string;
  scope: string[];
  epistemicStatus: string | null;
  source: string | null;
  evidenceIds: string[];
  updatedAt: string | null;
}

export interface KnowledgeApprovalItem extends KnowledgePendingDetail {
  key: string;
  /** 候选被提出时的原因（事件里有就显示；没有就不显示）。 */
  reason: string;
  arrivedAt: number;
  detailStatus: LoadStatus;
  detailError: string;
}

export interface UnifiedApprovalItem {
  key: string;
  kind: UnifiedKind;
  /** 真实身份：approval_id（工具类）或 knowledge id（知识类）。 */
  identity: string;
  version: number | null;
  title: string;
  origin: ToolOrigin;
  highRisk: boolean;
  stale: boolean;
  deferred: boolean;
  approval: ApprovalItem | null;
  knowledge: KnowledgeApprovalItem | null;
}

export function unifiedKeyForTool(approvalId: string): string {
  return `tool:${approvalId}`;
}

export function unifiedKeyForKnowledge(knowledgeId: string): string {
  return `knowledge:${knowledgeId}`;
}

/** 用户是否正在输入（与 events store 的判定保持一致）。 */
export function isUserEditing(): boolean {
  if (typeof document === "undefined") return false;
  const active = document.activeElement as HTMLElement | null;
  if (!active) return false;
  return (
    active.tagName === "INPUT" ||
    active.tagName === "TEXTAREA" ||
    active.isContentEditable === true
  );
}

/**
 * 是否已有**别的**模态打开（不叠加、不替换）。
 * 统一审批自己的对话框带 data-unified-approval-modal，不算「别的」。
 */
export function isBlockingModalOpen(): boolean {
  if (typeof document === "undefined") return false;
  const nodes = Array.from(
    document.querySelectorAll('[role="dialog"][aria-modal="true"], dialog[open]'),
  );
  return nodes.some((node) => !node.hasAttribute("data-unified-approval-modal"));
}

export interface KnowledgeSubmitError {
  message: string;
  code: string;
  /** 需要重新拉一次待确认列表（版本/内容冲突、已在别处处理、超时结果未知）。 */
  refresh: boolean;
  /** 申请已不存在 / 已有结局：从队列里收口，不再提供提交按钮。 */
  terminal: boolean;
}

/** 把知识接口的结构化失败翻译成「结论先行」的中文（测试直接覆盖这一层）。 */
export function classifyKnowledgeSubmitError(error: unknown): KnowledgeSubmitError {
  const err = error as {
    status?: number;
    body?: { code?: string; error?: string; current?: { version?: number } };
    name?: string;
    message?: string;
  };
  const status = Number(err?.status ?? 0);
  const body = (err?.body ?? {}) as { code?: string; error?: string };
  const code = String(body.code ?? "");
  const detail = String(body.error ?? "").trim();
  if (status === 409) {
    if (code === "version_changed" || code === "content_changed" || code === "superseded") {
      return {
        message: "这条申请在你确认前已经变化，这次没有保存任何内容；已重新加载，请再看一遍后确认。",
        code: code || "content_changed",
        refresh: true,
        terminal: false,
      };
    }
    return {
      message: detail || "这条申请已经在别处被处理过；已重新加载待确认列表。",
      code: code || "already_handled",
      refresh: true,
      terminal: code === "already_handled" || code === "invalid_state",
    };
  }
  if (status === 404) {
    return {
      message: "这条申请已经不存在（可能已在别处处理）；没有保存任何内容。",
      code: "not_found",
      refresh: true,
      terminal: true,
    };
  }
  if (status === 400) {
    return {
      message: detail || "这次操作没有被服务端接受；没有保存任何内容。",
      code: code || "invalid",
      refresh: false,
      terminal: false,
    };
  }
  if (err?.name === "ApiTimeoutError") {
    return {
      message: "没有等到服务端响应：这次是否保存成功还不确定，正在重新核对待确认列表。",
      code: "timeout",
      refresh: true,
      terminal: false,
    };
  }
  if (status >= 500) {
    return {
      message: `服务端没有完成这次保存（${status}）；没有确认到结果，可以重试。`,
      code: "server_error",
      refresh: true,
      terminal: false,
    };
  }
  return {
    message: "没有连上服务端，这次是否保存成功还不确定；正在重新核对待确认列表。",
    code: "network",
    refresh: true,
    terminal: false,
  };
}

/** 把「记忆待确认」里的知识候选视图映射成知识保存申请（字段缺失就留空，不编造）。 */
export function knowledgeDetailFromView(view: PendingItemView): KnowledgePendingDetail | null {
  const ref = Array.isArray(view.knowledge) ? view.knowledge[0] : undefined;
  if (!ref || !ref.id) return null;
  return {
    id: String(ref.id),
    version: typeof ref.version === "number" ? ref.version : null,
    content: String(ref.content ?? ""),
    category: String(ref.category ?? ""),
    scope: Array.isArray(ref.scope) ? ref.scope.map((s) => String(s)) : [],
    epistemicStatus: ref.epistemic_status ? String(ref.epistemic_status) : null,
    source: ref.source ? String(ref.source) : null,
    evidenceIds: (view.evidence ?? [])
      .map((entry) => String((entry as { id?: unknown }).id ?? ""))
      .filter((id) => id.length > 0),
    updatedAt: view.updated_at ? String(view.updated_at) : null,
  };
}

/** 知识保存申请在队列里的排序：先到先处理。 */
function nextKnowledgeArrival(): number {
  return Date.now();
}

/** 统一队列的唯一拼装实现（getter 共用；顺序：工具队列 → 知识到达顺序）。 */
function mergeUnifiedItems(
  state: { knowledge: KnowledgeApprovalItem[]; deferredKeys: string[] },
  queue: ApprovalItem[],
): UnifiedApprovalItem[] {
  const tools: UnifiedApprovalItem[] = queue.map((approval) => {
    const key = unifiedKeyForTool(approval.approval_id);
    const facts = approvalFacts(approval);
    return {
      key,
      kind: "tool" as const,
      identity: approval.approval_id,
      version: null,
      title: APPROVAL_KIND_TITLES[approval.kind] ?? "需要你确认的操作",
      origin: classifyToolOrigin(approval.kind, approval.payload),
      highRisk: facts?.highRisk === true,
      stale: approval.stale === true,
      deferred: state.deferredKeys.includes(key),
      approval,
      knowledge: null,
    };
  });
  const knowledge: UnifiedApprovalItem[] = state.knowledge.map((item) => ({
    key: item.key,
    kind: "knowledge" as const,
    identity: item.id,
    version: item.version,
    title: "保存这条知识？",
    origin: (item.source && item.source !== "主对话" ? "background" : "main_agent") as ToolOrigin,
    highRisk: false,
    stale: false,
    deferred: state.deferredKeys.includes(item.key),
    approval: null,
    knowledge: item,
  }));
  return [...tools, ...knowledge];
}

const DEFERRED_STORAGE_KEY = "qio.unifiedApprovals.deferred";
const DEFERRED_MAX = 200;

function loadDeferredKeys(): string[] {
  try {
    const raw = globalThis.localStorage?.getItem(DEFERRED_STORAGE_KEY);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter((item): item is string => typeof item === "string").slice(-DEFERRED_MAX);
  } catch {
    return [];
  }
}

function persistDeferredKeys(keys: string[]): void {
  try {
    globalThis.localStorage?.setItem(
      DEFERRED_STORAGE_KEY,
      JSON.stringify(keys.slice(-DEFERRED_MAX)),
    );
  } catch {
    // 隐私模式 / 存储不可用：只影响跨重启记忆，不影响本次会话的暂缓语义
  }
}

let knowledgeInflight: Promise<boolean> | null = null;

export const useUnifiedApprovalsStore = defineStore("unifiedApprovals", {
  state: () => ({
    knowledge: [] as KnowledgeApprovalItem[],
    knowledgeStatus: "idle" as LoadStatus,
    knowledgeError: "",
    /** 最近一次从服务端读到待确认列表的时间（毫秒）；null = 还没成功读过 */
    knowledgeLoadedAt: null as number | null,
    visible: false,
    activeKey: null as string | null,
    /** 用户「稍后处理 / 关闭」过的申请：同一项不再自动弹 */
    deferredKeys: loadDeferredKeys(),
    /** 本会话已经自动弹出过的申请：不因轮询/重渲染反复弹 */
    autoShownKeys: [] as string[],
    /** 自动弹出被「别的弹窗 / 正在输入」挡住：条件恢复后可再试 */
    autoPending: false,
    submittingKey: null as string | null,
    submitError: "",
    submitErrorCode: "",
    /** 处理完一条后的一句话结果（成功/已处理），入口与弹窗都可展示 */
    lastNotice: "",
    editingKey: null as string | null,
    drafts: {} as Record<string, string>,
  }),
  getters: {
    /** 统一队列：工具/权限类在前（更紧急），知识保存按到达顺序在后。 */
    items(state): UnifiedApprovalItem[] {
      const approvals = useApprovalsStore();
      return mergeUnifiedItems(state, approvals.queue);
    },
    toolItems(state): UnifiedApprovalItem[] {
      const approvals = useApprovalsStore();
      return mergeUnifiedItems(state, approvals.queue).filter((item) => item.kind === "tool");
    },
    knowledgeItems(state): UnifiedApprovalItem[] {
      return mergeUnifiedItems(state, []).filter((item) => item.kind === "knowledge");
    },
    count(state): number {
      return useApprovalsStore().queue.length + state.knowledge.length;
    },
    toolCount(): number {
      return useApprovalsStore().queue.length;
    },
    knowledgeCount(state): number {
      return state.knowledge.length;
    },
    activeItem(state): UnifiedApprovalItem | null {
      const items = mergeUnifiedItems(state, useApprovalsStore().queue);
      if (!items.length) return null;
      return items.find((item) => item.key === state.activeKey) ?? items[0];
    },
    activeIndex(state): number {
      const items = mergeUnifiedItems(state, useApprovalsStore().queue);
      const active = items.find((item) => item.key === state.activeKey) ?? items[0];
      if (!active) return -1;
      return items.findIndex((item) => item.key === active.key);
    },
    knowledgeLoading(state): boolean {
      return state.knowledgeStatus === "loading";
    },
  },
  actions: {
    /** 从服务端读「待确认」里的知识保存申请（权威快照；失败如实保留错误，不当作 0）。 */
    async refreshKnowledgePending(): Promise<boolean> {
      if (knowledgeInflight) return knowledgeInflight;
      this.knowledgeStatus = "loading";
      knowledgeInflight = (async () => {
        try {
          const listing = await request<MemoryPendingListing>(
            "/api/memory/pending?kind=knowledge_proposal&limit=500",
          );
          const views = Array.isArray(listing.items) ? listing.items : [];
          const previous = new Map(this.knowledge.map((item) => [item.id, item]));
          const next: KnowledgeApprovalItem[] = [];
          const seen = new Set<string>();
          for (const view of views) {
            const detail = knowledgeDetailFromView(view);
            if (!detail || seen.has(detail.id)) continue;
            seen.add(detail.id);
            const prior = previous.get(detail.id);
            next.push({
              ...detail,
              key: unifiedKeyForKnowledge(detail.id),
              reason: prior?.reason ?? "",
              arrivedAt: prior?.arrivedAt ?? nextKnowledgeArrival(),
              detailStatus: "ready",
              detailError: "",
            });
          }
          this.knowledge = next;
          this.knowledgeStatus = "ready";
          this.knowledgeError = "";
          this.knowledgeLoadedAt = Date.now();
          this.noteQueueChanged();
          return true;
        } catch (error) {
          this.knowledgeStatus = "error";
          this.knowledgeError =
            "没能读到待确认的知识列表：" +
            ((error as Error)?.message || "未知原因") +
            "（已有条目保留；数量可能不是最新）";
          return false;
        } finally {
          knowledgeInflight = null;
        }
      })();
      return knowledgeInflight;
    },

    /**
     * 实时候选事件（KNOWLEDGE_CANDIDATE）：先入队（数量立刻更新），再拉权威详情。
     * 主对话提出的知识保存**从不自动弹窗**，只更新入口数量。
     */
    ingestKnowledgeCandidate(candidate: {
      knowledgeId: string;
      category: string;
      content: string;
      reason?: string;
    }): void {
      if (!candidate.knowledgeId) return;
      const key = unifiedKeyForKnowledge(candidate.knowledgeId);
      const existing = this.knowledge.find((item) => item.id === candidate.knowledgeId);
      if (existing) {
        if (candidate.reason) existing.reason = candidate.reason;
        if (candidate.content && !existing.content) existing.content = candidate.content;
      } else {
        this.knowledge.push({
          id: candidate.knowledgeId,
          key,
          version: null,
          content: candidate.content,
          category: candidate.category,
          scope: [],
          epistemicStatus: null,
          source: "主对话",
          evidenceIds: [],
          updatedAt: null,
          reason: candidate.reason ?? "",
          arrivedAt: nextKnowledgeArrival(),
          detailStatus: "loading",
          detailError: "",
        });
      }
      void this.refreshKnowledgePending();
    },

    /** 审批队列变化（响应外部 resolve/reconcile）：当前项没了就切到下一条。 */
    noteQueueChanged(): void {
      const items = this.items as UnifiedApprovalItem[];
      if (!items.length) {
        this.visible = false;
        this.activeKey = null;
        this.editingKey = null;
        // 注意：不清 submitError —— 队列被权威刷新清空时，失败原因仍要能说清楚
        return;
      }
      if (!this.activeKey || !items.some((item) => item.key === this.activeKey)) {
        this.activeKey = items[0]?.key ?? null;
        this.editingKey = null;
      }
    },

    /**
     * 自动弹出规则（只对工具/权限类；知识保存从不自动）：
     * - 主智能体新增权限 / 后台工作 / 子智能体 / 删除覆盖类重点确认 → 自动；
     * - 已暂缓的同一项不重复弹；本会话自动弹过的不重复弹；
     * - 已经打开 / 用户正在输入 / 别的弹窗开着 → 不叠加（保留申请，条件恢复再试）；
     * - 内联审批正在显示这一条 → 不自动弹（同一时刻只有一套按钮）。
     */
    maybeAutoOpen(): boolean {
      if (this.visible) return false;
      const approvals = useApprovalsStore();
      // 恢复路径 / 用户已点「稍后处理」：不抢焦点，入口亮着等用户主动打开
      if (approvals.deferred) return false;
      const items = this.items as UnifiedApprovalItem[];
      const candidate = items.find(
        (item) =>
          item.kind === "tool" &&
          !item.deferred &&
          !this.autoShownKeys.includes(item.key) &&
          !item.stale,
      );
      if (!candidate) return false;
      if (approvals.inlineClaimed || isUserEditing() || isBlockingModalOpen()) {
        this.autoPending = true;
        return false;
      }
      this.autoShownKeys = [...this.autoShownKeys.slice(-99), candidate.key];
      this.autoPending = false;
      this.open(candidate.key);
      return true;
    },

    /** 用户主动打开（入口点击 / 桌面通知等）：清除全局「稍后」并显示。 */
    open(key?: string | null): boolean {
      const items = this.items as UnifiedApprovalItem[];
      if (!items.length) return false;
      const target = (key && items.find((item) => item.key === key)) || this.activeItem || items[0];
      if (!target) return false;
      const approvals = useApprovalsStore();
      if (target.kind === "tool") {
        // 内联审批已经显示这条的按钮：同一时刻只能有一套按钮，不叠加。
        // 用户可在过程区内联卡上点「查看完整信息」把按钮交回统一弹窗。
        if (approvals.inlineClaimed) {
          this.lastNotice = "这项确认的按钮正在对话过程中显示；可以在那里查看完整信息。";
          return false;
        }
        approvals.openNow();
      }
      this.activeKey = target.key;
      this.visible = true;
      this.submitError = "";
      this.submitErrorCode = "";
      return true;
    },

    /** 关闭：只隐藏，不批准也不拒绝；当前项记为「稍后」，不因轮询反复自动弹。 */
    close(): void {
      const active = this.activeItem as UnifiedApprovalItem | null;
      if (active) this.deferKey(active.key);
      this.visible = false;
      this.activeKey = null;
      this.editingKey = null;
      this.autoPending = false;
      this.submitError = "";
      this.submitErrorCode = "";
    },

    /** 「稍后处理」当前项：隐藏这一项；队列里还有别的就展示下一条。 */
    deferActive(): void {
      const active = this.activeItem as UnifiedApprovalItem | null;
      if (!active) {
        this.close();
        return;
      }
      this.deferKey(active.key);
      if (active.kind === "tool") {
        // 与既有 store 语义一致：收起窗口但保留待审批项
        useApprovalsStore().defer();
      }
      const items = this.items as UnifiedApprovalItem[];
      if (!items.length) {
        this.visible = false;
        this.activeKey = null;
      } else {
        this.activeKey = items[0]?.key ?? null;
        this.visible = true;
      }
      this.editingKey = null;
      this.submitError = "";
      this.submitErrorCode = "";
    },

    deferKey(key: string): void {
      if (!key || this.deferredKeys.includes(key)) return;
      this.deferredKeys = [...this.deferredKeys, key].slice(-DEFERRED_MAX);
      persistDeferredKeys(this.deferredKeys);
    },

    /** 用户主动切换到队列中的另一项（不改变任何申请状态）。 */
    switchTo(key: string): boolean {
      const items = this.items as UnifiedApprovalItem[];
      const target = items.find((item) => item.key === key);
      if (!target) return false;
      if (target.kind === "tool") {
        const approvals = useApprovalsStore();
        if (approvals.inlineClaimed && approvals.inlineId !== target.identity) {
          this.lastNotice = "另一项确认的按钮正在对话过程中显示；先在那里处理完再切换。";
          return false;
        }
        approvals.showFull(target.identity);
      }
      this.activeKey = key;
      this.visible = true;
      this.submitError = "";
      this.submitErrorCode = "";
      this.editingKey = null;
      return true;
    },

    startEdit(): void {
      const active = this.activeItem as UnifiedApprovalItem | null;
      if (!active || active.kind !== "knowledge") return;
      this.drafts[active.key] = active.knowledge?.content ?? "";
      this.editingKey = active.key;
    },

    stopEdit(): void {
      this.editingKey = null;
    },

    updateDraft(text: string): void {
      const key = this.editingKey;
      if (!key) return;
      this.drafts[key] = text;
    },

    draftFor(key: string | null): string {
      if (!key) return "";
      return this.drafts[key] ?? "";
    },

    /** 「知道了」：后端已经不存在这条审批（stale）时唯一的出口。 */
    dismissStale(): void {
      const active = this.activeItem as UnifiedApprovalItem | null;
      if (!active || active.kind !== "tool" || !active.approval) return;
      useApprovalsStore().resolve(active.identity);
      this.noteQueueChanged();
    },

    async retryKnowledgeLoad(): Promise<boolean> {
      return this.refreshKnowledgePending();
    },

    /**
     * 提交当前申请。decision 只接受本 store 认识的四种动作；
     * 工具类 → approvals.respondById（approval_id）；知识类 → 知识原子接口（knowledge id）。
     * 返回是否成功（失败会保留申请并把可读原因写进 submitError）。
     */
    async submit(
      decision: "approve" | "approve_modified" | "ignore" | "reject",
      options: { overrides?: Record<string, unknown> } = {},
    ): Promise<boolean> {
      const active = this.activeItem as UnifiedApprovalItem | null;
      if (!active) return false;
      if (this.submittingKey) return false;
      if (active.kind === "tool") {
        if (decision !== "approve" && decision !== "reject") return false;
        if (active.stale) return false;
        this.submittingKey = active.key;
        this.submitError = "";
        this.submitErrorCode = "";
        try {
          const approvals = useApprovalsStore();
          await approvals.respondById(
            active.identity,
            decision === "approve" ? "approved" : "rejected",
            options.overrides,
          );
          const still = approvals.queue.some((item) => item.approval_id === active.identity);
          if (still) {
            this.submitError =
              approvals.error || "提交没有完成；这次没有做出任何授权，可以重试。";
            this.submitErrorCode = "tool_failed";
            return false;
          }
          this.lastNotice = decision === "approve" ? "已确认这一次操作。" : "已拒绝这一次操作。";
          this.finishHandled(active.key);
          return true;
        } finally {
          this.submittingKey = null;
        }
      }
      // 知识保存申请：只走知识自己的原子接口
      const knowledge = active.knowledge;
      if (!knowledge) return false;
      const content = decision === "approve_modified" ? this.draftFor(active.key).trim() : undefined;
      if (decision === "approve_modified" && !content) {
        this.submitError = "修改后的内容不能为空；请先填写要保存的内容。";
        this.submitErrorCode = "empty_content";
        return false;
      }
      const body: Record<string, unknown> = { decision };
      if (content !== undefined) body.content = content;
      if (knowledge.version !== null) body.expected_version = knowledge.version;
      if (knowledge.content) body.expected_content = knowledge.content;
      if (knowledge.updatedAt) body.expected_updated_at = knowledge.updatedAt;
      this.submittingKey = active.key;
      this.submitError = "";
      this.submitErrorCode = "";
      try {
        const result = await request<{
          ok: boolean;
          decision: string;
          already_handled?: boolean;
          message?: string;
        }>(`/api/knowledge/${encodeURIComponent(knowledge.id)}/approval`, {
          method: "POST",
          body: JSON.stringify(body),
        });
        const messages: Record<string, string> = {
          approve: "已保存你确认的这条知识。",
          approve_modified: "已按你修改后的内容保存。",
          ignore: "这条知识没有保存，之后不会再询问同一类内容。",
          reject: "已把这条申请打回草稿。",
        };
        this.lastNotice = result?.message || messages[decision] || "已处理。";
        this.removeKnowledge(knowledge.id);
        this.finishHandled(active.key);
        return true;
      } catch (error) {
        const classified = classifyKnowledgeSubmitError(error);
        this.submitError = classified.message;
        this.submitErrorCode = classified.code;
        if (classified.terminal) {
          // 申请已经有结局：收口（不假装成功，也不留一个点不动的按钮）；
          // 结局原因留在入口提示里，不让用户以为「点了一下什么都没发生」。
          this.lastNotice = classified.message;
          this.removeKnowledge(knowledge.id);
          this.finishHandled(active.key);
        }
        if (classified.refresh) {
          void this.refreshKnowledgePending().then(() => {
            if (classified.code === "timeout" && !this.knowledge.some((item) => item.id === knowledge.id)) {
              this.lastNotice = "已重新核对：这条已不在待确认列表里（可能已经保存），没有重复提交。";
            }
          });
        }
        return false;
      } finally {
        this.submittingKey = null;
      }
    },

    removeKnowledge(knowledgeId: string): void {
      this.knowledge = this.knowledge.filter((item) => item.id !== knowledgeId);
      const session = this.sessionStore();
      if (session) {
        try {
          session._dropCandidate(knowledgeId);
        } catch {
          // 会话 store 尚未初始化 / 候选已不在：不影响审批结果本身
        }
      }
    },

    sessionStore(): ReturnType<typeof useSessionStore> | null {
      try {
        // 会话 store 尚未初始化（单元测试只装 pinia 也能跑）：候选卡清理失败不影响审批结果
        return useSessionStore();
      } catch {
        return null;
      }
    },

    finishHandled(key: string): void {
      const items = this.items as UnifiedApprovalItem[];
      const remaining = items.filter((item) => item.key !== key);
      if (!remaining.length) {
        this.visible = false;
        this.activeKey = null;
        this.editingKey = null;
        return;
      }
      this.activeKey = remaining[0]?.key ?? null;
      this.visible = true;
      this.editingKey = null;
    },

    reset(): void {
      this.knowledge = [];
      this.knowledgeStatus = "idle";
      this.knowledgeError = "";
      this.knowledgeLoadedAt = null;
      this.visible = false;
      this.activeKey = null;
      this.deferredKeys = [];
      this.autoShownKeys = [];
      this.autoPending = false;
      this.submittingKey = null;
      this.submitError = "";
      this.submitErrorCode = "";
      this.lastNotice = "";
      this.editingKey = null;
      this.drafts = {};
      persistDeferredKeys([]);
    },
  },
});
