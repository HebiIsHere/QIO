<!-- 待审批预览叠加层（Stage 1）：把 QIO 的虚线预览画在真实板面上，并允许编辑未批准的计划。

  契约：docs/interactive-mode-contract.md §1.6 / §4.4；
        docs/superpowers/specs/2026-10-21-intent-preview-apply-design.md §4.3 / §5。
  本轮冻结规则（Stage 1 + Stage 2）：
  - 数据来自 store.intents 里**仍未落地**的意图（proposed / awaiting_confirmation / approved，
    以及旧状态 pending / needs_update / waiting_dependency / waiting_confirm / running）的
    preview（结构与 BoardState 的 cards/groups/links 子集相同）；已 applied 或已结束的不画；
  - 一律虚线：未获得的实际结果不能伪装成确定结论；元素上写「预览（未确定）」；
  - 链接方向只画用户写明的方向，不解释成因果 / 支持 / 执行顺序；
  - 整层 pointer-events: none：不参与拖动与选择命中，不挡正式卡片操作；
    只有「可编辑预演」的幽灵卡（proposed / awaiting_confirmation 且带 operations）打开
    pointer-events，用于拖动改落点 / 双击编辑正文 / Ctrl(⌘)+点击多选 / 删除新增卡；
  - Stage 2 编辑（全部走既有 PATCH plan）：
    多选 >=2 张 → 成组（追加 group_cards）；恰好 2 张 → 连线（追加 add_link）；
    双击幽灵卡 → 编辑 Markdown 正原文（空白正文本地拦截）；删除时连带清理依赖操作；
    任何一步失败都**如实提示并回滚本地预演**，绝不假成功；
  - 新增控件（选择工具条 / 正文编辑器）都在预览层内、只接管自己的指针与键盘事件，
    不改板面手势与坐标换算，也不挡真实卡片操作；
  - 收到 window 事件 qio:interactive:locate-preview 时由 BoardCanvas 高亮对应意图。
-->
<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from "vue";
import { useInteractiveStore } from "../../stores/interactive";
import { CARD_KIND_LABELS } from "../../interactive/board";
import type { BoardCard, BoardGroup, Intent, IntentOperation } from "../../interactive/types";
import { linkGeometry } from "../../interactive/linkGeometry";
import { groupOutline } from "../../interactive/groupOutline";
import {
  isPlanEditable,
  movePreviewCardInPlan,
  persistPlanEdit,
  previewCardOperationIndex,
  removePreviewCardFromPlan,
  type PlanEditBaseline,
} from "../../interactive/intentPreviewApply";
import {
  editPreviewCardContent,
  groupPreviewCards,
  linkPreviewCards,
  readPreviewCardContent,
  togglePreviewSelection,
  type PreviewPlanEdit,
} from "../../interactive/intentPreviewEdit";

const props = defineProps<{
  intents: Intent[];
  locatedIntentId: string | null;
  width: number;
  height: number;
  /**
   * 指针（屏幕坐标）→ 板面坐标。由 BoardCanvas 提供，保证与卡片拖动 / 框选
   * 走**同一条** viewport.toBoardPoint 口径（缩放与平移都算对）。
   */
  toBoardPoint?: (clientX: number, clientY: number) => { x: number; y: number };
}>();

const emit = defineEmits<{
  (event: "edit-error", message: string): void;
  (event: "edit-applied", intentId: string): void;
}>();

const store = useInteractiveStore();

interface PreviewCardBox {
  id: string;
  style: Record<string, string>;
  kindLabel: string;
  text: string;
  /** 这张幽灵卡可拖动 / 可删除 / 可编辑（未批准 + 有对应 add 操作） */
  editable: boolean;
  /** Stage 2：Ctrl/⌘+点击多选中的卡片 */
  selected: boolean;
}

interface PreviewGroupRect {
  x: number;
  y: number;
  w: number;
  h: number;
}

interface PreviewGroupSegment {
  x1: number;
  y1: number;
  x2: number;
  y2: number;
}

/**
 * 预览组（用户决策 2 / 问题 1、4c）：组框改为贴合成员的轮廓。
 * - 轮廓由成员虚卡的**显示位置**实时算出（拖动成员 / 整组时跟着走）；
 * - 找不到成员的组不画（不再出现左上角的大空框）；
 * - 头部是浮动的小条（组名 / 有序 / 张数），整组拖动从这里开始。
 */
interface PreviewGroupView {
  id: string;
  name: string;
  ordered: boolean;
  memberCount: number;
  sequenceTitle: string;
  /** 轮廓 svg 的定位（板面坐标；内部坐标已平移到 0,0） */
  svgStyle: Record<string, string>;
  /** 轮廓外接矩形（板面坐标；预览标注定位用） */
  bounds: PreviewGroupRect;
  /** 浮动头部（板面坐标） */
  headerStyle: Record<string, string>;
  fills: PreviewGroupRect[];
  segments: PreviewGroupSegment[];
  editable: boolean;
}

interface PreviewLine {
  id: string;
  x1: number;
  y1: number;
  x2: number;
  y2: number;
  direction: boolean;
  meaning: string;
  labelX: number;
  labelY: number;
  labelW: number;
}

interface RenderedIntent {
  id: string;
  title: string;
  statusLabel: string;
  located: boolean;
  badgeStyle: Record<string, string>;
  editable: boolean;
  saving: boolean;
  cards: PreviewCardBox[];
  groups: PreviewGroupView[];
  lines: PreviewLine[];
}

