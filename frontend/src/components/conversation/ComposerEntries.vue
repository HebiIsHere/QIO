<script setup lang="ts">
/**
 * 输入框正上方紧邻的五个图标入口（B）。
 *
 * 规则（契约 §6）：
 * - 有事项才显示；没有就不占位置（不常驻横幅）；
 * - 默认全部收起；每次最多展开一个；打开另一项收起当前；再点当前收起；
 * - 收起 ≠ 处理完成 / 忽略 / 删除 —— 面板只是折叠，记录与状态原样保留；
 * - 待审批入口**不展开面板**：它直接打开统一审批弹窗（D 的实现），并收起其余四项。
 *
 * 图标本身不自证用途：每个入口都有 title / aria-label / aria-expanded，键盘可聚焦。
 */
import { computed, ref, watch } from "vue";
import { useSessionStore } from "../../stores/session";
import { useApprovalsStore } from "../../stores/approvals";
import { vQioScrollbar } from "../../composables/useQioScrollbars";
import SendIssuesPanel from "./SendIssuesPanel.vue";
import QueuePanel from "./QueuePanel.vue";
import {
  COMPOSER_ENTRY_TEXT,
  continuationText,
  deriveComposerEntries,
  snippetOf,
  type ComposerEntryId,
} from "./entries";

const session = useSessionStore();
const approvals = useApprovalsStore();

/** 当前展开的入口（null = 全部收起） */
const openEntry = ref<ComposerEntryId | null>(null);

const entries = computed(() =>
  deriveComposerEntries({
    sendIssues: {
      failedSends: session.failedSendsForTopic(session.currentTopicId),
      pendingSends: session.unresolvedSendsForTopic(session.currentTopicId),
      displacedDrafts: session.displacedDraftsForTopic(session.currentTopicId),
      interruptedTurnIds: session.interruptedTurns.map((turn) => turn.turn_id),
      interruptedNotice: session.interruptedNotice,
      draftSaveFailed: session.draftSaveStatus === "error",
      storageWriteError: session.failedSendPersistError,
      storageReadError: session.failedSendReadError,
    },
    continuation: {
      hasPendingIntent: Boolean(session.pendingContinuation),
      anchorHistoric: session.anchorHistoric,
    },
    topicSwitch: Boolean(session.pendingSwitch),
    queue: session.turnQueue,
    localQueued: session.messages
      .filter((message) => message.queued && !message.turnId)
      .map((message) => ({ id: message.id, content: message.content })),
    approvals: approvals.pendingCount,
  }),
);

const visibleEntries = computed(() => entries.value.filter((entry) => entry.visible));

/** 展开中的入口如果不再有事项（被处理完 / 切了话题），面板自动收起（状态本身不动）。 */
watch(visibleEntries, (list) => {
  if (!openEntry.value) return;
  if (!list.some((entry) => entry.id === openEntry.value)) openEntry.value = null;
});

function toggle(id: ComposerEntryId) {
  if (id === "approvals") {
    // 审批：直接打开统一弹窗并收起其余四项（不在这里放内联批准/拒绝按钮）
    openEntry.value = null;
    approvals.openNow();
    return;
  }
  openEntry.value = openEntry.value === id ? null : id;
}

function close() {
  openEntry.value = null;
}

/** Esc 收起当前面板（不动任何记录） */
function onKeydown(event: KeyboardEvent) {
  if (event.key !== "Escape" || !openEntry.value) return;
  event.stopPropagation();
  close();
}

const continuationBusy = ref(false);
const continuationNotice = ref("");
const continuationFailed = ref(false);

async function cancelContinuation() {
  if (continuationBusy.value) return;
  continuationBusy.value = true;
  continuationNotice.value = "";
  continuationFailed.value = false;
  try {
    const ok = await session.cancelPendingContinuation();
    continuationNotice.value = ok
      ? "已取消：下一条消息不再从所选历史继续"
      : "没有取消：起点未改变（原因见上方提示）";
    continuationFailed.value = !ok;
  } finally {
    continuationBusy.value = false;
  }
}

const switchBusy = ref(false);
const switchNotice = ref("");
const switchFailed = ref(false);

async function decideSwitch(switchTo: boolean) {
  if (switchBusy.value) return;
  switchBusy.value = true;
  switchNotice.value = "";
  switchFailed.value = false;
  try {
    if (switchTo) await session.confirmPendingSwitch();
    else await session.rejectPendingSwitch();
    const error = session.lastError;
    if (error) {
      switchNotice.value = error;
      switchFailed.value = true;
    } else {
      switchNotice.value = switchTo
        ? "已提交切换：等后端确认后生效"
        : "已保留当前话题（这段内容仍属于当前对话）";
    }
  } finally {
    switchBusy.value = false;
  }
}

