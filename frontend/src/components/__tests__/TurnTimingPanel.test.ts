import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia } from "pinia";
import TurnTimingPanel from "../TurnTimingPanel.vue";
import { clearTurnTimingCache } from "../../composables/useTurnTiming";
import { useUiStore } from "../../stores/ui";
import type { PhaseLedger } from "../../services/trace";

const { getTrace } = vi.hoisted(() => ({ getTrace: vi.fn() }));

vi.mock("../../services/api", () => ({
  api: { getTrace: (...args: unknown[]) => getTrace(...args) },
  ApiError: class ApiError extends Error {},
}));

function ledger(over: Partial<PhaseLedger> = {}): PhaseLedger {
  return {
    version: 1,
    total_ms: 1699,
    sum_ms: 1669,
    residual_ms: 30,
    notes: { queue_wait_ms: 100 },
    spans: [
      { name: "adapter_setup", ms: 182, depth: 1, start_ms: 0 },
      { name: "context_assembly", ms: 200, depth: 1, start_ms: 190 },
      { name: "retrieval", ms: 120, depth: 2, start_ms: 200 },
      { name: "agent_loop", ms: 1000, depth: 1, start_ms: 400 },
      { name: "approval_wait", ms: 900, depth: 2, start_ms: 410 },
      { name: "model_wait", ms: 60, depth: 2, start_ms: 1320 },
      { name: "persistence", ms: 200, depth: 1, start_ms: 1400 },
      { name: "memory_post", ms: 87, depth: 1, start_ms: 1600 },
    ],
    after_turn: [],
    ...over,
  };
}

function mountPanel(props: Record<string, unknown> = {}, devMode = false) {
  const pinia = createPinia();
  const w = mount(TurnTimingPanel, {
    props: { turnId: "turn_1", ...props },
    global: { plugins: [pinia] },
  });
  if (devMode) useUiStore().developerMode = true;
  return w;
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
  getTrace.mockResolvedValue({ turn_id: "turn_1", status: "done", duration_ms: 1699, phases: ledger() });
});

describe("耗时面板：懒加载与用户分类", () => {
  it("展开之前不发请求（一页几十条消息不能各拉一次）", async () => {
    const w = mountPanel();
    await flushPromises();
    expect(getTrace).not.toHaveBeenCalled();
    expect(w.text()).not.toContain("等待确认");
    w.unmount();
  });

  it("展开后给出用户分类与占比，不出现内部阶段名", async () => {
    const w = mountPanel();
    await open(w);
    const text = w.text();
    for (const label of ["排队", "准备环境", "上下文准备", "记忆检索", "模型", "等待确认", "保存", "记忆整理"]) {
      expect(text).toContain(label);
    }
    expect(text).not.toContain("adapter_setup");
    expect(text).not.toContain("approval_wait");
    // 时间有单位
    expect(text).toMatch(/毫秒|秒/);
    // 占比条存在
    expect(w.findAll(".tt-fill").length).toBeGreaterThan(3);
    w.unmount();
  });

  it("开发者模式才显示内部阶段名，未归类的也看得到", async () => {
    getTrace.mockResolvedValue({
      turn_id: "turn_dev",
      duration_ms: 50,
      phases: ledger({
        notes: {},
        residual_ms: 0,
        spans: [
          { name: "brand_new_stage", ms: 20, depth: 1, start_ms: 0 },
          { name: "model_wait", ms: 30, depth: 1, start_ms: 20 },
        ],
      }),
    });
    const w = mountPanel({ turnId: "turn_dev", dev: true });
    await open(w);
    const dev = w.find("[data-test='tt-dev']").text();
    expect(dev).toContain("brand_new_stage");
    expect(dev).toContain("model_wait");
    expect(w.text()).toContain("其它"); // 未知阶段落进「其它」，没有消失
    w.unmount();
  });

  it("读屏句子挂在 summary 上：总耗时 + 大头", async () => {
    const w = mountPanel();
    await open(w);
    const label = w.find("summary").attributes("aria-label") ?? "";
    expect(label).toContain("总耗时");
    expect(label).toContain("等待确认");
    w.unmount();
  });

  it("turn 之后的后台整理单独说明，不混进总耗时", async () => {
    getTrace.mockResolvedValue({
      turn_id: "turn_after",
      duration_ms: 100,
      phases: ledger({
        notes: {},
        residual_ms: 0,
        spans: [{ name: "model_wait", ms: 100, depth: 1, start_ms: 0 }],
        after_turn: [{ name: "derived_work", ms: 420 }],
      }),
    });
    const w = mountPanel({ turnId: "turn_after" });
    await open(w);
    expect(w.text()).toContain("还整理了");
    expect(w.text()).toContain("不计入");
    w.unmount();
  });
});

