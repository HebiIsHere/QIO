/**
 * B / F01：互动页的权限模式常驻展示（页面级定向测试）。
 *
 * 用户可见契约（任务书 F01 / COMMON §4.2）：
 * - 互动页 v2 布局里必须能看到**当前权限模式**，对话收起 / 展开都留在原位；
 * - 显示的是服务端恢复的真实模式：读取中不冒充默认值，读取失败如实说未知并可进入设置；
 * - 徽标与对话页共用同一个 security store（唯一状态源），不建立第二份模式状态；
 * - 徽标不压输入区 / 工具栏（结构上位于板面 HUD 的状态组，真实遮挡由 480x600 界面核查证明）。
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

import { flushPromises } from "@vue/test-utils";
import { mountInteractivePage, resetServices, settlePage, stubServices, type PageHarness } from "./acceptance-i2v2-page-harness";
import { useSecurityStore } from "../../stores/security";
import { useUiStore } from "../../stores/ui";

let harness: PageHarness | null = null;

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
});

afterEach(() => {
  try {
    harness?.wrapper.unmount();
  } catch {
    /* 已经卸载 */
  }
  harness = null;
  resetServices();
  document.body.innerHTML = "";
});

function badgeOf() {
  const wrapper = harness!.wrapper;
  return wrapper.get('[data-im="permission-mode-badge"]');
}

describe("互动页权限模式常驻可见", () => {
  it("互动页确实挂了权限模式徽标：读取中如实显示，不冒充默认模式", async () => {
    stubServices();
    harness = mountInteractivePage();
    await settlePage(harness.wrapper);
    const badge = badgeOf();
    expect(badge.attributes("data-loaded")).toBe("0");
    expect(badge.text()).toContain("读取中");
    // 不是冒充「受限模式」（默认值）
    expect(badge.text()).not.toContain("受限模式");
  });

  it("读取到真实模式后显示服务端模式名；对话收起 / 展开都在，且不建立第二份状态", async () => {
    stubServices();
    harness = mountInteractivePage();
    await settlePage(harness.wrapper);
    const security = useSecurityStore();
    await security.load();
    await flushPromises();

    let badge = badgeOf();
    expect(badge.attributes("data-loaded")).toBe("1");
    expect(badge.attributes("data-mode")).toBe("read_only");
    expect(badge.text()).toBe("只读模式");

    // 对话展开（store.chatOpen 是互动对话的真实开合状态）
    harness.store.chatOpen = true;
    await settlePage(harness.wrapper);
    badge = badgeOf();
    expect(badge.text()).toBe("只读模式");
    expect(badge.isVisible()).toBe(true);

    // 收起
    harness.store.chatOpen = false;
    await settlePage(harness.wrapper);
    expect(badgeOf().text()).toBe("只读模式");

    // 唯一状态源：store 变化直接反映到页面徽标（没有第二份模式状态）
    security.applyMode({ mode: "unrestricted", label: "完全无限制模式", legacyValue: "bypass" });
    await flushPromises();
    expect(badgeOf().text()).toBe("完全无限制模式");
    expect(badgeOf().attributes("data-mode")).toBe("unrestricted");
  });

  it("读取失败如实显示未知 + 原因入口（点击进入设置），不显示成已读取的默认模式", async () => {
    mocks.getSecurityMode.mockRejectedValue(new Error("后端不可达"));
    stubServices();
    harness = mountInteractivePage();
    await settlePage(harness.wrapper);
    const security = useSecurityStore();
    await security.load();
    await flushPromises();

    const badge = badgeOf();
    expect(badge.attributes("data-loaded")).toBe("0");
    expect(badge.text()).toBe("权限模式未知");
    expect(badge.attributes("title")).toContain("后端不可达");

    await badge.trigger("click");
    expect(useUiStore().settingsSection).toBe("tools");
  });

  it("权限模式徽标位于板面 HUD 状态组，不在输入区 / 工具栏里面（结构上不压输入区）", async () => {
    stubServices();
    harness = mountInteractivePage();
    await settlePage(harness.wrapper);
    const wrapper = harness.wrapper;
    const hud = wrapper.get('[data-im="interactive-stage"] .im-board-hud .im-hud-right');
    expect(hud.find('[data-im="permission-mode-badge"]').exists()).toBe(true);
    // 输入区与工具栏都只是替身/固定结构，里面没有第二枚徽标
    expect(wrapper.findAll('[data-stub="composer"] [data-im="permission-mode-badge"]')).toHaveLength(0);
    expect(wrapper.findAll('[data-stub="toolbar"] [data-im="permission-mode-badge"]')).toHaveLength(0);
  });
});
