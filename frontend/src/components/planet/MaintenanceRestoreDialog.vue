<!--
  整批撤回「记忆整理」维护批次 —— 中央确认弹窗（D · 记忆星球）。

  为什么是中央弹窗而不是条目内确认：整批撤回会同时影响多条真实知识，用户必须先看到
  「摘要 / 受影响条目 / 可恢复与不可恢复 / 原因 / 修改前后内容」再决定；条目内一行字说不清。
  （单项恢复仍然留在条目内联确认里：不是所有记忆操作都升级成中央弹窗。）

  状态与数据全部来自父组件（预览只读 + 批次操作记录）；本组件只负责显示、焦点与暴露动作。
  恢复保护约束照实写：不覆盖用户后续修改、历史不删除、受影响的项会被拒绝并说明原因。
-->
<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from "vue";
import { useModalArbiterStore } from "../../stores/modalArbiter";
import QIconButton from "../ui/QIconButton.vue";

export interface RestoreDialogItem {
  key: string;
  /** 可恢复 / 部分可恢复 / 不可安全恢复 / 已恢复过 */
  label: string;
  /** qio-state 的语义档：ok / warn / quiet / info */
  tone: string;
  title: string;
  reason: string;
  /** 修改前快照内容（真实批次操作记录；没有就留空，不臆造） */
  before: string;
  /** 修改后快照内容（真实批次操作记录；没有就留空，不臆造） */
  after: string;
}

const props = withDefaults(
  defineProps<{
    open: boolean;
    /** 仲裁器里的稳定身份（同一批次同一身份） */
    modalId: string;
    /** 目标批次的一句话 */
    targetLabel?: string;
    /** 标题（默认是整批撤回；单条恢复传「恢复这一项的维护修改？」） */
    title?: string;
    /** 确认按钮文案（默认「整批撤回」；单条恢复传「恢复这一项」） */
    confirmText?: string;
    /** 摘要：可恢复 / 部分 / 不可恢复 / 已恢复过 */
    summary?: string;
    items?: RestoreDialogItem[];
    warnings?: string[];
    previewLoading?: boolean;
    previewError?: string;
    restoring?: boolean;
    restoreError?: string;
  }>(),
  {
    targetLabel: "",
    title: "整批撤回这次记忆整理？",
    confirmText: "整批撤回",
    summary: "",
    items: () => [],
    warnings: () => [],
    previewLoading: false,
    previewError: "",
    restoring: false,
    restoreError: "",
  },
);

const emit = defineEmits<{
  confirm: [];
  cancel: [];
  "retry-preview": [];
}>();

const arbiter = useModalArbiterStore();
/**
 * 统一活动判断（F01）：已打开 && 无整体挂起 && 是栈顶活动项。
 * 被更高优先级窗口盖住时同样让位（只说「暂不显示」）；DOM、展开状态与滚动位置都保留。
 */
const layerVisible = computed(() => props.open && arbiter.isVisible(props.modalId));
const rootRef = ref<HTMLElement | null>(null);
const cancelRef = ref<HTMLButtonElement | null>(null);
/** 展开「修改前后内容」的条目（按条目身份，不按索引） */
const expanded = ref<string[]>([]);
let restoreFocusTo: HTMLElement | null = null;

function toggleItem(key: string) {
  expanded.value = expanded.value.includes(key)
    ? expanded.value.filter((entry) => entry !== key)
    : [...expanded.value, key];
}

function hasContent(item: RestoreDialogItem): boolean {
  return Boolean(item.before || item.after);
}

function focusables(): HTMLElement[] {
  const root = rootRef.value;
  if (!root) return [];
  return Array.from(
    root.querySelectorAll<HTMLElement>(
      'button:not([disabled]), input:not([disabled]), [href], [tabindex]:not([tabindex="-1"])',
    ),
  );
}

function cancel() {
  if (props.restoring) return;
  emit("cancel");
}

function onKeydown(event: KeyboardEvent) {
  if (!props.open) return;
  // 让位期间（更高优先级窗口 / 关闭询问挂起）：键盘事件不落在这层，Esc 只关最上层那个弹窗。
  if (!layerVisible.value) return;
  if (event.key === "Escape") {
    event.preventDefault();
    cancel();
    return;
  }
  if (event.key !== "Tab") return;
  const items = focusables();
  if (!items.length) return;
  const first = items[0];
  const last = items[items.length - 1];
  const active = document.activeElement as HTMLElement | null;
  if (event.shiftKey && (active === first || !rootRef.value?.contains(active))) {
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
      // 中央弹窗必须走仲裁；被 false 挡住时不强行显示（交给父组件收起）。
      const allowed = arbiter.open({
        id: props.modalId,
        kind: "recovery",
        dismissible: true,
      });
      if (!allowed) {
        emit("cancel");
        return;
      }
      restoreFocusTo = document.activeElement instanceof HTMLElement ? document.activeElement : null;
      window.addEventListener("keydown", onKeydown, true);
      await nextTick();
      cancelRef.value?.focus();
      return;
    }
    window.removeEventListener("keydown", onKeydown, true);
    arbiter.close(props.modalId);
    expanded.value = [];
    const el = restoreFocusTo;
    restoreFocusTo = null;
    if (el && el.isConnected) el.focus();
  },
  { immediate: true },
);

