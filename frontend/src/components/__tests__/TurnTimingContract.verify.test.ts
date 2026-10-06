/**
 * D 独立验证：耗时契约（契约 §3）。
 *
 * 三条产品规则：
 *   1. 总耗时记在 turn 上，**不展开也能看见**（「已完成 · 耗时 1.5 秒」）；
 *   2. 展开才拉明细；只有真的在请求明细时才显示「读取中」；
 *   3. 已知总耗时不受明细加载失败影响；缺失 != 0；不永久转圈。
 * 外加：一轮只保留**一个**耗时入口（并入 TurnProcess 之后不得每条助手消息各来一个）。
 *
 * 基线（ee6bbff）现状：TurnTimingPanel 折叠态在没有明细时永远显示「读取中」，
 * 而且同一轮每条助手消息都会渲染一个入口 —— 本文件在实现合并前应当是**红的**。
 *
 * 运行：cd frontend; npx vitest run src/components/__tests__/TurnTimingContract.verify.test.ts
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";
import MessageStream from "../MessageStream.vue";
import { useEventStore } from "../../stores/events";
import { useSessionStore } from "../../stores/session";
import { clearTurnTimingCache, useTurnTiming } from "../../composables/useTurnTiming";
import type { AgentEvent } from "../../services/events";

const { getTrace, fetchTiming } = vi.hoisted(() => ({
  getTrace: vi.fn(),
  fetchTiming: vi.fn(),
}));

vi.mock("../../services/api", () => {
  const payload = () => ({
    tasks: [],
    messages: [],
    approvals: [],
    tools: [],
    narratives: [],
    instance_id: "inst_verify",
    turn_queue: { running: null, queued: [], cancelled: [], revision: 0, instance_id: "inst_verify" },
  });
  const base = new Proxy({}, { get: () => vi.fn(async () => payload()) });
  return {
    api: new Proxy(
      { getTrace: (...args: unknown[]) => getTrace(...args) },
      { get: (target: Record<string, unknown>, key: string) => (key in target ? target[key] : vi.fn(async () => payload())) },
    ),
    ApiError: class ApiError extends Error {},
  };
});

vi.mock("../../services/trace", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../services/trace")>();
  return { ...actual, fetchTurnTiming: (...args: unknown[]) => fetchTiming(...args) };
});

/**
 * 虚拟滚动在 jsdom 里没有布局，不会渲染任何轮次。这里替换成「全部渲染」的桩：
 * 验证的是我们自己的过程区/耗时渲染规则，不是 tanstack 的虚拟化。
 */
vi.mock("@tanstack/vue-virtual", async () => {
  const { computed } = await import("vue");
  return {
    // 真库返回的是 ref；组件里读 virtualizer.value.measureElement，所以这里也返回 ref。
    useVirtualizer: (options: { value: { count: number } }) =>
      computed(() => ({
        getTotalSize: () => options.value.count * 400,
        getVirtualItems: () =>
          Array.from({ length: options.value.count }, (_, index) => ({
            index,
            key: index,
            start: index * 400,
          })),
        measureElement: () => undefined,
      })),
  };
});

let seq = 0;

function ev(type: string, data: Record<string, unknown>): AgentEvent {
  seq += 1;
  return { type, id: "evt_" + seq, ts: new Date().toISOString(), data };
}

async function settle(): Promise<void> {
  await flushPromises();
  await nextTick();
  await nextTick();
  await new Promise((resolve) => setTimeout(resolve, 0));
  await nextTick();
}

/** jsdom 不做布局：手动给滚动容器一个可测的几何，否则虚拟滚动不渲染任何轮次。 */
function shape(el: HTMLElement, scrollHeight: number, clientHeight: number) {
  Object.defineProperty(el, "scrollHeight", { value: scrollHeight, configurable: true });
  Object.defineProperty(el, "clientHeight", { value: clientHeight, configurable: true });
}

async function mountStream() {
  const pinia = createPinia();
  setActivePinia(pinia);
  const events = useEventStore();
  const session = useSessionStore();
  const wrapper = mount(MessageStream, { global: { plugins: [pinia] } });
  await settle();
  const stream = wrapper.find(".stream").element as HTMLElement;
  shape(stream, 1000, 400);
  await settle();
  return { wrapper, events, session };
}

