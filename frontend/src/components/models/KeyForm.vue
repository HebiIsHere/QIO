<script setup lang="ts">
/**
 * Key 接入表单（正常流程 + 高级兼容入口）。
 *
 * 正常流程只有三步：选服务商（或接受本地前缀提示）→ 填 API Key → 获取模型 → 保存。
 * 正常情况下**不需要**填地址、协议、模型名 —— 这些收进默认收起的高级入口。
 *
 * 诚实边界：
 * - 本地前缀提示只是「看起来像谁家」，不联网、不等于验证成功；有歧义时让用户自己选；
 * - 获取模型 ≠ 验证可用：列表来自服务商，能不能调用要保存后按模型验证；
 * - 改地址 / 改协议 = 换了发送目标，必须重新填 Key 并显式确认后才生效（与后端规则一致）。
 */
import { computed, reactive, ref } from "vue";
import QCombo from "../ui/QCombo.vue";
import QIconButton from "../ui/QIconButton.vue";
import QInput from "../ui/QInput.vue";
import QNumber from "../ui/QNumber.vue";
import QSelect from "../ui/QSelect.vue";
import { api, type CredentialMeta, type ProviderPreset } from "../../services/api";
import { keyHint, readableError } from "../../services/credentials";

const props = defineProps<{
  providers: ProviderPreset[];
  mode: "create" | "edit" | "rotate";
  initial?: CredentialMeta | null;
}>();
const emit = defineEmits<{
  saved: [credentialId: string];
  cancel: [];
}>();

const PURPOSE_OPTIONS = [
  { usageKey: "main-chat", label: "主对话", legacyTags: ["main-loop"] },
  { usageKey: "vision", label: "看图 / 视觉", legacyTags: ["vision"] },
  { usageKey: "video", label: "视频", legacyTags: ["video"] },
  { usageKey: "audio", label: "语音", legacyTags: ["audio"] },
  { usageKey: "research", label: "研究", legacyTags: ["research"] },
  { usageKey: "subagent", label: "子任务", legacyTags: ["subagent"] },
  { usageKey: "embedding", label: "向量 / 嵌入", legacyTags: ["embedding"] },
];

const form = reactive({
  providerId: props.initial?.provider_id ?? (props.mode === "create" ? "" : ""),
  secret: "",
  endpoint: props.initial?.endpoint ?? "",
  kind: props.initial?.kind ?? "openai",
  note: props.initial?.note ?? "",
  manualModel: "",
  budget: props.initial?.budget ?? null,
  confirmTarget: false,
});
const purposes = ref<string[]>(
  props.initial
    ? PURPOSE_OPTIONS.filter((option) =>
        option.legacyTags.some((tag) => (props.initial?.tags ?? []).includes(tag)),
      ).map((option) => option.usageKey)
    : ["main-chat"],
);
const customTags = ref<string[]>(
  (props.initial?.tags ?? []).filter(
    (tag) => !PURPOSE_OPTIONS.some((option) => option.legacyTags.includes(tag)),
  ),
);
const customTagDraft = ref("");

// 高级入口一律默认收起（含编辑态）：正常流程不要求用户面对地址 / 协议 / 模型名。
const advanced = ref(false);
const showSecret = ref(false);
const busy = ref(false);
const discovering = ref(false);
const error = ref("");
const notice = ref("");
const models = ref<string[]>([]);
const modelsNote = ref("");
const selectedModel = ref<string>("");
const fetched = ref(false);

