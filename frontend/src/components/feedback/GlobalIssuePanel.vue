<script setup lang="ts">
/**
 * 全局问题面板（B）：顶部全局栏入口展开后的逐项清单。
 *
 * - 逐项显示：问题 / 简短原因 / 处理动作 / 可展开诊断（默认折叠）；
 * - 同一根因已去重（store 负责），这里不重复计数、不重复渲染；
 * - 关闭询问 / 业务弹窗打开时自动收起（不靠 z-index 抢层）；
 * - 外部点击 / Esc 关闭，Esc 之后焦点回到入口；
 * - 标题栏左右槽位等宽（关闭按钮不挤偏标题），按钮组居中、同高、窄窗纵排。
 */
import { computed, nextTick, onBeforeUnmount, ref, watch } from "vue";
import type { GlobalIssue } from "../../stores/globalIssues";
import { useGlobalIssuesStore } from "../../stores/globalIssues";
import { useModalArbiterStore } from "../../stores/modalArbiter";
import { runGlobalIssueAction } from "./issueActions";

const issues = useGlobalIssuesStore();
const arbiter = useModalArbiterStore();
const panelRef = ref<HTMLElement | null>(null);
const expandedIds = ref<string[]>([]);

const open = computed(() => issues.panelOpen && issues.hasIssues);

function isExpanded(id: string): boolean {
  return expandedIds.value.includes(id);
}
function toggleDiagnostics(id: string): void {
  expandedIds.value = isExpanded(id)
    ? expandedIds.value.filter((entry) => entry !== id)
    : [...expandedIds.value, id];
}
function close(): void {
  issues.closePanel();
}
/** 关闭并把焦点还给入口（入口在 WindowChrome 里，按 data 属性找）。 */
function closeAndReturnFocus(): void {
  close();
  void nextTick().then(() => {
    const entry = document.querySelector<HTMLElement>("[data-uf-issue-entry]");
    entry?.focus();
  });
}

async function runAction(issue: GlobalIssue): Promise<void> {
  const action = issue.actions[0];
  if (!action) return;
  await runGlobalIssueAction(action);
}

function onPointerDown(event: Event): void {
  const target = event.target as Element | null;
  if (!target) return;
  if (target.closest("[data-uf-issue-panel], [data-uf-issue-entry]")) return;
  close();
}
function onKeydown(event: KeyboardEvent): void {
  if (event.key !== "Escape") return;
  event.preventDefault();
  event.stopPropagation();
  closeAndReturnFocus();
}

let listening = false;
function startListening(): void {
  if (listening || typeof document === "undefined") return;
  listening = true;
  document.addEventListener("pointerdown", onPointerDown, true);
  document.addEventListener("keydown", onKeydown, true);
}
function stopListening(): void {
  if (!listening || typeof document === "undefined") return;
  listening = false;
  document.removeEventListener("pointerdown", onPointerDown, true);
  document.removeEventListener("keydown", onKeydown, true);
}

watch(
  open,
  async (value) => {
    if (value) {
      startListening();
      await nextTick();
      // 用户主动打开：把焦点放进面板（这是用户动作，不是「抢焦点」）；入口仍可点关闭
      panelRef.value?.focus();
      return;
    }
    stopListening();
    expandedIds.value = [];
  },
  { immediate: true },
);

// 有中央弹窗时收起面板：不与弹窗竞争同一块视觉层（不用 z-index 抢层）
watch(
  () => arbiter.hasModal,
  (hasModal) => {
    if (hasModal && issues.panelOpen) close();
  },
);

onBeforeUnmount(() => {
  stopListening();
});
</script>

<template>
  <Transition name="uf-issue-panel">
    <section
      v-if="open"
      ref="panelRef"
      class="uf-issue-panel"
      data-uf-issue-panel
      role="dialog"
      aria-modal="false"
      aria-labelledby="uf-issue-title"
      tabindex="-1"
    >
      <header class="uf-issue-panel__head">
        <span class="uf-issue-panel__slot" aria-hidden="true"></span>
        <h2 id="uf-issue-title" class="uf-issue-panel__title">全局问题 {{ issues.count }} 项</h2>
        <span class="uf-issue-panel__slot">
          <button
            class="uf-issue-panel__close"
            type="button"
            aria-label="收起问题列表"
            @click="closeAndReturnFocus"
          >
            <svg viewBox="0 0 24 24" aria-hidden="true">
              <path d="M7 7l10 10M17 7L7 17" />
            </svg>
          </button>
        </span>
      </header>
      <p class="uf-issue-panel__lead">
        这里只有一个入口：每项都是独立问题，恢复一项就少一项；全部恢复后入口自动消失。
      </p>
      <div class="uf-issue-panel__body">
        <article
          v-for="issue in issues.issues"
          :key="issue.id"
          class="uf-issue"
          :class="{ warning: issue.severity === 'warning' }"
          :data-test="'global-issue-' + issue.id"
        >
          <p class="uf-issue__title">{{ issue.title }}</p>
          <p v-if="issue.reason" class="uf-issue__reason">{{ issue.reason }}</p>
          <div class="uf-issue__actions">
            <button
              v-if="issue.actions.length"
              class="uf-issue__action primary"
              type="button"
              :data-test="'global-issue-action-' + issue.id"
              @click="runAction(issue)"
            >
              {{ issue.actions[0].label }}
            </button>
            <button
              v-if="issue.diagnostics.length"
              class="uf-issue__action quiet"
              type="button"
              :aria-expanded="isExpanded(issue.id) ? 'true' : 'false'"
              :data-test="'global-issue-diagnostics-' + issue.id"
              @click="toggleDiagnostics(issue.id)"
            >
              {{ isExpanded(issue.id) ? "收起诊断" : "查看诊断" }}
            </button>
          </div>
          <div v-if="isExpanded(issue.id)" class="uf-issue__diagnostics">
            <p v-for="(line, index) in issue.diagnostics" :key="index" class="uf-issue__diag-line mono">{{ line }}</p>
          </div>
        </article>
      </div>
    </section>
  </Transition>
