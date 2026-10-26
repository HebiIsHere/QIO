<!--
  互动模式入口与页面组合（v2 页面壳，Lead 集成）。

  结构：
  - 全局栏由 WindowChrome 提供（品牌 / 搜索与保存 / 设置 / 窗口按钮）；
  - 中央板面、底部工具栏与输入区、书签栏由 InteractiveWorkspace 分层；
  - 旧 im-top 板面头不再画第二条标题栏：板面名与真实状态收进板面内的轻量 HUD；
  - 统一审批层只挂这一处，旧 ApprovalEntry / ApprovalModal 在聊天路由让位（App.vue 门控）。
-->
<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useInteractiveStore } from "../stores/interactive";
import BoardCanvas from "../components/interactive/BoardCanvas.vue";
import BoardToolbar from "../components/interactive/BoardToolbar.vue";
import ConversationModeSwitch from "../components/conversation/ConversationModeSwitch.vue";

import UnifiedApprovalLayer from "../components/conversation/UnifiedApprovalLayer.vue";
import {
  InteractiveWorkspace,
  InteractiveChromeTools,
  InteractiveConversation,
  RecoveryPanel,
  SaveFailureBanner,
  ImpactConfirmLayer,
  TaskCenterPanel,
  RECOVERY_ENTRY_TEXT,
  recoveryEntryVisible,
  IntentDisplayAuthority,
  type IntentDisplayPoint,
} from "../components/interactive/v2";
import IntentLayer from "../components/interactive/v2/IntentLayer.vue";
import InteractiveBookmarks from "../components/interactive/v2/InteractiveBookmarks.vue";
import ExecutionRecordPanel from "../components/interactive/ExecutionRecordPanel.vue";
import type { IntentView } from "../interactive/intentWorkbench";
import { saveIntentDisplay } from "../services/interactive";
import { understandingSettled, understandingText } from "../interactive/approval";

const store = useInteractiveStore();
/** 执行记录：唯一的新入口，查看确认材料、范围、操作、结果、失败与重试事实。 */
const recordIntent = ref<IntentView | null>(null);
/** 书签栏纯查看状态；由工具栏切换，不写板面、不产生意图。 */
const bookmarksOpen = ref(false);
/** 底聊展开时的最大高度：保证板面始终留出约 220px，而不是把板面压到 128px。 */
const conversationMaxHeight = ref(
  typeof window === "undefined"
    ? 420
    : Math.max(280, Math.min(720, window.innerHeight - 306)),
);
function syncConversationMaxHeight(): void {
  if (typeof window === "undefined") return;
  // 对话浮在板面上：最多长到仍露出约 220px 板面（70 全局栏 + 220 板面 + 16 边距）。
  conversationMaxHeight.value = Math.max(280, Math.min(720, window.innerHeight - 306));
}

/* ------------------------------------------------------------------
 * F02：意图视图只读 store（唯一维护位置）
 *
 * 页面不再自己持有第二份 intentViews：加载 / 失败 / 乱序 / 刷新全部由 store 统一负责，
 * 页面只做呈现与「重试读取」入口。
 * ------------------------------------------------------------------ */
const intentViews = computed(() => store.intentViews);
const intentReadFailed = computed(() => store.intentViewsLoadState === "error");
const intentReadErrorText = computed(() => {
  const reason = store.intentViewsLoadError || "原因未知";
  return store.intentViewsLoadedOnce
    ? "更新失败，当前显示上次读取的内容（" + reason + "）"
    : "意图读取失败（还没成功读到过，" + reason + "）：当前不能判定为空，请重试。";
});
const intentReadBusy = ref(false);
async function retryIntentRead(): Promise<void> {
  if (intentReadBusy.value) return;
  intentReadBusy.value = true;
  try {
    await store.refreshIntentFacts();
  } finally {
    intentReadBusy.value = false;
  }
}

/** 意图层确认 / 恢复 / 拒绝之后的统一刷新入口（意图状态 + 执行记录一起更新）。 */
const intentRefreshBusy = ref(false);
async function refreshAfterIntentAction(): Promise<void> {
  if (intentRefreshBusy.value) return;
  intentRefreshBusy.value = true;
  try {
    await store.refreshAfterIntentAction();
  } finally {
    intentRefreshBusy.value = false;
  }
}

