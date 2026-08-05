<script setup lang="ts">
/**
 * Markdown 渲染（remark 管线 → 自定义 mdast → HTML 渲染器）。
 * directive 节点 v1 渲染为行内文本，后续扩展为内联组件。
 */
import { computed } from "vue";
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

const props = defineProps<{ source: string }>();

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
      return `<pre><code class="hljs">${html}</code></pre>`;
    }
    case "thematicBreak":
      return "<hr />";
    case "table": {
      const head = (node.children?.[0]?.children ?? []).map((c: any) => `<th>${renderInline(c.children)}</th>`).join("");
      const body = (node.children?.slice(1) ?? []).map((row: any) =>
        `<tr>${row.children.map((c: any) => `<td>${renderInline(c.children)}</td>`).join("")}</tr>`
      ).join("");
      return `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
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
  const tree = processor.parse(props.source);
  processor.runSync(tree);
  return renderBlock(tree);
});
</script>

<template>
  <div class="markdown-body" v-html="html"></div>
</template>

<style scoped>
.markdown-body { line-height: 1.65; font-size: 14px; overflow-wrap: anywhere; }
.markdown-body :deep(p) { margin: 0.4em 0; }
.markdown-body :deep(h1), .markdown-body :deep(h2), .markdown-body :deep(h3) { margin: 0.8em 0 0.4em; }
.markdown-body :deep(pre) { background: var(--bg-elevated); border-radius: 8px; padding: 10px 12px; overflow-x: auto; }
.markdown-body :deep(code.inline) { background: var(--bg-accent-subtle); border-radius: 4px; padding: 1px 5px; }
.markdown-body :deep(a) { color: var(--link); }
.markdown-body :deep(blockquote) { border-left: 3px solid var(--accent); margin: 0.4em 0; padding-left: 10px; color: var(--text-secondary); }
.markdown-body :deep(table) { border-collapse: collapse; margin: 0.5em 0; }
.markdown-body :deep(th), .markdown-body :deep(td) { border: 1px solid var(--border-subtle); padding: 4px 10px; }
</style>