/**
 * 通知点击 → 任务列表可见落点（C · 公共约定 §3.3）。
 *
 * 设计边界：
 * - 有中央弹窗时**不导航、不关弹窗、不丢通知**：返回 blocked-by-modal，由调用方保留通知；
 * - 没有弹窗时用现有机制让任务列表真实可见：切到对话路由、关闭页内覆盖层
 *   （记忆星球 / 恢复收件箱 / 全窗口预览）、必要时展开左栏；窄窗走现有任务抽屉；
 * - 再把整组任务交给 conversationTasks.focusTasks(ids) 展开定位（全部目标，不截断）；
 * - 任一环节失败都返回 ok:false + 可直接展示的中文说明，不静默成功。
 *
 * 页面内的可见性由 ConversationView 注册的 host 提供（星球 / 恢复区 / 抽屉这些是
 * 页面级状态，归页面自己收口）；本服务只负责路由与调用顺序，不直接改别人的组件状态。
 */
import router from "../router";
import { useConversationShellStore } from "../stores/conversationShell";
import { useConversationTasksStore } from "../stores/conversationTasks";
import { useModalArbiterStore } from "../stores/modalArbiter";

export type NoticeNavigationReason = "ok" | "no-targets" | "blocked-by-modal" | "navigation-failed";

export interface NoticeNavigationResult {
  ok: boolean;
  reason: NoticeNavigationReason;
  taskIds: string[];
  /** 失败时可直接展示给用户的中文说明（不静默丢弃处理意图）。 */
  message: string;
}

/** ConversationView 注册的页面内可见性宿主：关闭覆盖层、窄窗打开任务抽屉。 */
export interface NoticeNavigationHost {
  /** 返回任务列表此刻是否真实可见（false 表示调用方必须把失败说出来）。 */
  ensureTaskListVisible(): boolean | Promise<boolean>;
}

/** 等待对话页挂载并注册宿主的上限（懒加载路由才有意义；已经注册时立即返回）。 */
export const NOTICE_HOST_WAIT_MS = 1000;
const HOST_POLL_MS = 20;

/**
 * 兼容旧基线的能力视图：C 的 focusTasks 合入前，A 分支可能先带着本文件跑通
 * vue-tsc / 单测；真正集成后走 focusTasks（全部目标），旧基线才退回单目标。
 */
interface NoticeTaskFocusApi {
  focusTasks?: (taskIds: string[]) => Promise<void>;
  focusTask?: (taskId: string) => Promise<void>;
  focusError?: string;
}

let host: NoticeNavigationHost | null = null;

/** 对话页挂载时注册；传 null 表示没有页面内可见性宿主。 */
export function registerNoticeNavigationHost(next: NoticeNavigationHost | null): void {
  host = next ?? null;
}

/** 卸载时只移除自己注册的那一个，避免在页面切换瞬间误清新页面的宿主。 */
export function unregisterNoticeNavigationHost(target: NoticeNavigationHost | null): void {
  if (target && host === target) host = null;
}

/** 当前宿主（测试与诊断用；正常调用不需要读它）。 */
export function currentNoticeNavigationHost(): NoticeNavigationHost | null {
  return host;
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, Math.max(0, ms)));
}

async function waitForHost(): Promise<NoticeNavigationHost | null> {
  const started = Date.now();
  while (!host && Date.now() - started < NOTICE_HOST_WAIT_MS) {
    await sleep(HOST_POLL_MS);
  }
  return host;
}

function errorText(error: unknown): string {
  if (!error) return "未知原因";
  const message = (error as Error)?.message;
  return message && String(message).trim() ? String(message) : "未知原因";
}

/** 去空、去重并保序：同一任务只定位一次，顺序沿用通知里的任务顺序。 */
export function normalizeNoticeTaskIds(taskIds: readonly string[] | null | undefined): string[] {
  const out: string[] = [];
  for (const raw of Array.isArray(taskIds) ? taskIds : []) {
    const id = String(raw ?? "").trim();
    if (id && !out.includes(id)) out.push(id);
  }
  return out;
}

function routeName(): string {
  try {
    const value = router.currentRoute.value?.name;
    return typeof value === "string" ? value : "";
  } catch {
    return "";
  }
}

function isOnConversationRoute(): boolean {
  const name = routeName();
  if (name) return name === "conversation";
  try {
    return router.currentRoute.value?.path === "/";
  } catch {
    return false;
  }
}

