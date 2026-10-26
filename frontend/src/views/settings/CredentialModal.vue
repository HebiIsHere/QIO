<script lang="ts">
export type CredentialModalMode = "create" | "edit" | "rotate";
</script>

<script setup lang="ts">
/**
 * 凭据弹窗：只负责「弹窗」这件事（遮罩、标题、进出场、焦点），
 * 表单本身与首次引导共用 `components/credentials/CredentialForm.vue`。
 */
import { computed, onMounted, onUnmounted, ref } from "vue";
import { vQioScrollbar } from "../../composables/useQioScrollbars";
import CredentialForm from "../../components/credentials/CredentialForm.vue";
import QIconButton from "../../components/ui/QIconButton.vue";
import type { CredentialMeta, VerifyReport } from "../../services/api";
import type { CredentialMode } from "../../services/credentials";
import { useModalArbiterStore } from "../../stores/modalArbiter";

/**
 * 中央弹窗仲裁身份：凭据表单是「局部中央弹窗」（优先级 credential）。
 * 只有仲裁器允许（没有更高优先级的弹窗）时才显示；被关闭询问等挂起时只 v-show 隐藏，
 * 已填写的密钥草稿与焦点顺序都保留在 DOM 里，取消后原样恢复。
 */
const CREDENTIAL_MODAL_ID = "qio-credential-modal";
const arbiter = useModalArbiterStore();
const hiddenBy = computed(() => arbiter.hiddenBy(CREDENTIAL_MODAL_ID));

const props = defineProps<{
  mode: CredentialModalMode;
  initial?: CredentialMeta | null;
  /** 正在播退出动画：仍拦截点击（避免落到下层危险操作），但内容已经不可交互 */
  leaving?: boolean;
}>();
const emit = defineEmits<{
  saved: [payload: { credential: CredentialMeta | null; report: VerifyReport | null; mode: CredentialMode }];
  cancel: [];
}>();

const title = ref("");
function refreshTitle() {
  title.value =
    props.mode === "edit"
      ? "编辑凭据"
      : props.mode === "rotate"
        ? "更换 API Key"
        : "添加凭据";
}
refreshTitle();

const dialog = ref<HTMLElement | null>(null);
let lastFocused: HTMLElement | null = null;

function firstFocusable(): HTMLElement | null {
  return (
    // 优先落在表单里的第一个可操作元素（服务厂商），而不是标题栏的关闭按钮：
    // 直接开始填写比「先面对一个关闭按钮」更符合键盘用户的操作顺序。
    dialog.value?.querySelector<HTMLElement>(
      ".cred-form input, .cred-form button, .cred-form select, .cred-form textarea",
    ) ??
    dialog.value?.querySelector<HTMLElement>(
      "input, select, textarea, button, [tabindex]:not([tabindex='-1'])",
    ) ?? null
  );
}

/**
 * 键盘行为与 ApprovalModal 保持一致：
 *
 * * Esc 取消（stopPropagation：只关最上层这一个弹窗）；
 * * Tab 在弹窗内循环。没有这一段时，Tab 会从"保存"走到弹窗后面的设置页按钮上 ——
 *   对一个模态窗口来说，焦点跑到遮罩以外就是键盘用户"点到了看不见的地方"。
 */
function focusables(): HTMLElement[] {
  const root = dialog.value;
  if (!root) return [];
  return Array.from(
    root.querySelectorAll<HTMLElement>(
      'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), summary, [href], [tabindex]:not([tabindex="-1"])',
    ),
  );
}

function onKeydown(event: KeyboardEvent) {
  // 被更高层弹窗（关闭询问等）暂时隐藏时：键盘事件不落在这个已经看不见的弹窗里。
  if (hiddenBy.value) return;
  if (event.key === "Escape") {
    event.stopPropagation();
    emit("cancel");
    return;
  }
  if (event.key !== "Tab") return;
  const items = focusables();
  if (!items.length) return;
  const first = items[0];
  const last = items[items.length - 1];
  const active = document.activeElement as HTMLElement | null;
  if (event.shiftKey && (active === first || active === dialog.value)) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && active === last) {
    event.preventDefault();
    first.focus();
  }
}

