<script setup lang="ts">
import { vQioTip } from "../composables/useQioTip";
/**
 * 「未完成事项」收件箱（A01 + A03 的前端出口）。
 *
 * 为什么需要它：以前「上一次没执行完的消息」「被抢占过却没有后继的孤儿重发」
 * 「升级前无归属的历史行」「归属已死的派生任务」各自藏在不同接口里，谁也没被
 * 完整渲染过 —— 用户看到的就是「我说过的话/排过的任务不见了」，而且点都点不到。
 * 这里把它们放在**同一份安静清单**里：数量、原文、原状态、「为什么现在动不了」、
 * 以及服务端说明可用的动作。
 *
 * 五条刻意的设计：
 *
 * 1. **安静**：一行常驻入口 + 常驻列表，不是弹窗、不 autofocus、不抢焦点；
 * 2. **不自动动**：任何动作都只由用户点；「修好」不等于重发；
 * 3. **详情默认收起**：主行已经能判断要不要处理，内部标识与时间按需展开；
 * 4. **失败就地说话**：原因写在**那一条**下面，并留着可重试的按钮 ——
 *    绝不静默、也绝不把整块清掉；
 * 5. **截断要可见**：服务端说还有没显示完的，就明说「还有 N 条未显示」；
 * 6. **原文默认简述 + 就地展开**（滚动规则）：长原文默认两行，点「展开原文」看全文 ——
 *    不在同一阅读流里再切出一个小滚动窗口，也不把全文藏进只有悬停才看得到的 title。
 */
import { computed, nextTick, onBeforeUnmount, ref, watch } from "vue";
import QConfirm from "./ui/QConfirm.vue";
import { useSessionStore } from "../stores/session";
import {
  recoveryActionLabel,
  recoveryReasonOf,
  type RecoveryOutcome,
} from "../stores/session";
import type { RecoveryActionView, RecoveryRecordView } from "../services/recoveryApi";

const props = withDefaults(
  defineProps<{
    /**
     * #78：要滚动并聚焦的那一条（由对话页在「去处理」时传入）。
     * 变化时滚动到同一条并聚焦它的第一个可用动作；目标已不存在时给真实反馈。
     */
    focusRecordId?: string | null;
  }>(),
  { focusRecordId: null },
);

const session = useSessionStore();
/** 哪些条目展开了详情（默认全部收起） */
const openIds = ref<string[]>([]);
/** 哪些条目展开了原文（默认只显示两行简述；展开后完整换行，不新开滚动层） */
const openTextIds = ref<string[]>([]);
/** 每条自己最近一次的失败原因（就地显示，不弹全局提示） */
const localErrors = ref<Record<string, string>>({});
/** 每条自己最近一次的成功说明（列表消失前也能看到结果） */
const localNotices = ref<Record<string, string>>({});
/**
 * 每条最近一次**失败的动作 id**（记录 id → 动作 id）。
 *
 * #77：重试必须重试原来那个动作 —— 用「第一个可用动作」顶替，会让用户在
 * 「忽略」失败后点重试却执行了「继续发送」，两件事的后果完全不同。
 */
const failedActions = ref<Record<string, string>>({});
/**
 * 正在等用户确认「旧执行者已停止」的那一条（F01）。
 *
 * 为什么单独一步确认：无归属 = 库里没有证据说明旧执行者停了。点「继续 / 知道了」
 * 之前必须先由用户**显式**承担「旧进程已经退出」这个判断，否则同一条消息可能被
 * 执行两次。这一步不执行任何东西，只把用户的判断交给服务端持久化。
 */
const confirmingId = ref("");

const items = computed<RecoveryRecordView[]>(() => session.recoveryRecords);
const count = computed(() => items.value.length);
const busyId = computed(() => session.recoveryBusyId);
const listError = computed(() => session.recoveryError);
/**
 * 还有多少条没显示。
 *
 * `total` 是服务端说的匹配总数（不受 limit 影响）：它比已显示的条数多，
 * 就必须让用户知道「这不是全部」，而不是让剩下的记录看起来不存在。
 */