/* ------------------------------------------------------------------
 * F10：意图显示位置的权威状态（服务器位置 / 等待保存 / 保存失败分开）
 *
 * 页面不再整块回滚 intentDisplay，也不再用「本机缓存优先」覆盖服务器事实：
 * 每次保存按对象 + 操作版本归属，失败只回退这一张卡到这次操作前的位置。
 * ------------------------------------------------------------------ */
function displayStorageKey(): string {
  return "qio-interactive-intent-display:" + (store.board?.boardId ?? "board_default");
}
function readDisplayCache(): Record<string, IntentDisplayPoint> {
  try {
    const raw = localStorage.getItem(displayStorageKey());
    if (!raw) return {};
    const parsed = JSON.parse(raw) as Record<string, unknown>;
    const next: Record<string, IntentDisplayPoint> = {};
    for (const [id, value] of Object.entries(parsed)) {
      if (!value || typeof value !== "object") continue;
      const point = value as { x?: unknown; y?: unknown };
      if (
        typeof point.x === "number" && Number.isFinite(point.x) &&
        typeof point.y === "number" && Number.isFinite(point.y)
      ) {
        next[id] = { x: point.x, y: point.y };
      }
    }
    return next;
  } catch {
    // 本机显示位置不可用不影响意图真实状态
    return {};
  }
}
function writeDisplayCache(cache: Record<string, IntentDisplayPoint>): void {
  try {
    localStorage.setItem(displayStorageKey(), JSON.stringify(cache));
  } catch {
    // 同上
  }
}

const displayAuthority = new IntentDisplayAuthority(readDisplayCache());
const displayOverrides = ref<Record<string, IntentDisplayPoint>>({});
const displayStates = ref<Record<string, { phase: string; error?: string | null }>>({});
const displayFailures = ref<{ intentId: string; error: string }[]>([]);

function publishDisplay(): void {
  const snapshot = displayAuthority.snapshot();
  displayOverrides.value = snapshot.overrides;
  const states: Record<string, { phase: string; error?: string | null }> = {};
  for (const [intentId, state] of Object.entries(snapshot.states)) {
    states[intentId] = { phase: state.phase, error: state.error };
  }
  displayStates.value = states;
  displayFailures.value = snapshot.failures;
  writeDisplayCache(displayAuthority.cacheSnapshot());
}

const intentDisplayError = computed(() => {
  if (!displayFailures.value.length) return "";
  const detail = displayFailures.value
    .map((item) => item.intentId + "：" + item.error)
    .join("；");
  return (
    "意图位置保存失败：" + detail +
    "（已回到这次操作前的位置，可以重新拖动；本机缓存不会覆盖服务器上更新的位置）"
  );
});

async function onIntentDisplayChange(payload: { intentId: string; x: number; y: number }): Promise<void> {
  const intentId = String(payload?.intentId ?? "");
  if (!intentId) return;
  const op = displayAuthority.begin(intentId, { x: payload.x, y: payload.y });
  publishDisplay();
  const boardId = store.board?.boardId;
  if (!boardId) {
    displayAuthority.fail(intentId, op, "板面身份还没准备好，位置只保存在本机");
    publishDisplay();
    return;
  }
  try {
    const result = await saveIntentDisplay(intentId, boardId, payload.x, payload.y);
    const accepted = (result as { display?: IntentDisplayPoint } | null)?.display ?? {
      x: payload.x,
      y: payload.y,
    };
    displayAuthority.settle(intentId, op, accepted);
  } catch (err) {
    displayAuthority.fail(intentId, op, (err as Error)?.message ?? "原因未知");
  }
  publishDisplay();
}

/**
 * 服务器读取到的意图视图是位置事实的来源：
 * 每次成功读取都用它更新权威位置（给了 display 就以服务器为准，已结束操作的临时位置随之下线）。
 */
watch(
  () => store.intentViews,
  (views) => {
    displayAuthority.applyServerViews(views);
    publishDisplay();
  },
);

/* ------------------------------------------------------------------
 * F05：恢复事项摘要来自 store（不再依赖 RecoveryPanel 是否挂载）
 * ------------------------------------------------------------------ */
const recoveryEntries = computed(() => {
  const entry = store.recoveryEntry;
  return [
    {
      ...RECOVERY_ENTRY_TEXT,
      id: "recovery",
      count: entry.count,
      tone: "warn" as const,
      visible: recoveryEntryVisible(entry.count) || entry.readFailed || entry.pending,
    },
  ];
});
const recoveryReadError = computed(() => (store.recoveryEntry.readFailed ? store.recoveryEntry.readError : ""));
const recoveryReadBusy = ref(false);
async function retryRecoveryRead(): Promise<void> {
  if (recoveryReadBusy.value) return;
  recoveryReadBusy.value = true;
  try {
    await store.refreshIntentFacts();
  } finally {
    recoveryReadBusy.value = false;
  }
}

