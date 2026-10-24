<!--
  底部横向悬浮功能栏（主智能体维护）。

  契约：docs/interactive-mode-contract.md
  - §9.6：常态只留**添加、板内搜索、撤销/重做**与右端提交区；
  - §8.4.1：会改板面状态的操作由触发方自己调 board.ts 纯函数 + store.commit；指针模式走 store。

  2026-10-15 布局重构（用户报「窄栏/长说明下说明被挤成极窄竖列、功能栏异常增高」）：
  - 根因一：行列关系原来由**视口**媒体查询（max-width: 1080 / 900 / 620）决定。窗口很宽、
    但聊天面板或侧栏把功能栏挤窄时断点不触发，功能栏仍按宽档挤一行，提交区的长说明被压成竖列。
    现在改成实测「功能栏自己的可用宽度」（见 interactive/toolbarLayout.ts），再决定 data-tb-band。
  - 根因二：主栏是一层 flex-wrap + margin-left:auto，行与列的对应关系随内容长度随机变化。
    现在主栏是显式 grid（edit / history / stats / notes / actions 五个具名区域），
    每一档的「哪一行放哪一组」都写死在 CSS 里，不随内容长度随机排列。
  - 提交区的长说明正文不再参与主栏的横向空间分配：主栏只留简短状态，
    完整说明由 SubmitCluster 在**主栏上方**用功能栏完整宽度按需展开（绝对定位，不参与主栏高度）。

  自包含：不接收任何 props。右端提交区与顶部保存状态是两条独立路径（保存不调用 QIO）。
-->
<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from "vue";
import { useInteractiveStore } from "../../stores/interactive";
import { toolbarAvailableWidth, toolbarBand, type ToolbarBand } from "../../interactive/toolbarLayout";
import AddMenu from "./AddMenu.vue";
import SubmitCluster from "./SubmitCluster.vue";

const props = withDefaults(
  defineProps<{
    /** 侧边书签栏是否展开（纯查看状态；由页面壳持有）。 */
    bookmarksOpen?: boolean;
    /** 嵌入悬浮对话卡时取消板面绝对定位，由对话卡布局接管。 */
    floating?: boolean;
  }>(),
  { bookmarksOpen: false },
);
const emit = defineEmits<{
  (e: "toggle-bookmarks"): void;
  (e: "annotate", tool: "pen" | "eraser"): void;
}>();

const store = useInteractiveStore();

/** 标注菜单：本轮只有准确的占位提示，不创建笔迹、不改变鼠标模式。 */
const annotateOpen = ref(false);
/**
 * 一次操作后的短回执：浮在功能栏上方，**不参与功能栏高度**，
 * 所以功能栏在操作前后保持同一高度（避免「点一下长高一截」）。
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

/* --------------------------------------------------------------------------
 * 按**实际可用宽度**分档（不是视口宽度）
 *
 * availableWidth 由 toolbarLayout.toolbarAvailableWidth 实测：舞台内容宽度扣掉聊天保留宽度
 * 与两侧留白，再与 --im-toolbar-max-width 取小 —— 与 CSS 里 max-width 的实际取值同源。
 * 量不到（jsdom / 首帧）时保持上一档，绝不用 0 当成「最窄」。
 * ------------------------------------------------------------------------ */
const root = ref<HTMLElement | null>(null);
const reserveProbe = ref<HTMLElement | null>(null);
const band = ref<ToolbarBand>("wide");
const availableWidth = ref(0);

function syncBand(): void {
  const el = root.value;
  if (!el) return;
  const width = toolbarAvailableWidth(el, reserveProbe.value);
  availableWidth.value = width;
  band.value = toolbarBand({ available: width, previous: band.value });
}

let observer: ResizeObserver | null = null;

onMounted(() => {
  syncBand();
  if (typeof ResizeObserver === "undefined") return;
  observer = new ResizeObserver(() => syncBand());
  /*
    观察三样东西：
    - 舞台：窗口缩放 / 侧栏出现去掉都会改变它的内容宽度；
    - 功能栏自己：可用宽度被压到内容宽度以下时，功能栏自身会变窄；
    - 保留宽度探测器：聊天几何计划写在舞台上的 --im-geo-* 变了、但舞台尺寸没变时，
      只有探测器的盒子会变 —— 不观察它就漏掉这一档（这正是「聊天面板打开后必须正确响应」那条）。
  */
  const stage = (root.value?.offsetParent as HTMLElement | null) ?? null;
  for (const target of [stage, root.value, reserveProbe.value]) {
    if (target) observer.observe(target);
  }
});

onBeforeUnmount(() => {
  if (noticeTimer) clearTimeout(noticeTimer);
  observer?.disconnect();
  observer = null;
});

</script>

