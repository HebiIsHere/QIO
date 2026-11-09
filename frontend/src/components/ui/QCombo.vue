<script setup lang="ts">
/**
 * 可搜索下拉（QSelect 的兄弟，不是它的替代）。
 *
 * 为什么单独一个组件：厂商列表有十几项并且以后只会更多，键盘用户没法靠方向键
 * 在长列表里找到目标；QSelect 的语义是「短列表里选一个」，加一个 search prop 会
 * 把两种交互塞进一个组件。这里保持同样的视觉与键盘模型（↑↓ / Home / End /
 * Enter / Esc / 空格），只是多了输入过滤。
 *
 * 本轮视觉/状态收敛：触发框、菜单、选项行统一 var(--qio-ui-radius)；
 * 箭头用 QIcon(chevron-down) 而不是内联 svg；补 busy（处理中）与空结果文案 prop。
 */
import { computed, nextTick, onBeforeUnmount, ref, useId, watch } from "vue";
import { vQioScrollbar } from "../../composables/useQioScrollbars";
import { isComposingKeyEvent, useEscapeLayer } from "../../composables/useEscapeLayer";
import QIcon from "./QIcon.vue";

const props = withDefaults(
  defineProps<{
    options: { value: string; label: string; hint?: string }[];
    modelValue?: string;
    placeholder?: string;
    disabled?: boolean;
    /** 处理中：保留当前值、不展开菜单 */
    busy?: boolean;
    /** 搜不到时的提示（默认与原厂商列表语境一致） */
    emptyText?: string;
    /** 搜索框占位（默认与原厂商列表语境一致） */
    searchPlaceholder?: string;
  }>(),
  {
    modelValue: "",
    placeholder: "",
    disabled: false,
    busy: false,
    emptyText: "没有匹配的厂商",
    searchPlaceholder: "搜索厂商…",
  },
);
const emit = defineEmits<{ "update:modelValue": [string] }>();

const open = ref(false);
const query = ref("");
const hl = ref(-1);
const root = ref<HTMLElement | null>(null);
const searchEl = ref<HTMLInputElement | null>(null);
const blocked = computed(() => props.disabled || props.busy);
/** 输入法组合输入（选字）期间：回车 / 方向键属于输入法，不是选择与导航。 */
const composing = ref(false);
/** Esc 层级身份：所有 QCombo 实例互相独立，界面层级由 useEscapeLayer 统一仲裁。 */
const escapeLayerId = "qio-combo-" + useId();

const selectedLabel = computed(
  () => props.options.find((o) => o.value === props.modelValue)?.label ?? "",
);
const filtered = computed(() => {
  const q = query.value.trim().toLowerCase();
  if (!q) return props.options;
  return props.options.filter(
    (o) => o.label.toLowerCase().includes(q) || o.hint?.toLowerCase().includes(q) || o.value.includes(q),
  );
});

async function openMenu() {
  if (blocked.value) return;
  composing.value = false;
  open.value = true;
  query.value = "";
  hl.value = Math.max(0, filtered.value.findIndex((o) => o.value === props.modelValue));
  await nextTick();
  searchEl.value?.focus();
}

function close(restoreFocus = false) {
  if (!open.value) return;
  open.value = false;
  query.value = "";
  composing.value = false;
  if (restoreFocus) (root.value?.querySelector(".qio-combo-trigger") as HTMLElement | null)?.focus();
}

function pick(value: string) {
  // 提交入口自校验：处理中 / 已禁用时，迟到的事件（鼠标或键盘）不能改变取值。
  if (blocked.value) return;
  emit("update:modelValue", value);
  close(true);
}

function toggle() {
  if (blocked.value) return;
  if (open.value) close(true);
  else void openMenu();
}

function onCompositionStart() {
  composing.value = true;
}

function onCompositionEnd() {
  // 组合输入结束（候选字已确认）：之后正常键盘选择恢复；这里只解除保护，不提交任何东西。
  composing.value = false;
}

