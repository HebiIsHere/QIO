<script setup lang="ts">
/**
 * 话题申请的正文（C）：当前话题 → 目标话题（switch）/ 可编辑新名称（create）
 * + 简短原因 + 两个动作（转到并回答 / 新建并回答、在当前话题回答）+ 右上角「暂时收起」。
 *
 * 边界（契约 §2.1 / §2.4）：
 * - **只**展示允许的字段：当前话题、目标/新名称、简短原因；不展示原问题/摘要/预览；
 * - 两个动作都只 emit 决定，真正的提交由 store 的唯一入口完成（不在这里发请求）；
 * - 关闭（右上角）只收起，不提交任何决定；
 * - 执行中两个动作都禁用并显示进行中；成功前不说「已切换 / 已创建」；
 * - 空名称在本地阻断并就地显示错误（不关闭、不提交）；回车不直接提交；
 * - 颜色只用 var(--*)，新控件圆角 14px（--qio-ui-radius），字体三声部。
 */
import { computed, ref, watch } from "vue";
import type { TopicApprovalItem } from "../../stores/unifiedApprovals";

const props = defineProps<{
  item: TopicApprovalItem;
  /** 用户可编辑的新话题名（create）；由 store 持有，收起 / 失败后不丢。 */
  name: string;
  busy: boolean;
  expired: boolean;
  stale: boolean;
}>();

const emit = defineEmits<{
  decide: [payload: { decision: "approve" | "reject"; edits?: { name: string } }];
  "update:name": [value: string];
  collapse: [];
  dismiss: [];
}>();

const EMPTY_NAME_ERROR = "话题名不能为空；请填写新话题名后再提交。";

const inputRef = ref<HTMLInputElement | null>(null);
const emptyNameError = ref(false);

const isCreate = computed(() => props.item.action === "create");
const currentName = computed(() => props.item.currentTopicName || "未记录");
const targetName = computed(() => props.item.targetTopicName || "目标话题未记录");
/** 申请已有结局（stale）时两个动作都不再可用，只给收口入口。 */
const actionDisabled = computed(() => props.busy || props.stale);
/** 到期后不再提供「批准」；「在当前话题回答」仍是有效选择（那是不执行）。 */
const approveDisabled = computed(() => actionDisabled.value || props.expired);
const mainLabel = computed(() => {
  if (props.busy) return "提交中…";
  return isCreate.value ? "新建并回答" : "转到并回答";
});

watch(
  () => [props.item.key, props.name] as const,
  ([, name]) => {
    if (String(name ?? "").trim()) emptyNameError.value = false;
  },
);

function onNameInput(event: Event) {
  const value = (event.target as HTMLInputElement).value;
  if (value.trim()) emptyNameError.value = false;
  emit("update:name", value);
}

function approve() {
  if (actionDisabled.value || props.expired) return;
  if (isCreate.value && !props.name.trim()) {
    // 本地阻断：不发请求、不关闭弹窗，把焦点放回输入框
    emptyNameError.value = true;
    inputRef.value?.focus();
    return;
  }
  emit("decide", {
    decision: "approve",
    ...(isCreate.value ? { edits: { name: props.name.trim() } } : {}),
  });
}

function stay() {
  if (actionDisabled.value) return;
  emit("decide", { decision: "reject" });
}
</script>

<template>
  <section
    class="topic-approval"
    data-test="topic-approval-content"
    :aria-busy="busy ? 'true' : undefined"
  >
    <button
      class="topic-close"
      type="button"
      data-test="topic-approval-close"
      aria-label="暂时收起这项话题申请（不会替你选择，也不会丢失申请）"
      @click="emit('collapse')"
    >
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path d="M7 7l10 10M17 7L7 17" />
      </svg>
    </button>

    <p class="topic-kicker mono">话题申请 · 本轮暂停，等待你的选择</p>

    <p class="topic-route" data-test="topic-route">
      <span class="topic-route__block">
        <span class="topic-route__label mono">当前话题</span>
        <span class="topic-route__name serif" data-test="topic-current-name">{{ currentName }}</span>
      </span>
      <span class="topic-route__arrow" aria-hidden="true">→</span>
      <span class="topic-route__block">
        <span class="topic-route__label mono">{{ isCreate ? "新话题名" : "目标话题" }}</span>
        <input
          v-if="isCreate"
          ref="inputRef"
          class="topic-name-input"
          data-test="topic-name-input"
          type="text"
          :value="name"
          :disabled="busy || stale"
          :aria-invalid="emptyNameError ? 'true' : undefined"
          aria-describedby="topic-name-hint"
          aria-label="新话题名（可修改）"
          autocomplete="off"
          spellcheck="false"
          @input="onNameInput"
          @keydown.enter.prevent
        />
        <span v-else class="topic-route__name serif" data-test="topic-target-name">{{ targetName }}</span>
      </span>
    </p>

    <p v-if="isCreate" id="topic-name-hint" class="topic-name-hint">
      这是模型建议的名字，你可以改；确认之前不会创建话题。
    </p>
    <p v-if="emptyNameError" class="topic-error" role="alert" data-test="topic-name-error">
      {{ EMPTY_NAME_ERROR }}
    </p>

    <p v-if="item.reason" class="topic-reason" data-test="topic-reason">{{ item.reason }}</p>

    <p v-if="stale" class="topic-state" role="status" data-test="topic-stale">
      这项申请已经有结局（可能已在别处处理、已过期或你已手动切换话题），不会再执行。
    </p>
    <p v-else-if="expired" class="topic-state" role="status" data-test="topic-expired">
      这项申请已经到期，不能再批准；仍可选择在当前话题回答。
    </p>
    <p v-if="busy" class="topic-progress" role="status" data-test="topic-progress">
      正在提交你的选择…（执行结果以后端为准）
    </p>

    <div class="topic-actions">
      <button
        v-if="stale"
        class="topic-btn later"
        type="button"
        data-test="topic-stale-ack"
        @click="emit('dismiss')"
      >
        知道了
      </button>
      <template v-else>
        <button
          class="topic-btn quiet"
          type="button"
          :disabled="actionDisabled"
          data-test="topic-stay"
          @click="stay"
        >
          在当前话题回答
        </button>
        <button
          class="topic-btn primary"
          type="button"
          :disabled="approveDisabled"
          data-test="topic-approve"
          @click="approve"
        >
          {{ mainLabel }}
        </button>
      </template>
    </div>
    <p class="topic-note">确认之前话题不会切换或新建；你的原问题会继续得到回答。</p>
  </section>
