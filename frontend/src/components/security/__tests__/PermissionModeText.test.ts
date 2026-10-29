/**
 * 权限模式文字（全局栏）单元测试：纯文字、不冒充、点击进设置。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";

const push = vi.hoisted(() => vi.fn(async () => undefined));
vi.mock("../../../router", () => ({ default: { push } }));

import PermissionModeText from "../PermissionModeText.vue";
import { useSecurityStore } from "../../../stores/security";
import { useUiStore } from "../../../stores/ui";

function mountIt() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return mount(PermissionModeText, { global: { plugins: [pinia] } });
}

beforeEach(() => {
  push.mockClear();
});

describe("PermissionModeText 全局栏文字", () => {
  it("加载后显示服务端标签，且是纯文字（没有胶囊/徽标）", () => {
    const w = mountIt();
    const security = useSecurityStore();
    security.applyMode({ mode: "restricted", label: "受限模式" });
    return flushPromises().then(() => {
      const el = w.get('[data-im="permission-mode-text"]');
      expect(el.text()).toBe("受限模式");
      expect(el.attributes("data-mode")).toBe("restricted");
      expect(el.attributes("data-loaded")).toBe("1");
      expect(el.classes()).not.toContain("perm-badge");
      expect(w.find('[data-im="permission-mode-badge"]').exists()).toBe(false);
    });
  });

  it("读取中 / 失败如实显示，不冒充默认模式", async () => {
    const w = mountIt();
    const security = useSecurityStore();
    await flushPromises();
    expect(w.text()).toContain("权限模式读取中");
    security.error = "读取权限模式失败：HTTP 500";
    await flushPromises();
    expect(w.text()).toBe("权限模式未知");
  });

  it("点击进入设置→工具与权限（本身不改模式）", async () => {
    const w = mountIt();
    const security = useSecurityStore();
    security.applyMode({ mode: "read_only", label: "只读模式" });
    const ui = useUiStore();
    await w.get('[data-im="permission-mode-text"]').trigger("click");
    await flushPromises();
    expect(ui.settingsSection).toBe("tools");
    expect(push).toHaveBeenCalledWith("/settings");
    expect(security.mode).toBe("read_only"); // 点击不会切换模式
  });
});
