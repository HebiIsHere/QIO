<script setup lang="ts">
/**
 * 消息流：@tanstack/vue-virtual 虚拟滚动 + 底部跟随 + 阅读位置恢复。
 *
 * 阅读位置（修复提示词 §二）：
 * - 保存「话题 + 轮次稳定身份 + 轮内锚点 / 文本片段 + 视口相对偏移」，旧像素只做回退；
 * - 恢复先按稳定身份定位，再用真实布局逐帧校正，直到同一内容处于相近视口位置；
 * - 图片 / Markdown / 字体 / 窗口宽度变化只做**有界**后续校正，不固定等若干毫秒；
 * - 用户滚动或键盘导航立刻接管，恢复与校正全部作废；
 * - 切话题、组件卸载都会换代，旧异步回调不得改动新话题。
 *
 * 虚拟化单位是「轮次块」（用户消息开新一轮），每轮渲染 TURN 分隔头 + 消息项。
 */
import { computed, nextTick, onBeforeUnmount, onMounted, onUnmounted, ref, watch } from "vue";
import { useVirtualizer } from "@tanstack/vue-virtual";
import { useSessionStore } from "../stores/session";
import { useApprovalsStore } from "../stores/approvals";
import { useChatViewportStore, type SavedReadingPosition } from "../stores/chatViewport";
import MessageItem from "./MessageItem.vue";
import TurnProcess from "./TurnProcess.vue";
import ContinueBar from "./ContinueBar.vue";
import { turnLabel } from "../utils/turnLabel";
import { prefersReducedMotion } from "../utils/motion";
import { contentViewport } from "../utils/windowFrame";
import { groupTurns, visibleIntentExecutionMessages, type StreamTurn } from "../stores/session";
import {
  anchorOffsetOf,
  captureReadingAnchor,
  findAnchorElement,
  findSnippetElement,
  turnKeyOf,
} from "../utils/readingAnchor";
import { subscribeComposerMetrics } from "../composables/composerMetrics";
import { vQioScrollbar } from "../composables/useQioScrollbars";

/**
 * #16：互动页专属空态（只影响互动模式的一句引导）。
 * 其它模式不传这个 prop，保持自己的空态（含话题星球入口）原样。
 */
const props = withDefaults(defineProps<{ emptyVariant?: "default" | "interactive" }>(), {
  emptyVariant: "default",
});

const session = useSessionStore();
const approvals = useApprovalsStore();
const viewport = useChatViewportStore();
const containerRef = ref<HTMLDivElement | null>(null);
/** 卸载阶段模板 ref 会被置空，这里留一份引用，保证离开时仍能读到滚动位置 */
let lastContainer: HTMLDivElement | null = null;
const spacerRef = ref<HTMLDivElement | null>(null);

/**
 * 跟随 / 未读：本地 ref 与共享 store 同步。
 * 入口行（ComposerEntries）与消息流读同一份事实，不各自维护一套。
 */
const followBottom = ref(viewport.following);
const unseen = ref(viewport.unseen);
watch(
  [followBottom, unseen],
  ([following, count]) => {
    viewport.setFollowing(following);
    if (following) viewport.setUnseen(0);
    else viewport.setUnseen(count);
    session.streamFollowing = following;
  },
  { immediate: true },
);

/** 程序化滚动（回到最新 / 贴底）的 rAF 句柄：用户一操作就中断 */
let programmaticRaf = 0;
/** 平滑「回到最新」进行中：期间不按 scroll 事件改写跟随状态 */
let backToLatestActive = false;
/**
 * 异步滚动操作的**代次**：历史锚定、阅读位置恢复、输入区挂载重试都记自己的代次，
 * 提交前比一次。切话题、组件卸载后，旧回调一律不再改滚动位置。
 */
let streamGen = 0;
/** 历史锚定循环 / 阅读位置恢复的 rAF 句柄：卸载时要能取消 */
let anchorRaf = 0;
let restoreRaf = 0;
/** 内容增高的跟随检查合并到一帧（观察器可能连续触发多次） */
let followRaf = 0;
/** 正在恢复阅读位置 / 程序写入滚动位置：此时的 scroll 事件不算用户意图 */
let restoring = false;
/** 恢复是否还在等历史到位（有界，不会无限重试） */
let initialRestorePending = false;
let initialRestoreDone = false;
/** 有界校正的次数：不无限重试、也不固定等若干毫秒 */
let restoreCorrections = 0;
/**
 * 阅读恢复任务的票据（问题 2）：用户接管 / 切换话题 / 卸载 / 开始新一次恢复都会 +1。
 *
 * 只停 rAF 或清标志不够 —— 已经发出的历史请求、nextTick、延迟帧在返回后必须重新核对
 * 票据，否则旧任务还会继续补页、按旧目标定位，甚至「找不到位置」时兜底跳到顶部。
 */
let restoreTaskId = 0;
/**
 * 恢复程序正在驱动虚拟列表定位：scrollToIndex 之后虚拟列表还会按实测高度自己补写
 * scrollTop（浏览器会补发 scroll 事件），那不是用户接管。
 * 真实用户滚动一定先经过滚轮 / 触屏 / 指针 / 键盘（onUserInput 已先一步作废恢复）；
 * 拖动 QIO 指示条也会在捕获阶段的 pointerdown 里被拦到。
 */
let restoreVirtualScrolling = false;
/** 恢复成功后的短校正窗口：图片 / Markdown / 字体 / 窗口宽度变化可能继续改变布局 */
let settleSaved: SavedReadingPosition | null = null;
/** 短校正窗口属于哪一次恢复任务 / 哪一代：旧任务不得借它继续改位置 */
let settleTask = 0;
let settleGen = 0;
let settleDeadline = 0;
let settleAttempts = 0;
/** 原阅读位置确实不可用时的一句简短说明（不静默跳到无关位置） */
const restoreNote = ref("");
let restoreNoteTimer: ReturnType<typeof setTimeout> | null = null;
/** 本机发送前的阅读状态：请求被后端拒绝时要放回原位（没发出去的消息不该留下后遗症） */
let preSend: { follow: boolean; top: number } | null = null;
/** 上翻阅读中发送 → 若被拒绝则回到原位；用户中途自己滚过就不再强行放回 */
let pendingRestoreOnReject = false;