/** 重新成为活动层（更高优先级关闭 / 挂起解除）：焦点回到本窗口内。 */
watch(layerVisible, async (visible) => {
  if (!visible) return;
  await nextTick();
  if (rootRef.value?.contains(document.activeElement)) return;
  cancelRef.value?.focus();
});

onBeforeUnmount(() => {
  window.removeEventListener("keydown", onKeydown, true);
  arbiter.close(props.modalId);
  // 卸载时弹窗还开着（例如父级被替换）：把焦点还给打开它的元素，不让键盘用户掉到页面开头。
  if (props.open) {
    const el = restoreFocusTo;
    restoreFocusTo = null;
    if (el && el.isConnected) el.focus();
  }
});
</script>

<template>
  <Transition name="qio-fade">
    <div
      v-if="open"
      v-show="layerVisible"
      class="restore-scrim"
      :aria-hidden="layerVisible ? undefined : 'true'"
      :inert="layerVisible ? undefined : true"
    >
      <div
        ref="rootRef"
        class="qio-confirm restore-dialog qio-floating"
        role="dialog"
        aria-modal="true"
        aria-labelledby="qio-restore-dialog-title"
      >
        <!-- 标题栏三列等宽：左槽只占位，中间标题严格居中，右槽关闭按钮不会挤偏标题 -->
        <div class="qio-modal-head restore-dialog__head">
          <span class="qio-modal-head__slot" aria-hidden="true" />
          <h2 id="qio-restore-dialog-title" class="qio-modal-title restore-dialog__title">
            {{ title }}
          </h2>
          <span class="qio-modal-head__slot qio-modal-head__slot--end">
            <QIconButton
              icon="close"
              label="关闭"
              description="关闭这个弹窗，不执行撤回"
              size="sm"
              tone="quiet"
              :disabled="restoring"
              @click="cancel"
            />
          </span>
        </div>
        <p v-if="targetLabel" class="restore-dialog__target mono">{{ targetLabel }}</p>

        <!-- 摘要：先给影响面，再给条目 -->
        <section class="restore-dialog__section" aria-label="影响摘要">
          <p class="restore-dialog__lead">
            这会把这次维护改动的知识恢复成维护前的内容。恢复会用新的版本记录，不删除历史；
            你后来做过的修改不会被覆盖 —— 受影响的那几项会被拒绝并列出原因。
          </p>
          <p v-if="summary" class="restore-dialog__summary mono" data-testid="restore-summary">{{ summary }}</p>
        </section>

        <!-- 预览读取中 / 失败：中央弹窗里也要如实说明，不能拿空的当「没有影响」 -->
        <section v-if="previewLoading" class="restore-dialog__section" role="status">
          <p class="restore-dialog__hint">正在预览这次撤回的真实影响…</p>
        </section>
        <section v-else-if="previewError" class="restore-dialog__section" role="alert">
          <p class="restore-dialog__error">{{ previewError }}</p>
          <button class="qio-btn mini restore-dialog__retry-preview" type="button" @click="emit('retry-preview')">
            重试预览
          </button>
        </section>

        <!-- 受影响条目：逐项状态 + 原因 + 可展开的修改前后内容 -->
        <section v-else class="restore-dialog__section" aria-label="受影响条目">
          <p class="restore-dialog__label">受影响条目（{{ items.length }}）</p>
          <p v-if="!items.length" class="restore-dialog__hint">
            这次预览没有可恢复的操作（服务端未返回操作计划）；不臆造可恢复结论。
          </p>
          <ul class="restore-dialog__items">
            <li v-for="item in items" :key="item.key" class="restore-dialog__item">
              <div class="restore-dialog__item-head">
                <span class="qio-state" :class="item.tone">{{ item.label }}</span>
                <span class="restore-dialog__item-title mono">{{ item.title }}</span>
              </div>
              <p v-if="item.reason" class="restore-dialog__reason">{{ item.reason }}</p>
              <button
                v-if="hasContent(item)"
                class="qio-btn mini quiet restore-dialog__toggle"
                type="button"
                :aria-expanded="expanded.includes(item.key)"
                @click="toggleItem(item.key)"
              >
                {{ expanded.includes(item.key) ? "收起修改前后内容" : "查看修改前后内容" }}
              </button>
              <div v-if="expanded.includes(item.key)" class="restore-dialog__diff">
                <div class="restore-dialog__diff-part">
                  <span class="restore-dialog__diff-label">修改前</span>
                  <pre class="restore-dialog__diff-text mono">{{ item.before || "（这次操作没有留下修改前快照）" }}</pre>
                </div>
                <div class="restore-dialog__diff-part">
                  <span class="restore-dialog__diff-label">修改后</span>
                  <pre class="restore-dialog__diff-text mono">{{ item.after || "（这次操作没有留下修改后快照）" }}</pre>
                </div>
              </div>
            </li>
          </ul>
          <ul v-if="warnings.length" class="restore-dialog__warnings">
            <li v-for="(warning, index) in warnings" :key="index" class="restore-dialog__warning">
              {{ warning }}
            </li>
          </ul>
        </section>

        <p v-if="restoreError" class="restore-dialog__error" role="alert">
          {{ restoreError }}
        </p>

        <!-- 动作：居中、同高、配对同宽；窄窗纵排（共享 .qio-modal-actions 契约） -->
        <div class="qio-modal-actions restore-dialog__actions">
          <button
            ref="cancelRef"
            class="qio-btn restore-dialog__cancel"
            type="button"
            :disabled="restoring"
            @click="cancel"
          >
            {{ restoreError ? "关闭" : "取消" }}
          </button>
          <button
            class="qio-btn danger-solid restore-dialog__confirm"
            type="button"
            :disabled="restoring || previewLoading || Boolean(previewError)"
            :aria-busy="restoring ? 'true' : undefined"
            @click="emit('confirm')"
          >
            {{ restoring ? "正在撤回…" : restoreError ? "重试撤回" : confirmText }}
          </button>
        </div>
      </div>
    </div>
  </Transition>
