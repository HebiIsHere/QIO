<!-- 互动板面画布（子智能体 B 负责）：卡片、组框、关系连线、平移 / 空格框选 / 滚轮缩放 / 连接点拖线。

  契约：docs/interactive-mode-contract.md §1.2 / §1.3 / §4.3 / §8.2。
  这里落地的硬规则：
  1. 拖动期间只预演（previewDrop），松手才 dropCard 并提交；Esc / 指针中断丢弃本地拖动状态即可，
     因为板面状态从头到尾没被改过，卡片自然回到操作前位置；没产生变化的放下不提交（不留下无意义改动）。
  2. 组件绝不直接改 store.board：所有变化都算成 next 之后调 store.commit(next, 中文说明)。
  3. 位置只影响显示，不构成意图依据；链接方向只表示用户写明的方向，不推断因果 / 先后。
  4. 平移 / 缩放只是**查看状态**（不形成表达、不调用 QIO、不触发保存）：
     - 拖动板面空白处 = 平移；空格 + 拖动空白处 = 框选卡片；
     - 滚轮以指针附近为缩放中心；聊天 / 列表 / 代码区等可滚动内容优先滚动自身，不穿透成板面缩放；
     - 所有坐标换算统一走 interactive/viewport.ts，保证缩放平移后卡片拖动 / 框选 / 连线 / 工具栏都准确。
  5. 卡片局部工具栏与连接点由 BoardCard 渲染；重叠成组的提示与判定走 board.ts 的 bestMergeTarget。
  6. 改版后底部工具栏与板内搜索由页面壳与 A 的组件渲染：本文件不再引用 <BoardToolbar /> 与 <BoardSearchPanel />。
-->
<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from "vue";
import { useInteractiveStore } from "../../stores/interactive";
import { cardDraftKey } from "../../interactive/drafts";
import {
  addLink,
  bestMergeTarget,
  cardById,
  cardRect,
  CARD_KIND_LABELS,
  createGroup,
  dissolveGroup,
  dropCard,
  duplicateCard,
  groupById,
  groupOfCard,
  joinGroup,
  mergeGroups,
  normalizeState,
  pointInRect,
  previewDrop,
  removeCard,
  removeFromGroup,
  removeLink,
  renameGroup,
  selectInRect,
  setGroupOrdered,
  setSelection,
  updateCard,
  updateLink,
  type DropPreview,
} from "../../interactive/board";
import {
  clampViewport,
  effectiveRect,
  clampScroll,
  IDENTITY_VIEWPORT,
  rectFromDrag,
  toBoardPoint,
  toScreenPoint,
  wheelDeltaY,
  wheelZoomFactor,
  zoomAtScroll,
  type DOMRectLike,
  type Viewport,
} from "../../interactive/viewport";
import type { BoardCard as BoardCardModel, BoardState, CardKind, Intent } from "../../interactive/types";
import BoardCard from "./BoardCard.vue";
import BoardGroupFrame from "./BoardGroupFrame.vue";
import BoardLinkLayer from "./BoardLinkLayer.vue";
import BoardPreviewLayer from "./BoardPreviewLayer.vue";

/** 板面坐标系大小：卡片位置是板面状态，平移 / 缩放只是查看方式。 */
const SURFACE_W = 2400;
const SURFACE_H = 1600;

/** 工具栏 / 提示与卡片之间的间距（屏幕像素）。 */
const TOOLBAR_GAP = 10;
/** 卡片局部工具栏的估算高度（用于判断放在卡片上方还是下方）。 */
const TOOLBAR_H = 34;
const HINT_GAP = 8;

type GroupOp = "form" | "join" | "leave" | "dissolve" | "ordered" | "unordered" | "merge";

interface DragState {
  cardId: string;
  offsetX: number;
  offsetY: number;
  x: number;
  y: number;
  preview: DropPreview;
  fromGroupId: string | null;
  /** 是否真的移动过（超过 3px）：纯单击只是选中，松手时不许判落点、不许成组、不许保存 */
  moved: boolean;
  /** 按下时的客户端坐标，用来判断有没有真的移动 */
  startClient: { x: number; y: number };
}

interface PanState {
  startClient: { x: number; y: number };
  startScroll: { x: number; y: number };
  moved: boolean;
}

interface RectDrag {
  startClient: { x: number; y: number };
  currentClient: { x: number; y: number };
  additive: boolean;
  moved: boolean;
}

interface LinkDraft {
  fromId: string;
  point: { x: number; y: number } | null;
  targetId: string | null;
}

const store = useInteractiveStore();
const boardState = computed<BoardState | null>(() => store.board);

const surface = ref<HTMLElement | null>(null);
const viewportEl = ref<HTMLElement | null>(null);
const shell = ref<HTMLElement | null>(null);
/**
 * 查看状态：**缩放 + 滚动**。
 * 内容用 transform: scale() 缩放，平移查看位置就是容器的 scrollLeft / scrollTop
 * （两者叠加会让坐标算错：滚动会改变内容原点在屏幕上的位置）。
 * 滚动量用 ref 保存，作为坐标换算的**唯一事实来源**（不依赖浏览器取整后的 scrollLeft）。
 */
const view = ref<Viewport>({ ...IDENTITY_VIEWPORT });
const scroll = ref({ x: 0, y: 0 });

const dragging = ref<DragState | null>(null);
const panning = ref<PanState | null>(null);
const rectSelect = ref<RectDrag | null>(null);
const linkDraft = ref<LinkDraft | null>(null);
const spaceDown = ref(false);
const editingLinkId = ref<string | null>(null);
const linkMeaning = ref("");
const linkDirection = ref(false);
const notice = ref("");
const highlightId = ref<string | null>(null);
const locatedIntentId = ref<string | null>(null);
/** 工具栏 / 提示的浮层位置（板面容器坐标） */
const overlay = ref({ left: 0, top: 0, visible: false });
let highlightTimer: ReturnType<typeof setTimeout> | null = null;
let locatedTimer: ReturnType<typeof setTimeout> | null = null;
let scrollSyncLock = false;

/**
 * 手势是固定的（契约 §10.7），画布不再有「指针模式」：
 * 不按空格拖空白＝平移；空格＋拖空白＝框选；从连接点拖线＝建立关系。
 * 以前工具栏的常驻模式会改变同一个手势的含义（选了「框选」后不按空格也框选），已移除。
 */

/** 仍在等待的意图：它们的虚线预览要一直画在真实板面上（未确定 ≠ 已确定结论）。 */
const OPEN_INTENT_STATUSES = ["pending", "needs_update", "waiting_dependency", "waiting_confirm", "running"];

const previewIntents = computed<Intent[]>(() =>
  store.intents.filter((item) => OPEN_INTENT_STATUSES.includes(item.status) && item.preview),
);

const liveCards = computed(() => (boardState.value?.cards ?? []).filter((card) => !card.deleted));
const groups = computed(() => boardState.value?.groups ?? []);
const links = computed(() => boardState.value?.links ?? []);
const selection = computed(() => boardState.value?.selection ?? []);

const selectedGroupIds = computed(() => {
  const current = boardState.value;
  if (!current) return [] as string[];
  const ids = new Set<string>();
  for (const cardId of current.selection) {
    const group = groupOfCard(current, cardId);
    if (group) ids.add(group.id);
  }
  return [...ids];
});

