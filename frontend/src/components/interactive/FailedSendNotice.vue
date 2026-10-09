<!--
  失败原文的恢复入口（子智能体 B 负责实现）。

  契约：docs/interactive-mode-contract.md §11.4 / §11.8。

  普通对话页的输入区与互动模式的悬浮聊天**共用这一个组件**，不各写一套判断：
  - 显示这次尝试的真实失败原因（由入口传入，组件不猜、不改写）；
  - 逐条列出还没成功发出去的文字，提供「找回原文 / 与当前文字互换 / 不再保留」；
  - **找回不是发送**：只把文字放回输入框，什么时候再发由用户决定，绝不自动重试；
  - 原文较长时限高滚动，恢复入口再怎么多也不会挤掉输入框与发送按钮。

  分层（§11.8）：当前输入是主角，这里只是辅助信息 —— 所以用退缩底色、
  次级按钮与更小的字号，不抢输入行的视觉重量。
-->
<script setup lang="ts">
import { computed, ref } from "vue";
import { useSessionStore, type FailedSend } from "../../stores/session";
import { isBlankText, normalizeSendText } from "../../interactive/chat";

const props = withDefaults(
  defineProps<{
    /** 入口作用域：决定自动化钩子前缀（chat = 悬浮聊天，conversation = 对话页） */
    scope?: "chat" | "conversation";
    /** 本次尝试的失败文案（含真实原因）；为空时只显示待恢复原文 */
    notice?: string;
    /** 本次尝试的真实原因（与记录自带的原因相同时不重复显示一遍） */
    reason?: string | null;
  }>(),
  { scope: "chat", notice: "", reason: null },
);

const session = useSessionStore();
/** 最近一次恢复操作的结果（成功或失败都说清楚，不静默） */
const actionNotice = ref("");

/** 只取当前话题的：失败原文始终属于原话题，切到别的话题不插入（§11.4） */
const records = computed(() => session.failedSendsForTopic(session.currentTopicId));
/**
 * 展示数量与**保留数量**分开（契约 §12.5）：默认只展开最新几条，
 * 其余以「查看其余」收起 —— 收起只影响显示，**隐藏不等于删除**，
 * store 里未处理的失败原文一条都不会少。结构见下方模板改动点，
 * 仅使用既有 class（btn ghost），未新增样式令牌。
 */
const FAILED_SEND_VISIBLE_DEFAULT = 3;
const recoveryExpanded = ref(false);
const visibleRecords = computed(() =>
  recoveryExpanded.value ? records.value : records.value.slice(0, FAILED_SEND_VISIBLE_DEFAULT),
);
const hiddenCount = computed(() => Math.max(0, records.value.length - visibleRecords.value.length));

function revealMore(): void {
  recoveryExpanded.value = true;
  actionNotice.value = "已展开其余的失败原文（都还保留着，隐藏不等于删除）";
}
/** 自动化钩子前缀：悬浮聊天沿用既有 chat-*，对话页用 composer-*（scope 仍用于样式分层） */
const hook = computed(() => (props.scope === "chat" ? "chat" : "composer"));
const inputBlank = computed(() => isBlankText(session.draft));

function recordKey(record: FailedSend): string {
  return record.id ?? `legacy-${record.at}-${record.text.length}`;
}

/** 这条记录自己的原因；与顶部本次尝试的原因相同时不重复显示 */
function recordReason(record: FailedSend): string {
  const reason = record.id ? session.failedSendErrors[record.id] ?? "" : session.failedSendError ?? "";
  return reason === (props.reason ?? "") ? "" : reason;
}

/** 输入框里现在就是这条失败原文吗（是的话不需要再「找回」，也不会被覆盖） */
function originalInInput(record: FailedSend): boolean {
  return !isBlankText(session.draft) && normalizeSendText(session.draft) === normalizeSendText(record.text);
}

