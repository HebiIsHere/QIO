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

  2026-11-17 互动页底部操作区改版（rail 形态；rail prop 默认 false）：
  - rail=false 时上面这套 dock 行为 / 样式 / 分档**一律不变**；
  - rail=true 时根变成一条**透明单行操作行**：右对齐、不换行、不套整条胶囊；每个控件是
    自己的命中区（中性描边 + --qio-ui-radius + 悬停 / 聚焦 / 禁用态），细线图标；
  - 降级顺序固定：先省略能由图标说明的文字（full → icons：添加 / 详情只剩图标 + aria-label），
    再把低频工具收进右侧「更多」（icons → more：撤销 / 重做 / 书签 / 提交详情；添加、提交、
    更多仍在行上）。档位由**工具栏自己**的实测宽度决定（ResizeObserver + getBoundingClientRect），
    纯函数见 interactive/toolbarLayout.ts 的 railToolbarMode；
  - rail 下行高稳定在 32px：提交说明区（.sc-notes，含真实入口）浮在操作行上方，不参与行高；
    「更多」面板挂在行右上，Esc 走共用层级栈、外点关闭、打开焦点进第一项、关闭回按钮；
    开合只改显示，不改任何板面数据。

  右端提交区与顶部保存状态是两条独立路径（保存不调用 QIO）。
-->
<script setup lang="ts">
import { vQioTip } from "../../composables/useQioTip";
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { registerEscapeLayer } from "../../composables/useEscapeLayer";
import { useInteractiveStore } from "../../stores/interactive";
import {
  railToolbarMode,
  toolbarAvailableWidth,
  toolbarBand,
  type RailToolbarMode,
  type ToolbarBand,
} from "../../interactive/toolbarLayout";
import AddMenu from "./AddMenu.vue";
import SubmitCluster from "./SubmitCluster.vue";

const props = withDefaults(
  defineProps<{
    /** 侧边书签栏是否展开（纯查看状态；由页面壳持有）。 */
    bookmarksOpen?: boolean;
    /** 嵌入悬浮对话卡时取消板面绝对定位，由对话卡布局接管。 */
    floating?: boolean;
    /**
     * 互动页底部「操作行」形态：单行透明右对齐、独立按钮，按**自有实测宽度**三档降级
     * （full → icons → more）。默认 false 时保持上面的 dock 行为，一点不变。
     */
    rail?: boolean;
  }>(),
  { bookmarksOpen: false, rail: false },
);
const emit = defineEmits<{
  (e: "toggle-bookmarks"): void;
  /** 提交详情 / 上次结果 / 过程面板任一被打开（页面壳据此做窄窗面板切换，#05）。 */
  (e: "panel-open"): void;
}>();

const store = useInteractiveStore();

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

/* --------------------------------------------------------------------------
 * rail（互动页底部「操作行」）三档降级：full → icons → more
 *
 * 宽度必须量**工具栏自己**（getBoundingClientRect().width），不是视口：操作行嵌在聊天面板里，
 * 窗口很宽但面板 / 侧栏把它挤窄时，视口断点根本不会触发（与上面 dock 分档同一个教训）。
 * 父组件（InteractiveConversation 的 .ic-rail-right）把右侧槽设成确定宽度，所以这里
 * width:100% 量到的就是真实可用宽度；不设 flex:1（父容器已经是确定宽度）。
 * 量不到（jsdom / 首帧 / 容器还没布局）时保持上一档，初始 full —— 绝不用 0 当成「最窄」。
 * ------------------------------------------------------------------------ */
const railMode = ref<RailToolbarMode>("full");
const isRail = computed(() => props.rail);
/** rail 的 icons 档：能由图标说明的文字（添加 / 详情）让位给图标。 */
const isRailIcons = computed(() => props.rail && railMode.value === "icons");
/** rail 的 more 档：撤销 / 重做 / 书签 / 提交详情收进「更多」，添加 + 提交 + 更多常驻行上。 */
const isRailMore = computed(() => props.rail && railMode.value === "more");
/** full 档才显示「添加 / 详情」文字；icons / more 档由图标 + aria-label 承担可访问名称。 */
const railIconOnly = computed(() => props.rail && railMode.value !== "full");

