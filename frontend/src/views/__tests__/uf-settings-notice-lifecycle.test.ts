/**
 * 设置页反馈生命周期（改版后）：
 * 成功提示约 4 秒自动收回、失败持久到下次操作；不出现全局顶部通知。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises } from "@vue/test-utils";
import { nextTick } from "vue";
import { resetFloatPositions } from "../../composables/floatingState";
import {
  chooseSelectLabel,
  mountSettings,
  setViewportWidth,
} from "../settings/__tests__/settingsTestUtils";

vi.mock("../../services/api", async () => {
  const { settingsApiModule } = await import("../settings/__tests__/settingsApiMock");
  return settingsApiModule();
});

// 「模型与服务」是真实面板（不是占位）：数据源换成确定性替身。
vi.mock("../../services/modelCatalog", async () => {
  const { modelCatalogModule } = await import("../settings/__tests__/settingsApiMock");
  return modelCatalogModule();
});

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  document.body.innerHTML = "";
  document.documentElement.removeAttribute("data-motion");
  document.documentElement.removeAttribute("data-scrollbar");
  resetFloatPositions();
  setViewportWidth(1280);
});

function activeSection(w: Awaited<ReturnType<typeof mountSettings>>): HTMLElement {
  const sections = w.findAll("[data-settings-section]");
  const shown = sections.find(
    (section) => (section.element as HTMLElement).style.display !== "none",
  );
  return shown!.element as HTMLElement;
}

describe("设置页反馈生命周期", () => {
  it("成功提示约 4 秒后自动收回（成功不必长期占位）", async () => {
    const w = await mountSettings();
    vi.useFakeTimers();
    // 假计时器下不能用会 flushPromises 的辅助函数（flushPromises 自己依赖真实定时器）
    // 选一个与当前偏好不同的档位（默认就是「系统」，点它不会产生切换/提示）
    const dark = w.findAll(".set-seg__opt").find((option) => option.text() === "暗紫晶");
    expect(dark).toBeTruthy();
    await dark!.trigger("click");
    await nextTick();
    const notice = activeSection(w).querySelector(".set-notice");
    expect(notice).not.toBeNull();
    expect(notice!.className).toContain("ok");
    vi.advanceTimersByTime(4100);
    await nextTick();
    expect(activeSection(w).querySelector(".set-notice")).toBeNull();
    vi.useRealTimers();
    w.unmount();
  });

  it("失败提示持久保留（比成功更久），不随计时自动消失", async () => {
    const { api } = await import("../../services/api");
    (api.updateCloseBehavior as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
      new Error("后端 500"),
    );
    const w = await mountSettings();
    const regular = w.findAll('[data-settings-section="regular"]')[0];
    await chooseSelectLabel(regular, "最小化到托盘");
    await flushPromises();

    const notice = activeSection(w).querySelector(".set-notice");
    expect(notice).not.toBeNull();
    expect(notice!.className).toContain("err");
    expect(notice!.textContent).toContain("保存关闭行为失败");

    vi.useFakeTimers();
    vi.advanceTimersByTime(10_000);
    await nextTick();
    expect(activeSection(w).querySelector(".set-notice")!.className).toContain("err");
    vi.useRealTimers();
    w.unmount();
  });
});
