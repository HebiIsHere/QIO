<!--
  右下角互动对话窗口（子智能体 A 负责实现）。

  契约：docs/interactive-mode-contract.md §8.1 / §8.3；互动模式契约 2026-10-13 §2 / §4。

  结构（任务书 §4）只有三块：
  1. 顶部：标题 + 一句运行状态 + 尺寸调整入口（−/＋）+ 帮助 + 收起；
  2. 中间：连续消息流（正式回答持续流式显示；工具与阶段记录默认简短、详情按需展开）；
  3. 底部：附件、输入框、发送按钮合成一个输入区；「文字发送不会提交板面」只留一句短提示，
     完整的可见范围 / 媒体读取方式 / 附件限制 / 快捷键放进**可打开的帮助**。
  —— 板面提交过程（理解说明、受理说明）归提交入口显示，这里不再常驻多块长说明。

  移动与尺寸（任务书 §3.1/3.2/3.5/3.6/3.7/3.8/3.10）：
  - 标题栏是**唯一**的移动把手：正文区不参与移动，阅读与输入不被干扰；
  - 四条边 + 四个角走公共 useResizeGesture，标题栏里另有键盘 −/＋；
  - 位置 / 尺寸是**本机查看偏好**（chatFrame.ts，按板面身份绑定）：不进板面状态、
    不进提交载荷、不调模型、不产生表达、不改许可；
  - 拖动中只预览（跟手、无过渡动画），松手才提交一次；取消（指针取消 / Esc / 失焦 / 卸载）
    恢复拖动前状态并清掉监听；
  - 当前显示框用 publishChatFrame 发布，供审批托盘与展开卡片避让。

  保留的既有能力：与对话页同一套会话与附件状态（消息、草稿、附件归属、未决发送、
  失败恢复、回到最新、输入法选字不发送），拖动 / 调整尺寸 / 收起 / 重新渲染都不改这些状态。
-->
<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useInteractiveStore } from "../../stores/interactive";
import { useSessionStore } from "../../stores/session";
import MessageItem from "../MessageItem.vue";
import AttachmentChip from "../AttachmentChip.vue";
import PermissionModeBadge from "../security/PermissionModeBadge.vue";
import FailedSendNotice from "./FailedSendNotice.vue";
import ResizeHandles from "./ResizeHandles.vue";
import { useResizeGesture } from "../../composables/useResizeGesture";
import { vQioScrollbar } from "../../composables/useQioScrollbars";
import {
  isAttachmentDead,
  invalidateAttachmentIdentity,
  beginAttachmentOp,
  decideAttachmentWrite,
  registerAttachmentIdentity,
} from "../../composables/attachmentOps";
import type { AttachmentOpCapture } from "../../composables/attachmentOps";
import {
  humanSize,
  isDesktopShell,
  isSendable,
  markAttachmentRemoved,
  openAttachment,
  pickBrowserFile,
  pickLocalPath,
  prepareAttachment,
  relocateAttachment,
  removeAttachment,
  retryAttachment,
  stateText,
  uploadAttachment,
  waitUntilSettled,
} from "../../services/attachments";
import type { AttachmentRef } from "../../services/attachments";
import {
  canSend,
  chatStatusText,
  normalizeSendText,
  olderMessagesText,
  pendingSendText,
  sendFailureText,
  shouldSendOnKeydown,
} from "../../interactive/chat";
import {
  approveAvailability,
  currentUnderstandingStep,
  rejectAvailability,
  understandingHistory,
  understandingOutcome,
  understandingSettled,
  understandingStepText,
  understandingText,
} from "../../interactive/approval";
import type { Intent } from "../../interactive/types";
import {
  CHAT_FRAME_MIN,
  chatFrameLimits,
  convergeChatFrame,
  defaultChatFrame,
  loadChatFrame,
  saveChatFrame,
  type ChatFrame,
} from "../../interactive/chatFrame";
import { publishChatFrame } from "../../interactive/chatFrameState";
import { RESIZE_DRAG_THRESHOLD, boxEquals, clampNumber } from "../../interactive/resize";
import {
  conversationBindingForSend,
  type ConversationBinding,
} from "./v2/conversationLayout";

/**
 * Phase 3 §6.4：互动绑定由**可信 props**传入（Lead 从 interactive store 投影后传进来）；
 * 不传时保持既有行为（内部读 store.chatBinding），所以旧调用方不受影响。
 * 绑定只作为 session.send 的发送选项，绝不拼进用户原文。
 */
const props = defineProps<{
  binding?: ConversationBinding;
}>();

/**
 * 面板里最多渲染多少条消息。
 * 更早的内容没有被删：它们仍在会话状态里（对话页看得到），这里只是不重绘整个历史。
 */
const TAIL_LIMIT = 60;
/** 输入框最大高度：超过后输入框内部滚动，不把消息区挤没 */
const MAX_INPUT_PX = 132;
/**
 * 附件条默认只列出的条数（验收：附件默认给「数量 + 简述 + 展开入口」）。
 * 其余条目**一条都不删**，只是不默认铺开：点「展开其余 N 个」就地看到全部。
 */
const ATTACH_BRIEF_COUNT = 2;
/** 会话警告的简述长度：默认给一句能读完的简述，完整原文按需展开（原文一个字不改） */
const WARNING_BRIEF_MAX = 72;
/** 可用区域四周留白：贴边的把手不好抓，也和自绘标题栏 / 底部工具栏留出呼吸 */
const AREA_MARGIN = 8;
/** 移动阈值：与尺寸拖动同一个指针语义（阈值内算点击，不改位置） */
const MOVE_THRESHOLD = RESIZE_DRAG_THRESHOLD;
/** 首次打开的默认框要给「入口按钮」留出位置：展开时它就挂在面板下方 */
const TOGGLE_RESERVE = 48;

const store = useInteractiveStore();
const session = useSessionStore();

const inputRef = ref<HTMLTextAreaElement | null>(null);
const streamRef = ref<HTMLElement | null>(null);
const contentRef = ref<HTMLElement | null>(null);
const dockRef = ref<HTMLElement | null>(null);
/** 发送请求进行中（不是「对话在跑」：turnRunning 由 store 表达） */
const sending = ref(false);
/** 发送失败的真实原因（保留输入，不伪装成功） */
const failure = ref("");
/** 这条失败属于哪个话题：切到别的话题后不再把它显示成那边的失败（契约 §10.1） */
const failureTopic = ref<string | null>(null);
/** 对着空白按 Enter 时的提示：说清为什么不发，而不是静默 */
const blankNotice = ref("");
/** 是否跟随最新消息（用户上翻阅读后不再强行拉回） */
const following = ref(true);
/** 附件完整列表是否展开（默认只给数量 + 简述 + 展开入口） */
const attachmentsOpen = ref(false);
/** 会话警告是否展开全文（默认只给简述 + 展开入口） */
const warningOpen = ref(false);

const draft = computed({
  get: () => session.draft,
  set: (value: string) => {
    // 草稿属于会话状态，不属于本组件：收起面板再展开仍然在（与卡片草稿 store.drafts 分开）
    session.draft = value;
  },
});
const verdict = computed(() => canSend(draft.value));
const canSubmitNow = computed(
  () => verdict.value.ok && !sending.value && !attachBusy.value && unready.value.length === 0,
);
const statusText = computed(() =>
  chatStatusText({
    turnRunning: session.turnRunning,
    queuedCount: session.queuedMessageIds.length,
  }),
);
const shownMessages = computed(() =>
  session.messages.length > TAIL_LIMIT ? session.messages.slice(-TAIL_LIMIT) : session.messages,
);
const hiddenText = computed(() => olderMessagesText(session.messages.length - shownMessages.value.length));
/** 轮次级错误（例如后端说没有模型凭据）：如实显示，不当成发送失败 */
const turnError = computed(() => (failure.value ? "" : (session.lastError ?? "")));
/**
 * 会话的非致命警告（WARNING 事件，例如「还没有配置可用的模型凭据」）。
 * 这是「为什么没有回答」最直接的原因，必须显示，不能让用户对着一个空的等待框猜。
 */
const sessionWarning = computed(() => session.warning ?? "");
/**
 * 会话警告的默认呈现：**简述 + 展开入口**。
 * 只截断显示文本，会话层的事实一字不改；展开后看到的是完整原文。
 */
const warningLong = computed(() => sessionWarning.value.length > WARNING_BRIEF_MAX);
const warningText = computed(() => {
  const full = sessionWarning.value;
  if (!warningLong.value || warningOpen.value) return full;
  return full.slice(0, WARNING_BRIEF_MAX).trimEnd() + "…";
});

/* ------------------------------------------------------------------------
 * 位置 / 尺寸：本机查看偏好（按板面身份绑定），只改布局与查看
 * ---------------------------------------------------------------------- */

/** 当前可用区域（视口坐标）：上界是自绘标题栏下边缘，下界是底部工具栏上边缘 */
interface ChatArea {
  x: number;
  y: number;
  w: number;
  h: number;
}

/**
 * 量当前可用区域。
 *
 * 为什么必须扣掉这两块：悬浮对话窗口在屏幕坐标系里，标题栏（0.2.3 自绘）与底部工具栏
 * 都会随窗口宽度换行变高，写死一个高度必然在某个宽度上把面板顶出视口或压住工具栏。
 * 量不到时（jsdom / 还没布好局，rect 全 0）退回视口，绝不把 0 当成「工具栏在顶部」。
 */
function measureArea(): ChatArea {
  const vw = Math.max(1, Math.round(window.innerWidth || 0));
  const vh = Math.max(1, Math.round(window.innerHeight || 0));
  const title = document.querySelector<HTMLElement>(".im-top");
  const titleBottom = title ? title.getBoundingClientRect().bottom : 0;
  const top = (Number.isFinite(titleBottom) && titleBottom > 0 ? Math.round(titleBottom) : 0) + AREA_MARGIN;
  const toolbar = document.querySelector<HTMLElement>('[data-im="board-toolbar"]');
  const rect = toolbar?.getBoundingClientRect();
  /*
    工具栏只在**横向真的伸到对话窗口那一列**时才算边界（2026-10-14 第八个问题）：
    工具栏现在是内容宽度、左对齐，对话窗口默认贴右下角，两者通常不相交 ——
    那就把整块右下角还给对话窗口，不再「被下方工具栏卡在上方」而留白。
    量不到工具栏（首帧 / 收起）时按窗口底边算，和以前一致。
  */
  const dockColumnLeft = vw - AREA_MARGIN - CHAT_FRAME_MIN.w;
  const toolbarReachesDock = Boolean(rect && rect.top > 0 && rect.height > 0 && rect.right > dockColumnLeft);
  const bottom =
    toolbarReachesDock && rect ? Math.round(rect.top) - AREA_MARGIN : vh - AREA_MARGIN;
  return {
    x: AREA_MARGIN,
    y: Math.max(0, top),
    w: Math.max(1, vw - AREA_MARGIN * 2),
    // 下限 120：窗口真的小到装不下时让它自己滚动，而不是算出一个负的可用高度
    h: Math.max(120, bottom - Math.max(0, top)),
  };
}

