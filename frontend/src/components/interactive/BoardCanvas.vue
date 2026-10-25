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
  6. 拖线目标判定认**当前显示矩形**（displayRect）：拖动 / 尺寸预览期间卡片 / 连线 /
     高亮画在哪，落点就判在哪；这些临时显示坐标只读，绝不写回板面（0.2.5 §四）。
     展开阅读只切卡内内容，不产生第二套位置（_IML-COMMON.md §1 F1）。
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
  leaveGroupToFreeSpots,
  linkBetween,
  mergeGroups,
  normalizeState,
  participationOf,
  setParticipation,
  pointInRect,
  previewDrop,
  removeCard,
  removeLink,
  renameGroup,
  selectInRect,
  setGroupOrdered,
  setSelection,
  updateCard,
  updateLink,
  type DropPreview,
} from "../../interactive/board";
// 局部工具栏的落点决策（纯函数，单测在 interactive/__tests__/overlayPlacement.test.ts）
import {
  placeToolbarRelativeToCard,
  rectsIntersect,
  type OverlayRect,
} from "../../interactive/overlayPlacement";
import { prefersReducedMotion } from "../../utils/motion";
import {
  BOARD_SURFACE_H,
  BOARD_SURFACE_W,
  rebaseOrigin,
  rebaseToFit,
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
import { cardSizeLimits, DEFAULT_LAYOUT_GAP, type LayoutRect } from "../../interactive/cardLayout";
import { resolvePlacement, type PlacementResult } from "../../interactive/geometry";
// Phase 2 集成（task-14）：统一几何（Phase 2-A）/ 图片比例（Phase 2-C）/ 组端点（Phase 2-B）。
// 显示矩形、组缩放、避让都走同一份纯函数，画布不再自己维护第二套坐标。
import {
  avoidOverlaps,
  groupResizeFrameChanges,
  planGroupResize,
  resolveDisplayFrames,
  type FrameObject,
  type V2Rect,
} from "../../interactive/v2/geometry";
import {
  constrainCardSizeForCard,
  imageRatioForCard,
  isAspectSatisfied,
} from "../../interactive/renderers/imageDisplay";
import {
  GROUP_ENDPOINT_PREFIX,
  GROUP_HEADER_HEIGHT,
  GROUP_PAD_BOTTOM,
  GROUP_PAD_X,
  CARD_ANCHOR_GAP,
  cardEdgeAnchors,
  groupEdgeAnchors,
  parseEndpoint,
  type CardAnchor,
  type GroupAnchor,
} from "../../interactive/linkEndpoints";
import { publishBoardScale } from "../../interactive/submission";
import { useResizeGesture } from "../../composables/useResizeGesture";
import { vQioScrollbar } from "../../composables/useQioScrollbars";
import { chatObstacleFrame } from "../../interactive/chatFrameState";
import type { ResizeBox, ResizeHandle, SizeLimits } from "../../interactive/resize";
import { titlebarHeight } from "../../utils/titlebar";
import type { BoardCard as BoardCardModel, BoardGroup, BoardState, CardKind, Intent } from "../../interactive/types";
import BoardCard from "./BoardCard.vue";
import BoardGroupFrame from "./BoardGroupFrame.vue";
import BoardCardAnchorLayer from "./BoardCardAnchorLayer.vue";
import BoardGroupAnchorLayer from "./BoardGroupAnchorLayer.vue";
import BoardLinkLayer from "./BoardLinkLayer.vue";
import BoardPreviewLayer from "./BoardPreviewLayer.vue";
import BoardResizeLayer from "./BoardResizeLayer.vue";

/** 板面坐标系大小：卡片位置是板面状态，平移 / 缩放只是查看方式。 */
// 画布尺寸与卡片坐标夹取共用 board.ts 的同一对常量（板面坐标的唯一事实源）
const SURFACE_W = BOARD_SURFACE_W;
const SURFACE_H = BOARD_SURFACE_H;
//: 内容之外的留白：拖到边界后还能继续往外拖（画布会跟着长）
const SURFACE_PAD = 400;

/*
  画布尺寸 = max(最小画布, 内容包围盒 + 留白)（2026-10-14 用户要求「板面无限」，方案 B）。
  原来固定 2400×1600：卡片一碰边就被夹住，越不过去。
  现在画布随内容增长 —— 卡片能往右下拖到任意远，滚动范围跟着变。
  左上仍以 0 为界（坐标原点不漂，见 board.ts 的 clampCardRect 说明）。
*/
const surfaceSize = computed(() => {
  let maxX = 0;
  let maxY = 0;
  for (const card of liveCards.value) {
    if (card.deleted) continue;
    maxX = Math.max(maxX, card.x + card.w);
    maxY = Math.max(maxY, card.y + card.h);
  }
  return {
    w: Math.max(SURFACE_W, Math.ceil(maxX + SURFACE_PAD)),
    h: Math.max(SURFACE_H, Math.ceil(maxY + SURFACE_PAD)),
  };
});

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
  /** 当前指针在板面坐标系中的位置：组落点看它，不看卡片左上角。 */
  pointer: { x: number; y: number };
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
const linkEditorOpen = ref(false);
const linkEditorPos = ref<{ x: number; y: number } | null>(null);
const linkMeaning = ref("");
const linkDirection = ref(false);
/** 指针悬停的组（纯显示）：只让该组的已有锚点从圆点变短条。 */
const hoveredGroupId = ref<string | null>(null);
/** 指针悬停的卡片（纯显示）：只让该卡已有的锚点从圆点变短条。 */
const hoveredCardId = ref<string | null>(null);
const notice = ref("");

/**
 * 拖线目标按**端点类型**拆开：卡片给卡片高亮 / 「连接到卡片」，
 * 组给组框高亮 / 「连接到组」。这里只读 linkDraft.targetId，
 * 不把组里的卡片目标升级成组，也不反过来降级。
 */
const linkTarget = computed(() => {
  const raw = linkDraft.value?.targetId ?? "";
  const parsed = parseEndpoint(raw);
  if (!parsed.ok) return { kind: null as "card" | "group" | null, cardId: null as string | null, groupId: null as string | null };
  if (parsed.endpoint.kind === "group") {
    return { kind: "group" as const, cardId: null, groupId: parsed.endpoint.id };
  }
  return { kind: "card" as const, cardId: parsed.endpoint.id, groupId: null };
});
/**
 * 板面操作提示**不常驻**（2026-10-14 修）。
 *
 * 原来 `.view-hint` 是无条件渲染的：一进板面就永远占着顶部一行，与互动空间既有规则冲突 ——
 * ChatDock 里明确写着「快捷键说明只留一处（帮助里），面板里不再常驻小字」。
 * 现在只在本机**还没用过**这三个查看手势时显示一条，用过一次就永久收起；
 * 完整说明同时留在 ChatDock 的帮助里（板面手势一节）。
 *
 * 读写沿用项目既有的防御式 localStorage 范式（chatFrame.ts / drafts.ts）：
 * 隐私模式或企业策略下连读属性都可能抛异常，抛了就退回「本次会话内存」。
 */
const BOARD_HINT_KEY = "qio-board-hint-seen";
const hintSeen = ref(readBoardHintSeen());

function readBoardHintSeen(): boolean {
  try {
    return (globalThis as { localStorage?: Storage | null }).localStorage?.getItem(BOARD_HINT_KEY) === "1";
  } catch {
    return false;
  }
}

/** 手势结束时调用（不是开始时）：避免收起提示造成的高度变化发生在拖动过程中。 */
function markBoardHintSeen(): void {
  if (hintSeen.value) return;
  hintSeen.value = true;
  try {
    (globalThis as { localStorage?: Storage | null }).localStorage?.setItem(BOARD_HINT_KEY, "1");
  } catch {
    // 存不进去也不影响：内存里已经记住，本次会话不再显示
  }
}

const highlightId = ref<string | null>(null);
const locatedIntentId = ref<string | null>(null);
/**
 * 工具栏的浮层位置：left / top 是**相对工具栏宿主（.card-toolbar-host）定位原点**的屏幕像素。
 * visible = 「所属卡片 ∩ 板面可视区」且「工具栏屏幕外框 ∩ 板面可视区」都相交
 * （严格相交；两者缺一才隐藏，部分相交照常显示）。
 * 它只决定显隐：位置公式不受影响，任何视口边缘都不改变相对位置。
 */
const overlay = ref({ left: 0, top: 0, visible: false });
/**
 * 当前所有者是否已经完成过一次测量。
 * 未完成时先不显示：否则切换所有者的那一帧会用**上一位所有者**的位置闪一下
 * （用户报告的「偏移」里最刺眼的一种）。测量完成后立刻置真。
 */
const overlaySettled = ref(false);
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

/**
 * F02（Lead 集成修复）：新链意图视图已经由 IntentLayer 画在板面坐标系里时，
 * 旧链 BoardPreviewLayer 不得再把同一条意图画一份虚线预览 ——
 * 否则同一意图同时出现两套卡 / 两套操作面，用户会看到重复对象并可能在两边各点一次。
 * 新链没有对应视图时（纯旧链兼容记录）仍按原行为画预览。
 */
const newChainIntentIds = computed(() => new Set(store.intentViews.map((view) => String(view.intent_id))));

const previewIntents = computed<Intent[]>(() =>
  store.intents.filter(
    (item) =>
      OPEN_INTENT_STATUSES.includes(item.status) &&
      item.preview &&
      !newChainIntentIds.value.has(String(item.id)),
  ),
);

const liveCards = computed(() => (boardState.value?.cards ?? []).filter((card) => !card.deleted));
const groups = computed(() => boardState.value?.groups ?? []);
const links = computed(() => boardState.value?.links ?? []);
const selection = computed(() => boardState.value?.selection ?? []);

// --- Phase 2 集成状态（全部是显示态 / 临时手势，不写板面、不进材料范围） -----

/** 组收起：纯显示状态，只决定「组内成员卡片是否还单独画」。 */
const collapsedGroupIds = ref<string[]>([]);
const collapsedGroupSet = computed(() => new Set(collapsedGroupIds.value));
/** 收起组的成员：只隐藏卡片本体，仍是正式状态与全部连线的真实端点。 */
const hiddenMemberIds = computed(() => {
  const hidden = new Set<string>();
  for (const group of groups.value) {
    if (!collapsedGroupSet.value.has(group.id)) continue;
    for (const cardId of group.members) hidden.add(cardId);
  }
  return hidden;
});
/** 真正画在板面上的卡片：收起的组只留组框概览。 */
const renderedCards = computed(() => liveCards.value.filter((card) => !hiddenMemberIds.value.has(card.id)));

/** 参与范围（§5.1 participation 是唯一事实源）：给连线层与卡片参与控件。 */
const includedCardIds = computed(() =>
  liveCards.value.filter((card) => participationOf(card) === "included").map((card) => card.id),
);

interface GroupDragState {
  groupId: string;
  delta: { x: number; y: number };
  moved: boolean;
  startClient: { x: number; y: number };
}
/** 组标题拖动整组：只预演（显示坐标），松手才一次提交。 */
const groupDrag = ref<GroupDragState | null>(null);

type GroupResizeEdge = "e" | "s" | "se";

interface GroupResizePreview {
  slots: Record<string, V2Rect>;
  frame: V2Rect;
  issues: string[];
}
interface GroupResizeState {
  groupId: string;
  edge: GroupResizeEdge;
  moved: boolean;
  preview: GroupResizePreview | null;
}
/** 组框缩放：成员槽位由 A 的 planGroupResize 算，拖动中只预演，松手一次提交。 */
const groupResize = ref<GroupResizeState | null>(null);

function groupRect(group: BoardGroup): V2Rect {
  return { x: group.x, y: group.y, w: group.w, h: group.h };
}

/** 收起 / 展开一个组（纯显示状态；不改成员、顺序、材料范围）。 */
function onGroupToggleCollapse(groupId: string, collapsed: boolean): void {
  const next = new Set(collapsedGroupSet.value);
  if (collapsed) next.add(groupId);
  else next.delete(groupId);
  collapsedGroupIds.value = [...next];
  notice.value = collapsed ? "已收起这个组：只显示组框概览，成员没有变化。" : "已展开这个组。";
}

function isGroupCollapsed(groupId: string): boolean {
  return collapsedGroupSet.value.has(groupId);
}

/** 传给连线层：收起组的成员的对外连线汇聚到组边缘（真实端点身份不变）。 */
const foldedGroupIds = computed(() =>
  [...collapsedGroupSet.value].filter((id) => groups.value.some((group) => group.id === id && !group.deleted)),
);

/** 组拖动 / 缩放的临时状态在切换手势时一次清掉（与既有手势同一条规则）。 */
function releaseGroupGestures(): void {
  groupDrag.value = null;
  groupResize.value = null;
}

// --- 显示帧：A 的 geometry.resolveDisplayFrames 是唯一来源 --------------------
//
// 拖动卡片 / 整组时，卡片、组框、连线、命中测试读**同一份**显示帧（契约 §11.10）：
// 拖动中线与组框实时跟随，松手前板面状态一个字段都不改。

/** 当前有效的临时位移（卡片或整组）；没有拖动时为 null。 */
const liveDragDelta = computed<{ id: string; deltaX: number; deltaY: number } | null>(() => {
  if (groupDrag.value) {
    return { id: groupDrag.value.groupId, deltaX: groupDrag.value.delta.x, deltaY: groupDrag.value.delta.y };
  }
  const drag = dragging.value;
  if (!drag || !drag.moved) return null;
  const current = boardState.value;
  const card = current ? cardById(current, drag.cardId) : null;
  if (!card) return null;
  return { id: drag.cardId, deltaX: drag.x - card.x, deltaY: drag.y - card.y };
});

const groupIdByCard = computed(() => {
  const map = new Map<string, string>();
  for (const group of groups.value) {
    for (const cardId of group.members) map.set(cardId, group.id);
  }
  return map;
});

/** 每个卡片 / 组的这一帧显示矩形（板面坐标；拖动组时组框与成员同位移）。 */
const displayFrameIndex = computed<Record<string, V2Rect>>(() => {
  const current = boardState.value;
  if (!current) return {};
  const groupOf = groupIdByCard.value;
  const objects: FrameObject[] = [];
  for (const card of liveCards.value) {
    objects.push({ id: card.id, kind: "card", frame: cardRect(card), groupId: groupOf.get(card.id) ?? null });
  }
  for (const group of groups.value) {
    objects.push({ id: group.id, kind: "group", frame: groupRect(group), groupId: null });
  }
  return resolveDisplayFrames({ objects, drag: liveDragDelta.value }).frames;
});

// --- 显示几何：正式矩形是唯一权威（F1；展开不再产生临时槽位） ---------------

/** 窗口尺寸变化后重新读一次容器尺寸（clientWidth 不是响应式的） */
const stageTick = ref(0);
function onWindowResize(): void {
  stageTick.value += 1;
  // 窗口尺寸变化会改变可视区与宿主几何：重新测量一次
  requestPlacement();
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
 * **实际可用屏幕区域**：容器可视矩形扣掉自绘标题栏、底部工具栏、审批浮层与对话窗口。
 *
 * 与 boardRegion（板面坐标版本）共用这一份扣减顺序：展开阅读、定位、添加菜单看到的
 * 是同一块「用户真的能看到 / 点到」的区域，不会各自算一套。量不到容器几何时返回 null
 * （jsdom / 未挂载），调用方回落到既有口径。
 */
function usableScreenBox(): ScreenBox | null {
  const box = viewportScreenBox();
  if (!box) return null;
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
  return work;
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
  const usable = usableScreenBox();
  if (!box || !usable) {
    // 量不到屏幕几何（jsdom / 还没挂载）：保持既有口径 —— 只用容器宽高，原点在板面原点
    return { origin: { x: 0, y: 0 }, size: stageSize.value };
  }
  const work: ScreenBox = usable;
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
 * 把画布缩放发布给提交路径（F6 冻结视图的 view.scale）。
 *
 * 与上面那块**同一个模式**：只读显示态，不进板面状态、不产生表达、不排保存；
 * 画布没挂载时提交侧如实拿 null，绝不编造一个 scale。缩放只是查看方式，不构成新意图。
 */
watch(scaleSafe, (scale) => publishBoardScale(scale), { immediate: true });

/**
 * 某张卡这一帧画在哪：**正式矩形就是唯一权威**（F1，恒等映射）。
 *
 * 展开阅读只切卡内内容（BoardCard 的 reading 态），不改任何卡片的外框；
 * 缩放 / 平移 / 窗口变化 / 浮层开合都不参与这里的计算。
 */
function slotOf(card: BoardCardModel): LayoutRect {
  return cardRect(card);
}

/**
 * 卡片、组框、连线、尺寸把手**共用**的这一帧显示矩形。
 * 优先级：拖动中的实时坐标 > 尺寸拖动预览（已过几何约束） > 正式矩形。
 *
 * 这里**没有**第三套位置：展开不产生任何临时外框（F1）。
 */
function displayRect(card: BoardCardModel): LayoutRect {
  const slot = slotOf(card);
  // 单卡尺寸预览（未被图片比例约束后的最终帧）
  const size = resizing.value;
  if (size && size.cardId === card.id) return size.box;
  // 组缩放预览：成员槽位是这一帧的显示矩形（图片比例已在 planGroupResize 里约束）
  const groupSlot = groupResize.value?.preview?.slots?.[card.id];
  if (groupSlot) return groupSlot;
  // 拖动（卡片 / 整组）：统一读 A 的显示帧，连线与命中用同一份坐标
  const frame = displayFrameIndex.value[card.id];
  if (frame && (frame.x !== slot.x || frame.y !== slot.y || frame.w !== slot.w || frame.h !== slot.h)) {
    return frame;
  }
  return slot;
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
  /**
   * 没有任何进行中的手势（单卡拖动 / 单卡尺寸预览 / 整组拖动 / 组框缩放预览）时原样返回，
   * 避免无谓的对象重建；有手势时逐张走 displayRect，连线层与组框读同一份临时坐标。
   */
  const gestureActive = Boolean(moved || size || groupDrag.value || groupResize.value?.preview);
  if (!gestureActive) return liveCards.value;
  return liveCards.value.map((card) => {
    const rect = displayRect(card);
    if (rect.x === card.x && rect.y === card.y && rect.w === card.w && rect.h === card.h) return card;
    return { ...card, x: rect.x, y: rect.y, w: rect.w, h: rect.h };
  });
});

/**
 * 组框也走**显示坐标**：组框的存储外框来自成员**正式位置**（board.ts 的 reflowGroup 口径），
 * 卡片被拖动 / 预览尺寸之后必须跟着重算 —— 否则框不住成员，读起来像是「分组变了」。
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
    const collapsed = collapsedGroupSet.value.has(group.id);
    if (left === group.x && top === group.y && right === group.x + group.w && bottom === group.y + group.h) {
      // 收起组即使成员没动过，也要把显示外框压成紧凑框：连线 / 锚点 / 命中
      // 必须和 BoardGroupFrame 的 120px 外框读同一份几何（存储几何保持不变）。
      return collapsed ? { ...group, h: Math.min(120, Math.max(56, group.h)) } : group;
    }
    const display = { ...group, x: left, y: top, w: Math.max(1, right - left), h: Math.max(1, bottom - top) };
    // 收起组：显示外框压成标题 + 一行概览，连线/命中读同一份紧凑框；存储的组几何不变。
    return collapsed
      ? { ...display, h: Math.min(120, Math.max(56, display.h)) }
      : display;
  });
});

/**
 * 组端点锚点：只保留**真实组端点**用到的边缘位置（组收起时的成员汇聚不算）。
 * 几何直接复用 buildDisplayLinks 的输出，因此锚点与线端点逐字段一致；
 * 没有组端点关系时返回空数组 —— 组框上不会常驻四个端口。
 */
const groupAnchors = computed<GroupAnchor[]>(() =>
  groupEdgeAnchors({
    links: links.value,
    cards: displayCards.value,
    groups: displayGroups.value,
    foldedGroupIds: foldedGroupIds.value,
    includedCardIds: includedCardIds.value,
    onlyInRange: false,
  }),
);

/**
 * 卡片端点锚点：只画真实卡片端点已使用的位置。
 * 选中卡片仍由 BoardResizeLayer 的短条端口承载，避免同一边出现两套端口；
 * 未选中卡片只显示已连接空心点，悬停时由锚点层变短条。
 */
const cardAnchors = computed<CardAnchor[]>(() =>
  cardEdgeAnchors({
    links: links.value,
    cards: displayCards.value,
    groups: displayGroups.value,
    foldedGroupIds: foldedGroupIds.value,
    includedCardIds: includedCardIds.value,
    onlyInRange: false,
  }),
);

/** 选中或悬停的卡片补四个可拖端口；已连接侧仍复用真实锚点，避免重复。 */
const hoverCardAnchors = computed<CardAnchor[]>(() => {
  const ids = new Set<string>(selection.value);
  if (hoveredCardId.value) ids.add(hoveredCardId.value);
  if (!ids.size) return [];
  const out: CardAnchor[] = [];
  for (const card of displayCards.value) {
    if (!ids.has(card.id) || card.deleted) continue;
    const left = card.x;
    const top = card.y;
    const right = card.x + card.w;
    const bottom = card.y + card.h;
    const midX = left + card.w / 2;
    const midY = top + card.h / 2;
    const endpoint = card.id;
    const make = (side: CardAnchor["side"], x: number, y: number): CardAnchor => ({
      cardId: card.id,
      endpoint,
      side,
      x,
      y,
      linkIds: [],
    });
    out.push(
      make("top", midX, top - CARD_ANCHOR_GAP),
      make("right", right + CARD_ANCHOR_GAP, midY),
      make("bottom", midX, bottom + CARD_ANCHOR_GAP),
      make("left", left - CARD_ANCHOR_GAP, midY),
    );
  }
  return out;
});

const renderedCardAnchors = computed<CardAnchor[]>(() => {
  const keys = new Set(cardAnchors.value.map((anchor) => anchor.cardId + ":" + anchor.side));
  return [
    ...cardAnchors.value,
    ...hoverCardAnchors.value.filter((anchor) => !keys.has(anchor.cardId + ":" + anchor.side)),
  ];
});

const TOOL_COUNT = 5;
const TOOL_STEP = 36;
const TOOL_START = 8;
const TOOL_MARGIN = 14;

/**
 * 逐按钮实时位置（相对卡片左上角）。
 * 上边容量用尽后，最左侧按钮移到左边并压住前一个；
 * 左边容量也用尽后自动进入第二列。
 */
function toolbarButtonPositions(card: BoardCardModel): Array<{ x: number; y: number }> {
  const rect = displayRect(card);
  const topCapacity = Math.max(0, Math.min(TOOL_COUNT, Math.floor((rect.w / 2 - TOOL_MARGIN) / TOOL_STEP)));
  const leftCount = TOOL_COUNT - topCapacity;
  const leftCapacity = Math.max(1, Math.floor((rect.h / 2 - TOOL_MARGIN) / TOOL_STEP));
  const out: Array<{ x: number; y: number }> = [];
  for (let i = 0; i < TOOL_COUNT; i += 1) {
    if (i < leftCount) {
      const rank = leftCount - 1 - i;
      const column = Math.floor(rank / leftCapacity);
      const row = rank % leftCapacity;
      out.push({ x: -42 - column * TOOL_STEP, y: TOOL_START + row * TOOL_STEP });
    } else {
      out.push({ x: TOOL_START + (i - leftCount) * TOOL_STEP, y: -42 });
    }
  }
  return out;
}

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
    // 预览与提交共用同一份几何约束：松手后的外框逐字段等于最后一帧预览（A4/A5）
    resizing.value = { cardId: resizeTargetId, box: resolveResizeBox(box).rect };
    // 尺寸预览改变卡片外框：工具栏逐帧跟随（走合并调度，不直接写位置）
    requestPlacement();
  },
  onCommit: (box) => commitCardSize(box),
  onCancel: () => {
    resizing.value = null;
  },
});

