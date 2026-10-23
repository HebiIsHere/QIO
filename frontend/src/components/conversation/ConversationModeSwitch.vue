<script setup lang="ts">
/**
 * 底部输入区旁的模式切换入口：对话模式 ⇄ 互动模式（互动页 v2 契约 §2.2）。
 *
 * 规则：
 * - 同一组件显示**当前路由模式**：按钮文字与 aria-label 都跟着当前模式走，不写死对话模式；
 * - 两个菜单项都是可导航的真实按钮（对话页能去互动页，互动页也能回对话页）；
 *   当前项带 aria-current 与「当前」标记，非当前项写清它是什么；
 * - 切换的是**整个页面**（router push），不在顶部再放一个重复入口；
 * - 切换只导航：不取消任务、不清空草稿、不丢审批、不丢附件（这些状态都在各自的 store /
 *   本机存储里），也不重复提交；点当前项不重复导航（同一地址不产生第二条历史）；
 * - 键盘可操作（Enter / Esc）。
 */
import { computed, onBeforeUnmount, ref } from "vue";
import { useRouter } from "vue-router";

/** 两种页面模式：菜单顺序、说明文字与目标地址只在这里写一次。 */
interface PageMode {
  id: "conversation" | "interactive";
  label: string;
  /** 切到这一页能做什么（当前项不显示这句，改为显示「当前」） */
  note: string;
  path: string;
}

const MODES: readonly PageMode[] = [
  { id: "conversation", label: "对话模式", note: "边聊边整理材料", path: "/" },
  { id: "interactive", label: "互动模式", note: "一起整理材料与关系", path: "/interactive" },
];

const router = useRouter();
const open = ref(false);

/** 当前路由对应的模式；读不到路由（组件单测）时按对话模式显示。 */
const current = computed<PageMode>(() => {
  const name = router?.currentRoute?.value?.name;
  return MODES.find((mode) => mode.id === name) ?? MODES[0];
});

const modeButtonLabel = computed(() => `切换页面模式（当前为${current.value.label}）`);

const menuItems = computed(() =>
  MODES.map((mode) => ({ ...mode, isCurrent: mode.id === current.value.id })),
);

function toggle() {
  open.value = !open.value;
}

/** 只切页面：任务、草稿、审批、附件状态都不由这里清空或提交。 */
function go(mode: PageMode) {
  open.value = false;
  if (!router) return;
  // 已经在这一页：不再 push，避免同一地址堆出两条历史记录
  if (mode.id === current.value.id) return;
  void router.push(mode.path);
}

function onKeydown(event: KeyboardEvent) {
  if (event.key !== "Escape" || !open.value) return;
  event.stopPropagation();
  open.value = false;
}

/** 点空白处收起菜单（不改任何业务状态） */
function onPointerDown(event: PointerEvent) {
  if (!open.value) return;
  const target = event.target as Node | null;
  const root = (event.currentTarget as HTMLElement | null) ?? null;
  if (root && target && root.contains(target)) return;
  open.value = false;
}

let stopOutside: (() => void) | null = null;

function bindOutside() {
  if (stopOutside) return;
  const handler = (event: PointerEvent) => {
    if (!open.value) return;
    const el = document.getElementById("composer-mode-switch");
    if (el && event.target instanceof Node && el.contains(event.target)) return;
    open.value = false;
  };
  document.addEventListener("pointerdown", handler, true);
  stopOutside = () => document.removeEventListener("pointerdown", handler, true);
}

onBeforeUnmount(() => {
  stopOutside?.();
  stopOutside = null;
});

// 首次打开菜单时挂监听（默认不占监听器）
function ensureOutside() {
  if (open.value) bindOutside();
}
</script>

<template>
  <div
    id="composer-mode-switch"
    class="mode-switch"
    @keydown="onKeydown"
    @pointerdown="onPointerDown"
    @click="ensureOutside"
  >
    <button
      class="mode-btn"
      type="button"
      aria-haspopup="menu"
      :aria-expanded="open ? 'true' : 'false'"
      :aria-label="modeButtonLabel"
      title="切换页面模式：对话模式 / 互动模式"
      @click="toggle"
    >
      <span class="mode-dot" aria-hidden="true"></span>
      <span class="mode-text">{{ current.label }}</span>
      <span class="chev" :class="{ open }" aria-hidden="true">
        <svg viewBox="0 0 24 24">
          <path d="M6 9l6 6 6-6" />
        </svg>
      </span>
    </button>
    <ul v-if="open" class="menu" role="menu" aria-label="页面模式">
      <li v-for="item in menuItems" :key="item.id">
        <button
          class="menu-item as-btn"
          type="button"
          role="menuitem"
          :aria-current="item.isCurrent ? 'true' : undefined"
          :title="item.isCurrent ? '当前页面' : `切换到${item.label}`"
          @click="go(item)"
        >
          <span class="item-name">{{ item.label }}</span>
          <span class="item-note" :class="{ mono: item.isCurrent }">{{ item.isCurrent ? "当前" : item.note }}</span>
        </button>
      </li>
    </ul>
  </div>
</template>

<style scoped>
.mode-switch {
  position: relative;
  flex: none;
  min-width: 0;
}
.mode-btn {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  height: 38px;
  padding: 0 9px;
  border: 1px solid transparent;
  border-radius: var(--r-pill);
  background: transparent;
  color: var(--text-secondary);
  font-family: var(--sans);
  font-size: 12px;
  cursor: pointer;
  transition: color var(--dur-fast) var(--ease-1), border-color var(--dur-fast) var(--ease-1),
    background var(--dur-fast) var(--ease-1);
}
.mode-btn:hover {
  color: var(--text-strong);
  border-color: var(--border-subtle);
  background: var(--layer-hover);
}
.mode-btn:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 1px;
}
.mode-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--accent);
}
.mode-text {
  white-space: nowrap;
}
.chev {
  flex: none;
  width: 24px;
  height: 24px;
  border-radius: 50%;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: var(--text-muted);
  transition: color var(--dur-fast) var(--ease-1), background-color var(--dur-fast) var(--ease-1);
}
.chev svg {
  width: 18px;
  height: 18px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.7;
  stroke-linecap: round;
  stroke-linejoin: round;
  transform-origin: 50% 50%;
  transition: transform var(--dur-fast) var(--ease-1);
}
.chev.open svg {
  transform: rotate(180deg);
}
.mode-btn:hover .chev {
  color: var(--text-strong);
  background: var(--layer-hover);
}
.menu {
  position: absolute;
  left: 0;
  bottom: calc(100% + 6px);
  z-index: 20;
  min-width: 196px;
  max-width: min(280px, 70vw);
  margin: 0;
  padding: 4px;
  list-style: none;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  background: var(--bg-elevated);
  box-shadow: var(--shadow-2);
}
.menu-item {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 10px;
  width: 100%;
  padding: 7px 9px;
  border: none;
  border-radius: var(--r-sm);
  background: transparent;
  color: var(--text-secondary);
  font-family: var(--sans);
  font-size: var(--fs-sm);
  text-align: left;
}
.menu-item[aria-current="true"] {
  color: var(--text-strong);
}
.as-btn {
  cursor: pointer;
}
.as-btn:hover {
  background: var(--layer-hover);
  color: var(--text-strong);
}
.as-btn:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: -2px;
}
.item-note {
  flex: none;
  color: var(--text-muted);
  font-size: 10.5px;
}
</style>