const area = ref<ChatArea>(measureArea());

/**
 * 工具栏矩形（量不到时为 null）。
 * 只有**横向真的与对话窗口相交**时才需要让位：工具栏现在是内容宽度、左对齐，
 * 对话窗口默认贴右下角，两者通常根本不相交 —— 那样窗口就能一直贴到窗口底边，
 * 不再被整行工具栏「卡在上方」而在右下角留白（2026-10-14 第八个问题）。
 */
const toolbarRect = ref<{ top: number; left: number; right: number } | null>(null);

function measureToolbar(): void {
  const el = document.querySelector<HTMLElement>('[data-im="board-toolbar"]');
  const rect = el?.getBoundingClientRect();
  toolbarRect.value =
    rect && rect.height > 0 && rect.top > 0
      ? { top: Math.round(rect.top), left: Math.round(rect.left), right: Math.round(rect.right) }
      : null;
}

/** 与工具栏横向相交时把底边收到工具栏上方；不相交就原样返回（可以贴到窗口右下角）。 */
function avoidToolbar(box: ChatFrame): ChatFrame {
  const toolbar = toolbarRect.value;
  if (!toolbar) return box;
  const overlapsX = box.x < toolbar.right - 8 && box.x + box.w > toolbar.left + 8;
  if (!overlapsX) return box;
  const maxBottom = toolbar.top - AREA_MARGIN;
  if (box.y + box.h <= maxBottom) return box;
  return { ...box, h: Math.max(CHAT_FRAME_MIN.h, maxBottom - box.y) };
}

/** 恢复：已保存的框按**当前**可用空间收敛（绝不恢复到屏幕外）；没保存过就贴右下角 */
function resolveFrame(next: ChatArea, stored: ChatFrame | null): ChatFrame {
  const origin = { x: next.x, y: next.y };
  if (stored) return convergeChatFrame(stored, { w: next.w, h: next.h }, origin);
  // 默认落点：贴右下角，并且给下方那个「入口按钮」留出一行
  const size = { w: next.w, h: Math.max(CHAT_FRAME_MIN.h, next.h - TOGGLE_RESERVE) };
  return defaultChatFrame(size, origin);
}

/**
 * **用户设定的框**：唯一会被落盘、也是窗口变化后要回去的那一份。
 * 与 frame（当前生效的框）分开，是因为窗口临时变小会把生效框收敛小，
 * 而用户并没有改过尺寸 —— 窗口放大回来必须回到用户框（2026-10-14 第八个问题：
 * 原来收敛结果直接写回生效框，窗口恢复后停在缩小后的尺寸）。
 */
const preferredFrame = ref<ChatFrame>(resolveFrame(area.value, loadChatFrame(store.boardId)));
const frame = ref<ChatFrame>({ ...preferredFrame.value });
/** 拖动中的预览框：只用于显示，不落盘、不写板面状态、不发请求 */
const preview = ref<ChatFrame | null>(null);
const activeFrame = computed(() => preview.value ?? frame.value);

/**
 * 定位父级（板面舞台）在视口里的原点与缩放。
 * frame 一律是**视口坐标**（与 chatFrameState 的契约一致），落到绝对定位的 left/top
 * 时才减去舞台原点；舞台被 CSS zoom 缩放时按实测比例换算，不假设「1 局部像素 = 1 视口像素」。
 */
const stageOrigin = ref({ left: 0, top: 0, scaleX: 1, scaleY: 1 });

function readStageOrigin(): void {
  const host = dockRef.value;
  const parent =
    (host?.offsetParent as HTMLElement | null) ?? document.querySelector<HTMLElement>(".im-stage");
  if (!parent) {
    stageOrigin.value = { left: 0, top: 0, scaleX: 1, scaleY: 1 };
    return;
  }
  const rect = parent.getBoundingClientRect();
  const scaleX = parent.offsetWidth > 0 && rect.width > 0 ? rect.width / parent.offsetWidth : 1;
  const scaleY = parent.offsetHeight > 0 && rect.height > 0 ? rect.height / parent.offsetHeight : 1;
  stageOrigin.value = {
    left: rect.left,
    top: rect.top,
    scaleX: scaleX > 0 ? scaleX : 1,
    scaleY: scaleY > 0 ? scaleY : 1,
  };
}

const dockStyle = computed<Record<string, string>>(() => {
  const current = activeFrame.value;
  const origin = stageOrigin.value;
  const round = (value: number) => Math.round(value * 100) / 100;
  return {
    left: round((current.x - origin.left) / origin.scaleX) + "px",
    top: round((current.y - origin.top) / origin.scaleY) + "px",
    width: round(current.w / origin.scaleX) + "px",
    height: round(current.h / origin.scaleY) + "px",
  };
});

/**
 * 面板尺寸由 frame 说话：样式表里那两条 max-width / max-height 是**旧锚定模型**的上限
 * （从工具栏顶边倒推、只限制不改位置），现在位置与尺寸都由用户拖动决定，再夹一层会让
 * 「拖到一半被截住」；外层 margin-bottom（切换条抬升）同理，这里显式归零。
 */
const panelStyle: Record<string, string> = { maxWidth: "none", maxHeight: "none", margin: "0" };

function applyFrame(next: ChatFrame, options: { save?: boolean } = {}): void {
  frame.value = { x: next.x, y: next.y, w: next.w, h: next.h };
  if (options.save) {
    // 只有用户拖动 / 调整尺寸才改「用户框」并落盘；窗口收敛不落盘、也不改用户框
    preferredFrame.value = { ...frame.value };
    saveChatFrame(store.boardId, frame.value);
  }
}

/** 显示框变化（恢复 / 拖动预览 / 提交 / 收起）与卸载都发布一次，供别的浮层避让 */
watch(
  [activeFrame, () => store.chatOpen],
  () => publishChatFrame(activeFrame.value, !store.chatOpen),
  { immediate: true },
);

/** 视口 / 工具栏变化：重新量可用区域，并把当前框收敛回可见范围（收敛不落盘，保留用户原意） */
function refreshArea(): void {
  area.value = measureArea();
  measureToolbar();
  readStageOrigin();
  if (preview.value) return; // 拖动中不打断跟手
  // 从**用户框**重新推导（不是从当前生效框）：窗口放大回来就能回到用户设定的尺寸
  const converged = avoidToolbar(
    convergeChatFrame(preferredFrame.value, { w: area.value.w, h: area.value.h }, { x: area.value.x, y: area.value.y }),
  );
  if (!boxEquals(frame.value, converged)) applyFrame(converged);
}

// 换板面：各恢复各的本机偏好（键里带板面身份），不把上一个板面的窗口带过去
watch(
  () => store.boardId,
  (id) => {
    if (preview.value) return;
    // 换板面 = 换一份本机偏好：用户框与生效框一起换，之后窗口变化都从这一份推导
    const next = resolveFrame(area.value, loadChatFrame(id));
    preferredFrame.value = { ...next };
    applyFrame(avoidToolbar(next));
  },
);

/* ------------------------------------------------------------------------
 * 尺寸：四条边 + 四个角（公共手势层）
 * ---------------------------------------------------------------------- */

const {
  active: resizeActive,
  onHandleDown,
  nudge,
  cancel: cancelResize,
} = useResizeGesture({
  // 常态框：拖动起点快照的来源（拖动期间它保持不变）
  box: () => activeFrame.value,
  // 上限 = 当前可用区域；窗口比最小尺寸还小时由 chatFrameLimits 向内收敛
  limits: () => chatFrameLimits({ w: area.value.w, h: area.value.h }),
  // 屏幕坐标系：1 视口像素就是 1 像素
  scale: () => 1,
  onPreview: (box) => {
    preview.value = { ...box };
  },
  onCommit: (box) => {
    preview.value = null;
    applyFrame(box, { save: true });
  },
  onCancel: () => {
    // 恢复拖动前那一版（frame 本来就没被改过），只把预览收掉
    preview.value = null;
  },
});

const resizing = computed(() => resizeActive.value);

/* ------------------------------------------------------------------------
 * 移动：标题栏拖动（指针 + 阈值），取消即恢复
 * ---------------------------------------------------------------------- */

const moving = ref(false);
let moveSnapshot: ChatFrame | null = null;
let movePointerId: number | null = null;
let moveStart = { x: 0, y: 0 };
let moveLatest: ChatFrame | null = null;
let moveStarted = false;
let moveAttached = false;

function detachMove(): void {
  if (!moveAttached) return;
  moveAttached = false;
  document.removeEventListener("pointermove", onMoveMove, true);
  document.removeEventListener("pointerup", onMoveUp, true);
  document.removeEventListener("pointercancel", onMoveCancel, true);
  document.removeEventListener("keydown", onMoveKeyDown, true);
  window.removeEventListener("blur", onMoveCancel);
}

function resetMove(): void {
  detachMove();
  moveSnapshot = null;
  movePointerId = null;
  moveLatest = null;
  moveStarted = false;
  moving.value = false;
}

/** 取消：恢复这次拖动之前的框，清理监听（不留半完成状态） */
function cancelMove(): void {
  const restore = moveSnapshot;
  const wasDragging = moveStarted;
  resetMove();
  if (!wasDragging || !restore) return;
  preview.value = null;
  applyFrame(restore);
}

function attachMove(): void {
  if (moveAttached) return;
  moveAttached = true;
  document.addEventListener("pointermove", onMoveMove, true);
  document.addEventListener("pointerup", onMoveUp, true);
  document.addEventListener("pointercancel", onMoveCancel, true);
  document.addEventListener("keydown", onMoveKeyDown, true);
  window.addEventListener("blur", onMoveCancel);
}

/** 位移 → 新的视口坐标框，并夹进当前可用区域（拖动中只算，不写任何状态） */
function movedFrame(start: ChatFrame, dx: number, dy: number): ChatFrame {
  const box = area.value;
  const maxX = box.x + Math.max(0, box.w - start.w);
  const maxY = box.y + Math.max(0, box.h - start.h);
  return {
    x: clampNumber(start.x + dx, box.x, maxX),
    y: clampNumber(start.y + dy, box.y, maxY),
    w: start.w,
    h: start.h,
  };
}

function onHeadPointerDown(event: PointerEvent): void {
  // 只接主键 / 主指针：右键菜单与多指手势不进入移动
  if (event.button !== 0 || event.isPrimary === false) return;
  const target = event.target as HTMLElement | null;
  // 标题栏里的按钮与 −/＋ 有自己的语义：不让它们变成「拖动面板」
  if (target?.closest("button, a, input, textarea, summary, [data-resize-root]")) return;
  event.preventDefault();
  cancelMove();
  moveSnapshot = { ...activeFrame.value };
  movePointerId = typeof event.pointerId === "number" ? event.pointerId : null;
  moveStart = { x: Number(event.clientX ?? 0), y: Number(event.clientY ?? 0) };
  moveLatest = null;
  moveStarted = false;
  attachMove();
}

