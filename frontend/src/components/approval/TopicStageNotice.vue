<script setup lang="ts">
/**
 * 对话级话题阶段 / 恢复提示（D · 契约 §8）。
 *
 * 只呈现后端事实（经 E 的 store 派生）：
 * - executing / context_preparing：说明「已确认执行中 / 目标上下文准备中」，不伪造模型生成；
 * - recoverable_failure：显示后端真实失败阶段说明 + 重试入口（只 emit，动作由 store 唯一入口执行）；
 * - 其它阶段（等待确认 / 可回答 / 未知）：不渲染，避免与弹窗和消息区状态条重复；
 * - 重试不会清空原问题 / 草稿 / 已成功的话题操作（组件只负责呈现，不改这些状态）。
 */
import { computed } from "vue";
import type { TopicRecoveryView, TopicStageValue } from "./TopicStageTypes";

const props = withDefaults(
  defineProps<{
    stage?: TopicStageValue | null;
    recovery?: TopicRecoveryView | null;
    retrying?: boolean;
  }>(),
  { stage: null, recovery: null, retrying: false },
);

const emit = defineEmits<{
  /** 只把 recordId 交给 store 的 retry 动作；组件不发请求。 */
  retry: [recordId: string];
}>();

const show = computed(
  () =>
    props.stage === "executing" ||
    props.stage === "context_preparing" ||
    props.stage === "recoverable_failure",
);

const failed = computed(() => props.stage === "recoverable_failure");

const title = computed(() => {
  if (props.stage === "executing") return "已确认，正在执行话题操作…";
  if (props.stage === "context_preparing") return "话题已切换，正在准备目标上下文…";
  return String(props.recovery?.message ?? "").trim() || "话题操作没有完成，回答还没有生成。";
});

const body = computed(() => {
  if (props.stage === "recoverable_failure") {
    return "原问题与草稿都已保留；不会切回旧话题，也不会改用旧上下文回答。";
  }
  if (props.stage === "context_preparing") {
    return "准备好之后才开始回答；原问题和草稿都保留。";
  }
  return "执行结果以后端为准，不需要再操作。";
});

const canRetry = computed(
  () => failed.value && Boolean(props.recovery?.recordId) && props.recovery?.retryable === true,
);

const retryLabel = computed(() => (props.retrying ? "重试中…" : "重试回答"));

function retry(): void {
  if (!canRetry.value || props.retrying) return;
  emit("retry", String(props.recovery?.recordId ?? ""));
}
</script>

<template>
  <div
    v-if="show"
    class="topic-stage-notice"
    :class="{ failed }"
    :data-stage="stage ?? ''"
    data-test="topic-stage-notice"
    :role="failed ? 'alert' : 'status'"
  >
    <span class="topic-stage-notice__dot" aria-hidden="true"></span>
    <div class="topic-stage-notice__text">
      <p class="topic-stage-notice__title">{{ title }}</p>
      <p class="topic-stage-notice__body">{{ body }}</p>
    </div>
    <button
      v-if="canRetry"
      class="topic-stage-notice__retry"
      type="button"
      data-test="topic-stage-retry"
      :disabled="retrying"
      @click="retry"
    >
      {{ retryLabel }}
    </button>
  </div>
</template>

<style scoped>
/* 位置由挂载容器（UnifiedApprovalLayer 的提示栈）负责：这里只负责一个安静的状态块。 */
.topic-stage-notice {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  max-width: min(560px, calc(100vw - 32px));
  padding: 8px 14px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--qio-ui-radius);
  background: var(--bg-elevated);
  color: var(--text-secondary);
  font-family: var(--sans);
  font-size: 12px;
  line-height: 1.5;
  box-shadow: var(--shadow-1);
  overflow-wrap: anywhere;
}
.topic-stage-notice.failed {
  border-color: var(--danger);
  background: var(--danger-soft);
}
.topic-stage-notice__dot {
  flex: none;
  width: 6px;
  height: 6px;
  margin-top: 7px;
  border-radius: 50%;
  background: var(--accent);
}
.topic-stage-notice.failed .topic-stage-notice__dot {
  background: var(--danger);
}
.topic-stage-notice__text {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}
.topic-stage-notice__title {
  margin: 0;
  color: var(--text-strong);
  font-size: 12px;
  line-height: 1.5;
  overflow-wrap: anywhere;
}
.topic-stage-notice.failed .topic-stage-notice__title {
  color: var(--danger);
}
.topic-stage-notice__body {
  margin: 0;
  color: var(--text-muted);
  font-size: 11px;
  line-height: 1.5;
  overflow-wrap: anywhere;
}
.topic-stage-notice__retry {
  flex: none;
  align-self: center;
  height: 28px;
  padding: 0 12px;
  border: 1px solid var(--border-strong);
  border-radius: var(--qio-ui-radius);
  background: transparent;
  color: var(--text-primary);
  font-family: var(--sans);
  font-size: 12px;
  cursor: pointer;
}
.topic-stage-notice__retry:hover:not(:disabled) {
  background: var(--bg-inset);
}
.topic-stage-notice__retry:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 1px;
}
.topic-stage-notice__retry:disabled {
  opacity: 0.5;
  cursor: default;
}
</style>