/**
 * 键盘只在根节点处理一次（输入框的 keydown 会冒泡到这里）。
 * 之前输入框和根各挂一份，一次方向键会移动两项、回车先选中又被外层重新展开。
 */
function onKeydown(event: KeyboardEvent) {
  // 组合输入阶段：确认候选字的回车、选字用的方向键都不是「提交 / 导航」。
  if (composing.value || isComposingKeyEvent(event)) return;
  // Tab 只收起菜单，不阻止默认焦点移动；无结果时同样要收起（空列表不是「还有活干」）。
  if (event.key === "Tab") {
    close(false);
    return;
  }
  if (blocked.value) return;
  if (!open.value) {
    if (["ArrowDown", "ArrowUp", "Enter", " "].includes(event.key)) {
      event.preventDefault();
      void openMenu();
    }
    return;
  }
  const items = filtered.value;
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    // 菜单打开时方向键归菜单：即使没有结果也不能让页面滚动，且一次按键只移动一项。
    event.preventDefault();
    if (!items.length) return;
    const step = event.key === "ArrowDown" ? 1 : -1;
    hl.value = (hl.value + step + items.length) % items.length;
    return;
  }
  if (event.key === "Home" || event.key === "End") {
    event.preventDefault();
    if (!items.length) return;
    hl.value = event.key === "Home" ? 0 : items.length - 1;
    return;
  }
  if (event.key === "Enter") {
    // 回车只选择一次；菜单打开时永远不触发周围表单提交（无结果时也不提交）。
    event.preventDefault();
    if (hl.value >= 0 && items[hl.value]) pick(items[hl.value].value);
    return;
  }
}

function onDocPointer(event: PointerEvent) {
  if (!root.value?.contains(event.target as Node)) close();
}
watch(open, (value) => {
  if (value) document.addEventListener("pointerdown", onDocPointer);
  else document.removeEventListener("pointerdown", onDocPointer);
});
watch(blocked, (value) => {
  // 处理中 / 禁用：已展开的列表必须收起，后续鼠标、键盘与迟到事件都不能再提交。
  if (value && open.value) close(false);
});
watch(filtered, (items) => {
  hl.value = items.length ? 0 : -1;
});
onBeforeUnmount(() => document.removeEventListener("pointerdown", onDocPointer));

// Esc 层级：菜单展开时这一层最内层，先收菜单；收起后（active=false）Esc 才轮到外层弹窗。
useEscapeLayer({
  id: escapeLayerId,
  active: () => open.value,
  onEscape: () => {
    if (!open.value) return false;
    close(true);
    return true;
  },
});
</script>

<template>
  <div
    ref="root"
    class="qio-combo"
    :class="{ open, disabled, busy }"
    role="combobox"
    :aria-expanded="open"
    :aria-disabled="disabled || undefined"
    :aria-busy="busy || undefined"
    aria-haspopup="listbox"
    @keydown="onKeydown"
    @compositionstart="onCompositionStart"
    @compositionend="onCompositionEnd"
  >
    <button
      type="button"
      class="qio-combo-trigger"
      :disabled="blocked"
      :aria-expanded="open"
      @click="toggle"
    >
      <span class="qio-combo-value" :class="{ empty: !selectedLabel }">
        {{ selectedLabel || placeholder || "请选择" }}
      </span>
      <QIcon class="chev" name="chevron-down" :size="14" />
    </button>
    <div v-if="open" class="qio-combo-menu" data-qio-fit>
      <input
        ref="searchEl"
        v-model="query"
        class="qio-combo-search"
        type="text"
        :placeholder="searchPlaceholder"
        :aria-label="searchPlaceholder"
        @blur="composing = false"
      />
      <ul class="qio-combo-list" role="listbox" v-qio-scrollbar="{ axis: 'y' }" data-qio-fit>
        <li
          v-for="(o, i) in filtered"
          :key="o.value"
          class="qio-combo-opt"
          :class="{ sel: o.value === modelValue, hl: i === hl }"
          role="option"
          :aria-selected="o.value === modelValue ? 'true' : 'false'"
          @click.stop="pick(o.value)"
          @mouseenter="hl = i"
        >
          <span class="opt-label">{{ o.label }}</span>
          <span v-if="o.hint" class="opt-hint">{{ o.hint }}</span>
        </li>
        <li v-if="!filtered.length" class="qio-combo-empty">{{ emptyText }}</li>
      </ul>
    </div>
  </div>