function onMoveMove(event: PointerEvent): void {
  if (!moveSnapshot) return;
  if (movePointerId !== null && typeof event.pointerId === "number" && event.pointerId !== movePointerId) {
    return;
  }
  const dx = Number(event.clientX ?? 0) - moveStart.x;
  const dy = Number(event.clientY ?? 0) - moveStart.y;
  if (!moveStarted) {
    // 阈值之内算点击：不改位置，也不进入「拖动中」
    if (Math.abs(dx) < MOVE_THRESHOLD && Math.abs(dy) < MOVE_THRESHOLD) return;
    moveStarted = true;
    moving.value = true;
  }
  moveLatest = movedFrame(moveSnapshot, dx, dy);
  preview.value = moveLatest;
}

function onMoveUp(event: PointerEvent): void {
  if (movePointerId !== null && typeof event.pointerId === "number" && event.pointerId !== movePointerId) {
    return;
  }
  const commit = moveStarted ? moveLatest : null;
  resetMove();
  if (!commit) return;
  preview.value = null;
  applyFrame(commit, { save: true });
}

function onMoveCancel(): void {
  cancelMove();
}

function onMoveKeyDown(event: KeyboardEvent): void {
  if (event.key === "Escape") cancelMove();
}

/**
 * 失败事实只属于它自己的话题（契约 §10.1）：
 * - 当前就在原话题：显示失败原因与**取回入口**（不自动写回、不自动重发）；
 * - 当前在别的话题：一个字都不显示、不改输入框、不改草稿 —— 那边没有发生过失败。
 */
const failureVisible = computed(
  () => Boolean(failure.value) && failureTopic.value === session.currentTopicId,
);

/**
 * 草稿保存状态（契约 §9.4）：失败必须显示真实原因并可重试，
 * 保存成功才说「已保存在本机」—— 绝不把失败说成已保存。
 */
const draftStatusText = computed(() => {
  if (session.draftSaveStatus === "saving") return "草稿保存中…";
  if (session.draftSaveStatus === "saved") return "草稿已保存在本机";
  return "";
});
const draftSaveError = computed(() =>
  session.draftSaveStatus === "error"
    ? "草稿未保存：" + (session.draftSaveError || "原因未知") + "（文字还在输入框里，可以重试）"
    : "",
);

/**
 * 被让位的旧草稿（11a）：第一次迁移到某个话题时，话题原有草稿被让位但**不删**，
 * 两份都保留。这里给出「取回（互换）/ 放弃」入口，与对话页同一份会话状态。
 */
const displacedDraft = computed(() => session.displacedDraftsForTopic(session.currentTopicId)[0] ?? null);
const displacedCount = computed(() => session.displacedDraftsForTopic(session.currentTopicId).length);
const displacedSnippet = computed(() => {
  const text = displacedDraft.value?.text ?? "";
  const oneLine = text.replace(/\s+/g, " ").trim();
  return oneLine.length > 32 ? oneLine.slice(0, 32) + "…" : oneLine || "（空草稿）";
});
const displacedNotice = ref("");

function restoreDisplaced(): void {
  const target = displacedDraft.value;
  if (!target) return;
  const result = session.restoreDisplacedDraft(target.id);
  displacedNotice.value = result.ok
    ? "已取回那份草稿：输入框是它，刚才的文字留在入口里（两份都还在）"
    : result.reason ?? "现在不能取回这份草稿";
}

function discardDisplaced(): void {
  const target = displacedDraft.value;
  if (!target) return;
  session.discardDisplacedDraft(target.id);
  displacedNotice.value = "已放弃这份被让位的草稿（当前输入不受影响）";
}

/* ------------------------------------------------------------------------
 * 过程状态（契约 §14.6）：默认只留一行「这次到哪一步了」
 *
 * 板面提交过程的长说明归提交入口（任务书 §4.4）：这里不再渲染 understandingNote /
 * submitNote，只保留当前这一句与服务端事实（阶段、补充要求、无任务、审批入口）。
 * 这一块只读 store 里服务端给出的事实，不伪造进度。
 * ---------------------------------------------------------------------- */
const uStatus = computed(() => store.understandingStatus);
const submitting = computed(() => store.submitStatus === "submitting");
const uText = computed(() => {
  // 正在提交时先显示这一次的事实，不让上一次的理解结果冒充这一次
  if (submitting.value) {
    return {
      label: "正在提交",
      detail: "正在保存板面并把这次表达交给 QIO；受理成功后会立刻显示「已受理」。",
      tone: "waiting" as const,
      settled: false,
      needsUser: false,
      known: false,
    };
  }
  return understandingText(store.understanding, store.understandingStatus);
});
const uOutcome = computed(() => understandingOutcome(uStatus.value));
const uLive = computed(() => Boolean(uStatus.value) && !understandingSettled(uStatus.value));
const uCurrent = computed(() => currentUnderstandingStep(store.understanding));
const uHistory = computed(() => understandingHistory(store.understanding));
/** 只留这一块：有理解状态或正在提交时才占位，不再被「说明文案」撑出来 */
const processVisible = computed(() => uStatus.value !== null || submitting.value);
/** 有没有可展开的详情（没有就不给一个点不动的「详情」按钮） */
const hasProcessDetail = computed(
  () =>
    Boolean(uCurrent.value) ||
    uHistory.value.length > 0 ||
    uOutcome.value === "clarification" ||
    uOutcome.value === "no_action",
);
const missingText = computed(() => {
  const missing = store.understanding?.clarification?.missing ?? [];
  return missing.length ? missing.join("、") : "请补充这次要做什么";
});
/** 这次理解产出的、还在等待审批的条目（审批入口自动展开） */
const pendingApprovals = computed(() =>
  store.understandingIntents.filter((item) =>
    ["pending", "needs_update", "waiting_dependency", "waiting_confirm"].includes(item.status),
  ),
);
/**
 * 下面还有用户**没看见、又需要他处理**的东西：待审批 / 轮次错误 / 失败恢复。
 *
 * 为什么必须有它：过程与提示已并进消息流（同一阅读流只留一层滚动），用户上翻阅读时
 * 它们会滚出视野 —— 「简化滚动」绝不允许变成「藏掉审批或重要错误」。
 * 这个字符串由钉在阅读区底部的固定入口（不在滚动内容里，滚不走）显示出来：
 * 用户一步就看得见「下面还有什么」，一步就能回到最新。
 */
const attentionText = computed(() => {
  const parts: string[] = [];
  if (pendingApprovals.value.length) parts.push("待审批 " + pendingApprovals.value.length + " 项");
  if (turnError.value || failureVisible.value) parts.push("有错误待查看");
  return parts.join(" · ");
});
const processOpen = ref(false);
const processBusy = ref<string | null>(null);
const processNote = ref<string | null>(null);
const processRefreshing = ref(false);

async function retryUnderstanding(): Promise<void> {
  if (processRefreshing.value) return;
  processRefreshing.value = true;
  processNote.value = null;
  try {
    const result = await store.retryUnderstanding();
    if (!result.ok) processNote.value = result.reason ?? "没有开始重试";
  } finally {
    processRefreshing.value = false;
  }
}

async function refreshUnderstanding(): Promise<void> {
  if (processRefreshing.value) return;
  processRefreshing.value = true;
  processNote.value = null;
  try {
    await store.refreshUnderstanding();
  } finally {
    processRefreshing.value = false;
  }
}

/** 批准 / 拒绝：重复点击不会重复提交（store 里同一意图只允许一次） */
async function decideIntent(intent: Intent, decision: "approve" | "reject"): Promise<void> {
  if (processBusy.value) return;
  processBusy.value = intent.id;
  processNote.value = null;
  try {
    if (decision === "approve") {
      const result = await store.approve(intent.id, approveAvailability(intent).needsConfirm);
      processNote.value = result.ok
        ? result.detail ?? "已批准：任务已交给真实执行，进度以执行记录为准。"
        : "没有批准：" + (result.detail ?? result.reason ?? "服务端未说明原因");
    } else {
      const result = await store.reject(intent.id);
      processNote.value = result.ok
        ? "已拒绝：预览消失，板面原内容保留。"
        : "没有拒绝：" + (result.detail ?? result.reason ?? "服务端未说明原因");
    }
  } finally {
    processBusy.value = null;
  }
}

/* ------------------------------------------------------------------------
 * 收起 / 展开：草稿、光标、滚动位置都要留在原处
 * ---------------------------------------------------------------------- */

/** 收起时记下的光标位置：重新展开恢复到原处（输入法组合无法跨越销毁-重建，见交付说明） */
let caretMemory: { start: number; end: number } | null = null;
/** 收起时记下的阅读位置：用户上翻过就回到那一处，没有就继续跟随最新 */
let scrollMemory = 0;

watch(
  () => store.chatOpen,
  (open) => {
    if (!open) {
      const input = inputRef.value;
      caretMemory = input ? { start: input.selectionStart ?? 0, end: input.selectionEnd ?? 0 } : null;
      scrollMemory = streamRef.value?.scrollTop ?? 0;
      session.flushDraft();
      return;
    }
    void nextTick(() => {
      const input = inputRef.value;
      if (input) {
        input.focus({ preventScroll: true });
        if (caretMemory) {
          const length = input.value.length;
          input.setSelectionRange(
            Math.min(caretMemory.start, length),
            Math.min(caretMemory.end, length),
          );
          caretMemory = null;
        }
      }
      autosize();
      const el = streamRef.value;
      if (!el) return;
      if (following.value) {
        void scrollToBottom(true);
        return;
      }
      // 只在真的量到可滚动范围时才夹住：量不到（首帧 / 无布局）时按记下的位置原样恢复
      const maxScroll = el.scrollHeight - el.clientHeight;
      el.scrollTop = maxScroll > 0 ? Math.min(Math.max(0, scrollMemory), maxScroll) : Math.max(0, scrollMemory);
    });
  },
);

function nearBottom(): boolean {
  const el = streamRef.value;
  if (!el) return true;
  return el.scrollHeight - el.scrollTop - el.clientHeight <= 48;
}

/**
 * 这段时间内到达的 scroll 事件**不算用户意图**。
 *
 * 两种事件会自己制造出来：程序化贴底，以及窗口/面板尺寸变化导致的重新换行。
 * 实测（360px 宽、160 条消息）：尺寸一变，浏览器就按新高度发一次 scroll，
 * 那一刻的位置看起来「离底部很远」（差 157px）。不设防就会把「跟随最新」
 * 误判成「用户在上翻阅读」，面板停在半截并弹出「回到最新」——而用户什么都没做。
 */
let ignoreScrollUntil = 0;

function onScroll() {
  if (Date.now() < ignoreScrollUntil) return;
  following.value = nearBottom();
}

/**
 * 用户自己滚动（滚轮）时才立刻解除上面的保护。
 * 滚轮事件先于它引起的 scroll 事件到达，所以「用户上翻」不会被误挡。
 */
function onUserScroll() {
  ignoreScrollUntil = 0;
}

/**
 * 窗口尺寸变化：先设保护再贴底。
 *
 * 顺序很关键：浏览器在尺寸变化时会**先**发出由布局引起的 scroll 事件、
 * 之后才轮到 ResizeObserver 回调。只在 ResizeObserver 里设保护就晚了一步 ——
 * 「跟随最新」会先被误判成 false，回调里的跟随判断直接不成立。
 */
