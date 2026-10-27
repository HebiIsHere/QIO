<script setup lang="ts">
/**
 * 知识面板（B · 管理流程）。
 *
 * 信息架构：列表 → 点条目 → 详情 → 返回列表。
 *
 * 三条硬约束（改版要求，全部用真实数据与既有接口实现）：
 * 1. **列表不重建**：列表与详情用 v-show 切换，搜索/筛选/滚动位置由组件自己保存与恢复
 *    （返回后回到原来那一段，而不是跳回顶部）。A 用 v-show 保活面板；本组件即使被
 *    销毁重建，也能重新从服务端读到真实状态。
 * 2. **草稿化编辑**：点「修正」进入明确的草稿态，只有点「保存」才提交一次（CAS 带
 *    expected_version）；取消丢弃本次修改；保存失败保留草稿与可读错误。
 * 3. **冲突不静默覆盖**：编辑期间这条知识被别处改了（409 / 重新读到新版本），
 *    提示冲突、保留用户输入并把「服务端当前版本」摆出来，由用户决定刷新还是继续。
 *    返回列表时若有未保存修改，先走确认层，不悄悄丢掉。
 *
 * 基线能力一个都不少：分类/状态/来源筛选、搜索、新建、批准/打回、修订（新版本）、
 * 归档、标记结束/恢复、适用范围调整、关联话题跳转、依据数、版本与更新时间。
 */
import { computed, nextTick, onBeforeUnmount, ref, watch } from "vue";
import { getActivePinia } from "pinia";
import {
  api,
  conflictCurrentVersion,
  isVersionConflict,
  type KnowledgeItem,
  type TopicFingerprint,
} from "../../services/api";
import QInput from "../ui/QInput.vue";
import QSelect from "../ui/QSelect.vue";
import QConfirm from "../ui/QConfirm.vue";
import QIcon from "../ui/QIcon.vue";
import { vQioScrollbar } from "../../composables/useQioScrollbars";
import { useActionFeedback } from "../../composables/useActionFeedback";
import { usePlanetPendingStore } from "../../stores/planetPending";

const props = withDefaults(defineProps<{ focusId?: string | null }>(), { focusId: null });
const emit = defineEmits<{ "focus-topic": [topicId: string]; changed: [] }>();

const CATEGORY_LABELS: Record<string, string> = {
  user_profile: "用户画像",
  agent_self: "自我",
  goal: "目标",
  general_fact: "常识",
  tool_experience: "工具经验",
};
const STATE_LABELS: Record<string, string> = {
  draft: "草稿",
  pending_review: "待审核",
  verified: "已验证",
  active: "生效",
  expired: "过期",
  revoked: "已归档",
};

/** 状态色调统一走 .qio-state 的语义档，不各写一套徽章底色。 */
function stateTone(state: string): string {
  if (state === "active" || state === "verified") return "ok";
  if (state === "pending_review" || state === "draft") return "warn";
  return "quiet";
}

/** 事实的确定程度（epistemic_status）不是审批状态，只用来解释「这条从哪来」。 */
const EPISTEMIC_LABELS: Record<string, string> = {
  user_explicit: "用户明确",
  shared_understanding: "共同认识",
  qio_observed: "实际观测",
  qio_inference: "QIO 推测",
};
function epistemicLabel(status?: string | null): string {
  return status ? EPISTEMIC_LABELS[status] || status : "";
}

/** 反馈键：同一面板里不同条目的动作结果互相隔离。 */
function fbKey(k: KnowledgeItem | null): string {
  return k ? "k:" + k.id : "k:none";
}

// -- 列表数据与筛选 ---------------------------------------------------------

const items = ref<KnowledgeItem[]>([]);
const topics = ref<TopicFingerprint[]>([]);
const q = ref("");
const category = ref("");
const state = ref("");
const source = ref("");
const loading = ref(false);
/** 读取失败原因：失败必须自己可见，不能显示成「暂无记录」 */
const loadError = ref("");
const feedback = useActionFeedback();
const creating = ref(false);
const createForm = ref({ category: "general_fact", content: "", topic_id: "" });
const confirmId = ref<string | null>(null);
/**
 * 待处理角标 store：A 负责渲染，B 负责在写入成功后刷新。
 * 单测/画廊环境没有 pinia 时跳过（与 QConfirm / QModal 的处理一致），不改变任何业务行为。
 */