/** 接续说明（不暴露 intent_id 这类内部术语） */
const continuationDetail = computed(() => continuationText(session.pendingContinuation));
const anchorTitle = computed(() => session.anchorFragment?.title ?? "");

const panelTitle = computed(() =>
  openEntry.value ? COMPOSER_ENTRY_TEXT[openEntry.value] : null,
);
</script>

<template>
  <div class="composer-entries" data-qio-fit @keydown="onKeydown">
    <!-- 面板在前（贴着输入区向上生长），图标行紧随输入框上缘 -->
    <transition name="qio-rise">
      <section
        v-if="openEntry && panelTitle"
        class="panel"
        role="region"
        :data-panel="openEntry"
        :aria-label="panelTitle.title"
        data-qio-fit
      >
        <header class="panel-head">
          <span class="panel-title">{{ panelTitle.title }}</span>
          <span class="panel-hint mono">{{ panelTitle.hint }}</span>
          <button
            class="panel-close"
            type="button"
            :aria-label="`收起${panelTitle.title}（不会处理或删除其中事项）`"
            @click="close"
          >
            ×
          </button>
        </header>
        <div class="panel-body" data-qio-fit v-qio-scrollbar="{ policy: 'auto', axis: 'y' }">
          <SendIssuesPanel v-if="openEntry === 'sendIssues'" />
          <QueuePanel v-else-if="openEntry === 'queue'" @open-send-issues="openEntry = 'sendIssues'" />

          <div v-else-if="openEntry === 'continuation'" class="simple" data-section="continuation">
            <p class="line">{{ continuationDetail }}</p>
            <p v-if="session.pendingContinuation" class="sub">
              已经登记了接续选择，但还没有落实：下一条消息发出时才生效。收起这个面板不会取消已选位置。
            </p>
            <p v-else class="sub">
              当前位置停在历史片段<template v-if="anchorTitle">「{{ anchorTitle }}」</template>上，下一条消息会从这里继续。
              收起这个面板不会改变起点。
            </p>
            <p v-if="continuationNotice" class="feedback" :class="{ err: continuationFailed }" role="status">
              {{ continuationNotice }}
            </p>
            <button
              v-if="session.pendingContinuation"
              class="qio-btn mini quiet"
              type="button"
              :disabled="continuationBusy"
              @click="cancelContinuation"
            >
              {{ continuationBusy ? "取消中…" : "取消这个接续选择" }}
            </button>
          </div>

          <div v-else-if="openEntry === 'topicSwitch' && session.pendingSwitch" class="simple" data-section="topic-switch">
            <p class="line">转到「{{ session.pendingSwitch.topicName }}」？</p>
            <p class="sub">
              这段内容看起来属于另一个话题。是否切换由你决定，QIO 不会自行切换。
              <template v-if="session.pendingSwitch.reason">{{ session.pendingSwitch.reason }}</template>
            </p>
            <p v-if="switchNotice" class="feedback" :class="{ err: switchFailed }" role="status">
              {{ switchNotice }}
            </p>
            <div class="actions">
              <button
                class="qio-btn mini quiet"
                type="button"
                :disabled="switchBusy || session.pendingSwitchBusy"
                @click="decideSwitch(false)"
              >
                保留当前
              </button>
              <button
                class="qio-btn mini primary"
                type="button"
                :disabled="switchBusy || session.pendingSwitchBusy"
                @click="decideSwitch(true)"
              >
                {{ switchBusy || session.pendingSwitchBusy ? "切换中…" : "转到这里" }}
              </button>
            </div>
          </div>
        </div>
      </section>
    </transition>

    <div
      v-if="visibleEntries.length"
      class="entry-row"
      role="toolbar"
      aria-label="输入区快捷入口"
      data-qio-fit
    >
      <button
        v-for="entry in visibleEntries"
        :key="entry.id"
        class="entry-btn"
        type="button"
        :data-entry="entry.id"
        :data-tone="entry.tone"
        :aria-expanded="openEntry === entry.id ? 'true' : 'false'"
        :aria-label="`${COMPOSER_ENTRY_TEXT[entry.id].title}：${COMPOSER_ENTRY_TEXT[entry.id].hint}${entry.count > 0 ? `（${entry.count} 项）` : ''}`"
        :title="`${COMPOSER_ENTRY_TEXT[entry.id].title} — ${COMPOSER_ENTRY_TEXT[entry.id].hint}`"
        @click="toggle(entry.id)"
      >
        <svg class="ico" viewBox="0 0 16 16" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round">
          <template v-if="entry.id === 'sendIssues'">
            <path d="M8 1.9 14.7 13.4H1.3Z" />
            <path d="M8 6v3.1" />
            <circle cx="8" cy="11.4" r="0.55" fill="currentColor" stroke="none" />
          </template>
          <template v-else-if="entry.id === 'continuation'">
            <path d="M8 2.6a5.4 5.4 0 1 1-5.2 3.9" />
            <path d="M2.4 2.6v3.9h3.9" />
            <path d="M8 5.4V8l1.9 1.2" />
          </template>
          <template v-else-if="entry.id === 'topicSwitch'">
            <path d="M2.2 5.2h10.2M9.6 2.4l2.8 2.8-2.8 2.8" />
            <path d="M13.8 10.8H3.6M6.4 8l-2.8 2.8 2.8 2.8" />
          </template>
          <template v-else-if="entry.id === 'queue'">
            <path d="M2.6 4h10.8M2.6 8h10.8M2.6 12h6.2" />
          </template>
          <template v-else>
            <path d="M8 1.6 13.5 3.6v4.1c0 3-2.2 5.2-5.5 6.4-3.3-1.2-5.5-3.4-5.5-6.4V3.6Z" />
            <path d="M5.6 7.8 7.3 9.5l3.2-3.4" />
          </template>
        </svg>
        <span v-if="entry.count > 0" class="count mono">{{ entry.count }}</span>
        <span v-else-if="entry.tone === 'warn'" class="dot" aria-hidden="true"></span>
        <span v-if="entry.id === 'continuation'" class="entry-label">继续</span>
        <span v-else-if="entry.id === 'queue'" class="entry-label">排队</span>
        <span v-else-if="entry.id === 'approvals'" class="entry-label">审批</span>
      </button>
    </div>
  </div>
