<script setup lang="ts">
import { vQioTip } from "../../composables/useQioTip";
/**
 * 入口①：发送异常与草稿恢复（B）。
 *
 * 覆盖的真实事项（全部来自现有 store，不新建状态源）：
 * - 受理未确认的发送（pendingSends，按请求身份查证 / 同身份重试 / 放弃，不自动重发）；
 * - 明确失败的可恢复原文（failedSends，找回 / 互换 / 不再保留）；
 * - 上次退出时没有执行的消息（interruptedTurns，继续 / 忽略，不自动重放）；
 * - 被让位的旧草稿（chatDraftDisplaced，取回不覆盖当前输入）；
 * - 草稿保存失败 / 失败原文存储读写失败（如实说明 + 真实重试入口）。
 *
 * 收起面板≠处理完成：这里没有任何「忽略全部」把未处理事项静默清掉；
 * 每条记录都要用户明确点一次，且动作失败会留在原地。
 */
import { computed, ref } from "vue";
import { useSessionStore } from "../../stores/session";
import { pendingSendStateText, snippetOf, whenText } from "./entries";

const session = useSessionStore();

const failed = computed(() => session.failedSendsForTopic(session.currentTopicId));
const pending = computed(() => session.unresolvedSendsForTopic(session.currentTopicId));

/** 当前正在查证的那条（store 的唯一「发送确认」状态）：文案与可用动作都按它来 */
const activeConfirm = computed(() => session.pendingSendConfirm);

function confirmFor(id: string) {
  const current = activeConfirm.value;
  return current && current.messageId === id ? current : null;
}

/** 原样显示 store 的查证文案（「正在确认」/「发送未确认」是两件不同的事） */
function pendingNotice(record: (typeof pending.value)[number]): string {
  const current = confirmFor(record.id);
  if (current) return current.notice;
  if (record.lastCheck) return `上次查证：${record.lastCheck}`;
  return record.lastError ?? pendingSendStateText(record.state);
}

function pendingBusy(id: string): boolean {
  const current = confirmFor(id);
  if (current?.busy) return true;
  return busy.value === `check:${id}` || busy.value === `retrySend:${id}` || busy.value === `cancel:${id}`;
}

/** 已受理的发送不再提供「重试 / 放弃」：只有取消（与停止同一条通道） */
function canRetry(record: { id: string; state: string }): boolean {
  const current = confirmFor(record.id);
  return current ? !current.accepted : record.state !== "accepted";
}

/** 只有后端明确「没有这条请求记录」时才给放弃（避免把已受理的消息撤掉） */
function canAbandon(record: { id: string; state: string }): boolean {
  const current = confirmFor(record.id);
  return current ? current.unknown : record.state === "needs-confirm";
}

function canCancel(record: { id: string; state: string }): boolean {
  const current = confirmFor(record.id);
  return current ? current.accepted : record.state === "accepted";
}
const displaced = computed(() => session.displacedDraftsForTopic(session.currentTopicId));
const interrupted = computed(() => session.interruptedTurns);
const interruptedNotice = computed(() => session.interruptedNotice);

const draftSaveFailed = computed(() => session.draftSaveStatus === "error");
const storageWriteError = computed(() => session.failedSendPersistError ?? "");
const storageReadError = computed(() => session.failedSendReadError ?? "");

/** 正在提交的动作（`kind:id`）：同一事项只允许一个请求在飞，防重复提交 */
const busy = ref("");
/** 最近一次动作的真实结果：失败原因优先，成功也说清楚（不静默） */
const notice = ref("");
const noticeFailed = ref(false);

function begin(key: string): boolean {
  if (busy.value) return false;
  busy.value = key;
  notice.value = "";
  noticeFailed.value = false;
  return true;
}

function finish(): void {
  busy.value = "";
}

function say(message: string, failedState = false): void {
  notice.value = message;
  noticeFailed.value = failedState;
}

function reasonOfFailed(id: string | undefined, index: number): string {
  if (id && session.failedSendErrors[id]) return session.failedSendErrors[id];
  if (id && session.failedSend?.id === id && session.failedSendError) return session.failedSendError;
  void index;
  return "原因未记录";
}

