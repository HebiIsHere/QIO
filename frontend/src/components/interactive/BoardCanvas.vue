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
  5. 卡片局部工具栏由 BoardCard 渲染；**连接点与尺寸把手统一由 BoardResizeLayer**（不随画布缩放的
     交互层，同一份 displayRect × scale）渲染：连接点在四边中点、尺寸热区在边 / 角，两者同时可用，
     不需要切换模式（0.2.5 §三 / §四）。重叠成组的提示与判定走 board.ts 的 bestMergeTarget。
  6. 拖线目标判定认**当前显示矩形**（displayRect）：展开、让位、拖动、尺寸预览之后，
     卡片 / 连线 / 高亮画在哪，落点就判在哪；临时显示坐标只读，绝不写回板面（0.2.5 §四）。
  7. 改版后底部工具栏与板内搜索由页面壳与 A 的组件渲染：本文件不再引用 <BoardToolbar /> 与 <BoardSearchPanel />。
-->
<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
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
  publishBoardArea,
  rectFromDrag,
  toBoardPoint,
  wheelDeltaY,
  wheelZoomFactor,
  zoomAtScroll,
  type DOMRectLike,
  type Viewport,
} from "../../interactive/viewport";
import {
  cardSizeLimits,
  EXPANDED_PREVIEW_SIZE,
  planExpandedLayout,
  type LayoutRect,
} from "../../interactive/cardLayout";
import { useResizeGesture } from "../../composables/useResizeGesture";
import { chatObstacleFrame } from "../../interactive/chatFrameState";
import type { ResizeBox, ResizeHandle, SizeLimits } from "../../interactive/resize";
import { titlebarHeight } from "../../utils/titlebar";
import type { BoardCard as BoardCardModel, BoardGroup, BoardState, CardKind, Intent } from "../../interactive/types";
import BoardCard from "./BoardCard.vue";
import BoardGroupFrame from "./BoardGroupFrame.vue";
import BoardLinkLayer from "./BoardLinkLayer.vue";
import BoardPreviewLayer from "./BoardPreviewLayer.vue";
import BoardResizeLayer from "./BoardResizeLayer.vue";

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
/**
 * 尺寸拖动中的本地预览（只用于显示：跟手、无过渡动画）。
 * 拖动期间**不写板面状态**，松手才 commit 一次（任务书 §3.7）。
 */
const resizing = ref<{ cardId: string; box: ResizeBox } | null>(null);
/** 尺寸手势起点的显示槽位与可用范围（按下那一刻取快照，拖动期间必须稳定） */
const resizeStartBox = ref<ResizeBox>({ x: 0, y: 0, w: 0, h: 0 });
const resizeLimits = ref<SizeLimits>({ minW: 1, minH: 1, maxW: 1, maxH: 1 });
/** 本次尺寸手势作用于哪张卡（useResizeGesture 只认 box，不认 id） */
let resizeTargetId: string | null = null;
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

// --- 临时布局：展开预览的槽位（契约 2026-10-12 §3.6） -----------------------

/** 窗口尺寸变化后重新读一次容器尺寸（clientWidth 不是响应式的） */
const stageTick = ref(0);
function onWindowResize(): void {
  stageTick.value += 1;
}

/**
 * 板面可视范围（**板面坐标**）：容器像素尺寸 ÷ 缩放。
 * 容器量不到尺寸（jsdom 没有真实布局）时 containerSize() 已经兜底成板面大小。
 * 这是「可用区域」的兜底口径：量不到屏幕几何时退回到它 + 原点 0。
 */
const stageSize = computed(() => {
  void stageTick.value;
  const scale = scaleSafe.value;
  const size = containerSize();
  return { w: size.w / scale, h: size.h / scale };
});

/** 缩放的安全值：非有限 / 非正一律按 1（与 viewport 的 clampScale 同一条口径）。 */
const scaleSafe = computed(() => (Number.isFinite(view.value.scale) && view.value.scale > 0 ? view.value.scale : 1));

/** 屏幕矩形（左上 + 右下）。 */
interface ScreenBox {
  left: number;
  top: number;
  right: number;
  bottom: number;
}

function overlapsBox(a: ScreenBox, b: ScreenBox): boolean {
  return a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;
}

/**
 * 从可用区里扣掉一块浮层：取「剩下四块里面积最大的一块」。
 * 宁可把可用区算小（保守），也不让展开卡压在浮层上 —— 这才是「可用区域」该有的口径。
 */
function cutOutScreenBox(box: ScreenBox, obstacle: ScreenBox): ScreenBox {
  if (!overlapsBox(box, obstacle)) return box;
  const candidates: ScreenBox[] = [
    { ...box, bottom: Math.min(box.bottom, obstacle.top) },
    { ...box, top: Math.max(box.top, obstacle.bottom) },
    { ...box, right: Math.min(box.right, obstacle.left) },
    { ...box, left: Math.max(box.left, obstacle.right) },
  ].filter((item) => item.right - item.left > 1 && item.bottom - item.top > 1);
  if (!candidates.length) return box;
  let best = candidates[0];
  let bestArea = -1;
  for (const item of candidates) {
    const area = (item.right - item.left) * (item.bottom - item.top);
    if (area > bestArea) {
      bestArea = area;
      best = item;
    }
  }
  return best;
}

