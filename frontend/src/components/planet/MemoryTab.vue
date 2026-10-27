<script setup lang="ts">
/**
 * 记忆标签页（契约 §3.5 / A 交付 §4）。
 *
 * 三层浏览，每一层都保留上一层的状态（列表滚动、搜索词、打开的条目）：
 *   话题列表（进行中 / 已结束分组 + 搜索 + 结束/恢复）
 *     → 话题概览（名称/概览、讨论记录预览、关联知识、关联实体、对话当前话题）
 *       → 完整讨论记录（真实 api.fragmentMessages 分页，随时回到概览）
 *
 * 两个必须分清的「话题」：
 * - **对话当前话题** = 会话起点（session.currentTopicId），是真实生效的对话位置；
 * - **正在浏览的话题** = 这里打开的那一个，纯浏览，**不改变**起点。
 * 只有按「继续讨论 / 从这里继续」才走既有的锚点切换机制（emit continue，由 PlanetView 执行），
 * 浏览、搜索、切标签页都不碰锚点。
 *
 * 关联知识 / 关联实体点击不在这里自己开面板：emit 出去由管理区切标签页并用 focusId 定位，
 * 这样「记忆页的状态」原样留在原地（v-show 保活）。
 */
import { computed, onMounted, ref, watch } from "vue";
import { api, type TopicDetail, type TopicDetailFragment, type TopicFingerprint } from "../../services/api";
import QInput from "../ui/QInput.vue";
import QIconButton from "../ui/QIconButton.vue";
import { vQioScrollbar } from "../../composables/useQioScrollbars";
import { useSessionStore } from "../../stores/session";
import {
  formatWhen,
  groupTopics,
  hasMoreMessages,
  isCurrentTopic,
  mergeMessagePage,
  type PagedMessages,
} from "../../planet/manageViews";

/** 一页取多少条讨论记录：够读一段，不把整段历史一次拉回来 */
const RAW_PAGE = 20;

interface RawMessage {
  id: string;
  role: string;
  content: string;
  created_at: string;
}

const props = withDefaults(
  defineProps<{
    /** 星球上选中的话题（用户点了话题点/列表行）→ 打开它的概览 */
    focusTopicId?: string | null;
    /** 聚焦请求代号：同一个话题再次被请求也要重新打开（不会因为值没变而失效） */
    focusTicket?: number;
    /** 锚点切换的进行中 / 失败原因由父级给（真实状态只有一个来源） */
    continueBusy?: boolean;
    continueError?: string;
  }>(),
  { focusTopicId: null, focusTicket: 0, continueBusy: false, continueError: "" },
);

const emit = defineEmits<{
  /** 列表里选了一个话题：星球同步聚焦（只是浏览） */
  "topic-select": [topicId: string];
  /** 继续讨论 / 从这里继续：走既有的锚点切换机制（父级执行） */
  continue: [payload: { topicId: string; fragmentId: string | null }];
  "focus-knowledge": [knowledgeId: string];
  "focus-entity": [entityId: string];
  "topics-changed": [];
}>();

const session = useSessionStore();

type Mode = "list" | "overview" | "record";
const mode = ref<Mode>("list");

const topics = ref<TopicFingerprint[]>([]);
const listLoading = ref(false);
const listError = ref("");
const query = ref("");
const actionError = ref("");
const busyTopicId = ref<string | null>(null);

const groups = computed(() => groupTopics(topics.value, query.value));

const openedTopicId = ref<string | null>(null);
const detail = ref<TopicDetail | null>(null);
const detailLoading = ref(false);
const detailError = ref("");
let detailSeq = 0;

const selectedFragmentId = ref<string | null>(null);
const page = ref<PagedMessages<RawMessage>>({ items: [], total: 0 });
const pageLoading = ref(false);
const pageError = ref("");
let pageSeq = 0;

const ROLE_LABELS: Record<string, string> = { user: "你", assistant: "QIO", tool: "工具", system: "系统" };
function roleLabel(role: string): string {
  return ROLE_LABELS[role] ?? role;
}

