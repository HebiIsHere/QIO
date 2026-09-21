<script setup lang="ts">
/**
 * QConfirm —— QIO 自己的确认层（第四阶段）。
 *
 * 为什么需要它：界面里此前散落着浏览器原生 `window.confirm`（知识归档、实体归档、
 * 删除凭据、覆盖保存、清除博钥）。原生框有三个问题：
 * 1) 它的视觉与 QIO 完全无关，像是「另一个软件的弹窗」；
 * 2) 它只能给一句标题，说不清「动作 + 影响 + 后果」；
 * 3) 它无法按危险程度分档 —— 归档一条知识和高危删除凭据长得一模一样。
 *
 * 三档形态（同一套视觉语言，危险程度递增）：
 * - `inline`：就地展开在条目/表单里（中等风险，如归档一条记录）；
 * - `popover`：浮在触发点附近的小浮层（可逆但有影响）；
 * - `layer`：轻量浮动层 + 遮罩（真正高风险，如删除凭据、清除密钥）。
 *
 * 安全与无障碍约定：
 * - `Esc` 一律取消（与既有审批窗口的键盘约定一致）；
 * - `layer` 档：`role="dialog"` + `aria-modal` + 焦点陷阱 + 关闭后焦点归还触发元素；
 * - `tone="danger"` 时确认按钮用中性实心（`.qio-btn.danger-solid`），**不用品牌主操作色**
 *   —— 品牌色会让「危险动作」看起来像是被推荐的默认选择。
 */
import { nextTick, onBeforeUnmount, ref, watch } from "vue";

const props = withDefaults(
  defineProps<{
    open: boolean;
    title: string;
    detail?: string;
    confirmText?: string;
    cancelText?: string;
    tone?: "normal" | "danger";
    variant?: "inline" | "popover" | "layer";
    /** 标题元素 id：layer 档用 aria-labelledby 指过去；不传则用默认 id */
    titleId?: string;
  }>(),
  {
    detail: "",
    confirmText: "确定",
    cancelText: "取消",
    tone: "normal",
    variant: "inline",
    titleId: "qio-confirm-title",
  },
);

const emit = defineEmits<{ confirm: []; cancel: [] }>();

const cancelRef = ref<HTMLButtonElement | null>(null);
const rootRef = ref<HTMLElement | null>(null);
/** 打开前的焦点元素：关闭后必须还回去，否则键盘用户会掉到页面开头 */
let restoreFocusTo: HTMLElement | null = null;

function rememberFocus() {
  const active = document.activeElement;
  restoreFocusTo = active instanceof HTMLElement ? active : null;
}

function restoreFocus() {
  const el = restoreFocusTo;
  restoreFocusTo = null;
  // 触发元素可能已经因为操作完成而从 DOM 移除（例如被归档的条目）
  if (el && el.isConnected) {
    el.focus();
    return;
  }
  // 触发元素没了：把焦点还给同一张卡片上的相邻可聚焦元素，
  // 否则键盘用户会掉到页面开头，要重新 Tab 一圈才能回到刚才那条。
  const scope = rootRef.value?.parentElement ?? null;
  if (!scope) return;
  const focusable = scope.querySelector<HTMLElement>(
    'button:not([disabled]), [href], input:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
  );
  if (focusable && focusable.isConnected) focusable.focus();
  else {
    scope.setAttribute("tabindex", "-1");
    scope.focus();
  }
}

function cancel() {
  emit("cancel");
}

function confirm() {
  emit("confirm");
}

/**
 * `layer` 档的焦点陷阱：Tab 在层内循环，不允许跑到底下的页面。
 * 只处理 Tab —— Esc 由 keydown 统一取消。
 */
function onKeydown(e: KeyboardEvent) {
  if (!props.open) return;
  if (e.key === "Escape") {
    e.preventDefault();
    e.stopPropagation();
    cancel();
    return;
  }
  if (props.variant !== "layer" || e.key !== "Tab") return;
  const root = rootRef.value;
  if (!root) return;
  const focusable = [...root.querySelectorAll<HTMLElement>("button:not([disabled]), [href], input, textarea, select")];
  if (!focusable.length) return;
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  const active = document.activeElement as HTMLElement | null;
  if (e.shiftKey && (active === first || !root.contains(active))) {
    e.preventDefault();
    last.focus();
  } else if (!e.shiftKey && active === last) {
    e.preventDefault();
    first.focus();
  }
}

watch(
  () => props.open,
  async (open) => {
    if (open) {
      rememberFocus();
      // **所有档位都挂全局 Esc。**
      //
      // 之前只有 layer 档挂：inline / popover 档的 Esc 依赖元素内的冒泡，
      // 而触发按钮被移除后焦点就落到 body —— 此时按 Esc 毫无反应
      // （实测 4 个状态 × 2 套主题共 8 次复现完全一致）。
      window.addEventListener("keydown", onKeydown, true);
      // 焦点先给「取消」：危险动作不应该因为一次回车就被执行
      await nextTick();
      if (props.variant === "layer") cancelRef.value?.focus();
    } else {
      window.removeEventListener("keydown", onKeydown, true);
      restoreFocus();
    }
  },
  // immediate：挂载时就已经是 open 的情况（父组件用 v-if 直接渲染出确认层）
  // 也必须注册 Esc 与焦点归还，否则键盘用户会卡在这一层。
  { immediate: true },
);

onBeforeUnmount(() => {
  window.removeEventListener("keydown", onKeydown, true);
  if (props.open) restoreFocus();
});
</script>

<template>
  <Transition name="qio-fade">
    <div
      v-if="open && variant === 'layer'"
      class="qio-confirm-scrim"
    >
      <div
        ref="rootRef"
        class="qio-confirm qio-confirm--layer qio-floating"
        role="dialog"
        aria-modal="true"
        :aria-labelledby="titleId"
      >
        <p :id="titleId" class="qio-confirm__title">{{ title }}</p>
        <p v-if="detail" class="qio-confirm__detail">{{ detail }}</p>
        <div class="qio-confirm__actions">
          <button ref="cancelRef" class="qio-btn" type="button" @click="cancel">
            {{ cancelText }}
          </button>
          <button
            class="qio-btn"
            :class="tone === 'danger' ? 'danger-solid' : 'primary'"
            type="button"
            @click="confirm"
          >
            {{ confirmText }}
          </button>
        </div>
      </div>
    </div>
  </Transition>

  <div
    v-if="open && variant !== 'layer'"
    ref="rootRef"
    class="qio-confirm"
    :class="variant === 'popover' ? 'qio-confirm--popover qio-floating' : 'qio-confirm--inline'"
    role="group"
    :aria-labelledby="titleId"
  >
    <p :id="titleId" class="qio-confirm__title">{{ title }}</p>
    <p v-if="detail" class="qio-confirm__detail">{{ detail }}</p>
    <div class="qio-confirm__actions">
      <button ref="cancelRef" class="qio-btn mini" type="button" @click="cancel">
        {{ cancelText }}
      </button>
      <button
        class="qio-btn mini"
        :class="tone === 'danger' ? 'danger-solid' : 'primary'"
        type="button"
        @click="confirm"
      >
        {{ confirmText }}
      </button>
    </div>
  </div>
</template>

<style scoped>
.qio-confirm--layer {
  max-width: min(420px, 100%);
}
.qio-confirm__title {
  margin: 0;
}
.qio-confirm__actions {
  margin-top: 2px;
}
</style>
