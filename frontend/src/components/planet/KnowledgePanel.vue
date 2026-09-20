<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { api, type KnowledgeItem, type TopicFingerprint } from "../../services/api";
import QInput from "../ui/QInput.vue";
import QSelect from "../ui/QSelect.vue";
import QConfirm from "../ui/QConfirm.vue";
import { useActionFeedback } from "../../composables/useActionFeedback";

const emit = defineEmits<{ "focus-topic": [topicId: string] }>();

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

/**
 * 状态色调统一走 `.qio-state` 的语义档（不再各写一套徽章底色）：
 * 生效/已验证 = ok；待审核/草稿 = warn；已归档/过期 = quiet（安静的事实，不是警报）。
 */
function stateTone(state: string): string {
  if (state === "active" || state === "verified") return "ok";
  if (state === "pending_review" || state === "draft") return "warn";
  return "quiet";
}

const items = ref<KnowledgeItem[]>([]);
const topics = ref<TopicFingerprint[]>([]);
const q = ref("");
const category = ref("");
const state = ref("");
const loading = ref(false);
/** 读取失败原因：失败必须自己可见，不能只留在 console 里、更不能显示成「无数据」 */
const loadError = ref("");
/**
 * 操作反馈统一走 `useActionFeedback`：反馈显示在对应条目里（组件级），
 * 不再用固定右上角的全局 Toast（spec 第 71~74、81~84 条）。
 */
const feedback = useActionFeedback();
const creating = ref(false);
const createForm = ref({ category: "general_fact", content: "", topic_id: "" });
const editingId = ref<string | null>(null);
const editDraft = ref("");
/**
 * 正在就地确认归档的条目 id。
 * 第四阶段：归档不再用浏览器原生 `confirm` —— 原生框说不清影响、风格也和 QIO 无关。
 */
const confirmId = ref<string | null>(null);

// 第一项是「全部」：既是关闭态可见的标签（否则用户只看到两个空白方块），
// 也是筛过之后唯一的回退路径（不再有「选了就回不到全部」的死角）。
const categoryOptions = [
  { value: "", label: "全部分类" },
  ...Object.entries(CATEGORY_LABELS).map(([value, label]) => ({ value, label })),
];
const stateOptions = [
  { value: "", label: "全部状态" },
  ...Object.entries(STATE_LABELS).map(([value, label]) => ({ value, label })),
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
    if (query && !k.content.toLowerCase().includes(query)) return false;
    return true;
  });
});

/** 当前是否有生效的筛选条件（用于区分「暂无记录」和「没有匹配」） */
const hasFilters = computed(() => Boolean(q.value.trim() || category.value || state.value));

function clearFilters() {
  q.value = "";
  category.value = "";
  state.value = "";
}