const hiddenCount = computed(() =>
  Math.max(0, Number(session.recoveryTotal || 0) - count.value),
);

/**
 * 文案 5：整个窗口只用一个标题「未完成事项 · N」。
 * 页面侧不再重复写标题/数量；读取失败或读取中时只保留标题本身。
 */
const titleText = computed(() =>
  count.value > 0 ? `未完成事项 · ${count.value}` : "未完成事项",
);

const rootEl = ref<HTMLElement | null>(null);
/** #78：要定位的那条已经不存在时的真实反馈（不静默、不假装已经定位）。 */
const focusError = ref("");
/** 刚被定位的那一条（短暂高亮），只是让用户看见「就是这条」。 */
const focusedId = ref("");
let focusTimer: ReturnType<typeof setTimeout> | null = null;

function recordElement(recordId: string): HTMLElement | null {
  const root = rootEl.value;
  if (!root) return null;
  const rows = root.querySelectorAll("[data-record-id]");
  for (const row of Array.from(rows)) {
    if ((row as HTMLElement).dataset.recordId === recordId) return row as HTMLElement;
  }
  return null;
}

/** #78：滚动到同一条并聚焦它的第一个可操作按钮（没有就聚焦详情入口）。 */
async function locateFocus(recordId: string): Promise<void> {
  const id = String(recordId ?? "").trim();
  if (!id) return;
  focusError.value = "";
  focusedId.value = "";
  if (focusTimer) clearTimeout(focusTimer);
  await nextTick();
  if (!currentRecord(id)) {
    // 目标已消失：这是必须让用户知道的事实，不能静默打开一个「什么都没发生」的窗口
    focusError.value =
      "要定位的这条未完成事项已经不存在：可能已经被处理，或后端已经清理（清单会随刷新更新）。如需核对，可以回到任务列表刷新后再看。";
    return;
  }
  const row = recordElement(id);
  if (!row) return;
  row.scrollIntoView?.({ block: "nearest", behavior: "smooth" });
  const target =
    row.querySelector<HTMLElement>("button.btn-action:not([disabled])") ??
    row.querySelector<HTMLElement>("button.btn-details") ??
    row;
  target?.focus?.();
  focusedId.value = id;
  focusTimer = setTimeout(() => {
    focusTimer = null;
    focusedId.value = "";
  }, 1800);
}

watch(
  () => props.focusRecordId,
  (value) => {
    if (value) void locateFocus(value);
  },
  { immediate: true },
);

function isOpen(recordId: string): boolean {
  return openIds.value.includes(recordId);
}

function toggle(recordId: string) {
  openIds.value = isOpen(recordId)
    ? openIds.value.filter((id) => id !== recordId)
    : [...openIds.value, recordId];
}

function isTextOpen(recordId: string): boolean {
  return openTextIds.value.includes(recordId);
}

function toggleText(recordId: string) {
  openTextIds.value = isTextOpen(recordId)
    ? openTextIds.value.filter((id) => id !== recordId)
    : [...openTextIds.value, recordId];
}

/**
 * 是否需要「展开原文」：两行装不下的长原文（长文本或含换行）才给入口。
 * 这里只决定**要不要给入口**，不改写文本；判定不依赖渲染后的实际宽度，
 * 所以在窄窗口与宽窗口下都不会出现「被裁掉却没有入口」。
 */
function isLongText(text: string): boolean {
  return text.length > 80 || text.includes("\n");
}

/**
 * Esc 分层（#79）：先收这一块里展开的详情/原文/确认，再让调用方收弹窗。
 *
 * 返回 true = 本次 Esc 已被这里消费（调用方不要再关窗口）；
 * 返回 false = 这里没有可收的内层，调用方按自己的层级继续处理。
 */
