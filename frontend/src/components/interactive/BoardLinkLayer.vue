<!-- 关系链接层（子智能体 B 负责）：卡片 / 组之间的连线、方向箭头、用户写明的含义，以及拖线预览。

  契约：docs/interactive-page-v2-contract.md §5.3（组端点）/ docs/interactive-mode-contract.md §1.3。
  - 端点身份：卡片仍为裸 card id，组为 `group:<groupId>`；旧数据不需要迁移。
  - 方向只表示用户写明的方向；界面文案不得把它说成因果 / 支持 / 执行顺序。
  - 连线锚在**最终显示矩形的边缘**（拖动 / 缩放 / 收起后的显示坐标由调用方传入），
    所以显示与命中一致，不再挂在卡片中心。
  - 组收起时：成员对外连线视觉汇聚到组边缘（真实 src/dst 身份不变），组内成员线不画；
    展开后恢复。组端点线只有一条，它表示**当前成员集合**，不按成员复制、不列成员名。
  - 所有坐标都是**板面坐标**：本层放在被平移 / 缩放的内容层里，所以缩放平移后仍然准确。
-->
<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import type { BoardCard, BoardGroup, BoardLink, LinkCurve } from "../../interactive/types";
import {
  clampCurveOffset,
  curveFromPointerDrag,
  curveGeometry,
  curvePath,
  normalizeCurveScale,
} from "../../interactive/linkCurve";
import {
  buildDisplayLinks,
  edgeAnchor,
  offsetCardAnchor,
  rectCenter,
  resolveEndpoint,
  type BoardRect,
  type ResolvedEndpoint,
} from "../../interactive/linkEndpoints";

const props = withDefaults(
  defineProps<{
    links: BoardLink[];
    cards: BoardCard[];
    activeLinkId: string | null;
    width: number;
    height: number;
    /**
     * 板面 → 屏幕的缩放倍数（= BoardCanvas 的 scaleSafe；默认 1 = 不缩放）。
     *
     * 本层画在 `.board-surface` 的 transform: scale() 里，屏幕位移 = 板面位移 × scale，
     * 所以拖动换算固定为「板面位移 = 屏幕位移 / scale」（非正 / 非有限按 1）。
     * 不按缩放倍数错误放大 / 缩小曲线调整：50% / 100% / 200% 下同样跟手。
     */
    scale?: number;
    /** 正在从哪个端点拖线（卡片裸 id 或 group:<id>；null = 没有在拖） */
    draftFromId: string | null;
    /** 指针当前所在的板面坐标 */
    draftPoint: { x: number; y: number } | null;
    /** 有效目标端点（卡片裸 id 或 group:<id>；null = 没有有效目标） */
    draftTargetId: string | null;
    /** 组端点解析所需（Phase 2）。缺省 = 没有组端点（旧行为不变）。 */
    groups?: BoardGroup[];
    /** 已收起的组：成员的对外连线汇聚到组边缘（真实端点身份不变）。 */
    foldedGroupIds?: string[];
    /** 参与范围（Phase 2 §5.1）。缺省 = 不区分（用户视野）。 */
    includedCardIds?: string[] | null;
    /** true = 只画可交给 QIO 的线（组端点需至少一名获准成员）。 */
    onlyInRange?: boolean;
  }>(),
  {
    groups: () => [],
    foldedGroupIds: () => [],
    includedCardIds: null,
    onlyInRange: false,
    scale: 1,
  },
);

const emit = defineEmits<{
  (e: "select-link", linkId: string, clientX?: number, clientY?: number): void;
  (e: "edit-link", linkId: string, clientX?: number, clientY?: number): void;
  (e: "delete-link", linkId: string): void;
  (e: "curve-change", linkId: string, curve: LinkCurve): void;
}>();

const rootEl = ref<SVGSVGElement | null>(null);
const previewCurves = ref<Record<string, LinkCurve>>({});

/** 拖动结束后抑制 click / dblclick 的窗口（ms）：浏览器在 pointerup 后仍会派发 click。 */
const CLICK_SUPPRESS_MS = 320;

/**
 * 当前手势：只认**这一个** pointerId。
 *
 * - 其他指针的 move / up / cancel 一律不参与（不能改曲线，也不能结束本次拖动）；
 * - 拖动期间只写 previewCurves（临时预览），不 emit、不碰传入的 links；
 * - 正常松手才 emit 一次 curve-change；取消（Esc / pointercancel / 失焦 / 卸载）只恢复。
 */
