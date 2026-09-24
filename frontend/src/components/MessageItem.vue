<script setup lang="ts">
import { computed, ref } from "vue";
import MarkdownContent from "./MarkdownContent.vue";
import ToolCreationCard from "./ToolCreationCard.vue";
import { useSessionStore } from "../stores/session";
import { useEventStore } from "../stores/events";
import { useUiStore } from "../stores/ui";
import type { StreamMessage } from "../stores/session";

const props = defineProps<{ message: StreamMessage; showTopic?: boolean }>();
const session = useSessionStore();
const events = useEventStore();
const ui = useUiStore();

/**
 * token 下缀：只显示「这条消息所属 turn」的用量（turn_id 归属），
 * 且只在开发者模式显示。后端当前只提供单 turn 总 token，
 * 没有 input/output 分解就不显示分解数字（不编造）。
 */
const tokText = computed(() => {
  if (!ui.developerMode) return "";
  const usage = events.turnUsageFor(props.message.turnId);
  if (!usage) return "";
  return `token ${usage.tokens.toLocaleString("en-US")}`;
});

const open = ref(false);
/** 本次会话新产生的消息：最多一次很短的入场；历史消息不带这个标记 */
const isFresh = computed(() => session.freshIds.includes(props.message.id));
/** 复制结果：只有真的写进剪贴板才显示「已复制」 */
const copyState = ref<"idle" | "ok" | "fail">("idle");
let copyTimer: ReturnType<typeof setTimeout> | null = null;

const copyLabel = computed(() => {
  if (copyState.value === "ok") return "已复制";
  if (copyState.value === "fail") return "复制失败";
  return "复制";
});

/** 复制消息内容；剪贴板不存在或写入被拒绝时给出失败反馈，由用户手动选择复制 */
async function copyContent() {
  const text = props.message.content ?? "";
  if (!text) return;
  let ok = false;
  try {
    const cb = navigator.clipboard;
    if (cb && typeof cb.writeText === "function") {
      await cb.writeText(text);
      ok = true;
    }
  } catch {
    ok = false;
  }
  copyState.value = ok ? "ok" : "fail";
  if (copyTimer) clearTimeout(copyTimer);
  copyTimer = setTimeout(() => {
    copyState.value = "idle";
    copyTimer = null;
  }, ok ? 1600 : 2400);
}

/** 呈现优先：title 兜底工具名 */
const toolTitle = computed(
  () => props.message.presentation?.title || props.message.toolName || "工具调用",
);

/**
 * 工具卡状态：运行中就说运行中，不为「还在跑」画一个 ✓。
 * 用户必须能分辨「正在执行」与「已完成」（spec 第 32~34 条）。
 *
 * 数据模型里区分五种语义（运行中 / 成功 / 失败 / 已取消 / 结果未收到）；
 * 视觉上沿用既有的卡片状态（取消与未知复用最接近的失败外观），
 * 但文案必须说出真实状态 —— 「取消」不是「失败」，「结果未收到」也不是。
 */
const toolState = computed<"running" | "success" | "failed" | "cancelled" | "unknown">(() => {
  if (props.message.toolStatus) return props.message.toolStatus;
  // 老数据（历史 / 测试构造的消息）没有 toolStatus：按旧字段兜底
  if (props.message.toolRunning === true) return "running";
  return props.message.toolOk === false ? "failed" : "success";
});
const toolRunning = computed(() => toolState.value === "running");
const toolFailed = computed(() => !toolRunning.value && toolState.value !== "success");
const toolMark = computed(() => {
  if (toolRunning.value) return "◌";
  if (toolState.value === "cancelled") return "⊘";
  if (toolState.value === "unknown") return "?";
  return toolFailed.value ? "✕" : "✓";
});
/**
 * 卡片上的状态徽标：运行中 / 已取消由语义决定；
 * 其余（成功、失败）沿用工具自己的呈现文案，没有就不显示（不制造噪声）。
 */
const toolStateLabel = computed(() => {
  if (toolRunning.value) return "运行中";
  if (toolState.value === "cancelled") return "已取消";
  return props.message.presentation?.status || "";
});

/** 耗时：只有拿得到且有意义（≥0.1s）时才显示，不编造数字 */
const durationText = computed(() => {
  const ms = props.message.toolDurationMs;
  if (typeof ms !== "number" || !Number.isFinite(ms) || ms < 100) return "";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
});

