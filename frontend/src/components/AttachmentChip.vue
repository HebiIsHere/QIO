<script setup lang="ts">
/**
 * 附件 chip：一个附件 = 一条事实。
 *
 * 显示什么由 state 决定（准备中 / 已保存副本 / 引用本地文件 / 失败 / 不在原位 / 内容有变化），
 * **动作由服务端 payload.actions 决定**（契约 §1.4）：不许出现走不通的按钮 ——
 *   * retry    → 「重试」（有真实原路径，重新读它做副本）
 *   * relocate → 「重新定位」（引用型附件的唯一依据）
 *   * reupload → 「重新上传」+ 明说「QIO 无法从原地址恢复」（浏览器字节上传）
 *   * actions 为空 → 一个动作按钮都不出（「打开 / 移除」另算，见下）
 * 缺字段（老后端）才退回既有状态逻辑（见 attachmentActions）。
 * 引用型（> 100MB）明确写「引用本地文件」，悬停给出「历史保留的是位置，不保证内容仍然存在」。
 */
import { computed } from "vue";
import {
  REFERENCE_CAVEAT,
  attachmentActions,
  humanSize,
  stateText,
  type AttachmentRef,
} from "../services/attachments";

const props = defineProps<{ attachment: AttachmentRef; busy?: boolean }>();
const emit = defineEmits<{
  (e: "remove", id: string): void;
  (e: "retry", id: string): void;
  (e: "open", id: string): void;
  (e: "relocate", id: string): void;
  (e: "reupload", id: string): void;
}>();

/** 可用动作：服务端说了算（空数组 = 没有动作）；缺字段才按状态兜底。 */
const actions = computed(() => attachmentActions(props.attachment));
function has(action: "retry" | "relocate" | "reupload"): boolean {
  return actions.value.includes(action);
}
/** 能打开：就绪或内容有变化（都读得到）。准备中/失败/丢失不给「打开」这个假入口。 */
const openable = computed(() => props.attachment.state === "ready" || props.attachment.state === "changed");

const title = computed(() => {
  const parts = [props.attachment.name, humanSize(props.attachment.sizeBytes)];
  if (props.attachment.kind === "reference") parts.push(REFERENCE_CAVEAT);
  if (props.attachment.error) parts.push(props.attachment.error);
  return parts.join("\n");
});
</script>

<template>
  <span class="chip" :class="['s-' + attachment.state, 'k-' + attachment.kind]" :title="title">
    <span class="name">{{ attachment.name }}</span>
    <span class="size mono">{{ humanSize(attachment.sizeBytes) }}</span>
    <span class="state">{{ stateText(attachment) }}</span>
    <!-- 失败原因必须**用户可见**（契约 §1.4）：不能只留在服务端日志里 -->
    <span
      v-if="attachment.error"
      class="err"
      data-test="attach-error"
      :title="attachment.error"
    >
      {{ attachment.error }}
    </span>
    <button
      v-if="openable"
      class="act"
      type="button"
      :disabled="busy"
      :aria-label="'打开附件 ' + attachment.name"
      :title="'打开 ' + attachment.name"
      @click="emit('open', attachment.id)"
    >
      打开
    </button>
    <button
      v-if="has('relocate')"
      class="act locate"
      type="button"
      :disabled="busy"
      :aria-label="'重新定位附件 ' + attachment.name"
      title="文件被移动或改名了？重新指定它的位置"
      @click="emit('relocate', attachment.id)"
    >
      重新定位
    </button>
    <button
      v-if="has('retry')"
      class="act retry"
      type="button"
      :disabled="busy"
      :aria-label="'重试附件 ' + attachment.name"
      @click="emit('retry', attachment.id)"
    >
      重试
    </button>
    <!-- 浏览器字节上传：QIO 手里没有内容、也没有原地址 —— 只能用户重新给一次 -->
    <button
      v-if="has('reupload')"
      class="act reupload"
      type="button"
      :disabled="busy"
      :aria-label="'重新上传附件 ' + attachment.name + '（QIO 无法从原地址恢复）'"
      title="QIO 无法从原地址恢复：请重新选择这个文件"
      @click="emit('reupload', attachment.id)"
    >
      重新上传
    </button>
    <span
      v-if="has('reupload')"
      class="no-recovery"
      data-test="attach-no-recovery-note"
    >
      QIO 无法从原地址恢复
    </span>
    <button
      class="act remove"
      type="button"
      :aria-label="'移除附件 ' + attachment.name"
      @click="emit('remove', attachment.id)"
    >
      ×
    </button>
  </span>
</template>

<style scoped>
.chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  max-width: 100%;
  padding: 3px 6px 3px 9px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-pill);
  background: var(--bg-inset);
  font-family: var(--sans);
  font-size: var(--fs-xs);
  color: var(--text-secondary);
  transition: border-color var(--dur-fast) var(--ease-1), color var(--dur-fast) var(--ease-1);
}
.chip:hover {
  border-color: var(--border-strong);
  color: var(--text-primary);
}
.name {
  max-width: 200px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-primary);
}
.size {
  font-size: 10px;
  color: var(--text-muted);
}
.state {
  color: var(--text-muted);
}
/* 准备中：不假装完成，用最轻的一档提示 */
.s-prepared {
  border-style: dashed;
}
.s-prepared .state {
  color: var(--text-secondary);
}
/* 就绪：把保存方式说清楚（已保存副本 / 引用本地文件） */
.s-ready .state {
  color: var(--text-secondary);
}
/* 失败 / 不在原位：必须显眼，但仍可重试 */
.s-failed,
.s-missing {
  border-color: var(--danger);
}
.s-failed .state,
.s-missing .state {
  color: var(--danger);
}
/* 内容有变化：不是失败，但不能当成就绪 */
.s-changed {
  border-color: var(--warning);
}
.s-changed .state {
  color: var(--warning);
}
.act {
  border: 0;
  background: none;
  padding: 0 2px;
  font: inherit;
  font-size: 10.5px;
  color: var(--link);
  cursor: pointer;
}
.act:hover:not(:disabled) {
  text-decoration: underline;
}
.act:disabled {
  color: var(--text-muted);
  cursor: default;
}
/* 失败/丢失原因：截断显示，完整原文在 title 里（用户可见，不只是日志） */
.err {
  max-width: 220px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--danger);
}
/* 「QIO 无法从原地址恢复」：一句实话，别让用户以为点什么都能找回来 */
.no-recovery {
  font-size: 10px;
  color: var(--text-muted);
}
.act.remove {
  font-size: 13px;
  line-height: 1;
  color: var(--text-muted);
}
.act.remove:hover {
  color: var(--danger);
}
</style>
