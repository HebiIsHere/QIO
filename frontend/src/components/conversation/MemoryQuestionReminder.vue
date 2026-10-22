<script setup lang="ts">
/**
 * 记忆问题提醒（D）。
 *
 * 记忆整理问题**不是审批**：这里只显示真实待回答数量，点击后把「打开星球 → 待确认
 * 页 → 定位某条问题」的目标交给外壳（props/事件）。本组件不改星球入口样式或功能。
 *
 * 状态口径：加载失败 ≠ 没有问题。失败时显示「无法读取」并给出重试，而不是藏起来。
 */
import { computed, onBeforeUnmount, onMounted } from "vue";
import { useMemoryQuestionsStore } from "../../stores/memoryQuestions";

const emit = defineEmits<{
  /** 外壳把它接到「打开星球并定位到待确认问题」 */
  open: [{ tab: "memory"; itemId: string | null }];
}>();

const store = useMemoryQuestionsStore();
const isError = computed(() => store.status === "error");
const text = computed(() => store.reminderText);

const RETRY_INTERVAL_MS = 30_000;
let timer = 0;

function refreshIfVisible() {
  if (typeof document !== "undefined" && document.visibilityState === "hidden") return;
  void store.load();
}

function onVisibilityChange() {
  if (document.visibilityState === "visible") void store.load();
}

onMounted(() => {
  void store.load();
  timer = window.setInterval(refreshIfVisible, RETRY_INTERVAL_MS);
  document.addEventListener("visibilitychange", onVisibilityChange);
});

onBeforeUnmount(() => {
  window.clearInterval(timer);
  document.removeEventListener("visibilitychange", onVisibilityChange);
});

function onClick() {
  if (isError.value) {
    void store.retry();
    return;
  }
  const target = store.openTarget;
  emit("open", target);
  // 外壳尚未接线时的最小兜底：给出同一个目标事件，PlanetView 侧可监听后定位。
  if (typeof window !== "undefined") {
    window.dispatchEvent(new CustomEvent("qio:open-memory-pending", { detail: target }));
  }
}
</script>

<template>
  <button
    v-if="store.shouldShow && text"
    class="memory-question-reminder"
    type="button"
    data-test="memory-question-reminder"
    :class="{ error: isError }"
    :aria-label="text + (isError ? '，点击重试读取' : '，打开记忆星球待确认')"
    @mousedown.prevent
    @click="onClick"
  >
    <span class="dot" aria-hidden="true"></span>
    <span class="label">{{ isError ? text : text }}</span>
    <span v-if="!isError" class="go mono" aria-hidden="true">星球 ›</span>
    <span v-else class="go mono" aria-hidden="true">重试</span>
  </button>
</template>

<style scoped>
/* 固定在右下角星球入口上方：只占一小条，不遮挡星球与设置。 */
.memory-question-reminder {
  position: fixed;
  right: 24px;
  bottom: 132px;
  z-index: 12;
  display: inline-flex;
  align-items: center;
  gap: var(--sp-2, 8px);
  max-width: min(360px, calc(100vw - 48px));
  padding: 6px 12px;
  border-radius: var(--r-pill);
  border: 1px solid var(--border-subtle);
  background: var(--bg-elevated);
  color: var(--text-secondary);
  font-family: var(--sans);
  font-size: 12px;
  box-shadow: var(--shadow-1);
  cursor: pointer;
  transition:
    border-color var(--dur-fast) var(--ease),
    color var(--dur-fast) var(--ease);
}
.memory-question-reminder:hover {
  border-color: var(--accent);
  color: var(--text-strong);
}
.memory-question-reminder:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 2px;
}
.memory-question-reminder.error {
  border-color: var(--warning);
  color: var(--text-strong);
}
.dot {
  flex: none;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--accent);
}
.memory-question-reminder.error .dot {
  background: var(--warning);
}
.label {
  overflow-wrap: anywhere;
}
.go {
  flex: none;
  color: var(--text-muted);
  font-size: 10.5px;
  letter-spacing: 0.04em;
}
</style>