const provider = computed<ProviderPreset | null>(
  () => props.providers.find((item) => item.id === form.providerId) ?? null,
);
const isCustom = computed(() => provider.value?.category === "custom" || form.providerId === "");
const endpointValue = computed(() =>
  (form.endpoint || provider.value?.base_url || "").trim(),
);
const kindValue = computed(() => (form.kind || provider.value?.kind || "openai").trim());
const hint = computed(() => {
  if (props.mode !== "create" || !form.secret.trim()) return null;
  const hit = keyHint(form.secret, props.providers);
  if (!hit) return null;
  return hit.confident
    ? "看起来像 " + hit.preset.name + " 的 Key（只是本地提示，未验证）"
    : "这个前缀多家共用，请自己确认服务商（只是本地提示）";
});
const targetLabel = computed(() => {
  if (isCustom.value) return form.endpoint.trim() || "未填写服务地址";
  return provider.value?.base_url ?? "";
});
const targetChanged = computed(() => {
  if (props.mode === "create") return false;
  const initial = props.initial;
  if (!initial) return false;
  return (
    (form.endpoint.trim() || initial.endpoint || "") !== (initial.endpoint || "") ||
    (form.kind || initial.kind || "openai") !== (initial.kind || "openai")
  );
});
const needsSecret = computed(
  () => props.mode !== "edit" || targetChanged.value,
);
const title = computed(() =>
  props.mode === "edit" ? "编辑连接" : props.mode === "rotate" ? "更换 API Key" : "添加 Key",
);

function chooseProvider(id: string) {
  form.providerId = id;
  const preset = props.providers.find((item) => item.id === id);
  if (!preset) return;
  if (preset.base_url) form.endpoint = preset.base_url;
  form.kind = preset.kind || "openai";
  // 换服务商 = 换发送目标：之前获取的模型列表不再属于这个地址，必须清掉。
  models.value = [];
  selectedModel.value = "";
  fetched.value = false;
  modelsNote.value = "";
}

function onSecretInput() {
  // 换了 Key 也说明「之前那次发现不能代表这次」，清空模型结果，避免保存旧列表。
  if (fetched.value) {
    models.value = [];
    selectedModel.value = "";
    fetched.value = false;
    modelsNote.value = "Key 变了，请重新获取模型";
  }
}

function togglePurpose(usageKey: string) {
  const index = purposes.value.indexOf(usageKey);
  if (index >= 0) purposes.value.splice(index, 1);
  else purposes.value.push(usageKey);
}

function addCustomTag() {
  const tag = customTagDraft.value.trim();
  if (!tag || customTags.value.includes(tag)) return;
  customTags.value.push(tag);
  customTagDraft.value = "";
}

function removeCustomTag(tag: string) {
  customTags.value = customTags.value.filter((item) => item !== tag);
}

const legacyTags = computed(() => {
  const tags: string[] = [];
  for (const usageKey of purposes.value) {
    const option = PURPOSE_OPTIONS.find((item) => item.usageKey === usageKey);
    for (const tag of option?.legacyTags ?? []) {
      if (!tags.includes(tag)) tags.push(tag);
    }
  }
  for (const tag of customTags.value) {
    if (!tags.includes(tag)) tags.push(tag);
  }
  if (!tags.length) tags.push("main-loop"); // 后端要求至少一个用途（兼容字段）
  return tags;
});

async function fetchModels() {
  error.value = "";
  notice.value = "";
  const endpoint = endpointValue.value;
  if (!endpoint) {
    error.value = "请先选择服务商或填写服务地址";
    return;
  }
  if (!form.secret.trim()) {
    error.value = "请先填写 API Key（只有你提交后才向这个地址请求模型列表）";
    return;
  }
  discovering.value = true;
  try {
    const reply = await api.listCredentialModels({
      endpoint,
      kind: kindValue.value,
      secret: form.secret.trim(),
    });
    models.value = reply.models;
    fetched.value = true;
    if (!reply.models.length) {
      modelsNote.value =
        reply.note || "这个地址没有返回模型列表；可以在高级选项里手动填写模型名称，或保存后稍后重试。";
    } else {
      modelsNote.value = "已获取 " + reply.models.length + " 个模型；是否真的可用要保存后按模型验证。";
      if (!selectedModel.value) selectedModel.value = reply.models[0];
    }
  } catch (exc) {
    error.value = readableError(exc);
  } finally {
    discovering.value = false;
  }
}