function syncRailMode(): void {
  if (!props.rail) return;
  const el = root.value;
  if (!el) return;
  let width = 0;
  // 量不到（jsdom / 非布局环境）时按 0 处理：函数会保持上一档，不会误降成 more
  try {
    width = el.getBoundingClientRect().width;
  } catch {
    width = 0;
  }
  railMode.value = railToolbarMode({ available: width, previous: railMode.value });
}

let observer: ResizeObserver | null = null;

onMounted(() => {
  syncBand();
  syncRailMode();
  // rail「更多」面板：点击外部关闭（pointerdown 捕获阶段；点在根内不关，交给按钮自己的 click）
  document.addEventListener("pointerdown", onDocumentPointerDown, true);
  if (typeof ResizeObserver === "undefined") return;
  observer = new ResizeObserver(() => {
    syncBand();
    syncRailMode();
  });
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
  observer?.disconnect();
  observer = null;
  document.removeEventListener("pointerdown", onDocumentPointerDown, true);
  moreEscapeDispose?.();
  moreEscapeDispose = null;
});

const submitRef = ref<InstanceType<typeof SubmitCluster> | null>(null);

/** 页面壳（#05 窄窗切换 / #40 Esc 层级）用：只关提交面板，不改任何数据。 */
defineExpose({
  closeSubmitDetails(): void {
    submitRef.value?.closePanel?.();
  },
  submitDetailsOpen(): boolean {
    return typeof submitRef.value?.isPanelOpen === "function" ? Boolean(submitRef.value.isPanelOpen()) : false;
  },
});

/* --------------------------------------------------------------------------
 * rail 的「更多」菜单（只在 more 档出现）
 *
 * 只收**低频工具**：撤销 / 重做 / 书签栏 / 提交详情；添加、提交与「更多」本身仍在行上常驻。
 * 打开 / 关闭只改显示：不调用 store.commit，不改板面、草稿、排队与提交状态。
 * 可访问性：aria-haspopup + aria-expanded；Esc 走共用层级栈（不自己抢 window keydown）；
 * 点击外部关闭；打开焦点进第一项、关闭回「更多」按钮，Tab 可在面板内逐项到达。
 * ------------------------------------------------------------------------ */
const moreOpen = ref(false);
const moreButton = ref<HTMLButtonElement | null>(null);
const morePanel = ref<HTMLElement | null>(null);

function openMore(): void {
  if (moreOpen.value) return;
  moreOpen.value = true;
  // 打开后焦点进入第一项：键盘用户不必再从「更多」按钮 Tab 一遍（禁用项不可聚焦，跳过）
  void nextTick(() => {
    const panel = morePanel.value;
    const first =
      panel?.querySelector<HTMLButtonElement>("button:not(:disabled)") ??
      panel?.querySelector<HTMLButtonElement>("button");
    first?.focus();
  });
}

/** 收起更多面板；returnFocus=true 时焦点还给「更多」按钮（Esc / 选项动作后不掉焦点）。 */
function closeMore(returnFocus: boolean): void {
  if (!moreOpen.value) return;
  moreOpen.value = false;
  if (returnFocus) void nextTick(() => moreButton.value?.focus());
}

function toggleMore(): void {
  if (moreOpen.value) closeMore(true);
  else openMore();
}

let moreEscapeDispose: (() => void) | null = null;
function syncMoreEscapeLayer(open: boolean): void {
  if (open && !moreEscapeDispose) {
    moreEscapeDispose = registerEscapeLayer({
      id: "board-toolbar-more",
      active: () => moreOpen.value,
      onEscape: () => {
        closeMore(true);
        return true;
      },
    });
  } else if (!open && moreEscapeDispose) {
    moreEscapeDispose();
    moreEscapeDispose = null;
  }
}
watch(moreOpen, (open) => syncMoreEscapeLayer(open), { immediate: true });

// 降级档位回到 full / icons 时「更多」按钮会消失：面板必须一起收起，不留「看不见但已打开」的状态
watch([() => props.rail, railMode], () => {
  if (!isRailMore.value) closeMore(false);
}, { immediate: true });

