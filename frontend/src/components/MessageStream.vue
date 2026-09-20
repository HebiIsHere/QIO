<script setup lang="ts">
/**
 * 消息流：@tanstack/vue-virtual 虚拟滚动 + 底部跟随。
 * 按「用户消息开新轮次」把消息分组为 turn，虚拟化单位是轮次块；
 * 每轮渲染 TURN 分隔头（等宽）+ 消息项（MessageItem）。
 */
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from "vue";
import { useVirtualizer } from "@tanstack/vue-virtual";
import { useSessionStore } from "../stores/session";
import type { StreamMessage } from "../stores/session";
import MessageItem from "./MessageItem.vue";
import ContinueBar from "./ContinueBar.vue";
import QueueChip from "./QueueChip.vue";
import { turnLabel } from "../utils/turnLabel";
import { prefersReducedMotion } from "../utils/motion";

/** 对话内容列宽：用户消息与回答共用一个居中列，不分别贴窗口两端 */
const CONTENT_MAX_PX = 860;

interface Turn {
  id: string;
  index: number;
  startedAt: string;
  items: StreamMessage[];
}

const session = useSessionStore();
const containerRef = ref<HTMLDivElement | null>(null);
/** 卸载阶段模板 ref 会被置空，这里留一份引用，保证离开时仍能读到滚动位置 */
let lastContainer: HTMLDivElement | null = null;
const spacerRef = ref<HTMLDivElement | null>(null);
const followBottom = ref(true);
/** 上翻阅读期间新到的消息数（「回到最新消息」入口上的提示数字） */
const unseen = ref(0);
/** 程序化滚动（回到最新/贴底）的 rAF 句柄：用户一操作就中断 */
let programmaticRaf = 0;
/** 正在恢复阅读位置（此时到来的 scroll 事件是程序写入造成的，不算用户意图） */
let restoring = false;
/** 本机发送前的阅读状态：请求被后端拒绝时要放回原位（没发出去的消息不该留下后遗症） */
let preSend: { follow: boolean; top: number } | null = null;
/** 上翻阅读中发送 → 若被拒绝则回到原位；用户中途自己滚过就不再强行放回 */
let pendingRestoreOnReject = false;

/** 距底部多少像素内算「跟随中」 */
const NEAR_BOTTOM_PX = 120;

const messages = computed(() => session.messages);

const turns = computed<Turn[]>(() => {
  const out: Turn[] = [];
  let cur: Turn | null = null;
  let n = 0;
  for (const m of messages.value) {
    if (!cur || m.role === "user" || m.role === "system") {
      n += 1;
      cur = { id: `turn_${n}`, index: n, startedAt: m.createdAt, items: [] };
      out.push(cur);
    }
    cur.items.push(m);
  }
  return out;
});

// options 整体作为 computed：count 依赖轮次数变化时自动 setOptions
const virtualizer = useVirtualizer(
  computed(() => ({
    count: turns.value.length,
    getScrollElement: () => containerRef.value,
    estimateSize: () => 120,
    overscan: 6,
  })),
);

function onScroll() {
  const el = containerRef.value;
  if (!el) return;
  // 恢复阅读位置期间会触发 scroll 事件：那不是用户意图，不能据此改写跟随状态与位置
  if (restoring) return;
  // 只按「距底部距离」判定是否跟随：用户向上滚 → 立刻停止跟随，不再强行拉回
  followBottom.value = el.scrollHeight - el.scrollTop - el.clientHeight <= NEAR_BOTTOM_PX;
  // 同步记住阅读位置：不依赖卸载时的 ref（卸载阶段模板 ref 已经被置空）
  session.streamScrollTop = el.scrollTop;
  session.streamFollowing = followBottom.value;
  if (followBottom.value) unseen.value = 0;
}

/** 用户一动滚轮/触屏：立刻中断程序化滚动（自动滚动不能和用户抢） */
function onUserInput() {
  cancelProgrammaticScroll();
  // 用户自己接管了滚动：发送失败时不再把位置放回发送前
  pendingRestoreOnReject = false;
}

/**
 * 键盘滚动（PageUp/PageDown/方向键/Home/End/空格）：同属用户意图。
 * .stream 现在是可聚焦的滚动区域（tabindex=0），否则键盘用户根本滚不动消息列表。
 */
function onKeyScroll(e: KeyboardEvent) {
  const keys = ["PageUp", "PageDown", "ArrowUp", "ArrowDown", "Home", "End", " ", "Spacebar"];
  if (keys.includes(e.key)) cancelProgrammaticScroll();
}

