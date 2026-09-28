<script setup lang="ts">
/**
 * 凭据表单（首次引导与设置页**共用同一个组件**）。
 *
 * 基础区只有四件事：选厂商、填 API Key、确认用途（默认「主对话」）、保存。
 * 地址 / 协议 / 模型 / 名称 / 用量上限 / 自定义标签都在「高级设置」里，默认收起；
 * 内部标识只读展示，不要求用户输入。
 *
 * 逻辑全部在 `services/credentials.ts`（`useCredentialForm`），这个文件只负责
 * 渲染与把用户动作转进去 —— 两处入口因此不可能出现「默认值/提示语不一样」。
 */
import { computed, onBeforeUnmount } from "vue";
import type { CredentialMeta, VerifyReport } from "../../services/api";
import {
  USAGE_OPTIONS,
  usageLabel,
  usageSummary,
  useCredentialForm,
  type CredentialMode,
} from "../../services/credentials";
import QCombo from "../ui/QCombo.vue";
import QInput from "../ui/QInput.vue";
import QNumber from "../ui/QNumber.vue";

const props = withDefaults(
  defineProps<{
    mode: CredentialMode;
    initial?: CredentialMeta | null;
    /** onboarding 里表单更窄，且需要「保存没通过验证时留在原地看原因」 */
    variant?: "modal" | "onboarding";
    /**
     * 是否自带「取消 / 保存」这一行。
     *
     * 首次引导那一步把保存并进流程主按钮（一个按钮同时完成保存与前进），
     * 所以隐藏自己那一行，由父组件通过 `submit()` 触发。
     *
     * 必须给默认值 true：布尔 prop 缺省时 Vue 会把它强制成 `false`，
     * 于是「不传 = 隐藏按钮」——那正是这里最不希望发生的事。
     */
    showActions?: boolean;
  }>(),
  { showActions: true, initial: null },
);
const emit = defineEmits<{
  saved: [payload: { credential: CredentialMeta | null; report: VerifyReport | null; mode: CredentialMode }];
  cancel: [];
}>();

const form = useCredentialForm({
  mode: props.mode,
  initial: props.initial ?? null,
  onSaved: (payload) => emit("saved", payload),
});

const useModelCombo = computed(() => form.models.value.length > 0);
const modelOptions = computed(() =>
  form.models.value.map((m) => ({ value: m, label: m })),
);
const keyId = computed(() => props.initial?.key_id ?? "（保存后自动生成）");
/**
 * 版式类名必须与弹窗自己的 `.modal` 区分开：子组件根元素会继承父组件的
 * scoped 作用域属性，如果这里也叫 `modal`，弹窗的 `.modal { padding/width }`
 * 会命中表单根节点，表单被撑到弹窗宽度而横向溢出。
 */
const layoutClass = computed(() => `layout-${props.variant ?? "modal"}`);
const saveLabel = computed(() => {
  if (props.mode === "rotate") return form.busy.value ? "保存中…" : "保存";
  return form.primaryLabel.value;
});
const showRetry = computed(() => form.status.value?.kind === "warn" && Boolean(form.savedKeyId.value));

// 给父组件（首次引导）用的最小接口：点主按钮 = 提交这个表单
defineExpose({
  submit: () => form.submit(),
  retry: () => form.retry(),
  busy: form.busy,
  touched: form.touched,
});

onBeforeUnmount(() => form.dispose());
</script>