function restore(record: FailedSend): void {
  const result = session.retryFailedSend(record.id);
  if (result.ok && result.restored) {
    actionNotice.value = "已把这段原文放回输入框（还没有发送）";
    return;
  }
  actionNotice.value = result.reason ?? (result.ok ? "原文已经在输入框里" : "现在不能取回这段文字");
}

function swap(record: FailedSend): void {
  const result = session.swapFailedSendText(record.id);
  actionNotice.value =
    result.ok && result.swapped
      ? "已与当前文字互换：输入框里是失败原文，刚才的文字留在恢复入口"
      : result.reason ?? "现在不能取回这段文字";
}

function discard(record: FailedSend): void {
  session.discardFailedSend(record.id);
  actionNotice.value = "已不再保留这段失败原文";
}
</script>

<template>
  <section
    v-if="notice || records.length"
    class="failed-send"
    :data-im="`${hook}-recovery-wrap`"
  >
    <!-- 本次尝试的真实原因：失败后第一时间要能读到（不是只有打开详情才看得到） -->
    <p
      v-if="notice"
      class="attempt"
      role="alert"
      :data-im="hook === 'chat' ? 'chat-failure' : 'composer-failure'"
    >
      {{ notice }}
    </p>

    <div
      v-if="records.length"
      class="recovery"
      :class="`scope-${scope}`"
      :data-im="`${hook}-recovery`"
      role="group"
      :aria-label="records.length > 1 ? `上一次没有发出去的文字（${records.length} 条）` : '上一次没有发出去的文字'"
    >
      <p class="recovery-head">
        <span class="head-title">上一次没有发出去的文字</span>
        <span v-if="records.length > 1" class="head-count mono">{{ records.length }} 条待找回</span>
      </p>

      <ul class="recovery-list">
        <li
          v-for="record in visibleRecords"
          :key="recordKey(record)"
          class="recovery-item"
          :data-im="`${hook}-recovery-item`"
        >
          <p v-if="recordReason(record)" class="reason" :data-im="`${hook}-recovery-reason`">
            原因：{{ recordReason(record) }}
          </p>
          <!-- 原文原样显示（不截断）：限高滚动，长文不会把输入区挤走 -->
          <p class="quote" :data-im="`${hook}-recovery-quote`">{{ record.text }}</p>
          <p
            v-if="originalInInput(record)"
            class="quote-state"
            :data-im="`${hook}-recovery-in-input`"
          >
            这段原文已经在输入框里，还没有发出去
          </p>
          <div class="actions">
            <button
              v-if="inputBlank"
              type="button"
              class="btn"
              :data-im="`${hook}-recovery-restore`"
              @click="restore(record)"
            >
              找回原文
            </button>
            <button
              v-else-if="!originalInInput(record)"
              type="button"
              class="btn"
              :data-im="`${hook}-recovery-swap`"
              @click="swap(record)"
            >
              与当前文字互换
            </button>
            <button
              type="button"
              class="btn ghost"
              :data-im="`${hook}-recovery-discard`"
              @click="discard(record)"
            >
              不再保留
            </button>
          </div>
        </li>
      </ul>

      <!--
        「查看其余」（§12.5）：默认收起的其余失败原文的展开入口。
        这些记录本就完整保留在 store / 本机存储里，展开按钮不影响任何清理。
      -->
      <button
        v-if="hiddenCount > 0"
        type="button"
        class="btn ghost"
        :data-im="`${hook}-recovery-more`"
        @click="revealMore"
      >
        查看其余 {{ hiddenCount }} 条
      </button>

      <p
        v-if="actionNotice"
        class="action-notice"
        role="status"
        :data-im="`${hook}-recovery-status`"
      >
        {{ actionNotice }}
      </p>
      <p
        v-if="session.failedSendPersistError"
        class="persist-warning"
        role="status"
        :data-im="`${hook}-recovery-persist`"
      >
        这段原文没能保存在本机，刷新后可能取不回：{{ session.failedSendPersistError }}
      </p>
    </div>
  </section>