const pendingStore = getActivePinia() ? usePlanetPendingStore() : null;
function refreshPendingCount() {
  void pendingStore?.refresh();
}

/** 面板级成功提示：成功后条目会刷新甚至换行，条目内提示可能跟着消失。 */
const notice = ref("");
let noticeTimer: ReturnType<typeof setTimeout> | null = null;
function announce(text: string) {
  notice.value = text;
  if (noticeTimer) clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => {
    notice.value = "";
  }, 4200);
}

const categoryOptions = [
  { value: "", label: "全部分类" },
  ...Object.entries(CATEGORY_LABELS).map(([value, label]) => ({ value, label })),
];
const stateOptions = [
  { value: "", label: "全部状态" },
  ...Object.entries(STATE_LABELS).map(([value, label]) => ({ value, label })),
];
const SOURCE_LABELS = ["引导", "对话", "你的修正", "后台整理", "未记录"];
const sourceOptions = [
  { value: "", label: "全部来源" },
  ...SOURCE_LABELS.map((label) => ({ value: label, label })),
];
const topicOptions = computed(() => [
  { value: "", label: "不关联话题" },
  ...topics.value.map((t) => ({ value: t.topic_id, label: t.title })),
]);

const filtered = computed(() => {
  const query = q.value.trim().toLowerCase();
  return items.value.filter((k) => {
    if (category.value && k.category !== category.value) return false;
    if (state.value && k.state !== state.value) return false;
    if (source.value && k.source !== source.value) return false;
    if (query && !k.content.toLowerCase().includes(query)) return false;
    return true;
  });
});

const hasFilters = computed(() =>
  Boolean(q.value.trim() || category.value || state.value || source.value),
);

function clearFilters() {
  q.value = "";
  category.value = "";
  state.value = "";
  source.value = "";
}

// -- 列表 ↔ 详情（v-show 保留列表，返回恢复滚动位置） -------------------------

type PanelView = "list" | "detail";
const view = ref<PanelView>("list");
const activeId = ref<string | null>(null);
const rootRef = ref<HTMLElement | null>(null);
/** 打开详情前列表的滚动位置：返回时原样恢复（不重建列表、不跳顶部）。 */
let listScrollTop = 0;

const activeItem = computed<KnowledgeItem | null>(
  () => items.value.find((k) => k.id === activeId.value) ?? null,
);

// 编辑草稿
const editing = ref(false);
const draft = ref("");
const saving = ref(false);
/** 开始编辑时看到的服务端版本（CAS 基准 + 「有没有改过」的比较基准）。 */
const baseline = ref<{ content: string; version: number | null }>({ content: "", version: null });
const saveError = ref("");
/** 冲突：保留用户输入，把服务端当前版本也摆出来，由用户决定下一步。 */
const conflict = ref<{ message: string; remoteContent: string; remoteVersion: number | null } | null>(null);
/** 返回时的未保存修改确认层（沿用 QConfirm 的确认层语义与按钮层级）。 */
const pendingLeave = ref(false);

const dirty = computed(
  () => editing.value && draft.value.trim() !== baseline.value.content.trim(),
);

function resetDraftState() {
  editing.value = false;
  draft.value = "";
  saveError.value = "";
  conflict.value = null;
  pendingLeave.value = false;
}

function scrollRootTo(top: number) {
  const root = rootRef.value;
  if (!root) return;
  root.scrollTop = Math.max(0, top);
}

function openDetail(id: string) {
  if (view.value === "detail" && activeId.value === id) return;
  if (view.value === "list") listScrollTop = rootRef.value?.scrollTop ?? 0;
  resetDraftState();
  activeId.value = id;
  view.value = "detail";
  confirmId.value = null;
  void nextTick(() => scrollRootTo(0));
}

function leaveDetail() {
  resetDraftState();
  confirmId.value = null;
  view.value = "list";
  activeId.value = null;
  const top = listScrollTop;
  void nextTick(() => scrollRootTo(top));
}

function requestBack() {
  if (dirty.value) {
    pendingLeave.value = true;
    return;
  }
  leaveDetail();
}

function discardAndLeave() {
  pendingLeave.value = false;
  leaveDetail();
}

// -- 读取 -------------------------------------------------------------------

