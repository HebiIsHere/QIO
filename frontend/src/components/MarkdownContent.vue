<script setup lang="ts">
/**
 * Markdown 渲染（remark 管线 → 自定义 mdast → HTML 渲染器）。
 * directive 节点 v1 渲染为行内文本，后续扩展为内联组件。
 */
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { unified } from "unified";
import remarkParse from "remark-parse";
import remarkGfm from "remark-gfm";
import remarkDirective from "remark-directive";
import hljs from "highlight.js/lib/core";
import javascript from "highlight.js/lib/languages/javascript";
import typescript from "highlight.js/lib/languages/typescript";
import python from "highlight.js/lib/languages/python";
import json from "highlight.js/lib/languages/json";
import bash from "highlight.js/lib/languages/bash";
import css from "highlight.js/lib/languages/css";
import xml from "highlight.js/lib/languages/xml";
hljs.registerLanguage("javascript", javascript);
hljs.registerLanguage("typescript", typescript);
hljs.registerLanguage("python", python);
hljs.registerLanguage("json", json);
hljs.registerLanguage("bash", bash);
hljs.registerLanguage("css", css);
hljs.registerLanguage("xml", xml);

const props = defineProps<{
  source: string;
  /** 逐字打字机：完整结构先渲染，再按文档顺序逐字符点亮 */
  reveal?: boolean;
  /** 字符/秒（三档 25/50/75），null 时按 3 秒封顶自适应 */
  cps?: number | null;
}>();

/** 增量渲染：reveal 时按已产出字符量喂给 Markdown，气泡随内容增长 */
const shown = ref(props.reveal ? 0 : props.source.length);
let raf = 0;

const renderSource = computed(() =>
  props.reveal ? props.source.slice(0, shown.value) : props.source,
);

/**
 * 从 fromLen 继续点亮到 total：不回到 0，只推进新增部分。
 * 已有内容保持不动（不闪、不跳、不重播动画）。
 */
function animateFrom(fromLen: number, total: number, cps: number) {
  cancelAnimationFrame(raf);
  if (fromLen >= total) {
    shown.value = total;
    return;
  }
  const capMs = 3000;
  const naturalMs = ((total - fromLen) / Math.max(1, cps)) * 1000;
  const durMs = Math.min(naturalMs, capMs);
  if (!(durMs > 0)) {
    shown.value = total;
    return;
  }
  const t0 = performance.now();
  const step = (now: number) => {
    const p = Math.min(1, (now - t0) / durMs);
    shown.value = Math.round(fromLen + p * (total - fromLen));
    if (p < 1) raf = requestAnimationFrame(step);
  };
  raf = requestAnimationFrame(step);
}

watch(
  () => [props.source, props.reveal] as const,
  ([source, reveal], prev) => {
    const [prevSource, prevReveal] = prev ?? (["", false] as const);
    const cps = props.cps ?? 50;
    if (!reveal) {
      cancelAnimationFrame(raf);
      shown.value = source.length;
      return;
    }
    // 刚切到 reveal：从头点亮
    if (!prevReveal) {
      shown.value = 0;
      animateFrom(0, source.length, cps);
      return;
    }
    if (source === prevSource) return;
    // 追加：从已点亮位置继续；被替换（非追加）：重新开始
    if (!source.startsWith(prevSource)) {
      shown.value = 0;
      animateFrom(0, source.length, cps);
      return;
    }
    animateFrom(Math.min(shown.value, source.length), source.length, cps);
  },
  { immediate: true },
);

onBeforeUnmount(() => cancelAnimationFrame(raf));

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function renderInline(children: any[]): string {
  if (!children) return "";
  return children.map(renderNode).join("");
}

function renderNode(node: any): string {
  switch (node.type) {
    case "text":
      return escapeHtml(node.value ?? "");
    case "strong":
      return `<strong>${renderInline(node.children)}</strong>`;
    case "emphasis":
      return `<em>${renderInline(node.children)}</em>`;
    case "delete":
      return `<del>${renderInline(node.children)}</del>`;
    case "inlineCode":
      return `<code class="inline">${escapeHtml(node.value ?? "")}</code>`;
    case "link":
      return `<a href="${escapeHtml(node.url ?? "")}" target="_blank" rel="noreferrer">${renderInline(node.children)}</a>`;
    case "break":
      return "<br />";
    case "textDirective":
    case "leafDirective":
    case "containerDirective":
      return renderInline(node.children ?? []);
    default:
      return renderInline(node.children ?? []);
  }
}

