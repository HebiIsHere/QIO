import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useOnboardingStore } from "../onboarding";
import { api } from "../../services/api";

const baseStatus = {
  done: false,
  has_credential: false,
  has_name: false,
  wizard_seen: false,
  welcome_version: "",
  app_version: "0.1.6",
  show_wizard: true,
  hint_dismissed: false,
};

describe("onboarding store", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.restoreAllMocks();
  });

  it("load() 拉状态并暴露 showWizard", async () => {
    vi.spyOn(api, "getOnboardingStatus").mockResolvedValue({ ...baseStatus });
    const store = useOnboardingStore();
    await store.load();
    expect(store.showWizard).toBe(true);
    expect(store.needsSetup).toBe(true);
  });

  it("markSeen() 用后端返回覆盖本地状态（版本更新后再关掉）", async () => {
    vi.spyOn(api, "getOnboardingStatus").mockResolvedValue({ ...baseStatus });
    vi.spyOn(api, "markOnboardingSeen").mockResolvedValue({
      ...baseStatus,
      wizard_seen: true,
      welcome_version: "0.1.6",
      show_wizard: false,
    });
    const store = useOnboardingStore();
    await store.load();
    await store.markSeen();
    expect(store.showWizard).toBe(false);
  });

  it("saveProfile() 把 payload 原样交给 API，失败时落下 error", async () => {
    const spy = vi.spyOn(api, "saveOnboardingProfile").mockRejectedValue(new Error("boom"));
    const store = useOnboardingStore();
    await expect(store.saveProfile({ name: "小舟" })).rejects.toThrow("boom");
    expect(spy).toHaveBeenCalledWith({ name: "小舟" });
    expect(store.error).toBeTruthy();
  });

  it("setHintDismissed() 更新提示条状态", async () => {
    vi.spyOn(api, "setOnboardingHint").mockResolvedValue({
      ...baseStatus,
      hint_dismissed: true,
    });
    const store = useOnboardingStore();
    await store.setHintDismissed(true);
    expect(store.status?.hint_dismissed).toBe(true);
  });
});
