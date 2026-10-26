<!--
  初始设置未完成的一次性轻提醒（Lead 在 App.vue 挂载；不占布局、不抢焦点）。

  规则（用户任务书 · 引导/启动）：
  - 只有「初始设置未完成、但已经允许进入主页」时才提醒，而且**只自动出现一次**；
  - 显示用 B 的通知能力（notifications.enqueue，14px 居中短通知、约 5s、悬停/聚焦暂停、
    中央弹窗期间排队），不自己做第二套 toast；
  - 持久化「确实已经显示过」：只有通知真的开始显示（activeKey 等于本条 key）才写 localStorage；
    只是入队、排队中没显示出来，不算已提醒；
  - 点击接续设置由 B 的通知 host 触发 onActivate（合并后接入）→ onboarding.reopen()。
  - 设置页保留主动「继续设置」入口（SettingsView），这里只是轻提醒，不替代它。
-->
<script lang="ts">
/** 事件身份：同一次提示只入队一次（不用文案判断）。 */
export const SETUP_INCOMPLETE_NOTICE_KEY = "setup-incomplete:v1";
/** 「确实显示过」的本地标记（现有 localStorage 键风格；换版本时可加后缀升级）。 */
export const SETUP_INCOMPLETE_SHOWN_KEY = "qio.onboarding.setup-notice-shown-v1";
</script>

<script setup lang="ts">
import { onBeforeUnmount, watch } from "vue";
import { useOnboardingStore } from "../../stores/onboarding";
import { useNotificationsStore } from "../../stores/notifications";

const SETUP_INCOMPLETE_NOTICE_KEY = "setup-incomplete:v1";
const SETUP_INCOMPLETE_SHOWN_KEY = "qio.onboarding.setup-notice-shown-v1";

const onboarding = useOnboardingStore();
const notifications = useNotificationsStore();

/** 本组件生命周期内是否已经入过队（避免 watch 反复触发产生重复入队）。 */
let enqueued = false;

function readShownFlag(): boolean {
  try {
    return localStorage.getItem(SETUP_INCOMPLETE_SHOWN_KEY) === "1";
  } catch {
    // 读不到 localStorage（隐私模式等）：本次仍可提醒一次，只是跨重启去重会失效
    return false;
  }
}

function persistShownFlag(): void {
  try {
    localStorage.setItem(SETUP_INCOMPLETE_SHOWN_KEY, "1");
  } catch {
    /* 存不下只影响下次启动的默认值 */
  }
}

/** 当前是否满足「未完成但已允许进主页」：向导正开着时不需要轻提醒。 */
function shouldOfferNotice(): boolean {
  return Boolean(
    onboarding.loaded &&
      onboarding.status &&
      !onboarding.status.done &&
      !onboarding.showWizard,
  );
}

function maybeEnqueue(): void {
  if (enqueued || !shouldOfferNotice() || readShownFlag()) return;
  enqueued = true;
  notifications.enqueue({
    key: SETUP_INCOMPLETE_NOTICE_KEY,
    kind: "setup_incomplete",
    text: "初始设置尚未完成",
    mergeable: false,
    focus: false,
    // 排队期间用户也可能已经完成设置：显示前最后一道校验，过期就不显示。
    stillRelevant: () => shouldOfferNotice(),
  });
}

/**
 * 接续设置：点击这条通知时（B 的 host 提供激活回调后）重新打开向导。
 * 通知本身由 host 关闭；这里只做业务动作，不伪造「已显示」状态。
 */
function continueSetup(): void {
  notifications.dismiss(SETUP_INCOMPLETE_NOTICE_KEY);
  onboarding.reopen();
}

// 完成状态可能在任意时刻到达（后端 load / 用户完成设置 / 关闭向导）。
watch(
  () => [onboarding.loaded, onboarding.status?.done, onboarding.showWizard] as const,
  () => maybeEnqueue(),
  { immediate: true },
);

// 只有真的开始显示，才写「已提醒过」；排队未显示不算。
watch(
  () => notifications.activeKey,
  (key) => {
    if (key === SETUP_INCOMPLETE_NOTICE_KEY && !readShownFlag()) persistShownFlag();
  },
);

onBeforeUnmount(() => {
  // 离开时不要把这条通知留在队列里（它不是任务失败，没有重放价值）。
  try {
    notifications.dismiss(SETUP_INCOMPLETE_NOTICE_KEY);
  } catch {
    /* store 已销毁时忽略 */
  }
});

defineExpose({ continueSetup });
</script>

<template>
  <!-- 该组件不渲染自己的 UI：显示完全交给 B 的 GlobalFeedbackHost（单一通知出口）。 -->
  <span class="setup-incomplete-notice-anchor" hidden aria-hidden="true"></span>
</template>