function onWindowResize() {
  ignoreScrollUntil = Date.now() + 400;
  refreshArea();
  if (following.value) void scrollToBottom(true);
}

async function scrollToBottom(force = false) {
  await nextTick();
  const el = streamRef.value;
  if (!el) return;
  if (!force && !following.value) return;
  ignoreScrollUntil = Date.now() + 300;
  el.scrollTop = el.scrollHeight;
  following.value = true;
}

// 新消息、以及一轮的阶段变化（等待中 → 生成中）都要跟着走，除非用户正在上翻阅读
watch(() => session.messages.length, () => void scrollToBottom());
watch(() => session.turnPhase, () => void scrollToBottom());

/** 内容或容器尺寸变化时重新贴底（窄窗口换行变多、面板被拉动都算） */
let resizeObserver: ResizeObserver | null = null;
/** 工具栏尺寸变化时重新量一次可用区域（它会长高，可用高度随之变化） */
let toolbarObserver: ResizeObserver | null = null;

onMounted(async () => {
  // 首帧渲染时父级（标题栏 / 工具栏）可能还没布好局：挂载后按真实可用区域收敛一次
  refreshArea();
  readStageOrigin();
  await scrollToBottom(true);
  inputRef.value?.focus({ preventScroll: true });
  // jsdom 里没有 ResizeObserver：没有就跳过（不影响功能，只影响「尺寸变化后自动贴底」）
  if (typeof ResizeObserver !== "undefined") {
    resizeObserver = new ResizeObserver(() => {
      // 尺寸/换行变化自带的 scroll 事件不是用户意图
      ignoreScrollUntil = Date.now() + 300;
      if (following.value) void scrollToBottom(true);
    });
    // 容器与内容都要观察：只观察容器时，「窗口变窄 → 文字换行变多 → 内容变高」
    // 不会再触发回调，画面就停在半截（实测过：390px 下 atBottom 变 false）。
    if (streamRef.value) resizeObserver.observe(streamRef.value);
    if (contentRef.value) resizeObserver.observe(contentRef.value);

    const toolbar = document.querySelector('[data-im="board-toolbar"]');
    if (toolbar) {
      toolbarObserver = new ResizeObserver(() => refreshArea());
      toolbarObserver.observe(toolbar);
    }
  }
  window.addEventListener("resize", onWindowResize);
});

onBeforeUnmount(() => {
  // 面板被路由切换卸载：把待保存的草稿落盘（本机写入同步完成，不依赖网络）
  session.flushDraft();
  // 还在拖动 / 调整尺寸就先恢复，再清理监听；最后撤掉发布出去的显示框
  cancelMove();
  cancelResize();
  resizeObserver?.disconnect();
  resizeObserver = null;
  toolbarObserver?.disconnect();
  toolbarObserver = null;
  window.removeEventListener("resize", onWindowResize);
  publishChatFrame(null);
});

function autosize() {
  const el = inputRef.value;
  if (!el) return;
  if (!el.value.trim()) {
    el.style.height = "";
    return;
  }
  el.style.height = "auto";
  el.style.height = Math.min(el.scrollHeight, MAX_INPUT_PX) + "px";
}

// ---------------------------------------------------------------------------
// 附件入口（契约 §4.1）：只发送**当前消息 + 明确选定的附件**
// ---------------------------------------------------------------------------
//
// 这里复用与对话页同一个归属 / tombstone 规则（composables/attachmentOps）：
//   * 归属：点击那一刻用 beginAttachmentOp 冻结（切话题也不改这条附件的去向）；
//   * tombstone：用户明确移除的附件先 markAttachmentRemoved，晚到的准备结果一律丢弃（不复活）。
// 但**不**自动带上板面、未提交改动、未许可文字卡与预览内容：发送载荷就三样
// （当前文字、本次选定的附件 id、它们的引用），其余什么都不带。

/** 悬浮聊天里已选定、准备随下一条消息发出的附件（组件本地，不进会话草稿）。 */
const pendingAttachments = ref<AttachmentRef[]>([]);
const attachBusy = ref(false);
const attachError = ref("");
const attachNotice = ref("");
const attachPath = ref("");
const attachPathOpen = ref(false);
const attachInput = ref<HTMLInputElement | null>(null);
/** 上一次失败的操作（重试入口按同一个来源重放，绝不换一个来源偷偷成功）。 */
const attachRetry = ref<{ kind: "file"; file: File } | { kind: "path"; path: string } | null>(null);

const readyAttachments = computed(() => pendingAttachments.value.filter((item) => isSendable(item)));
const unready = computed(() => pendingAttachments.value.filter((item) => !isSendable(item)));
/** 默认只列出前几条：数量与简述先给到，完整列表由「展开其余 N 个」打开 */
const shownAttachments = computed(() =>
  attachmentsOpen.value ? pendingAttachments.value : pendingAttachments.value.slice(0, ATTACH_BRIEF_COUNT),
);
const hiddenAttachmentCount = computed(() => Math.max(0, pendingAttachments.value.length - ATTACH_BRIEF_COUNT));
/** 待发附件的总大小（只算本次真的会发出去的那些）。 */
const attachTotalBytes = computed(() =>
  (readyAttachments.value.length ? readyAttachments.value : pendingAttachments.value).reduce(
    (sum, item) => sum + (item.sizeBytes || 0),
    0,
  ),
);
/**
 * 附件区的可见状态：
 *
 * * 有真实错误/提示 → 显示它（不与下面的「闸门原因」重复）；
 * * 有**没准备好**的附件 → 直接显示卡在哪一个（不要求用户先点一次发送才知道：
 *   发送按钮这时本来就是禁用的，点了也不会发生任何事）。
 */
const attachHintText = computed(() => {
  if (attachError.value) return attachError.value;
  if (attachBusy.value) return "正在登记附件…";
  return attachmentBlockReason();
});

/** 把结果写到待发列表：已移除（tombstone）的一律不写回（K1.3）。 */
function commitAttachment(item: AttachmentRef): void {
  pendingAttachments.value = upsertAttachment(pendingAttachments.value, item);
}

function upsertAttachment(list: AttachmentRef[], item: AttachmentRef): AttachmentRef[] {
  if (isAttachmentDead(null, item.id)) return list.filter((entry) => entry.id !== item.id);
  const next = list.slice();
  const index = next.findIndex((entry) => entry.id === item.id);
  if (index >= 0) next[index] = item;
  else next.push(item);
  return next;
}

/**
 * 跟进一个附件的准备状态（prepared → ready / failed / changed / missing）。
 * 返回最终引用；状态没跟到时返回 null（旧条目保留，不谎报就绪）。
 */
async function trackAttachment(item: AttachmentRef, capture: AttachmentOpCapture): Promise<AttachmentRef | null> {
  commitAttachment(item);
  try {
    const settled = await waitUntilSettled(item, { onUpdate: (next) => commitAttachment(next) });
    // 归属裁决：结果不该写进当前界面时也不丢（它属于发起那一刻的那个话题）
    const verdict = decideAttachmentWrite(capture, { id: settled.id, isCurrentTopic: true });
    if (verdict.target === "drop") {
      pendingAttachments.value = pendingAttachments.value.filter((entry) => entry.id !== settled.id);
    } else {
      commitAttachment(settled);
    }
    return settled;
  } catch (err) {
    attachError.value = "附件「" + item.name + "」的准备状态没有跟到：" + messageOfError(err);
    return null;
  }
}

