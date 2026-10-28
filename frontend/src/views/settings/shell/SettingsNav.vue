<script setup lang="ts">
/**
 * 设置导航（宽窗左侧竖排 / 窄窗顶部标签页）。
 *
 * 「只改导航的位置与形式」：这里只有容器布局变化（CSS class + flex 方向），
 * 内容区 DOM 完全不动（各分区一直挂载，仅 v-show 切换），所以切换宽窄不会重建
 * 表单、不会丢草稿、滚动位置与展开状态也都在。
 *
 * 页头说明：设置页**不画自己的标题栏**（全局栏由 WindowChrome 负责）。
 * 只有一个紧凑的「返回对话」图标按钮与「搜索设置」图标按钮 —— 都是图标按钮，
 * 带自绘提示（QIconButton 内置 v-qio-tip）与 aria-label，不构成整行页头。
 */
import QIconButton from "../../../components/ui/QIconButton.vue";
import type { SettingsCategory, SettingsCategoryId } from "../types";

const props = withDefaults(
  defineProps<{
    categories: SettingsCategory[];
    active: SettingsCategoryId;
    /** 窄窗：导航容器从左侧竖排变成顶部标签页（内容 DOM 不变） */
    narrow: boolean;
    searchOpen: boolean;
    /** 搜索过且当前不在搜索面板：显示「返回搜索结果」 */
    canReturnToSearch: boolean;
  }>(),
  { canReturnToSearch: false },
);

const emit = defineEmits<{
  select: [id: SettingsCategoryId];
  back: [];
  toggleSearch: [];
  returnToResults: [];
}>();

/**
 * 标签页的方向键：只移动焦点并切换（WAI-ARIA tabs 的常规行为），
 * 不吞 Tab —— Tab 仍然离开导航去内容区。禁用循环外滚动。
 */
function onTabKey(event: KeyboardEvent, index: number, id: SettingsCategoryId): void {
  if (event.key !== "ArrowDown" && event.key !== "ArrowRight" && event.key !== "ArrowUp" && event.key !== "ArrowLeft" && event.key !== "Home" && event.key !== "End") {
    return;
  }
  event.preventDefault();
  const last = props.categories.length - 1;
  let next = index;
  if (event.key === "Home") next = 0;
  else if (event.key === "End") next = last;
  else if (event.key === "ArrowDown" || event.key === "ArrowRight") next = index >= last ? 0 : index + 1;
  else next = index <= 0 ? last : index - 1;
  const target = props.categories[next];
  emit("select", target.id);
  const tabs = (event.currentTarget as HTMLElement).closest(".set-nav__tabs");
  const button = tabs?.querySelector<HTMLElement>(`[data-settings-nav-tab="${target.id}"]`);
  button?.focus();
  void id;
}
</script>

<template>
  <nav class="set-nav" :class="{ 'is-top': narrow }" :data-settings-nav="narrow ? 'top' : 'side'" aria-label="设置分类导航">
    <div class="set-nav__tools">
      <QIconButton
        icon="chevron-left"
        label="返回对话"
        description="回到对话页（未保存的修改会丢弃）"
        size="sm"
        data-testid="settings-back"
        @click="emit('back')"
      />
      <button
        v-if="canReturnToSearch"
        type="button"
        class="set-nav__return"
        data-testid="settings-search-return"
        @click="emit('returnToResults')"
      >
        返回搜索结果
      </button>
      <span class="set-nav__spacer"></span>
      <QIconButton
        icon="search"
        label="搜索设置"
        description="搜索设置项、用途名称与高级参数"
        size="sm"
        :expanded="searchOpen"
        data-testid="settings-search-entry"
        :data-settings-search-open="searchOpen ? '1' : '0'"
        @click="emit('toggleSearch')"
      />
    </div>
    <div class="set-nav__tabs" role="tablist" aria-label="设置分类">
      <button
        v-for="(category, index) in categories"
        :key="category.id"
        type="button"
        class="set-nav__tab"
        :class="{ active: category.id === active }"
        role="tab"
        :id="`set-tab-${category.id}`"
        :aria-selected="category.id === active"
        :aria-controls="`set-panel-${category.id}`"
        :tabindex="category.id === active ? 0 : -1"
        :data-settings-nav-tab="category.id"
        @click="emit('select', category.id)"
        @keydown="onTabKey($event, index, category.id)"
      >
        {{ category.label }}
      </button>
    </div>
  </nav>
</template>
