<script setup lang="ts">
/**
 * 页面工具行（契约 §3.2）—— 内容区域右上角一组**克制**的入口：搜索 + 管理。
 *
 * 它不是新顶栏，也不复制全局栏：没有标题、没有品牌、没有窗口按钮，只是一行两个入口。
 * 「管理」按钮上的数量角标必须来自真实未处理数（父级从 B 的 planetPending store 读），
 * 没有未处理事项就完全不出现角标（不摆一个 0 当装饰）。
 */
import { computed } from "vue";
import QIcon from "../ui/QIcon.vue";
import QIconButton from "../ui/QIconButton.vue";

const props = withDefaults(
  defineProps<{
    /** 管理区当前是否展开（按钮语义随之切换：管理 / 收起） */
    expanded?: boolean;
    /** 真实未处理数量（0 或负数一律不显示角标） */
    pendingCount?: number;
  }>(),
  { expanded: false, pendingCount: 0 },
);
const emit = defineEmits<{ search: []; "toggle-manage": [] }>();

const showBadge = computed(() => props.pendingCount > 0);
const badgeText = computed(() => (props.pendingCount > 99 ? "99+" : String(props.pendingCount)));
const manageLabel = computed(() =>
  props.expanded ? "收起管理" : showBadge.value ? `打开管理（${props.pendingCount} 项待处理）` : "打开管理",
);
</script>

<template>
  <div class="page-tools qio-glass" role="group" aria-label="页面工具">
    <QIconButton
      icon="search"
      label="搜索话题、知识、实体"
      description="在话题、知识、实体里一起搜索"
      size="sm"
      data-mp-search-open
      @click="emit('search')"
    />
    <button
      class="manage-btn"
      type="button"
      data-mp-manage-toggle
      :aria-expanded="expanded"
      :aria-label="manageLabel"
      @click="emit('toggle-manage')"
    >
      <QIcon :name="expanded ? 'collapse' : 'expand'" :size="14" />
      <span class="manage-text">{{ expanded ? "收起" : "管理" }}</span>
      <span v-if="showBadge" class="pending-badge mono" aria-hidden="true">{{ badgeText }}</span>
    </button>
  </div>
</template>

<style scoped>
.page-tools {
  display: flex;
  align-items: center;
  gap: 2px;
  padding: 3px;
  /* 圆角统一 14px（本页契约）：容器与内部按钮都走同一个令牌 */
  border-radius: var(--qio-ui-radius);
}
.manage-btn {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  height: 28px;
  padding: 0 10px;
  border: 1px solid transparent;
  border-radius: var(--qio-ui-radius);
  background: none;
  color: var(--text-secondary);
  font-family: var(--sans);
  font-size: var(--fs-sm);
  cursor: pointer;
  transition: background var(--mo-1-state) var(--ease-1), color var(--mo-1-state) var(--ease-1);
}
.manage-btn:hover {
  background: var(--layer-hover);
  color: var(--text-strong);
}
.manage-btn:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 2px;
}
.manage-btn[aria-expanded="true"] {
  color: var(--text-strong);
}
.manage-text {
  white-space: nowrap;
}
/* 数量角标：真实未处理数的唯一视觉，不抢「管理」文字的层级 */
.pending-badge {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 17px;
  height: 17px;
  padding: 0 5px;
  border-radius: var(--qio-ui-radius);
  background: var(--accent-soft);
  color: var(--link);
  font-size: var(--fs-xs);
  line-height: 1;
  font-variant-numeric: tabular-nums;
}
</style>