function renderBlock(node: any): string {
  switch (node.type) {
    case "root":
      return (node.children ?? []).map(renderBlock).join("\n");
    case "heading":
      const level = node.depth || 2;
      return `<h${level}>${renderInline(node.children)}</h${level}>`;
    case "paragraph":
      return `<p>${renderInline(node.children)}</p>`;
    case "blockquote":
      return `<blockquote>${(node.children ?? []).map(renderBlock).join("\n")}</blockquote>`;
    case "list":
      const tag = node.ordered ? "ol" : "ul";
      return `<${tag}>${(node.children ?? []).map((item: any) => renderBlock(item)).join("")}</${tag}>`;
    case "listItem":
      return `<li>${renderInline(node.children ?? [])}</li>`;
    case "code": {
      const lang = node.lang || "";
      let html = escapeHtml(node.value ?? "");
      if (lang && hljs.getLanguage(lang)) {
        html = hljs.highlight(node.value ?? "", { language: lang }).value;
      }
      // 代码块带 hover 复制按钮（长代码不用手动选择）
      const label = escapeHtml(lang || "code");
      return (
        `<div class="code-block" data-lang="${label}">` +
        `<button class="code-copy" type="button" aria-label="复制代码">复制</button>` +
        `<pre><code class="hljs">${html}</code></pre>` +
        `</div>`
      );
    }
    case "thematicBreak":
      return "<hr />";
    case "table": {
      const head = (node.children?.[0]?.children ?? []).map((c: any) => `<th>${renderInline(c.children)}</th>`).join("");
      const body = (node.children?.slice(1) ?? []).map((row: any) =>
        `<tr>${row.children.map((c: any) => `<td>${renderInline(c.children)}</td>`).join("")}</tr>`
      ).join("");
      // 表格放进可横向滚动容器：窄窗口下滚动而不是撑破气泡
      return `<div class="table-wrap"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
    }
    default:
      return renderInline(node.children ?? []);
  }
}

const html = computed(() => {
  const processor = unified()
    .use(remarkParse)
    .use(remarkGfm)
    .use(remarkDirective);
  const tree = processor.parse(renderSource.value);
  processor.runSync(tree);
  return renderBlock(tree);
});

/** 复制状态（对代码块按钮的短期反馈） */
const copiedCode = ref<HTMLElement | null>(null);
let copyTimer: ReturnType<typeof setTimeout> | null = null;

/** 事件委托：v-html 内的复制按钮由容器统一处理 */
async function onClick(e: MouseEvent) {
  const target = e.target as HTMLElement | null;
  const btn = target?.closest?.(".code-copy") as HTMLElement | null;
  if (!btn) return;
  const code = btn.parentElement?.querySelector("pre code")?.textContent ?? "";
  try {
    await navigator.clipboard?.writeText(code);
  } catch {
    // 剪贴板不可用：仍给出反馈
  }
  btn.textContent = "已复制";
  copiedCode.value = btn;
  if (copyTimer) clearTimeout(copyTimer);
  copyTimer = setTimeout(() => {
    if (copiedCode.value) copiedCode.value.textContent = "复制";
    copiedCode.value = null;
    copyTimer = null;
  }, 1600);
}

onBeforeUnmount(() => {
  if (copyTimer) clearTimeout(copyTimer);
});
</script>

<template>
<div class="markdown-body" v-html="html" @click="onClick"></div>
</template>

<style scoped>
.markdown-body { line-height: 1.65; font-size: 14px; overflow-wrap: anywhere; }
.markdown-body .tw-char { visibility: hidden; }
/* 阅读行宽：长文不铺满整屏 */
.markdown-body :deep(p), .markdown-body :deep(li), .markdown-body :deep(blockquote) { max-width: var(--measure); }
.markdown-body :deep(p) { margin: 0.4em 0; }
.markdown-body :deep(ul), .markdown-body :deep(ol) { margin: 0.4em 0; padding-left: 1.4em; }
.markdown-body :deep(li) { margin: 0.2em 0; }
.markdown-body :deep(h1), .markdown-body :deep(h2), .markdown-body :deep(h3) { margin: 0.8em 0 0.4em; }
.markdown-body :deep(h4), .markdown-body :deep(h5), .markdown-body :deep(h6) { margin: 1em 0 0.4em; }
.markdown-body :deep(pre) { background: var(--bg-elevated); border-radius: 8px; padding: 10px 12px; overflow-x: auto; max-width: 100%; }
.markdown-body :deep(pre code) { font-family: var(--mono); font-size: 12.5px; }
.markdown-body :deep(.code-block) { position: relative; margin: 0.5em 0; }
.markdown-body :deep(.code-copy) {
  position: absolute; top: 6px; right: 8px; z-index: 1;
  font-family: var(--sans); font-size: 11px; line-height: 1;
  padding: 5px 10px; border-radius: var(--r-sm);
  border: 1px solid var(--border-strong); background: var(--bg-surface);
  color: var(--text-secondary); cursor: pointer;
  opacity: 0; transition: opacity var(--dur-fast) var(--ease), color var(--dur-fast) var(--ease);
}
.markdown-body :deep(.code-block:hover .code-copy),
.markdown-body :deep(.code-copy:focus-visible) { opacity: 1; }
.markdown-body :deep(.code-copy:hover) { color: var(--accent); border-color: var(--accent); }
.markdown-body :deep(.table-wrap) { max-width: 100%; overflow-x: auto; }
.markdown-body :deep(code.inline) { background: var(--bg-accent-subtle); border-radius: 4px; padding: 1px 5px; }
.markdown-body :deep(a) { color: var(--link); }
.markdown-body :deep(blockquote) { border-left: 3px solid var(--accent); margin: 0.4em 0; padding-left: 10px; color: var(--text-secondary); }
.markdown-body :deep(table) { border-collapse: collapse; margin: 0.5em 0; min-width: max-content; }
.markdown-body :deep(th), .markdown-body :deep(td) { border: 1px solid var(--border-subtle); padding: 4px 10px; }
</style>