function messageOfError(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

/** 选择本地文件（浏览器只有字节时走上传；绝不用 input[type=file] 的 fakepath）。 */
async function addFiles(files: File[]): Promise<void> {
  if (!files.length) return;
  const capture = beginAttachmentOp({ kind: "upload", topicId: session.currentTopicId });
  attachError.value = "";
  attachNotice.value = "";
  attachRetry.value = { kind: "file", file: files[0] };
  attachBusy.value = true;
  try {
    for (const file of files) {
      try {
        const created = await uploadAttachment(file, { topicId: capture.topicId, sourceKind: "browser_upload" });
        registerAttachmentIdentity(capture.topicId, created.id, capture.opToken);
        attachRetry.value = null;
        void trackAttachment(created, capture);
      } catch (err) {
        attachError.value =
          "「" + file.name + "」没有上传成功：" + messageOfError(err) + "（输入框里的文字还在，可以重试）";
        attachRetry.value = { kind: "file", file };
      }
    }
  } finally {
    attachBusy.value = false;
  }
}

/** 粘贴/输入一个真实路径（走 prepareAttachment，不做浏览器上传）。 */
async function addPath(path: string): Promise<void> {
  const value = path.trim();
  if (!value) return;
  const capture = beginAttachmentOp({ kind: "prepare", topicId: session.currentTopicId });
  attachError.value = "";
  attachNotice.value = "";
  attachBusy.value = true;
  try {
    const created = await prepareAttachment(value, { topicId: capture.topicId, sourceKind: "desktop_path" });
    registerAttachmentIdentity(capture.topicId, created.id, capture.opToken);
    attachRetry.value = null;
    attachPath.value = "";
    attachPathOpen.value = false;
    void trackAttachment(created, capture);
  } catch (err) {
    attachError.value =
      "「" + value + "」没有登记成功：" + messageOfError(err) + "（输入框里的文字还在，可以重试）";
    attachRetry.value = { kind: "path", path: value };
  } finally {
    attachBusy.value = false;
  }
}

/** 重试上一次失败的操作：按同一个来源重放（不换来源，也不谎报成功）。 */
async function retryAttach(): Promise<void> {
  const target = attachRetry.value;
  if (!target) return;
  if (target.kind === "file") await addFiles([target.file]);
  else await addPath(target.path);
}

/**
 * 打开文件选择器（对话框取消时什么都不发生，也不报假失败）。
 *
 * 桌面壳优先用原生选择器拿**真实路径**（登记源引用、不复制）；只有拿不到路径
 * （浏览器 / 原生选择器不可用）才退回字节上传，并在来源标注里说清是工作区材料。
 */
async function pickFile(): Promise<void> {
  attachError.value = "";
  if (isDesktopShell()) {
    try {
      const path = await pickLocalPath();
      if (path) {
        await addPath(path);
        return;
      }
    } catch (err) {
      attachError.value = "原生文件选择器不可用（" + messageOfError(err) + "）：已退回文件上传";
    }
  }
  const file = await pickBrowserFile();
  if (!file) return;
  await addFiles([file]);
}

/** 粘贴路径入口：桌面端弹系统路径选择器，浏览器里退回手输（都如实）。 */
async function pickPath(): Promise<void> {
  attachError.value = "";
  const picked = await pickLocalPath();
  if (picked) {
    await addPath(picked);
    return;
  }
  attachPathOpen.value = true;
  await nextTick();
}

async function onFileInput(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement;
  const files = Array.from(input.files ?? []);
  input.value = "";
  if (!files.length) return;
  await addFiles(files);
}

/** 明确移除：先写 tombstone，再删服务端记录；晚到的结果不得把它放回来。 */
async function removeAttachmentAt(id: string): Promise<void> {
  const target = pendingAttachments.value.find((item) => item.id === id);
  // 身份的失效（attachmentOps）与 tombstone（attachments）都要写：一个管归属，一个管持久化的「已移除」
  invalidateAttachmentIdentity(id);
  markAttachmentRemoved(session.currentTopicId, id);
  pendingAttachments.value = pendingAttachments.value.filter((item) => item.id !== id);
  if (attachRetry.value?.kind === "file" && attachRetry.value.file.name === target?.name) {
    attachRetry.value = null;
  }
  try {
    await removeAttachment(id);
  } catch (err) {
    // 服务端记录没删掉：本地仍然按「用户已经移除」处理（界面不复活它），如实说明
    attachNotice.value = "「" + (target?.name ?? id) + "」已从这条消息里移除，但服务端记录没有删掉：" + messageOfError(err);
  }
}

async function openAttachmentAt(item: AttachmentRef): Promise<void> {
  attachNotice.value = "";
  try {
    const result = await openAttachment(item, { desktop: false });
    attachNotice.value = result?.note ?? "";
  } catch (err) {
    attachNotice.value = "打不开「" + item.name + "」：" + messageOfError(err);
  }
}

async function retryAttachmentAt(item: AttachmentRef): Promise<void> {
  attachError.value = "";
  try {
    commitAttachment(await retryAttachment(item.id));
  } catch (err) {
    attachError.value = "「" + item.name + "」重试失败：" + messageOfError(err);
  }
}

async function relocateAttachmentAt(item: AttachmentRef): Promise<void> {
  const path = await pickLocalPath();
  if (!path) return;
  attachError.value = "";
  try {
    commitAttachment(await relocateAttachment(item.id, path));
  } catch (err) {
    attachError.value = "「" + item.name + "」重新定位失败：" + messageOfError(err);
  }
}

/** 附件闸门：没准备好就不许当作「这条消息发出去了」。 */
function attachmentBlockReason(): string {
  if (attachBusy.value) return "附件还在登记中：等它登记完再发送，或先移除它";
  if (!unready.value.length) return "";
  return (
    "这些附件还没有准备好，不能当作发送成功：" +
    unready.value.map((item) => "「" + item.name + "」" + stateText(item)).join("；") +
    "（文字与附件都留在输入区）"
  );
}

async function submit() {
  if (sending.value) return;
  const check = canSend(draft.value);
  if (!check.ok) {
    // 空白不发：把原因说出来，不要静默丢掉这次操作
    blankNotice.value = check.reason ?? "还没有输入内容";
    return;
  }
  /**
   * 附件闸门：没准备好就不许当作「这条消息发出去了」。
   * 文字与附件都留在输入区，并说清卡在哪一个。
   */
  const blocked = attachmentBlockReason();
  if (blocked) {
    // 闸门直接可见（按钮此时也是禁用的）：这里再如实说一次，绝不静默发出去
    attachError.value = "";
    attachNotice.value = blocked;
    return;
  }
  const snapshot = draft.value;
  // 只发输入的文字：不追加板面、未提交改动、注释或选择范围
  const payload = normalizeSendText(snapshot);
  // 本次**明确选定**的附件：用户在悬浮聊天里亲手选的那些，一个不多一个不少
  const sentAttachments = pendingAttachments.value.filter((item) => isSendable(item));
  blankNotice.value = "";
  failure.value = "";
  attachError.value = "";
  /**
   * 归属在**点击这一刻**定下（契约 §10.1）：清空输入框、等待回执期间切换话题，
   * 都不许改变这条消息的去向，也不许把失败原文放进别的话题的输入框。
   */
  const attribution = session.sendAttribution();
  failureTopic.value = attribution.topicId;
  sending.value = true;
  draft.value = "";
  await nextTick();
  autosize();
  try {
    const ok = await session.send(
      payload,
      sentAttachments.map((item) => item.id),
      sentAttachments,
      {
        /**
         * 阶段三契约 §13.3：互动悬浮聊天发出的消息携带可验证的互动绑定。
         * 服务端核对板面存在 / 提交归属后写入 TurnContext；演示或普通聊天不会走到这里。
         */
        interactive: conversationBindingForSend(props.binding) ?? store.chatBinding,
      },
    );
    if (ok) {
      // 发送成功：这些附件已经跟着这条消息走了，不许跟着下一条再发一次
      const sentIds = new Set(sentAttachments.map((item) => item.id));
      pendingAttachments.value = pendingAttachments.value.filter((item) => !sentIds.has(item.id));
    }
    if (!ok) {
      /**
       * 与 Composer 同一套规则（F12）：未决发送（受理情况未知）时**不**自动把原文
       * 放回输入框 —— 那会让用户顺手按 Enter 就用新身份再发一次，绕开幂等保护。
       * 状态与入口都交给「还没有确认是否发出的文字」那一块（查证 / 同身份重试 /
       * 查看原文 / 明确放弃），两个入口读的是会话层的同一份事实。
       */
      if (session.pendingSendConfirm) {
        failure.value = pendingSendText(
          session.unresolvedSendReason(session.pendingSendConfirm.messageId) ?? session.lastError,
        );
      } else {
        failure.value = sendFailureText(session.lastError);
        session.retryFailedSend(attribution.draftId);
      }
    }
  } catch (err) {
    failure.value = sendFailureText((err as Error).message);
  } finally {
    sending.value = false;
    await nextTick();
    autosize();
    void scrollToBottom(true);
    inputRef.value?.focus({ preventScroll: true });
  }
}

function onKeydown(event: KeyboardEvent) {
  // Enter 发送 / Shift+Enter 换行 / 输入法选字中不发送，判定都在纯函数里（可离线验证）
  if (!shouldSendOnKeydown(event)) return;
  event.preventDefault();
  void submit();
}
</script>

<template>
  <div class="chat-dock" ref="dockRef" :class="{ moving, open: store.chatOpen }" :style="dockStyle" data-im="chat-dock">
    <section
      v-if="store.chatOpen"
      id="im-chat-panel"
      class="panel"
      :class="{ resizing }"
      :style="panelStyle"
      data-im="chat-panel"
      data-qio-scope="chat"
      data-qio-fit
      aria-label="与 QIO 对话"
      @wheel.stop
    >
      <!--
        尺寸调整：四条边 + 四个角的拖动热区盖住**整块面板**。
        键盘 −/＋ 放在标题栏里单独一组（见下），两组共用同一个手势层。
      -->
      <ResizeHandles :show-keys="false" label="对话窗口尺寸调整" @handle-down="onHandleDown" />

      <!-- 标题栏 = 身份条 + 唯一的移动把手；正文区不参与移动，阅读与输入不被干扰 -->
      <header class="panel-head" data-im="chat-head" data-qio-fit @pointerdown="onHeadPointerDown">
        <span class="panel-title serif">对话</span>
        <span class="panel-status mono" data-im="chat-status">{{ statusText }}</span>
        <div class="panel-tools">
          <!--
            2026-10-14（用户要求）：互动模式里不再出现宽 / 高那对按钮 —— 对话窗口标题栏里这一份也隐藏。
            面板尺寸仍可用八向拖动调整。
          -->
          <ResizeHandles
            class="head-keys"
            :handles="[]"
            :show-mark="false"
            label="对话窗口尺寸"
            @nudge="nudge"
          />
          <button class="panel-close" type="button" data-im="chat-collapse" @click="store.chatOpen = false">
            收起
          </button>
        </div>
      </header>

      <div class="panel-body">
        <!-- 中间：连续消息流（正式回答持续流式显示；工具与阶段记录默认简短、详情可展开） -->
        <div class="stream-wrap">
          <!--
            整段对话的**唯一主要纵向滚动视口**（policy auto：默认不画指示条，
            滚动 / 靠近边缘 / 键盘使用该区域时才出现；指示条浮在边缘，不占布局列）。
            只声明 y 轴：面板里不存在横向可滚动范围，连指示条也不会为 x 轴而建。
          -->
          <div
            ref="streamRef"
            class="stream"
            data-im="chat-stream"
            data-qio-fit
            v-qio-scrollbar="{ policy: 'auto', axis: 'y' }"
            @scroll="onScroll"
            @wheel.stop="onUserScroll"
          >
            <div ref="contentRef" class="stream-inner" data-qio-fit>

              <p v-if="hiddenText" class="stream-older mono" data-im="chat-older">{{ hiddenText }}</p>
              <MessageItem v-for="message in shownMessages" :key="message.id" :message="message" class="stream-item" />

              <!--
                过程：默认只留一行（这次到哪一步）；工具与阶段记录收进「过程详情」，
                审批条目直接可见（批准前不改动板面）。板面提交的长说明归提交入口。

                它就地在**同一条阅读流**里：不再是面板里另开的一个限高滚动小窗口。
                内容自然展开（content 策略），整段阅读区只由外层 .stream 承担纵向滚动；
                展开后的详情再长也一定滚得到，不会被裁掉。
              -->
              <div
                v-if="processVisible"
                class="process"
                data-im="chat-process"
                data-qio-fit
                :data-process-status="uStatus ?? 'none'"
                :data-process-outcome="uOutcome"
              >
                <p class="process-line" role="status">
                  <strong class="process-label" :class="'tone-' + uText.tone" data-im="chat-process-status">
                    {{ uText.label }}
                  </strong>
                  <span class="process-detail" data-im="chat-process-detail" :title="uText.detail">{{ uText.detail }}</span>
                  <button
                    v-if="hasProcessDetail"
                    class="process-btn ghost"
                    type="button"
                    data-im="chat-process-toggle"
                    :aria-expanded="processOpen"
                    @click="processOpen = !processOpen"
                  >
                    {{ processOpen ? "收起详情" : "过程详情" }}
                  </button>
                </p>
                <p v-if="uCurrent && !processOpen" class="process-step" data-im="chat-process-step">
                  {{ understandingStepText(uCurrent) }}
                </p>
                <div class="process-actions">
                  <button
                    v-if="!submitting && uStatus === 'failed'"
                    class="process-btn"
                    type="button"
                    data-im="chat-process-retry"
                    :disabled="processRefreshing"
                    @click="retryUnderstanding"
                  >
                    {{ processRefreshing ? "重试中…" : "重试理解（从快照恢复）" }}
                  </button>
                  <button
                    v-else-if="!submitting && uLive"
                    class="process-btn"
                    type="button"
                    data-im="chat-process-refresh"
                    :disabled="processRefreshing"
                    @click="refreshUnderstanding"
                  >
                    {{ processRefreshing ? "查询中…" : "刷新理解状态" }}
                  </button>
                </div>
                <template v-if="processOpen">
                  <p v-if="uOutcome === 'clarification'" class="process-note" data-im="chat-process-missing">
                    需要你补充：{{ missingText }}；这次没有生成任何任务。
                  </p>
                  <p v-else-if="uOutcome === 'no_action'" class="process-note" data-im="chat-process-no-action">
                    这次提交没有需要开展的任务：没有生成任何任务，板面保持原样。
                  </p>
                  <ol v-if="uHistory.length" class="process-history" data-im="chat-process-history">
                    <li v-for="step in uHistory" :key="step.key + '-' + step.label" class="process-history-item">
                      {{ understandingStepText(step) }}
                    </li>
                  </ol>
                </template>
                <!-- 审批入口自动展开：批准前不会改动板面 -->
                <div v-if="pendingApprovals.length" class="approval" data-im="chat-approval">
                  <p class="approval-title">待审批：{{ pendingApprovals.length }} 项（批准前不改动板面）</p>
                  <div
                    v-for="intent in pendingApprovals"
                    :key="intent.id"
                    class="approval-row"
                    data-im="chat-approval-item"
                    :data-intent-id="intent.id"
                  >
                    <span class="approval-name">
                      <span v-if="intent.demo" class="approval-demo">演示</span>{{ intent.title }}
                    </span>
                    <button
                      class="process-btn"
                      type="button"
                      data-im="chat-approve"
                      :data-intent-id="intent.id"
                      :disabled="processBusy === intent.id || !approveAvailability(intent).allowed"
                      :title="approveAvailability(intent).text"
                      @click="decideIntent(intent, 'approve')"
                    >
                      {{ processBusy === intent.id ? "处理中…" : intent.demo ? "批准（演示）" : "批准" }}
                    </button>
                    <button
                      class="process-btn ghost"
                      type="button"
                      data-im="chat-reject"
                      :data-intent-id="intent.id"
                      :disabled="processBusy === intent.id || !rejectAvailability(intent).allowed"
                      :title="rejectAvailability(intent).text"
                      @click="decideIntent(intent, 'reject')"
                    >
                      拒绝
                    </button>
                  </div>
                </div>
                <p v-if="processNote" class="process-note" data-im="chat-process-result">{{ processNote }}</p>
              </div>

              <!--
                会话级提示（警告 / 轮次错误 / 失败恢复）：content 策略，和消息流**同一条阅读流**。
                正文自然展开，整块阅读区只保留 .stream 一层纵向滚动。
                警告默认给「简述 + 展开全文」；轮次错误与失败恢复入口始终完整可见 —— 不藏掉重要错误。
              -->
              <div class="panel-notices" data-im="chat-notices" data-qio-fit>
                <p
                  v-if="sessionWarning"
                  class="notice warn"
                  role="status"
                  data-im="chat-warning"
                  :title="warningLong ? '完整提示：' + sessionWarning : undefined"
                >
                  <span class="notice-text">{{ warningText }}</span>
                  <button
                    v-if="warningLong"
                    class="notice-more"
                    type="button"
                    data-im="chat-warning-toggle"
                    :aria-expanded="warningOpen"
                    @click="warningOpen = !warningOpen"
                  >
                    {{ warningOpen ? "收起" : "展开全文" }}
                  </button>
                </p>
                <p v-if="turnError" class="notice err" role="status" data-im="chat-turn-error" data-qio-fit>{{ turnError }}</p>
                <!--
                  失败原因 + 待恢复原文：与对话页共用同一个组件（契约 §11.4），
                  只在原话题出现，不自动写回、不自动重发。原文条数与「查看其余 N 条 / 查看原文」
                  入口由该组件自己给出（默认数量 + 简述 + 展开入口，不占固定高度的小滚动区）。
                -->
                <FailedSendNotice
                  scope="chat"
                  :notice="failureVisible ? failure : ''"
                  :reason="failureVisible ? session.lastError : ''"
                />
              </div>
            </div>
          </div>
          <!--
            悬浮在消息区底部：不占布局行、不压输入区；用户上翻时只提示、不自动拉回。
            消息区底部留出它的高度，最后一屏内容永远能滚到它上面。
          -->
          <!--
            固定入口（钉在阅读区底部，不在滚动内容里，滚不走）：
            一步回到最新；下面还有待审批 / 错误没看见时把话说全，
            而不是只丢一句含糊的「回到最新」。
          -->
          <button
            v-if="!following"
            class="to-latest"
            type="button"
            data-im="chat-latest"
            :title="attentionText ? '下面还有：' + attentionText : '回到最新消息'"
            @click="scrollToBottom(true)"
          >
            回到最新<template v-if="attentionText"> · <span class="to-latest-attention" data-im="chat-latest-attention">{{ attentionText }}</span></template>
          </button>
        </div>
      </div>

      <!-- 底部：附件 + 输入 + 发送合成一个输入区（没有附件时不显示附件条） -->
      <!--
        底部输入区：**独立于阅读流的编辑区**，它是这块区域自己那一层纵向滚动
        （面板被拖到很矮时输入行不会被裁掉、也不需要外层再滚一次）。
        其它区域一律 content：让内容自然展开，不在这里再切小滚动窗口。

        策略是 **hidden**（输入区这一档的既定口径：板面、输入框等）：
        保留全部滚动与定位能力（长输入到上限后仍能纵向滚、光标跟随照旧），
        但**始终不画** QIO 指示条 —— 输入框旁再来一根浮动条既无必要也干扰编辑。
        **不要改回 auto**：auto 属于消息流 / 独立列表 / 浮层正文那一档。
      -->
      <div
        class="composer"
        data-im="chat-composer"
        data-qio-fit
        v-qio-scrollbar="{ policy: 'hidden', axis: 'y' }"
      >
        <div class="attach-tools" data-qio-fit>
          <button
            class="attach-btn"
            type="button"
            data-im="chat-attach-file-btn"
            :disabled="attachBusy"
            @click="pickFile"
          >
            添加附件
          </button>
          <button
            class="attach-btn"
            type="button"
            data-im="chat-attach-path-btn"
            :disabled="attachBusy"
            @click="pickPath"
          >
            粘贴路径
          </button>
          <input
            ref="attachInput"
            class="attach-file-input"
            data-im="chat-attach-file"
            type="file"
            multiple
            aria-label="选择要随这条消息发送的附件"
            @change="onFileInput"
          />
        </div>

        <div v-if="attachPathOpen" class="attach-path-row" data-qio-fit>
          <input
            v-model="attachPath"
            class="path-input"
            data-im="chat-attach-path"
            type="text"
            placeholder="粘贴文件的完整路径（例如 D:\资料\报告.pdf）"
            aria-label="附件路径"
            @keydown.enter.prevent="addPath(attachPath)"
          />
          <button
            class="attach-btn"
            type="button"
            data-im="chat-attach-path-add"
            :disabled="attachBusy || !attachPath.trim()"
            @click="addPath(attachPath)"
          >
            登记这个路径
          </button>
        </div>

        <!-- 选定附件之后才出现；多个附件限高滚动，状态 / 移除 / 重试仍然可达 -->
        <div v-if="pendingAttachments.length" class="attach-strip" data-im="chat-attach-strip" data-qio-fit>
          <p class="attach-total mono" data-im="chat-attach-total">
            {{ pendingAttachments.length }} 个附件<template v-if="attachTotalBytes > 0"> · {{ humanSize(attachTotalBytes) }}</template>
          </p>
          <div class="attach-list" data-qio-fit>
            <div
              v-for="item in shownAttachments"
              :key="item.id"
              class="attach-item"
              data-im="chat-attachment"
              :data-attachment-id="item.id"
            >
              <AttachmentChip
                :attachment="item"
                :busy="attachBusy"
                @remove="removeAttachmentAt(item.id)"
                @retry="retryAttachmentAt(item)"
                @open="openAttachmentAt(item)"
                @relocate="relocateAttachmentAt(item)"
              />
              <button
                class="attach-mini"
                type="button"
                data-im="chat-attachment-remove"
                :aria-label="'从这条消息里移除「' + item.name + '」'"
                @click="removeAttachmentAt(item.id)"
              >
                移除
              </button>
            </div>
          </div>
          <!--
            附件默认只给「数量 + 简述 + 展开入口」：不设一排固定高度的小滚动区。
            展开后完整列表自然铺开，交给下面的输入区（编辑区自己那一层滚动）承载；
            条目、状态、移除 / 重试入口一个都不少，也一条都不删。
          -->
          <button
            v-if="hiddenAttachmentCount > 0"
            class="attach-more"
            type="button"
            data-im="chat-attachments-more"
            :aria-expanded="attachmentsOpen"
            @click="attachmentsOpen = !attachmentsOpen"
          >
            {{ attachmentsOpen ? "收起其余附件" : "展开其余 " + hiddenAttachmentCount + " 个" }}
          </button>
        </div>

        <p v-if="attachHintText" class="attach-note err" role="status" data-im="chat-attach-error" data-qio-fit>
          <span>{{ attachHintText }}</span>
          <button
            v-if="attachRetry && !attachBusy"
            class="attach-mini"
            type="button"
            data-im="chat-attach-retry"
            @click="retryAttach"
          >
            重试这个附件
          </button>
        </p>
        <p v-if="attachNotice" class="attach-note mono" role="status" data-im="chat-attach-note" data-qio-fit>{{ attachNotice }}</p>

        <p v-if="draftSaveError" class="notice err draft-status" role="alert" data-im="chat-draft-status" data-qio-fit>
          <span>{{ draftSaveError }}</span>
          <button class="draft-retry" type="button" data-im="chat-draft-retry" @click="session.retryDraftSave()">
            重试保存
          </button>
        </p>
        <p v-else-if="draftStatusText" class="draft-status mono" role="status" data-im="chat-draft-status" data-qio-fit>
          {{ draftStatusText }}
        </p>

        <!-- 11a：被让位的旧草稿（两份都保留，取回 = 与当前文字互换） -->
        <p v-if="displacedDraft" class="notice displaced" role="status" data-im="chat-displaced-draft" data-qio-fit>
          <span>
            这个话题原来还有一份草稿：{{ displacedSnippet }}
            <template v-if="displacedCount > 1">（另有 {{ displacedCount - 1 }} 份）</template>
          </span>
          <button class="draft-retry" type="button" data-im="chat-displaced-restore" @click="restoreDisplaced">
            取回（互换）
          </button>
          <button class="draft-retry" type="button" data-im="chat-displaced-discard" @click="discardDisplaced">
            放弃这份
          </button>
        </p>
        <p v-if="displacedNotice" class="draft-status mono" role="status" data-im="chat-displaced-status">
          {{ displacedNotice }}
        </p>

        <div class="input-row" data-qio-fit>
          <!-- 权限模式常驻可见（R9 契约 §1）：互动模式与对话页同一枚徽标 -->
          <PermissionModeBadge />
          <textarea
            ref="inputRef"
            v-model="draft"
            class="input"
            data-im="chat-input"
            rows="2"
            aria-label="聊天输入"
            placeholder="和 QIO 说点什么…"
            @keydown.stop="onKeydown"
            @input="autosize"
          ></textarea>
          <button
            class="send"
            type="button"
            data-im="chat-send"
            :disabled="!canSubmitNow"
            :title="session.turnRunning ? '发送（会排队）' : '发送'"
            @click="submit"
          >
            {{ sending ? "发送中…" : "发送" }}
          </button>
        </div>
        <p v-if="blankNotice" class="notice warn" role="status" data-im="chat-blank" data-qio-fit>{{ blankNotice }}</p>

      </div>
    </section>

    <!--
      入口按钮：收起时就地停在窗口右下角（再点开回到原处），展开时挂到面板下边缘之外。
      收起只是不渲染面板，不清消息 / 草稿 / 附件归属。
    -->
    <button
      class="toggle"
      type="button"
      data-im="chat-toggle"
      :aria-expanded="store.chatOpen"
      aria-controls="im-chat-panel"
      @click="store.chatOpen = !store.chatOpen"
    >
      <span class="toggle-mark" aria-hidden="true">◍</span>
      <span>{{ store.chatOpen ? "收起对话" : "对话" }}</span>
      <span v-if="session.turnRunning" class="toggle-live mono" aria-hidden="true">进行中</span>
    </button>
  </div>
</template>

<style scoped>
/*
  外层容器只是**坐标载体**：位置与尺寸来自 frame（视口坐标换算成舞台内的 left/top）。
  它自己绝不接收指针事件 —— 收起时容器仍然是 frame 那么大，挡住板面就等于挡掉整块工作区。
*/
.chat-dock {
  position: absolute;
  left: 0;
  top: 0;
  z-index: var(--im-z-chat, 25);
  pointer-events: none;
  background: transparent;
  /* 正文与操作一律无衬线（三声部）；标题单独用衬线，数据/时间用等宽 */
  font-family: var(--sans);
}

/* 面板：令牌里的高透低雾底色（不是不透明整块，也不做重度模糊），能看见后面的板面 */
.panel {
  position: absolute;
  left: 0;
  top: 0;
  right: 0;
  bottom: 0;
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  padding: var(--sp-3);
  background: var(--glass-bg);
  border: 1px solid var(--glass-border);
  border-radius: var(--r-lg);
  box-shadow: var(--shadow-2);
  /*
    纯裁剪（圆角 + 边界），不需要滚动 → 用 clip 而不是 hidden：
    hidden 仍建立「可程序性滚动」的容器（scrollLeft 能被改，focus / scrollIntoView
    也能把它滚走），clip 完全不建立滚动容器 —— 面板任何层级都没有可滚动范围。
  */
  overflow: clip;
  pointer-events: auto;
}

/*
  拖动中：位置与尺寸必须**跟手**，任何过渡都会变成「追赶动画」（松手才停下）。
  这里显式关掉过渡属性，而不是依赖「现在恰好没有写 transition」。
*/
.panel.resizing,
.chat-dock.moving .panel {
  transition-property: none;
}

/* --- 标题栏（唯一的移动把手） ------------------------------------------- */
.panel-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--sp-1) var(--sp-2);
  min-width: 0;
  flex: none;
  /* 标题条局部稳定底色（§12.7）：既有半实色令牌，明暗两主题自动成立 */
  background: var(--bg-panel);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-sm);
  box-shadow: var(--shadow-1);
  padding: var(--sp-1) var(--sp-2);
  cursor: grab;
  /* 触屏上要先把滚动手势交给指针事件，否则拖动会被浏览器当成滚动吃掉 */
  touch-action: none;
  user-select: none;
}
.chat-dock.moving .panel-head {
  cursor: grabbing;
}
.panel-title {
  flex: none;
  font-size: var(--fs-md);
  font-weight: 600;
  color: var(--text-strong);
}
.panel-status {
  flex: 1 1 6ch;
  min-width: 0;
  font-size: var(--fs-xs);
  color: var(--text-muted);
  /* 单行省略是纯裁剪、不需要滚动 → clip（text-overflow 只在 overflow ≠ visible 时生效） */
  overflow: clip;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.panel-tools {
  flex: none;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--sp-2);
  min-width: 0;
  margin-left: auto;
}
/*
  标题栏里的 −/＋：ResizeHandles 默认是「盖在宿主右下角、鼠标悬停才浮现」，
  放进标题栏就要参与排版并一直可见（它本来就是这里的一个入口）。
*/
.panel-head :deep(.rz-root) {
  position: static;
  inset: auto;
  flex: none;
  pointer-events: auto;
}
.panel-head :deep(.rz-keys) {
  /* 2026-10-14（用户要求）：宽 / 高按钮在互动模式里不出现 —— 仍在 DOM，仅视觉隐藏 */
  display: none;
  position: static;
  opacity: 1;
  pointer-events: auto;
  background: none;
  border: none;
  padding: 0;
}
.panel-close {
  flex: none;
  font: inherit;
  font-size: var(--fs-xs);
  color: var(--link);
  background: none;
  border: none;
  padding: 0;
  cursor: pointer;
}
.panel-close:hover {
  color: var(--accent-hover);
}
.panel-close:focus-visible {
  outline: 2px solid var(--link);
  outline-offset: 2px;
}