/** 距底部多少像素内算「跟随中」 */
const NEAR_BOTTOM_PX = 120;
/** 距顶部多少像素内算「读到头了」 */
const NEAR_TOP_PX = 80;
/** 一次恢复的硬上限（毫秒 / 校正次数）：只做有界校正 */
const RESTORE_MAX_MS = 2600;
const RESTORE_MAX_CORRECTIONS = 60;
/** 贴底恢复的稳定窗口 */
const FOLLOW_SETTLE_MAX_MS = 1600;
/** 恢复成功后的后续校正窗口 */
const SETTLE_MAX_MS = 1800;
const SETTLE_MAX_ATTEMPTS = 8;
const FALLBACK_NOTE = "原来的阅读位置不在当前已加载的记录里，已回到最近可用的位置。";

/**
 * 消息流里真正要渲染的消息。
 *
 * 意图执行记录（R2 契约 §四 / 返修二）：同一 record_key 只渲染**一张**卡 —— 保留消息顺序
 * 最早的那条 head（服务端的分页 head 是「本页最早」，同一记录跨页时可能各带一个 head），
 * 自动重试 / 再次执行产生的其余消息不渲染（每次 attempt 仍在头部记录的详情里可见）。
 * 只做显示过滤，不改 session.messages、不删数据 —— 历史 / 互动对话两个入口共用这一处。
 */
const messages = computed(() => visibleIntentExecutionMessages(session.messages));

/**
 * 按 turn 身份分组（见 stores/session.ts 的 groupTurns）。
 *
 * 排队消息插进数组中间不会改变任何一轮的归属：正在跑的那一轮仍然是 running，
 * 它随后的说明 / 工具 / 回答也仍然回到它自己那一轮。
 */
const turns = computed<StreamTurn[]>(() =>
  groupTurns(messages.value, {
    turnRunning: session.turnRunning,
    activeTurnId: session.activeTurnId,
    stagesFor: (turnId) => session.stagesFor(turnId),
    factsFor: (turnId) => session.factsFor(turnId),
  }),
);

// options 整体作为 computed：count 依赖轮次数变化时自动 setOptions
const virtualizer = useVirtualizer(
  computed(() => ({
    count: turns.value.length,
    getScrollElement: () => containerRef.value,
    estimateSize: () => 120,
    overscan: 6,
  })),
);

/** 保存阅读位置的节流句柄（滚动时每帧最多量一次） */
let captureRaf = 0;
/**
 * 最近一次**程序写入**的 scrollTop。
 * 恢复/校正期间浏览器补发的 scroll 事件与用户滚动都长一样，靠这个值区分：
 * 位置和我们刚写的不一致 = 用户在恢复完成前接管了，恢复立即作废。
 */
let lastProgrammaticTop: number | null = null;

function onScroll() {
  const el = containerRef.value;
  if (!el) return;
  // 恢复 / 校正期间会触发 scroll 事件：那不是用户意图，不能据此改写跟随状态与位置。
  // 但用户可能在恢复完成前自己滚动：位置与最近一次程序写入不一致 = 用户接管，整次恢复作废。
  if (restoring) {
    // 虚拟列表在 scrollToIndex 之后会按实测高度自己继续微调（并补发 scroll 事件）：
    // 那是恢复程序产生的滚动，不能据此取消自己。真实用户滚动有 onUserInput 先兜住。
    if (restoreVirtualScrolling) {
      lastProgrammaticTop = el.scrollTop;
      return;
    }
    const userMoved =
      lastProgrammaticTop === null || Math.abs(el.scrollTop - lastProgrammaticTop) > 2;
    if (!userMoved) return;
    cancelRestore();
  }
  // 平滑回到最新由动画自己负责，中途的 scroll 事件不改写跟随状态
  if (backToLatestActive) return;
  // 只按「距底部距离」判定是否跟随：用户向上滚 → 立刻停止跟随，不再强行拉回
  followBottom.value = el.scrollHeight - el.scrollTop - el.clientHeight <= NEAR_BOTTOM_PX;
  if (followBottom.value) unseen.value = 0;
  session.streamFollowing = followBottom.value;
  session.streamScrollTop = el.scrollTop;
  if (captureRaf) return;
  captureRaf = requestAnimationFrame(() => {
    captureRaf = 0;
    saveReadingPosition(el);
  });
  // 读到头了：再往前要更早的历史（渐进式加载，首屏不再读全量）
  if (el.scrollTop <= NEAR_TOP_PX) void loadOlderHistoryVue();
}

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
  // 前插历史后轮次索引整体后移：虚拟列表按索引缓存的实测高度会错位到别的轮次。
  // 用稳定轮次身份作为 key + 清空测量缓存重测，避免「索引变化造成关联漂移」。
  try {
    virtualizer.value.measure();
  } catch {
    // 旧虚拟列表 API 差异：保留既有锚定逻辑兜底
  }
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
        lastProgrammaticTop = node.scrollTop;
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
 * 都不再允许改动新话题的滚动位置。
 */
watch(
  () => session.currentTopicId,
  (topicId) => {
    streamGen += 1;
    // 旧话题的恢复任务（含已经发出、还没返回的历史请求）整次作废：不许再驱动新话题的位置
    cancelRestore();
    restoreNote.value = "";
    if (anchorRaf) cancelAnimationFrame(anchorRaf);
    anchorRaf = 0;
    // 切话题后默认回到最新位置；旧话题的位置已按滚动实时保存，不会丢
    viewport.setTopic(topicId);
    followBottom.value = true;
    unseen.value = 0;
    void nextTick(() => {
      const el = containerRef.value;
      if (el) {
        el.scrollTop = Math.max(0, el.scrollHeight - el.clientHeight);
        lastProgrammaticTop = el.scrollTop;
        session.streamScrollTop = el.scrollTop;
        saveReadingPosition(el);
      }
    });
  },
);

