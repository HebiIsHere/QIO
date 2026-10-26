<script setup lang="ts">
/**
 * 顶部短通知（B）：同一时间只显示一条，窗口居中、全局栏下方。
 *
 * 视觉与行为（用户任务书）：
 * - 对称布局：左右槽位等宽（左侧状态点、右侧关闭按钮），标题/正文居中；
 * - 圆角走统一 token（--qio-ui-radius，14px）；轻玻璃感、深浅色都由 token 提供；
 * - 长任务名在可见区域单行省略，**读屏与完整文本**仍可用（aria-label 不截断，不用原生 title）；
 * - 悬停 / 键盘聚焦暂停倒计时，移开恢复剩余；关闭按钮只关这条通知；
 * - 不抢焦点：出现时不 focus 任何元素，只有用户 Tab 才进入；
 * - 点击（只有可定位任务的通知）→ 展开左栏并定位任务集合。
 */
import { computed } from "vue";
import {
  noticeActivation,
  noticeDisplayText,
  noticeFullText,
  noticeIsActivatable,
  noticeTaskIds,
  useNotificationsStore,
} from "../../stores/notifications";
import { useConversationShellStore } from "../../stores/conversationShell";
import { useConversationTasksStore } from "../../stores/conversationTasks";

const notifications = useNotificationsStore();
const notice = computed(() => notifications.active);
const text = computed(() => noticeDisplayText(notice.value));
const fullText = computed(() => noticeFullText(notice.value));
const taskIds = computed(() => noticeTaskIds(notice.value));
/**
 * 可激活才做成按钮：任务失败类点击去左栏定位；带激活动作的通知（例如接续引导）
 * 点击执行那个动作。两者都没有时保持纯文本（不做假按钮）。
 */
const clickable = computed(() => noticeIsActivatable(notice.value));
const canLocate = computed(
  () => Boolean(notice.value?.focus) && taskIds.value.length > 0,
);
const kindClass = computed(() => "is-" + (notice.value?.kind ?? "task_failed"));

function pause(): void {
  if (notice.value) notifications.pause(notice.value.key);
}
function resume(): void {
  if (notice.value) notifications.resume(notice.value.key);
}

/**
 * 点击通知：展开左栏（收起了就点不到任务）并定位本次任务集合。
 * 定位失败由 conversationTasks 自己的 focusError / 左栏提示表达，这里不伪造成功。
 */
/** 点击通知：能定位任务就先定位；否则执行调用方给的激活动作（例如接续引导）。 */
async function activate(): Promise<void> {
  const current = notifications.active;
  if (!current) return;
  if (canLocate.value) {
    await locate();
    return;
  }
  const handler = noticeActivation(current);
  const key = current.key;
  notifications.dismiss(key);
  if (!handler) return;
  try {
    handler();
  } catch {
    // 激活动作失败不阻断界面：具体失败由该功能自己的反馈表达
  }
}

async function locate(): Promise<void> {
  const current = notifications.active;
  if (!current) return;
  const ids = noticeTaskIds(current);
  if (!ids.length) return;
  try {
    const shell = useConversationShellStore();
    if (shell.effectiveLeftCollapsed) shell.setLeftWidth(shell.prefs.leftWidth);
  } catch {
    // 外壳 store 未初始化（极端早期 / 单测挂载）：不影响定位本身
  }
  const tasks = useConversationTasksStore();
  for (const taskId of ids.slice(0, 8)) {
    try {
      await tasks.focusTask(taskId);
    } catch {
      // 单条定位失败不打断批量：左栏会显示「没有找到这项任务」
    }
  }
  notifications.dismiss(current.key);
}

function close(): void {
  const current = notifications.active;
  if (current) notifications.dismiss(current.key);
}

function onKeydown(event: KeyboardEvent): void {
  if (event.key !== "Escape") return;
  event.preventDefault();
  event.stopPropagation();
  close();
}
</script>