function completedTurn(events: ReturnType<typeof useEventStore>, session: ReturnType<typeof useSessionStore>) {
  events.dispatch(ev("TURN_START", { turn_id: "turn_1", revision: 1 }));
  session.pushUser("帮我改个文件");
  events.dispatch(
    ev("ASSISTANT", {
      turn_id: "turn_1",
      content: "我先看几个文件",
      interim: true,
      streaming: true,
      delta_id: "dl_turn1_1",
      seq: 1,
    }),
  );
  events.dispatch(ev("TOOL_START", { turn_id: "turn_1", call_id: "c1", tool: "fs_read" }));
  events.dispatch(ev("TOOL_END", { turn_id: "turn_1", call_id: "c1", tool: "fs_read", ok: true, duration_ms: 12 }));
  // 正式回答真流式：TURN_END 的 final_content 是权威全文，只做校准（不追加第二条）
  events.dispatch(
    ev("ASSISTANT", {
      turn_id: "turn_1",
      content: "已经改好了",
      interim: false,
      streaming: true,
      delta_id: "dl_turn1_2",
      seq: 1,
    }),
  );
  events.dispatch(
    ev("TURN_END", {
      turn_id: "turn_1",
      status: "completed",
      final_content: "已经改好了。",
      duration_ms: 1500,
      queue_ms: 0,
      started_at: "2026-10-06T08:00:00+00:00",
      ended_at: "2026-10-06T08:00:01.500+00:00",
    }),
  );
}

/**
 * 折叠态可见的耗时文字（未展开就应该有总数，且不出现「读取中」「0」）。
 * 锚点优先级：过程区里的耗时 span → 耗时控件 → 整个过程区 → 整页。
 * （实现把「已完成 · 耗时 1.5 秒」放在耗时 span 里；span 没文字时退回过程区读，口径不放宽。）
 */
function foldedTimingText(wrapper: VueWrapper): string {
  const candidates = [
    wrapper.find('[data-test="turn-process-duration"]'),
    wrapper.find('[data-test="turn-timing"]'),
    wrapper.find('[data-test="turn-process"]'),
  ];
  for (const found of candidates) {
    if (found.exists() && found.text().trim()) return found.text();
  }
  return wrapper.text();
}

/**
 * 展开耗时明细（真的触发 fetchTurnTiming 的那条路径）。
 * 返回 "details" 表示确实展开了耗时控件 —— 此时必须真的请求过明细。
 */
async function expandTimingDetail(wrapper: VueWrapper): Promise<"details" | "toggle" | "none"> {
  const timing = wrapper.find('[data-test="turn-timing"]');
  if (timing.exists() && timing.element.tagName.toLowerCase() === "details") {
    (timing.element as HTMLDetailsElement).open = true;
    await timing.trigger("toggle");
    await settle();
    return "details";
  }
  const toggle = wrapper.find('[data-test="turn-process-toggle"]');
  if (toggle.exists()) {
    await toggle.trigger("click");
    await settle();
    return "toggle";
  }
  const details = wrapper.findAll("details").find((node) => node.text().includes("耗时"));
  if (details) {
    (details.element as HTMLDetailsElement).open = true;
    await details.trigger("toggle");
    await settle();
    return "details";
  }
  return "none";
}

beforeEach(() => {
  seq = 0;
  getTrace.mockReset();
  fetchTiming.mockReset();
  clearTurnTimingCache();
});

