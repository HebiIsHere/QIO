/** 记忆问题提醒：真实数量 + 加载失败与 0 区分 + 点击定位目标。 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";

const mocks = vi.hoisted(() => ({ request: vi.fn() }));
vi.mock("../../services/api", () => ({ request: mocks.request }));

import {
  firstAnswerableQuestion,
  memoryQuestionFromView,
  memoryReminderText,
  useMemoryQuestionsStore,
} from "../memoryQuestions";

function setup() {
  setActivePinia(createPinia());
  return useMemoryQuestionsStore();
}

const view = (id: string, status = "awaiting_answer") => ({
  id,
  kind: "maintenance_question",
  status,
  handler: "maintenance",
  title: "问题 " + id,
  question: "问题内容 " + id,
  knowledge: [],
  before: [],
  after: [],
  evidence: [],
  actions: ["answer", "defer"],
  created_at: "2026-10-22T00:00:00+00:00",
  updated_at: "2026-10-22T00:00:00+00:00",
});

beforeEach(() => {
  mocks.request.mockReset();
});

describe("记忆问题数量与加载失败区分", () => {
  it("没有待回答问题时状态是 ready 且不显示提醒（与加载失败区分开）", async () => {
    const store = setup();
    mocks.request.mockResolvedValue({ items: [], total: 0, truncated: false, maintenance_available: true });

    await store.load();

    expect(store.status).toBe("ready");
    expect(store.count).toBe(0);
    expect(store.reminderText).toBe("");
    expect(store.shouldShow).toBe(false);
  });

  it("有 2 个问题时显示真实数量", async () => {
    const store = setup();
    mocks.request.mockResolvedValue({
      items: [view("q1"), view("q2", "deferred")],
      total: 2,
      truncated: false,
      maintenance_available: true,
    });

    await store.load();

    expect(store.count).toBe(2);
    expect(store.reminderText).toBe("有 2 个问题待回答");
    expect(store.shouldShow).toBe(true);
  });

  it("加载失败不能显示成「没有问题」，并给出可重试状态", async () => {
    const store = setup();
    mocks.request.mockRejectedValueOnce(new Error("后端 503"));

    await store.load();

    expect(store.status).toBe("error");
    expect(store.reminderText).toContain("无法读取");
    expect(store.reminderText).not.toContain("0 个");
    expect(store.error).toContain("503");
    expect(store.shouldShow).toBe(true); // 失败也要让用户知道，而不是消失

    mocks.request.mockResolvedValueOnce({
      items: [view("q1")],
      total: 1,
      truncated: false,
      maintenance_available: true,
    });
    await store.load();
    expect(store.status).toBe("ready");
    expect(store.reminderText).toBe("有 1 个问题待回答");
  });

  it("维护存储不可用时返回 error，而不是 0", async () => {
    const store = setup();
    mocks.request.mockResolvedValue({
      items: [],
      total: 0,
      truncated: false,
      maintenance_available: false,
      warnings: ["维护事项存储不可用（迁移 47 未应用）"],
    });

    await store.load();

    expect(store.status).toBe("error");
    expect(store.maintenanceAvailable).toBe(false);
    expect(store.reminderText).toContain("无法读取");
  });

  it("点击定位：定位到等待回答的那一条（deferred 排在其后）", async () => {
    const store = setup();
    mocks.request.mockResolvedValue({
      items: [view("q_deferred", "deferred"), view("q_waiting", "awaiting_answer")],
      total: 2,
      truncated: false,
      maintenance_available: true,
    });

    await store.load();

    expect(store.openTarget).toEqual({ tab: "memory", itemId: "q_waiting" });
    expect(firstAnswerableQuestion(store.items)?.id).toBe("q_waiting");
  });

  it("提醒文案是纯函数：失败 / 0 / 有数量三种结论互不混淆", () => {
    expect(memoryReminderText({ status: "error", count: 0 })).toContain("无法读取");
    expect(memoryReminderText({ status: "ready", count: 0 })).toBe("");
    expect(memoryReminderText({ status: "ready", count: 3 })).toBe("有 3 个问题待回答");
    expect(
      memoryReminderText({ status: "error", count: 3, maintenanceAvailable: false }),
    ).toContain("维护存储不可用");
  });

  it("只把 kind=maintenance_question 的项当问题（知识待审不混进来）", () => {
    expect(memoryQuestionFromView({ ...view("q1"), kind: "knowledge_proposal" })).toBeNull();
  });
});
