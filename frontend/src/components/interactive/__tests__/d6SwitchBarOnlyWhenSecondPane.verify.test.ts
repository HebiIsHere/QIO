/**
 * 独立验收 D（第六轮）：切换条只在确有第二个面板时出现（契约 §12.6）。
 *
 * 由独立验收子智能体 D 编写，**不修改任何产品代码**。对应验收条文：
 * - §12.6 反例 7：480×600、没有任何审批项（审批列表不存在）、只开聊天 ——
 *   不许出现虚假切换条，不许为不存在的切换空间做多余预留，聊天面板不许被抬高。
 *   （800×600 对照：宽窗口不进入切换状态，同样不该有切换条与抬高。）
 *
 * 标注：【组件/DOM】挂载真实 IntentBatchTray，量的是写进舞台的真实几何变量；
 * 真实矩形与截图由探针在真实浏览器里补充。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { mount } from "@vue/test-utils";
import IntentBatchTray from "../IntentBatchTray.vue";
import { useInteractiveStore } from "../../../stores/interactive";

interface FakeRect {
  top: number;
  height: number;
}

function rectLike(r: FakeRect): DOMRect {
  return {
    x: 0, y: r.top, top: r.top, bottom: r.top + r.height,
    left: 0, right: 1000, width: 1000, height: r.height, toJSON: () => ({}),
  } as unknown as DOMRect;
}

const TOOLBAR_TOP = 484;

async function mountTrayAt(viewport: { width: number; height: number }, chatOpen: boolean) {
  const toolbar = document.createElement("div");
  toolbar.setAttribute("data-im", "board-toolbar");
  const toggle = document.createElement("button");
  toggle.setAttribute("data-im", "chat-toggle");
  document.body.append(toolbar, toggle);

  const rects = new Map<Element, FakeRect>([
    [toolbar, { top: TOOLBAR_TOP, height: 60 }],
    [toggle, { top: TOOLBAR_TOP + 20, height: 34 }],
  ]);
  const native = Element.prototype.getBoundingClientRect;
  const spy = vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function (this: Element) {
    const fake = rects.get(this);
    if (fake) return rectLike(fake);
    return native.call(this);
  });

  const store = useInteractiveStore();
  store.intents = []; // 没有任何审批项：审批列表不存在（batches 为空）
  store.chatOpen = chatOpen;
  store.batchOpen = false;

  vi.useFakeTimers();
  const wrapper = mount(IntentBatchTray, { attachTo: document.body });
  const tray = wrapper.find('[data-im="batch-area"]').element;
  const stage = tray.parentElement as HTMLElement;
  expect(stage, "找不到板面舞台（组件挂载点）").toBeTruthy();
  rects.set(stage, { top: 48, height: viewport.height - 48 - 64 });
  await vi.advanceTimersByTimeAsync(60);
  await wrapper.vm.$nextTick();
  await vi.advanceTimersByTimeAsync(60);
  await wrapper.vm.$nextTick();
  return { wrapper, stage, store, spy };
}

describe("§12.6 反例 7：480×600 无审批项只开聊天 —— 不许有虚假切换条 / 多余预留 / 抬高", () => {
  let mounted: Awaited<ReturnType<typeof mountTrayAt>> | null = null;

  beforeEach(() => {
    localStorage.clear();
    setActivePinia(createPinia());
    Object.defineProperty(window, "innerWidth", { value: 480, configurable: true });
    Object.defineProperty(window, "innerHeight", { value: 600, configurable: true });
  });

  afterEach(async () => {
    if (mounted) {
      mounted.wrapper.unmount();
      mounted = null;
    }
    vi.restoreAllMocks();
    vi.useRealTimers();
    document.body.innerHTML = "";
  });

  it("【组件/DOM】切换条不渲染、切换预留不出现、聊天面板不被抬高", async () => {
    mounted = await mountTrayAt({ width: 480, height: 600 }, true);
    const { wrapper, stage } = mounted;

    expect(
      wrapper.find('[data-im="overlay-switch"]').exists(),
      "没有审批项、只开聊天时出现了虚假切换条（§12.6：不存在可展示的第二个面板就不许进入切换布局）",
    ).toBe(false);
    expect(
      stage.style.getPropertyValue("--im-geo-chat-lift"),
      "聊天面板被为不存在的切换空间抬高了（§12.6：不预留不存在的切换条空间）",
    ).toBe("0px");
    expect(
      stage.getAttribute("im-geo-mode"),
      "布局状态被算成了需要切换显示（只有一个真实面板时不应进入切换状态）",
    ).not.toBe("switched");
  });
});

describe("§12.6 对照：800×600 只开聊天（宽窗口，本来就不该有切换条）", () => {
  let mounted: Awaited<ReturnType<typeof mountTrayAt>> | null = null;

  beforeEach(() => {
    localStorage.clear();
    setActivePinia(createPinia());
    Object.defineProperty(window, "innerWidth", { value: 800, configurable: true });
    Object.defineProperty(window, "innerHeight", { value: 600, configurable: true });
  });

  afterEach(async () => {
    if (mounted) {
      mounted.wrapper.unmount();
      mounted = null;
    }
    vi.restoreAllMocks();
    vi.useRealTimers();
    document.body.innerHTML = "";
  });

  it("【组件/DOM】宽窗口对照：同样没有切换条、没有抬高", async () => {
    mounted = await mountTrayAt({ width: 800, height: 600 }, true);
    const { wrapper, stage } = mounted;

    expect(wrapper.find('[data-im="overlay-switch"]').exists()).toBe(false);
    expect(stage.style.getPropertyValue("--im-geo-chat-lift")).toBe("0px");
    expect(stage.getAttribute("im-geo-mode")).not.toBe("switched");
  });
});