</template>

<style scoped>
/* 恢复入口整体是辅助层：退缩底色 + 次级按钮，不抢输入行的重量 */
.failed-send {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
  min-width: 0;
  /**
   * **必须可收缩**（契约 §11.7 / §11.8）。
   *
   * 面板是一个纵向 flex 容器、且 `overflow: hidden`；恢复块如果按内容高度长下去，
   * 会把下面的输入行与发送按钮顶出面板、被直接裁掉 —— 480×600 实机复现：
   * 面板底边 348，输入框 top 383、发送按钮 top 478，elementFromPoint 都命中不到自己。
   * 所以这里显式允许收缩（flex: 0 1 auto + min-height: 0）并限高滚动：
   * 内容再长，输入框与发送按钮也始终留在面板里。
   */
  flex: 0 1 auto;
  /* 既不许把输入行顶出面板，也不许自己被压没：至少留出「原因 + 一个按钮」的高度 */
  min-height: 56px;
  max-height: min(34vh, 200px);
  overflow-y: auto;
  overscroll-behavior: contain;
}
.attempt {
  margin: 0;
  font-size: var(--fs-xs);
  line-height: 1.5;
  color: var(--danger);
  /* 板面后方有密集文字/连线时也要读得清：给一层稳定的局部底色 */
  background: var(--bg-elevated);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-sm);
  padding: var(--sp-1) var(--sp-2);
}
.recovery {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
  min-width: 0;
  background: var(--bg-inset);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-sm);
  padding: var(--sp-2);
  /* 限高滚动：原文再长、待找回的再多，也不挤掉输入框与发送按钮（§11.8） */
  overflow-y: auto;
  overscroll-behavior: contain;
}
.scope-chat { max-height: min(26vh, 200px); }
.scope-conversation { max-height: min(30vh, 240px); }
.recovery-head {
  display: flex;
  align-items: baseline;
  gap: var(--sp-2);
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--text-muted);
}
.head-title { color: var(--text-secondary); }
.head-count { color: var(--text-muted); }
.recovery-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  min-width: 0;
}
.recovery-item {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
  min-width: 0;
  border-top: 1px solid var(--border-subtle);
  padding-top: var(--sp-1);
}
.recovery-item:first-child { border-top: 0; padding-top: 0; }
.reason {
  margin: 0;
  font-size: var(--fs-xs);
  line-height: 1.5;
  color: var(--danger);
  overflow-wrap: anywhere;
}
.quote {
  margin: 0;
  font-size: var(--fs-sm);
  line-height: var(--lh-tight);
  color: var(--text-primary);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  /* 单条原文自己再限一层高：一条超长原文也不会占满整个面板 */
  max-height: 5.5em;
  overflow-y: auto;
  overscroll-behavior: contain;
}
.quote-state {
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--text-muted);
}
.actions {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2);
}
.btn {
  font: inherit;
  font-family: var(--sans);
  /* 主要操作不缩成元信息字号（§10.8）：按钮文字能看清才谈得上「直接说明结果」 */
  font-size: var(--fs-sm);
  color: var(--text-strong);
  background: var(--bg-elevated);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-pill);
  padding: 2px var(--sp-3);
  cursor: pointer;
}
.btn:hover { border-color: var(--link); color: var(--link); }
.btn:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
.btn.ghost {
  color: var(--text-secondary);
  background: transparent;
  border-color: var(--border-subtle);
}
.btn.ghost:hover { color: var(--danger); border-color: var(--danger); }
.action-notice {
  margin: 0;
  font-size: var(--fs-xs);
  line-height: 1.5;
  color: var(--text-muted);
}
.persist-warning {
  margin: 0;
  font-size: var(--fs-xs);
  line-height: 1.5;
  color: var(--warning);
  overflow-wrap: anywhere;
}
/* 减少动态效果：本组件不引入任何动画/位移，恢复操作即时生效 */
</style>