const rootEl = ref<HTMLElement | null>(null);

/* --------------------------------------------------------------------------
 * 本地编辑状态（只在显示态；不写 store、不改板面）
 * ------------------------------------------------------------------------ */

interface EditDragState {
  intentId: string;
  cardId: string;
  startClient: { x: number; y: number };
  startBoard: { x: number; y: number };
  startCard: { x: number; y: number };
  /** 超过 3px 才算拖动：纯点击不提交任何修改 */
  moved: boolean;
  current: { x: number; y: number };
}

const drag = ref<EditDragState | null>(null);
/** 本地乐观位置（"intentId:cardId" → 板面坐标）：拖动期间跟手，PATCH 返回后清掉。 */
const localPositions = ref<Record<string, { x: number; y: number }>>({});
/** 正在保存预演修改的意图 id（同一时间只处理一次） */
const saving = ref<string | null>(null);

/** 整组拖动（问题 1）：从组头部按下开始；松手时统一改成员的 add 操作坐标再 PATCH plan。 */
interface GroupDragState {
  intentId: string;
  groupId: string;
  memberIds: string[];
  startClient: { x: number; y: number };
  startBoard: { x: number; y: number };
  moved: boolean;
  dx: number;
  dy: number;
}
const groupDrag = ref<GroupDragState | null>(null);

/* --------------------------------------------------------------------------
 * Stage 2：多选（成组 / 连线）与正文编辑（只改 operations，预览以服务端重算为准）
 * ------------------------------------------------------------------------ */

/** 多选状态：只在一个意图内选择（跨意图成组 / 连线没有意义）。 */
interface PreviewSelection {
  intentId: string;
  cardIds: string[];
}
const selection = ref<PreviewSelection | null>(null);

/** 正文编辑器（双击幽灵卡打开；保存 = 改 add 操作 content 后 PATCH plan）。 */
interface PreviewEditorState {
  intentId: string;
  cardId: string;
  content: string;
  error: string;
}
const editor = ref<PreviewEditorState | null>(null);

/** 幽灵卡的当前显示位置（单卡拖动用本地乐观位置；整组拖动再叠加组位移）。 */
function cardDisplayPosition(intentId: string, card: BoardCard): { x: number; y: number } {
  const base = localPositions.value[keyFor(intentId, card.id)] ?? { x: card.x, y: card.y };
  const groupState = groupDrag.value;
  if (
    groupState &&
    groupState.intentId === intentId &&
    groupState.memberIds.includes(card.id)
  ) {
    return { x: base.x + groupState.dx, y: base.y + groupState.dy };
  }
  return base;
}

function selectedCardIds(intentId: string): string[] {
  const current = selection.value;
  return current && current.intentId === intentId ? current.cardIds : [];
}

function isCardSelected(intentId: string, cardId: string): boolean {
  return selectedCardIds(intentId).includes(cardId);
}

function toggleCardSelection(intentId: string, cardId: string): void {
  const next = togglePreviewSelection(selectedCardIds(intentId), cardId);
  selection.value = next.length ? { intentId, cardIds: next } : null;
}

function clearSelection(): void {
  selection.value = null;
}

function dropFromSelection(intentId: string, cardId: string): void {
  const current = selection.value;
  if (!current || current.intentId !== intentId) return;
  const next = current.cardIds.filter((item) => item !== cardId);
  selection.value = next.length ? { intentId, cardIds: next } : null;
}

interface SelectionToolbarView {
  intentId: string;
  style: Record<string, string>;
  count: number;
  canGroup: boolean;
  canLink: boolean;
}

/**
 * 选择工具条：贴着被选中卡片的上方显示（板面坐标；整层在缩放层里，所以随缩放移动）。
 * 只有 >=2 张时才出现（成组门槛）；恰好 2 张时「连线」可用。
 */
const selectionToolbar = computed<SelectionToolbarView | null>(() => {
  const current = selection.value;
  if (!current) return null;
  const intent = props.intents.find((item) => item.id === current.intentId);
  if (!intent || !isPlanEditable(intent) || saving.value) return null;
  const cards = (intent.preview?.cards ?? []).filter((card) =>
    current.cardIds.includes(card.id),
  );
  if (cards.length < 2) return null;
  const boxes = cards.map((card) => {
    const position = cardDisplayPosition(intent.id, card);
    return { x: position.x, y: position.y, w: card.w, h: card.h };
  });
  const left = Math.min(...boxes.map((box) => box.x));
  const top = Math.min(...boxes.map((box) => box.y));
  return {
    intentId: intent.id,
    style: {
      left: Math.round(left) + "px",
      top: Math.max(0, Math.round(top) - 38) + "px",
    },
    count: boxes.length,
    canGroup: boxes.length >= 2,
    canLink: boxes.length === 2,
  };
});

interface EditorPanelView {
  style: Record<string, string>;
}

/** 编辑器面板位置：贴着被编辑的幽灵卡下方（同样用板面坐标）。 */
const editorPanel = computed<EditorPanelView | null>(() => {
  const state = editor.value;
  if (!state) return null;
  const intent = props.intents.find((item) => item.id === state.intentId);
  if (!intent) return null;
  const card = (intent.preview?.cards ?? []).find((item) => item.id === state.cardId);
  if (!card) return null;
  const position = cardDisplayPosition(state.intentId, card);
  return {
    style: {
      left: Math.round(position.x) + "px",
      top: Math.max(0, Math.round(position.y + card.h + 8)) + "px",
      width: "300px",
    },
  };
});

