<script setup lang="ts">
/**
 * 可搜索下拉（QSelect 的兄弟，不是它的替代）。
 *
 * 为什么单独一个组件：厂商列表有十几项并且以后只会更多，键盘用户没法靠方向键
 * 在长列表里找到目标；QSelect 的语义是「短列表里选一个」，加一个 search prop 会
 * 把两种交互塞进一个组件。这里保持同样的视觉与键盘模型（↑↓ / Home / End /
 * Enter / Esc / 空格），只是多了输入过滤。
 */
import { computed, nextTick, onBeforeUnmount, ref, watch } from "vue";

const props = defineProps<{
  options: { value: string; label: string; hint?: string }[];
  modelValue?: string;
  placeholder?: string;
  disabled?: boolean;
}>();
const emit = defineEmits<{ "update:modelValue": [string] }>();

const open = ref(false);
const query = ref("");
const hl = ref(-1);
const root = ref<HTMLElement | null>(null);
const searchEl = ref<HTMLInputElement | null>(null);

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
  if (props.disabled) return;
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
  if (restoreFocus) (root.value?.querySelector(".qio-combo-trigger") as HTMLElement | null)?.focus();
}

function pick(value: string) {
  emit("update:modelValue", value);
  close(true);
}

function toggle() {
  if (props.disabled) return;
  if (open.value) close(true);
  else void openMenu();
}

function onKeydown(event: KeyboardEvent) {
  if (props.disabled) return;
  if (event.key === "Escape") {
    event.preventDefault();
    close(true);
    return;
  }
  if (!open.value) {
    if (["ArrowDown", "ArrowUp", "Enter", " "].includes(event.key)) {
      event.preventDefault();
      void openMenu();
    }
    return;
  }
  const items = filtered.value;
  if (!items.length) return;
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    const step = event.key === "ArrowDown" ? 1 : -1;
    hl.value = (hl.value + step + items.length) % items.length;
  } else if (event.key === "Home") {
    event.preventDefault();
    hl.value = 0;
  } else if (event.key === "End") {
    event.preventDefault();
    hl.value = items.length - 1;
  } else if (event.key === "Enter") {
    event.preventDefault();
    if (hl.value >= 0 && items[hl.value]) pick(items[hl.value].value);
  }
}

function onDocPointer(event: PointerEvent) {
  if (!root.value?.contains(event.target as Node)) close();
}
watch(open, (value) => {
  if (value) document.addEventListener("pointerdown", onDocPointer);
  else document.removeEventListener("pointerdown", onDocPointer);
});
watch(filtered, (items) => {
  hl.value = items.length ? 0 : -1;
});
onBeforeUnmount(() => document.removeEventListener("pointerdown", onDocPointer));
</script>

<template>
  <div
    ref="root"
    class="qio-combo"
    :class="{ open, disabled }"
    role="combobox"
    :aria-expanded="open"
    :aria-disabled="disabled || undefined"
    aria-haspopup="listbox"
    @keydown="onKeydown"
  >
    <button
      type="button"
      class="qio-combo-trigger"
      :disabled="disabled"
      :aria-expanded="open"
      @click="toggle"
    >
      <span class="qio-combo-value" :class="{ empty: !selectedLabel }">
        {{ selectedLabel || placeholder || "请选择" }}
      </span>
      <svg class="chev" width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">
        <path d="M4 6l4 4 4-4" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" />
      </svg>
    </button>
    <div v-if="open" class="qio-combo-menu">
      <input
        ref="searchEl"
        v-model="query"
        class="qio-combo-search"
        type="text"
        placeholder="搜索厂商…"
        aria-label="搜索厂商"
        @keydown="onKeydown"
      />
      <ul class="qio-combo-list" role="listbox">
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
        <li v-if="!filtered.length" class="qio-combo-empty">没有匹配的厂商</li>
      </ul>
    </div>
  </div>
</template>

<style scoped>
.qio-combo { position: relative; }
.qio-combo-trigger {
  width: 100%; display: flex; align-items: center; justify-content: space-between; gap: var(--sp-2);
  height: 36px; padding: 0 var(--sp-3); border-radius: var(--r-md);
  border: 1px solid var(--border-subtle); background: var(--bg-inset); color: var(--text-primary);
  font-size: var(--fs-base); cursor: pointer;
  transition: border-color var(--mo-1-state) var(--ease-1), background var(--mo-1-state) var(--ease-1);
}
.qio-combo-trigger:hover { border-color: var(--border-strong); }
.qio-combo-trigger:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
.qio-combo.disabled .qio-combo-trigger { opacity: .55; cursor: default; }
.qio-combo-value.empty { color: var(--text-muted); }
.chev { color: var(--text-muted); flex: none; transition: transform var(--mo-1-state) var(--ease-1); }
.qio-combo.open .chev { transform: rotate(180deg); }
.qio-combo-menu {
  position: absolute; z-index: 20; left: 0; right: 0; top: calc(100% + 4px);
  background: var(--bg-elevated); border: 1px solid var(--border-strong); border-radius: var(--r-md);
  box-shadow: var(--elev-floating); overflow: hidden;
  animation: combo-in var(--mo-2-in) var(--ease-2) both;
}
@keyframes combo-in { from { opacity: 0; transform: translateY(calc(var(--shift-2) * -1)); } to { opacity: 1; transform: none; } }
.qio-combo-search {
  width: 100%; height: 32px; padding: 0 var(--sp-3); border: none; outline: none;
  border-bottom: 1px solid var(--border-subtle); background: var(--bg-inset); color: var(--text-primary);
  font-family: var(--sans); font-size: var(--fs-sm);
}
.qio-combo-list { list-style: none; margin: 0; padding: var(--sp-1); max-height: 260px; overflow: auto; }
.qio-combo-opt {
  display: flex; align-items: baseline; gap: var(--sp-2); justify-content: space-between;
  padding: 6px var(--sp-3); border-radius: var(--r-sm); cursor: pointer; font-size: var(--fs-sm); color: var(--text-primary);
}
.qio-combo-opt.hl { background: var(--layer-hover); }
.qio-combo-opt.sel { color: var(--text-strong); background: var(--layer-selected); }
.opt-hint { font-size: var(--fs-xs); color: var(--text-muted); text-align: right; }
.qio-combo-empty { padding: var(--sp-3); color: var(--text-muted); font-size: var(--fs-sm); }
</style>