describe("展示路径不碰 trace 里的敏感/隐私字段", () => {
  it("API 响应里有路径与用户原文时，面板只渲染 phases（不把它们带进界面）", async () => {
    const secret = "sk-LIVECANARY0123456789";
    const winPath = "C:\\Users\\someone\\Documents\\private\\plan.md";
    getTrace.mockResolvedValue({
      turn_id: "turn_priv",
      duration_ms: 100,
      // 下面这些字段后端**确实会返回**（trace 的既有内容），但展示路径不该读它们
      final_preview: `回答里带了 ${secret} 和 ${winPath}`,
      injection: { items: [{ surface: "user", preview: `用户原文 ${winPath}` }] },
      tool_runs: [{ tool: "fs_read", args_preview: winPath, result_preview: secret }],
      phases: ledger({
        notes: {},
        residual_ms: 0,
        spans: [{ name: "model_wait", ms: 100, depth: 1, start_ms: 0 }],
      }),
    });
    const w = mountPanel({ turnId: "turn_priv" });
    await open(w);
    const text = w.text();
    const html = w.html();
    for (const blob of [text, html]) {
      expect(blob).not.toContain(secret);
      expect(blob).not.toContain(winPath);
      expect(blob).not.toContain("C:\\Users");
    }
    expect(text).toContain("模型");
    w.unmount();
  });
});

describe("降级：不显示 0，也不留空白", () => {
  it("老数据（没有 phases）：给总耗时 + 明确说明", async () => {
    getTrace.mockResolvedValue({ turn_id: "old", duration_ms: 8436, phases: {} });
    const w = mountPanel({ turnId: "old" });
    await open(w);
    expect(w.text()).toContain("8.4 秒");
    expect(w.text()).toContain("没有分阶段记录");
    expect(w.find(".tt-rows").exists()).toBe(false);
    w.unmount();
  });

  it("连耗时都没有：说「没有留下耗时记录」，不显示 0", async () => {
    getTrace.mockResolvedValue({ turn_id: "empty", duration_ms: null, phases: {} });
    const w = mountPanel({ turnId: "empty" });
    await open(w);
    expect(w.text()).toContain("没有留下耗时记录");
    expect(w.text()).not.toContain("0 毫秒");
    w.unmount();
  });

  it("接口失败：说清没读到，不抛错也不空白", async () => {
    getTrace.mockRejectedValue(new Error("boom"));
    const w = mountPanel({ turnId: "broken" });
    await open(w);
    expect(w.text()).toContain("没读到耗时明细");
    w.unmount();
  });

  it("没有 turnId 时整个面板不出现", () => {
    const w = mountPanel({ turnId: null });
    expect(w.find("details").exists()).toBe(false);
    w.unmount();
  });
});

describe("长 trace 的渲染开销", () => {
  it("500 个 span 仍然能快速渲染（给出真实数字 + 上限）", async () => {
    const spans = [];
    for (let i = 0; i < 250; i += 1) {
      spans.push({ name: "model_wait", ms: 2, depth: 2, start_ms: i * 4 + 1 });
      spans.push({ name: "tool_wait", ms: 2, depth: 2, start_ms: i * 4 + 3 });
    }
    getTrace.mockResolvedValue({
      turn_id: "turn_big",
      duration_ms: 1000,
      phases: {
        version: 1,
        total_ms: 1000,
        sum_ms: 1000,
        residual_ms: 0,
        notes: { queue_wait_ms: 5 },
        spans: [{ name: "agent_loop", ms: 1000, depth: 1, start_ms: 0 }, ...spans],
        after_turn: [],
      },
    });
    const w = mountPanel({ turnId: "turn_big" });
    const started = performance.now();
    await open(w);
    const elapsed = performance.now() - started;
    // eslint-disable-next-line no-console
    console.log(`[perf] 501-span trace 展开渲染 ${elapsed.toFixed(1)}ms`);
    expect(w.findAll(".tt-row").length).toBeGreaterThan(0);
    expect(elapsed).toBeLessThan(300);
    w.unmount();
  });
});