function handleEscape(): boolean {
  if (confirmingId.value) {
    confirmingId.value = "";
    return true;
  }
  if (openIds.value.length || openTextIds.value.length) {
    openIds.value = [];
    openTextIds.value = [];
    return true;
  }
  return false;
}

/** Esc 只收起这一块里展开的详情，不动任何记录、不碰别的组件。 */
function onKeydown(event: KeyboardEvent) {
  if (event.key !== "Escape") return;
  if (handleEscape()) event.stopPropagation();
}

/** #79：把 Esc 分层能力交给对话页（先调它，false 再关弹窗）。 */
defineExpose({ handleEscape });

/** 消息原文；派生任务没有用户原文，就给一句不编造的说明。 */
function messageOf(record: RecoveryRecordView): string {
  const text = (record.message || "").trim();
  if (text) return text;
  if (record.kind === "derived_task") return "一个没有跑完的后台任务";
  return "（这条记录里没有留下消息原文）";
}

/** 原状态：说清它在持久状态里的位置，不美化也不隐藏。 */
function statusOf(record: RecoveryRecordView): string {
  return (record.status || "状态未知").trim() || "状态未知";
}

function kindLabel(record: RecoveryRecordView): string {
  if (record.kind === "derived_task") return "后台任务";
  // F04：话题回答恢复是「话题操作成功但回答没生成」的可重试事项，不是一条待重发的消息
  if (record.kind === "topic_answer_recovery") return "话题回答恢复";
  return "你的消息";
}

function actionsOf(record: RecoveryRecordView): RecoveryActionView[] {
  return Array.isArray(record.actions) ? record.actions : [];
}

function actionLabel(action: RecoveryActionView): string {
  return recoveryActionLabel(action.id, action.label);
}

/**
 * 文案 6：把「具体后果」放在对应动作旁（页面的通用导语删掉后，用户仍要能预判）。
 * 只描述这个动作真的会做什么/不会做什么，不重复禁用原因。
 */
function recoveryActionEffect(actionId: string): string {
  if (actionId === "continue") return "按原话题重新排队，不会新建一条";
  if (actionId === "repair") return "只修好重发关系，不会自动重发";
  if (actionId === "ignore") return "不再提示；原文与记录仍然保留";
  if (actionId === "requeue") return "放回待执行队列，不改已尝试次数";
  if (actionId === "confirm_stopped") return "只记录你的判断，不执行任何东西";
  if (actionId === "retry_answer" || actionId === "retry_topic_answer") {
    return "只重试这一次回答，不重发消息";
  }
  return "";
}

function actionEffect(action: RecoveryActionView): string {
  return action.enabled ? recoveryActionEffect(action.id) : "";
}

/**
 * 文案 7：多个动作共用的禁用原因只显示一次；不同原因分别保留。
 * 返回按「原因」分组后的禁用动作（同一句原因里列出所有受它影响的动作）。
 */
function blockedGroups(record: RecoveryRecordView): { reason: string; labels: string[] }[] {
  const groups: { reason: string; labels: string[] }[] = [];
  for (const action of actionsOf(record)) {
    if (action.enabled) continue;
    const reason = (action.reason || "").trim();
    if (!reason) continue;
    const existing = groups.find((group) => group.reason === reason);
    if (existing) existing.labels.push(actionLabel(action));
    else groups.push({ reason, labels: [actionLabel(action)] });
  }
  return groups;
}

/** 就地写一条失败原因（清掉同一行的成功说明）。 */
function sayError(id: string, message: string): void {
  localNotices.value = { ...localNotices.value, [id]: "" };
  localErrors.value = { ...localErrors.value, [id]: message };
}

/** 清单里当前的那条记录（没有 = 已经被处理 / 被后端清理）。 */
function currentRecord(recordId: string): RecoveryRecordView | null {
  return items.value.find((row) => row.record_id === recordId) ?? null;
}