onMounted(() => {
  refreshTitle();
  const opened = arbiter.open({
    id: CREDENTIAL_MODAL_ID,
    kind: "credential",
    dismissible: true,
  });
  if (!opened) {
    // 仲裁器说现在不能显示：不叠层、不抢焦点，交给调用方收起（用户可稍后重试）。
    emit("cancel");
    return;
  }
  lastFocused = document.activeElement as HTMLElement | null;
  firstFocusable()?.focus();
});
onUnmounted(() => {
  arbiter.close(CREDENTIAL_MODAL_ID);
  // 关闭后把焦点还给打开弹窗的那个按钮，键盘用户不会掉到页面开头
  lastFocused?.focus?.();
});
</script>

<template>
  <div
    v-show="!hiddenBy"
    class="modal-mask"
    :class="{ leaving: props.leaving }"
    :aria-hidden="hiddenBy ? 'true' : undefined"
    :inert="hiddenBy ? true : undefined"
    @click.self="emit('cancel')"
    @keydown="onKeydown"
  >
    <div
      ref="dialog"
      class="modal qio-card"
      v-qio-scrollbar
      data-qio-fit
      role="dialog"
      aria-modal="true"
      :aria-label="title"
    >
      <!-- 对称标题：左右两槽等宽（左槽只占位），关闭按钮不会把标题挤偏；图标按钮走 QIconButton。 -->
      <header class="modal-head qio-modal-head">
        <span class="modal-head__slot qio-modal-head__slot" aria-hidden="true"></span>
        <h3 class="modal-title qio-modal-title">{{ title }}</h3>
        <span class="modal-head__slot modal-head__slot--end qio-modal-head__slot qio-modal-head__slot--end">
          <QIconButton
            icon="close"
            label="关闭"
            description="关闭这个弹窗，本次未保存的填写会丢失"
            size="sm"
            tone="quiet"
            @click="emit('cancel')"
          />
        </span>
      </header>
      <p v-if="mode === 'rotate'" class="hint">
        先验证新的 API Key，再替换这条凭据的密钥。验证或写入失败时，原来的凭据仍然可用。
      </p>
      <CredentialForm
        :mode="mode"
        :initial="initial ?? null"
        variant="modal"
        @saved="emit('saved', $event)"
        @cancel="emit('cancel')"
      />
    </div>
  </div>
</template>

<style scoped>
.modal-mask {
  position: fixed; inset: 0; z-index: 200; background: var(--bg-overlay);
  display: flex; align-items: center; justify-content: center; padding: var(--sp-4);
  /* 出现：立即可见随后减速；退出：短淡出 + 微收，结束后由父级卸载 */
  animation: cred-mask-in var(--dur-menu) var(--ease-out) both;
  transition: opacity var(--dur-exit) var(--ease-in);
}
.modal-mask.leaving { opacity: 0; }
.modal {
  /* 弹窗唯一的纵向滚动层（表单较长时在这里滚）；横向永不滚动（长 Key / 长 URL 换行）。
     标题栏 sticky 在顶部，保存 / 验证按钮在表单里 sticky 在底部 —— 关键操作不随正文滚走。 */
  width: min(560px, 92vw); max-height: 88vh;
  overflow-y: auto; overflow-x: clip; overscroll-behavior: contain;
  background: var(--bg-elevated);
  border: 1px solid var(--border-strong); border-radius: var(--qio-ui-radius); padding: var(--sp-5); color: var(--text-primary);
  animation: cred-modal-in var(--dur-menu) var(--ease-out) both;
  transition: transform var(--dur-exit) var(--ease-in), opacity var(--dur-exit) var(--ease-in);
}
.modal-mask.leaving .modal { transform: translateY(var(--shift-2)) scale(.995); opacity: 0; }
@keyframes cred-mask-in { from { opacity: 0; } to { opacity: 1; } }
@keyframes cred-modal-in {
  from { opacity: 0; transform: translateY(var(--shift-4)) scale(.99); }
  to { opacity: 1; transform: none; }
}
/* 标题栏固定不随正文滚走；对称三列布局由 base.css 的 .qio-modal-head 提供。 */
.modal-head {
  margin-bottom: var(--sp-3);
  position: sticky; top: 0; z-index: 2;
  background: var(--bg-elevated);
}
.modal-head h3 { font-size: var(--fs-md); color: var(--text-strong); }
.hint { margin: 0 0 var(--sp-3); font-size: var(--fs-xs); color: var(--text-muted); line-height: 1.5; text-align: center; }
</style>
