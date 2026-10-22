<script setup lang="ts">
/**
 * 预览栏里的文本 / 代码 / Markdown 阅读面（只读）。
 *
 * 职责边界：
 *   * 只负责排版（行号、等宽、语言/编码事实、Markdown 按正文解析）；
 *   * 搜索高亮由父级传入 query（代码按行切片段；Markdown 的高亮由父级在渲染后标 DOM）；
 *   * 阅读位置、缩放、复制、外部打开都在 PreviewReader，这里不藏第二套逻辑。
 *
 * 硬约束：默认软换行，**代码行不撑出栏宽**；关闭换行时只在本面板内部横向滚动，
 * 页面本身不出现横向滚动。
 */
import MarkdownContent from "../../MarkdownContent.vue";
import { highlightSegments } from "../../../conversation/preview";
import { vQioScrollbar } from "../../../composables/useQioScrollbars";

withDefaults(
  defineProps<{
    mode: "code" | "markdown";
    /** Markdown 模式使用原文；代码模式使用行数组。 */
    lines: string[];
    markdownSource: string;
    languageLabel: string;
    encoding: string;
    totalLines: number;
    truncated: boolean;
    wrap: boolean;
    query: string;
    activeLine: number | null;
    /** Markdown 重渲染 key：搜索词变化时让父级重新标 DOM。 */
    markKey?: number;
  }>(),
  {
    markKey: 0,
  },
);
</script>

<template>
  <div class="pv-text" :data-pv-mode="mode">
    <template v-if="mode === 'markdown'">
      <p class="pv-fact">
        Markdown · 共 {{ totalLines }} 行 · 已解析为正文 · 只读
      </p>
      <div class="pv-markdown" data-pv-markdown :key="markKey">
        <MarkdownContent :source="markdownSource" />
      </div>
    </template>
    <template v-else>
      <p class="pv-fact">
        <span class="mono">{{ languageLabel }}</span> · 共 {{ totalLines }} 行 · 编码 {{ encoding }} · 只读
      </p>
      <p v-if="truncated" class="pv-warn">
        内容超出预览上限：这里只显示前一部分（已截断，完整内容请打开或下载）
      </p>
      <div class="pv-code" :class="{ 'is-nowrap': !wrap }" v-qio-scrollbar="{ policy: 'auto', axis: 'both' }">
        <div
          v-for="(line, index) in lines"
          :key="index"
          class="pv-line"
          :class="{ 'is-active': activeLine === index + 1 }"
          :data-line="index + 1"
        >
          <span class="pv-lineno mono" aria-hidden="true">{{ index + 1 }}</span>
          <span class="pv-line-text"><template
            v-for="(segment, index) in highlightSegments(line, query)"
            :key="index"
          ><mark v-if="segment.hit" class="pv-hit">{{ segment.text }}</mark><template v-else>{{ segment.text }}</template></template></span>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.pv-text {
  --pv-scale: 1;
  min-width: 0;
  font-size: calc(var(--fs-sm) * var(--pv-scale));
}
.pv-fact {
  margin: 0 0 var(--sp-2);
  color: var(--text-muted);
  font-size: var(--fs-tech);
  overflow-wrap: anywhere;
}
.pv-warn {
  margin: 0 0 var(--sp-2);
  padding: var(--sp-1) var(--sp-2);
  border-left: 2px solid var(--warning);
  background: var(--warning-soft);
  color: var(--text-secondary);
  font-size: var(--fs-xs);
  line-height: var(--lh-tight);
  overflow-wrap: anywhere;
}
.pv-markdown {
  min-width: 0;
  overflow-wrap: anywhere;
  line-height: var(--lh-base);
}
.pv-code {
  min-width: 0;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-sm);
  background: var(--bg-inset);
  overflow: hidden;
}
.pv-line {
  display: flex;
  min-width: 0;
  min-height: 1.7em;
  line-height: var(--lh-base);
  font-family: var(--mono);
  color: var(--text-secondary);
}
.pv-line.is-active {
  background: var(--accent-soft);
}
.pv-lineno {
  flex: none;
  width: 3.4em;
  padding: 0 var(--sp-2) 0 var(--sp-1);
  text-align: right;
  color: var(--text-faint);
  font-size: var(--fs-tech);
  user-select: none;
  border-right: 1px solid var(--border-subtle);
}
.pv-line-text {
  flex: 1 1 auto;
  min-width: 0;
  padding: 0 var(--sp-2);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  word-break: break-word;
  tab-size: 2;
}
.pv-code.is-nowrap {
  overflow-x: auto;
}
.pv-code.is-nowrap .pv-line-text {
  white-space: pre;
  overflow-wrap: normal;
  word-break: normal;
}
.pv-hit {
  background: color-mix(in srgb, var(--accent) 26%, transparent);
  color: var(--text-strong);
  border-radius: 2px;
}
:deep(.pv-markdown .pv-hit) {
  background: color-mix(in srgb, var(--accent) 26%, transparent);
  color: var(--text-strong);
  border-radius: 2px;
  padding: 0 1px;
}
:deep(.pv-markdown) {
  color: var(--text-primary);
  font-size: calc(var(--fs-base) * var(--pv-scale));
  line-height: var(--lh-base);
  overflow-wrap: anywhere;
}
:deep(.pv-markdown h1),
:deep(.pv-markdown h2),
:deep(.pv-markdown h3) {
  font-family: var(--serif);
  color: var(--text-strong);
}
:deep(.pv-markdown pre) {
  overflow-x: auto;
  max-width: 100%;
}
:deep(.pv-markdown table) {
  display: block;
  max-width: 100%;
  overflow-x: auto;
}
:deep(.pv-markdown img) {
  max-width: 100%;
}
@media (prefers-reduced-motion: reduce) {
  .pv-text *,
  .pv-text *::before,
  .pv-text *::after {
    transition: none !important;
    animation: none !important;
  }
}
</style>