/** 用户一动滚轮/触屏/指针/键盘：立刻中断程序化滚动，恢复与校正全部作废（用户操作优先） */
function onUserInput() {
  cancelProgrammaticScroll();
  // 用户自己接管了滚动：发送失败时不再把位置放回发送前
  pendingRestoreOnReject = false;
  // 「恢复上次阅读位置」属于会跳位置的旧回调：用户一接管就作废，不许再改 scrollTop。
  // （历史插入锚定不在这里取消：它做的是「内容插在上方时补回高度增量」，作用是让用户
  //   停在原来那一行，不是把用户挪走。）
  cancelRestore();
  settleSaved = null;
  lastProgrammaticTop = null;
  if (backToLatestActive) {
    backToLatestActive = false;
    syncFollowFromScroll();
  }
}

/**
 * 作废整次阅读恢复任务（不动当前滚动位置）。
 *
 * 票据 +1 之后，在飞的历史请求 / nextTick / 延迟帧返回时都会发现自己不再有效：
 * 不再补页、不再按旧目标定位、不再兜底跳转；已经发出的请求仍可完成数据读取。
 */
function cancelRestore() {
  restoreTaskId += 1;
  stopVirtualScrollReconcile();
  initialRestorePending = false;
  initialRestoreDone = true;
  restoring = false;
  if (restoreRaf) cancelAnimationFrame(restoreRaf);
  restoreRaf = 0;
  settleSaved = null;
  lastProgrammaticTop = null;
}

/**
 * 停掉虚拟列表自己的定位补写。
 *
 * scrollToIndex 之后 @tanstack/virtual 会按实测高度继续 reconcile，最多数秒；
 * 那也是恢复流程的延迟回调 —— 用户已经接管时如果不停，它会在旧目标附近继续写
 * scrollTop，把用户刚选的位置拽走。不同版本没有这些运行态字段时静默退化为
 * 只停我们自己的 rAF（不影响恢复正确性，只是少一层保护）。
 */
function stopVirtualScrollReconcile(): void {
  restoreVirtualScrolling = false;
  try {
    const core = virtualizer.value as unknown as {
      scrollState?: unknown;
      rafId?: number | null;
    };
    core.scrollState = null;
    if (typeof core.rafId === "number") {
      cancelAnimationFrame(core.rafId);
      core.rafId = null;
    }
  } catch {
    // 旧虚拟列表版本没有这些字段：忽略
  }
}

/** 当前票据是否仍属于这次恢复（且话题 / 组件代次没有变化） */
function restoreTaskAlive(task: number, gen: number): boolean {
  return task === restoreTaskId && gen === streamGen;
}

/** 恢复目标是否仍属于当前话题（话题刚切换、watcher 还没跑时先按归属拦一次） */
function restoreTopicMatches(saved: SavedReadingPosition): boolean {
  return (saved.topicId ?? null) === (session.currentTopicId ?? null);
}

/** 恢复程序写滚动位置：同时记账，浏览器随后补发的 scroll 事件不会被当成用户接管 */
function writeRestoreScroll(el: HTMLElement, top: number): void {
  el.scrollTop = top;
  lastProgrammaticTop = el.scrollTop;
  session.streamScrollTop = el.scrollTop;
}

/**
 * 键盘滚动（PageUp/PageDown/方向键/Home/End/空格）：同属用户意图。
 * .stream 现在是可聚焦的滚动区域（tabindex=0），否则键盘用户根本滚不动消息列表。
 */
function onKeyScroll(e: KeyboardEvent) {
  const keys = ["PageUp", "PageDown", "ArrowUp", "ArrowDown", "Home", "End", " ", "Spacebar"];
  if (!keys.includes(e.key)) return;
  onUserInput();
}

function syncFollowFromScroll() {
  const el = containerRef.value;
  if (!el) return;
  followBottom.value = el.scrollHeight - el.scrollTop - el.clientHeight <= NEAR_BOTTOM_PX;
  if (followBottom.value) unseen.value = 0;
  session.streamFollowing = followBottom.value;
  session.streamScrollTop = el.scrollTop;
  saveReadingPosition(el);
}

function scrollToBottom() {
  scrollToBottomWith(false);
}

/**
 * 贴底 / 回到最新。
 * smooth 时用 rAF 自己走（不用 scroll-behavior），这样用户一动滚轮或触屏
 * 就能立刻中断自动滚动——浏览器的平滑滚动是中断不掉的。
 */
function scrollToBottomWith(smooth: boolean, done?: () => void) {
  const el = containerRef.value;
  if (!el) {
    done?.();
    return;
  }
  cancelProgrammaticScroll();
  const target = Math.max(0, el.scrollHeight - el.clientHeight);
  const start = el.scrollTop;
  const dist = target - start;
  if (!smooth || prefersReducedMotion() || Math.abs(dist) < 2) {
    el.scrollTop = target;
    lastProgrammaticTop = el.scrollTop;
    session.streamScrollTop = el.scrollTop;
    done?.();
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
    lastProgrammaticTop = el.scrollTop;
    session.streamScrollTop = el.scrollTop;
    if (p < 1) {
      programmaticRaf = requestAnimationFrame(step);
      return;
    }
    programmaticRaf = 0;
    done?.();
  };
  programmaticRaf = requestAnimationFrame(step);
}

function cancelProgrammaticScroll() {
  if (programmaticRaf) cancelAnimationFrame(programmaticRaf);
  programmaticRaf = 0;
}

/**
 * 回到最新：用户明确点击后滚动到最新并恢复跟随。
 * 入口在输入区辅助入口行（ComposerEntries），消息流消费共享 store 的请求序号。
 */
function backToLatest() {
  const el = containerRef.value;
  if (!el) return;
  backToLatestActive = true;
  unseen.value = 0;
  // 主动点击即视为恢复跟随：不依赖浏览器随后补发的 scroll 事件
  followBottom.value = true;
  session.streamFollowing = true;
  scrollToBottomWith(true, () => {
    backToLatestActive = false;
    followBottom.value = true;
    unseen.value = 0;
    session.streamFollowing = true;
    session.streamScrollTop = el.scrollTop;
    saveReadingPosition(el);
  });
}

/** 入口行点击「回到最新」：只改变阅读位置，不取消接续选择、不改消息状态 */
watch(
  () => viewport.backRequest,
  (now, before) => {
    if (now === before) return;
    backToLatest();
  },
);