</template>

<style scoped>
.topic-approval {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 10px;
  min-width: 0;
  padding: 2px 2px 0;
  font-family: var(--sans);
  font-size: 13px;
  line-height: 1.65;
  color: var(--text-primary);
}
.topic-close {
  position: absolute;
  top: -4px;
  right: -4px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  padding: 0;
  border: 1px solid transparent;
  border-radius: 50%;
  background: transparent;
  color: var(--text-secondary);
  cursor: pointer;
}
.topic-close svg {
  width: 14px;
  height: 14px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.6;
  stroke-linecap: round;
}
.topic-close:hover {
  background: var(--bg-inset);
  color: var(--text-strong);
}
.topic-close:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 1px;
}
.topic-kicker {
  margin: 0;
  padding: 0 30px;
  text-align: center;
  font-size: 10px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--text-muted);
  overflow-wrap: anywhere;
}
.topic-route {
  display: flex;
  flex-wrap: wrap;
  align-items: stretch;
  gap: 10px;
  min-width: 0;
  margin: 0;
}
.topic-route__block {
  display: flex;
  flex: 1 1 170px;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
  padding: 10px 12px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--qio-ui-radius, var(--r-lg));
  background: var(--bg-inset);
}
.topic-route__label {
  font-size: 10px;
  letter-spacing: 0.06em;
  color: var(--text-muted);
}
.topic-route__name {
  min-width: 0;
  font-size: 15px;
  font-weight: 500;
  line-height: 1.45;
  color: var(--text-strong);
  overflow-wrap: anywhere;
  word-break: break-word;
}
.topic-route__arrow {
  align-self: center;
  flex: none;
  color: var(--text-muted);
  font-size: 16px;
  line-height: 1;
}
.topic-name-input {
  width: 100%;
  min-width: 0;
  padding: 7px 10px;
  border: 1px solid var(--border-strong);
  border-radius: var(--qio-ui-radius, var(--r-lg));
  background: var(--bg-elevated);
  color: var(--text-strong);
  font-family: var(--serif);
  font-size: 14px;
  line-height: 1.5;
}
.topic-name-input:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 1px;
}
.topic-name-hint {
  margin: -2px 0 0;
  color: var(--text-muted);
  font-size: 11px;
  overflow-wrap: anywhere;
}
.topic-error {
  margin: 0;
  padding: 7px 10px;
  border: 1px solid var(--danger);
  border-radius: var(--qio-ui-radius, var(--r-lg));
  background: var(--danger-soft);
  color: var(--danger);
  font-size: 12px;
  overflow-wrap: anywhere;
}
.topic-reason {
  margin: 0;
  color: var(--text-secondary);
  font-size: 12px;
  overflow-wrap: anywhere;
}
.topic-state {
  margin: 0;
  padding: 8px 12px;
  border: 1px solid var(--warning);
  border-radius: var(--qio-ui-radius, var(--r-lg));
  background: var(--warning-soft, var(--bg-inset));
  color: var(--text-primary);
  font-size: 12px;
  line-height: 1.6;
  overflow-wrap: anywhere;
}
.topic-progress {
  margin: 0;
  text-align: center;
  color: var(--text-secondary);
  font-size: 12px;
  overflow-wrap: anywhere;
}
.topic-actions {
  display: flex;
  align-items: center;
  justify-content: center;
  flex-wrap: wrap;
  gap: 8px;
  padding-top: 10px;
  border-top: 1px solid var(--border-subtle);
}
.topic-btn {
  min-width: 128px;
  height: 34px;
  padding: 0 16px;
  border: 1px solid transparent;
  border-radius: var(--qio-ui-radius, var(--r-lg));
  font-family: var(--sans);
  font-size: 13px;
  cursor: pointer;
  transition:
    transform var(--dur-press) var(--ease-1-out),
    background var(--dur-fast) var(--ease-1);
}
.topic-btn:active:not(:disabled) {
  transform: translateY(var(--shift-1, 1px));
}
.topic-btn:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 2px;
}
.topic-btn:disabled {
  opacity: 0.5;
  cursor: default;
}
.topic-btn.primary {
  background: var(--accent);
  color: var(--on-accent);
}
.topic-btn.primary:hover:not(:disabled) {
  background: var(--accent-hover);
}
.topic-btn.quiet {
  background: transparent;
  color: var(--text-primary);
  border-color: var(--border-strong);
}
.topic-btn.quiet:hover:not(:disabled) {
  background: var(--bg-inset);
}
.topic-btn.later {
  background: transparent;
  color: var(--text-secondary);
  border: 1px dashed var(--border-strong);
}
.topic-btn.later:hover:not(:disabled) {
  color: var(--text-primary);
  border-color: var(--text-muted);
}
.topic-note {
  margin: 0;
  color: var(--text-muted);
  font-size: 10.5px;
  line-height: 1.5;
  text-align: center;
  overflow-wrap: anywhere;
}
</style>
