<script setup lang="ts">
/**
 * 消息流：@tanstack/vue-virtual 虚拟滚动 + 底部跟随。
 * 按「用户消息开新轮次」把消息分组为 turn，虚拟化单位是轮次块；
 * 每轮渲染 TURN 分隔头（等宽）+ 消息项（MessageItem）。
 */
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from "vue";
import { useVirtualizer } from "@tanstack/vue-virtual";
import { useSessionStore } from "../stores/session";
import { useApprovalsStore } from "../stores/approvals";
import MessageItem from "./MessageItem.vue";
import TurnProcess from "./TurnProcess.vue";
import ContinueBar from "./ContinueBar.vue";
import QueueChip from "./QueueChip.vue";
import { turnLabel } from "../utils/turnLabel";
import { prefersReducedMotion } from "../utils/motion";
import {
  splitTurnItems,
  type StreamMessage,
  type TurnFacts,
  type TurnItemsView,
  type TurnStage,
} from "../stores/session";
import { useLatestButtonAnchor } from "../composables/useLatestButtonAnchor";
import { subscribeComposerMetrics } from "../composables/composerMetrics";

/** 对话内容列宽：用户消息与回答共用一个居中列，不分别贴窗口两端 */
const CONTENT_MAX_PX = 860;

interface Turn {
  id: string;
  index: number;
  startedAt: string;
  items: StreamMessage[];
  /** 一轮的消息分到哪一块：过程区 / 正文区 / 其余卡片（见 splitTurnItems） */
  parts: TurnItemsView;
  /** 这一轮的 turn_id（旧历史记录可能没有） */
  turnId: string;
  /** 本轮第一条正式回答（用于显示话题名） */
  firstAssistantId: string | null;
  /** 这一轮正在运行（只有最后一轮 + turnRunning 同时成立） */
  running: boolean;
  /** 已受理、还没开始执行 */
  queued: boolean;
  stages: TurnStage[];
  facts: TurnFacts | null;
  /** 是否渲染过程区：有过程内容 / 在跑 / 有权威事实 */
  showProcess: boolean;
}

const session = useSessionStore();
const approvals = useApprovalsStore();
const containerRef = ref<HTMLDivElement | null>(null);
/** 卸载阶段模板 ref 会被置空，这里留一份引用，保证离开时仍能读到滚动位置 */
let lastContainer: HTMLDivElement | null = null;
const spacerRef = ref<HTMLDivElement | null>(null);
const followBottom = ref(true);
/** 上翻阅读期间新到的消息数（「回到最新消息」入口上的提示数字） */
const unseen = ref(0);
/** 程序化滚动（回到最新/贴底）的 rAF 句柄：用户一操作就中断 */
let programmaticRaf = 0;
/**
 * 异步滚动操作的**代次**：历史锚定、阅读位置恢复、输入区挂载重试都记自己的代次，
 * 提交前比一次。切话题、组件卸载后，旧回调一律不再改滚动位置（见修复提示词 §6）。
 */
let streamGen = 0;
/** 历史锚定循环 / 阅读位置恢复的 rAF 句柄：卸载时要能取消 */
let anchorRaf = 0;
let restoreRaf = 0;
/** 内容增高的跟随检查合并到一帧（观察器可能连续触发多次） */
let followRaf = 0;
/** 正在恢复阅读位置（此时到来的 scroll 事件是程序写入造成的，不算用户意图） */
let restoring = false;
/** 本机发送前的阅读状态：请求被后端拒绝时要放回原位（没发出去的消息不该留下后遗症） */
let preSend: { follow: boolean; top: number } | null = null;
/** 上翻阅读中发送 → 若被拒绝则回到原位；用户中途自己滚过就不再强行放回 */
let pendingRestoreOnReject = false;

/** 距底部多少像素内算「跟随中」 */
const NEAR_BOTTOM_PX = 120;

const messages = computed(() => session.messages);

