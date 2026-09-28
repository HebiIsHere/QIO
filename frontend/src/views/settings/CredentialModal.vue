<script lang="ts">
export type CredentialModalMode = "create" | "edit" | "rotate";
</script>

<script setup lang="ts">
/**
 * 凭据弹窗：只负责「弹窗」这件事（遮罩、标题、进出场、焦点），
 * 表单本身与首次引导共用 `components/credentials/CredentialForm.vue`。
 */
import { onMounted, onUnmounted, ref } from "vue";
import CredentialForm from "../../components/credentials/CredentialForm.vue";
import type { CredentialMeta, VerifyReport } from "../../services/api";
import type { CredentialMode } from "../../services/credentials";

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

function onKeydown(event: KeyboardEvent) {
  if (event.key === "Escape") {
    event.stopPropagation();
    emit("cancel");
  }
}

onMounted(() => {
  refreshTitle();
  lastFocused = document.activeElement as HTMLElement | null;
  firstFocusable()?.focus();
});
onUnmounted(() => {
  // 关闭后把焦点还给打开弹窗的那个按钮，键盘用户不会掉到页面开头
  lastFocused?.focus?.();
});
</script>

<template>
  <div class="modal-mask" :class="{ leaving: props.leaving }" @click.self="emit('cancel')" @keydown="onKeydown">
    <div
      ref="dialog"
      class="modal qio-card"
      role="dialog"
      aria-modal="true"
      :aria-label="title"
    >
      <header class="modal-head">
        <h3>{{ title }}</h3>
        <button type="button" class="close" aria-label="关闭" @click="emit('cancel')">×</button>
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
  width: min(560px, 92vw); max-height: 88vh; overflow: auto; background: var(--bg-elevated);
  border: 1px solid var(--border-strong); border-radius: var(--r-lg); padding: var(--sp-5); color: var(--text-primary);
  animation: cred-modal-in var(--dur-menu) var(--ease-out) both;
  transition: transform var(--dur-exit) var(--ease-in), opacity var(--dur-exit) var(--ease-in);
}
.modal-mask.leaving .modal { transform: translateY(var(--shift-2)) scale(.995); opacity: 0; }
@keyframes cred-mask-in { from { opacity: 0; } to { opacity: 1; } }
@keyframes cred-modal-in {
  from { opacity: 0; transform: translateY(var(--shift-4)) scale(.99); }
  to { opacity: 1; transform: none; }
}
.modal-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: var(--sp-3); }
.modal-head h3 { margin: 0; font-size: var(--fs-md); color: var(--text-strong); }
.close {
  border: none; background: transparent; color: var(--text-muted); font-size: 20px; line-height: 1;
  cursor: pointer; padding: 2px 6px; border-radius: var(--r-sm);
}
.close:hover { color: var(--text-strong); background: var(--accent-soft); }
.hint { margin: 0 0 var(--sp-3); font-size: var(--fs-xs); color: var(--text-muted); line-height: 1.5; }
</style>
