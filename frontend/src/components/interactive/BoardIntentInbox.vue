<!--
  对话内「待批准的板面意图」（P73 Stage 2）。

  契约（Lead 2026-10-21 冻结）：
  - 普通对话请求默认携带 interactive: { mode: "board" }；QIO 在对话里可以读板面、
    并产生 proposed / awaiting_confirmation 的板面意图（不需要提交绑定）。
  - 对话视图列出 board_default 的待批准意图（标题 / 目标 / 简短预演摘要），
    提供「确认并落地」「拒绝」；确认走 store.confirmIntent（Stage 1 的落地实现），
    成功后刷新列表；结果文字如实转述 applied 与 continuation（正在继续执行 ≠ 已完成）。
  - 刷新时机只有三种：挂载、每轮对话结束（session.turnRunning false）、批准/拒绝后；
    另加低频兜底（60s，页面隐藏时不发请求）—— 不轮询、不打断对话。
  - 读取失败不是「没有待批准意图」：保留上一次列表并如实说明。
-->
<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useSessionStore } from "../../stores/session";
import { useInteractiveStore } from "../../stores/interactive";
import { fetchIntents, rejectIntent } from "../../services/interactive";
import {
  CONVERSATION_BOARD_ID,
  boardPendingIntents,
  conversationIntentSummary,
} from "../../interactive/boardIntentInbox";
import type { Intent } from "../../interactive/types";
import { vQioScrollbar } from "../../composables/useQioScrollbars";

const session = useSessionStore();
const store = useInteractiveStore();

const intents = ref<Intent[]>([]);
const loading = ref(false);
const loadError = ref("");
const busy = ref("");
const notice = ref("");
const actionError = ref("");

/** 低频兜底：事件接不到时也能在 60s 内看到其它入口产生的提案（不轮询）。 */
const FALLBACK_REFRESH_MS = 60_000;
let fallbackTimer: ReturnType<typeof setInterval> | null = null;

function errorMessage(error: unknown): string {
  const message = (error as Error)?.message ?? String(error);
  return message || "原因未知";
}

const visible = computed(
  () =>
    intents.value.length > 0 ||
    Boolean(loadError.value) ||
    Boolean(notice.value) ||
    Boolean(actionError.value),
);

/** 读取 board_default 上未确认的板面意图（读取失败保留上一次列表，不假装没有）。 */
async function refresh(): Promise<void> {
  if (loading.value) return;
  loading.value = true;
  try {
    const payload = await fetchIntents(CONVERSATION_BOARD_ID);
    intents.value = boardPendingIntents(
      Array.isArray(payload?.intents) ? payload.intents : [],
    );
    loadError.value = "";
  } catch (error) {
    loadError.value = "读取待批准的板面意图失败：" + errorMessage(error);
  } finally {
    loading.value = false;
  }
}

function summary(intent: Intent) {
  return conversationIntentSummary(intent);
}

/** 确认并落地：走 Stage 1 的 store.confirmIntent；成功后立刻重读列表。 */
async function confirm(intent: Intent): Promise<void> {
  if (busy.value) return;
  busy.value = intent.id;
  notice.value = "";
  actionError.value = "";
  try {
    const result = await store.confirmIntent(intent.id, {
      boardId: CONVERSATION_BOARD_ID,
    });
    notice.value =
      result.detail ?? (result.ok ? "已确认：结果以服务端账本为准。" : "确认没有完成。");
    if (!result.ok) {
      actionError.value = result.detail ?? result.reason ?? "确认没有完成。";
    }
    await refresh();
  } catch (error) {
    actionError.value = "确认没有完成：" + errorMessage(error);
  } finally {
    busy.value = "";
  }
}

/** 拒绝：预览消失、板面原内容保留（走既有拒绝接口，失败如实说明）。 */
async function reject(intent: Intent): Promise<void> {
  if (busy.value) return;
  busy.value = intent.id;
  notice.value = "";
  actionError.value = "";
  try {
    const result = await rejectIntent(intent.id);
    notice.value = result.ok
      ? "已拒绝：预演消失，板面原内容保留。"
      : "没有拒绝：" + (result.detail ?? result.reason ?? "服务端未说明原因");
    if (!result.ok) actionError.value = notice.value;
    await refresh();
  } catch (error) {
    actionError.value = "拒绝没有完成：" + errorMessage(error);
  } finally {
    busy.value = "";
  }
}

function refreshWhenVisible(): void {
  if (typeof document !== "undefined" && document.visibilityState === "hidden") return;
  void refresh();
}

onMounted(() => {
  void refresh();
  window.addEventListener("focus", refreshWhenVisible);
  fallbackTimer = setInterval(refreshWhenVisible, FALLBACK_REFRESH_MS);
});

onBeforeUnmount(() => {
  window.removeEventListener("focus", refreshWhenVisible);
  if (fallbackTimer) clearInterval(fallbackTimer);
  fallbackTimer = null;
});

// 每轮对话结束后刷新一次：board 模式的提案发生在轮内，轮末才可能出现新意图
watch(
  () => session.turnRunning,
  (running, previous) => {
    if (previous && !running) void refresh();
  },
);
</script>