function retryFailed(id: string | undefined, index: number): void {
  if (!begin(`failed:${id ?? index}`)) return;
  try {
    const result = session.retryFailedSend(id);
    if (!result.ok && !result.restored) {
      say(result.reason ?? "现在不能取回这段文字", true);
      return;
    }
    say(result.reason ?? "原文已放回输入框（还没有发送，确认后再按发送）");
  } finally {
    finish();
  }
}

function swapFailed(id: string | undefined, index: number): void {
  if (!begin(`swap:${id ?? index}`)) return;
  try {
    const result = session.swapFailedSendText(id);
    if (!result.ok) {
      say(result.reason ?? "现在不能互换", true);
      return;
    }
    say(
      result.reason ??
        "已与输入框里的文字互换：两份都还在，这条记录继续保留到真正发出",
    );
  } finally {
    finish();
  }
}

function discardFailed(id: string | undefined, index: number): void {
  if (!begin(`discard:${id ?? index}`)) return;
  try {
    session.discardFailedSend(id);
    say("已不再保留这条失败原文（输入框与草稿不受影响）");
  } finally {
    finish();
  }
}

async function recheckPending(id: string): Promise<void> {
  if (!begin(`check:${id}`)) return;
  try {
    await session.recheckSendAttempt(id);
    const current = session.pendingSendConfirm;
    say(
      current && current.messageId === id
        ? current.notice
        : "已重新查证：这条发送的状态仍以下面的说明为准",
    );
  } catch (e) {
    say(`查证没有成功：${(e as Error).message}（可以再查）`, true);
  } finally {
    finish();
  }
}

async function retryPending(id: string): Promise<void> {
  if (!begin(`retrySend:${id}`)) return;
  try {
    const ok = await session.retrySendAttempt(id);
    say(ok ? "已用同一个请求身份重试（不会创建重复消息）" : "重试没有成功（原因见本条）", !ok);
  } finally {
    finish();
  }
}

async function cancelAccepted(id: string): Promise<void> {
  if (!begin(`cancel:${id}`)) return;
  try {
    const ok = await session.cancelConfirmedSend(id);
    say(
      ok
        ? "已发出取消请求（等这一轮真正结束；不是「没有发送」）"
        : "现在不能取消这条（可能已经不是等待状态，请再查一次）",
      !ok,
    );
  } finally {
    finish();
  }
}

function abandonPending(id: string): void {
  if (!begin(`abandon:${id}`)) return;
  try {
    const ok = session.abandonSendAttempt(id);
    say(
      ok
        ? "已放弃这条发送：原话放回输入框（不会自动发送）"
        : "现在不能放弃这条（可能已经受理，请先看查证结果）",
      !ok,
    );
  } finally {
    finish();
  }
}

async function resumeInterrupted(id: string): Promise<void> {
  if (!begin(`resume:${id}`)) return;
  try {
    const result = await session.resumeInterruptedTurn(id);
    say(result.message, !result.ok);
  } finally {
    finish();
  }
}

async function dismissInterrupted(id: string): Promise<void> {
  if (!begin(`dismiss:${id}`)) return;
  try {
    const result = await session.dismissInterruptedTurn(id);
    say(result.message, !result.ok);
  } finally {
    finish();
  }
}

async function dismissAllInterrupted(): Promise<void> {
  if (!begin("dismiss:all")) return;
  try {
    const result = await session.dismissAllInterruptedTurns();
    say(result.message, !result.ok);
  } finally {
    finish();
  }
}

function restoreDisplaced(id: string): void {
  if (!begin(`draft:${id}`)) return;
  try {
    const result = session.restoreDisplacedDraft(id);
    say(result.reason ?? "已取回那份草稿：输入框是它，刚才的文字留在入口里（两份都还在）");
  } finally {
    finish();
  }
}

function discardDisplaced(id: string): void {
  if (!begin(`draftDiscard:${id}`)) return;
  try {
    session.discardDisplacedDraft(id);
    say("已放弃这份被让位的草稿（当前输入不受影响）");
  } finally {
    finish();
  }
}

