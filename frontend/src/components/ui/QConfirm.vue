<script setup lang="ts">
/**
 * QConfirm —— QIO 自己的确认层（第四阶段，2026-10-26 统一改造）。
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
 * - `layer`：轻量浮动层 + 轻遮罩（真正高风险，如删除凭据、清除密钥）。
 *
 * 对称布局（本轮视觉要求）：
 * - 标题栏三列等宽（左槽留空、中间标题、右槽关闭按钮），标题严格落在组件中轴；
 * - 正文居中、长文本断行；按钮组整体居中、同高、配对同宽，窄窗纵排；
 * - 圆角统一 var(--qio-ui-radius)；
 * - layer 档关闭按钮是 QIconButton（48h 用途说明由指令提供），不挤偏标题。
 *
 * 安全与无障碍约定（保持不变）：
 * - `Esc` 一律取消（与既有审批窗口的键盘约定一致）；
 * - `layer` 档：`role="dialog"` + `aria-modal` + 焦点陷阱 + 关闭后焦点归还触发元素；
 * - 焦点先落在「取消」：危险动作不应该因为一次回车就被执行；
 * - `tone="danger"` 时确认按钮用中性实心（`.qio-btn.danger-solid`），**不用品牌主操作色**
 *   —— 品牌色会让「危险动作」看起来像是被推荐的默认选择。
 *
 * 仲裁（待接）：layer 打开时应向 B 的 useModalArbiterStore 注册
 * open({ id, kind: "confirm", dismissible: true })、关闭 unregister；
 * A 分支不重复实现该 store，Lead 合并 B 后在此接入（同一组件只做一层中央弹窗）。
 */
import { computed, nextTick, onBeforeUnmount, ref, watch } from "vue";
import QIconButton from "./QIconButton.vue";

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
const detailId = computed(() => `${props.titleId}-detail`);
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
        class="qio-confirm qio-confirm--layer qio-floating qio-pop-in"
        role="dialog"
        aria-modal="true"
        :aria-labelledby="titleId"
        :aria-describedby="detail ? detailId : undefined"
      >
        <!-- 三列等宽：左槽只占位（空内容），标题严格居中，右槽关闭按钮 -->
        <div class="qio-modal-head qio-confirm__head">
          <span class="qio-modal-head__slot" aria-hidden="true" />
          <div class="qio-modal-heading">
            <p :id="titleId" class="qio-confirm__title qio-modal-title">{{ title }}</p>
          </div>
          <span class="qio-modal-head__slot qio-modal-head__slot--end">
            <QIconButton
              icon="close"
              label="取消并关闭"
              description="取消并关闭，不执行这个动作"
              size="sm"
              tone="quiet"
              data-qio-confirm-close
              @click="cancel"
            />
          </span>
        </div>
        <p v-if="detail" :id="detailId" class="qio-confirm__detail qio-modal-body">{{ detail }}</p>
        <div class="qio-confirm__actions qio-modal-actions">
          <button ref="cancelRef" class="qio-btn" type="button" data-qio-confirm-action="cancel" @click="cancel">
            {{ cancelText }}
          </button>
          <button
            class="qio-btn"
            :class="tone === 'danger' ? 'danger-solid' : 'primary'"
            type="button"
            data-qio-confirm-action="confirm"
            @click="confirm"
          >
            {{ confirmText }}
          </button>
        </div>
      </div>
    </div>
  </Transition>

  <Transition name="qio-fade">
    <div
      v-if="open && variant !== 'layer'"
      ref="rootRef"
      class="qio-confirm"
      :class="variant === 'popover' ? 'qio-confirm--popover qio-floating' : 'qio-confirm--inline'"
      role="group"
      :aria-labelledby="titleId"
      :aria-describedby="detail ? detailId : undefined"
    >
      <div class="qio-modal-head qio-confirm__head">
        <span class="qio-modal-head__slot" aria-hidden="true" />
        <div class="qio-modal-heading">
          <p :id="titleId" class="qio-confirm__title qio-modal-title">{{ title }}</p>
        </div>
        <!-- 非 layer 档没有关闭按钮；右槽仍保留等宽空间，标题不会被挤偏 -->
        <span class="qio-modal-head__slot qio-modal-head__slot--end" aria-hidden="true" />
      </div>
      <p v-if="detail" :id="detailId" class="qio-confirm__detail qio-modal-body">{{ detail }}</p>
      <div class="qio-confirm__actions qio-modal-actions">
        <button ref="cancelRef" class="qio-btn" type="button" data-qio-confirm-action="cancel" @click="cancel">
          {{ cancelText }}
        </button>
        <button
          class="qio-btn"
          :class="tone === 'danger' ? 'danger-solid' : 'primary'"
          type="button"
          data-qio-confirm-action="confirm"
          @click="confirm"
        >
          {{ confirmText }}
        </button>
      </div>
    </div>
  </Transition>
</template>

<style scoped>
.qio-confirm--layer {
  max-width: min(420px, 100%);
}
.qio-confirm__title {
  margin: 0;
}
/* 正文左右留白，长文本不贴边 */
.qio-confirm__detail {
  padding: 0 var(--sp-1);
}
@media (max-width: 480px) {
  .qio-confirm--layer {
    max-width: 100%;
  }
}
</style>
