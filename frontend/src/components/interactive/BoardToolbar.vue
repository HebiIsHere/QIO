<!--
  底部横向悬浮工具栏（子智能体 A 负责）。

  契约：docs/interactive-mode-contract.md
  - §9.6（本轮，覆盖 §8.1 的旧写法）：常态只留**添加、板内搜索、撤销/重做、视图控制**与右端提交区；
    分组类操作搬到选中对象附近的 SelectionMenu（由 BoardCard 渲染），未选中时不铺一排禁用按钮；
    消除重复分隔线与重复包裹；800×600 下总高不超过约 96px、最多两行。
  - §8.4.1：会改板面状态的操作由触发方自己调 board.ts 纯函数 + store.commit；指针模式走 store；
    一次性定位走 window 事件 qio:interactive:locate-card。
  - 提交区是 C 的 <SubmitCluster />：这里只负责给它位置与分界，右端与编辑操作分开。

  自包含：不接收任何 props —— 指针模式读/写 store.boardMode，撤销/重做直接调 store 的栈。
  右端提交区与顶部保存状态是两条独立路径（保存不调用 QIO）。
-->
<script setup lang="ts">
import { onBeforeUnmount, ref } from "vue";
import { useInteractiveStore } from "../../stores/interactive";
import AddMenu from "./AddMenu.vue";
import BoardSearchPanel from "./BoardSearchPanel.vue";
import SubmitCluster from "./SubmitCluster.vue";

type BoardMode = "select" | "rect" | "link";

const store = useInteractiveStore();

/** 板内搜索浮层是否展开（只影响查看，不改板面数据）。 */
const searchOpen = ref(false);
/**
 * 一次操作后的短回执：浮在工具栏上方，**不参与工具栏高度**，
 * 所以工具栏在操作前后保持同一高度（避免「点一下长高一截」）。
 */
const notice = ref("");
let noticeTimer: ReturnType<typeof setTimeout> | null = null;

function say(text: string): void {
  notice.value = text;
  if (noticeTimer) clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => {
    notice.value = "";
  }, 5000);
}

onBeforeUnmount(() => {
  if (noticeTimer) clearTimeout(noticeTimer);
});

// --- 视图控制 -------------------------------------------------------------
// 已经确认的手势不需要模式：拖动空白处平移、空格 + 拖动框选、从连接点拖线。
// 这三个开关只是把同一件事固定成常驻方式（给不习惯修饰键的人用），所以做得紧凑、不抢位置。

const MODES: { mode: BoardMode; label: string; title: string; hint: string }[] = [
  { mode: "select", label: "选择", title: "点卡片选择，Shift 点可以加选；拖动空白处平移", hint: "选择：点卡片选中，Shift 加选；拖动空白处平移。" },
  { mode: "rect", label: "框选", title: "拖动空白处框选卡片（也可以随时按住空格拖动，不用先切这里）", hint: "框选：拖动空白处把卡片框起来；按空格再拖也一样。" },
  { mode: "link", label: "连线", title: "从卡片连接点拖到另一张卡片建立关系", hint: "连线：从卡片连接点拖到另一张卡片；方向与含义由你写。" },
];

function setMode(next: BoardMode): void {
  if (store.boardMode === next) return;
  store.setBoardMode(next);
  const entry = MODES.find((item) => item.mode === next);
  if (entry) say(entry.hint);
}

function toggleSearch(): void {
  searchOpen.value = !searchOpen.value;
}
</script>

