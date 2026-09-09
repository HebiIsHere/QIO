<script lang="ts">
export type CredentialModalMode = "create" | "meta" | "rotate";
</script>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { identifyCredential } from "../../services/identify";
import QInput from "../../components/ui/QInput.vue";
import QNumber from "../../components/ui/QNumber.vue";
import QSelect from "../../components/ui/QSelect.vue";

const props = defineProps<{
  mode: CredentialModalMode;
  initial?: Record<string, unknown> | null;
}>();
const emit = defineEmits<{ save: [payload: Record<string, unknown>]; cancel: [] }>();

const TAG_PRESETS: { value: string; label: string }[] = [
  { value: "main-loop", label: "主对话" },
  { value: "chat", label: "通用对话" },
  { value: "code", label: "写代码" },
  { value: "subagent", label: "子任务" },
  { value: "vision", label: "看图/视觉" },
  { value: "research", label: "研究" },
];

const form = ref({
  key_id: "",
  secret: "",
  note: "",
  endpoint: "",
  default_model: "",
  budget: null as number | null,
});
const tags = ref<string[]>([]);
const customTag = ref("");
const error = ref("");
const modelOptions = ref<string[]>([]);
const identifyState = ref<"idle" | "working" | "done" | "failed">("idle");
const identifiedProvider = ref("");
let identifyTimer: ReturnType<typeof setTimeout> | null = null;

const isSecretMode = computed(() => props.mode !== "meta");
const modelSelectOptions = computed(() => modelOptions.value.map((m) => ({ value: m, label: m })));
const tagLabels = computed(() => {
  const map = new Map(TAG_PRESETS.map((p) => [p.value, p.label]));
  return tags.value.map((t) => map.get(t) ?? t);
});
const title = computed(() =>
  props.mode === "meta" ? "编辑凭据（不改密钥）" : props.mode === "rotate" ? "换钥（新建）" : "新建凭据",
);
const submitLabel = computed(() => (props.mode === "meta" ? "保存修改" : "创建凭据"));
const identifyText = computed(() =>
  identifyState.value === "working"
    ? "识别中…"
    : identifyState.value === "done"
      ? `已识别：${identifiedProvider.value}`
      : identifyState.value === "failed"
        ? "未能自动识别，请手动填写"
        : "",
);
const preview = computed(() => {
  const usage = tagLabels.value.join("、") || "（未选用途）";
  const budget = form.value.budget && form.value.budget > 0 ? `${form.value.budget} token` : "不限";
  return `用途：${usage}；预算：${budget}`;
});

function labelFor(value: string) {
  return TAG_PRESETS.find((p) => p.value === value)?.label ?? value;
}
function toggleTag(value: string) {
  const set = new Set(tags.value);
  if (set.has(value)) set.delete(value);
  else set.add(value);
  tags.value = Array.from(set);
}
function addCustomTag() {
  const value = customTag.value.trim();
  if (value && !tags.value.includes(value)) {
    tags.value = [...tags.value, value];
  }
  customTag.value = "";
}

function resetFromInitial() {
  const i = (props.initial ?? {}) as Record<string, unknown>;
  form.value = {
    key_id: String(i.key_id ?? ""),
    secret: "",
    note: String(i.note ?? ""),
    endpoint: String(i.endpoint ?? ""),
    default_model: String(i.default_model ?? ""),
    budget: (i.budget as number | null) ?? null,
  };
  tags.value = Array.isArray(i.tags) ? (i.tags as string[]) : [];
  modelOptions.value = Array.isArray(i.models) ? (i.models as string[]) : [];
  identifyState.value = "idle";
  identifiedProvider.value = "";
}

watch(
  () => form.value.secret,
  (secret) => {
    if (identifyTimer) clearTimeout(identifyTimer);
    if (!secret.trim()) {
      identifyState.value = "idle";
      modelOptions.value = [];
      return;
    }
    identifyTimer = setTimeout(async () => {
      identifyState.value = "working";
      try {
        const result = await identifyCredential(secret.trim());
        if (result.identified) {
          identifiedProvider.value = result.provider ?? "";
          form.value.endpoint = result.base_url ?? form.value.endpoint;
          form.value.default_model = result.default_model ?? form.value.default_model;
          modelOptions.value = result.models ?? [];
          identifyState.value = "done";
        } else {
          identifyState.value = "failed";
          modelOptions.value = [];
        }
      } catch (e) {
        console.error("[identify] failed:", e);
        identifyState.value = "failed";
        modelOptions.value = [];
      }
    }, 800);
  },
);

function buildPayload(): Record<string, unknown> {
  const payload: Record<string, unknown> = { tags: tags.value };
  if (form.value.note.trim()) payload.note = form.value.note.trim();
  if (form.value.endpoint.trim()) payload.endpoint = form.value.endpoint.trim();
  if (form.value.default_model.trim()) payload.default_model = form.value.default_model.trim();
  if (form.value.budget && form.value.budget > 0) payload.budget = form.value.budget;
  if (props.mode === "create") {
    if (form.value.key_id.trim()) payload.key_id = form.value.key_id.trim();
  }
  if (isSecretMode.value) payload.secret = form.value.secret;
  return payload;
}

function submit() {
  if (tags.value.length === 0) {
    error.value = "请至少选择一种用途";
    return;
  }
  if (isSecretMode.value && !form.value.secret.trim()) {
    error.value = "请先粘贴 API Key";
    return;
  }
  emit("save", buildPayload());
}

