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
    /**
     * 设置页「同次使用」记忆：分类与分类列表滚动位置。
     * 设置是整页路由，离开会卸载组件；不记在 store 里就会每次回到外观。
     */
    settingsSection: null as string | null,
    settingsNavScroll: 0,
    /**
     * 设置页「非敏感」表单草稿（同次运行期）。
     * 设置是整页路由，离开会卸载组件；不记住就会把用户改到一半的值丢掉。
     * 凭据密钥一类敏感输入**不进这里**（只在表单自身的内存状态里）。
     */
    settingsDraft: {} as Record<string, unknown>,
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
        // 抛给调用方：设置页要给出可见的失败反馈，不能只写控制台、
        // 也不能让界面停在「已保存」的假状态上。
        console.error("[ui] save typewriter cps failed:", e);
        throw e;
      }
    },
  },
});
