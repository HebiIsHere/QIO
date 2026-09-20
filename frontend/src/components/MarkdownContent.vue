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
import { isSafeExternalUrl, openExternal } from "../utils/externalLink";
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
  /** 观测到的真实增量到达间隔（毫秒）。给了它就以它为准，不再用字/秒估算。 */
  paceMs?: number | null;
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
function animateFrom(fromLen: number, total: number, cps: number, paceMs: number | null = null) {
  cancelAnimationFrame(raf);
  if (fromLen >= total) {
    shown.value = total;
    return;
  }
  const remaining = total - fromLen;
  // 有真实到达节奏就按它走（与模型实际速度一致）；没有时退回字/秒估计。
  const naturalMs = paceMs != null ? paceMs : (remaining / Math.max(1, cps)) * 1000;
  // 兜底追赶：积压超过 200 字时这一段压到 600ms 内，绝不把「逐字」变成明显落后。
  const capMs = remaining > 200 ? 600 : 3000;
  const durMs = Math.max(60, Math.min(naturalMs, capMs));
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
    animateFrom(Math.min(shown.value, source.length), source.length, cps, props.paceMs ?? null);
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
    case "link": {
      // 模型给的 URL 不可信：只有白名单 scheme 才渲染成可点链接，
      // 其余（javascript: / data: / file: / vbscript: …）退化为纯文本。
      const url = typeof node.url === "string" ? node.url : "";
      const label = renderInline(node.children);
      if (!isSafeExternalUrl(url)) {
        return `<span class="link-blocked" title="已阻止不安全的链接">${label}</span>`;
      }
      return `<a href="${escapeHtml(url.trim())}" class="ext-link" rel="noopener noreferrer nofollow" target="_blank">${label}</a>`;
    }
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

/**
 * 复制状态（对代码块按钮的短期反馈）。
 * 每个按钮独立计时：连续复制不同代码块时，各自的「已复制/复制失败」都要能自己复位，
 * 不能共用一个计时器（否则先点的那个会永久停在「已复制」）。
 */
const copyTimers = new Map<HTMLElement, ReturnType<typeof setTimeout>>();

function resetCopyButton(btn: HTMLElement) {
  const timer = copyTimers.get(btn);
  if (timer) clearTimeout(timer);
  copyTimers.delete(btn);
  btn.textContent = "复制";
  btn.classList.remove("fail");
  btn.removeAttribute("title");
}

/**
 * 写剪贴板。返回是否真的写成功：
 * 受限 WebView 下 navigator.clipboard 可能不存在，写入也可能被拒绝，
 * 这两种情况都必须让调用方知道，不能假装成功。
 */
async function writeClipboard(text: string): Promise<boolean> {
  try {
    const cb = navigator.clipboard;
    if (!cb || typeof cb.writeText !== "function") return false;
    await cb.writeText(text);
    return true;
  } catch {
    return false;
  }
}

/** 事件委托：v-html 内的复制按钮由容器统一处理 */
async function onClick(e: MouseEvent) {
  const target = e.target as HTMLElement | null;
  // 外链：不让 WebView 自己导航，统一交给系统打开（失败要说出来）
  const link = target?.closest?.("a.ext-link") as HTMLAnchorElement | null;
  if (link) {
    const url = link.getAttribute("href") ?? "";
    if (!isSafeExternalUrl(url)) return;
    e.preventDefault();
    resetLinkHint(link);
    const ok = await openExternal(url);
    if (!ok) {
      link.classList.add("link-failed");
      link.title = "打开失败：可以复制链接地址后手动打开";
      window.setTimeout(() => {
        link.classList.remove("link-failed");
        if (link.title.startsWith("打开失败")) link.removeAttribute("title");
      }, 2400);
    }
    return;
  }
  const btn = target?.closest?.(".code-copy") as HTMLElement | null;
  if (!btn) return;
  const codeEl = btn.parentElement?.querySelector("pre code") as HTMLElement | null;
  const code = codeEl?.textContent ?? "";
  resetCopyButton(btn); // 重试/重复点击：本次反馈归零后重新开始
  const ok = await writeClipboard(code);
  btn.textContent = ok ? "已复制" : "复制失败";
  btn.classList.toggle("fail", !ok);
  if (!ok) {
    // 失败时把代码选中，用户不必自己拖选；按钮保持可点，方便再试一次
    btn.title = "复制失败：已选中代码，可按 Ctrl+C 手动复制，或再点一次重试";
    selectNodeText(codeEl);
  }
  copyTimers.set(
    btn,
    setTimeout(() => resetCopyButton(btn), ok ? 1600 : 2400),
  );
}

function resetLinkHint(link: HTMLAnchorElement) {
  link.classList.remove("link-failed");
  if (link.title.startsWith("打开失败")) link.removeAttribute("title");
}

/** 选中代码块文本（手动复制退路）。环境不支持 Selection 时静默跳过。 */
function selectNodeText(el: HTMLElement | null) {
  if (!el) return;
  try {
    const range = document.createRange();
    range.selectNodeContents(el);
    const sel = window.getSelection();
    sel?.removeAllRanges();
    sel?.addRange(range);
  } catch {
    /* 选区不可用时只保留按钮反馈 */
  }
}

onBeforeUnmount(() => {
  for (const timer of copyTimers.values()) clearTimeout(timer);
  copyTimers.clear();
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
/* 触屏没有 hover：复制入口必须默认可见，不能只靠鼠标悬停才出现 */
@media (hover: none) {
  .markdown-body :deep(.code-copy) { opacity: 1; }
}
.markdown-body :deep(.code-copy:hover) { color: var(--accent); border-color: var(--accent); }
.markdown-body :deep(.code-copy.fail) { color: var(--danger); border-color: var(--danger); opacity: 1; }
.markdown-body :deep(.table-wrap) { max-width: 100%; overflow-x: auto; }
.markdown-body :deep(code.inline) { background: var(--bg-accent-subtle); border-radius: 4px; padding: 1px 5px; }
.markdown-body :deep(a) { color: var(--link); }
/* 危险 scheme：渲染成普通文本，不给可点链接 */
.markdown-body :deep(.link-blocked) { color: var(--text-secondary); text-decoration: underline dotted; cursor: help; }
.markdown-body :deep(a.link-failed) { color: var(--danger); }
.markdown-body :deep(blockquote) { border-left: 3px solid var(--accent); margin: 0.4em 0; padding-left: 10px; color: var(--text-secondary); }
.markdown-body :deep(table) { border-collapse: collapse; margin: 0.5em 0; min-width: max-content; }
.markdown-body :deep(th), .markdown-body :deep(td) { border: 1px solid var(--border-subtle); padding: 4px 10px; }
</style>
