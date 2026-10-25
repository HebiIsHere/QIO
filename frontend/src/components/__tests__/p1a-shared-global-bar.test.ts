/**
 * Phase 1-A（互动页 v2）：对话页与互动页共用同一条全局栏（契约 §2.1）。
 *
 * 守的几条：
 * - 互动路由也有同一张 52px 圆角栏、同一份品牌（轨道标志 + PERSONAL AGENT）、设置入口与窗口控制；
 * - --titlebar-height 在对话、互动两个路由都是 70px，离开到其他页恢复原值；
 * - 互动路由渲染 id="qio-interactive-chrome-extra" 的搜索 / 保存状态挂载点，位置在品牌与设置之间；
 * - 该挂载点是拖动面的兄弟、显式恢复命中（pointer-events:auto），且只占自身内容宽度（不吞拖动面）；
 * - 挂载点允许溢出、也不建立 fixed 定位的包含块 —— B 用 fixed 浮层时不会被裁切或挪位；
 * - 对话路由不渲染该挂载点，现有视觉不回退。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { defineComponent, nextTick } from "vue";
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { createPinia } from "pinia";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const shell = vi.hoisted(() => {
  const unlisten = vi.fn();
  const state: { resized: (() => void) | null } = { resized: null };
  const win = {
    minimize: vi.fn(async () => {}),
    toggleMaximize: vi.fn(async () => {}),
    close: vi.fn(async () => {}),
    isMaximized: vi.fn(async () => false),
    onResized: vi.fn(async (cb: () => void) => {
      state.resized = cb;
      return unlisten;
    }),
  };
  const push = vi.fn(async () => undefined);
  return { win, unlisten, state, push };
});

vi.mock("@tauri-apps/api/window", () => ({ getCurrentWindow: () => shell.win }));

vi.mock("../../router", async () => {
  const { ref } = await import("vue");
  const currentRoute = ref<{ name: string }>({ name: "conversation" });
  return {
    default: { currentRoute, push: shell.push },
    setRouteName: (name: string) => {
      currentRoute.value = { name };
    },
  };
});

import * as routerModule from "../../router";
import WindowChrome from "../WindowChrome.vue";

function setRouteName(name: string) {
  (routerModule as unknown as { setRouteName: (name: string) => void }).setRouteName(name);
}

function setShell(on: boolean) {
  const g = globalThis as unknown as Record<string, unknown>;
  if (on) g.__TAURI_INTERNALS__ = {};
  else delete g.__TAURI_INTERNALS__;
}

const CHROME_SOURCE = readFileSync(
  resolve(process.cwd(), "src/components/WindowChrome.vue"),
  "utf8",
);

/** 取第一条命中选择器的样式块内容（选择器按字面量匹配，不做正则再解释） */
function cssBlock(selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = CHROME_SOURCE.match(new RegExp(escaped + "\\s*\\{([^}]*)\\}"));
  return match?.[1] ?? "";
}

const EXTRA_ID = "qio-interactive-chrome-extra";
const TITLEBAR_PROPERTY = "--titlebar-height";

/** 挂载过的组件一律在 afterEach 卸载：某条用例中途失败时，残留的窗口层会污染后面的用例 */
let activeWrappers: VueWrapper[] = [];

async function mountChrome(attachTo?: Element) {
  // 合并 R9 后 WindowChrome 依赖 pinia（关闭行为存在 ui store 里，ask 确认框要读它）。
  // 这里按真实启动方式装一个 pinia；不为测试在组件里加无 store 分支。
  const pinia = createPinia();
  const w = mount(WindowChrome, {
    global: { plugins: [pinia] },
    ...(attachTo ? { attachTo } : {}),
  });
  activeWrappers.push(w);
  await flushPromises();
  await flushPromises();
  return w;
}

beforeEach(() => {
  vi.clearAllMocks();
  shell.state.resized = null;
  shell.win.isMaximized.mockResolvedValue(false);
  setShell(true);
  setRouteName("conversation");
  document.documentElement.style.removeProperty(TITLEBAR_PROPERTY);
});

afterEach(() => {
  for (const w of activeWrappers) {
    try {
      w.unmount();
    } catch {
      // 已经卸载过（用例自己收了尾）就跳过
    }
  }
  activeWrappers = [];
  setShell(false);
  setRouteName("conversation");
  document.documentElement.style.removeProperty(TITLEBAR_PROPERTY);
  document.body.innerHTML = "";
});