function cardSummary(card: BoardCardModel | null | undefined): string {
  if (!card) return "（卡片）";
  const text = (card.content || String(card.meta?.name ?? card.meta?.title ?? "")).trim();
  if (!text) return CARD_KIND_LABELS[card.kind];
  return text.length > 14 ? text.slice(0, 14) + "…" : text;
}

function groupIdOf(cardId: string): string | null {
  const current = boardState.value;
  if (!current) return null;
  const group = groupOfCard(current, cardId);
  return group ? group.id : null;
}

function groupNameOf(cardId: string): string | null {
  const current = boardState.value;
  if (!current) return null;
  const group = groupOfCard(current, cardId);
  return group ? group.name : null;
}

function displayX(card: BoardCardModel): number {
  const drag = dragging.value;
  return drag && drag.cardId === card.id ? drag.x : card.x;
}

function displayY(card: BoardCardModel): number {
  const drag = dragging.value;
  return drag && drag.cardId === card.id ? drag.y : card.y;
}

/**
 * **显示用**卡片列表：位置按拖动中的实时坐标替换。
 *
 * 为什么需要：板面状态在松手前不变（只预演），所以 store 里的坐标是静止的。
 * 卡片自己用 displayX/displayY 拿到了实时坐标，但**关系线与组框**是另外的图层，
 * 它们只拿到 store 的卡片 —— 于是拖动时线还挂在旧位置上，松手（提交）才跟着跳过去。
 * 这两个图层是纯显示、不参与落点判定，所以给它们这一份实时坐标即可。
 */
const displayCards = computed<BoardCardModel[]>(() => {
  const drag = dragging.value;
  if (!drag || !drag.moved) return liveCards.value;
  return liveCards.value.map((card) =>
    card.id === drag.cardId ? { ...card, x: drag.x, y: drag.y } : card,
  );
});

function commit(next: BoardState, label: string) {
  store.commit(next, label);
}

// --- 视口：平移 / 缩放 / 坐标换算 -----------------------------------------

const surfaceStyle = computed(() => ({
  width: SURFACE_W * view.value.scale + "px",
  height: SURFACE_H * view.value.scale + "px",
  transform: `scale(${view.value.scale})`,
  transformOrigin: "0 0",
}));

const contentStyle = computed(() => ({
  width: SURFACE_W * view.value.scale + "px",
  height: SURFACE_H * view.value.scale + "px",
}));

/** 板面内容原点在屏幕上的位置（含滚动量）—— 所有坐标换算都用它。 */
/**
 * 真实滚动量：DOM 是最终事实（用户拖滚动条、浏览器自动滚动都会改它）。
 * 只在容器**真的能滚动**时采用 DOM 值：jsdom 没有真实布局，scrollLeft 会被钳制，
 * 无条件采用反而会让坐标换算失真（单元测试里就是这种情况）。
 */
function liveScroll(): { x: number; y: number } {
  const element = viewportEl.value;
  if (!element) return { x: scroll.value.x, y: scroll.value.y };
  const canScrollX = element.scrollWidth > element.clientWidth + 1;
  const canScrollY = element.scrollHeight > element.clientHeight + 1;
  const x = canScrollX ? element.scrollLeft : scroll.value.x;
  const y = canScrollY ? element.scrollTop : scroll.value.y;
  if (x !== scroll.value.x || y !== scroll.value.y) {
    scroll.value = { x, y };
    view.value = { ...view.value, x: -x, y: -y };
  }
  return { x, y };
}

function surfaceRect(): DOMRectLike {
  const element = viewportEl.value;
  if (!element) return { left: 0, top: 0 };
  const rect = typeof element.getBoundingClientRect === "function"
    ? element.getBoundingClientRect()
    : { left: 0, top: 0 };
  const live = liveScroll();
  return effectiveRect(rect, live.x, live.y);
}

function boardPointOf(event: { clientX: number; clientY: number }): { x: number; y: number } {
  return toBoardPoint(view.value, { x: event.clientX, y: event.clientY }, surfaceRect());
}

function containerSize(): { w: number; h: number } {
  const element = viewportEl.value;
  if (!element) return { w: SURFACE_W, h: SURFACE_H };
  return { w: element.clientWidth || SURFACE_W, h: element.clientHeight || SURFACE_H };
}

/** 把滚动量写回容器（滚动量本身已经是坐标换算的事实来源）。 */
function applyScroll(next: { x: number; y: number }) {
  scroll.value = clampScroll(next, view.value.scale, containerSize(), { w: SURFACE_W, h: SURFACE_H });
  const element = viewportEl.value;
  if (!element) return;
  // 浏览器不接受负滚动量：以 DOM 为准回写，保证坐标换算与真实显示一致
  element.scrollLeft = scroll.value.x;
  element.scrollTop = scroll.value.y;
  scroll.value = { x: element.scrollLeft, y: element.scrollTop };
  scrollSyncLock = true;
  void nextTick(() => {
    scrollSyncLock = false;
  });
}

/**
 * 用户直接拖动滚动条 / 触控板滚动时，把滚动量同步进 ref。
 * 只在容器**真的能滚动**时同步：jsdom 没有真实布局，scrollLeft 会被浏览器取整或钳制，
 * 无条件同步反而会让坐标换算失真。
 */
function onViewportScroll() {
  if (scrollSyncLock) return;
  const element = viewportEl.value;
  if (!element) return;
  const canScrollX = element.scrollWidth > element.clientWidth + 1;
  const canScrollY = element.scrollHeight > element.clientHeight + 1;
  if (!canScrollX && !canScrollY) return;
  if (canScrollX) scroll.value = { ...scroll.value, x: element.scrollLeft };
  if (canScrollY) scroll.value = { ...scroll.value, y: element.scrollTop };
  view.value = { ...view.value, x: -scroll.value.x, y: -scroll.value.y };
}

/** 以指针附近为缩放中心：同时收敛缩放与滚动，指针下的板面点保持不动。 */
function zoomAtPointer(factor: number, client: { x: number; y: number }) {
  const result = zoomAtScroll(
    liveScroll(),
    view.value.scale,
    factor,
    client,
    surfaceRect(),
    containerSize(),
    { w: SURFACE_W, h: SURFACE_H },
  );
  view.value = { scale: result.scale, x: -result.scroll.x, y: -result.scroll.y };
  applyScroll(result.scroll);
}

/**
 * 浮层（局部工具栏 / 合并提示）定位：板面坐标 → **offsetParent（.board-surface）坐标**。
 *
 * 独立复核定位到的根因：工具栏挂在 `.board-surface` 里（它是 position:relative 的 offsetParent），
 * 行内 `left/top` 是**相对 surface** 的；而这里原来按 shell 坐标算，
 * 两个坐标系相差 `surface.top − shell.top`（实测 1440×900 下 55px），
 * 于是工具栏每次都比预期低 55px、压在卡片顶边上，把画在卡片内侧的顶部连接点整块盖住
 * （命中测试命中的是 card-edit / card-fold，不是连接点）。
 *
 * 现在统一成：**先用客户端坐标算好，最后一步再减去 surface 的位置**转成 offsetParent 坐标。
 * 判定里保留「量真实高度 + 夹取后复验」：工具栏只要会碰到卡片顶边就一律挪到卡片下方。
 */
