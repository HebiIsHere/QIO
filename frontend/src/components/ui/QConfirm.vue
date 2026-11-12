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
 * 仲裁（已接入 B 的 stores/modalArbiter.ts）：只有 layer 档是中央弹窗——
 * 打开时 open({ id, kind: "confirm", dismissible: true })；返回 false 时不得显示，
 * 等挡路弹窗关闭后重试；被 suspendAll(by) 挂起时 v-show 隐藏（状态保留）；
 * 关闭/卸载时 close(id)。inline/popover 是就地控件，不参与中央弹窗仲裁。
 *
 * 窗口层（#82）：layer 档 **Teleport 到 body**，与 QModal 一致。
 * 之前就地渲染，设置页的 .set-main 有 backdrop-filter —— 它会为 position:fixed 后代
 * 创建包含块并形成 stacking context，于是遮罩不再相对“内容视口”，而是跟着可滚动的内容
 * 盒子走：800×600 实测遮罩 rect 为 y=-1169 / height=2847，确认框标题与关闭按钮落到
 * 顶部全局栏（WindowChrome，z-index 240）下面，点不到也看不清。
 * 现在遮罩仍用既有 --window-frame-inset（= 内容视口的四边），面板高度上限取遮罩内容盒
 * （不是写死 top/魔法数字）；窗口标题条继续是最高层（拖窗口/关窗口不被模态挡住）。
 * layer 档的面板/遮罩带 data-qio-confirm-layer="window"，供验收脚本与设置页样式定位。
 */
import { computed, nextTick, onBeforeUnmount, ref, useId, watch } from "vue";
import { getActivePinia } from "pinia";
import QIconButton from "./QIconButton.vue";
import { useModalArbiterStore } from "../../stores/modalArbiter";

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

/** 仲裁：layer 档是中央弹窗；没有 pinia 的画廊/单测环境跳过（只做纯视觉）。 */
const instanceId = useId();
const confirmModalId = computed(() => `qio-confirm-${instanceId}`);
const arbiter = getActivePinia() ? useModalArbiterStore() : null;
const layerRegistered = ref(false);
const layerBlocked = ref(false);
/**
 * 统一活动判断（F01）：已打开 && 无整体挂起 && 是栈顶活动项。
 * 覆盖「被更高优先级窗口盖住」与「关闭询问整体挂起」两种让位；没有 pinia 的
 * 画廊/单测环境退化为纯视觉（注册成功即可见）。
 */
const layerActiveArbiter = computed(() =>
  arbiter ? arbiter.isVisible(confirmModalId.value) : layerRegistered.value,
);
const layerShown = computed(
  () => props.open && props.variant === "layer" && !layerBlocked.value && layerActiveArbiter.value,
);
/** 键盘/Esc 只在这一层真正可见时生效；被挡住或挂起时把按键让给更高层 */
const layerActive = computed(() => (props.variant !== "layer" ? true : layerShown.value));

function registerLayer(): void {
  if (!props.open || props.variant !== "layer" || layerRegistered.value) return;
  if (!arbiter) {
    layerRegistered.value = true;
    return;
  }
  const allowed = arbiter.open({ id: confirmModalId.value, kind: "confirm", dismissible: true });
  layerRegistered.value = allowed;
  layerBlocked.value = !allowed;
}

function unregisterLayer(): void {
  const wasRegistered = layerRegistered.value;
  layerRegistered.value = false;
  layerBlocked.value = false;
  if (wasRegistered) arbiter?.close(confirmModalId.value);
}

watch(
  () => [props.open, props.variant] as const,
  () => {
    if (props.open && props.variant === "layer") registerLayer();
    else unregisterLayer();
  },
  { immediate: true },
);
watch(
  () => (arbiter ? arbiter.activeId : null),
  () => {
    if (props.open && props.variant === "layer" && !layerRegistered.value) registerLayer();
  },
);
// 被挡住后终于放行：焦点仍然先给“取消”（与正常打开一致）
watch(layerShown, async (allowed) => {
  if (!allowed || !props.open) return;
  await nextTick();
  cancelRef.value?.focus();
});

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
  if (!props.open || !layerActive.value) return;
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
  unregisterLayer();
  window.removeEventListener("keydown", onKeydown, true);
  if (props.open) restoreFocus();
});
</script>

<template>
  <!-- layer 档是窗口层：Teleport 到 body，脱离设置页 .set-main 的 backdrop-filter 包含块
       （就地渲染时遮罩会跟着滚动内容走、并被 z-index 240 的顶部全局栏盖住）。 -->
  <Teleport to="body">
    <Transition name="qio-fade">
      <div
        v-if="open && variant === 'layer' && !layerBlocked"
        v-show="layerShown"
        :aria-hidden="layerShown ? undefined : 'true'"
        :inert="layerShown ? undefined : true"
        class="qio-confirm-scrim"
        data-qio-confirm-layer="window"
      >
        <div
          ref="rootRef"
          class="qio-confirm qio-confirm--layer qio-floating qio-pop-in"
          role="dialog"
          aria-modal="true"
          :aria-labelledby="titleId"
          :aria-describedby="detail ? detailId : undefined"
          data-qio-confirm-layer="window"
        >
        <!-- 三列等宽：左槽只占位（空内容），标题严格居中，右槽关闭按钮 -->
        <div class="qio-modal-head qio-confirm__head">
          <span class="qio-modal-head__slot" aria-hidden="true" />
          <div class="qio-modal-heading">
            <p :id="titleId" class="qio-confirm__title qio-modal-title">{{ title }}</p>
          </div>
          <!-- 右槽保持等宽占位；关闭按钮在 DOM 里放在动作按钮之后（视觉绝对定位在右上），
               既满足“左右槽等宽、标题中轴”，也不改变既有调用方对动作按钮顺序的假设。 -->
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
        <QIconButton
          class="qio-confirm__close"
          icon="close"
          label="取消并关闭"
          description="取消并关闭，不执行这个动作"
          size="sm"
          tone="quiet"
          data-qio-confirm-close
          @click="cancel"
        />
        </div>
      </div>
    </Transition>
  </Teleport>

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
  position: relative;
  max-width: min(420px, 100%);
  /* 窗口层：面板永不超出遮罩内容盒（= 内容视口去掉遮罩内边距），长正文在面板内部滚动。
     高度上限直接取父级 flex 容器（遮罩已按 --window-frame-inset 固定到内容视口），
     不写死 top / height，也不用 100vh 再减魔法数字。 */
  max-height: 100%;
  overflow-y: auto;
  overscroll-behavior: contain;
}
/* 危险档确认按钮：用既有 --danger 令牌落地“实心危险”。
   选择器（.qio-confirm--layer + data 钩子 + .danger-solid，再加 scoped 属性）比设置页
   普通按钮覆盖规则（.qio-settings .qio-btn.qio-btn，0-3-0）更高一级，不依赖 !important；
   即使将来 layer 不再 Teleport、或调用方把它放进别的按钮规则作用域，危险态也不会被压成普通按钮。 */
.qio-confirm--layer[data-qio-confirm-layer="window"] [data-qio-confirm-action="confirm"].danger-solid {
  background: var(--danger);
  border-color: var(--danger);
  color: var(--bg-base);
}
/* 关闭按钮视觉在右上（右槽已留出等宽空间，标题不会被挤偏） */
.qio-confirm__close {
  position: absolute;
  top: var(--sp-3);
  right: var(--sp-3);
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