function resolvedModel(): string {
  if (form.manualModel.trim()) return form.manualModel.trim();
  if (selectedModel.value) return selectedModel.value;
  return provider.value?.suggested_model?.trim() ?? "";
}

async function submit() {
  error.value = "";
  notice.value = "";
  if (needsSecret.value && !form.secret.trim()) {
    error.value = "请填写 API Key";
    return;
  }
  if (targetChanged.value && !form.confirmTarget) {
    error.value = "改服务地址或连接协议等于换了发送目标，请勾选确认后再保存";
    return;
  }
  await save();
}

async function save() {
  busy.value = true;
  try {
    if (props.mode === "create") {
      const model = resolvedModel();
      if (!endpointValue.value) throw new Error("请选择服务商或填写服务地址");
      if (!model) throw new Error("请选择或填写模型名称");
      const reply = await api.createCredential({
        provider: isCustom.value ? "custom" : form.providerId,
        secret: form.secret.trim(),
        endpoint: endpointValue.value,
        kind: kindValue.value,
        default_model: model,
        tags: legacyTags.value,
        note: form.note.trim() || provider.value?.name || undefined,
        budget: form.budget,
        client_request_id:
          "cred-" + Date.now().toString(36) + Math.random().toString(36).slice(2, 8),
      });
      emit("saved", reply.key_id);
      return;
    }
    const keyId = props.initial?.key_id;
    if (!keyId) throw new Error("缺少要编辑的连接标识");
    if (targetChanged.value || props.mode === "rotate") {
      await api.updateCredentialMeta(keyId, {
        endpoint: form.endpoint || props.initial?.endpoint,
        kind: kindValue.value,
        note: form.note.trim() || props.initial?.note || "",
        budget: form.budget,
        tags: legacyTags.value,
        secret: form.secret.trim() || undefined,
        confirm_reconfigure: form.confirmTarget || props.mode === "rotate",
      });
    } else {
      await api.updateCredentialMeta(keyId, {
        note: form.note.trim(),
        budget: form.budget,
        tags: legacyTags.value,
      });
    }
    emit("saved", keyId);
  } catch (exc) {
    error.value = readableError(exc);
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <form class="key-form" @submit.prevent="submit">
    <p class="lead">
      {{ mode === "create" ? "选服务商 → 填 API Key → 获取模型 → 保存。正常情况下不需要填地址、协议或模型名。" : mode === "rotate" ? "只替换这把 Key 的密钥；地址与协议不变。" : "修改显示名称、用量上限或用途；改地址 / 协议需要重新填 Key 并确认。" }}
    </p>

    <div v-if="mode === 'create'" class="field">
      <label class="field__label">服务商</label>
      <QCombo
        :options="providers.map((item) => ({ value: item.id, label: item.name, hint: item.category_label }))"
        :model-value="form.providerId"
        placeholder="选择服务商（或下面的自定义服务地址）"
        search-placeholder="搜索服务商…"
        empty-text="没有匹配的服务商"
        :busy="busy"
        @update:model-value="chooseProvider"
      />
      <p v-if="provider?.note" class="field__hint">{{ provider.note }}</p>
      <p v-if="provider?.discovery_note" class="field__hint">{{ provider.discovery_note }}</p>
      <!-- 地区 / 接入类型有差异时如实提示：不同地区是不同发送目标，不能默认猜一个。 -->
      <p v-if="provider?.regions?.length" class="field__hint">
        这个服务商区分地区（{{ provider.regions.join(" / ") }}）；当前地址是默认地区的，换地区请用高级选项里的自定义地址。
      </p>
      <p v-if="provider?.entry_types?.length" class="field__hint">
        接入类型：{{ provider.entry_types.join(" / ") }}。这里只覆盖一种；其它接入方式请用高级选项填写。
      </p>
    </div>

    <div class="field">
      <label class="field__label" for="key-secret">API Key</label>
      <div class="secret-row">
        <QInput
          id="key-secret"
          :model-value="form.secret"
          :type="showSecret ? 'text' : 'password'"
          mono
          placeholder="粘贴 API Key（只保存在本机安全凭据库）"
          aria-label="API Key"
          :disabled="busy"
          @update:model-value="form.secret = $event; onSecretInput()"
        />
        <QIconButton
          :icon="'eye'"
          :label="showSecret ? '隐藏 API Key' : '显示 API Key'"
          size="sm"
          tone="quiet"
          @click="showSecret = !showSecret"
        />
      </div>
      <p v-if="hint" class="field__hint">{{ hint }}</p>
      <p class="field__hint">
        只有你提交（保存）后，QIO 才会用这把 Key 向你选的地址请求模型或做验证；不会发给别家。
      </p>
    </div>

    <div v-if="mode !== 'rotate'" class="field">
      <label class="field__label">用途（兼容字段，可稍后在「用途分配」里细调）</label>
      <div class="chips">
        <button
          v-for="option in PURPOSE_OPTIONS"
          :key="option.usageKey"
          type="button"
          class="chip"
          :class="{ 'is-on': purposes.includes(option.usageKey) }"
          :aria-pressed="purposes.includes(option.usageKey)"
          @click="togglePurpose(option.usageKey)"
        >
          {{ option.label }}
        </button>
      </div>
    </div>

    <div class="field">
      <div class="field__row">
        <label class="field__label">模型</label>
        <button
          type="button"
          class="qio-btn mini"
          :disabled="busy || discovering"
          :aria-busy="discovering"
          @click="fetchModels"
        >
          {{ discovering ? "获取中…" : fetched ? "重新获取模型" : "获取模型" }}
        </button>
      </div>
      <QSelect
        v-if="models.length"
        :options="models.map((model) => ({ value: model, label: model }))"
        :model-value="selectedModel"
        placeholder="选择模型"
        :disabled="busy"
        @update:model-value="selectedModel = $event"
      />
      <p v-if="modelsNote" class="field__hint">{{ modelsNote }}</p>
    </div>

    <button type="button" class="advanced-toggle" :aria-expanded="advanced" @click="advanced = !advanced">
      高级选项（自定义地址 / 协议 / 手动模型名 / 显示名称 / 用量上限 / 自定义标签）
    </button>
    <div v-if="advanced" class="advanced">
      <div class="field">
        <label class="field__label" for="key-endpoint">服务地址</label>
        <QInput id="key-endpoint" :model-value="form.endpoint" mono placeholder="https://…" aria-label="服务地址" @update:model-value="form.endpoint = $event" />
      </div>
      <div class="field">
        <label class="field__label">连接协议</label>
        <QSelect
          :options="[{ value: 'openai', label: 'OpenAI 兼容接口' }, { value: 'anthropic', label: 'Anthropic 接口' }]"
          :model-value="form.kind"
          @update:model-value="form.kind = $event"
        />
      </div>
      <div class="field">
        <label class="field__label" for="key-manual-model">手动模型名称</label>
        <QInput id="key-manual-model" :model-value="form.manualModel" mono placeholder="列表里没有时手动填写" aria-label="手动模型名称" @update:model-value="form.manualModel = $event" />
      </div>
      <div class="field">
        <label class="field__label" for="key-note">显示名称</label>
        <QInput id="key-note" :model-value="form.note" placeholder="例如「工作用 DeepSeek」" aria-label="显示名称" @update:model-value="form.note = $event" />
      </div>
      <div class="field">
        <label class="field__label">累计用量上限（token，留空 = 不限）</label>
        <QNumber :model-value="form.budget" :min="0" :step="1000" unit="token" label="累计用量上限" @update:model-value="form.budget = $event" />
      </div>
      <div class="field">
        <label class="field__label">历史自定义用途标签</label>
        <div class="chips">
          <span v-for="tag in customTags" :key="tag" class="chip is-on">
            {{ tag }}
            <button type="button" class="chip__x" :aria-label="'移除 ' + tag" @click="removeCustomTag(tag)">×</button>
          </span>
        </div>
        <div class="field__row">
          <QInput :model-value="customTagDraft" placeholder="例如「写作」" aria-label="自定义用途标签" @update:model-value="customTagDraft = $event" @change="addCustomTag" />
          <button type="button" class="qio-btn mini" @click="addCustomTag">添加</button>
        </div>
      </div>
      <label v-if="targetChanged" class="confirm">
        <input type="checkbox" :checked="form.confirmTarget" @change="form.confirmTarget = ($event.target as HTMLInputElement).checked" />
        我确认把发送目标改成 {{ targetLabel }}，并已重新填写这把 Key
      </label>
      <div class="target-note">
        <span>实际连接目标：</span>
        <span class="mono">{{ targetLabel }}</span>
        <span>（{{ kindValue === "anthropic" ? "Anthropic 接口" : "OpenAI 兼容接口" }}）</span>
      </div>
    </div>

    <p v-if="error" class="qio-feedback err" role="alert">{{ error }}</p>
    <p v-if="notice" class="qio-feedback ok" role="status">{{ notice }}</p>

    <div class="actions">
      <button type="button" class="qio-btn" :disabled="busy" @click="emit('cancel')">取消</button>
      <button type="submit" class="qio-btn primary" :disabled="busy" :aria-busy="busy">
        {{ busy ? "保存中…" : title }}
      </button>
    </div>
  </form>
</template>

<style scoped>
.key-form { display: flex; flex-direction: column; gap: 12px; text-align: left; }
.lead { margin: 0; font-size: 11px; color: var(--text-muted); line-height: 1.6; }
.field { display: flex; flex-direction: column; gap: 6px; }
.field__label { font-size: 11px; color: var(--text-secondary); }
.field__row { display: flex; align-items: center; gap: 8px; justify-content: space-between; }
.field__hint { margin: 0; font-size: 10px; color: var(--text-muted); line-height: 1.5; }
.secret-row { display: flex; align-items: center; gap: 6px; }
.secret-row :deep(.qio-input) { flex: 1; }
.chips { display: flex; gap: 6px; flex-wrap: wrap; }
.chip {
  display: inline-flex; align-items: center; gap: 4px;
  border: 1px solid var(--border-subtle); background: var(--bg-inset);
  color: var(--text-secondary); font: inherit; font-size: 11px;
  padding: 3px 10px; border-radius: var(--qio-ui-radius); cursor: pointer;
}
.chip:hover { border-color: var(--border-strong); }
.chip:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 1px; }
.chip.is-on { border-color: var(--accent); color: var(--link); background: var(--accent-soft); }
.chip__x { border: none; background: none; color: inherit; cursor: pointer; font-size: 12px; line-height: 1; padding: 0; }
.advanced-toggle {
  align-self: flex-start; border: none; background: none; padding: 0;
  font: inherit; font-size: 11px; color: var(--text-muted); cursor: pointer; text-decoration: underline dotted;
}
.advanced-toggle:hover { color: var(--text-strong); }
.advanced { display: flex; flex-direction: column; gap: 10px; padding: 10px; border: 1px solid var(--border-subtle); border-radius: var(--qio-ui-radius); background: var(--bg-inset); }
.confirm { display: flex; align-items: flex-start; gap: 6px; font-size: 11px; color: var(--warning); line-height: 1.5; }
.target-note { font-size: 10.5px; color: var(--text-muted); display: flex; gap: 6px; flex-wrap: wrap; }
.actions { display: flex; justify-content: flex-end; gap: 8px; }
</style>
