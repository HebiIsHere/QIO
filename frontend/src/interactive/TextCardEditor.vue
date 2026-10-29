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
    /**
     * 吃满卡片正文区（2026-10-14 用户要求：输入框占满整张卡片）。
     *
     * 关闭（缺省）时保持既有行为：编辑框跟随内容自动增高、自己不滚动，
     * 纵向滚动只由卡片正文这一层承担。打开时编辑框吃满剩余高度并自己滚动 ——
     * 这时「输入框占满卡片」是用户明确要的形态，卡片正文不再出现输入框下方的空白。
     */
    fill?: boolean;
  }>(),
  { value: undefined, readonly: false, minHeight: 160, fill: false },
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

/** 用户可读的当前状态（诚实反馈：链接动作与格式动作的结果等）。 */
const note = ref("");

/* ------------------------------------------------------------------ */
/* 链接行：在卡片里就地输入网址（不再用浏览器原生 prompt）              */
/* ------------------------------------------------------------------ */
/**
 * 为什么不再用 prompt：原生对话框会抢焦点、挡住卡片、无法说明「哪段文字变成链接」，
 * 也不能在取消后把选区还给用户；更不符合「不用原生 alert/confirm/prompt」的硬约束。
 *
 * 行内两种模式：
 * - insert：把当前选区（没有选区就插入「链接」两个字）变成 [标题](地址)；
 * - edit：光标/选区落在已有链接里时，直接改这条链接的地址或把它移除（文字保留）。
 * 确认 / 取消都留在卡片里；地址不合法就在行内说原因，不写坏正文。
 */
interface LinkRowState {
  open: boolean;
  mode: "insert" | "edit";
  label: string;
  href: string;
  range: { start: number; end: number } | null;
  error: string;
  composing: boolean;
}
const linkRow = ref<LinkRowState>({
  open: false,
  mode: "insert",
  label: "",
  href: "",
  range: null,
  error: "",
  composing: false,
});
const linkInput = ref<HTMLInputElement | null>(null);

interface LinkSpan {
  start: number;
  end: number;
  label: string;
  href: string;
}

/** 找到包含 [start,end] 的那条 markdown 链接（行内链接，不跨行）。 */
function findLinkSpan(value: string, start: number, end: number): LinkSpan | null {
  const pattern = /\[([^\]\n]*)\]\(([^)\n]*)\)/g;
  let match: RegExpExecArray | null;
  while ((match = pattern.exec(value)) !== null) {
    const from = match.index;
    const to = from + match[0].length;
    if (start >= from && end <= to) {
      return { start: from, end: to, label: match[1], href: match[2] };
    }
  }
  return null;
}

/** 地址校验：只接受 http / https / mailto（与卡片打开链接的白名单一致）。 */
function normalizeLinkHref(raw: string): { href: string; error: string } {
  const href = raw.trim();
  if (!href) {
    return { href: "", error: "网址不能为空：卡片里的链接现在会打不开。" };
  }
  if (/^https?:\/\//i.test(href) || /^mailto:/i.test(href)) {
    return { href, error: "" };
  }
  return {
    href,
    error: "只支持 http / https / mailto 链接：其他写法不会被当成可打开的地址。",
  };
}

function returnFocusToEditor(selection?: { start: number; end: number }): void {
  const el = area.value;
  if (!el) return;
  el.focus();
  if (selection) el.setSelectionRange(selection.start, selection.end);
}

/** 打开链接行：落在已有链接里就是「改这条」，否则是「插入新链接」。 */
function openLinkRow(): void {
  if (props.readonly) return;
  if (linkRow.value.open) return;
  const el = area.value;
  const { start, end } = currentSelection();
  const existing = el ? findLinkSpan(text.value, start, end) : null;
  if (existing) {
    linkRow.value = {
      open: true,
      mode: "edit",
      label: existing.label || "链接",
      href: existing.href,
      range: { start: existing.start, end: existing.end },
      error: "",
      composing: false,
    };
  } else {
    const labelEnd = trimEndIndex(text.value, start, end);
    linkRow.value = {
      open: true,
      mode: "insert",
      label: text.value.slice(start, labelEnd) || "链接",
      href: "",
      range: { start, end: labelEnd },
      error: "",
      composing: false,
    };
  }
  note.value = "";
  void nextTick(() => {
    const input = linkInput.value;
    if (!input) return;
    input.focus();
    const at = input.value.length;
    input.setSelectionRange(at, at);
  });
}

