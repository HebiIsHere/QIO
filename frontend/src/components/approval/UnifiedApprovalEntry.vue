<script setup lang="ts">
/**
 * 统一审批入口（D / Phase 3-C）。
 *
 * 职责只有一件事：让「有真实待办」在任何时刻都能被用户**手动找回**。
 * - 数量与文字全部来自统一队列（工具/权限 + 知识），不造假；
 * - 弹窗正在显示时不重复出现（同一时刻只有一个入口）；
 * - 暂缓 / 关闭只影响「自动弹出」，入口始终可用：审批永远不会变成不可达；
 * - 两种呈现：`floating`（顶部居中，兼容旧页面）与 `inline`（输入区上方事项行里的一格，
 *   由 ComposerEntries 或 v2 底聊放进同一行，避免互动页出现两个入口）。
 *
 * 这里不发起任何审批请求：打开动作只调用 store 的 open()（由 store 决定分派）。
 */
import { computed } from "vue";
import { useUnifiedApprovalsStore } from "../../stores/unifiedApprovals";

const props = withDefaults(
  defineProps<{
    /** floating：顶部居中（兼容既有页面）；inline：事项行内的一格（不固定定位）。 */
    variant?: "floating" | "inline";
    /** 紧凑：只显示「审批 N」，完整分解放 aria-label / title（事项行更窄）。 */
    compact?: boolean;
    /** inline 时是否显示图标（默认显示；纯文字档可关掉）。 */
    withMark?: boolean;
  }>(),
  { variant: "floating", compact: false, withMark: true },
);

const emit = defineEmits<{
  /** 用户点开入口（打开统一弹窗）。外层可用于收起其它事项面板。 */
  opened: [];
}>();

const unified = useUnifiedApprovalsStore();

const toolCount = computed(() => unified.items.filter((item) => item.kind === "tool").length);
const knowledgeCount = computed(() =>
  unified.items.filter((item) => item.kind === "knowledge").length,
);
const parts = computed(() => {
  const out: string[] = [];
  if (toolCount.value) out.push("权限 " + toolCount.value);
  if (knowledgeCount.value) out.push("知识 " + knowledgeCount.value);
  return out.join(" · ");
});
const title = computed(() => {
  const stale = unified.knowledgeStatus === "error" ? "（知识待办数量可能不是最新）" : "";
  return "有 " + unified.count + " 项申请等待确认" + stale;
});
const text = computed(() => (props.compact ? "审批 " + unified.count : "有 " + unified.count + " 项申请等待确认"));
const visible = computed(() => unified.count > 0 && !unified.visible);

function open() {
  if (unified.open()) emit("opened");
}

defineExpose({ open, visible, title });
</script>

<template>
  <button
    v-if="visible"
    class="unified-approval-entry"
    :class="{
      'is-floating': props.variant === 'floating',
      'is-inline': props.variant === 'inline',
      'is-compact': props.compact,
    }"
    type="button"
    data-test="unified-approval-entry"
    data-im="unified-approval-entry"
    :data-variant="props.variant"
    :aria-label="title + '，打开审批窗口'"
    :title="title"
    @mousedown.prevent
    @click="open"
  >
    <span v-if="props.withMark" class="mark" aria-hidden="true">!</span>
    <span class="text">{{ text }}</span>
    <span v-if="parts && !props.compact" class="breakdown mono" aria-hidden="true">{{ parts }}</span>
  </button>
</template>

<style scoped>
.unified-approval-entry {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-2, 8px);
  max-width: min(560px, calc(100vw - 32px));
  padding: 6px 14px;
  border-radius: var(--r-pill);
  border: 1px solid var(--warning);
  background: var(--bg-elevated);
  color: var(--text-strong);
  font-family: var(--sans);
  font-size: 12.5px;
  box-shadow: var(--shadow-2);
  cursor: pointer;
  transition:
    transform var(--dur-press) var(--ease-out),
    border-color var(--dur-fast) var(--ease);
}
.unified-approval-entry:hover {
  border-color: var(--accent);
}
.unified-approval-entry:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 2px;
}
/* 兼容档：顶部居中固定定位（与旧 ApprovalEntry 同一位置语义）。 */
.unified-approval-entry.is-floating {
  position: fixed;
  top: var(--qio-top-notes-offset, calc(var(--titlebar-height) + 12px));
  left: 50%;
  transform: translateX(-50%);
  z-index: 90;
}
.unified-approval-entry.is-floating:active {
  transform: translateX(-50%) translateY(var(--shift-1, 1px));
}
/* 事项行档：参与布局，不固定定位、不盖住输入区与板面。 */
.unified-approval-entry.is-inline {
  padding: 3px 10px;
  border-radius: var(--r-sm);
  box-shadow: none;
  font-size: var(--fs-sm, 12px);
}
.unified-approval-entry.is-compact {
  padding: 2px 8px;
  gap: 6px;
}
.mark {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex: none;
  width: 16px;
  height: 16px;
  border-radius: 50%;
  background: var(--warning);
  color: var(--bg-base);
  font-family: var(--mono);
  font-size: 11px;
  font-weight: 700;
}
.breakdown {
  color: var(--text-muted);
  font-size: 10.5px;
  letter-spacing: 0.04em;
  white-space: nowrap;
}
</style>
