<script setup lang="ts">
import { vQioTip } from "../../composables/useQioTip";
/**
 * 底部输入区旁的模式切换入口：对话模式 ⇄ 互动模式（互动页 v2 契约 §2.2）。
 *
 * FU5：**点击即切换**，不再弹出列表。
 * - 显示当前页面的模式（按钮文字跟随当前路由）；aria-label / title 说清楚点它会去哪个模式；
 * - 点击直接 router.push 到另一模式（切换整个页面），不经过菜单；
 * - 只导航：不取消任务、不清空草稿、不丢审批/附件，也不重复提交；
 * - 键盘可操作（Enter / Space 是 button 原生行为）。
 */
import { computed } from "vue";
import { useRouter } from "vue-router";

interface PageMode {
  id: "conversation" | "interactive";
  label: string;
  path: string;
}

const MODES: readonly PageMode[] = [
  { id: "conversation", label: "对话模式", path: "/" },
  { id: "interactive", label: "互动模式", path: "/interactive" },
];

const router = useRouter();

/** 当前路由对应的模式；读不到路由（组件单测）时按对话模式显示。 */
const current = computed<PageMode>(() => {
  const name = router?.currentRoute?.value?.name;
  return MODES.find((mode) => mode.id === name) ?? MODES[0];
});

/** 点击后要去的模式（永远是与当前不同的那一个）。 */
const target = computed<PageMode>(() =>
  MODES.find((mode) => mode.id !== current.value.id) ?? MODES[0],
);

const actionLabel = computed(() => `切换到${target.value.label}`);

function switchMode() {
  if (!router) return;
  void router.push(target.value.path);
}
</script>

<template>
  <div id="composer-mode-switch" class="mode-switch">
    <button
      class="mode-btn"
      type="button"
      :aria-label="actionLabel"
      v-qio-tip="actionLabel"
      :data-current-mode="current.id"
      :data-target-mode="target.id"
      @click="switchMode"
    >
      <span class="mode-dot" aria-hidden="true"></span>
      <span class="mode-text">{{ current.label }}</span>
    </button>
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
</style>