function onDocumentPointerDown(event: PointerEvent): void {
  if (!moreOpen.value) return;
  const target = event.target as Node | null;
  if (target && root.value?.contains(target)) return;
  closeMore(false); // 点外部关面板不抢焦点：用户已经点到了别处
}

/** 更多菜单里的操作：与行上同名入口走同一条 store / emit / SubmitCluster 通道，不另开数据路径。 */
function moreUndo(): void {
  if (!store.canUndo) return;
  store.undo();
  closeMore(true);
}
function moreRedo(): void {
  if (!store.canRedo) return;
  store.redo();
  closeMore(true);
}
function moreToggleBookmarks(): void {
  emit("toggle-bookmarks");
  closeMore(true);
}
function moreOpenSubmitDetails(): void {
  submitRef.value?.openDetails?.();
  closeMore(true);
}

</script>

<template>
  <div
    ref="root"
    class="board-toolbar"
    :class="{ 'is-floating': floating, 'is-rail': isRail }"
    data-im="board-toolbar"
    :data-tb-rail="isRail ? 'true' : undefined"
    :data-tb-mode="isRail ? railMode : undefined"
    :data-tb-band="band"
    :data-tb-available="availableWidth ? Math.round(availableWidth) : undefined"
    role="toolbar"
    aria-label="板面操作"
  >
    <!--
      只用来量「功能栏实际可用宽度」的隐形探测器：不显示、不可点、不参与主栏高度。
      reserve 的宽度写成 --im-toolbar-chat-reserve 的取值（可能含 min() 与 vw，无法用
      getPropertyValue 直接取像素），gap 的两项都是纯 px 令牌。
    -->
    <span ref="reserveProbe" class="tb-probe tb-probe-reserve" aria-hidden="true"></span>
    <span class="tb-probe tb-probe-gap" aria-hidden="true"></span>

    <div class="tb-main">
      <div class="tb-edit">
        <AddMenu :icon-only="railIconOnly" />

        <span class="tb-sep" aria-hidden="true"></span>

        <!--
          撤销 / 重做用图标按钮（参考图口径）：图标省宽，但必须同时给可访问名称与悬停提示，
          禁用态也要一眼看得出（不是靠自己猜某个灰图标能不能点）。
        -->
        <button
          v-if="!isRailMore"
          class="tb-btn tb-icon-btn"
          type="button"
          data-im="undo"
          aria-label="撤销"
          v-qio-tip="'撤销上一步板面操作'"
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
          v-if="!isRailMore"
          class="tb-btn tb-icon-btn"
          type="button"
          data-im="redo"
          aria-label="重做"
          v-qio-tip="'重做被撤销的操作'"
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

        <!-- #14：画笔 / 橡皮入口已移除：常态工具栏只保留已开放的能力；书签栏入口紧邻编辑组。 -->
        <button
          v-if="!isRailMore"
          class="tb-btn tb-icon-btn"
          type="button"
          data-im="bookmarks-toggle"
          :class="{ on: props.bookmarksOpen }"
          :aria-label="props.bookmarksOpen ? '收起书签栏' : '打开书签栏'"
          :aria-pressed="props.bookmarksOpen"
          v-qio-tip="props.bookmarksOpen ? '收起书签栏' : '打开书签栏'"
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
        <!--
          rail 下按档位换 SubmitCluster 的形态：icons 档用 compact（「详情」变图标）；
          more 档隐藏行上「详情」，入口改由右侧「更多」菜单的 openDetails() 打开。
          full 档保持带「详情」文字的按钮；提交按钮文案与逻辑始终由 SubmitCluster 自己维护。
          rail 模式下 floating 不再决定 compact（页面壳为了共用悬浮卡样式会同时传 floating，
          但 full 档必须露出「详情」文字，所以 compact 只跟 icons 档走）。
        -->
        <SubmitCluster
          ref="submitRef"
          :compact="isRail ? isRailIcons : floating"
          :hide-details="isRailMore"
          @panel-open="emit('panel-open')"
        />
      </div>

      <!--
        rail 的「更多」（只在 more 档出现）：低频工具收在这里，添加 / 提交 / 更多仍在行上常驻。
        面板从操作行右上方展开（绝对定位，不参与行高）；Esc / 外点关闭、打开焦点进第一项。
      -->
      <div v-if="isRailMore" class="tb-more">
        <button
          ref="moreButton"
          class="tb-btn tb-icon-btn"
          type="button"
          data-im="board-more"
          aria-haspopup="dialog"
          :aria-expanded="moreOpen"
          aria-label="更多板面工具"
          v-qio-tip="'更多板面工具：撤销、重做、书签栏、提交详情'"
          @click="toggleMore"
        >
          <svg class="tb-icon" viewBox="0 0 16 16" aria-hidden="true" focusable="false">
            <g fill="currentColor" stroke="none">
              <circle cx="3.6" cy="8" r="1.3" />
              <circle cx="8" cy="8" r="1.3" />
              <circle cx="12.4" cy="8" r="1.3" />
            </g>
          </svg>
        </button>
        <div
          v-if="moreOpen"
          ref="morePanel"
          class="tb-more-panel"
          data-im="board-more-panel"
          role="dialog"
          aria-label="更多板面工具"
        >
          <button
            class="tb-more-item"
            type="button"
            data-im="board-more-undo"
            :disabled="!store.canUndo"
            v-qio-tip="'撤销上一步板面操作'"
            @click="moreUndo"
          >撤销</button>
          <button
            class="tb-more-item"
            type="button"
            data-im="board-more-redo"
            :disabled="!store.canRedo"
            v-qio-tip="'重做被撤销的操作'"
            @click="moreRedo"
          >重做</button>
          <button
            class="tb-more-item"
            type="button"
            data-im="board-more-bookmarks"
            :class="{ on: props.bookmarksOpen }"
            :aria-pressed="props.bookmarksOpen"
            v-qio-tip="props.bookmarksOpen ? '收起书签栏' : '打开书签栏'"
            @click="moreToggleBookmarks"
          >书签栏</button>
          <button
            class="tb-more-item"
            type="button"
            data-im="board-more-details"
            v-qio-tip="'提交详情'"
            @click="moreOpenSubmitDetails"
          >提交详情</button>
        </div>
      </div>
    </div>

  </div>
