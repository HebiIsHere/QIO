/**
 * 首次引导（欢迎页）状态。
 *
 * `show_wizard` 由后端判定：真正首次启动，或「本版本还没展示过欢迎页」
 * —— 刚更新到这个版本的用户会被强制展开一次（追加规则 2026-09-23）。
 * 向导一旦打开就调 `markSeen()`，所以「无论如何」也只展开这一次。
 */
import { defineStore } from "pinia";
import { api, type OnboardingProfilePayload, type OnboardingStatus } from "../services/api";

export const useOnboardingStore = defineStore("onboarding", {
  state: () => ({
    status: null as OnboardingStatus | null,
    loaded: false,
    saving: false,
    error: "",
    /** 本轮会话内用户手动关掉向导后的兜底（后端 seen 已经记过，这里只保证 UI 立刻消失） */
    dismissed: false,
  }),
  getters: {
    showWizard: (state): boolean => !state.dismissed && Boolean(state.status?.show_wizard),
    needsSetup: (state): boolean => !state.status?.done,
    hintVisible: (state): boolean =>
      Boolean(state.status && !state.status.done && !state.status.hint_dismissed),
  },
  actions: {
    async load() {
      try {
        this.status = await api.getOnboardingStatus();
        this.error = "";
      } catch (error) {
        this.error = error instanceof Error ? error.message : String(error);
      } finally {
        this.loaded = true;
      }
    },
    async markSeen() {
      try {
        this.status = await api.markOnboardingSeen();
      } catch (error) {
        this.error = error instanceof Error ? error.message : String(error);
      }
    },
    async saveProfile(payload: OnboardingProfilePayload) {
      this.saving = true;
      try {
        await api.saveOnboardingProfile(payload);
        await this.load();
      } catch (error) {
        this.error = error instanceof Error ? error.message : String(error);
        throw error;
      } finally {
        this.saving = false;
      }
    },
    async complete() {
      this.status = await api.completeOnboarding();
      this.dismissed = true;
    },
    async setHintDismissed(dismissed: boolean) {
      this.status = await api.setOnboardingHint(dismissed);
    },
    closeForSession() {
      this.dismissed = true;
    },
  },
});
