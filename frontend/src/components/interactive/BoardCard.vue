<!-- 单张板面卡片（子智能体 A 负责）：文字注释 / 文件 / 图片 / 代码 / 网址 / QIO 结果。

  契约：docs/interactive-mode-contract.md §1.1 / §1.4 / §4.4。
  三条与语义有关的显示规则：
  - 勾选框只出现在文字注释上（材料默认在 QIO 可查看范围内，不需要勾选；reply 不参与）；
  - 状态一律「文字 + 颜色」双通道，不能只靠颜色；
  - 卡片本身不做任何状态计算：所有变化 emit 给 BoardCanvas，由它调 board.ts 纯函数 + store.commit。
-->
<script setup lang="ts">
import { computed, ref } from "vue";
import { useInteractiveStore } from "../../stores/interactive";
import { CARD_KIND_LABELS, CHECKABLE_KINDS } from "../../interactive/board";
import type { BoardCard, BoardGroup } from "../../interactive/types";

const props = defineProps<{
  card: BoardCard;
  selected: boolean;
  /** 板内搜索定位后的短暂高亮（只是显示） */
  highlight: boolean;
  dragging: boolean;
  x: number;
  y: number;
  groupName: string | null;
  groups: BoardGroup[];
}>();

const emit = defineEmits<{
  (e: "select", cardId: string, additive: boolean): void;
  (e: "drag-start", cardId: string, event: PointerEvent): void;
  (e: "patch", cardId: string, patch: Partial<BoardCard>, label: string): void;
  (e: "toggle", cardId: string, flag: "checked" | "hidden" | "folded" | "bookmarked"): void;
  (e: "remove", cardId: string): void;
  (e: "duplicate", cardId: string): void;
  (e: "leave-group", cardId: string): void;
  (e: "join-group", cardId: string, groupId: string): void;
}>();

const store = useInteractiveStore();
const editing = ref(false);
const draft = ref("");
const metaName = ref("");
const metaLanguage = ref("");
const metaHref = ref("");
const metaTitle = ref("");
const joinTarget = ref("");

const checkable = computed(() => CHECKABLE_KINDS.includes(props.card.kind));
const kindLabel = computed(() => CARD_KIND_LABELS[props.card.kind]);
const name = computed(() => String(props.card.meta?.name ?? ""));
const language = computed(() => String(props.card.meta?.language ?? ""));
const href = computed(() => String(props.card.meta?.href ?? ""));
const linkTitle = computed(() => String(props.card.meta?.title ?? "") || href.value);

const headline = computed(() => {
  const text = (props.card.content || name.value || linkTitle.value).trim();
  if (!text) return "（还没有内容）";
  return text.length > 24 ? text.slice(0, 24) + "…" : text;
});

/** 状态说明：颜色之外必须能读出来。 */
const statusText = computed(() => {
  const parts: string[] = [];
  if (props.card.hidden) parts.push("已隐藏：退出讨论范围");
  else if (checkable.value) {
    parts.push(props.card.checked ? "已勾选：本次允许 QIO 查看（仍需提交）" : "未勾选：QIO 看不到它的文字");
  } else if (props.card.kind === "reply") parts.push("QIO 结果：不进提交载荷");
  else parts.push("材料：默认在本次允许查看范围内（不需要勾选）");
  if (props.card.folded) parts.push("已折叠：只改变显示");
  if (props.card.bookmarked) parts.push("书签：查找优先");
  if (props.groupName) parts.push("在组「" + props.groupName + "」");
  return parts.join(" · ");
});

const style = computed(() => ({
  left: props.x + "px",
  top: props.y + "px",
  width: props.card.w + "px",
  height: props.card.folded ? "auto" : props.card.h + "px",
  zIndex: props.dragging ? 30 : props.selected ? 8 : 4,
}));

function onPointerDown(event: PointerEvent) {
  const target = event.target as HTMLElement | null;
  // 按钮 / 输入框 / 链接上的按下不算拖动
  if (target && target.closest("button, input, textarea, select, a, label")) return;
  emit("select", props.card.id, event.shiftKey || event.ctrlKey || event.metaKey);
  emit("drag-start", props.card.id, event);
}

