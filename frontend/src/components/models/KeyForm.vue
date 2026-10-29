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
import { computed, reactive, ref, watch } from "vue";
import QCombo from "../ui/QCombo.vue";
import QIcon from "../ui/QIcon.vue";
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
/** 模型列表请求序号（F09）：换服务商 / 地址 / Key 后，旧请求的结果不得写回。 */
let modelSeq = 0;
/** 最近一次新建提交的内容签名与请求身份（F13：同内容重试复用、内容变了才换）。 */
let lastSubmit: { signature: string; requestId: string } | null = null;

/** 这次「获取模型」请求到底属于哪一份表单配置（只用于比较，不含其他状态）。 */
function modelSignature(): string {
  return JSON.stringify([
    form.providerId,
    endpointValue.value,
    kindValue.value,
    form.secret.trim(),
  ]);
}

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
  // 换服务商 = 换发送目标：在途的获取请求不再属于这份配置，先作废再清展示（F09）。
  modelSeq += 1;
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
  // 换了 Key 也说明「之前那次发现不能代表这次」：在途请求作废并清空模型结果。
  modelSeq += 1;
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
  const token = ++modelSeq;
  const signature = modelSignature();
  try {
    const reply = await api.listCredentialModels({
      endpoint,
      kind: kindValue.value,
      secret: form.secret.trim(),
    });
    // F09：等待期间换了服务商 / 地址 / 协议 / Key —— 这份结果不属于当前配置，
    // 不能更新模型列表、默认选择和获取状态。
    if (token !== modelSeq || signature !== modelSignature()) return;
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
    if (token !== modelSeq || signature !== modelSignature()) return;
    error.value = readableError(exc);
  } finally {
    // 「获取中」必须结束：同一个表单同一时刻只允许一个获取请求（按钮在 discovering 时禁用）。
    discovering.value = false;
  }
}

// 高级选项里直接改地址 / 协议同样是换发送目标：作废在途请求并清掉旧列表。
watch(
  () => [form.endpoint, form.kind],
  () => {
    modelSeq += 1;
    if (fetched.value) {
      models.value = [];
      selectedModel.value = "";
      fetched.value = false;
      modelsNote.value = "连接目标变了，请重新获取模型";
    }
  },
);

function resolvedModel(): string {
  if (form.manualModel.trim()) return form.manualModel.trim();
  if (selectedModel.value) return selectedModel.value;
  return provider.value?.suggested_model?.trim() ?? "";
}

