<script setup lang="ts">
/**
 * 全局搜索（契约 §3.2 / A 交付 §5）。
 *
 * 三个硬要求：
 * 1. **范围与当前标签页无关**：一次搜索同时覆盖话题 / 知识 / 实体，结果按三类分组；
 * 2. **真实数据**：话题 / 知识 / 实体都是后端全量接口，这里并行取回再按关键词匹配
 *    （不构造演示数据、不前端拼假条目）；
 * 3. **旧响应不得覆盖新关键词**：每次检索自增序号，只有最新一次的结果可以落到界面。
 *
 * 状态区分要诚实：加载中（保留上一次结果并标记「正在搜索」）、无结果（真的没有命中）、
 * 失败（说明原因并可重试）—— 三者不能混成一个空列表。
 *
 * 结果点击只上报 hit（kind + 真实 id）：切哪个标签页、打开哪个条目由管理区决定，
 * 这样「搜索 → 打开条目 → 返回搜索结果」的上下文留在同一处。
 */
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from "vue";
import { api } from "../../services/api";
import QInput from "../ui/QInput.vue";
import QIconButton from "../ui/QIconButton.vue";
import { vQioScrollbar } from "../../composables/useQioScrollbars";
import {
  buildSearchGroups,
  SEARCH_GROUP_LIMIT,
  type SearchGroups,
  type SearchHit,
} from "../../planet/manageViews";

/** 结果分组的键（与 SearchGroups 的三个列表同形；SearchKind 是单数，不能混用） */
type GroupKey = "topics" | "knowledge" | "entities";

const props = withDefaults(
  defineProps<{
    /** 搜索视图当前是否可见（用于聚焦输入框；结果与滚动由 v-show 保活） */
    active?: boolean;
    /** 输入停顿多久再检索（测试传 0 直接同步走） */
    debounceMs?: number;
  }>(),
  { active: false, debounceMs: 220 },
);
const emit = defineEmits<{ open: [hit: SearchHit] }>();

const EMPTY: SearchGroups = {
  topics: [],
  knowledge: [],
  entities: [],
  total: 0,
  counts: { topics: 0, knowledge: 0, entities: 0 },
};

const query = ref("");
const groups = ref<SearchGroups>(EMPTY);
/** 已经出结果的关键词（用于文案与空状态；与输入框里的实时值区分开） */
const searchedFor = ref("");
const loading = ref(false);
const error = ref("");
const inputRef = ref<{ $el?: HTMLInputElement } | null>(null);

/** 请求序号：只接受最后一次检索的结果（旧响应一律丢弃） */
let seq = 0;
let timer = 0;

/**
 * 每类当前展示到第几条（F11：分页只看展示层，命中列表与总数始终是完整的）。
 * 换关键词时重置——展开状态属于「这一次搜索」，不能带到下一次。
 */
const shown = reactive<Record<GroupKey, number>>({
  topics: SEARCH_GROUP_LIMIT,
  knowledge: SEARCH_GROUP_LIMIT,
  entities: SEARCH_GROUP_LIMIT,
});

function resetShown(): void {
  shown.topics = SEARCH_GROUP_LIMIT;
  shown.knowledge = SEARCH_GROUP_LIMIT;
  shown.entities = SEARCH_GROUP_LIMIT;
}

/** 某一类再展开一批（每次一批，直到最后一条也可见、可点）。 */
function showMore(kind: GroupKey): void {
  const all = groups.value[kind];
  shown[kind] = Math.min(all.length, shown[kind] + SEARCH_GROUP_LIMIT);
}

const visibleGroups = computed(() =>
  (
    [
      { key: "topics" as const, title: "话题" },
      { key: "knowledge" as const, title: "知识" },
      { key: "entities" as const, title: "实体" },
    ] as { key: GroupKey; title: string }[]
  )
    .filter((group) => groups.value[group.key].length > 0)
    .map((group) => {
      const all = groups.value[group.key];
      const limit = Math.min(shown[group.key], all.length);
      return {
        key: group.key,
        title: group.title,
        total: groups.value.counts[group.key],
        hits: all.slice(0, limit),
        remaining: all.length - limit,
      };
    }),
);
const hasQuery = computed(() => query.value.trim().length > 0);
const showEmpty = computed(
  () => !loading.value && !error.value && searchedFor.value !== "" && groups.value.total === 0,
);

async function runSearch(raw: string) {
  const keyword = raw.trim();
  const mySeq = ++seq;
  if (!keyword) {
    loading.value = false;
    error.value = "";
    groups.value = EMPTY;
    searchedFor.value = "";
    resetShown();
    return;
  }
  loading.value = true;
  error.value = "";
  try {
    // 三个接口都是全量数据源：并行取回，关键词在本地匹配（不等串行）
    const [topics, knowledge, entities] = await Promise.all([
      api.listTopics(),
      api.listKnowledge(),
      api.listEntities(),
    ]);
    if (mySeq !== seq) return; // 旧响应：新的关键词已经发起，丢弃这一次
    const next = buildSearchGroups(
      { topics: topics.topics, knowledge: knowledge.knowledge, entities: entities.entities },
      keyword,
    );
    // 换关键词 = 新一代搜索：结果与展开状态一起重置（旧关键词的展开位置没有意义）。
    if (keyword !== searchedFor.value) resetShown();
    groups.value = next;
    searchedFor.value = keyword;
  } catch (e) {
    if (mySeq !== seq) return;
    // 失败不冒充「没有结果」：上一次结果保留，同时说清原因
    error.value = `搜索失败：${(e as Error).message}`;
  } finally {
    if (mySeq === seq) loading.value = false;
  }
}