function placeOverlay(rect: { x: number; y: number; w: number; h: number }) {
  const element = viewportEl.value;
  const shellElement = shell.value;
  const surfaceElement = surface.value;
  if (!element || !shellElement || !surfaceElement) {
    overlay.value = { left: 0, top: 0, visible: false };
    return;
  }
  const viewRect = element.getBoundingClientRect();
  const surfaceNow = surfaceElement.getBoundingClientRect();
  const screen = toScreenPoint(view.value, { x: rect.x, y: rect.y }, surfaceRect());
  const cardTopClient = screen.y;
  const cardBottomClient = cardTopClient + rect.h * view.value.scale;
  const limitTopClient = viewRect.top + 2;
  const maxTopClient = viewRect.bottom - 30;
  const toolbarEl = shellElement.querySelector('[data-im="card-toolbar"]');
  const toolbarH = toolbarEl ? Math.max(1, Math.round(toolbarEl.getBoundingClientRect().height)) : TOOLBAR_H;
  // 工具栏宽度也要量：只夹左边缘的话，窄窗口里右侧按钮会被视口裁掉（复核实测 800×600 下超出 74px）
  const toolbarW = toolbarEl ? Math.max(1, Math.round(toolbarEl.getBoundingClientRect().width)) : 420;
  const aboveTopClient = cardTopClient - toolbarH - TOOLBAR_GAP;
  const belowTopClient = Math.min(maxTopClient, cardBottomClient + TOOLBAR_GAP);
  const touchesCardTop = (value: number) => value + toolbarH > cardTopClient - 2 && value < cardTopClient;
  let topClient = aboveTopClient >= limitTopClient ? aboveTopClient : belowTopClient;
  if (touchesCardTop(topClient)) topClient = belowTopClient;
  topClient = Math.max(limitTopClient, topClient);
  if (touchesCardTop(topClient)) topClient = Math.max(limitTopClient, cardBottomClient + TOOLBAR_GAP);
  // 卡片被拖出可视区时不再显示浮层（但状态仍然保留）
  const visible =
    screen.x + rect.w * view.value.scale > viewRect.left - 40 &&
    screen.x < viewRect.right + 40 &&
    screen.y + rect.h * view.value.scale > viewRect.top - 40 &&
    screen.y < viewRect.bottom + 40;
  const leftClient = Math.max(viewRect.left + 4, Math.min(viewRect.right - toolbarW - 4, screen.x));
  overlay.value = {
    // 行内 left/top 是相对 offsetParent（.board-surface）的：最后一步统一减去它的位置
    left: leftClient - surfaceNow.left,
    top: topClient - surfaceNow.top,
    visible,
  };
}

// --- 拖动：预演 → 放下 / 中断 ---------------------------------------------

function onDragStart(cardId: string, event: PointerEvent) {
  const current = boardState.value;
  if (!current) return;
  const card = cardById(current, cardId);
  if (!card) return;
  const point = boardPointOf(event);
  const from = groupOfCard(current, cardId);
  dragging.value = {
    cardId,
    offsetX: point.x - card.x,
    offsetY: point.y - card.y,
    x: card.x,
    y: card.y,
    preview: previewDrop(current, cardId, card.x, card.y),
    fromGroupId: from ? from.id : null,
    // 是否真的移动过：纯单击（用来选中卡片）**不是**拖动，松手时不许判落点、不许成组、不许保存
    moved: false,
    startClient: { x: event.clientX, y: event.clientY },
  };
  panning.value = null;
  notice.value = "";
  attachPointerListeners();
}

function onPointerMove(event: PointerEvent) {
  const current = boardState.value;
  const pan = panning.value;
  if (pan) {
    const dx = event.clientX - pan.startClient.x;
    const dy = event.clientY - pan.startClient.y;
    if (Math.abs(dx) > 3 || Math.abs(dy) > 3) pan.moved = true;
    applyScroll({ x: pan.startScroll.x - dx, y: pan.startScroll.y - dy });
    view.value = { ...view.value, x: -scroll.value.x, y: -scroll.value.y };
    return;
  }
  const drag = dragging.value;
  if (drag && current) {
    // 与平移/框选一致：超过 3px 才算拖动；在此之前不移动卡片、不预演落点
    if (!drag.moved && (Math.abs(event.clientX - drag.startClient.x) > 3 || Math.abs(event.clientY - drag.startClient.y) > 3)) {
      drag.moved = true;
    }
    if (!drag.moved) return;
    const point = boardPointOf(event);
    const x = point.x - drag.offsetX;
    const y = point.y - drag.offsetY;
      drag.x = x;
    drag.y = y;
    // 只预演：板面状态在松手前不变
    drag.preview = previewDrop(current, drag.cardId, x, y);
    const card = cardById(current, drag.cardId);
    if (card) placeOverlay({ x, y, w: card.w, h: card.folded ? 60 : card.h });
    return;
  }
  const rect = rectSelect.value;
  if (rect) {
    rect.currentClient = { x: event.clientX, y: event.clientY };
    if (Math.abs(event.clientX - rect.startClient.x) > 3 || Math.abs(event.clientY - rect.startClient.y) > 3) {
      rect.moved = true;
    }
    return;
  }
  const draft = linkDraft.value;
  if (draft) {
    draft.point = boardPointOf(event);
    draft.targetId = hitCardAt(draft.point, draft.fromId);
  }
}

function onPointerUp() {
  const current = boardState.value;
  const drag = dragging.value;
  if (drag && current) {
    detachPointerListeners();
    dragging.value = null;
    if (!drag.moved) {
      // 纯单击：只当作选中（选中逻辑在 pointerdown 里已经处理），板面**不产生任何改动**
      refreshOverlay();
      return;
    }
    const result = dropCard(current, drag.cardId, drag.x, drag.y);
    const formed = !drag.fromGroupId && Boolean(result.groupId);
    const label = dragLabel(drag, result.groupId, result.merged, formed);
    // 时间戳每次都不同，不能直接比较 JSON：只比较**真实含义**（位置 / 成员 / 顺序 / 选择 / 链接 / 勾选等）
    const changed = stateDiffers(normalizeState(current), result.state);
    if (changed) {
      commit(result.state, label);
      notice.value =
        label + "：" + (result.groupId ? "已放入组（板面会自动保存，不会调用 QIO）。" : "只改变位置（位置不构成意图依据）。");
    } else {
      notice.value = "卡片没有移动：板面没有产生改动，也没有保存。";
    }
    refreshOverlay();
    return;
  }
  const pan = panning.value;
  if (pan) {
    detachPointerListeners();
    panning.value = null;
    // 空白处单击（没有拖动）＝ 取消选择
    if (!pan.moved && current && current.selection.length) {
      commit(setSelection(current, []), "清空选择");
    }
    return;
  }
  const rect = rectSelect.value;
  if (rect && current) {
    rectSelect.value = null;
    detachPointerListeners();
    const area = rectFromDrag(view.value, rect.startClient, rect.currentClient, surfaceRect());
    if (area.w > 4 || area.h > 4) {
      commit(selectInRect(current, area, rect.additive), "框选卡片");
      notice.value = "已框选 " + area.w.toFixed(0) + "×" + area.h.toFixed(0) + " 板面范围内的卡片。";
    }
    return;
  }
  const draft = linkDraft.value;
  if (draft) {
    finishLinkDraft(true);
  }
}

