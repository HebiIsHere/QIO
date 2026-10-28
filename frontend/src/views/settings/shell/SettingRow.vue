<script setup lang="ts">
/**
 * 设置行：左侧「标题 + 一句说明」，右侧控件。
 *
 * 为什么统一成一个组件：设置页里 30+ 行曾经各写一套 flex/间距，
 * 宽窄切换时对齐就开始漂。这里只负责结构与可访问名称，
 * 控件由调用方通过默认插槽传入（不同控件需要的宽度与行为不一样）。
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
      <div v-if="desc" class="set-row__desc">{{ desc }}</div>
    </div>
    <div class="set-row__ctl">
      <slot />
    </div>
  </div>
</template>