describe("互动页与对话页共用同一条全局栏", () => {
  it("互动路由用同一张 52px 圆角全局栏（不是另一条自带标题栏）", async () => {
    setRouteName("interactive");
    const w = await mountChrome();
    const bar = w.find(".window-chrome");
    expect(bar.classes()).toContain("conversation-bar");
    // 只有一条窗口层、一个拖动面：互动页不需要再画第二条窗口标题栏
    expect(w.findAll(".window-chrome")).toHaveLength(1);
    expect(w.findAll("[data-tauri-drag-region]")).toHaveLength(1);
    w.unmount();
  });

  it("互动路由的品牌与对话页一致：轨道标志 + PERSONAL AGENT", async () => {
    setRouteName("interactive");
    const w = await mountChrome();
    expect(w.find(".brand-logo").exists()).toBe(true);
    expect(w.find(".brand-caption").text()).toBe("PERSONAL AGENT");
    expect(w.find(".brand-name").exists()).toBe(false);
    w.unmount();
  });

  it("互动路由有设置入口，且真的能进设置页", async () => {
    setRouteName("interactive");
    const w = await mountChrome();
    const settings = w.find('button[aria-label="设置"]');
    expect(settings.exists()).toBe(true);
    await settings.trigger("click");
    expect(shell.push).toHaveBeenCalledWith({ name: "settings" });
    w.unmount();
  });

  it("互动路由在浏览器预览里同样不画窗口按钮（只有壳里才有真按钮）", async () => {
    setShell(false);
    setRouteName("interactive");
    const w = await mountChrome();
    expect(w.find(".window-controls").exists()).toBe(false);
    expect(w.findAll(".wc-btn")).toHaveLength(0);
    w.unmount();
  });

  it("互动路由在壳里仍是三个真窗口按钮，顺序不变", async () => {
    setRouteName("interactive");
    const w = await mountChrome();
    const labels = w.findAll(".window-controls button").map((b) => b.attributes("aria-label"));
    expect(labels).toEqual(["最小化", "最大化", "关闭"]);
    w.unmount();
  });

  it("标题条高度在对话与互动之间保持一致，离开时恢复原值", async () => {
    document.documentElement.style.setProperty(TITLEBAR_PROPERTY, "56px");
    const w = await mountChrome();
    expect(document.documentElement.style.getPropertyValue(TITLEBAR_PROPERTY)).toBe("70px");

    setRouteName("interactive");
    await flushPromises();
    expect(document.documentElement.style.getPropertyValue(TITLEBAR_PROPERTY)).toBe("70px");

    setRouteName("settings");
    await flushPromises();
    expect(document.documentElement.style.getPropertyValue(TITLEBAR_PROPERTY)).toBe("56px");

    setRouteName("interactive");
    await flushPromises();
    expect(document.documentElement.style.getPropertyValue(TITLEBAR_PROPERTY)).toBe("70px");

    w.unmount();
    expect(document.documentElement.style.getPropertyValue(TITLEBAR_PROPERTY)).toBe("56px");
  });

  it("设置 / 调试等其他页面仍是原来的短标题条，不显示设置入口", async () => {
    setRouteName("settings");
    const w = await mountChrome();
    expect(w.find(".window-chrome").classes()).not.toContain("conversation-bar");
    expect(w.find('button[aria-label="设置"]').exists()).toBe(false);
    expect(document.documentElement.style.getPropertyValue(TITLEBAR_PROPERTY)).toBe("");
    w.unmount();
  });
});

describe("互动页搜索 / 保存状态挂载点", () => {
  it("互动路由渲染 #qio-interactive-chrome-extra，对话页不渲染", async () => {
    setRouteName("interactive");
    const w = await mountChrome();
    expect(w.find("#" + EXTRA_ID).exists()).toBe(true);
    w.unmount();

    setRouteName("conversation");
    const c = await mountChrome();
    expect(c.find("#" + EXTRA_ID).exists()).toBe(false);
    c.unmount();
  });

  it("挂载点位于品牌与设置之间（DOM 顺序即新的视觉顺序）", async () => {
    setRouteName("interactive");
    const w = await mountChrome();
    const brand = w.find(".brand").element;
    const extra = w.find("#" + EXTRA_ID).element;
    const settings = w.find('button[aria-label="设置"]').element;
    expect(brand.compareDocumentPosition(extra) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(extra.compareDocumentPosition(settings) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    w.unmount();
  });

  it("挂载点是拖动面的兄弟、不在拖动面内部：点它不会被拖窗口吃掉", async () => {
    setRouteName("interactive");
    const w = await mountChrome();
    const drag = w.find("[data-tauri-drag-region]").element;
    const extra = w.find("#" + EXTRA_ID).element;
    expect(extra.closest("[data-tauri-drag-region]")).toBeNull();
    expect(extra.parentElement).toBe(drag.parentElement);
    expect(drag.compareDocumentPosition(extra) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    w.unmount();
  });

  it("B 用 Teleport 注入的顶栏控件确实落在挂载点里", async () => {
    setRouteName("interactive");
    await mountChrome(document.body);

    const Probe = defineComponent({
      template: '<Teleport to="#qio-interactive-chrome-extra"><button id="probe-search" type="button">搜索</button></Teleport>',
    });
    const probe = mount(Probe, { attachTo: document.body });
    await nextTick();

    const target = document.getElementById(EXTRA_ID);
    expect(target).toBeTruthy();
    expect(target!.querySelector("#probe-search")?.textContent).toBe("搜索");

    probe.unmount();
  });

  it("挂载点样式：显式恢复命中、允许溢出、不建立 fixed 包含块、只占内容宽度", () => {
    const block = cssBlock(".chrome-extra");
    expect(block, ".chrome-extra 样式块").toBeTruthy();
    // 命中：拖动面是兄弟，挂载点自己重新打开 pointer-events，z-index 压过拖动面
    expect(block).toMatch(/pointer-events:\s*auto/);
    expect(block).toMatch(/z-index:\s*1/);
    // 搜索结果浮层不被挂载点裁切
    expect(block).toMatch(/overflow:\s*visible/);
    // 不用 transform：否则 B 的 fixed 浮层会被重新锚定到这个容器上，位置和裁切都变了
    expect(block).not.toMatch(/transform:/);
    // 只占自身内容宽度：空容器与两侧留白仍归拖动面，整条栏照样能拖
    expect(block).toMatch(/width:\s*fit-content/);
  });

  it("全局栏本身在互动路由不裁剪挂载点的溢出（浮层可越出栏外）", () => {
    expect(CHROME_SOURCE).toMatch(
      /\.conversation-bar\.interactive-bar\s*\{[^}]*overflow:\s*visible/,
    );
  });
});