interface ActiveDrag {
  pointerId: number | null;
  linkId: string;
  startClientX: number;
  startClientY: number;
  origin: LinkCurve;
  moved: boolean;
  lastCurve: LinkCurve | null;
}

const dragging = ref<ActiveDrag | null>(null);
let suppressClickUntil = 0;
let globalListenersAttached = false;

/** scale 的安全值：非正 / 非有限按 1（与 BoardCanvas 的 scaleSafe 同一口径）。 */
const dragScale = computed(() => normalizeCurveScale(props.scale));

function curveOf(item: { id: string; curve: LinkCurve }): LinkCurve {
  return previewCurves.value[item.id] ?? item.curve ?? { x: 0, y: 0 };
}

function pathOf(item: { id: string; x1: number; y1: number; x2: number; y2: number; curve: LinkCurve }): string {
  return curvePath({
    x1: item.x1,
    y1: item.y1,
    x2: item.x2,
    y2: item.y2,
    curve: curveOf(item),
  });
}

function labelPoint(item: { id: string; x1: number; y1: number; x2: number; y2: number; curve: LinkCurve }): { x: number; y: number } {
  const geometry = curveGeometry({
    x1: item.x1,
    y1: item.y1,
    x2: item.x2,
    y2: item.y2,
    curve: curveOf(item),
  });
  return { x: geometry.labelX, y: geometry.labelY };
}

/** 清掉某条关系的临时预览：显示回到调用方数据（起始曲线 / 外部回写值）。 */
function clearPreview(linkId: string): void {
  if (!(linkId in previewCurves.value)) return;
  const next = { ...previewCurves.value };
  delete next[linkId];
  previewCurves.value = next;
}

/** 指针身份：鼠标 / 触摸事件给 pointerId；合成事件没有就退化为 null（同样是稳定身份）。 */
function pointerIdentityOf(event: PointerEvent): number | null {
  const value = Number((event as PointerEvent).pointerId);
  return Number.isFinite(value) ? value : null;
}

/** 事件是否属于当前这次手势的指针身份：其他指针一律不参与。 */
function samePointer(event: PointerEvent, active: ActiveDrag): boolean {
  return pointerIdentityOf(event) === active.pointerId;
}

/** 指针 → 临时预览（唯一换算入口：板面位移 = 屏幕位移 / scale）。 */
function applyPointer(active: ActiveDrag, event: PointerEvent): void {
  const result = curveFromPointerDrag({
    origin: active.origin,
    startClientX: active.startClientX,
    startClientY: active.startClientY,
    clientX: event.clientX,
    clientY: event.clientY,
    scale: dragScale.value,
  });
  // 还没超过抖动阈值：仍按单击处理，不产生预览
  if (!result.moved && !active.moved) return;
  active.moved = true;
  active.lastCurve = result.curve;
  previewCurves.value = { ...previewCurves.value, [active.linkId]: result.curve };
}

/** 取消本次拖动：恢复起始曲线、不 emit，并移除全局监听。取消后迟到的 pointerup 没有提交通道。 */
function cancelDrag(): void {
  const active = dragging.value;
  dragging.value = null;
  detachGlobalListeners();
  if (active) clearPreview(active.linkId);
}

function attachGlobalListeners(): void {
  if (globalListenersAttached) return;
  globalListenersAttached = true;
  // capture：其他元素的 stopPropagation 不会让我们漏掉 move / up / cancel
  window.addEventListener("pointermove", onWindowPointerMove, true);
  window.addEventListener("pointerup", onWindowPointerUp, true);
  window.addEventListener("pointercancel", onWindowPointerCancel, true);
  window.addEventListener("keydown", onWindowKeyDown, true);
  window.addEventListener("blur", onWindowBlur, true);
}

function detachGlobalListeners(): void {
  if (!globalListenersAttached) return;
  globalListenersAttached = false;
  window.removeEventListener("pointermove", onWindowPointerMove, true);
  window.removeEventListener("pointerup", onWindowPointerUp, true);
  window.removeEventListener("pointercancel", onWindowPointerCancel, true);
  window.removeEventListener("keydown", onWindowKeyDown, true);
  window.removeEventListener("blur", onWindowBlur, true);
}

function onWindowPointerMove(event: PointerEvent): void {
  const active = dragging.value;
  if (!active || !samePointer(event, active)) return;
  applyPointer(active, event);
}