async function load() {
  loading.value = true;
  loadError.value = "";
  try {
    const r = await api.listKnowledge();
    items.value = r.knowledge;
    // 详情开着但这一行已经不在当前列表里（例如被修订成了新版本）：详情自己会说明。
    if (
      view.value === "detail" &&
      activeId.value &&
      !items.value.some((k) => k.id === activeId.value)
    ) {
      activeId.value = null;
    }
  } catch (e) {
    console.error("[knowledge] load failed:", e);
    loadError.value = "加载知识失败：" + (e as Error).message;
  } finally {
    loading.value = false;
  }
}

async function loadTopics() {
  try {
    const r = await api.listTopics();
    topics.value = r.topics;
  } catch (e) {
    console.error("[knowledge] load topics failed:", e);
  }
}

/** 成功写入后：刷新权威数据 + 让 A 的待处理角标跟着真实数量走。 */
async function afterMutation() {
  await load();
  emit("changed");
  refreshPendingCount();
}

// -- 条目动作（详情里，基线能力全部保留） ------------------------------------

async function toggleEnded(k: KnowledgeItem) {
  await feedback.run(fbKey(k), async () => {
    if (k.ended) await api.resumeKnowledge(k.id);
    else await api.endKnowledge(k.id);
    await afterMutation();
  }, {
    okText: k.ended ? "已恢复" : "已标记结束",
    failText: k.ended ? "恢复没有成功" : "标记结束没有成功",
  });
}

const scopeOptions = computed(() => [
  { value: "global", label: "全局（你）" },
  ...topics.value.map((t) => ({ value: "topic:" + t.topic_id, label: "仅话题：" + t.title })),
]);
function scopeValueOf(k: KnowledgeItem): string {
  return k.topic_id ? "topic:" + k.topic_id : "global";
}
async function changeScope(k: KnowledgeItem, value: string) {
  await feedback.run(fbKey(k), async () => {
    if (value.startsWith("topic:")) {
      await api.setKnowledgeScope(k.id, { type: "topic", topic_id: value.slice(6) });
    } else {
      await api.setKnowledgeScope(k.id, { type: "global" });
    }
    await afterMutation();
  }, { okText: "适用范围已更新", failText: "改适用范围没有成功" });
}

async function approve(k: KnowledgeItem) {
  await feedback.run(fbKey(k), async () => {
    await api.verifyKnowledge(k.id);
    await afterMutation();
  }, { okText: "已批准并生效", failText: "批准没有成功" });
}

async function reject(k: KnowledgeItem) {
  await feedback.run(fbKey(k), async () => {
    await api.rejectKnowledge(k.id);
    await afterMutation();
  }, { okText: "已打回草稿", failText: "打回没有成功" });
}

async function archive(k: KnowledgeItem) {
  confirmId.value = null;
  await feedback.run(fbKey(k), async () => {
    await api.revokeKnowledge(k.id);
    await afterMutation();
  }, { okText: "已归档", failText: "归档没有完成" });
}

// -- 草稿化编辑 -------------------------------------------------------------

function startEdit(k: KnowledgeItem) {
  editing.value = true;
  draft.value = k.content;
  baseline.value = {
    content: k.content,
    version: typeof k.version === "number" ? k.version : null,
  };
  saveError.value = "";
  conflict.value = null;
}

function cancelEdit() {
  editing.value = false;
  draft.value = "";
  saveError.value = "";
  conflict.value = null;
}

/** 409：这条知识已经不是用户看到的那一版（可能被别处修订成了新版本）。 */
async function onConflict(error: unknown) {
  const remoteVersion = conflictCurrentVersion(error);
  // 刷新权威数据：保留草稿，只更新「服务端现在是什么」。
  await load();
  const fresh = activeItem.value;
  baseline.value = {
    content: fresh?.content ?? baseline.value.content,
    // 用户看清楚之后再次保存是一次明确决定（不是静默覆盖）。
    version: typeof fresh?.version === "number" ? fresh.version : remoteVersion,
  };
  if (!fresh) {
    conflict.value = {
      message:
        "这条知识已经在别处被修订成新的当前版本，原来的这一条不再作为当前版本。你的修改还在下面；可以复制后返回列表打开新版本。",
      remoteContent: "",
      remoteVersion,
    };
    return;
  }
  conflict.value = {
    message:
      "这条知识在你编辑期间被更新过。你的修改还在，没有被覆盖；下面是服务端当前内容，确认后再决定是否再次保存。",
    remoteContent: fresh.content,
    remoteVersion: typeof fresh.version === "number" ? fresh.version : remoteVersion,
  };
}