/** 一个浮层元素的屏幕矩形；不存在 / 零尺寸 / 量不到时 null（不产生假的扣减）。 */
function overlayScreenBox(selector: string): ScreenBox | null {
  if (typeof document === "undefined" || typeof document.querySelector !== "function") return null;
  const element = document.querySelector<HTMLElement>(selector);
  if (!element || typeof element.getBoundingClientRect !== "function") return null;
  const rect = element.getBoundingClientRect();
  if (!(rect.width > 0 && rect.height > 0)) return null;
  return { left: rect.left, top: rect.top, right: rect.right, bottom: rect.bottom };
}

/**
 * 平移后的真实滚动量：只在容器**真的能滚动**时认它。
 * jsdom 没有真实布局（scrollWidth 恒为 0、setter 会把滚动量钳到 0），
 * 无条件采用会让「可视起点」在测试里变成幽灵坐标。
 */
function layoutScroll(): { x: number; y: number } {
  const element = viewportEl.value;
  if (!element) return { x: 0, y: 0 };
  const live = liveScroll();
  const canScrollX = element.scrollWidth > element.clientWidth + 1;
  const canScrollY = element.scrollHeight > element.clientHeight + 1;
  return { x: canScrollX ? live.x : 0, y: canScrollY ? live.y : 0 };
}

/**
 * 板面**可用区域**（板面坐标）：平移 / 滚动后的可视起点 + 缩放之后，
 * 再扣掉自绘标题栏、底部工具栏、对话窗口（chatObstacleFrame）与审批浮层。
 *
 * 为什么必须扣：只按「容器宽高 + 画布原点」算，展开卡会盖住底部工具栏 / 对话窗口，
 * 用户既读不到展开的内容，也点不到被压住的按钮（任务书 §7.4）。
 * X 轴与 Y 轴都要扣：工具栏吃的是高度、对话窗口两轴都吃。
 */
const boardRegion = computed<{ origin: { x: number; y: number }; size: { w: number; h: number } }>(() => {
  void stageTick.value;
  const scale = scaleSafe.value;
  const box = viewportScreenBox();
  if (!box) {
    // 量不到屏幕几何（jsdom / 还没挂载）：保持既有口径 —— 只用容器宽高，原点在板面原点
    return { origin: { x: 0, y: 0 }, size: stageSize.value };
  }
  let work: ScreenBox = { ...box };
  // 0.2.3 自绘标题栏：它压在视口顶部时扣掉它那一行（视口本来就在标题栏之下时是空操作）
  const titlebar = Math.max(0, titlebarHeight());
  if (titlebar > 0) {
    work = cutOutScreenBox(work, { left: 0, top: 0, right: work.right, bottom: titlebar });
  }
  // 底部工具栏与审批浮层：按自己的真实矩形扣
  for (const selector of ['[data-im="board-toolbar"]', '[data-im="impact-dialog"]']) {
    const obstacle = overlayScreenBox(selector);
    if (obstacle) work = cutOutScreenBox(work, obstacle);
  }
  // 对话窗口：IRL-D / A 发布的真实显示框（收起时 chatObstacleFrame 返回 null = 不避让）
  const chat = chatObstacleFrame();
  if (chat && chat.w > 0 && chat.h > 0) {
    work = cutOutScreenBox(work, { left: chat.x, top: chat.y, right: chat.x + chat.w, bottom: chat.y + chat.h });
  }
  const scrollNow = layoutScroll();
  const insetLeft = Math.max(0, work.left - box.left);
  const insetTop = Math.max(0, work.top - box.top);
  return {
    origin: { x: (scrollNow.x + insetLeft) / scale, y: (scrollNow.y + insetTop) / scale },
    size: {
      w: Math.max(0, work.right - work.left) / scale,
      h: Math.max(0, work.bottom - work.top) / scale,
    },
  };
});

/** 画布视口在屏幕上的矩形（含滚动量）；量不到时 null。 */
function viewportScreenBox(): ScreenBox | null {
  const element = viewportEl.value;
  if (!element || typeof element.getBoundingClientRect !== "function") return null;
  const rect = element.getBoundingClientRect();
  if (!(rect.width > 0 && rect.height > 0)) return null;
  return { left: rect.left, top: rect.top, right: rect.right, bottom: rect.bottom };
}

/**
 * 把可用区域发布给别的组件（底部「添加」菜单要按它算新卡落点）。
 * 只发布显示态，不进板面状态；画布卸载时清空，别的组件据此回落到既有兜底行为。
 */
watch(
  boardRegion,
  (region) => {
    publishBoardArea({ x: region.origin.x, y: region.origin.y, w: region.size.w, h: region.size.h });
  },
  { immediate: true },
);

/**
 * 用户**亲自调过尺寸**的卡片（本次会话内）。展开时沿用用户尺寸，不再套默认阅读尺寸 ——
 * 否则用户在展开状态下把卡改小，槽位又按「max(卡尺寸, 阅读尺寸)」弹回去，看着像没生效。
 * 这是显示态、不进板面状态；刷新后回到默认阅读尺寸，但 card.w/h 仍是用户那一份。
 */
const userSizedCards = ref<Set<string>>(new Set());

/**
 * 这一帧的临时布局：只用于渲染。
 * 入参是**正式位置**（board.cards 的 x/y/w/h），输出槽位绝不写回板面状态；
 * 展开 / 让位 / 收起都不产生表达、不触发意图 / 任务、不排保存。
 * `expandedAvoid` 打开：多张卡同时展开时互相让位（任务书 §7.2）。
 */