</template>

<style scoped>
.uf-issue-panel {
  position: fixed;
  top: calc(var(--titlebar-height, 32px) + 8px);
  right: 12px;
  z-index: 198;
  display: flex;
  flex-direction: column;
  gap: 8px;
  width: min(440px, calc(100vw - 24px));
  max-height: min(70vh, 560px);
  padding: 12px 14px 14px;
  border: 1px solid var(--border-strong);
  border-radius: var(--qio-ui-radius, var(--r-lg));
  background: var(--glass-bg-strong), var(--bg-elevated);
  backdrop-filter: blur(var(--glass-blur)) saturate(var(--glass-sat));
  -webkit-backdrop-filter: blur(var(--glass-blur)) saturate(var(--glass-sat));
  box-shadow: var(--shadow-3);
  color: var(--text-primary);
  font-family: var(--sans);
  outline: none;
}
.uf-issue-panel:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 2px;
}
/* 标题三槽位：左右等宽，标题居中；关闭按钮不占标题宽度 */
.uf-issue-panel__head {
  display: grid;
  grid-template-columns: 28px minmax(0, 1fr) 28px;
  align-items: center;
  gap: 6px;
}
.uf-issue-panel__slot {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 0;
}
.uf-issue-panel__title {
  margin: 0;
  font-family: var(--serif);
  font-size: 16px;
  font-weight: 500;
  color: var(--text-strong);
  text-align: center;
  overflow-wrap: anywhere;
}
.uf-issue-panel__close {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  padding: 0;
  border: 1px solid transparent;
  border-radius: var(--qio-ui-radius, var(--r-lg));
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
}
.uf-issue-panel__close svg {
  width: 14px;
  height: 14px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.6;
  stroke-linecap: round;
}
.uf-issue-panel__close:hover {
  background: var(--bg-inset);
  color: var(--text-strong);
}
.uf-issue-panel__close:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 1px;
}
.uf-issue-panel__lead {
  margin: 0;
  color: var(--text-muted);
  font-size: 11.5px;
  line-height: 1.55;
  text-align: center;
  overflow-wrap: anywhere;
}
.uf-issue-panel__body {
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-height: min(52vh, 420px);
  overflow-y: auto;
  overscroll-behavior: contain;
  padding-right: 2px;
}
.uf-issue {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px 12px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--qio-ui-radius, var(--r-lg));
  background: var(--bg-elevated);
}
.uf-issue.warning {
  border-color: var(--warning);
}
.uf-issue__title {
  margin: 0;
  color: var(--text-strong);
  font-size: 12.5px;
  line-height: 1.5;
  text-align: center;
  overflow-wrap: anywhere;
}
.uf-issue__reason {
  margin: 0;
  color: var(--text-secondary);
  font-size: 12px;
  line-height: 1.6;
  text-align: center;
  overflow-wrap: anywhere;
}
/* 按钮组整体居中、同高、配对同宽；窄窗纵排 */
.uf-issue__actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: center;
  gap: 8px;
}
.uf-issue__action {
  min-width: 108px;
  height: 32px;
  padding: 0 14px;
  border-radius: var(--qio-ui-radius, var(--r-lg));
  border: 1px solid transparent;
  font-family: var(--sans);
  font-size: 12px;
  cursor: pointer;
  transition:
    background var(--dur-fast) var(--ease-1),
    border-color var(--dur-fast) var(--ease-1),
    transform var(--dur-press) var(--ease-1-out);
}
.uf-issue__action:active {
  transform: translateY(var(--shift-1, 1px));
}
.uf-issue__action:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 2px;
}
.uf-issue__action.primary {
  background: var(--accent);
  color: var(--on-accent);
}
.uf-issue__action.primary:hover {
  background: var(--accent-hover);
}
.uf-issue__action.quiet {
  border-color: var(--border-strong);
  background: transparent;
  color: var(--text-primary);
}
.uf-issue__action.quiet:hover {
  background: var(--bg-inset);
}
.uf-issue__diagnostics {
  display: flex;
  flex-direction: column;
  gap: 3px;
  padding: 8px 10px;
  border-radius: var(--qio-ui-radius, var(--r-lg));
  background: var(--bg-inset);
}
.uf-issue__diag-line {
  margin: 0;
  color: var(--text-muted);
  font-size: 11px;
  line-height: 1.55;
  overflow-wrap: anywhere;
  white-space: pre-wrap;
}
@media (max-width: 480px) {
  .uf-issue-panel {
    right: 8px;
    left: 8px;
    width: auto;
  }
  .uf-issue__actions {
    flex-direction: column;
    align-items: stretch;
  }
  .uf-issue__action {
    width: 100%;
  }
}
/* 平缓出现（尊重减少动态效果） */
.uf-issue-panel-enter-active,
.uf-issue-panel-leave-active {
  transition: opacity var(--dur-menu) var(--ease-1-out), transform var(--dur-menu) var(--ease-1-out);
}
.uf-issue-panel-enter-from,
.uf-issue-panel-leave-to {
  opacity: 0;
  transform: translateY(calc(-1 * var(--shift-4, 4px)));
}
@media (prefers-reduced-motion: reduce) {
  .uf-issue-panel-enter-active,
  .uf-issue-panel-leave-active {
    transition: none;
  }
}
</style>
