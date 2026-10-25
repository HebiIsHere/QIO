/**
 * 记忆整理问题提醒（D 负责）。
 *
 * 记忆整理问题**不是审批**：不套用批准/拒绝，也不进入主对话上下文。对话页只在
 * 星球入口旁显示一句真实提醒与真实数量；点击后进入现有星球「待确认」并定位到具体
 * 问题（定位能力由 Lead 在 PlanetView 集成 props 时消费本 store 的 openTarget）。
 *
 * 两条底线：
 * - 数量来自服务端 GET /api/memory/pending?kind=maintenance_question 的 total，
 *   不从知识候选或主对话消息里推断；
 * - **加载失败 ≠ 没有问题**：失败时 status=error 并给出可重试提示，不显示成 0。
 */
import { defineStore } from "pinia";
import { request } from "../services/api";
import type { MemoryPendingListing, PendingItemView } from "../services/api";

export type MemoryQuestionStatus = "idle" | "loading" | "ready" | "error";

export interface MemoryQuestionItem {
  id: string;
  title: string;
  question: string;
  status: string;
  createdAt: string;
  updatedAt: string;
  scope: string[];
}

/** 点击提醒后要打开的星球位置：现有星球「待确认」= tab memory。 */
export interface MemoryOpenTarget {
  tab: "memory";
  /** 要定位的待回答问题的真实 id；列表为空时为 null。 */
  itemId: string | null;
}

/** 把维护问题视图映射成提醒条目（只取真实字段）。 */
export function memoryQuestionFromView(view: PendingItemView): MemoryQuestionItem | null {
  if (!view || !view.id) return null;
  if (view.kind !== "maintenance_question") return null;
  return {
    id: String(view.id),
    title: String(view.title ?? ""),
    question: String(view.question ?? ""),
    status: String(view.status ?? ""),
    createdAt: String(view.created_at ?? ""),
    updatedAt: String(view.updated_at ?? ""),
    scope: Array.isArray(view.knowledge)
      ? view.knowledge.flatMap((ref) => (Array.isArray(ref.scope) ? ref.scope.map(String) : []))
      : [],
  };
}

/** 优先定位「等待回答」，其次「稍后处理」；都没有返回 null。 */
export function firstAnswerableQuestion(items: MemoryQuestionItem[]): MemoryQuestionItem | null {
  return (
    items.find((item) => item.status === "awaiting_answer") ??
    items.find((item) => item.status === "deferred") ??
    items[0] ??
    null
  );
}

/** 提醒文案：失败说失败，0 说空（调用方用空串表示「不显示」），成功说真实数量。 */
export function memoryReminderText(input: {
  status: MemoryQuestionStatus;
  count: number | null;
  maintenanceAvailable?: boolean;
}): string {
  if (input.status === "loading" && !input.count) return "正在读取记忆问题…";
  if (input.status === "error") {
    return input.maintenanceAvailable === false
      ? "记忆问题暂时无法读取（维护存储不可用）"
      : "记忆问题暂时无法读取";
  }
  if (input.status === "ready" && input.count !== null && input.count > 0) {
    return "有 " + input.count + " 个问题待回答";
  }
  return "";
}

let inflight: Promise<boolean> | null = null;

export const useMemoryQuestionsStore = defineStore("memoryQuestions", {
  state: () => ({
    status: "idle" as MemoryQuestionStatus,
    /** 服务端 total（null = 还没成功读过）；失败时保留上一次已知数量但状态为 error */
    count: null as number | null,
    items: [] as MemoryQuestionItem[],
    maintenanceAvailable: true,
    error: "",
    loadedAt: null as number | null,
  }),
  getters: {
    reminderText(state): string {
      return memoryReminderText({
        status: state.status,
        count: state.count,
        maintenanceAvailable: state.maintenanceAvailable,
      });
    },
    /** 有真实待回答问题时才显示提醒（0 个 = 不打扰） */
    shouldShow(state): boolean {
      if (state.status === "error") return true;
      return state.status === "ready" && (state.count ?? 0) > 0;
    },
    openTarget(state): MemoryOpenTarget {
      const first = firstAnswerableQuestion(state.items);
      return { tab: "memory", itemId: first?.id ?? null };
    },
    hasQuestions(state): boolean {
      return state.status === "ready" && (state.count ?? 0) > 0;
    },
  },
  actions: {
    async load(): Promise<boolean> {
      if (inflight) return inflight;
      this.status = "loading";
      inflight = (async () => {
        try {
          const listing = await request<MemoryPendingListing>(
            "/api/memory/pending?kind=maintenance_question&limit=500",
          );
          const views = Array.isArray(listing.items) ? listing.items : [];
          this.items = views
            .map((view) => memoryQuestionFromView(view))
            .filter((item): item is MemoryQuestionItem => item !== null);
          this.maintenanceAvailable = listing.maintenance_available !== false;
          if (!this.maintenanceAvailable) {
            // 存储不可用：服务端没给出可信的问题列表，不能显示成「没有问题」
            this.status = "error";
            this.error =
              (listing.warnings && listing.warnings[0]) ||
              "维护事项存储不可用：读不到记忆问题，不代表没有问题。";
            return false;
          }
          this.count = Number.isFinite(listing.total) ? Number(listing.total) : this.items.length;
          this.status = "ready";
          this.error = "";
          this.loadedAt = Date.now();
          return true;
        } catch (error) {
          this.status = "error";
          this.error =
            "没能读到记忆整理问题：" +
            ((error as Error)?.message || "未知原因") +
            "（这不代表没有问题）";
          return false;
        } finally {
          inflight = null;
        }
      })();
      return inflight;
    },
    /** 失败后的重试（保留上一次已知数量，等待新结果覆盖） */
    async retry(): Promise<boolean> {
      return this.load();
    },
  },
});
