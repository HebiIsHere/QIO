/**
 * Phase 1-B 顶栏搜索 / 保存状态控件（子智能体 p1-layout 负责）。
 *
 * 契约：互动页的搜索与唯一保存状态挂到 #qio-interactive-chrome-extra（A 提供），
 * 搜索结果浮层用 Teleport 到 body + fixed 定位，**不被全局栏裁切**。
 * 这里只用真实 store 的真实板面数据与真实搜索纯函数，不 mock 业务数据。
 */
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";
import InteractiveChromeTools from "../v2/InteractiveChromeTools.vue";
import { saveStatusView } from "../v2/saveStatus";
import { useInteractiveStore } from "../../../stores/interactive";
import * as board from "../../../interactive/board";

const TARGET_ID = "qio-interactive-chrome-extra";

let store: ReturnType<typeof useInteractiveStore>;
let target: HTMLDivElement | null = null;
let wrapper: VueWrapper | null = null;

beforeEach(() => {
  setActivePinia(createPinia());
  store = useInteractiveStore();
  store.board = board.emptyState("board_t");
  document.querySelectorAll("#" + TARGET_ID).forEach((node) => node.remove());
  target = document.createElement("div");
  target.id = TARGET_ID;
  document.body.appendChild(target);
});

afterEach(() => {
  wrapper?.unmount();
  wrapper = null;
  target?.remove();
  target = null;
});

async function settle(): Promise<void> {
  await nextTick();
  await nextTick();
}

async function mountTools(): Promise<VueWrapper> {
  wrapper = mount(InteractiveChromeTools, { attachTo: document.body });
  await settle();
  return wrapper;
}

function pick<T extends Element>(selector: string): T | null {
  return document.querySelector<T>(selector);
}

function pickOrFail<T extends Element>(selector: string): T {
  const node = pick<T>(selector);
  if (!node) throw new Error("找不到元素：" + selector);
  return node;
}

function saveText(): string {
  return pickOrFail('[data-iw-chrome-save]').textContent ?? "";
}

async function typeQuery(text: string): Promise<void> {
  const input = pickOrFail<HTMLInputElement>("[data-iw-chrome-search-input]");
  input.value = text;
  input.dispatchEvent(new Event("input"));
  await nextTick();
}

describe("保存状态文案（纯函数）", () => {
  it("按真实状态说话：未保存 / 保存中 / 已保存 / 有改动", () => {
    expect(saveStatusView({ status: "idle", dirty: false, error: null })).toEqual({
      text: "尚未保存过",
      tone: "ok",
      detail: "",
    });
    expect(saveStatusView({ status: "saving", dirty: true, error: null }).text).toBe("正在保存…");
    expect(saveStatusView({ status: "saving", dirty: true, error: null }).tone).toBe("busy");
    expect(saveStatusView({ status: "saved", dirty: false, error: null }).text).toBe("已保存");
    expect(saveStatusView({ status: "saved", dirty: true, error: null }).text).toBe("有改动尚未保存");
  });

  it("保存失败：一句人话 + 可排查的详情，不把接口路径/内部代码铺在脸上", () => {
    const view = saveStatusView({
      status: "error",
      dirty: true,
      error: "/api/interactive/boards/b1/state -> 500: 服务端拒绝了这次请求",
    });
    expect(view.tone).toBe("err");
    expect(view.text.startsWith("保存失败：")).toBe(true);
    expect(view.text).not.toContain("/api/");
    expect(view.detail).not.toContain("/api/");
    expect(view.detail.length).toBeGreaterThan(0);
  });

  it("没有拿到失败原因时如实说，不编造", () => {
    const view = saveStatusView({ status: "error", dirty: true, error: null });
    expect(view.text).toBe("保存失败：没有拿到失败原因");
    expect(view.tone).toBe("err");
  });
});

describe("InteractiveChromeTools：挂载与保存状态", () => {
  it("挂载到 #qio-interactive-chrome-extra", async () => {
    await mountTools();
    const tools = pickOrFail("[data-iw-chrome-tools]");
    expect(target?.contains(tools)).toBe(true);
    expect(pickOrFail('[data-iw-chrome-search]').getAttribute("aria-label")).toBe("搜索板面");
    expect(pickOrFail("[data-iw-chrome-save]").getAttribute("role")).toBe("status");
  });

  it("目标挂载点不存在时不渲染，也不报错；挂载点出现后自动接上", async () => {
    target?.remove();
    target = null;
    await mountTools();
    expect(pick("[data-iw-chrome-tools]")).toBeNull();

    // 模拟 A 的 WindowChrome 在互动路由挂载出容器的时机
    target = document.createElement("div");
    target.id = TARGET_ID;
    document.body.appendChild(target);
    await new Promise((resolve) => setTimeout(resolve, 0));
    await settle();
    expect(target.contains(pickOrFail("[data-iw-chrome-tools]"))).toBe(true);
  });

  it("保存状态直接跟随真实 store，不落成假文案", async () => {
    store.saveStatus = "saving";
    await mountTools();
    expect(saveText()).toContain("正在保存");

    store.saveStatus = "saved";
    store.dirty = true;
    await settle();
    expect(saveText()).toContain("有改动尚未保存");

    store.saveStatus = "error";
    store.saveError = "/api/interactive/boards/b1/state -> 500: 服务端拒绝了这次请求";
    await settle();
    expect(saveText()).toContain("保存失败：");
    expect(saveText()).not.toContain("/api/");
  });
});

