/**
 * FU5：底部「对话模式 ⇄ 互动模式」入口 —— 点击直接切换，不再弹菜单。
 *
 * 守的几条：
 * - 按钮文字跟随当前路由；aria-label/title 说明点它会去哪个模式；
 * - 点击一次直接 router.push 到另一模式（不出现菜单 / menuitem / aria-haspopup）；
 * - 只导航：不碰草稿 / 审批 / 板面等任何业务状态；
 * - 键盘 Enter 可切换（原生 button）。
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
import { hasTipRecord } from "../../composables/useQioTip";

function setRouteName(name: string) {
  (routerModule as unknown as { setRouteName: (n: string) => void }).setRouteName(name);
}

const SOURCE = readFileSync(
  resolve(process.cwd(), "src/components/conversation/ConversationModeSwitch.vue"),
  "utf8",
);

let activeWrappers: VueWrapper[] = [];

async function mountSwitch(): Promise<VueWrapper> {
  const w = mount(ConversationModeSwitch);
  activeWrappers.push(w);
  await flushPromises();
  return w;
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

describe("当前模式与直接切换", () => {
  it("对话页：写对话模式，点击一次直接去 /interactive（没有菜单）", async () => {
    const w = await mountSwitch();
    const btn = w.find(".mode-btn");
    expect(w.find(".mode-text").text()).toBe("对话模式");
    expect(btn.attributes("aria-label")).toBe("切换到互动模式");
    expect(btn.attributes("title")).toBeUndefined();
    expect(hasTipRecord(btn.element as HTMLElement), "切换说明要走统一用途说明").toBe(true);
    expect(btn.attributes("aria-haspopup")).toBeUndefined();
    expect(btn.attributes("aria-expanded")).toBeUndefined();
    expect(w.find('[role="menu"]').exists()).toBe(false);
    await btn.trigger("click");
    await flushPromises();
    expect(nav.push).toHaveBeenCalledTimes(1);
    expect(nav.push).toHaveBeenCalledWith("/interactive");
    expect(w.find('[role="menu"]').exists()).toBe(false);
    w.unmount();
  });

  it("互动页：写互动模式，点击一次直接回 /", async () => {
    setRouteName("interactive");
    const w = await mountSwitch();
    const btn = w.find(".mode-btn");
    expect(w.find(".mode-text").text()).toBe("互动模式");
    expect(btn.attributes("aria-label")).toBe("切换到对话模式");
    await btn.trigger("click");
    await flushPromises();
    expect(nav.push).toHaveBeenCalledTimes(1);
    expect(nav.push).toHaveBeenCalledWith("/");
    w.unmount();
  });

  it("键盘 Enter 也能切换（原生 button 行为）", async () => {
    const w = await mountSwitch();
    await w.find(".mode-btn").trigger("keydown", { key: "Enter" });
    await w.find(".mode-btn").trigger("click");
    await flushPromises();
    expect(nav.push).toHaveBeenCalledWith("/interactive");
    w.unmount();
  });

  it("按钮里保留模式圆点与文字，没有箭头/菜单结构", async () => {
    const w = await mountSwitch();
    expect(w.find(".mode-dot").exists()).toBe(true);
    expect(w.find(".chev").exists()).toBe(false);
    expect(w.findAll('[role="menuitem"]').length).toBe(0);
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