function onWindowPointerUp(event: PointerEvent): void {
  const active = dragging.value;
  if (!active || !samePointer(event, active)) return;
  // 松手坐标也折算一次：有些输入设备只在 up 里给出最终位置
  const result = curveFromPointerDrag({
    origin: active.origin,
    startClientX: active.startClientX,
    startClientY: active.startClientY,
    clientX: event.clientX,
    clientY: event.clientY,
    scale: dragScale.value,
  });
  const moved = active.moved || result.moved;
  const finalCurve = moved ? result.curve : active.lastCurve;
  const linkId = active.linkId;
  dragging.value = null;
  detachGlobalListeners();
  clearPreview(linkId);
  // 没超过阈值 = 单击：交给 click 处理选中，不提交曲线
  if (!moved) return;
  // 拖动结束：抑制紧随其后的 click / dblclick，避免误触选中 / 编辑 / 删除
  suppressClickUntil = Date.now() + CLICK_SUPPRESS_MS;
  emit("curve-change", linkId, finalCurve ?? active.origin);
}

function onWindowPointerCancel(event: PointerEvent): void {
  const active = dragging.value;
  if (!active || !samePointer(event, active)) return;
  cancelDrag();
}

function onWindowBlur(): void {
  if (dragging.value) cancelDrag();
}

function onWindowKeyDown(event: KeyboardEvent): void {
  if (!dragging.value) return;
  if (event.key !== "Escape" && event.key !== "Esc") return;
  event.preventDefault();
  event.stopPropagation();
  cancelDrag();
}

/**
 * 当前手势对应关系的「外部真相」指纹（端点 + 已提交曲线）。
 *
 * 拖动期间父层回写（撤销 / 重做 / 保存恢复 / 删除关系 / 改端点）时外部真相优先：
 * 取消本次拖动，避免迟到的松手用旧起点覆盖新数据。
 */
const activeDragLinkKey = computed<string | null>(() => {
  const active = dragging.value;
  if (!active) return null;
  const link = props.links.find((item) => item.id === active.linkId && !item.deleted);
  if (!link) return "missing";
  const x = Number.isFinite(Number(link.curve?.x)) ? Number(link.curve?.x) : 0;
  const y = Number.isFinite(Number(link.curve?.y)) ? Number(link.curve?.y) : 0;
  return [link.src, link.dst, x, y].join("\u0000");
});

watch(activeDragLinkKey, (current, previous) => {
  if (previous === null || current === null) return;
  if (current === previous) return;
  cancelDrag();
});

function startLabelDrag(linkId: string, curve: LinkCurve, event: PointerEvent): void {
  if (event.button !== 0 && event.pointerType === "mouse") return;
  // 一次只允许一个手势：拖动中的其他指针按下不能抢走手势 / 重置起点
  if (dragging.value) return;
  event.preventDefault();
  event.stopPropagation();
  dragging.value = {
    pointerId: pointerIdentityOf(event),
    linkId,
    startClientX: Number.isFinite(event.clientX) ? event.clientX : 0,
    startClientY: Number.isFinite(event.clientY) ? event.clientY : 0,
    origin: {
      x: Number.isFinite(Number(curve.x)) ? Number(curve.x) : 0,
      y: Number.isFinite(Number(curve.y)) ? Number(curve.y) : 0,
    },
    moved: false,
    lastCurve: null,
  };
  attachGlobalListeners();
}

function isDeleteTarget(target: EventTarget | null): boolean {
  return Boolean((target as Element | null)?.closest?.("[data-link-delete]"));
}

function onLabelClick(linkId: string, event?: MouseEvent): void {
  if (Date.now() < suppressClickUntil) return;
  emit("select-link", linkId, event?.clientX, event?.clientY);
}

const LABEL_DELETE_SPACE = 24;
const LABEL_DELETE_RADIUS = 8;

/** 胶囊整体 = 原说明宽度 + 右侧删除按钮；文字只占左侧说明区。 */
function labelBox(item: { id: string; x1: number; y1: number; x2: number; y2: number; curve: LinkCurve; labelW: number }) {
  const point = labelPoint(item);
  const width = item.labelW + LABEL_DELETE_SPACE;
  const left = point.x - width / 2;
  return {
    point,
    width,
    left,
    textX: left + item.labelW / 2,
    buttonX: left + width - LABEL_DELETE_RADIUS - 4,
  };
}

