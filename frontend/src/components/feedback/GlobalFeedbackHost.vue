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
import { useUnifiedApprovalsStore } from "../../stores/unifiedApprovals";
import {
  INPUT_LEAVE_RETRY_DELAY_MS,
  installFeedbackGate,
  subscribeInputLeft,
} from "../../stores/feedbackGate";

const notifications = useNotificationsStore();
const arbiter = useModalArbiterStore();
const unified = useUnifiedApprovalsStore();

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
  issueSources = useGlobalIssueSources();
  // 中央弹窗关闭：把通知队列放出来（flushQueue 内部会先过滤已恢复/已处理的成员）
  stopModalWatch = watch(
    () => arbiter.hasModal,
    (hasModal) => {
      if (hasModal) return;
      notifications.flushQueue();
      if (unified.autoPending) unified.maybeAutoOpen();
    },
  );
  notifications.flushQueue();
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
