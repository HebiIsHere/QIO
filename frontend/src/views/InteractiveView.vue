<!--
  互动模式入口与页面组合（主智能体维护）。

  契约：docs/interactive-mode-contract.md §8 / §9.6（信息层级）。
  这一版的结构：
  - 板面为主体，所有操作浮在板面上，**没有常驻右侧栏**；
  - 顶部只留板面名、**唯一的准确保存状态**与必要入口；数量等次要信息退到次级层，不与主状态同样显眼；
  - 「已保存」在顶部说一次；「已提交」由工具栏右端的提交区说一次（两处各只一处主要状态）；
  - 未接入状态如实可见（QIO 还没有读取板面），不用开发用语；
  - 底部是横向悬浮工具栏（A 负责），右端是提交区（C 负责）；
  - 右下角独立聊天入口（C 负责），右上角批量列表入口（D 负责），影响确认是页面中央对话框（D 负责）。
-->
<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { useInteractiveStore } from "../stores/interactive";
import BoardCanvas from "../components/interactive/BoardCanvas.vue";
import BoardToolbar from "../components/interactive/BoardToolbar.vue";
import ChatDock from "../components/interactive/ChatDock.vue";
import IntentBatchTray from "../components/interactive/IntentBatchTray.vue";
import IntentStatusPopover from "../components/interactive/IntentStatusPopover.vue";
import ImpactConfirmDialog from "../components/interactive/ImpactConfirmDialog.vue";
import DemoIntentEntry from "../components/interactive/DemoIntentEntry.vue";

const store = useInteractiveStore();
const demoOpen = ref(false);

onMounted(() => {
  void store.load();
});

/**
 * 顶部是**唯一**的保存状态：说准「已保存 ≠ QIO 已收到」。
 * 提交是否成功由提交区单独说明，两处不重复解释同一件事。
 */
const saveText = computed(() => {
  if (store.saveStatus === "saving") return "正在保存…";
  if (store.saveStatus === "error") return `保存失败：${store.saveError ?? "原因未知"}`;
  if (store.dirty) return "有改动尚未保存";
  // 顶部只说「保存」这一件事；「提交」由工具栏右端的提交区单独表达（§9.6：各自只设一处主状态）
  if (store.saveStatus === "saved") return "已保存";
  return "尚未保存过";
});

const saveTone = computed(() => {
  if (store.saveStatus === "error") return "err";
  if (store.saveStatus === "saving" || store.dirty) return "busy";
  return "ok";
});

/** 次要信息：数量退到次级层（空板面时不显示，避免一排同样显眼的标签） */
const boardSize = computed(() => {
  const state = store.board;
  if (!state) return "";
  const cards = state.cards.filter((card) => !card.deleted).length;
  if (!cards) return "";
  const groups = state.groups.filter((group) => !group.deleted).length;
  const links = state.links.filter((link) => !link.deleted).length;
  const parts = [`${cards} 张卡片`];
  if (groups) parts.push(`${groups} 个组`);
  if (links) parts.push(`${links} 条关系`);
  return parts.join(" · ");
});
</script>