<template>
  <div
    ref="root"
    class="board-toolbar"
    :class="{ 'is-floating': floating }"
    data-im="board-toolbar"
    :data-tb-band="band"
    :data-tb-available="availableWidth ? Math.round(availableWidth) : undefined"
    role="toolbar"
    aria-label="板面操作"
  >
    <p v-if="notice" class="tb-notice" role="status">{{ notice }}</p>

    <!--
      只用来量「功能栏实际可用宽度」的隐形探测器：不显示、不可点、不参与主栏高度。
      reserve 的宽度写成 --im-toolbar-chat-reserve 的取值（可能含 min() 与 vw，无法用
      getPropertyValue 直接取像素），gap 的两项都是纯 px 令牌。
    -->
    <span ref="reserveProbe" class="tb-probe tb-probe-reserve" aria-hidden="true"></span>
    <span class="tb-probe tb-probe-gap" aria-hidden="true"></span>

    <div class="tb-main">
      <div class="tb-edit">
        <AddMenu />

        <span class="tb-sep" aria-hidden="true"></span>

        <!--
          撤销 / 重做用图标按钮（参考图口径）：图标省宽，但必须同时给可访问名称与悬停提示，
          禁用态也要一眼看得出（不是靠自己猜某个灰图标能不能点）。
        -->
        <button
          class="tb-btn tb-icon-btn"
          type="button"
          data-im="undo"
          aria-label="撤销"
          title="撤销上一步板面操作"
          :disabled="!store.canUndo"
          @click="store.undo()"
        >
          <svg class="tb-icon" viewBox="0 0 16 16" aria-hidden="true" focusable="false">
            <g fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
              <path d="M5.2 3.2 2 6.4l3.2 3.2" />
              <path d="M2 6.4h6.2a3.6 3.6 0 0 1 0 7.2H5.4" />
            </g>
          </svg>
        </button>
        <button
          class="tb-btn tb-icon-btn"
          type="button"
          data-im="redo"
          aria-label="重做"
          title="重做被撤销的操作"
          :disabled="!store.canRedo"
          @click="store.redo()"
        >
          <svg class="tb-icon" viewBox="0 0 16 16" aria-hidden="true" focusable="false">
            <g fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" transform="scale(-1,1) translate(-16,0)">
              <path d="M5.2 3.2 2 6.4l3.2 3.2" />
              <path d="M2 6.4h6.2a3.6 3.6 0 0 1 0 7.2H5.4" />
            </g>
          </svg>
        </button>

        <span class="tb-sep" aria-hidden="true"></span>

        <!-- 标注：本轮只给准确的「正在开发中」提示，不创建笔迹、不改变鼠标模式。 -->
        <button
          class="tb-btn tb-icon-btn"
          type="button"
          data-im="annotate-toggle"
          aria-label="标注"
          title="标注（画笔 / 橡皮）"
          :aria-expanded="annotateOpen"
          @click="annotateOpen = !annotateOpen"
        >
          <svg class="tb-icon" viewBox="0 0 16 16" aria-hidden="true" focusable="false">
            <g fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round">
              <path d="M2.8 11.2 11.9 2.1a1.5 1.5 0 0 1 2.1 2.1l-9.1 9.1-2.8.7z" />
              <path d="M10.8 3.2 12.8 5.2" />
            </g>
          </svg>
        </button>
        <div v-if="annotateOpen" class="tb-annotate" data-im="annotate-menu" role="menu">
          <button type="button" class="tb-annotate-item" role="menuitem" @click="say('正在开发中，敬请期待'); annotateOpen = false; emit('annotate', 'pen')">画笔</button>
          <button type="button" class="tb-annotate-item" role="menuitem" @click="say('正在开发中，敬请期待'); annotateOpen = false; emit('annotate', 'eraser')">橡皮</button>
          <p class="tb-annotate-note">正在开发中，敬请期待</p>
        </div>

        <button
          class="tb-btn tb-icon-btn"
          type="button"
          data-im="bookmarks-toggle"
          :class="{ on: props.bookmarksOpen }"
          :aria-label="props.bookmarksOpen ? '收起书签栏' : '打开书签栏'"
          :aria-pressed="props.bookmarksOpen"
          :title="props.bookmarksOpen ? '收起书签栏' : '打开书签栏'"
          @click="emit('toggle-bookmarks')"
        >
          <svg class="tb-icon" viewBox="0 0 16 16" aria-hidden="true" focusable="false">
            <path d="M4 2.5h8v11l-4-2.6-4 2.6z" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round" />
          </svg>
        </button>

        <!-- 2026-10-14：用户要求删除「操作说明」入口与浮层（手势说明不再常驻界面） -->
      </div>

      <!--
        提交区（SubmitCluster）：它自己 display: contents，所以它内部的历史 / 统计 / 操作组
        是**本 grid 的直接网格项**，由 SubmitCluster 声明 grid-area，
        这里只负责哪一档把这些区域放在哪一行哪一列。
      -->
      <div class="tb-submit">
        <SubmitCluster :compact="floating" />
      </div>
    </div>

  </div>