const layoutPlan = computed(() =>
  planExpandedLayout({
    cards: liveCards.value,
    expanded: store.expandedCardIds,
    expandedSize: EXPANDED_PREVIEW_SIZE,
    stage: boardRegion.value.size,
    stageOrigin: boardRegion.value.origin,
    expandedAvoid: true,
    sizedCards: [...userSizedCards.value],
  }),
);

/** 只把**真正展开**的槽位交给预览层（普通卡片不需要额外容器） */
const expandedSlots = computed<Record<string, LayoutRect>>(() => {
  const plan = layoutPlan.value;
  const out: Record<string, LayoutRect> = {};
  for (const id of plan.expanded) {
    const slot = plan.slots[id];
    if (slot) out[id] = slot;
  }
  return out;
});

/** 某张卡这一帧画在哪：没有展开时就是正式位置（与改动前逐字段相同） */
function slotOf(card: BoardCardModel): LayoutRect {
  return layoutPlan.value.slots[card.id] ?? cardRect(card);
}

/**
 * 卡片、组框、连线、尺寸把手**共用**的这一帧显示矩形。
 * 优先级：拖动中的实时坐标 > 尺寸拖动预览 > 布局槽位。
 */
function displayRect(card: BoardCardModel): LayoutRect {
  const slot = slotOf(card);
  const drag = dragging.value;
  if (drag && drag.cardId === card.id) return { x: drag.x, y: drag.y, w: slot.w, h: slot.h };
  const size = resizing.value;
  if (size && size.cardId === card.id) return previewRectOf(card, slot, size.box);
  return slot;
}

/**
 * 这张卡这一帧是不是**临时显示布局**（展开 / 被让位）。
 *
 * 判定必须同时看「槽位位置」与「是否在展开集合里」：原位展开的卡片槽位 x/y 与正式
 * 位置相同（不是被让位），但它的宽高是临时放大的 —— 位置仍然属于显示布局，
 * 不能被尺寸调整顺手写回板面（任务书 §7.1）。
 */
function isTemporaryDisplay(card: BoardCardModel, slot: LayoutRect): boolean {
  const official = cardRect(card);
  return slot.x !== official.x || slot.y !== official.y || store.isCardPreviewExpanded(card.id);
}

/**
 * 尺寸拖动中的预览矩形。
 *
 * 位置只由「拖动卡片」改变：临时显示布局里的卡片（展开 / 被让位）绝不把临时坐标
 * 写回板面，所以预览也只取宽高、左上角留在槽位上 —— 否则松手后卡片会整块跳回正式位置
 * （所见非所得）。槽位就是正式位置的常态卡片，西 / 北把手本来就要「对边不动」，
 * 预览与提交都用完整 box。
 */
function previewRectOf(card: BoardCardModel, slot: LayoutRect, box: ResizeBox): LayoutRect {
  if (isTemporaryDisplay(card, slot)) return { x: slot.x, y: slot.y, w: box.w, h: box.h };
  return { x: box.x, y: box.y, w: box.w, h: box.h };
}

/** 交给 BoardCard 的卡片对象：只把渲染坐标换成显示矩形，其余字段与正式状态一致 */
function renderCard(card: BoardCardModel): BoardCardModel {
  const rect = displayRect(card);
  if (rect.x === card.x && rect.y === card.y && rect.w === card.w && rect.h === card.h) return card;
  return { ...card, x: rect.x, y: rect.y, w: rect.w, h: rect.h };
}

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
  return displayRect(card).x;
}

function displayY(card: BoardCardModel): number {
  return displayRect(card).y;
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
  const moved = drag && drag.moved ? drag : null;
  const size = resizing.value;
  // 没有展开、也没有进行中的拖动 / 尺寸预览时原样返回（与改动前完全一致，避免无谓的对象重建）
  if (!moved && !size && !layoutPlan.value.expanded.length) return liveCards.value;
  return liveCards.value.map((card) => {
    const rect = displayRect(card);
    if (rect.x === card.x && rect.y === card.y && rect.w === card.w && rect.h === card.h) return card;
    return { ...card, x: rect.x, y: rect.y, w: rect.w, h: rect.h };
  });
});

/**
 * 组框也走**显示坐标**：组框的存储外框来自成员**正式位置**（board.ts 的 reflowGroup 口径），
 * 卡片被展开放大 / 让位 / 拖动之后必须跟着重算 —— 否则框不住成员，读起来像是「分组变了」。
 *
 * 这里不新引入间距常量：从存储外框与正式成员框**反推**四边的留白，再套到显示框上。
 * 显示坐标等于正式坐标时逐字段还原成存储值（既有行为与既有测试完全不变），
 * 并且**只用于显示**：真实分组、关系、成员顺序与材料范围一个字段都不动。
 */