/** Esc / 指针中断 / 窗口失焦：不提交，卡片回到操作前位置。 */
function onDragCancel() {
  detachPointerListeners();
  const hadDrag = Boolean(dragging.value);
  const hadRect = Boolean(rectSelect.value);
  const hadPan = Boolean(panning.value);
  dragging.value = null;
  rectSelect.value = null;
  panning.value = null;
  if (linkDraft.value) {
    linkDraft.value = null;
    notice.value = "已取消建立关系：没有保存半条链接。";
    return;
  }
  if (hadDrag) notice.value = "已取消拖动：卡片回到操作前的位置，板面没有改动。";
  else if (hadRect) notice.value = "已取消框选：选择范围没有改变。";
  else if (hadPan) notice.value = "已停止平移查看位置（查看位置不属于板面改动）。";
}

/**
 * 两次板面状态在**含义**上是否有差别（忽略 updatedAt 这类每次都变的时间戳）。
 * 用于「松手后什么都没变」时不提交，避免留下无意义的改动与撤销记录。
 */
function stateDiffers(before: BoardState, after: BoardState): boolean {
  const strip = (value: unknown) => JSON.stringify(value, (key, item) => (key === "updatedAt" || key === "createdAt" ? "" : item));
  return strip(before) !== strip(after);
}

function dragLabel(drag: DragState, groupId: string | null, merged: boolean, formed: boolean): string {
  if (merged) return "合并组";
  if (groupId && drag.fromGroupId === groupId) return "调整组内顺序";
  if (groupId && drag.fromGroupId) return "换组";
  if (groupId) return "加入组";
  if (formed) return "两张卡片重叠成组";
  if (drag.fromGroupId) return "移出组";
  return "移动卡片";
}

function dragHintText(): string {
  const drag = dragging.value;
  const current = boardState.value;
  if (!drag || !current) return "";
  const preview = drag.preview;
  if (preview.groupId) {
    const target = groupById(current, preview.groupId);
    const name = target ? target.name : "组";
    if (preview.mergesWith) {
      const source = groupById(current, preview.mergesWith);
      return (
        "将把「" +
        (source ? source.name : "原组") +
        "」并入「" +
        name +
        "」，整组连续插入第 " +
        ((preview.index ?? 0) + 1) +
        " 位；合并后的新组用默认名。"
      );
    }
    if (target && target.ordered) {
      return "将加入「" + name + "」，插入第 " + ((preview.index ?? 0) + 1) + " 位（后续序号自动更新）。";
    }
    return "将加入「" + name + "」（普通组：摆放顺序不代表先后）。";
  }
  if (preview.mergesWith) {
    const other = cardById(current, preview.mergesWith);
    return "将与「" + cardSummary(other) + "」自动成组（默认组名「默认组名」）。";
  }
  if (drag.fromGroupId) return "松手后移出组（自由摆放，组空了会自动消失）。";
  return "松手后只移动位置：位置不构成意图依据。";
}

const dragHint = computed(dragHintText);

/**
 * 明确的成组目标：只有**明确重叠**才提示「松开后合并成组」，松手才成组。
 * 靠近、边框相碰都不提示（判定见 board.ts 的 MERGE_MIN_AREA_RATIO）。
 */
const mergeTarget = computed(() => {
  const drag = dragging.value;
  const current = boardState.value;
  if (!drag || !current) return null;
  // 已经会落入某个组（含两组相撞合并）时，用组框上的提示，不再叠一条「合并成组」
  if (drag.preview.groupId) return null;
  if (drag.fromGroupId) return null;
  // 与 previewDrop 的 mergesWith 用同一个判定：达到阈值才算「明确重叠」
  return bestMergeTarget(current, drag.cardId, drag.x, drag.y);
});

const mergeHintText = computed(() => {
  const target = mergeTarget.value;
  const current = boardState.value;
  if (!target || !current) return "";
  const card = cardById(current, target.cardId);
  const percent = Math.round(target.ratio * 100);
  return "松开后合并成组：与「" + cardSummary(card) + "」明确重叠（覆盖 " + percent + "%），松手才成组。";
});

/** 合并提示浮在目标卡片上方（data-im="group-merge-hint"）。 */
const mergeHintStyle = computed(() => {
  const target = mergeTarget.value;
  const current = boardState.value;
  if (!target || !current) return { display: "none" };
  const card = cardById(current, target.cardId);
  if (!card) return { display: "none" };
  const rect = cardRect(card);
  const element = viewportEl.value;
  const shellElement = shell.value;
  if (!element || !shellElement) return { display: "none" };
  const viewRect = element.getBoundingClientRect();
  const shellRect = shellElement.getBoundingClientRect();
  const screen = toScreenPoint(view.value, { x: rect.x, y: rect.y }, surfaceRect());
  // 放在目标卡片**上方**：卡片贴近容器顶部时才落到它下方，避免盖住卡片内容
  const above = screen.y - shellRect.top - 34;
  const below = screen.y - shellRect.top + rect.h * view.value.scale + 6;
  const minTop = viewRect.top - shellRect.top + 4;
  const maxTop = viewRect.bottom - shellRect.top - 30;
  return {
    left: Math.max(viewRect.left - shellRect.left + 4, Math.min(screen.x - shellRect.left, viewRect.right - shellRect.left - 300)) + "px",
    top: Math.max(minTop, Math.min(maxTop, above < minTop ? below : above)) + "px",
  };
});

function attachPointerListeners() {
  window.addEventListener("pointermove", onPointerMove);
  window.addEventListener("pointerup", onPointerUp);
  window.addEventListener("pointercancel", onDragCancel);
  // 指针移出窗口 / 窗口失焦时不会再有 pointerup：按中断处理，回到操作前位置
  window.addEventListener("blur", onDragCancel);
}

function detachPointerListeners() {
  window.removeEventListener("pointermove", onPointerMove);
  window.removeEventListener("pointerup", onPointerUp);
  window.removeEventListener("pointercancel", onDragCancel);
  window.removeEventListener("blur", onDragCancel);
}

// --- 平移 / 空格框选 / 滚轮缩放 -------------------------------------------

/** 输入框、可编辑内容、聊天、菜单、确认框里的操作不算板面操作。 */
function isTypingTarget(target: EventTarget | null): boolean {
  const element = target as HTMLElement | null;
  if (!element) return false;
  const tag = element.tagName;
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return true;
  if (element.isContentEditable) return true;
  if (typeof element.closest !== "function") return false;
  return Boolean(element.closest("[contenteditable='true'], [data-im='chat-panel'], [data-im='impact-dialog'], [role='dialog'], [role='menu']"));
}