const currentTopicLabel = computed(() => {
  const id = session.currentTopicId;
  if (!id) return "未设置（按最新位置继续）";
  const name = session.topicName || topics.value.find((t) => t.topic_id === id)?.title || "已设置的话题";
  const fragment = session.anchorFragment?.title;
  return fragment ? `${name} · ${fragment}` : name;
});

const browsingIsCurrent = computed(() => isCurrentTopic(openedTopicId.value, session.currentTopicId));

/** 概览顶部的「讨论记录预览」：最新一段的摘要（没有摘要就不编造内容） */
const previewFragment = computed<TopicDetailFragment | null>(() => {
  const list = detail.value?.fragments ?? [];
  return list.length ? list[list.length - 1] : null;
});

const detailFacts = computed(() => {
  const d = detail.value;
  if (!d) return "";
  const parts: string[] = [];
  if (d.last_activity) parts.push(`最近活动 ${formatWhen(d.last_activity)}`);
  parts.push(`${d.fragments.length} 个片段`);
  if (typeof d.message_count === "number") parts.push(`${d.message_count} 条消息`);
  return parts.join(" · ");
});

const selectedFragment = computed<TopicDetailFragment | null>(() => {
  const id = selectedFragmentId.value;
  if (!id) return null;
  return detail.value?.fragments.find((f) => f.fragment_id === id) ?? null;
});
const selectedIsHistoric = computed(() => Boolean(selectedFragment.value?.closed_at));

const continueHint = computed(() => {
  if (mode.value === "record") {
    return selectedFragmentId.value
      ? "从这里继续不会修改历史片段：会在该位置之后接续新的内容。"
      : "先选一段讨论记录，再从那个位置继续。";
  }
  return browsingIsCurrent.value
    ? "这是对话当前的话题；浏览不会改变对话位置。"
    : "继续讨论会把对话起点切换到这个话题的最新位置，然后回到对话。";
});

const continueLabel = computed(() => {
  if (props.continueBusy) return "切换中…";
  if (mode.value === "record" && selectedFragmentId.value) {
    return `从这里继续（${selectedIsHistoric.value ? "历史位置" : "当前片段"}）`;
  }
  return browsingIsCurrent.value ? "回到对话继续" : "继续讨论（切换到这个话题）";
});

const canContinue = computed(() => {
  if (props.continueBusy) return false;
  if (!openedTopicId.value) return false;
  if (mode.value === "record" && !selectedFragmentId.value) return false;
  return true;
});

async function loadTopics() {
  listLoading.value = true;
  listError.value = "";
  try {
    const r = await api.listTopics();
    topics.value = r.topics;
  } catch (e) {
    // 失败必须自己可见：不拿空列表冒充「没有话题」
    listError.value = `读取话题失败：${(e as Error).message}`;
  } finally {
    listLoading.value = false;
  }
}

/** 结束 / 恢复话题：结束不是删掉（它仍可被搜到、被恢复） */
async function toggleEnded(topic: TopicFingerprint) {
  if (busyTopicId.value) return;
  busyTopicId.value = topic.topic_id;
  actionError.value = "";
  try {
    if (topic.ended) await api.resumeTopic(topic.topic_id);
    else await api.endTopic(topic.topic_id);
    await loadTopics();
    emit("topics-changed");
  } catch (e) {
    actionError.value = `操作失败，话题状态没有改变：${(e as Error).message}`;
  } finally {
    busyTopicId.value = null;
  }
}

async function loadDetail(topicId: string) {
  const seq = ++detailSeq;
  detailLoading.value = true;
  detailError.value = "";
  try {
    const d = await api.getTopicDetail(topicId);
    if (seq !== detailSeq) return; // 期间又打开了别的话题：丢弃过期响应
    detail.value = d;
  } catch (e) {
    if (seq !== detailSeq) return;
    detail.value = null;
    detailError.value = `读取话题概览失败：${(e as Error).message}`;
  } finally {
    if (seq === detailSeq) detailLoading.value = false;
  }
}

