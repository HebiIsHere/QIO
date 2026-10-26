<script setup lang="ts">
/**
 * QModal —— 全站共享的中央弹窗外壳（2026-10-26 统一改造）。
 *
 * 解决的是一类“每个弹窗各长一副样子”的问题：标题栏不对称（关闭按钮把标题挤偏）、
 * 正文左对齐、按钮一个宽一个窄、Esc/遮罩点击/焦点陷阱各写各的、关闭后焦点掉回页面开头。
 *
 * 结构契约（B/C/D 接同一套）：
 * - Teleport 到 body，遮罩轻变暗 + 轻模糊（仍能辨认原页面）；
 * - 唯一 role="dialog" + aria-modal + aria-labelledby 指向标题；
 * - 标题栏三列等宽：左槽留空、中间标题严格居中、右槽关闭图标按钮；
 * - Esc / 外部点击 / 关闭按钮只在 dismissible 且非 busy 时触发 close；
 * - 焦点陷阱、打开时记录焦点、关闭后归还；不主动抢输入框焦点
 *   （弹窗可能在用户打字时被打开，抢焦点会打断输入/输入法）；
 * - 重复的关闭请求只发一次 close，避免父级重复提交；
 * - 14px 圆角与 .qio-modal-actions 按钮组（等宽/同高/窄窗纵排）来自 base.css。
 *
 * 仲裁：中央弹窗同一时刻只能有一个。B 的 stores/modalArbiter.ts 就绪后，
 * 在本组件 open 时调用 open({ id: modalId, kind: "other", dismissible }) 并把
 * 返回值作为是否渲染的第二条件；close/unmount 时 close(modalId)。
 * 目前先留 TODO（A 分支上不重复实现仲裁 store），Lead 合并 B 后接上。
 */
import { computed, nextTick, onBeforeUnmount, ref, watch } from "vue";
import QIconButton from "./QIconButton.vue";

const props = withDefaults(
  defineProps<{
    open: boolean;
    title: string;
    /** 标题上方的短说明（例如“危险操作”“来自审批”），与标题同轴 */
    kicker?: string;
    /** 模态身份：aria-labelledby 的 id 前缀，也是将来向仲裁器注册的 key */
    modalId: string;
    /** 是否允许收起（审批/影响确认等不可取消场景传 false） */
    dismissible?: boolean;
    tone?: "normal" | "danger";
    /** 面板宽度（CSS 长度），默认 480px */
    width?: string;
    /** 提交中：挡住关闭动作，防止重复提交/关闭中的状态错乱 */
    busy?: boolean;
  }>(),
  {
    kicker: "",
    dismissible: true,
    tone: "normal",
    width: "480px",
    busy: false,
  },
);

const emit = defineEmits<{ close: [] }>();

const panelRef = ref<HTMLElement | null>(null);
const titleId = computed(() => `${props.modalId}-title`);
const canDismiss = computed(() => props.dismissible && !props.busy);

let restoreFocusTo: HTMLElement | null = null;
let movedFocus = false;
let closeEmitted = false;

const panelStyle = computed<Record<string, string>>(() => ({ "--qio-modal-width": props.width }));

function requestClose(): void {
  if (!props.open || !canDismiss.value || closeEmitted) return;
  // 同一次打开只发一次：Esc + 遮罩点击 + 关闭按钮不会让父级收三次
  closeEmitted = true;
  emit("close");
}

function onScrimClick(): void {
  requestClose();
}

function focusablesInPanel(): HTMLElement[] {
  const root = panelRef.value;
  if (!root) return [];
  return [...root.querySelectorAll<HTMLElement>(
    'button:not([disabled]), [href], input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])',
  )].filter((el) => el.getAttribute("aria-hidden") !== "true");
}

/** 不抢输入焦点：用户正在输入时保持原焦点；否则把焦点给对话框容器（不是输入框）。 */
function focusInitial(): void {
  const active = document.activeElement as HTMLElement | null;
  const typingInPage =
    !!active &&
    !panelRef.value?.contains(active) &&
    (active.matches("input, textarea, select") || active.isContentEditable);
  if (typingInPage) return;
  const panel = panelRef.value;
  if (!panel) return;
  if (!panel.contains(document.activeElement)) {
    movedFocus = true;
    panel.focus({ preventScroll: true });
  }
}

