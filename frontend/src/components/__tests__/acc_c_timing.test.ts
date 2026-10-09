/**
 * F14 反例（acc-c）：折叠耗时与明细总耗时必须是同一个口径（契约 C8）。
 *
 * 用户可见「总耗时」= 排队 + 执行；TURN_END 的 duration_ms 只是执行时长。
 * 折叠态显示总耗时（不展开也能看到），明细加载不得把它改小/改没。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia } from "pinia";
import TurnTimingPanel from "../TurnTimingPanel.vue";
import { clearTurnTimingCache } from "../../composables/useTurnTiming";

const { getTrace } = vi.hoisted(() => ({ getTrace: vi.fn() }));

vi.mock("../../services/api", () => ({
  api: { getTrace: (...args: unknown[]) => getTrace(...args) },
  ApiError: class ApiError extends Error {},
}));

function mountPanel(props: Record<string, unknown> = {}) {
  const pinia = createPinia();
  return mount(TurnTimingPanel, {
    props: { turnId: "turn_1", ...props },
    global: { plugins: [pinia] },
  });
}

async function open(w: ReturnType<typeof mountPanel>) {
  const details = w.find("details");
  (details.element as HTMLDetailsElement).open = true;
  await details.trigger("toggle");
  await flushPromises();
}

beforeEach(() => {
  clearTurnTimingCache();
  getTrace.mockReset();
});

describe("F14：折叠与明细使用同一总耗时口径", () => {
  it("执行 3 秒 + 排队 7 秒：折叠态显示总耗时 10 秒（不是 3 秒）", async () => {
    getTrace.mockResolvedValue({
      turn_id: "turn_1",
      duration_ms: 3000,
      phases: {
        version: 1,
        total_ms: 3000,
        sum_ms: 3000,
        residual_ms: 0,
        notes: { queue_wait_ms: 7000 },
        spans: [{ name: "model_wait", ms: 3000, depth: 1, start_ms: 0 }],
        after_turn: [],
      },
    });
    const w = mountPanel({ durationMs: 3000, queueMs: 7000 });
    await flushPromises();
    expect(w.find("summary").text()).toContain("10 秒");
    expect(w.find("summary").attributes("aria-label")).toContain("总耗时 10 秒");

    await open(w);
    // 明细加载后折叠态仍是同一个总耗时（不能被明细覆盖）
    expect(w.find("summary").text()).toContain("10 秒");
    expect(w.find("summary").attributes("aria-label")).toContain("总耗时 10 秒");
    // 分项：排队 7 秒 + 模型 3 秒，比例对总耗时成立
    expect(w.text()).toContain("排队");
    expect(w.text()).toContain("7.0 秒");
    w.unmount();
  });

  it("无排队（queue_ms=0）：折叠态仍显示总耗时（与执行相同）", async () => {
    getTrace.mockResolvedValue({
      turn_id: "turn_1",
      duration_ms: 2500,
      phases: {
        version: 1,
        total_ms: 2500,
        sum_ms: 2500,
        residual_ms: 0,
        notes: { queue_wait_ms: 0 },
        spans: [{ name: "model_wait", ms: 2500, depth: 1, start_ms: 0 }],
        after_turn: [],
      },
    });
    const w = mountPanel({ durationMs: 2500, queueMs: 0 });
    await flushPromises();
    expect(w.find("summary").text()).toContain("2.5 秒");
    w.unmount();
  });

  it("只有执行时长、排队未知：显示可证明的执行时间并明确标签，不假装是总耗时", async () => {
    getTrace.mockResolvedValue({ turn_id: "turn_1", duration_ms: null, phases: {} });
    const w = mountPanel({ durationMs: 3000, queueMs: null });
    await flushPromises();
    const text = w.find("summary").text();
    expect(text).toContain("3.0 秒");
    expect(text).toContain("排队");
    expect(text).not.toContain("总耗时");
    w.unmount();
  });

  it("缺 trace：TURN_END 的排队 + 执行仍然显示总耗时", async () => {
    getTrace.mockRejectedValue(new Error("no trace"));
    const w = mountPanel({ durationMs: 3000, queueMs: 7000 });
    await flushPromises();
    expect(w.find("summary").text()).toContain("10 秒");
    w.unmount();
  });

  it("明细口径与 TURN_END 不一致时：已知总耗时不被明细覆盖，占比按显示的总耗时算", async () => {
    // trace 只认得执行 3 秒；TURN_END 的权威事实是 排队 7 秒 + 执行 3 秒
    getTrace.mockResolvedValue({
      turn_id: "turn_1",
      duration_ms: 3000,
      phases: {
        version: 1,
        total_ms: 3000,
        sum_ms: 3000,
        residual_ms: 0,
        notes: {},
        spans: [{ name: "model_wait", ms: 3000, depth: 1, start_ms: 0 }],
        after_turn: [],
      },
    });
    const w = mountPanel({ durationMs: 3000, queueMs: 7000 });
    await flushPromises();
    await open(w);
    expect(w.find("summary").text()).toContain("总耗时 10 秒");
    expect(w.find("summary").attributes("aria-label")).toContain("总耗时 10 秒");
    // 模型 3 秒占显示总耗时 10 秒的 30%
    const modelRow = w.findAll(".tt-row").find((row) => row.text().includes("模型"));
    expect(modelRow?.text()).toContain("30%");
    w.unmount();
  });

  it("零值是可证明的时间：duration 0 + queue 0 显示 0 毫秒，而缺失不显示 0", async () => {
    getTrace.mockResolvedValue({ turn_id: "turn_1", duration_ms: null, phases: null });
    const zero = mountPanel({ durationMs: 0, queueMs: 0 });
    await flushPromises();
    expect(zero.find("summary").text()).toContain("0 毫秒");
    zero.unmount();

    const missing = mountPanel({ durationMs: null, queueMs: null });
    await flushPromises();
    expect(missing.find("summary").text()).not.toContain("0 毫秒");
    missing.unmount();
  });
});
