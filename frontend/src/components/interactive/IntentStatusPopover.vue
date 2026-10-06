<!--
  顶部「任务」浮层 + 靠近板面虚线预览的单项审批浮条（子智能体 D）。

  契约 §8.1 / §4.4：
  - 任务浮层放左上，右上留给批量列表、右下留给聊天，互不遮挡；
  - 单项批准 / 拒绝入口靠近对应预览：板面本身由 BoardPreviewLayer 画虚线预览，
    这里在板面下沿放一条**靠在预览附近的审批浮条**（IntentPreviewCard docked），
    点条目的「定位预览」会高亮板面上那处预览；
  - 不把演示状态说成真实执行成功：第一阶段没有接入真实执行。
-->
<script setup lang="ts">
import { computed, ref } from "vue";
import { useInteractiveStore } from "../../stores/interactive";
import { previewBounds, statusText } from "../../interactive/approval";
import type { Intent } from "../../interactive/types";
import IntentPreviewCard from "./IntentPreviewCard.vue";

const store = useInteractiveStore();

const busy = ref(false);
const notice = ref<string | null>(null);
const error = ref<string | null>(null);
/** 当前聚焦查看的任务（点「查看」时设置；用来在板面下沿放审批浮条） */
const focusedId = ref<string | null>(null);

/** 执行中 / 已暂停：需要用户决定的任务 */
const tasks = computed(() => store.activeIntents);
/** 浮条展示的意图：优先用用户刚聚焦的那一项，否则用第一项执行中的任务 */
const focused = computed<Intent | null>(() => {
  const byFocus = focusedId.value ? store.intentById(focusedId.value) : undefined;
  if (byFocus && ["running", "paused"].includes(byFocus.status)) return byFocus;
  return tasks.value[0] ?? null;
});

function label(intent: Intent): string {
  return statusText(intent).label;
}

function locate(intentId: string): void {
  focusedId.value = intentId;
}

async function resume(intent: Intent): Promise<void> {
  busy.value = true;
  error.value = null;
  try {
    const result = await store.approve(intent.id, true);
    notice.value = result.ok
      ? result.detail ?? "已确认按当前材料继续。"
      : "没有继续：" + (result.detail ?? result.reason ?? "服务端未说明原因");
  } catch (err) {
    error.value = (err as Error).message;
  } finally {
    busy.value = false;
  }
}

function boundsText(intent: Intent): string {
  const bounds = previewBounds(intent.preview);
  if (!bounds) return "预览没有可展示的位置";
  return (
    "预览位置 (" + bounds.x + ", " + bounds.y + ")，大小 " + bounds.w + "×" + bounds.h
  );
}
</script>

<template>
  <section v-if="store.tasksOpen" class="tasks-pop" data-im="tasks-popover">
    <header class="head">
      <h2 class="title">任务</h2>
      <p class="counts mono">
        执行中 {{ tasks.length }} 项 · 已结束 {{ store.finishedIntents.length }} 项
      </p>
      <button class="close" type="button" data-im="tasks-close" @click="store.tasksOpen = false">
        收起
      </button>
    </header>

    <p class="hint" role="note">
      待审批的条目走批量审批入口；这里只列已经批准、正在执行或已暂停的任务。
      第一阶段没有接入真实执行，进度来自演示入口，不能说成真实执行成功。
    </p>
    <p v-if="notice" class="notice" role="status" data-im="tasks-notice">{{ notice }}</p>
    <p v-if="error" class="error" role="alert">操作失败：{{ error }}</p>

    <p v-if="!tasks.length" class="hint">现在没有执行中或已暂停的任务。</p>
    <ul class="rows">
      <li
        v-for="intent in tasks"
        :key="intent.id"
        class="row"
        data-im="task-item"
        :data-intent-id="intent.id"
        :data-intent-status="intent.status"
      >
        <span class="task-title">
          <span v-if="intent.demo" class="badge">演示</span>
          {{ intent.title }}
        </span>
        <span class="task-status">{{ label(intent) }}</span>
        <span class="task-progress mono">{{ intent.progress.text || "进度未记录" }}</span>
        <span class="task-bounds">{{ boundsText(intent) }}</span>
        <span class="task-actions">
          <button
            class="btn ghost"
            type="button"
            data-im="task-locate"
            :data-intent-id="intent.id"
            @click="locate(intent.id)"
          >
            在板面上定位
          </button>
          <button
            v-if="intent.status === 'paused'"
            class="btn"
            type="button"
            data-im="task-resume"
            :data-intent-id="intent.id"
            :disabled="busy"
            @click="resume(intent)"
          >
            继续（按当前材料）
          </button>
        </span>
      </li>
    </ul>
  </section>

  <!-- 单项审批浮条：靠在板面下沿、按预览位置对齐，靠近板面上的虚线预览 -->
  <IntentPreviewCard
    v-if="store.tasksOpen && focused"
    :intent="focused"
    :all-intents="store.intents"
    docked
    active
  />
