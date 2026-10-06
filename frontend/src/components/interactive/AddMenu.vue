<!--
  添加菜单（子智能体 A 负责）：文字 / 文件 / 图片 / 代码 / 网址五类材料的统一入口。

  契约：docs/interactive-mode-contract.md
  - §8.1：添加菜单是五类的统一入口，文字卡片不再单独常驻在工具栏上；
  - §8.4.1：会改板面状态的操作由触发方自己调 board.ts 纯函数 + store.commit（不做命令总线）。

  归属决定（题面二选一，已定）：**本组件直接调 store.commit(addCard(...), "添加…")**，
  不把选择再 emit 给工具栏转一次手 —— 添加只依赖 board.ts 与 store，工具栏只负责把它放进底部栏。

  收起时用 v-if 而不是 v-show：隐藏元素留在 DOM 里会让「按钩子驱动」的验收出现假通过
  （lead 在 task-6 里明确要求：收起后这些按钮不该存在）。
-->
<script setup lang="ts">
import { nextTick, onBeforeUnmount, ref, watch } from "vue";
import { useInteractiveStore } from "../../stores/interactive";
import { addCard, CARD_KIND_LABELS } from "../../interactive/board";
import type { CardKind } from "../../interactive/types";

/** 可添加的五类；reply 是 QIO 结果产生的卡片，不由用户「添加」。 */
type AddableKind = Exclude<CardKind, "reply">;

interface AddItem {
  kind: AddableKind;
  label: string;
  hint: string;
}

const store = useInteractiveStore();
const open = ref(false);
const root = ref<HTMLElement | null>(null);
const trigger = ref<HTMLButtonElement | null>(null);
const popup = ref<HTMLElement | null>(null);

/**
 * 五类入口与说明。说明里必须写清权限边界（不能只靠颜色或图标表达）：
 * 文字注释默认未勾选（勾选后才允许 QIO 查看，而且仍要提交）；材料默认在本次允许查看范围内。
 */
const ITEMS: AddItem[] = [
  { kind: "text", label: "文字注释", hint: "默认未勾选：勾选后才允许 QIO 查看，而且仍要提交" },
  { kind: "file", label: "文件", hint: "材料默认在本次允许查看范围内，不需要勾选" },
  { kind: "image", label: "图片", hint: "材料默认在本次允许查看范围内，不需要勾选" },
  { kind: "code", label: "代码", hint: "材料默认在本次允许查看范围内，不需要勾选" },
  { kind: "url", label: "网址", hint: "材料默认在本次允许查看范围内，不需要勾选" },
];

/**
 * 新卡片的初始内容：与旧 BoardCanvas.onAdd 的五份默认值一致（同一个「添加文件」不能有两种默认值）。
 * B 正在把这份默认值抽到 board.ts 的 defaultCardSeed(kind)；他给出后这里改成引用，避免两处漂移。
 */
const SEEDS: Record<AddableKind, { content: string; meta: Record<string, unknown> }> = {
  text: { content: "", meta: {} },
  file: { content: "待补充文件说明", meta: { name: "未命名文件" } },
  image: { content: "", meta: { name: "未命名图片" } },
  code: { content: "// 待补充代码", meta: { language: "text" } },
  url: { content: "", meta: { href: "https://", title: "待补充标题" } },
};

function toggle(): void {
  open.value = !open.value;
}

/** 收起菜单；returnFocus=true 时把焦点还给「添加」按钮（键盘用户不会掉焦点）。 */
function close(returnFocus = false): void {
  if (!open.value) return;
  open.value = false;
  if (returnFocus) void nextTick(() => trigger.value?.focus());
}

function add(kind: AddableKind): void {
  const current = store.board;
  if (!current) return;
  const seed = SEEDS[kind];
  store.commit(addCard(current, { kind, content: seed.content, meta: seed.meta }), "添加" + CARD_KIND_LABELS[kind]);
  close(true);
}

/** 点菜单外面收起。 */
function onDocumentPointerDown(event: PointerEvent): void {
  const target = event.target as Node | null;
  if (target && root.value && !root.value.contains(target)) close(false);
}

