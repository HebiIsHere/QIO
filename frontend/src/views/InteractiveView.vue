<!--
  互动模式入口与页面组合（v2 页面壳，Lead 集成）。

  结构：
  - 全局栏由 WindowChrome 提供（品牌 / 搜索与保存 / 设置 / 窗口按钮）；
  - 中央板面、底部工具栏与输入区、书签栏由 InteractiveWorkspace 分层；
  - 旧 im-top 板面头不再画第二条标题栏：板面名与真实状态收进板面内的轻量 HUD；
  - 统一审批层只挂这一处，旧 ApprovalEntry / ApprovalModal 在聊天路由让位（App.vue 门控）。
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
import IntentWorkbench from "../components/interactive/IntentWorkbench.vue";
import UnifiedApprovalLayer from "../components/conversation/UnifiedApprovalLayer.vue";
import UnifiedApprovalEntry from "../components/approval/UnifiedApprovalEntry.vue";
import { InteractiveWorkspace, InteractiveChromeTools } from "../components/interactive/v2";
import { understandingSettled, understandingText } from "../interactive/approval";

const store = useInteractiveStore();
/** 意图与结果工作台：保留旧能力入口，Phase 3 再迁到虚拟意图/底部操作。 */
const workbenchOpen = ref(false);

onMounted(() => {
  void store.load();
});

const uText = computed(() => {
  if (!store.understandingStatus && store.submitStatus === "succeeded") {
    return {
      label: "已提交，等待理解状态",
      detail: "这次提交已经记录；回执没有给出理解请求身份，理解状态以服务端记录为准。",
      tone: "waiting" as const,
    };
  }
  return understandingText(store.understanding, store.understandingStatus);
});
const uLive = computed(
  () => Boolean(store.understandingStatus) && !understandingSettled(store.understandingStatus),
);
const retrying = ref(false);
async function retryUnderstanding(): Promise<void> {
  if (retrying.value) return;
  retrying.value = true;
  try {
    await store.retryUnderstanding();
  } finally {
    retrying.value = false;
  }
}
const refreshing = ref(false);
async function refreshUnderstanding(): Promise<void> {
  if (refreshing.value) return;
  refreshing.value = true;
  try {
    await store.refreshUnderstanding();
  } finally {
    refreshing.value = false;
  }
}

const boardSize = computed(() => {
  const state = store.board;
  if (!state) return "";
  const cards = state.cards.filter((card) => !card.deleted).length;
  if (!cards) return "";
  const groups = state.groups.filter((group) => !group.deleted).length;
  const links = state.links.filter((link) => !link.deleted).length;
  const parts = [cards + " 张卡片"];
  if (groups) parts.push(groups + " 个组");
  if (links) parts.push(links + " 条关系");
  return parts.join(" · ");
});
</script>

<template>
  <div class="interactive">
    <!-- 互动页的搜索 + 唯一保存状态：Teleport 到 WindowChrome 的挂载点。 -->
    <InteractiveChromeTools />
    <!-- 统一审批：互动页恰好一套；记忆提醒没有星球落点，先不渲染。 -->
    <UnifiedApprovalLayer :show-memory="false" />
    <!-- 过渡入口：Phase 3 迁移到输入区事项行后删除。 -->
    <UnifiedApprovalEntry />

    <InteractiveWorkspace>
      <template #board>
        <div class="im-board-stage" data-im="interactive-stage">
          <BoardCanvas />

          <!-- 板面 HUD：板面名 / 数量 / 真实理解状态 / 旧能力入口；不再占一条标题栏。 -->
          <div class="im-board-hud">
            <div class="im-hud-left">
              <h1 class="im-title">互动板面</h1>
              <span v-if="boardSize" class="im-count" data-im="board-counts">{{ boardSize }}</span>
            </div>
            <div class="im-hud-right">
              <span
                v-if="store.submissionStage === 'idle'"
                class="im-note"
                data-im="not-connected"
                title="保存只落库、不调用模型；提交之后 QIO 才会读取本次允许查看的内容并开始理解"
              >
                还没有提交给 QIO
              </span>
              <template v-else>
                <span
                  class="im-note"
                  :class="'tone-' + uText.tone"
                  data-im="understanding-status"
                  :data-understanding-status="store.understandingStatus ?? 'none'"
                  :title="uText.detail"
                >{{ uText.label }}</span>
                <button
                  v-if="store.understandingStatus === 'failed'"
                  class="im-chip"
                  type="button"
                  data-im="understanding-retry"
                  :disabled="retrying"
                  @click="retryUnderstanding"
                >{{ retrying ? "重试中…" : "重试理解" }}</button>
                <button
                  v-else-if="uLive"
                  class="im-chip"
                  type="button"
                  data-im="understanding-refresh"
                  :disabled="refreshing"
                  @click="refreshUnderstanding"
                >{{ refreshing ? "查询中…" : "刷新理解状态" }}</button>
              </template>
              <button
                v-if="store.previewExpandedCount"
                class="im-chip"
                type="button"
                data-im="collapse-previews"
                @click="store.collapseAllPreviews()"
              >收起预览（{{ store.previewExpandedCount }}）</button>
              <button
                class="im-chip"
                type="button"
                data-im="tasks-entry"
                :aria-expanded="store.tasksOpen"
                @click="store.tasksOpen = !store.tasksOpen"
              >任务{{ store.taskCount ? " " + store.taskCount : "" }}</button>
              <button
                class="im-chip"
                type="button"
                data-im="workbench-entry"
                :aria-expanded="workbenchOpen"
                @click="workbenchOpen = !workbenchOpen"
              >意图与结果</button>
            </div>
          </div>

          <!-- 旧浮层仍是板面舞台的绝对定位子层；Phase 2/3 再迁入 workspace 的 toolbar/chat 槽。 -->
          <BoardToolbar />
          <IntentStatusPopover />
          <IntentBatchTray />
          <ChatDock />
          <ImpactConfirmDialog />
          <IntentWorkbench v-if="workbenchOpen" @close="workbenchOpen = false" />

          <p v-if="store.loadError" class="im-notice err" role="alert" data-im="load-error">
            板面读取失败：{{ store.loadError }}
            <button class="im-link" type="button" @click="store.load()">重试</button>
          </p>
          <p v-if="store.recoverNotice.length" class="im-notice warn" role="status" data-im="recover-notice">
            上次没有结束的任务已经暂停（{{ store.recoverNotice.length }} 项）：重新打开不会自动继续，需要你确认后才会开始。
          </p>
        </div>
      </template>
    </InteractiveWorkspace>
  </div>
