<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { api, type KnowledgeItem, type TopicFingerprint } from "../../services/api";
import QInput from "../ui/QInput.vue";
import QSelect from "../ui/QSelect.vue";

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

const items = ref<KnowledgeItem[]>([]);
const topics = ref<TopicFingerprint[]>([]);
const q = ref("");
const category = ref("");
const state = ref("");
const loading = ref(false);
const toast = ref("");
const creating = ref(false);
const createForm = ref({ category: "general_fact", content: "", topic_id: "" });
const editingId = ref<string | null>(null);
const editDraft = ref("");

const categoryOptions = Object.entries(CATEGORY_LABELS).map(([value, label]) => ({ value, label }));
const stateOptions = Object.entries(STATE_LABELS).map(([value, label]) => ({ value, label }));
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

function showToast(text: string) {
  toast.value = text;
  window.setTimeout(() => (toast.value = ""), 2200);
}

async function load() {
  loading.value = true;
  try {
    const r = await api.listKnowledge();
    items.value = r.knowledge;
  } catch (e) {
    console.error("[knowledge] load failed:", e);
    showToast("加载知识失败");
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
  try {
    await api.verifyKnowledge(k.id);
    showToast("已批准并生效");
    await load();
  } catch (e) {
    console.error(e);
    showToast("批准失败");
  }
}

async function reject(k: KnowledgeItem) {
  try {
    await api.rejectKnowledge(k.id);
    showToast("已打回草稿");
    await load();
  } catch (e) {
    console.error(e);
    showToast("打回失败");
  }
}

async function archive(k: KnowledgeItem) {
  if (!window.confirm(`归档知识条目？\n${k.content.slice(0, 60)}`)) return;
  try {
    await api.revokeKnowledge(k.id);
    showToast("已归档");
    await load();
  } catch (e) {
    console.error(e);
    showToast("归档失败");
  }
}

function startEdit(k: KnowledgeItem) {
  editingId.value = k.id;
  editDraft.value = k.content;
}

async function saveEdit(k: KnowledgeItem) {
  const content = editDraft.value.trim();
  if (!content) return;
  try {
    await api.reviseKnowledge(k.id, content);
    editingId.value = null;
    showToast("已保存新版本");
    await load();
  } catch (e) {
    console.error(e);
    showToast("保存失败");
  }
}

function openCreate() {
  creating.value = true;
  createForm.value = { category: "general_fact", content: "", topic_id: "" };
}

async function submitCreate() {
  const content = createForm.value.content.trim();
  if (!content) return;
  try {
    await api.createKnowledge({ category: createForm.value.category, content, topic_id: createForm.value.topic_id || null });
    creating.value = false;
    showToast("已创建并生效");
    await load();
  } catch (e) {
    console.error(e);
    showToast("创建失败");
  }
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
        <button class="qio-btn mini primary k-create-submit" type="submit">创建</button>
      </div>
    </form>

    <div v-if="loading" class="hint">加载中…</div>
    <ul v-else class="k-list">
      <li v-for="k in filtered" :key="k.id" class="k-item">
        <div class="k-head">
          <span class="qio-badge k-cat">{{ CATEGORY_LABELS[k.category] || k.category }}</span>
          <span class="qio-badge k-state" :class="k.state">{{ STATE_LABELS[k.state] || k.state }}</span>
          <button
            v-if="k.topic_name"
            class="link k-topic-link"
            type="button"
            @click="emit('focus-topic', k.topic_id!)"
          >{{ k.topic_name }}</button>
        </div>
        <template v-if="editingId === k.id">
          <textarea v-model="editDraft" class="qio-input k-edit"></textarea>
          <div class="row">
            <button class="qio-btn mini" @click="editingId = null">取消</button>
            <button class="qio-btn mini primary" @click="saveEdit(k)">保存</button>
          </div>
        </template>
        <template v-else>
          <p class="k-content">{{ k.content }}</p>
          <div class="row k-actions">
            <button class="qio-btn mini k-approve" v-if="k.state === 'pending_review'" @click="approve(k)">批准</button>
            <button class="qio-btn mini k-reject" v-if="k.state === 'pending_review'" @click="reject(k)">打回</button>
            <button class="qio-btn mini" @click="startEdit(k)">修正</button>
            <button class="qio-btn mini danger k-archive" @click="archive(k)">归档</button>
          </div>
        </template>
      </li>
      <li v-if="!filtered.length && !loading" class="hint">无知识条目。</li>
    </ul>

    <div v-if="toast" class="toast" role="status">{{ toast }}</div>
  </div>
</template>

<style scoped>
.kpanel { display: flex; flex-direction: column; gap: 8px; padding: 0 14px 20px; }
.row { display: flex; align-items: center; gap: 8px; }
.qio-btn.mini { height: auto; padding: 4px 10px; font-size: 11px; border-radius: 8px; }
.qio-btn.mini.danger { color: var(--danger); border-color: var(--border-danger); }
.grow { flex: 1; }
.filters { margin-bottom: 4px; }
.create-form { border: 1px dashed var(--accent); border-radius: 10px; padding: 10px; display: flex; flex-direction: column; gap: 8px; }
.k-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; }
.k-item { border: 1px solid var(--border-subtle); border-radius: 10px; padding: 10px; background: var(--bg-inset); }
.k-head { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.k-content { margin: 6px 0 0; font-size: 13px; color: var(--text-primary); white-space: pre-wrap; }
.k-actions { margin-top: 6px; flex-wrap: wrap; }
.link { background: none; border: none; color: var(--link); cursor: pointer; font-size: 12px; padding: 0; }
.link:hover { text-decoration: underline; }
.k-state.pending_review { background: var(--warning-soft); color: var(--warning); }
.k-state.active { background: var(--success-soft); color: var(--success); }
.hint { font-size: 12px; color: var(--text-muted); }
.toast { position: fixed; top: 20px; right: 20px; z-index: 60; padding: 10px 16px; border-radius: 12px; font-size: 12.5px; background: var(--bg-surface); border: 1px solid var(--border-strong); color: var(--text-primary); }
</style>
