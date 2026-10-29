import { defineStore } from "pinia";
import { api } from "../services/api";
import {
  DEFAULT_CLOSE_BEHAVIOR,
  normalizeCloseBehavior,
  type CloseBehavior,
} from "../services/quitGuard";
import { syncCloseBehaviorToShell } from "../services/desktop";

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

/**
 * 关闭行为的写序号（F05）。
 *
 * 关闭行为是「最后一次选择必须赢」的单值设置：界面上先切到新值，保存请求可能乱序返回。
 * 保护点必须在**真正写状态和同步桌面壳**的地方，而不是页面显示层：
 * 只有序号仍然最新的那次响应才允许写 closeBehavior / closeBehaviorEffective / 同步壳；
 * 旧响应（无论成功还是失败）一律忽略。加载（load）同样领号，晚到的旧配置不得盖掉用户的新选择。
 */
let closeBehaviorWriteSeq = 0;

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
     * 最近一次**确认生效**的关闭行为（加载成功或保存成功时更新）。
     *
     * 保存失败时界面必须回到这个值：停在「看起来改了、其实没保存」的新值上，
     * 下一次关窗行为就和界面不一致了（F05）。
     */
    closeBehaviorEffective: DEFAULT_CLOSE_BEHAVIOR as CloseBehavior,
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
      // 领号：加载期间用户若已经改过关闭行为，这份（旧的）配置不得覆盖它。
      const token = closeBehaviorWriteSeq;
      try {
        const s = await api.getUISettings();
        this.typewriterCps = s.typewriter_cps ?? DEFAULT_TYPEWRITER_CPS;
        if (token !== closeBehaviorWriteSeq) return;
        // 旧客户端/后端没有该字段：按契约默认 ask，不报错（不静默提高成 quit）。
        const loaded = normalizeCloseBehavior(s.close_behavior);
        this.closeBehavior = loaded;
        this.closeBehaviorEffective = loaded;
        // 同步给壳：系统关闭事件由壳按这个值执行（不依赖前端再回传动作）。
        void syncCloseBehaviorToShell(loaded);
      } catch (e) {
        console.error("[ui] load settings failed:", e);
      } finally {
        this.loaded = true;
      }
    },
    /**
     * 保存关闭行为：失败要抛给调用方（设置页/确认框要给出可见反馈）。
     *
     * 顺序保护（F05）：界面上可以先乐观显示用户刚选的值，但**只有最新那次选择**
     * 才允许把服务端结果写回状态、更新真实生效值、同步给桌面壳。
     * 较早的请求晚返回（无论成功还是失败）一律忽略 —— 否则旧响应会把新选择盖回去，
     * 壳也会拿到旧行为，下一次关窗就和界面不一致。
     */
    async setCloseBehavior(behavior: CloseBehavior) {
      const token = ++closeBehaviorWriteSeq;
      // 乐观显示：用户点下去就有反馈；真实生效值仍以上一次确认为准。
      this.closeBehavior = behavior;
      try {
        const r = await api.updateCloseBehavior(behavior);
        // 迟到的旧响应：它代表的不是最新选择，不写状态也不同步壳。
        if (token !== closeBehaviorWriteSeq) return;
        const effective = normalizeCloseBehavior(r.close_behavior ?? behavior);
        this.closeBehavior = effective;
        this.closeBehaviorEffective = effective;
        // 保存成功后立即让壳拿到新值：下一次点 X 就按新行为执行（无需重启）。
        // 同步本身在 desktop 层串行化，保证最后发起的那次最后到达。
        void syncCloseBehaviorToShell(effective);
      } catch (e) {
        // 旧请求的失败不影响较新结果（新选择自己会给出成功/失败反馈）。
        if (token !== closeBehaviorWriteSeq) return;
        // 最新选择保存失败：界面必须回到真实生效值，不能停在没保存成功的值上。
        this.closeBehavior = this.closeBehaviorEffective;
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
