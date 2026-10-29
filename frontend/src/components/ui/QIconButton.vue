<script setup lang="ts">
/**
 * QIconButton —— 纯图标按钮（全站唯一形态）。
 *
 * 为什么必须统一：图标按钮有三个只有统一才能保证的事：
 * 1) 可访问名称：`aria-label` 由调用方强制提供（label 是必填 prop），
 *    不依赖视觉猜测；
 * 2) 用途说明：悬停 500ms 出现统一浮层（v-qio-tip），键盘聚焦立即出现，
 *    触控长按出现；不用原生 title（外观/延迟/键盘可达性都不可控）；
 * 3) 命中区：桌面 32×32（sm 28×28），触控 coarse 下 44×44，容器统一 14px 圆角。
 *
 * 约定：
 * - 只渲染 <button type="button">，不抢表单提交；
 * - 不接收原生 title；说明文字走 description || label；
 * - disabled / busy 时都由原生 disabled 挡住点击，并保留 aria-busy；
 * - 相邻按钮的点击区不重叠：触控放大是「真实盒子变大」，不用负 inset 撑命中区。
 */
import { computed } from "vue";
import QIcon from "./QIcon.vue";
import { vQioTip } from "../../composables/useQioTip";

const props = withDefaults(
  defineProps<{
    /** 图标名（见 components/ui/icons.ts） */
    icon: string;
    /** 可访问名称（必填）；同时作为默认用途说明 */
    label: string;
    /** 更完整的用途说明（可选，显示为浮层文字）；不传则等同于 label */
    description?: string;
    /** 桌面尺寸档：md=32、sm=28（触控一律 44） */
    size?: "sm" | "md";
    /** 语气：普通 / 安静（低对比）/ 危险（删除类动作） */
    tone?: "normal" | "quiet" | "danger";
    disabled?: boolean;
    /** 处理中：保留尺寸与位置，挡住重复点击 */
    busy?: boolean;
    /** 展开态（折叠/展开类按钮用），渲染 aria-expanded；不传则不带该属性 */
    expanded?: boolean;
  }>(),
  {
    description: "",
    size: "md",
    tone: "normal",
    disabled: false,
    busy: false,
    expanded: undefined,
  },
);

const emit = defineEmits<{ click: [event: MouseEvent] }>();

const iconSize = computed(() => (props.size === "sm" ? 14 : 16));
const tipText = computed(() => props.description || props.label);
const isBlocked = computed(() => props.disabled || props.busy);

function onClick(event: MouseEvent): void {
  if (isBlocked.value) {
    event.preventDefault();
    return;
  }
  emit("click", event);
}
</script>

<template>
  <button
    type="button"
    class="qio-icon-btn"
    :class="[`is-${size}`, `tone-${tone}`, { 'is-busy': busy }]"
    :aria-label="label"
    :aria-busy="busy || undefined"
    :aria-expanded="expanded === undefined ? undefined : expanded"
    :disabled="isBlocked"
    v-qio-tip="tipText"
    @click="onClick"
  >
    <QIcon :name="icon" :size="iconSize" />
    <span v-if="busy" class="qio-icon-btn__busy" aria-hidden="true" />
  </button>
</template>

<style scoped>
.qio-icon-btn {
  position: relative;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex: none;
  width: 32px;
  height: 32px;
  padding: 0;
  border: 1px solid transparent;
  border-radius: var(--qio-ui-radius);
  background: none;
  color: var(--text-secondary);
  cursor: pointer;
  touch-action: manipulation;
  -webkit-tap-highlight-color: transparent;
  transition:
    background var(--mo-1-state) var(--ease-1),
    border-color var(--mo-1-state) var(--ease-1),
    color var(--mo-1-state) var(--ease-1),
    transform var(--mo-1-press) var(--ease-1-out);
}
.qio-icon-btn.is-sm {
  width: 28px;
  height: 28px;
}
.qio-icon-btn:hover:not(:disabled) {
  background: var(--layer-hover);
  color: var(--text-strong);
}
.qio-icon-btn:active:not(:disabled) {
  transform: translateY(var(--press-shift));
}
.qio-icon-btn:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 2px;
}
.qio-icon-btn.tone-quiet {
  color: var(--text-muted);
}
.qio-icon-btn.tone-quiet:hover:not(:disabled) {
  color: var(--text-strong);
}
.qio-icon-btn.tone-danger {
  color: var(--danger);
}
.qio-icon-btn.tone-danger:hover:not(:disabled) {
  background: var(--danger-soft);
  color: var(--danger);
}
.qio-icon-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.qio-icon-btn.is-busy {
  cursor: progress;
}
.qio-icon-btn.is-busy .qio-icon {
  opacity: 0.35;
}
/* 处理中的小环：时长仍走令牌（reduced-motion 下被全局压到 90ms 且只转一圈） */
.qio-icon-btn__busy {
  position: absolute;
  width: 14px;
  height: 14px;
  border: 1.6px solid var(--accent-soft);
  border-top-color: var(--accent);
  border-radius: 50%;
  animation: qio-icon-btn-spin var(--mo-3-expand) linear infinite;
}
@keyframes qio-icon-btn-spin {
  to {
    transform: rotate(360deg);
  }
}
/*
 * 触控：有效命中区 44×44。
 * 直接放大真实盒子（而不是用 ::before 负 inset 扩展），
 * 这样在 44px 合约下相邻按钮的点击区天然不重叠。
 */
@media (pointer: coarse) {
  .qio-icon-btn,
  .qio-icon-btn.is-sm {
    width: 44px;
    height: 44px;
  }
}
</style>
