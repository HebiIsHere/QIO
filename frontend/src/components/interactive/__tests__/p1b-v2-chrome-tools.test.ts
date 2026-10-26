/**
 * Phase 1-B 顶栏搜索 / 保存状态控件（子智能体 p1-layout 负责）。
 *
 * FU4 交互更新：搜索不再是「按钮 + 弹窗」，而是直接渲染在
 * #qio-interactive-chrome-extra 里的**内联输入框**；聚焦即进入输入并加宽，
 * 除 input 自身 placeholder 外没有任何标题 / label / 说明 / 关闭按钮文字。
 * 结果列表是锚定在输入框下方的小浮层（Teleport 到 body + fixed，避免被全局栏裁切），
 * 点结果仍 dispatch locate-card 定位到卡片。这里只用真实 store 板面与真实搜索纯函数。
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

/** 聚焦内联搜索框（点击与聚焦是同一条入口：聚焦即进入输入状态并加宽）。 */
async function focusSearch(): Promise<HTMLInputElement> {
  const input = pickOrFail<HTMLInputElement>("[data-iw-chrome-search-input]");
  input.focus();
  input.dispatchEvent(new Event("focus"));
  await settle();
  return input;
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
    expect(pickOrFail("[data-iw-chrome-search-input]").getAttribute("aria-label")).toBe("搜索板面");
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

describe("InteractiveChromeTools：板内搜索（内联输入）", () => {
  it("内联输入框直接渲染在挂载点里：无按钮、无弹窗，除 placeholder 外没有额外文字", async () => {
    await mountTools();
    const input = pickOrFail<HTMLInputElement>("[data-iw-chrome-search-input]");
    // 直接挂在全局栏挂载点里，不是按钮触发的弹窗
    expect(target?.contains(input)).toBe(true);
    expect(pick("[data-iw-chrome-search]"), "旧搜索按钮应已移除").toBeNull();
    expect(pick("[data-iw-chrome-search-panel]"), "旧弹窗应已移除").toBeNull();
    expect(pick('[role="dialog"]')).toBeNull();
    // input 自身之外没有标题 / label / 说明 / 关闭按钮文字
    const field = pickOrFail<HTMLElement>("[data-iw-chrome-search-field]");
    expect(field.textContent?.trim()).toBe("");
    expect(pickOrFail("[data-iw-chrome-tools]").textContent).not.toContain("板内搜索");
    expect(pickOrFail("[data-iw-chrome-tools]").textContent).not.toContain("搜你自己的板面");
    expect(pickOrFail("[data-iw-chrome-tools]").textContent).not.toContain("收起");
    expect(input.getAttribute("placeholder")).toBeTruthy();
    // aria-label 只落在控件本身
    expect(input.getAttribute("aria-label")).toBe("搜索板面");
  });

  it("点击 / 聚焦即进入输入状态并加宽（不需要点第二次）", async () => {
    await mountTools();
    const field = pickOrFail<HTMLElement>("[data-iw-chrome-search-field]");
    expect(field.getAttribute("data-expanded")).toBe("false");
    const input = await focusSearch();
    expect(field.getAttribute("data-expanded")).toBe("true");
    expect(document.activeElement).toBe(input);

    // 点搜索框任何位置（含放大镜图标区）都直接进入输入
    input.blur();
    await settle();
    field.click();
    await settle();
    expect(document.activeElement).toBe(input);
  });

  it("用真实板面数据搜索：结果列表在输入框下方，点结果 dispatch locate-card", async () => {
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
      await focusSearch();
      await typeQuery("预算");

      const results = pickOrFail<HTMLElement>("[data-iw-chrome-search-results]");
      // 浮层列表（不是弹窗）：无标题、无 role=dialog，Teleport 到 body 用 fixed 定位
      expect(results.parentElement).toBe(document.body);
      expect(results.style.position).toBe("fixed");
      expect(results.getAttribute("role")).not.toBe("dialog");
      expect(results.querySelector("header")).toBeNull();
      expect(results.textContent).not.toContain("板内搜索");
      expect(results.textContent).not.toContain("搜索只查");

      const result = pickOrFail<HTMLElement>("[data-iw-chrome-search-result]");
      expect(result.getAttribute("data-card-id")).toBe(cardId);
      expect(result.textContent).toContain("预算表要重做");

      result.click();
      await nextTick();
      expect(seen).toEqual([cardId]);
    } finally {
      window.removeEventListener("qio:interactive:locate-card", onLocate);
    }
  });

  it("没有匹配时不渲染结果列表，也不加「没有匹配」之类说明文字", async () => {
    store.board = board.addCard(board.emptyState("board_t"), { kind: "text", content: "预算表要重做" });
    await mountTools();
    await focusSearch();
    await typeQuery("完全不存在的关键词");
    expect(pick("[data-iw-chrome-search-results]")).toBeNull();
    expect(pickOrFail("[data-iw-chrome-search-field]").textContent?.trim()).toBe("");
    expect(pickOrFail("[data-iw-chrome-tools]").textContent).not.toContain("没有匹配");
  });

  it("Esc 收起结果、保留输入，且不把 Esc 交给板面", async () => {
    store.board = board.addCard(board.emptyState("board_t"), { kind: "text", content: "预算表要重做" });
    await mountTools();
    await focusSearch();
    await typeQuery("预算");
    expect(pick("[data-iw-chrome-search-results]")).not.toBeNull();

    let leaked = false;
    const onDocKey = () => {
      leaked = true;
    };
    document.addEventListener("keydown", onDocKey);
    try {
      const input = pickOrFail<HTMLInputElement>("[data-iw-chrome-search-input]");
      input.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
      await settle();
      expect(pick("[data-iw-chrome-search-results]")).toBeNull();
      expect(pickOrFail("[data-iw-chrome-search-field]").getAttribute("data-expanded")).toBe("false");
      expect(input.value).toBe("预算"); // 收起结果不清空输入
      expect(document.activeElement).toBe(input);
      expect(leaked, "Esc 不应冒泡到板面（避免被当成取消选择）").toBe(false);
    } finally {
      document.removeEventListener("keydown", onDocKey);
    }
  });

  it("键盘可达：ArrowDown 从输入框进入结果列表，Esc 再收起并回到输入框", async () => {
    store.board = board.addCard(board.emptyState("board_t"), { kind: "text", content: "预算表要重做" });
    await mountTools();
    const input = await focusSearch();
    await typeQuery("预算");

    input.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true }));
    await settle();
    const first = pickOrFail<HTMLElement>("[data-iw-chrome-search-result]");
    expect(document.activeElement).toBe(first);

    first.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await settle();
    expect(pick("[data-iw-chrome-search-results]")).toBeNull();
    expect(document.activeElement).toBe(input);
  });

  it("点击外部收起结果，输入保留", async () => {
    store.board = board.addCard(board.emptyState("board_t"), { kind: "text", content: "预算表要重做" });
    await mountTools();
    await focusSearch();
    await typeQuery("预算");
    expect(pick("[data-iw-chrome-search-results]")).not.toBeNull();

    document.body.dispatchEvent(new Event("pointerdown", { bubbles: true }));
    await settle();
    expect(pick("[data-iw-chrome-search-results]")).toBeNull();
    expect(pickOrFail("[data-iw-chrome-search-field]").getAttribute("data-expanded")).toBe("false");
    expect(pickOrFail<HTMLInputElement>("[data-iw-chrome-search-input]").value).toBe("预算");
  });

  it("窄窗 480：结果列表被视口钳制，不横向溢出", async () => {
    store.board = board.addCard(board.emptyState("board_t"), { kind: "text", content: "预算表要重做" });
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 480 });
    Object.defineProperty(window, "innerHeight", { configurable: true, value: 600 });
    await mountTools();
    const field = pickOrFail<HTMLElement>("[data-iw-chrome-search-field]");
    field.getBoundingClientRect = () =>
      ({
        x: 300,
        y: 8,
        left: 300,
        top: 8,
        right: 470,
        bottom: 36,
        width: 170,
        height: 28,
        toJSON: () => ({}),
      }) as DOMRect;

    await focusSearch();
    await typeQuery("预算");
    const results = pickOrFail<HTMLElement>("[data-iw-chrome-search-results]");
    const left = Number.parseFloat(results.style.left);
    const width = Number.parseFloat(results.style.width);
    expect(Number.isFinite(left)).toBe(true);
    expect(Number.isFinite(width)).toBe(true);
    expect(left).toBeGreaterThanOrEqual(8);
    expect(left + width).toBeLessThanOrEqual(480 - 8);
    expect(width).toBeLessThanOrEqual(480 - 16);
  });
});