function labelOf(target: EventTarget | null): HTMLElement | null {
  const element = target instanceof Element ? target : null;
  return (element?.closest("[data-link-label]") as HTMLElement | null) ?? null;
}

function handlePointerDown(event: PointerEvent): void {
  if (isDeleteTarget(event.target)) return;
  const label = labelOf(event.target);
  if (!label) return;
  const linkId = label.dataset.linkLabel ?? "";
  const item = drawn.value.find((link) => link.id === linkId);
  if (!item) return;
  startLabelDrag(item.id, curveOf(item), event);
}

function handleClick(event: MouseEvent): void {
  if (isDeleteTarget(event.target)) return;
  const label = labelOf(event.target);
  if (!label) return;
  const linkId = label.dataset.linkLabel ?? "";
  if (!linkId) return;
  event.stopPropagation();
  onLabelClick(linkId, event);
}

function handleDoubleClick(event: MouseEvent): void {
  // 拖动结束后的 click / dblclick 不是编辑意图（浏览器仍会派发）
  if (Date.now() < suppressClickUntil) return;
  if (isDeleteTarget(event.target)) return;
  const label = labelOf(event.target);
  if (!label) return;
  const linkId = label.dataset.linkLabel ?? "";
  if (!linkId) return;
  event.preventDefault();
  event.stopPropagation();
  emit("edit-link", linkId, event.clientX, event.clientY);
}

onMounted(() => {
  rootEl.value?.addEventListener("pointerdown", handlePointerDown, true);
  rootEl.value?.addEventListener("click", handleClick, true);
  rootEl.value?.addEventListener("dblclick", handleDoubleClick, true);
});

onBeforeUnmount(() => {
  // 组件卸载即取消本次拖动：不提交、清预览、移除全部全局监听
  cancelDrag();
  rootEl.value?.removeEventListener("pointerdown", handlePointerDown, true);
  rootEl.value?.removeEventListener("click", handleClick, true);
  rootEl.value?.removeEventListener("dblclick", handleDoubleClick, true);
});

/** 端点解析上下文：显示层用真实板面数据；不在这里改任何状态。 */
const endpointContext = computed(() => ({ cards: props.cards, groups: props.groups }));

/** 已画出的线：组收起汇聚 / 可见范围 / 非法端点跳过都在纯函数里决定。 */
const drawn = computed(() =>
  buildDisplayLinks({
    links: props.links,
    cards: props.cards,
    groups: props.groups,
    foldedGroupIds: props.foldedGroupIds,
    activeLinkId: props.activeLinkId,
    includedCardIds: props.includedCardIds,
    onlyInRange: props.onlyInRange,
  }).links,
);

interface DraftDisplay {
  fromId: string;
  fromKind: ResolvedEndpoint["kind"];
  x1: number;
  y1: number;
  x2: number;
  y2: number;
  valid: boolean;
  targetId: string | null;
  targetKind: ResolvedEndpoint["kind"] | null;
  targetRect: BoardRect | null;
}

function expandedRect(rect: BoardRect, pad: number): BoardRect {
  return { x: rect.x - pad, y: rect.y - pad, w: rect.w + pad * 2, h: rect.h + pad * 2 };
}

/**
 * 待建连线：从源端点边缘指向指针（或有效目标边缘）。
 * 目标可以是卡片或组 —— 组端点同样校验「存在 / 未删 / 至少一名存活成员」。
 * 松手到无效位置或取消时不建链、不保存半条链接。
 */
const draft = computed<DraftDisplay | null>(() => {
  if (!props.draftFromId) return null;
  const from = resolveEndpoint(props.draftFromId, endpointContext.value);
  if (!from) return null;
  const target = props.draftTargetId
    ? resolveEndpoint(props.draftTargetId, endpointContext.value)
    : null;
  const startAim = target
    ? rectCenter(target.rect)
    : (props.draftPoint ?? rectCenter(from.rect));
  const startEdge = edgeAnchor(from.rect, startAim);
  const start = offsetCardAnchor(from, startEdge, startAim);
  const endEdge = target
    ? edgeAnchor(target.rect, rectCenter(from.rect))
    : props.draftPoint;
  const end = target && endEdge ? offsetCardAnchor(target, endEdge, rectCenter(from.rect)) : endEdge;
  if (!end) return null;
  const valid = Boolean(target) && target!.value !== from.value;
  return {
    fromId: from.value,
    fromKind: from.kind,
    x1: start.x,
    y1: start.y,
    x2: end.x,
    y2: end.y,
    valid,
    targetId: target?.value ?? null,
    targetKind: target?.kind ?? null,
    targetRect: target ? expandedRect(target.rect, target.kind === "group" ? 2 : 4) : null,
  };
});