async function saveDraft() {
  const item = activeItem.value;
  if (!item || saving.value) return;
  const content = draft.value.trim();
  if (!content) {
    saveError.value = "内容不能为空。";
    return;
  }
  saving.value = true;
  saveError.value = "";
  conflict.value = null;
  try {
    const result = await api.reviseKnowledge(item.id, content, {
      expectedVersion: baseline.value.version,
    });
    editing.value = false;
    draft.value = "";
    await afterMutation();
    // 修订会生成新的当前版本行：详情跟着走，用户不会看到「保存了但内容没变」。
    if (result?.knowledge_id && items.value.some((k) => k.id === result.knowledge_id)) {
      activeId.value = result.knowledge_id;
    }
    announce("已保存新版本。");
  } catch (e) {
    if (isVersionConflict(e)) {
      await onConflict(e);
    } else {
      // 失败保留草稿与错误：用户改完可以直接重试。
      saveError.value = "保存没有成功：" + (e as Error).message + "。草稿还在，可以重试。";
    }
  } finally {
    saving.value = false;
  }
}

/**
 * 编辑期间权威数据变了（例如同一面板里刷新到了新版本）：
 * 提示冲突但**不覆盖**草稿 —— 用户还没保存，界面上看到的仍是他的输入。
 */
watch(
  () => activeItem.value,
  (next) => {
    if (!editing.value || !next) return;
    const version = typeof next.version === "number" ? next.version : null;
    if (
      version !== null &&
      baseline.value.version !== null &&
      version !== baseline.value.version
    ) {
      conflict.value = {
        message:
          "这条知识在服务端已经有了新的版本。你的修改还在，没有被覆盖；可以查看服务端当前内容后再决定。",
        remoteContent: next.content,
        remoteVersion: version,
      };
      baseline.value = { content: next.content, version };
    }
  },
  { deep: false },
);

// -- 新建 -------------------------------------------------------------------

function openCreate() {
  creating.value = true;
  createForm.value = { category: "general_fact", content: "", topic_id: "" };
}

async function submitCreate() {
  const content = createForm.value.content.trim();
  if (!content) return;
  await feedback.run("create", async () => {
    await api.createKnowledge({
      category: createForm.value.category,
      content,
      topic_id: createForm.value.topic_id || null,
    });
    creating.value = false;
    await afterMutation();
  }, { okText: "已创建并生效", failText: "创建没有成功" });
}

// -- 生命周期 ---------------------------------------------------------------

function openFocusId() {
  const id = String(props.focusId || "");
  if (!id) return;
  if (!items.value.some((k) => k.id === id)) return;
  openDetail(id);
}

watch(
  () => props.focusId,
  (id) => {
    if (id) openFocusId();
  },
);
watch(
  () => items.value.length,
  () => openFocusId(),
);

async function bootstrap() {
  await Promise.all([load(), loadTopics()]);
  openFocusId();
  refreshPendingCount();
}

void bootstrap();

onBeforeUnmount(() => {
  if (noticeTimer) clearTimeout(noticeTimer);
});
</script>

