<script setup lang="ts">
/**
 * 全局搜索（契约 §3.2 / A 交付 §5）。
 *
 * 三条硬要求：
 * 1. **范围与当前标签页无关**：一次搜索覆盖话题 / 知识 / 实体，结果按来源分组；
 * 2. **真实数据**：三个来源都是后端全量接口，各自并行取回再按关键词匹配
 *    （不构造演示数据、不前端拼假条目）；
 * 3. **来源各自诚实**：每个来源单独维护 loading / ok / error —— 一个来源失败
 *    只让那一个来源就地报错并可单独重试，其余来源的结果照常可用；只有全部来源
 *    都成功且命中为 0 才是「总空态」。
 *
 * 旧响应绝不能覆盖当前关键词：每次检索自增序号，每个来源还带自己的请求序号，
 * 落结果前做「请求序号 + 关键词 + 来源」三重校验（快速换词、单源重试、顶部刷新
 * 都可能让旧请求晚到）。
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
  buildEntityHits,
  buildKnowledgeHits,
  buildTopicHits,
  SEARCH_GROUP_LIMIT,
  type SearchHit,
} from "../../planet/manageViews";

/** 三个来源（= 结果分组的键；顺序即界面顺序） */
type SourceKey = "topics" | "knowledge" | "entities";
const SOURCE_KEYS: readonly SourceKey[] = ["topics", "knowledge", "entities"] as const;
const SOURCE_TITLES: Record<SourceKey, string> = { topics: "话题", knowledge: "知识", entities: "实体" };

/** 单个来源的状态：加载中 / 成功 / 失败（idle = 还没有搜索） */
type SourceStatus = "idle" | "loading" | "ok" | "error";
interface SourceState {
  status: SourceStatus;
  /** 失败原因（只在 status === "error" 时有意义） */
  error: string;
  /** 当前关键词下该来源的**完整**命中列表（展示层再分批，不在这里截断） */
  hits: SearchHit[];
}

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

const query = ref("");
/** 已经出结果的关键词（与输入框里的实时值区分开） */
const searchedFor = ref("");
const sources = reactive<Record<SourceKey, SourceState>>({
  topics: { status: "idle", error: "", hits: [] },
  knowledge: { status: "idle", error: "", hits: [] },
  entities: { status: "idle", error: "", hits: [] },
});
const inputRef = ref<{ $el?: HTMLInputElement } | null>(null);

/** 整次检索序号：换关键词 / 顶部刷新都自增，旧的一整次响应全部作废 */
let seq = 0;
/** 每个来源自己的请求序号：单源重试只作废这一个来源的旧响应，不影响其他来源 */
const sourceSeq: Record<SourceKey, number> = { topics: 0, knowledge: 0, entities: 0 };
let timer = 0;

interface SourceToken {
  /** 整次检索序号 */
  seq: number;
  /** 发起时的关键词 */
  keyword: string;
  /** 来源（三重校验的第三项） */
  source: SourceKey;
  /** 该来源自己的请求序号 */
  req: number;
}

/**
 * 三重校验：请求序号 + 关键词 + 来源（外加该来源自己的请求序号）。
 * 任何一项对不上都说明这是旧响应 / 别的来源的响应，一律丢弃。
 */
function accept(key: SourceKey, token: SourceToken): boolean {
  return (
    token.seq === seq &&
    token.keyword === searchedFor.value &&
    token.source === key &&
    token.req === sourceSeq[key]
  );
}

/**
 * 每类当前展示到第几条（F11：分页只看展示层，命中列表与总数始终是完整的）。
 * 换关键词时重置——展开状态属于「这一次搜索」，不能带到下一次。
 */
