/**
 * 权限模式设置段（R9 契约 §1）—— 定向测试。
 *
 * 覆盖：三模式都在、当前模式与旧值映射展示、只有点击才切换（PUT 只由用户操作触发）、
 * 切换后保留「新操作立即生效 / 已开始执行沿用开始权限」的边界说明、失败不假装已切换。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";

const mocks = vi.hoisted(() => ({
  getSecurityMode: vi.fn(),
  setSecurityMode: vi.fn(),
}));

vi.mock("../../../services/security", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../services/security")>();
  return {
    ...actual,
    getSecurityMode: mocks.getSecurityMode,
    setSecurityMode: mocks.setSecurityMode,
  };
});

import PermissionModeSettings from "../PermissionModeSettings.vue";
import { useSecurityStore } from "../../../stores/security";

function makePinia() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return pinia;
}

function mountSettings() {
  return mount(PermissionModeSettings, { global: { plugins: [makePinia()] } });
}

beforeEach(() => {
  mocks.getSecurityMode.mockReset();
  mocks.setSecurityMode.mockReset();
});

describe("三模式选择与旧值映射展示", () => {
  it("三个模式全部列出；当前模式与旧值映射都能读到", async () => {
    const w = mountSettings();
    const store = useSecurityStore();
    store.applyMode({ mode: "restricted", label: "受限模式", legacyValue: "default" });
    await flushPromises();

    const options = w.findAll('[data-im="permission-mode-options"] [role="radio"]');
    expect(options.map((o) => o.text())).toHaveLength(3);
    expect(w.text()).toContain("只读模式");
    expect(w.text()).toContain("受限模式");
    expect(w.text()).toContain("完全无限制模式");

    expect(w.get('[data-im="permission-mode-current"]').text()).toContain("受限模式");
    const legacy = w.get('[data-im="permission-mode-legacy"]').text();
    expect(legacy).toContain("default");
    expect(legacy).toContain("受限模式");
    expect(options[1].attributes("aria-checked")).toBe("true");
    w.unmount();
  });

  it("旧接口兼容读取时说明「读取的是旧设置接口的值」并给出映射", async () => {
    const w = mountSettings();
    const store = useSecurityStore();
    store.applyMode({
      mode: "restricted",
      label: "受限模式",
      legacyValue: "accept-edits",
      compat: true,
    });
    await flushPromises();
    const legacy = w.get('[data-im="permission-mode-legacy"]').text();
    expect(legacy).toContain("旧设置接口");
    expect(legacy).toContain("accept-edits");
    expect(w.text()).toContain("兼容旧接口");
    w.unmount();
  });
});

describe("只有用户操作才能切换（PUT user_confirmed=true）", () => {
  it("点击另一个模式才调用 setSecurityMode；点击当前模式不发请求", async () => {
    mocks.setSecurityMode.mockResolvedValue({
      mode: "read_only",
      label: "只读模式",
      legacyValue: "plan",
      compat: false,
    });
    const w = mountSettings();
    const store = useSecurityStore();
    store.applyMode({ mode: "restricted", label: "受限模式", legacyValue: "default" });
    await flushPromises();

    await w.get('[data-mode="restricted"]').trigger("click");
    expect(mocks.setSecurityMode).not.toHaveBeenCalled();

    await w.get('[data-mode="read_only"]').trigger("click");
    await flushPromises();
    expect(mocks.setSecurityMode).toHaveBeenCalledTimes(1);
    expect(mocks.setSecurityMode).toHaveBeenCalledWith("read_only");
    expect(store.mode).toBe("read_only");
    // 切换后仍要看见「新操作立即生效 / 已开始的执行沿用开始权限」的边界
    const note = w.get('[data-im="permission-mode-switch-note"]').text();
    expect(note).toContain("新操作立即生效");
    expect(note).toContain("已经开始的执行沿用开始时的权限");
    w.unmount();
  });

  it("切换失败：保留原模式并显示原因（不假装已切换）", async () => {
    mocks.setSecurityMode.mockRejectedValue(new Error("PUT /api/security/mode -> 400：缺少用户确认"));
    const w = mountSettings();
    const store = useSecurityStore();
    store.applyMode({ mode: "restricted", label: "受限模式", legacyValue: "default" });
    await flushPromises();

    await w.get('[data-mode="unrestricted"]').trigger("click");
    await flushPromises();
    expect(store.mode).toBe("restricted");
    expect(w.get('[data-im="permission-mode-error"]').text()).toContain("当前模式未改变");
    w.unmount();
  });
});
