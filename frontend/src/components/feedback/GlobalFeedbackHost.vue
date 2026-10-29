<script setup lang="ts">
/**
 * 反馈宿主（B · 全局唯一）：Lead 在 App.vue 挂一次即可。
 *
 * 挂载用法：
 *   <GlobalFeedbackHost />
 *
 * 说明：
 * - 无 props / 无事件；内部自己安装并清理输入门控、任务失败监听、全局异常来源与弹窗监听；
 * - 渲染两件事：顶部短通知（TopNotice）与全局问题面板（GlobalIssuePanel）；
 * - 全局异常**入口**按钮在 WindowChrome（D 挂 GlobalIssueEntry），本宿主不渲染入口；
 * - 中央弹窗关闭后先把排队通知过滤一遍再显示；输入结束后的延迟重试也在本组件统一调度。
 */
import { onBeforeUnmount, onMounted, watch, type WatchStopHandle } from "vue";
import GlobalIssuePanel from "./GlobalIssuePanel.vue";
import TopNotice from "./TopNotice.vue";
import { useTaskFailureNotices, type TaskFailureNoticesHandle } from "./useTaskFailureNotices";
import { useGlobalIssueSources, type GlobalIssueSourcesHandle } from "./useGlobalIssueSources";
import { useModalArbiterStore } from "../../stores/modalArbiter";
import { useNotificationsStore } from "../../stores/notifications";
import { useConversationTasksStore } from "../../stores/conversationTasks";
import { useUnifiedApprovalsStore } from "../../stores/unifiedApprovals";
import {
  INPUT_LEAVE_RETRY_DELAY_MS,
  installFeedbackGate,
  subscribeInputLeft,
} from "../../stores/feedbackGate";

const notifications = useNotificationsStore();
const arbiter = useModalArbiterStore();
const unified = useUnifiedApprovalsStore();
/**
 * 任务数据是**应用级**服务，不是对话页的私有状态（F02）。
 * 这里用引用计数拿一份全局所有权：首个 acquire 才建立事件订阅/轮询/首份快照，
 * 重复 acquire 只加计数，最后一个 release 才真正 dispose。页面只管理自己的展示状态。
 */
const tasks = useConversationTasksStore();

let stopGate: (() => void) | null = null;
let stopInputLeft: (() => void) | null = null;
let taskNotices: TaskFailureNoticesHandle | null = null;
let issueSources: GlobalIssueSourcesHandle | null = null;
let stopModalWatch: WatchStopHandle | null = null;
let retryTimer: ReturnType<typeof setTimeout> | null = null;

/**
 * 离开输入框后延迟再试：自动弹出审批（条件已恢复时）与显示排队通知。
 * 定时器只留一个，卸载时清掉。
 */
function scheduleAfterInputLeft(): void {
  if (retryTimer) clearTimeout(retryTimer);
  retryTimer = setTimeout(() => {
    retryTimer = null;
    notifications.flushQueue();
    if (unified.autoPending) unified.maybeAutoOpen();
  }, INPUT_LEAVE_RETRY_DELAY_MS + 20);
}

onMounted(() => {
  stopGate = installFeedbackGate();
  stopInputLeft = subscribeInputLeft(scheduleAfterInputLeft);
  taskNotices = useTaskFailureNotices();
  // 应用级任务数据：先建立失败监听（基线），再取全局所有权触发首份快照。
  // 直接进入设置/互动/调试页也会有真实任务基线；切换页面不会停掉订阅与轮询。
  tasks.acquireGlobal();
  issueSources = useGlobalIssueSources();
  /**
   * 中央弹窗开/关：统一由通知调度处理「撤下 / 恢复」。
   *
   * - 打开：正在显示的通知撤下（保留在队列并记录剩余时长），新通知继续排队；
   * - 关闭：恢复被挂起的那条并按剩余时间继续；弹窗期间新入队的从显示时起算 5s。
   * 不在任何组件里用 z-index / 遮罩「盖住」通知来假装撤下。
   */
  stopModalWatch = watch(
    () => arbiter.hasModal,
    (hasModal) => {
      if (hasModal) {
        notifications.suspendForModal();
        return;
      }
      notifications.resumeFromModal();
      if (unified.autoPending) unified.maybeAutoOpen();
    },
    { immediate: true },
  );
});

onBeforeUnmount(() => {
  if (retryTimer) clearTimeout(retryTimer);
  retryTimer = null;
  stopModalWatch?.();
  stopModalWatch = null;
  taskNotices?.stop();
  taskNotices = null;
  issueSources?.stop();
  issueSources = null;
  stopInputLeft?.();
  stopInputLeft = null;
  stopGate?.();
  stopGate = null;
  // 应用卸载才释放全局任务数据（页面切换不释放）。重复 release 是安全的。
  tasks.releaseGlobal();
  // 卸载时收掉通知计时器（避免宿主消失后仍改状态）
  notifications.reset();
});
</script>

<template>
  <div class="uf-feedback-host" data-uf-feedback-host>
    <TopNotice />
    <GlobalIssuePanel />
  </div>
</template>

<style scoped>
/* 宿主不占布局：两个子组件各自用 fixed 定位（通知居中、问题面板靠右）。 */
.uf-feedback-host {
  position: static;
}
</style>