</template>

<style scoped>
.restore-scrim {
  position: fixed;
  inset: 0;
  z-index: 210;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
  background: var(--bg-overlay);
  backdrop-filter: blur(4px);
  -webkit-backdrop-filter: blur(4px);
}
/* 居中对称；只在这一层纵向滚动（条目多时不把按钮挤出屏幕）。 */
.restore-dialog {
  width: min(560px, 100%);
  max-height: min(80vh, 720px);
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 20px 22px 18px;
  border-radius: var(--qio-ui-radius);
  border: 1px solid var(--border-strong);
  background: var(--bg-elevated);
  box-shadow: var(--shadow-float);
  color: var(--text-secondary);
  text-align: center;
  overflow-y: auto;
  overflow-x: clip;
  overscroll-behavior: contain;
}
.restore-dialog__head { flex: none; }
.restore-dialog__title {
  font-family: var(--serif);
  font-size: 16px;
  color: var(--text-strong);
}
.restore-dialog__target {
  margin: -4px 0 0;
  font-size: 11px;
  color: var(--text-muted);
  overflow-wrap: anywhere;
}
.restore-dialog__section {
  display: flex;
  flex-direction: column;
  gap: 8px;
  text-align: center;
}
.restore-dialog__lead {
  margin: 0;
  font-size: 12.5px;
  line-height: 1.7;
  color: var(--text-secondary);
}
.restore-dialog__summary {
  margin: 0;
  font-size: 12px;
  color: var(--text-strong);
  text-align: center;
}
.restore-dialog__label {
  margin: 0;
  font-size: 11px;
  color: var(--text-muted);
  letter-spacing: 0.02em;
}
.restore-dialog__hint {
  margin: 0;
  font-size: 12px;
  line-height: 1.7;
  color: var(--text-muted);
}
.restore-dialog__items,
.restore-dialog__warnings {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.restore-dialog__item {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 8px 10px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--qio-ui-radius);
  background: var(--bg-surface);
}
.restore-dialog__item-head {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  flex-wrap: wrap;
  text-align: center;
}
.restore-dialog__item-title {
  font-size: 11px;
  color: var(--text-secondary);
  overflow-wrap: anywhere;
  min-width: 0;
  text-align: center;
}
.restore-dialog__reason {
  margin: 0;
  font-size: 11.5px;
  line-height: 1.6;
  color: var(--text-muted);
  overflow-wrap: anywhere;
  text-align: center;
}
.restore-dialog__toggle {
  align-self: center;
}
.restore-dialog__diff {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 8px;
  border-radius: var(--qio-ui-radius);
  background: var(--bg-inset);
  /* 修改前后内容是业务快照，保持左对齐以便逐行比较；只让外层行居中。 */
  text-align: left;
}
.restore-dialog__diff-part {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.restore-dialog__diff-label {
  font-size: 10.5px;
  color: var(--text-muted);
}
.restore-dialog__diff-text {
  margin: 0;
  font-size: 11.5px;
  line-height: 1.6;
  color: var(--text-primary);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.restore-dialog__warning {
  font-size: 11.5px;
  line-height: 1.6;
  color: var(--warning);
  overflow-wrap: anywhere;
  text-align: center;
}
.restore-dialog__error {
  margin: 0;
  font-size: 12px;
  line-height: 1.6;
  color: var(--danger);
  text-align: center;
  overflow-wrap: anywhere;
}
.restore-dialog__actions { flex: none; margin-top: 6px; }
.restore-dialog__retry-preview {
  align-self: center;
}
@media (max-width: 520px) {
  .restore-scrim { padding: 16px; }
}
</style>
