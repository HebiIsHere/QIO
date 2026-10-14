/**
 * Markdown 渲染器（2026-10-14 用户要求：.md 文件卡片要解析成正文，而不是按纯文本逐行显示）。
 *
 * 视图模型**不重写解析**：后端对 .md 仍按 `code` 类别给出文本行，这里直接复用
 * {@link ./code} 的同一份实现（行 → 视图模型）；真正的 Markdown 解析由组件用
 * `MarkdownContent`（remark-gfm 管线）完成，因此只有一处解析实现。
 */
export { view, languageLabel } from "./code";

export const RENDERER = "markdown";