function retryDraftSave(): void {
  session.retryDraftSave();
  say("已重试把草稿保存到本机（不会发送消息）");
}

function retryStorageRead(): void {
  if (!begin("storage:read")) return;
  try {
    const result = session.retryFailedSendRestore();
    say(
      result.ok
        ? result.restored > 0
          ? `已从本机重新读到 ${result.restored} 条失败原文`
          : "读取成功：本机没有更多失败原文"
        : `读取失败：${result.error ?? "原因未知"}`,
      !result.ok,
    );
  } finally {
    finish();
  }
}

/** 供测试与诊断：当前面板里各组的真实条目数 */
const counts = computed(() => ({
  pending: pending.value.length,
  failed: failed.value.length,
  interrupted: interrupted.value.length,
  displaced: displaced.value.length,
}));

</script>

<template>
  <div class="send-issues" data-panel-section="send-issues">
    <p v-if="notice" class="notice" :class="{ err: noticeFailed }" role="status">
      {{ notice }}
    </p>
    <!-- 旧消息恢复动作的结果：即使列表被清空、入口随后收起，也要留在屏幕上 -->
    <p v-else-if="interruptedNotice" class="notice" role="status">{{ interruptedNotice }}</p>

    <!-- 受理未确认：不能当失败、更不能自动重发 -->
    <section
      v-if="pending.length"
      class="group"
      data-section="pending"
      data-im="composer-pending"
      role="group"
      :aria-label="`还没有确认是否发出的文字（${pending.length} 条）`"
    >
      <h4 class="head">
        受理未确认的发送<span class="count mono">{{ pending.length }}</span>
      </h4>
      <p class="lead">
        这些消息还没有拿到可靠回执：可能已经到后端，也可能没有。请不要直接重写再发一次。
      </p>
      <ul class="list">
        <li
          v-for="record in pending"
          :key="record.id"
          class="item"
          data-item="pending"
          data-im="composer-pending-item"
        >
          <p class="text" data-im="composer-pending-quote" v-qio-tip="record.snapshot.text">
            {{ snippetOf(record.snapshot.text, 64) }}
          </p>
          <p class="meta">
            <span
              class="state mono"
              :class="{ unknown: confirmFor(record.id)?.unknown }"
              data-im="composer-pending-state"
            >
              {{ pendingNotice(record) }}
            </span>
            <span v-if="confirmFor(record.id)?.busy" class="why">正在确认…</span>
            <span v-else-if="record.lastError && confirmFor(record.id)" class="why">{{ record.lastError }}</span>
          </p>
          <div class="actions">
            <button
              v-if="!pendingBusy(record.id) && !confirmFor(record.id)?.accepted"
              class="qio-btn mini quiet"
              type="button"
              data-im="composer-pending-recheck"
              @click="recheckPending(record.id)"
            >
              再查一次
            </button>
            <button
              v-if="canRetry(record) && !pendingBusy(record.id)"
              class="qio-btn mini primary"
              type="button"
              data-im="composer-pending-retry"
              @click="retryPending(record.id)"
            >
              用原身份重试
            </button>
            <button
              v-if="canAbandon(record) && !pendingBusy(record.id)"
              class="qio-btn mini quiet"
              type="button"
              data-im="composer-pending-abandon"
              @click="abandonPending(record.id)"
            >
              放弃（原文放回输入框）
            </button>
            <button
              v-if="canCancel(record) && !pendingBusy(record.id)"
              class="qio-btn mini quiet"
              type="button"
              data-im="composer-pending-cancel"
              @click="cancelAccepted(record.id)"
            >
              取消这条已受理的轮次
            </button>
          </div>
        </li>
      </ul>
    </section>

    <!-- 上次退出时没有执行的消息：不自动重放 -->
    <section v-if="interrupted.length" class="group" data-section="interrupted">
      <h4 class="head">
        上次没有执行的消息<span class="count mono">{{ interrupted.length }}</span>
      </h4>
      <p class="lead">这些消息已经被 QIO 收下，但没有执行。继续发送或忽略都由你决定，不会自动重放。</p>
      <ul class="list">
        <li v-for="item in interrupted" :key="item.turn_id" class="item" data-item="interrupted">
          <p class="text">{{ snippetOf(item.message, 64) }}</p>
          <p class="meta">
            <span class="when mono">{{ whenText(item.ended_at || item.updated_at || item.created_at) }}</span>
            <span class="why">{{ item.reason_text?.trim() || "这条消息没有被执行" }}</span>
          </p>
          <div class="actions">
            <button class="qio-btn mini primary" type="button" :disabled="Boolean(busy)" @click="resumeInterrupted(item.turn_id)">
              {{ busy === `resume:${item.turn_id}` ? "提交中…" : "继续发送这条" }}
            </button>
            <button class="qio-btn mini quiet" type="button" :disabled="Boolean(busy)" @click="dismissInterrupted(item.turn_id)">
              忽略
            </button>
          </div>
        </li>
      </ul>
      <button
        v-if="interrupted.length > 1"
        class="qio-btn mini quiet bulk"
        type="button"
        :disabled="Boolean(busy)"
        @click="dismissAllInterrupted"
      >
        {{ busy === "dismiss:all" ? "处理中…" : `全部忽略（${interrupted.length} 条）` }}
      </button>
    </section>

    <!-- 明确失败的可恢复原文：找回不覆盖当前内容 -->
    <section
      v-if="failed.length"
      class="group"
      data-section="failed"
      data-im="composer-recovery"
      role="group"
      :aria-label="failed.length > 1 ? `上一次没有发出去的文字（${failed.length} 条）` : '上一次没有发出去的文字'"
    >
      <h4 class="head">
        发送失败的原文<span class="count mono">{{ failed.length }}</span>
      </h4>
      <p class="lead">失败原文保存在本机。找回只在输入框为空时放回；有更新文字时用「互换」，两份都不丢。</p>
      <ul class="list">
        <li
          v-for="(record, index) in failed"
          :key="`${record.id ?? record.draftId ?? 'row'}-${index}`"
          class="item"
          data-item="failed"
          data-im="composer-recovery-item"
        >
          <p class="text" data-im="composer-recovery-quote" v-qio-tip="record.text">
            {{ snippetOf(record.text, 64) }}
          </p>
          <p class="meta">
            <span class="why" data-im="composer-recovery-reason">{{ reasonOfFailed(record.id, index) }}</span>
          </p>
          <div class="actions">
            <button
              class="qio-btn mini primary"
              type="button"
              data-im="composer-recovery-restore"
              :disabled="Boolean(busy)"
              @click="retryFailed(record.id, index)"
            >
              找回原文
            </button>
            <button
              class="qio-btn mini quiet"
              type="button"
              data-im="composer-recovery-swap"
              :disabled="Boolean(busy)"
              @click="swapFailed(record.id, index)"
            >
              与当前文字互换
            </button>
            <button
              class="qio-btn mini quiet"
              type="button"
              data-im="composer-recovery-discard"
              :disabled="Boolean(busy)"
              @click="discardFailed(record.id, index)"
            >
              不再保留
            </button>
          </div>
        </li>
      </ul>
    </section>

    <!-- 被让位的旧草稿：取回 = 与当前文字互换 -->
    <section
      v-if="displaced.length"
      class="group"
      data-section="displaced"
      data-im="composer-displaced"
      role="group"
      aria-label="被让位的旧草稿"
    >
      <h4 class="head">
        被让位的旧草稿<span class="count mono">{{ displaced.length }}</span>
      </h4>
      <p class="lead">这个话题原本还有一份草稿。取回会与当前文字互换，两份都保留，不会自动发送。</p>
      <ul class="list">
        <li
          v-for="record in displaced"
          :key="record.id"
          class="item"
          data-item="displaced"
          data-im="composer-displaced-item"
        >
          <p class="text" data-im="composer-displaced-quote" v-qio-tip="record.text">
            {{ snippetOf(record.text, 64) }}
          </p>
          <div class="actions">
            <button
              class="qio-btn mini primary"
              type="button"
              data-im="composer-displaced-restore"
              :disabled="Boolean(busy)"
              @click="restoreDisplaced(record.id)"
            >
              取回（与当前文字互换）
            </button>
            <button
              class="qio-btn mini quiet"
              type="button"
              data-im="composer-displaced-discard"
              :disabled="Boolean(busy)"
              @click="discardDisplaced(record.id)"
            >
              放弃这份
            </button>
          </div>
        </li>
      </ul>
    </section>

    <!-- 保存失败：草稿 / 失败原文本机存储 -->
    <section v-if="draftSaveFailed" class="group" data-section="draft-save" data-im="composer-draft-status">
      <h4 class="head">草稿没有保存进本机</h4>
      <p class="lead">
        {{ session.draftSaveError ?? "原因未知" }}（文字还在输入框里，重试只重写本机草稿，不会发送）
      </p>
      <button
        class="qio-btn mini quiet"
        type="button"
        data-im="composer-draft-retry"
        @click="retryDraftSave"
      >
        重试保存
      </button>
    </section>

    <section
      v-if="storageWriteError || storageReadError"
      class="group"
      data-section="storage"
      data-im="composer-recovery-storage"
      role="group"
      aria-label="失败原文的本机存储"
    >
      <h4 class="head">失败原文的本机存储</h4>
      <p v-if="storageWriteError" class="lead" data-im="composer-recovery-persist">
        写入失败：{{ storageWriteError }}（内存里的记录仍可找回；关闭后可能丢失）
      </p>
      <p v-if="storageReadError" class="lead" data-im="composer-recovery-read-error">
        读取失败：{{ storageReadError }}（这不代表没有失败原文）
      </p>
      <button
        v-if="storageReadError"
        class="qio-btn mini quiet"
        type="button"
        data-im="composer-recovery-read-retry"
        :disabled="Boolean(busy)"
        @click="retryStorageRead"
      >
        {{ busy === "storage:read" ? "读取中…" : "重试读取" }}
      </button>
    </section>

    <p v-if="!counts.pending && !counts.failed && !counts.interrupted && !counts.displaced && !draftSaveFailed && !storageWriteError && !storageReadError && !interruptedNotice" class="empty">
      现在没有需要处理的发送异常。
    </p>
  </div>