/**
 * 「回到最新消息」按钮的位置：由输入框与底部内容块的**真实边界**算出来
 * （见 useLatestButtonAnchor）：下缘距输入框上缘 8–12px，水平对齐对话内容列中心。
 * 锚点元素本身参与测量（CSS zoom 下的局部→视口换算要用它自己的比例）。
 */
const latestAnchorRef = ref<HTMLElement | null>(null);
const {
  bottomPx: latestBottomPx,
  centerX: latestCenterX,
  remeasure: remeasureLatest,
} = useLatestButtonAnchor(latestAnchorRef);
// 按钮出现/消失时重新量一次：它自己渲染出来之后才拿得到「锚点自身的缩放比例」
watch(
  () => !followBottom.value && messages.value.length > 0,
  async (visible) => {
    if (!visible) return;
    await nextTick();
    remeasureLatest();
  },
);

const turns = computed<Turn[]>(() => {
  const out: Turn[] = [];
  let cur: Turn | null = null;
  let n = 0;
  for (const m of messages.value) {
    if (!cur || m.role === "user" || m.role === "system") {
      n += 1;
      cur = {
        id: `turn_${n}`,
        index: n,
        startedAt: m.createdAt,
        items: [],
        parts: { user: [], process: [], answers: [], other: [] },
        turnId: "",
        firstAssistantId: null,
        running: false,
        queued: false,
        stages: [],
        facts: null,
        showProcess: false,
      };
      out.push(cur);
    }
    cur.items.push(m);
    // 话题名挂在第一条**正式回答**上（中间话不再单独成气泡，也不该抢这一行）
    if (cur.firstAssistantId === null && m.role === "assistant" && !m.interim) {
      cur.firstAssistantId = m.id;
    }
    if (!cur.turnId && m.turnId) cur.turnId = m.turnId;
  }
  const lastIndex = out.length;
  for (const turn of out) {
    turn.parts = splitTurnItems(turn.items);
    turn.queued = turn.items.some((m) => m.role === "user" && m.queued === true);
    // 主对话同一时刻只有一轮在跑：只有最后一轮可能是「正在运行」
    turn.running = session.turnRunning && turn.index === lastIndex && !turn.queued;
    turn.stages = session.stagesFor(turn.turnId);
    turn.facts = session.factsFor(turn.turnId);
    turn.showProcess =
      turn.parts.process.length > 0 ||
      turn.running ||
      turn.queued ||
      Boolean(turn.turnId && turn.facts);
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
  // 读到头了：再往前要更早的历史（渐进式加载，首屏不再读全量）
  if (el.scrollTop <= NEAR_TOP_PX) void loadOlderHistoryVue();
}

/** 距顶部多少像素内算「读到头了」 */
const NEAR_TOP_PX = 80;

/** 正在加载更早历史时做滚动锚定：不能把用户正在读的那一行甩走 */
let anchoringHeight = 0;

/**
 * 加载更早的一页，并保持视觉位置不动。
 *
 * 旧消息插在列表顶部，虚拟列表的总高度会先按估算值、再按实测值变化，
 * 所以不能只在 nextTick 里补一次差值：每帧把「高度增量」增量式地补进 scrollTop，
 * 直到高度稳定。用户读到哪里就还在哪里 —— 不丢位置、不跳。
 */
async function loadOlderHistoryVue() {
  const el = containerRef.value;
  if (!el || !session.historyHasMore || session.historyOlderLoading) return;
  // 这次锚定属于哪一代：切话题或卸载后，旧的一帧不得再动滚动位置
  const gen = streamGen;
  anchoringHeight = el.scrollHeight;
  const loaded = await session.loadOlderHistory();
  if (!loaded || gen !== streamGen) return;
  await nextTick();
  if (gen !== streamGen) return;
  restoring = true;
  let frames = 0;
  const anchor = () => {
    anchorRaf = 0;
    // 代次/归属校验：用户已经切了话题或组件已卸载 → 这次锚定作废（但要把 restoring 交还）
    if (gen !== streamGen) {
      restoring = false;
      return;
    }
    const node = containerRef.value;
    if (node) {
      const height = node.scrollHeight;
      const delta = height - anchoringHeight;
      if (delta !== 0) {
        node.scrollTop += delta;
        anchoringHeight = height;
        session.streamScrollTop = node.scrollTop;
      }
    }
    frames += 1;
    if (frames < 8) anchorRaf = requestAnimationFrame(anchor);
    else restoring = false;
  };
  anchorRaf = requestAnimationFrame(anchor);
}

/**
 * 切话题 = 上一代异步操作的归属全部失效：锚定、阅读位置恢复、输入区挂载重试
 * 都不再允许改动新话题的滚动位置（见修复提示词 §4/§6）。
 */
watch(
  () => session.currentTopicId,
  () => {
    streamGen += 1;
    restoring = false;
    if (anchorRaf) cancelAnimationFrame(anchorRaf);
    anchorRaf = 0;
    if (restoreRaf) cancelAnimationFrame(restoreRaf);
    restoreRaf = 0;
  },
);

/** 用户一动滚轮/触屏：立刻中断程序化滚动（自动滚动不能和用户抢） */
function onUserInput() {
  cancelProgrammaticScroll();
  // 用户自己接管了滚动：发送失败时不再把位置放回发送前
  pendingRestoreOnReject = false;
  // 「恢复上次阅读位置」属于会跳位置的旧回调：用户一接管就作废，不许再改 scrollTop。
  // （历史插入锚定不在这里取消：它做的是「内容插在上方时补回高度增量」，作用是让用户
  //   停在原来那一行，不是把用户挪走；见修复提示词 §6 的区分。）
  if (restoreRaf) {
    cancelAnimationFrame(restoreRaf);
    restoreRaf = 0;
  }
  restoring = false;
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
 *  滚动到底时最新消息恰好停在浮动气泡上方（消息少时无额外留白）。
 *  尺寸走共享量测（composerMetrics）：以前这里、底部让位、按钮锚定各量一遍同一个输入框。 */
let unsubscribeComposer: (() => void) | null = null;
/** 内容高度观察器：assistant 流式正文变高（不换条）时也要跟随底部 */
let streamObserver: ResizeObserver | null = null;
function applyComposerPad(height: number) {
  const el = containerRef.value;
  if (!el) return;
  el.style.paddingBottom = `${Math.round(height) + 16}px`;
}
onMounted(() => {
  lastContainer = containerRef.value;
  // 触屏与滚轮属于用户意图：中断自动滚动，并把跟随状态交回用户
  containerRef.value?.addEventListener("wheel", onUserInput, { passive: true });
  containerRef.value?.addEventListener("touchstart", onUserInput, { passive: true });
  containerRef.value?.addEventListener("keydown", onKeyScroll);
  restoreScrollPosition();
  // 内容增高（同一条流式消息变长 / markdown 布局变化）时，若仍在跟随就贴底。
  // 观察器通知合并到一帧：连续多次通知只做一次测量与一次贴底（避免同帧反复写滚动位置）。
  if (typeof ResizeObserver !== "undefined") {
    streamObserver = new ResizeObserver(() => {
      if (followRaf) return;
      followRaf = requestAnimationFrame(() => {
        followRaf = 0;
        if (followBottom.value) scrollToBottom();
      });
    });
    if (spacerRef.value) streamObserver.observe(spacerRef.value);
  }
  if (typeof ResizeObserver === "undefined") {
    unsubscribeComposer = subscribeComposerMetrics(({ height }) => applyComposerPad(height));
    return;
  }
  // 输入区尺寸由共享量测负责观察（含「输入框比本视图晚挂载」的有限次重试）
  unsubscribeComposer = subscribeComposerMetrics(({ height }) => applyComposerPad(height));
});
onUnmounted(() => {
  // 卸载即换代：所有在飞的锚定/恢复回调作废，不再改动任何滚动位置
  streamGen += 1;
  restoring = false;
  containerRef.value?.removeEventListener("wheel", onUserInput);
  containerRef.value?.removeEventListener("touchstart", onUserInput);
  containerRef.value?.removeEventListener("keydown", onKeyScroll);
  lastContainer?.removeEventListener("wheel", onUserInput);
  lastContainer?.removeEventListener("touchstart", onUserInput);
  lastContainer?.removeEventListener("keydown", onKeyScroll);
  cancelProgrammaticScroll();
  if (anchorRaf) cancelAnimationFrame(anchorRaf);
  anchorRaf = 0;
  if (restoreRaf) cancelAnimationFrame(restoreRaf);
  restoreRaf = 0;
  if (followRaf) cancelAnimationFrame(followRaf);
  followRaf = 0;
  saveScrollPosition();
  unsubscribeComposer?.();
  unsubscribeComposer = null;
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
  const gen = streamGen;
  // 虚拟列表首次布局可能晚于 mount：容器还不可滚动就下一帧再试（有上限，不会无限重试）
  let tries = 0;
  const attempt = () => {
    restoreRaf = 0;
    // 归属校验：切了话题（换代）或组件已卸载 → 这次恢复作废，不许改动新视图的位置
    if (gen !== streamGen) {
      restoring = false;
      return;
    }
    const el = containerRef.value;
    if (!el) {
      restoring = false;
      return;
    }
    const max = el.scrollHeight - el.clientHeight;
    if (max <= 0 && tries < 10) {
      tries += 1;
      restoreRaf = afterNextPaint(attempt);
      return;
    }
    el.scrollTop = Math.min(target, Math.max(0, max));
    restoring = false;
  };
  restoreRaf = afterNextPaint(attempt);
}

/** 等下一帧（无 rAF 的环境退回 setTimeout，保证恢复逻辑不会因为环境而中断）；返回句柄供卸载时取消 */
function afterNextPaint(cb: () => void): number {
  if (typeof requestAnimationFrame === "function") return requestAnimationFrame(cb);
  return setTimeout(cb, 0) as unknown as number;
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
    const first = session.messages[0];
    return [
      session.messages.length,
      last?.content.length ?? 0,
      session.turnRunning,
      first?.id ?? "",
    ] as const;
  },
  async (now, prev) => {
    const [count, , , firstId] = now;
    const [prevCount, , , prevFirstId] = prev ?? now;
    // 更早的历史被插到列表顶部（首条变了、总数变多）：这不是「新消息」，
    // 既不该计入未读数，也不该触发任何滚动 —— 用户的位置由锚定逻辑负责保持。
    if (prevFirstId && firstId !== prevFirstId && count > prevCount) return;
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

/**
 * 整体状态什么时候需要出现：
 * - 正在生成时，流式正文本身就是进度，不再重复说一遍；
 * - **正在使用工具时不出现**：过程由模型自己写的执行叙事表达，工具卡自己说状态
 *   （机械的"正在使用工具"是待替换的旧提示，见 spec 2026-09-22）；
 * - 等待响应 / 等待确认 / 正在处理独立任务时，说一句就够了。
 */
/**
 * 当前轮是否已经有过程区，并且这一轮的运行状态由过程区状态行表达。
 *
 * 契约 §1.5 的归并要求：有过程区时，全局状态条不再显示同一条状态 ——
 * 否则「已受理」与「正在处理」会同时出现在页面上。
 */
const processRegionSpeaks = computed(() => {
  const last = turns.value[turns.value.length - 1];
  return Boolean(last?.showProcess && last.running);
});

const showGlobalStatus = computed(() => {
  // 「正在使用工具」「正在生成」本来就不显示（过程由过程区 / 工具卡表达）
  if (
    session.activity === "idle" ||
    session.activity === "generating" ||
    session.activity === "tool"
  ) {
    return false;
  }
  // 当前轮的过程区已经在说「已受理」：全局条不再重复「正在处理」
  if (processRegionSpeaks.value && session.activity === "waiting") return false;
  /**
   * 内联审批卡已经在过程区里承担了「等待确认」这件事（含按钮），
   * 全局状态条不再重复说一遍；没有内联卡（非当前轮 / 恢复路径）时照旧显示。
   */
  if (session.activity === "approval" && approvals.inlineClaimed) return false;
  // 其余仍然显示：排队等待 / 独立任务 / 系统通知 / 没有过程区的旧记录
  return true;
});
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
          <!-- 用户消息：这一轮的起点 -->
          <MessageItem
            v-for="m in turns[item.index].parts.user"
            :key="m.id"
            :message="m"
          />
          <!--
            一轮 = **一个过程区域**（契约 §1.5）：中间话、工具卡、legacy 叙事行、
            内联审批、耗时入口都在里面，同一内容只出现一次。
          -->
          <TurnProcess
            v-if="turns[item.index].showProcess"
            :turn-id="turns[item.index].turnId"
            :items="turns[item.index].parts.process"
            :stages="turns[item.index].stages"
            :facts="turns[item.index].facts"
            :running="turns[item.index].running"
            :queued="turns[item.index].queued"
          />
          <!-- 独立任务 / 工具创建卡：生命周期比一轮的过程说明长，留在过程区外 -->
          <MessageItem
            v-for="m in turns[item.index].parts.other"
            :key="m.id"
            :message="m"
          />
          <!-- 正式回答（正文区）：流式生成时用增量渲染 -->
          <MessageItem
            v-for="m in turns[item.index].parts.answers"
            :key="m.id"
            :message="m"
            :show-topic="m.id === turns[item.index].firstAssistantId"
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
    <!--
      回到最新：只有用户主动点才滚动，且用可被滚轮/触屏中断的短滚动。
      位置由输入框与底部内容块的**真实边界**算出来（useLatestButtonAnchor）：
      外层 fixed 锚点把按钮放在「输入框上缘往上 8–12px」，并水平对齐对话内容列中心。
      按钮因此不在滚动流里，不会再被「消息区底边」和空着的底部内容块一起抬高。
      （曾经用 Teleport 投到对话视图的浮层：Teleport 在挂载时解析目标，而那时父视图的根
       还没插进文档，目标解析成 null 且因为 disabled 连警告都没有 —— 按钮永远不出现。
       改用固定定位，位置一样由真实元素边界算，且没有挂载时序问题。）
    -->
    <div
      v-if="!followBottom && messages.length"
      ref="latestAnchorRef"
      class="latest-anchor"
      :style="{
        ...(latestBottomPx === null ? {} : { bottom: `${latestBottomPx}px` }),
        ...(latestCenterX === null ? {} : { left: `${latestCenterX}px` }),
      }"
    >
      <button class="back-latest" type="button" @click="backToLatest">
        <span class="arrow">↓</span>
        回到最新消息<span v-if="unseen > 0" class="count mono qio-state info">{{ unseen }}</span>
      </button>
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
  /* 左右留白取「正文列」令牌：与输入气泡同一条列。
     ≤1400px 时右侧令牌会变成 132px —— 那是给常驻星球入口球留的通道，
     两个组件读同一个值，谁也不能单独挪（见 tokens.css 的 --column-inset-*）。 */
  padding: 34px var(--column-inset-right) 20px var(--column-inset-left);
  scrollbar-width: thin;
  background: var(--bg-base);
}
/* 键盘聚焦时才显示焦点环：滚动区域平时不抢视觉焦点 */
.stream:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: -2px;
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
/*
 * 外层锚点：fixed 定位，位置来自真实边界测量（bottom = 输入框上缘往上 8–12px；
 * left = 对话内容列中心）。只有它做位移变换，按钮自己仍保留按压缩放，
 * 两者不互相覆盖。
 */
.latest-anchor {
  position: fixed;
  z-index: 7;
  transform: translateX(-50%);
  pointer-events: none;
}
.latest-anchor > .back-latest {
  pointer-events: auto;
}
.back-latest {
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