<template>
  <div class="board-toolbar" data-im="board-toolbar" role="toolbar" aria-label="板面操作">
    <p v-if="notice" class="tb-notice" role="status">{{ notice }}</p>

    <div class="tb-main">
      <div class="tb-edit">
        <AddMenu />

        <span class="tb-sep" aria-hidden="true"></span>

        <button
          class="tb-btn"
          type="button"
          data-im="search-toggle"
          :class="{ on: searchOpen }"
          :aria-pressed="searchOpen"
          title="板内搜索：只查你自己的板面"
          @click="toggleSearch"
        >
          搜索
        </button>

        <span class="tb-sep" aria-hidden="true"></span>

        <button class="tb-btn" type="button" data-im="undo" :disabled="!store.canUndo" title="撤销上一步板面操作" @click="store.undo()">
          撤销
        </button>
        <button class="tb-btn" type="button" data-im="redo" :disabled="!store.canRedo" title="重做被撤销的操作" @click="store.redo()">
          重做
        </button>

        <span class="tb-sep" aria-hidden="true"></span>

        <div class="tb-modes" role="group" aria-label="视图">
          <button
            v-for="item in MODES"
            :key="item.mode"
            class="tb-btn mode"
            type="button"
            :data-im="'mode-' + item.mode"
            :class="{ on: store.boardMode === item.mode }"
            :aria-pressed="store.boardMode === item.mode"
            :title="item.title"
            @click="setMode(item.mode)"
          >
            {{ item.label }}
          </button>
        </div>
      </div>

      <!-- 提交区（C 负责）：与编辑操作留出间距并区分样式，避免误点 -->
      <div class="tb-submit">
        <SubmitCluster />
      </div>
    </div>

    <BoardSearchPanel v-if="searchOpen" @close="searchOpen = false" />
  </div>
</template>

<style scoped>
/*
  悬浮在板面底部居中：position:absolute + left:50% + translateX(-50%) + bottom。
  --tb-chat-reserve 是给右下角聊天入口留的横向空间：工具栏在**剩余空间**里居中，
  右端不会压住聊天按钮。数值与 styles/interactive-shell.css 里的 --im-chat-reserve 一致
  （shell.css 由主智能体在集成时导入，两处必须同值；这里是未导入时的兜底）。
  背景只用令牌，不做重度模糊。高度问题不靠 max-height + 滚动条解决：
  上一轮实测那样会和「量工具栏高度做避让」的观察器形成尺寸震荡。
*/
.board-toolbar {
  --tb-chat-reserve: 132px;
  --tb-max-width: 1180px;
  position: absolute;
  left: calc(50% - var(--tb-chat-reserve) / 2);
  transform: translateX(-50%);
  bottom: var(--sp-4);
  max-width: min(var(--tb-max-width), calc(100% - var(--tb-chat-reserve) - var(--sp-8)));
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
  padding: var(--sp-1) var(--sp-3);
  background: var(--bg-elevated);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-lg);
  box-shadow: var(--shadow-2);
  z-index: 30;
  font-family: var(--sans);
}

/*
  短回执浮在工具栏正上方右侧：绝对定位，**不改变工具栏高度**。
  左下的板内搜索浮层占左侧，这里靠右，两者在 800×600 下互不相交。
*/
.tb-notice {
  position: absolute;
  right: 0;
  bottom: calc(100% + var(--sp-2));
  margin: 0;
  max-width: min(320px, 62%);
  padding: var(--sp-1) var(--sp-2);
  background: var(--bg-elevated);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-sm);
  box-shadow: var(--shadow-1);
  font-size: var(--fs-xs);
  line-height: var(--lh-tight);
  color: var(--text-secondary);
}

.tb-main {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--sp-1) var(--sp-2);
  min-width: 0;
}

.tb-edit {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--sp-1);
  min-width: 0;
}
.tb-sep { width: 1px; height: 16px; background: var(--border-subtle); flex: none; }

.tb-btn {
  font: inherit;
  font-size: var(--fs-sm);
  line-height: 1.7;
  color: var(--text-secondary);
  background: var(--bg-surface);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-xs);
  padding: 0 var(--sp-2);
  cursor: pointer;
  white-space: nowrap;
}
.tb-btn:hover:not(:disabled) { color: var(--text-strong); border-color: var(--border-strong); }
.tb-btn:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 1px; }
.tb-btn:disabled { opacity: 0.45; cursor: default; }
.tb-btn.on { color: var(--on-accent); background: var(--accent); border-color: var(--accent); }

/* 三个视图开关收成一段：共用一个边框与圆角，减少一组按钮的碎片感 */
.tb-modes {
  display: inline-flex;
  align-items: center;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-xs);
  overflow: hidden;
  background: var(--bg-surface);
}
.tb-modes .tb-btn {
  border: none;
  border-radius: 0;
  background: none;
  padding: 0 var(--sp-2);
}
.tb-modes .tb-btn + .tb-btn { border-left: 1px solid var(--border-subtle); }
.tb-modes .tb-btn.on { color: var(--on-accent); background: var(--accent); }