function keyFor(intentId: string, cardId: string): string {
  return intentId + ":" + cardId;
}

/**
 * 屏幕坐标 → 板面坐标。优先用 BoardCanvas 注入的函数（唯一口径）；
 * 万一没注入（独立挂载）就按 .board-surface 的真实缩放兜底，绝不假装 scale=1 却不除缩放。
 */
function boardPointOf(clientX: number, clientY: number): { x: number; y: number } {
  if (props.toBoardPoint) return props.toBoardPoint(clientX, clientY);
  const surface = rootEl.value?.closest?.(".board-surface") as HTMLElement | null;
  if (!surface || typeof surface.getBoundingClientRect !== "function") return { x: clientX, y: clientY };
  const rect = surface.getBoundingClientRect();
  const unscaled = surface.offsetWidth || rect.width;
  const scale = unscaled > 0 && rect.width > 0 ? rect.width / unscaled : 1;
  return { x: (clientX - rect.left) / scale, y: (clientY - rect.top) / scale };
}

function attachPointerListeners(): void {
  if (typeof window === "undefined") return;
  window.addEventListener("pointermove", onPointerMove);
  window.addEventListener("pointerup", onPointerUp);
  window.addEventListener("pointercancel", onPointerCancel);
  window.addEventListener("keydown", onKeydown, true);
}

function detachPointerListeners(): void {
  if (typeof window === "undefined") return;
  window.removeEventListener("pointermove", onPointerMove);
  window.removeEventListener("pointerup", onPointerUp);
  window.removeEventListener("pointercancel", onPointerCancel);
  window.removeEventListener("keydown", onKeydown, true);
}

function clearLocal(intentId: string, cardId: string): void {
  const key = keyFor(intentId, cardId);
  if (!(key in localPositions.value)) return;
  const next = { ...localPositions.value };
  delete next[key];
  localPositions.value = next;
}

function clearAllLocal(): void {
  localPositions.value = {};
}

function onPointerMove(event: PointerEvent): void {
  const groupState = groupDrag.value;
  if (groupState) {
    if (
      !groupState.moved &&
      (Math.abs(event.clientX - groupState.startClient.x) > 3 ||
        Math.abs(event.clientY - groupState.startClient.y) > 3)
    ) {
      groupState.moved = true;
    }
    if (!groupState.moved) return;
    const point = boardPointOf(event.clientX, event.clientY);
    groupState.dx = point.x - groupState.startBoard.x;
    groupState.dy = point.y - groupState.startBoard.y;
    return;
  }
  const state = drag.value;
  if (!state) return;
  if (
    !state.moved &&
    (Math.abs(event.clientX - state.startClient.x) > 3 ||
      Math.abs(event.clientY - state.startClient.y) > 3)
  ) {
    state.moved = true;
  }
  if (!state.moved) return;
  const point = boardPointOf(event.clientX, event.clientY);
  const x = Math.round(state.startCard.x + (point.x - state.startBoard.x));
  const y = Math.round(state.startCard.y + (point.y - state.startBoard.y));
  state.current = { x, y };
  localPositions.value = {
    ...localPositions.value,
    [keyFor(state.intentId, state.cardId)]: { x, y },
  };
}

function onPointerUp(event: PointerEvent): void {
  const groupState = groupDrag.value;
  if (groupState) {
    detachPointerListeners();
    groupDrag.value = null;
    if (groupState.moved) void commitGroupMove(groupState);
    return;
  }
  const state = drag.value;
  detachPointerListeners();
  drag.value = null;
  if (!state) return;
  if (!state.moved) {
    // 纯点击：不产生任何板面修改（也不会误 PATCH）；Ctrl/⌘+点击 = 多选切换
    clearLocal(state.intentId, state.cardId);
    if (event?.ctrlKey || event?.metaKey) {
      toggleCardSelection(state.intentId, state.cardId);
    } else if (selection.value) {
      clearSelection();
    }
    return;
  }
  const intent = props.intents.find((item) => item.id === state.intentId);
  if (!intent) {
    clearAllLocal();
    return;
  }
  const moved = movePreviewCardInPlan(intent.operations ?? [], state.cardId, state.current);
  if (!moved.ok) {
    clearLocal(state.intentId, state.cardId);
    emit("edit-error", moved.reason);
    return;
  }
  void commitPlan(intent, moved.operations);
}

function onPointerCancel(): void {
  if (groupDrag.value) {
    groupDrag.value = null;
    detachPointerListeners();
    return;
  }
  const state = drag.value;
  detachPointerListeners();
  drag.value = null;
  if (!state) return;
  // Esc / 指针中断：本地预演回到操作前，不提交任何修改
  clearLocal(state.intentId, state.cardId);
}

function onKeydown(event: KeyboardEvent): void {
  if (event.key !== "Escape") return;
  if (!drag.value && !groupDrag.value) return;
  event.preventDefault();
  onPointerCancel();
}

/**
 * 把一次预演修改发给服务端（PATCH plan），并按结果决定本地显示：
 * - 成功：清掉本地乐观位置，store 会按服务端重算结果重新读取意图；
 * - 失败：清掉本地乐观位置（回到修改前），并把真实原因交给 BoardCanvas 提示 —— 不假成功。
 */