/**
 * 尺寸口径：只夹进卡片口径 [CARD_MIN, CARD_MAX]。
 * **视野大小永远不是卡片上限**（F2/A4）：空白区域可以继续扩大，卡片可以大于可视区、
 * 可以部分 / 完全在屏外；这里不再有任何「到可用区域右下角的距离」这种视野口径。
 */
function clampToCardCaliber(box: ResizeBox): ResizeBox {
  const limits = resizeLimits.value;
  const anchor = resizeStartBox.value;
  let w = Math.max(Math.min(limits.minW, anchor.w), Math.min(limits.maxW, box.w));
  let h = Math.max(Math.min(limits.minH, anchor.h), Math.min(limits.maxH, box.h));
  /**
   * Phase 2-C（契约 §5.2）：图片卡的宽高必须满足原图比例，所有尺寸路径共用同一份约束。
   * 非图片卡 ratio 为 null，**一个字段都不改**（旧口径原样保留）。
   * driver=auto：拖哪条边就以变化更大的那一维为准，东西 / 南北拖动都跟手。
   */
  const cardId = resizeTargetId;
  const current = boardState.value;
  const card = cardId && current ? cardById(current, cardId) : null;
  if (card && imageRatioForCard(card) !== null) {
    const constrained = constrainCardSizeForCard(
      card,
      { w: anchor.w, h: anchor.h },
      { w, h },
      {
        driver: "auto",
        limits: {
          minW: limits.minW,
          minH: limits.minH,
          maxW: limits.maxW,
          maxH: limits.maxH,
        },
      },
    );
    w = constrained.w;
    h = constrained.h;
  }
  if (w === box.w && h === box.h) return box;
  // 夹紧后保持被拖动的边（对边不动）：拉西 / 拉北时回推左上角
  const x = box.x === anchor.x ? box.x : anchor.x + anchor.w - w;
  const y = box.y === anchor.y ? box.y : anchor.y + anchor.h - h;
  return { x, y, w, h };
}

