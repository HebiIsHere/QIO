<!--
  统一文字卡编辑区（契约 2026-10-12 §1.3 / §1.6）。

  立场：**一个连续编辑区**。整张卡片只在这里编辑 —— 正文、标题、列表、引用、代码、链接
  都是同一份 markdown 子集文本里的块，不切换控件、不弹子编辑器。

  硬约束（测试锁死）：
  - 只有**一个** textarea：格式动作只改文本 + 把选区还给用户（真实 textarea 的
    setSelectionRange），不重建 DOM，因此「在文字 / 链接 / 代码之间继续输入、移动光标、
    选择、撤销重做」全部由浏览器原生行为负责 —— 组件**不自建撤销栈**、不拦输入事件；
  - 代码块逐字保真：解析与序列化都在 interactive/textCard.ts（纯函数，唯一实现）；
  - 输入即 emit change { docText, doc }：草稿与提交由父组件负责（这里不碰 store、不发请求）；
  - 诚实反馈：插入链接时用户取消输入地址、剪贴板不可用，都如实写在编辑区内，不假装成功。
-->
<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from "vue";
import {
  FENCE,
  detectLanguage,
  docFromCard,
  parseDocText,
  serializeDocText,
  wrapSelection,
  type InlineFormat,
  type TextDoc,
} from "./textCard";
import type { BoardCard } from "./types";

const props = withDefaults(
  defineProps<{
    card: BoardCard;
    /**
     * 编辑区显示的那一份文本（可选）。
     *
     * 为什么要有它：用户正在编辑的可能是**未完成输入**（草稿恢复、冲突候选），
     * 它与卡片正式字段不是同一份；父组件说了算，组件不自己猜。
     * 没给时才从 card 推导（meta.docText → meta.doc → content）。
     */
    value?: string;
    /** 只读（例如预览里的副本）：编辑面只读、工具栏禁用 */
    readonly?: boolean;
    /** 编辑区最小高度（显示参数，不改变任何格式语义） */
    minHeight?: number;
  }>(),
  { value: undefined, readonly: false, minHeight: 160 },
);

const emit = defineEmits<{
  (e: "change", payload: { docText: string; doc: TextDoc }): void;
}>();

const area = ref<HTMLTextAreaElement | null>(null);

/** 编辑区的唯一文本状态：父组件给的 value 优先，否则从 card 推导。 */
function initialText(): string {
  return typeof props.value === "string" ? props.value : serializeDocText(docFromCard(props.card));
}
const text = ref(initialText());

/** 用户可读的当前状态（诚实反馈：插入链接取消了输入、格式动作的结果等）。 */
const note = ref("");

const textareaStyle = computed(() => ({ minHeight: props.minHeight + "px" }));

/**
 * 连续编辑区：编辑框**跟随内容自动增高，自己不再滚动**。
 *
 * 为什么必须这样：编辑态的纵向滚动只允许有一层（卡片正文 .card-body）。
 * 编辑框如果自己也能滚，就会变成「外层正文滚一段、内层编辑框再滚一段」——
 * 同一个连续编辑动作被拆成两个小窗口，光标位置与可见区间对不上。
 *
 * 只改显示高度：文本、选区、撤销重做、提交内容与已保存尺寸都不受影响。
 * jsdom / 还没布局时 scrollHeight 为 0，这时不写高度（绝不把编辑框压成 0）。
 */
function autosize(): void {
  const el = area.value;
  if (!el) return;
  const content = el.scrollHeight;
  if (!content) return;
  el.style.height = "auto";
  el.style.height = content + "px";
}

onMounted(() => {
  autosize();
});

/** 工具栏动作 → 编辑区文本（唯一实现是纯函数 wrapSelection）。 */
const ACTIONS: { action: string; label: string; title: string; format: InlineFormat }[] = [
  { action: "bold", label: "加粗", title: "加粗选中文字（**文字**）", format: "bold" },
  { action: "italic", label: "斜体", title: "斜体选中文字（*文字*）", format: "italic" },
  { action: "code", label: "行内代码", title: "行内代码（一对反引号）", format: "code" },
  { action: "strike", label: "删除线", title: "删除线（~~文字~~）", format: "strike" },
  { action: "heading2", label: "标题2", title: "把选中行变成二级标题（## 文字）", format: "heading2" },
  { action: "list", label: "列表", title: "把选中行变成列表（- 文字）", format: "list" },
  { action: "quote", label: "引用", title: "把选中行变成引用（> 文字）", format: "quote" },
  { action: "link", label: "插入链接", title: "把选中文字变成链接（[标题](地址)）", format: "link" },
];

/**
 * 改文本 → emit change（输入即发，父组件负责草稿与提交）。
 *
 * 选区必须还给用户，而且**现在就要还**：先把新文本写进真实 textarea，
 * 再把选区设好，然后才更新响应式状态 —— 这样渲染时 DOM 值已经相同，
 * 浏览器不会在 patch 里把光标挪到末尾（同步 setSelectionRange 也不会被后续渲染清掉）。
 */