async function commitPlan(
  intent: Intent,
  operations: IntentOperation[],
): Promise<{ ok: boolean; error: string }> {
  const baseline: PlanEditBaseline = {
    preview: intent.preview ?? { cards: [], groups: [], links: [], note: "" },
    operations: intent.operations ?? [],
  };
  saving.value = intent.id;
  try {
    const outcome = await persistPlanEdit({
      baseline,
      nextOperations: operations,
      patch: (next) => store.updateIntentPlan(intent.id, next),
    });
    clearAllLocal();
    if (!outcome.ok) {
      emit("edit-error", "预演修改没有保存（已回到修改前）：" + outcome.error);
      return { ok: false, error: outcome.error };
    }
    // 成功：服务端会重算 preview；选择与编辑器都收起（不留悬空的旧状态）
    clearSelection();
    editor.value = null;
    emit("edit-applied", intent.id);
    return { ok: true, error: "" };
  } finally {
    saving.value = null;
  }
}

function intentAt(intentId: string): Intent | undefined {
  return props.intents.find((item) => item.id === intentId);
}

function onCardPointerDownById(intentId: string, cardId: string, event: PointerEvent): void {
  const intent = intentAt(intentId);
  if (!intent) return;
  if (!isPlanEditable(intent) || saving.value) return;
  const card = (intent.preview?.cards ?? []).find((item) => item.id === cardId);
  if (!card) return;
  if (previewCardOperationIndex(intent.operations ?? [], cardId) < 0) return;
  if (event.button !== undefined && event.button !== 0) return;
  // 不把这次指针交给板面（否则会变成平移 / 框选）
  event.preventDefault();
  event.stopPropagation();
  const position = localPositions.value[keyFor(intentId, cardId)] ?? { x: card.x, y: card.y };
  drag.value = {
    intentId,
    cardId,
    startClient: { x: event.clientX, y: event.clientY },
    startBoard: boardPointOf(event.clientX, event.clientY),
    startCard: { x: position.x, y: position.y },
    moved: false,
    current: { x: position.x, y: position.y },
  };
  attachPointerListeners();
}

/**
 * 组头部按下（问题 1）：开始整组拖动。只移动计划内的新增虚卡；
 * 没有可移动成员 / 计划不可编辑 / 正在保存时不进入拖动。
 */
function onGroupHeaderPointerDown(
  intentId: string,
  groupId: string,
  event: PointerEvent,
): void {
  if (event.button !== undefined && event.button !== 0) return;
  const intent = intentAt(intentId);
  if (!intent || !isPlanEditable(intent) || saving.value) return;
  const group = (intent.preview?.groups ?? []).find((item) => item.id === groupId);
  if (!group) return;
  const memberIds = (intent.preview?.cards ?? [])
    .filter((card) => group.members.includes(card.id))
    .map((card) => card.id);
  if (!memberIds.length) return;
  event.preventDefault();
  event.stopPropagation();
  groupDrag.value = {
    intentId,
    groupId,
    memberIds,
    startClient: { x: event.clientX, y: event.clientY },
    startBoard: boardPointOf(event.clientX, event.clientY),
    moved: false,
    dx: 0,
    dy: 0,
  };
  attachPointerListeners();
}

/** 整组拖动松手：每个成员虚卡按位移改自己的 add 操作坐标，一次 PATCH plan。 */
async function commitGroupMove(state: GroupDragState): Promise<void> {
  const intent = intentAt(state.intentId);
  if (!intent) return;
  const dx = Math.round(state.dx);
  const dy = Math.round(state.dy);
  if (!dx && !dy) return;
  const memberCards = (intent.preview?.cards ?? []).filter((card) =>
    state.memberIds.includes(card.id),
  );
  if (!memberCards.length) return;
  const operations: IntentOperation[] = (intent.operations ?? []).map((op) => ({ ...op }));
  let changed = false;
  for (const card of memberCards) {
    const index = previewCardOperationIndex(operations, card.id);
    if (index < 0) continue;
    operations[index] = {
      ...operations[index],
      x: Math.round(card.x) + dx,
      y: Math.round(card.y) + dy,
    };
    changed = true;
  }
  if (!changed) return;
  await commitPlan(intent, operations);
}

async function onDeleteById(intentId: string, cardId: string, event: MouseEvent): Promise<void> {
  event.preventDefault();
  event.stopPropagation();
  const intent = intentAt(intentId);
  if (!intent || !isPlanEditable(intent) || saving.value) return;
  const removed = removePreviewCardFromPlan(
    intent.operations ?? [],
    cardId,
    intent.preview?.refs,
  );
  if (!removed.ok) {
    emit("edit-error", removed.reason);
    return;
  }
  clearLocal(intentId, cardId);
  dropFromSelection(intentId, cardId);
  if (editor.value?.intentId === intentId && editor.value?.cardId === cardId) {
    editor.value = null;
  }
  await commitPlan(intent, removed.operations);
}

/** 成组 / 连线的公共入口：把选中卡片交给纯函数，成功后 PATCH plan。 */
async function applySelectionEdit(
  transform: (operations: IntentOperation[], cardIds: string[]) => PreviewPlanEdit,
): Promise<void> {
  const current = selection.value;
  if (!current || saving.value) return;
  const intent = intentAt(current.intentId);
  if (!intent || !isPlanEditable(intent)) return;
  const result = transform(intent.operations ?? [], current.cardIds);
  if (!result.ok) {
    emit("edit-error", result.reason);
    return;
  }
  await commitPlan(intent, result.operations);
}

