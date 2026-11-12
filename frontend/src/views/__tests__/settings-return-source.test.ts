/**
 * #48 · 设置页记录进入来源与需要恢复的位置。
 *
 * 从互动页进入设置 → 返回回互动页的原板面（含查询参数）；
 * 直达设置（深链 / 启动即设置）→ 默认返回对话页。
 * 返回目标是「进入那一刻」记录的真实来源，设置页内部导航不改变它。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import router, {
  recordSettingsEntrySource,
  resetSettingsEntrySource,
  settingsReturnPath,
} from "../../router";
import { resetFloatPositions } from "../../composables/floatingState";
import { nextTick } from "vue";
import { mountSettings, openCategory, setViewportWidth } from "../settings/__tests__/settingsTestUtils";

vi.mock("../../services/api", async () => {
  const { settingsApiModule } = await import("../settings/__tests__/settingsApiMock");
  return settingsApiModule();
});

vi.mock("../../services/modelCatalog", async () => {
  const { modelCatalogModule } = await import("../settings/__tests__/settingsApiMock");
  return modelCatalogModule();
});

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  resetFloatPositions();
  resetSettingsEntrySource();
  document.body.innerHTML = "";
  setViewportWidth(1280);
});

describe("设置来源记录（#48）", () => {
  it("直达设置没有来源：默认返回对话", () => {
    expect(settingsReturnPath()).toBe("/");
  });

  it("从互动页进入：记住原板面完整路径（含查询参数）", () => {
    recordSettingsEntrySource({ name: "interactive", fullPath: "/interactive?board=b1" });
    expect(settingsReturnPath()).toBe("/interactive?board=b1");
  });

  it("从对话页/调试页进入：没有需要恢复的原板面，仍按默认回对话", () => {
    recordSettingsEntrySource({ name: "conversation", fullPath: "/" });
    expect(settingsReturnPath()).toBe("/");
    recordSettingsEntrySource({ name: "debug", fullPath: "/debug" });
    expect(settingsReturnPath()).toBe("/");
  });

  it("来源是互动页时，返回按钮写「返回互动」并回到原板面", async () => {
    recordSettingsEntrySource({ name: "interactive", fullPath: "/interactive?board=b1" });
    const push = vi.spyOn(router, "push").mockResolvedValue(undefined as never);
    const w = await mountSettings();
    const back = w.find('[data-testid="settings-back"]');
    expect(back.attributes("aria-label")).toBe("返回互动");
    await back.trigger("click");
    await nextTick();
    expect(push).toHaveBeenCalledWith({ path: "/interactive?board=b1" });
    push.mockRestore();
    w.unmount();
  });

  it("直达设置时返回按钮写「返回对话」并回到对话页", async () => {
    const push = vi.spyOn(router, "push").mockResolvedValue(undefined as never);
    const w = await mountSettings();
    const back = w.find('[data-testid="settings-back"]');
    expect(back.attributes("aria-label")).toBe("返回对话");
    await back.trigger("click");
    await nextTick();
    expect(push).toHaveBeenCalledWith({ name: "conversation" });
    push.mockRestore();
    w.unmount();
  });

  it("设置页内部切分类/开关搜索不改变返回目标", async () => {
    recordSettingsEntrySource({ name: "interactive", fullPath: "/interactive" });
    const w = await mountSettings();
    await openCategory(w, "tools");
    await w.find('[data-testid="settings-search-entry"]').trigger("click");
    await nextTick();
    await w.find('[data-testid="settings-search-input"]').trigger("keydown", { key: "Escape" });
    await nextTick();
    expect(settingsReturnPath()).toBe("/interactive");
    expect(w.find('[data-testid="settings-back"]').attributes("aria-label")).toBe("返回互动");
    w.unmount();
  });
});
