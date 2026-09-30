/**
 * 「有 N 个工具开发任务没做完」的唯一数据来源是后端 `GET /api/dev/tasks`。
 *
 * 以前任务只在工具调用里出现过：模型不说，界面就再也找不到那个任务。
 * 这里回归的是取数与「未完成」判定本身 —— 界面不许自己推断任务状态，
 * 也不许把「拉不到」装成「一条都没有」。
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useSessionStore } from "../session";
import { api } from "../../services/api";

vi.mock("../../services/api", () => ({
  api: {
    getDevTasks: vi.fn(),
  },
}));

const getDevTasks = api.getDevTasks as unknown as ReturnType<typeof vi.fn>;

function setup() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return { session: useSessionStore() };
}

const TASKS = [
  {
    id: "ws_a",
    request: "做一个求和工具",
    phase: "testing_failed",
    submitted: false,
    test_passed: false,
    test_evidence_current: true,
    updated_at: "2026-09-30T10:00:00+00:00",
  },
  {
    id: "ws_b",
    request: "已经提交过的任务",
    phase: "ready",
    submitted: true,
    test_passed: true,
    test_evidence_current: true,
    updated_at: "2026-09-30T11:00:00+00:00",
  },
];

beforeEach(() => {
  vi.clearAllMocks();
  getDevTasks.mockResolvedValue({ tasks: TASKS });
});

describe("开发任务列表", () => {
  it("拉取到的任务进 store，已提交的不算「没做完」", async () => {
    const { session } = setup();

    await session.refreshDevTasks();

    expect(session.devTasks.map((t) => t.id)).toEqual(["ws_a", "ws_b"]);
    expect(session.unfinishedDevTasks.map((t) => t.id)).toEqual(["ws_a"]);
  });

  it("拉不到列表时保留上一次的结果，不把「拉不到」装成「一条都没有」", async () => {
    const { session } = setup();
    await session.refreshDevTasks();

    getDevTasks.mockRejectedValueOnce(new Error("boom"));
    await session.refreshDevTasks();

    expect(session.unfinishedDevTasks.map((t) => t.id)).toEqual(["ws_a"]);
  });
});