</template>

<style scoped>
/* 放左上：右上留给批量列表，右下留给聊天，避免浮层互相遮挡 */
.tasks-pop {
  position: absolute;
  left: var(--sp-4);
  top: var(--sp-4);
  z-index: 30;
  width: min(380px, calc(100vw - var(--sp-6)));
  max-height: min(60vh, 480px);
  overflow: auto;
  padding: var(--sp-3);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-md);
  background: var(--bg-elevated);
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}
.head {
  display: flex;
  align-items: baseline;
  gap: var(--sp-2);
  flex-wrap: wrap;
}
.title {
  margin: 0;
  flex: 1;
  font-family: var(--serif);
  font-size: var(--fs-md);
  color: var(--text-strong);
}
.counts { margin: 0; flex-basis: 100%; font-size: var(--fs-xs); color: var(--text-muted); }
.close {
  font: inherit;
  font-size: var(--fs-xs);
  color: var(--link);
  background: none;
  border: none;
  padding: 0;
  cursor: pointer;
}
.hint,
.notice,
.error {
  margin: 0;
  font-size: var(--fs-xs);
  line-height: var(--lh-base);
  color: var(--text-muted);
}
.hint { color: var(--text-faint); }
.notice { color: var(--text-secondary); }
.error { color: var(--danger); }
.rows {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}
.row {
  display: flex;
  flex-direction: column;
  gap: 2px;
  border-top: 1px solid var(--border-subtle);
  padding-top: var(--sp-1);
}
.task-title {
  font-size: var(--fs-sm);
  color: var(--text-primary);
  display: flex;
  align-items: center;
  gap: var(--sp-1);
}
.badge {
  font-size: var(--fs-xs);
  border-radius: var(--r-pill);
  padding: 0 var(--sp-1);
  border: 1px solid var(--warning);
  color: var(--warning);
  background: var(--warning-soft);
}
.task-status { font-size: var(--fs-xs); color: var(--text-secondary); }
.task-progress,
.task-bounds { font-size: var(--fs-xs); color: var(--text-faint); line-height: var(--lh-base); }
.task-actions { display: flex; gap: var(--sp-2); flex-wrap: wrap; padding-top: 2px; }
.btn {
  font: inherit;
  font-size: var(--fs-xs);
  color: var(--text-primary);
  background: var(--bg-surface);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-sm);
  padding: var(--sp-1) var(--sp-3);
  cursor: pointer;
}
.btn.ghost { background: none; color: var(--link); }
.btn:disabled { opacity: 0.55; cursor: default; }
.btn:focus-visible,
.close:focus-visible { outline: 2px solid var(--link); outline-offset: 2px; }

@media (max-width: 900px) {
  .tasks-pop { left: var(--sp-2); top: var(--sp-2); width: min(340px, calc(100vw - var(--sp-4))); }
}
</style>
