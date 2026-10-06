/**
 * D 独立验证：统一过程区（契约 §1.5）。
 *
 * 验证方式：真挂载 MessageStream + MessageItem，用冻结的 SSE 载荷驱动 events store，
 * 然后断言 DOM 上用户能看到什么。不读实现方结论。
 *
 * DOM 锚点（契约没冻结 DOM，由验证方向 Lead/B 声明的**最小可测点**；
 * 找不到锚点会明确失败并说明缺什么，不会静默通过）：
 *   [data-test="turn-process"]           一轮一个过程区（根元素）
 *   [data-test="turn-process-status"]    状态行：系统事实（受理中/运行中/等待确认/已停止/已完成 + 耗时）
 *   [data-test="turn-process-duration"]  总耗时（未展开也要有）
 *   [data-test="turn-process-history"]   历史体：展开才可见
 *
 * 断言的是契约规则本身，不是某一版实现：
 *   立即出现 / 安静（没有阶段也要显示真实状态） / 同阶段更新 / 自主转阶段 /
 *   并行工具与晚到结果 / 无重复气泡 / 完成自动收起。
 *
 * 基线（ee6bbff）现状：没有 TurnProcess、interim 会单独成泡、耗时按消息出现 ——
 * 本文件在实现合并前应当是**红的**。
 *
 * 运行：cd frontend; npx vitest run src/components/__tests__/TurnProcess.verify.test.ts
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount, type DOMWrapper, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";
import MessageStream from "../MessageStream.vue";
import { useEventStore } from "../../stores/events";
import { useSessionStore } from "../../stores/session";
import type { AgentEvent } from "../../services/events";

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
  return {
    api: new Proxy({}, { get: () => vi.fn(async () => payload()) }),
    ApiError: class ApiError extends Error {},
  };
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

function processRegion(wrapper: VueWrapper): DOMWrapper<Element> | null {
  for (const selector of [
    '[data-test="turn-process"]',
    '[data-test="turn-process-region"]',
    ".turn-process",
  ]) {
    const found = wrapper.findAll(selector);
    if (found.length) return found[0] as DOMWrapper<Element>;
  }
  return null;
}

function requireProcessRegion(wrapper: VueWrapper): DOMWrapper<Element> {
  const region = processRegion(wrapper);
  expect(
    region,
    '契约 §1.5：一轮必须有一个可识别的过程区（锚点 [data-test="turn-process"]）',
  ).not.toBeNull();
  return region as DOMWrapper<Element>;
}

function occurrenceCount(haystack: string, needle: string): number {
  if (!needle) return 0;
  return haystack.split(needle).length - 1;
}

/** 历史体是否处于「收起」状态：缺失 / 不可见 / aria-hidden / 祖先 details 未 open。 */
function historyCollapsed(wrapper: VueWrapper): boolean {
  const body = wrapper.find('[data-test="turn-process-history"]');
  if (!body.exists()) return true;
  if (!body.isVisible()) return true;
  const node = body.element as HTMLElement;
  if (node.getAttribute("aria-hidden") === "true") return true;
  const details = node.closest("details") as HTMLDetailsElement | null;
  return Boolean(details && !details.open);
}

beforeEach(() => {
  seq = 0;
});

describe("契约 §1.5：立即出现、安静、无重复", () => {
  it("工具一开跑就出现过程区，没有阶段也要显示真实系统状态", async () => {
    const { wrapper, events, session } = await mountStream();
    events.dispatch(ev("TURN_START", { turn_id: "turn_1", revision: 1 }));
    session.pushUser("帮我看下这个项目");
    await settle();

    events.dispatch(
      ev("TOOL_START", { turn_id: "turn_1", call_id: "c1", tool: "fs_read", arguments: { path: "a.py" } }),
    );
    await settle();

    const region = requireProcessRegion(wrapper);
    const text = region.text();
    expect(text, "过程区必须显示系统事实（运行中/正在做什么），不能空着").toMatch(/运行中|正在|读取/);
    wrapper.unmount();
  });

  it("同一内容只出现一次：interim 不再单独成泡", async () => {
    const { wrapper, events, session } = await mountStream();
    events.dispatch(ev("TURN_START", { turn_id: "turn_1", revision: 1 }));
    session.pushUser("帮我看下这个项目");
    events.dispatch(
      ev("ASSISTANT", {
        turn_id: "turn_1",
        content: "我先看几个文件再来回答",
        interim: true,
        streaming: true,
        delta_id: "dl_turn1_1",
        seq: 1,
      }),
    );
    events.dispatch(ev("TOOL_START", { turn_id: "turn_1", call_id: "c1", tool: "fs_read" }));
    await settle();

    requireProcessRegion(wrapper);
    const all = wrapper.text();
    expect(occurrenceCount(all, "我先看几个文件再来回答")).toBe(1);
    wrapper.unmount();
  });
});