<template>
  <div class="interactive">
    <header class="im-top">
      <div class="im-identity">
        <h1 class="im-title">互动板面</h1>
        <span v-if="boardSize" class="im-count" data-im="board-counts">{{ boardSize }}</span>
      </div>

      <div class="im-top-right">
        <!-- 唯一的保存状态（顶部） -->
        <p class="im-save" :class="saveTone" data-im="save-status" role="status">
          <span class="im-dot" aria-hidden="true"></span>{{ saveText }}
        </p>
        <!-- 未接入状态如实可见，且不用开发用语 -->
        <span class="im-note" data-im="not-connected" title="提交只会记录在本地，QIO 还没有真正读取或理解板面内容">
          QIO 还没有读取板面
        </span>
        <button
          class="im-chip"
          type="button"
          data-im="tasks-entry"
          :aria-expanded="store.tasksOpen"
          @click="store.tasksOpen = !store.tasksOpen"
        >
          任务{{ store.taskCount ? " " + store.taskCount : "" }}
        </button>
        <button
          class="im-chip"
          type="button"
          data-im="demo-entry"
          :aria-expanded="demoOpen"
          @click="demoOpen = !demoOpen"
        >
          演示
        </button>
        <router-link class="im-link" to="/">回到对话</router-link>
      </div>
    </header>

    <!-- 演示入口单独放在明确标注的浮层里，日常界面不混入演示成功提示 -->
    <div v-if="demoOpen" class="im-demo-pop" data-im="demo-popover">
      <DemoIntentEntry />
    </div>

    <main class="im-stage">
      <BoardCanvas />

      <!-- 板面上的浮层：底部工具栏 / 右下聊天 / 右上批量列表 / 任务浮层 / 中央影响确认 -->
      <BoardToolbar />
      <IntentStatusPopover />
      <IntentBatchTray />
      <ChatDock />
      <ImpactConfirmDialog />

      <p v-if="store.loadError" class="im-notice err" role="alert" data-im="load-error">
        板面读取失败：{{ store.loadError }}
        <button class="im-link" type="button" @click="store.load()">重试</button>
      </p>
      <p v-if="store.recoverNotice.length" class="im-notice warn" role="status" data-im="recover-notice">
        上次没有结束的任务已经暂停（{{ store.recoverNotice.length }} 项）：重新打开不会自动继续，需要你确认后才会开始。
      </p>
    </main>
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
/* 顶部：板面名 + 唯一保存状态 + 必要入口；不放编辑按钮，也不长期显示大段说明 */
.im-top {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--sp-4);
  padding: var(--sp-2) var(--sp-5);
  border-bottom: 1px solid var(--border-subtle);
  background: var(--bg-surface);
  flex: none;
}
.im-identity {
  display: flex;
  align-items: baseline;
  gap: var(--sp-3);
  min-width: 0;
}
.im-title {
  margin: 0;
  font-family: var(--serif);
  font-size: var(--fs-lg);
  font-weight: 600;
  color: var(--text-strong);
  white-space: nowrap;
}
/* 次要信息：比主状态更小，但不低于可读对比度（独立复核量到 --text-faint 只有 3.1/3.28:1） */
.im-count {
  font-size: var(--fs-xs);
  color: var(--text-secondary);
  font-family: var(--mono);
  white-space: nowrap;
}
.im-top-right {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
  flex: none;
}
.im-save {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-2);
  margin: 0;
  font-size: var(--fs-sm);
  color: var(--text-secondary);
  white-space: nowrap;
}
/* 状态不只靠颜色：文字已经说明，这里只做辅助色点 */
.im-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--text-faint);
}
.im-save.ok .im-dot { background: var(--success); }
.im-save.busy .im-dot { background: var(--warning); }
.im-save.err { color: var(--danger); }
.im-save.err .im-dot { background: var(--danger); }
/* 未接入状态：安静但真实可见 */
.im-note {
  font-size: var(--fs-xs);
  color: var(--text-faint);
  border-bottom: 1px dashed var(--border-subtle);
  cursor: help;
  white-space: nowrap;
}
.im-chip {
  font: inherit;
  font-size: var(--fs-sm);
  color: var(--text-secondary);
  background: none;
  border: 1px solid transparent;
  border-radius: var(--r-pill);
  padding: 2px var(--sp-3);
  cursor: pointer;
}
.im-chip:hover { color: var(--text-strong); border-color: var(--border-subtle); }
.im-chip:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
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
.im-link:hover { color: var(--accent-hover); }
.im-link:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
/* 演示浮层挂在顶部下方，独立于板面操作 */
.im-demo-pop {
  position: absolute;
  top: 52px;
  right: var(--sp-5);
  z-index: 30;
  max-width: min(420px, 92vw);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-md);
  background: var(--bg-elevated);
  box-shadow: var(--shadow-2);
}
/* 板面舞台：所有浮层都相对它定位 */
.im-stage {
  position: relative;
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}
.im-notice {
  position: absolute;
  left: 50%;
  transform: translateX(-50%);
  top: var(--sp-3);
  margin: 0;
  padding: var(--sp-2) var(--sp-4);
  border-radius: var(--r-sm);
  font-size: var(--fs-sm);
  background: var(--bg-elevated);
  border: 1px solid var(--border-subtle);
  z-index: 40;
}
.im-notice.err { color: var(--danger); }
.im-notice.warn { color: var(--warning); }

@media (max-width: 900px) {
  .im-count, .im-note { display: none; }
  .im-top { padding: var(--sp-2) var(--sp-3); gap: var(--sp-2); }
  .im-top-right { gap: var(--sp-2); }
}
</style>
