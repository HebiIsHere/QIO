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

interface Turn {
  id: string;
  index: number;
  startedAt: string;
  items: StreamMessage[];
}

const session = useSessionStore();
const containerRef = ref<HTMLDivElement | null>(null);
const spacerRef = ref<HTMLDivElement | null>(null);
const followBottom = ref(true);

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
  // 只按「距底部距离」判定是否跟随：用户向上滚 → 立刻停止跟随，不再强行拉回
  followBottom.value = el.scrollHeight - el.scrollTop - el.clientHeight <= NEAR_BOTTOM_PX;
}

function scrollToBottom() {
  const el = containerRef.value;
  if (!el) return;
  el.scrollTop = Math.max(0, el.scrollHeight - el.clientHeight);
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
  composerObserver?.disconnect();
  composerObserver = null;
  streamObserver?.disconnect();
  streamObserver = null;
});

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
  async () => {
    if (followBottom.value) {
      await nextTick();
      scrollToBottom();
    }
  },
);

watch(
  () => session.turnRunning,
  async (running) => {
    // 用户刚发起一轮：回到跟随模式（主动发送意味着想看结果）
    if (running) {
      followBottom.value = true;
      await nextTick();
      scrollToBottom();
    }
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

/** 打字指示器：turn 运行中且当前轮次尚无助手消息时显示三圆点 */
const showTyping = computed(() => {
  if (!session.turnRunning) return false;
  const last = turns.value[turns.value.length - 1];
  if (!last) return false;
  return !last.items.some((m) => m.role === "assistant");
});
</script>

<template>
  <div ref="containerRef" class="stream" @scroll.passive="onScroll">
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
    <div v-if="showTyping" class="typing" role="status" aria-label="QIO 正在回复">
      <div class="typing-bubble">
        <span class="dot"></span><span class="dot"></span><span class="dot"></span>
      </div>
    </div>
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
  padding: 34px 44px 20px;
  scrollbar-width: thin;
  background: var(--bg-base);
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
  margin-bottom: 26px;
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
