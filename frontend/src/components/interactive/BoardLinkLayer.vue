<!-- 关系链接层（子智能体 B 负责）：卡片之间的连线、方向箭头、用户写明的含义，以及拖线预览。

  契约：docs/interactive-mode-contract.md §1.3 / §8.2。
  - 方向只表示用户写明的方向；界面文案不得把它说成因果 / 支持 / 执行顺序；
  - 从连接点拖线时画**待建连线**（虚线）与有效目标提示（data-im="link-draft"），
    松手到无效位置或取消时不建链、不保存半条链接；
  - 所有坐标都是**板面坐标**：本层放在被平移 / 缩放的内容层里，所以缩放平移后仍然准确。
-->
<script setup lang="ts">
import { computed } from "vue";
import { cardRect } from "../../interactive/board";
import type { BoardCard, BoardLink } from "../../interactive/types";

const props = defineProps<{
  links: BoardLink[];
  cards: BoardCard[];
  activeLinkId: string | null;
  width: number;
  height: number;
  /** 正在从哪张卡片的连接点拖线（null = 没有在拖） */
  draftFromId: string | null;
  /** 指针当前所在的板面坐标 */
  draftPoint: { x: number; y: number } | null;
  /** 有效目标卡片 id（松手会建链的那张） */
  draftTargetId: string | null;
}>();

const emit = defineEmits<{
  (e: "select-link", linkId: string): void;
}>();

interface DrawnLink {
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
  active: boolean;
}

function centerOf(card: BoardCard): { x: number; y: number } {
  const rect = cardRect(card);
  return { x: rect.x + rect.w / 2, y: rect.y + rect.h / 2 };
}

const drawn = computed<DrawnLink[]>(() => {
  const byId = new Map(props.cards.map((card) => [card.id, card]));
  const result: DrawnLink[] = [];
  for (const link of props.links) {
    if (link.deleted) continue;
    const src = byId.get(link.src);
    const dst = byId.get(link.dst);
    if (!src || !dst) continue;
    const a = cardRect(src);
    const b = cardRect(dst);
    const x1 = a.x + a.w / 2;
    const y1 = a.y + a.h / 2;
    const x2 = b.x + b.w / 2;
    const y2 = b.y + b.h / 2;
    const meaning = link.meaning.trim() || (link.direction ? "有方向（含义待填写）" : "关联（含义待填写）");
    result.push({
      id: link.id,
      x1,
      y1,
      x2,
      y2,
      direction: link.direction,
      meaning,
      labelX: (x1 + x2) / 2,
      labelY: (y1 + y2) / 2,
      labelW: Math.max(64, meaning.length * 13 + 18),
      active: props.activeLinkId === link.id,
    });
  }
  return result;
});

/** 待建连线：从源卡片中心指向指针（或有效目标中心）。 */
const draft = computed(() => {
  const from = props.cards.find((card) => card.id === props.draftFromId);
  if (!from) return null;
  const start = centerOf(from);
  const target = props.draftTargetId ? props.cards.find((card) => card.id === props.draftTargetId) : null;
  const end = target ? centerOf(target) : props.draftPoint;
  if (!end) return null;
  return {
    fromId: from.id,
    x1: start.x,
    y1: start.y,
    x2: end.x,
    y2: end.y,
    valid: Boolean(target),
    targetId: target ? target.id : null,
  };
});

/** 有效目标的高亮框（虚线矩形，不参与命中）。 */
const targetRect = computed(() => {
  if (!props.draftTargetId) return null;
  const card = props.cards.find((item) => item.id === props.draftTargetId);
  if (!card) return null;
  const rect = cardRect(card);
  return { x: rect.x - 4, y: rect.y - 4, w: rect.w + 8, h: rect.h + 8 };
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
    <g v-for="item in drawn" :key="item.id">
      <line class="hit" :x1="item.x1" :y1="item.y1" :x2="item.x2" :y2="item.y2" @click="emit('select-link', item.id)" />
      <line
        class="line"
        :class="{ active: item.active }"
        :x1="item.x1"
        :y1="item.y1"
        :x2="item.x2"
        :y2="item.y2"
        :marker-end="item.direction ? 'url(#im-link-arrow)' : undefined"
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
      <text class="label" :x="item.labelX" :y="item.labelY + 4" text-anchor="middle" @click="emit('select-link', item.id)">
        {{ item.meaning }}
      </text>
    </g>

    <!-- 待建连线（虚线）：松手前只是预览，不是正式关系 -->
    <g v-if="draft" class="draft" data-im="link-draft" :data-from="draft.fromId" :data-target="draft.targetId ?? ''">
      <line class="draft-line" :class="{ valid: draft.valid }" :x1="draft.x1" :y1="draft.y1" :x2="draft.x2" :y2="draft.y2" />
      <circle class="draft-dot" :cx="draft.x1" :cy="draft.y1" r="4" />
      <rect
        v-if="targetRect"
        class="draft-target"
        :x="targetRect.x"
        :y="targetRect.y"
        :width="targetRect.w"
        :height="targetRect.h"
        rx="8"
      />
      <text class="draft-text" :x="draft.x2 + 10" :y="draft.y2 - 8">
        {{ draft.valid ? "松开后建立关系（方向与含义由你写明）" : "拖到另一张卡片上才能建立关系" }}
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
