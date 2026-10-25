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
import { useInteractiveStore } from "../../stores/interactive";
import {
  isBlockingModalOpen,
  isUserEditing,
  useUnifiedApprovalsStore,
} from "../../stores/unifiedApprovals";
import UnifiedApprovalModal from "../approval/UnifiedApprovalModal.vue";
import MemoryQuestionReminder from "./MemoryQuestionReminder.vue";
import type { MemoryOpenTarget } from "../../stores/memoryQuestions";

const props = withDefaults(
  defineProps<{
    /** 互动页没有记忆星球落点：默认隐藏记忆提醒，避免点击无效。 */
    showMemory?: boolean;
  }>(),
  { showMemory: true },
);

const emit = defineEmits<{
  /** 外层把它接到「打开星球并定位到待确认问题」 */
  "open-memory": [target: MemoryOpenTarget];
}>();

const approvals = useApprovalsStore();
const session = useSessionStore();
const unified = useUnifiedApprovalsStore();
const interactive = useInteractiveStore();

const REFRESH_MS = 30_000;
const RETRY_THROTTLE_MS = 250;
let refreshTimer = 0;
let noticeTimer = 0;
let lastRetryAt = 0;
const notice = ref("");

/**
 * 唯一挂载的守卫（Phase 3-C）：
 * 两个同时挂载的层会让「自动弹出 / 唯一入口 / 内联抑制」三件事互相打架
 * （真实风险：路由过渡期间新旧页面各挂一次）。这里不改任何人，只如实报出来，
 * 由测试与集成发现；运行时不打断界面。
 */
const LAYER_MOUNT_KEY = "__qioUnifiedApprovalLayers";
function layerRegistry(): { count: number } {
  const host = globalThis as Record<string, unknown>;
  const existing = host[LAYER_MOUNT_KEY] as { count: number } | undefined;
  if (existing && typeof existing.count === "number") return existing;
  const created = { count: 0 };
  host[LAYER_MOUNT_KEY] = created;
  return created;
}
const layerState = layerRegistry();

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

/**
 * F01：保存前影响确认正在等用户决定时，**不自动弹出**工具/权限审批。
 *
 * 两者可以共用呈现机制，但不能同时叠出多个阻塞弹窗：影响确认先占着这个位置，
 * 等它结束（确认 / 取消）后再按既有规则尝试自动打开审批（申请不会丢）。
 */
function impactConfirmPending(): boolean {
  return Boolean(interactive.pendingImpact);
}

function retryAutoOpen() {
  // 窗口在后台时不抢焦点：回到前台（visibilitychange）会再试一次
  if (typeof document !== "undefined" && document.visibilityState === "hidden") return;
  if (impactConfirmPending()) {
    unified.autoPending = true;
    return;
  }
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
  layerState.count += 1;
  if (layerState.count > 1) {
    console.warn("[qio] 统一审批层被重复挂载（同一时刻只允许一个）：自动弹出与唯一入口可能互相干扰");
  }
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
  layerState.count = Math.max(0, layerState.count - 1);
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

/** 影响确认结束后：把被它让位挡住的那次自动弹出补上（条件恢复再试，不抢焦点）。 */
watch(
  () => Boolean(interactive.pendingImpact),
  (pending) => {
    if (!pending && unified.autoPending) retryAutoOpen();
  },
);

/**
 * 手动找回（Phase 3-C）：入口组件与事故恢复都走这一个口。
 * 不自动弹、不绕过 deferred 规则 —— 用户点一次就打开一次。
 */
function openApprovals(key?: string | null): boolean {
  return unified.open(key ?? null);
}

defineExpose({ openApprovals, retryAutoOpen });
</script>

<template>
  <div class="unified-approval-layer" data-unified-approval-layer>
    <!-- 入口统一放到输入区上方的五图标入口（⑤）：对话页只有这一处审批入口，
         这里不再额外渲染顶部浮动条，避免同一时刻出现两个入口。 -->
    <UnifiedApprovalModal />
    <MemoryQuestionReminder v-if="props.showMemory" @open="onOpenMemory" />
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