const displayGroups = computed<BoardGroup[]>(() => {
  const cards = displayCards.value;
  const officialCards = liveCards.value;
  return groups.value.map((group) => {
    const officialMembers = officialCards.filter((card) => group.members.includes(card.id));
    const members = cards.filter((card) => group.members.includes(card.id));
    if (!officialMembers.length || !members.length) return group;
    const padLeft = Math.min(...officialMembers.map((card) => card.x)) - group.x;
    const padTop = Math.min(...officialMembers.map((card) => card.y)) - group.y;
    const padRight = group.x + group.w - Math.max(...officialMembers.map((card) => card.x + card.w));
    const padBottom = group.y + group.h - Math.max(...officialMembers.map((card) => card.y + card.h));
    const left = Math.min(...members.map((card) => card.x)) - padLeft;
    const top = Math.min(...members.map((card) => card.y)) - padTop;
    const right = Math.max(...members.map((card) => card.x + card.w)) + padRight;
    const bottom = Math.max(...members.map((card) => card.y + card.h)) + padBottom;
    if (left === group.x && top === group.y && right === group.x + group.w && bottom === group.y + group.h) {
      return group;
    }
    return { ...group, x: left, y: top, w: Math.max(1, right - left), h: Math.max(1, bottom - top) };
  });
});

function commit(next: BoardState, label: string) {
  store.commit(next, label);
}

// --- 卡片尺寸调整（任务书 §3） ---------------------------------------------

/**
 * 手势层只负责「指针 → 尺寸」：
 * - 拖动中只回调预览（跟手、不写状态、不排保存），松手 / 键盘 −/＋ 才回调一次提交；
 * - 指针取消 / Esc / 失焦 / 组件卸载 → 恢复拖动前显示（板面状态从头到尾没被改过）；
 * - `scale` 把屏幕位移换算回**板面单位**（画布是 1 / zoom）；少了它，缩放后拖动会跟着放大。
 */
const resizeGesture = useResizeGesture({
  box: () => ({ ...resizeStartBox.value }),
  limits: () => resizeLimits.value,
  scale: () => 1 / scaleSafe.value,
  onPreview: (box) => {
    if (!resizeTargetId) return;
    resizing.value = { cardId: resizeTargetId, box: { ...box } };
  },
  onCommit: (box) => commitCardSize(box),
  onCancel: () => {
    resizing.value = null;
  },
});

/** 这张卡还能长到多大：从它的位置到可用区域右下角（板面坐标）。 */
function resizeAreaFor(rect: LayoutRect): { w: number; h: number } {
  const region = boardRegion.value;
  const right = region.origin.x + region.size.w;
  const bottom = region.origin.y + region.size.h;
  /**
   * 至少按卡片**当前尺寸**算：卡片被让位 / 平移到可用区边缘时，
   * 「到右下角的距离」可能小于最小值，会把最小尺寸一起拖塌（sizeLimits 保证 min ≤ max）。
   */
  return { w: Math.max(rect.w, right - rect.x), h: Math.max(rect.h, bottom - rect.y) };
}

/** 按下 / 键盘步进前先把这一帧的槽位与可用范围快照给手势，保证拖动期间稳定。 */
function beginResize(cardId: string): LayoutRect | null {
  const current = boardState.value;
  const card = current ? cardById(current, cardId) : null;
  if (!card) return null;
  const rect = displayRect(card);
  resizeTargetId = cardId;
  resizeStartBox.value = { x: rect.x, y: rect.y, w: rect.w, h: rect.h };
  const limits = cardSizeLimits(resizeAreaFor(rect));
  /**
   * 卡片本来就比最小口径还小时（历史数据、很小的卡片）：把最小值收敛到它自己的尺寸。
   * 否则按一下「−」会被夹到 180，看上去「点变小反而变大」；也让 +/− 在极小卡片上可预期。
   */
  resizeLimits.value = {
    ...limits,
    minW: Math.min(limits.minW, rect.w),
    minH: Math.min(limits.minH, rect.h),
  };
  return rect;
}

function onResizeHandleDown(cardId: string, handle: ResizeHandle, event: PointerEvent) {
  // 同一指针只驱动一种手势：开始调整尺寸就退出可能还在的拖动 / 平移 / 框选 / 拖线
  releaseGestures();
  if (!beginResize(cardId)) return;
  // 尺寸调整不是移动卡片：不预演落点、不判定成组目标，也就不会触发重叠成组
  notice.value = "";
  resizeGesture.onHandleDown(handle, event);
}

function onResizeNudge(cardId: string, axis: "w" | "h", direction: 1 | -1) {
  if (!beginResize(cardId)) return;
  resizeGesture.nudge(axis, direction);
}

/** 四舍五入到整数板面单位；差值不足 1 视为「没有变化」。 */
function sameSize(a: number, b: number): boolean {
  return Math.abs(a - b) < 1;
}

/**
 * 松手 / 键盘步进：**一次** `updateCard` + `store.commit`，标签固定「调整卡片尺寸」。
 *
 * 提交口径（Lead 裁决）：
 * - 常态卡片（没有展开、也没有被让位）→ 提交 { x, y, w, h }：
 *   西 / 北把手要「对边不动」，不带上位置就会在松手后整块跳回原左边缘；
 * - 展开中 / 被让位（{@link isTemporaryDisplay}）→ **只提交 { w, h }**：
 *   临时槽位的坐标属于显示布局，绝不写回板面状态（任务书 §7.1）。
 *
 * 两条路径都只改尺寸 / 位置：不碰 selection、checked、hidden、deleted、expandedCardIds，
 * 也不清草稿。后端 submission.py 把纯几何变化归为 layout_only，不产生意图、不申请审批。
 */