async function onGroupSelected(): Promise<void> {
  await applySelectionEdit(groupPreviewCards);
}

async function onLinkSelected(): Promise<void> {
  await applySelectionEdit(linkPreviewCards);
}

/** 双击幽灵卡：打开正文编辑器（Markdown 原文；空白提交会被本地拦截）。 */
function onCardDoubleClickById(
  intentId: string,
  cardId: string,
  event: MouseEvent,
): void {
  event.preventDefault();
  event.stopPropagation();
  const intent = intentAt(intentId);
  if (!intent || !isPlanEditable(intent) || saving.value) return;
  const content = readPreviewCardContent(intent.operations ?? [], cardId);
  if (content === null) return;
  clearSelection();
  editor.value = { intentId, cardId, content, error: "" };
}

function closeEditor(): void {
  if (saving.value) return;
  editor.value = null;
}

async function saveEditor(): Promise<void> {
  const state = editor.value;
  if (!state || saving.value) return;
  const intent = intentAt(state.intentId);
  if (!intent) {
    editor.value = null;
    return;
  }
  const result = editPreviewCardContent(intent.operations ?? [], state.cardId, state.content);
  if (!result.ok) {
    // 空白正文等本地可判定的错误：不发注定失败的请求
    state.error = result.reason;
    return;
  }
  state.error = "";
  const outcome = await commitPlan(intent, result.operations);
  if (!outcome.ok && editor.value) {
    // 服务端拒绝 / 网络失败：编辑器保持打开，原因原样给出（不假成功、不吞错）
    editor.value.error = "正文没有保存（已回到修改前）：" + outcome.error;
  }
}

function cardText(card: BoardCard): string {
  const text = (card.content || String(card.meta?.name ?? card.meta?.title ?? "")).trim();
  if (!text) return "（无内容）";
  return text.length > 20 ? text.slice(0, 20) + "…" : text;
}

/**
 * 预览组轮廓（问题 1 / 4c）：**始终**按成员虚卡的显示位置推算；
 * 找不到成员就返回 null（不画大空框，也不信任 preview.groups 自带坐标）。
 */
function previewGroupView(
  group: BoardGroup,
  cards: BoardCard[],
  editable: boolean,
): PreviewGroupView | null {
  const members = group.members
    .map((id) => cards.find((card) => card.id === id))
    .filter((card): card is BoardCard => Boolean(card));
  if (!members.length) return null;
  const outline = groupOutline(
    members.map((card) => ({
      cardId: card.id,
      rect: { x: card.x, y: card.y, w: card.w, h: card.h },
    })),
  );
  if (!outline) return null;
  const bounds = outline.bounds;
  return {
    id: group.id,
    name: group.name,
    ordered: Boolean(group.ordered),
    memberCount: members.length,
    sequenceTitle: group.ordered
      ? members.map((card, index) => String(index + 1) + ". " + cardText(card)).join(" · ")
      : "普通组：摆放顺序不代表先后",
    bounds: { x: bounds.x, y: bounds.y, w: bounds.w, h: bounds.h },
    svgStyle: {
      left: bounds.x + "px",
      top: bounds.y + "px",
      width: Math.max(1, bounds.w) + "px",
      height: Math.max(1, bounds.h) + "px",
    },
    headerStyle: {
      left: outline.header.x + "px",
      top: outline.header.y + "px",
      width: Math.max(120, outline.header.w) + "px",
    },
    fills: outline.members.map((fill) => ({
      x: fill.x - bounds.x,
      y: fill.y - bounds.y,
      w: fill.w,
      h: fill.h,
    })),
    segments: outline.segments.map((segment) => ({
      x1: segment.x1 - bounds.x,
      y1: segment.y1 - bounds.y,
      x2: segment.x2 - bounds.x,
      y2: segment.y2 - bounds.y,
    })),
    editable,
  };
}

function boxStyle(rect: PreviewGroupRect): Record<string, string> {
  return {
    left: rect.x + "px",
    top: rect.y + "px",
    width: Math.max(1, rect.w) + "px",
    height: Math.max(1, rect.h) + "px",
  };
}

