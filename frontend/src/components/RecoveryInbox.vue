<script setup lang="ts">
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
 * 5. **截断要可见**：服务端说还有没显示完的，就明说「还有 N 条未显示」。
 */
import { computed, ref } from "vue";
import { useSessionStore } from "../stores/session";
import {
  recoveryActionLabel,
  recoveryReasonOf,
  type RecoveryOutcome,
} from "../stores/session";
import type { RecoveryActionView, RecoveryRecordView } from "../services/recoveryApi";

const session = useSessionStore();
/** 哪些条目展开了详情（默认全部收起） */
const openIds = ref<string[]>([]);
/** 每条自己最近一次的失败原因（就地显示，不弹全局提示） */
const localErrors = ref<Record<string, string>>({});
/** 每条自己最近一次的成功说明（列表消失前也能看到结果） */
const localNotices = ref<Record<string, string>>({});

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

const summary = computed(() =>
  count.value === 1 ? "有 1 条未完成事项" : `有 ${count.value} 条未完成事项`,
);

function isOpen(recordId: string): boolean {
  return openIds.value.includes(recordId);
}

function toggle(recordId: string) {
  openIds.value = isOpen(recordId)
    ? openIds.value.filter((id) => id !== recordId)
    : [...openIds.value, recordId];
}

/** Esc 只收起这一块里展开的详情，不动任何记录、不碰别的组件。 */
function onKeydown(event: KeyboardEvent) {
  if (event.key !== "Escape") return;
  if (!openIds.value.length) return;
  event.stopPropagation();
  openIds.value = [];
}

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
  return record.kind === "derived_task" ? "后台任务" : "你的消息";
}

function actionsOf(record: RecoveryRecordView): RecoveryActionView[] {
  return Array.isArray(record.actions) ? record.actions : [];
}

function actionLabel(action: RecoveryActionView): string {
  return recoveryActionLabel(action.id, action.label);
}

/** 一次动作的结果；成功说明留在那一条旁边。 */
async function run(record: RecoveryRecordView, action: RecoveryActionView) {
  if (!action.enabled || busyId.value) return;
  const id = record.record_id;
  localErrors.value = { ...localErrors.value, [id]: "" };
  localNotices.value = { ...localNotices.value, [id]: "" };
  let outcome: RecoveryOutcome;
  if (action.id === "continue") outcome = await session.continueRecovery(id);
  else if (action.id === "repair") outcome = await session.repairOrphan(id);
  else if (action.id === "ignore") outcome = await session.ignoreRecovery(id);
  else if (action.id === "requeue") outcome = await session.requeueDerived(id);
  else {
    // 认不出的动作：如实说，不给一个点了没效果的按钮
    localErrors.value = {
      ...localErrors.value,
      [id]: "这一版界面还不认识这个操作，请更新后再试",
    };
    return;
  }
  if (outcome.ok) {
    localNotices.value = { ...localNotices.value, [id]: outcome.message };
    return;
  }
  localErrors.value = { ...localErrors.value, [id]: outcome.message };
}

function retry(record: RecoveryRecordView, action: RecoveryActionView) {
  void run(record, action);
}

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
    v-if="count || listError || session.recoveryLoading"
    class="recovery-inbox"
    aria-label="未完成事项"
    @keydown="onKeydown"
  >
    <header class="head">
      <span class="mark" aria-hidden="true">↻</span>
      <span class="title">{{ count ? summary : "未完成事项" }}</span>
      <span v-if="session.recoveryLoading" class="loading" role="status">读取中…</span>
    </header>

    <!-- 拉清单失败：保留已显示的记录，如实说可能不完整，并给一次重试 -->
    <p v-if="listError" class="list-error" role="status">
      <span class="text">{{ listError }}</span>
      <button class="qio-btn quiet btn-retry-list" type="button" @click="session.loadRecoveryInbox()">
        重试
      </button>
    </p>

    <ul class="list">
      <li v-for="record in items" :key="record.record_id" class="item">
        <p class="message" :title="messageOf(record)">{{ messageOf(record) }}</p>
        <p class="meta">
          <span class="kind">{{ kindLabel(record) }}</span>
          <span class="status mono">{{ statusOf(record) }}</span>
          <span class="why">{{ recoveryReasonOf(record) }}</span>
        </p>

        <div class="actions">
          <button
            v-for="action in actionsOf(record)"
            :key="`${record.record_id}:${action.id}`"
            class="qio-btn btn-action"
            :class="[`btn-${action.id}`, action.enabled ? 'primary' : 'quiet']"
            type="button"
            :disabled="!action.enabled || Boolean(busyId)"
            :aria-label="`${actionLabel(action)}：${messageOf(record)}`"
            :title="action.enabled ? '' : action.reason"
            @click="run(record, action)"
          >
            {{ busyId === record.record_id ? "提交中…" : actionLabel(action) }}
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

        <!-- 为什么动不了：服务端给的一句话，按钮禁用时也要能读到 -->
        <p
          v-for="action in actionsOf(record).filter((a) => !a.enabled && a.reason)"
          :key="`${record.record_id}:why:${action.id}`"
          class="blocked"
        >
          {{ actionLabel(action) }}：{{ action.reason }}
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

        <!-- 失败就地说话 + 可重试（重试就是同一个动作再点一次） -->
        <p v-if="localErrors[record.record_id]" class="inline-error" role="status">
          <span class="text">{{ localErrors[record.record_id] }}</span>
          <button
            v-if="actionsOf(record).some((a) => a.enabled)"
            class="qio-btn quiet btn-retry"
            type="button"
            :disabled="Boolean(busyId)"
            @click="retry(record, actionsOf(record).find((a) => a.enabled)!)"
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
  border: 1px solid var(--border-subtle); border-radius: var(--r-lg);
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
  /* 截断可见：两行之外给省略号；完整原文在 title 与 DOM 文本里都还在 */
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
  overflow: hidden; overflow-wrap: anywhere;
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
.btn-details {
  background: none; border: none; padding: 0; cursor: pointer;
  color: var(--link); font-size: var(--fs-xs); font-family: var(--sans);
}
.btn-details:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
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
.truncated { margin: 0; font-size: var(--fs-xs); color: var(--text-muted); }
</style>