/** 右下角输入框高度观察：消息流底部滚动缓冲 = 输入框高 + 间距 */
let unsubscribeComposer: (() => void) | null = null;
/** 内容高度观察器：assistant 流式正文变高（不换条）时也要跟随底部 */
let streamObserver: ResizeObserver | null = null;
function composerIsFloating(): boolean {
  // v2 起输入区是对话列里的正常流布局（position: relative）；只有旧式悬浮输入区
  // 才需要消息流补一块底部滚动缓冲。读不到样式时按悬浮处理（保持旧行为）。
  if (typeof document === "undefined" || typeof getComputedStyle !== "function") return true;
  const panel = document.querySelector<HTMLElement>(".composer");
  if (!panel) return true;
  return getComputedStyle(panel).position === "fixed";
}
function applyComposerPad(height: number) {
  const el = containerRef.value;
  if (!el) return;
  el.style.paddingBottom = composerIsFloating() ? `${Math.round(height) + 16}px` : "";
}

/** 内容加载（图片 / 嵌入资源）或窗口尺寸变化：恢复成功后只做有界的后续校正 */
function onContentSettled() {
  if (restoring) return;
  if (followBottom.value) {
    scrollToBottom();
    return;
  }
  maybeSettleCorrect();
}

function onWindowResize() {
  onContentSettled();
}

onMounted(() => {
  lastContainer = containerRef.value;
  // 触屏 / 滚轮 / 指针 / 键盘属于用户意图：中断自动滚动与恢复，把控制权交回用户
  containerRef.value?.addEventListener("wheel", onUserInput, { passive: true });
  containerRef.value?.addEventListener("touchstart", onUserInput, { passive: true });
  // 捕获阶段：拖动 QIO 指示条时 pointerdown 会在滑块上 stopPropagation，冒泡阶段收不到
  containerRef.value?.addEventListener("pointerdown", onUserInput, true);
  containerRef.value?.addEventListener("keydown", onKeyScroll);
  containerRef.value?.addEventListener("load", onContentSettled, true);
  window.addEventListener("resize", onWindowResize);
  beginInitialRestore();
  // 内容增高（同一条流式消息变长 / markdown 布局变化）时，若仍在跟随就贴底。
  // 观察器通知合并到一帧：连续多次通知只做一次测量与一次贴底（避免同帧反复写滚动位置）。
  if (typeof ResizeObserver !== "undefined") {
    streamObserver = new ResizeObserver(() => {
      if (followRaf) return;
      followRaf = requestAnimationFrame(() => {
        followRaf = 0;
        if (restoring) return;
        if (followBottom.value) scrollToBottom();
        else maybeSettleCorrect();
      });
    });
    if (spacerRef.value) streamObserver.observe(spacerRef.value);
  }
  // 输入区尺寸由共享量测负责观察（含「输入框比本视图晚挂载」的有限次重试）
  unsubscribeComposer = subscribeComposerMetrics(({ height }) => applyComposerPad(height));
  // 字体加载会改变行高与换行：恢复成功后做一次有界校正
  if (typeof document !== "undefined" && document.fonts?.ready) {
    void document.fonts.ready.then(() => maybeSettleCorrect());
  }
});

onBeforeUnmount(() => {
  // 离开前页面仍可可靠测量：在这里保存最后一次真实阅读位置（卸载后的零值不采信）。
  // 单测挂载的组件不在 document 里（isConnected=false），但 scrollTop 仍是刚测到的真实值；
  // 真实应用卸载时节点仍在文档中，这条只影响测试装置与极端环境。
  saveReadingPosition(undefined, { allowDetached: true });
});

onUnmounted(() => {
  // 卸载即换代：所有在飞的锚定/恢复回调作废，不再改动任何滚动位置
  streamGen += 1;
  cancelRestore();
  containerRef.value?.removeEventListener("wheel", onUserInput);
  containerRef.value?.removeEventListener("touchstart", onUserInput);
  containerRef.value?.removeEventListener("pointerdown", onUserInput, true);
  containerRef.value?.removeEventListener("keydown", onKeyScroll);
  containerRef.value?.removeEventListener("load", onContentSettled, true);
  lastContainer?.removeEventListener("wheel", onUserInput);
  lastContainer?.removeEventListener("touchstart", onUserInput);
  lastContainer?.removeEventListener("pointerdown", onUserInput, true);
  lastContainer?.removeEventListener("keydown", onKeyScroll);
  lastContainer?.removeEventListener("load", onContentSettled, true);
  window.removeEventListener("resize", onWindowResize);
  cancelProgrammaticScroll();
  if (anchorRaf) cancelAnimationFrame(anchorRaf);
  anchorRaf = 0;
  if (restoreRaf) cancelAnimationFrame(restoreRaf);
  restoreRaf = 0;
  if (followRaf) cancelAnimationFrame(followRaf);
  followRaf = 0;
  if (captureRaf) cancelAnimationFrame(captureRaf);
  captureRaf = 0;
  if (restoreNoteTimer) clearTimeout(restoreNoteTimer);
  restoreNoteTimer = null;
  unsubscribeComposer?.();
  unsubscribeComposer = null;
  streamObserver?.disconnect();
  streamObserver = null;
});

/**
 * 阅读位置：保存「话题 + 轮次稳定身份 + 轮内锚点 + 文本片段 + 视口相对偏移」。
 * 旧像素值只作为回退；页面已脱离文档（读到的 scrollTop 是 0）时不采信。
 */
function saveReadingPosition(el?: HTMLElement | null, options?: { allowDetached?: boolean }) {
  if (restoring) return;
  const node = el ?? containerRef.value ?? lastContainer;
  if (!node) return;
  // 已脱离文档的节点读到的 scrollTop 可能是 0：除卸载钩子显式允许外，一律不采信
  if (!node.isConnected && !options?.allowDetached) return;
  const topicId = session.currentTopicId;
  viewport.setTopic(topicId);
  const capture = captureReadingAnchor(node);
  const list = session.messages;
  const last = list[list.length - 1];
  const base: SavedReadingPosition = {
    topicId,
    turnKey: capture?.turnKey ?? "",
    anchorKey: capture?.anchorKey ?? null,
    anchorOffset: capture?.anchorOffset ?? 0,
    turnOffset: capture?.turnOffset ?? 0,
    textSnippet: capture?.textSnippet ?? null,
    textOffset: capture?.textOffset ?? null,
    scrollTop: node.scrollTop,
    following: followBottom.value,
    messageCount: list.length,
    lastMessageId: last ? last.id : null,
    savedAt: Date.now(),
    viewportWidth: typeof window !== "undefined" ? contentViewport().width : 0,
  };
  session.streamScrollTop = node.scrollTop;
  viewport.savePosition(base);
}