async function openTopic(topicId: string) {
  if (!topicId) return;
  if (openedTopicId.value === topicId && mode.value !== "list") {
    mode.value = "overview";
    return;
  }
  openedTopicId.value = topicId;
  selectedFragmentId.value = null;
  page.value = { items: [], total: 0 };
  pageError.value = "";
  mode.value = "overview";
  await loadDetail(topicId);
}

/** 列表点击：先让星球同步聚焦（浏览），再打开概览 */
function pickTopic(topicId: string) {
  emit("topic-select", topicId);
  void openTopic(topicId);
}

function backToList() {
  mode.value = "list";
}
function backToOverview() {
  mode.value = "overview";
}

async function openRecord() {
  mode.value = "record";
  if (!detail.value) return;
  const list = detail.value.fragments;
  if (!selectedFragmentId.value && list.length) {
    selectedFragmentId.value = list[list.length - 1].fragment_id;
  }
  page.value = { items: [], total: 0 };
  pageError.value = "";
  if (selectedFragmentId.value) await loadPage(0);
}

async function selectFragment(fragmentId: string) {
  if (selectedFragmentId.value === fragmentId) return;
  selectedFragmentId.value = fragmentId;
  await loadPage(0);
}

async function loadPage(offset: number) {
  const fragmentId = selectedFragmentId.value;
  if (!fragmentId) return;
  const seq = ++pageSeq;
  pageLoading.value = true;
  pageError.value = "";
  try {
    const r = await api.fragmentMessages(fragmentId, offset, RAW_PAGE);
    if (seq !== pageSeq) return; // 期间切了片段：旧响应不得覆盖
    page.value = mergeMessagePage(page.value, r, offset);
  } catch (e) {
    if (seq !== pageSeq) return;
    pageError.value = `读取讨论记录失败：${(e as Error).message}`;
  } finally {
    if (seq === pageSeq) pageLoading.value = false;
  }
}

const hasMore = computed(() => hasMoreMessages(page.value));

/** 继续讨论 / 从这里继续：只上报真实 id 与位置，锚点切换由父级统一执行 */
function continueDiscussion(fragmentId: string | null) {
  const topicId = openedTopicId.value;
  if (!topicId) return;
  emit("continue", { topicId, fragmentId: fragmentId ?? null });
}

watch(
  () => props.focusTicket,
  () => {
    if (props.focusTopicId) void openTopic(props.focusTopicId);
  },
  { immediate: true },
);

onMounted(() => void loadTopics());
</script>

