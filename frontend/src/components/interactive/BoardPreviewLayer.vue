<!-- 待审批预览叠加层（子智能体 A 负责）：把 QIO 的虚线预览画在真实板面上。

  契约：docs/interactive-mode-contract.md §1.6 / §4.4。
  - 数据来自 store.intents 里仍在等待的意图（pending / needs_update / waiting_dependency /
    waiting_confirm / running）的 preview（结构与 BoardState 的 cards/groups/links 子集相同）；
  - 一律虚线：未获得的实际结果不能伪装成确定结论；元素上写「预览（未确定）」；
  - 链接方向只画用户写明的方向，不解释成因果 / 支持 / 执行顺序；
  - 整层 pointer-events: none：不参与拖动与选择命中，不挡正式卡片操作；
  - 收到 window 事件 qio:interactive:locate-preview 时由 BoardCanvas 高亮对应意图。
-->
<script setup lang="ts">
import { computed } from "vue";
import { useInteractiveStore } from "../../stores/interactive";
import { CARD_KIND_LABELS, groupFrame, type BoardRect } from "../../interactive/board";
import type { BoardCard, BoardGroup, Intent } from "../../interactive/types";

const props = defineProps<{
  intents: Intent[];
  locatedIntentId: string | null;
  width: number;
  height: number;
}>();

const store = useInteractiveStore();

interface PreviewCardBox {
  id: string;
  style: Record<string, string>;
  kindLabel: string;
  text: string;
}

interface PreviewGroupBox {
  id: string;
  style: Record<string, string>;
  name: string;
  ordered: boolean;
  sequence: string;
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
  cards: PreviewCardBox[];
  groups: PreviewGroupBox[];
  lines: PreviewLine[];
}

function cardText(card: BoardCard): string {
  const text = (card.content || String(card.meta?.name ?? card.meta?.title ?? "")).trim();
  if (!text) return "（无内容）";
  return text.length > 20 ? text.slice(0, 20) + "…" : text;
}

/** 预览组框：优先用预览自带的位置，没有就按成员卡片推出来。 */
function previewGroupFrame(group: BoardGroup, cards: BoardCard[]): BoardRect {
  const frame = groupFrame(group);
  if (frame.w > 2 && frame.h > 2) return frame;
  const members = cards.filter((card) => group.members.includes(card.id));
  if (!members.length) return frame;
  const left = Math.min(...members.map((card) => card.x)) - 16;
  const top = Math.min(...members.map((card) => card.y)) - 30;
  const right = Math.max(...members.map((card) => card.x + card.w)) + 16;
  const bottom = Math.max(...members.map((card) => card.y + card.h)) + 16;
  return { x: left, y: top, w: Math.max(1, right - left), h: Math.max(1, bottom - top) };
}

function boxStyle(rect: BoardRect): Record<string, string> {
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
    const cards = preview?.cards ?? [];
    const groups = preview?.groups ?? [];
    const links = preview?.links ?? [];
    const byId = new Map(cards.map((card) => [card.id, card]));

    const cardBoxes: PreviewCardBox[] = cards.map((card) => ({
      id: card.id,
      style: boxStyle({ x: card.x, y: card.y, w: card.w, h: card.h }),
      kindLabel: CARD_KIND_LABELS[card.kind],
      text: cardText(card),
    }));

    const groupBoxes: PreviewGroupBox[] = groups.map((group) => ({
      id: group.id,
      style: boxStyle(previewGroupFrame(group, cards)),
      name: group.name,
      ordered: Boolean(group.ordered),
      sequence: group.ordered
        ? group.members
            .map((id, index) => {
              const card = byId.get(id);
              return String(index + 1) + ". " + (card ? cardText(card) : "（成员）");
            })
            .join(" · ")
        : "普通组：摆放顺序不代表先后",
    }));

    const lines: PreviewLine[] = [];
    for (const link of links) {
      const src = byId.get(link.src);
      const dst = byId.get(link.dst);
      if (!src || !dst) continue;
      const x1 = src.x + src.w / 2;
      const y1 = src.y + src.h / 2;
      const x2 = dst.x + dst.w / 2;
      const y2 = dst.y + dst.h / 2;
      const meaning = link.meaning.trim() || (link.direction ? "有方向（含义待填写）" : "关联（含义待填写）");
      lines.push({
        id: link.id,
        x1,
        y1,
        x2,
        y2,
        direction: Boolean(link.direction),
        meaning,
        labelX: (x1 + x2) / 2,
        labelY: (y1 + y2) / 2,
        labelW: Math.max(64, meaning.length * 13 + 18),
      });
    }

    // 预览标注放在预览范围的上方，避免盖住内容
    const rects = [...cards.map((card) => ({ x: card.x, y: card.y, w: card.w, h: card.h })), ...groups.map((group) => previewGroupFrame(group, cards))];
    const top = rects.length ? Math.min(...rects.map((rect) => rect.y)) : 0;
    const left = rects.length ? Math.min(...rects.map((rect) => rect.x)) : 0;

    return {
      id: intent.id,
      title: intent.title || "待审批结果",
      statusLabel: store.statusLabel(intent.status),
      located: props.locatedIntentId === intent.id,
      badgeStyle: { left: left + "px", top: Math.max(0, top - 24) + "px" },
      cards: cardBoxes,
      groups: groupBoxes,
      lines,
    };
  }),
);
</script>

<template>
  <div class="preview-layer" aria-label="待审批预览（虚线，未确定）">
    <div
      v-for="item in rendered"
      :key="item.id"
      class="preview-intent"
      :class="{ located: item.located }"
      data-im="preview"
      :data-intent-id="item.id"
    >
      <p class="preview-badge" :style="item.badgeStyle">
        预览（未确定）· {{ item.title }} · {{ item.statusLabel }}
      </p>

      <div
        v-for="group in item.groups"
        :key="group.id"
        class="preview-group"
        :style="group.style"
        data-im="preview"
        :data-intent-id="item.id"
      >
        <span class="preview-group-head">
          {{ group.name }}<span v-if="group.ordered"> · 有序</span>
        </span>
        <span class="preview-group-note">{{ group.sequence }}</span>
      </div>

      <div
        v-for="card in item.cards"
        :key="card.id"
        class="preview-card"
        :style="card.style"
        data-im="preview"
        :data-intent-id="item.id"
      >
        <span class="preview-card-kind mono">{{ card.kindLabel }}</span>
        <span class="preview-card-text">{{ card.text }}</span>
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
.preview-intent.located .preview-group { border-color: var(--warning); }
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
.preview-card-kind { color: var(--link); font-size: var(--fs-xs); }
.preview-card-text { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.preview-group {
  position: absolute;
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 2px var(--sp-2);
  border: 1px dashed var(--link);
  border-radius: var(--r-lg);
  background: transparent;
  opacity: 0.9;
  color: var(--text-secondary);
  font-size: var(--fs-xs);
  overflow: hidden;
}
.preview-group-head { color: var(--link); font-family: var(--serif); font-size: var(--fs-sm); }
.preview-group-note { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.preview-links { position: absolute; left: 0; top: 0; overflow: visible; }
.preview-arrow { fill: var(--link); }
.preview-line { stroke: var(--link); stroke-width: 1.5; stroke-dasharray: 6 4; }
.preview-line-label-bg { fill: var(--bg-elevated); stroke: var(--border-subtle); }
.preview-line-label { fill: var(--text-secondary); font-size: var(--fs-xs); font-family: var(--sans); }
</style>