/**
 * 目标浮标：明确写出「连接到卡片」还是「连接到组」。
 *
 * 目标判定本身由 BoardCanvas 先卡片、再组框（更具体的优先）；
 * 这里只把那个结果翻译成用户能读的一句话，不重新判一次目标、不自动升级。
 */
const targetBadge = computed(() => {
  const current = draft.value;
  if (!current || !current.valid || !current.targetKind) return null;
  const text = current.targetKind === "group" ? "连接到组" : "连接到卡片";
  const w = text.length * 12 + 18;
  const x = Math.max(6, Math.min(current.x2 + 12, props.width - w - 6));
  const y = Math.max(6, current.y2 - 32);
  return { text, w, x, y, kind: current.targetKind };
});
</script>

<template>
  <svg ref="rootEl" class="link-layer" :width="width" :height="height" aria-label="关系链接层">
    <defs>
      <linearGradient
        v-for="(item, index) in drawn"
        :id="'im-link-grad-' + index"
        :key="'grad-' + item.id"
        gradientUnits="userSpaceOnUse"
        :x1="item.x1"
        :y1="item.y1"
        :x2="item.x2"
        :y2="item.y2"
      >
        <stop offset="0" stop-color="var(--link)" />
        <stop offset="1" stop-color="var(--accent)" />
      </linearGradient>
    </defs>
    <g
      v-for="(item, index) in drawn"
      :key="item.id"
      :data-link-id="item.id"
      :data-src="item.src"
      :data-dst="item.dst"
      :data-src-kind="item.srcKind"
      :data-dst-kind="item.dstKind"
      :data-display-src="item.displaySrcId"
      :data-display-dst="item.displayDstId"
      :data-aggregated="item.aggregatedSrc || item.aggregatedDst ? 'true' : undefined"
    >
      <!-- 只给可访问名称：含义 + 「组名 / 卡片」，绝不列成员名或正文 -->
      <title>{{ item.ariaLabel }}</title>
      <path
        class="hit"
        :d="pathOf(item)"
        @click="emit('select-link', item.id, $event.clientX, $event.clientY)"
      />
      <path
        class="line"
        :class="{ active: item.active }"
        :d="pathOf(item)"
        :stroke="'url(#im-link-grad-' + index + ')'"
      />
      <!-- 汇聚点：组收起时真实端点在这里汇到组边缘（身份仍记在 data-src / data-dst） -->
      <circle
        v-if="item.aggregatedSrc"
        class="aggregated-dot"
        :cx="item.x1"
        :cy="item.y1"
        r="3.5"
      />
      <circle
        v-if="item.aggregatedDst"
        class="aggregated-dot"
        :cx="item.x2"
        :cy="item.y2"
        r="3.5"
      />
      <g
        class="link-label"
        :class="{ active: item.active, dragging: dragging?.linkId === item.id }"
        :data-link-label="item.id"
      >
        <rect
          class="label-bg"
          :class="{ active: item.active }"
          :x="labelBox(item).left"
          :y="labelBox(item).point.y - 10"
          :width="labelBox(item).width"
          height="20"
          rx="10"
        />
        <text
          class="label"
          :x="labelBox(item).textX"
          :y="labelBox(item).point.y + 4"
          text-anchor="middle"
        >
          {{ item.meaning }}
        </text>
        <g
          class="label-delete"
          data-link-delete
          role="button"
          :aria-label="'删除关系：' + item.meaning"
          @pointerdown.stop
          @click.stop="emit('delete-link', item.id)"
          @dblclick.stop
        >
          <circle
            class="label-delete-bg"
            :cx="labelBox(item).buttonX"
            :cy="labelBox(item).point.y"
            :r="8"
          />
          <path
            class="label-delete-x"
            :d="'M ' + (labelBox(item).buttonX - 3) + ' ' + (labelBox(item).point.y - 3) + ' L ' + (labelBox(item).buttonX + 3) + ' ' + (labelBox(item).point.y + 3) + ' M ' + (labelBox(item).buttonX + 3) + ' ' + (labelBox(item).point.y - 3) + ' L ' + (labelBox(item).buttonX - 3) + ' ' + (labelBox(item).point.y + 3)"
          />
        </g>
      </g>
    </g>

    <!-- 待建连线（虚线）：松手前只是预览，不是正式关系 -->
    <g
      v-if="draft"
      class="draft"
      data-im="link-draft"
      :data-from="draft.fromId"
      :data-target="draft.targetId ?? ''"
      :data-target-kind="draft.targetKind ?? ''"
    >
      <line
        class="draft-line"
        :class="{ valid: draft.valid }"
        :x1="draft.x1"
        :y1="draft.y1"
        :x2="draft.x2"
        :y2="draft.y2"
      />
      <circle class="draft-dot" :cx="draft.x1" :cy="draft.y1" r="4" />
      <rect
        v-if="draft.targetRect"
        class="draft-target"
        :x="draft.targetRect.x"
        :y="draft.targetRect.y"
        :width="draft.targetRect.w"
        :height="draft.targetRect.h"
        rx="8"
      />
      <!-- 浮标：目标到底是卡片还是组，不用用户猜 -->
      <g
        v-if="targetBadge"
        class="link-target-badge"
        data-im="link-target-badge"
        :data-target-kind="targetBadge.kind"
        :data-target="draft.targetId ?? ''"
        :transform="'translate(' + targetBadge.x + ' ' + targetBadge.y + ')'"
      >
        <rect :width="targetBadge.w" height="22" rx="6" />
        <text x="9" y="15">{{ targetBadge.text }}</text>
      </g>
      <text class="draft-text" :x="draft.x2 + 10" :y="draft.y2 - 8">
        {{ draft.valid ? "松开后建立关系（方向与含义由你写明）" : "拖到卡片或一个组上才能建立关系" }}
      </text>
    </g>
  </svg>
