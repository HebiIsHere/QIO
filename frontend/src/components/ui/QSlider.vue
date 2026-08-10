<script setup lang="ts">
/**
 * QSlider —— 自定义滑块（设计系统 v2）：
 * 包装原生 range，轨道用渐变绘制已填充段（--qslider-fill），自定义玫红滑块；
 * 保留原生键盘方向键 / 触控 / 无障碍。
 */
import { computed } from "vue";

const props = withDefaults(
  defineProps<{
    modelValue: number;
    min?: number;
    max?: number;
    step?: number;
    label?: string;
    disabled?: boolean;
  }>(),
  { min: 0, max: 1, step: 0.01, label: "", disabled: false },
);
const emit = defineEmits<{ "update:modelValue": [value: number] }>();

const fill = computed(() => {
  const t = (props.modelValue - props.min) / Math.max(1e-6, props.max - props.min);
  return `${Math.min(100, Math.max(0, t * 100))}%`;
});
</script>

<template>
  <input
    class="q-slider"
    type="range"
    :min="min"
    :max="max"
    :step="step"
    :value="modelValue"
    :disabled="disabled"
    :aria-label="label"
    :style="{ '--qslider-fill': fill }"
    @input="emit('update:modelValue', Number(($event.target as HTMLInputElement).value))"
  />
</template>

<style scoped>
.q-slider {
  -webkit-appearance: none;
  appearance: none;
  width: 100%;
  height: 18px;
  margin: 0;
  background: transparent;
  cursor: pointer;
}
.q-slider:disabled {
  opacity: 0.5;
  cursor: default;
}
/* 轨道：左侧 accent 已填充，右侧弱边框底色 */
.q-slider::-webkit-slider-runnable-track {
  height: 4px;
  border-radius: 2px;
  background: linear-gradient(
    to right,
    var(--accent) 0%,
    var(--accent) var(--qslider-fill, 0%),
    var(--border-strong) var(--qslider-fill, 0%),
    var(--border-strong) 100%
  );
}
.q-slider::-webkit-slider-thumb {
  -webkit-appearance: none;
  appearance: none;
  width: 14px;
  height: 14px;
  margin-top: -5px;
  border-radius: 50%;
  background: var(--bg-surface);
  border: 2px solid var(--accent);
  box-shadow: 0 1px 4px rgba(0, 0, 0, 0.35);
  transition: transform 0.15s ease, box-shadow 0.15s ease;
}
.q-slider:hover::-webkit-slider-thumb,
.q-slider:focus-visible::-webkit-slider-thumb {
  transform: scale(1.15);
  box-shadow: 0 0 0 4px var(--accent-soft), 0 1px 4px rgba(0, 0, 0, 0.35);
}
.q-slider:focus-visible {
  outline: none;
}
/* Firefox 兜底（Tauri 主要 WebKit，这里保证可用） */
.q-slider::-moz-range-track {
  height: 4px;
  border-radius: 2px;
  background: var(--border-strong);
}
.q-slider::-moz-range-progress {
  height: 4px;
  border-radius: 2px;
  background: var(--accent);
}
.q-slider::-moz-range-thumb {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: var(--bg-surface);
  border: 2px solid var(--accent);
}
</style>