/**
 * #77：动作开始前核对「这条记录还在 + 这个动作还在 + 当前真的可用」。
 * 任何一项不成立都给出真实原因（不盲目再打一次注定失败的请求，也不静默）。
 */
function readyAction(recordId: string, actionId: string): RecoveryActionView | null {
  const current = currentRecord(recordId);
  if (!current) {
    sayError(
      recordId,
      "这条未完成事项已经不在清单里：可能已经被处理，或后端已经清理（刷新清单可以确认）。",
    );
    return null;
  }
  const action = actionsOf(current).find((item) => item.id === actionId) ?? null;
  if (!action) {
    sayError(recordId, "这个操作在当前状态下已经不存在（记录可能已经变化，刷新清单后再试）。");
    return null;
  }
  if (!action.enabled) {
    const reason = (action.reason || "").trim() || "记录状态已经变化";
    sayError(recordId, actionLabel(action) + "现在不可用：" + reason);
    return null;
  }
  return action;
}

/** 一次动作的结果；成功说明留在那一条旁边。 */
async function run(record: RecoveryRecordView, action: RecoveryActionView) {
  if (busyId.value) return;
  const id = record.record_id;
  const ready = readyAction(id, action.id);
  if (!ready) return;
  if (ready.id === "confirm_stopped") {
    // 先弹一次就地确认（这一步不执行任何东西），确认后才真的提交
    confirmingId.value = id;
    return;
  }
  localErrors.value = { ...localErrors.value, [id]: "" };
  localNotices.value = { ...localNotices.value, [id]: "" };
  // 这一次动作重新开始：上一条失败动作从「待重试」里撤下（失败时再按当前动作记回）
  if (failedActions.value[id]) {
    const next = { ...failedActions.value };
    delete next[id];
    failedActions.value = next;
  }
  let outcome: RecoveryOutcome;
  if (ready.id === "continue") outcome = await session.continueRecovery(id);
  else if (ready.id === "repair") outcome = await session.repairOrphan(id);
  else if (ready.id === "ignore") outcome = await session.ignoreRecovery(id);
  else if (ready.id === "requeue") outcome = await session.requeueDerived(id);
  else if (ready.id === "retry_answer" || ready.id === "retry_topic_answer") {
    // F04：话题回答恢复只走 store 的唯一 retry 入口（不重发消息、不重跑话题工具）
    const result = await session.retryTopicAnswer(id);
    outcome = { ok: result.ok, message: result.message };
  } else {
    // 认不出的动作：如实说，不给一个点了没效果的按钮
    sayError(id, "这一版界面还不认识这个操作，请更新后再试");
    return;
  }
  if (outcome.ok) {
    localNotices.value = { ...localNotices.value, [id]: outcome.message };
    return;
  }
  // #77：记住失败发生的**这个动作**；重试只重试它
  failedActions.value = { ...failedActions.value, [id]: ready.id };
  localErrors.value = { ...localErrors.value, [id]: outcome.message };
}

/**
 * 重试上一次失败的那一个动作（原记录 + 原动作）。
 * 动作已经不可用（记录消失 / 动作被删除 / 被禁用）时只给真实原因，不再调用接口。
 */
function retry(record: RecoveryRecordView) {
  const id = record.record_id;
  const actionId = failedActions.value[id];
  if (!actionId) {
    sayError(id, "没有记录到上一次失败的操作，请重新选择要处理的动作。");
    return;
  }
  const ready = readyAction(id, actionId);
  if (!ready) return;
  if (ready.id === "confirm_stopped") {
    confirmingId.value = id;
    return;
  }
  const current = currentRecord(id);
  if (!current) return;
  void run(current, ready);
}

