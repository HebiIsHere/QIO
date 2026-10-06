<!-- 板面工具栏（子智能体 A 负责）：添加五类材料、撤销重做、选择模式、分组与顺序操作。

  契约：docs/interactive-mode-contract.md §4.4（常用操作集中显示、窄窗口仍可用）。
  工具栏只发事件：真正的状态计算与提交在 BoardCanvas（board.ts 纯函数 + store.commit）。
-->
<script setup lang="ts">
import { computed, ref } from "vue";
import { useInteractiveStore } from "../../stores/interactive";
import type { BoardGroup, CardKind } from "../../interactive/types";

const props = defineProps<{
  mode: "select" | "rect" | "link";
  selectedIds: string[];
  groups: BoardGroup[];
  selectedGroupIds: string[];
}>();

const emit = defineEmits<{
  (e: "add", kind: CardKind): void;
  (e: "mode", mode: "select" | "rect" | "link"): void;
  (e: "undo"): void;
  (e: "redo"): void;
  (e: "group-op", op: "form" | "join" | "leave" | "dissolve" | "ordered" | "unordered" | "merge", groupId?: string): void;
  (e: "delete-selected"): void;
}>();

const store = useInteractiveStore();
const joinTarget = ref("");

const hasSelection = computed(() => props.selectedIds.length > 0);
const canMerge = computed(() => props.selectedGroupIds.length > 1);
//: 勾选框只出现在文字注释上：材料默认就在 QIO 可查看范围内（契约 §1.4）
const materialHint = "文字注释默认未勾选（勾选后才允许 QIO 查看，且仍要提交）；材料默认在本次允许查看范围内，不需要勾选。";
</script>

<template>
  <div class="toolbar" role="toolbar" aria-label="板面操作">
    <div class="cluster">
      <span class="cluster-label">添加</span>
      <button class="btn" type="button" data-im="add-text" @click="emit('add', 'text')">文字注释</button>
      <button class="btn" type="button" data-im="add-file" @click="emit('add', 'file')">文件</button>
      <button class="btn" type="button" data-im="add-image" @click="emit('add', 'image')">图片</button>
      <button class="btn" type="button" data-im="add-code" @click="emit('add', 'code')">代码</button>
      <button class="btn" type="button" data-im="add-url" @click="emit('add', 'url')">网址</button>
    </div>

    <div class="cluster">
      <span class="cluster-label">历史</span>
      <button class="btn" type="button" data-im="undo" :disabled="!store.canUndo" title="撤销上一步板面操作" @click="emit('undo')">
        撤销
      </button>
      <button class="btn" type="button" data-im="redo" :disabled="!store.canRedo" title="重做被撤销的操作" @click="emit('redo')">
        重做
      </button>
    </div>

    <div class="cluster">
      <span class="cluster-label">选择</span>
      <button class="btn" type="button" :class="{ on: mode === 'select' }" :aria-pressed="mode === 'select'" @click="emit('mode', 'select')">
        单选 / 多选
      </button>
      <button class="btn" type="button" :class="{ on: mode === 'rect' }" :aria-pressed="mode === 'rect'" @click="emit('mode', 'rect')">
        区域选择
      </button>
      <button class="btn" type="button" :class="{ on: mode === 'link' }" :aria-pressed="mode === 'link'" @click="emit('mode', 'link')">
        关系模式
      </button>
      <span class="count mono">已选 {{ selectedIds.length }}</span>
    </div>

    <div class="cluster">
      <span class="cluster-label">整理</span>
      <button class="btn" type="button" :disabled="!hasSelection" @click="emit('group-op', 'form')">所选成组</button>
      <select v-model="joinTarget" :disabled="!hasSelection || !groups.length" aria-label="选择要加入的组">
        <option value="">加入组…</option>
        <option v-for="group in groups" :key="group.id" :value="group.id">{{ group.name }}</option>
      </select>
      <button
        class="btn"
        type="button"
        :disabled="!hasSelection || !joinTarget"
        @click="joinTarget && emit('group-op', 'join', joinTarget)"
      >
        加入
      </button>
      <button class="btn" type="button" :disabled="!hasSelection" @click="emit('group-op', 'leave')">移出组</button>
      <button class="btn" type="button" :disabled="!hasSelection" @click="emit('group-op', 'dissolve')">解除组</button>
      <button class="btn" type="button" :disabled="!hasSelection" @click="emit('group-op', 'ordered')">设为有序</button>
      <button class="btn" type="button" :disabled="!hasSelection" @click="emit('group-op', 'unordered')">取消有序</button>
      <button class="btn" type="button" :disabled="!canMerge" title="把所选卡片所在的多个组合并成一个" @click="emit('group-op', 'merge')">
        合并组
      </button>
    </div>

    <div class="cluster">
      <button class="btn danger" type="button" :disabled="!hasSelection" @click="emit('delete-selected')">删除所选</button>
    </div>

    <p class="hint">{{ materialHint }} 普通组的摆放顺序不代表先后。</p>
  </div>
</template>

<style scoped>
.toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--sp-2) var(--sp-4);
  padding: var(--sp-2) var(--sp-4);
  background: var(--bg-surface);
  border-bottom: 1px solid var(--border-subtle);
}
.cluster { display: flex; flex-wrap: wrap; align-items: center; gap: var(--sp-1); }
.cluster-label { font-size: var(--fs-xs); color: var(--text-faint); margin-right: var(--sp-1); }
.count { font-size: var(--fs-xs); color: var(--text-muted); }
.hint { flex-basis: 100%; margin: 0; font-size: var(--fs-xs); color: var(--text-faint); }
.btn {
  font: inherit;
  font-size: var(--fs-xs);
  color: var(--text-secondary);
  background: var(--bg-elevated);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-xs);
  padding: 2px var(--sp-2);
  cursor: pointer;
}
.btn:hover { color: var(--text-strong); border-color: var(--border-strong); }
.btn:focus-visible { outline: 2px solid var(--link); outline-offset: 1px; }
.btn:disabled { opacity: 0.5; cursor: default; }
.btn.on { color: var(--on-accent); background: var(--accent); border-color: var(--accent); }
.btn.danger { color: var(--danger); }
select {
  font: inherit;
  font-size: var(--fs-xs);
  color: var(--text-primary);
  background: var(--bg-inset);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-xs);
  max-width: 110px;
}
</style>
