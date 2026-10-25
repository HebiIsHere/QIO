<script setup lang="ts">
/**
 * 对话页统一审批层（D）：入口 + 弹窗 + 记忆问题提醒的唯一挂载点。
 *
 * 职责边界：
 * - 挂载期间把 approvals.inlineSuppressed 置 true（对话页由统一弹窗接管按钮；
 *   其他页面保持内联审批原样可用）。TurnProcess.vue 的一行门控由 Lead 在集成时加；
 *   未加之前本层用「内联已显示就不自动弹」兜底，绝不出现两套按钮。
 * - 实时候选事件 / 轮询 / 窗口回到前台时刷新待确认知识；不要求用户先发消息。
 * - 记忆问题提醒只显示真实待回答数量；点击后把「星球 → 待确认 → 定位问题」的目标
 *   交给外层（Lead 集成 PlanetView 的 initialTab/focusPendingId 时消费）。
 */
import { onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useApprovalsStore } from "../../stores/approvals";
import { useSessionStore } from "../../stores/session";
import {
  isBlockingModalOpen,
  isUserEditing,
  useUnifiedApprovalsStore,
} from "../../stores/unifiedApprovals";
import UnifiedApprovalModal from "../approval/UnifiedApprovalModal.vue";
import MemoryQuestionReminder from "./MemoryQuestionReminder.vue";
import type { MemoryOpenTarget } from "../../stores/memoryQuestions";

const emit = defineEmits<{
  /** 外层把它接到「打开星球并定位到待确认问题」 */
  "open-memory": [target: MemoryOpenTarget];
}>();

const approvals = useApprovalsStore();
const session = useSessionStore();
const unified = useUnifiedApprovalsStore();

const REFRESH_MS = 30_000;
const RETRY_THROTTLE_MS = 250;
let refreshTimer = 0;
let noticeTimer = 0;
let lastRetryAt = 0;
const notice = ref("");

watch(
  () => unified.lastNotice,
  (text) => {
    if (!text) return;
    notice.value = text;
    window.clearTimeout(noticeTimer);
    noticeTimer = window.setTimeout(() => {
      notice.value = "";
    }, 6000);
  },
);

function ingestCandidates() {
  for (const candidate of session.knowledgeCandidates) {
    unified.ingestKnowledgeCandidate({
      knowledgeId: candidate.knowledgeId,
      category: candidate.category,
      content: candidate.content,
      reason: candidate.reason,
    });
  }
}

function retryAutoOpen() {
  const now = Date.now();
  if (now - lastRetryAt < RETRY_THROTTLE_MS) return;
  lastRetryAt = now;
  unified.maybeAutoOpen();
}

function onUserInteraction() {
  if (unified.autoPending) retryAutoOpen();
}

function onVisibilityChange() {
  if (document.visibilityState !== "visible") return;
  void unified.refreshKnowledgePending();
  retryAutoOpen();
}

function onOpenMemory(target: MemoryOpenTarget) {
  emit("open-memory", target);
}

onMounted(() => {
  // 对话页由统一弹窗接管按钮：让过程区内联卡让位（Lead 在 TurnProcess 加一行门控）
  approvals.inlineSuppressed = true;
  ingestCandidates();
  // 页面加载时已经存在的待审批项也要遵守自动弹出规则（此前只在「队列变化」时尝试，
  // 启动前就存在的申请会只亮入口、不自动弹）。
  void unified.refreshKnowledgePending().then(() => {
    unified.noteQueueChanged();
    unified.maybeAutoOpen();
  });
  refreshTimer = window.setInterval(() => {
    void unified.refreshKnowledgePending();
  }, REFRESH_MS);
  // 条件恢复（用户点到别处 / 窗口回到前台）后，被挡住的自动弹出再试一次
  document.addEventListener("pointerdown", onUserInteraction, true);
  document.addEventListener("focusin", onUserInteraction, true);
  document.addEventListener("visibilitychange", onVisibilityChange);
});

onBeforeUnmount(() => {
  approvals.inlineSuppressed = false;
  window.clearInterval(refreshTimer);
  window.clearTimeout(noticeTimer);
  document.removeEventListener("pointerdown", onUserInteraction, true);
  document.removeEventListener("focusin", onUserInteraction, true);
  document.removeEventListener("visibilitychange", onVisibilityChange);
});

watch(
  () => session.knowledgeCandidates.map((candidate) => candidate.knowledgeId).join("|"),
  () => ingestCandidates(),
);

watch(
  () => approvals.queue.map((item) => item.approval_id).join("|"),
  () => {
    unified.noteQueueChanged();
    unified.maybeAutoOpen();
  },
);

/**
 * 既有「查看完整信息」入口（过程区内联卡）会调用 approvals.showFull()：
 * 对话页由统一弹窗接管这一次显式请求。只在没有别的模态/没有在输入时打开，
 * 绝不叠加；内联卡仍占着按钮（inlineClaimed）时也不抢。
 */
watch(
  () => approvals.visible,
  (visible) => {
    if (!visible || unified.visible) return;
    const current = approvals.current;
    if (!current) return;
    if (approvals.inlineClaimed || isUserEditing() || isBlockingModalOpen()) return;
    unified.open("tool:" + current.approval_id);
  },
);

watch(
  () => [unified.autoPending, unified.visible, unified.count] as const,
  () => {
    if (unified.autoPending) retryAutoOpen();
  },
);
</script>

<template>
  <div class="unified-approval-layer">
    <!-- 入口统一放到输入区上方的五图标入口（⑤）：对话页只有这一处审批入口，
         这里不再额外渲染顶部浮动条，避免同一时刻出现两个入口。 -->
    <UnifiedApprovalModal />
    <MemoryQuestionReminder @open="onOpenMemory" />
    <p v-if="notice" class="ua-layer-notice" role="status">{{ notice }}</p>
  </div>
</template>

<style scoped>
/* 层本身不占布局：子组件各自固定定位（入口顶部居中 / 弹窗遮罩 / 记忆提醒右下）。 */
.unified-approval-layer {
  position: static;
}
.ua-layer-notice {
  position: fixed;
  top: calc(var(--qio-top-notes-offset, calc(var(--titlebar-height) + 12px)) + 40px);
  left: 50%;
  transform: translateX(-50%);
  z-index: 91;
  max-width: min(560px, calc(100vw - 32px));
  margin: 0;
  padding: 6px 14px;
  border-radius: var(--r-pill);
  border: 1px solid var(--border-subtle);
  background: var(--bg-elevated);
  color: var(--text-secondary);
  font-family: var(--sans);
  font-size: 12px;
  line-height: 1.5;
  box-shadow: var(--shadow-1);
  overflow-wrap: anywhere;
}
</style>