</template>

<style scoped>
/*
  外观与边框在这里；位置（left / bottom / max-width / z-index）由
  styles/interactive-shell.css 统一持有 —— 那里要用重复属性选择器压过 scoped 规则，
  两处不许各写一份宽度上限（旧实现就是两处各一份，窄窗口塌成 0 宽）。
*/
.board-toolbar {
  display: flex;
  flex-direction: column;
  padding: var(--sp-2) var(--sp-3);
  background: var(--bg-elevated);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-lg);
  box-shadow: var(--shadow-2);
  font-family: var(--sans);
}

/* 隐形探测器：参与布局但不可见、不可点，且不影响主栏高度（绝对定位 + 0 高）*/
.tb-probe {
  position: absolute;
  left: 0;
  bottom: 0;
  height: 0;
  visibility: hidden;
  pointer-events: none;
}
.tb-probe-reserve { width: var(--im-toolbar-chat-reserve); }
.tb-probe-gap { width: calc(var(--sp-8) + var(--sp-4)); }

/*
  短回执浮在功能栏正上方右侧：绝对定位，**不改变功能栏高度**。
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

/*
  主栏：显式 grid，五个具名区域。每一档的行列关系写死在下面，不随内容长度随机排列。
  notes 是提交区「需要立刻看见的说明」（失败 / 被挡 / 理解补充等），永远整行独占。
*/
.tb-main {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  grid-template-areas:
    "edit    history"
    "stats   actions"
    "notes   notes";
  align-items: center;
  column-gap: var(--sp-3);
  row-gap: var(--sp-1);
  min-width: 0;
  /* 主栏永不横向滚动：这一条是安全网，真正的修法是每一档都把行列关系排清楚 */
  overflow-x: clip;
}
.tb-main > * { min-width: 0; }

/* mid：可用宽度放不下「编辑操作 + 上次提交状态与查看过程」同一行 —— 上次提交整组独占一行 */
[data-tb-band="mid"] .tb-main {
  grid-template-areas:
    "edit    edit"
    "stats   actions"
    "history history"
    "notes   notes";
}
/* narrow：提交操作组独占一行并右对齐；统计自己一行（允许受控换行） */
[data-tb-band="narrow"] .tb-main {
  grid-template-columns: minmax(0, 1fr);
  grid-template-areas:
    "edit"
    "stats"
    "actions"
    "history"
    "notes";
}
/*
  窄档：提交操作组独占一行并右对齐。
  必须是**后代**选择器（不能用 > 子选择器）：.actions 在 DOM 里隔着 .tb-submit / .submit-cluster
  两层，而这两层是 display: contents —— 它们是元素的父节点，却让子元素在布局上"上提"，
  子选择器会静默失效（上一版就是这么写的，真实渲染量到 justify-self 仍是 auto）。
*/
[data-tb-band="narrow"] .tb-main :deep(.actions) { justify-self: end; }

/*
  主栏里有两块**按状态出现**的内容：上次提交那一组（.sc-history）与说明区（.sc-notes）。
  grid-template-areas 里写死的行，即使一个网格项都没有，也仍然占一条 row-gap ——
  于是「默认两行」会白高 4~8px（真实渲染实测过：默认 764 档 main 高 77 = 28 + 40.6 + 0 + 8）。
  :has() 只看「这一行到底有没有内容」，不改变任何文案或状态：有哪几组就只排哪几行。
  同时写两个 :not(:has()) 时特异度更高（0,4,0），所以「两块都没有」自动压过「只缺一块」。
  宽档不需要单独的 history 规则：history 与 edit 同一行，它不在也不会多出一条轨道。
*/
.tb-main:not(:has([data-im="submit-notes"])) {
  grid-template-areas:
    "edit    history"
    "stats   actions";
}
[data-tb-band="mid"] .tb-main:not(:has([data-im="submit-history"])) {
  grid-template-areas:
    "edit    edit"
    "stats   actions"
    "notes   notes";
}
[data-tb-band="mid"] .tb-main:not(:has([data-im="submit-notes"])) {
  grid-template-areas:
    "edit    edit"
    "stats   actions"
    "history history";
}
[data-tb-band="mid"] .tb-main:not(:has([data-im="submit-history"])):not(:has([data-im="submit-notes"])) {
  grid-template-areas:
    "edit    edit"
    "stats   actions";
}
[data-tb-band="narrow"] .tb-main:not(:has([data-im="submit-history"])) {
  grid-template-areas:
    "edit"
    "stats"
    "actions"
    "notes";
}
[data-tb-band="narrow"] .tb-main:not(:has([data-im="submit-notes"])) {
  grid-template-areas:
    "edit"
    "stats"
    "actions"
    "history";
}
[data-tb-band="narrow"] .tb-main:not(:has([data-im="submit-history"])):not(:has([data-im="submit-notes"])) {
  grid-template-areas:
    "edit"
    "stats"
    "actions";
}