/** 用户在确认框里点了「确认已停止」：提交给服务端，结果就地说明。 */
async function confirmStopped(record: RecoveryRecordView) {
  const id = record.record_id;
  confirmingId.value = "";
  localErrors.value = { ...localErrors.value, [id]: "" };
  localNotices.value = { ...localNotices.value, [id]: "" };
  const outcome = await session.confirmStopped(id);
  if (outcome.ok) {
    localNotices.value = { ...localNotices.value, [id]: outcome.message };
    return;
  }
  // #77：确认失败也记住「原失败动作是确认」，重试回到同一个确认步骤
  failedActions.value = { ...failedActions.value, [id]: "confirm_stopped" };
  localErrors.value = { ...localErrors.value, [id]: outcome.message };
}

function cancelConfirm() {
  confirmingId.value = "";
}

onBeforeUnmount(() => {
  if (focusTimer) clearTimeout(focusTimer);
  focusTimer = null;
});

/** 时间：台账里是 UTC ISO，用户判断要不要处理靠本地时间。 */
function formatWhen(record: RecoveryRecordView): string {
  const raw = record.updated_at || record.created_at;
  if (!raw) return "";
  const d = new Date(raw);
  if (Number.isNaN(d.getTime())) return "";
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}
</script>

<template>
  <!--
    根条件包含 listError / recoveryLoading：拉清单失败时清单可能暂时是空的，
    但「为什么没有 / 为什么可能是旧的」必须留在屏幕上，不能整块消失。
  -->
  <section
    v-if="count || listError || session.recoveryLoading || focusError"
    ref="rootEl"
    class="recovery-inbox"
    data-qio-fit
    aria-label="未完成事项"
    @keydown="onKeydown"
  >
    <header class="head">
      <span class="mark" aria-hidden="true">↻</span>
      <!-- 文案 5：整块只有一个标题「未完成事项 · N」（页面不再重复写一遍） -->
      <span class="title">{{ titleText }}</span>
      <span v-if="session.recoveryLoading" class="loading" role="status">读取中…</span>
    </header>

    <!-- #78：要定位的那条已经不存在 —— 真实反馈，不静默 -->
    <p v-if="focusError" class="focus-error" role="alert">{{ focusError }}</p>

    <!-- 拉清单失败：保留已显示的记录，如实说可能不完整，并给一次重试 -->
    <p v-if="listError" class="list-error" role="status">
      <span class="text">{{ listError }}</span>
      <button class="qio-btn quiet btn-retry-list" type="button" @click="session.loadRecoveryInbox()">
        重试
      </button>
    </p>

    <ul class="list">
      <li
        v-for="record in items"
        :key="record.record_id"
        class="item"
        :class="{ focused: focusedId === record.record_id }"
        :data-record-id="record.record_id"
      >
        <p
          class="message"
          :class="{ clamped: !isTextOpen(record.record_id) && isLongText(messageOf(record)) }"
          v-qio-tip="messageOf(record)"
        >
          {{ messageOf(record) }}
        </p>
        <p class="meta">
          <span class="kind">{{ kindLabel(record) }}</span>
          <span class="status mono">{{ statusOf(record) }}</span>
          <span class="why">{{ recoveryReasonOf(record) }}</span>
        </p>

        <div class="actions">
          <template v-for="action in actionsOf(record)" :key="`${record.record_id}:${action.id}`">
            <button
              class="qio-btn btn-action"
              :class="[`btn-${action.id}`, action.enabled ? 'primary' : 'quiet']"
              type="button"
              :disabled="!action.enabled || Boolean(busyId)"
              :aria-label="`${actionLabel(action)}：${messageOf(record)}`"
              @click="run(record, action)"
            >
              {{ busyId === record.record_id ? "提交中…" : actionLabel(action) }}
            </button>
            <!-- 文案 6：具体后果就写在对应动作旁 -->
            <span v-if="actionEffect(action)" class="action-effect">{{ actionEffect(action) }}</span>
          </template>
          <button
            v-if="isLongText(messageOf(record))"
            class="link btn-text"
            type="button"
            :aria-expanded="isTextOpen(record.record_id)"
            @click="toggleText(record.record_id)"
          >
            {{ isTextOpen(record.record_id) ? "收起原文" : "展开原文" }}
          </button>
          <button
            class="link btn-details"
            type="button"
            :aria-expanded="isOpen(record.record_id)"
            @click="toggle(record.record_id)"
          >
            {{ isOpen(record.record_id) ? "收起详情" : "详情" }}
          </button>
        </div>

        <!--
          为什么动不了：服务端给的一句话，按钮禁用时也要能读到。
          文案 7：多个动作共用同一个禁用原因时只显示一次（不同原因分别保留）。
        -->
        <p
          v-for="(group, index) in blockedGroups(record)"
          :key="`${record.record_id}:why:${index}`"
          class="blocked"
        >
          {{ group.labels.join("、") }}：{{ group.reason }}
        </p>

        <!-- 详情默认收起：内部标识与时间按需展开 -->
        <dl v-if="isOpen(record.record_id)" class="details">
          <div v-if="formatWhen(record)" class="row">
            <dt>最近变化</dt>
            <dd class="mono">{{ formatWhen(record) }}</dd>
          </div>
          <div v-if="record.owner_state" class="row">
            <dt>归属</dt>
            <dd>{{ record.owner_state }}</dd>
          </div>
          <div v-if="record.attempts !== null" class="row">
            <dt>已尝试</dt>
            <dd class="mono">{{ record.attempts }} 次</dd>
          </div>
          <div v-if="record.last_error" class="row">
            <dt>上次失败</dt>
            <dd>{{ record.last_error }}</dd>
          </div>
        </dl>

        <!-- F01：确认「旧执行者已停止」——唯一能把这条从「只可见」变成「可操作」的一步 -->
        <QConfirm
          v-if="confirmingId === record.record_id"
          :open="true"
          variant="inline"
          tone="danger"
          title="确认旧执行者已经停止？"
          detail="这条记录来自没有实例归属的旧版本。只有在确定那个旧进程已经退出时才确认；否则「继续」会让同一条消息被执行两次。"
          confirm-text="确认已停止"
          cancel-text="先不确认"
          @confirm="confirmStopped(record)"
          @cancel="cancelConfirm"
        />

        <!-- 失败就地说话 + 可重试（重试就是同一个动作再点一次） -->
        <p v-if="localErrors[record.record_id]" class="inline-error" role="status">
          <span class="text">{{ localErrors[record.record_id] }}</span>
          <!-- #77：重试只重试**上一次失败的那个动作**（忽略失败只能重试忽略） -->
          <button
            v-if="failedActions[record.record_id]"
            class="qio-btn quiet btn-retry"
            type="button"
            :disabled="Boolean(busyId)"
            @click="retry(record)"
          >
            重试
          </button>
        </p>
        <p v-else-if="localNotices[record.record_id]" class="inline-notice" role="status">
          {{ localNotices[record.record_id] }}
        </p>
      </li>
    </ul>

    <!-- 服务端说还有没显示完的：必须说出来，不能让剩下的记录看起来不存在 -->
    <p v-if="hiddenCount" class="truncated" role="status">
      还有 {{ hiddenCount }} 条未显示
    </p>
  </section>