function startEdit() {
  editing.value = true;
  draft.value = store.draftFor("card:" + props.card.id) || props.card.content;
  metaName.value = name.value;
  metaLanguage.value = language.value;
  metaHref.value = href.value;
  metaTitle.value = String(props.card.meta?.title ?? "");
  store.setDraft("card:" + props.card.id, draft.value);
}

function onDraftInput() {
  store.setDraft("card:" + props.card.id, draft.value);
}

/** 确认编辑才形成有效文字状态；输入过程只存草稿，不调用 QIO。 */
function confirmEdit() {
  const patch: Partial<BoardCard> = { content: draft.value };
  if (props.card.kind === "file" || props.card.kind === "image") {
    patch.meta = { ...(props.card.meta ?? {}), name: metaName.value };
  } else if (props.card.kind === "code") {
    patch.meta = { ...(props.card.meta ?? {}), language: metaLanguage.value };
  } else if (props.card.kind === "url") {
    patch.meta = { ...(props.card.meta ?? {}), href: metaHref.value, title: metaTitle.value };
  }
  emit("patch", props.card.id, patch, "编辑" + kindLabel.value);
  store.setDraft("card:" + props.card.id, "");
  editing.value = false;
}

function cancelEdit() {
  editing.value = false;
}
</script>

<template>
  <article
    class="card"
    :class="{ selected, highlight, dragging, folded: card.folded, hidden: card.hidden, reply: card.kind === 'reply' }"
    :style="style"
    data-im="card"
    :data-card-id="card.id"
    @pointerdown="onPointerDown"
  >
    <header class="card-head">
      <span class="kind mono">{{ kindLabel }}</span>
      <span class="headline">{{ headline }}</span>
      <button
        v-if="card.folded"
        class="btn"
        type="button"
        @click="emit('toggle', card.id, 'folded')"
      >
        展开
      </button>
    </header>

    <p class="card-status" role="status">{{ statusText }}</p>

    <div v-if="!card.folded" class="card-body">
      <template v-if="editing">
        <textarea
          v-model="draft"
          class="editor"
          data-im="card-editor"
          :data-card-id="card.id"
          rows="4"
          :aria-label="'编辑' + kindLabel"
          @input="onDraftInput"
        ></textarea>
        <label v-if="card.kind === 'file' || card.kind === 'image'" class="field">
          <span>名称</span>
          <input v-model="metaName" type="text" />
        </label>
        <label v-if="card.kind === 'code'" class="field">
          <span>语言</span>
          <input v-model="metaLanguage" type="text" />
        </label>
        <template v-if="card.kind === 'url'">
          <label class="field"><span>网址</span><input v-model="metaHref" type="text" /></label>
          <label class="field"><span>标题</span><input v-model="metaTitle" type="text" /></label>
        </template>
        <p class="draft-note">输入过程只保存草稿；点「完成编辑」才形成有效文字状态。</p>
        <div class="row">
          <button class="btn primary" type="button" @click="confirmEdit">完成编辑</button>
          <button class="btn" type="button" @click="cancelEdit">取消</button>
        </div>
      </template>
      <template v-else>
        <p v-if="card.kind === 'text'" class="content">{{ card.content || "（还没有内容，点「编辑」写下来）" }}</p>
        <p v-else-if="card.kind === 'code'" class="content code mono">{{ card.content || "// 待补充代码" }}</p>
        <p v-else-if="card.kind === 'url'" class="content">
          <a :href="href" target="_blank" rel="noreferrer" @pointerdown.stop>{{ linkTitle || "（还没有网址）" }}</a>
        </p>
        <p v-else class="content">
          {{ name || kindLabel }}<span v-if="card.content"> · {{ card.content }}</span>
        </p>
      </template>
    </div>

    <footer v-if="!card.folded" class="card-actions">
      <label v-if="checkable" class="check">
        <input
          type="checkbox"
          :checked="card.checked"
          data-im="check"
          :data-card-id="card.id"
          @change="emit('toggle', card.id, 'checked')"
        />
        <span>{{ card.checked ? "已勾选" : "勾选" }}</span>
      </label>
      <button class="btn" type="button" @click="startEdit">编辑</button>
      <button class="btn" type="button" @click="emit('toggle', card.id, 'hidden')">
        {{ card.hidden ? "取消隐藏" : "隐藏" }}
      </button>
      <button class="btn" type="button" @click="emit('toggle', card.id, 'folded')">
        {{ card.folded ? "展开" : "折叠" }}
      </button>
      <button class="btn" type="button" @click="emit('toggle', card.id, 'bookmarked')">
        {{ card.bookmarked ? "取消书签" : "书签" }}
      </button>
      <button class="btn" type="button" data-im="duplicate-card" :data-card-id="card.id" @click="emit('duplicate', card.id)">
        复制
      </button>
      <button class="btn danger" type="button" data-im="delete-card" :data-card-id="card.id" @click="emit('remove', card.id)">
        删除
      </button>
      <button v-if="groupName" class="btn" type="button" @click="emit('leave-group', card.id)">移出组</button>
      <span v-if="groups.length" class="join">
        <select v-model="joinTarget" aria-label="选择要加入的组">
          <option value="">选择组…</option>
          <option v-for="group in groups" :key="group.id" :value="group.id">{{ group.name }}</option>
        </select>
        <button
          class="btn"
          type="button"
          :disabled="!joinTarget"
          @click="joinTarget && emit('join-group', card.id, joinTarget)"
        >
          加入组
        </button>
      </span>
    </footer>
  </article>
