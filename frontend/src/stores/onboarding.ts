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
    /**
     * 本轮会话里向导是否打开 / 是否被关掉。
     *
     * 为什么要和 `status.show_wizard` 分开：向导一打开就调 `markSeen()`，后端随后返回
     * `show_wizard=false`；如果直接用后端字段做 `v-if`，向导会在打开的一瞬间把自己关掉。
     * 所以展开决策只在 `load()` 时做一次（`decided`），之后由 `opened` / `dismissed` 决定。
     */
    opened: false,
    decided: false,
    dismissed: false,
  }),
  getters: {
    showWizard: (state): boolean => state.opened && !state.dismissed,
    needsSetup: (state): boolean => !state.status?.done,
    hintVisible: (state): boolean =>
      Boolean(state.status && !state.status.done && !state.status.hint_dismissed),
  },
  actions: {
    async load() {
      try {
        this.status = await api.getOnboardingStatus();
        if (!this.decided) {
          this.opened = Boolean(this.status.show_wizard);
          this.decided = true;
        }
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
      this.opened = false;
    },
    async setHintDismissed(dismissed: boolean) {
      this.status = await api.setOnboardingHint(dismissed);
    },
    /**
     * 重新展开向导（对话页「继续设置」与设置页「重新运行设置助手」共用）。
     *
     * 向导一打开就记过 seen，后端的 show_wizard 已经翻成 false；这里是**本机重新打开**，
     * 所以直接把本地状态抬起来，不动后端版本记录。
     */
    reopen() {
      this.opened = true;
      this.dismissed = false;
    },
    closeForSession() {
      this.dismissed = true;
      this.opened = false;
    },
  },
});