<template>
  <div class="memory-tab">
    <!-- 1. 话题列表 -->
    <section v-show="mode === 'list'" class="mt-pane" aria-label="话题列表">
      <div class="mt-list-head">
        <QInput v-model="query" placeholder="搜索话题…" aria-label="搜索话题" />
        <QIconButton
          icon="refresh"
          label="刷新话题列表"
          description="重新读取话题列表"
          size="sm"
          :busy="listLoading"
          @click="loadTopics"
        />
      </div>
      <p v-if="actionError" class="mt-error" role="alert">{{ actionError }}</p>
      <div class="mt-scroll" v-qio-scrollbar data-qio-fit data-scroll="memory-topics">
        <p v-if="listLoading && !topics.length" class="mt-status" role="status">正在读取话题…</p>
        <div v-else-if="listError" class="mt-block" role="alert">
          <p class="mt-error-text">{{ listError }}</p>
          <button class="qio-btn mini" type="button" @click="loadTopics">重试</button>
        </div>
        <template v-else>
          <ul class="topic-list" role="listbox" aria-label="进行中的话题" data-memory-topics>
            <li
              v-for="t in groups.active"
              :key="t.topic_id"
              class="topic-item"
              :data-topic-id="t.topic_id"
              :class="{ active: t.topic_id === openedTopicId }"
              tabindex="0"
              role="option"
              :aria-selected="t.topic_id === openedTopicId"
              @click="pickTopic(t.topic_id)"
              @keydown.enter.prevent="pickTopic(t.topic_id)"
              @keydown.space.prevent="pickTopic(t.topic_id)"
            >
              <span class="name serif">{{ t.title }}</span>
              <span class="meta qio-badge">{{ t.fragment_count }} 片段</span>
              <button
                class="qio-btn mini quiet topic-toggle-ended"
                type="button"
                :disabled="busyTopicId === t.topic_id"
                @click.stop="toggleEnded(t)"
              >
                结束
              </button>
            </li>
          </ul>
          <p v-if="!groups.active.length && !groups.ended.length" class="mt-status">
            {{ query.trim() ? "没有匹配的话题。" : "还没有话题：对话开始后，这里会出现第一个话题。" }}
          </p>
          <ul v-if="groups.ended.length" class="topic-list ended-list" role="listbox" aria-label="已结束的话题">
            <!-- 分组标题不是可选项：给它 presentation，避免 listbox 里混进非 option 子节点 -->
            <li class="ended-head" role="presentation">已结束（{{ groups.ended.length }}）</li>
            <li
              v-for="t in groups.ended"
              :key="t.topic_id"
              class="topic-item"
              :data-topic-id="t.topic_id"
              :class="{ active: t.topic_id === openedTopicId }"
              tabindex="0"
              role="option"
              :aria-selected="t.topic_id === openedTopicId"
              @click="pickTopic(t.topic_id)"
              @keydown.enter.prevent="pickTopic(t.topic_id)"
            >
              <span class="name serif">{{ t.title }}</span>
              <span class="meta qio-badge">{{ t.fragment_count }} 片段</span>
              <button
                class="qio-btn mini quiet topic-resume"
                type="button"
                :disabled="busyTopicId === t.topic_id"
                @click.stop="toggleEnded(t)"
              >
                恢复
              </button>
            </li>
          </ul>
        </template>
      </div>
    </section>

    <!-- 2. 话题概览 -->
    <section v-show="mode === 'overview'" class="mt-pane" aria-label="话题概览" data-memory-overview>
      <div class="mt-pane-head">
        <button class="qio-btn quiet mini mt-back" type="button" @click="backToList">← 话题列表</button>
      </div>
      <div class="mt-scroll" v-qio-scrollbar data-qio-fit data-scroll="memory-overview">
        <p v-if="detailLoading" class="mt-status" role="status">正在读取话题概览…</p>
        <div v-else-if="detailError" class="mt-block" role="alert">
          <p class="mt-error-text">{{ detailError }}</p>
          <button class="qio-btn mini" type="button" @click="openedTopicId && loadDetail(openedTopicId)">重试</button>
        </div>
        <template v-else-if="detail">
          <h2 class="serif mt-title">{{ detail.name }}</h2>
          <p v-if="detail.summary" class="mt-summary">{{ detail.summary }}</p>
          <p class="mt-facts mono">{{ detailFacts }}</p>
          <div v-if="detail.keywords?.length" class="mt-keywords">
            <span v-for="k in detail.keywords.slice(0, 6)" :key="k" class="qio-tag">{{ k }}</span>
          </div>

          <!-- 浏览的话题 ≠ 对话当前话题：两个概念并排写清，不留歧义 -->
          <dl class="mt-identity">
            <div class="id-row">
              <dt>正在浏览</dt>
              <dd>{{ detail.name }}</dd>
            </div>
            <div class="id-row">
              <dt>对话当前话题</dt>
              <dd class="mono">{{ currentTopicLabel }}</dd>
            </div>
          </dl>

          <section class="mt-section">
            <h3 class="mt-section-title serif">讨论记录</h3>
            <p v-if="previewFragment" class="mt-preview">
              {{ previewFragment.summary || "（这一段的摘要还没生成）" }}
            </p>
            <p v-else class="mt-hint">这个话题还没有讨论记录。</p>
            <button
              v-if="detail.fragments.length"
              class="qio-btn mini mt-open-record"
              type="button"
              @click="openRecord"
            >
              查看完整讨论记录
            </button>
          </section>

          <section class="mt-section">
            <h3 class="mt-section-title serif">关联知识</h3>
            <div v-if="detail.knowledge.length" class="mt-chips">
              <button
                v-for="k in detail.knowledge"
                :key="k.id"
                class="mt-chip"
                type="button"
                :data-rel-knowledge-id="k.id"
                @click="emit('focus-knowledge', k.id)"
              >
                {{ k.content }}
              </button>
            </div>
            <p v-else class="mt-hint">这个话题还没有关联知识。</p>
          </section>

          <section class="mt-section">
            <h3 class="mt-section-title serif">关联实体</h3>
            <div v-if="detail.entities.length" class="mt-chips">
              <button
                v-for="e in detail.entities"
                :key="e.id"
                class="mt-chip"
                type="button"
                :data-rel-entity-id="e.id"
                @click="emit('focus-entity', e.id)"
              >
                {{ e.name }}
              </button>
            </div>
            <p v-else class="mt-hint">这个话题还没有关联实体。</p>
          </section>
        </template>
      </div>
      <div v-if="detail && !detailLoading" class="mt-actions">
        <p class="mt-action-hint">{{ continueHint }}</p>
        <p v-if="continueError" class="mt-error" role="alert">{{ continueError }}</p>
        <button
          class="qio-btn primary mt-continue"
          type="button"
          data-memory-continue
          :disabled="!canContinue"
          @click="continueDiscussion(null)"
        >
          {{ continueLabel }}
        </button>
      </div>
    </section>

    <!-- 3. 完整讨论记录（只读 + 分页） -->
    <section v-show="mode === 'record'" class="mt-pane" aria-label="完整讨论记录" data-memory-record>
      <div class="mt-pane-head">
        <button class="qio-btn quiet mini mt-back" type="button" @click="backToOverview">← 话题概览</button>
      </div>
      <div class="mt-scroll" v-qio-scrollbar data-qio-fit data-scroll="memory-record">
        <h2 class="serif mt-title mt-record-title">{{ detail?.name || "完整讨论记录" }}</h2>
        <p class="mt-hint">
          以下是这个话题里已经发生过的内容，只读。对话没有停在这里，也不会被改写。
        </p>
        <div v-if="detail && detail.fragments.length > 1" class="mt-fragments">
          <button
            v-for="f in detail.fragments"
            :key="f.fragment_id"
            class="mt-fragment"
            :class="{ active: f.fragment_id === selectedFragmentId }"
            type="button"
            :aria-pressed="f.fragment_id === selectedFragmentId"
            @click="selectFragment(f.fragment_id)"
          >
            <span class="mono mt-fragment-when">{{ formatWhen(f.created_at ?? f.closed_at) }}</span>
            <span class="mt-fragment-summary">{{ f.summary || "（无摘要）" }}</span>
            <span class="mono mt-fragment-count">{{ f.message_count }} 条</span>
          </button>
        </div>
        <p v-if="pageLoading && !page.items.length" class="mt-status" role="status">正在读取讨论记录…</p>
        <div v-else-if="pageError" class="mt-block" role="alert">
          <p class="mt-error-text">{{ pageError }}</p>
          <button class="qio-btn mini" type="button" @click="loadPage(page.items.length)">重试</button>
        </div>
        <template v-else>
          <ul class="raw-list">
            <li v-for="m in page.items" :key="m.id" class="raw-item" :class="m.role">
              <span class="raw-role mono">{{ roleLabel(m.role) }}</span>
              <span class="raw-content">{{ m.content }}</span>
            </li>
          </ul>
          <button
            v-if="hasMore"
            class="qio-btn mini raw-more"
            type="button"
            :disabled="pageLoading"
            @click="loadPage(page.items.length)"
          >
            {{ pageLoading ? "读取中…" : `继续读取（已读 ${page.items.length}/${page.total}）` }}
          </button>
          <p v-else-if="page.total" class="mt-hint mono">已读完这段记录（共 {{ page.total }} 条）</p>
          <p v-else-if="!selectedFragmentId" class="mt-hint">这个话题还没有讨论记录。</p>
        </template>
      </div>
      <div v-if="detail" class="mt-actions">
        <p class="mt-action-hint">{{ continueHint }}</p>
        <p v-if="continueError" class="mt-error" role="alert">{{ continueError }}</p>
        <button
          class="qio-btn primary mt-continue"
          type="button"
          data-memory-continue
          :disabled="!canContinue"
          @click="continueDiscussion(selectedFragmentId)"
        >
          {{ continueLabel }}
        </button>
      </div>
    </section>
  </div>