.tb-edit {
  grid-area: edit;
  display: flex;
  flex-wrap: nowrap;
  align-items: center;
  gap: var(--sp-1);
  min-width: 0;
}
.tb-sep { width: 1px; height: 16px; background: var(--border-subtle); flex: none; }

.tb-btn {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-1);
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
.tb-icon { width: 14px; height: 14px; flex: none; }
/* 图标按钮仍保留足够可点面积（高度 ≥ 28px），并保持和其它按钮同一套边界与圆角 */
.tb-icon-btn { padding: 0 var(--sp-1); min-width: 28px; height: 28px; justify-content: center; }
.tb-label { white-space: nowrap; }
.tb-btn:hover:not(:disabled) { color: var(--text-strong); border-color: var(--border-strong); }
.tb-btn:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 1px; }
/* 禁用态：不只降透明度（小图标降透明度后很容易看不清是否禁用），同时去掉边框高光 */
.tb-btn:disabled { opacity: 0.4; cursor: default; border-color: var(--border-subtle); background: var(--bg-inset); }
.tb-btn.on { color: var(--on-accent); background: var(--accent); border-color: var(--accent); }

/* 操作说明：默认收起，点开后浮在功能栏上方（不参与功能栏高度） */
.tb-help { position: relative; display: inline-flex; }
.tb-help-pop {
  position: absolute;
  left: 0;
  bottom: calc(100% + var(--sp-2));
  z-index: 20;
  width: min(420px, 80vw);
  padding: var(--sp-3) var(--sp-4);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-sm);
  background: var(--bg-elevated);
  box-shadow: var(--shadow-2);
}
.tb-help-pop .help-title { margin: 0 0 var(--sp-2); font-size: var(--fs-sm); color: var(--text-strong); }
.tb-help-pop ul { margin: 0; padding-left: 1.1em; }
.tb-help-pop li { font-size: var(--fs-xs); line-height: 1.6; color: var(--text-secondary); }
.tb-help-pop .help-note { margin: var(--sp-2) 0 0; font-size: var(--fs-xs); color: var(--text-muted); }
/* 标注占位菜单：轻量浮层，只说明尚未开发，不生成任何数据。 */
.tb-annotate {
  position: absolute;
  left: 0;
  bottom: calc(100% + var(--sp-2));
  z-index: 22;
  min-width: 150px;
  padding: var(--sp-1);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-sm);
  background: var(--bg-elevated);
  box-shadow: var(--shadow-2);
}
.tb-annotate-item {
  display: block;
  width: 100%;
  padding: var(--sp-1) var(--sp-2);
  border: 0;
  border-radius: var(--r-xs);
  background: none;
  color: var(--text-secondary);
  font: inherit;
  font-size: var(--fs-xs);
  text-align: left;
  cursor: pointer;
}
.tb-annotate-item:hover { background: var(--layer-hover); color: var(--text-strong); }
.tb-annotate-note { margin: var(--sp-1) var(--sp-2); color: var(--text-muted); font-size: var(--fs-xs); }

/*
  提交区容器：display: contents —— 它自己不生成盒子，内部的历史 / 统计 / 操作组
  直接成为上面 .tb-main 的网格项（SubmitCluster 自己声明 grid-area）。
  于是「第一行右 = 上次提交状态」「第二行左 = 本次统计」这种交错只用一个 grid 就能成立。
*/
.tb-submit { display: contents; }

/*
  极窄档：只压内边距与行高，**不缩字号**（主要操作不缩成元信息字号）。
  这些是纯外观微调，行列关系已经由 data-tb-band 决定。
*/
[data-tb-band="narrow"] .board-toolbar { padding: var(--sp-1) var(--sp-2); }
[data-tb-band="narrow"] .tb-btn { line-height: 1.45; }
[data-tb-band="narrow"] .tb-main { column-gap: var(--sp-2); row-gap: 2px; }
[data-tb-band="narrow"] .tb-edit { gap: 2px; }
@media (max-width: 620px) {
  /* 窗口本身很小（不是容器被挤窄）时的兜底微调：同样只压行高与内边距，不缩字号 */
  .tb-btn { line-height: 1.45; padding: 0 var(--sp-1); }
  .tb-main { gap: 2px var(--sp-1); }
  .tb-edit { gap: 2px; }
}
</style>