/**
 * 工具失败时卡片上的一行结论（完整错误仍在折叠详情里）。
 *
 * 上限从 60 放到 120 字：60 字会吃掉「列目录失败：[WinError 3] …」这类真正
 * 有信息量的原因，用户只看到一句笼统的「这次执行没有成功」，等于又静默了一次。
 */
const FAILURE_LINE_LIMIT = 120;
const toolFailureLine = computed(() => {
  if (!toolFailed.value) return "";
  const raw = (props.message.toolError ?? "").trim();
  if (raw) {
    if (raw.length <= FAILURE_LINE_LIMIT) return raw;
    return `${raw.slice(0, FAILURE_LINE_LIMIT)}…（展开可看完整原因）`;
  }
  if (toolState.value === "cancelled") return "这次执行已取消";
  if (toolState.value === "unknown") return "结果未收到";
  return "这次执行没有成功";
});

/** 独立任务的状态文案（用户看到的是「独立任务」，不是内部智能体进程） */
const SUBAGENT_LABELS: Record<string, string> = {
  queued: "开始",
  running: "进行中",
  done: "已完成",
  failed: "失败",
};
const subagentLabel = computed(
  () => SUBAGENT_LABELS[props.message.taskStatus ?? "running"] ?? "进行中",
);
const subagentRunning = computed(
  () => props.message.taskStatus === "queued" || props.message.taskStatus === "running",
);

/** 原始工具名：只用于悬停提示 / 排查，不作为界面标题（界面标题是中文展示名） */
const rawToolName = computed(
  () => props.message.presentation?.tool || props.message.toolName || "工具",
);

/** 呈现优先：summary 兜底原始内容预览 */
const toolSummary = computed(
  () => props.message.presentation?.summary || props.message.content || "",
);

/** 工具调用历史的全文（参数 + 输出）：展开时才去取，避免历史页一次带上几十万字 */
const toolArgsText = computed(() => props.message.toolArgs ?? "");
const toolRecordLoading = computed(() => props.message.toolRecordLoading === true);
const toolRecordError = computed(() => props.message.toolRecordError ?? "");
/** 库里没有输出正文时，说清是「没保存」还是「按保留期清掉了」 */
const toolMissingNote = computed(() => {
  if (!props.message.toolOutputMissing) return "";
  return props.message.toolMissingReason === "setting"
    ? "完整输出未保存（设置里关闭了「保存工具输出全文」）"
    : "完整输出已按保留设置清理";
});

/**
 * 展开卡片：首次展开时按记录 id 取全文（参数 + 输出）。
 *
 * 实时与历史两种卡片走同一条路径 —— 实时事件只带 200 字预览，
 * 历史预览 400 字，全文都只在库里。
 */
function toggleTool() {
  open.value = !open.value;
  if (!open.value) return;
  const m = props.message;
  if (!m.toolRecordId || m.toolRecordLoaded || m.toolRecordLoading) return;
  void session.loadToolRecord(m.id);
}

function formatTime(iso?: string): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

const topicLine = computed(() => {
  // 只展示话题名，不暴露内部话题 ID（此前 slice(-4) 会显示"话题 #5320"这类编号，
  // 用户误以为是回答内容）
  return props.message.topicName || session.topicName || "默认话题";
});

/** 单行 metadata：时间 +（仅开发者模式）本 turn token */
const metaText = computed(() => {
  const time = formatTime(props.message.createdAt);
  return tokText.value ? `${time} · ${tokText.value}` : time;
});
</script>