</template>

<style scoped>
.card {
  position: absolute;
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
  padding: var(--sp-2);
  overflow: hidden;
  background: var(--bg-surface);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  box-shadow: var(--shadow-1);
  color: var(--text-primary);
  font-size: var(--fs-sm);
  cursor: grab;
  /* 拖动要跟手：卡片上不加过渡动画 */
}
.card.selected { border-color: var(--accent); background: var(--bg-accent-subtle); }
.card.highlight { outline: 2px dashed var(--warning); outline-offset: 2px; }
.card.dragging { cursor: grabbing; opacity: 0.92; border-style: dashed; border-color: var(--accent); }
.card.hidden { border-style: dashed; }
.card.reply { border-left: 3px solid var(--link); }
.card-head { display: flex; align-items: baseline; gap: var(--sp-2); min-width: 0; }
.kind {
  flex: none;
  font-size: var(--fs-tech, var(--fs-xs));
  color: var(--text-muted);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-pill);
  padding: 0 var(--sp-2);
}
.headline {
  font-family: var(--serif);
  color: var(--text-strong);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.card-status { margin: 0; font-size: var(--fs-xs); color: var(--text-muted); }
.card-body { flex: 1; min-height: 0; overflow: auto; }
.content { margin: 0; white-space: pre-wrap; word-break: break-word; }
.content.code { font-family: var(--mono); font-size: var(--fs-xs); }
.content a { color: var(--link); }
.editor {
  width: 100%;
  box-sizing: border-box;
  font: inherit;
  color: var(--text-primary);
  background: var(--bg-inset);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-xs);
  padding: var(--sp-1);
  resize: vertical;
}
.field { display: flex; align-items: center; gap: var(--sp-2); margin-top: var(--sp-1); font-size: var(--fs-xs); }
.field input {
  flex: 1;
  min-width: 0;
  font: inherit;
  color: var(--text-primary);
  background: var(--bg-inset);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-xs);
  padding: 2px var(--sp-1);
}
.draft-note { margin: var(--sp-1) 0 0; font-size: var(--fs-xs); color: var(--text-faint); }
.card-actions { display: flex; flex-wrap: wrap; gap: var(--sp-1); align-items: center; }
.check { display: inline-flex; align-items: center; gap: var(--sp-1); font-size: var(--fs-xs); }
.join { display: inline-flex; align-items: center; gap: var(--sp-1); }
.join select {
  font: inherit;
  font-size: var(--fs-xs);
  color: var(--text-primary);
  background: var(--bg-inset);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-xs);
  max-width: 96px;
}
.btn {
  font: inherit;
  font-size: var(--fs-xs);
  color: var(--text-secondary);
  background: var(--bg-elevated);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-xs);
  padding: 1px var(--sp-2);
  cursor: pointer;
}
.btn:hover { color: var(--text-strong); border-color: var(--border-strong); }
.btn:focus-visible { outline: 2px solid var(--link); outline-offset: 1px; }
.btn:disabled { opacity: 0.5; cursor: default; }
.btn.primary { color: var(--on-accent); background: var(--accent); border-color: var(--accent); }
.btn.danger { color: var(--danger); }
</style>