<template>
  <div
    ref="rootRef"
    class="kpanel"
    v-qio-scrollbar
    data-qio-fit
    data-scroll="knowledge-list"
  >
    <!-- 列表（v-show 保活：返回时搜索、筛选与滚动位置都还在） -->
    <div v-show="view === 'list'" class="k-list-view" data-k-list>
      <div class="k-toolbar">
        <QInput v-model="q" placeholder="搜索知识…" class="grow" data-k-search />
        <button class="qio-btn mini k-create-open" type="button" @click="openCreate">
          <QIcon name="plus" :size="12" /> 新建
        </button>
      </div>
      <div class="row filters">
        <QSelect v-model="category" :options="categoryOptions" class="grow" data-k-filter-category />
        <QSelect v-model="state" :options="stateOptions" class="grow" data-k-filter-state />
        <QSelect v-model="source" :options="sourceOptions" class="grow k-source-filter" data-k-filter-source />
      </div>

      <form v-if="creating" class="create-form" @submit.prevent="submitCreate">
        <QSelect v-model="createForm.category" :options="categoryOptions" class="k-category" />
        <QSelect v-model="createForm.topic_id" :options="topicOptions" class="k-topic" />
        <textarea v-model="createForm.content" class="qio-input k-content" placeholder="知识内容…"></textarea>
        <div class="row">
          <button class="qio-btn mini" type="button" @click="creating = false">取消</button>
          <button
            class="qio-btn mini primary k-create-submit"
            type="submit"
            :disabled="feedback.stateOf('create') === 'busy'"
          >
            {{ feedback.stateOf("create") === "busy" ? "创建中…" : "创建" }}
          </button>
        </div>
        <p v-if="feedback.stateOf('create') === 'failed'" class="k-feedback err" role="alert">
          {{ feedback.errorOf("create") }}
        </p>
      </form>

      <div v-if="loading && !items.length" class="hint">加载中…</div>
      <!-- 失败与「没有数据」必须区分：失败给原因和重试，不伪装成空集合 -->
      <div v-else-if="loadError" class="k-load-error" role="alert">
        <span>{{ loadError }}</span>
        <button class="qio-btn mini" type="button" @click="load">重试</button>
      </div>
      <ul v-else class="k-list">
        <li
          v-for="k in filtered"
          :key="k.id"
          class="k-item qio-card qio-card--quiet qio-row"
          :data-k-id="k.id"
          role="button"
          tabindex="0"
          @click="openDetail(k.id)"
          @keydown.enter.prevent="openDetail(k.id)"
          @keydown.space.prevent="openDetail(k.id)"
        >
          <div class="k-head">
            <span class="qio-tag k-cat">{{ CATEGORY_LABELS[k.category] || k.category }}</span>
            <span class="qio-state k-state" :class="[k.state, stateTone(k.state)]">
              {{ STATE_LABELS[k.state] || k.state }}
            </span>
            <span v-if="k.ended" class="qio-state k-ended quiet">已结束</span>
            <span v-if="k.epistemic_status" class="qio-tag k-epistemic">{{ epistemicLabel(k.epistemic_status) }}</span>
            <span class="k-source">{{ k.source }}</span>
            <span class="k-scope">{{ k.scope }}</span>
            <span v-if="k.topic_name" class="k-topic-name">{{ k.topic_name }}</span>
          </div>
          <p class="k-content">{{ k.content }}</p>
        </li>
        <li v-if="!filtered.length" class="hint k-empty">
          <template v-if="items.length">没有匹配「{{ q.trim() || '当前筛选' }}」的知识记录。</template>
          <template v-else>暂无知识记录。</template>
          <button v-if="items.length" class="link k-clear-filters" type="button" @click="clearFilters">清除筛选</button>
        </li>
      </ul>
    </div>

    <!-- 详情 -->
    <div v-show="view === 'detail'" class="k-detail" data-k-detail>
      <div class="k-detail-head">
        <button class="qio-btn mini quiet k-back" type="button" data-k-back @click="requestBack">
          <QIcon name="chevron-left" :size="12" /> 返回
        </button>
        <span class="k-detail-title serif">知识详情</span>
        <span class="spacer"></span>
        <button
          v-if="activeItem && !editing"
          class="qio-btn mini k-edit-open"
          type="button"
          data-k-edit
          @click="startEdit(activeItem)"
        >修正</button>
      </div>

      <p v-if="notice" class="k-notice" role="status">{{ notice }}</p>

      <!-- 详情开着但这条已经不在当前列表（例如被合成新版本）：如实说明，并保留草稿。 -->
      <div v-if="!activeItem" class="k-card-content">
        <p class="hint k-missing">这条知识已经不在当前列表里（可能被修订成了新的当前版本，或被归档后筛掉了）。</p>
        <div v-if="editing" class="k-draft" data-k-draft>
          <label class="k-label" for="k-draft-missing">你的修改（草稿）</label>
          <textarea id="k-draft-missing" v-model="draft" class="qio-input" rows="4"></textarea>
          <div class="row">
            <button class="qio-btn mini" type="button" data-k-cancel @click="cancelEdit">丢弃草稿</button>
          </div>
        </div>
        <button class="qio-btn mini primary" type="button" @click="leaveDetail">返回列表</button>
      </div>

      <div v-else class="k-card qio-card">
        <div class="k-head">
          <span class="qio-tag k-cat">{{ CATEGORY_LABELS[activeItem.category] || activeItem.category }}</span>
          <span class="qio-state k-state" :class="[activeItem.state, stateTone(activeItem.state)]">
            {{ STATE_LABELS[activeItem.state] || activeItem.state }}
          </span>
          <span v-if="activeItem.ended" class="qio-state k-ended quiet">已结束</span>
          <span v-if="activeItem.epistemic_status" class="qio-tag k-epistemic">
            {{ epistemicLabel(activeItem.epistemic_status) }}
          </span>
        </div>

        <!-- 阅读态 / 草稿态：草稿是明确的一层，保存前不改服务端任何东西 -->
        <template v-if="editing">
          <div class="k-draft" data-k-draft>
            <label class="k-label" for="k-draft">内容（保存后才写入新版本）</label>
            <textarea id="k-draft" v-model="draft" class="qio-input k-edit" rows="5"></textarea>
            <p class="hint k-draft-hint">
              保存会在服务端生成一个新的当前版本；取消丢弃这次修改，原内容不变。
            </p>
          </div>
          <div class="row k-actions open">
            <button class="qio-btn mini quiet k-cancel" type="button" data-k-cancel @click="cancelEdit">取消</button>
            <button
              class="qio-btn mini primary k-save"
              type="button"
              data-k-save
              :disabled="saving"
              :aria-busy="saving ? 'true' : undefined"
              @click="saveDraft"
            >
              {{ saving ? "保存中…" : "保存" }}
            </button>
          </div>
          <p v-if="saveError" class="k-feedback err" role="alert">{{ saveError }}</p>
        </template>
        <template v-else>
          <p class="k-detail-content">{{ activeItem.content }}</p>
        </template>

        <!-- 冲突：不覆盖草稿，把服务端当前内容摆出来 -->
        <div v-if="conflict" class="k-conflict" role="alert">
          <p class="k-conflict-text">{{ conflict.message }}</p>
          <div v-if="conflict.remoteContent" class="k-conflict-remote">
            <span class="k-label">服务端当前内容（v{{ conflict.remoteVersion ?? "?" }}）</span>
            <p class="k-detail-content">{{ conflict.remoteContent }}</p>
          </div>
          <div class="row">
            <button class="qio-btn mini quiet" type="button" @click="load">重新读取</button>
            <button class="qio-btn mini quiet" type="button" @click="conflict = null">知道了</button>
          </div>
        </div>

        <!-- 未保存修改确认层（返回列表前；不悄悄丢） -->
        <div v-if="pendingLeave" class="k-guard" data-unsaved-guard role="alertdialog" aria-label="未保存的修改">
          <p class="k-guard-title">还有未保存的修改</p>
          <p class="k-guard-detail">返回列表会丢弃这次修改；已保存的内容不受影响。</p>
          <div class="row k-guard-actions">
            <button class="qio-btn mini" type="button" data-unsaved-keep @click="pendingLeave = false">继续编辑</button>
            <button class="qio-btn mini danger-solid" type="button" data-unsaved-discard @click="discardAndLeave">
              放弃修改并返回
            </button>
          </div>
        </div>

        <!-- 详情元数据：来源/分类/状态/适用范围/关联话题/依据数/修订与归档 -->
        <dl class="k-meta">
          <div class="k-meta-row">
            <dt>来源</dt>
            <dd class="k-source">{{ activeItem.source || "未记录" }}</dd>
          </div>
          <div class="k-meta-row">
            <dt>分类</dt>
            <dd>{{ CATEGORY_LABELS[activeItem.category] || activeItem.category }}</dd>
          </div>
          <div class="k-meta-row">
            <dt>状态</dt>
            <dd>{{ STATE_LABELS[activeItem.state] || activeItem.state }}{{ activeItem.ended ? " · 已结束" : "" }}</dd>
          </div>
          <div class="k-meta-row">
            <dt>适用范围</dt>
            <dd>
              <QSelect
                class="k-scope-select"
                :model-value="scopeValueOf(activeItem)"
                :options="scopeOptions"
                :busy="feedback.stateOf(fbKey(activeItem)) === 'busy'"
                @update:model-value="(v: string) => changeScope(activeItem!, v)"
              />
              <span class="hint">{{ activeItem.scope }}</span>
            </dd>
          </div>
          <div class="k-meta-row">
            <dt>关联话题</dt>
            <dd>
              <button
                v-if="activeItem.topic_name"
                class="link k-topic-link"
                type="button"
                @click="emit('focus-topic', activeItem.topic_id!)"
              >{{ activeItem.topic_name }}</button>
              <span v-else class="hint">未关联</span>
            </dd>
          </div>
          <div class="k-meta-row">
            <dt>依据</dt>
            <dd>
              <span v-if="activeItem.evidence_ids && activeItem.evidence_ids.length" class="k-evidence">
                {{ activeItem.evidence_ids.length }} 条真实记录
              </span>
              <span v-else class="hint">没有记录依据（多为用户手动创建）</span>
            </dd>
          </div>
          <div class="k-meta-row">
            <dt>修订</dt>
            <dd class="mono k-revise">
              v{{ activeItem.version ?? 1 }}
              <span v-if="activeItem.supersedes_id" class="hint">（由上一版修订而来）</span>
            </dd>
          </div>
          <div class="k-meta-row">
            <dt>时间</dt>
            <dd class="mono k-times">
              创建 {{ activeItem.created_at || "未记录" }} · 更新 {{ activeItem.updated_at || "未记录" }}
            </dd>
          </div>
          <div v-if="activeItem.ended_at" class="k-meta-row">
            <dt>结束于</dt>
            <dd class="mono">{{ activeItem.ended_at }}</dd>
          </div>
        </dl>

        <!-- 管理动作（阅读态才出现；编辑中只留保存/取消） -->
        <div v-if="!editing" class="row k-actions open">
          <button
            v-if="activeItem.state === 'pending_review'"
            class="qio-btn mini primary k-approve"
            type="button"
            :disabled="feedback.stateOf(fbKey(activeItem)) === 'busy'"
            @click="approve(activeItem)"
          >批准</button>
          <button
            v-if="activeItem.state === 'pending_review'"
            class="qio-btn mini k-reject"
            type="button"
            :disabled="feedback.stateOf(fbKey(activeItem)) === 'busy'"
            @click="reject(activeItem)"
          >打回</button>
          <button
            class="qio-btn mini quiet k-toggle-ended"
            type="button"
            :disabled="feedback.stateOf(fbKey(activeItem)) === 'busy'"
            @click="toggleEnded(activeItem)"
          >{{ activeItem.ended ? "恢复" : "标记结束" }}</button>
          <button
            v-if="activeItem.state !== 'revoked'"
            class="qio-btn mini quiet k-archive"
            type="button"
            :disabled="feedback.stateOf(fbKey(activeItem)) === 'busy'"
            @click="confirmId = activeItem.id"
          >归档</button>
          <span v-if="feedback.stateOf(fbKey(activeItem)) === 'ok'" class="k-feedback ok" role="status">
            {{ feedback.okTextOf(fbKey(activeItem)) }}
          </span>
        </div>
        <p v-if="feedback.stateOf(fbKey(activeItem)) === 'failed'" class="k-feedback err" role="alert">
          {{ feedback.errorOf(fbKey(activeItem)) }}
        </p>

        <!-- 归档是中等风险动作：就地确认并说明真实影响（不是一句「确定吗？」） -->
        <QConfirm
          v-if="activeItem && confirmId === activeItem.id"
          :open="true"
          variant="inline"
          tone="danger"
          title="归档这条知识？"
          detail="归档后它不再参与回答，也不会被删除：之后可以在筛选「已归档」里找到它。"
          confirm-text="归档"
          @confirm="archive(activeItem)"
          @cancel="confirmId = null"
        />
      </div>

      <div v-if="activeItem" class="k-detail-foot">
        <span class="k-id mono">{{ activeItem.id }}</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* 面板正文层：只有这一层纵向滚动（列表与详情共用同一个阅读区）。 */