const shown = reactive<Record<SourceKey, number>>({
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
function showMore(kind: SourceKey): void {
  const all = sources[kind].hits;
  shown[kind] = Math.min(all.length, shown[kind] + SEARCH_GROUP_LIMIT);
}

const hasQuery = computed(() => query.value.trim().length > 0);
/** 只要还有来源在加载就说明这次检索没跑完（顶部刷新按钮据此显示 busy） */
const loading = computed(() => SOURCE_KEYS.some((key) => sources[key].status === "loading"));
/** 当前关键词下**成功来源**的真实命中总数（失败来源不贡献数字） */
const totalHits = computed(() => SOURCE_KEYS.reduce((sum, key) => sum + sources[key].hits.length, 0));
const allSucceeded = computed(() => SOURCE_KEYS.every((key) => sources[key].status === "ok"));
/** 总空态只在「全部来源都成功且命中为 0」时出现：失败不能冒充没有结果 */
const showEmpty = computed(
  () => !loading.value && searchedFor.value !== "" && allSucceeded.value && totalHits.value === 0,
);

/** 按来源顺序给出「就地」块：失败来源显示错误与重试，成功来源显示分组结果 */
const blocks = computed(() =>
  SOURCE_KEYS.map((key) => {
    const state = sources[key];
    const all = state.hits;
    const limit = Math.min(shown[key], all.length);
    return {
      key,
      title: SOURCE_TITLES[key],
      status: state.status,
      error: state.error,
      total: all.length,
      hits: all.slice(0, limit),
      remaining: all.length - limit,
    };
  }),
);

/** 取回单个来源并按当前关键词本地匹配（三个接口都是全量数据源） */
async function loadSource(key: SourceKey, keyword: string): Promise<SearchHit[]> {
  switch (key) {
    case "topics":
      return buildTopicHits((await api.listTopics()).topics, keyword);
    case "knowledge":
      return buildKnowledgeHits((await api.listKnowledge()).knowledge, keyword);
    case "entities":
      return buildEntityHits((await api.listEntities()).entities, keyword);
  }
}

/**
 * 跑单个来源：只有通过三重校验的响应才允许落到界面。
 * 失败时清掉该来源的旧命中（旧关键词的结果不能挂在失败状态下面冒充当前结果）。
 */
async function runSource(key: SourceKey, keyword: string, options: { resetHits: boolean }): Promise<void> {
  const req = ++sourceSeq[key];
  const token: SourceToken = { seq, keyword, source: key, req };
  const state = sources[key];
  if (options.resetHits) state.hits = [];
  state.status = "loading";
  state.error = "";
  try {
    const hits = await loadSource(key, keyword);
    if (!accept(key, token)) return;
    state.hits = hits;
    state.status = "ok";
  } catch (e) {
    if (!accept(key, token)) return;
    state.hits = [];
    state.status = "error";
    state.error = (e as Error).message || "未知错误";
  }
}

/** 整次检索：三个来源并行跑，各自落各自的成功 / 失败状态 */
async function runSearch(raw: string): Promise<void> {
  const keyword = raw.trim();
  seq += 1;
  if (!keyword) {
    searchedFor.value = "";
    for (const key of SOURCE_KEYS) {
      sourceSeq[key] += 1;
      sources[key].status = "idle";
      sources[key].error = "";
      sources[key].hits = [];
    }
    resetShown();
    return;
  }
  // 换关键词 = 新一代搜索：上一关键词的结果与展开状态都不能留在界面上
  const keywordChanged = keyword !== searchedFor.value;
  searchedFor.value = keyword;
  if (keywordChanged) resetShown();
  await Promise.all(SOURCE_KEYS.map((key) => runSource(key, keyword, { resetHits: keywordChanged })));
}

function scheduleSearch(raw: string): void {
  window.clearTimeout(timer);
  const wait = Math.max(0, props.debounceMs);
  if (wait === 0) {
    void runSearch(raw);
    return;
  }
  timer = window.setTimeout(() => void runSearch(raw), wait);
}

watch(query, (value) => scheduleSearch(value));

/** 顶部刷新：用当前关键词重跑**全部**来源（不是只补失败的那一个） */
function retry(): void {
  void runSearch(query.value);
}

/** 单源重试：只重跑这一个来源，其余来源的结果保持原样可用 */
function retrySource(key: SourceKey): void {
  const keyword = searchedFor.value;
  if (!keyword) return;
  void runSource(key, keyword, { resetHits: true });
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
        size="sm"
        :busy="loading"
        :disabled="!hasQuery"
        @click="retry"
      />
    </div>

    <p v-if="loading" class="gs-status" role="status">正在搜索「{{ searchedFor }}」…</p>

    <div class="gs-scroll" v-qio-scrollbar data-qio-fit data-mp-search-results data-scroll="search-results">
      <p v-if="searchedFor && totalHits > 0" class="gs-summary mono">「{{ searchedFor }}」共 {{ totalHits }} 项结果</p>
      <!-- 按来源「就地」呈现：失败来源只影响自己（错误 + 单源重试），其余结果照常可点 -->
      <template v-for="block in blocks" :key="block.key">
        <div
          v-if="block.status === 'error'"
          class="gs-source-error"
          role="alert"
          :data-search-source-error="block.key"
        >
          <span class="gs-source-error-text">{{ block.title }}搜索失败：{{ block.error }}</span>
          <button
            class="qio-btn mini"
            type="button"
            :data-search-source-retry="block.key"
            @click="retrySource(block.key)"
          >
            重试
          </button>
        </div>
        <section
          v-else-if="block.hits.length > 0"
          class="gs-group"
          :data-search-source="block.key"
          :data-source-status="block.status"
        >
          <h3 class="gs-group-title serif">{{ block.title }}（{{ block.total }}）</h3>
          <ul class="gs-list">
            <li v-for="hit in block.hits" :key="`${hit.kind}:${hit.id}`">
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
            v-if="block.remaining > 0"
            class="qio-btn mini gs-more"
            type="button"
            :data-search-more="block.key"
            @click="showMore(block.key)"
          >
            查看更多（还剩 {{ block.remaining }} 条）
          </button>
        </section>
      </template>
      <p v-if="showEmpty" class="gs-hint">
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
.gs-status {
  flex: none;
  font-size: var(--fs-sm);
  line-height: 1.6;
  color: var(--text-muted);
}
/* 单来源失败：就在这个来源该出现的位置说明原因，并给出只重试它自己的按钮 */
.gs-source-error {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  flex-wrap: wrap;
  margin-bottom: var(--sp-3);
  padding: 8px 10px;
  border: 1px solid var(--border-danger);
  border-radius: var(--qio-ui-radius);
  font-size: var(--fs-sm);
  line-height: 1.6;
  color: var(--danger);
}
.gs-source-error-text {
  min-width: 0;
  overflow-wrap: anywhere;
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
