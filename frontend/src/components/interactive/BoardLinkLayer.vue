<!-- 关系链接层（子智能体 A 负责）：卡片之间的连线、方向箭头与用户写明的含义。

  契约：docs/interactive-mode-contract.md §1.3 / §4.4。
  方向只表示用户写明的方向；界面文案不得把它说成因果 / 支持 / 执行顺序。
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
</style>