/*
  右端提交区：只给位置与分界 —— 分隔线**只画一条**。
  SubmitCluster 自己那条 border-left 在这里被收掉，改由本组件给一条，避免出现两条挨着的线。
*/
.tb-submit {
  margin-left: auto;
  display: flex;
  align-items: center;
  min-width: 0;
  padding-left: var(--sp-3);
  border-left: 1px solid var(--border-strong);
}

/*
  下面几条 :deep() 是对 C 的 SubmitCluster 的**容器级收束**：编辑器与提交区各占一行、
  提交区里不再把三行文字竖着摞起来。为什么必须由容器管：可测量目标是
  「800×600 下工具栏总高 ≤ 约 96px、最多两行」，而提交区内部排布由子组件决定。
    1) 排成一行：状态文字在左、提交操作在右；
    2) 收起与顶栏 save-status 重复的「已保存…」长句（§9.6：各自只设一处主状态），
       提交状态与允许查看范围都还在，边界说法也在提交状态里；
    3) 可见范围最多两行显示，全文仍留在页面里（自动化读到的文字不变）。
  只按冻结的 data-im 钩子与 .texts 布局钩子选中；C 把 SubmitCluster 自己做短之后可以整块删掉。
*/
.tb-submit :deep([data-im="submit-cluster"]) {
  flex-direction: row;
  align-items: center;
  gap: var(--sp-3);
  min-width: 0;
  padding-left: 0;
  border-left: none;
}
.tb-submit :deep([data-im="submit-save-status"]) { display: none; }
.tb-submit :deep(.texts) {
  flex: 1 1 auto;
  min-width: min(240px, 100%);
  max-width: min(460px, 46vw);
  gap: 0;
}
.tb-submit :deep([data-im="visible-range"]) { -webkit-line-clamp: 2; }
/* 改动条数摘要只在宽窗口显示：窄窗口它会把状态文字挤成三行（实测 800×600 下 240px 宽 → 49px 高）。
   完整的「本次改动」本来就在「查看本次改动」里，这里收起不影响可达性。 */
.tb-submit :deep([data-im="submit-cluster"] .count) { display: none; }

/*
  窄窗口（≤1080，涵盖 1024×768 与 800×600）：提交区独占一行，编辑操作在上、提交在下。
  工具栏同时吃满可用宽度：实测「按内容收缩」到 435px 时，提交区的状态文字会被挤成
  一个字一行的竖排（800×600 实测 386px 高）。
*/
@media (max-width: 1080px) {
  .board-toolbar { width: 100%; }
  .tb-submit {
    flex: 1 1 100%;
    margin-left: 0;
    padding-left: 0;
    padding-top: var(--sp-1);
    border-left: none;
    border-top: 1px solid var(--border-subtle);
  }
  .tb-submit :deep(.texts) { max-width: none; }
  .tb-submit :deep([data-im="visible-range"]) { -webkit-line-clamp: 1; }
  /* 这一档按钮不必那么大：提交按钮保持可点面积（高度 ≥ 28px）但不再撑高整条栏 */
  .tb-submit :deep([data-im="submit"]) { font-size: var(--fs-sm); padding: var(--sp-1) var(--sp-4); }
}

/*
  宽窗口：工具栏按内容取宽（max-content），让「编辑操作 + 提交区」排在同一行。
  实测按收缩宽度算时 1440×900 会被压成两行（343 + 693 = 1036 > 实际分到的 786）。
  上限仍由 max-width 与 --tb-chat-reserve 兜住，不会压到右下角聊天入口。
*/
@media (min-width: 1081px) {
  .board-toolbar { width: max-content; }
}

/* 1024×768：标签与间距收一档（主要入口全部保留） */
@media (max-width: 1024px) {
  .board-toolbar {
    --tb-chat-reserve: 104px;
    --tb-max-width: 100%;
    bottom: var(--sp-3);
    padding: var(--sp-1) var(--sp-2);
  }
}

/* 800×600：再压一档内边距与分隔线；按钮字号**不缩**（主要操作不缩成元信息字号） */
@media (max-width: 800px) {
  .board-toolbar {
    --tb-chat-reserve: 88px;
    bottom: var(--sp-2);
    padding: var(--sp-1) var(--sp-2);
    gap: 0;
  }
  .tb-sep { display: none; }
}
</style>