function commitCardSize(box: ResizeBox) {
  const current = boardState.value;
  const cardId = resizeTargetId;
  resizing.value = null;
  if (!current || !cardId) return;
  const card = cardById(current, cardId);
  if (!card) return;
  const official = cardRect(card);
  const slot = layoutPlan.value.slots[cardId] ?? official;
  const temporary = isTemporaryDisplay(card, slot);
  const w = Math.max(1, Math.round(box.w));
  const h = Math.max(1, Math.round(box.h));
  if (temporary) {
    if (sameSize(w, official.w) && sameSize(h, official.h)) return;
    userSizedCards.value = new Set([...userSizedCards.value, cardId]);
    commit(updateCard(current, cardId, { w, h }), "调整卡片尺寸");
  } else {
    const x = Math.round(box.x);
    const y = Math.round(box.y);
    if (sameSize(x, official.x) && sameSize(y, official.y) && sameSize(w, official.w) && sameSize(h, official.h)) return;
    userSizedCards.value = new Set([...userSizedCards.value, cardId]);
    commit(updateCard(current, cardId, { x, y, w, h }), "调整卡片尺寸");
  }
  // 局部工具栏跟着卡片走：尺寸变了，它原来的位置可能压住卡片或跑出视口
  refreshOverlay();
}

/**
 * 尺寸把手只给**当前选中的卡片**：未选中的卡先点一下（选择）再调整。
 * 每张卡都出八向热区会把板面变成一片抓不到卡片的把手海。
 */
const resizeBoxes = computed<Record<string, LayoutRect>>(() => {
  const out: Record<string, LayoutRect> = {};
  if (resizeDisabled.value) return out;
  const current = boardState.value;
  if (!current) return out;
  for (const cardId of current.selection) {
    const card = current.cards.find((item) => item.id === cardId);
    if (!card || card.deleted) continue;
    /**
     * 折叠的卡片高度是 auto（只有一条标题），真实外框与板面尺寸不同：
     * 这时不摆把手，免得把手画在卡片外面（先展开再调尺寸）。
     */
    if (card.folded) continue;
    out[cardId] = displayRect(card);
  }
  return out;
});

const resizeLabels = computed<Record<string, string>>(() => {
  const out: Record<string, string> = {};
  const current = boardState.value;
  if (!current) return out;
  for (const cardId of Object.keys(resizeBoxes.value)) {
    out[cardId] = cardSummary(cardById(current, cardId)) + "：尺寸调整";
  }
  return out;
});

/** 关系编辑 / 拖线进行中：把手会让路，避免同一指针同时驱动两件事。 */
const resizeDisabled = computed(() => Boolean(editingLinkId.value || linkDraft.value));

/**
 * 「同一个指针只驱动一种手势」（0.2.5 §三.6）：开始任何一种手势前，把其余手势的临时状态
 * 一次清掉 —— 拖线中不移动 / 不拉伸 / 不成组，拉伸中不拖线，半条关系都不留。
 *
 * 只清**临时状态**与监听：不碰板面状态，也不写提示（提示由各条取消路径自己给）。
 * `linkDraft` 被清掉后 `resizeDisabled` 立刻复原，尺寸把手在同一帧就回到可用状态。
 */
function releaseGestures(): void {
  linkDraft.value = null;
  dragging.value = null;
  panning.value = null;
  rectSelect.value = null;
  resizing.value = null;
  // 尺寸手势自带 document / window 监听：必须走它自己的取消入口，不能只清显示
  resizeGesture.cancel();
  detachPointerListeners();
}