<template>
  <Transition name="uf-notice" appear>
    <div
      v-if="notice"
      class="uf-notice"
      :class="kindClass"
      data-test="top-notice"
      @mouseenter="pause"
      @mouseleave="resume"
      @focusin="pause"
      @focusout="resume"
      @keydown="onKeydown"
    >
      <!-- 读屏/完整文案：可见文本会省略长任务名，这里永远完整 -->
      <span class="qio-sr-only" role="status" aria-live="polite" aria-atomic="true">
        {{ fullText }}
      </span>
      <!-- 左侧槽位：状态点（等宽，不是图标冒充交互） -->
      <span class="uf-notice__slot" aria-hidden="true">
        <span class="uf-notice__dot"></span>
      </span>
      <button
        v-if="clickable"
        class="uf-notice__body"
        type="button"
        :aria-label="canLocate ? fullText + '，点击在左侧任务栏定位' : fullText + '，点击继续'"
        @click="activate"
      >
        <span class="uf-notice__line">{{ text }}</span>
      </button>
      <p v-else class="uf-notice__body uf-notice__body--static">
        <span class="uf-notice__line">{{ text }}</span>
      </p>
      <!-- 右侧槽位：与左侧同宽，关闭按钮不会把文字挤偏 -->
      <span class="uf-notice__slot uf-notice__slot--right">
        <button
          class="uf-notice__close"
          type="button"
          aria-label="关闭这条通知（不会改变任务或设置状态）"
          @click.stop="close"
        >
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="M7 7l10 10M17 7L7 17" />
          </svg>
        </button>
      </span>
    </div>
  </Transition>
</template>

<style scoped>
/* 窗口居中、全局栏下方；宽度自适应内容但不超过窄窗。 */
.uf-notice {
  position: fixed;
  top: calc(var(--titlebar-height, 32px) + 12px);
  left: 50%;
  transform: translateX(-50%);
  z-index: 196;
  display: grid;
  grid-template-columns: 28px minmax(0, 1fr) 28px;
  align-items: center;
  gap: 6px;
  width: max-content;
  max-width: min(560px, calc(100vw - 24px));
  min-height: 36px;
  padding: 5px 8px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--qio-ui-radius, var(--r-lg));
  background: var(--glass-bg), var(--bg-elevated);
  backdrop-filter: blur(var(--glass-blur)) saturate(var(--glass-sat));
  -webkit-backdrop-filter: blur(var(--glass-blur)) saturate(var(--glass-sat));
  box-shadow: var(--shadow-2);
  color: var(--text-secondary);
  font-family: var(--sans);
  font-size: 12.5px;
  line-height: 1.5;
}
.uf-notice__slot {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 0;
}
.uf-notice__dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--text-muted);
}
.uf-notice.is-task_failed .uf-notice__dot {
  background: var(--warning);
}
.uf-notice.is-setup_incomplete .uf-notice__dot {
  background: var(--accent);
}
.uf-notice__body {
  margin: 0;
  padding: 0;
  min-width: 0;
  border: none;
  background: transparent;
  color: var(--text-strong);
  font: inherit;
  text-align: center;
}
button.uf-notice__body {
  cursor: pointer;
  border-radius: var(--r-sm);
}
button.uf-notice__body:hover .uf-notice__line {
  text-decoration: underline;
  text-underline-offset: 2px;
}
button.uf-notice__body:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 2px;
}
/* 可见文本单行省略；完整内容在读屏文案里（不用原生 title 凑） */
.uf-notice__line {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  overflow-wrap: anywhere;
}
.uf-notice__close {
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
  transition: background var(--dur-fast) var(--ease-1), color var(--dur-fast) var(--ease-1);
}
.uf-notice__close svg {
  width: 14px;
  height: 14px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.6;
  stroke-linecap: round;
}
.uf-notice__close:hover {
  background: var(--bg-inset);
  color: var(--text-strong);
}
.uf-notice__close:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 1px;
}

/* 平缓出现/离开；只动透明度与小位移，尊重减少动态效果 */
.uf-notice-enter-active,
.uf-notice-leave-active {
  transition: opacity var(--dur-menu) var(--ease-1-out), transform var(--dur-menu) var(--ease-1-out);
}
.uf-notice-enter-from,
.uf-notice-leave-to {
  opacity: 0;
  transform: translateX(-50%) translateY(calc(-1 * var(--shift-4, 4px)));
}
@media (prefers-reduced-motion: reduce) {
  .uf-notice-enter-active,
  .uf-notice-leave-active {
    transition: none;
  }
}
/* 窄窗：左右槽位仍等宽，文字自行换行（不因宽度不足挤偏居中轴） */
@media (max-width: 480px) {
  .uf-notice {
    width: calc(100vw - 24px);
  }
  .uf-notice__line {
    white-space: normal;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
  }
}
</style>