<!--
  卡片尺寸把手层 + 关系连接点入口（IRL-C · 任务书 §3.1 / §3.2；0.2.5 §三 / §四）。

  为什么是单独一层、而且放在**缩放之外**：
  - 卡片画在 .board-surface 里，而那一层带 transform: scale()。把手如果也画在里面，
    热区会跟着缩放一起放大缩小 —— scale 0.25 时 10px 的边只剩 2.5px，用户根本抓不住。
    所以这一层放在**未缩放**的滚动内容层里：位置 = 显示槽位 × 缩放，
    10px 边 / 16px 角在任何缩放下都是真实像素，与卡片边缘仍然严格对齐。
  - 卡片、组框、关系线、把手共用同一份显示坐标（BoardCanvas 的 displayRect）：
    展开让位、拖动、尺寸预览之后四者不会各画各的。
  - 整层 pointer-events: none，只有把手热区与连接点接管指针：只读卡片正文、组框、连线都不被挡住。
  - 本层不写任何状态：按下 / −＋ / 连接点只把事件发出去，预览与提交由 BoardCanvas 决定
    （尺寸只是查看与布局，不产生表达、不调用模型、不申请审批）。

  0.2.5 回归修复：**连接点也从这一层走**（原来画在卡片内部）。
  - 根因：连接点在 .board-surface 的缩放层叠上下文里，四边中点正好落在本层 10px 的尺寸热区
    下面 —— 真实鼠标按下先被尺寸调整接走，连接点永远拖不出关系。
  - 现在为「恰好选中一张、未折叠」的卡片在本层里单独排一层连接点宿主（.connect-host，
    z-index 比 .resize-host 高），四边中点各有独立命中区：**屏幕像素 18×18**（不随画布缩放），
    视觉圆点仍是 12px、颜色仍走既有令牌。
  - 命中规则因此是纯几何的：连接点 → 拖线；同层其余边 / 角（边 10px / 角 16px，一个都没缩）→
    尺寸调整；卡片正文 → 卡片自己的选择 / 拖动。两种操作同时可用，不需要切换模式。
  - **2026-10-15（D1 修复）**：连接点宿主 z-index 更高，而它的 18×18 命中区从前**骑在边的
    10px 热区上**（top: 0 → 盖住 y 0..10）—— 于是「抓边正中」被连接点抢走，按下去是拉线、
    拉不动尺寸（偏离中点才行）。现在把命中区整体**移到边热区的内侧**：
    top/bottom 各让开 10px、left/right 各让开 10px（= 热区厚度），
    四边中点 ⊆ 尺寸把手热区 → 稳定开始拉伸；18×18 命中区与 12px 圆点同心，
    整个圆点仍然落在自己的命中区里 → 连接点拖线照旧可用（契约 §8.2）。
-->
<script setup lang="ts">
import { computed } from "vue";
import type { LayoutRect } from "../../interactive/cardLayout";
import type { ResizeHandle } from "../../interactive/resize";

const props = withDefaults(
  defineProps<{
    /** 这一帧的显示槽位（板面坐标）：键 = 卡片 id，值是 displayRect 的结果 */
    boxes: Record<string, LayoutRect>;
    /** 画布缩放：屏幕 px = 板面单位 × scale */
    scale: number;
    /** 无障碍名称（一般是「卡片标题：尺寸调整」） */
    labels?: Record<string, string>;
    /** 关系编辑 / 拖线进行中时整层让路（只关尺寸把手，不关连接点） */
    disabled?: boolean;
    /**
     * 关系连接点宿主（板面坐标）：键 = 卡片 id，值是 displayRect 的结果。
     * 只有「恰好选中一张、未折叠」的卡片才传进来（筛选在 BoardCanvas 里做，本层只看几何）。
     */
    connectBoxes?: Record<string, LayoutRect>;
  }>(),
  { labels: () => ({}), disabled: false, connectBoxes: () => ({}) },
);

