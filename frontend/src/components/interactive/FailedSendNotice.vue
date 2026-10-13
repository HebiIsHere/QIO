<!--
  失败原文的恢复入口（子智能体 B 负责实现）。

  契约：docs/interactive-mode-contract.md §11.4 / §11.8。

  普通对话页的输入区与互动模式的悬浮聊天**共用这一个组件**，不各写一套判断：
  - 显示这次尝试的真实失败原因（由入口传入，组件不猜、不改写）；
  - 逐条列出还没成功发出去的文字，提供「找回原文 / 与当前文字互换 / 不再保留」；
  - **找回不是发送**：只把文字放回输入框，什么时候再发由用户决定，绝不自动重试；
  - 恢复入口**不自开滚动层**（task-7 / 验收硬条件 5）：内容自然展开，
    同一阅读流只保留外层那一层主要纵向滚动；规模由「默认 3 条 + 查看其余 N 条」
    与未决列表分页控制，不用高度上限切成小滚动窗口。

  分层（§11.8）：当前输入是主角，这里只是辅助信息 —— 所以用退缩底色、
  次级按钮与更小的字号，不抢输入行的视觉重量。
-->
<script setup lang="ts">
import { computed, ref } from "vue";
import {
  pendingSendView,
  useSessionStore,
  type FailedSend,
  type PendingSendView,
} from "../../stores/session";
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

/**
 * §12.5：展示数量与**保留**数量分开。
 * 默认只直视较新的三条；其余收起但**一条都不删**（数据始终在会话层，可随时展开找回）。
 */
const DEFAULT_SHOWN = 3;
const expansionOpen = ref(false);

/**
 * 未决发送（F03 / F12）：还没拿到**可靠受理结论**的发送，连同完整提交快照与请求身份。
 *
 * 与「可恢复的失败原文」分开呈现：前者要**先查证再决定**，后者是已经确定没发出去的原文。
 * 只按提交快照里的话题取 —— 切到别的话题不串入这边的内容与错误。
 */
const pendingRecords = computed(() => session.unresolvedSendsForTopic(session.currentTopicId));
const pendingViews = computed<PendingSendView[]>(() =>
  pendingRecords.value.map((record) => pendingSendView(record, session.pendingSendConfirm)),
);
/** 未决列表的分页展示（数据一条不删，只是翻页看） */
const PENDING_PAGE_SIZE = 5;
const pendingPage = ref(0);
const pendingPageCount = computed(() =>
  Math.max(1, Math.ceil(pendingViews.value.length / PENDING_PAGE_SIZE)),
);
const shownPending = computed(() => {
  const page = Math.min(pendingPage.value, pendingPageCount.value - 1);
  return pendingViews.value.slice(page * PENDING_PAGE_SIZE, (page + 1) * PENDING_PAGE_SIZE);
});
function nextPendingPage(): void {
  pendingPage.value = (Math.min(pendingPage.value, pendingPageCount.value - 1) + 1) % pendingPageCount.value;
}

/**
 * 已经被未决台账覆盖的那条，不再在失败原文列表里重复出现 ——
 * 同一个发送动作只在一个地方呈现，动作也只给一处（否则用户要在两处之间猜该点哪个）。
 */