/** 等下一帧（无 rAF 的环境退回 setTimeout，保证恢复逻辑不会因为环境而中断）；返回句柄供卸载时取消 */
function afterNextPaint(cb: () => void): number {
  if (typeof requestAnimationFrame === "function") return requestAnimationFrame(cb);
  return setTimeout(cb, 0) as unknown as number;
}

/** 开始（或继续等待）首次阅读位置恢复 */
function beginInitialRestore() {
  if (initialRestoreDone) return;
  initialRestorePending = true;
  attemptInitialRestore();
}

/**
 * 首次恢复的入口：等历史读取不再处于 loading、且容器已挂载后做一次。
 * 只尝试一次；后续消息变化由 watch 触发继续尝试（有界）。
 */
function attemptInitialRestore() {
  if (!initialRestorePending || initialRestoreDone) return;
  const el = containerRef.value;
  if (!el) return;
  const saved = viewport.getPosition(session.currentTopicId);
  viewport.setTopic(session.currentTopicId);
  // 历史还在读且当前没有可用的消息：等它到位（watch 会再调用），不提前宣告恢复完成
  if (!session.messages.length && session.history.status === "loading") return;
  initialRestorePending = false;
  initialRestoreDone = true;
  followBottom.value = saved ? saved.following : true;
  unseen.value = 0;
  const gen = streamGen;
  // 新一次恢复任务开始：上一次恢复（如果有）的延迟回调一律失效
  const task = ++restoreTaskId;
  restoreVirtualScrolling = false;
  if (!saved || followBottom.value) {
    restoring = true;
    settleFollowing(gen, task);
    return;
  }
  void restoreReadingPosition(saved, gen, task);
}

watch(
  () => [session.history.status, session.messages.length, session.messages[0]?.id ?? ""] as const,
  () => {
    if (initialRestorePending) attemptInitialRestore();
    else if (!initialRestoreDone && !session.messages.length && session.history.status !== "loading") {
      // 真的没有消息：不再等，按跟随底部处理（不伪造阅读位置）
      initialRestoreDone = true;
      followBottom.value = true;
    }
  },
);

/** 离开时在底部：主动贴底并保持跟随，不依赖「之后再来一条消息」 */
function settleFollowing(gen: number, task: number): void {
  const deadline = performance.now() + FOLLOW_SETTLE_MAX_MS;
  let stableFrames = 0;
  const step = () => {
    restoreRaf = 0;
    // 用户接管 / 切话题 / 卸载 / 新任务：旧任务不许再写位置（也不动新任务的 restoring）
    if (!restoreTaskAlive(task, gen)) return;
    const el = containerRef.value;
    if (!el) {
      restoring = false;
      return;
    }
    const max = Math.max(0, el.scrollHeight - el.clientHeight);
    const atBottom = Math.abs(el.scrollTop - max) <= 1;
    if (!atBottom) writeRestoreScroll(el, max);
    else lastProgrammaticTop = el.scrollTop;
    stableFrames = atBottom ? stableFrames + 1 : 0;
    if (stableFrames >= 2 || performance.now() > deadline) {
      restoring = false;
      followBottom.value = true;
      unseen.value = 0;
      session.streamFollowing = true;
      session.streamScrollTop = el.scrollTop;
      saveReadingPosition(el);
      return;
    }
    restoreRaf = afterNextPaint(step);
  };
  restoreRaf = afterNextPaint(step);
}

function findTurnIndex(turnKey: string): number {
  if (!turnKey) return -1;
  return turns.value.findIndex((turn) => turnKeyOf(turn) === turnKey);
}

function findTurnElement(turnKey: string): HTMLElement | null {
  const el = containerRef.value;
  if (!el || !turnKey) return null;
  const nodes = el.querySelectorAll<HTMLElement>("[data-turn-key]");
  for (const node of Array.from(nodes)) {
    if (node.getAttribute("data-turn-key") === turnKey) return node;
  }
  return null;
}

/** 离开期间新增的消息数：以保存时最后一条消息为界（历史前插不算新消息） */
function computeUnseen(saved: SavedReadingPosition): number {
  const list = session.messages;
  if (saved.lastMessageId) {
    const index = list.findIndex((message) => message.id === saved.lastMessageId);
    if (index >= 0) return Math.max(0, list.length - index - 1);
  }
  return Math.max(0, list.length - saved.messageCount);
}

function showRestoreNote(text: string) {
  restoreNote.value = text;
  if (restoreNoteTimer) clearTimeout(restoreNoteTimer);
  restoreNoteTimer = setTimeout(() => {
    restoreNote.value = "";
    restoreNoteTimer = null;
  }, 6000);
}

/**
 * 恢复阅读位置：先按稳定身份定位轮次，再按真实布局校正到「同一内容处于相近视口位置」。
 * 原目标不在当前加载范围时按现有分页机制有界寻找；确实不可用才回退并简短说明。
 */
