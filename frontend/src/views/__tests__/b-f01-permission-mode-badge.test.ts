/**
 * B / F01：互动页的权限模式常驻展示（页面级定向测试）。
 *
 * 用户可见契约（任务书 F01 / COMMON §4.2）：
 * - 互动页 v2 布局里必须能看到**当前权限模式**，对话收起 / 展开都留在原位；
 * - 显示的是服务端恢复的真实模式：读取中不冒充默认值，读取失败如实说未知并可进入设置；
 * - 徽标与对话页共用同一个 security store（唯一状态源），不建立第二份模式状态；
 * - 位置：互动对话窗的常驻状态条（收起 / 展开都在，且位于 InteractiveView 的组件树里，
 *   不经过 Teleport —— E 的页面级验收按 wrapper.findAll 取证）；在输入区与工具栏上方，
 *   不在消息面板 / 输入区 / 工具栏内部，也不新增覆盖板面的浮动入口。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  getSecurityMode: vi.fn(),
  setSecurityMode: vi.fn(),
}));

vi.mock("../../services/security", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../services/security")>();
  return {
    ...actual,
    getSecurityMode: mocks.getSecurityMode,
    setSecurityMode: mocks.setSecurityMode,
  };
});

import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import InteractiveView from "../../views/InteractiveView.vue";
import { resetServices, settlePage, stubServices } from "./acceptance-i2v2-page-harness";
import { useSecurityStore } from "../../stores/security";
import { useUiStore } from "../../stores/ui";
import { useInteractiveStore } from "../../stores/interactive";

const STUBS = {
  BoardCanvas: { template: '<div data-stub="canvas"><slot name="overlay" :scale="1" :width="1200" :height="800" /></div>' },
  BoardToolbar: { template: '<div data-stub="toolbar" />' },
  ConversationModeSwitch: { template: '<div data-stub="mode-switch" />' },
  MessageStream: { template: '<div data-stub="stream" />' },
  Composer: { template: '<div data-stub="composer" />' },
} as const;

let wrapper: VueWrapper | null = null;

function mountPage(): VueWrapper {
  const pinia = createPinia();
  setActivePinia(pinia);
  wrapper = mount(InteractiveView, {
    attachTo: document.body,
    global: { plugins: [pinia], stubs: STUBS as never },
  }) as unknown as VueWrapper;
  return wrapper;
}

/** 徽标在 Teleport 目标里（页面全局栏），不在组件 wrapper 的根 DOM 里。 */
function badge(): HTMLElement | null {
  return document.querySelector<HTMLElement>('[data-im="permission-mode-badge"]');
}

function badgeVisible(): boolean {
  const el = badge();
  if (!el) return false;
  const style = getComputedStyle(el);
  return style.display !== "none" && style.visibility !== "hidden";
}

beforeEach(() => {
  resetServices();
  mocks.getSecurityMode.mockReset();
  mocks.setSecurityMode.mockReset();
  mocks.getSecurityMode.mockResolvedValue({
    mode: "read_only",
    label: "只读模式",
    legacyValue: "plan",
    compat: false,
  });
  document.body.innerHTML = '<div id="qio-interactive-chrome-extra"></div>';
});

afterEach(() => {
  try {
    wrapper?.unmount();
  } catch {
    /* 已经卸载 */
  }
  wrapper = null;
  resetServices();
  document.body.innerHTML = "";
});

describe("互动页权限模式常驻可见", () => {
  it("互动页常驻状态条里挂了权限模式徽标：读取中如实显示，不冒充默认模式", async () => {
    stubServices();
    mountPage();
    await settlePage(wrapper!);
    const el = badge();
    expect(el, "互动页没有渲染权限模式徽标").not.toBeNull();
    expect(el!.closest('[data-ic-bar]'), "徽标不在常驻对话状态条里（收起态会看不到）").not.toBeNull();
    expect(el!.dataset.loaded).toBe("0");
    expect(el!.textContent).toContain("读取中");
    expect(el!.textContent).not.toContain("受限模式");
  });

  it("读取到真实模式后显示服务端模式名；对话收起 / 展开都在，且不建立第二份状态", async () => {
    stubServices();
    mountPage();
    await settlePage(wrapper!);
    const security = useSecurityStore();
    await security.load();
    await flushPromises();

    expect(badge()!.dataset.loaded).toBe("1");
    expect(badge()!.dataset.mode).toBe("read_only");
    expect(badge()!.textContent).toBe("只读模式");
    expect(badgeVisible()).toBe(true);

    // 展开互动对话（store.chatOpen 是真实开合状态）
    const store = useInteractiveStore();
    store.chatOpen = true;
    await settlePage(wrapper!);
    expect(badge()!.textContent).toBe("只读模式");
    expect(badgeVisible()).toBe(true);

    store.chatOpen = false;
    await settlePage(wrapper!);
    expect(badge()!.textContent).toBe("只读模式");

    // 唯一状态源：store 变化直接反映到页面徽标（没有第二份模式状态）
    security.applyMode({ mode: "unrestricted", label: "完全无限制模式", legacyValue: "bypass" });
    await flushPromises();
    expect(badge()!.textContent).toBe("完全无限制模式");
    expect(badge()!.dataset.mode).toBe("unrestricted");
  });

  it("读取失败如实显示未知 + 原因入口（点击进入设置），不显示成已读取的默认模式", async () => {
    mocks.getSecurityMode.mockRejectedValue(new Error("后端不可达"));
    stubServices();
    mountPage();
    await settlePage(wrapper!);
    const security = useSecurityStore();
    await security.load();
    await flushPromises();

    const el = badge();
    expect(el!.dataset.loaded).toBe("0");
    expect(el!.textContent).toBe("权限模式未知");
    expect(el!.getAttribute("title")).toContain("后端不可达");

    el!.click();
    await flushPromises();
    expect(useUiStore().settingsSection).toBe("tools");
  });

  it("徽标在页面组件树里（E 的页面级 findAll 口径），且不在消息面板 / 输入区 / 工具栏内部", async () => {
    stubServices();
    mountPage();
    await settlePage(wrapper!);
    const store = useInteractiveStore();
    // 收起与展开都在同一份页面组件树上（不是 Teleport 到别处的元素）
    await settlePage(wrapper!);
    expect(wrapper!.findAll('[data-im="permission-mode-badge"]')).toHaveLength(1);
    store.chatOpen = true;
    await settlePage(wrapper!);
    expect(wrapper!.findAll('[data-im="permission-mode-badge"]')).toHaveLength(1);
    store.chatOpen = false;
    await settlePage(wrapper!);
    expect(wrapper!.findAll('[data-im="permission-mode-badge"]')).toHaveLength(1);

    const el = badge()!;
    expect(el.closest('[data-ic-bar]')).not.toBeNull();
    expect(el.closest('[data-ic-stream]')).toBeNull();
    expect(el.closest(".ic-composer")).toBeNull();
    expect(el.closest('[data-ic-toolbar]')).toBeNull();
    expect(el.closest('[data-im="board-toolbar"]')).toBeNull();
  });
});