async function load() {
  loading.value = true;
  loadError.value = "";
  try {
    const r = await api.listKnowledge();
    items.value = r.knowledge;
  } catch (e) {
    console.error("[knowledge] load failed:", e);
    loadError.value = `加载知识失败：${(e as Error).message}`;
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

async function approve(k: KnowledgeItem) {
  await feedback.run(
    `k:${k.id}`,
    async () => {
      await api.verifyKnowledge(k.id);
      await load();
    },
    { okText: "已批准并生效", failText: "批准没有成功" },
  );
}

async function reject(k: KnowledgeItem) {
  await feedback.run(
    `k:${k.id}`,
    async () => {
      await api.rejectKnowledge(k.id);
      await load();
    },
    { okText: "已打回草稿", failText: "打回没有成功" },
  );
}

async function archive(k: KnowledgeItem) {
  confirmId.value = null;
  await feedback.run(
    `k:${k.id}`,
    async () => {
      await api.revokeKnowledge(k.id);
      await load();
    },
    { okText: "已归档", failText: "归档没有完成" },
  );
}

function startEdit(k: KnowledgeItem) {
  editingId.value = k.id;
  editDraft.value = k.content;
}

async function saveEdit(k: KnowledgeItem) {
  const content = editDraft.value.trim();
  if (!content) return;
  await feedback.run(
    `k:${k.id}`,
    async () => {
      await api.reviseKnowledge(k.id, content);
      editingId.value = null;
      await load();
    },
    { okText: "已保存新版本", failText: "保存没有成功，内容未改变" },
  );
}

function openCreate() {
  creating.value = true;
  createForm.value = { category: "general_fact", content: "", topic_id: "" };
}

async function submitCreate() {
  const content = createForm.value.content.trim();
  if (!content) return;
  await feedback.run(
    "create",
    async () => {
      await api.createKnowledge({
        category: createForm.value.category,
        content,
        topic_id: createForm.value.topic_id || null,
      });
      creating.value = false;
      await load();
    },
    { okText: "已创建并生效", failText: "创建没有成功" },
  );
}

onMounted(() => {
  load();
  loadTopics();
});
</script>

<template>
  <div class="kpanel">
    <div class="row">
      <QInput v-model="q" placeholder="搜索知识…" class="grow" />
      <button class="qio-btn mini k-create-open" @click="openCreate">+ 新建</button>
    </div>
    <div class="row filters">
      <QSelect v-model="category" :options="categoryOptions" class="grow" />
      <QSelect v-model="state" :options="stateOptions" class="grow" />
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

    <div v-if="loading" class="hint">加载中…</div>
    <!-- 失败与「没有数据」必须区分：失败给原因和重试，不伪装成空集合 -->
    <div v-else-if="loadError" class="k-load-error" role="alert">
      <span>{{ loadError }}</span>
      <button class="qio-btn mini" type="button" @click="load">重试</button>
    </div>
    <ul v-else class="k-list">
      <li v-for="k in filtered" :key="k.id" class="k-item qio-card qio-card--quiet">
        <div class="k-head">
          <span class="qio-tag k-cat">{{ CATEGORY_LABELS[k.category] || k.category }}</span>
          <span class="qio-state k-state" :class="[k.state, stateTone(k.state)]">{{ STATE_LABELS[k.state] || k.state }}</span>
          <button
            v-if="k.topic_name"
            class="link k-topic-link"
            type="button"
            @click="emit('focus-topic', k.topic_id!)"
          >{{ k.topic_name }}</button>
        </div>
        <template v-if="editingId === k.id">
          <textarea v-model="editDraft" class="qio-inline-edit k-edit" rows="3" aria-label="修正知识内容"></textarea>
          <div class="row k-actions open">
            <button class="qio-btn mini quiet" type="button" @click="editingId = null">取消</button>
            <button
              class="qio-btn mini primary k-save"
              type="button"
              :disabled="feedback.stateOf(`k:${k.id}`) === 'busy'"
              @click="saveEdit(k)"
            >
              {{ feedback.stateOf(`k:${k.id}`) === "busy" ? "保存中…" : "保存" }}
            </button>
          </div>
        </template>
        <template v-else>
          <p class="k-content">{{ k.content }}</p>
          <!-- 阅读态：内容与「待确认」的动作在明处，管理动作（修正/归档）退到次要位置 -->
          <div class="row k-actions">
            <button class="qio-btn mini k-approve" type="button" v-if="k.state === 'pending_review'" @click="approve(k)">批准</button>
            <button class="qio-btn mini k-reject" type="button" v-if="k.state === 'pending_review'" @click="reject(k)">打回</button>
            <button class="qio-btn mini quiet k-edit-open" type="button" @click="startEdit(k)">修正</button>
            <button
              class="qio-btn mini quiet k-archive"
              type="button"
              :disabled="feedback.stateOf(`k:${k.id}`) === 'busy'"
              @click="confirmId = k.id"
            >
              归档
            </button>
            <span v-if="feedback.stateOf(`k:${k.id}`) === 'ok'" class="k-feedback ok" role="status">
              {{ feedback.okTextOf(`k:${k.id}`) }}
            </span>
          </div>
          <p v-if="feedback.stateOf(`k:${k.id}`) === 'failed'" class="k-feedback err" role="alert">
            {{ feedback.errorOf(`k:${k.id}`) }}
          </p>
        </template>
        <!-- 归档是中等风险动作：就地确认，说明它的真实影响（不是一句「确定吗？」） -->
        <QConfirm
          v-if="confirmId === k.id"
          :open="true"
          variant="inline"
          tone="danger"
          title="归档这条知识？"
          detail="归档后它不再参与回答，也不会被删除：之后可以在筛选「已归档」里找到它。"
          confirm-text="归档"
          @confirm="archive(k)"
          @cancel="confirmId = null"
        />
      </li>
      <li v-if="!filtered.length" class="hint k-empty">
        <template v-if="items.length">没有匹配「{{ q.trim() || '当前筛选' }}」的知识记录。</template>
        <template v-else>暂无知识记录。</template>
        <button v-if="items.length" class="link k-clear-filters" type="button" @click="clearFilters">清除筛选</button>
      </li>
    </ul>

  </div>
</template>

<style scoped>
.kpanel { display: flex; flex-direction: column; gap: 8px; padding: 0 14px 20px; flex: 1 1 auto; min-height: 0; overflow-y: auto; }
.row { display: flex; align-items: center; gap: 8px; }
.qio-btn.mini { height: auto; padding: 4px 10px; font-size: 11px; border-radius: 8px; }
.qio-btn.mini.danger { color: var(--danger); border-color: var(--border-danger); }
.grow { flex: 1; }
.filters { margin-bottom: 4px; }
.create-form { border: 1px dashed var(--accent); border-radius: 10px; padding: 10px; display: flex; flex-direction: column; gap: 8px; }
.k-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; }
/* 卡片家族：骨架与内边距对齐 `.qio-card`，只在需要时覆盖（scoped 优先级更高） */
.k-item { padding: var(--sp-3); border-radius: var(--r-lg); }
.k-head { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
/* 阅读态：内容是主体（字号/行高比元数据高一级） */
.k-content { margin: 6px 0 0; font-size: 13.5px; line-height: 1.7; color: var(--text-primary); white-space: pre-wrap; }
/* 管理动作默认退场，hover / 键盘聚焦 / 触屏才出现（「默认阅读，按需编辑」） */
.k-actions {
  margin-top: 6px;
  flex-wrap: wrap;
  opacity: 0;
  transition: opacity var(--dur-fast) var(--ease-1);
}
.k-item:hover .k-actions,
.k-item:focus-within .k-actions,
.k-actions.open { opacity: 1; }
@media (hover: none) {
  .k-actions { opacity: 1; }
}
.link { background: none; border: none; color: var(--link); cursor: pointer; font-size: 12px; padding: 0; }
.link:hover { text-decoration: underline; }
/* 状态底色由 `.qio-state` 的语义档提供（ok / warn / quiet），这里不再重复定义，
   只保留 `.pending_review` / `.active` 这些「状态名」类，供脚本与测试辨认。 */
.k-edit { width: 100%; font-size: 13.5px; resize: vertical; }
.hint { font-size: 12px; color: var(--text-muted); }
.k-empty { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.k-load-error {
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  padding: 8px 10px; border-radius: 8px; font-size: 12px;
  border: 1px solid var(--danger); background: var(--danger-soft); color: var(--danger);
}
/* 操作反馈留在条目里：成功短暂、失败保留（失败比成功持久） */
.k-feedback { font-size: 11.5px; }
.k-feedback.ok { color: var(--success); }
.k-feedback.err { margin: 4px 0 0; color: var(--danger); }
</style>
