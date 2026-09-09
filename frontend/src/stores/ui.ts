import { defineStore } from "pinia";
import { api } from "../services/api";

export const TYPEWRITER_SPEEDS = [25, 50, 75] as const;
export const DEFAULT_TYPEWRITER_CPS = 50;

export const useUiStore = defineStore("ui", {
  state: () => ({
    /** 打字机输出速度（字符/秒），三档 25/50/75 */
    typewriterCps: DEFAULT_TYPEWRITER_CPS as number,
    loaded: false,
  }),
  actions: {
    async load() {
      try {
        const s = await api.getUISettings();
        this.typewriterCps = s.typewriter_cps ?? DEFAULT_TYPEWRITER_CPS;
      } catch (e) {
        console.error("[ui] load settings failed:", e);
      } finally {
        this.loaded = true;
      }
    },
    async setCps(cps: number) {
      this.typewriterCps = cps;
      try {
        await api.updateUISettings(cps);
      } catch (e) {
        console.error("[ui] save typewriter cps failed:", e);
      }
    },
  },
});