function scrollToBottom() {
  scrollToBottomWith(false);
}

/**
 * 贴底 / 回到最新。
 * smooth 时用 rAF 自己走（不用 scroll-behavior），这样用户一动滚轮或触屏
 * 就能立刻中断自动滚动——浏览器的平滑滚动是中断不掉的。
 */
function scrollToBottomWith(smooth: boolean) {
  const el = containerRef.value;
  if (!el) return;
  cancelProgrammaticScroll();
  const target = Math.max(0, el.scrollHeight - el.clientHeight);
  const start = el.scrollTop;
  const dist = target - start;
  if (!smooth || prefersReducedMotion() || Math.abs(dist) < 2) {
    el.scrollTop = target;
    return;
  }
  const dur = Math.min(320, 140 + Math.abs(dist) * 0.4);
  const t0 = performance.now();
  const step = () => {
    // 目标每帧重算：虚拟列表在滚动过程中会逐条测量真实高度，总高度会比刚开始时的估算更高；
    // 只按「开始时算出的目标」插值，会在很长的对话里停在半路（实测可差几千像素）。
    const targetNow = Math.max(0, el.scrollHeight - el.clientHeight);
    // 统一用 performance.now()：rAF 回调参数的时间基准在部分环境里与它不同，
    // 混用会让进度算成负数或跳变。进度双向夹住，保证一定会落在目标位置。
    const p = Math.max(0, Math.min(1, (performance.now() - t0) / dur));
    const eased = 1 - Math.pow(1 - p, 3);
    // p 走到 1 时这一步写成当前真实底部，不会留下「差一截」的尾巴
    el.scrollTop = start + (targetNow - start) * eased;
    session.streamScrollTop = el.scrollTop;
    programmaticRaf = p < 1 ? requestAnimationFrame(step) : 0;
  };
  programmaticRaf = requestAnimationFrame(step);
}

function cancelProgrammaticScroll() {
  if (programmaticRaf) cancelAnimationFrame(programmaticRaf);
  programmaticRaf = 0;
}

/** 回到最新：用户明确点击后短暂滚动到最新，并恢复跟随 */
function backToLatest() {
  followBottom.value = true;
  unseen.value = 0;
  // 主动点击即视为恢复跟随：不依赖浏览器随后补发的 scroll 事件
  session.streamFollowing = true;
  scrollToBottomWith(true);
}

/** 右下角输入框高度观察：消息流底部滚动缓冲 = 输入框高 + 间距，
 *  滚动到底时最新消息恰好停在浮动气泡上方（消息少时无额外留白）。 */
let composerObserver: ResizeObserver | null = null;
/** 内容高度观察器：assistant 流式正文变高（不换条）时也要跟随底部 */
let streamObserver: ResizeObserver | null = null;
function composerHeight(): number {
  const el = document.querySelector<HTMLElement>(".composer");
  return el ? el.getBoundingClientRect().height : 0;
}
function applyComposerPad() {
  const el = containerRef.value;
  if (!el) return;
  el.style.paddingBottom = `${Math.round(composerHeight()) + 16}px`;
}
onMounted(() => {
  lastContainer = containerRef.value;
  // 触屏与滚轮属于用户意图：中断自动滚动，并把跟随状态交回用户
  containerRef.value?.addEventListener("wheel", onUserInput, { passive: true });
  containerRef.value?.addEventListener("touchstart", onUserInput, { passive: true });
  containerRef.value?.addEventListener("keydown", onKeyScroll);
  restoreScrollPosition();
  // 内容增高（同一条流式消息变长 / markdown 布局变化）时，若仍在跟随就贴底
  if (typeof ResizeObserver !== "undefined") {
    streamObserver = new ResizeObserver(() => {
      if (followBottom.value) scrollToBottom();
    });
    if (spacerRef.value) streamObserver.observe(spacerRef.value);
  }
  if (typeof ResizeObserver === "undefined") {
    applyComposerPad();
    return;
  }
  composerObserver = new ResizeObserver(applyComposerPad);
  const tryObserve = () => {
    const c = document.querySelector<HTMLElement>(".composer");
    if (c) composerObserver?.observe(c);
    else requestAnimationFrame(tryObserve);
  };
  tryObserve();
  applyComposerPad();
});
onUnmounted(() => {
  containerRef.value?.removeEventListener("wheel", onUserInput);
  containerRef.value?.removeEventListener("touchstart", onUserInput);
  containerRef.value?.removeEventListener("keydown", onKeyScroll);
  lastContainer?.removeEventListener("wheel", onUserInput);
  lastContainer?.removeEventListener("touchstart", onUserInput);
  lastContainer?.removeEventListener("keydown", onKeyScroll);
  cancelProgrammaticScroll();
  saveScrollPosition();
  composerObserver?.disconnect();
  composerObserver = null;
  streamObserver?.disconnect();
  streamObserver = null;
});