const rendered = computed<RenderedIntent[]>(() =>
  props.intents.map((intent) => {
    const preview = intent.preview;
    const originalCards = preview?.cards ?? [];
    const groups = preview?.groups ?? [];
    const links = preview?.links ?? [];
    const editableIntent = isPlanEditable(intent) && !saving.value;

    // 拖动期间用本地乐观位置渲染（跟手），其余时候就是服务端预览坐标
    const cards: BoardCard[] = originalCards.map((card) => {
      const position = cardDisplayPosition(intent.id, card);
      return position.x === card.x && position.y === card.y
        ? card
        : { ...card, x: position.x, y: position.y };
    });
    const byId = new Map(cards.map((card) => [card.id, card]));

    const cardBoxes: PreviewCardBox[] = cards.map((card) => ({
      id: card.id,
      style: boxStyle({ x: card.x, y: card.y, w: card.w, h: card.h }),
      kindLabel: CARD_KIND_LABELS[card.kind],
      text: cardText(card),
      editable:
        editableIntent && previewCardOperationIndex(intent.operations ?? [], card.id) >= 0,
      selected: isCardSelected(intent.id, card.id),
    }));

    const groupViews: PreviewGroupView[] = groups
      .map((group) => previewGroupView(group, cards, editableIntent))
      .filter((group): group is PreviewGroupView => group !== null);

    const lines: PreviewLine[] = [];
    for (const link of links) {
      const src = byId.get(link.src);
      const dst = byId.get(link.dst);
      if (!src || !dst) continue;
      const meaning = link.meaning.trim() || (link.direction ? "有方向（含义待填写）" : "关联（含义待填写）");
      const labelW = Math.max(64, meaning.length * 13 + 18);
      // 关系线几何的唯一实现（与 BoardLinkLayer 共用）：端点落在边框上，标签避开卡片。
      const geometry = linkGeometry(
        { x: src.x, y: src.y, w: src.w, h: src.h },
        { x: dst.x, y: dst.y, w: dst.w, h: dst.h },
        { label: { w: labelW, h: 20 } },
      );
      lines.push({
        id: link.id,
        x1: geometry.x1,
        y1: geometry.y1,
        x2: geometry.x2,
        y2: geometry.y2,
        direction: Boolean(link.direction),
        meaning,
        labelX: geometry.labelX,
        labelY: geometry.labelY,
        labelW,
      });
    }

    // 预览标注放在预览范围的上方，避免盖住内容
    const rects = [
      ...cards.map((card) => ({ x: card.x, y: card.y, w: card.w, h: card.h })),
      ...groupViews.map((group) => group.bounds),
    ];
    const top = rects.length ? Math.min(...rects.map((rect) => rect.y)) : 0;
    const left = rects.length ? Math.min(...rects.map((rect) => rect.x)) : 0;

    return {
      id: intent.id,
      title: intent.title || "待审批结果",
      statusLabel: store.statusLabel(intent.status),
      located: props.locatedIntentId === intent.id,
      badgeStyle: { left: left + "px", top: Math.max(0, top - 24) + "px" },
      editable: editableIntent,
      saving: saving.value === intent.id,
      cards: cardBoxes,
      groups: groupViews,
      lines,
    };
  }),
);

onBeforeUnmount(() => {
  detachPointerListeners();
  drag.value = null;
  clearAllLocal();
});
</script>