describe("InteractiveChromeTools：板内搜索", () => {
  it("搜索结果浮层 Teleport 到 body（固定定位），不被全局栏裁切", async () => {
    await mountTools();
    expect(pick("[data-iw-chrome-search-panel]")).toBeNull();

    const trigger = pickOrFail<HTMLElement>("[data-iw-chrome-search]");
    trigger.click();
    await settle();

    expect(trigger.getAttribute("aria-expanded")).toBe("true");
    const panel = pickOrFail<HTMLElement>("[data-iw-chrome-search-panel]");
    expect(panel.parentElement).toBe(document.body);
    expect(target?.contains(panel)).toBe(false);
    expect(panel.style.position).toBe("fixed");
    expect(panel.getAttribute("role")).toBe("dialog");
  });

  it("用真实板面数据搜索：能搜到未勾选的注释，点结果 dispatch locate-card", async () => {
    store.board = board.addCard(board.emptyState("board_t"), {
      kind: "text",
      content: "预算表要重做",
      checked: false,
    });
    const cardId = store.board.cards[0].id;
    const seen: string[] = [];
    const onLocate = (event: Event) => {
      const detail = (event as CustomEvent<{ cardId?: string }>).detail;
      if (detail?.cardId) seen.push(detail.cardId);
    };
    window.addEventListener("qio:interactive:locate-card", onLocate);

    try {
      await mountTools();
      pickOrFail<HTMLElement>("[data-iw-chrome-search]").click();
      await settle();
      await typeQuery("预算");

      const result = pickOrFail<HTMLElement>("[data-iw-chrome-search-result]");
      expect(result.getAttribute("data-card-id")).toBe(cardId);
      expect(result.textContent).toContain("未勾选");

      result.click();
      await nextTick();
      expect(seen).toEqual([cardId]);
      expect(pickOrFail("[data-iw-chrome-search-panel]").textContent).toContain("已在板面上定位");
    } finally {
      window.removeEventListener("qio:interactive:locate-card", onLocate);
    }
  });

  it("没有匹配时如实说「没有匹配」，并说明搜索不等于交给 QIO", async () => {
    store.board = board.addCard(board.emptyState("board_t"), { kind: "text", content: "预算表要重做" });
    await mountTools();
    pickOrFail<HTMLElement>("[data-iw-chrome-search]").click();
    await settle();
    await typeQuery("完全不存在的关键词");
    const panel = pickOrFail("[data-iw-chrome-search-panel]");
    expect(panel.textContent).toContain("没有匹配");
    expect(panel.textContent).toContain("不会因此交给 QIO");
  });

  it("Esc 收起浮层并把焦点还给搜索按钮", async () => {
    await mountTools();
    const trigger = pickOrFail<HTMLElement>("[data-iw-chrome-search]");
    trigger.click();
    await settle();
    const input = pickOrFail<HTMLInputElement>("[data-iw-chrome-search-input]");
    expect(document.activeElement).toBe(input);

    input.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await settle();
    expect(pick("[data-iw-chrome-search-panel]")).toBeNull();
    expect(trigger.getAttribute("aria-expanded")).toBe("false");
    expect(document.activeElement).toBe(trigger);
  });

  it("点击浮层外部收起（不抢板面焦点）", async () => {
    await mountTools();
    pickOrFail<HTMLElement>("[data-iw-chrome-search]").click();
    await settle();
    expect(pick("[data-iw-chrome-search-panel]")).not.toBeNull();

    document.body.dispatchEvent(new Event("pointerdown", { bubbles: true }));
    await settle();
    expect(pick("[data-iw-chrome-search-panel]")).toBeNull();
  });

  it("浮层位置被视口钳制：贴右的触发按钮不会把浮层推出屏幕", async () => {
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 1024 });
    Object.defineProperty(window, "innerHeight", { configurable: true, value: 768 });
    await mountTools();
    const trigger = pickOrFail<HTMLElement>("[data-iw-chrome-search]");
    trigger.getBoundingClientRect = () =>
      ({
        x: 700,
        y: 40,
        left: 700,
        top: 40,
        right: 760,
        bottom: 64,
        width: 60,
        height: 24,
        toJSON: () => ({}),
      }) as DOMRect;

    trigger.click();
    await settle();

    const panel = pickOrFail<HTMLElement>("[data-iw-chrome-search-panel]");
    expect(panel.style.left).toBe("400px"); // 760 - 360
    expect(panel.style.top).toBe("70px"); // 64 + 6
    expect(panel.style.width).toBe("360px");
    expect(panel.style.maxHeight).toBe(768 - 8 - 70 + "px");
  });
});
