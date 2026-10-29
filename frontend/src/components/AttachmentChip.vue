<script setup lang="ts">
import { vQioTip } from "../composables/useQioTip";
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
 *
 * v4 视觉：单胶囊 + 文件图标 + 「眼睛 / ⋯」图标动作；破坏性/恢复类动作保留文字并收进 ⋯ 菜单。
 */
import { computed } from "vue";
import {
  REFERENCE_CAVEAT,
  attachmentActions,
  humanSize,
  stateText,
  type AttachmentRef,
} from "../services/attachments";

const props = defineProps<{ attachment: AttachmentRef; busy?: boolean; previewable?: boolean }>();
const emit = defineEmits<{
  (e: "remove", id: string): void;
  (e: "retry", id: string): void;
  (e: "open", id: string): void;
  (e: "relocate", id: string): void;
  (e: "reupload", id: string): void;
  (e: "preview", id: string): void;
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
  <span
    class="chip"
    data-qio-fit
    :class="['s-' + attachment.state, 'k-' + attachment.kind]"
    v-qio-tip="title"
  >
    <!-- 文件类型的第一眼识别：一个轻量的圆形文件符号，不抢文件名 -->
    <span class="file-glyph" aria-hidden="true">
      <svg viewBox="0 0 24 24">
        <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
        <path d="M14 2v6h6" />
        <path d="M8 13h8M8 17h6" />
      </svg>
    </span>

    <span class="chip-body">
      <span class="name">{{ attachment.name }}</span>
      <span class="meta">
        <span class="size mono">{{ humanSize(attachment.sizeBytes) }}</span>
        <span class="dot" aria-hidden="true">·</span>
        <span class="state">{{ stateText(attachment) }}</span>
      </span>
      <!-- 失败原因必须**用户可见**（契约 §1.4）：不能只留在服务端日志里 -->
      <span
        v-if="attachment.error"
        class="err"
        data-test="attach-error"
        v-qio-tip="attachment.error"
      >
        {{ attachment.error }}
      </span>
      <span v-if="has('reupload')" class="no-recovery" data-test="attach-no-recovery-note">
        QIO 无法从原地址恢复
      </span>
    </span>

    <span class="chip-actions">
      <!-- 主操作：在右侧只读预览（仅在消息上下文提供） -->
      <button
        v-if="previewable"
        class="act preview-open"
        type="button"
        :disabled="busy"
        :aria-label="'在右侧预览附件 ' + attachment.name"
        v-qio-tip="'在右侧只读预览（不改源文件）'"
        @click="emit('preview', attachment.id)"
      >
        <svg viewBox="0 0 24 24" aria-hidden="true">
          <path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7S1 12 1 12z" />
          <circle cx="12" cy="12" r="3" />
        </svg>
      </button>

      <!-- 次操作收进 ⋯：打开副本 / 重新定位 / 重试 / 重新上传 / 移除 -->
      <details class="more">
        <summary
          class="more-btn"
          :aria-label="'更多附件操作：' + attachment.name"
          v-qio-tip="'更多附件操作（打开副本 / 重新定位 / 重试 / 移除）'"
        >
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <circle cx="5" cy="12" r="1.6" />
            <circle cx="12" cy="12" r="1.6" />
            <circle cx="19" cy="12" r="1.6" />
          </svg>
        </summary>
        <div class="more-menu">
          <button
            v-if="openable"
            class="act open"
            type="button"
            :disabled="busy"
            :aria-label="'打开附件 ' + attachment.name"
            v-qio-tip="'打开 ' + attachment.name"
            @click="emit('open', attachment.id)"
          >
            打开副本
          </button>
          <button
            v-if="has('relocate')"
            class="act locate"
            type="button"
            :disabled="busy"
            :aria-label="'重新定位附件 ' + attachment.name"
            v-qio-tip="'文件被移动或改名了？重新指定它的位置'"
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
          <button
            v-if="has('reupload')"
            class="act reupload"
            type="button"
            :disabled="busy"
            :aria-label="'重新上传附件 ' + attachment.name + '（QIO 无法从原地址恢复）'"
            v-qio-tip="'QIO 无法从原地址恢复：请重新选择这个文件'"
            @click="emit('reupload', attachment.id)"
          >
            重新上传
          </button>
          <button
            class="act remove"
            type="button"
            :aria-label="'移除附件 ' + attachment.name"
            @click="emit('remove', attachment.id)"
          >
            移除附件
          </button>
        </div>
      </details>
    </span>
  </span>
</template>

<style scoped>
/* 单胶囊：文件符号 → 文件名（主）→ 大小/状态（次）→ 图标动作（末） */
.chip {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  max-width: 100%;
  padding: 4px 5px 4px 5px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-pill);
  background: var(--bg-surface);
  font-family: var(--sans);
  font-size: var(--fs-xs);
  color: var(--text-secondary);
  position: relative;
  transition: border-color var(--dur-fast) var(--ease-1), color var(--dur-fast) var(--ease-1);
}
.chip:hover {
  border-color: var(--border-strong);
  color: var(--text-primary);
}
.file-glyph {
  width: 28px;
  height: 28px;
  border-radius: 50%;
  background: var(--accent-soft);
  color: var(--link);
  display: flex;
  align-items: center;
  justify-content: center;
  flex: none;
}
.file-glyph svg {
  width: 15px;
  height: 15px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.6;
  stroke-linecap: round;
  stroke-linejoin: round;
}
.chip-body {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 1px;
  min-width: 0;
  padding-right: 1px;
}
.name {
  min-width: 0;
  max-width: min(320px, 52vw);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12.5px;
  line-height: 1.35;
  color: var(--text-primary);
}
.meta {
  display: flex;
  align-items: center;
  gap: 5px;
  min-width: 0;
  max-width: 100%;
  overflow: hidden;
  white-space: nowrap;
  font-size: 10.5px;
  line-height: 1.35;
  color: var(--text-muted);
}
.meta .dot {
  color: var(--text-faint);
}
.state {
  color: var(--text-muted);
  overflow: hidden;
  text-overflow: ellipsis;
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
/* 失败/丢失原因：完整换行显示（不只是日志），完整原文仍在 title 里 */
.err {
  min-width: 0;
  max-width: 100%;
  overflow-wrap: anywhere;
  color: var(--danger);
  font-size: 10.5px;
  line-height: 1.5;
}
/* 「QIO 无法从原地址恢复」：一句实话，别让用户以为点什么都能找回来 */
.no-recovery {
  font-size: 10px;
  color: var(--text-muted);
  overflow-wrap: anywhere;
}
.chip-actions {
  display: flex;
  align-items: center;
  gap: 2px;
  flex: none;
  margin-left: 1px;
  position: relative;
}
.act {
  border: 0;
  background: none;
  padding: 7px 9px;
  font: inherit;
  font-size: 11.5px;
  line-height: 1.4;
  color: var(--text-secondary);
  cursor: pointer;
  text-align: left;
  border-radius: var(--qio-ui-radius);
}
.act:disabled {
  color: var(--text-muted);
  cursor: default;
}
/* 眼睛：图标按钮，主操作用 */
.preview-open {
  width: 28px;
  height: 28px;
  padding: 0;
  border-radius: 50%;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: var(--text-muted);
  flex: none;
}
.preview-open:hover:not(:disabled) {
  background: var(--layer-hover);
  color: var(--accent);
}
.preview-open svg {
  width: 15px;
  height: 15px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.6;
  stroke-linecap: round;
  stroke-linejoin: round;
}
/* ⋯ 菜单 */
.more {
  position: relative;
  display: inline-flex;
}
.more-btn {
  list-style: none;
  width: 28px;
  height: 28px;
  border-radius: 50%;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: var(--text-muted);
  cursor: pointer;
}
.more-btn::-webkit-details-marker {
  display: none;
}
.more-btn:hover,
.more[open] .more-btn {
  background: var(--layer-hover);
  color: var(--text-strong);
}
.more-btn:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 1px;
}
.more-btn svg {
  width: 15px;
  height: 15px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.6;
  stroke-linecap: round;
}
.more-menu {
  position: absolute;
  right: 0;
  bottom: calc(100% + 8px);
  z-index: 40;
  min-width: 142px;
  padding: 4px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--qio-ui-radius);
  background: var(--bg-elevated);
  box-shadow: var(--shadow-2);
  display: flex;
  flex-direction: column;
  gap: 1px;
}
.more-menu .act {
  width: 100%;
  padding: 7px 9px;
  font-size: 11.5px;
  color: var(--text-secondary);
}
.more-menu .act:hover:not(:disabled) {
  background: var(--layer-hover);
  color: var(--text-strong);
  text-decoration: none;
}
.more-menu .act.remove {
  color: var(--danger);
}
.more-menu .act.remove:hover:not(:disabled) {
  color: var(--danger);
}
</style>