onMounted(resetFromInitial);
</script>

<template>
  <div class="modal-mask" @click.self="emit('cancel')">
    <div class="modal qio-card" role="dialog" aria-modal="true">
      <header class="modal-head">
        <h3>{{ title }}</h3>
        <button type="button" class="close" aria-label="关闭" @click="emit('cancel')">×</button>
      </header>
      <p v-if="mode === 'meta'" class="hint mono">原位编辑，不替换密钥；如需换新密钥请用「换钥」。</p>
      <p v-else-if="mode === 'rotate'" class="hint mono">换钥：将新建一条凭据（新密钥），旧凭据需手动撤销，避免双 key 并存。</p>
      <p v-if="error" class="msg err">{{ error }}</p>
      <div class="form">
        <div class="field">
          <span class="label">名称 / 备注</span>
          <QInput v-model="form.note" placeholder="可选，如 GPT-5 Studio" />
        </div>
        <div v-if="mode === 'create'" class="field">
          <span class="label">名称（key_id）</span>
          <QInput v-model="form.key_id" mono placeholder="留空自动生成" />
        </div>
        <div v-if="isSecretMode" class="field span2">
          <span class="label">API Key（只写不读）</span>
          <QInput v-model="form.secret" type="password" mono placeholder="sk-…（粘贴后自动识别）" />
        </div>

        <div class="field span2">
          <span class="label">用途标签（这把钥匙给哪些任务用，可多选/自定义）</span>
          <div class="tag-grid">
            <button
              v-for="p in TAG_PRESETS" :key="p.value" type="button"
              class="tag-chip" :class="{ on: tags.includes(p.value) }"
              @click="toggleTag(p.value)"
            >{{ p.label }}</button>
          </div>
          <div class="tag-add">
            <QInput v-model="customTag" mono placeholder="自定义用途（回车添加）" @keyup.enter="addCustomTag" />
            <button type="button" class="qio-btn" @click="addCustomTag">添加</button>
          </div>
          <div v-if="tags.length" class="tag-selected">
            <button v-for="t in tags" :key="t" class="tag-chip on sel" @click="toggleTag(t)">{{ labelFor(t) }} ×</button>
          </div>
        </div>

        <div class="field">
          <span class="label">模型端点（base_url）</span>
          <QInput v-model="form.endpoint" mono placeholder="https://api.openai.com/v1" />
        </div>
        <div class="field">
          <span class="label">默认模型</span>
          <QSelect v-if="modelOptions.length" :options="modelSelectOptions" v-model="form.default_model" />
          <QInput v-else v-model="form.default_model" mono placeholder="自动识别或手动填写" />
        </div>
        <div class="field">
          <span class="label">预算 token</span>
          <QNumber :model-value="form.budget" :min="1" mono placeholder="可选" label="预算 token" @update:model-value="(v: number | null) => (form.budget = v)" />
        </div>
        <p v-if="isSecretMode" class="identify mono" :class="identifyState">{{ identifyText }}</p>
        <p class="preview mono">保存后：{{ preview }}</p>
        <div class="actions">
          <button type="button" class="qio-btn" @click="emit('cancel')">取消</button>
          <button type="button" class="qio-btn primary btn-submit" @click="submit">{{ submitLabel }}</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.modal-mask { position: fixed; inset: 0; z-index: 200; background: var(--bg-overlay); display: flex; align-items: center; justify-content: center; }
.modal { width: min(540px, 92vw); max-height: 88vh; overflow: auto; background: var(--bg-elevated); border: 1px solid var(--border-strong); border-radius: 14px; padding: 18px 20px; color: var(--text-primary); }
.modal-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px; }
.modal-head h3 { margin: 0; font-size: 16px; color: var(--text-strong); }
.close { border: none; background: transparent; color: var(--text-muted); font-size: 20px; line-height: 1; cursor: pointer; padding: 2px 6px; border-radius: 8px; }
.close:hover { color: var(--text-strong); background: var(--accent-soft); }
.hint { color: var(--text-muted); font-size: 11px; margin: 0 0 12px; }
.form { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-top: 4px; }
.field { display: flex; flex-direction: column; gap: 6px; }
.field .label { font-size: 11px; color: var(--text-secondary); }
.field.span2 { grid-column: 1 / -1; }
.tag-grid { display: flex; flex-wrap: wrap; gap: 8px; }
.tag-chip { font-family: var(--mono); font-size: 11px; padding: 4px 12px; border-radius: 20px; border: 1px solid var(--border-strong); background: transparent; color: var(--text-secondary); cursor: pointer; transition: all .18s; }
.tag-chip:hover { border-color: var(--border-strong); color: var(--text-strong); }
.tag-chip.on { color: var(--text-strong); border-color: var(--accent); background: var(--accent-soft); }
.tag-chip.sel { font-size: 11px; }
.tag-add { display: flex; gap: 8px; margin-top: 4px; }
.tag-add > :first-child { flex: 1; }
.tag-selected { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
.actions { grid-column: 1 / -1; display: flex; justify-content: flex-end; gap: 10px; margin-top: 8px; }
.msg.err { grid-column: 1 / -1; }
.identify { grid-column: 1 / -1; font-size: 11px; color: var(--text-muted); min-height: 16px; }
.identify.done { color: var(--success); }
.identify.failed { color: var(--warning); }
.preview { grid-column: 1 / -1; font-size: 11px; color: var(--text-secondary); margin: 2px 0 0; }
</style>