/**
 * 这一帧的尺寸结果：**实时预览与最终提交都走这里**（A4/A5）。
 *
 * - 尺寸按 {@link clampToCardCaliber} 收敛到卡片口径；
 * - 位置 / 外框再经 `geometry.resolvePlacement` 的 **bounded** 策略：
 *   只把本次被拉伸的那条边停在「第一个碰撞邻卡边界 − gap」处，**绝不移动别的卡片**；
 * - `anchor` 是按下把手时的起始框，用来判断哪条边被拉了（西 / 北把手要对边不动）。
 */
function resolveResizeBox(box: ResizeBox): { rect: ResizeBox; reason: PlacementResult["reason"] } {
  const clamped = clampToCardCaliber(box);
  const current = boardState.value;
  const cardId = resizeTargetId;
  const others = (current?.cards ?? [])
    .filter((item) => !item.deleted && item.id !== cardId)
    .map((item) => cardRect(item));
  const result = resolvePlacement({
    rect: clamped,
    others,
    policy: "bounded",
    gap: DEFAULT_LAYOUT_GAP,
    anchor: resizeStartBox.value,
  });
  return { rect: result.rect, reason: result.reason };
}

/** 按下 / 键盘步进前先把这一帧的槽位快照给手势，保证拖动期间稳定。 */
function beginResize(cardId: string): LayoutRect | null {
  const current = boardState.value;
  const card = current ? cardById(current, cardId) : null;
  if (!card) return null;
  const rect = displayRect(card);
  resizeTargetId = cardId;
  resizeStartBox.value = { x: rect.x, y: rect.y, w: rect.w, h: rect.h };
  // 卡片口径（geometry 常量），与视野无关；视野不再参与上限
  const limits = cardSizeLimits();
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
 * 图片卡把尺寸收敛到原图比例（契约 §5.2）。
 * 非图片卡（ratio=null）原样返回 —— 这条路径**只**服务图片，不改任何别的卡片口径。
 */
function constrainImageCardState(state: BoardState, cardId: string): BoardState {
  const card = cardById(state, cardId);
  if (!card) return state;
  const ratio = imageRatioForCard(card);
  // 已经满足比例（含像素取整的 2% 容差）就不动尺寸：避免每次落点都改 ±1px
  if (ratio === null || isAspectSatisfied({ w: card.w, h: card.h }, ratio)) return state;
  const size = constrainCardSizeForCard(
    card,
    { w: card.w, h: card.h },
    { w: card.w, h: card.h },
    { driver: "width" },
  );
  if (sameSize(size.w, card.w) && sameSize(size.h, card.h)) return state;
  return updateCard(state, cardId, { w: size.w, h: size.h });
}

/**
 * 松手 / 键盘步进：**一次** `updateCard` + `store.commit`，标签固定「调整卡片尺寸」。
 *
 * 提交口径（F1/F2 之后只有一条）：外框就是正式几何，提交 { x, y, w, h }；
 * 「展开中 / 被让位只提交 w/h」那条旧口径随 F1 一起消失（展开不再有临时槽位）。
 *
 * 最终碰撞检查走 `geometry.resolvePlacement`（A5），结果与最后一帧预览逐字段一致
 * （同一个 resolveResizeBox）。无法满足时给一句简短原因。
 *
 * 只改尺寸 / 位置：不碰 selection、checked、hidden、deleted、expandedCardIds，也不清草稿。
 * 后端 submission.py 把纯几何变化归为 layout_only，不产生意图、不申请审批。
 */
function commitCardSize(box: ResizeBox) {
  const current = boardState.value;
  const cardId = resizeTargetId;
  resizing.value = null;
  if (!current || !cardId) return;
  const card = cardById(current, cardId);
  if (!card) return;
  const official = cardRect(card);
  const resolved = resolveResizeBox(box);
  const x = Math.round(resolved.rect.x);
  const y = Math.round(resolved.rect.y);
  const w = Math.max(1, Math.round(resolved.rect.w));
  const h = Math.max(1, Math.round(resolved.rect.h));
  const changed =
    !sameSize(x, official.x) || !sameSize(y, official.y) || !sameSize(w, official.w) || !sameSize(h, official.h);
  if (changed) {
    commit(updateCard(current, cardId, { x, y, w, h }), "调整卡片尺寸");
  }
  // 无法满足时给一句简短原因（A5）：被邻卡挡住 / 附近真的没有空位
  if (resolved.reason === "bounded") {
    notice.value = "尺寸已停在相邻卡片的边界上（不会移动其他卡片）。";
  } else if (resolved.reason === "overlap") {
    notice.value = "附近没有空位：已停在最接近可行的位置。";
  }
  // 局部工具栏跟着卡片走（走合并调度：同一帧最多一次测量、一次位置写入）
  requestPlacement();
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
    // 收起的组里成员卡片不画，把手也就不该出现在空处
    if (hiddenMemberIds.value.has(cardId)) continue;
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
const resizeDisabled = computed(() => Boolean(linkDraft.value));

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
  hoveredGroupId.value = null;
  releaseGroupGestures();
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
  width: surfaceSize.value.w * view.value.scale + "px",
  height: surfaceSize.value.h * view.value.scale + "px",
  transform: `scale(${view.value.scale})`,
  transformOrigin: "0 0",
}));

const contentStyle = computed(() => ({
  width: surfaceSize.value.w * view.value.scale + "px",
  height: surfaceSize.value.h * view.value.scale + "px",
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
  if (!element) return { w: surfaceSize.value.w, h: surfaceSize.value.h };
  return { w: element.clientWidth || surfaceSize.value.w, h: element.clientHeight || surfaceSize.value.h };
}

/** 把滚动量写回容器（滚动量本身已经是坐标换算的事实来源）。 */
function applyScroll(next: { x: number; y: number }) {
  scroll.value = clampScroll(next, view.value.scale, containerSize(), { w: surfaceSize.value.w, h: surfaceSize.value.h });
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
    { w: surfaceSize.value.w, h: surfaceSize.value.h },
  );
  view.value = { scale: result.scale, x: -result.scroll.x, y: -result.scroll.y };
  applyScroll(result.scroll);
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
    pointer: point,
    preview: previewDrop(current, cardId, card.x, card.y, point),
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
    drag.pointer = point;
    // 只预演：板面状态在松手前不变
    drag.preview = previewDrop(current, drag.cardId, x, y, drag.pointer);
    // 拖动中也走同一个入口：只置脏标记、由同一帧 rAF 做一次测量与一次写入
    requestPlacement();
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
    // 目标可以是卡片，也可以是组框（group:<id>，契约 §5.3）
    draft.targetId = hitEndpointAt(draft.point, draft.fromId);
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
      requestPlacement();
      return;
    }
    const result = dropCard(current, drag.cardId, drag.x, drag.y, drag.pointer);
    const formed = !drag.fromGroupId && Boolean(result.groupId);
    const label = dragLabel(drag, result.groupId, result.merged, formed);
    // 时间戳每次都不同，不能直接比较 JSON：只比较**真实含义**（位置 / 成员 / 顺序 / 选择 / 链接 / 勾选等）
    const changed = stateDiffers(normalizeState(current), result.state);
    if (changed) {
      /*
        2026-10-14（方案 B 第二步：左上也要无限）：
        卡片被拖到负坐标（画布左上之外）时，把**整块板面整体平移**回 ≥0 的区域，
        并把视图滚动量补上同样的量 —— 卡片在屏幕上原地不动，用户察觉不到坐标系换了原点。
        这样卡片的 x/y 永远是"相对画布原点"的非负值，渲染 / 连线 / 组框 / 工具栏换算都不用改。
      */
      // 图片卡落点后再收敛一次尺寸（比例来自 meta.display；非图片卡不受影响）
      let nextState = constrainImageCardState(result.state, drag.cardId);
      // 阈值是 0：只有真的拖到负坐标才重定位（用留白当阈值会让普通拖动也整体平移）
      const rebase = rebaseToFit(nextState, 0);
      if (rebase.dx || rebase.dy) {
        nextState = rebaseOrigin(nextState, rebase.dx, rebase.dy);
        const scale = view.value.scale;
        applyScroll({
          x: scroll.value.x + rebase.dx * scale,
          y: scroll.value.y + rebase.dy * scale,
        });
      }
      commit(nextState, label);
      /*
        2026-10-14（用户要求「把这个删掉」）：**只移动位置不再弹提示**。
        位置不构成意图依据这件事已经写在提交区与操作说明里，每次拖动都弹一条属于噪音；
        改变了结构的结果（成组 / 合并）仍然提示，因为它会真的改变板面组织方式。
      */
      if (result.groupId) {
        notice.value = label + "：已放入组（板面会自动保存，不会调用 QIO）。";
      }
    } else {
      notice.value = "卡片没有移动：板面没有产生改动，也没有保存。";
    }
    requestPlacement();
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
    if (pan.moved) markBoardHintSeen();
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
    if (rect.moved) markBoardHintSeen();
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
  /*
    2026-10-14（用户要求删掉这条粉边栏，两次反馈）：纯位置变化**不提示** ——
    拖动中它只是一直挂在头顶的噪音，而「位置不构成意图依据」已经写在提交区与操作说明里。
    返回空串，配合模板上的 v-if="dragging && dragHint" 让这条横幅根本不出现。
  */
  return "";
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
  // 用户已经用上查看手势：这条一次性提示可以收起了（手势结束后再收，不在拖动中改高度）
  markBoardHintSeen();
}

/**
 * 框选虚线的样式：**板面单位**。
 *
 * .select-rect 画在 .board-surface 内部，而那一层带 transform: scale() —— 它的坐标就是板面坐标，
 * 缩放由 surface 统一作用**一次**。所以这里必须给板面矩形：与松手时 selectInRect 用的是
 * 同一条换算（rectFromDrag + contentViewport），不再做视口 / 缩放换算。
 *
 * 30：原来按「视口相对像素」给 left/top/width/height —— 平移之后虚线整体偏 −滚动量
 * （选区判定是对的，画出来的框是错的），缩放 ≠ 1 时再被 surface 的 transform 二次缩放。
 * 现在板面单位 = 所见即所得：拖动全程跟着指针，缩放只作用一次。
 */
