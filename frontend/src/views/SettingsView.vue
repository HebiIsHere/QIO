<script setup lang="ts">
/**
 * 设置页外壳（五类：常规 / 模型与服务 / 记忆与整理 / 工具与权限 / 关于与更新）。
 *
 * 三件事只在这里做：
 * 1. 导航位置：宽窗左侧竖排、窄窗顶部标签页 —— **只换导航容器布局**，
 *    内容用同一份 DOM（各分区一直挂载，仅 v-show 切换），宽窄切换不重建表单、
 *    不丢滚动位置/展开状态/未保存草稿。
 * 2. 搜索定位：点结果 → 切分类 → 展开折叠组 → 滚动并短暂高亮；
 *    关键词与结果位置在返回搜索面板时保留。
 * 3. 单一滚动视口：整个设置页只有这一层纵向滚动（导航 sticky，内容不再各自滚动）。
 *
 * 不画自己的标题栏：顶部全局栏由 App 的 WindowChrome 提供（§3）；
 * 只有导航区里的一个紧凑「返回对话」图标按钮。
 *
 * 离开设置页：不自动保存、不弹询问、不发额外写请求；未保存草稿随组件卸载丢弃（§5）。
 */
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from "vue";
import router from "../router";
import { useUiStore } from "../stores/ui";
import { vQioScrollbar } from "../composables/useQioScrollbars";
import "../styles/settings.css";
// 视觉修复（2026-10-28）：去掉常规态灰底与药丸描边。只作用于 .qio-settings，
// 其它页面与共享原语的默认样式不受影响；详见该文件头部说明。
import "../styles/settings-surface.css";
import {
  SETTINGS_CATEGORIES,
  SETTINGS_CATEGORY_IDS,
  type SettingsCategoryId,
} from "./settings/types";
import SettingsNav from "./settings/shell/SettingsNav.vue";
import SettingsSearchPanel from "./settings/shell/SettingsSearchPanel.vue";
import type { SettingsSearchEntry } from "./settings/searchIndex";
import { requestSettingsReveal, clearSettingsReveal } from "./settings/reveal";
import { resetEffectiveFacts } from "./settings/effectiveFacts";
import { resetModelFacts } from "./settings/modelFacts";
import RegularSection from "./settings/sections/RegularSection.vue";
import ModelServicesSection from "./settings/sections/ModelServicesSection.vue";
import MemorySection from "./settings/sections/MemorySection.vue";
import ToolsSection from "./settings/sections/ToolsSection.vue";
import AboutSection from "./settings/sections/AboutSection.vue";

const ui = useUiStore();

/* ---------------- 分类 ---------------- */
function isCategoryId(value: unknown): value is SettingsCategoryId {
  return SETTINGS_CATEGORY_IDS.includes(value as SettingsCategoryId);
}
const activeCategory = ref<SettingsCategoryId>(
  isCategoryId(ui.settingsSection) ? ui.settingsSection : "regular",
);
watch(activeCategory, (value) => {
  ui.settingsSection = value;
});

/* ---------------- 宽窄：只切导航布局 ---------------- */
/** 900px 左右：再窄左侧竖排就会挤掉内容列（设计约定「窄窗导航移到顶部」）。 */
const NARROW_MAX_WIDTH = 900;
const narrow = ref(false);
function syncViewport(): void {
  if (typeof window === "undefined") return;
  narrow.value = window.innerWidth <= NARROW_MAX_WIDTH;
}

/* ---------------- 搜索 ---------------- */
const rootRef = ref<HTMLElement | null>(null);
const panelsRef = ref<HTMLElement | null>(null);
const searchPanelRef = ref<InstanceType<typeof SettingsSearchPanel> | null>(null);
const searchOpen = ref(false);
const searchQuery = ref("");
/** 内容区滚动位置：搜索面板与内容区共用同一个滚动视口，来回切换要还回去。 */
let contentScroll = 0;
/** 结果面板的滚动位置：点结果去定位，返回结果时停回原处。 */
let searchReturnScroll = 0;
const canReturnToSearch = computed(() => !searchOpen.value && searchQuery.value.trim().length > 0);

function openSearch(): void {
  if (searchOpen.value) return;
  contentScroll = rootRef.value?.scrollTop ?? 0;
  searchOpen.value = true;
  void nextTick(() => {
    if (rootRef.value) rootRef.value.scrollTop = 0;
    searchPanelRef.value?.focusInput();
  });
}

function closeSearch(): void {
  if (!searchOpen.value) return;
  searchOpen.value = false;
  void nextTick(() => {
    if (rootRef.value) rootRef.value.scrollTop = contentScroll;
  });
}

function toggleSearch(): void {
  if (searchOpen.value) closeSearch();
  else openSearch();
}

function returnToResults(): void {
  if (searchOpen.value) return;
  searchOpen.value = true;
  void nextTick(() => {
    if (rootRef.value) rootRef.value.scrollTop = searchReturnScroll;
    searchPanelRef.value?.focusInput();
  });
}

/* ---------------- 搜索定位：切分类 → 展开 → 滚动 + 短暂高亮 ---------------- */
const highlightedKey = ref<string | null>(null);
let highlightTimer: ReturnType<typeof setTimeout> | null = null;
const HIGHLIGHT_MS = 1600;

