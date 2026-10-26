/**
 * D · 调试视图（DebugView）：读取失败必须留在它发生的区域，
 * 加载失败 ≠ 没有记录；列表 / 详情 / 开关失败不互相覆盖。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import DebugView from "../DebugView.vue";

const mocks = vi.hoisted(() => ({
  listTraces: vi.fn(),
  getTrace: vi.fn(),
  getTraceSettings: vi.fn(),
  updateTraceSettings: vi.fn(),
}));

vi.mock("../../services/api", () => ({
  api: {
    listTraces: mocks.listTraces,
    getTrace: mocks.getTrace,
    getTraceSettings: mocks.getTraceSettings,
    updateTraceSettings: mocks.updateTraceSettings,
  },
}));

const trace = {
  turn_id: "t1",
  status: "done",
  started_at: "2026-10-21T00:00:00+00:00",
  duration_ms: 12,
  final_topic: "话题",
};

function mountView() {
  return mount(DebugView, { global: { stubs: { RouterLink: true } } });
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.getTraceSettings.mockResolvedValue({ enabled: true });
  mocks.updateTraceSettings.mockImplementation(async (enabled: boolean) => ({ enabled }));
  mocks.getTrace.mockResolvedValue({
    turn_id: "t1",
    status: "done",
    started_at: "2026-10-21T00:00:00+00:00",
    duration_ms: 12,
    topic: {},
    injection: {},
    model_calls: [],
    tool_runs: [],
    writes: [],
    warnings: [],
    final_preview: "答案",
    error: null,
  });
});

describe("调试视图读取错误分区显示", () => {
  it("列表读取失败：错误在列表区、有重试；不显示成「还没有 trace」", async () => {
    mocks.listTraces.mockRejectedValueOnce(new Error("后端 503"));
    mocks.listTraces.mockResolvedValue({ traces: [trace], total: 1 });
    const w = mountView();
    await flushPromises();
    const error = w.find(".debug-error");
    expect(error.exists()).toBe(true);
    expect(error.text()).toContain("读取 Trace 列表失败");
    expect(error.text()).toContain("后端 503");
    expect(w.text()).not.toContain("还没有 trace");

    await error.find(".debug-retry").trigger("click");
    await flushPromises();
    expect(w.find(".row").exists()).toBe(true);
    w.unmount();
  });

  it("详情读取失败：错误在详情区、可重试；列表与选择保持", async () => {
    mocks.listTraces.mockResolvedValue({ traces: [trace], total: 1 });
    mocks.getTrace.mockRejectedValueOnce(new Error("详情 500"));
    mocks.getTrace.mockResolvedValueOnce({
      turn_id: "t1",
      status: "done",
      started_at: "2026-10-21T00:00:00+00:00",
      duration_ms: 12,
      topic: {},
      injection: {},
      model_calls: [],
      tool_runs: [],
      writes: [],
      warnings: [],
      final_preview: "答案",
      error: null,
    });
    const w = mountView();
    await flushPromises();
    await w.find(".row").trigger("click");
    await flushPromises();
    const detailError = w.find(".detail-sec .debug-error");
    expect(detailError.exists()).toBe(true);
    expect(detailError.text()).toContain("读取这条 Turn 的详情失败");
    expect(w.find(".row").exists()).toBe(true);

    await detailError.find(".debug-retry").trigger("click");
    await flushPromises();
    expect(w.find(".detail-sec .debug-error").exists()).toBe(false);
    expect(w.find(".detail-sec").text()).toContain("答案");
    w.unmount();
  });

  it("开关切换失败：错误在开关旁且不改真实状态（不假装切换）", async () => {
    mocks.listTraces.mockResolvedValue({ traces: [trace], total: 1 });
    mocks.updateTraceSettings.mockRejectedValueOnce(new Error("权限不足"));
    const w = mountView();
    await flushPromises();
    const toggle = w.findAll("button").find((b) => b.text().includes("记录已开启"))!;
    await toggle.trigger("click");
    await flushPromises();
    const error = w.find(".debug-settings-error");
    expect(error.exists()).toBe(true);
    expect(error.text()).toContain("切换记录开关失败");
    expect(w.text()).toContain("记录已开启");
    w.unmount();
  });
});
