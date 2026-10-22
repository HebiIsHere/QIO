<script setup lang="ts">
/**
 * 中央对话工作区（B 负责完整实现）。
 *
 * 结构（自上而下）：
 *   话题栏（常驻标题；点击展开完整说明：描述 / 关键词 / 规模 / 历史续接状态）
 *   全局状态区（历史读取失败 / 连接与同步异常 / 错误 / 警告 / 凭据 / 兼容模式 / 布局保存失败）
 *   消息流（MessageStream：虚拟列表、流式、工具详情、轮次状态、回到最新）
 *   底部候选（高影响知识候选，需要用户决定时才出现）
 *   输入区（ComposerDock：五入口 + 模式切换 + 输入气泡；固定贴底）
 *
 * 两条迁移：
 * - 上次没执行的消息、失败原文、草稿恢复 → 输入区上方入口①（不再放在流里的横幅）；
 * - 排队消息 → 入口④；话题切换建议 → 入口③；用户消息恢复 → 入口①。
 * 全局异常只影响「整个对话」时才出现在顶部状态区，轮次自己的失败留在那一轮里。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useSessionStore } from "../../stores/session";
import { useEventStore } from "../../stores/events";
import { useOnboardingStore } from "../../stores/onboarding";
import { useConversationShellStore } from "../../stores/conversationShell";
import { restoreRuntimeState } from "../../stores/restore";
import { api, type TopicDetail } from "../../services/api";
import MessageStream from "../MessageStream.vue";
import KnowledgeCandidateCard from "../KnowledgeCandidateCard.vue";
import ComposerDock from "./ComposerDock.vue";
import { useComposerClearance } from "../../composables/useComposerClearance";
import { vQioScrollbar } from "../../composables/useQioScrollbars";

const session = useSessionStore();
const events = useEventStore();
const onboarding = useOnboardingStore();
const shell = useConversationShellStore();

/* ------------------------------------------------------------------ */
/* 话题栏                                                              */
/* ------------------------------------------------------------------ */

const topicTitle = computed(
  () => session.topicName || (session.currentTopicId ? "当前话题" : "默认话题"),
);
const anchorTitle = computed(() => session.anchorFragment?.title ?? "");
const hasAnchorPosition = computed(
  () => session.anchorHistoric && Boolean(session.anchorFragmentId),
);
const continuationLine = computed(() => {
  const pending = session.pendingContinuation;
  if (!pending) return "";
  return pending.sourceTitle
    ? `下一条将从「${pending.sourceTitle}」继续`
    : "下一条将从所选历史继续";
});

/** 话题完整说明：打开时才向后端读；读不到就如实说，绝不编造描述。 */
const detailsOpen = ref(false);
const detail = ref<TopicDetail | null>(null);
const detailLoading = ref(false);
const detailError = ref("");
let detailSeq = 0;

async function loadTopicDetail(): Promise<void> {
  const topicId = session.currentTopicId;
  if (!topicId) return;
  const seq = ++detailSeq;
  detailLoading.value = true;
  detailError.value = "";
  try {
    const res = await api.getTopicDetail(topicId);
    if (seq !== detailSeq || session.currentTopicId !== topicId) return;
    detail.value = res;
  } catch (e) {
    if (seq !== detailSeq) return;
    detail.value = null;
    detailError.value = `话题说明没有读出来：${(e as Error).message}`;
  } finally {
    if (seq === detailSeq) detailLoading.value = false;
  }
}

function toggleDetails(): void {
  detailsOpen.value = !detailsOpen.value;
  if (detailsOpen.value && !detail.value && !detailLoading.value) void loadTopicDetail();
}

function retryTopicDetail(): void {
  detail.value = null;
  void loadTopicDetail();
}

watch(
  () => session.currentTopicId,
  () => {
    // 切话题：旧话题的说明作废（迟到的响应由 seq 丢弃）
    detailSeq += 1;
    detail.value = null;
    detailError.value = "";
    detailLoading.value = false;
    if (detailsOpen.value) void loadTopicDetail();
  },
);