const rectStyle = computed(() => {
  const rect = rectSelect.value;
  if (!rect || !viewportEl.value) return {};
  const area = rectFromDrag(contentViewport(), rect.startClient, rect.currentClient, surfaceRect());
  return {
    left: area.x + "px",
    top: area.y + "px",
    width: area.w + "px",
    height: area.h + "px",
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

// --- 卡片局部工具栏：独立宿主 + 真实 DOM 锚点 + 单帧合并调度 ----------------
/*
  2026-10-14 第二轮（用户报告「工具栏与卡片有偏移、松手后还会漂」）：
  工具栏原来渲染在 .board-surface 内部（position: relative + transform: scale），
  行内 left/top 写的却是客户端像素差值 —— 浏览器再乘一次缩放，坐标系直接错；
  刷新用的锚点又走 card.x / card.y（正式板面坐标），与卡片本体使用的 displayRect()
  不是同一份坐标，于是「拖动中正确、松手后偏移」。
  现在两条都换掉：
  1. 工具栏由 BoardCard 用 Teleport 送进本组件自己的 .card-toolbar-host（不缩放、不滚动，
     与 .board-viewport 并列），行内 left/top 的语义 = 相对宿主定位原点的屏幕像素；
  2. 锚点唯一来源：所属卡片 <article> 的真实 getBoundingClientRect()
     （只有 jsdom 没有真实布局时才退回「板面坐标 × 缩放」的数学结果，见 measureAnchoredCard）。
  相对位置固定：左边缘对齐卡片左边缘，底边在卡片顶边上方 TOOLBAR_GAP 屏幕像素；
  不做夹取 / 翻转 / 侧移。卡片与可视区完全不相交时只隐藏（visible=false），重新相交自动恢复。
  所有触发源都走 requestPlacement()：同一帧最多一次有效测量与一次位置写入。
*/

interface BoardCardHandle {
  getCardElement: () => HTMLElement | null;
  getToolbarElement: () => HTMLElement | null;
}

/** 工具栏宿主：本板面实例自己的 ref（绝不 querySelector 全局查找）。 */
const toolbarHostEl = ref<HTMLElement | null>(null);
/** card.id → BoardCard 暴露的锚点入口（卸载 / 回调收到 null 时删除）。 */
const cardHandles = new Map<string, BoardCardHandle>();
/** v-for 的 :ref 必须是稳定函数，否则每次 patch 都会重新注册（见 registerCardHandle）。 */
const cardRefHandlers = new Map<string, (instance: unknown) => void>();

/**
 * 工具栏所有者 = 选中列表的**最后一项**（与 BoardCard 的 isToolbarOwner 逐字同口径），
 * 并且必须是这一帧真的渲染出来的卡：已删除的卡不渲染，自然也不持有工具栏。
 */
const toolbarOwnerId = computed<string | null>(() => {
  const ids = boardState.value?.selection ?? [];
  const last = ids.length ? ids[ids.length - 1] : null;
  if (!last) return null;
  return liveCards.value.some((card) => card.id === last) ? last : null;
});

function cardRefFor(cardId: string): (instance: unknown) => void {
  let handler = cardRefHandlers.get(cardId);
  if (!handler) {
    handler = (instance: unknown) => registerCardHandle(cardId, instance);
    cardRefHandlers.set(cardId, handler);
  }
  return handler;
}

/** 按 card.id 注册 / 注销 BoardCard 实例：只登记实例，不写任何板面状态。 */
function registerCardHandle(cardId: string, instance: unknown): void {
  const handle = instance as BoardCardHandle | null;
  const usable = handle && typeof handle.getCardElement === "function" ? handle : null;
  const previous = cardHandles.get(cardId) ?? null;
  if (usable) cardHandles.set(cardId, usable);
  else {
    cardHandles.delete(cardId);
    cardRefHandlers.delete(cardId);
  }
  // 同一个实例的重复回调（函数 ref 每次 patch 都会被调用）不是几何变化，不排新帧
  if (usable === previous) return;
  if (toolbarOwnerId.value === cardId) requestPlacement();
}

/* ---- 单帧合并调度：所有触发源都走这里 ---------------------------------- */

let placementFrame: number | null = null;
let placementDirty = false;
let lastMeasureStamp = -1;

/** 请求一次定位：同一帧只做一次有效测量与一次位置写入，后续请求只置脏标记。 */
function requestPlacement(): void {
  placementDirty = true;
  if (placementFrame !== null) return;
  placementFrame = requestAnimationFrame((stamp: number) => {
    placementFrame = null;
    if (!placementDirty) return;
    placementDirty = false;
    applyToolbarPlacement(stamp);
  });
}

/* ---- 几何过渡跟随：逐帧测量直到静止 / 超时，空闲不跑 rAF --------------- */

/** 安全时限：再慢的过渡也不会让逐帧循环一直跑下去。 */
const FOLLOW_TIMEOUT_MS = 600;
/** 连续这么多帧没有位移就认为静止，做最后一次定位并停止。 */
const FOLLOW_STABLE_FRAMES = 3;
const FOLLOW_EPSILON = 0.5;

let followFrame: number | null = null;
let followDeadline = 0;
let followStable = 0;
let followLast: { left: number; top: number; width: number; height: number } | null = null;
let anchorCardEl: HTMLElement | null = null;
let anchorOwnerId: string | null = null;
/** 锚点是不是来自真实 DOM 外框（jsdom 里是数学兜底，没有几何可跟随）。 */
let anchorFromDom = false;
let cardResizeObserver: ResizeObserver | null = null;
let toolbarResizeObserver: ResizeObserver | null = null;
let hostResizeObserver: ResizeObserver | null = null;
let observedToolbarEl: HTMLElement | null = null;
/** 上一次实测到的工具栏宽度（收起时量不到，用它避免把可见性自锁在 false）。 */
let lastToolbarWidth = 0;

function nowMs(): number {
  return typeof performance !== "undefined" && typeof performance.now === "function" ? performance.now() : Date.now();
}

/** 锚点卡片是否有 left / top / width / height（或 transform）的几何过渡。 */
function cardHasGeometryTransition(element: HTMLElement | null): boolean {
  if (!element || typeof getComputedStyle !== "function") return false;
  let style: CSSStyleDeclaration;
  try {
    style = getComputedStyle(element);
  } catch {
    return false;
  }
  const properties = String(style.transitionProperty ?? "")
    .split(",")
    .map((item) => item.trim());
  const durations = String(style.transitionDuration ?? "")
    .split(",")
    .map((item) => item.trim());
  const tracked = ["left", "top", "width", "height", "transform", "all"];
  return properties.some((property, index) => {
    if (!tracked.includes(property)) return false;
    const duration = durations.length ? durations[index % durations.length] : "";
    return Number.parseFloat(duration) > 0;
  });
}

function endFollow(): void {
  if (followFrame !== null) {
    cancelAnimationFrame(followFrame);
    followFrame = null;
  }
  followStable = 0;
  followLast = null;
  followDeadline = 0;
}

/** 过渡进行中：逐帧测量；连续若干帧静止或超过安全时限后做最后一次定位并停止。 */
function startFollow(): void {
  if (followFrame !== null) return;
  // jsdom 没有真实布局：没有几何可跟随，也不在这里空转
  if (!anchorFromDom) return;
  // 减少动画 / 没有几何过渡：绝不进入循环
  if (prefersReducedMotion()) return;
  if (!cardHasGeometryTransition(anchorCardEl)) return;
  followStable = 0;
  followLast = null;
  followDeadline = nowMs() + FOLLOW_TIMEOUT_MS;
  followFrame = requestAnimationFrame(stepFollow);
}

function stepFollow(stamp: number): void {
  followFrame = null;
  applyToolbarPlacement(stamp);
  const element = anchorCardEl;
  const rect = element && element.isConnected ? element.getBoundingClientRect() : null;
  if (!rect) {
    endFollow();
    return;
  }
  const previous = followLast;
  const still =
    previous !== null &&
    Math.abs(rect.left - previous.left) < FOLLOW_EPSILON &&
    Math.abs(rect.top - previous.top) < FOLLOW_EPSILON &&
    Math.abs(rect.width - previous.width) < FOLLOW_EPSILON &&
    Math.abs(rect.height - previous.height) < FOLLOW_EPSILON;
  followLast = { left: rect.left, top: rect.top, width: rect.width, height: rect.height };
  followStable = still ? followStable + 1 : 0;
  if (followStable >= FOLLOW_STABLE_FRAMES || nowMs() >= followDeadline) {
    endFollow();
    return;
  }
  followFrame = requestAnimationFrame(stepFollow);
}

/** 过渡重新开始（transitionrun）：重新进入跟随，保证逐帧贴住卡片。 */
function onAnchorTransitionRun(): void {
  startFollow();
}

function attachAnchor(element: HTMLElement | null): void {
  if (anchorCardEl === element) return;
  if (anchorCardEl) anchorCardEl.removeEventListener("transitionrun", onAnchorTransitionRun);
  anchorCardEl = element;
  if (element) element.addEventListener("transitionrun", onAnchorTransitionRun);
  endFollow();
}

/** 切换所有者时重新绑定锚点、两个 ResizeObserver 与过渡监听。 */
function bindAnchorObservers(cardId: string, handle: BoardCardHandle): void {
  const element = handle.getCardElement();
  if (anchorOwnerId !== cardId || anchorCardEl !== element) {
    anchorOwnerId = cardId;
    attachAnchor(element);
    cardResizeObserver?.disconnect();
    cardResizeObserver = null;
  }
  if (element && !cardResizeObserver && typeof ResizeObserver === "function") {
    cardResizeObserver = new ResizeObserver(() => requestPlacement());
    cardResizeObserver.observe(element);
  }
  const toolbarElement = handle.getToolbarElement();
  if (toolbarElement !== observedToolbarEl) {
    toolbarResizeObserver?.disconnect();
    toolbarResizeObserver = null;
    observedToolbarEl = toolbarElement;
    if (toolbarElement && typeof ResizeObserver === "function") {
      toolbarResizeObserver = new ResizeObserver(() => requestPlacement());
      toolbarResizeObserver.observe(toolbarElement);
    }
  }
}

/** 卸载 / 切板 / 失去所有者：断开全部观察器与过渡监听，取消跟随。 */
function unbindAnchor(): void {
  anchorOwnerId = null;
  attachAnchor(null);
  cardResizeObserver?.disconnect();
  cardResizeObserver = null;
  toolbarResizeObserver?.disconnect();
  toolbarResizeObserver = null;
  observedToolbarEl = null;
  endFollow();
}

/* ---- 测量与写入 -------------------------------------------------------- */

function elementScreenRect(element: Element | null | undefined): OverlayRect | null {
  if (!element || typeof element.getBoundingClientRect !== "function") return null;
  const rect = element.getBoundingClientRect();
  const left = finiteNumber(rect.left);
  const top = finiteNumber(rect.top);
  const right = Number.isFinite(rect.right) ? finiteNumber(rect.right) : left + finiteNumber(rect.width);
  const bottom = Number.isFinite(rect.bottom) ? finiteNumber(rect.bottom) : top + finiteNumber(rect.height);
  return { left, top, right, bottom };
}

/**
 * 锚点唯一来源：所属卡片的真实 DOM 外框。
 * 只有「元素不存在 / 未 isConnected / 外框宽高都为 0」时才退回数学结果 ——
 * 那是 **jsdom 专用兜底**（测试环境没有真实布局），真实浏览器一律用 DOM 外框。
 */
function measureAnchoredCard(handle: BoardCardHandle, card: BoardCardModel): { rect: OverlayRect; fromDom: boolean } {
  const element = handle.getCardElement();
  const rect = element && element.isConnected ? elementScreenRect(element) : null;
  if (rect && rect.right - rect.left > 0 && rect.bottom - rect.top > 0) return { rect, fromDom: true };
  const viewRect = elementScreenRect(viewportEl.value) ?? { left: 0, top: 0, right: 0, bottom: 0 };
  const display = displayRect(card);
  const scale = scaleSafe.value;
  // 不读 liveScroll（它会回写 scroll / view）：滚动量以 ref 为准，等价且没有副作用
  const left = viewRect.left + display.x * scale - scroll.value.x;
  const top = viewRect.top + display.y * scale - scroll.value.y;
  const height = card.folded ? 60 : display.h;
  return {
    rect: { left, top, right: left + display.w * scale, bottom: top + height * scale },
    fromDom: false,
  };
}

/** 宿主外框与自身布局盒的实测比例（hostRect / offsetWidth）；量不到就按 1（不缩放）。 */
function hostScaleOf(host: HTMLElement, rect: OverlayRect): { x: number; y: number } {
  const width = rect.right - rect.left;
  const height = rect.bottom - rect.top;
  const scaleX = host.offsetWidth > 0 && width > 0 ? width / host.offsetWidth : 1;
  const scaleY = host.offsetHeight > 0 && height > 0 ? height / host.offsetHeight : 1;
  return {
    x: Number.isFinite(scaleX) && scaleX > 0 ? scaleX : 1,
    y: Number.isFinite(scaleY) && scaleY > 0 ? scaleY : 1,
  };
}

function setOverlay(left: number, top: number, visible: boolean): void {
  const previous = overlay.value;
  if (previous.left === left && previous.top === top && previous.visible === visible) return;
  overlay.value = { left, top, visible };
}

/**
 * 本帧唯一的一次有效测量与位置写入。
 *
 * 顺序固定为「先量卡片外框 → 再量工具栏高度 → 最后写位置」，三段全部在**同一帧内**完成，
 * 不跨帧混用；坐标一律不取整（不引入任何取整 / 补偿常数），
 * 因此底边到卡片顶边只剩浏览器子像素舍入带来的 <0.2px 误差。
 * 工具栏自身高度变化（换行 / 文案）由它的 ResizeObserver 触发下一帧复测。
 *
 * stamp 由 requestAnimationFrame 传入：同一帧里的第二个调用直接返回（见 lastMeasureStamp），
 * 于是「统一请求入口」与「过渡跟随」共用同一帧预算，不会重复测量 / 重复写位置。
 */
function applyToolbarPlacement(stamp?: number): void {
  const currentStamp = stamp ?? nowMs();
  if (currentStamp === lastMeasureStamp) return;
  lastMeasureStamp = currentStamp;

  const host = toolbarHostEl.value;
  const ownerId = toolbarOwnerId.value;
  const handle = ownerId ? cardHandles.get(ownerId) ?? null : null;
  if (!host || !ownerId || !handle) {
    unbindAnchor();
    overlaySettled.value = false;
    setOverlay(0, 0, false);
    return;
  }
  const current = boardState.value;
  const card = current ? cardById(current, ownerId) : null;
  if (!card) {
    unbindAnchor();
    overlaySettled.value = false;
    setOverlay(0, 0, false);
    return;
  }

  bindAnchorObservers(ownerId, handle);

  const hostRect = elementScreenRect(host);
  if (!hostRect) {
    overlaySettled.value = false;
    setOverlay(0, 0, false);
    return;
  }
  const measured = measureAnchoredCard(handle, card);
  anchorFromDom = measured.fromDom;
  const cardRect = measured.rect;

  const toolbarElement = handle.getToolbarElement();
  const toolbarRect = toolbarElement && toolbarElement.isConnected ? elementScreenRect(toolbarElement) : null;
  const toolbarHeight =
    toolbarRect && toolbarRect.bottom - toolbarRect.top > 0 ? toolbarRect.bottom - toolbarRect.top : TOOLBAR_H;

  const hostScale = hostScaleOf(host, hostRect);
  const placed = placeToolbarRelativeToCard({
    cardRect,
    hostRect,
    anchorAtCard: true,
    toolbarHeight,
    toolbarWidth: toolbarRect && toolbarRect.right > toolbarRect.left ? toolbarRect.right - toolbarRect.left : lastToolbarWidth,
    gap: TOOLBAR_GAP,
    hostScale,
  });
  /*
    工具栏的屏幕外框：由「宿主原点 + 本帧 placement × 宿主实测缩放」算出 ——
    与写入位置同一份数据（placed）与同一份实测比例（hostScale），不另起一套换算；
    宽高也取本帧实测值。
    量不到宽度（工具栏被 v-show 收起时外框恒为 0）时沿用上一次实测宽度；
    从未量到过就只按竖向判断 —— 否则 display: none 会把宽高都变成 0，
    visible 会自锁在 false、再也回不来。
  */
  const widthThisFrame = toolbarRect ? toolbarRect.right - toolbarRect.left : 0;
  if (widthThisFrame > 0) lastToolbarWidth = widthThisFrame;
  const toolbarWidth = widthThisFrame > 0 ? widthThisFrame : lastToolbarWidth;

  /*
    可见性（状态诚实口径）：卡片与板面可视区相交 **且** 工具栏（按即将写入的位置算出的屏幕外框）
    与板面可视区相交，才算可见。二者缺一不可：
    - 卡片完全离屏：本来就不该有工具栏；
    - 卡片只部分可见、工具栏整条落在可视区上沿之外：宿主 overflow: hidden 会把它整条裁掉
      （屏幕上看不见、也点不到），此时 visible=false 才是诚实的，不会出现「标着可见却什么都没有」。
    严格相交（边贴边不算），部分相交照常显示；位置规则一个字都没改，这里只决定显隐。
    jsdom 量不到容器几何（宽高为 0）时不做隐藏判断 —— 不能把「测试环境没有布局」
    当成「卡片在视口外」。
  */
  const viewRect = elementScreenRect(viewportEl.value);
  const hasViewportGeometry = Boolean(
    viewRect && viewRect.right - viewRect.left > 0 && viewRect.bottom - viewRect.top > 0,
  );
  let visible = true;
  if (hasViewportGeometry) {
    const board: OverlayRect = viewRect as OverlayRect;
    const cardIntersects = rectsIntersect(cardRect, board);
    if (cardIntersects) {
      const toolbarScreen: OverlayRect = {
        left: hostRect.left + placed.left * hostScale.x,
        top: hostRect.top + placed.top * hostScale.y,
        right: hostRect.left + (placed.left + toolbarWidth) * hostScale.x,
        bottom: hostRect.top + (placed.top + toolbarHeight) * hostScale.y,
      };
      visible =
        toolbarWidth > 0
          ? rectsIntersect(toolbarScreen, board)
          : toolbarScreen.bottom > board.top && toolbarScreen.top < board.bottom;
    } else {
      visible = false;
    }
  }

  const wasHidden = !overlay.value.visible;
  setOverlay(placed.left, placed.top, visible);
  overlaySettled.value = true;
  // 从隐藏恢复到显示：工具栏元素是这一帧才挂上的，补量一次真实高度（只补一次）
  if (visible && wasHidden && !(toolbarRect && toolbarRect.bottom - toolbarRect.top > 0)) {
    requestPlacement();
    return;
  }
  if (visible) startFollow();
}

/** 恰好选中一张卡片时的那一张（连接点层用）。 */
const primarySelectedId = computed(() => {
  const current = boardState.value;
  if (!current || current.selection.length !== 1) return null;
  return current.selection[0];
});

/*
  触发源全部走 requestPlacement()：所有者 / 选择变化、view（scale / x / y）与真实 scroll、
  展开集合、store 状态（commit / 撤销 / 重做）、窗口 resize、宿主与锚点的 ResizeObserver、
  工具栏 ResizeObserver、拖动 pointermove、尺寸预览与提交、挂载与恢复。
  这里刻意**不**监听 layoutPlan：定位只读 DOM 几何，不把显示布局卷进来。
*/
watch(
  () => [view.value.scale, view.value.x, view.value.y, scroll.value.x, scroll.value.y, store.expandedCardIds],
  () => requestPlacement(),
);
watch(toolbarOwnerId, () => {
  // 换了所有者：先隐藏，等这一帧测完再显示（位置绝不会停留在上一张卡上）
  overlaySettled.value = false;
  requestPlacement();
});
watch(boardState, () => requestPlacement());

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
  if (!card || card.deleted || card.folded || hiddenMemberIds.value.has(cardId)) return out;
  out[cardId] = displayRect(card);
  return out;
});

// --- 卡片操作 -------------------------------------------------------------

function onCardPatch(cardId: string, patch: Partial<BoardCardModel>, label: string) {
  const current = boardState.value;
  if (!current) return;
  let next = updateCard(current, cardId, patch);
  /**
   * Phase 2-C「恢复」路径：图片解码后 BoardCard 通过 patch 写入 meta.display（真实比例）。
   * 比例一出现就把这张卡的 w/h 收敛到它 —— 与拖动 / 键盘 / 组缩放 / resize 同一份约束；
   * 非图片卡 ratio 为 null，一个字段都不改。
   */
  const card = cardById(next, cardId);
  const ratio = card ? imageRatioForCard(card) : null;
  if (card && ratio !== null && !isAspectSatisfied({ w: card.w, h: card.h }, ratio)) {
    const size = constrainCardSizeForCard(
      card,
      { w: card.w, h: card.h },
      { w: card.w, h: card.h },
      { driver: "width" },
    );
    if (!sameSize(size.w, card.w) || !sameSize(size.h, card.h)) {
      next = updateCard(next, cardId, { w: size.w, h: size.h });
    }
  }
  commit(next, label);
}

function onCardToggle(cardId: string, flag: "checked" | "hidden" | "folded" | "bookmarked" | "participation") {
  const current = boardState.value;
  if (!current) return;
  const card = cardById(current, cardId);
  if (!card) return;
  if (flag === "participation") {
    const included = participationOf(card) !== "included";
    commit(
      setParticipation(current, cardId, included),
      included ? "加入讨论范围" : "退出讨论范围",
    );
    return;
  }
  if (flag === "checked") {
    const checked = !card.checked;
    // 参与状态是 v2 的唯一事实源（§5.1）：勾选 / 取消必须同时写 participation，避免旧 checked 与它打架
    commit(
      updateCard(current, cardId, { checked, participation: checked ? "included" : "excluded" }),
      checked ? "勾选注释（本次允许 QIO 查看）" : "取消勾选",
    );
    return;
  }
  if (flag === "hidden") {
    const hidden = !card.hidden;
    commit(
      updateCard(current, cardId, { hidden, participation: hidden ? "excluded" : "included" }),
      hidden ? "隐藏卡片（退出讨论范围）" : "取消隐藏",
    );
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

/**
 * 菜单「移出组」（F07）：卡片**没有落点**，必须给它找组外最近的合理空位；
 * 剩余成员按既定纵向排列收拢、组框同步，成员变化与最终位置作为**一次提交**
 * （撤销一次就回到移出前，重做结果一致）。拖动出组不走这里（落点已有避让规则）。
 */
function commitLeaveGroup(current: BoardState, cardIds: string[]) {
  const outcome = leaveGroupToFreeSpots(current, cardIds);
  if (!outcome.placements.length) {
    notice.value = "所选卡片不在任何组里：没有需要移出的对象。";
    return;
  }
  commit(outcome.state, cardIds.length > 1 ? "把所选卡片移出组（自动避开重叠）" : "把卡片移出组（自动避开重叠）");
  const spilled = outcome.placements.filter((item) => !item.free);
  if (spilled.length) {
    const names = spilled
      .map((item) => {
        const card = cardById(outcome.state, item.cardId);
        return card ? cardSummary(card) : "卡片";
      })
      .join("、");
    notice.value =
      "已移出组，但没有找到完全不重叠的空位：" +
      names +
      " 停在最接近可行的位置，仍与其他对象有重叠，请手动拖开。";
    return;
  }
  const moved = outcome.placements.some((item) => item.moved);
  notice.value = moved
    ? "已移出组：卡片放到组外最近的空位，其余成员已按顺序收拢。"
    : "已移出组：位置不变（本来就在组外空位上），空掉的组会自动消失。";
}

function onCardLeaveGroup(cardId: string) {
  const current = boardState.value;
  if (!current) return;
  commitLeaveGroup(current, [cardId]);
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
    commitLeaveGroup(current, selected);
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
  // 组内成员条上的「移出」与卡片菜单同一条语义（F07）：组外找空位 + 一次提交。
  void groupId;
  commitLeaveGroup(current, [cardId]);
}

// --- Phase 2-B 接线：组标题拖整组 / 组框缩放（组件只发意图，这里落板面） ---

interface BoardObstacle {
  id: string;
  kind: "card" | "group";
  frame: V2Rect;
  groupId: string | null;
}

/** 组以外的全部对象（其它组框 + 不在本组的卡片）：避让的最小障碍集合。 */
function otherBoardObjects(groupId: string): BoardObstacle[] {
  const group = groups.value.find((item) => item.id === groupId);
  const memberIds = new Set(group?.members ?? []);
  const others: BoardObstacle[] = [];
  for (const item of groups.value) {
    if (item.id === groupId || item.deleted) continue;
    others.push({ id: item.id, kind: "group", frame: groupRect(item), groupId: null });
  }
  for (const card of liveCards.value) {
    if (memberIds.has(card.id)) continue;
    others.push({ id: card.id, kind: "card", frame: cardRect(card), groupId: groupIdOf(card.id) });
  }
  return others;
}

function onGroupDragStart(groupId: string, point: { x: number; y: number }): void {
  // 同一指针只驱动一种手势：开始拖整组前把别的临时手势清掉（不碰板面状态）
  releaseGestures();
  const current = boardState.value;
  const group = current ? groupById(current, groupId) : null;
  if (!group) return;
  groupDrag.value = {
    groupId,
    delta: { x: 0, y: 0 },
    moved: false,
    startClient: { x: point.x, y: point.y },
  };
  notice.value = "";
}

function onGroupDragMove(groupId: string, delta: { dx: number; dy: number }): void {
  const state = groupDrag.value;
  if (!state || state.groupId !== groupId) return;
  if (!state.moved && (Math.abs(delta.dx) > 3 || Math.abs(delta.dy) > 3)) state.moved = true;
  if (!state.moved) return;
  // 屏幕位移 → 板面位移（画布缩放是查看方式，不参与板面坐标）
  state.delta = { x: delta.dx / scaleSafe.value, y: delta.dy / scaleSafe.value };
  requestPlacement();
}

function onGroupDragEnd(groupId: string, delta: { dx: number; dy: number }): void {
  const state = groupDrag.value;
  groupDrag.value = null;
  const moved = Boolean(state?.moved) || Math.abs(delta.dx) > 3 || Math.abs(delta.dy) > 3;
  if (!moved) {
    notice.value = "组没有移动：板面没有产生改动。";
    return;
  }
  commitGroupMove(groupId, {
    x: delta.dx / scaleSafe.value,
    y: delta.dy / scaleSafe.value,
  });
}

function onGroupDragCancel(groupId: string): void {
  if (groupDrag.value?.groupId === groupId) groupDrag.value = null;
  notice.value = "已取消移动组：板面没有改动。";
}

/**
 * 组移动：组框与成员**统一移动一次提交**（组框由 board.ts reflowGroup 从成员派生）。
 *
 * 避让用 A 的 geometry.avoidOverlaps：组是障碍单元、组与自身成员不互算；
 * 到达轮数 / 位移上限仍重叠时如实提示 limited，绝不无限推挤。
 */
function commitGroupMove(groupId: string, boardDelta: { x: number; y: number }): void {
  const current = boardState.value;
  if (!current) return;
  const group = groupById(current, groupId);
  if (!group) return;
  const members = group.members
    .map((cardId) => cardById(current, cardId))
    .filter((card): card is BoardCardModel => Boolean(card));
  if (!members.length) return;

  const movedFrame: V2Rect = {
    x: group.x + boardDelta.x,
    y: group.y + boardDelta.y,
    w: group.w,
    h: group.h,
  };
  const avoided = avoidOverlaps({
    moved: { id: groupId, kind: "group", frame: movedFrame, groupId: null },
    others: otherBoardObjects(groupId),
    gap: DEFAULT_LAYOUT_GAP,
  });
  const applied = { x: avoided.frame.x - group.x, y: avoided.frame.y - group.y };

  let next = current;
  for (const member of members) {
    next = updateCard(next, member.id, {
      x: Math.round(member.x + applied.x),
      y: Math.round(member.y + applied.y),
    });
  }
  if (!stateDiffers(normalizeState(current), normalizeState(next))) {
    notice.value = "组没有移动：板面没有产生改动。";
    return;
  }
  commit(next, "移动组（组框与成员一起移动）");
  if (avoided.limited) {
    notice.value = "组与其它对象仍有重叠（" + avoided.blockers.length + " 处）：已停在能到达的位置，没有无限推挤。";
  } else if (avoided.adjusted) {
    notice.value = "组已让开其它对象（不会移动别的卡片）。";
  }
}

/** 组框内部的成员区域（与 board.ts reflowGroup 同一份留白）。 */
function groupMemberAreaOf(rect: V2Rect): V2Rect {
  return {
    x: rect.x + GROUP_PAD_X,
    y: rect.y + GROUP_HEADER_HEIGHT,
    w: Math.max(1, rect.w - GROUP_PAD_X * 2),
    h: Math.max(1, rect.h - GROUP_HEADER_HEIGHT - GROUP_PAD_BOTTOM),
  };
}

/** 由成员槽位反推组框（与 reflowGroup 同口径；用于如实说明缩放后的组框位置）。 */
function frameOfSlots(slots: Record<string, V2Rect>): V2Rect | null {
  const values = Object.values(slots);
  if (!values.length) return null;
  const left = Math.min(...values.map((rect) => rect.x)) - GROUP_PAD_X;
  const top = Math.min(...values.map((rect) => rect.y)) - GROUP_HEADER_HEIGHT;
  const right = Math.max(...values.map((rect) => rect.x + rect.w)) + GROUP_PAD_X;
  const bottom = Math.max(...values.map((rect) => rect.y + rect.h)) + GROUP_PAD_BOTTOM;
  return { x: left, y: top, w: Math.max(1, right - left), h: Math.max(1, bottom - top) };
}

/**
 * 组缩放的一次规划（A 的 planGroupResize）：右 / 下 / 右下把手改变组框，成员按比例适配。
 *
 * 图片成员的 aspectRatio 来自 C 的 meta.display（未知传 null → 按普通成员处理，不猜比例）；
 * 组框与全部成员槽位摊成**一份原子清单**（groupResizeFrameChanges），一次提交。
 */
function planGroupResizeFor(
  groupId: string,
  edge: GroupResizeEdge,
  boardDelta: { x: number; y: number },
): GroupResizePreview | null {
  const current = boardState.value;
  const group = current ? groupById(current, groupId) : null;
  if (!current || !group) return null;
  const startGroup = groupRect(group);
  const nextGroup: V2Rect = {
    x: startGroup.x,
    y: startGroup.y,
    w: Math.max(1, startGroup.w + (edge === "e" || edge === "se" ? boardDelta.x : 0)),
    h: Math.max(1, startGroup.h + (edge === "s" || edge === "se" ? boardDelta.y : 0)),
  };
  const members = group.members
    .map((cardId) => cardById(current, cardId))
    .filter((card): card is BoardCardModel => Boolean(card))
    .map((card) => ({ id: card.id, frame: cardRect(card), aspectRatio: imageRatioForCard(card) }));
  if (!members.length) return null;

  const result = planGroupResize({
    frame: groupMemberAreaOf(startGroup),
    nextFrame: groupMemberAreaOf(nextGroup),
    members,
    gap: DEFAULT_LAYOUT_GAP,
    padding: 0,
  });
  const slots: Record<string, V2Rect> = {};
  for (const change of groupResizeFrameChanges(groupId, result)) {
    if (change.kind === "card") slots[change.id] = { ...change.frame };
  }
  return { slots, frame: frameOfSlots(slots) ?? groupRect(group), issues: [...result.issues] };
}

function onGroupResizeStart(groupId: string, edge: GroupResizeEdge, _point: { x: number; y: number }): void {
  releaseGestures();
  const current = boardState.value;
  const group = current ? groupById(current, groupId) : null;
  if (!group) return;
  groupResize.value = { groupId, edge, moved: false, preview: null };
  notice.value = "";
}

function onGroupResizeMove(
  groupId: string,
  edge: GroupResizeEdge,
  delta: { dx: number; dy: number },
): void {
  const state = groupResize.value;
  if (!state || state.groupId !== groupId) return;
  if (!state.moved && (Math.abs(delta.dx) > 3 || Math.abs(delta.dy) > 3)) state.moved = true;
  if (!state.moved) return;
  state.preview = planGroupResizeFor(groupId, edge, {
    x: delta.dx / scaleSafe.value,
    y: delta.dy / scaleSafe.value,
  });
  requestPlacement();
}

function onGroupResizeEnd(groupId: string, edge: GroupResizeEdge, delta: { dx: number; dy: number }): void {
  const state = groupResize.value;
  groupResize.value = null;
  const moved = Boolean(state?.moved) || Math.abs(delta.dx) > 3 || Math.abs(delta.dy) > 3;
  if (!moved) {
    notice.value = "组框尺寸没有改动：板面没有产生改动。";
    return;
  }
  const preview = planGroupResizeFor(groupId, edge, {
    x: delta.dx / scaleSafe.value,
    y: delta.dy / scaleSafe.value,
  });
  if (!preview) return;
  commitGroupResize(groupId, preview);
}

function onGroupResizeCancel(groupId: string): void {
  if (groupResize.value?.groupId === groupId) groupResize.value = null;
  notice.value = "已取消调整组框尺寸：板面没有改动。";
}

/** 组框缩放：成员槽位一次提交（组框由 reflowGroup 派生）；图片比例已在规划里约束。 */
function commitGroupResize(groupId: string, preview: GroupResizePreview): void {
  const current = boardState.value;
  if (!current) return;
  let next = current;
  for (const [cardId, rect] of Object.entries(preview.slots)) {
    const card = cardById(next, cardId);
    if (!card) continue;
    const x = Math.round(rect.x);
    const y = Math.round(rect.y);
    const w = Math.max(1, Math.round(rect.w));
    const h = Math.max(1, Math.round(rect.h));
    if (sameSize(x, card.x) && sameSize(y, card.y) && sameSize(w, card.w) && sameSize(h, card.h)) continue;
    next = updateCard(next, cardId, { x, y, w, h });
  }
  if (!stateDiffers(normalizeState(current), normalizeState(next))) {
    notice.value = "组框尺寸没有改动：板面没有产生改动。";
    return;
  }
  let nextGroup = groupById(next, groupId);
  let limited = false;
  /**
   * 缩放后若与其它对象冲突：先对**组本身**做确定性最小避让（整组平移），
   * 不移动无关卡片、不改成员顺序；避让受限时保留用户尺寸并如实提示。
   */
  if (nextGroup) {
    const avoided = avoidOverlaps({
      moved: { id: groupId, kind: "group", frame: groupRect(nextGroup), groupId: null },
      others: otherBoardObjects(groupId),
      gap: DEFAULT_LAYOUT_GAP,
    });
    if (!avoided.limited && avoided.adjusted) {
      const dx = Math.round(avoided.frame.x - nextGroup.x);
      const dy = Math.round(avoided.frame.y - nextGroup.y);
      const shifted = groupById(next, groupId);
      if (shifted && (dx !== 0 || dy !== 0)) {
        for (const cardId of shifted.members) {
          const card = cardById(next, cardId);
          if (!card) continue;
          next = updateCard(next, cardId, { x: card.x + dx, y: card.y + dy });
        }
        nextGroup = groupById(next, groupId);
      }
    } else if (avoided.limited) {
      limited = true;
    }
  }
  commit(next, "调整组框尺寸（成员按比例适配）");
  if (preview.issues.includes("min_clamped")) {
    notice.value = "组框已停在成员最小尺寸上（成员不会小于卡片最小尺寸）。";
  }
  if (limited) {
    notice.value = "缩放后的组框与其它对象仍有重叠：不会无限推挤，可再调整尺寸。";
  }
}

// --- 关系链接：连接点拖线 -------------------------------------------------

/**
 * 指针下的卡片：多个命中时取面积最小的（最具体）。
 *
 * **用当前显示矩形**（displayRect）：拖动 / 尺寸预览之后，卡片 / 连线 / 高亮画在哪，
 * 落点就判在哪 —— 用正式保存矩形（cardRect）会判在一个屏幕上不存在的旧位置上（0.2.5 §四）。
 * 入参 point 已经是板面坐标（boardPointOf 的结果），displayRect 也是板面坐标：
 * 全程板面坐标系比较，**不再乘除 scale**。displayRect 只读，临时显示坐标绝不写回板面。
 */
function hitCardAt(point: { x: number; y: number }, excludeId: string | null): string | null {
  const current = boardState.value;
  if (!current) return null;
  let best: { id: string; area: number } | null = null;
  for (const card of current.cards) {
    if (card.deleted || card.id === excludeId) continue;
    // 收起的组只画组框：成员卡片不在画布上，也就不能被指针命中（端点身份仍然是它们自己）
    if (hiddenMemberIds.value.has(card.id)) continue;
    const rect = displayRect(card);
    if (!pointInRect(point.x, point.y, rect)) continue; // board.ts 的签名是 (x, y, rect)
    const area = rect.w * rect.h;
    if (!best || area < best.area) best = { id: card.id, area };
  }
  return best ? best.id : null;
}

/**
 * 指针下的组框（收起 / 展开都算）：返回 group:<id>。
 *
 * 组框是**关系端点**（契约 §5.3）：拖线可以落在组上。这里只决定「落点指向哪个组」，
 * 是否真的能建链由 finishLinkDraft 按当前板面模型如实回答。
 */
function hitGroupAt(point: { x: number; y: number }, excludeEndpoint: string | null): string | null {
  const current = boardState.value;
  if (!current) return null;
  for (let index = displayGroups.value.length - 1; index >= 0; index -= 1) {
    const group = displayGroups.value[index];
    if (!group || group.deleted) continue;
    const value = GROUP_ENDPOINT_PREFIX + group.id;
    if (value === excludeEndpoint) continue;
    if (pointInRect(point.x, point.y, group)) return value;
  }
  return null;
}

/** 拖线起点所在的组：起点是组端点就直接取组 id，是卡片就按成员关系找。 */
function groupIdOfEndpoint(endpoint: string | null): string | null {
  if (!endpoint) return null;
  const parsed = parseEndpoint(endpoint);
  if (parsed.ok) {
    return parsed.endpoint.kind === "group" ? parsed.endpoint.id : groupIdOf(parsed.endpoint.id);
  }
  return groupIdOf(endpoint);
}

/**
 * 拖线落点：**先卡片（更具体），再组框**；返回裸卡片 id 或 group:<id>。
 *
 * 这就是「不自动升级」的入口：指针在组内成员卡上时命中卡片，即使组框也在指针下面，
 * 目标仍然是那张卡；只有指针落在组框标题 / 空白（没有卡片）时才命中组。
 */
function hitEndpointAt(point: { x: number; y: number }, fromId: string | null): string | null {
  const card = hitCardAt(point, fromId);
  if (card) return card;
  const fromGroup = groupIdOfEndpoint(fromId);
  const ownGroup = fromGroup ? GROUP_ENDPOINT_PREFIX + fromGroup : null;
  return hitGroupAt(point, ownGroup);
}

/**
 * 悬停卡片：精确命中优先；没有精确命中时，在卡片外保留一圈缓冲区。
 * 端点中心在卡片外 6px、命中区还有 17px，没有这圈缓冲区，指针从卡片移向端点时会先丢 hover，
 * 端点会在到达前消失。缓冲区按屏幕像素换算回板面单位，缩小时不会变窄。
 */
function hoveredCardAt(point: { x: number; y: number }): string | null {
  const exact = hitCardAt(point, null);
  if (exact) return exact;
  const margin = 32 / scaleSafe.value;
  let bestId: string | null = null;
  let bestDistance = Number.POSITIVE_INFINITY;
  for (const card of displayCards.value) {
    if (card.deleted) continue;
    const dx = point.x < card.x ? card.x - point.x : point.x > card.x + card.w ? point.x - card.x - card.w : 0;
    const dy = point.y < card.y ? card.y - point.y : point.y > card.y + card.h ? point.y - card.y - card.h : 0;
    const distance = Math.hypot(dx, dy);
    if (distance <= margin && distance < bestDistance) {
      bestDistance = distance;
      bestId = card.id;
    }
  }
  return bestId;
}

/** 指针悬停在组框上（纯显示）：组已有锚点因此可以从圆点变短条。 */
function onBoardHoverMove(event: PointerEvent): void {
  // 没有任何被关系使用的锚点时，悬停状态没有可见效果，不必逐帧做命中测试。
  if (!groupAnchors.value.length && !cardAnchors.value.length) {
    hoveredGroupId.value = null;
    hoveredCardId.value = null;
    return;
  }
  if (dragging.value || panning.value || rectSelect.value || resizing.value || groupDrag.value || groupResize.value || linkDraft.value) {
    return;
  }
  const point = boardPointOf(event);
  // 指针在卡片上或卡片外侧缓冲区时，卡片锚点优先；组框其次。
  const cardId = hoveredCardAt(point);
  hoveredCardId.value = cardId;
  if (cardId) {
    hoveredGroupId.value = null;
    return;
  }
  const endpoint = hitGroupAt(point, null);
  hoveredGroupId.value = endpoint ? endpoint.slice(GROUP_ENDPOINT_PREFIX.length) : null;
}

function onBoardHoverLeave(): void {
  hoveredGroupId.value = null;
  hoveredCardId.value = null;
}

/** 从卡片已连接锚点开始拖线：起点身份仍是裸 card id。 */
function onCardAnchorDown(anchor: CardAnchor, event: PointerEvent): void {
  onConnectStart(anchor.endpoint, event);
}

/** 从组锚点开始拖线：起点身份就是 group:<id>，不是成员卡。 */
function onGroupAnchorDown(anchor: GroupAnchor, event: PointerEvent): void {
  onConnectStart(anchor.endpoint, event);
}

function onConnectStart(fromId: string, event: PointerEvent) {
  event.preventDefault();
  /**
   * 同一指针只驱动一种手势（0.2.5 §三.6）：从连接点开始拖线时，把可能残留的卡片拖动 /
   * 平移 / 框选 / **尺寸调整** 全部清掉 —— 拖线过程中不移动、不拉伸、不成组。
   */
  releaseGestures();
  linkDraft.value = { fromId, point: boardPointOf(event), targetId: null };
  notice.value = "";
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
  /*
    F04：端点对已经有关系时**不得**再走 addLink —— 那会用这次拖线传入的空方向 / 空含义
    覆盖原关系（id 变了、含义被清空、曲线之外的语义丢失），并在用户写任何内容前就保存。
    正确语义：无论从哪一端拖过来，都选中 / 打开**原关系**；打开编辑器不等于修改，
    关闭 / 取消原样保留，只有用户明确保存才更新对应字段。
  */
  const existing = linkBetween(current, draft.fromId, draft.targetId);
  if (existing) {
    openLinkEditor(existing.id);
    notice.value =
      "这条关系已经存在：已打开原有关系（id、方向、含义与曲线都保留），没有新建，也没有改动。";
    return;
  }
  const next = addLink(current, draft.fromId, draft.targetId, false, "");
  const created = next.links.find(
    (link) =>
      (link.src === draft.fromId && link.dst === draft.targetId) ||
      (link.src === draft.targetId && link.dst === draft.fromId),
  );
  if (!created) {
    notice.value = "没有建立关系：拖到无效位置或取消时不建链，也不保存半条链接。";
    return;
  }
  commit(next, "新建关系（方向与含义由你写明）");
  openLinkEditor(created.id);
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

function openLinkEditor(linkId: string, clientX?: number, clientY?: number) {
  const current = boardState.value;
  if (!current) return;
  const link = current.links.find((item) => item.id === linkId);
  if (!link) return;
  editingLinkId.value = linkId;
  linkEditorOpen.value = true;
  linkMeaning.value = link.meaning;
  linkDirection.value = link.direction;
  const host = shell.value;
  if (host && typeof clientX === "number" && typeof clientY === "number") {
    const rect = host.getBoundingClientRect();
    linkEditorPos.value = {
      x: Math.max(8, Math.min(rect.width - 380, clientX - rect.left + 12)),
      y: Math.max(8, Math.min(rect.height - 250, clientY - rect.top + 12)),
    };
  } else {
    linkEditorPos.value = null;
  }
}

const linkEditorStyle = computed<Record<string, string>>(() => {
  if (linkEditorPos.value) {
    const placed: Record<string, string> = { left: linkEditorPos.value.x + "px", top: linkEditorPos.value.y + "px" };
    return placed;
  }
  const fallback: Record<string, string> = { right: "16px", top: "16px" };
  return fallback;
});

function onLinkCurveChange(linkId: string, curve: { x: number; y: number }) {
  const current = boardState.value;
  if (!current) return;
  commit(updateLink(current, linkId, { curve }), "调整关系曲线");
}

/** 单击只选中并高亮关系，不弹出编辑卡；双击说明才编辑。 */
function selectLink(linkId: string) {
  editingLinkId.value = linkId;
  linkEditorOpen.value = false;
}

function saveLinkEditor() {
  const current = boardState.value;
  const linkId = editingLinkId.value;
  if (!current || !linkId) return;
  commit(updateLink(current, linkId, { meaning: linkMeaning.value, direction: linkDirection.value }), "修改关系含义 / 方向");
  notice.value = "已保存关系的方向与含义（含义是你写的原话，系统不补充解释）。";
  editingLinkId.value = null;
  linkEditorOpen.value = false;
  linkEditorPos.value = null;
}

function onDeleteLink(linkId: string) {
  const current = boardState.value;
  if (!current) return;
  commit(removeLink(current, linkId), "删除关系（撤回这条关联）");
  if (editingLinkId.value === linkId) {
    editingLinkId.value = null;
    linkEditorOpen.value = false;
    linkEditorPos.value = null;
  }
}

function deleteEditingLink() {
  const current = boardState.value;
  const linkId = editingLinkId.value;
  if (!current || !linkId) return;
  commit(removeLink(current, linkId), "删除关系（撤回这条关联）");
  editingLinkId.value = null;
  linkEditorOpen.value = false;
  linkEditorPos.value = null;
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

/**
 * 屏幕矩形：DOMRectLike 的最小形状 + 可选的右 / 下边与宽高
 * （真实 DOMRect 四边都有；量不到时用 left + width 兜底）。
 */
type ScreenRectLike = DOMRectLike & { right?: number; bottom?: number; width?: number; height?: number };

/**
 * 一次性把某个板面矩形移进**屏幕可用区**需要的位移。
 *
 * **只在用户明确点击「定位 / 完整查看」时调用**（F1/A6）：不给任何自动追踪、
 * 不因为缩放 / 平移 / 浮层开合而调整，也绝不改卡片本身。
 *
 * originRect 只用来把板面坐标换算成屏幕坐标（容器原始矩形 + 当前滚动量）；
 * area 是**实际可用区域**（已扣浮动遮挡）。两者分开，滚动量不会被算两次，
 * 也不会把「被对话窗口盖住的那块屏幕」当成可见区域。
 *
 * 明确视图策略：
 * - 整个矩形放得下 → 上下左右各夹一次，让它完整可见；
 * - 矩形比可用区还大（大卡 / 大预览）→ **只对齐左上角**，不再试图把右下边也拉进来 ——
 *   否则同一张卡会在两次定位之间左右 / 上下轮流对齐，看着像"两边轮流跳"。
 */
function revealDeltaInArea(
  bounds: LayoutRect,
  originRect: ScreenRectLike,
  area: ScreenBox,
  margin: number,
): { dx: number; dy: number } {
  const scale = Number.isFinite(view.value.scale) && view.value.scale > 0 ? view.value.scale : 1;
  const screen = screenPointOfBoard(bounds.x, bounds.y, originRect);
  if (!Number.isFinite(screen.x) || !Number.isFinite(screen.y)) return { dx: 0, dy: 0 };
  const left = finiteNumber(area.left);
  const top = finiteNumber(area.top);
  const right = Math.max(left, finiteNumber(area.right));
  const bottom = Math.max(top, finiteNumber(area.bottom));
  const width = Math.max(0, finiteNumber(bounds.w)) * scale;
  const height = Math.max(0, finiteNumber(bounds.h)) * scale;
  const availW = Math.max(0, right - left - margin * 2);
  const availH = Math.max(0, bottom - top - margin * 2);
  let dx = 0;
  let dy = 0;
  if (width > availW) dx = left + margin - screen.x;
  else if (screen.x < left + margin) dx = left + margin - screen.x;
  else if (screen.x + width > right - margin) dx = right - margin - (screen.x + width);
  if (height > availH) dy = top + margin - screen.y;
  else if (screen.y < top + margin) dy = top + margin - screen.y;
  else if (screen.y + height > bottom - margin) dy = bottom - margin - (screen.y + height);
  return { dx, dy };
}

/**
 * 旧口径包装：可用区 = 传入的可视矩形（待审批预览定位沿用；卡片定位走
 * {@link revealDeltaInArea}，还要扣掉浮动对话等遮挡）。
 */
function revealDelta(bounds: LayoutRect, viewRect: ScreenRectLike, margin: number): { dx: number; dy: number } {
  // DOMRectLike 只保证 left / top：右 / 下边缺失时用宽高兜底（jsdom 的桩几何两种都有）
  const left = finiteNumber(viewRect.left);
  const top = finiteNumber(viewRect.top);
  const right = Number.isFinite(viewRect.right) ? Number(viewRect.right) : left + finiteNumber(viewRect.width);
  const bottom = Number.isFinite(viewRect.bottom) ? Number(viewRect.bottom) : top + finiteNumber(viewRect.height);
  return revealDeltaInArea(bounds, viewRect, { left, top, right, bottom }, margin);
}

/** 只动查看位置（滚动），板面状态一个字段都不改。 */
function applyReveal(dx: number, dy: number): void {
  if (!dx && !dy) return;
  applyScroll({ x: scroll.value.x - dx, y: scroll.value.y - dy });
  view.value = { ...view.value, x: -scroll.value.x, y: -scroll.value.y };
}

/** 定位请求序号：只执行用户最后一次明确请求，不排队、不持续追踪（F08）。 */
let locateSeq = 0;

/** 等一帧：让「展开收起组」这类布局变化真正落地；没有 rAF 的环境退回宏任务。 */
function nextLayoutFrame(): Promise<void> {
  return new Promise<void>((resolve) => {
    if (typeof requestAnimationFrame === "function") requestAnimationFrame(() => resolve());
    else setTimeout(resolve, 16);
  });
}

/**
 * 定位一张卡片（F08）。搜索 / 书签等入口都发同一个事件，因此走同一个流程：
 * 1. 目标属于**已收起的组**：先展开该组（纯显示状态，不改成员 / 参与 / 正文），
 *    等一次渲染 + 一帧，让目标真的出现在板面上、布局可用；
 * 2. 按**实际可用视野**（扣掉标题栏 / 工具栏 / 审批浮层 / 浮动对话窗口）把卡片移进来：
 *    放得下就完整可见，比可用区还大就只对齐左上角（稳定可预期）；
 * 3. 高亮目标；只执行用户明确请求的这一次 —— 不持续追踪、不自动抢下一次视野。
 */
async function locate(cardId: string) {
  const current = boardState.value;
  if (!current) return;
  const card = cardById(current, cardId);
  if (!card) return;
  const seq = (locateSeq += 1);

  const group = groupOfCard(current, cardId);
  const needsExpand = Boolean(group && collapsedGroupSet.value.has(group.id));
  if (needsExpand && group) {
    // 收起只是显示态（collapsedGroupIds），展开不写板面状态、不产生保存。
    collapsedGroupIds.value = collapsedGroupIds.value.filter((id) => id !== group.id);
    notice.value = "已展开「" + group.name + "」，再把这张卡片定位到视野里。";
  }
  if (needsExpand) {
    await nextTick();
    await nextLayoutFrame();
    // 等待期间用户又点了另一个定位：只执行最后一次（每次定位都是一次明确请求）。
    if (seq !== locateSeq) return;
  }

  const latest = boardState.value;
  const target = latest ? cardById(latest, cardId) : null;
  if (!latest || !target) return;
  const element = viewportEl.value;
  if (element) {
    const container = element.getBoundingClientRect();
    const usable = usableScreenBox();
    // 显示矩形与卡片渲染同源（正式几何；展开收起只切显示，不改位置）。
    const bounds = displayRect(target);
    const delta = usable
      ? revealDeltaInArea(bounds, container, usable, 80)
      : revealDelta(bounds, container as ScreenRectLike, 80);
    applyReveal(delta.dx, delta.dy);
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
  void locate(detail.cardId);
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
   * 用户明确点击「在板面上定位」：只调这一次视野（F1/A6）。
   * 四方向统一走 {@link revealDelta}：放得下就整块移进来，比可视区还大就只对齐左上角；
   * 数值先做有限性检查，NaN / 非有限值不参与计算，避免把滚动位置写坏。
   */
  const delta = revealDelta(
    { x: bounds.x, y: bounds.y, w: bounds.w, h: bounds.h },
    element.getBoundingClientRect(),
    60,
  );
  applyReveal(delta.dx, delta.dy);
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
  // 工具栏宿主自己的尺寸变化（窗口 / 布局变化）也要重新定位
  if (typeof ResizeObserver === "function" && toolbarHostEl.value) {
    hostResizeObserver = new ResizeObserver(() => requestPlacement());
    hostResizeObserver.observe(toolbarHostEl.value);
  }
  // 挂载后布局还没稳定（滚动位置 / 板面区域刚发布）：交给合并调度，下一帧做第一次测量
  requestPlacement();
});

/*
  选中的卡片一变就重算工具栏位置（2026-10-14）：引擎自己的 watch（toolbarOwnerId）负责 ——
  刷新页面时选中状态是从 store 恢复的，没有经过任何点击，也必须算准。
*/

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
  // 工具栏定位：取消待执行帧、断开全部 ResizeObserver 与过渡监听 / 跟随状态
  if (placementFrame !== null) {
    cancelAnimationFrame(placementFrame);
    placementFrame = null;
  }
  placementDirty = false;
  hostResizeObserver?.disconnect();
  hostResizeObserver = null;
  unbindAnchor();
  cardHandles.clear();
  cardRefHandlers.clear();
  // 可用区域与缩放都是显示态桥：画布没了就清空，别的组件（添加菜单）回到既有兜底行为，
  // 提交路径也不再拿到上一块画布的 scale
  publishBoardArea(null);
  publishBoardScale(null);
  if (highlightTimer) clearTimeout(highlightTimer);
  if (locatedTimer) clearTimeout(locatedTimer);
});
</script>

<template>
  <section ref="shell" class="board-shell" data-im="board">
    <p v-if="dragging && dragHint" class="banner drag" role="status">{{ dragHint }}</p>
    <p v-if="notice" class="banner" role="status">{{ notice }}</p>
    <!--
      操作提示只在「本机还没用过查看手势」时出现一条，用过一次即永久收起。
      刻意不设 role="status"：它不是实时状态，而且文案里的缩放百分比会让 aria-live
      在每一次滚轮缩放时把整条提示重复播报一遍。
    -->
    <p v-if="!hintSeen" class="banner view-hint" data-im="board-view-hint">
      拖动空白处平移查看位置（{{ Math.round(view.scale * 100) }}%）；按住空格拖动空白处框选卡片；滚轮以指针附近为缩放中心。
      平移与缩放只改变查看方式，不形成改动、不调用 QIO。
    </p>

    <div class="board-main">
      <!--
        板面滚动区：策略 **hidden**（统一滚动规则）。
        - 原生滚动条与 QIO 指示条都不出现（板面上已经有「拖动空白处平移」这条显式手势，
          再画一条指示条只会占住卡片边缘）；
        - 但滚动能力一个不少：overflow 仍是 auto，scrollLeft / scrollTop、平移、滚轮缩放、
          定位与坐标换算（canScroll / insideScrollable 读的是 computed overflow）全部照旧。
        不要为了「藏滚动条」改成 overflow: hidden / clip —— 那会连程序性滚动一起去掉。
      -->
      <div
        ref="viewportEl"
        class="board-viewport"
        v-qio-scrollbar="'hidden'"
        :class="{ dragging: Boolean(dragging), panning: Boolean(panning), framing: Boolean(rectSelect) }"
        @pointerdown="onSurfacePointerDown"
        @pointermove="onBoardHoverMove"
        @pointerleave="onBoardHoverLeave"
      >
        <div class="board-scroll-content" :style="contentStyle">
          <div
            ref="surface"
            class="board-surface"
            :class="{ resizing: Boolean(resizing), dragging: Boolean(dragging), 'link-target-card': linkTarget.kind === 'card' }"
            :style="surfaceStyle"
          >
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
            :collapsed="isGroupCollapsed(group.id)"
            :link-target="linkTarget.kind === 'group' && linkTarget.groupId === group.id"
            @rename="onGroupRename"
            @toggle-ordered="onToggleOrdered"
            @dissolve="onDissolveGroup"
            @move-member="onMoveMember"
            @leave="onLeaveMember"
            @select-member="(cardId) => onSelect(cardId, false)"
            @toggle-collapse="onGroupToggleCollapse"
            @group-drag-start="onGroupDragStart"
            @group-drag-move="onGroupDragMove"
            @group-drag-end="onGroupDragEnd"
            @group-drag-cancel="onGroupDragCancel"
            @group-resize-start="onGroupResizeStart"
            @group-resize-move="onGroupResizeMove"
            @group-resize-end="onGroupResizeEnd"
            @group-resize-cancel="onGroupResizeCancel"
          />

          <BoardCard
            v-for="card in renderedCards"
            :key="card.id"
            :ref="cardRefFor(card.id)"
            :card="renderCard(card)"
            :selected="selection.includes(card.id)"
            :highlight="highlightId === card.id || (linkTarget.kind === 'card' && linkTarget.cardId === card.id)"
            :dragging="dragging !== null && dragging.cardId === card.id"
            :group-dragging="groupDrag !== null && groupDrag.groupId === groupIdOf(card.id)"
            :x="displayX(card)"
            :y="displayY(card)"
            :group-name="groupNameOf(card.id)"
            :groups="groups.filter((item) => item.id !== groupIdOf(card.id))"
            :toolbar-left="overlay.left"
            :toolbar-top="overlay.top"
            :toolbar-positions="toolbarButtonPositions(card)"
            :toolbar-host="toolbarHostEl"
            :toolbar-visible="overlay.visible && overlaySettled"
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
            :scale="scaleSafe"
            :width="surfaceSize.w"
            :height="surfaceSize.h"
            :draft-from-id="linkDraft ? linkDraft.fromId : null"
            :draft-point="linkDraft ? linkDraft.point : null"
            :draft-target-id="linkDraft ? linkDraft.targetId : null"
            :groups="displayGroups"
            :folded-group-ids="foldedGroupIds"
            :included-card-ids="includedCardIds"
            @select-link="selectLink"
            @edit-link="openLinkEditor"
            @delete-link="onDeleteLink"
            @curve-change="onLinkCurveChange"
          />

          <BoardPreviewLayer
            :intents="previewIntents"
            :located-intent-id="locatedIntentId"
            :width="surfaceSize.w"
            :height="surfaceSize.h"
          />

          <!-- 虚拟意图层等板面坐标系叠层：由页面注入，随画布一起缩放/平移。 -->
          <slot name="overlay" :scale="scaleSafe" :width="surfaceSize.w" :height="surfaceSize.h" />

            <div v-if="rectSelect" class="select-rect" :style="rectStyle"></div>
          </div>

          <!--
            组端点锚点层：只画已经被关系使用的组边缘锚点（不常驻四个端口）。
            与卡片连接点同一套「未缩放屏幕像素」定位，位置 = 板面锚点 × scale。
          -->
          <BoardGroupAnchorLayer
            :anchors="groupAnchors"
            :scale="scaleSafe"
            :selected-group-ids="selectedGroupIds"
            :hovered-group-id="hoveredGroupId"
            @anchor-down="onGroupAnchorDown"
          />

          <!--
            卡片端点锚点层：未选中卡片只显示已连接空心点，悬停变短条、悬停锚点向外半圆。
            选中卡片的四向短条仍由 BoardResizeLayer 承载，避免同一边出现两套端口。
          -->
          <BoardCardAnchorLayer
            :anchors="renderedCardAnchors"
            :scale="scaleSafe"
            :selected-card-ids="selection"
            :hovered-card-id="hoveredCardId"
            @anchor-down="onCardAnchorDown"
          />

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

      <!--
        工具栏宿主：**独立、不缩放**的浮层容器，与 .board-viewport 并列（在 .board-main 里），
        所以不受 .board-surface 的 transform: scale() 与容器滚动影响；
        行内 left / top 由本组件按「卡片真实外框 − 宿主原点」算好。
        整层 pointer-events: none（只有工具栏与已打开菜单接收事件）。
        本层**裁切到板面可视区**（overflow: hidden）：卡片只部分可见时工具栏不会溢出到
        板面上方的页面头部；裁切只作用于绘制，元素的完整几何（getBoundingClientRect）
        不受影响，所以固定锚点仍然精确。菜单方向由 BoardCard 按实测几何决定，不会被裁掉。
        这里是本实例自己的 ref，绝不 querySelector 全局查找别处的节点。
      -->
      <div
        ref="toolbarHostEl"
        class="card-toolbar-host"
        data-im="card-toolbar-host"
        :data-owner-card-id="toolbarOwnerId ?? ''"
        :data-toolbar-visible="overlay.visible && overlaySettled ? '1' : '0'"
      ></div>

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

    <div v-if="editingLink && linkEditorOpen" class="link-editor" :style="linkEditorStyle" role="dialog" aria-label="关系编辑">
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
        <button class="btn" type="button" @click="editingLinkId = null; linkEditorOpen = false; linkEditorPos = null">关闭</button>
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
/*
  2026-10-14（用户要求）：提示条**浮在板面上**，不许把整块板面顶下去。
  原来它们排在 flex 列里占一行 —— 一出现，板面与所有卡片整体下移，看起来像「板面跳了一下」。
  现在绝对定位到板面顶部、半透明底、pointer-events: none（不挡任何板面操作）。
*/
.banner {
  position: absolute;
  left: 0;
  right: 0;
  top: 0;
  z-index: 40;
  margin: 0;
  padding: var(--sp-1) var(--sp-4);
  font-size: var(--fs-xs);
  color: var(--text-secondary);
  background: color-mix(in srgb, var(--bg-surface) 92%, transparent);
  border-bottom: 1px solid var(--border-subtle);
  pointer-events: none;
}
.banner.drag { color: var(--text-strong); background: var(--bg-accent-subtle); }
.banner.view-hint { color: var(--text-faint); }
.board-main { position: relative; flex: 1; display: flex; min-height: 0; min-width: 0; }
/*
  工具栏宿主层：只有这一层（外加已打开菜单）接收指针事件，其余区域全部穿透。
  z-index 只要高过 BoardResizeLayer（45）这一层即可 —— 不靠无限抬 z-index 掩盖层级问题。
  它不在滚动区里，工具栏因此不随板面缩放、也不随容器滚动。
*/
.card-toolbar-host {
  position: absolute;
  inset: 0;
  z-index: 50;
  pointer-events: none;
  /*
    裁切到板面可视区（宿主盒 = .board-main 盒 = .board-viewport 盒，无 padding / border 偏差）。
    透明浮层因此不会覆盖板面之外的页面头部；接近边缘时按板面可视区域裁切是允许的。
    注意：裁切**不改变** getBoundingClientRect —— 固定锚点与外框测量都不受影响。
  */
  overflow: hidden;
}
/*
  板面滚动区：overflow 仍是 auto（滚动能力保留），滚动条外观由统一规则去掉：
  v-qio-scrollbar="'hidden'" 补上 .qio-scroll-host 与 data-qio-scroll="hidden"，
  引擎在 hidden 策略下**完全不建指示条、不挂监听**，data-qio-range 固定 none。
  坐标系与手势读的都是 computed overflow（见 canScroll / insideScrollable），不受影响。
*/
.board-viewport {
  position: relative;
  flex: 1;
  min-width: 0;
  min-height: 0;
  overflow: auto;
  overscroll-behavior: contain;
  background-color: var(--bg-surface);
  background-image: radial-gradient(circle, color-mix(in srgb, var(--accent) 16%, transparent) 0 1px, transparent 1.1px);
  background-size: 24px 24px;
  background-attachment: local;
  touch-action: none;
}
.board-viewport.dragging { user-select: none; }
.board-viewport.panning { cursor: grabbing; }
.board-viewport.framing { cursor: crosshair; }
.board-scroll-content { position: relative; min-width: 100%; min-height: 100%; }
.board-surface {
  position: relative;
  min-width: 100%;
  min-height: 100%;
  background: transparent;
  will-change: transform;
}
/* 暗色底同主题色点阵，略提高一点点可见度；亮色保持白底。 */
:global(:root:not([data-theme="light"])) .board-viewport {
  background-image: radial-gradient(circle, color-mix(in srgb, var(--accent) 20%, transparent) 0 1px, transparent 1.1px);
}
/*
 * 尺寸拖动中：关掉卡片的几何过渡（left / top / width / height）。
 * 卡片自己的过渡是「松手后归位」用的；拖动中还带着它，预览会滞后于指针（看着不跟手）。
 * 用父层类名 + :deep 关，避免动 BoardCard（子智能体 B 的写域）。
 */
.board-surface.resizing :deep(.card) { transition: none; }
/* 拖动中同样关掉几何过渡：位置预览必须逐帧跟手，不能有"追着指针跑"的滞后（A4）。 */
.board-surface.dragging :deep(.card) { transition: none; }
/*
  拖线落到成员卡：卡片通过既有的 highlight 布尔 prop 拿到 .highlight 高亮类
  （BoardCard 根节点是 article + Teleport 片段，class 不能可靠透传；不改卡片组件）。
  这里只在「拖线目标是卡片」时把高亮改成品牌色，组内成员卡优先于组框。
*/
.board-surface.link-target-card :deep(.card.highlight) {
  outline: 2px solid var(--accent);
  outline-offset: 3px;
  box-shadow: 0 0 0 4px var(--accent-soft), var(--shadow-1);
}
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
  border-radius: var(--r-lg);
  box-shadow: var(--shadow-2);
  pointer-events: none;
}
.link-editor {
  position: absolute;
  z-index: 70;
  width: min(420px, 92%);
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  padding: var(--sp-3);
  background: var(--bg-elevated);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-lg);
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
  border-radius: var(--r-lg);
  padding: var(--sp-1);
}
.link-actions { display: flex; gap: var(--sp-1); flex-wrap: wrap; }
.btn {
  font: inherit;
  font-size: var(--fs-xs);
  color: var(--text-secondary);
  background: var(--bg-surface);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-lg);
  padding: 2px var(--sp-2);
  cursor: pointer;
}
.btn:hover { color: var(--text-strong); border-color: var(--border-strong); }
.btn:focus-visible { outline: 2px solid var(--link); outline-offset: 1px; }
.btn.primary { color: var(--on-accent); background: var(--accent); border-color: var(--accent); }
.btn.danger { color: var(--danger); }
@media (max-width: 900px) {
  .link-editor { left: var(--sp-2); bottom: var(--sp-2); }
  /* 窄窗隐藏首次使用提示条：两行提示会盖住板面顶部与组标题；手势本身仍可用。 */
  .banner.view-hint { display: none; }
}
</style>