/**
 * 没有宿主时的兜底：只依赖外壳 store 展开左栏。
 * 窄窗（窗口宽度临时收起）必须由页面抽屉承接，这里不会假装已经可见。
 */
function ensureLeftRailVisible(): boolean {
  const shell = useConversationShellStore();
  if (shell.leftVisible) return true;
  if (shell.prefs.leftCollapsed) shell.toggleLeft();
  return shell.leftVisible;
}

function result(
  ok: boolean,
  reason: NoticeNavigationReason,
  taskIds: string[],
  message: string,
): NoticeNavigationResult {
  return { ok, reason, taskIds, message };
}

/**
 * 保证任务列表真实可见后，展开并定位这组任务（全部目标）。
 *
 * 调用方（TopNotice）按返回的 reason 处理：
 * - ok：可以关闭通知（定位已经落到可见列表上）；
 * - blocked-by-modal：保留通知、不打断中央弹窗；
 * - no-targets / navigation-failed：把 message 展示给用户，不静默关闭通知。
 */
export async function openTaskNoticeTargets(
  taskIds: readonly string[] | null | undefined,
): Promise<NoticeNavigationResult> {
  const ids = normalizeNoticeTaskIds(taskIds);
  if (!ids.length) {
    return result(false, "no-targets", [], "这条通知没有可定位的任务（它可能已经被处理，或记录已经被清理）。");
  }

  let arbiter: ReturnType<typeof useModalArbiterStore>;
  try {
    arbiter = useModalArbiterStore();
  } catch {
    return result(
      false,
      "navigation-failed",
      ids,
      "任务列表暂时无法打开：应用状态还没有准备好（可以再点一次通知）。",
    );
  }
  // 有中央弹窗：不导航、不关弹窗、不丢通知。
  if (arbiter.hasModal) {
    return result(
      false,
      "blocked-by-modal",
      ids,
      "现在有需要先处理的中央弹窗：先处理它，通知和对应的任务记录都还在，处理完再点这次通知就能定位。",
    );
  }

  let navigated = false;
  if (!isOnConversationRoute()) {
    navigated = true;
    try {
      await router.push({ name: "conversation" });
    } catch (error) {
      return result(
        false,
        "navigation-failed",
        ids,
        "没能切换到任务列表所在的页面：" + errorText(error) + "。可以再点一次通知。",
      );
    }
    if (!isOnConversationRoute()) {
      return result(
        false,
        "navigation-failed",
        ids,
        "页面切换没有完成（还没有进入对话页）。任务记录仍在，可以再点一次通知。",
      );
    }
  }

  // 跨页进入时对话页是懒加载的：等它挂载并把页面内可见性职责注册上来。
  const activeHost = navigated ? await waitForHost() : host;
  if (activeHost) {
    try {
      const visible = await activeHost.ensureTaskListVisible();
      if (visible === false) {
        return result(
          false,
          "navigation-failed",
          ids,
          "任务列表没能显示出来（页面可能正在切换）。可以再点一次通知，或在左侧展开任务列表逐条查看。",
        );
      }
    } catch (error) {
      return result(
        false,
        "navigation-failed",
        ids,
        "打开任务列表时出错：" + errorText(error) + "。任务记录仍在，可以展开左侧任务列表查看。",
      );
    }
  } else if (!ensureLeftRailVisible()) {
    return result(
      false,
      "navigation-failed",
      ids,
      "任务列表现在是收起状态：点一次左侧「任务」入口展开，就能看到这些任务。",
    );
  }

  const tasks = useConversationTasksStore() as unknown as NoticeTaskFocusApi;
  try {
    if (typeof tasks.focusTasks === "function") {
      await tasks.focusTasks(ids);
    } else if (typeof tasks.focusTask === "function") {
      // 旧基线兼容路径（C 的 focusTasks 合入后不会走到这里）。
      for (const id of ids) await tasks.focusTask(id);
    } else {
      return result(
        false,
        "navigation-failed",
        ids,
        "任务列表的定位功能还没有准备好：任务记录仍在，可以展开左侧任务列表逐条查看。",
      );
    }
  } catch (error) {
    return result(
      false,
      "navigation-failed",
      ids,
      "定位任务时出错：" + errorText(error) + "。任务记录仍在，可以展开左侧任务列表查看。",
    );
  }

  const focusError = typeof tasks.focusError === "string" ? tasks.focusError.trim() : "";
  if (focusError) {
    return result(false, "navigation-failed", ids, focusError);
  }
  return result(true, "ok", ids, "");
}