function isBoardInteractive(target: EventTarget | null): boolean {
  const element = target as HTMLElement | null;
  if (!element || typeof element.closest !== "function") return false;
  return Boolean(
    element.closest(
      "article[data-im='card'], button, input, textarea, select, a, label, [data-im='card-toolbar'], [data-im='group'], [data-im='preview'], [data-im='link-draft'], [data-im='search'], [data-im='group-merge-hint'], [data-im='board-toolbar']",
    ),
  );
}

/** 可滚动容器（聊天 / 列表 / 代码区）优先滚动自身，不穿透成板面缩放。 */
function canScroll(element: HTMLElement): boolean {
  const style = typeof getComputedStyle === "function" ? getComputedStyle(element) : null;
  if (!style) return false;
  const overflowY = style.overflowY;
  const overflowX = style.overflowX;
  const scrollableY = (overflowY === "auto" || overflowY === "scroll") && element.scrollHeight > element.clientHeight + 1;
  const scrollableX = (overflowX === "auto" || overflowX === "scroll") && element.scrollWidth > element.clientWidth + 1;
  return scrollableY || scrollableX;
}

function insideScrollable(target: EventTarget | null): boolean {
  let element = target as HTMLElement | null;
  while (element && element !== viewportEl.value) {
    if (canScroll(element)) return true;
    element = element.parentElement;
  }
  return false;
}

function onSurfacePointerDown(event: PointerEvent) {
  if (event.button !== 0 && event.button !== 1) return;
  if (isBoardInteractive(event.target) || insideScrollable(event.target)) return;
  // 输入框 / 可编辑内容里按下不启动板面操作（空格正常输入）
  if (isTypingTarget(event.target)) return;
  event.preventDefault();
  const rect = rectSelect.value;
  if (rect) return;
  if (dragging.value || panning.value || linkDraft.value) return;
  const current = boardState.value;
  // 框选只由空格决定：没有常驻模式，同一个手势在任何时刻含义都一样
  const wantsRect = spaceDown.value;
  if (wantsRect) {
    rectSelect.value = {
      startClient: { x: event.clientX, y: event.clientY },
      currentClient: { x: event.clientX, y: event.clientY },
      additive: event.shiftKey,
      moved: false,
    };
    attachPointerListeners();
    return;
  }
  panning.value = {
    startClient: { x: event.clientX, y: event.clientY },
    startScroll: { ...scroll.value },
    moved: false,
  };
  if (current && current.selection.length) {
    // 单击空白处会清空选择；拖动则只平移，不改变选择
    notice.value = "";
  }
  attachPointerListeners();
}

function onWheel(event: WheelEvent) {
  // 聊天 / 列表 / 代码区优先滚动自身
  if (insideScrollable(event.target) || isTypingTarget(event.target)) return;
  event.preventDefault();
  const delta = wheelDeltaY(event);
  if (!delta) return;
  zoomAtPointer(wheelZoomFactor(delta), { x: event.clientX, y: event.clientY });
}

/** 框选矩形的屏幕样式（相对板面容器）。 */
const rectStyle = computed(() => {
  const rect = rectSelect.value;
  const element = viewportEl.value;
  if (!rect || !element) return {};
  const viewRect = element.getBoundingClientRect();
  const x0 = rect.startClient.x - viewRect.left;
  const y0 = rect.startClient.y - viewRect.top;
  const x1 = rect.currentClient.x - viewRect.left;
  const y1 = rect.currentClient.y - viewRect.top;
  return {
    left: Math.min(x0, x1) + "px",
    top: Math.min(y0, y1) + "px",
    width: Math.abs(x1 - x0) + "px",
    height: Math.abs(y1 - y0) + "px",
  };
});

// --- 选择 -----------------------------------------------------------------

function onSelect(cardId: string, additive: boolean) {
  const current = boardState.value;
  if (!current) return;
  const currentSelection = current.selection;
  let ids: string[];
  if (additive) {
    ids = currentSelection.includes(cardId)
      ? currentSelection.filter((id) => id !== cardId)
      : [...currentSelection, cardId];
  } else {
    ids = [cardId];
  }
  commit(setSelection(current, ids), additive ? "加选 / 减选卡片" : "选择卡片");
}

// --- 卡片局部工具栏 -------------------------------------------------------

const primarySelectedId = computed(() => {
  const current = boardState.value;
  if (!current || current.selection.length !== 1) return null;
  return current.selection[0];
});

const primarySelectedCard = computed(() => {
  const current = boardState.value;
  const id = primarySelectedId.value;
  if (!current || !id) return null;
  return cardById(current, id);
});

/** 工具栏跟着选中卡片：位置由 placeOverlay 换算（缩放平移后仍准确）。 */
function refreshOverlay() {
  const card = primarySelectedCard.value;
  if (!card) {
    overlay.value = { ...overlay.value, visible: false };
    return;
  }
  placeOverlay({ x: card.x, y: card.y, w: card.w, h: card.folded ? 60 : card.h });
}

const toolbarStyle = computed(() => {
  const card = primarySelectedCard.value;
  if (!card) return { display: "none" };
  return { left: overlay.value.left + "px", top: overlay.value.top + "px" };
});

// --- 卡片操作 -------------------------------------------------------------

function onCardPatch(cardId: string, patch: Partial<BoardCardModel>, label: string) {
  const current = boardState.value;
  if (!current) return;
  commit(updateCard(current, cardId, patch), label);
}

function onCardToggle(cardId: string, flag: "checked" | "hidden" | "folded" | "bookmarked") {
  const current = boardState.value;
  if (!current) return;
  const card = cardById(current, cardId);
  if (!card) return;
  if (flag === "checked") {
    commit(updateCard(current, cardId, { checked: !card.checked }), card.checked ? "取消勾选" : "勾选注释（本次允许 QIO 查看）");
    return;
  }
  if (flag === "hidden") {
    commit(updateCard(current, cardId, { hidden: !card.hidden }), card.hidden ? "取消隐藏" : "隐藏卡片（退出讨论范围）");
    return;
  }
  if (flag === "folded") {
    commit(updateCard(current, cardId, { folded: !card.folded }), card.folded ? "展开卡片" : "折叠卡片");
    return;
  }
  commit(updateCard(current, cardId, { bookmarked: !card.bookmarked }), card.bookmarked ? "取消书签" : "加书签");
}

/**
 * 删除卡片时，它上面的编辑草稿也要作为「待同步的清除」登记（契约 §11.2）。
 *
 * 只删内存里的草稿与本机记录**不会**让服务器删掉那份草稿：下次刷新又会把它恢复出来
 * （用户看到的正是「清除过的草稿复活了」）。没有草稿的卡片不登记，免得留下无意义的删除依据。
 */
function forgetCardDraft(cardId: string): void {
  if (!store.hasCardDraft(cardId)) return;
  store.clearDraft(cardDraftKey(cardId));
}

function onCardRemove(cardId: string) {
  const current = boardState.value;
  if (!current) return;
  commit(removeCard(current, cardId), "删除卡片（撤回这条材料或注释）");
  forgetCardDraft(cardId);
}

function onCardDuplicate(cardId: string) {
  const current = boardState.value;
  if (!current) return;
  // 复制只对这张卡片生效：多选时工具栏不显示「复制」，不会把单卡片编辑应用到整组
  commit(duplicateCard(current, cardId), "复制卡片");
}

