import { defineStore } from "pinia";
import { api } from "../services/api";
import {
  DEFAULT_CLOSE_BEHAVIOR,
  normalizeCloseBehavior,
  type CloseBehavior,
} from "../services/quitGuard";

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
     * 关闭主窗口时的行为（R9 契约 §11，设置键 ui.close_behavior）：
     * ask 每次询问 / tray 最小化到托盘 / quit 直接退出。
     */
    closeBehavior: DEFAULT_CLOSE_BEHAVIOR as CloseBehavior,
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
        // 旧客户端/后端没有该字段：按契约默认 ask，不报错（不静默提高成 quit）。
        this.closeBehavior = normalizeCloseBehavior(s.close_behavior);
      } catch (e) {
        console.error("[ui] load settings failed:", e);
      } finally {
        this.loaded = true;
      }
    },
    /** 保存关闭行为：失败要抛给调用方（设置页/确认框要给出可见反馈）。 */
    async setCloseBehavior(behavior: CloseBehavior) {
      this.closeBehavior = behavior;
      try {
        const r = await api.updateCloseBehavior(behavior);
        this.closeBehavior = normalizeCloseBehavior(r.close_behavior ?? behavior);
      } catch (e) {
        console.error("[ui] save close behavior failed:", e);
        throw e;
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
