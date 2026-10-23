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
import { computed } from "vue";
import type { BoardCard, BoardGroup, BoardLink } from "../../interactive/types";
import {
  buildDisplayLinks,
  edgeAnchor,
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
  },
);

const emit = defineEmits<{
  (e: "select-link", linkId: string): void;
}>();

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
  const start = edgeAnchor(from.rect, startAim);
  const end = target
    ? edgeAnchor(target.rect, rectCenter(from.rect))
    : props.draftPoint;
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
</script>

<template>
  <svg class="link-layer" :width="width" :height="height" aria-label="关系链接层">
    <defs>
      <marker
        id="im-link-arrow"
        viewBox="0 0 10 10"
        refX="9"
        refY="5"
        markerWidth="7"
        markerHeight="7"
        orient="auto-start-reverse"
      >
        <path class="arrow" d="M 0 0 L 10 5 L 0 10 z" />
      </marker>
    </defs>
    <g
      v-for="item in drawn"
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
      <line
        class="hit"
        :x1="item.x1"
        :y1="item.y1"
        :x2="item.x2"
        :y2="item.y2"
        @click="emit('select-link', item.id)"
      />
      <line
        class="line"
        :class="{ active: item.active }"
        :x1="item.x1"
        :y1="item.y1"
        :x2="item.x2"
        :y2="item.y2"
        :marker-end="item.direction ? 'url(#im-link-arrow)' : undefined"
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
      <rect
        class="label-bg"
        :class="{ active: item.active }"
        :x="item.labelX - item.labelW / 2"
        :y="item.labelY - 10"
        :width="item.labelW"
        height="20"
        rx="6"
        @click="emit('select-link', item.id)"
      />
      <text
        class="label"
        :x="item.labelX"
        :y="item.labelY + 4"
        text-anchor="middle"
        @click="emit('select-link', item.id)"
      >
        {{ item.meaning }}
      </text>
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
.arrow { fill: var(--link); }
.hit { stroke: transparent; stroke-width: 16; pointer-events: stroke; cursor: pointer; }
.line { stroke: var(--link); stroke-width: 1.5; stroke-dasharray: none; }
.line.active { stroke: var(--accent); stroke-width: 2.5; }
/* 组收起时的汇聚点：与线同色，只是标记「真实端点在组里」 */
.aggregated-dot { fill: var(--link); pointer-events: none; }
.label-bg {
  fill: var(--bg-elevated);
  stroke: var(--border-subtle);
  pointer-events: auto;
  cursor: pointer;
}
.label-bg.active { stroke: var(--accent); }
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
.draft-text { fill: var(--text-secondary); font-size: var(--fs-xs); font-family: var(--sans); }
</style>
