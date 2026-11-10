<script setup lang="ts">
/**
 * 话题申请的正文（C/D）：当前话题 → 目标话题（switch）/ 可编辑新名称（create）
 * + 简短原因 + 两个动作（转到并回答 / 新建并回答、在当前话题回答）+ 右上角「暂时收起」。
 *
 * 边界（契约 §2.1 / §2.4 / §8）：
 * - **只**展示允许的字段：当前话题、目标/新名称、简短原因；不展示原问题/摘要/预览；
 * - 两个动作都只 emit 决定，真正的提交由 store 的唯一入口完成（不在这里发请求）；
 * - 关闭（右上角）只收起，不提交任何决定；
 * - 阶段文案来自 store 派生事实（TOPIC_PHASE），没有就是没有：不伪造「模型生成中」；
 * - 失效 ≠ 拒绝：原确认不可执行、说明真实原因、保留「在当前话题回答」恢复入口，
 *   不写拒绝范围、不自动跳回旧位置、不覆盖用户后来的选择；
 * - 恢复：显示后端真实失败阶段说明，重试只 emit recordId（动作由 store 的唯一入口调用）；
 * - 查重冲突：显示已有相似话题名，说明是同一个操作的二次确认，不生成第二份申请；
 * - 空名称在本地阻断并就地显示错误（不关闭、不提交）；回车不直接提交；
 * - 颜色只用 var(--*)，新控件圆角 14px（--qio-ui-radius），字体三声部。
 */
import { computed, ref, watch } from "vue";
import type { TopicApprovalItem } from "../../stores/unifiedApprovals";
import type {
  TopicConflictView,
  TopicRecoveryView,
  TopicStageValue,
} from "./TopicStageTypes";

const props = defineProps<{
  item: TopicApprovalItem;
  /** 用户可编辑的新话题名（create）；由 store 持有，收起 / 失败后不丢。 */
  name: string;
  busy: boolean;
  expired: boolean;
  stale: boolean;
  /** 阶段事实；undefined / null = 后端没给（按「等待你的选择」呈现，不编造后续阶段）。 */
  stage?: TopicStageValue | null;
  /** 申请失效（导航版本冲突 / 已在别处处理等）：原确认不可执行。 */
  invalidated?: boolean;
  /** 失效的真实原因（后端给出，原文显示）。 */
  invalidatedReason?: string;
  /** 回答准备失败等可恢复事实。 */
  recovery?: TopicRecoveryView | null;
  /** 同一操作的查重冲突状态。 */
  conflict?: TopicConflictView | null;
  /** 重试正在提交（按钮禁用，不重复提交）。 */
  retrying?: boolean;
}>();

const emit = defineEmits<{
  decide: [payload: { decision: "approve" | "reject"; edits?: { name: string } }];
  "update:name": [value: string];
  collapse: [];
  dismiss: [];
  /** 恢复入口：只把 recordId 交给 store 的唯一 retry 动作，组件不发请求。 */
  retry: [recordId: string];
  /** 失效收口：不发 /respond、不写拒绝范围，只把事实交给 store 收口。 */
  acknowledge: [];
}>();

const EMPTY_NAME_ERROR = "话题名不能为空；请填写新话题名后再提交。";

const inputRef = ref<HTMLInputElement | null>(null);
const emptyNameError = ref(false);

const isCreate = computed(() => props.item.action === "create");
const currentName = computed(() => props.item.currentTopicName || "未记录");
const targetName = computed(() => props.item.targetTopicName || "目标话题未记录");

/**
 * 受控阶段：有 pending 申请本身 = 在等待确认；阶段字段缺失不臆造后续阶段。
 * decision_open / answerable 与「这条申请仍在等待决定」不相容，按等待确认呈现（安全侧）。
 */
const stageKind = computed<
  "awaiting_confirmation" | "executing" | "context_preparing" | "recoverable_failure" | "invalidated"