</template>

<style scoped>
/*
 * 位置由对话页负责（它和「上次没执行的消息」入口排在同一个槽里）；
 * 这里只描述这一块自己为什么长这样：安静、可读、不抢焦点。
 */
.recovery-inbox {
  width: 100%; box-sizing: border-box; padding: var(--sp-3) var(--sp-4);
  border: 1px solid var(--border-subtle); border-radius: var(--qio-ui-radius);
  background: var(--bg-elevated); box-shadow: var(--shadow-1);
  font-family: var(--sans);
  display: flex; flex-direction: column; gap: var(--sp-2);
  /* 与「上次没执行的消息」入口同一列宽：窄窗口不溢出，长消息可换行 */
  max-width: min(560px, calc(100vw - 32px));
  margin: var(--sp-2) auto 0;
}
.head { display: flex; align-items: center; gap: 8px; }
.mark {
  display: inline-flex; align-items: center; justify-content: center;
  width: 16px; height: 16px; border-radius: 50%;
  background: var(--accent-soft); color: var(--accent);
  font-family: var(--mono); font-size: 11px; font-weight: 700;
}
.title { font-size: var(--fs-sm); color: var(--text-strong); }
.loading { font-size: var(--fs-xs); color: var(--text-muted); }

.list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--sp-3); }
.item { display: flex; flex-direction: column; gap: 6px; }
.message {
  margin: 0; font-size: var(--fs-sm); color: var(--text-primary); line-height: 1.6;
  overflow-wrap: anywhere;
  /* 默认完整换行显示：不新开滚动层，也不裁切 */
}
/* 长原文的**默认简述**：两行之外给省略号，并由同一条目里的「展开原文」看全文；
   这不是唯一的查看通道（DOM 文本与 title 始终是全文，屏幕阅读器读到的也是全文）。 */