async function restoreReadingPosition(
  saved: SavedReadingPosition,
  gen: number,
  task: number,
): Promise<void> {
  // 任务已被用户接管 / 切话题 / 卸载作废：一个滚动位置都不许再写
  if (!restoreTaskAlive(task, gen)) return;
  const el = containerRef.value;
  if (!el) {
    restoring = false;
    return;
  }
  restoring = true;
  followBottom.value = false;
  unseen.value = computeUnseen(saved);
  restoreCorrections = 0;

  // 旧记录 / 量不到 DOM：只保留像素回退（不是恢复成功的证明）。
  // 容器可能还没完成首帧布局（可滚动高度为 0）：等最多 3 帧再写，避免把旧像素截成 0。
  if (!saved.turnKey) {
    restorePixelFallback(saved, gen, task, 0);
    return;
  }

  // 1) 有界寻找目标轮次：必要时按现有分页加载更早历史
  let index = findTurnIndex(saved.turnKey);
  let loadTries = 0;
  /** 等「别人正在读的同一页历史」落地的总预算（有界） */
  let waitDeadline = 0;
  while (index < 0 && session.historyHasMore && loadTries < 6) {
    loadTries += 1;
    const heightBefore = el.scrollHeight;
    const wasLoading = session.historyOlderLoading;
    const loaded = await session.loadOlderHistory();
    // 每次异步等待后都要核对：请求可以读完数据，但失效任务不许再补页 / 定位 / 兜底跳转
    if (!restoreTaskAlive(task, gen)) {
      // 数据已经插进当前话题：只补回「上方插入」造成的高度位移，让用户停在刚选的位置，
      // 不因为同一份历史被读回来就把当前话题的视野顶走（不做任何旧目标定位）。
      if (loaded) {
        await nextTick();
        anchorHistoryInsertion(el, heightBefore, gen);
      }
      return;
    }
    if (wasLoading && !loaded) {
      // 「同一页历史正在读」不是「没有更早历史」：连续进出设置时上一个实例的请求
      // 可能还在飞。等它落地再判断（有界），不要急着兜底跳到顶部。
      if (!waitDeadline) waitDeadline = performance.now() + RESTORE_MAX_MS;
      const state = await waitForOlderHistoryIdle(task, gen, waitDeadline);
      if (state === "invalid") return;
      if (state === "idle") {
        await nextTick();
        if (!restoreTaskAlive(task, gen) || !restoreTopicMatches(saved)) return;
        index = findTurnIndex(saved.turnKey);
      }
      // timeout：按原有有界策略继续（store 的防重入保证不会并发打同一页）
      continue;
    }
    await nextTick();
    if (!restoreTaskAlive(task, gen) || !restoreTopicMatches(saved)) return;
    index = findTurnIndex(saved.turnKey);
  }
  if (!restoreTaskAlive(task, gen) || !restoreTopicMatches(saved)) return;
  if (index < 0) {
    // 2) 确实不可用：回退到最近可用内容（最早一页的起点），并简短说明
    followBottom.value = false;
    writeRestoreScroll(el, 0);
    restoring = false;
    saveReadingPosition(el);
    showRestoreNote(FALLBACK_NOTE);
    return;
  }
  // 3) 先按稳定索引定位（虚拟列表用估算尺寸，可能不准），再用真实布局校正
  restoreVirtualScrolling = true;
  try {
    virtualizer.value.scrollToIndex(index, { align: "start" });
    // 虚拟列表随后可能按实测高度自己补写位置：先记成程序化滚动，别被自己的 scroll 事件取消
    lastProgrammaticTop = el.scrollTop;
  } catch {
    // 旧虚拟列表 API 差异：退回按旧像素接近目标
    writeRestoreScroll(
      el,
      Math.min(Math.max(0, saved.scrollTop), Math.max(0, el.scrollHeight - el.clientHeight)),
    );
  }
  await nextTick();
  if (!restoreTaskAlive(task, gen) || !restoreTopicMatches(saved)) return;
  correctRestoredPosition(saved, gen, task, false);
}

/**
 * 等 store 里正在进行的「更早一页」请求落地（有界，不重复发请求）。
 *
 * 连续进出设置时，上一个消息流实例的请求可能仍在飞；它读回的数据同样属于当前话题。
 * 把它当成「忙」而不是「没有更早历史」，避免恢复过早兜底跳转。
 */
async function waitForOlderHistoryIdle(
  task: number,
  gen: number,
  deadline: number,
): Promise<"idle" | "invalid" | "timeout"> {
  while (session.historyOlderLoading) {
    if (!restoreTaskAlive(task, gen)) return "invalid";
    if (performance.now() > deadline) return "timeout";
    await new Promise<void>((resolve) => {
      afterNextPaint(() => resolve());
    });
  }
  return restoreTaskAlive(task, gen) ? "idle" : "invalid";
}

/**
 * 恢复任务失效时更早历史恰好读回来了：把「上方插入」的高度增量补回 scrollTop。
 *
 * 这不是按旧目标恢复位置 —— 只让用户当前读的那一行保持不动（与滚动锚定同一口径）；
 * 数据本身已经由 store 按话题归属落进当前列表，不丢、不串话题。
 */
function anchorHistoryInsertion(el: HTMLElement, heightBefore: number, gen: number): void {
  if (gen !== streamGen) return;
  // 请求发出时容器还没有真实布局（高度 0）：量不到「上方插入了多少」，宁可不补，
  // 也绝不能把用户的位置按一个假增量冲走（真实页面在恢复开始时已有布局）。
  if (heightBefore <= 0) return;
  // 增量式记账：每补一次都把基线更新到当前高度，避免下一帧重复补同一段增量
  let baseline = heightBefore;
  const apply = () => {
    if (gen !== streamGen) return;
    const node = containerRef.value;
    // 只用实例身份判断：单测挂载的节点不在 document 里（isConnected=false），但几何仍是可测的
    if (!node || node !== el) return;
    const height = node.scrollHeight;
    const delta = height - baseline;
    baseline = height;
    if (!delta) return;
    const max = Math.max(0, height - node.clientHeight);
    writeRestoreScroll(node, Math.min(Math.max(0, node.scrollTop + delta), max));
    saveReadingPosition(node);
  };
  apply();
  // 虚拟列表可能按实测高度再量一帧：补一帧即可，不做无界循环
  afterNextPaint(apply);
}

/** 旧像素回退：等容器完成首帧布局（最多 3 帧），再按真实可滚动范围夹取。 */
function restorePixelFallback(
  saved: SavedReadingPosition,
  gen: number,
  task: number,
  attempt: number,
): void {
  restoreRaf = 0;
  if (!restoreTaskAlive(task, gen)) return;
  const el = containerRef.value;
  if (!el) {
    restoring = false;
    return;
  }
  const max = Math.max(0, el.scrollHeight - el.clientHeight);
  if (max <= 0 && attempt < 3) {
    restoreRaf = afterNextPaint(() => restorePixelFallback(saved, gen, task, attempt + 1));
    return;
  }
  if (!restoreTaskAlive(task, gen) || !restoreTopicMatches(saved)) return;
  writeRestoreScroll(el, Math.min(Math.max(0, saved.scrollTop), max));
  restoring = false;
  saveReadingPosition(el);
}

