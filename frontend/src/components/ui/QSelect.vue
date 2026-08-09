<script setup lang="ts">
import { ref, computed, watch, onUnmounted } from "vue";

const props = defineProps<{
  options: { value: string; label: string }[];
  modelValue?: string;
  disabled?: boolean;
}>();
const emit = defineEmits<{ "update:modelValue": [string] }>();

const open = ref(false);
const hl = ref(-1); // 当前高亮项索引
const root = ref<HTMLElement | null>(null);
const val = computed(() => props.options.find(o => o.value === props.modelValue)?.label ?? "");

function openMenu() {
  if (props.disabled || props.options.length === 0) return;
  open.value = true;
  hl.value = Math.max(0, props.options.findIndex(o => o.value === props.modelValue));
}
function toggle() {
  if (props.disabled) return;
  if (open.value) open.value = false;
  else openMenu();
}
function pick(v: string) {
  emit("update:modelValue", v);
  open.value = false;
}

function onDocPointer(e: PointerEvent) {
  if (!root.value?.contains(e.target as Node)) open.value = false;
}
watch(open, v => {
  if (v) document.addEventListener("pointerdown", onDocPointer);
  else document.removeEventListener("pointerdown", onDocPointer);
});
onUnmounted(() => document.removeEventListener("pointerdown", onDocPointer));

function onKeydown(e: KeyboardEvent) {
  if (props.disabled) return;
  if (e.key === "Escape") { open.value = false; return; }
  if (props.options.length === 0) return; // 空选项不响应方向/确认键
  const len = props.options.length;
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    if (!open.value) { openMenu(); return; }
    hl.value = (hl.value + (e.key === "ArrowDown" ? 1 : -1) + len) % len;
  } else if (e.key === "Enter") {
    e.preventDefault();
    if (open.value) {
      if (hl.value >= 0) pick(props.options[hl.value].value);
      else open.value = false;
    } else {
      openMenu();
    }
  } else if (e.key === " ") {
    e.preventDefault();
    toggle();
  }
}
</script>
<template>
  <div ref="root" class="qio-select" tabindex="0" :class="{ open, disabled }" role="combobox"
       aria-haspopup="listbox" :aria-expanded="open"
       :aria-disabled="disabled || undefined"
       :aria-activedescendant="open && hl >= 0 && options[hl] ? 'qio-opt-' + options[hl].value : undefined"
       @click="toggle" @keydown="onKeydown">
    <span class="qio-select-val">{{ val }}</span>
    <svg class="chev" width="14" height="14" viewBox="0 0 16 16" fill="none"><path d="M4 6l4 4 4-4" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>
    <div class="qio-select-menu" role="listbox" @click.stop>
      <div v-for="(o, i) in options" :key="o.value" :id="'qio-opt-' + o.value"
           class="opt" :class="{ sel: o.value === modelValue, hl: i === hl }"
           role="option" :aria-selected="o.value === modelValue ? 'true' : 'false'"
           @click.stop="pick(o.value)" @mouseenter="hl = i">{{ o.label }}</div>
    </div>
  </div>
</template>