const emit = defineEmits<{
  (e: "handle-down", cardId: string, handle: ResizeHandle, event: PointerEvent): void;
  (e: "nudge", cardId: string, axis: "w" | "h", direction: 1 | -1): void;
  (e: "connect-start", cardId: string, event: PointerEvent): void;
}>();

/** 非有限 / 非正的缩放按 1 处理：宁可尺寸差一点，也不让把手画到不存在的位置。 */
const safeScale = computed(() => (Number.isFinite(props.scale) && props.scale > 0 ? props.scale : 1));

/**
 * 连接点的四个方向与顺序（与既有 data-side 一致）。
 * top / right / bottom / left 是**渲染顺序**：既有测试与浏览器验证都按它取第一个连接点。
 */
const CONNECT_SIDES = ["top", "right", "bottom", "left"] as const;
type ConnectSide = (typeof CONNECT_SIDES)[number];

/** 连接点的 title 文案（沿用改动前那一句，只补方向，方便读屏 / 悬停辨认）。 */
const CONNECT_LABELS: Record<ConnectSide, string> = {
  top: "上边",
  right: "右边",
  bottom: "下边",
  left: "左边",
};

/** 宿主定位：显示槽位（板面坐标）× 缩放。把手与连接点用**同一个**换算，不各写一套。 */
function hostStyle(box: LayoutRect, scale: number): Record<string, string> {
  return {
    left: box.x * scale + "px",
    top: box.y * scale + "px",
    width: Math.max(1, box.w * scale) + "px",
    height: Math.max(1, box.h * scale) + "px",
  };
}

interface ResizeHost {
  id: string;
  label: string;
  style: Record<string, string>;
  w: number;
  h: number;
}

const hosts = computed<ResizeHost[]>(() => {
  const scale = safeScale.value;
  const out: ResizeHost[] = [];
  for (const [id, box] of Object.entries(props.boxes ?? {})) {
    if (!box) continue;
    out.push({
      id,
      label: props.labels?.[id] ?? "卡片尺寸调整",
      style: hostStyle(box, scale),
      w: Math.max(1, box.w * scale),
      h: Math.max(1, box.h * scale),
    });
  }
  return out;
});

interface ConnectHost {
  id: string;
  style: Record<string, string>;
}

/** 连接点宿主：与把手同一份 displayRect × scale，但排在更高一层。 */
const connectHosts = computed<ConnectHost[]>(() => {
  const scale = safeScale.value;
  const out: ConnectHost[] = [];
  for (const [id, box] of Object.entries(props.connectBoxes ?? {})) {
    if (!box) continue;
    out.push({ id, style: hostStyle(box, scale) });
  }
  return out;
});

/**
 * 从连接点开始拖线：连接点独占这一次指针。
 * 这里拦住冒泡与默认行为 —— 板面不会把这次按下当成空白平移 / 框选；
 * 拖动中不移动、不拉伸，建链与否由 BoardCanvas 决定（本层只发事件，不建链）。
 */
function onConnectDown(cardId: string, event: PointerEvent): void {
  event.stopPropagation();
  event.preventDefault();
  emit("connect-start", cardId, event);
}

/** 右下角唯一缩放把手：只发 se 方向，其余边角不再有尺寸热区。 */
function onCornerDown(cardId: string, event: PointerEvent): void {
  event.stopPropagation();
  event.preventDefault();
  emit("handle-down", cardId, "se", event);
}

/**
 * 右下角热区：沿边框加粗段走的 L 形带状路径，不是方形按钮。
 * 取卡片右下角约 110px 的两条边，路径中心压在边框上，stroke 宽度 26px；
 * 因此实际可抓区域覆盖加粗边框，并向外、向内各多出一圈。
 */