const pendingRequestIds = computed(
  () => new Set(pendingRecords.value.map((record) => record.clientRequestId)),
);
/** 只取当前话题的：失败原文始终属于原话题，切到别的话题不插入（§11.4） */
const records = computed(() =>
  session.failedSendsForTopic(session.currentTopicId).filter(
    (record) => !record.clientRequestId || !pendingRequestIds.value.has(record.clientRequestId),
  ),
);
/** 当前渲染的子集：未展开时显示**最新的** DEFAULT_SHOWN 条（其余仍有数据，不淘汰） */
const shownRecords = computed(() =>
  expansionOpen.value ? records.value : records.value.slice(0, DEFAULT_SHOWN),
);
const hiddenCount = computed(() => Math.max(0, records.value.length - DEFAULT_SHOWN));
function toggleExpansion(): void {
  expansionOpen.value = !expansionOpen.value;
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

/* ---- 未决发送的恢复动作（F03）：查证 / 同身份重试 / 查看原文 / 明确放弃 ---- */

/** 正在查看原文的那一条（只读：不改输入框、不发送、不删记录） */
const viewingPending = ref<string | null>(null);

function attachmentNote(record: PendingSendView): string {
  const count = record.attachmentIds.length;
  if (count === 0) {
    // 显式空列表也必须说清楚：重试不会顺带绑上别处的附件
    return "这次提交没有附件（提交快照里是显式空列表，重试不会绑定其他附件）";
  }
  return `这次提交带了 ${count} 个附件；重试沿用提交那一刻的同一份附件列表`;
}

function recordKeyOfPending(record: PendingSendView): string {
  return record.id;
}

async function recheck(record: PendingSendView): Promise<void> {
  actionNotice.value = "正在查证这次发送在后端有没有记录…";
  await session.recheckPendingSend(record.id);
  actionNotice.value = "查证完成：结果见上面的状态说明（没有结论时仍然是「未知」，没有被当成未发送）";
}

async function retryPending(record: PendingSendView): Promise<void> {
  const ok = await session.retrySendAttempt(record.id);
  actionNotice.value = ok
    ? "已用同一个请求身份重新提交：后端认得出这是同一次发送，不会重复执行"
    : "重试没有拿到受理回执：仍然是「未知」，原文与请求身份都保留着，可以再查证或明确放弃";
}

function viewPending(record: PendingSendView): void {
  viewingPending.value = viewingPending.value === record.id ? null : record.id;
  actionNotice.value = "查看原文只读：不改输入框、不写草稿、不发送";
}

function abandonPending(record: PendingSendView): void {
  const ok = session.abandonSendAttempt(record.id);
  actionNotice.value = ok
    ? "已明确放弃这次发送：消息撤回，原文放回输入框（还没有发送）"
    : "当前状态不允许放弃：查证已经命中受理的那条请用「取消」";
}

/**
 * F12：输入框为空时**不能**再提示「输入已保留，可以重试」。
 *
 * 原文确实还在（在独立的恢复入口里），所以要把话说准：说清它存放在哪里、入口在哪。
 * 输入框里根本没有文字、也没有任何记录时也不许虚报「已保留」。
 */
const INPUT_KEPT_NOTE = "（输入已保留，可以重试）";
const hasRecovery = computed(() => records.value.length + pendingViews.value.length > 0);
const displayNotice = computed(() => {
  const raw = props.notice;
  if (!raw) return "";
  if (!inputBlank.value) return raw;
  if (!raw.includes(INPUT_KEPT_NOTE)) return raw;
  return raw
    .split(INPUT_KEPT_NOTE)
    .join(
      hasRecovery.value
        ? "（输入框是空的：原文保存在下面的恢复入口里，不会自动重发）"
        : "（输入框是空的：这段文字没有留在输入框里）",
    );
});

/**
 * 「读取失败」与「写入失败」是两件事（收尾轮 15）：
 *
 * - 写入失败（failedSendPersistError）：这次原文没能写进本机存储，刷新后可能取不回；
 * - 读取失败（failedSendReadError）：启动时读本机存储出错 —— 它**不代表**原文已经丢失，
 *   只是这一次没读出来，修好存储后可以重新读取。
 *
 * 会话层还没提供读取失败字段时按「没有读取失败」处理：界面不会因此虚报任何状态，
 * 也绝不会在零条记录时声称「已丢失」或「已恢复」。
 */
const readError = computed(
  () => (session as unknown as { failedSendReadError?: string | null }).failedSendReadError ?? "",
);
const canRetryRead = computed(
  () => typeof (session as unknown as { retryFailedSendRestore?: () => void }).retryFailedSendRestore === "function",
);
function retryRead(): void {
  (session as unknown as { retryFailedSendRestore?: () => void }).retryFailedSendRestore?.();
}
/** 未决发送的读取失败也要能重试（F03：本机读写失败必须保留事实 + 提示 + 重试入口） */
function retryPendingRead(): void {
  const result = session.retryPendingSendRestore();
  actionNotice.value = result.ok
    ? `已重新读取本机保存的未决发送（补进 ${result.restored} 条）`
    : `重新读取仍然失败：${result.error ?? "原因未知"}`;
}
/**
 * 未决发送的本机存储错误（F03）：读不出来 / 写不进去都必须如实说，
 * 并且**不能**说成「没有未决发送」或「已经保存好了」。
 */
const pendingStorageError = computed(
  () => session.pendingSendReadError || session.pendingSendPersistError || "",
);
/** 零条记录时，只要有一个真实错误，入口本身也要出现（错误提示不能依赖「已经恢复出内容」） */
const hasStorageWarning = computed(() =>
  Boolean(readError.value || session.failedSendPersistError || pendingStorageError.value),
);
</script>

<template>
  <section
    v-if="displayNotice || records.length || pendingViews.length || hasStorageWarning"
    class="failed-send"
    data-qio-fit
    :data-im="`${hook}-recovery-wrap`"
  >
    <!-- 本次尝试的真实原因：失败后第一时间要能读到（不是只有打开详情才看得到） -->
    <p
      v-if="displayNotice"
      class="attempt"
      role="alert"
      :data-im="hook === 'chat' ? 'chat-failure' : 'composer-failure'"
    >
      {{ displayNotice }}
    </p>

    <!--
      未决发送（F03 / F12）：与「可恢复的失败原文」分开的一块。
      共同规则由会话层统一给出（pendingSendView），普通对话页与悬浮聊天读同一份 ——
      所以四个动作（查证 / 同身份重试 / 查看原文 / 明确放弃）在两个入口完全一致。
      切到别的话题时这一块整个不出现：原文、错误都不串门。
    -->
    <div
      v-if="pendingViews.length"
      class="pending"
      :data-im="`${hook}-pending`"
      role="group"
      :aria-label="`还没有确认是否发出的文字（${pendingViews.length} 条）`"
    >
      <p class="recovery-head">
        <span class="head-title">还没有确认是否发出的文字</span>
        <span class="head-count mono">{{ pendingViews.length }} 条待确认</span>
        <button
          v-if="pendingPageCount > 1"
          type="button"
          class="expansion-toggle"
          :data-im="`${hook}-pending-more`"
          @click="nextPendingPage()"
        >
          第 {{ pendingPage + 1 }} / {{ pendingPageCount }} 页 · 下一页
        </button>
      </p>
      <ul class="recovery-list">
        <li
          v-for="record in shownPending"
          :key="recordKeyOfPending(record)"
          class="recovery-item"
          :data-im="`${hook}-pending-item`"
        >
          <p
            class="pending-state"
            :class="{ unknown: record.state === 'unknown' }"
            :data-im="`${hook}-pending-state`"
          >
            {{ record.state === "unknown" ? "⚠️ " : "" }}{{ record.notice }}
          </p>
          <p v-if="record.reason" class="reason" :data-im="`${hook}-pending-reason`">
            原因：{{ record.reason }}
          </p>
          <p class="quote-state" :data-im="`${hook}-pending-attachments`">
            {{ attachmentNote(record) }}
          </p>
          <!-- 「查看原文」之前不直接铺开正文：未决发送的重点是「还没定论」，不是重发 -->
          <p v-if="viewingPending === record.id" class="quote" :data-im="`${hook}-pending-quote`">
            {{ record.text }}
          </p>
          <div class="actions">
            <button
              type="button"
              class="btn"
              :disabled="record.busy"
              :data-im="`${hook}-pending-recheck`"
              @click="recheck(record)"
            >
              查证
            </button>
            <button
              type="button"
              class="btn"
              :disabled="record.busy"
              :data-im="`${hook}-pending-retry`"
              @click="retryPending(record)"
            >
              同身份重试
            </button>
            <button
              type="button"
              class="btn ghost"
              :data-im="`${hook}-pending-view`"
              @click="viewPending(record)"
            >
              {{ viewingPending === record.id ? "收起原文" : "查看原文" }}
            </button>
            <button
              type="button"
              class="btn ghost"
              :data-im="`${hook}-pending-abandon`"
              @click="abandonPending(record)"
            >
              明确放弃
            </button>
          </div>
        </li>
      </ul>
    </div>

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
        <!-- §12.5（B 转交的结构点）：按钮放在**滚动区之外的头部**，长列表也不会把它挤出可视区 -->
        <button
          v-if="hiddenCount > 0"
          type="button"
          class="expansion-toggle"
          :data-im="`${hook}-recovery-more`"
          :aria-expanded="expansionOpen"
          @click="toggleExpansion()"
        >
          {{ expansionOpen ? "收起其余" : "查看其余 " + hiddenCount + " 条" }}
        </button>
      </p>

      <ul class="recovery-list">
        <li
          v-for="record in shownRecords"
          :key="recordKey(record)"
          class="recovery-item"
          :data-im="`${hook}-recovery-item`"
        >
          <p v-if="recordReason(record)" class="reason" :data-im="`${hook}-recovery-reason`">
            原因：{{ recordReason(record) }}
          </p>
          <!-- 原文原样显示（不截断、不限高滚动）：纵向滚动只留外层那一层 -->
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
    </div>

    <!--
      恢复动作的结果（找回 / 互换 / 放弃 / 查证 / 同身份重试）统一显示一次：
      两个块共用一句话，避免同一件事在界面上说两遍。
    -->
    <p
      v-if="actionNotice"
      class="action-notice"
      role="status"
      :data-im="`${hook}-recovery-status`"
    >
      {{ actionNotice }}
    </p>

    <!--
      存储相关的真实错误放在 records 块**外面**（收尾轮 15）：
      零条记录也可能伴随「读取失败」或「写入失败」，错误提示不能依赖已经恢复出内容。
    -->
    <p
      v-if="readError"
      class="persist-warning"
      role="alert"
      :data-im="`${hook}-recovery-read-error`"
    >
      读取本机保存的失败原文时出错：{{ readError }}（这不代表原文已经丢失；恢复写入能力后可以重新读取）
      <button
        v-if="canRetryRead"
        type="button"
        class="btn ghost retry-read"
        :data-im="`${hook}-recovery-read-retry`"
        @click="retryRead()"
      >
        重新读取
      </button>
    </p>
    <p
      v-if="session.failedSendPersistError"
      class="persist-warning"
      role="status"
      :data-im="`${hook}-recovery-persist`"
    >
      这段原文没能保存在本机，刷新后可能取不回：{{ session.failedSendPersistError }}
    </p>
    <!-- 未决发送的本机读写错误（F03）：读不出来 ≠ 没有未决发送；写不进去 ≠ 已经保存好 -->
    <p
      v-if="session.pendingSendReadError"
      class="persist-warning"
      role="alert"
      :data-im="`${hook}-pending-read-error`"
    >
      读取本机保存的未决发送时出错：{{ session.pendingSendReadError }}（这不代表这些发送不存在；恢复写入能力后可以重新读取）
      <button
        type="button"
        class="btn ghost retry-read"
        :data-im="`${hook}-pending-read-retry`"
        @click="retryPendingRead()"
      >
        重新读取
      </button>
    </p>
    <p
      v-if="session.pendingSendPersistError"
      class="persist-warning"
      role="status"
      :data-im="`${hook}-pending-persist`"
    >
      这次未决发送没能保存在本机，刷新后可能找不到它：{{ session.pendingSendPersistError }}
    </p>
  </section>
</template>

<style scoped>
/* 恢复入口整体是辅助层：退缩底色 + 次级按钮，不抢输入行的重量 */
.failed-send {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
  min-width: 0;
  /* content 策略（task-7 / 验收硬条件 5）：恢复入口**不再自开滚动层**。
     原先这里的 flex 收缩 + min-height:56px + max-height:min(34vh,200px) + overflow-y:auto
     会在同一个阅读流里再切出一层固定高度的小滚动窗口；
     现在内容自然展开，纵向滚动只留**外层那一层**（互动聊天由 ChatDock 提供滚动视口，
     对话页由输入区所在外层提供），本组件只负责把原因、原文与操作按顺序排出来。 */
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
  /* content 策略：不再限高、不再开第二层滚动（规模由「默认 3 条 + 查看其余 N 条」控制） */
}
/*
 * 未决发送块（F03/F12）：与失败原文同一套辅助层级，但状态行用警示色 ——
 * 「受理未知」不是「明确失败」，颜色也要能区分，同时不抢输入行的重量。
 */
.pending {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
  min-width: 0;
  background: var(--bg-inset);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-sm);
  padding: var(--sp-2);
  /* content 策略：与失败原文同一层阅读流，不再自开滚动（未决列表用分页控制规模） */
}
.pending-state {
  margin: 0;
  font-size: var(--fs-xs);
  line-height: 1.5;
  color: var(--warning);
  overflow-wrap: anywhere;
}
.pending-state.unknown { color: var(--danger); }
/*
 * §12.5：展开/收起按钮 —— 次级小按钮，不抢主输入的重量；
 * 底色/边框/圆角全走令牌（不硬编码颜色）。
 */
.expansion-toggle {
  align-self: flex-start;
  font: inherit;
  font-size: var(--fs-xs);
  color: var(--text-secondary);
  background: var(--bg-inset);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-xs);
  padding: 0 var(--sp-2);
  cursor: pointer;
}
.expansion-toggle:hover { color: var(--text-strong); border-color: var(--border-strong); }
.expansion-toggle:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 1px; }

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
  /* content 策略：原文完整显示（不截断、不裁切、不再自开滚动） */
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