</template>

<style scoped>
.memory-tab {
  display: flex;
  flex-direction: column;
  min-height: 0;
  height: 100%;
}
.mt-pane {
  display: flex;
  flex-direction: column;
  min-height: 0;
  flex: 1 1 auto;
}
.mt-list-head {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  padding: var(--sp-3) var(--sp-4) var(--sp-2);
  flex: none;
}
.mt-pane-head {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  padding: var(--sp-2) var(--sp-4);
  flex: none;
}
.mt-back {
  padding-left: 0;
}
.mt-scroll {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  overflow-x: clip;
  overscroll-behavior: contain;
  max-width: 100%;
  padding: 0 var(--sp-4) var(--sp-4);
}
/* 长话题名 / 长摘要 / 长 id 一律可换行 —— 横向不溢出的根因修复 */
.mt-scroll :where(p, li, dd, dt, span, h2, h3, button) {
  overflow-wrap: anywhere;
}
.mt-title {
  margin: var(--sp-3) 0 var(--sp-2);
  font-size: var(--fs-md);
  color: var(--text-strong);
}
.mt-summary {
  margin: 0 0 var(--sp-2);
  font-size: var(--fs-sm);
  line-height: 1.75;
  color: var(--text-primary);
}
.mt-facts {
  margin: 0 0 var(--sp-2);
  font-size: var(--fs-xs);
  color: var(--text-muted);
  letter-spacing: 0.04em;
}
.mt-keywords {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2);
  margin-bottom: var(--sp-3);
}
.mt-identity {
  margin: var(--sp-2) 0 var(--sp-3);
  padding: var(--sp-2) var(--sp-3);
  border: 1px solid var(--border-subtle);
  border-radius: var(--qio-ui-radius);
  background: var(--bg-inset);
}
.mt-identity .id-row {
  display: flex;
  gap: var(--sp-2);
  font-size: var(--fs-sm);
  line-height: 1.7;
}
.mt-identity dt {
  flex: 0 0 88px;
  margin: 0;
  color: var(--text-muted);
}
.mt-identity dd {
  margin: 0;
  min-width: 0;
  color: var(--text-primary);
}
.mt-section {
  margin: var(--sp-4) 0 0;
}
.mt-section-title {
  margin: 0 0 var(--sp-2);
  font-size: var(--fs-sm);
  color: var(--text-secondary);
  letter-spacing: 0.02em;
}
.mt-preview {
  margin: 0 0 var(--sp-2);
  padding: var(--sp-2) var(--sp-3);
  border: 1px solid var(--border-subtle);
  border-radius: var(--qio-ui-radius);
  background: var(--bg-inset);
  font-size: var(--fs-sm);
  line-height: 1.7;
  color: var(--text-primary);
}
.mt-chips {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2);
}
.mt-chip {
  max-width: 100%;
  padding: 3px 10px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--qio-ui-radius);
  background: var(--bg-inset);
  color: var(--text-secondary);
  font-family: var(--sans);
  font-size: var(--fs-sm);
  text-align: left;
  cursor: pointer;
  transition: color var(--dur-fast) var(--ease-1), border-color var(--dur-fast) var(--ease-1);
}
.mt-chip:hover {
  color: var(--accent);
  border-color: var(--accent);
}
.mt-chip:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 1px;
}
.mt-status {
  margin: var(--sp-3) 0;
  font-size: var(--fs-sm);
  color: var(--text-muted);
}
.mt-hint {
  margin: var(--sp-2) 0;
  font-size: var(--fs-sm);
  color: var(--text-muted);
  line-height: 1.7;
}
.mt-block {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: var(--sp-2);
  margin: var(--sp-2) 0;
}
.mt-error-text,
.mt-error {
  margin: 0;
  font-size: var(--fs-sm);
  line-height: 1.6;
  color: var(--danger);
}
.mt-error {
  padding: 0 var(--sp-4) var(--sp-2);
}
/* ---- 话题列表 ---- */
.topic-list {
  list-style: none;
  margin: var(--sp-1) 0;
  padding: 0;
  min-width: 0;
}
.topic-item {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: var(--sp-2);
  padding: 8px 10px;
  border: 1px solid transparent;
  border-radius: var(--qio-ui-radius);
  cursor: pointer;
  font-size: var(--fs-base);
  min-width: 0;
  flex-wrap: wrap;
}
.topic-item:hover {
  background: var(--layer-hover);
}
.topic-item.active {
  background: var(--accent-soft);
  border-color: var(--accent-soft);
}
.topic-item:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 1px;
}
.topic-list .name {
  font-size: var(--fs-md);
  color: var(--text-primary);
  min-width: 0;
  overflow-wrap: anywhere;
}
.topic-list .meta {
  flex-shrink: 0;
}
.ended-list {
  margin-top: var(--sp-3);
  border-top: 1px solid var(--border-subtle);
  padding-top: var(--sp-2);
}
.ended-head {
  padding: 2px 10px;
  font-size: var(--fs-xs);
  color: var(--text-muted);
  letter-spacing: 0.04em;
}
/* ---- 完整讨论记录 ---- */
.mt-record-title {
  margin-top: var(--sp-2);
}
.mt-fragments {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
  margin: var(--sp-2) 0 var(--sp-3);
}
.mt-fragment {
  display: flex;
  align-items: baseline;
  gap: var(--sp-2);
  width: 100%;
  min-width: 0;
  padding: 6px 10px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--qio-ui-radius);
  background: var(--bg-inset);
  color: var(--text-secondary);
  font: inherit;
  font-size: var(--fs-sm);
  text-align: left;
  cursor: pointer;
}
.mt-fragment.active {
  border-color: var(--accent);
  color: var(--text-strong);
}
.mt-fragment:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 1px;
}
.mt-fragment-when {
  flex: none;
  font-size: var(--fs-xs);
  color: var(--text-muted);
}
.mt-fragment-summary {
  flex: 1 1 auto;
  min-width: 0;
  overflow-wrap: anywhere;
}
.mt-fragment-count {
  flex: none;
  font-size: var(--fs-xs);
  color: var(--text-muted);
}
.raw-list {
  list-style: none;
  margin: var(--sp-2) 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}
.raw-item {
  display: flex;
  gap: var(--sp-2);
  font-size: var(--fs-sm);
  line-height: 1.65;
}
.raw-role {
  flex: 0 0 34px;
  padding-top: 2px;
  font-size: var(--fs-xs);
  color: var(--text-muted);
  letter-spacing: 0.04em;
}
.raw-content {
  min-width: 0;
  color: var(--text-secondary);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.raw-item.assistant .raw-content {
  color: var(--text-primary);
}
.raw-more {
  align-self: flex-start;
  margin-top: var(--sp-3);
}
/* ---- 底部动作区（在正常流里，不覆盖正文） ---- */
.mt-actions {
  flex: none;
  padding: var(--sp-3) var(--sp-4) var(--sp-4);
  border-top: 1px solid var(--border-subtle);
  background: var(--bg-panel);
}
.mt-action-hint {
  margin: 0 0 var(--sp-2);
  font-size: var(--fs-sm);
  line-height: 1.6;
  color: var(--text-secondary);
}
.mt-continue {
  width: 100%;
  height: 38px;
  font-size: var(--fs-base);
}
.mt-continue:disabled {
  opacity: 0.55;
  cursor: default;
}
</style>