async function submit() {
  // F13：提交中的重复点击 / 回车不得再发一次保存（否则会凭空多一条连接）。
  if (busy.value) return;
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
  if (busy.value) return;
  busy.value = true;
  try {
    if (props.mode === "create") {
      const model = resolvedModel();
      if (!endpointValue.value) throw new Error("请选择服务商或填写服务地址");
      if (!model) throw new Error("请选择或填写模型名称");
      const payload: Record<string, unknown> = {
        provider: isCustom.value ? "custom" : form.providerId,
        secret: form.secret.trim(),
        endpoint: endpointValue.value,
        kind: kindValue.value,
        default_model: model,
        tags: legacyTags.value,
        note: form.note.trim() || provider.value?.name || undefined,
        budget: form.budget,
      };
      // F13：同一个表单内容的重试必须复用同一个请求身份（后端据此返回同一条连接；
      // 客户端丢了应答也不会新建第二条）。只有实际提交内容变化才换新身份。
      const signature = JSON.stringify(payload);
      if (!lastSubmit || lastSubmit.signature !== signature) {
        lastSubmit = {
          signature,
          requestId: "cred-" + Date.now().toString(36) + Math.random().toString(36).slice(2, 8),
        };
      }
      const reply = await api.createCredential({
        ...payload,
        client_request_id: lastSubmit.requestId,
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
    <!-- create 的「选服务商 → 填 Key → 获取模型 → 保存」由字段与按钮本身说清楚，不再复述一遍；
         rotate / edit 保留一句「这次改什么、不改什么」的边界说明（重新填 Key 是硬规则）。 -->
    <p v-if="mode !== 'create'" class="lead">
      {{ mode === "rotate" ? "只替换这把 Key 的密钥；地址与协议不变。" : "改服务地址或连接协议需要重新填 Key 并确认。" }}
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
      <label class="field__label">用途</label>
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
      高级选项
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
      <!--
        自绘勾选框：设置页不允许出现系统复选框外观（原生 input type=checkbox 在
        不同平台长得不一样，也拿不到统一焦点/主题）。语义仍用 role=checkbox +
        aria-checked，键盘 Space/Enter 与点击同效。
      -->
      <button
        v-if="targetChanged"
        type="button"
        class="confirm"
        role="checkbox"
        :aria-checked="form.confirmTarget"
        data-testid="confirm-target-change"
        @click="form.confirmTarget = !form.confirmTarget"
      >
        <span class="confirm__box" :class="{ 'is-on': form.confirmTarget }" aria-hidden="true">
          <QIcon v-if="form.confirmTarget" name="check" :size="11" />
        </span>
        <span>我确认把发送目标改成 {{ targetLabel }}，并已重新填写这把 Key</span>
      </button>
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
/*
 * KeyForm 渲染在 QModal 里，而 QModal Teleport 到 body —— 设置页的 `.qio-settings` 作用域
 * 覆盖到不了这里。所以弹窗内的共享原语在本表单作用域内就地改写（只影响这个表单，
 * 其它页面与共享组件保持原样）：
 *   输入 / 下拉 / 步进 = 透明 + 中性底线（聚焦只把底线转 accent）；
 *   按钮 = 次级纯文字（hover 一层柔光）、主操作实心玫红、无边框、高度 ≥34px；
 *   下拉菜单 = 轻玻璃浮层 + 1px 中性边界 + 层级阴影。
 */
.key-form { display: flex; flex-direction: column; gap: 14px; text-align: left; }
.lead { margin: 0; font-size: 11.5px; color: var(--text-muted); line-height: 1.6; }
.field { display: flex; flex-direction: column; gap: 6px; }
.field__label { font-size: 11.5px; color: var(--text-secondary); }
.field__row { display: flex; align-items: center; gap: 8px; justify-content: space-between; }
/* 行内按钮不参与伸缩：否则输入框吃满宽度会把「添加」挤成两行 */
.field__row :deep(.qio-input) { flex: 1 1 auto; min-width: 0; }
.field__row :deep(.qio-btn) { flex: none; }
.field__hint { margin: 0; font-size: 10px; color: var(--text-muted); line-height: 1.5; }
.secret-row { display: flex; align-items: center; gap: 6px; }
.secret-row :deep(.qio-input) { flex: 1; }

/* ---- 输入框：透明 + 中性底线，不画常驻闭合框 ---- */
.key-form :deep(.qio-input) {
  height: 34px;
  padding: 4px 2px;
  background: none;
  border: 0;
  border-bottom: 1px solid var(--border-subtle);
  border-radius: 0;
  font-size: 13.5px;
}
.key-form :deep(.qio-input.mono) { font-size: 12.5px; }
.key-form :deep(.qio-input:hover) { border-bottom-color: var(--border-strong); background: none; }
.key-form :deep(.qio-input:focus) { border-bottom-color: var(--accent); background: none; box-shadow: none; }
.key-form :deep(.qio-input:focus-visible) { outline: 2px solid var(--focus-ring); outline-offset: 3px; border-radius: 4px; }
.key-form :deep(.qio-input.err) { border-bottom-color: var(--danger); }
.key-form :deep(.qio-input:disabled) { opacity: .5; }

/* ---- 下拉（服务商 / 模型 / 协议）：同样透明 + 中性底线 ---- */
.key-form :deep(.qio-combo-trigger),
.key-form :deep(.qio-select) {
  height: 34px;
  padding: 0 2px;
  background: none;
  border: 0;
  border-bottom: 1px solid var(--border-subtle);
  border-radius: 0;
  font-size: 13.5px;
}
.key-form :deep(.qio-combo-trigger:hover),
.key-form :deep(.qio-select:hover) { border-bottom-color: var(--border-strong); background: none; }
.key-form :deep(.qio-combo-trigger:focus-visible),
.key-form :deep(.qio-select:focus-visible) {
  border-bottom-color: var(--accent);
  box-shadow: none;
  outline: 2px solid var(--focus-ring);
  outline-offset: 3px;
  border-radius: 4px;
}
.key-form :deep(.qio-select:focus) { background: none; box-shadow: none; border-bottom-color: var(--accent); }

/* ---- 下拉浮层：轻玻璃 + 1px 中性边界 + 层级阴影（14px 圆角） ---- */
.key-form :deep(.qio-select-menu),
.key-form :deep(.qio-combo-menu) {
  background: var(--glass-bg-strong);
  border: 1px solid var(--border-subtle);
  border-radius: var(--qio-ui-radius);
  box-shadow: var(--elev-floating);
  backdrop-filter: blur(var(--glass-blur)) saturate(var(--glass-sat));
  -webkit-backdrop-filter: blur(var(--glass-blur)) saturate(var(--glass-sat));
}
.key-form :deep(.qio-select-menu .opt),
.key-form :deep(.qio-combo-opt) { border-radius: 0; }
.key-form :deep(.qio-select-menu .opt:hover),
.key-form :deep(.qio-combo-opt.hl) { background: var(--layer-hover); color: var(--text-strong); }
.key-form :deep(.qio-select-menu .opt.sel),
.key-form :deep(.qio-combo-opt.sel) { color: var(--link); background: none; }
.key-form :deep(.qio-combo-search) { background: none; border-bottom: 1px solid var(--border-subtle); }

/* ---- 数字步进：去掉包围数值与按钮的灰壳，只留数值、按钮与底线 ---- */
.key-form :deep(.q-number) {
  height: 34px;
  background: none;
  border: 0;
  border-bottom: 1px solid var(--border-subtle);
  border-radius: 0;
  overflow: visible;
}
.key-form :deep(.q-number:focus-within) { background: none; border-bottom-color: var(--accent); box-shadow: none; }
.key-form :deep(.q-number .q-number-steps) { border-left: 0; }
.key-form :deep(.q-number .step.up) { border-bottom: 0; }
.key-form :deep(.q-number .step) { width: 26px; color: var(--text-muted); }
.key-form :deep(.q-number .step:hover:not(:disabled)) { background: none; color: var(--accent); }
.key-form :deep(.q-number .q-number-unit) { padding-right: 4px; }

/* ---- 按钮：次级＝纯文字（hover 起一层柔光，不画药丸）；主操作＝实心玫红、无边框 ---- */
.key-form :deep(.qio-btn) {
  height: 32px;
  padding: 0 10px;
  background: none;
  border: 0;
  color: var(--text-secondary);
  font-size: 12.5px;
  white-space: nowrap;
}
.key-form :deep(.qio-btn:hover) { background: var(--layer-hover); color: var(--text-strong); }
.key-form :deep(.qio-btn:active) { background: var(--accent-soft); }
.key-form :deep(.qio-btn.primary) {
  height: 34px;
  padding: 0 18px;
  background: var(--accent);
  color: var(--on-accent);
  font-size: 13px;
}
.key-form :deep(.qio-btn.primary:hover) { background: var(--accent-hover); color: var(--on-accent); }

/* ---- 用途 chip：透明文字，选中 = 品牌色文字 + 4px 圆点，不画胶囊外壳 ---- */
.chips { display: flex; gap: 2px 4px; flex-wrap: wrap; }
.chip {
  position: relative;
  display: inline-flex; align-items: center; gap: 4px;
  border: 0; background: none; border-radius: 0;
  color: var(--text-secondary); font: inherit; font-size: 11.5px;
  padding: 5px 8px 5px 14px; cursor: pointer;
  transition: color var(--mo-1-state) var(--ease-1);
}
.chip::before {
  content: ""; position: absolute; left: 3px; top: 50%; width: 4px; height: 4px; margin-top: -2px;
  border-radius: 50%; background: var(--accent);
  opacity: 0; transform: scale(.6);
  transition: opacity var(--mo-1-state) var(--ease-1), transform var(--mo-1-state) var(--ease-1);
}
.chip:hover { color: var(--text-strong); }
.chip:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; border-radius: 4px; }
.chip.is-on { color: var(--link); font-weight: 600; }
.chip.is-on::before { opacity: 1; transform: scale(1); }
.chip__x { border: none; background: none; color: inherit; cursor: pointer; font-size: 12px; line-height: 1; padding: 0; }

/* ---- 高级选项：只用一条顶部分隔线分组，不套灰底容器 ---- */
.advanced-toggle {
  align-self: flex-start; border: none; background: none; padding: 4px 0;
  font: inherit; font-size: 11.5px; color: var(--text-muted); cursor: pointer;
  text-decoration: underline dotted; text-underline-offset: 3px;
}
.advanced-toggle:hover { color: var(--text-strong); }
.advanced-toggle:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 3px; border-radius: 4px; }
.advanced {
  display: flex; flex-direction: column; gap: 12px;
  padding: 14px 0 0; border: 0; border-top: 1px solid var(--border-subtle);
  border-radius: 0; background: none;
}

/* 自绘勾选框：窄方块 + 勾（不做成圆点，避免被读成单选） */
.confirm {
  display: flex; align-items: flex-start; gap: 8px; font-size: 11.5px; color: var(--warning);
  line-height: 1.5; background: none; border: none; padding: 0; text-align: left;
  cursor: pointer; font-family: var(--sans);
}
.confirm:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 3px; border-radius: 4px; }
.confirm__box {
  flex: 0 0 auto; width: 16px; height: 16px; margin-top: 1px;
  display: inline-flex; align-items: center; justify-content: center;
  border-radius: 4px; border: 1px solid var(--border-strong);
  background: none; color: var(--on-accent);
  transition: background var(--mo-1-state) var(--ease-1), border-color var(--mo-1-state) var(--ease-1);
}
.confirm__box.is-on { background: var(--warning); border-color: var(--warning); }
.target-note { font-size: 10.5px; color: var(--text-muted); display: flex; gap: 6px; flex-wrap: wrap; }
.actions { display: flex; justify-content: flex-end; gap: 8px; padding-top: 2px; }
</style>