</template>

<style scoped>
.send-issues {
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
  min-width: 0;
}
.notice {
  margin: 0;
  padding: 6px 10px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--qio-ui-radius);
  background: var(--bg-inset);
  color: var(--text-secondary);
  font-size: var(--fs-xs);
  overflow-wrap: anywhere;
}
.notice.err {
  border-color: var(--border-danger);
  color: var(--danger);
}
.group {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
}
.head {
  margin: 0;
  display: flex;
  align-items: center;
  gap: 8px;
  font-family: var(--sans);
  font-size: var(--fs-sm);
  font-weight: 600;
  color: var(--text-strong);
}
.head .count {
  min-width: 18px;
  padding: 0 6px;
  border-radius: var(--qio-ui-radius);
  background: var(--accent-soft);
  color: var(--accent);
  font-size: 10.5px;
  line-height: 16px;
  text-align: center;
}
.lead {
  margin: 0;
  color: var(--text-muted);
  font-size: var(--fs-xs);
  line-height: 1.65;
  overflow-wrap: anywhere;
}
.list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-width: 0;
}
.item {
  min-width: 0;
  padding: 8px 10px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--qio-ui-radius);
  background: var(--bg-inset);
}
.text {
  margin: 0;
  color: var(--text-primary);
  font-size: var(--fs-sm);
  line-height: 1.6;
  overflow-wrap: anywhere;
}
.meta {
  margin: 4px 0 0;
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: baseline;
  font-size: var(--fs-xs);
  color: var(--text-muted);
}
.state {
  color: var(--warning);
}
.state.unknown {
  color: var(--danger);
  font-weight: 600;
}
.why {
  min-width: 0;
  overflow-wrap: anywhere;
}
.actions {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 8px;
}
.actions .qio-btn {
  height: auto;
  padding: 3px 10px;
  font-size: 11.5px;
  border-radius: var(--qio-ui-radius);
}
.bulk {
  align-self: flex-end;
}
.empty {
  margin: 0;
  color: var(--text-muted);
  font-size: var(--fs-xs);
}
</style>