/** 取消进行中的拖线：清临时状态、摘掉监听，并如实说明没有建立任何关系。 */
function cancelLinkDraft(message: string): void {
  linkDraft.value = null;
  detachPointerListeners();
  notice.value = message;
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

/**
 * 板面坐标 → 屏幕坐标，**与真实渲染一致**：
 *   屏幕 = 容器左上 + 板面坐标 × 缩放 − 容器滚动量。
 *
 * 19 复核修正：原来定位用 `toScreenPoint(view, point, surfaceRect())`，而 surfaceRect() 是
 * 「内容原点」位置（已经把滚动量减掉了），view.x 又是 −滚动量 —— 滚动量被减了两次。
 * 滚动为 0 时碰巧正确，一旦平移过（滚动 ≠ 0）定位就会往错误方向/错误幅度移动，
 * 右侧与下侧离屏的预览因此永远进不了可用区。
 */
function screenPointOfBoard(boardX: number, boardY: number, viewRect: DOMRectLike): { x: number; y: number } {
  const live = liveScroll();
  const scale = Number.isFinite(view.value.scale) && view.value.scale > 0 ? view.value.scale : 1;
  return {
    x: finiteNumber(viewRect.left) + finiteNumber(boardX) * scale - live.x,
    y: finiteNumber(viewRect.top) + finiteNumber(boardY) * scale - live.y,
  };
}

function finiteNumber(value: unknown, fallback = 0): number {
  const result = typeof value === "number" ? value : Number(value);
  return Number.isFinite(result) ? result : fallback;
}

/**
 * 内容原点：板面 (0,0) 在屏幕上的位置 = 容器左上 − 滚动量（{@link effectiveRect}）。
 * 与 {@link contentViewport} **配套**使用：两者缺一个就会把滚动量算错（见下面的说明）。
 */
function surfaceRect(): DOMRectLike {
  const element = viewportEl.value;
  if (!element) return { left: 0, top: 0 };
  const rect = typeof element.getBoundingClientRect === "function"
    ? element.getBoundingClientRect()
    : { left: 0, top: 0 };
  const live = liveScroll();
  return effectiveRect(rect, live.x, live.y);
}

/**
 * 与 surfaceRect() 配套的视口：surfaceRect() 已经是**内容原点**（滚动量减过一次），
 * 所以这里的平移量必须留 0 —— 再传 view.value（x / y = −滚动量）就会减第二次。
 *
 * 这是本文件坐标换算的唯一口径：**屏幕 → 板面**走
 * toBoardPoint(contentViewport(), client, surfaceRect())，
 * **板面 → 屏幕**走 screenPointOfBoard(x, y, 容器原始矩形)，两者严格互逆。
 * （例外只有 zoomAtScroll：它自己的口径是「容器原始矩形 + scroll」，见 {@link viewportRect}。）
 */
function contentViewport(): Viewport {
  return { scale: view.value.scale, x: 0, y: 0 };
}

/**
 * 容器自身的屏幕矩形（**不含**滚动量）：zoomAtScroll 的口径是「容器原始矩形 + scroll」，
 * 它内部自己会减滚动量；传 surfaceRect()（内容原点）会让滚动量被减两次
 * —— 平移之后滚轮一滚画面就跳（指针下的板面点留不住）。
 */
function viewportRect(): DOMRectLike {
  const element = viewportEl.value;
  if (!element || typeof element.getBoundingClientRect !== "function") return { left: 0, top: 0 };
  const rect = element.getBoundingClientRect();
  return { left: finiteNumber(rect.left), top: finiteNumber(rect.top) };
}

/**
 * 屏幕坐标 → 板面坐标：必须与 {@link screenPointOfBoard} **严格互逆**：
 *   板面 = (屏幕 − 容器左上 + 滚动量) ÷ 缩放。
 *
 * surfaceRect() 给的是「内容原点」（已经把滚动量减掉），所以这里平移量必须传 0（{@link contentViewport}）。
 * 原来传的是 view.value（x / y = −滚动量）：滚动量被减了两次 —— 一旦平移过，
 * 拖线落点会整体偏移「滚动量 ÷ 缩放」：线画在卡片上，判定却落在别处。
 * 拖动看不出来（用的是位移差，偏移在 offset 里被减掉），而连接点拖线用的是**绝对坐标**，
 * 所以这个回归只在「先平移再拖线」时暴露（0.2.5 §四）。
 */
function boardPointOf(event: { clientX: number; clientY: number }): { x: number; y: number } {
  return toBoardPoint(contentViewport(), { x: event.clientX, y: event.clientY }, surfaceRect());
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
  /**
   * 只有容器**真的能滚动**时才以 DOM 为准回写（与 onViewportScroll 同一条口径）。
   * jsdom 没有真实布局：scrollWidth/scrollHeight 恒为 0，setter 会把滚动量钳到 0，
   * 无条件回写会让定位、平移、框选在测试环境里全部「算了却没动」，
   * 从而把真实缺陷掩盖成假通过、或把真实修复显示成失败。
   */
  const canScrollX = element.scrollWidth > element.clientWidth + 1;
  const canScrollY = element.scrollHeight > element.clientHeight + 1;
  if (canScrollX || canScrollY) {
    scroll.value = {
      x: canScrollX ? element.scrollLeft : scroll.value.x,
      y: canScrollY ? element.scrollTop : scroll.value.y,
    };
  }
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
    // 容器**原始**矩形 + scroll：zoomAtScroll 内部自己换算内容原点（见 viewportRect 的说明）
    viewportRect(),
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
  // 板面 → 屏幕统一走 screenPointOfBoard：不要再拿 surfaceRect()（内容原点）配 view.value（已含 −滚动量）
  const screen = screenPointOfBoard(rect.x, rect.y, viewRect);
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
  /**
   * 拖线进行中：这一次指针已经属于连接点，不再开始拖动（同一指针只驱动一种手势）。
   * 否则两个手势会同时挂着 window 监听，松手时只结掉其中一个 —— 剩下半条拖线卡在那里。
   */
  if (linkDraft.value) return;
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
    // 与 boardPointOf 同一口径：surfaceRect() 已是内容原点，平移量必须留 0（否则框选整体偏移）
    const area = rectFromDrag(contentViewport(), rect.startClient, rect.currentClient, surfaceRect());
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

/**
 * Esc / 指针中断 / 窗口失焦：不提交，卡片回到操作前位置。
 *
 * 所有临时手势（拖动 / 框选 / 平移 / 拖线 / 尺寸预览）都在这里一次清掉并摘掉监听：
 * 取消路径只有一个出口，不会留下「拖了一半的线」或「停不下来的尺寸拖动」。
 */
function onDragCancel() {
  const hadDrag = Boolean(dragging.value);
  const hadRect = Boolean(rectSelect.value);
  const hadPan = Boolean(panning.value);
  const hadLink = Boolean(linkDraft.value);
  releaseGestures();
  if (hadLink) {
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
  // 与局部工具栏同一个口径：板面 → 屏幕只走 screenPointOfBoard（滚动量不重复减）
  const screen = screenPointOfBoard(rect.x, rect.y, viewRect);
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

/**
 * 临时按键与进行中的手势复位（20）：失焦、页面隐藏、取消手势、卸载时都必须复位。
 *
 * 反例：按住空格切窗口，在别的窗口松开 —— 本窗口收不到 keyup，spaceDown 残留成 true，
 * 回来后普通拖动被当成框选。这里在 blur / visibilitychange(hidden) / Escape / 卸载
 * 四个出口统一复位，而不是各写一份。
 */
function resetTransientGesture() {
  spaceDown.value = false;
  /**
   * 拖线也必须在这一条出口里被清掉：原来只判拖动 / 框选 / 平移，
   * 于是「拖线途中切窗口再回来」会留下一条半截的草稿 —— 尺寸把手一直被禁用（resizeDisabled），
   * 用户再也拉不动卡片，只能靠 Esc 收拾。取消路径必须覆盖全部临时手势。
   */
  if (dragging.value || rectSelect.value || panning.value || linkDraft.value) onDragCancel();
}

function onWindowBlur() {
  resetTransientGesture();
}

function onVisibilityChange() {
  if (document.visibilityState === "hidden") resetTransientGesture();
}

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
      "article[data-im='card'], button, input, textarea, select, a, label, [data-im='card-toolbar'], [data-im='group'], [data-im='preview'], [data-im='link-draft'], [data-im='search'], [data-im='group-merge-hint'], [data-im='board-toolbar'], [data-im='resize-layer']",
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

// --- 关系连接点：宿主交给交互层（BoardResizeLayer） -------------------------

/**
 * 关系连接点的宿主：**恰好选中一张、未折叠**的卡片才有
 * （与改动前的显示条件逐条一致 —— 原条件写在 BoardCard 模板里：selected && !folded && !multi）。
 *
 * 为什么放在 BoardResizeLayer 那一层：连接点原来画在卡片内部（.board-surface 的缩放层叠
 * 上下文里），四边中点正好落在同层 10px 的尺寸热区下面，真实鼠标按下先被尺寸调整接走。
 * 现在它与尺寸把手共用**同一份显示坐标**（displayRect）与**同一个不随画布缩放的层**：
 * 位置 = displayRect × scale，缩到 25% 时命中区仍是屏幕像素。
 *
 * 这里**不**看 resizeDisabled：拖线 / 关系编辑进行中连接点照常在（与改动前一致）；
 * 尺寸把手让不让路由 resizeBoxes 自己决定。
 */
const connectBoxes = computed<Record<string, LayoutRect>>(() => {
  const out: Record<string, LayoutRect> = {};
  const current = boardState.value;
  const cardId = primarySelectedId.value;
  if (!current || !cardId) return out;
  const card = current.cards.find((item) => item.id === cardId);
  if (!card || card.deleted || card.folded) return out;
  out[cardId] = displayRect(card);
  return out;
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
 * 删除卡片时，它上面的编辑草稿的去向（收尾轮 07）。
 *
 * 这里只**登记**「这次板面变更成功后清掉这张卡的草稿」，不做真正的清除：
 * 删除还要经过影响确认与保存 —— 未确认、保存失败、用户取消的期间，
 * 草稿候选与本机恢复来源都必须原样留着（否则取消后新输入就找不回来了）。
 * 真正的清除由 store 在保存回执对应当前候选、且没有任何更新候选时按登记版本执行。
 * 没有草稿的卡片不登记，免得留下无意义的删除依据。
 */
function forgetCardDraft(cardId: string): void {
  if (!store.hasCardDraft(cardId)) return;
  store.requestDraftClear(cardDraftKey(cardId));
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

/**
 * 指针下的卡片：多个命中时取面积最小的（最具体）。
 *
 * **用当前显示矩形**（displayRect）：展开、让位、拖动、尺寸预览之后，卡片 / 连线 / 高亮画在哪，
 * 落点就判在哪 —— 用正式保存矩形（cardRect）会判在一个屏幕上不存在的旧位置上（0.2.5 §四）。
 * 入参 point 已经是板面坐标（boardPointOf 的结果），displayRect 也是板面坐标：
 * 全程板面坐标系比较，**不再乘除 scale**。displayRect 只读，临时槽位绝不写回板面。
 */
function hitCardAt(point: { x: number; y: number }, excludeId: string | null): string | null {
  const current = boardState.value;
  if (!current) return null;
  let best: { id: string; area: number } | null = null;
  for (const card of current.cards) {
    if (card.deleted || card.id === excludeId) continue;
    const rect = displayRect(card);
    if (!pointInRect(point.x, point.y, rect)) continue; // board.ts 的签名是 (x, y, rect)
    const area = rect.w * rect.h;
    if (!best || area < best.area) best = { id: card.id, area };
  }
  return best ? best.id : null;
}

function onConnectStart(cardId: string, event: PointerEvent) {
  event.preventDefault();
  /**
   * 同一指针只驱动一种手势（0.2.5 §三.6）：从连接点开始拖线时，把可能残留的卡片拖动 /
   * 平移 / 框选 / **尺寸调整** 全部清掉 —— 拖线过程中不移动、不拉伸、不成组。
   */
  releaseGestures();
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
    const screen = screenPointOfBoard(card.x, card.y, viewRect);
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
  /**
   * 19：预览的四个方向都要处理。原来只把「左/上离屏」拉回来，
   * 右侧/下侧离屏时不做任何事 —— 用户点了定位却仍看不到预览。
   *
   * 这里用一个统一的矩形约束：先算预览在屏幕上的框（含缩放后的宽高），
   * 再分别对左/右/上/下越界求位移；完全离屏的极远坐标也被拉回可用区域。
   * 数值一律先做有限性检查：NaN / 非有限值不参与计算，避免把滚动位置写坏。
   */
  const viewRect = element.getBoundingClientRect();
  const scale = Number.isFinite(view.value.scale) && view.value.scale > 0 ? view.value.scale : 1;
  const screen = screenPointOfBoard(bounds.x, bounds.y, viewRect);
  if (!Number.isFinite(screen.x) || !Number.isFinite(screen.y)) return;
  const width = (Number.isFinite(bounds.w) ? bounds.w : 0) * scale;
  const height = (Number.isFinite(bounds.h) ? bounds.h : 0) * scale;
  const margin = 60;
  const left = screen.x;
  const top = screen.y;
  const right = screen.x + width;
  const bottom = screen.y + height;
  let dx = 0;
  let dy = 0;
  if (left < viewRect.left + margin) dx = viewRect.left + margin - left;
  else if (right > viewRect.right - margin) dx = viewRect.right - margin - right;
  if (top < viewRect.top + margin) dy = viewRect.top + margin - top;
  else if (bottom > viewRect.bottom - margin) dy = viewRect.bottom - margin - bottom;
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
      resetTransientGesture();
      return;
    }
    // Escape 也复位临时按键：取消手势之后不该继续停在框选模式（20）
    spaceDown.value = false;
    if (linkDraft.value) {
      // 拖线取消 = 清临时状态 + 摘掉监听 + 如实说明：不建立关系，也不留半条链接
      cancelLinkDraft("已退出建立关系。");
      return;
    }
    if (editingLinkId.value) editingLinkId.value = null;
    return;
  }
  // 空格 + 拖动空白处 = 框选卡片；输入框 / 可编辑内容 / 聊天 / 菜单 / 确认框里空格正常输入
  if (event.code === "Space" || event.key === " ") {
    // 中文输入法选字确认（isComposing）里的空格不是板面手势，绝不劫持（20）
    if (event.isComposing) return;
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
  // 窗口尺寸变化会改变「板面可视范围」：展开卡的内收要跟着重算
  window.addEventListener("resize", onWindowResize);
  // 20：失焦与页面隐藏时复位临时按键与进行中的手势
  window.addEventListener("blur", onWindowBlur);
  document.addEventListener("visibilitychange", onVisibilityChange);
  window.addEventListener("qio:interactive:locate-preview", onLocatePreview as EventListener);
  window.addEventListener("qio:interactive:locate-card", onLocateCard as EventListener);
  refreshOverlay();
});

onBeforeUnmount(() => {
  viewportEl.value?.removeEventListener("wheel", onWheel);
  viewportEl.value?.removeEventListener("scroll", onViewportScroll);
  window.removeEventListener("keydown", onKeyDown);
  window.removeEventListener("keyup", onKeyUp);
  window.removeEventListener("resize", onWindowResize);
  window.removeEventListener("blur", onWindowBlur);
  document.removeEventListener("visibilitychange", onVisibilityChange);
  window.removeEventListener("qio:interactive:locate-preview", onLocatePreview as EventListener);
  window.removeEventListener("qio:interactive:locate-card", onLocateCard as EventListener);
  detachPointerListeners();
  // 卸载时同样复位：组件已经不在了，临时按键状态不许残留（20）
  resetTransientGesture();
  // 拖线 / 尺寸预览也一样：卸载后不留半条手势（0.2.5 §三.7）
  releaseGestures();
  // 可用区域是显示态桥：画布没了就清空，别的组件（添加菜单）回到既有兜底行为
  publishBoardArea(null);
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
          <div ref="surface" class="board-surface" :class="{ resizing: Boolean(resizing) }" :style="surfaceStyle">
          <BoardGroupFrame
            v-for="group in displayGroups"
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
            :card="renderCard(card)"
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
            :expanded-slots="expandedSlots"
          />

            <div v-if="rectSelect" class="select-rect" :style="rectStyle"></div>
          </div>

          <!--
            尺寸把手 + 关系连接点层：放在**未缩放**的滚动内容层里（不在 .board-surface 内部）。
            位置 = 显示槽位（displayRect）× 缩放，所以 10px 边 / 16px 角 / 18px 连接点在
            任何缩放下都是真实屏幕像素；整层 pointer-events: none，只有把手热区与连接点
            接管指针，卡片正文、组框与连线都不被挡住。连接点在同层更高 z-index 上：
            四边中点按下 = 拖线，其余边 / 角 = 尺寸调整，两种操作同时可用。
          -->
          <BoardResizeLayer
            :boxes="resizeBoxes"
            :scale="scaleSafe"
            :labels="resizeLabels"
            :disabled="resizeDisabled"
            :connect-boxes="connectBoxes"
            @handle-down="onResizeHandleDown"
            @nudge="onResizeNudge"
            @connect-start="onConnectStart"
          />
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
/*
 * 尺寸拖动中：关掉卡片的几何过渡（left / top / width / height）。
 * 卡片自己的过渡是「松手后归位」用的；拖动中还带着它，预览会滞后于指针（看着不跟手）。
 * 用父层类名 + :deep 关，避免动 BoardCard（子智能体 B 的写域）。
 */
.board-surface.resizing :deep(.card) { transition: none; }
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
