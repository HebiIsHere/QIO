<script setup lang="ts">
/**
 * 高级选项折叠组（自绘，不用 <details> 的系统三角形）。
 *
 * 语义：真正的 <button aria-expanded aria-controls> + 折叠区 role=group。
 * 行内容用 v-show 保留 DOM —— 搜索定位可以在不重建表单的情况下把目标展开，
 * 已填的草稿也不会因为折叠而丢失。
 *
 * 搜索联动：settingsReveal 请求里的 fold 等于本组 id 时自动展开，
 * 这样「点搜索结果 → 展开高级选项 → 定位」不需要 shell 知道各分区内部结构。
 */
import { computed, onMounted, ref, watch } from "vue";
import QIcon from "../../../components/ui/QIcon.vue";
import { settingsReveal } from "../reveal";

const props = withDefaults(
  defineProps<{
    /** 折叠组稳定 id（搜索索引里的 fold 值） */
    id: string;
    label: string;
    /** 折叠时显示的一句提示（说明里面有什么） */
    hint?: string;
  }>(),
  { hint: "" },
);

const open = ref(false);
const bodyId = computed(() => `set-adv-body-${props.id.replace(/[^a-zA-Z0-9_-]/g, "-")}`);

function expandForReveal(): void {
  const target = settingsReveal.target;
  if (target && target.fold === props.id) open.value = true;
}

onMounted(expandForReveal);
watch(() => settingsReveal.target?.token, expandForReveal);
</script>

<template>
  <div class="set-adv" :data-settings-fold="id">
    <button
      type="button"
      class="set-adv__toggle"
      :aria-expanded="open"
      :aria-controls="bodyId"
      :data-settings-fold-toggle="id"
      @click="open = !open"
    >
      <QIcon class="set-adv__chev" :name="open ? 'chevron-up' : 'chevron-down'" :size="14" />
      <span class="set-adv__label">{{ label }}</span>
      <span v-if="hint && !open" class="set-adv__hint">{{ hint }}</span>
    </button>
    <div v-show="open" :id="bodyId" class="set-adv__body" role="group" :aria-label="label">
      <slot />
    </div>
  </div>
</template>
