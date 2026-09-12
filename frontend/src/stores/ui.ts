import { defineStore } from "pinia";
import { api } from "../services/api";

export const TYPEWRITER_SPEEDS = [25, 50, 75] as const;
export const DEFAULT_TYPEWRITER_CPS = 50;
/** 开发者模式持久化键（纯本地显示偏好，不进后端设置） */
export const DEVELOPER_MODE_KEY = "qio-developer-mode";

function readDeveloperMode(): boolean {
  try {
    return localStorage.getItem(DEVELOPER_MODE_KEY) === "1";
  } catch {
    return false;
  }
}

export const useUiStore = defineStore("ui", {
  state: () => ({
    /** 打字机输出速度（字符/秒），三档 25/50/75 */
    typewriterCps: DEFAULT_TYPEWRITER_CPS as number,
    /**
     * 开发者模式（默认 OFF）：FPS / WebGL / token 等诊断信息只在开启后出现；
     * 正常模式保持「像产品，不像 debugger」。
     */
    developerMode: readDeveloperMode(),
    loaded: false,
  }),
  actions: {
    setDeveloperMode(on: boolean) {
      this.developerMode = on;
      try {
        if (on) localStorage.setItem(DEVELOPER_MODE_KEY, "1");
        else localStorage.removeItem(DEVELOPER_MODE_KEY);
      } catch {
        // localStorage 不可用：仅本次会话生效
      }
    },
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