function restoreFocus(): void {
  const el = restoreFocusTo;
  restoreFocusTo = null;
  if (!movedFocus) return;
  movedFocus = false;
  if (el && el.isConnected) {
    el.focus();
    return;
  }
  // 触发元素已随操作消失：把焦点还给页面里仍在的第一处可聚焦元素，别让键盘用户从头 Tab
  const fallback = document.querySelector<HTMLElement>(
    'body > button:not([disabled]), body > [href], main button:not([disabled])',
  );
  fallback?.focus();
}

function onKeydown(event: KeyboardEvent): void {
  if (!props.open) return;
  if (event.key === "Escape") {
    if (canDismiss.value) {
      event.preventDefault();
      requestClose();
    }
    return;
  }
  if (event.key !== "Tab") return;
  const root = panelRef.value;
  if (!root) return;
  const focusable = focusablesInPanel();
  if (!focusable.length) {
    event.preventDefault();
    root.focus({ preventScroll: true });
    return;
  }
  const active = document.activeElement as HTMLElement | null;
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (!active || !root.contains(active)) {
    event.preventDefault();
    (event.shiftKey ? last : first).focus();
    return;
  }
  if (event.shiftKey && active === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && active === last) {
    event.preventDefault();
    first.focus();
  }
}

watch(
  () => props.open,
  async (open) => {
    if (open) {
      closeEmitted = false;
      restoreFocusTo = document.activeElement instanceof HTMLElement ? document.activeElement : null;
      window.addEventListener("keydown", onKeydown, true);
      await nextTick();
      focusInitial();
    } else {
      window.removeEventListener("keydown", onKeydown, true);
      restoreFocus();
    }
  },
  { immediate: true },
);

onBeforeUnmount(() => {
  window.removeEventListener("keydown", onKeydown, true);
  if (props.open) restoreFocus();
});
</script>

<template>
  <Teleport to="body">
    <Transition name="qio-fade">
      <div v-if="open" class="qio-modal-scrim" @click.self="onScrimClick">
        <div
          ref="panelRef"
          class="qio-modal qio-floating qio-pop-in"
          :class="`qio-modal--${tone}`"
          role="dialog"
          aria-modal="true"
          :aria-labelledby="titleId"
          :aria-busy="busy || undefined"
          :data-modal-id="modalId"
          :style="panelStyle"
          tabindex="-1"
        >
          <div class="qio-modal-head">
            <!-- 左槽只占位：防标题被右侧关闭按钮挤偏；不放可聚焦控件 -->
            <span class="qio-modal-head__slot" aria-hidden="true" />
            <div class="qio-modal-heading">
              <p :id="titleId" class="qio-modal-title">{{ title }}</p>
              <p v-if="kicker" class="qio-modal-kicker">{{ kicker }}</p>
              <slot name="headerExtra" />
            </div>
            <span class="qio-modal-head__slot qio-modal-head__slot--end">
              <QIconButton
                v-if="dismissible"
                icon="close"
                label="关闭"
                description="关闭这个弹窗，返回原页面"
                size="sm"
                tone="quiet"
                :busy="busy"
                @click="requestClose"
              />
            </span>
          </div>
          <div class="qio-modal-body">
            <slot />
          </div>
          <div v-if="$slots.footer" class="qio-modal-actions">
            <slot name="footer" />
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.qio-modal {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  width: min(var(--qio-modal-width, 480px), 100%);
  max-height: calc(100vh - 2 * var(--sp-6));
  padding: var(--sp-5) var(--sp-5) var(--sp-4);
  border-radius: var(--qio-ui-radius);
  color: var(--text-primary);
  overflow-y: auto;
  overscroll-behavior: contain;
  outline: none;
}
.qio-modal-heading {
  min-width: 0;
  text-align: center;
}
/* 危险档：只在标题上给一个可辨认的信号，不把整个弹窗染红 */
.qio-modal--danger .qio-modal-title {
  color: var(--danger);
}
@media (max-width: 480px) {
  .qio-modal {
    padding: var(--sp-4);
    max-height: calc(100vh - 2 * var(--sp-4));
  }
}
</style>