/**
 * 阅读位置：离开（打开设置/星球）时记住滚到哪，回来时按原位置恢复，
 * 不重播也不把用户甩到最新。历史消息本来就是直接显示，恢复位置即可。
 */
function saveScrollPosition() {
  const el = containerRef.value ?? lastContainer;
  // 卸载时节点已经脱离文档，浏览器会把 scrollTop 读成 0：这种值不能采信，
  // 否则会把 onScroll 一直同步着的真实位置覆盖掉。
  if (!el || !el.isConnected) return;
  session.streamScrollTop = el.scrollTop;
  session.streamFollowing = followBottom.value;
}

function restoreScrollPosition() {
  followBottom.value = session.streamFollowing;
  const target = session.streamScrollTop;
  if (followBottom.value || target <= 0) return;
  restoring = true;
  // 虚拟列表首次布局可能晚于 mount：容器还不可滚动就下一帧再试（有上限，不会无限重试）
  let tries = 0;
  const attempt = () => {
    const el = containerRef.value;
    if (!el) {
      restoring = false;
      return;
    }
    const max = el.scrollHeight - el.clientHeight;
    if (max <= 0 && tries < 10) {
      tries += 1;
      afterNextPaint(attempt);
      return;
    }
    el.scrollTop = Math.min(target, Math.max(0, max));
    restoring = false;
  };
  afterNextPaint(attempt);
}

/** 等下一帧（无 rAF 的环境退回 setTimeout，保证恢复逻辑不会因为环境而中断） */
function afterNextPaint(cb: () => void) {
  if (typeof requestAnimationFrame === "function") requestAnimationFrame(cb);
  else setTimeout(cb, 0);
}

// 动态测量列表项真实高度（替代固定 estimateSize），避免长消息重叠
function measureItem(el: unknown) {
  if (el instanceof Element) virtualizer.value.measureElement(el);
}

// 新消息 / 末尾消息内容增长（流式）/ turn 开始时都触发跟随检查；
// 只有处于 follow 状态才贴底，用户上翻时绝不强行拉回。
watch(
  () => {
    const last = session.messages[session.messages.length - 1];
    return [session.messages.length, last?.content.length ?? 0, session.turnRunning] as const;
  },
  async (now, prev) => {
    const [count] = now;
    const [prevCount] = prev ?? [count];
    if (!followBottom.value) {
      // 上翻阅读时新到的消息只计数，不主动滚动
      if (count > prevCount) unseen.value += count - prevCount;
      return;
    }
    unseen.value = 0;
    await nextTick();
    // 等布局这一帧里用户可能已经上翻：滚动前再确认一次意图，避免把刚上翻的人拉回去
    if (followBottom.value) scrollToBottom();
  },
);

watch(
  // 只有「本机发送」才回到跟随。后台/排队任务开始的 TURN_START 也会把
  // turnRunning 置为 true，但那时用户可能正在往上读，不能被拽回底部。
  () => session.localSendSeq,
  async () => {
    const el = containerRef.value;
    // 先记住发送前的位置：请求一旦被拒绝，这条消息不存在，阅读位置也不该被改变
    preSend = el ? { follow: followBottom.value, top: el.scrollTop } : null;
    pendingRestoreOnReject = !!el && !followBottom.value;
    followBottom.value = true;
    await nextTick();
    scrollToBottom();
  },
);

// 本机发送被后端拒绝（草稿会回到输入框）：把阅读位置放回发送前，
// 别把正在上翻阅读的人留在「一条从未存在过的消息」的底部。
watch(
  () => session.sendRejectedSeq,
  async () => {
    const remembered = preSend;
    const shouldRestore = pendingRestoreOnReject && remembered && !remembered.follow;
    pendingRestoreOnReject = false;
    preSend = null;
    if (!shouldRestore || !remembered) return;
    followBottom.value = false;
    // 那条「未获受理」的消息连同它的未读计数一起作废
    unseen.value = 0;
    session.streamFollowing = false;
    // 从这里起忽略 scroll 事件：刚才那次贴底还会补送一个 scroll 事件，
    // 它会依据「还停在底部」把状态重新判成「跟随中」，随后内容高度变化又会把位置冲回底部。
    restoring = true;
    await nextTick();
    // 乐观消息已经被撤掉：等这一帧布局落定后再写位置
    afterNextPaint(() => {
      const el = containerRef.value;
      if (!el) {
        restoring = false;
        return;
      }
      el.scrollTop = Math.min(remembered.top, Math.max(0, el.scrollHeight - el.clientHeight));
      session.streamScrollTop = el.scrollTop;
      afterNextPaint(() => {
        restoring = false;
      });
    });
  },
);