function onCardLeaveGroup(cardId: string) {
  const current = boardState.value;
  if (!current) return;
  commit(removeFromGroup(current, cardId), "移出组");
}

function onCardJoinGroup(cardId: string, groupId: string) {
  const current = boardState.value;
  if (!current) return;
  commit(joinGroup(current, cardId, groupId), "把卡片加入组");
}

function deleteSelected() {
  const current = boardState.value;
  if (!current || !current.selection.length) return;
  const removed = [...current.selection];
  let next = current;
  for (const cardId of removed) next = removeCard(next, cardId);
  commit(next, "删除所选卡片（撤回材料或注释）");
  // 被删卡片上的编辑草稿同样要同步清除（§11.2）
  for (const cardId of removed) forgetCardDraft(cardId);
}

// --- 分组与顺序 -----------------------------------------------------------

function onGroupOp(op: GroupOp, groupId?: string) {
  const current = boardState.value;
  if (!current) return;
  const selected = current.selection.filter((cardId) => Boolean(cardById(current, cardId)));
  if (op === "form") {
    commit(createGroup(current, selected), "把所选卡片分成一组");
    return;
  }
  if (op === "join" && groupId) {
    let next = current;
    for (const cardId of selected) next = joinGroup(next, cardId, groupId);
    commit(next, "把所选卡片加入组");
    return;
  }
  if (op === "leave") {
    let next = current;
    for (const cardId of selected) next = removeFromGroup(next, cardId);
    commit(next, "把所选卡片移出组");
    return;
  }
  const targets = selectedGroupIds.value;
  if (!targets.length) {
    notice.value = "所选卡片不在任何组里：先用「所选成组」或把卡片拖到一起。";
    return;
  }
  if (op === "dissolve") {
    let next = current;
    for (const id of targets) next = dissolveGroup(next, id);
    commit(next, "解除组");
    return;
  }
  if (op === "ordered" || op === "unordered") {
    let next = current;
    for (const id of targets) next = setGroupOrdered(next, id, op === "ordered");
    commit(next, op === "ordered" ? "设为有序组（序号表示顺序）" : "取消有序（摆放不代表先后）");
    return;
  }
  if (op === "merge") {
    if (targets.length < 2) {
      notice.value = "合并组需要所选卡片分布在两个以上的组里。";
      return;
    }
    let next = current;
    const target = targets[0];
    for (const id of targets.slice(1)) next = mergeGroups(next, id, target);
    commit(next, "合并组（新组用默认名，成员连续插入）");
  }
}

function onMoveMember(groupId: string, cardId: string, index: number) {
  const current = boardState.value;
  if (!current) return;
  const group = groupById(current, groupId);
  if (!group || !group.members.includes(cardId)) return;
  commit(joinGroup(current, cardId, groupId, index), "调整组内顺序");
}

function onGroupRename(groupId: string, name: string) {
  const current = boardState.value;
  if (!current) return;
  commit(renameGroup(current, groupId, name), "改组名");
}

function onToggleOrdered(groupId: string, ordered: boolean) {
  const current = boardState.value;
  if (!current) return;
  commit(setGroupOrdered(current, groupId, ordered), ordered ? "设为有序组（序号表示顺序）" : "取消有序（摆放不代表先后）");
}

function onDissolveGroup(groupId: string) {
  const current = boardState.value;
  if (!current) return;
  commit(dissolveGroup(current, groupId), "解除组");
}

function onLeaveMember(groupId: string, cardId: string) {
  const current = boardState.value;
  if (!current) return;
  commit(removeFromGroup(current, cardId), "把卡片移出组");
}

// --- 关系链接：连接点拖线 -------------------------------------------------

/** 指针下的卡片：多个命中时取面积最小的（最具体）。 */
function hitCardAt(point: { x: number; y: number }, excludeId: string | null): string | null {
  const current = boardState.value;
  if (!current) return null;
  let best: { id: string; area: number } | null = null;
  for (const card of current.cards) {
    if (card.deleted || card.id === excludeId) continue;
    const rect = cardRect(card);
    if (!pointInRect(point.x, point.y, rect)) continue; // board.ts 的签名是 (x, y, rect)
    const area = rect.w * rect.h;
    if (!best || area < best.area) best = { id: card.id, area };
  }
  return best ? best.id : null;
}

function onConnectStart(cardId: string, event: PointerEvent) {
  event.preventDefault();
  // 从连接点开始拖线：取消可能残留的卡片拖动 / 平移状态，避免同一指针同时驱动两件事
  dragging.value = null;
  panning.value = null;
  rectSelect.value = null;
  linkDraft.value = { fromId: cardId, point: boardPointOf(event), targetId: null };
  notice.value = "从连接点拖到另一张卡片上建立关系；拖到无效位置或取消不会建立任何链接。";
  attachPointerListeners();
}

function finishLinkDraft(allowCreate: boolean) {
  const draft = linkDraft.value;
  linkDraft.value = null;
  detachPointerListeners();
  const current = boardState.value;
  if (!draft || !current) return;
  if (!allowCreate || !draft.targetId || draft.targetId === draft.fromId) {
    notice.value = "没有建立关系：拖到无效位置或取消时不建链，也不保存半条链接。";
    return;
  }
  const next = addLink(current, draft.fromId, draft.targetId, false, "");
  commit(next, "新建关系（方向与含义由你写明）");
  const created = next.links.find(
    (link) =>
      (link.src === draft.fromId && link.dst === draft.targetId) ||
      (link.src === draft.targetId && link.dst === draft.fromId),
  );
  if (created) openLinkEditor(created.id);
}

/** 关系模式（工具栏选择模式）里依次点两张卡片也能建立关系。 */
function pickLinkCard(cardId: string) {
  const current = boardState.value;
  if (!current) return;
  dragging.value = null;
  panning.value = null;
  rectSelect.value = null;
  const source = linkDraft.value?.fromId ?? null;
  if (!source) {
    linkDraft.value = { fromId: cardId, point: null, targetId: null };
    notice.value = "关系模式：已选起点「" + cardSummary(cardById(current, cardId)) + "」，再点一张卡片建立关系。";
    return;
  }
  if (source === cardId) {
    linkDraft.value = null;
    notice.value = "已取消建立关系。";
    return;
  }
  linkDraft.value = { fromId: source, point: null, targetId: cardId };
  finishLinkDraft(true);
}

function openLinkEditor(linkId: string) {
  const current = boardState.value;
  if (!current) return;
  const link = current.links.find((item) => item.id === linkId);
  if (!link) return;
  editingLinkId.value = linkId;
  linkMeaning.value = link.meaning;
  linkDirection.value = link.direction;
}

function saveLinkEditor() {
  const current = boardState.value;
  const linkId = editingLinkId.value;
  if (!current || !linkId) return;
  commit(updateLink(current, linkId, { meaning: linkMeaning.value, direction: linkDirection.value }), "修改关系含义 / 方向");
  notice.value = "已保存关系的方向与含义（含义是你写的原话，系统不补充解释）。";
  editingLinkId.value = null;
}

