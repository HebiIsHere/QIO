<script setup lang="ts">
/**
 * 设置行：左侧「标题」，右侧控件，说明文字在两者下方整行。
 *
 * 为什么统一成一个组件：设置页里 30+ 行曾经各写一套 flex/间距，
 * 宽窄切换时对齐就开始漂。这里只负责结构与可访问名称，
 * 控件由调用方通过默认插槽传入（不同控件需要的宽度与行为不一样）。
 *
 * #70（2026-11-12）：宽窗里控件不再被 flex 的增长推到大面板最右侧，
 * 而是紧跟在标题文字右侧（固定 24px 列间距），标题 + 控件落在同一段可读区域；
 * 说明文字单独占下一行（跨两列），不再把标题列撑宽。
 * 窄窗（≤900px）改为「标题 → 说明 → 控件」上下排列，控件独占一行。
 */
withDefaults(
  defineProps<{
    title: string;
    desc?: string;
    /** 与搜索索引一致的稳定 key，落在 data-settings-key 上 */
    settingsKey: string;
  }>(),
  { desc: "" },
);
</script>

<template>
  <div class="set-row" :data-settings-key="settingsKey">
    <div class="set-row__txt">
      <div class="set-row__title">{{ title }}</div>
    </div>
    <p v-if="desc" class="set-row__desc">{{ desc }}</p>
    <div class="set-row__ctl">
      <slot />
    </div>
  </div>
</template>
