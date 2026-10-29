<script setup lang="ts">
/**
 * 悬浮球（第四阶段）：**全屏 Planet 的压缩态**，不是图标按钮。
 *
 * 它用 `PlanetOrb` 以 canvas 画同一套融合环距离场（`planet/ringField.ts`），
 * 因此与全屏 Planet 共享几何、数学、颜色令牌与生命感。点击不是「打开一个页面」，
 * 而是让这个对象展开：阶段推进由 `planetContinuum` 与 PlanetView 共享，
 * 所以「小球长大成 Planet」在视觉上真的是同一件东西在变尺度。
 *
 * 浮动窗口行为（拖动、贴边、贴边后半隐藏、拖动不误触发展开）保持不变。
 */
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { useSessionStore } from "../stores/session";
import { vQioTip } from "../composables/useQioTip";
import { useFloatingWindow } from "../composables/useFloatingWindow";
import { FLOAT_AVOID_SELECTORS } from "../composables/floatingState";
import { planetContinuum } from "../composables/planetContinuum";
import { prefersReducedMotion } from "../utils/motion";
import PlanetOrb from "./planet/PlanetOrb.vue";

const props = defineProps<{ anchored?: boolean }>();
const session = useSessionStore();
const emit = defineEmits<{ open: [] }>();
const elRef = ref<HTMLElement | null>(null);
/**
 * 对话页 v3：星球固定在输入区右下角的槽位里（锚定模式）。
 * 锚定时不启用浮动窗口：不拖动、不贴边、不参与避让；样式与点击展开行为不变。
 */
const anchored = computed(() => props.anchored === true);

/** 收到回来的星球时的一次轻微「接收」反馈（不做回弹、不做庆祝） */
const receiving = ref(false);
let receiveTimer = 0;

/**
 * 转场期间小球的状态：
 * - activating：被激活（细节增强、轻微放大），用户意识到「这个对象即将展开」；
 * - expanding / ready / collapsing / returning：已经交棒给全屏 Planet，小球让位（不遮挡）。
 * 让位是**瞬时的**而不是淡出：交棒瞬间全屏 Planet 就压在入口的同一位置上，
 * 这正是「同一个对象」——任何淡出都会立刻变成「两个东西在交叉」。
 */
const handedOff = computed(() =>
  ["expanding", "ready", "collapsing", "returning"].includes(planetContinuum.phase),
);
const activating = computed(() => planetContinuum.phase === "activating");
/**
 * 真实星球渲染是否已经在位。
 *
 * 在位之后，入口的视觉由星球层（同一个 WebGL 场景缩到入口尺度）承担，
 * 这里只保留按钮本身：点击、拖动、贴边、隐藏、无障碍标签都还归它管。
 * 在 three.js 就绪之前（或 WebGL 不可用时）由 `PlanetOrb` 顶着首屏。
 */
const ballLive = computed(() => planetContinuum.ballLive);

watch(
  () => planetContinuum.phase,
  (phase, prev) => {
    if (prev === "returning" && phase === "idle") {
      receiving.value = true;
      window.clearTimeout(receiveTimer);
      receiveTimer = window.setTimeout(() => {
        receiving.value = false;
      }, prefersReducedMotion() ? 120 : 460);
    }
  },
);

onBeforeUnmount(() => {
  window.clearTimeout(receiveTimer);
});

const float = anchored.value
  ? { moved: ref(false) }
  : useFloatingWindow(elRef, {
      id: "planet-dock",
      dockMode: "edge",
      // 输入气泡与设置齿轮都不是「已贴靠的浮动组件」，但压住球一样致命：
      // 球会既看不见也点不到（用户看到的是「星球没办法进行移动」）。
      avoidSelectors: FLOAT_AVOID_SELECTORS,
      // 非锚定模式（其他页面）保持原浮动行为：默认右下角、可拖动。
      defaultPos: (el, vp) => ({
        x: vp.width - 26 - (el.offsetWidth || 96),
        y: vp.height - 24 - (el.offsetHeight || 96),
      }),
    });

function onClick() {
  if (!anchored.value && float.moved.value) return; // 拖动不误触发
  if (handedOff.value) return; // 转场中不重复触发
  emit("open");
}
</script>

<template>
  <button
    ref="elRef"
    class="dock"
    :class="{ activating, handed: handedOff, receiving, anchored }"
    data-planet-entry
    :aria-expanded="handedOff"
    aria-label="打开话题星球（星球入口；可拖动贴边）"
    v-qio-tip="'打开话题星球：查看话题、知识与记忆待确认（可拖动贴边）'"
    @click="onClick"
  >
    <!-- 真实渲染未就位时才显示 2D 压缩态（首屏兜底 / WebGL 不可用） -->
    <PlanetOrb
      v-if="!ballLive"
      :size="anchored ? 54 : 84"
      :detail="activating ? 1 : 0.72"
      :activity="session.turnRunning"
    />
  </button>
</template>

<style scoped>
/* 浮动窗口（Task B）：定位由 useFloatingWindow 用 left/top 像素控制，整元素可拖 */
.dock {
  position: fixed;
  z-index: 11;
  width: 96px;
  height: 96px;
  padding: 0;
  background: transparent;
  border: none;
  cursor: grab;
  user-select: none;
  -webkit-user-select: none;
  -webkit-user-drag: none;
  touch-action: none;
  display: flex;
  align-items: center;
  justify-content: center;
  /* 高频层：hover/按下用短令牌；贴边隐藏的淡入用中频档 */
  transition: transform var(--dur-fast) var(--ease-1), opacity var(--dur-planet-settle) var(--ease-1);
}
/* 对话页 v3：锚定在输入区右下槽位（不固定到视口、不参与拖动/贴边） */
.dock.anchored {
  position: static;
  z-index: auto;
  left: auto;
  top: auto;
  flex: none;
  cursor: pointer;
}
.dock:active {
  cursor: grabbing;
}
.dock:hover {
  transform: scale(1.04);
}
/* 按下立即反馈：不等页面加载完成，用户马上知道点到了 */
.dock:active {
  transform: scale(0.96);
  transition-duration: var(--dur-press);
}
/* 阶段 A：入口被激活（细节增强 + 轻微放大），只是「意识到即将展开」，不是动画表演 */
.dock.activating {
  transform: scale(1.06);
  transition: transform var(--mo-3-prepare) var(--ease-3-in);
}
/* 贴靠隐藏：淡化但保留轮廓与环，用户始终知道星球在哪；hover 展开、移出再隐藏 */
.dock.fw-hidden {
  opacity: 0.35;
}
.dock.fw-hidden:hover {
  opacity: 1;
}
/* 交棒之后：全屏 Planet 已经压在同一位置上，小球不再参与显示与命中。
   放在最后并显式覆盖 hover / 贴边隐藏，否则「贴边半隐藏 + hover」会把
   已经交棒的小球重新点亮（那会在 Planet 上面多出一个球）。 */
.dock.handed,
.dock.handed:hover,
.dock.handed.fw-hidden,
.dock.handed.fw-hidden:hover {
  opacity: 0;
  pointer-events: none;
}
/* 收到回来的星球：一次很短、幅度极小的「归位」，不是回弹庆祝 */
.dock.receiving {
  transform: scale(1.03);
  transition: transform var(--mo-3-settle) var(--ease-3-settle);
}
</style>
