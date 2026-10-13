<!--
  卡片尺寸把手层（IRL-C · 任务书 §3.1 / §3.2）。

  为什么是单独一层、而且放在**缩放之外**：
  - 卡片画在 .board-surface 里，而那一层带 transform: scale()。把手如果也画在里面，
    热区会跟着缩放一起放大缩小 —— scale 0.25 时 10px 的边只剩 2.5px，用户根本抓不住。
    所以这一层放在**未缩放**的滚动内容层里：位置 = 显示槽位 × 缩放，
    10px 边 / 16px 角在任何缩放下都是真实像素，与卡片边缘仍然严格对齐。
  - 卡片、组框、关系线、把手共用同一份显示坐标（BoardCanvas 的 displayRect）：
    展开让位、拖动、尺寸预览之后四者不会各画各的。
  - 整层 pointer-events: none，只有把手热区接管指针：只读卡片正文、组框、连线都不被挡住。
  - 本层不写任何状态：按下 / −＋ 只把事件发出去，预览与提交由 BoardCanvas 决定
    （尺寸只是查看与布局，不产生表达、不调用模型、不申请审批）。
-->
<script setup lang="ts">
import { computed } from "vue";
import ResizeHandles from "./ResizeHandles.vue";
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
    /** 关系编辑 / 拖线进行中时整层让路 */
    disabled?: boolean;
  }>(),
  { labels: () => ({}), disabled: false },
);

const emit = defineEmits<{
  (e: "handle-down", cardId: string, handle: ResizeHandle, event: PointerEvent): void;
  (e: "nudge", cardId: string, axis: "w" | "h", direction: 1 | -1): void;
}>();

/** 非有限 / 非正的缩放按 1 处理：宁可尺寸差一点，也不让把手画到不存在的位置。 */
const safeScale = computed(() => (Number.isFinite(props.scale) && props.scale > 0 ? props.scale : 1));

interface ResizeHost {
  id: string;
  label: string;
  style: Record<string, string>;
}

const hosts = computed<ResizeHost[]>(() => {
  const scale = safeScale.value;
  const out: ResizeHost[] = [];
  for (const [id, box] of Object.entries(props.boxes ?? {})) {
    if (!box) continue;
    out.push({
      id,
      label: props.labels?.[id] ?? "卡片尺寸调整",
      style: {
        left: box.x * scale + "px",
        top: box.y * scale + "px",
        width: Math.max(1, box.w * scale) + "px",
        height: Math.max(1, box.h * scale) + "px",
      },
    });
  }
  return out;
});
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
      <ResizeHandles
        :label="host.label"
        :disabled="props.disabled"
        @handle-down="(handle, event) => emit('handle-down', host.id, handle, event)"
        @nudge="(axis, direction) => emit('nudge', host.id, axis, direction)"
      />
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
/* 宿主只定位：真正的热区 / 按钮由 ResizeHandles 画（父级必须是 relative / absolute） */
.resize-host {
  position: absolute;
  pointer-events: none;
}
</style>