</template>

<style scoped>
.composer-entries {
  min-width: 0;
}
.panel {
  margin-bottom: 8px;
  min-width: 0;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  background: var(--bg-inset);
  overflow: hidden;
}
.panel-head {
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 8px;
  padding: 7px 10px 4px;
  min-width: 0;
}
.panel-title {
  font-family: var(--serif);
  font-size: var(--fs-sm);
  color: var(--text-strong);
}
.panel-hint {
  flex: 1;
  min-width: 0;
  color: var(--text-faint);
  font-size: 10.5px;
  overflow-wrap: anywhere;
}
.panel-close {
  flex: none;
  border: none;
  background: none;
  padding: 0 2px;
  color: var(--text-muted);
  font-size: 15px;
  line-height: 1;
  cursor: pointer;
}
.panel-close:hover {
  color: var(--text-strong);
}
.panel-close:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 1px;
}
.panel-body {
  max-height: min(34vh, 320px);
  overflow-y: auto;
  overflow-x: clip;
  padding: 4px 10px 10px;
  min-width: 0;
}
.simple {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
}
.simple .line {
  margin: 0;
  color: var(--text-primary);
  font-size: var(--fs-sm);
  line-height: 1.65;
  overflow-wrap: anywhere;
}
.simple .sub {
  margin: 0;
  color: var(--text-muted);
  font-size: var(--fs-xs);
  line-height: 1.65;
  overflow-wrap: anywhere;
}
.actions {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 2px;
}
.simple .qio-btn {
  height: auto;
  padding: 3px 10px;
  font-size: 11.5px;
  border-radius: var(--r-sm);
}
.feedback {
  margin: 0;
  color: var(--text-secondary);
  font-size: var(--fs-xs);
  overflow-wrap: anywhere;
}
.feedback.err {
  color: var(--danger);
}
.entry-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 4px;
  min-width: 0;
  padding: 0 0 6px;
}
.entry-btn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  height: 26px;
  padding: 0 8px;
  border: 1px solid transparent;
  border-radius: var(--r-pill);
  background: transparent;
  color: var(--text-secondary);
  font-family: var(--sans);
  font-size: var(--fs-xs);
  cursor: pointer;
  transition: color var(--dur-fast) var(--ease-1), border-color var(--dur-fast) var(--ease-1),
    background var(--dur-fast) var(--ease-1);
}
.entry-btn:hover,
.entry-btn[aria-expanded="true"] {
  color: var(--text-strong);
  border-color: var(--border-subtle);
  background: var(--layer-hover);
}
.entry-btn:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 1px;
}
.entry-btn[data-tone="warn"] {
  color: var(--warning);
}
.entry-btn[data-tone="warn"]:hover {
  color: var(--warning);
  border-color: var(--warning);
}
.entry-btn .ico {
  width: 14px;
  height: 14px;
  flex: none;
}
.entry-btn .count {
  min-width: 15px;
  padding: 0 4px;
  border-radius: var(--r-pill);
  background: var(--bg-elevated);
  color: var(--text-secondary);
  font-size: 10px;
  line-height: 14px;
  text-align: center;
}
.entry-btn .dot {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: var(--warning);
}
.entry-label {
  letter-spacing: 0.02em;
}
</style>