>(() => {
  if (props.invalidated) return "invalidated";
  const stage = props.stage ?? null;
  if (stage === "executing") return "executing";
  if (stage === "context_preparing") return "context_preparing";
  if (stage === "recoverable_failure") return "recoverable_failure";
  return "awaiting_confirmation";
});
/** 已过决策点（执行中 / 上下文准备 / 失败待恢复）：不再提供任何决定动作。 */
const pastDecision = computed(
  () =>
    stageKind.value === "executing" ||
    stageKind.value === "context_preparing" ||
    stageKind.value === "recoverable_failure",
);
/** 申请已有结局（stale）时两个动作都不再可用，只给收口入口。 */
const actionDisabled = computed(() => props.busy || props.stale || props.invalidated);
/** 到期后不再提供「批准」；「在当前话题回答」仍是有效选择（那是不执行）。 */
const approveDisabled = computed(() => actionDisabled.value || props.expired);
const canDecide = computed(() => !props.invalidated && !props.stale && !pastDecision.value);
const mainLabel = computed(() => {
  if (props.busy) return "提交中…";
  return isCreate.value ? "新建并回答" : "转到并回答";
});

const stageKicker = computed(() => {
  switch (stageKind.value) {
    case "executing":
      return "话题申请 · 已确认，正在执行";
    case "context_preparing":
      return "话题申请 · 话题已变更，目标上下文准备中";
    case "recoverable_failure":
      return "话题申请 · 回答准备失败";
    case "invalidated":
      return "话题申请 · 已失效";
    default:
      return "话题申请 · 本轮暂停，等待你的选择";
  }
});

const stageDetail = computed(() => {
  if (stageKind.value === "executing") {
    return "正在执行你确认的话题操作；执行结果以后端为准，不需要再操作。";
  }
  if (stageKind.value === "context_preparing") {
    return "话题已经变更成功，正在准备目标话题的上下文；准备好之后才会开始回答。";
  }
  return "";
});

/** 恢复：后端真实失败阶段说明优先；缺失时只说事实，不编造具体阶段。 */
const recoveryMessage = computed(
  () => String(props.recovery?.message ?? "").trim() || "话题操作没有完成，回答还没有生成。",
);
const canRetry = computed(
  () => Boolean(props.recovery?.recordId) && props.recovery?.retryable === true,
);
const retryLabel = computed(() => (props.retrying ? "重试中…" : "重试回答"));

const conflictText = computed(() => {
  const conflict = props.conflict ?? null;
  if (!conflict) return "";
  const existing = String(conflict.existingTopicName ?? "").trim();
  const name = existing ? "「" + existing + "」" : "";
  if (conflict.status === "conflict_confirmed") {
    return "你已确认仍要新建" + name + "；执行结果以后端为准。";
  }
  if (conflict.status === "conflict_declined") {
    return "你已选择不新建" + name + "；会留在当前话题回答。";
  }
  // conflict_pending 与未知状态：都说成「需要你确认的相似话题」，不猜后端没给的含义
  return (
    "检测到相似话题" +
    name +
    "：确认后会走同一个话题操作的相似话题二次确认，不会生成第二份申请。"
  );
});

