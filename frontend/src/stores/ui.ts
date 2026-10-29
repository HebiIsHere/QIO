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
/**
 * 已经被采纳为「服务端真实持久值」的最大写序号（F05 返修）。
 *
 * 保存请求的返回顺序不受前端控制：**前一次成功、后一次失败**时，如果只把
 * 「最新一次失败」回退到本地记住的值，就会退回更早的旧值，和实际已保存的值（以及壳）
 * 不一致。这里让每次成功（哪怕是迟到的）都把自己的持久值记为候选，
 * 再配合失败后的一次服务端校准，保证「实际保存值 = 界面 = 壳」。
 */
let closeBehaviorValueSeq = 0;
/** 在途的保存请求数：迟到请求结算完时用来判断「是否还有更新的一次在选择中」。 */
let closeBehaviorInFlight = 0;

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
     * 最近一次**已经提交给桌面壳**的值（同一次运行期内去重）。
     * 迟到请求结算、失败校准等路径可能把同一个值重复提交；壳只需要最后一次真实值，
     * 重复 IPC 既无意义也会放大「旧同步过程」的机会。
     */
    closeBehaviorShellSynced: null as CloseBehavior | null,
    /**
     * 最新一次保存失败、但还有别的保存在途：此刻的服务端回读不是最终结果。
     * 先展示已知值，等**全部请求结算**后再补读一次真实值（本轮返修）。
     */
    closeBehaviorReconcilePending: false,
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
    /** 把关闭行为提交给桌面壳（相同值不重复提交）。 */
    pushCloseBehaviorToShell(behavior: CloseBehavior) {
      if (this.closeBehaviorShellSynced === behavior) return;
      this.closeBehaviorShellSynced = behavior;
      void syncCloseBehaviorToShell(behavior);
    },
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
        // 加载到的值是当前真实基线：之前（以及随后迟到的）写响应不得再把更早的值当基线。
        closeBehaviorValueSeq = closeBehaviorWriteSeq;
        // 同步给壳：系统关闭事件由壳按这个值执行（不依赖前端再回传动作）。
        this.pushCloseBehaviorToShell(loaded);
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
      closeBehaviorInFlight += 1;
      // 乐观显示：用户点下去就有反馈；真实生效值仍以上一次确认为准。
      this.closeBehavior = behavior;
      try {
        const r = await api.updateCloseBehavior(behavior);
        const persisted = normalizeCloseBehavior(r.close_behavior ?? behavior);
        // 每一次成功都带着「服务端真实持久值」；只要它是比已知基线更新的一次选择就采纳。
        // 迟到的旧成功也可以更新基线（它确实写进去了），但不能覆盖更新一次选择的结果。
        if (token > closeBehaviorValueSeq) {
          closeBehaviorValueSeq = token;
          this.closeBehaviorEffective = persisted;
        }
        // 只有最新一次选择才允许写界面并同步壳。
        if (token === closeBehaviorWriteSeq) {
          // 最新一次已经成功：之前因「还有请求在途」挂起的补读不再需要。
          this.closeBehaviorReconcilePending = false;
          this.closeBehavior = this.closeBehaviorEffective;
          // 同步本身在 desktop 层串行化，保证最后发起的那次最后到达。
          this.pushCloseBehaviorToShell(this.closeBehaviorEffective);
        }
      } catch (e) {
        const latest = token === closeBehaviorWriteSeq;
        if (latest) {
          // 最新选择失败：不能只回退到本地记的值 —— 前一次需求可能已经保存成功但响应迟到。
          if (closeBehaviorInFlight > 1) {
            /**
             * 还有别的保存请求在途（本轮返修的触发顺序：后一次先失败、前一次随后才写入成功）：
             * 此刻的回读看不到那次写入，**不能当成最终结果**。先展示最近一次已确认值，
             * 等全部请求结算后由 finally 补读一次真实值。
             */
            this.closeBehaviorReconcilePending = true;
            this.closeBehavior = this.closeBehaviorEffective;
          } else {
            // 只有本次在途：回读就是最终结果（超时可能其实写进去了 / 明确拒绝没写进去）。
            await this.reconcileCloseBehavior(token);
          }
        }
        console.error("[ui] save close behavior failed:", e);
        if (latest) throw e;
      } finally {
        closeBehaviorInFlight -= 1;
        if (closeBehaviorInFlight === 0) {
          if (this.closeBehaviorReconcilePending) {
            /**
             * 全部保存请求终于都结算了：这时服务端才知道最终值（可能正是迟到的旧成功写进去的）。
             * 补读一次真实值作为最终状态；读不到时 reconcileCloseBehavior 会退回最近一次已确认值。
             */
            this.closeBehaviorReconcilePending = false;
            await this.reconcileCloseBehavior(closeBehaviorWriteSeq);
          } else if (token !== closeBehaviorWriteSeq) {
            // 迟到的旧请求结算完、且没有更新的选择在途：界面与壳对齐到最新已知持久值。
            // 这一步保证「前一次成功、后一次失败」在任意返回顺序下都收敛到同一个值。
            this.closeBehavior = this.closeBehaviorEffective;
            this.pushCloseBehaviorToShell(this.closeBehaviorEffective);
          }
        }
      }
    },
    /**
     * 保存失败后的服务端校准（F05 返修）：读一次真实持久值，界面与壳都跟它走。
     *
     * 失败原因可能是超时（请求其实已经写进去了），也可能是服务端明确拒绝（没写进去）；
     * 只有服务端能回答「现在到底是什么」。读不到时退回最近一次已确认值并保留错误反馈，
     * 绝不退回更早的旧值。期间用户又改了选择（token 变了）时不校准，交给新选择。
     */
    async reconcileCloseBehavior(token: number) {
      try {
        const s = await api.getUISettings();
        if (token !== closeBehaviorWriteSeq) return;
        const actual = normalizeCloseBehavior(s.close_behavior);
        closeBehaviorValueSeq = closeBehaviorWriteSeq;
        this.closeBehavior = actual;
        this.closeBehaviorEffective = actual;
        this.pushCloseBehaviorToShell(actual);
      } catch (err) {
        if (token !== closeBehaviorWriteSeq) return;
        console.warn("[ui] 校准关闭行为失败，退回最近一次已确认值", err);
        this.closeBehavior = this.closeBehaviorEffective;
        this.pushCloseBehaviorToShell(this.closeBehaviorEffective);
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