<template>
  <div class="message" :class="[message.role, { fresh: isFresh }]">
    <template v-if="message.role === 'user'">
      <div class="bubble user-bubble">
        <div class="plain">{{ message.content }}</div>
      </div>
      <div class="meta mono">
        <span v-if="message.queued" class="queued-tag">等待中</span>
        <span class="ts">{{ formatTime(message.createdAt) }}</span>
        <button v-if="!message.queued" class="copy-btn" type="button" :class="{ fail: copyState === 'fail' }" @click="copyContent">
          {{ copyLabel }}
        </button>
      </div>
    </template>

    <template v-else-if="message.role === 'tool'">
      <div
        class="tool-card qio-card"
        :class="{ fail: toolFailed, running: toolRunning, open }"
        :data-state="toolRunning ? 'running' : toolFailed ? 'failed' : 'ready'"
        :title="rawToolName"
      >
        <button
          class="tool-head"
          type="button"
          @click="toggleTool"
          :aria-expanded="open"
          :title="open ? '收起' : '展开'"
        >
          <span
            class="tool-mark"
            :class="toolRunning ? 'running' : toolFailed ? 'fail' : 'ok'"
          >
            {{ toolMark }}
          </span>
          <span class="tool-name mono">{{ toolTitle }}</span>
          <span
            v-if="toolRunning || toolStateLabel"
            class="tool-status qio-state"
            :class="toolRunning ? 'running info' : toolFailed ? 'fail err' : 'ok'"
          >
            {{ toolStateLabel }}
          </span>
          <span v-if="durationText" class="tool-duration mono">{{ durationText }}</span>
          <span class="tool-time mono">{{ formatTime(message.createdAt) }}</span>
          <span class="tool-chev">{{ open ? "▾" : "▸" }}</span>
        </button>
        <!-- 结论写在卡面上：失败不能只藏在折叠详情里 -->
        <p v-if="toolFailureLine" class="tool-fail-line" role="status">{{ toolFailureLine }}</p>
        <!-- 展开/收起必须连续：用 0fr→1fr 网格行做真实高度过渡，
             不再 v-show 瞬切（detail-wrap 不设内边距，否则收起时会留下细条） -->
        <div class="tool-detail-wrap" :class="{ open }">
          <div class="tool-detail-clip">
            <div class="tool-detail">
              <!-- 参数与输出分开：参数是「它想做什么」，输出是「结果是什么」 -->
              <div v-if="toolArgsText" class="tool-detail-sec">
                <div class="sec-label mono">参数</div>
                <pre>{{ toolArgsText }}</pre>
              </div>
              <div class="tool-detail-sec">
                <div class="sec-label mono">输出</div>
                <pre>{{ toolSummary }}</pre>
              </div>
              <p v-if="toolRecordLoading" class="tool-note mono" role="status">正在读取完整输出…</p>
              <p v-else-if="toolRecordError" class="tool-note err" role="status">
                {{ toolRecordError }}
                <button class="link" type="button" @click="session.loadToolRecord(message.id)">
                  重试
                </button>
              </p>
              <p v-else-if="toolMissingNote" class="tool-note mono" role="status">
                {{ toolMissingNote }}
              </p>
              <p v-else-if="message.toolTruncated" class="tool-note mono" role="status">
                输出过长，已截断（单条上限 4 万字）
              </p>
              <p v-if="message.toolError" class="tool-error">{{ message.toolError }}</p>
            </div>
          </div>
        </div>
      </div>
    </template>

    <!-- 独立任务：与「普通工具」明确区分（spec 第 61~65 条） -->
    <template v-else-if="message.role === 'subagent'">
      <div
        class="subagent-card qio-card"
        :class="{ fail: message.taskStatus === 'failed' }"
        :data-state="subagentRunning ? 'running' : message.taskStatus === 'failed' ? 'failed' : 'ready'"
      >
        <div class="sub-head">
          <span class="sub-kind qio-tag">独立任务</span>
          <span class="sub-name serif">{{ message.toolName || "未命名任务" }}</span>
          <span
            class="sub-state qio-state"
            :class="subagentRunning ? 'running info' : message.taskStatus === 'failed' ? 'err' : 'ok'"
          >{{ subagentLabel }}</span>
        </div>
        <p class="sub-line">
          QIO 正在单独处理这项任务{{ subagentRunning ? "" : "（已完成）" }}
        </p>
        <p v-if="message.taskGoal" class="sub-goal">目标：{{ message.taskGoal }}</p>
        <p v-if="!subagentRunning && message.content" class="sub-result">{{ message.content }}</p>
        <p v-if="message.taskStatus === 'failed'" class="sub-fail" role="status">
          这项独立任务没有完成：{{ message.toolError || "展开后可让 QIO 继续处理" }}
        </p>
      </div>
    </template>

    <!-- 工具创建：一条流程一张卡，原地推进 -->
    <template v-else-if="message.role === 'tool_creation'">
      <ToolCreationCard :message="message" />
    </template>

    <template v-else>
      <!-- 流式生成中的正文不逐字播报给辅助阅读工具（aria-busy + 不设 live 区域），
           落定后由正常文档流阅读即可。 -->
      <div
        class="bubble assist-bubble"
        :class="{ interim: message.interim }"
        :aria-busy="message.streaming ? 'true' : undefined"
        :aria-live="message.streaming ? 'off' : undefined"
      >
        <div v-if="message.interim" class="interim-tag mono">◈ 过程</div>
        <div v-if="showTopic" class="tname serif">{{ topicLine }}</div>
        <MarkdownContent
          :source="message.content"
          :reveal="!!message.streaming"
          :cps="ui.typewriterCps"
          :pace-ms="message.paceMs ?? null"
        />
      </div>
      <div class="meta mono">
        <span class="ts">{{ metaText }}</span>
        <button class="copy-btn" type="button" :class="{ fail: copyState === 'fail' }" @click="copyContent">
          {{ copyLabel }}
        </button>
      </div>
    </template>
  </div>
