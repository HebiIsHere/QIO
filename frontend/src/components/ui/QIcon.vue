<script setup lang="ts">
/**
 * QIcon —— 全站唯一图标渲染器。
 *
 * 只用描边路径、currentColor 上色、24×24 viewBox；装饰性图标 aria-hidden，
 * 图标本身不承载可访问名称 —— 名称由 QIconButton 的 aria-label 或
 * 相邻可见文字承担（纯图标按钮必须用 QIconButton，不要直接 <QIcon> + @click）。
 *
 * 图标名集合见 ./icons.ts（唯一来源）。未知名字不会中断渲染：开发期 console.warn，
 * 生产期渲染空 svg 占位，避免一个拼写错误把整块界面打白。
 */
import { computed } from "vue";
import { ICON_PATHS, type IconName } from "./icons";

const props = withDefaults(
  defineProps<{
    /** 图标名（见 icons.ts 的 ICON_NAMES） */
    name: IconName | string;
    /** 视觉尺寸（px 或 CSS 长度）；默认 16 */
    size?: number | string;
    /** 描边宽度；默认 1.6（统一的细线语言） */
    strokeWidth?: number | string;
  }>(),
  { size: 16, strokeWidth: 1.6 },
);

const paths = computed<readonly string[]>(() => {
  const found = ICON_PATHS[props.name as IconName];
  if (!found && import.meta.env.DEV) {
    console.warn(`[QIcon] 未知图标名："${props.name}"，请在 components/ui/icons.ts 里补充`);
  }
  return found ?? [];
});
</script>

<template>
  <svg
    class="qio-icon"
    :width="size"
    :height="size"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    :stroke-width="strokeWidth"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
    focusable="false"
    :data-icon="name"
  >
    <path v-for="(d, i) in paths" :key="i" :d="d" />
  </svg>
</template>

<style scoped>
.qio-icon {
  display: inline-block;
  flex: none;
  vertical-align: middle;
  overflow: visible;
}
</style>