/** 就地取消：正文一个字都不改，焦点与选区回到编辑区。 */
function cancelLink(): void {
  if (!linkRow.value.open) return;
  const range = linkRow.value.range;
  linkRow.value = { open: false, mode: "insert", label: "", href: "", range: null, error: "", composing: false };
  note.value = "已取消这次链接修改：正文没有变化。";
  returnFocusToEditor(range ?? undefined);
}

function onLinkEnter(event: KeyboardEvent): void {
  // 输入法组合输入期间的回车只是确认候选字，不能当成「确认链接」。
  if (event.isComposing || linkRow.value.composing) return;
  confirmLink();
}

/** 确认：插入 / 更新都在正文原位完成；地址不合法就停在行内说原因。 */
function confirmLink(): void {
  const row = linkRow.value;
  if (!row.open) return;
  const checked = normalizeLinkHref(row.href);
  if (checked.error) {
    row.error = checked.error;
    linkInput.value?.focus();
    return;
  }
  const source = text.value;
  const label = row.label || "链接";
  const inserted = "[" + label + "](" + checked.href + ")";
  if (row.mode === "edit" && row.range) {
    // 行打开期间用户可能继续改过正文：按当前内容重新定位这条链接，找不到就用原位置（钳进范围）。
    const fresh = findLinkSpan(source, row.range.start, row.range.start) ?? null;
    const start = fresh ? fresh.start : Math.max(0, Math.min(source.length, row.range.start));
    const end = fresh ? fresh.end : Math.max(start, Math.min(source.length, row.range.end));
    if (fresh && fresh.href === checked.href && fresh.label === label) {
      note.value = "这条链接没有变化（地址与标题都还是原来的）。";
      closeLinkRow();
      returnFocusToEditor({ start: start + 1, end: start + 1 + label.length });
      return;
    }
    const next = source.slice(0, start) + inserted + source.slice(end);
    apply(next, { start: start + 1, end: start + 1 + label.length });
    note.value = "已更新链接地址：" + label + " → " + checked.href + "（点「完成编辑」后生效）";
    closeLinkRow();
    return;
  }
  const range = row.range ?? { start: source.length, end: source.length };
  const start = Math.max(0, Math.min(source.length, range.start));
  const end = Math.max(start, Math.min(source.length, range.end));
  const next = source.slice(0, start) + inserted + source.slice(end);
  apply(next, { start: start + 1, end: start + 1 + label.length });
  note.value =
    "已插入链接：" + label + " → " + checked.href + "（点「完成编辑」后生效）";
  closeLinkRow();
}

function closeLinkRow(): void {
  linkRow.value = { open: false, mode: "insert", label: "", href: "", range: null, error: "", composing: false };
}

/** 移除链接：只去掉 [..](..) 的包装，文字留在正文里。 */
function removeLink(): void {
  const row = linkRow.value;
  if (!row.open || row.mode !== "edit" || !row.range) return;
  const source = text.value;
  const fresh = findLinkSpan(source, row.range.start, row.range.start) ?? null;
  const start = fresh ? fresh.start : Math.max(0, Math.min(source.length, row.range.start));
  const end = fresh ? fresh.end : Math.max(start, Math.min(source.length, row.range.end));
  const label = fresh ? fresh.label : row.label || "链接";
  const next = source.slice(0, start) + label + source.slice(end);
  apply(next, { start, end: start + label.length });
  note.value = "已移除链接：" + (label || "这段文字") + " 现在是普通文字（文字本身保留）。";
  closeLinkRow();
}