</template>

<style scoped>
.message {
  display: flex;
  flex-direction: column;
  /* 左半边完整显示：比原 640px 更宽，宽屏下消息占左侧更充分 */
  max-width: min(760px, 100%);
  margin: 6px 0;
  font-size: 14.5px;
  line-height: 1.75;
}
/* 本次会话新产生的消息最多一次很短的入场；历史消息没有这个类，不会重播 */
.message.fresh {
  animation: msg-in var(--dur-menu) var(--ease-out) both;
}
@keyframes msg-in {
  from { opacity: 0; transform: translateY(3px); }
  to { opacity: 1; transform: none; }
}
.message.user {
  margin-left: auto;
  align-items: flex-end;
  /* 用户消息收窄：靠 alignment + 字色区分角色，不用整块色填充 */
  max-width: min(620px, 88%);
}
.message.assistant {
  margin-right: auto;
  align-items: flex-start;
}
.bubble {
  padding: 10px 16px;
}
/* 用户侧：无底色，右侧玫红细规线 + 右对齐位置 —— editorial 对话而非 IM 气泡 */
.user-bubble {
  background: transparent;
  color: var(--text-strong);
  border-right: 2px solid var(--accent);
  border-radius: 0;
  padding: 2px 14px 2px 16px;
  text-align: left;
}
.user-bubble .plain {
  white-space: pre-wrap;
}
.assist-bubble {
  background: var(--bg-elevated);
  border: 1px solid var(--border-subtle);
  border-radius: 14px 14px 14px 4px;
}
.tname {
  font-size: 16px;
  font-weight: 600;
  color: var(--text-strong);
  margin-bottom: 6px;
}
.assist-bubble.interim {
  background: transparent;
  border-style: dashed;
}
.interim-tag {
  display: inline-flex;
  align-items: center;
  margin-bottom: 6px;
  font-size: 10.5px;
  color: var(--text-muted);
  letter-spacing: 0.05em;
}
/* 元数据：低调存在，hover / focus 时才完全显形（第一眼只看内容） */
.meta {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 6px;
  font-size: 10.5px;
  color: var(--text-muted);
  letter-spacing: 0.05em;
  opacity: 0.62;
  transition: opacity var(--dur-fast) var(--ease);
}
.message:hover .meta,
.message:focus-within .meta {
  opacity: 1;
}
.ts {
  color: var(--text-muted);
}
.queued-tag {
  color: var(--text-secondary);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-pill);
  padding: 0 8px;
  letter-spacing: 0.06em;
}
.copy-btn {
  border: none;
  background: none;
  padding: 0 2px;
  font: inherit;
  color: var(--text-muted);
  cursor: pointer;
  opacity: 0;
  transition: opacity var(--dur-fast) var(--ease), color var(--dur-fast) var(--ease);
}
.message:hover .copy-btn,
.copy-btn:focus-visible {
  opacity: 1;
}
/* 触屏没有 hover：复制入口必须默认可见 */
@media (hover: none) {
  .copy-btn { opacity: 1; }
}
.copy-btn:hover {
  color: var(--accent);
}
.copy-btn.fail {
  color: var(--danger);
  opacity: 1;
}
.copy-btn:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 2px;
  border-radius: var(--r-xs);
}
/* ---- 工具卡 ---- */
/* 卡片家族共用契约：`.qio-card`（底/边/圆角/层级）+ `data-state`（running/waiting/ready/failed）。
   工具卡是紧凑行式卡片，所以在这里覆盖内边距并把圆角对齐到家族值。 */