/** 菜单开着时 Esc 只收起菜单，不让板面把它当成「取消拖动 / 清空选择」。 */
function onDocumentKeydown(event: KeyboardEvent): void {
  if (event.key !== "Escape") return;
  event.stopPropagation();
  close(true);
}

watch(open, async (value) => {
  if (value) {
    document.addEventListener("pointerdown", onDocumentPointerDown, true);
    document.addEventListener("keydown", onDocumentKeydown);
    await nextTick();
    popup.value?.querySelector("button")?.focus();
  } else {
    document.removeEventListener("pointerdown", onDocumentPointerDown, true);
    document.removeEventListener("keydown", onDocumentKeydown);
  }
});

onBeforeUnmount(() => {
  document.removeEventListener("pointerdown", onDocumentPointerDown, true);
  document.removeEventListener("keydown", onDocumentKeydown);
});
</script>

<template>
  <div ref="root" class="add">
    <button
      ref="trigger"
      class="tb-btn"
      type="button"
      data-im="add-menu"
      aria-haspopup="menu"
      :aria-expanded="open"
      title="添加文字注释或材料：文件 / 图片 / 代码 / 网址"
      @click="toggle"
    >
      <span class="plus" aria-hidden="true">＋</span>添加
    </button>

    <div v-if="open" ref="popup" class="add-pop" data-im="add-menu-list" role="menu" aria-label="添加到板面">
      <p class="pop-title">添加到板面</p>
      <button
        v-for="item in ITEMS"
        :key="item.kind"
        class="pop-item"
        type="button"
        role="menuitem"
        :data-im="'add-' + item.kind"
        @click="add(item.kind)"
      >
        <span class="item-label">{{ item.label }}</span>
        <span class="item-hint">{{ item.hint }}</span>
      </button>
      <p class="pop-note">只加到本地板面：保存不调用 QIO，提交才会。</p>
    </div>
  </div>
</template>

<style scoped>
.add { position: relative; display: inline-flex; }
/* 与 BoardToolbar 的 .tb-btn 同一套兜底样式：scoped 样式不能跨组件，
   所以这里必须自带一份，保证菜单没被父级包住时也是同一个样子。 */
.tb-btn {
  font: inherit;
  font-size: var(--fs-xs);
  line-height: 1.6;
  color: var(--text-secondary);
  background: var(--bg-surface);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-xs);
  padding: 2px var(--sp-2);
  cursor: pointer;
  white-space: nowrap;
}
.tb-btn:hover { color: var(--text-strong); border-color: var(--border-strong); }
.tb-btn:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 1px; }
.tb-btn[aria-expanded="true"] { color: var(--text-strong); border-color: var(--border-strong); background: var(--bg-inset); }
.plus { color: var(--link); }

/* 菜单从工具栏上方展开（工具栏贴底，向下没有空间） */
.add-pop {
  position: absolute;
  left: 0;
  bottom: calc(100% + var(--sp-2));
  z-index: 2;
  min-width: min(320px, calc(100vw - 48px));
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
  padding: var(--sp-2);
  background: var(--bg-elevated);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-md);
  box-shadow: var(--shadow-2);
}
.pop-title { margin: 0 0 var(--sp-1); font-size: var(--fs-xs); color: var(--text-faint); }
.pop-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
  text-align: left;
  font: inherit;
  background: none;
  border: 1px solid transparent;
  border-radius: var(--r-xs);
  padding: var(--sp-1) var(--sp-2);
  cursor: pointer;
}
.pop-item:hover { background: var(--bg-surface); border-color: var(--border-subtle); }
.pop-item:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 1px; }
.item-label { font-size: var(--fs-sm); color: var(--text-strong); }
.item-hint { font-size: var(--fs-xs); color: var(--text-faint); }
.pop-note {
  margin: var(--sp-1) 0 0;
  padding-top: var(--sp-1);
  border-top: 1px solid var(--border-subtle);
  font-size: var(--fs-xs);
  color: var(--text-muted);
}
</style>
