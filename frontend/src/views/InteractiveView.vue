<!--
  互动模式入口与基础布局（Lead 维护）。

  契约：docs/interactive-mode-contract.md §4.4。三条布局约定：
  - 板面占主要空间，提交入口始终容易找到（在板面下方，不随滚动消失）；
  - QIO 回复与任务列表放在可收起的辅助区域，局部批准 / 拒绝入口留在预览附近（由 ReplyPanel 负责）；
  - 状态同时用文字说明，不靠颜色区分。
-->
<script setup lang="ts">
import { computed, onMounted } from "vue";
import { useInteractiveStore } from "../stores/interactive";
import BoardCanvas from "../components/interactive/BoardCanvas.vue";
import SubmitPanel from "../components/interactive/SubmitPanel.vue";
import ReplyPanel from "../components/interactive/ReplyPanel.vue";

const store = useInteractiveStore();

onMounted(() => {
  void store.load();
});

const saveText = computed(() => {
  if (store.saveStatus === "saving") return "正在保存…";
  if (store.saveStatus === "error") return `保存失败：${store.saveError ?? "原因未知"}`;
  if (store.dirty) return "有未保存的改动";
  if (store.saveStatus === "saved") return "已保存（尚未提交）";
  return "尚未保存过";
});

const submitText = computed(() => {
  if (store.submitStatus === "submitting") return "正在提交…";
  if (store.submitStatus === "succeeded") return "提交成功，本次表达已交给 QIO";
  if (store.submitStatus === "failed") return `提交失败：${store.submitError ?? "原因未知"}（改动与勾选已保留）`;
  if (store.submitStatus === "empty") return "没有可提交的有效改动，未提交、未更新基准";
  if (store.submitStatus === "duplicate") return "与上次成功提交内容一致，未重复提交";
  return "";
});

const boardSize = computed(() => {
  const state = store.board;
  if (!state) return "";
  const cards = state.cards.filter((card) => !card.deleted).length;
  const groups = state.groups.filter((group) => !group.deleted).length;
  return `卡片 ${cards} · 组 ${groups} · 关系 ${state.links.filter((l) => !l.deleted).length}`;
});
</script>

<template>
  <div class="interactive">
    <header class="im-header">
      <div class="im-identity">
        <span class="im-mode mono">互动模式</span>
        <h1 class="im-title">{{ store.board?.boardId ? "互动板面" : "互动板面" }}</h1>
        <span class="im-count mono">{{ boardSize }}</span>
      </div>
      <div class="im-header-right">
        <p class="im-save" role="status">{{ saveText }}</p>
        <button
          class="im-toggle"
          type="button"
          :aria-pressed="store.auxOpen"
          @click="store.auxOpen = !store.auxOpen"
        >
          {{ store.auxOpen ? "收起回复与任务" : "展开回复与任务" }}
        </button>
        <router-link class="im-link" to="/">回到对话</router-link>
      </div>
    </header>

    <p v-if="store.loadError" class="im-notice err" role="alert">
      板面读取失败：{{ store.loadError }}
      <button class="im-link" type="button" @click="store.load()">重试</button>
    </p>
    <p v-if="store.recoverNotice.length" class="im-notice warn" role="status">
      上次没有结束的任务已经暂停（{{ store.recoverNotice.length }} 项）：重新打开不会自动继续，需要你确认后才会开始。
    </p>

    <div class="im-body">
      <main class="im-board">
        <BoardCanvas />
        <SubmitPanel />
      </main>
      <aside v-show="store.auxOpen" class="im-aux" aria-label="QIO 回复与任务">
        <ReplyPanel />
      </aside>
    </div>

    <p class="im-foot mono" role="status">
      {{ submitText || "编辑板面不会调用 QIO；点「提交」才会把本次允许查看的表达交给 QIO。" }}
    </p>
  </div>
</template>

<style scoped>
.interactive {
  display: flex;
  flex-direction: column;
  height: 100%;
  background: var(--bg-base);
  color: var(--text-primary);
  font-family: var(--sans);
}
.im-header {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--sp-4);
  padding: var(--sp-3) var(--sp-5);
  border-bottom: 1px solid var(--border-subtle);
  background: var(--bg-surface);
}
.im-identity {
  display: flex;
  align-items: baseline;
  gap: var(--sp-3);
  min-width: 0;
}
.im-mode {
  font-size: var(--fs-xs);
  color: var(--link);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-pill);
  padding: 1px var(--sp-2);
}
.im-title {
  margin: 0;
  font-family: var(--serif);
  font-size: var(--fs-lg);
  font-weight: 600;
  color: var(--text-strong);
}
.im-count {
  font-size: var(--fs-xs);
  color: var(--text-faint);
}
.im-header-right {
  display: flex;
  align-items: center;
  gap: var(--sp-4);
}
.im-save {
  margin: 0;
  font-size: var(--fs-sm);
  color: var(--text-secondary);
}
.im-toggle,
.im-link {
  font: inherit;
  font-size: var(--fs-sm);
  color: var(--link);
  background: none;
  border: none;
  padding: 0;
  cursor: pointer;
  text-decoration: none;
}
.im-toggle:hover,
.im-link:hover { color: var(--accent-hover); }
.im-toggle:focus-visible,
.im-link:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.im-notice {
  margin: 0;
  padding: var(--sp-2) var(--sp-5);
  font-size: var(--fs-sm);
  border-bottom: 1px solid var(--border-subtle);
}
.im-notice.err { color: var(--danger); }
.im-notice.warn { color: var(--warning); }
.im-body {
  flex: 1;
  display: flex;
  min-height: 0;
}
.im-board {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
}
.im-aux {
  width: 340px;
  border-left: 1px solid var(--border-subtle);
  background: var(--bg-surface);
  overflow: auto;
  flex: none;
}
.im-foot {
  margin: 0;
  padding: var(--sp-2) var(--sp-5);
  border-top: 1px solid var(--border-subtle);
  font-size: var(--fs-xs);
  color: var(--text-faint);
}
/* 常见的小窗口：辅助区域改为覆盖式，板面与提交入口保持可用 */
@media (max-width: 900px) {
  .im-aux {
    position: absolute;
    right: 0;
    top: 0;
    bottom: 0;
    width: min(340px, 92vw);
    box-shadow: 0 18px 44px rgba(0, 0, 0, 0.38);
    z-index: 3;
  }
  .im-body { position: relative; }
}
</style>