/**
 * 逐帧校正到「同一内容处于相近视口位置」。
 *
 * 成功条件 = 锚定元素（文本片段优先）的视口偏移连续两帧与保存值相差 ≤1px；
 * 不是「列表暂时可滚动」就算成功，也不是固定等若干毫秒。
 */
function correctRestoredPosition(
  saved: SavedReadingPosition,
  gen: number,
  task: number,
  settle: boolean,
): void {
  const deadline = performance.now() + (settle ? 900 : RESTORE_MAX_MS);
  let stableFrames = 0;
  let stuckFrames = 0;
  const finish = () => {
    restoring = false;
    restoreVirtualScrolling = false;
    const el = containerRef.value;
    if (el) {
      session.streamScrollTop = el.scrollTop;
      saveReadingPosition(el);
    }
    if (!settle) {
      // 恢复成功后留一个短校正窗口（图片 / Markdown / 字体 / 宽度变化）
      settleSaved = saved;
      settleTask = task;
      settleGen = gen;
      settleDeadline = performance.now() + SETTLE_MAX_MS;
      settleAttempts = 0;
    }
  };
  const step = () => {
    restoreRaf = 0;
    if (!restoreTaskAlive(task, gen) || !restoreTopicMatches(saved)) return;
    const el = containerRef.value;
    if (!el) {
      restoring = false;
      return;
    }
    restoreCorrections += 1;
    const index = findTurnIndex(saved.turnKey);
    if (index < 0) {
      finish();
      return;
    }
    const turnEl = findTurnElement(saved.turnKey);
    if (!turnEl) {
      try {
        virtualizer.value.scrollToIndex(index, { align: "start" });
      } catch {
        // 忽略：下一帧再看是否渲染出来
      }
      if (restoreCorrections < RESTORE_MAX_CORRECTIONS && performance.now() <= deadline) {
        restoreRaf = afterNextPaint(step);
        return;
      }
      finish();
      return;
    }
    // 文本锚点优先（长回答内部），其次轮内锚点，最后轮次偏移
    let target: number;
    let current: number | null = null;
    const snippetEl = saved.textSnippet ? findSnippetElement(turnEl, saved.textSnippet) : null;
    if (snippetEl) {
      target = saved.textOffset ?? saved.anchorOffset;
      current = anchorOffsetOf(el, snippetEl);
    } else {
      const anchorEl = findAnchorElement(turnEl, saved.anchorKey);
      if (anchorEl) {
        target = saved.anchorOffset;
        current = anchorOffsetOf(el, anchorEl);
      } else {
        target = saved.turnOffset;
        current = anchorOffsetOf(el, turnEl);
      }
    }
    if (current === null) {
      finish();
      return;
    }
    const delta = current - target;
    if (Math.abs(delta) <= 1) {
      stableFrames += 1;
      stuckFrames = 0;
    } else {
      // 改变位置前再核对一次：任务可能已经在上一帧被用户接管
      if (!restoreTaskAlive(task, gen)) return;
      const before = el.scrollTop;
      writeRestoreScroll(el, before + delta);
      const applied = Math.abs(el.scrollTop - before) > 0.5;
      stableFrames = 0;
      // 已经到滚动边界仍无法接近目标：不再空转（可能目标内容已被裁剪/替换）
      stuckFrames = applied ? 0 : stuckFrames + 1;
    }
    const atLimit = performance.now() > deadline || restoreCorrections >= RESTORE_MAX_CORRECTIONS;
    if (stableFrames >= 2 || atLimit || stuckFrames >= 3) {
      finish();
      return;
    }
    restoreRaf = afterNextPaint(step);
  };
  restoring = true;
  restoreVirtualScrolling = true;
  restoreRaf = afterNextPaint(step);
}

/** 恢复成功后的有界后续校正：布局再变化时把同一内容拉回相近视口位置 */
function maybeSettleCorrect() {
  if (restoring || !settleSaved) return;
  if (!restoreTaskAlive(settleTask, settleGen)) {
    settleSaved = null;
    return;
  }
  if (settleAttempts >= SETTLE_MAX_ATTEMPTS || performance.now() > settleDeadline) {
    settleSaved = null;
    return;
  }
  settleAttempts += 1;
  correctRestoredPosition(settleSaved, settleGen, settleTask, true);
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
      lastProgrammaticTop = el.scrollTop;
      session.streamScrollTop = el.scrollTop;
      afterNextPaint(() => {
        restoring = false;
      });
    });
  },
);

function labelOf(t: StreamTurn): string {
  return turnLabel(t.index, t.running);
}