.message.clamped {
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
  overflow: hidden;
}
.meta {
  margin: 0; display: flex; flex-wrap: wrap; gap: 8px; align-items: baseline;
  font-size: var(--fs-xs); color: var(--text-secondary);
}
.kind { color: var(--text-muted); }
.status { color: var(--text-muted); }
.why { color: var(--text-secondary); }
.actions { display: flex; flex-wrap: wrap; gap: var(--sp-2); align-items: center; }
.btn-action:disabled { opacity: .6; cursor: not-allowed; }
.btn-details,
.btn-text {
  background: none; border: none; padding: 0; cursor: pointer;
  color: var(--link); font-size: var(--fs-xs); font-family: var(--sans);
}
.btn-details:focus-visible,
.btn-text:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
.blocked { margin: 0; font-size: var(--fs-xs); color: var(--text-muted); line-height: 1.6; }

.details {
  margin: 0; padding: var(--sp-2) 0 0; border-top: 1px solid var(--border-subtle);
  display: flex; flex-direction: column; gap: 2px; font-size: var(--fs-xs);
}
.details .row { display: flex; gap: 8px; }
.details dt { color: var(--text-muted); min-width: 64px; }
.details dd { margin: 0; color: var(--text-secondary); overflow-wrap: anywhere; }

.inline-error {
  margin: 0; font-size: var(--fs-xs); color: var(--danger); line-height: 1.6;
  display: flex; flex-wrap: wrap; gap: 8px; align-items: baseline;
}
.inline-notice { margin: 0; font-size: var(--fs-xs); color: var(--text-secondary); line-height: 1.6; }
.list-error {
  margin: 0; font-size: var(--fs-xs); color: var(--text-secondary); line-height: 1.6;
  display: flex; flex-wrap: wrap; gap: 8px; align-items: baseline;
}
/* #78：定位目标已不存在时的真实反馈（就地可见，不静默） */
.focus-error {
  margin: 0; font-size: var(--fs-xs); color: var(--warning); line-height: 1.6;
  overflow-wrap: anywhere;
}
/* 文案 6：动作后果，紧跟对应按钮，小字不抢按钮层级 */
.action-effect {
  font-size: var(--fs-xs); color: var(--text-muted); line-height: 1.5;
  overflow-wrap: anywhere;
}
/* #78：被定位的那一条短暂高亮（只说明「就是这条」，不改变任何记录） */
.item.focused {
  outline: 2px solid var(--focus-ring);
  outline-offset: 2px;
  border-radius: var(--qio-ui-radius);
}
.item:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
.truncated { margin: 0; font-size: var(--fs-xs); color: var(--text-muted); }
</style>