</template>

<style scoped>
/*
  外观与边框在这里；宽度测量变量（--im-toolbar-max-width / --im-toolbar-chat-reserve）
  与悬浮态表面重置由 styles/layers.css 统一持有 —— 档位纯函数（interactive/toolbarLayout.ts）
  与这里的隐形探测器读同一份变量，两处不许各写一份宽度上限
  （旧实现就是两处各一份，窄窗口塌成 0 宽）。
*/
.board-toolbar {
  display: flex;
  flex-direction: column;
  padding: var(--sp-2) var(--sp-3);
  background: var(--bg-elevated);
  border: 1px solid var(--border-strong);
  border-radius: var(--qio-ui-radius);
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
  border-radius: var(--qio-ui-radius);
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
  border-radius: var(--qio-ui-radius);
  background: var(--bg-elevated);
  box-shadow: var(--shadow-2);
}
.tb-help-pop .help-title { margin: 0 0 var(--sp-2); font-size: var(--fs-sm); color: var(--text-strong); }
.tb-help-pop ul { margin: 0; padding-left: 1.1em; }
.tb-help-pop li { font-size: var(--fs-xs); line-height: 1.6; color: var(--text-secondary); }
.tb-help-pop .help-note { margin: var(--sp-2) 0 0; font-size: var(--fs-xs); color: var(--text-muted); }

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

/* ==========================================================================
 * rail：互动页底部「操作行」形态（rail=true；rail=false 时上面所有 dock 规则不受影响）
 *
 * 操作行是一条**透明单行**：不套整条胶囊、不换行、右对齐；每个控件是自己的命中区
 * （中性描边 + --qio-ui-radius + 悬停 / 聚焦 / 禁用态）。根宽由父容器的确定宽度决定
 * （InteractiveConversation 的 .ic-rail-right），这里 width:100% 才能量到真实可用宽度。
 * ========================================================================== */