const textareaStyle = computed(() =>
  props.fill ? { minHeight: "0px" } : { minHeight: props.minHeight + "px" },
);

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
  // 吃满卡片时不写高度：flex 决定高度，写死高度会把「占满」又变回「跟内容一样高」。
  if (props.fill) return;
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
  // 上一次格式动作的反馈只说「刚才那一下」：用户继续打字后就不该再占着编辑区
  if (note.value) note.value = "";
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
    // 链接不再走原生 prompt：在卡片里打开网址输入行，确认 / 取消 / 移除都在原位。
    openLinkRow();
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
  <div
    class="text-card-editor"
    :class="{ 'is-fill': fill }"
    data-im="text-card-editor"
    data-qio-fit
  >
    <div class="editor-toolbar" data-im="card-editor-toolbar" role="toolbar" aria-label="文字卡格式" @pointerdown.prevent>
      <button
        v-for="item in ACTIONS"
        :key="item.action"
        class="fmt"
        type="button"
        :data-im="'card-fmt-' + item.action"
        :aria-label="item.title"
        :aria-expanded="item.format === 'link' ? (linkRow.open ? 'true' : 'false') : undefined"
        :disabled="readonly"
        @click="runFormat(item.format)"
      >
        {{ item.label }}
      </button>
      <button
        class="fmt"
        type="button"
        data-im="card-fmt-codeblock"
        aria-label="插入一对代码围栏（语言写在第一行围栏后面）"
        :disabled="readonly"
        @click="insertCodeBlock"
      >
        插入代码块
      </button>
    </div>

    <!--
      链接行：网址就地输入（替代原生 prompt）。
      只改这一条链接：确认 / 取消 / 移除都在卡片里；错误停在行内，不写坏正文。
      键盘：Enter 确认（输入法组合输入期间不算）、Esc 取消；事件不再冒泡到板面手势。
    -->
    <div
      v-if="linkRow.open"
      class="link-row"
      data-im="card-link-row"
      role="group"
      :aria-label="linkRow.mode === 'edit' ? '修改链接地址' : '插入链接地址'"
      @keydown.esc.stop.prevent="cancelLink"
    >
      <span class="link-row-label" data-im="card-link-label">
        {{ linkRow.mode === "edit" ? "修改链接" : "插入链接" }}：{{ linkRow.label }}
      </span>
      <div class="link-row-line">
        <input
          ref="linkInput"
          v-model="linkRow.href"
          class="link-row-input"
          data-im="card-link-input"
          type="text"
          inputmode="url"
          autocomplete="off"
          spellcheck="false"
          placeholder="https://"
          :aria-label="'链接地址（' + linkRow.label + '）'"
          @keydown.enter.prevent="onLinkEnter"
          @compositionstart="linkRow.composing = true"
          @compositionend="linkRow.composing = false"
        />
        <button class="qio-btn mini primary" type="button" data-im="card-link-confirm" @click="confirmLink">
          确认
        </button>
        <button class="qio-btn mini quiet" type="button" data-im="card-link-cancel" @click="cancelLink">
          取消
        </button>
        <button
          v-if="linkRow.mode === 'edit'"
          class="qio-btn mini quiet"
          type="button"
          data-im="card-link-remove"
          @click="removeLink"
        >
          移除链接
        </button>
      </div>
      <p v-if="linkRow.error" class="link-row-error" role="alert" data-im="card-link-error">
        {{ linkRow.error }}
      </p>
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
  </div>
</template>

<style scoped>
.text-card-editor { display: flex; flex-direction: column; gap: var(--sp-2); }
/*
  吃满卡片（fill）：这一层先吃满卡片正文区，输入框再吃满这一层剩下的高度。
  父级不是 flex 时这两条不生效，独立挂载的用法与既有行为一致。
*/
.text-card-editor.is-fill { flex: 1 1 auto; min-height: 0; }
.text-card-editor.is-fill .editor { flex: 1 1 0%; min-height: 0; overflow: auto; resize: none; }

.editor-toolbar { display: flex; flex-wrap: wrap; align-items: center; gap: var(--sp-1); }
.fmt {
  font: inherit;
  font-size: var(--fs-xs);
  color: var(--text-secondary);
  background: var(--bg-surface);
  border: 1px solid var(--border-subtle);
  border-radius: var(--qio-ui-radius);
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
  border-radius: var(--qio-ui-radius);
  padding: var(--sp-2);
  overflow: hidden;
  resize: vertical;
}
.editor:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 1px; }

.fmt-note { margin: 0; font-size: var(--fs-xs); color: var(--text-secondary); line-height: var(--lh-tight); }

/* 链接行：就地输入网址；窄容器里按钮换行，不把卡片撑宽 */
.link-row {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
  min-width: 0;
  padding: var(--sp-2);
  border: 1px solid var(--border-strong);
  border-radius: var(--qio-ui-radius);
  background: var(--bg-elevated);
}
.link-row-label {
  font-size: var(--fs-xs);
  color: var(--text-secondary);
  overflow-wrap: anywhere;
}
.link-row-line {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--sp-1);
  min-width: 0;
}
.link-row-input {
  flex: 1 1 160px;
  min-width: 0;
  height: 28px;
  padding: 0 var(--sp-2);
  border: 1px solid var(--border-strong);
  border-radius: var(--qio-ui-radius);
  background: var(--bg-inset);
  color: var(--text-primary);
  font-family: var(--mono);
  font-size: var(--fs-xs);
}
.link-row-input:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 1px;
}
.link-row-error {
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--danger);
  overflow-wrap: anywhere;
}
</style>