describe("契约 §3：折叠态必须能看见总耗时", () => {
  it("一轮结束后不展开就有总耗时，且不出现「读取中」，也不发明细请求", async () => {
    const { wrapper, events, session } = await mountStream();
    completedTurn(events, session);
    await settle();

    const folded = foldedTimingText(wrapper);
    expect(folded, "未展开必须看得见总耗时").toMatch(/耗时[\s\S]{0,8}1\.5/);
    expect(wrapper.text()).not.toContain("读取中");
    // 本文件把 services/trace.fetchTurnTiming mock 成 fetchTiming，所以「有没有请求明细」看它
    expect(fetchTiming, "未展开不得请求明细").not.toHaveBeenCalled();
    expect(getTrace).not.toHaveBeenCalled();
    wrapper.unmount();
  });

  it("一轮只有一个耗时入口（同一轮多条助手消息不得各来一个）", async () => {
    const { wrapper, events, session } = await mountStream();
    completedTurn(events, session);
    await settle();

    const regions = wrapper.findAll('[data-test="turn-process"]');
    expect(regions.length, "契约 §1.5：一轮 = 一个过程区").toBe(1);
    const entries = wrapper.findAll('[data-test="turn-timing"]');
    expect(entries.length, "契约 §3：过程区与耗时面板只保留一个入口").toBeLessThanOrEqual(1);
    expect(foldedTimingText(wrapper), "总耗时必须落在这一个入口里").toMatch(/耗时[\s\S]{0,8}1\.5/);
    wrapper.unmount();
  });

  it("展开才拉明细；明细失败时总耗时仍在，不显示 0", async () => {
    const { wrapper, events, session } = await mountStream();
    completedTurn(events, session);
    await settle();

    fetchTiming.mockRejectedValue(new Error("trace 读取失败（验证注入）"));
    const expanded = await expandTimingDetail(wrapper);
    await flushPromises();
    await nextTick();

    const folded = foldedTimingText(wrapper);
    expect(folded, "明细失败不得抹掉已知总耗时").toMatch(/耗时[\s\S]{0,8}1\.5/);
    expect(folded).not.toContain("读取中");
    expect(folded).not.toMatch(/耗时\s*0\s*(毫秒|秒)/);
    if (expanded === "details") {
      expect(fetchTiming, "展开才拉明细：真的展开了耗时控件就必须真的请求过明细").toHaveBeenCalled();
    } else if (expanded === "none") {
      // 锚点缺失时至少证明：没有因为明细请求而丢掉总耗时
      expect(wrapper.text()).toContain("耗时");
    }
    wrapper.unmount();
  });
});

describe("契约 §3：五种状态互不混淆（composable 层）", () => {
  it("未请求 → idle；请求中 → loading；有数据 → ready", async () => {
    let resolveLoad: (value: unknown) => void = () => undefined;
    fetchTiming.mockImplementation(
      () => new Promise((resolve) => {
        resolveLoad = resolve;
      }),
    );
    const { state, timing, load } = useTurnTiming(() => "turn_1");
    expect(state.value).toBe("idle");

    const pending = load();
    expect(state.value).toBe("loading");

    resolveLoad({
      turnId: "turn_1",
      totalMs: 1500,
      executionMs: 1500,
      queueMs: 0,
      rows: [],
      residualMs: 0,
      afterTurnMs: 0,
      unknownStages: [],
      rawStages: [],
      legacy: false,
    });
    await pending;
    expect(state.value).toBe("ready");
    expect(timing.value?.totalMs).toBe(1500);
  });

  it("没有账本 → missing（不是 error、也不是 0）", async () => {
    fetchTiming.mockResolvedValue(null);
    const { state, timing, load } = useTurnTiming(() => "turn_2");
    await load();
    expect(state.value).toBe("missing");
    expect(timing.value).toBeNull();
  });

  it("请求失败 → error；缺失 != 0", async () => {
    fetchTiming.mockRejectedValue(new Error("boom"));
    const { state, timing, load } = useTurnTiming(() => "turn_3");
    await load();
    expect(state.value).toBe("error");
    expect(timing.value).toBeNull();
  });

  it("旧记录缺字段：totalMs 必须是 null 而不是 0", async () => {
    const actual = await vi.importActual<typeof import("../../services/trace")>("../../services/trace");
    const legacy = actual.buildTurnTiming({ turn_id: "turn_4", duration_ms: null, phases: null });
    expect(legacy).not.toBeNull();
    expect(legacy?.totalMs).toBeNull();
    expect(legacy?.legacy).toBe(true);
  });
});
