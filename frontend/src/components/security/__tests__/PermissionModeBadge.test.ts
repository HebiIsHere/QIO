/**
 * 权限模式常驻徽标（R9 契约 §1：当前模式必须可见）—— 定向测试。
 */
import { beforeEach, describe, expect, it } from "vitest";
import { mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import PermissionModeBadge from "../PermissionModeBadge.vue";
import { useSecurityStore } from "../../../stores/security";
import { useUiStore } from "../../../stores/ui";

function mountBadge() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return mount(PermissionModeBadge, { global: { plugins: [pinia] } });
}

describe("权限模式徽标", () => {
  it("没读到真实模式时不冒充默认值：显示「读取中」", () => {
    const w = mountBadge();
    const badge = w.get('[data-im="permission-mode-badge"]');
    expect(badge.text()).toContain("读取中");
    expect(badge.attributes("data-loaded")).toBe("0");
    w.unmount();
  });

  it("读到完全无限制模式：常驻显示模式名（颜色之外的文字通道）", () => {
    const w = mountBadge();
    const store = useSecurityStore();
    store.applyMode({ mode: "unrestricted", label: "完全无限制模式", legacyValue: "bypass" });
    return w.vm.$nextTick().then(() => {
      const badge = w.get('[data-im="permission-mode-badge"]');
      expect(badge.text()).toBe("完全无限制模式");
      expect(badge.attributes("data-mode")).toBe("unrestricted");
      expect(badge.attributes("data-loaded")).toBe("1");
      expect(badge.attributes("title")).toContain("当前权限模式");
      w.unmount();
    });
  });

  it("点击进入设置 →「工具与权限」（徽标本身不切模式）", async () => {
    const w = mountBadge();
    const ui = useUiStore();
    await w.get('[data-im="permission-mode-badge"]').trigger("click");
    expect(ui.settingsSection).toBe("tools");
    w.unmount();
  });
});