</template>

<style scoped>
.link-layer {
  position: absolute;
  left: 0;
  top: 0;
  pointer-events: none; /* 只有线与标签接管指针，别挡住卡片 */
  z-index: 3;
  overflow: visible;
}
.hit { fill: none; stroke: transparent; stroke-width: 16; pointer-events: stroke; cursor: pointer; }
.line { fill: none; stroke: var(--link); stroke-width: 1.5; stroke-dasharray: none; }
.line.active { stroke: var(--accent); stroke-width: 2.5; }
/* 组收起时的汇聚点：与线同色，只是标记「真实端点在组里」 */
.aggregated-dot { fill: var(--link); pointer-events: none; }
.label-bg {
  fill: var(--bg-elevated);
  stroke: var(--border-subtle);
  pointer-events: auto;
  cursor: grab;
}
.label-bg.active { stroke: var(--accent); }
.link-label.dragging .label-bg { cursor: grabbing; }
.label-delete { pointer-events: auto; cursor: pointer; }
.label-delete-bg {
  fill: var(--bg-surface);
  stroke: var(--border-subtle);
  transition: fill var(--dur-fast) var(--ease-1), stroke var(--dur-fast) var(--ease-1);
}
.label-delete-x {
  fill: none;
  stroke: var(--text-muted);
  stroke-width: 1.4;
  stroke-linecap: round;
  transition: stroke var(--dur-fast) var(--ease-1);
}
.label-delete:hover .label-delete-bg { fill: var(--danger-soft); stroke: var(--danger); }
.label-delete:hover .label-delete-x { stroke: var(--danger); }
.label {
  fill: var(--text-secondary);
  font-size: var(--fs-xs);
  font-family: var(--sans);
  pointer-events: auto;
  cursor: pointer;
}
/* 待建连线：虚线 + 明确的有效 / 无效两种说法（不只靠颜色） */
.draft { pointer-events: none; }
.draft-line { stroke: var(--text-muted); stroke-width: 1.5; stroke-dasharray: 6 5; }
.draft-line.valid { stroke: var(--accent); stroke-width: 2; }
.draft-dot { fill: var(--accent); }
.draft-target { fill: none; stroke: var(--accent); stroke-width: 1.5; stroke-dasharray: 5 4; }
/* 目标浮标：深底浅字，跟着端点走；只说明目标类型，不代替用户写含义。 */
.link-target-badge { pointer-events: none; }
.link-target-badge rect { fill: var(--text-strong); }
.link-target-badge text {
  fill: var(--bg-base);
  font-size: var(--fs-xs);
  font-family: var(--sans);
}
.draft-text { fill: var(--text-secondary); font-size: var(--fs-xs); font-family: var(--sans); }
</style>