/* --- 中间：消息阅读区 ---------------------------------------------------- */
.panel-body {
  flex: 1 1 auto;
  min-height: 0;
  /*
    阅读区只有一个孩子（.stream-wrap）：过程与提示已经并在消息流**里面**，
    所以这里不需要再滚动、也裁不到东西 —— 只作为「不压到输入区」的边界。
    纯裁剪语义 → clip（hidden 会留下一个可程序性滚动的容器）。
  */
  overflow: clip;
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}
.stream-wrap {
  position: relative;
  flex: 1 1 auto;
  /*
    最小尺寸下也要留住消息阅读区：附件 / 帮助 / 错误 / 审批都不许把它压成一行。
    72px ≈ 三行正文（CHAT_FRAME_MIN 的口径）。
  */
  min-height: 72px;
  min-width: 0;
  display: flex;
  flex-direction: column;
}
/*
  整段对话的**唯一主要纵向滚动视口**：消息、过程、提示、失败恢复都在它里面，
  所以同一段阅读流永远不会被切成几个小滚动窗口。最大高度受 .stream-wrap（面板尺寸）约束。
*/
.stream {
  flex: 1 1 auto;
  min-height: 0;
  min-width: 0;
  overflow-y: auto;
  overscroll-behavior: contain;
  display: flex;
  flex-direction: column;
  padding-right: var(--sp-1);
}
.stream-inner {
  /*
    flex: none：内容按自然高度铺开，由外层 .stream 滚动。
    可收缩的话（面板小、内容多）这里会退化成第二个纵向滚动区。
  */
  flex: none;
  display: flex;
  flex-direction: column;
  min-width: 0;
  /* 「回到最新」浮在消息区底部：这里留出它的高度，最后一屏内容永远能滚到它上面 */
  padding-bottom: 28px;
}
/*
  可读性（契约 §10.8）：聊天**外层保持透明**（仍能看见板面），
  但消息与过程说明各自带一层轻底色 —— 板面上有长注释、代码块与虚线预览时，
  文字不能和后方文字混在一起。样式只作用于互动聊天（:deep 限定在 .stream 内），
  普通对话页的 MessageItem 外观不受影响。
*/
.stream-item { max-width: 100%; }
.stream :deep(.message .plain) {
  background: var(--glass-bg-strong);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-sm);
  padding: var(--sp-2) var(--sp-3);
}
.stream :deep(.message.user .plain) {
  /* 用户消息给更强的底色：它是「我说过的话」，最不该被背景吃掉 */
  background: var(--bg-elevated);
  border-color: var(--border-strong);
}
.stream :deep(.message .meta) {
  background: var(--glass-bg);
  border-radius: var(--r-xs);
  padding: 0 var(--sp-1);
  display: inline-flex;
  gap: var(--sp-1);
}
.stream :deep(.tool-card),
.stream :deep(.qio-card) {
  background: var(--glass-bg-strong);
}
.stream :deep(.tool-detail-wrap) {
  background: var(--glass-bg);
}
/* 不同尺寸下正文自然换行；长文件名 / 表格 / 代码不能撑宽面板（代码只在代码区横向滚动） */
.stream :deep(.message),
.stream :deep(.qio-card) {
  min-width: 0;
  overflow-wrap: anywhere;
}
.stream-empty,
.stream-older {
  margin: var(--sp-2) 0;
  font-size: var(--fs-xs);
  color: var(--text-muted);
}
.stream-empty {
  /* 空闲说明也铺在透明区上：给它与 notice 一致的稳定底色，不与板面长文字相叠 */
  background: var(--bg-elevated);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-sm);
  padding: var(--sp-1) var(--sp-2);
  overflow-wrap: anywhere;
}
/*
  更早消息说明（「还有更早的 N 条没有显示」）不是装饰：它告诉用户内容还在、只是没渲染。
  实测亮色主题下 --text-faint 只有 3.27:1（真实像素采样，见 closure-c-verify 报告），
  小字读不清就等于这条信息不存在；改成 --text-muted（亮色实测 4.7:1 以上），
  只动这一处有操作意义的小字，不全局加深装饰元素。
*/
.stream-older { color: var(--text-muted); }
/* 回到最新：悬浮在消息区底部，不单独占布局行，也不压住输入区 */
.to-latest {
  position: absolute;
  left: 50%;
  bottom: var(--sp-2);
  transform: translateX(-50%);
  z-index: 2;
  font: inherit;
  font-size: var(--fs-xs);
  color: var(--text-secondary);
  background: var(--bg-elevated);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-pill);
  padding: 1px var(--sp-3);
  box-shadow: var(--shadow-1);
  cursor: pointer;
}
.to-latest:focus-visible { outline: 2px solid var(--link); outline-offset: 2px; }
/* 还没看见的待办（待审批 / 错误）：说清楚有什么，而不是只藏一句「回到最新」 */
.to-latest-attention { color: var(--warning); }

