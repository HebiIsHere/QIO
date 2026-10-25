<script setup lang="ts">
/**
 * 底部输入区旁的模式切换入口（B）：对话模式 ⇄ 互动模式。
 *
 * 规则（契约 §6 / 任务书）：
 * - 切换的是**整个页面**（router push /interactive），不在顶部再放一个重复入口；
 * - 切换页面不取消任务、不清空草稿、不丢审批、不丢附件（这些状态都在各自的 store /
 *   本机存储里，本组件只负责导航，不碰任何业务状态），也不重复提交；
 * - 菜单里明确标出当前模式；键盘可操作（Enter / Esc）。
 */
import { computed, onBeforeUnmount, ref } from "vue";
import { useRouter } from "vue-router";

const router = useRouter();
const open = ref(false);

const current = computed(() => {
  const name = router?.currentRoute?.value?.name;
  return name === "interactive" ? "interactive" : "conversation";
});

function toggle() {
  open.value = !open.value;
}

function goInteractive() {
  open.value = false;
  if (!router) return;
  if (router.currentRoute.value.name === "interactive") return;
  // 只切页面：任务、草稿、审批、附件状态都不由这里清空或提交
  void router.push("/interactive");
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
      aria-label="切换页面模式（当前为对话模式）"
      title="切换页面模式：对话模式 / 互动模式"
      @click="toggle"
    >
      <span class="mode-dot" aria-hidden="true"></span>
      <span class="mode-text">{{ current === "interactive" ? "互动模式" : "对话模式" }}</span>
      <span class="chev" :class="{ open }" aria-hidden="true">
        <svg viewBox="0 0 24 24">
          <path d="M6 9l6 6 6-6" />
        </svg>
      </span>
    </button>
    <ul v-if="open" class="menu" role="menu" aria-label="页面模式">
      <li class="menu-item" role="menuitem" aria-current="true">
        <span class="item-name">对话模式</span>
        <span class="item-note mono">当前</span>
      </li>
      <li>
        <button class="menu-item as-btn" type="button" role="menuitem" @click="goInteractive">
          <span class="item-name">互动模式</span>
          <span class="item-note">一起整理材料与关系</span>
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