/* ------------------------------------------------------------------
 * F02.6：切页返回 / 窗口回前台 / 断线恢复时重新核实（只读，不重复创建/执行）
 * ------------------------------------------------------------------ */
function onPageVisibility(): void {
  if (typeof document === "undefined") return;
  if (document.visibilityState !== "visible") return;
  void store.reverifyAfterResume();
}
function onOnline(): void {
  void store.reverifyAfterResume();
}

function openRecordById(intentId: string): void {
  const view = store.intentViews.find((item) => item.intent_id === intentId);
  if (view) recordIntent.value = view as IntentView;
}

onMounted(() => {
  syncConversationMaxHeight();
  window.addEventListener("resize", syncConversationMaxHeight);
  document.addEventListener("visibilitychange", onPageVisibility);
  window.addEventListener("online", onOnline);
  publishDisplay();
  void store.load().then(() => {
    displayAuthority.applyServerViews(store.intentViews);
    publishDisplay();
  });
});
onBeforeUnmount(() => {
  window.removeEventListener("resize", syncConversationMaxHeight);
  document.removeEventListener("visibilitychange", onPageVisibility);
  window.removeEventListener("online", onOnline);
  // F02.7：离开互动页时停掉轮询与监听（重新进入时 load() 会重新核实）
  store.disposePageRuntime();
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
    // F02.5：手动刷新理解也接回同一套状态更新（意图事实 + 执行记录，只读、不重复创建）
    await store.refreshAfterIntentAction();
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
    <!-- 统一审批：互动页恰好一套；入口由输入区事项行第⑤项提供，不再另挂浮动入口。 -->
    <UnifiedApprovalLayer :show-memory="false" />
    <!-- F01：保存前影响确认的唯一承接界面（与统一审批共用「同时只有一个阻塞面」规则） -->
    <ImpactConfirmLayer />

    <InteractiveWorkspace :bookmarks-open="bookmarksOpen">
      <template #board>
        <div class="im-board-stage" data-im="interactive-stage">
          <BoardCanvas>
            <template #overlay="{ scale, width, height }">
              <IntentLayer
                v-if="intentViews.length"
                :views="intentViews"
                :board-id="store.board?.boardId ?? ''"
                :width="width"
                :height="height"
                :scale="scale"
                :display-overrides="displayOverrides"
                :display-states="displayStates"
                :facts-stale="intentReadFailed"
                :facts-error="store.intentViewsLoadError"
                :disabled="store.saveStatus === 'saving'"
                @display-change="onIntentDisplayChange"
                @refresh="refreshAfterIntentAction"
                @open-record="recordIntent = $event"
              />
            </template>
          </BoardCanvas>

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
                title="保存只落库、不调用模型；提交之后 QIO 才会读取本次互动材料的内容并开始理解"
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
            </div>
          </div>

          <!-- FU6：任务入口钉在板面右上角（不再和状态行挤在一行）。 -->
          <div class="im-board-corner">
            <button
              class="im-chip"
              type="button"
              data-im="tasks-entry"
              :aria-expanded="store.tasksOpen"
              title="互动任务：执行中可暂停 / 取消，已暂停可继续（与聊天停止不是同一件事）"
              @click="store.tasksOpen ? store.closeTaskCenter() : store.openTaskCenter('tasks')"
            >任务{{ store.taskRows.length ? " " + store.taskRows.length : "" }}</button>
          </div>

          <ExecutionRecordPanel
            v-if="recordIntent"
            :intent="recordIntent"
            :board-id="store.board?.boardId ?? ''"
            :open="true"
            @close="recordIntent = null"
          />

          <!-- F03：任务 / 批量入口的真实承接界面（内容与操作资格都来自 store.taskRows） -->
          <TaskCenterPanel
            v-if="store.tasksOpen"
            :focus="store.taskCenterFocus"
            @close="store.closeTaskCenter()"
            @open-record="openRecordById"
          />

          <!-- 悬浮对话：板面保持完整高度；工具栏与输入区固定在对话卡底部，消息向上展开。 -->
          <div class="im-floating-chat" data-im="floating-chat">
            <div class="im-conversation-slot">
              <SaveFailureBanner />
              <InteractiveConversation
                v-model:expanded="store.chatOpen"
                :binding="store.chatBinding"
                :max-height="conversationMaxHeight"
                :auto-collapse-on-board-action="true"
                :extra-entries="recoveryEntries"
              >
                <template #composer-leading>
                  <ConversationModeSwitch class="im-mode-switch" />
                </template>
                <template #toolbar>
                  <BoardToolbar
                    floating
                    :bookmarks-open="bookmarksOpen"
                    @toggle-bookmarks="bookmarksOpen = !bookmarksOpen"
                  />
                </template>
                <template #extra-panel="{ id }">
                  <!-- F05：面板只在被点开时挂载，因此它一挂载就是「正在查看」：
                       加载不再依赖消息区是否展开（对话收起时同样能读到真实事项）。 -->
                  <RecoveryPanel
                    v-if="id === 'recovery'"
                    :board-id="store.board?.boardId ?? ''"
                    :active="true"
                    @recovered="refreshAfterIntentAction"
                  />
                </template>
              </InteractiveConversation>
            </div>
          </div>

          <!-- 新链有 IntentView 时由 IntentLayer 接管；旧托盘只服务于旧链兼容记录。 -->

          <!-- 页面级说明：多个失败可能同时成立，这里纵向堆叠，不能互相遮挡 / 抢点击。 -->
          <div class="im-notices" data-im="board-notices">
            <p v-if="store.loadError" class="im-notice err" role="alert" data-im="load-error">
              板面读取失败：{{ store.loadError }}
              <button class="im-link" type="button" data-im="board-retry" @click="store.load()">重试</button>
            </p>
            <!-- F06：意图读取失败保留旧内容 + 明确区分「更新失败」与「从来没读到过」 -->
            <p v-if="intentReadFailed" class="im-notice warn" role="alert" data-im="intent-read-error">
              {{ intentReadErrorText }}
              <button class="im-link" type="button" data-im="intent-read-retry" :disabled="intentReadBusy" @click="retryIntentRead">
                {{ intentReadBusy ? "读取中…" : "重试读取" }}
              </button>
            </p>
            <p v-if="recoveryReadError" class="im-notice warn" role="status" data-im="recovery-read-error">
              恢复事项读取失败：{{ recoveryReadError }}（这不代表没有事项）
              <button class="im-link" type="button" data-im="recovery-retry" :disabled="recoveryReadBusy" @click="retryRecoveryRead">
                {{ recoveryReadBusy ? "读取中…" : "重试读取" }}
              </button>
            </p>
            <p v-if="intentDisplayError" class="im-notice warn" role="status" data-im="intent-display-error">
              {{ intentDisplayError }}
            </p>
            <p v-if="store.recoverNotice.length" class="im-notice warn" role="status" data-im="recover-notice">
              上次没有结束的任务已经暂停（{{ store.recoverNotice.length }} 项）：重新打开不会自动继续，需要你确认后才会开始。
            </p>

          </div>
        </div>
      </template>

      <template #bookmarks>
        <InteractiveBookmarks @close="bookmarksOpen = false" />
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
/* 悬浮在板面上的对话：板面不被压缩；只有对话卡与工具栏接收指针。 */
.im-floating-chat {
  position: absolute;
  left: 50%;
  bottom: 12px;
  transform: translateX(-50%);
  /* 浮在板面卡片工具栏之上；审批对话框仍由统一层/Teleport 处理。 */
  z-index: calc(var(--im-z-dialog) + 20);
  width: min(calc(100% - 24px), 980px);
  min-width: 0;
  pointer-events: none;
}
.im-floating-chat > .im-conversation-slot {
  pointer-events: auto;
}
/* 三块彼此独立：消息面板、工具栏、输入胶囊各自有表面，不再套一张大卡片。 */
.im-floating-chat :deep(.ic) {
  padding: 0;
  border: 0;
  border-radius: 0;
  background: transparent;
  box-shadow: none;
  backdrop-filter: none;
}
.im-floating-chat :deep(.ic-stream) {
  width: 100%;
  max-width: 860px;
  margin: 0 auto;
  /* FU7：栏本体透明，模糊由组件内的 ::before 承担（板面从栏后透出）。 */
  border: 1px solid color-mix(in srgb, var(--border-strong) 65%, transparent);
  border-radius: var(--r-lg);
  background: transparent;
  box-shadow: var(--shadow-2);
}
.im-floating-chat :deep(.ic-toolbar) {
  max-width: 860px;
  margin-bottom: var(--sp-2);
}
/* 工具栏不再是一整条底板：每个功能是独立按钮，悬在输入框上方。 */
.im-floating-chat :deep(.ic-toolbar .board-toolbar) {
  padding: 0;
  border: 0;
  border-radius: 0;
  background: transparent;
  box-shadow: none;
}
.im-floating-chat :deep(.ic-toolbar .tb-sep) {
  display: none;
}
.im-floating-chat :deep(.ic-toolbar .tb-btn) {
  height: 32px;
  padding: 0 var(--sp-3);
  border-radius: var(--r-pill);
  background: var(--bg-elevated);
  box-shadow: var(--shadow-1);
}
.im-floating-chat :deep(.ic-toolbar .tb-icon-btn) {
  min-width: 32px;
  padding: 0 var(--sp-1);
}
.im-floating-chat :deep(.ic-toolbar .tb-main) {
  display: flex;
  flex-wrap: wrap;
  justify-content: center;
  align-items: center;
  gap: var(--sp-2);
}
.im-floating-chat :deep(.ic-toolbar .tb-edit),
.im-floating-chat :deep(.ic-toolbar .tb-submit),
.im-floating-chat :deep(.ic-toolbar .submit-cluster) {
  display: contents;
}
.im-floating-chat :deep(.ic-toolbar .actions) {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
}
.im-floating-chat :deep(.ic-dock) {
  padding: 0;
}
.im-floating-chat :deep(.ic-composer) {
  /* FU5：输入行内部用三列 grid 居中输入列 —— 外层不能再限制成 860，
     否则 grid 两侧的 1fr 没有可用空间，输入列又会整体右偏。 */
  width: 100%;
  max-width: none;
  margin: 0;
}
.im-floating-chat :deep(.ic-bar) {
  width: fit-content;
  min-height: 26px;
  margin-bottom: var(--sp-1);
}
@media (max-width: 700px) {
  .im-floating-chat {
    bottom: 8px;
    width: calc(100% - 12px);
  }
}
/* 板面槽内舞台：现有 BoardCanvas 的定位祖先。 */
.im-conversation-slot {
  width: 100%;
  min-width: 0;
}
.im-mode-switch {
  flex: none;
  padding-bottom: 8px;
}
@media (max-width: 700px) {
  .im-mode-switch { padding-bottom: 6px; }
}

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
  top: 52px;
  left: 12px;
  right: 12px;
  z-index: 30;
  display: flex;
  align-items: flex-start;
  justify-content: flex-end;
  gap: var(--sp-3);
  pointer-events: none;
}
/* FU6：任务入口钉在板面右上角（HUD 只留状态；拖动/滚动不带走它）。 */
.im-board-corner {
  position: absolute;
  top: 10px;
  right: 10px;
  /* 高于板面自身的标题/提示层，否则上半个按钮会被那条白带盖住 */
  z-index: 60;
  pointer-events: auto;
}
@media (max-width: 900px) {
  .im-board-corner { top: 8px; right: 8px; }
}
/* 板面名让位给全局栏；HUD 只保留真实状态与旧能力入口，避免压住组标题。 */
.im-hud-left,
.im-hud-right {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  min-width: 0;
  pointer-events: auto;
}
.im-hud-left { display: none !important; }
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
/* 页面级说明容器：多个失败可能同时成立，纵向堆叠，互不遮挡点击。 */
.im-notices {
  position: absolute;
  left: 50%;
  transform: translateX(-50%);
  top: 58px;
  z-index: 6;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
  width: max-content;
  max-width: min(720px, calc(100vw - 32px));
  pointer-events: none;
}
.im-notice {
  margin: 0;
  padding: var(--sp-2) var(--sp-4);
  border-radius: var(--r-sm);
  font-size: var(--fs-sm);
  background: var(--bg-elevated);
  border: 1px solid var(--border-subtle);
  box-shadow: var(--shadow-1);
  pointer-events: auto;
  max-width: 100%;
  overflow-wrap: anywhere;
}
.im-notice.err { color: var(--danger); }
.im-notice.warn { color: var(--warning); }
@media (max-width: 900px) {
  .im-count { display: none; }
  .im-board-hud { top: 50px; left: 8px; right: 8px; }
  .im-hud-right { gap: 4px; }
  .im-note { display: none; }
  .im-hud-right {
    flex-wrap: wrap;
    justify-content: flex-end;
    max-width: calc(100vw - 32px);
    gap: 4px;
  }
  .im-chip { padding: 3px 7px; font-size: 10px; }
}
</style>