function cornerHitPath(w: number, h: number): string {
  const arm = Math.min(110, Math.max(54, Math.min(w, h) * 0.42));
  const x = Math.max(1, w - 1);
  const y = Math.max(1, h - 1);
  const r = Math.min(22, w / 2, h / 2);
  return (
    "M " + x + " " + (h - arm) +
    " L " + x + " " + (h - r) +
    " Q " + x + " " + y + " " + (w - r) + " " + y +
    " L " + (w - arm) + " " + y
  );
}
</script>

<template>
  <div class="resize-layer" data-im="resize-layer">
    <div
      v-for="host in hosts"
      :key="host.id"
      class="resize-host"
      :style="host.style"
      :data-resize-card-id="host.id"
    >
      <!-- 唯一缩放入口：贴合右下角加粗边框的 L 形带状热区，不是方块。 -->
      <svg
        v-if="!props.disabled"
        class="corner-resize"
        data-im="corner-resize"
        data-resize-handle="se"
        :data-card-id="host.id"
        :viewBox="'0 0 ' + host.w + ' ' + host.h"
        :aria-label="host.label"
        preserveAspectRatio="none"
        role="button"
        tabindex="0"
        @pointerdown="onCornerDown(host.id, $event)"
      >
        <path class="corner-hit-path" :d="cornerHitPath(host.w, host.h)" />
      </svg>
    </div>

    <!--
      关系连接点：与尺寸把手**同一层、同一份显示坐标**（displayRect × scale），
      但在本层里排在更高的 z-index 上 —— 边的中点上连接点优先，其余边 / 角仍是尺寸调整。
      命中区是屏幕像素 18×18（本层不随画布缩放），视觉圆点由 ::after 画成 12px。
    -->
    <div
      v-for="host in connectHosts"
      :key="'connect-' + host.id"
      class="connect-host"
      :style="host.style"
      :data-connect-card-id="host.id"
    >
      <span
        v-for="side in CONNECT_SIDES"
        :key="side"
        class="connect-point"
        :class="side"
        data-im="connect-point"
        :data-card-id="host.id"
        :data-side="side"
        :title="'从连接点拖到另一张卡片建立关系（' + CONNECT_LABELS[side] + '）'"
        @pointerdown="onConnectDown(host.id, $event)"
      ></span>
    </div>
  </div>
</template>

<style scoped>
/*
 * 定位在 .board-scroll-content（未缩放）里，位置由行内样式给（板面坐标 × 缩放）。
 * z-index 高于卡片层：注意 .board-surface 带 transform，本身就是一个层叠上下文，
 * 它的子元素（卡片 / 组框 / 连线）整体在这一层之下，所以这里的把手一定在卡片之上。
 */
.resize-layer {
  position: absolute;
  left: 0;
  top: 0;
  width: 100%;
  height: 100%;
  pointer-events: none;
  z-index: 45;
}
/*
 * 宿主只定位：真正的热区 / 按钮由 ResizeHandles 画（父级必须是 relative / absolute）。
 *
 * 2026-10-14（用户要求）：板面上**不出现**宽 / 高那对键盘按钮 —— 按钮仍在 DOM 里
 * （组件契约与既有用例不变），只在板面这一层视觉隐藏，键盘 / 读屏也拿不到。
 * 尺寸把手显式给 z-index 1，连接点给 2：同一层内**不依赖 DOM 顺序**，连接点永远在热区之上。
 */
/*
  2026-10-14（用户要求）：板面卡片上不出现宽 / 高那对键盘按钮。
  按钮仍在 DOM（组件契约与既有用例不变），这里只做视觉隐藏 —— 键盘 / 读屏也拿不到。
*/
.resize-host :deep(.rz-keys) {
  display: none;
}
.resize-host {
  position: absolute;
  pointer-events: none;
  z-index: 1;
}
/* 连接点宿主：同一层里更高一层；自身不接管指针，只有命中区接管 */
.connect-host {
  position: absolute;
  pointer-events: none;
  z-index: 2;
}