function formatMoment(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

const detailRows = computed(() => {
  const d = detail.value;
  if (!d) return [];
  const rows: { label: string; value: string }[] = [];
  if (typeof d.message_count === "number") rows.push({ label: "消息", value: `${d.message_count} 条` });
  if (Array.isArray(d.fragments) && d.fragments.length) {
    rows.push({ label: "片段", value: `${d.fragments.length} 个` });
  }
  if (d.last_activity) rows.push({ label: "最近活动", value: formatMoment(d.last_activity) });
  if (Array.isArray(d.keywords) && d.keywords.length) {
    rows.push({ label: "关键词", value: d.keywords.join(" · ") });
  }
  return rows;
});

const detailSummary = computed(() => detail.value?.summary?.trim() ?? "");

/* ------------------------------------------------------------------ */
/* 全局状态区                                                          */
/* ------------------------------------------------------------------ */

interface StatusAction {
  label: string;
  to?: string;
  run?: () => void;
  busy?: boolean;
}

interface StatusNotice {
  id: string;
  tone: "error" | "warn" | "quiet";
  tag: string;
  text: string;
  actions: StatusAction[];
}

const resyncRetrying = ref(false);

async function retryResync(): Promise<void> {
  if (resyncRetrying.value) return;
  resyncRetrying.value = true;
  try {
    await restoreRuntimeState("resync");
  } finally {
    resyncRetrying.value = false;
  }
}

function retryHistory(): void {
  void session.retryHistory();
}

function retryLayoutSave(): void {
  void shell.flush();
}

function reconnectEvents(): void {
  events.connect();
}

/**
 * 只收「影响整个对话」的真实状态：
 * 历史读取失败（不是没有历史）、连接/同步异常、全局错误与警告、凭据不可用、
 * 能力降级（兼容模式）、布局偏好保存失败。
 * 单轮失败 / 中断 / 取消不在这里 —— 它们由对应轮次自己说明（TurnProcess）。
 */
const statusNotices = computed<StatusNotice[]>(() => {
  const out: StatusNotice[] = [];
  if (session.history.status === "error") {
    out.push({
      id: "history",
      tone: "quiet",
      tag: "历史",
      text: "历史记录暂时无法读取（已经加载的内容仍保留，不代表没有历史）",
      actions: [{ label: "重试", run: retryHistory }],
    });
  }
  if (session.resyncState === "failed") {
    out.push({
      id: "resync",
      tone: "warn",
      tag: "同步",
      text: "断线后的状态同步没有完成：当前显示可能不是最新",
      actions: [{ label: "重新同步", run: () => void retryResync(), busy: resyncRetrying.value }],
    });
  } else if (session.resyncState === "resyncing") {
    out.push({
      id: "resyncing",
      tone: "quiet",
      tag: "同步",
      text: "连接恢复后正在同步最新状态…",
      actions: [],
    });
  }
  if (events.error) {
    out.push({
      id: "connection",
      tone: "warn",
      tag: "连接",
      text: events.error,
      actions: [{ label: "重新连接", run: reconnectEvents }],
    });
  }
  if (session.lastError) {
    out.push({
      id: "lastError",
      tone: "error",
      tag: "错误",
      text: session.lastError,
      actions: [
        { label: "查看详情", to: "/debug" },
        { label: "前往设置", to: "/settings" },
      ],
    });
  } else if (session.warning) {
    out.push({ id: "warning", tone: "warn", tag: "提示", text: session.warning, actions: [] });
  } else if (events.credentialNotice) {
    out.push({
      id: "credential",
      tone: "warn",
      tag: "提示",
      text: events.credentialNotice,
      actions: [{ label: "前往设置", to: "/settings" }],
    });
  } else if (session.lastTurnOutcome?.status === "cancelled") {
    // 取消是正常结局：安静地说一声（具体轮次状态仍由那一轮自己的过程区说明）
    out.push({
      id: "cancelled",
      tone: "quiet",
      tag: "已停止",
      text: "这一轮已按你的要求停止，可以继续输入",
      actions: [],
    });
  } else if (session.lastTurnOutcome?.status === "incomplete") {
    // 不完整结束：已收到的正文保留，但这**不是完成**（重试入口在过程区里）
    out.push({
      id: "incomplete",
      tone: "quiet",
      tag: "未完成",
      text: "这一轮没有正常结束：回答可能不完整，可以在过程区里重试",
      actions: [],
    });
  }
  if (events.fallbackNotice) {
    out.push({
      id: "fallback",
      tone: "quiet",
      tag: "兼容模式",
      text: events.fallbackNotice,
      actions: [{ label: "知道了", run: () => { events.fallbackNotice = null; } }],
    });
  }
  if (shell.saveError) {
    out.push({
      id: "layout",
      tone: "warn",
      tag: "布局",
      text: shell.saveError,
      actions: [{ label: "重试保存", run: retryLayoutSave }],
    });
  }
  return out;
});

/* ------------------------------------------------------------------ */
/* 底部让位 / 键盘                                                    */
/* ------------------------------------------------------------------ */

/** 底部候选块：按输入区真实高度让位（输入区是固定贴底的气泡） */
const bottomCluster = ref<HTMLElement | null>(null);
useComposerClearance(bottomCluster);

function focusComposer(e: MouseEvent): void {
  const el = document.getElementById("composer-input");
  if (!el) return;
  e.preventDefault();
  (el as HTMLTextAreaElement).focus();
}

function onWindowKeydown(event: KeyboardEvent): void {
  if (event.key !== "Escape") return;
  if (detailsOpen.value) {
    detailsOpen.value = false;
  }
}

onMounted(() => {
  window.addEventListener("keydown", onWindowKeydown);
});

onBeforeUnmount(() => {
  window.removeEventListener("keydown", onWindowKeydown);
});
</script>

<template>
  <section class="chat-column" aria-label="对话工作区" data-qio-scope="chat" data-qio-fit>
    <a class="skip-link" href="#composer-input" @click="focusComposer">跳到输入框</a>

    <!-- 话题栏：标题常驻；点击展开完整说明 -->
    <header class="topic-bar" data-qio-fit>
      <button
        class="topic-main"
        type="button"
        :aria-expanded="detailsOpen ? 'true' : 'false'"
        aria-controls="topic-details"
        :title="`${topicTitle}：点击查看话题说明与历史续接状态`"
        @click="toggleDetails"
      >
        <span class="topic-mark mono" aria-hidden="true">TOPIC</span>
        <span class="topic-title serif" data-test="topic-title">{{ topicTitle }}</span>
        <span v-if="continuationLine" class="topic-chip mono pending" data-test="topic-continuation">
          {{ continuationLine }}
        </span>
        <span v-else-if="hasAnchorPosition" class="topic-chip mono" data-test="topic-anchor">
          {{ anchorTitle ? `从「${anchorTitle}」继续` : "从选中的历史位置继续" }}
        </span>
        <span class="chev" :class="{ open: detailsOpen }" aria-hidden="true">⌄</span>
      </button>
    </header>

    <!-- 话题完整说明（打开才读；读不到就说读不到） -->
    <section
      v-if="detailsOpen"
      id="topic-details"
      class="topic-details"
      role="region"
      aria-label="话题说明"
      data-qio-fit
    >
      <div class="td-head">
        <span class="td-name serif">{{ detail?.name || topicTitle }}</span>
        <button
          class="td-close"
          type="button"
          aria-label="收起话题说明"
          @click="detailsOpen = false"
        >
          ×
        </button>
      </div>
      <p v-if="detailLoading" class="td-note" role="status">正在读取话题说明…</p>
      <template v-else-if="detailError">
        <p class="td-note err" role="status">{{ detailError }}</p>
        <button class="qio-btn mini quiet" type="button" @click="retryTopicDetail">重试</button>
      </template>
      <template v-else-if="detail">
        <p class="td-summary" :class="{ empty: !detailSummary }">
          {{ detailSummary || "这个话题还没有描述（读到的数据里没有摘要，这里不编造）" }}
        </p>
        <dl v-if="detailRows.length" class="td-rows">
          <div v-for="row in detailRows" :key="row.label" class="td-row">
            <dt class="mono">{{ row.label }}</dt>
            <dd>{{ row.value }}</dd>
          </div>
        </dl>
      </template>
      <p v-else-if="!session.currentTopicId" class="td-note">
        还没有话题：发出第一条消息后会建立话题。
      </p>
      <!-- 历史续接状态：说清「位置在哪」与「下一条会从哪继续」的区别 -->
      <div class="td-continuation">
        <span class="td-label mono">历史续接</span>
        <p v-if="session.pendingContinuation" class="td-line">
          {{ continuationLine }}（已经登记，发出下一条消息时生效；可在输入区上方的「继续」入口取消）
        </p>
        <p v-else-if="hasAnchorPosition" class="td-line">
          当前位置停在历史片段<template v-if="anchorTitle">「{{ anchorTitle }}」</template>上：下一条消息会从这里继续，
          不会排到话题最新位置。
        </p>
        <p v-else class="td-line">当前位置在话题最新处：下一条消息会接在最新内容后面。</p>
      </div>
    </section>

    <!-- 首次引导的轻提示（可关闭；不是错误） -->
    <div v-if="onboarding.hintVisible" class="setup-hint" role="status" data-qio-fit>
      <span class="text">还没设置完 QIO —— 补上模型、称呼与目标，它才会真正记得你</span>
      <button class="link resume" type="button" @click="onboarding.reopen()">继续设置</button>
      <button class="close" type="button" aria-label="关闭提示" @click="onboarding.setHintDismissed(true)">×</button>
    </div>

    <!-- 全局状态区：只放影响整个对话的事项（轮次内部状态留在轮次里） -->
    <div v-if="statusNotices.length" class="status-zone" data-qio-fit>
      <div
        v-for="notice in statusNotices"
        :key="notice.id"
        class="notice"
        :class="notice.tone === 'error' ? 'err' : notice.tone"
        :data-notice="notice.id"
        :data-test="notice.id === 'incomplete' ? 'turn-incomplete-notice' : undefined"
        :role="notice.tone === 'error' ? 'alert' : 'status'"
        data-qio-fit
      >
        <span class="kind mono">{{ notice.tag }}</span>
        <span class="text">{{ notice.text }}</span>
        <template v-for="action in notice.actions" :key="action.label">
          <router-link v-if="action.to" class="link" :to="action.to">{{ action.label }}</router-link>
          <button
            v-else
            class="link"
            type="button"
            :disabled="action.busy"
            @click="action.run?.()"
          >
            {{ action.busy ? `${action.label}…` : action.label }}
          </button>
        </template>
      </div>
    </div>

    <MessageStream class="message-stage" />

    <!-- 底部候选：需要用户决定的高影响知识；输入区是固定气泡，这里按它的高度让位 -->
    <div ref="bottomCluster" class="bottom-cluster" data-qio-fit>
      <div
        v-if="session.knowledgeCandidates.length"
        class="candidates"
        data-qio-fit
        v-qio-scrollbar="{ policy: 'auto', axis: 'y' }"
      >
        <KnowledgeCandidateCard
          v-for="candidate in session.knowledgeCandidates"
          :key="candidate.knowledgeId"
          :candidate="candidate"
        />
      </div>
    </div>

    <ComposerDock />
  </section>
</template>

<style scoped>
.chat-column {
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
  height: 100%;
  background: var(--bg-base);
  color: var(--text-primary);
  font-family: var(--sans);
  position: relative;
}
/* ---- 话题栏 ---- */
.topic-bar {
  flex: none;
  margin: 8px 12px 0;
  min-width: 0;
}
.topic-main {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  min-width: 0;
  padding: 8px 14px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-lg);
  background: var(--bg-surface);
  box-shadow: var(--shadow-1);
  color: inherit;
  text-align: left;
  cursor: pointer;
  transition: border-color var(--dur-fast) var(--ease-1), background var(--dur-fast) var(--ease-1);
}
.topic-main:hover {
  border-color: var(--border-strong);
}
.topic-main:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 1px;
}
.topic-mark {
  flex: none;
  font-size: 9px;
  letter-spacing: 0.1em;
  color: var(--text-faint);
}
.topic-title {
  font-size: 15px;
  font-weight: 600;
  color: var(--text-strong);
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.topic-chip {
  flex: none;
  max-width: min(340px, 42%);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  padding: 1px 8px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-pill);
  font-size: 10.5px;
  color: var(--text-muted);
  letter-spacing: 0.02em;
}
.topic-chip.pending {
  border-color: var(--accent);
  color: var(--accent);
}
.topic-main .chev {
  flex: none;
  margin-left: auto;
  color: var(--text-muted);
  font-size: 12px;
  transition: transform var(--dur-fast) var(--ease-1);
}
.topic-main .chev.open {
  transform: rotate(180deg);
}
/* ---- 话题说明 ---- */
.topic-details {
  flex: none;
  margin: 6px 12px 0;
  padding: 10px 14px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  background: var(--bg-surface);
  box-shadow: var(--shadow-1);
  font-size: var(--fs-sm);
  min-width: 0;
}
.td-head {
  display: flex;
  align-items: baseline;
  gap: 10px;
}
.td-name {
  font-size: var(--fs-md);
  color: var(--text-strong);
}
.td-close {
  margin-left: auto;
  border: none;
  background: none;
  padding: 0 2px;
  color: var(--text-muted);
  font-size: 16px;
  line-height: 1;
  cursor: pointer;
}
.td-close:hover {
  color: var(--text-strong);
}
.td-close:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 1px;
}
.td-note {
  margin: 6px 0 4px;
  color: var(--text-secondary);
  font-size: var(--fs-xs);
  overflow-wrap: anywhere;
}
.td-note.err {
  color: var(--danger);
}
.td-summary {
  margin: 6px 0 0;
  color: var(--text-primary);
  line-height: 1.7;
  overflow-wrap: anywhere;
}
.td-summary.empty {
  color: var(--text-muted);
  font-size: var(--fs-xs);
}
.td-rows {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 18px;
  margin: 8px 0 0;
}
.td-row {
  display: flex;
  align-items: baseline;
  gap: 6px;
  min-width: 0;
}
.td-row dt {
  color: var(--text-muted);
  font-size: 10.5px;
  letter-spacing: 0.04em;
}
.td-row dd {
  margin: 0;
  color: var(--text-secondary);
  font-size: var(--fs-xs);
  overflow-wrap: anywhere;
}
.td-continuation {
  margin-top: 10px;
  padding-top: 8px;
  border-top: 1px solid var(--border-subtle);
  min-width: 0;
}
.td-label {
  color: var(--text-muted);
  font-size: 10.5px;
  letter-spacing: 0.05em;
}
.td-line {
  margin: 4px 0 0;
  color: var(--text-secondary);
  font-size: var(--fs-xs);
  line-height: 1.65;
  overflow-wrap: anywhere;
}
/* ---- 首次引导 ---- */
.setup-hint {
  flex: none;
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
  padding: 7px 14px;
  margin: 6px 12px 0;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  background: var(--accent-soft);
  color: var(--text-secondary);
  font-size: var(--fs-sm);
  min-width: 0;
}
.setup-hint .text {
  flex: 1;
  min-width: 0;
  overflow-wrap: anywhere;
}
.setup-hint .close {
  border: none;
  background: none;
  color: var(--text-muted);
  font-size: 14px;
  line-height: 1;
  cursor: pointer;
}
/* ---- 全局状态区 ---- */
.status-zone {
  flex: none;
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin: 6px 12px 0;
  min-width: 0;
}
.notice {
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 8px 12px;
  padding: 6px 12px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  background: var(--bg-surface);
  font-size: var(--fs-sm);
  min-width: 0;
}
.notice.err {
  border-color: var(--border-danger);
  color: var(--danger);
}
.notice.warn {
  border-color: var(--warning);
}
.notice.quiet {
  color: var(--text-secondary);
}
.notice .kind {
  flex: none;
  font-size: 10.5px;
  letter-spacing: 0.08em;
  padding: 1px 8px;
  border-radius: var(--r-pill);
  border: 1px solid currentColor;
  opacity: 0.85;
}
.notice .text {
  flex: 1;
  min-width: 0;
  overflow-wrap: anywhere;
}
.notice .link {
  flex: none;
  align-self: baseline;
  padding: 0;
  border: none;
  background: none;
  font: inherit;
  color: var(--link);
  text-decoration: none;
  cursor: pointer;
  overflow-wrap: anywhere;
}
.notice .link:hover {
  text-decoration: underline;
}
.notice .link:disabled {
  color: var(--text-muted);
  text-decoration: none;
  cursor: default;
}
/* ---- 消息流 / 底部候选 ---- */
.message-stage {
  flex: 1;
  min-height: 0;
  display: flex;
}
.bottom-cluster {
  flex: none;
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
}
.candidates {
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 0 12px;
  min-width: 0;
  max-height: 42vh;
  overflow-y: auto;
  overflow-x: clip;
}
.skip-link {
  position: absolute;
  left: -9999px;
  top: 0;
  z-index: 5;
}
.skip-link:focus {
  left: 12px;
  top: 8px;
  padding: 6px 10px;
  background: var(--bg-elevated);
  border: 1px solid var(--accent);
  border-radius: var(--r-sm);
  color: var(--text-strong);
}
</style>