function apply(next: string, selection?: { start: number; end: number }): void {
  const el = area.value;
  if (el && el.value !== next) el.value = next;
  if (el && selection) el.setSelectionRange(selection.start, selection.end);
  text.value = next;
  autosize();
  emit("change", { docText: next, doc: parseDocText(next) });
}

function onInput(event: Event): void {
  const value = (event.target as HTMLTextAreaElement).value;
  text.value = value;
  autosize();
  emit("change", { docText: value, doc: parseDocText(value) });
}

/**
 * 选中区域的结尾（去掉尾部空白）：链接的 [标题] 里不能夹换行或尾随空格。
 * 只影响这一次动作的计算，不改用户自己的选区。
 */
function trimEndIndex(value: string, start: number, end: number): number {
  let at = Math.max(start, end);
  while (at > start && /\s/.test(value[at - 1])) at -= 1;
  return at;
}

function currentSelection(): { start: number; end: number } {
  const el = area.value;
  if (!el) return { start: text.value.length, end: text.value.length };
  return { start: el.selectionStart ?? 0, end: el.selectionEnd ?? 0 };
}

function runFormat(format: InlineFormat): void {
  if (props.readonly) return;
  const el = area.value;
  const { start, end } = currentSelection();
  if (format === "link") {
    const raw = typeof globalThis.prompt === "function" ? globalThis.prompt("链接地址（http/https）：", "https://") : null;
    const href = typeof raw === "string" ? raw.trim() : "";
    // 链接必须单独成行：选中区域末尾的空白（尤其是块后空行）不能包进 [标题] 里，
    // 否则方括号里会夹一个换行，解析时就不再是链接块（而是一段普通文字）。
    const labelEnd = trimEndIndex(text.value, start, end);
    const label = text.value.slice(start, labelEnd) || "链接";
    const result = wrapSelection(text.value, start, labelEnd, "link", href);
    apply(result.text, result.selection);
    note.value = href
      ? "已插入链接：" + label + " → " + href + "（点「完成编辑」后生效）"
      : "地址为空：链接地址还是空的，这张卡里的链接现在打不开；可以选中它再点「插入链接」补地址。";
    return;
  }
  const result = wrapSelection(text.value, start, end, format);
  apply(result.text, result.selection);
  note.value = el && document.activeElement !== el ? "格式已应用到选中的文字（编辑区仍在原文位置）" : "格式已应用到选中的文字";
}

/** 代码块：插入一对围栏，光标落在围栏之间（正文逐字归属 code 块）。 */
function insertCodeBlock(): void {
  if (props.readonly) return;
  const { start, end } = currentSelection();
  const selected = text.value.slice(start, end);
  const before = text.value.slice(0, start);
  const after = text.value.slice(end);
  const lead = before.length > 0 && !before.endsWith("\n") ? "\n" : "";
  const tail = after.length > 0 && !after.startsWith("\n") ? "\n" : "";
  const opener = lead + FENCE + "\n";
  const suffix = "\n" + FENCE + "\n" + tail;
  const next = before + opener + selected + suffix + after;
  const bodyAt = start + opener.length;
  apply(next, { start: bodyAt + selected.length, end: bodyAt + selected.length });
  note.value = "已插入一对代码围栏：语言写在第一行的围栏后面，正文逐字保留缩进与换行。";
}

/**
 * 粘贴时把一段带围栏的代码识别成代码块：首行是围栏就取语言，末行围栏去掉。
 * 没有可识别的语言就原样粘贴 —— 绝不改写用户粘贴的内容结构。
 */
function onPaste(event: ClipboardEvent): void {
  const raw = event.clipboardData?.getData("text/plain") ?? "";
  if (props.readonly || !raw.includes(FENCE)) return;
  const lines = raw.split("\n");
  if (!lines[0].startsWith(FENCE)) return;
  let end = lines.length;
  while (end > 1 && lines[end - 1].trim() === "") end -= 1;
  if (end < 2 || !lines[end - 1].startsWith(FENCE)) return;
  const inner = lines.slice(1, end - 1);
  const language = lines[0].slice(FENCE.length).trim() || detectLanguage(selectedLabel(inner));
  event.preventDefault();
  const { start, end: endSelection } = currentSelection();
  const body = inner.join("\n");
  const block = FENCE + language + "\n" + body + "\n" + FENCE;
  const before = text.value.slice(0, start);
  const after = text.value.slice(endSelection);
  const lead = before.length > 0 && !before.endsWith("\n") ? "\n" : "";
  const tail = after.length > 0 && !after.startsWith("\n") ? "\n" : "";
  const next = before + lead + block + tail + after;
  const at = before.length + lead.length + block.length;
  apply(next, { start: at, end: at });
  note.value = "已把粘贴的内容识别成代码块" + (language ? "（语言：" + language + "）" : "") + "：正文逐字保留。";
}