function scheduleSearch(raw: string) {
  window.clearTimeout(timer);
  const wait = Math.max(0, props.debounceMs);
  if (wait === 0) {
    void runSearch(raw);
    return;
  }
  timer = window.setTimeout(() => void runSearch(raw), wait);
}

watch(query, (value) => scheduleSearch(value));

/** 重试：同一个关键词再走一遍（失败后不必重新输入） */
function retry() {
  void runSearch(query.value);
}

/** 切回搜索视图时把焦点放回输入框（结果与滚动位置保持原样） */
watch(
  () => props.active,
  async (on) => {
    if (!on) return;
    await nextTickFocus();
  },
);

async function nextTickFocus() {
  const { nextTick } = await import("vue");
  await nextTick();
  const el = (inputRef.value as { $el?: HTMLInputElement } | null)?.$el;
  el?.focus?.();
}

onMounted(() => {
  if (props.active) void nextTickFocus();
});
onBeforeUnmount(() => window.clearTimeout(timer));
</script>

<template>
  <div class="gsearch" data-mp-search>
    <div class="gs-bar">
      <QInput
        ref="inputRef"
        v-model="query"
        data-mp-search-input
        placeholder="搜索话题、知识、实体…"
        aria-label="全局搜索"
      />
      <QIconButton
        icon="refresh"
        label="重新搜索"
        description="用当前关键词重新搜索一次"
        size="sm"
        :busy="loading"
        :disabled="!hasQuery"
        @click="retry"
      />
    </div>

    <p v-if="loading" class="gs-status" role="status">正在搜索「{{ query.trim() }}」…</p>
    <div v-else-if="error" class="gs-error" role="alert">
      <span>{{ error }}</span>
      <button class="qio-btn mini" type="button" @click="retry">重试</button>
    </div>

    <div class="gs-scroll" v-qio-scrollbar data-qio-fit data-mp-search-results data-scroll="search-results">
      <p v-if="!hasQuery" class="gs-hint">
        一次搜索同时覆盖话题、知识和实体，与当前标签页无关。
      </p>
      <template v-else-if="searchedFor && groups.total > 0">
        <p class="gs-summary mono">「{{ searchedFor }}」共 {{ groups.total }} 项结果</p>
        <section v-for="group in visibleGroups" :key="group.key" class="gs-group">
          <h3 class="gs-group-title serif">
            {{ group.title }}（{{ group.total }}）
          </h3>
          <ul class="gs-list">
            <li v-for="hit in group.hits" :key="`${hit.kind}:${hit.id}`">
              <button
                class="gs-hit"
                type="button"
                :data-search-kind="hit.kind"
                :data-search-id="hit.id"
                @click="emit('open', hit)"
              >
                <span class="gs-hit-title">{{ hit.title }}</span>
                <span class="gs-hit-note mono">{{ hit.note }}</span>
              </button>
            </li>
          </ul>
          <button
            v-if="group.remaining > 0"
            class="qio-btn mini gs-more"
            type="button"
            :data-search-more="group.key"
            @click="showMore(group.key)"
          >
            查看更多（还剩 {{ group.remaining }} 条）
          </button>
        </section>
      </template>
      <p v-else-if="showEmpty" class="gs-hint">
        「{{ searchedFor }}」没有匹配的话题、知识或实体。换个说法试试。
      </p>
    </div>
  </div>
</template>

<style scoped>
.gsearch {
  display: flex;
  flex-direction: column;
  min-height: 0;
  height: 100%;
  padding: var(--sp-3) var(--sp-4) var(--sp-4);
  gap: var(--sp-2);
}
.gs-bar {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  flex: none;
}
.gs-status,
.gs-error {
  flex: none;
  font-size: var(--fs-sm);
  line-height: 1.6;
  color: var(--text-muted);
}
.gs-error {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  flex-wrap: wrap;
  color: var(--danger);
}
.gs-scroll {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  overflow-x: clip;
  overscroll-behavior: contain;
  max-width: 100%;
}
.gs-summary {
  margin: 2px 0 var(--sp-2);
  font-size: var(--fs-xs);
  color: var(--text-muted);
  letter-spacing: 0.04em;
}
.gs-hint {
  margin: var(--sp-3) 0;
  font-size: var(--fs-sm);
  color: var(--text-muted);
  line-height: 1.7;
  overflow-wrap: anywhere;
}
.gs-group {
  margin-bottom: var(--sp-4);
}
.gs-group-title {
  margin: 0 0 var(--sp-2);
  font-size: var(--fs-sm);
  color: var(--text-secondary);
  letter-spacing: 0.02em;
}
.gs-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.gs-hit {
  display: flex;
  flex-direction: column;
  gap: 2px;
  width: 100%;
  min-width: 0;
  /* 结果行要「安静、高效」：上下 8px、两行间隔 2px，一屏能看更多条 */
  padding: 8px 10px;
  border: 1px solid transparent;
  border-radius: var(--qio-ui-radius);
  background: none;
  color: var(--text-primary);
  font: inherit;
  text-align: left;
  cursor: pointer;
  transition: background var(--dur-fast) var(--ease-1), border-color var(--dur-fast) var(--ease-1);
}
.gs-hit:hover {
  background: var(--layer-hover);
  border-color: var(--border-subtle);
}
.gs-hit:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 1px;
}
.gs-hit-title {
  min-width: 0;
  overflow-wrap: anywhere;
  font-size: var(--fs-base);
  color: var(--text-strong);
}
/* 第二行是补充信息：单行省略，不换行（否则行高又涨回去） */
.gs-hit-note {
  min-width: 0;
  font-size: var(--fs-xs);
  color: var(--text-muted);
  letter-spacing: 0.04em;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
/* 分批查看更多：安静的文字按钮，不抢结果本身的注意力 */
.gs-more {
  margin-top: var(--sp-2);
  color: var(--text-secondary);
}
</style>