/*
 * --- 关系连接点命中区 -----------------------------------------------------
 * 屏幕像素 18×18（本层在未缩放的滚动内容层里，不受画布 scale 影响）；
 * 贴在对应边的**热区内侧**（见下面的分侧规则），整个命中区仍在卡片内部 ——
 * 不会伸到卡片外面压住局部工具栏 / 相邻卡片，也不与边把手的 10px 热区重叠。
 */
/*
 * 唯一缩放入口：沿右下角加粗边框走的 L 形带状热区。
 * 视觉边框仍是卡片自己的轮廓；这里只是一层透明的 stroke 命中带，
 * 宽度 26px，比看得见的加粗边框向外、向内各多一圈，不是方块按钮。
 */
.corner-resize {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  overflow: visible;
  pointer-events: none;
  touch-action: none;
  z-index: 3;
}
.corner-hit-path {
  fill: none;
  stroke: rgba(0, 0, 0, 0.001);
  stroke-width: 26px;
  stroke-linecap: round;
  stroke-linejoin: round;
  pointer-events: stroke;
  cursor: nwse-resize;
}
.corner-resize:focus-visible .corner-hit-path {
  stroke: color-mix(in srgb, var(--focus-ring) 28%, transparent);
}
@media (pointer: coarse) {
  .corner-hit-path {
    stroke-width: 34px;
  }
}

/*
 * 连线端口：贴在卡片外侧边缘的短条，悬停时平滑变成圆点。
 * 命中区 30×30（桌面）；视觉短条横边 16×6、竖边 6×16。
 */
.connect-point {
  position: absolute;
  width: 30px;
  height: 30px;
  pointer-events: auto;
  touch-action: none;
  cursor: crosshair;
}
.connect-point::after {
  content: "";
  position: absolute;
  left: 50%;
  top: 50%;
  width: 6px;
  height: 16px;
  transform: translate(-50%, -50%);
  border-radius: 8px;
  background: var(--accent);
  opacity: 0.72;
  box-shadow: 0 1px 5px var(--shadow-2);
  transition:
    width var(--dur-fast) var(--ease-1),
    height var(--dur-fast) var(--ease-1),
    border-radius var(--dur-fast) var(--ease-1),
    opacity var(--dur-fast) var(--ease-1),
    box-shadow var(--dur-fast) var(--ease-1);
}
.connect-point:hover::after,
.connect-point:focus-visible::after {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  opacity: 1;
  box-shadow: 0 0 0 5px var(--accent-soft);
}
.connect-point.top {
  left: 50%;
  top: -30px;
  transform: translateX(-50%);
}
.connect-point.right {
  right: -30px;
  top: 50%;
  transform: translateY(-50%);
}
.connect-point.bottom {
  left: 50%;
  bottom: -30px;
  transform: translateX(-50%);
}
.connect-point.left {
  left: -30px;
  top: 50%;
  transform: translateY(-50%);
}
.connect-point.top::after,
.connect-point.bottom::after {
  width: 16px;
  height: 6px;
}
/* 方向规则在前、悬停形态在后：横边悬停也必须变成圆点，不能被方向尺寸覆盖。 */
.connect-point.top:hover::after,
.connect-point.bottom:hover::after,
.connect-point.left:hover::after,
.connect-point.right:hover::after,
.connect-point.top:focus-visible::after,
.connect-point.bottom:focus-visible::after,
.connect-point.left:focus-visible::after,
.connect-point.right:focus-visible::after {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  opacity: 1;
  box-shadow: 0 0 0 5px var(--accent-soft);
}
@media (pointer: coarse) {
  .connect-point {
    width: 44px;
    height: 44px;
  }
  .connect-point.top {
    left: calc(50% - 22px);
    top: -44px;
  }
  .connect-point.right {
    right: -44px;
    top: calc(50% - 22px);
  }
  .connect-point.bottom {
    left: calc(50% - 22px);
    bottom: -44px;
  }
  .connect-point.left {
    left: -44px;
    top: calc(50% - 22px);
  }
}
</style>