describe("契约 §1.2 / §1.3：同阶段更新与自主转阶段", () => {
  it("阶段说明就地更新；op=next 后当前阶段换成新阶段，各文字只出现一次", async () => {
    const { wrapper, events, session } = await mountStream();
    events.dispatch(ev("TURN_START", { turn_id: "turn_1", revision: 1 }));
    session.pushUser("帮我改个文件");

    events.dispatch(
      ev("STAGE", {
        turn_id: "turn_1",
        stage_id: "st_turn1_1",
        index: 1,
        status: "running",
        name: "读取仓库结构",
        text: "正在读取仓库结构",
        kind: "progress",
        op: "start",
        narrative_id: "msg_1",
        call_id: null,
        call_ids: ["c1"],
        created_at: "2026-10-06T08:00:00+00:00",
      }),
    );
    events.dispatch(ev("TOOL_START", { turn_id: "turn_1", call_id: "c1", tool: "fs_read", stage_id: "st_turn1_1" }));
    events.dispatch(
      ev("TOOL_END", { turn_id: "turn_1", call_id: "c1", tool: "fs_read", ok: true, stage_id: "st_turn1_1", duration_ms: 12 }),
    );
    // 同一阶段的第二次说明：只能更新，不能长出新阶段
    events.dispatch(
      ev("STAGE", {
        turn_id: "turn_1",
        stage_id: "st_turn1_1",
        index: 1,
        status: "running",
        name: "读取仓库结构",
        text: "已经读完 12 个文件",
        kind: "progress",
        op: "update",
        narrative_id: "msg_2",
        call_id: null,
        call_ids: ["c1"],
        created_at: "2026-10-06T08:00:01+00:00",
      }),
    );
    // op=next：结束当前阶段，开新阶段
    events.dispatch(
      ev("STAGE", {
        turn_id: "turn_1",
        stage_id: "st_turn1_2",
        index: 2,
        status: "running",
        name: "核对实现",
        text: "正在核对实现",
        kind: "progress",
        op: "next",
        narrative_id: "msg_3",
        call_id: null,
        call_ids: ["c2"],
        created_at: "2026-10-06T08:00:02+00:00",
      }),
    );
    events.dispatch(ev("TOOL_START", { turn_id: "turn_1", call_id: "c2", tool: "fs_write", stage_id: "st_turn1_2" }));
    events.dispatch(
      ev("TOOL_END", { turn_id: "turn_1", call_id: "c2", tool: "fs_write", ok: true, stage_id: "st_turn1_2", duration_ms: 20 }),
    );
    await settle();

    const region = requireProcessRegion(wrapper);
    const text = region.text();
    expect(text).toContain("核对实现");
    expect(occurrenceCount(wrapper.text(), "正在核对实现")).toBe(1);
    expect(occurrenceCount(wrapper.text(), "已经读完 12 个文件")).toBeLessThanOrEqual(1);
    wrapper.unmount();
  });

  it("并行工具与晚到结果：两个工具同时跑，晚到的结果只收口自己的卡", async () => {
    const { wrapper, events, session } = await mountStream();
    events.dispatch(ev("TURN_START", { turn_id: "turn_1", revision: 1 }));
    session.pushUser("并行做两件事");
    events.dispatch(ev("TOOL_START", { turn_id: "turn_1", call_id: "c1", tool: "fs_read", stage_id: "st_turn1_1" }));
    events.dispatch(ev("TOOL_START", { turn_id: "turn_1", call_id: "c2", tool: "grep_search", stage_id: "st_turn1_1" }));
    await settle();
    events.dispatch(
      ev("TOOL_END", { turn_id: "turn_1", call_id: "c1", tool: "fs_read", ok: true, stage_id: "st_turn1_1", duration_ms: 8 }),
    );
    await settle();
    const afterFirst = requireProcessRegion(wrapper).text();
    expect(afterFirst, "还有工具在跑时不能宣布都完成了").toMatch(/运行中|正在|1/);

    events.dispatch(
      ev("TOOL_END", {
        turn_id: "turn_1",
        call_id: "c2",
        tool: "grep_search",
        ok: false,
        error: "搜索失败",
        stage_id: "st_turn1_1",
        duration_ms: 30,
      }),
    );
    await settle();
    expect(wrapper.text()).toContain("搜索失败");
    wrapper.unmount();
  });
});

describe("契约 §1.5：完成自动收起", () => {
  it("完成后过程区收起，保留简短状态 + 总耗时；回答在下方", async () => {
    const { wrapper, events, session } = await mountStream();
    events.dispatch(ev("TURN_START", { turn_id: "turn_1", revision: 1 }));
    session.pushUser("帮我改个文件");
    events.dispatch(
      ev("STAGE", {
        turn_id: "turn_1",
        stage_id: "st_turn1_1",
        index: 1,
        status: "running",
        name: "读取仓库结构",
        text: "正在读取仓库结构",
        kind: "progress",
        op: "start",
        narrative_id: "msg_1",
        call_ids: ["c1"],
        created_at: "2026-10-06T08:00:00+00:00",
      }),
    );
    events.dispatch(ev("TOOL_START", { turn_id: "turn_1", call_id: "c1", tool: "fs_read", stage_id: "st_turn1_1" }));
    events.dispatch(
      ev("TOOL_END", { turn_id: "turn_1", call_id: "c1", tool: "fs_read", ok: true, stage_id: "st_turn1_1", duration_ms: 12 }),
    );
    events.dispatch(
      ev("ASSISTANT", {
        turn_id: "turn_1",
        content: "已经改好了。",
        interim: false,
        streaming: false,
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
    await settle();

    const region = requireProcessRegion(wrapper);
    expect(region.text()).toMatch(/已完成|已停止/);
    expect(historyCollapsed(wrapper), "完成之后过程区必须自动收起").toBe(true);
    expect(wrapper.text()).toContain("已经改好了。");
    wrapper.unmount();
  });
});