function labelOf(t: Turn): string {
  return turnLabel(t.index, session.turnRunning && t.index === turns.value.length);
}

function formatTime(iso?: string): string {
  if (!iso) return "──";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "──";
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function firstAssistantIdx(t: Turn): number {
  return t.items.findIndex((m) => m.role === "assistant");
}

/**
 * 整体状态什么时候需要出现：
 * - 正在生成时，流式正文本身就是进度，不再重复说一遍；
 * - 等待响应 / 正在使用工具 / 等待确认 / 正在处理独立任务时，说一句就够了。
 */
const showGlobalStatus = computed(
  () => session.activity !== "idle" && session.activity !== "generating",
);
/** 只有「还在等」才播三圆点；其它状态是安静的说明文字 */
const showWaitingDots = computed(
  () => session.activity === "waiting" || session.activity === "notify",
);
/**
 * 全局只表达「整体情况」的一句话（spec 第 36~38 条）：
 * 正在处理 / 正在使用工具 / 等待确认 / 正在处理独立任务 / 正在整理独立任务的结果。
 *
 * 绝不出现 `TOOL_RUNNING`、`MODEL_WAIT`、`SUBAGENT_PENDING` 这类内部事件名，
 * 也不把每种内部状态同时堆在页面上 —— 细节各自留在对应卡片里。
 */
const ACTIVITY_LABELS: Record<string, string> = {
  waiting: "正在处理",
  generating: "正在生成…",
  tool: "正在使用工具",
  approval: "等待你确认",
  subagent: "正在处理独立任务",
  notify: "正在整理独立任务的结果",
};
const phaseLabel = computed(() => {
  if (!session.turnRunning && session.activity === "idle") return "";
  return ACTIVITY_LABELS[session.activity] ?? "";
});
</script>

<template>
  <!-- 可聚焦滚动区域：键盘用户也能滚动消息，并且键盘滚动同样会停止自动跟随 -->
  <div
    ref="containerRef"
    class="stream"
    tabindex="0"
    aria-label="对话消息"
    @scroll.passive="onScroll"
  >
    <div
      ref="spacerRef"
      class="spacer"
      :style="{ height: virtualizer.getTotalSize() + 'px', position: 'relative' }"
    >
      <div
        v-for="item in virtualizer.getVirtualItems()"
        :key="String(item.key)"
        :data-index="item.index"
        :ref="measureItem"
        class="virtual-item"
        :style="{
          position: 'absolute',
          top: 0,
          left: 0,
          width: '100%',
          transform: `translateY(${item.start}px)`,
        }"
      >
        <div class="turn" :data-turn="turns[item.index].id">
          <div class="turn-meta">
            <span class="who">{{ labelOf(turns[item.index]) }}</span>
            <span class="bar"></span>
            <span class="ts">{{ formatTime(turns[item.index].startedAt) }}</span>
          </div>
          <MessageItem
            v-for="(m, i) in turns[item.index].items"
            :key="m.id"
            :message="m"
            :show-topic="i === firstAssistantIdx(turns[item.index])"
          />
        </div>
      </div>
    </div>
    <div v-if="showGlobalStatus" class="typing" role="status" aria-live="polite">
      <div class="typing-bubble">
        <template v-if="showWaitingDots">
          <span class="dot"></span><span class="dot"></span><span class="dot"></span>
        </template>
        <span class="phase mono">{{ phaseLabel }}</span>
      </div>
    </div>
    <!-- 回到最新：只有用户主动点才滚动，且用可被滚轮/触屏中断的短滚动 -->
    <button
      v-if="!followBottom && messages.length"
      class="back-latest"
      type="button"
      @click="backToLatest"
    >
      <span class="arrow">↓</span>
      回到最新消息<span v-if="unseen > 0" class="count mono qio-state info">{{ unseen }}</span>
    </button>
    <ContinueBar />
    <QueueChip />
    <div v-if="!messages.length" class="empty">
      <div class="greet serif">今天想聊点什么？</div>
      <div class="sub mono">你的星球在右下角等待 · 点击悬浮球查看话题大陆</div>
      <div class="chip"><span class="pd"></span>打开话题星球</div>
    </div>
  </div>