/*
  提示条也要有稳定的局部底色（契约 §11.8）：板面后方有密集文字或连线时，
  直接铺在透明面板上的小字会与背景混在一起。这里不整块加不透明背景，
  只给提示自己一层轻底色 + 边界。
*/
.notice {
  flex: none;
  margin: 0;
  font-size: var(--fs-xs);
  line-height: 1.5;
  background: var(--bg-elevated);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-sm);
  padding: var(--sp-1) var(--sp-2);
  overflow-wrap: anywhere;
}
.notice.err { color: var(--danger); }
.notice.warn { color: var(--warning); }
/* 提示默认「简述 + 展开入口」：入口本身就是一行可点的文字，不额外占一块高度 */
.notice-more {
  margin-left: var(--sp-2);
  font: inherit;
  font-size: var(--fs-xs);
  color: var(--link);
  background: none;
  border: none;
  padding: 0;
  cursor: pointer;
}
.notice-more:hover { color: var(--accent-hover); }
.notice-more:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
/*
  会话级提示：content 策略 —— 和消息流同一条阅读流（不再限高、不再自己滚一层）。
  提示再多再长，也只是让 .stream 的内容变高；重要错误与失败恢复入口始终完整可见。
*/
.panel-notices {
  flex: none;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
}
/*
  过程（契约 §14.6 / §12.7）：默认一行，详情按需展开；状态与说明要有稳定的局部底色，
  后方板面的长文字不许透上来叠在头上。
*/
/*
  过程：content 策略 —— 它就是阅读流里的一段，不在这里再开一个限高滚动小窗口。
  flex: none 让内容自然展开；展开后的详情由最外层 .stream 滚动，永远滚得到。
*/
.process {
  flex: none;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
  padding: var(--sp-2);
  background: var(--bg-inset);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-sm);
}
.process-line {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: var(--sp-2);
  margin: 0;
  min-width: 0;
  font-size: var(--fs-xs);
  line-height: 1.5;
}
.process-label {
  flex: none;
  font-family: var(--mono);
  color: var(--text-primary);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-xs);
  padding: 0 var(--sp-1);
}
.process-label.tone-open { color: var(--link); border-color: var(--link); }
.process-label.tone-waiting { color: var(--warning); border-color: var(--warning); }
.process-label.tone-active { color: var(--accent); border-color: var(--accent); }
.process-label.tone-settled { color: var(--text-secondary); }
.process-label.tone-problem { color: var(--danger); border-color: var(--danger); }
.process-detail { min-width: 0; color: var(--text-secondary); overflow-wrap: anywhere; }
.process-step,
.process-note {
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--text-muted);
  line-height: 1.5;
  overflow-wrap: anywhere;
}
.process-note { color: var(--text-secondary); }
.process-actions { display: flex; flex-wrap: wrap; gap: var(--sp-1); }
.process-btn {
  font: inherit;
  font-size: var(--fs-xs);
  color: var(--text-primary);
  background: var(--bg-elevated);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-xs);
  padding: 1px var(--sp-2);
  cursor: pointer;
}
.process-btn.ghost { background: none; color: var(--link); }
.process-btn:disabled { opacity: 0.55; cursor: default; }
.process-btn:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
.process-history { margin: 0; padding-left: var(--sp-4); display: flex; flex-direction: column; gap: 2px; }
.process-history-item { font-size: var(--fs-xs); color: var(--text-faint); line-height: 1.5; overflow-wrap: anywhere; }
/* 审批入口自动展开（批准前不改动板面），窄窗口里每行可换行 */
.approval {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
  border-top: 1px solid var(--border-subtle);
  padding-top: var(--sp-1);
}
.approval-title { margin: 0; font-size: var(--fs-xs); color: var(--text-secondary); }
.approval-row { display: flex; align-items: center; gap: var(--sp-1); flex-wrap: wrap; }
.approval-name {
  flex: 1 1 12ch;
  min-width: 0;
  font-size: var(--fs-xs);
  color: var(--text-primary);
  overflow-wrap: anywhere;
}
.approval-demo {
  margin-right: var(--sp-1);
  font-size: var(--fs-xs);
  border-radius: var(--r-pill);
  padding: 0 var(--sp-1);
  border: 1px solid var(--warning);
  color: var(--warning);
  background: var(--warning-soft);
}