<template>
  <form class="cred-form" :class="layoutClass" @submit.prevent="form.submit()">
    <div class="field">
      <span class="label">服务厂商</span>
      <QCombo
        :options="form.providerOptions.value"
        :model-value="form.form.providerId"
        placeholder="选择厂商"
        @update:model-value="form.chooseProvider"
      />
      <p v-if="form.providersError.value" class="hint err">{{ form.providersError.value }}</p>
      <p v-else-if="form.destination.value" class="hint destination">{{ form.destination.value }}</p>
      <p v-if="form.hintText.value" class="hint">{{ form.hintText.value }}</p>
      <p v-if="form.fieldError.value?.field === 'provider'" class="hint err">
        {{ form.fieldError.value.message }}
      </p>
    </div>

    <div class="field">
      <label class="label" for="cred-secret">API Key</label>
      <div class="secret-row">
        <QInput
          id="cred-secret"
          :model-value="form.form.secret"
          :type="form.showSecret.value ? 'text' : 'password'"
          mono
          autocomplete="off"
          spellcheck="false"
          :placeholder="mode === 'rotate' ? '粘贴新的 API Key' : mode === 'edit' ? '不改就留空' : '粘贴 API Key…'"
          :error="form.fieldError.value?.field === 'secret'"
          @update:model-value="(v: string) => { form.form.secret = v; form.onSecretInput(); }"
        />
        <button
          type="button"
          class="qio-btn mini quiet btn-reveal"
          :aria-pressed="form.showSecret.value"
          @click="form.showSecret.value = !form.showSecret.value"
        >
          {{ form.showSecret.value ? "隐藏" : "显示" }}
        </button>
      </div>
      <p v-if="form.fieldError.value?.field === 'secret'" class="hint err">{{ form.fieldError.value.message }}</p>
    </div>

    <div class="field">
      <span class="label">用途</span>
      <div class="usage-row">
        <span class="usage-value">{{ usageSummary(form.tags.value) }}</span>
        <button
          type="button"
          class="qio-btn mini quiet btn-usage"
          :aria-expanded="form.usageOpen.value"
          @click="form.usageOpen.value = !form.usageOpen.value"
        >
          {{ form.usageOpen.value ? "收起" : "修改" }}
        </button>
      </div>
      <!-- 展开/收起用真实的行高过渡（0fr → 1fr），不是 v-show 瞬切；
           收起时同时 inert，键盘 Tab 不会掉进看不见的选项里。 -->
      <div class="collapse" :class="{ open: form.usageOpen.value }" :inert="form.usageOpen.value ? undefined : true">
        <div class="collapse-clip">
          <div class="usage-panel">
            <div class="tag-grid">
              <button
                v-for="option in USAGE_OPTIONS"
                :key="option.value"
                type="button"
                class="tag-chip"
                :class="{ on: form.tags.value.includes(option.value) }"
                @click="form.toggleTag(option.value)"
              >
                {{ option.label }}
              </button>
            </div>
            <p class="hint">「主对话」= QIO 平时和你说话时用的模型。</p>
          </div>
        </div>
      </div>
      <p v-if="form.fieldError.value?.field === 'tags'" class="hint err">{{ form.fieldError.value.message }}</p>
    </div>

    <button
      type="button"
      class="advanced-toggle"
      :aria-expanded="form.advanced.value"
      @click="form.advanced.value = !form.advanced.value"
    >
      <span>{{ form.advanced.value ? "收起高级设置" : "高级设置" }}</span>
      <span class="chev" :class="{ open: form.advanced.value }" aria-hidden="true">⌄</span>
    </button>

    <div class="collapse" :class="{ open: form.advanced.value }" :inert="form.advanced.value ? undefined : true">
      <div class="collapse-clip">
        <div class="advanced">
        <div class="field">
          <span class="label">
            模型
            <span class="label-note">（预设里的模型名只是推荐值，真正能不能用看保存后的验证）</span>
          </span>
          <QCombo
            v-if="useModelCombo"
            :options="modelOptions"
            :model-value="form.form.model"
            placeholder="选择模型"
            @update:model-value="(v: string) => (form.form.model = v)"
          />
          <QInput
            v-else
            id="cred-model"
            :model-value="form.form.model"
            mono
            placeholder="填写模型名称"
            :error="form.fieldError.value?.field === 'model'"
            @update:model-value="(v: string) => (form.form.model = v)"
          />
          <p v-if="form.modelsNote.value" class="hint">{{ form.modelsNote.value }}</p>
          <p v-if="form.fieldError.value?.field === 'model'" class="hint err">{{ form.fieldError.value.message }}</p>
        </div>

        <div class="field">
          <label class="label" for="cred-endpoint">服务地址</label>
          <QInput
            id="cred-endpoint"
            :model-value="form.form.endpoint"
            mono
            placeholder="https://…"
            :error="form.fieldError.value?.field === 'endpoint'"
            @update:model-value="(v: string) => { form.form.endpoint = v; form.refreshModels(); }"
          />
          <p v-if="form.fieldError.value?.field === 'endpoint'" class="hint err">{{ form.fieldError.value.message }}</p>
        </div>

        <div class="field">
          <label class="label" for="cred-note">显示名称</label>
          <QInput
            id="cred-note"
            :model-value="form.form.note"
            placeholder="例如：主力 DeepSeek Key"
            @update:model-value="(v: string) => (form.form.note = v)"
          />
        </div>

        <div class="field">
          <span class="label">
            用量上限
            <span class="label-note">（单位 token；留空 = 不限制）</span>
          </span>
          <QNumber
            :model-value="form.form.budget"
            :min="1"
            mono
            placeholder="留空 = 不限制"
            label="用量上限"
            @update:model-value="(v: number | null) => (form.form.budget = v)"
          />
        </div>

        <div class="field">
          <label class="label" for="cred-kind">
            连接协议
            <span class="label-note">（只有自定义服务通常需要改）</span>
          </label>
          <select id="cred-kind" v-model="form.form.kind" class="qio-input proto">
            <option value="openai">OpenAI 兼容接口</option>
            <option value="anthropic">Anthropic 接口</option>
          </select>
        </div>

        <div class="field">
          <label class="label" for="cred-custom-tag">自定义用途标签</label>
          <div class="tag-add">
            <QInput
              id="cred-custom-tag"
              :model-value="form.customTag.value"
              mono
              placeholder="回车添加"
              @update:model-value="(v: string) => (form.customTag.value = v)"
              @keyup.enter.prevent="form.addCustomTag()"
            />
            <button type="button" class="qio-btn mini" @click="form.addCustomTag()">添加</button>
          </div>
          <div v-if="form.tags.value.length" class="tag-selected">
            <button
              v-for="tag in form.tags.value"
              :key="tag"
              type="button"
              class="tag-chip on sel"
              @click="form.toggleTag(tag)"
            >
              {{ usageLabel(tag) }} ×
            </button>
          </div>
        </div>

        <div class="field">
          <span class="label">内部标识（只读）</span>
          <p class="readonly mono">{{ keyId }}</p>
        </div>

        <div v-if="form.targetChanged.value" class="field target-warning">
          <p class="hint err">
            你改了服务地址或连接协议：这把 Key 会被发送到新的地址。确认无误再保存。
          </p>
          <label class="check">
            <input v-model="form.form.confirmTarget" type="checkbox" />
            <span>我确认要把这把 Key 发送到新的地址</span>
          </label>
          <p v-if="form.fieldError.value?.field === 'target'" class="hint err">
            {{ form.fieldError.value.message }}
          </p>
        </div>
        </div>
      </div>
    </div>

    <div v-if="form.status.value" class="status-area" aria-live="polite">
      <p class="qio-feedback" :class="form.status.value.kind">
        <strong>{{ form.status.value.title }}</strong>
        <span>{{ form.status.value.message }}</span>
      </p>
      <details v-if="form.status.value.detail" class="tech mono">
        <summary>技术详情</summary>
        <p>{{ form.status.value.detail }}</p>
      </details>
      <button
        v-if="showRetry"
        type="button"
        class="qio-btn mini btn-retry"
        :disabled="form.busy.value"
        @click="form.retry()"
      >
        重试验证
      </button>
    </div>

    <p class="progress mono" role="status" aria-live="polite">{{ form.progress.value }}</p>

    <div v-if="props.showActions" class="actions">
      <button type="button" class="qio-btn btn-cancel" @click="form.cancel(); emit('cancel')">取消</button>
      <button
        type="submit"
        class="qio-btn primary btn-submit"
        :aria-busy="form.busy.value"
        :disabled="form.busy.value"
        @click.prevent="form.submit()"
      >
        {{ saveLabel }}
      </button>
    </div>
  </form>