</template>

<style scoped>
.stream {
  flex: 1;
  overflow-y: auto;
  padding: 34px 24px 20px;
  scrollbar-width: thin;
  background: var(--bg-base);
}
/* 键盘聚焦时才显示焦点环：滚动区域平时不抢视觉焦点 */
.stream:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: -2px;
}
/* 话题星球停靠球是 fixed 悬浮元素（96px）；窄窗口下会压住靠右的气泡与时间戳。
   这里为它留出通道，避免遮挡正文。宽屏下气泡自身有 max-width，不受影响。 */
@media (max-width: 1400px) {
  .stream {
    padding-right: 132px;
  }
}
.spacer {
  width: 100%;
}
.turn {
  /* 用户消息与回答共用同一内容列（居中），不各自贴窗口两端 */
  max-width: 860px;
  margin: 0 auto 26px;
}
.turn-meta {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 8px;
  font-family: var(--mono);
  font-size: 10.5px;
  color: var(--text-muted);
  letter-spacing: 0.05em;
  /* 轮次索引属于次要信息：默认淡，hover 才完全显形（减少仪表盘感） */
  opacity: 0.55;
  transition: opacity var(--dur-fast) var(--ease);
}
.turn:hover .turn-meta {
  opacity: 1;
}
.turn-meta .who {
  color: var(--text-secondary);
}
.turn-meta .bar {
  flex: 1;
  height: 1px;
  background: var(--border-subtle);
}
/* ---- 空状态 ---- */
.empty {
  height: 100%;
  display: flex;
  flex-direction: column;
  justify-content: center;
  align-items: center;
  gap: 14px;
  text-align: center;
}
.empty .greet {
  font-size: 34px;
  font-weight: 600;
  color: var(--text-strong);
}
.empty .sub {
  font-size: 12px;
  color: var(--text-muted);
  letter-spacing: 0.06em;
}
.empty .chip {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  margin-top: 6px;
  padding: 7px 14px;
  border: 1px solid var(--border-subtle);
  border-radius: 20px;
  font-size: 12px;
  color: var(--text-secondary);
}
.empty .chip .pd {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--accent);
  box-shadow: 0 0 8px var(--accent);
}
/* ---- 打字指示器：三圆点来回跳动 ---- */
.typing {
  display: flex;
  justify-content: flex-start;
  margin-top: 4px;
}
.typing-bubble {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 12px 16px;
  border-radius: 14px 14px 14px 4px;
  background: var(--bg-elevated);
  border: 1px solid var(--border-subtle);
}
.typing-bubble .dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--text-secondary);
  animation: typing-bounce 1.2s infinite ease-in-out;
}
.typing-bubble .dot:nth-child(2) {
  animation-delay: 0.15s;
}
.typing-bubble .dot:nth-child(3) {
  animation-delay: 0.3s;
}
.typing-bubble .phase {
  margin-left: 6px;
  font-size: 11px;
  color: var(--text-muted);
  letter-spacing: 0.04em;
}
/* ---- 回到最新消息 ---- */
.back-latest {
  position: sticky;
  bottom: 8px;
  z-index: 6;
  margin: 6px auto 0;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 14px;
  border-radius: var(--r-pill);
  border: 1px solid var(--border-strong);
  background: var(--bg-elevated);
  color: var(--text-secondary);
  font-family: var(--sans);
  font-size: 12px;
  cursor: pointer;
  box-shadow: var(--shadow-1);
  transition: border-color var(--dur-fast) var(--ease), color var(--dur-fast) var(--ease),
    transform var(--dur-press) var(--ease-out);
}
.back-latest:hover {
  border-color: var(--accent);
  color: var(--accent);
}
.back-latest:active {
  transform: translateY(var(--press-shift));
}
.back-latest .count {
  /* 计数走统一状态徽章原语（info 语义 = 「有新内容」）；这里只保留布局相关属性 */
  min-width: 18px;
  text-align: center;
  font-size: 10.5px;
}
@keyframes typing-bounce {
  0%, 60%, 100% {
    transform: translateY(0);
    opacity: 0.4;
  }
  30% {
    transform: translateY(-4px);
    opacity: 1;
  }
}
</style>