.kpanel {
  display: flex; flex-direction: column; gap: 8px; padding: 0 14px 20px;
  flex: 1 1 auto; min-height: 0;
  overflow-y: auto; overflow-x: clip; overscroll-behavior: contain;
}
.k-list-view { display: flex; flex-direction: column; gap: 8px; }
.k-toolbar { display: flex; align-items: center; gap: 8px; }
.row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.qio-btn.mini { height: auto; padding: 4px 10px; font-size: 11px; }
.k-create-open { display: inline-flex; align-items: center; gap: 4px; }
.grow { flex: 1; }
.filters { margin-bottom: 4px; }
.create-form { border: 1px dashed var(--accent); border-radius: var(--qio-ui-radius); padding: 10px; display: flex; flex-direction: column; gap: 8px; }
.k-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; }
.k-item { padding: var(--sp-3); border-radius: var(--qio-ui-radius); display: flex; flex-direction: column; gap: 4px; cursor: pointer; }
.k-head { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.k-content { margin: 2px 0 0; font-size: 13.5px; line-height: 1.7; color: var(--text-primary); white-space: pre-wrap; }
.k-source, .k-scope { font-size: 11px; color: var(--text-muted); }
.k-topic-name { font-size: 11px; color: var(--link); }
.link { background: none; border: none; color: var(--link); cursor: pointer; font-size: 12px; padding: 0; }
.link:hover { text-decoration: underline; }
.hint { font-size: 12px; color: var(--text-muted); }
.k-empty { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.k-load-error {
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  padding: 8px 10px; border-radius: var(--qio-ui-radius); font-size: 12px;
  border: 1px solid var(--danger); background: var(--danger-soft); color: var(--danger);
}

/* 详情 */
.k-detail { display: flex; flex-direction: column; gap: 10px; }
.k-detail-head { display: flex; align-items: center; gap: 8px; }
.k-detail-head .spacer { flex: 1; }
.k-detail-title { font-size: 14px; color: var(--text-secondary); letter-spacing: 0.02em; }
.k-card { display: flex; flex-direction: column; gap: 10px; padding: var(--sp-4); }
.k-card-content { display: flex; flex-direction: column; gap: 8px; }
.k-detail-content { margin: 0; font-size: 14px; line-height: 1.75; color: var(--text-primary); white-space: pre-wrap; overflow-wrap: anywhere; }
.k-label { font-size: 11px; color: var(--text-secondary); letter-spacing: 0.03em; }
.k-draft { display: flex; flex-direction: column; gap: 6px; }
.k-edit { width: 100%; font-size: 13.5px; resize: vertical; }
.k-draft-hint { margin: 0; }
.k-actions { margin-top: 2px; }
.k-meta { display: flex; flex-direction: column; gap: 6px; margin: 0; padding: 10px 0 0; border-top: 1px solid var(--border-subtle); }
.k-meta-row { display: flex; gap: 10px; align-items: flex-start; }
.k-meta-row dt { flex: none; width: 64px; font-size: 11px; color: var(--text-secondary); }
.k-meta-row dd { margin: 0; min-width: 0; font-size: 12.5px; color: var(--text-primary); display: flex; flex-direction: column; gap: 4px; }
.k-meta-row dd.mono { font-family: var(--mono); font-size: 11.5px; color: var(--text-muted); overflow-wrap: anywhere; }
.k-evidence { font-size: 12px; color: var(--text-secondary); }
.k-notice { margin: 0; font-size: 12px; color: var(--text-secondary); }
.k-missing { color: var(--warning); }
.k-conflict {
  display: flex; flex-direction: column; gap: 6px; padding: 10px;
  border: 1px solid var(--warning); border-radius: var(--qio-ui-radius);
  background: var(--warning-soft); color: var(--warning);
}
.k-conflict-text { margin: 0; font-size: 12.5px; line-height: 1.65; overflow-wrap: anywhere; }
.k-conflict-remote { display: flex; flex-direction: column; gap: 4px; padding: 8px; border-radius: var(--qio-ui-radius); background: var(--bg-inset); }
.k-conflict-remote .k-detail-content { color: var(--text-primary); font-size: 13px; }
.k-guard {
  display: flex; flex-direction: column; gap: 6px; padding: 12px;
  border: 1px solid var(--border-strong); border-radius: var(--qio-ui-radius);
  background: var(--bg-inset);
}
.k-guard-title { margin: 0; font-family: var(--serif); font-size: 14px; color: var(--text-strong); }
.k-guard-detail { margin: 0; font-size: 12.5px; line-height: 1.65; color: var(--text-secondary); }
.k-guard-actions { justify-content: flex-end; }
.k-feedback { font-size: 11.5px; }
.k-feedback.ok { color: var(--success); }
.k-feedback.err { margin: 4px 0 0; color: var(--danger); }
.k-detail-foot { display: flex; justify-content: flex-end; }
.k-id { font-size: 10.5px; color: var(--text-faint); }
/* 状态底色由 .qio-state 的语义档提供，这里只保留状态名类供脚本/测试辨认 */
@media (hover: none) {
  .k-item { cursor: default; }
}
</style>