function formatTime(iso?: string): string {
  if (!iso) return "──";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "──";
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

/**
 * 当前轮是否已经有过程区，并且这一轮的运行状态由过程区状态行表达。
 *
 * 契约 §1.5 的归并要求：有过程区时，全局状态条不再显示同一条状态 ——
 * 否则「已受理」与「正在处理」会同时出现在页面上。
 */
const processRegionSpeaks = computed(() =>
  turns.value.some((turn) => turn.showProcess && turn.running),
);

const showGlobalStatus = computed(() => {
  /**
   * 等待用户选择话题：过程区已经在说这件事时，全局条不重复说；
   * 没有过程区（恢复 / 非当前轮）时全局条必须能说清 —— 不能显示成「正在生成」。
   */
  if (session.topicApprovalPending) return !processRegionSpeaks.value;
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
/** 等待用户选择话题的那句话（过程区/全局条共用同一措辞，不各说一套）。 */
const TOPIC_APPROVAL_WAITING_TEXT = "正在等待你选择话题";
const phaseLabel = computed(() => {
  if (session.topicApprovalPending) return TOPIC_APPROVAL_WAITING_TEXT;
  if (!session.turnRunning && session.activity === "idle") return "";
  return ACTIVITY_LABELS[session.activity] ?? "";
});
</script>

<template>
  <div
    ref="containerRef"
    class="stream"
    data-qio-scope="chat"
    tabindex="0"
    aria-label="对话消息"
    v-qio-scrollbar="{ policy: 'auto', axis: 'y', edgeZone: 24, lingerMs: 1800 }"
    @scroll.passive="onScroll"
  >
    <!-- 可聚焦滚动区域：键盘用户也能滚动消息，并且键盘滚动同样会停止自动跟随。 -->
    <!--
      统一滚动规则：这里是普通对话**唯一的主要纵向滚动视口** ——
      * data-qio-scope="chat"：对话根，启用长 URL/路径视觉换行、代码软换行、媒体按宽度适配；
      * v-qio-scrollbar：原生滚动条外观已全局消除，改用共享 QIO 指示条（默认隐藏，
        滚动 / 指针靠近右缘 / 键盘使用该区域时才出现，停止后约 900ms 淡出）；
      * 只声明纵向（axis: "y"）：消息区不得出现横向滚动范围，也不给横向指示条留位置。
      滚动跟随、历史锚定、虚拟列表与既有 ResizeObserver 全部照旧（指示条不接管滚动物理）。
    -->
    <div
      ref="spacerRef"
      class="spacer"
      :style="{ height: virtualizer.getTotalSize() + 'px', position: 'relative' }"
    >
      <div
        v-for="item in virtualizer.getVirtualItems()"
        :key="turnKeyOf(turns[item.index]) || String(item.key)"
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
        <!-- 一轮的宽度链：data-qio-fit（min-width:0 + max-width:100% + overflow-x:clip）
             保证轮内任何内容都不得撑出消息列，也不得产生横向滚动范围 -->
        <div class="turn" :data-turn-key="turnKeyOf(turns[item.index])" data-qio-fit>
          <div class="turn-meta">
            <span class="who">{{ labelOf(turns[item.index]) }}</span>
            <span class="bar"></span>
            <span class="ts">{{ formatTime(turns[item.index].startedAt) }}</span>
          </div>
          <!-- 用户消息：这一轮的起点 -->
          <MessageItem
            v-for="m in turns[item.index].parts.user"
            :key="m.id"
            :data-message-id="m.id"
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
            :data-message-id="m.id"
            :message="m"
          />
          <!-- 正式回答（正文区）：流式生成时用增量渲染 -->
          <MessageItem
            v-for="m in turns[item.index].parts.answers"
            :key="m.id"
            :message="m"
            :data-message-id="m.id"
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
    <!-- 阅读位置回退说明：原目标不在已加载范围时简短说明，不静默跳走 -->
    <p v-if="restoreNote" class="restore-note" role="status" data-test="restore-note">
      {{ restoreNote }}
    </p>
    <ContinueBar />
    <!--
      空状态必须如实（契约 §8）：
      * 正在读历史 → 说「正在读取」，不显示成「没有消息」；
      * 历史读取失败 → 说「没有读出来」，也不显示成「没有消息」；
      * 只有真的没有消息时才显示空对话引导。
    -->
    <div v-if="!messages.length" class="empty" data-test="stream-empty">
      <template v-if="session.history.status === 'loading'">
        <div class="greet serif">正在读取历史…</div>
        <div class="sub mono">之前在对话里的消息还在加载</div>
      </template>
      <template v-else-if="session.history.status === 'error'">
        <div class="greet serif">历史没有读取出来</div>
        <div class="sub mono">可在上方状态条里重试</div>
      </template>
      <!-- #16：互动空态只提示发消息或提交板面；对话模式的星球入口不移到这里。 -->
      <template v-else-if="props.emptyVariant === 'interactive'">
        <div class="greet serif">发消息，或提交板面</div>
        <div class="sub mono">在下方输入框发消息；板面改动准备好后点「提交」</div>
      </template>
      <template v-else>
        <div class="greet serif">今天想聊点什么？</div>
        <div class="sub mono">从下方输入框开始 · 话题星球在右下角</div>
      </template>
    </div>
  </div>
</template>

<style scoped>
.stream {
  flex: 1;
  min-width: 0;
  /* 纵向滚动视口是**普通块容器**：不参与 flex 行分配（历史 bug：被当成 flex 行时，
     spacer 的 width:100% 会把空状态推到右边）。空状态另行绝对定位。 */
  display: block;
  /* 空状态是绝对定位覆盖层（见 .empty）：必须相对消息可视区定位。 */
  position: relative;
  overflow-y: auto;
  /* 横向一律不建立滚动范围：内容在各自的布局链上换行 / 收缩（见 scroll.css 的 data-qio-fit） */
  overflow-x: clip;
  /* 左右留白 = 消息框的统一内边距（输入区读同一个 --chat-pad-inline）。
     星球入口已经移进输入行内部，不再需要 ≤1400px 时给浮动球预留的 132px 右让位。 */
  padding: 20px var(--chat-pad-inline, 12px) 12px var(--chat-pad-inline, 12px);
  /* 原生滚动条外观由 styles/scroll.css 全局消除（不预留轨道空白）；
     本区域的指示条由 v-qio-scrollbar 提供，不再写 scrollbar-width: thin。 */
  /* 表面（边框/圆角/底色）由外层 .chat-box 承担，消息流自己不再画一层底 */
  background: transparent;
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
  /* 统一元信息令牌：无需悬停也能清楚阅读，但仍低于正文层级 */
  font-size: var(--fs-meta, 11.5px);
  color: var(--text-muted);
  letter-spacing: 0.05em;
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
/*
  空状态（FU5）：相对 .stream 绝对铺满消息可视区并水平/垂直居中。
  它脱离滚动内容流 —— 不参与 spacer / 虚拟列表 / typing / ContinueBar 的宽度分配，
  因此既不会被推到右边，也不会影响有消息时的虚拟列表与滚动。
  pointer-events: none：只是文字引导，不拦截滚动 / 其他控件。
*/
.empty {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  justify-content: center;
  align-items: center;
  gap: 14px;
  text-align: center;
  pointer-events: none;
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
  border-radius: var(--qio-ui-radius);
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
/* ---- 阅读位置回退说明（原目标不在已加载范围） ---- */
.restore-note {
  position: absolute;
  top: 10px;
  left: 50%;
  transform: translateX(-50%);
  z-index: 3;
  margin: 0;
  padding: 4px 12px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-pill);
  background: var(--bg-surface);
  color: var(--text-secondary);
  font-size: var(--fs-xs);
  line-height: 1.5;
  box-shadow: var(--shadow-1);
  pointer-events: none;
  max-width: min(560px, calc(100% - 24px));
  text-align: center;
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