<template>
  <!-- 整层不参与命中（pointer-events: none），也不建任何滚动容器：data-qio-fit 只兜住横向边界 -->
  <div
    ref="rootEl"
    class="preview-layer"
    data-qio-fit
    aria-label="待审批预览（虚线，未确定）"
  >
    <div
      v-for="item in rendered"
      :key="item.id"
      class="preview-intent"
      :class="{ located: item.located }"
      data-im="preview"
      :data-intent-id="item.id"
    >
      <p class="preview-badge" :style="item.badgeStyle">
        预览（未确定）· {{ item.title }} · {{ item.statusLabel
        }}<template v-if="item.editable"> · 拖动 / 双击编辑 / Ctrl(⌘)+多选</template>
      </p>

      <template v-for="group in item.groups" :key="group.id">
        <svg
          class="preview-group-outline"
          :style="group.svgStyle"
          aria-hidden="true"
          data-im="preview-group-outline"
          :data-intent-id="item.id"
          :data-preview-group-id="group.id"
        >
          <line
            v-for="(segment, index) in group.segments"
            :key="'seg-' + index"
            class="preview-group-segment"
            :x1="segment.x1"
            :y1="segment.y1"
            :x2="segment.x2"
            :y2="segment.y2"
          />
        </svg>
        <div
          class="preview-group-head"
          :class="{ draggable: group.editable, saving: item.saving }"
          :style="group.headerStyle"
          data-im="preview-group-head"
          :data-intent-id="item.id"
          :data-preview-group-id="group.id"
          :title="group.sequenceTitle"
          @pointerdown="group.editable && onGroupHeaderPointerDown(item.id, group.id, $event)"
          @dragstart.prevent
        >
          <span class="preview-group-grip" aria-hidden="true">⠿</span>
          <span class="preview-group-name">{{ group.name }}</span>
          <span v-if="group.ordered" class="preview-group-meta mono">有序</span>
          <span class="preview-group-meta mono">{{ group.memberCount }} 张</span>
        </div>
      </template>

      <div
        v-for="card in item.cards"
        :key="card.id"
        class="preview-card"
        :class="{ editable: card.editable, saving: item.saving, selected: card.selected }"
        :style="card.style"
        data-im="preview"
        :data-intent-id="item.id"
        :data-preview-card-id="card.id"
        :data-selected="card.selected ? '1' : '0'"
        @pointerdown="card.editable && onCardPointerDownById(item.id, card.id, $event)"
        @dblclick="card.editable && onCardDoubleClickById(item.id, card.id, $event)"
        @dragstart.prevent
      >
        <span class="preview-card-kind mono">{{ card.kindLabel }}</span>
        <span class="preview-card-text">{{ card.text }}</span>
        <button
          v-if="card.editable"
          class="preview-card-delete"
          type="button"
          data-im="preview-delete"
          :data-intent-id="item.id"
          :data-preview-card-id="card.id"
          :disabled="item.saving"
          :aria-label="'删除这张预演卡片：' + card.text"
          title="删除这张预演卡片（只改计划，不动板面正式内容）"
          @pointerdown.stop.prevent
          @click.stop.prevent="onDeleteById(item.id, card.id, $event)"
        >
          ×
        </button>
      </div>

      <svg class="preview-links" :width="width" :height="height" aria-hidden="true">
        <defs>
          <marker
            :id="'im-preview-arrow-' + item.id"
            viewBox="0 0 10 10"
            refX="9"
            refY="5"
            markerWidth="7"
            markerHeight="7"
            orient="auto-start-reverse"
          >
            <path class="preview-arrow" d="M 0 0 L 10 5 L 0 10 z" />
          </marker>
        </defs>
        <g v-for="line in item.lines" :key="line.id" data-im="preview" :data-intent-id="item.id">
          <line
            class="preview-line"
            :x1="line.x1"
            :y1="line.y1"
            :x2="line.x2"
            :y2="line.y2"
            :marker-end="line.direction ? 'url(#im-preview-arrow-' + item.id + ')' : undefined"
          />
          <rect
            class="preview-line-label-bg"
            :x="line.labelX - line.labelW / 2"
            :y="line.labelY - 10"
            :width="line.labelW"
            height="20"
            rx="6"
          />
          <text class="preview-line-label" :x="line.labelX" :y="line.labelY + 4" text-anchor="middle">
            {{ line.meaning }}
          </text>
        </g>
      </svg>
    </div>

    <!--
      Stage 2 选择工具条：多选 >=2 张幽灵卡时出现（贴着选中卡片上方）。
      只接管自己的指针 / 键盘事件 —— 不触发板面平移 / 框选，也不改任何板面状态。
    -->
    <div
      v-if="selectionToolbar"
      class="preview-toolbar"
      :style="selectionToolbar.style"
      data-im="preview-select-toolbar"
      :data-intent-id="selectionToolbar.intentId"
      role="toolbar"
      aria-label="预演卡片选择"
      @pointerdown.stop
      @click.stop
      @keydown.stop
    >
      <span class="preview-toolbar-count mono">已选 {{ selectionToolbar.count }} 张</span>
      <button
        class="preview-btn"
        type="button"
        data-im="preview-group-action"
        :disabled="!selectionToolbar.canGroup || Boolean(saving)"
        :title="selectionToolbar.canGroup ? '把选中的预演卡片组成一个组' : '成组至少需要 2 张'"
        @click.stop="onGroupSelected"
      >
        成组
      </button>
      <button
        class="preview-btn"
        type="button"
        data-im="preview-link-action"
        :disabled="!selectionToolbar.canLink || Boolean(saving)"
        :title="selectionToolbar.canLink ? '在选中的两张卡片之间建立关系（无方向）' : '连线需要恰好 2 张'"
        @click.stop="onLinkSelected"
      >
        连线
      </button>
      <button
        class="preview-btn ghost"
        type="button"
        data-im="preview-selection-clear"
        :disabled="Boolean(saving)"
        @click.stop="clearSelection"
      >
        取消选择
      </button>
    </div>

    <!--
      Stage 2 正文编辑器：双击幽灵卡打开；保存 = 改对应 add 操作的 content 后 PATCH plan。
      空白正文本地拦截；失败如实提示并回到修改前（不假成功）。
    -->
    <div
      v-if="editor && editorPanel"
      class="preview-editor"
      :style="editorPanel.style"
      data-im="preview-editor"
      :data-intent-id="editor.intentId"
      :data-preview-card-id="editor.cardId"
      role="dialog"
      aria-label="编辑预演正文"
      @pointerdown.stop
      @click.stop
      @keydown.stop
      @keyup.stop
    >
      <p class="preview-editor-title">编辑预演正文（Markdown 原文）</p>
      <textarea
        v-model="editor.content"
        class="preview-editor-input"
        data-im="preview-editor-input"
        rows="5"
        spellcheck="false"
        :disabled="Boolean(saving)"
      ></textarea>
      <p v-if="editor.error" class="preview-editor-error" role="alert" data-im="preview-editor-error">
        {{ editor.error }}
      </p>
      <div class="preview-editor-actions">
        <button
          class="preview-btn primary"
          type="button"
          data-im="preview-editor-save"
          :disabled="Boolean(saving)"
          @click.stop="saveEditor"
        >
          {{ saving ? "保存中…" : "保存" }}
        </button>
        <button
          class="preview-btn ghost"
          type="button"
          data-im="preview-editor-cancel"
          :disabled="Boolean(saving)"
          @click.stop="closeEditor"
        >
          取消
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.preview-layer {
  position: absolute;
  left: 0;
  top: 0;
  width: 100%;
  height: 100%;
  pointer-events: none; /* 不参与拖动与选择命中，不挡正式卡片 */
  z-index: 24;
}
.preview-intent { position: absolute; left: 0; top: 0; width: 100%; height: 100%; }
.preview-intent.located .preview-card,
.preview-intent.located .preview-group-segment { stroke: var(--warning); }
.preview-intent.located .preview-group-head { color: var(--warning); }
.preview-badge {
  position: absolute;
  margin: 0;
  padding: 0 var(--sp-2);
  font-size: var(--fs-tech, var(--fs-xs));
  color: var(--on-accent);
  background: var(--link);
  border-radius: var(--r-pill);
  white-space: nowrap;
}
.preview-card {
  position: absolute;
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: var(--sp-1) var(--sp-2);
  overflow: hidden;
  border: 1px dashed var(--link);
  border-radius: var(--r-md);
  background: var(--bg-surface);
  opacity: 0.9;
  color: var(--text-primary);
  font-size: var(--fs-sm);
}
/*
  只有「未批准预演」里的新增卡才接管指针：可拖动改落点、可删除。
  正式卡片 / 组 / 连线仍由整层 pointer-events: none 保持不可命中。
*/
.preview-card.editable {
  pointer-events: auto;
  cursor: grab;
  touch-action: none;
}
.preview-card.editable:active { cursor: grabbing; }
.preview-card.saving { opacity: 0.7; }
.preview-card-kind { color: var(--link); font-size: var(--fs-xs); }
.preview-card-text { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.preview-card.editable .preview-card-text { padding-right: var(--sp-4); }
.preview-card-delete {
  position: absolute;
  top: 2px;
  right: 2px;
  width: 16px;
  height: 16px;
  padding: 0;
  line-height: 1;
  font: inherit;
  font-size: var(--fs-xs);
  color: var(--danger);
  background: var(--bg-elevated);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-pill);
  cursor: pointer;
}
.preview-card-delete:hover { border-color: var(--danger); }
.preview-card-delete:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 1px; }
.preview-card-delete:disabled { opacity: 0.5; cursor: default; }
.preview-group-outline { position: absolute; overflow: visible; pointer-events: none; }
.preview-group-segment {
  stroke: var(--link);
  stroke-width: 1.5;
  stroke-dasharray: 6 4;
  stroke-linecap: round;
  stroke-linejoin: round;
}
/*
 * 组头部是浮动小条（贴合轮廓的顶部留白带里）：只有可编辑预览的头部接管指针，
 * 按住拖动 = 整组虚卡同位移；不可编辑时只是显示。
 */