/** 从代码正文里找一行像文件路径的内容，用来推断语言（拿不到就返回空串）。 */
function selectedLabel(lines: string[]): string {
  for (const line of lines) {
    const trimmed = line.trim();
    if (/^[#/]{0,2}\s*\S+\.\w{1,8}$/.test(trimmed)) return trimmed.replace(/^[#/]{0,2}\s*/, "");
  }
  return "";
}

/**
 * 外部改了编辑区文本就跟随：草稿恢复、冲突里选「用服务器上的」、切换到另一张卡、
 * 父组件按块序列重建卡片 —— 编辑面都必须立刻显示那一份，不能停在旧文字上。
 *
 * 只比较**文本**，不比较对象身份：同一份内容反复设置不会打断用户输入。
 */
watch(
  () => [props.value, props.card.meta?.doc, props.card.meta?.docText, props.card.content],
  () => {
    const next = initialText();
    if (next !== text.value) text.value = next;
    // 换了一份文本（草稿恢复、冲突里选服务器上的、切到另一张卡）：高度跟着内容重算
    void nextTick(autosize);
  },
  { deep: true },
);

defineExpose({
  /** 把焦点与光标放到某一行（卡片显示里点「编辑」时用；行号从 0 开始）。 */
  async focusLine(line: number): Promise<void> {
    await nextTick();
    const el = area.value;
    if (!el) return;
    const at = offsetOfLine(text.value, line);
    el.focus();
    el.setSelectionRange(at, at);
  },
});

/** 第 line 行的起始偏移（超出范围时落到文本末尾，不抛错）。 */
function offsetOfLine(value: string, line: number): number {
  if (line <= 0) return 0;
  let seen = 0;
  for (let index = 0; index < value.length; index += 1) {
    if (value[index] === "\n") {
      seen += 1;
      if (seen === line) return index + 1;
    }
  }
  return value.length;
}
</script>

<template>
  <div class="text-card-editor" data-im="text-card-editor" data-qio-fit>
    <div class="editor-toolbar" data-im="card-editor-toolbar" role="toolbar" aria-label="文字卡格式" @pointerdown.prevent>
      <button
        v-for="item in ACTIONS"
        :key="item.action"
        class="fmt"
        type="button"
        :data-im="'card-fmt-' + item.action"
        :title="item.title"
        :disabled="readonly"
        @click="runFormat(item.format)"
      >
        {{ item.label }}
      </button>
      <button
        class="fmt"
        type="button"
        data-im="card-fmt-codeblock"
        title="插入一对代码围栏（语言写在第一行围栏后面）"
        :disabled="readonly"
        @click="insertCodeBlock"
      >
        插入代码块
      </button>
    </div>

    <textarea
      ref="area"
      class="editor"
      data-im="card-editor"
      :data-card-id="card.id"
      :value="text"
      :readonly="readonly"
      :style="textareaStyle"
      rows="8"
      spellcheck="false"
      :aria-label="readonly ? '只读文字卡正文' : '编辑文字卡正文（文字、链接、代码在同一张卡片里）'"
      @input="onInput"
      @paste="onPaste"
    ></textarea>

    <p v-if="note" class="fmt-note" data-im="card-fmt-note" role="status">{{ note }}</p>
    <p class="editor-note">
      文字、链接、代码写在同一张卡片里；格式用 markdown 子集（**粗体**、*斜体*、行内代码、~~删除线~~、## 标题、- 列表、&gt; 引用、[标题](地址)）。
    </p>
  </div>
</template>

<style scoped>
.text-card-editor { display: flex; flex-direction: column; gap: var(--sp-2); }

.editor-toolbar { display: flex; flex-wrap: wrap; align-items: center; gap: var(--sp-1); }
.fmt {
  font: inherit;
  font-size: var(--fs-xs);
  color: var(--text-secondary);
  background: var(--bg-surface);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-xs);
  padding: 1px var(--sp-2);
  cursor: pointer;
  white-space: nowrap;
}
.fmt:hover { color: var(--text-strong); border-color: var(--border-strong); }
.fmt:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 1px; }
.fmt:disabled { color: var(--text-faint); cursor: default; }

/*
  编辑面：等宽字体（三声部里的数据声部）—— 代码块、缩进与围栏都必须看得到空白。
  不重建 DOM、不拦输入：原生撤销/重做、光标与选区都由浏览器负责。

  overflow: hidden + 脚本里的 autosize：编辑框自己**从不滚动**，高度跟随内容长；
  纵向滚动因此只有卡片正文这一层（否则就是外层正文 + 内层编辑框两处各滚一段）。
  min-height 是显示参数（父组件按卡片高度给定），只影响一屏内的高度，不影响提交内容。
*/
.editor {
  width: 100%;
  box-sizing: border-box;
  font-family: var(--mono);
  font-size: var(--fs-sm);
  line-height: var(--lh-base);
  color: var(--text-primary);
  background: var(--bg-inset);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-xs);
  padding: var(--sp-2);
  overflow: hidden;
  resize: vertical;
}
.editor:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 1px; }

.fmt-note { margin: 0; font-size: var(--fs-xs); color: var(--text-secondary); line-height: var(--lh-tight); }
.editor-note { margin: 0; font-size: var(--fs-xs); color: var(--text-faint); line-height: var(--lh-tight); }
</style>