function deleteEditingLink() {
  const current = boardState.value;
  const linkId = editingLinkId.value;
  if (!current || !linkId) return;
  commit(removeLink(current, linkId), "删除关系（撤回这条关联）");
  editingLinkId.value = null;
}

const editingLink = computed(() => {
  const current = boardState.value;
  if (!current || !editingLinkId.value) return null;
  return current.links.find((link) => link.id === editingLinkId.value) ?? null;
});

const editingLinkSummary = computed(() => {
  const link = editingLink.value;
  const current = boardState.value;
  if (!link || !current) return "";
  const src = cardSummary(cardById(current, link.src));
  const dst = cardSummary(cardById(current, link.dst));
  return linkDirection.value ? src + " → " + dst + "（方向由你标注）" : src + " ↔ " + dst + "（无方向）";
});

// --- 板内搜索定位（页面壳的搜索面板 dispatch 事件） -----------------------

function locate(cardId: string) {
  const current = boardState.value;
  if (!current) return;
  const card = cardById(current, cardId);
  if (!card) return;
  // 平移查看位置，让卡片进入可视区（查看位置不是板面改动）
  const element = viewportEl.value;
  if (element) {
    const viewRect = element.getBoundingClientRect();
    const screen = toScreenPoint(view.value, { x: card.x, y: card.y }, surfaceRect());
    const margin = 80;
    let dx = 0;
    let dy = 0;
    if (screen.x < viewRect.left + margin) dx = viewRect.left + margin - screen.x;
    else if (screen.x + card.w * view.value.scale > viewRect.right - margin) {
      dx = viewRect.right - margin - (screen.x + card.w * view.value.scale);
    }
    if (screen.y < viewRect.top + margin) dy = viewRect.top + margin - screen.y;
    else if (screen.y + card.h * view.value.scale > viewRect.bottom - margin) {
      dy = viewRect.bottom - margin - (screen.y + card.h * view.value.scale);
    }
    if (dx || dy) {
      applyScroll({ x: scroll.value.x - dx, y: scroll.value.y - dy });
      view.value = { ...view.value, x: -scroll.value.x, y: -scroll.value.y };
    }
  }
  highlightId.value = cardId;
  if (highlightTimer) clearTimeout(highlightTimer);
  highlightTimer = setTimeout(() => {
    highlightId.value = null;
  }, 1600);
}

function onLocateCard(event: Event) {
  const detail = (event as CustomEvent<{ cardId?: string }>).detail;
  if (!detail || !detail.cardId) return;
  locate(detail.cardId);
}

// --- 待审批预览：定位事件 --------------------------------------------------

interface LocatePreviewDetail {
  intentId?: string;
  bounds?: { x: number; y: number; w: number; h: number };
}

/** C / D 的浮层点「在板面上定位」时 dispatch 这个事件；这里高亮 + 把预览移进可视区。 */
function onLocatePreview(event: Event) {
  const detail = (event as CustomEvent<LocatePreviewDetail>).detail;
  if (!detail || !detail.intentId) return;
  locatedIntentId.value = detail.intentId;
  if (locatedTimer) clearTimeout(locatedTimer);
  locatedTimer = setTimeout(() => {
    locatedIntentId.value = null;
  }, 2000);
  const bounds = detail.bounds;
  const element = viewportEl.value;
  if (!bounds || !element) return;
  const viewRect = element.getBoundingClientRect();
  const screen = toScreenPoint(view.value, { x: bounds.x, y: bounds.y }, surfaceRect());
  const dx = screen.x < viewRect.left + 60 ? viewRect.left + 60 - screen.x : 0;
  const dy = screen.y < viewRect.top + 60 ? viewRect.top + 60 - screen.y : 0;
  if (dx || dy) {
    applyScroll({ x: scroll.value.x - dx, y: scroll.value.y - dy });
    view.value = { ...view.value, x: -scroll.value.x, y: -scroll.value.y };
  }
}

// --- 键盘 -----------------------------------------------------------------

function onKeyDown(event: KeyboardEvent) {
  const typing = isTypingTarget(event.target);
  if (event.key === "Escape") {
    if (dragging.value || rectSelect.value || panning.value) {
      onDragCancel();
      return;
    }
    if (linkDraft.value) {
      linkDraft.value = null;
      notice.value = "已退出建立关系。";
      return;
    }
    if (editingLinkId.value) editingLinkId.value = null;
    return;
  }
  // 空格 + 拖动空白处 = 框选卡片；输入框 / 可编辑内容 / 聊天 / 菜单 / 确认框里空格正常输入
  if (event.code === "Space" || event.key === " ") {
    if (typing) return;
    const target = event.target as HTMLElement | null;
    if (target && typeof target.closest === "function" && target.closest("button, a, [role='button'], [role='menuitem']")) {
      return; // 空格是这些控件的激活键，不劫持
    }
    if (!spaceDown.value) spaceDown.value = true;
    if (!event.repeat) event.preventDefault();
    return;
  }
  if (typing) return;
  if ((event.key === "Delete" || event.key === "Backspace") && selection.value.length) {
    event.preventDefault();
    deleteSelected();
    return;
  }
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "z") {
    event.preventDefault();
    store.undo();
    return;
  }
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "y") {
    event.preventDefault();
    store.redo();
  }
}

function onKeyUp(event: KeyboardEvent) {
  if (event.code === "Space" || event.key === " ") spaceDown.value = false;
}

// --- 生命周期 -------------------------------------------------------------

onMounted(() => {
  // 滚轮要 preventDefault（否则浏览器会把页面也滚走），所以显式用非被动监听
  viewportEl.value?.addEventListener("wheel", onWheel, { passive: false });
  viewportEl.value?.addEventListener("scroll", onViewportScroll, { passive: true });
  window.addEventListener("keydown", onKeyDown);
  window.addEventListener("keyup", onKeyUp);
  window.addEventListener("qio:interactive:locate-preview", onLocatePreview as EventListener);
  window.addEventListener("qio:interactive:locate-card", onLocateCard as EventListener);
  refreshOverlay();
});

onBeforeUnmount(() => {
  viewportEl.value?.removeEventListener("wheel", onWheel);
  viewportEl.value?.removeEventListener("scroll", onViewportScroll);
  window.removeEventListener("keydown", onKeyDown);
  window.removeEventListener("keyup", onKeyUp);
  window.removeEventListener("qio:interactive:locate-preview", onLocatePreview as EventListener);
  window.removeEventListener("qio:interactive:locate-card", onLocateCard as EventListener);
  detachPointerListeners();
  if (highlightTimer) clearTimeout(highlightTimer);
  if (locatedTimer) clearTimeout(locatedTimer);
});
</script>