.board-toolbar.is-rail {
  position: relative; /* 「更多」面板与提交状态卡都锚定在操作行根内 */
  display: flex;
  flex-direction: row;
  width: 100%;
  min-width: 0;
  max-width: none;
  padding: 0;
  background: transparent;
  border: 0;
  border-radius: 0;
  box-shadow: none;
}

/* 单行、右对齐、永不换行；行高只由 32px 控件决定（面板绝对定位，不参与行高） */
[data-tb-rail="true"] .tb-main {
  display: flex;
  flex-wrap: nowrap;
  align-items: center;
  justify-content: flex-end;
  gap: var(--sp-1); /* 等距小间距 4px（约 4–6px） */
  width: 100%;
  min-width: 0;
  column-gap: var(--sp-1);
  row-gap: 0;
  overflow: visible; /* 面板要能溢出到操作行上方 */
}
/* 分组容器退成 display:contents：所有控件成为同一 flex 行的子项，间距完全等距 */
[data-tb-rail="true"] .tb-edit { display: contents; }
[data-tb-rail="true"] .tb-sep { display: none; }

/* 独立按钮：各自的中性描边 / 命中区；行本身透明 */
[data-tb-rail="true"] .tb-btn {
  height: 32px;
  /* 横向内边距统一 8px：与图标按钮的 32px 方框一起构成等距小间距，并让 full 档最长内容
     （提交态「用同一身份重试」约 326px）在 344px 下界内仍有 18px 余量。 */
  padding: 0 var(--sp-2);
  border-radius: var(--qio-ui-radius);
}
[data-tb-rail="true"] .tb-icon-btn {
  min-width: 32px;
  padding: 0;
}
[data-tb-rail="true"] .tb-btn:hover:not(:disabled) {
  color: var(--text-strong);
  background: var(--bg-inset);
  border-color: var(--border-strong);
}
[data-tb-rail="true"] .tb-btn:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 1px;
}
/* 禁用态：不只降透明度 —— 换底色 + 弱化描边与文字，仍能看清哪个按钮不可用 */
[data-tb-rail="true"] .tb-btn:disabled {
  opacity: 1;
  color: var(--text-faint);
  background: var(--bg-inset);
  border-color: var(--border-subtle);
  cursor: default;
}
/* 选中态（书签栏展开）保持既有强调色 */
[data-tb-rail="true"] .tb-btn.on {
  color: var(--on-accent);
  background: var(--accent);
  border-color: var(--accent);
}

/* AddMenu 的按钮在子组件里：scoped 样式用 :deep 透进来，与行上其它按钮同一套外观
   （问题一：同一操作行里的按钮只保留一种尺寸/圆角/描边/底色/禁用态/图标色）。 */
[data-tb-rail="true"] :deep(.add .tb-btn) {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: var(--sp-1);
  height: 32px;
  padding: 0 var(--sp-2);
  border: 1px solid var(--border-subtle);
  border-radius: var(--qio-ui-radius);
  background: var(--bg-surface);
  color: var(--text-secondary);
  line-height: 1;
}
[data-tb-rail="true"] :deep(.add .tb-btn.is-icon-only) {
  min-width: 32px;
  padding: 0;
  justify-content: center;
}
[data-tb-rail="true"] :deep(.add .tb-btn:hover) {
  color: var(--text-strong);
  background: var(--bg-inset);
  border-color: var(--border-strong);
}
/* 「＋」跟其它图标一样用当前文字色，不再单独用链接色（样式混乱来源之一）。 */
[data-tb-rail="true"] :deep(.add .plus) { color: currentColor; }

