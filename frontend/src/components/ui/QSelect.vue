<script setup lang="ts">
/**
 * QSelect —— 短列表选择器（自定义外观，不用原生 select）。
 *
 * 本轮补充（视觉/状态统一）：
 * - 圆角统一 var(--qio-ui-radius)（触发框与菜单，见 base.css 的 .qio-select 原语）；
 * - 选中标记用 QIcon(check)，不用 Unicode ✓；
 * - 空状态可读：无选中时显示 placeholder，无可选项时显示 emptyText（弱化色）；
 * - 处理中（busy）不展开菜单，但保留当前值；
 * - 键盘完整：↑↓ / Home / End / Enter / Space / Esc / Tab（Tab 收起并继续走焦点）。
 */
import { computed, ref, watch, onUnmounted } from "vue";
import { vQioScrollbar } from "../../composables/useQioScrollbars";
import QIcon from "./QIcon.vue";

const props = withDefaults(
  defineProps<{
    options: { value: string; label: string }[];
    modelValue?: string;
    disabled?: boolean;
    busy?: boolean;
    placeholder?: string;
    emptyText?: string;
  }>(),
  { modelValue: "", disabled: false, busy: false, placeholder: "", emptyText: "暂无选项" },
);
const emit = defineEmits<{ "update:modelValue": [string] }>();

const open = ref(false);
const hl = ref(-1); // 当前高亮项索引
const root = ref<HTMLElement | null>(null);
const selectedLabel = computed(
  () => props.options.find((o) => o.value === props.modelValue)?.label ?? "",
);
const displayText = computed(() => {
  if (props.options.length === 0) return props.emptyText;
  return selectedLabel.value || props.placeholder;
});
const blocked = computed(() => props.disabled || props.busy);

function openMenu() {
  if (blocked.value || props.options.length === 0) return;
  open.value = true;
  hl.value = Math.max(0, props.options.findIndex((o) => o.value === props.modelValue));
}
function toggle() {
  if (blocked.value) return;
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
  if (blocked.value) return;
  if (e.key === "Escape") { open.value = false; return; }
  if (e.key === "Tab") {
    // Tab 只收起菜单，不吞掉焦点移动（键盘用户继续去下一个控件）
    open.value = false;
    return;
  }
  if (props.options.length === 0) return; // 空选项不响应方向/确认键
  const len = props.options.length;
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    if (!open.value) { openMenu(); return; }
    hl.value = (hl.value + (e.key === "ArrowDown" ? 1 : -1) + len) % len;
  } else if (e.key === "Home" || e.key === "End") {
    if (!open.value) return;
    e.preventDefault();
    hl.value = e.key === "Home" ? 0 : len - 1;
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
    if (open.value && hl.value >= 0) pick(props.options[hl.value].value);
    else toggle();
  }
}
</script>
<template>
  <div ref="root" class="qio-select" tabindex="0" :class="{ open, disabled, busy }" role="combobox"
       aria-haspopup="listbox" :aria-expanded="open"
       :aria-disabled="disabled || undefined"
       :aria-busy="busy || undefined"
       :aria-activedescendant="open && hl >= 0 && options[hl] ? 'qio-opt-' + options[hl].value : undefined"
       @click="toggle" @keydown="onKeydown">
    <span class="qio-select-val" :class="{ empty: !selectedLabel }">{{ displayText }}</span>
    <QIcon class="chev" name="chevron-down" :size="14" />
    <div
      class="qio-select-menu"
      role="listbox"
      v-qio-scrollbar="{ axis: 'y' }"
      data-qio-fit
      @click.stop
    >
      <div v-for="(o, i) in options" :key="o.value" :id="'qio-opt-' + o.value"
           class="opt" :class="{ sel: o.value === modelValue, hl: i === hl }"
           role="option" :aria-selected="o.value === modelValue ? 'true' : 'false'"
           @click.stop="pick(o.value)" @mouseenter="hl = i">
        <span class="opt-label">{{ o.label }}</span>
        <QIcon v-if="o.value === modelValue" name="check" :size="14" />
      </div>
    </div>
  </div>
</template>

<style scoped>
/* 下拉浮层（契约：菜单列表滚动走统一指示条；不出现原生滚动条、不出现横向滚动）。
   这里显式重述 position:absolute —— 指令会给宿主补 .qio-scroll-host{position:relative}，
   而 base.css 的 .qio-select-menu 与它同权重，必须由本组件锁死浮层定位。
   宽度收进可用区域（不再靠 min-width:max-content 撑宽），标签换行而不是横向溢出。 */
.qio-select-menu {
  position: absolute;
  min-width: 0;
  max-width: min(420px, calc(100vw - 2 * var(--sp-3)));
  max-height: min(320px, 60vh);
  overflow-y: auto;
  overscroll-behavior: contain;
}
.qio-select-menu .opt {
  min-width: 0;
  white-space: normal;
  overflow-wrap: anywhere;
}
.qio-select-menu .opt .opt-label {
  min-width: 0;
  overflow-wrap: anywhere;
}
/* 空状态弱化：placeholder / 无选项 */
.qio-select-val.empty {
  color: var(--text-muted);
}
/* 处理中：保留值，光标提示，不展开 */
.qio-select.busy {
  cursor: progress;
}
@media (max-width: 480px) {
  .qio-select-menu {
    max-width: calc(100vw - 2 * var(--sp-2));
  }
}
</style>