/** 失效说明：真实原因原文 + 明确「没有执行、也没有拒绝」。 */
const invalidatedText = computed(() => {
  const reason = String(props.invalidatedReason ?? "").trim();
  return reason
    ? "这项申请已失效：" + reason
    : "这项申请已失效（导航位置或操作状态已经变化）。";
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
  if (!canDecide.value || actionDisabled.value || props.expired) return;
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
  // 失效后「在当前话题回答」= 恢复入口：不写拒绝范围、不发 /respond，只交回收口事实
  if (props.invalidated) {
    emit("acknowledge");
    return;
  }
  if (actionDisabled.value || pastDecision.value) return;
  emit("decide", { decision: "reject" });
}

function retryRecovery() {
  if (!canRetry.value || props.retrying || props.busy) return;
  emit("retry", String(props.recovery?.recordId ?? ""));
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

    <p class="topic-kicker mono" data-test="topic-stage" :data-stage="stageKind">
      {{ stageKicker }}
    </p>
    <p
      v-if="stageDetail"
      class="topic-stage-detail"
      role="status"
      data-test="topic-stage-detail"
    >
      {{ stageDetail }}
    </p>

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
          :disabled="busy || stale || invalidated || pastDecision"
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

    <!-- 失效：真实原因 + 明确没执行也没拒绝；恢复入口在动作区（在当前话题回答） -->
    <div v-if="invalidated" class="topic-invalidated" role="status" data-test="topic-invalidated">
      <p class="topic-state-title">{{ invalidatedText }}</p>
      <p class="topic-state-body">
        没有执行任何切换或新建，也没有替你拒绝；你后来的选择、草稿和已改过的名称都保留。
      </p>
    </div>

    <!-- 可恢复失败：后端真实阶段说明 + 重试（动作只到 store 的唯一入口） -->
    <div
      v-if="stageKind === 'recoverable_failure'"
      class="topic-recovery"
      role="alert"
      data-test="topic-recovery"
    >
      <p class="topic-state-title">{{ recoveryMessage }}</p>
      <p class="topic-state-body">
        原问题与草稿都已保留；不会切回旧话题，也不会改用旧上下文回答。
      </p>
      <button
        v-if="canRetry"
        class="topic-btn quiet topic-recovery__retry"
        type="button"
        data-test="topic-recovery-retry"
        :disabled="retrying || busy"
        @click="retryRecovery"
      >
        {{ retryLabel }}
      </button>
    </div>

    <!-- 查重冲突：同一操作的二次确认（不是第二份申请） -->
    <p
      v-if="conflictText"
      class="topic-conflict"
      role="status"
      data-test="topic-conflict"
    >
      {{ conflictText }}
    </p>

    <p v-if="stale && !invalidated" class="topic-state" role="status" data-test="topic-stale">
      这项申请已经有结局（可能已在别处处理、已过期或你已手动切换话题），不会再执行。
    </p>
    <p v-else-if="expired && !invalidated" class="topic-state" role="status" data-test="topic-expired">
      这项申请已经到期，不能再批准；仍可选择在当前话题回答。
    </p>
    <p v-if="busy && canDecide" class="topic-progress" role="status" data-test="topic-progress">
      正在提交你的选择…（执行结果以后端为准）
    </p>

    <div class="topic-actions">
      <button
        v-if="stale && !invalidated"
        class="topic-btn later"
        type="button"
        data-test="topic-stale-ack"
        @click="emit('dismiss')"
      >
        知道了
      </button>
      <button
        v-else-if="invalidated"
        class="topic-btn quiet"
        type="button"
        data-test="topic-stay"
        @click="stay"
      >
        在当前话题回答
      </button>
      <template v-else-if="canDecide">
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
    <p v-if="canDecide" class="topic-note">确认之前话题不会切换或新建；你的原问题会继续得到回答。</p>
    <p v-else-if="invalidated" class="topic-note">
      选择「在当前话题回答」不会执行这次申请，也不代表你拒绝过；原问题会按当前话题继续。
    </p>
    <p v-else-if="pastDecision" class="topic-note">
      你的决定已经提交；这里只显示真实状态，不再需要操作。
    </p>
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
.topic-stage-detail {
  margin: -2px 0 0;
  text-align: center;
  color: var(--text-secondary);
  font-size: 12px;
  line-height: 1.6;
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
.topic-state,
.topic-invalidated,
.topic-recovery,
.topic-conflict {
  display: flex;
  flex-direction: column;
  gap: 4px;
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
.topic-recovery {
  border-color: var(--danger);
  background: var(--danger-soft);
}
.topic-state-title {
  margin: 0;
  color: var(--text-strong);
  font-family: var(--serif);
  font-size: 13px;
  font-weight: 500;
  line-height: 1.55;
  overflow-wrap: anywhere;
}
.topic-state-body {
  margin: 0;
  color: var(--text-secondary);
  font-size: 12px;
  line-height: 1.6;
  overflow-wrap: anywhere;
}
.topic-recovery__retry {
  align-self: flex-start;
  min-width: 108px;
  margin-top: 2px;
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