/* SubmitCluster 的 .actions（详情 + 提交）在行内；历史 / 统计不占操作行（compact 档也不渲染） */
[data-tb-rail="true"] :deep(.sc-history),
[data-tb-rail="true"] :deep(.sc-stats) { display: none; }
[data-tb-rail="true"] :deep(.actions) {
  gap: var(--sp-1);
  justify-self: auto;
}
[data-tb-rail="true"] :deep(.actions .details) {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  height: 32px;
  padding: 0 var(--sp-2);
  font-size: var(--fs-sm);
  line-height: 1;
  color: var(--text-secondary);
  background: var(--bg-surface);
  border: 1px solid var(--border-subtle);
  border-radius: var(--qio-ui-radius);
}
[data-tb-rail="true"] :deep(.actions .details:hover) {
  color: var(--text-strong);
  border-color: var(--border-strong);
}
[data-tb-rail="true"] :deep(.actions .details[aria-expanded="true"]) {
  color: var(--text-strong);
  background: var(--bg-inset);
  border-color: var(--border-strong);
}
[data-tb-rail="true"] :deep(.actions .details:disabled) {
  opacity: 1;
  color: var(--text-faint);
  background: var(--bg-inset);
  border-color: var(--border-subtle);
  cursor: default;
}
[data-tb-rail="true"] :deep(.actions .sc-details-icon) {
  width: 32px;
  min-width: 32px;
  height: 32px;
  padding: 0;
  border-radius: var(--qio-ui-radius);
}
[data-tb-rail="true"] :deep(.actions .sc-details-icon svg) { width: 16px; height: 16px; }
/* 主提交按钮：操作行唯一的强调色操作；尺寸/圆角/内边距与相邻按钮一致。
   禁用时换成与其它按钮相同的中性块（不只靠透明度），仍能看清「提交给 QIO」这行字。 */
[data-tb-rail="true"] :deep(.actions .submit) {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  height: 32px;
  padding: 0 var(--sp-2);
  font-size: var(--fs-sm);
  line-height: 1;
  border: 1px solid transparent;
  border-radius: var(--qio-ui-radius);
}
[data-tb-rail="true"] :deep(.actions .submit:disabled) {
  opacity: 1;
  color: var(--text-faint);
  background: var(--bg-inset);
  border-color: var(--border-subtle);
}

/*
 * SubmitCluster 的说明区（.sc-notes：失败 / 未知 / 被挡 / 待审批 / 理解状态，含真实入口）
 * 不能整块藏掉，也不能参与单行流（长正文会把操作行撑高）。按既有的「面板在栏上方绝对定位」
 * 模式做成浮起状态卡：右对齐、限高内部滚动、1–2 行时是轻量提示，不抢操作行点击。
 * 层级 30：低于「更多」面板（40），打开更多时状态卡不会盖住菜单。
 */
[data-tb-rail="true"] :deep(.sc-notes) {
  position: absolute;
  right: 0;
  bottom: calc(100% + 6px);
  z-index: 30;
  width: min(420px, calc(100vw - 32px));
  max-height: 40vh;
  overflow: auto;
  padding: var(--sp-2) var(--sp-3);
  border: 1px solid var(--border-subtle);
  border-radius: var(--qio-ui-radius);
  background: var(--bg-elevated);
  box-shadow: var(--shadow-1);
  font-size: var(--fs-xs);
}

/* 「更多」：按钮 + 行右上方面板（面板层级高于提交状态卡） */
[data-tb-rail="true"] .tb-more {
  position: relative;
  display: inline-flex;
  flex: none;
}
.tb-more-panel {
  position: absolute;
  right: 0;
  bottom: calc(100% + 6px);
  z-index: 40;
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 168px;
  padding: var(--sp-1);
  background: var(--bg-elevated);
  border: 1px solid var(--border-strong);
  border-radius: var(--qio-ui-radius);
  box-shadow: var(--shadow-2);
  font-family: var(--sans);
}
.tb-more-item {
  display: flex;
  align-items: center;
  width: 100%;
  text-align: left;
  font: inherit;
  font-size: var(--fs-sm);
  line-height: 1.6;
  color: var(--text-secondary);
  background: transparent;
  border: 1px solid transparent;
  border-radius: var(--r-sm);
  padding: var(--sp-1) var(--sp-2);
  cursor: pointer;
  white-space: nowrap;
}
.tb-more-item:hover:not(:disabled) {
  color: var(--text-strong);
  background: var(--bg-inset);
  border-color: var(--border-subtle);
}
.tb-more-item:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 1px; }
/* 禁用态（没有可撤销 / 重做）：换底色 + 弱化描边，仍能看清文字 */
.tb-more-item:disabled {
  color: var(--text-faint);
  background: var(--bg-inset);
  border-color: var(--border-subtle);
  cursor: default;
}
.tb-more-item.on { color: var(--accent); border-color: var(--accent); }
</style>