.preview-group-head {
  position: absolute;
  display: flex;
  align-items: center;
  gap: var(--sp-1);
  min-height: 28px;
  padding: 2px var(--sp-2);
  pointer-events: none;
  border: 1px dashed transparent;
  border-radius: var(--r-pill);
  background: transparent;
  color: var(--link);
  font-family: var(--serif);
  font-size: var(--fs-sm);
  white-space: nowrap;
  overflow: hidden;
}
.preview-group-head.draggable {
  pointer-events: auto;
  cursor: grab;
  border-color: var(--link);
  background: var(--bg-elevated);
}
.preview-group-head.draggable:active { cursor: grabbing; }
.preview-group-head.saving { opacity: 0.7; }
.preview-group-grip { color: var(--text-muted); letter-spacing: -1px; }
.preview-group-name { overflow: hidden; text-overflow: ellipsis; }
.preview-group-meta { color: var(--text-secondary); font-size: var(--fs-xs); }
.preview-links { position: absolute; left: 0; top: 0; overflow: visible; }
.preview-arrow { fill: var(--link); }
.preview-line { stroke: var(--link); stroke-width: 1.5; stroke-dasharray: 6 4; }
.preview-line-label-bg { fill: var(--bg-elevated); stroke: var(--border-subtle); }
.preview-line-label { fill: var(--text-secondary); font-size: var(--fs-xs); font-family: var(--sans); }

/* Stage 2：多选高亮（颜色只是辅助，工具条上还有「已选 N 张」文字） */
.preview-card.selected {
  border-style: solid;
  border-color: var(--accent);
  box-shadow: 0 0 0 2px var(--accent-soft);
}

/* Stage 2：选择工具条（贴着选中卡片上方；只接管自己的指针事件） */
.preview-toolbar {
  position: absolute;
  z-index: 26;
  display: flex;
  align-items: center;
  gap: var(--sp-1);
  padding: var(--sp-1) var(--sp-2);
  pointer-events: auto;
  border: 1px solid var(--border-strong);
  border-radius: var(--r-pill);
  background: var(--bg-elevated);
  box-shadow: var(--elev-floating, var(--shadow-2));
  white-space: nowrap;
}
.preview-toolbar-count {
  font-size: var(--fs-xs);
  color: var(--text-secondary);
  padding-right: var(--sp-1);
}
.preview-btn {
  font: inherit;
  font-size: var(--fs-xs);
  color: var(--text-primary);
  background: var(--bg-surface);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-pill);
  padding: 0 var(--sp-2);
  cursor: pointer;
}
.preview-btn:hover { border-color: var(--link); }
.preview-btn:disabled { opacity: 0.5; cursor: default; }
.preview-btn:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 1px; }
.preview-btn.primary {
  color: var(--on-accent);
  background: var(--accent);
  border-color: var(--accent);
}
.preview-btn.ghost { background: none; color: var(--link); }

/* Stage 2：正文编辑器（双击幽灵卡打开；不改板面手势与坐标换算） */
.preview-editor {
  position: absolute;
  z-index: 27;
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
  padding: var(--sp-2);
  pointer-events: auto;
  border: 1px solid var(--border-strong);
  border-radius: var(--r-md);
  background: var(--bg-elevated);
  box-shadow: var(--elev-floating, var(--shadow-2));
  color: var(--text-primary);
  font-family: var(--sans);
}
.preview-editor-title {
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--text-secondary);
}
.preview-editor-input {
  width: 100%;
  min-height: 84px;
  box-sizing: border-box;
  resize: vertical;
  padding: var(--sp-1) var(--sp-2);
  font-family: var(--mono);
  font-size: var(--fs-xs);
  line-height: var(--lh-base);
  color: var(--text-primary);
  background: var(--bg-surface);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-sm);
  /* 板面正文可能被全局禁选：编辑框里必须可以选中与输入 */
  user-select: text;
  -webkit-user-select: text;
}
.preview-editor-input:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 1px; }
.preview-editor-error {
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--danger);
  overflow-wrap: anywhere;
}
.preview-editor-actions {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  justify-content: flex-end;
}
</style>