.tool-card {
  margin: 4px 0;
  padding: 0;
  border-radius: var(--r-lg);
  overflow: hidden;
  font-size: 12.5px;
}
.tool-card.fail {
  border-color: var(--border-danger);
}
/* 运行中的卡不抢注意力：只用细边框 + 文字表达「还在跑」 */
.tool-card.running {
  border-style: dashed;
}
.tool-head {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 8px 12px;
  background: transparent;
  border: none;
  cursor: pointer;
  text-align: left;
  font-size: 11px;
  color: var(--text-secondary);
}
.tool-head:hover {
  background: var(--layer-hover);
}
.tool-mark {
  font-size: 12px;
}
.tool-mark.ok {
  color: var(--success);
}
.tool-mark.fail {
  color: var(--danger);
}
.tool-mark.running {
  color: var(--link);
}
.tool-name {
  color: var(--text-strong);
  letter-spacing: 0.02em;
}
.tool-status {
  padding: 1px 8px;
  border-radius: 20px;
  font-family: var(--mono);
  font-size: 10px;
  letter-spacing: 0.04em;
  border: 1px solid currentColor;
}
.tool-status.ok {
  color: var(--success);
}
.tool-status.fail {
  color: var(--danger);
}
.tool-status.running {
  color: var(--link);
}
.tool-duration {
  font-size: 10px;
  color: var(--text-muted);
  letter-spacing: 0.04em;
}
.tool-fail-line {
  margin: 0;
  padding: 0 12px 8px;
  color: var(--danger);
  font-size: 12px;
  /* 失败原因可能是一整条路径（没有空格可断行）：必须允许任意位置折行，
    否则窄窗口下会把卡片撑出横向溢出。 */
  overflow-wrap: anywhere;
}
.tool-detail-sec + .tool-detail-sec {
  margin-top: 8px;
}
.sec-label {
  font-size: 11px;
  color: var(--text-muted);
  letter-spacing: 0.08em;
}
.tool-note {
  margin: 6px 0 0;
  font-size: 12px;
  color: var(--text-muted);
}
.tool-note.err {
  color: var(--danger);
}
.tool-time {
  margin-left: auto;
  color: var(--text-muted);
  letter-spacing: 0.05em;
}
.tool-chev {
  color: var(--text-muted);
  flex-shrink: 0;
  transition: transform var(--mo-1-state) var(--ease-1);
}
.tool-card.open .tool-chev { transform: rotate(90deg); }
/* 展开/收起：真实高度过渡（0fr → 1fr），收起后不占位、不拦截点击 */
.tool-detail-wrap {
  display: grid;
  grid-template-rows: 0fr;
  transition: grid-template-rows var(--mo-2-in) var(--ease-2);
}
.tool-detail-wrap.open { grid-template-rows: 1fr; }
.tool-detail-clip {
  overflow: hidden;
  min-height: 0;
  opacity: 0;
  transition: opacity var(--mo-2-in) var(--ease-2);
}
.tool-detail-wrap.open .tool-detail-clip { opacity: 1; }
.tool-detail {
  border-top: 1px solid var(--border-subtle);
  padding: 10px 12px;
  background: var(--bg-inset);
}
.tool-detail pre {
  font-size: 11px;
  white-space: pre-wrap;
  color: var(--text-secondary);
  margin: 0;
  font-family: var(--mono);
}
.tool-error {
  color: var(--danger);
  font-size: 12px;
  margin: 4px 0 0;
}
/* ---- 独立任务卡 ---- */
.subagent-card {
  margin: 4px 0;
  border-left: 2px solid var(--link);
  border-radius: var(--r-lg);
  padding: 10px 12px;
  font-size: 12.5px;
}
.subagent-card.fail {
  border-color: var(--border-danger);
  border-left-color: var(--danger);
}
.sub-head {
  display: flex;
  align-items: center;
  gap: 10px;
}
.sub-kind {
  font-family: var(--mono);
  font-size: 10px;
  letter-spacing: 0.06em;
  color: var(--text-muted);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-pill);
  padding: 0 8px;
}
.sub-name {
  color: var(--text-strong);
  font-size: 14px;
}
.sub-state {
  margin-left: auto;
  font-size: 11px;
  color: var(--text-secondary);
}
.sub-state.running {
  color: var(--link);
}
.sub-line,
.sub-goal,
.sub-result,
.sub-fail {
  margin: 6px 0 0;
  color: var(--text-secondary);
  line-height: 1.6;
}
.sub-result {
  color: var(--text-primary);
  white-space: pre-wrap;
}
.sub-fail {
  color: var(--danger);
}
</style>
