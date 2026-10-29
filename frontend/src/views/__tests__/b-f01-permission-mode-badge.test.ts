/**
 * F01 / FU4：权限模式出现在**全局栏**（纯文字，不再是页面内胶囊徽标）。
 *
 * 两个聊天路由共用同一条全局栏：对话页 / 互动页都在同一处看到当前模式；
 * 读取中 / 失败如实显示；纯文字（没有胶囊/边框/底色）；点击进设置，不切模式。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";

const push = vi.hoisted(() => vi.fn(async () => undefined));

vi.mock("../../router", async () => {
  const { ref } = await import("vue");
  const currentRoute = ref<{ name: string }>({ name: "conversation" });
  return {
    default: { currentRoute, push },
    setRouteName: (name: string) => {
      currentRoute.value = { name };
    },
  };
});

import * as routerModule from "../../router";
import WindowChrome from "../../components/WindowChrome.vue";
import { useSecurityStore } from "../../stores/security";
import { useUiStore } from "../../stores/ui";

let wrapper: VueWrapper | null = null;

function setRouteName(name: string) {
  (routerModule as unknown as { setRouteName: (n: string) => void }).setRouteName(name);
}

function mountChrome() {
  const pinia = createPinia();
  setActivePinia(pinia);
  wrapper = mount(WindowChrome, { global: { plugins: [pinia] } });
  return wrapper;
}

beforeEach(() => {
  push.mockClear();
  setRouteName("conversation");
});

afterEach(() => {
  wrapper?.unmount();
  wrapper = null;
  document.body.innerHTML = "";
});

describe("权限模式在全局栏（两个聊天路由同一处）", () => {
  it("对话页：全局栏显示当前模式，且不再渲染页面内胶囊徽标", async () => {
    const w = mountChrome();
    const security = useSecurityStore();
    security.applyMode({ mode: "restricted", label: "受限模式" });
    await flushPromises();
    expect(w.find('[data-im="permission-mode-text"]').exists()).toBe(true);
    expect(w.text()).toContain("受限模式");
    expect(w.find('[data-im="permission-mode-badge"]').exists()).toBe(false);
    expect(w.get('[data-im="permission-mode-text"]').classes()).not.toContain("perm-badge");
  });

  it("互动页：同一条全局栏同样显示（不是页面内徽标）", async () => {
    setRouteName("interactive");
    const w = mountChrome();
    const security = useSecurityStore();
    security.applyMode({ mode: "read_only", label: "只读模式" });
    await flushPromises();
    expect(w.text()).toContain("只读");
    expect(w.find('[data-im="permission-mode-badge"]').exists()).toBe(false);
  });

  it("读取中 / 读取失败如实显示", async () => {
    const w = mountChrome();
    const security = useSecurityStore();
    await flushPromises();
    expect(w.text()).toContain("权限模式读取中");
    security.error = "读取权限模式失败：HTTP 500";
    await flushPromises();
    expect(w.text()).toContain("权限模式未知");
  });

  it("点击文字进入设置的工具与权限（本身不切换模式）", async () => {
    const w = mountChrome();
    const security = useSecurityStore();
    security.applyMode({ mode: "restricted", label: "受限模式" });
    const ui = useUiStore();
    await w.get('[data-im="permission-mode-text"]').trigger("click");
    await flushPromises();
    expect(ui.settingsSection).toBe("tools");
    expect(push).toHaveBeenCalledWith("/settings");
    expect(security.mode).toBe("restricted");
  });
});