</template>

<style scoped>
.qio-combo { position: relative; }
.qio-combo-trigger {
  width: 100%; display: flex; align-items: center; justify-content: space-between; gap: var(--sp-2);
  height: 38px; padding: 0 var(--sp-3); border-radius: var(--qio-ui-radius);
  border: 1px solid var(--border-subtle); background: var(--bg-inset); color: var(--text-primary);
  font-size: var(--fs-base); cursor: pointer; min-width: 0;
  transition: border-color var(--mo-1-state) var(--ease-1), background var(--mo-1-state) var(--ease-1),
    box-shadow var(--mo-1-state) var(--ease-1);
}
.qio-combo-trigger:hover { border-color: var(--border-strong); }
.qio-combo-trigger:focus-visible {
  border-color: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-soft);
  outline: 2px solid var(--focus-ring);
  outline-offset: 1px;
}
.qio-combo.disabled .qio-combo-trigger { opacity: .55; cursor: default; }
.qio-combo.busy .qio-combo-trigger { cursor: progress; }
.qio-combo-value { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.qio-combo-value.empty { color: var(--text-muted); }
.chev { color: var(--text-muted); flex: none; transition: transform var(--mo-1-state) var(--ease-1); }
.qio-combo.open .chev { transform: rotate(180deg); color: var(--accent); }
.qio-combo-menu {
  position: absolute; z-index: 30; left: 0; right: 0; top: calc(100% + 4px);
  background: var(--bg-elevated); border: 1px solid var(--border-strong); border-radius: var(--qio-ui-radius);
  box-shadow: var(--elev-floating); overflow: hidden; min-width: 0;
  animation: combo-in var(--mo-2-in) var(--ease-2) both;
}
@keyframes combo-in { from { opacity: 0; transform: translateY(calc(var(--shift-2) * -1)); } to { opacity: 1; transform: none; } }
.qio-combo-search {
  width: 100%; height: 34px; padding: 0 var(--sp-3); border: none; outline: none;
  border-bottom: 1px solid var(--border-subtle); background: var(--bg-inset); color: var(--text-primary);
  font-family: var(--sans); font-size: var(--fs-sm);
}
.qio-combo-search:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: -2px; }
/* 菜单列表：统一纵向滚动（QIO 指示条），绝不横向滚 */
.qio-combo-list {
  list-style: none; margin: 0; padding: var(--sp-1);
  max-height: min(260px, 50vh); overflow-y: auto; overscroll-behavior: contain; min-width: 0;
}
.qio-combo-opt {
  display: flex; align-items: baseline; gap: var(--sp-2); justify-content: space-between;
  padding: 6px var(--sp-3); border-radius: var(--qio-ui-radius); cursor: pointer;
  font-size: var(--fs-sm); color: var(--text-primary); min-width: 0;
}
.opt-label { min-width: 0; overflow-wrap: anywhere; }
.qio-combo-opt.hl { background: var(--layer-hover); }
.qio-combo-opt.sel { color: var(--text-strong); background: var(--layer-selected); }
.opt-hint { font-size: var(--fs-xs); color: var(--text-muted); text-align: right; overflow-wrap: anywhere; }
.qio-combo-empty { padding: var(--sp-3); color: var(--text-muted); font-size: var(--fs-sm); text-align: center; }
@media (max-width: 480px) {
  .qio-combo-menu { max-width: calc(100vw - 2 * var(--sp-2)); }
}
</style>