<template>
  <section
    v-if="visible"
    class="board-intents"
    data-im="conversation-board-intents"
    aria-label="待批准的板面意图"
  >
    <header class="bi-head">
      <span class="bi-title">待批准的板面意图</span>
      <span class="bi-count mono">{{ intents.length }} 项</span>
      <button
        class="bi-link"
        type="button"
        data-im="conversation-board-intents-refresh"
        :disabled="loading"
        @click="refresh"
      >
        {{ loading ? "刷新中…" : "刷新" }}
      </button>
    </header>

    <p v-if="loadError" class="bi-error" role="status" data-im="conversation-board-intents-error">
      {{ loadError }}
    </p>
    <p v-if="notice" class="bi-notice" role="status" data-im="conversation-board-intents-notice">
      {{ notice }}
    </p>
    <p
      v-if="actionError"
      class="bi-error"
      role="alert"
      data-im="conversation-board-intents-action-error"
    >
      {{ actionError }}
    </p>

    <ul
      v-if="intents.length"
      v-qio-scrollbar="{ policy: 'auto', axis: 'y' }"
      class="bi-list"
      data-im="conversation-board-intent-list"
    >
      <li
        v-for="intent in intents"
        :key="intent.id"
        class="bi-item"
        data-im="conversation-board-intent"
        :data-intent-id="intent.id"
        :data-status="intent.status"
      >
        <div class="bi-item-head">
          <span class="bi-item-title">{{ summary(intent).title }}</span>
          <span class="bi-status">{{ summary(intent).statusLabel }}</span>
        </div>
        <p v-if="summary(intent).goal" class="bi-row">
          <span class="bi-label">目标</span>{{ summary(intent).goal }}
        </p>
        <p class="bi-row bi-preview">
          <span class="bi-label">预演</span>{{ summary(intent).previewText }}
        </p>
        <div class="bi-actions">
          <button
            class="bi-btn primary"
            type="button"
            data-im="conversation-board-confirm"
            :data-intent-id="intent.id"
            :disabled="Boolean(busy)"
            @click="confirm(intent)"
          >
            {{ busy === intent.id ? "处理中…" : "确认并落地" }}
          </button>
          <button
            class="bi-btn"
            type="button"
            data-im="conversation-board-reject"
            :data-intent-id="intent.id"
            :disabled="Boolean(busy)"
            @click="reject(intent)"
          >
            拒绝
          </button>
        </div>
      </li>
    </ul>
    <p v-else-if="!loadError" class="bi-hint">当前没有待批准的板面意图。</p>
  </section>
</template>

<style scoped>
/*
  安静地贴在输入区上方：不遮消息流、不抢焦点；条目多时自己滚动（纵向指示条走统一策略，
  横向不建立滚动范围）。颜色全部走 tokens。
*/
.board-intents {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
  margin: 0 24px 6px;
  padding: var(--sp-2) var(--sp-3);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  background: var(--bg-surface);
  color: var(--text-primary);
  font-family: var(--sans);
  flex-shrink: 0;
  min-width: 0;
}
.bi-head {
  display: flex;
  align-items: baseline;
  gap: var(--sp-2);
  flex-wrap: wrap;
}
.bi-title {
  flex: 1;
  font-family: var(--serif);
  font-size: var(--fs-sm);
  color: var(--text-strong);
}
.bi-count,
.bi-link {
  font-size: var(--fs-xs);
  color: var(--text-muted);
}
.bi-link {
  font: inherit;
  font-size: var(--fs-xs);
  color: var(--link);
  background: none;
  border: none;
  padding: 0;
  cursor: pointer;
}
.bi-link:disabled { opacity: 0.5; cursor: default; }
.bi-link:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
.bi-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  max-height: 34vh;
  overflow-y: auto;
  overflow-x: clip;
}
.bi-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: var(--sp-2);
  border: 1px dashed var(--border-strong);
  border-radius: var(--r-md);
  background: var(--bg-elevated);
  min-width: 0;
}
.bi-item-head {
  display: flex;
  align-items: baseline;
  gap: var(--sp-2);
  flex-wrap: wrap;
}
.bi-item-title {
  flex: 1;
  min-width: 0;
  font-size: var(--fs-sm);
  color: var(--text-strong);
  overflow-wrap: anywhere;
}
.bi-status {
  font-size: var(--fs-xs);
  color: var(--warning);
  border: 1px solid var(--warning);
  border-radius: var(--r-pill);
  padding: 0 var(--sp-2);
  white-space: nowrap;
}
.bi-row {
  margin: 0;
  font-size: var(--fs-xs);
  line-height: var(--lh-base);
  color: var(--text-primary);
  overflow-wrap: anywhere;
}
.bi-label {
  display: inline-block;
  margin-right: var(--sp-1);
  color: var(--text-muted);
}
.bi-preview { color: var(--text-secondary); }
.bi-actions {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  flex-wrap: wrap;
  padding-top: var(--sp-1);
}
.bi-btn {
  font: inherit;
  font-size: var(--fs-xs);
  color: var(--text-primary);
  background: var(--bg-surface);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-pill);
  padding: 2px var(--sp-3);
  cursor: pointer;
}
.bi-btn:hover { border-color: var(--link); }
.bi-btn:disabled { opacity: 0.55; cursor: default; }
.bi-btn:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
.bi-btn.primary {
  color: var(--on-accent);
  background: var(--accent);
  border-color: var(--accent);
}
.bi-btn.primary:hover { background: var(--accent-hover); }
.bi-hint,
.bi-notice,
.bi-error {
  margin: 0;
  font-size: var(--fs-xs);
  line-height: var(--lh-base);
  overflow-wrap: anywhere;
}
.bi-hint { color: var(--text-muted); }
.bi-notice { color: var(--text-secondary); }
.bi-error { color: var(--danger); }
@media (max-width: 720px) {
  .board-intents { margin: 0 var(--sp-3) 6px; }
}
</style>
