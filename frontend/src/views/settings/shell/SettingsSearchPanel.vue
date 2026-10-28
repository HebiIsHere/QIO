<script setup lang="ts">
/**
 * 设置搜索面板（设置页内独立入口，不改顶部全局栏行为）。
 *
 * 索引是静态的（searchIndex.ts），所以搜索本身同步、离线、不会失败；
 * 仍然给出「空结果」与「索引不可用」两种自绘状态 —— 后者用于索引被裁剪/加载异常的
 * 兜底展示，绝不用浏览器原生提示。
 *
 * 关键词与结果位置由父组件持有：面板用 v-show 常驻，关闭再打开时输入框内容、
 * 结果列表与页面滚动位置都不丢（「返回搜索结果」走的就是这条路径）。
 */
import { computed, nextTick, ref } from "vue";
import QIcon from "../../../components/ui/QIcon.vue";
import QIconButton from "../../../components/ui/QIconButton.vue";
import { settingsCategoryLabel } from "../types";
import {
  groupSearchResults,
  searchSettings,
  type SettingsSearchEntry,
} from "../searchIndex";

const props = withDefaults(
  defineProps<{ query: string; open: boolean; indexError?: boolean }>(),
  { indexError: false },
);

const emit = defineEmits<{
  "update:query": [value: string];
  select: [entry: SettingsSearchEntry];
  close: [];
}>();

const inputRef = ref<HTMLInputElement | null>(null);
const resultsRef = ref<HTMLElement | null>(null);

const trimmed = computed(() => props.query.trim());
const results = computed<SettingsSearchEntry[]>(() => searchSettings(props.query));
const groups = computed(() => groupSearchResults(results.value));

function onInput(event: Event): void {
  emit("update:query", (event.target as HTMLInputElement).value);
}

function clearQuery(): void {
  emit("update:query", "");
  void nextTick(() => inputRef.value?.focus());
}

function focusInput(): void {
  void nextTick(() => inputRef.value?.focus());
}

function onInputKeydown(event: KeyboardEvent): void {
  if (event.key === "Escape") {
    event.preventDefault();
    emit("close");
    return;
  }
  if (event.key === "ArrowDown" && results.value.length) {
    event.preventDefault();
    hitElements()[0]?.focus();
  }
}

function hitElements(): HTMLButtonElement[] {
  const root = resultsRef.value;
  if (!root) return [];
  return Array.from(root.querySelectorAll<HTMLButtonElement>("[data-search-hit]"));
}

/** 结果列表里的方向键：在命中项之间移动焦点（不改变页面滚动容器）。 */
function onResultsKeydown(event: KeyboardEvent): void {
  if (event.key === "Escape") {
    event.preventDefault();
    emit("close");
    return;
  }
  if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
  const hits = hitElements();
  if (!hits.length) return;
  const current = hits.findIndex((hit) => hit === document.activeElement);
  let next = current;
  if (event.key === "Home") next = 0;
  else if (event.key === "End") next = hits.length - 1;
  else if (event.key === "ArrowDown") next = current < 0 ? 0 : Math.min(hits.length - 1, current + 1);
  else next = current <= 0 ? 0 : current - 1;
  event.preventDefault();
  hits[next]?.focus();
}

defineExpose({ focusInput });
</script>

<template>
  <section v-show="open" class="set-search" data-testid="settings-search-panel" aria-label="设置搜索">
    <div class="set-search__bar">
      <QIcon class="set-search__icon" name="search" :size="15" />
      <input
        ref="inputRef"
        class="qio-input set-search__input"
        type="search"
        :value="query"
        aria-label="搜索设置"
        placeholder="搜索设置项、用途名称或高级参数…"
        autocomplete="off"
        data-testid="settings-search-input"
        @input="onInput"
        @keydown="onInputKeydown"
      />
      <QIconButton
        v-if="trimmed"
        icon="close"
        label="清空搜索"
        description="清空关键词并回到未搜索状态"
        size="sm"
        @click="clearQuery"
      />
      <QIconButton icon="close" label="关闭搜索" description="关闭搜索面板，回到设置内容" size="sm" @click="emit('close')" />
    </div>

    <p v-if="!trimmed" class="set-search__hint">
      输入设置名称、用途或参数名；例如「主题」「权限模式」「记忆整理」「返回条数」。
    </p>

    <p v-else-if="indexError" class="set-search__state err" role="alert" data-testid="settings-search-error">
      搜索索引暂时不可用。可以继续按分类浏览设置，或稍后再试。
    </p>

    <div v-else-if="!results.length" class="set-search__state" data-testid="settings-search-empty">
      <div class="set-search__state-title">没有找到「{{ trimmed }}」</div>
      <div class="set-search__state-desc">
        试试设置名称或用途名：主题、权限模式、默认模型、记忆整理、返回条数。
      </div>
    </div>

    <div v-else ref="resultsRef" class="set-search__results" @keydown="onResultsKeydown">
      <p class="set-search__count" role="status">{{ results.length }} 项结果</p>
      <template v-for="group in groups" :key="group.category">
        <h3 class="set-search__group">{{ settingsCategoryLabel(group.category) }}</h3>
        <ul class="set-search__list">
          <li v-for="entry in group.entries" :key="entry.key" class="set-search__item">
            <button
              type="button"
              class="set-search__hit"
              :data-search-hit="entry.key"
              @click="emit('select', entry)"
            >
              <span class="set-search__hit-title">{{ entry.title }}</span>
              <span class="set-search__hit-cat mono">{{ settingsCategoryLabel(entry.category) }}</span>
              <span class="set-search__hit-desc">{{ entry.description }}</span>
            </button>
          </li>
        </ul>
      </template>
    </div>
  </section>
</template>