</template>

<style scoped>
.cred-form { display: flex; flex-direction: column; gap: var(--sp-3); }
.field { display: flex; flex-direction: column; gap: 6px; }
.label { font-size: var(--fs-xs); color: var(--text-secondary); }
.label-note { color: var(--text-muted); }
.hint { margin: 0; font-size: var(--fs-xs); color: var(--text-muted); line-height: 1.5; }
.hint.err { color: var(--danger); }
.destination { color: var(--text-secondary); }
.secret-row { display: flex; gap: var(--sp-2); align-items: center; }
.secret-row > :first-child { flex: 1; min-width: 0; }
.usage-row { display: flex; align-items: center; justify-content: space-between; gap: var(--sp-2); }
.usage-value { font-size: var(--fs-base); color: var(--text-primary); }
.usage-panel { display: flex; flex-direction: column; gap: 6px; padding-top: 6px; }
/* 折叠区：真实高度过渡（0fr → 1fr），收起时不占位、不拦截、不参与 Tab */
.collapse {
  display: grid; grid-template-rows: 0fr;
  transition: grid-template-rows var(--mo-2-in) var(--ease-2);
}
.collapse.open { grid-template-rows: 1fr; }
.collapse-clip { overflow: hidden; min-height: 0; }
.collapse:not(.open) .collapse-clip { opacity: 0; }
.collapse-clip { transition: opacity var(--mo-2-in) var(--ease-2); }
.tag-grid { display: flex; flex-wrap: wrap; gap: var(--sp-2); }
.tag-chip {
  font-family: var(--sans); font-size: var(--fs-xs); padding: 4px 12px; border-radius: var(--r-pill);
  border: 1px solid var(--border-strong); background: transparent; color: var(--text-secondary); cursor: pointer;
  transition: color var(--mo-1-state) var(--ease-1), border-color var(--mo-1-state) var(--ease-1),
    background var(--mo-1-state) var(--ease-1), transform var(--mo-1-press) var(--ease-1);
}
.tag-chip:hover { color: var(--text-strong); }
.tag-chip:active { transform: translateY(var(--press-shift)); }
.tag-chip.on { color: var(--text-strong); border-color: var(--accent); background: var(--accent-soft); }
.tag-chip.sel { font-size: var(--fs-xs); }
.advanced-toggle {
  display: flex; align-items: center; justify-content: space-between; gap: var(--sp-2);
  background: none; border: none; border-top: 1px solid var(--border-subtle); padding: var(--sp-3) 0 0;
  color: var(--text-secondary); font-size: var(--fs-sm); cursor: pointer; text-align: left;
}
.advanced-toggle:hover { color: var(--text-strong); }
.advanced-toggle .chev { transition: transform var(--mo-1-state) var(--ease-1); }
.advanced-toggle .chev.open { transform: rotate(180deg); }
.advanced { display: flex; flex-direction: column; gap: var(--sp-3); padding-top: var(--sp-3); }
.proto {
  height: 36px; width: 100%; background: var(--bg-inset); border: 1px solid var(--border-subtle);
  border-radius: var(--r-md); color: var(--text-primary); padding: 0 var(--sp-3); font-size: var(--fs-base);
}
.readonly { margin: 0; font-size: var(--fs-sm); color: var(--text-secondary); }
.target-warning { padding: var(--sp-3); border: 1px solid var(--warning-soft); background: var(--warning-soft); border-radius: var(--r-md); }
.check { display: flex; gap: var(--sp-2); align-items: flex-start; font-size: var(--fs-sm); color: var(--text-primary); }
.tag-add { display: flex; gap: var(--sp-2); }
.tag-add > :first-child { flex: 1; }
.tag-selected { display: flex; flex-wrap: wrap; gap: 6px; }
.status-area { display: flex; flex-direction: column; gap: 6px; align-items: flex-start; }
.status-area .qio-feedback { flex-direction: column; gap: 2px; align-items: flex-start; }
.status-area .qio-feedback strong { font-weight: 600; }
.tech { font-size: var(--fs-xs); color: var(--text-muted); max-width: 100%; }
.tech p { margin: 4px 0 0; word-break: break-all; }
.progress { margin: 0; min-height: 14px; font-size: var(--fs-xs); color: var(--text-muted); }
.actions { display: flex; justify-content: flex-end; gap: var(--sp-2); margin-top: var(--sp-1); }
/* 标题由调用方给（弹窗有标题栏、引导页有步骤标题），表单不重复一遍 */
.cred-form.layout-onboarding { gap: var(--sp-2); }
</style>
