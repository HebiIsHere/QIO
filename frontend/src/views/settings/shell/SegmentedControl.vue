<script setup lang="ts">
/**
 * 自绘分段控件（替代原生 radio / select 的短列表场景）。
 *
 * 语义：role=group + 一组 aria-pressed 按钮。键盘完全走原生按钮的 Tab/Enter/Space，
 * 不发明「方向键切换」——那会和页面滚动抢键。
 * 选中态不只用颜色：底色 + 字重 + 下划线三重区分（色盲与低对比下仍可读）。
 */
const props = withDefaults(
  defineProps<{
    options: { value: string; label: string; hint?: string }[];
    modelValue: string;
    /** 组件的可访问名称（视觉上不出现标题，由 SettingRow 承担） */
    label: string;
    disabled?: boolean;
  }>(),
  { disabled: false },
);
const emit = defineEmits<{ "update:modelValue": [value: string] }>();

function pick(value: string) {
  if (props.disabled || value === props.modelValue) return;
  emit("update:modelValue", value);
}
</script>

<template>
  <div class="set-seg" role="group" :aria-label="label">
    <button
      v-for="option in options"
      :key="option.value"
      type="button"
      class="set-seg__opt"
      :class="{ on: option.value === modelValue }"
      :aria-pressed="option.value === modelValue"
      :disabled="disabled"
      :data-settings-option="option.value"
      @click="pick(option.value)"
    >
      {{ option.label }}
    </button>
  </div>
</template>
