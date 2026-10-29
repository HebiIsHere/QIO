<script setup lang="ts">
/**
 * QInput —— 单行/多行文本输入（QIO 原语 .qio-input 的组件外壳）。
 *
 * 本轮补充：
 * - busy（处理中）：保留内容、挡住编辑，视觉只做“正在进行”的中性提示，
 *   不用假进度条（真实进度由业务状态表达）；
 * - ariaLabel：控件没有可见 label 时也能有可访问名称（不改变调用方的布局）。
 * 状态（禁用/处理中/错误/焦点）全部由 .qio-input 的既有类表达，不另起一套。
 */
import { computed } from "vue";

const props = withDefaults(
  defineProps<{
    modelValue?: string;
    placeholder?: string;
    error?: boolean;
    mono?: boolean;
    disabled?: boolean;
    busy?: boolean;
    type?: string;
    ariaLabel?: string;
  }>(),
  { modelValue: "", error: false, mono: false, disabled: false, busy: false, ariaLabel: undefined },
);
defineEmits<{ "update:modelValue": [string]; change: [Event] }>();

const blocked = computed(() => props.disabled || props.busy);
</script>

<template>
  <input
    class="qio-input"
    :class="{ err: error, mono, busy }"
    :type="type ?? 'text'"
    data-qio-fit
    :value="modelValue"
    :placeholder="placeholder"
    :disabled="blocked"
    :readonly="busy"
    :aria-label="ariaLabel"
    :aria-invalid="error || undefined"
    :aria-busy="busy || undefined"
    @input="$emit('update:modelValue', ($event.target as HTMLInputElement).value)"
    @change="$emit('change', $event)"
  />
</template>