function clearHighlightTimer(): void {
  if (highlightTimer) {
    clearTimeout(highlightTimer);
    highlightTimer = null;
  }
}

function nextFrame(): Promise<void> {
  return new Promise((resolve) => {
    if (typeof requestAnimationFrame === "function") requestAnimationFrame(() => resolve());
    else setTimeout(resolve, 16);
  });
}

async function focusRevealed(key: string): Promise<void> {
  const root = panelsRef.value;
  if (!root) return;
  const target = root.querySelector<HTMLElement>(`[data-settings-key="${key}"]`);
  if (!target) return; // 目标控件可能由集成方（模型面板）提供：找不到就不假装定位成功
  target.scrollIntoView?.({ block: "center" });
  clearHighlightTimer();
  target.classList.remove("is-revealed");
  // 同一个目标再点一次也要重新播放高亮：读一次布局强制回流
  void target.offsetWidth;
  target.classList.add("is-revealed");
  highlightedKey.value = key;
  highlightTimer = setTimeout(() => {
    target.classList.remove("is-revealed");
    if (highlightedKey.value === key) highlightedKey.value = null;
    highlightTimer = null;
  }, HIGHLIGHT_MS);
}

async function onSearchSelect(entry: SettingsSearchEntry): Promise<void> {
  searchReturnScroll = rootRef.value?.scrollTop ?? 0;
  searchOpen.value = false;
  activeCategory.value = entry.category;
  requestSettingsReveal(entry.key, entry.fold ?? null);
  // 折叠组是 nextTick 里展开的；再等两帧让布局稳定后定位，避免滚动到旧位置
  await nextTick();
  await nextFrame();
  await nextFrame();
  await focusRevealed(entry.key);
}

function selectCategory(id: SettingsCategoryId): void {
  if (id === activeCategory.value) {
    if (searchOpen.value) closeSearch();
    return;
  }
  activeCategory.value = id;
  if (searchOpen.value) closeSearch();
}

function backToConversation(): void {
  void router.push({ name: "conversation" }).catch(() => {
    /* 单测环境没有真实路由：返回内容页失败不影响设置页自身 */
  });
}

/* ---------------- 生命周期 ---------------- */
onMounted(() => {
  ui.settingsSection = activeCategory.value;
  // 只读事实表按「本次进入」重新建立，避免把上次会话的真值当当前值
  resetEffectiveFacts();
  resetModelFacts();
  syncViewport();
  window.addEventListener("resize", syncViewport);
});

onUnmounted(() => {
  window.removeEventListener("resize", syncViewport);
  clearHighlightTimer();
  ui.settingsSection = activeCategory.value;
  clearSettingsReveal();
  // 注意：这里**不**保存任何草稿、也不发写请求 —— 离开即丢弃未保存修改（§5）
});
</script>

<template>
  <div
    ref="rootRef"
    class="qio-settings"
    v-qio-scrollbar
    data-qio-fit
    data-testid="settings-page"
    :data-settings-page="activeCategory"
    :data-nav-layout="narrow ? 'top' : 'side'"
  >
    <SettingsNav
      :categories="SETTINGS_CATEGORIES"
      :active="activeCategory"
      :narrow="narrow"
      :search-open="searchOpen"
      :can-return-to-search="canReturnToSearch"
      @select="selectCategory"
      @back="backToConversation"
      @toggle-search="toggleSearch"
      @return-to-results="returnToResults"
    />

    <div class="set-main">
      <SettingsSearchPanel
        ref="searchPanelRef"
        :query="searchQuery"
        :open="searchOpen"
        @update:query="searchQuery = $event"
        @select="onSearchSelect"
        @close="closeSearch"
      />

      <div v-show="!searchOpen" ref="panelsRef" class="set-panels" data-testid="settings-panels">
        <RegularSection
          v-show="activeCategory === 'regular'"
          id="set-panel-regular"
          role="tabpanel"
          aria-labelledby="set-tab-regular"
        />
        <ModelServicesSection
          v-show="activeCategory === 'models'"
          id="set-panel-models"
          role="tabpanel"
          aria-labelledby="set-tab-models"
        />
        <MemorySection
          v-show="activeCategory === 'memory'"
          id="set-panel-memory"
          role="tabpanel"
          aria-labelledby="set-tab-memory"
        />
        <ToolsSection
          v-show="activeCategory === 'tools'"
          id="set-panel-tools"
          role="tabpanel"
          aria-labelledby="set-tab-tools"
        />
        <AboutSection
          v-show="activeCategory === 'about'"
          id="set-panel-about"
          role="tabpanel"
          aria-labelledby="set-tab-about"
        />
      </div>
    </div>
  </div>
</template>

<style scoped>
/* 单一主滚动视口：整页只有这里纵向滚动（导航 sticky、内容不再各自滚动）。
   横向锁死：内容本身必须能换行，clip 只作边界保护（§15.3）。 */
.qio-settings {
  height: 100%;
  overflow-y: auto;
  overflow-x: clip;
  padding: 18px 22px 60px;
  background: var(--bg-base);
  color: var(--text-primary);
  font-family: var(--sans);
  /* 从对话页进入设置：只做一次短淡入，不做整页位移（§13.2） */
  animation: settings-in var(--dur-page-in) var(--ease-out) both;
}
@keyframes settings-in {
  from { opacity: 0; }
  to { opacity: 1; }
}
</style>