/* --- 底部：附件 + 输入 + 发送（一个输入区） ------------------------------ */
.composer {
  flex: 0 1 auto;
  /*
    面板变矮时**先让位的是阅读区，不是输入行**（2026-10-14 修）。
    附件行 31 + gap 4 + 输入行 57 ≈ 92 → 取 96 留余量。
    原来这里是 min-height: 0：短窗口（1200×560）下面板被收敛到 238px 时，输入行会被推到
    面板底边之外，再由 .panel 的 overflow: clip 裁掉 —— 发送键完全看不见、也没有任何入口能滚到它。
    帮助摘要（51px）在这之后才让位：顶部另有「帮助」入口，不会因此失去访问路径。
  */
  min-height: 96px;
  /* 输入区内容过多时自己滚动，而不是把消息阅读区顶掉 */
  overflow-y: auto;
  overscroll-behavior: contain;
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
}
.attach-tools,
.attach-path-row {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  flex-wrap: wrap;
  flex: none;
}
.attach-btn {
  font: inherit;
  font-size: var(--fs-sm);
  font-family: var(--sans);
  color: var(--text-primary);
  background: var(--bg-elevated);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-sm);
  padding: var(--sp-1) var(--sp-2);
  cursor: pointer;
}
.attach-btn:hover { background: var(--accent-soft); }
.attach-btn:focus-visible { outline: 2px solid var(--link); outline-offset: 2px; }
.attach-btn:disabled { color: var(--text-faint); cursor: not-allowed; }
.attach-file-input {
  position: absolute;
  width: 1px;
  height: 1px;
  /* 视觉隐藏但键盘可达：用 clip —— 它不该是一个「看不见但还能滚」的盒子 */
  overflow: clip;
  clip: rect(0 0 0 0);
  white-space: nowrap;
}
.attach-path-row .path-input {
  flex: 1;
  min-width: 0;
  font: inherit;
  font-size: var(--fs-sm);
  font-family: var(--mono);
  color: var(--text-primary);
  background: var(--bg-inset);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-sm);
  padding: var(--sp-1) var(--sp-2);
}
/* 附件条：选定之后才出现（默认给数量 + 简述 + 展开入口，完整列表按需展开） */
.attach-strip {
  flex: none;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.attach-total {
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--text-muted);
}
/*
  完整列表：content 策略 —— 不设固定高度、也不再自己滚一层。
  展开后由下面的输入区（编辑区那一层）承载滚动，状态 / 移除 / 重试入口都在条目里。
*/
.attach-list {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
  min-width: 0;
}
.attach-more {
  align-self: flex-start;
  font: inherit;
  font-size: var(--fs-xs);
  color: var(--link);
  background: none;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-sm);
  padding: 0 var(--sp-2);
  cursor: pointer;
}
.attach-more:hover { color: var(--accent-hover); border-color: var(--link); }
.attach-more:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 1px; }
.attach-note {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  flex-wrap: wrap;
  margin: 0;
  font-size: var(--fs-sm);
  color: var(--text-secondary);
  overflow-wrap: anywhere;
}
.attach-note.err { color: var(--danger); }
.attach-mini {
  font: inherit;
  font-size: var(--fs-xs);
  color: var(--link);
  background: none;
  border: none;
  padding: 0;
  cursor: pointer;
}

/* 草稿状态：贴着输入区的一行小字，不抢消息区的空间 */
.draft-status {
  flex: none;
  margin: 0;
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: var(--sp-2);
  font-size: var(--fs-xs);
  line-height: 1.5;
  /* 草稿状态是有操作意义的（它决定用户敢不敢关页面）：必须过小字可读线，不能当装饰色。
     亮色 --text-faint 实测 3.27:1 → 改为 --text-muted（迁移后实测 ≥4.7:1）。 */
  color: var(--text-muted);
  /* 草稿状态行同样贴着透明区铺字：给它与 notice 一致的稳定局部底色（§12.7） */
  background: var(--bg-elevated);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-sm);
  padding: var(--sp-1) var(--sp-2);
}
.draft-status.err { color: var(--danger); }
.draft-retry {
  flex: none;
  font: inherit;
  font-size: var(--fs-xs);
  color: var(--link);
  background: none;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-sm);
  padding: 0 var(--sp-2);
  cursor: pointer;
}
.draft-retry:hover { border-color: var(--link); }
.draft-retry:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }

.input-row {
  flex: none;
  display: flex;
  align-items: flex-end;
  gap: var(--sp-2);
  min-width: 0;
}
/*
  输入框自己的滚动：高度由 autosize() 跟着内容长，长到 MAX_INPUT_PX(132) 为止 ——
  空 / 短内容根本不产生滚动范围；到上限后只在输入框内部**纵向**滚。
  原生滚动条外观由全局 styles/scroll.css 统一消除，这里不自己声明任何滚动条样式。
*/
.input {
  flex: 1;
  min-width: 0;
  min-height: 40px;
  max-height: 132px;
  resize: none;
  font: inherit;
  font-family: var(--sans);
  font-size: var(--fs-base);
  line-height: var(--lh-tight);
  color: var(--text-strong);
  /*
    输入框用**稳定的局部底色**（契约 §11.8）：面板本身仍是透明玻璃，
    但正在写字的这一块不能和后方板面文字混在一起 —— 半透明渐变在密集
    注释/连线上会把光标前后的字吃掉。
  */
  background: var(--bg-elevated);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-md);
  padding: var(--sp-2) var(--sp-3);
}
/* 占位说明是「这里可以输入什么」的唯一提示：同样提到可读色，不做装饰处理 */
.input::placeholder { color: var(--text-muted); }
.input:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
.send {
  flex: none;
  font: inherit;
  font-size: var(--fs-sm);
  color: var(--on-accent);
  background: var(--accent);
  border: none;
  border-radius: var(--r-sm);
  padding: var(--sp-2) var(--sp-4);
  cursor: pointer;
}
.send:hover:not(:disabled) { background: var(--accent-hover); }
.send:disabled { opacity: 0.5; cursor: default; }
.send:focus-visible { outline: 2px solid var(--link); outline-offset: 2px; }

/*
  入口按钮：**固定**在窗口右下角（2026-10-14 用户要求）—— 展开 / 收起、面板拖到哪都不动。
  原来用 absolute + right/bottom: 0，是相对外层坐标载体定位的：面板一挪，按钮跟着挪。
*/
.toggle {
  position: fixed;
  right: var(--sp-4);
  bottom: var(--sp-4);
  display: inline-flex;
  align-items: center;
  gap: var(--sp-2);
  font: inherit;
  font-size: var(--fs-sm);
  font-family: var(--sans);
  color: var(--on-accent);
  background: var(--accent);
  border: 1px solid var(--accent);
  border-radius: var(--r-pill);
  padding: var(--sp-2) var(--sp-4);
  cursor: pointer;
  pointer-events: auto;
  box-shadow: var(--shadow-1);
}
/* 面板展开时入口不盖面板：挂到它的下边缘之外（默认框已经为它留了位置） */
/*
  2026-10-14（用户要求）：按钮**固定**在窗口右下角 —— 展开时不再挂到面板下边缘。
  原来这里把 open 状态改成 top: calc(100% + …)：配合 fixed 定位，按钮直接掉到视口下方 43px。
*/
.toggle:hover { background: var(--accent-hover); }
.toggle:focus-visible { outline: 2px solid var(--link); outline-offset: 2px; }
.toggle-mark { font-size: var(--fs-base); line-height: 1; }
.toggle-live {
  font-size: var(--fs-xs);
  color: var(--on-accent);
  border-left: 1px solid var(--on-accent);
  padding-left: var(--sp-2);
}

/*
  矮窗口（480×600 这类浏览器边缘档、以及 800×600 的桌面最小窗口）：
  输入框不必保留整块最大高度 —— 面板里还要同时容下过程 / 提示与发送按钮。
  （帮助正文已改成 content 策略，不再有限高需要在这里收紧。）
*/
@media (max-height: 700px) {
  .input { max-height: 84px; }
}
@media (max-width: 560px) {
  /* 窄窗口：面板宽度已经由 frame 收敛，这里只收紧内部留白 */
  .panel { padding: var(--sp-2); }
  .panel-head { padding: var(--sp-1); }
}
@media (prefers-reduced-motion: reduce) {
  .panel.resizing,
  .chat-dock.moving .panel {
    transition: none;
  }
}
</style>