<template>
  <section ref="shell" class="board-shell" data-im="board">
    <p v-if="dragging" class="banner drag" role="status">{{ dragHint }}</p>
    <p v-if="notice" class="banner" role="status">{{ notice }}</p>
    <p class="banner view-hint" role="status">
      拖动空白处平移查看位置（{{ Math.round(view.scale * 100) }}%）；按住空格拖动空白处框选卡片；滚轮以指针附近为缩放中心。
      平移与缩放只改变查看方式，不形成改动、不调用 QIO。
    </p>

    <div class="board-main">
      <div
        ref="viewportEl"
        class="board-viewport"
        :class="{ dragging: Boolean(dragging), panning: Boolean(panning), framing: Boolean(rectSelect) }"
        @pointerdown="onSurfacePointerDown"
      >
        <div class="board-scroll-content" :style="contentStyle">
          <div ref="surface" class="board-surface" :style="surfaceStyle">
          <BoardGroupFrame
            v-for="group in groups"
            :key="group.id"
            :group="group"
            :cards="displayCards"
            :selected-ids="selection"
            :drop-target="dragging !== null && dragging.preview.groupId === group.id"
            :drop-merge="dragging !== null && dragging.preview.mergesWith === group.id"
            :drop-index="dragging !== null && dragging.preview.groupId === group.id ? dragging.preview.index : null"
            :drop-card-id="dragging ? dragging.cardId : null"
            @rename="onGroupRename"
            @toggle-ordered="onToggleOrdered"
            @dissolve="onDissolveGroup"
            @move-member="onMoveMember"
            @leave="onLeaveMember"
            @select-member="(cardId) => onSelect(cardId, false)"
          />

          <BoardCard
            v-for="card in liveCards"
            :key="card.id"
            :card="card"
            :selected="selection.includes(card.id)"
            :highlight="highlightId === card.id"
            :dragging="dragging !== null && dragging.cardId === card.id"
            :x="displayX(card)"
            :y="displayY(card)"
            :group-name="groupNameOf(card.id)"
            :groups="groups.filter((item) => item.id !== groupIdOf(card.id))"
            :toolbar-left="overlay.left"
            :toolbar-top="overlay.top"
            :multi="selection.length > 1"
            :connecting="linkDraft !== null && linkDraft.fromId === card.id"
            @select="onSelect"
            @drag-start="onDragStart"
            @patch="onCardPatch"
            @toggle="onCardToggle"
            @remove="onCardRemove"
            @duplicate="onCardDuplicate"
            @leave-group="onCardLeaveGroup"
            @join-group="onCardJoinGroup"
            @connect-start="onConnectStart"
          />

          <BoardLinkLayer
            :links="links"
            :cards="displayCards"
            :active-link-id="editingLinkId"
            :width="SURFACE_W"
            :height="SURFACE_H"
            :draft-from-id="linkDraft ? linkDraft.fromId : null"
            :draft-point="linkDraft ? linkDraft.point : null"
            :draft-target-id="linkDraft ? linkDraft.targetId : null"
            @select-link="openLinkEditor"
          />

          <BoardPreviewLayer
            :intents="previewIntents"
            :located-intent-id="locatedIntentId"
            :width="SURFACE_W"
            :height="SURFACE_H"
          />

            <div v-if="rectSelect" class="select-rect" :style="rectStyle"></div>
          </div>
        </div>
      </div>

      <!-- 松开后合并成组：只在**明确重叠**时出现，松手才成组 -->
      <p
        v-if="mergeTarget"
        class="merge-hint"
        :style="mergeHintStyle"
        data-im="group-merge-hint"
        :data-target-card-id="mergeTarget.cardId"
        role="status"
      >
        {{ mergeHintText }}
      </p>
    </div>

    <div v-if="editingLink" class="link-editor" role="dialog" aria-label="关系编辑">
      <p class="link-title">{{ editingLinkSummary }}</p>
      <label class="link-row">
        <input v-model="linkDirection" type="checkbox" data-im="link-direction" />
        <span>有方向（方向由你写明；系统不解释成因果 / 支持 / 先后）</span>
      </label>
      <label class="link-row">
        <span>含义</span>
        <input v-model="linkMeaning" type="text" data-im="link-meaning" placeholder="写下这条关系的含义（你的原话）" />
      </label>
      <div class="link-actions">
        <button class="btn primary" type="button" @click="saveLinkEditor">保存方向与含义</button>
        <button class="btn danger" type="button" @click="deleteEditingLink">删除关系</button>
        <button class="btn" type="button" @click="editingLinkId = null">关闭</button>
      </div>
    </div>
  </section>
</template>

<style scoped>
.board-shell {
  position: relative;
  flex: 1;
  display: flex;
  flex-direction: column;
  min-height: 0;
  min-width: 0;
  background: var(--bg-base);
}
.banner {
  margin: 0;
  padding: var(--sp-1) var(--sp-4);
  font-size: var(--fs-xs);
  color: var(--text-secondary);
  background: var(--bg-surface);
  border-bottom: 1px solid var(--border-subtle);
}
.banner.drag { color: var(--text-strong); background: var(--bg-accent-subtle); }
.banner.view-hint { color: var(--text-faint); }
.board-main { position: relative; flex: 1; display: flex; min-height: 0; min-width: 0; }
.board-viewport {
  position: relative;
  flex: 1;
  min-width: 0;
  min-height: 0;
  overflow: auto;
  background: var(--bg-inset);
  touch-action: none;
}
.board-viewport.dragging { user-select: none; }
.board-viewport.panning { cursor: grabbing; }
.board-viewport.framing { cursor: crosshair; }
.board-scroll-content { position: relative; }
.board-surface { position: relative; will-change: transform; }
.select-rect {
  position: absolute;
  border: 1px dashed var(--accent);
  background: var(--accent-soft);
  pointer-events: none;
  z-index: 40;
}
/* 合并提示：浮在目标卡片附近，缩放平移后仍然指得准 */
.merge-hint {
  position: absolute;
  z-index: 55;
  margin: 0;
  max-width: 320px;
  padding: var(--sp-1) var(--sp-2);
  font-size: var(--fs-xs);
  color: var(--text-strong);
  background: var(--bg-elevated);
  border: 1px dashed var(--accent);
  border-radius: var(--r-sm);
  box-shadow: var(--shadow-2);
  pointer-events: none;
}
.link-editor {
  position: absolute;
  left: var(--sp-4);
  bottom: var(--sp-4);
  z-index: 70;
  width: min(420px, 92%);
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  padding: var(--sp-3);
  background: var(--bg-elevated);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-md);
  box-shadow: var(--shadow-2);
}
.link-title { margin: 0; font-size: var(--fs-sm); color: var(--text-strong); }
.link-row { display: flex; align-items: center; gap: var(--sp-2); font-size: var(--fs-xs); color: var(--text-secondary); }
.link-row input[type="text"] {
  flex: 1;
  min-width: 0;
  font: inherit;
  font-size: var(--fs-sm);
  color: var(--text-primary);
  background: var(--bg-inset);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-xs);
  padding: var(--sp-1);
}
.link-actions { display: flex; gap: var(--sp-1); flex-wrap: wrap; }
.btn {
  font: inherit;
  font-size: var(--fs-xs);
  color: var(--text-secondary);
  background: var(--bg-surface);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-xs);
  padding: 2px var(--sp-2);
  cursor: pointer;
}
.btn:hover { color: var(--text-strong); border-color: var(--border-strong); }
.btn:focus-visible { outline: 2px solid var(--link); outline-offset: 1px; }
.btn.primary { color: var(--on-accent); background: var(--accent); border-color: var(--accent); }
.btn.danger { color: var(--danger); }
@media (max-width: 900px) {
  .link-editor { left: var(--sp-2); bottom: var(--sp-2); }
}
</style>
