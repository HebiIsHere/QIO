/**
 * Phase 1-A（互动页 v2）：对话 ⇄ 互动 双向模式切换（契约 §2.2）。
 *
 * 守的几条：
 * - 按钮文字 / aria-label 随当前路由模式变化，不再写死「对话模式」；
 * - 菜单两项都是可导航的真实按钮：对话页能去互动页，互动页也能回对话页；
 * - 当前项有 aria-current 与「当前」标记，非当前项没有；
 * - 切换只导航：不重复导航当前页，也不碰草稿 / 审批 / 板面等任何业务状态；
 * - 现有视觉与 SVG 图标保持。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const nav = vi.hoisted(() => ({ push: vi.fn(async () => undefined) }));

vi.mock("vue-router", async () => {
  const { ref } = await import("vue");
  const currentRoute = ref<{ name: string }>({ name: "conversation" });
  return {
    useRouter: () => ({ currentRoute, push: nav.push }),
    setRouteName: (name: string) => {
      currentRoute.value = { name };
    },
  };
});

import * as routerModule from "vue-router";
import ConversationModeSwitch from "../conversation/ConversationModeSwitch.vue";

function setRouteName(name: string) {
  (routerModule as unknown as { setRouteName: (name: string) => void }).setRouteName(name);
}

const SOURCE = readFileSync(
  resolve(process.cwd(), "src/components/conversation/ConversationModeSwitch.vue"),
  "utf8",
);

/** 挂载过的组件一律在 afterEach 卸载：失败用例残留的 document 监听会串到后面的用例 */
let activeWrappers: VueWrapper[] = [];

async function mountSwitch(): Promise<VueWrapper> {
  const w = mount(ConversationModeSwitch);
  activeWrappers.push(w);
  await flushPromises();
  return w;
}

async function openMenu(w: VueWrapper) {
  await w.find(".mode-btn").trigger("click");
  await flushPromises();
}

function menuItems(w: VueWrapper) {
  return w.findAll('[role="menuitem"]');
}

beforeEach(() => {
  vi.clearAllMocks();
  setRouteName("conversation");
});

afterEach(() => {
  for (const w of activeWrappers) {
    try {
      w.unmount();
    } catch {
      // 已经卸载过就跳过
    }
  }
  activeWrappers = [];
  setRouteName("conversation");
});

describe("当前模式显示", () => {
  it("对话页：按钮写对话模式，aria-label 说当前是对话模式", async () => {
    const w = await mountSwitch();
    expect(w.find(".mode-text").text()).toBe("对话模式");
    const btn = w.find(".mode-btn");
    expect(btn.attributes("aria-label")).toBe("切换页面模式（当前为对话模式）");
    expect(btn.attributes("aria-haspopup")).toBe("menu");
    expect(btn.attributes("aria-expanded")).toBe("false");
    // 现有 SVG 图标保持
    expect(w.find(".chev svg").exists()).toBe(true);
    w.unmount();
  });

  it("互动页：按钮写互动模式，aria-label 跟着变", async () => {
    setRouteName("interactive");
    const w = await mountSwitch();
    expect(w.find(".mode-text").text()).toBe("互动模式");
    expect(w.find(".mode-btn").attributes("aria-label")).toBe("切换页面模式（当前为互动模式）");
    w.unmount();
  });
});

describe("菜单双向导航", () => {
  it("对话页菜单：对话项是当前项，互动项是可导航按钮", async () => {
    const w = await mountSwitch();
    await openMenu(w);

    expect(w.find(".mode-btn").attributes("aria-expanded")).toBe("true");
    const items = menuItems(w);
    expect(items).toHaveLength(2);
    // 两项都是真实按钮：当前项也从「只读标签」改成可点的导航项
    expect(items.map((i) => i.element.tagName)).toEqual(["BUTTON", "BUTTON"]);

    const [conversation, interactive] = items;
    expect(conversation.find(".item-name").text()).toBe("对话模式");
    expect(conversation.attributes("aria-current")).toBe("true");
    expect(conversation.find(".item-note").text()).toBe("当前");

    expect(interactive.find(".item-name").text()).toBe("互动模式");
    expect(interactive.attributes("aria-current")).toBeUndefined();
    // 现有说明文字保持
    expect(interactive.find(".item-note").text()).toBe("一起整理材料与关系");
    w.unmount();
  });

  it("对话页点互动项：导航到 /interactive，菜单收起，只导航一次", async () => {
    const w = await mountSwitch();
    await openMenu(w);
    await menuItems(w)[1].trigger("click");
    await flushPromises();

    expect(nav.push).toHaveBeenCalledTimes(1);
    expect(nav.push).toHaveBeenCalledWith("/interactive");
    expect(w.find('[role="menu"]').exists()).toBe(false);
    w.unmount();
  });

  it("互动页菜单：互动项是当前项，对话项可导航回 /", async () => {
    setRouteName("interactive");
    const w = await mountSwitch();
    await openMenu(w);

    const [conversation, interactive] = menuItems(w);
    expect(interactive.find(".item-name").text()).toBe("互动模式");
    expect(interactive.attributes("aria-current")).toBe("true");
    expect(interactive.find(".item-note").text()).toBe("当前");
    expect(conversation.attributes("aria-current")).toBeUndefined();
    // 非当前项也要有说明，不能只剩一个光秃秃的标签
    expect(conversation.find(".item-note").text()).not.toBe("");

    await conversation.trigger("click");
    await flushPromises();
    expect(nav.push).toHaveBeenCalledWith("/");
    expect(w.find('[role="menu"]').exists()).toBe(false);
    w.unmount();
  });

  it("点当前项不重复导航（同一地址不产生重复历史），但菜单收起", async () => {
    const w = await mountSwitch();
    await openMenu(w);
    await menuItems(w)[0].trigger("click");
    await flushPromises();
    expect(nav.push).not.toHaveBeenCalled();
    expect(w.find('[role="menu"]').exists()).toBe(false);

    setRouteName("interactive");
    await flushPromises();
    await openMenu(w);
    await menuItems(w)[1].trigger("click");
    await flushPromises();
    expect(nav.push).not.toHaveBeenCalled();
    w.unmount();
  });

  it("Esc 收起菜单，不导航", async () => {
    const w = await mountSwitch();
    await openMenu(w);
    await w.find(".mode-switch").trigger("keydown", { key: "Escape" });
    await flushPromises();
    expect(w.find('[role="menu"]').exists()).toBe(false);
    expect(nav.push).not.toHaveBeenCalled();
    w.unmount();
  });
});

describe("切换只导航，不碰业务状态", () => {
  it("组件不引入任何 store / service / 本机存储：草稿、附件、审批、板面状态都不由它清空", () => {
    expect(SOURCE).not.toMatch(/from\s+["'][^"']*(?:stores|services|interactive)[^"']*["']/);
    expect(SOURCE).not.toMatch(/localStorage|sessionStorage|indexedDB/);
    expect(SOURCE).not.toMatch(/fetch\(/);
  });
});