</template>

<style scoped>
.interactive {
  position: relative;
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  background: var(--bg-base);
  color: var(--text-primary);
  font-family: var(--sans);
}
/* 板面槽内舞台：现有 BoardCanvas / ChatDock / BoardToolbar 的定位祖先。 */
.im-board-stage {
  position: relative;
  flex: 1;
  display: flex;
  flex-direction: column;
  min-height: 0;
  min-width: 0;
  height: 100%;
}
/* HUD 是轻量浮层，不占布局、不形成第二条标题栏。 */
.im-board-hud {
  position: absolute;
  top: 10px;
  left: 12px;
  right: 12px;
  z-index: 5;
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--sp-3);
  pointer-events: none;
}
.im-hud-left,
.im-hud-right {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  min-width: 0;
  pointer-events: auto;
}
.im-hud-left {
  padding: 5px 10px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-pill);
  background: var(--bg-elevated);
  box-shadow: var(--shadow-1);
}
.im-title {
  margin: 0;
  font-family: var(--serif);
  font-size: var(--fs-sm);
  font-weight: 600;
  color: var(--text-strong);
  white-space: nowrap;
}
.im-count {
  font-size: var(--fs-xs);
  color: var(--text-secondary);
  font-family: var(--mono);
  white-space: nowrap;
}
.im-note {
  max-width: min(340px, 38vw);
  overflow: hidden;
  text-overflow: ellipsis;
  font-size: var(--fs-xs);
  color: var(--text-secondary);
  border-bottom: 1px dashed var(--border-subtle);
  white-space: nowrap;
  cursor: help;
}
.im-note.tone-open { color: var(--link); }
.im-note.tone-waiting { color: var(--warning); }
.im-note.tone-active { color: var(--accent); }
.im-note.tone-settled { color: var(--text-secondary); }
.im-note.tone-problem { color: var(--danger); }
.im-chip {
  font: inherit;
  font-size: var(--fs-xs);
  color: var(--text-secondary);
  background: var(--bg-elevated);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-pill);
  padding: 4px 9px;
  cursor: pointer;
  white-space: nowrap;
}
.im-chip:hover { color: var(--text-strong); border-color: var(--accent); }
.im-chip:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
.im-link {
  font: inherit;
  font-size: var(--fs-xs);
  color: var(--link);
  background: none;
  border: none;
  padding: 0;
  cursor: pointer;
}
.im-notice {
  position: absolute;
  left: 50%;
  transform: translateX(-50%);
  top: 58px;
  margin: 0;
  padding: var(--sp-2) var(--sp-4);
  border-radius: var(--r-sm);
  font-size: var(--fs-sm);
  background: var(--bg-elevated);
  border: 1px solid var(--border-subtle);
  box-shadow: var(--shadow-1);
  z-index: 6;
}
.im-notice.err { color: var(--danger); }
.im-notice.warn { color: var(--warning); }
@media (max-width: 900px) {
  .im-count { display: none; }
  .im-board-hud { top: 8px; left: 8px; right: 8px; }
  .im-hud-right { gap: 4px; }
  .im-note { max-width: 30vw; }
  .im-chip { padding: 4px 7px; }
}
</style>
