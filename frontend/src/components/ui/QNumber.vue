<script setup lang="ts">
/**
 * QNumber —— 自定义数字步进器（设计系统 v2）：
 * 隐藏原生 spinner，提供 − / + 按钮 + 数值输入；步进按 step、clamp min/max。
 * emit：update:modelValue(number|null)、change（Enter/失焦提交，透传给父级保存）。
 */
import { ref, watch } from "vue";

const props = withDefaults(
  defineProps<{
    modelValue: number | null;
    min?: number;
    max?: number;
    step?: number;
    placeholder?: string;
    mono?: boolean;
    label?: string;
    disabled?: boolean;
    /** 数值单位（如「小时」）。显示在数值右侧，让用户不必回头找标题里的单位 */
    unit?: string;
  }>(),
  { min: -Infinity, max: Infinity, step: 1, placeholder: "", mono: false, label: "", disabled: false, unit: "" },
);
const emit = defineEmits<{ "update:modelValue": [value: number | null]; change: [] }>();

const draft = ref(props.modelValue == null ? "" : String(props.modelValue));
/**
 * 最近一次已交付给父级的值。change 只在「值真的变了」时发出：
 * 点击 + → commit（一次保存）→ 失焦不再重复；纯聚焦点击也不触发保存。
 */
let committed: number | null = props.modelValue ?? null;
/**
 * 最近一次由本组件输入框 emit 出去的值。
 * 父组件回填（`:model-value` + `@update:model-value` 的真实双向绑定）会带着
 * 这个值回到 props；它只是「用户正在打的字」的回显，不代表已经提交，
 * 否则失焦时的 commit 会判定「没变化」而发不出 change，父级就不会保存。
 */
let echoed: number | null | undefined;
watch(
  () => props.modelValue,
  (v) => {
    if (draft.value !== String(v ?? "")) draft.value = v == null ? "" : String(v);
    if (v === echoed) {
      echoed = undefined; // 自己的回显：不改变 committed
      return;
    }
    // 父级/服务端真正回填的值视为已提交，不重复触发保存
    committed = v ?? null;
  },
);

function clamp(v: number): number {
  return Math.min(props.max, Math.max(props.min, v));
}
function toStep(v: number): number {
  const s = props.step;
  const r = Math.round(v / s) * s;
  return Number(r.toFixed(10)); // 去浮点误差（0.30000000000000004）
}
/** 把当前 draft 收口成提交值（空 → null；非法 → null；否则 step + clamp） */
function normalize(): number | null {
  const raw = draft.value.trim();
  if (raw === "") return null;
  const n = Number(raw);
  return Number.isFinite(n) ? clamp(toStep(n)) : null;
}
function commit() {
  const next = normalize();
  draft.value = next == null ? "" : String(next);
  if (next === committed) return;
  committed = next;
  echoed = undefined;
  emit("update:modelValue", next);
  emit("change");
}
function stepBy(dir: 1 | -1) {
  if (props.disabled) return;
  // 以「输入框当前显示的值」为步进基准：父级 prop 回填是异步的，
  // 连续点击 +/- 必须累加（否则第二次点击会从旧值重新计算）。
  const base = stepBase();
  const next = clamp(toStep(base + dir * props.step));
  draft.value = String(next);
  echoed = undefined;
  emit("update:modelValue", next);
  // +/- 与 Enter/blur 用同一套 commit 语义：确实变化才算一次提交
  if (next !== committed) {
    committed = next;
    emit("change");
  }
}
function stepBase(): number {
  const raw = draft.value.trim();
  const n = Number(raw);
  if (raw !== "" && Number.isFinite(n)) return n;
  if (props.modelValue != null) return props.modelValue;
  return Number.isFinite(props.min) ? props.min : 0;
}
function onInput(e: Event) {
  draft.value = (e.target as HTMLInputElement).value;
  const raw = draft.value.trim();
  if (raw === "") {
    echoed = null;
    emit("update:modelValue", null);
  }
  else {
    const n = Number(raw);
    if (Number.isFinite(n)) {
      echoed = n;
      emit("update:modelValue", n); // 输入过程不 clamp，失焦/change 再收口
    }
  }
}
</script>

<template>
  <div class="q-number" :class="{ mono }">
    <input
      class="q-number-input"
      :value="draft"
      inputmode="numeric"
      :placeholder="placeholder"
      :disabled="disabled"
      :aria-label="label"
      @input="onInput"
      @keydown.up.prevent="stepBy(1)"
      @keydown.down.prevent="stepBy(-1)"
      @keydown.enter.prevent="commit"
      @blur="commit"
      @change="commit"
    />
    <span v-if="unit" class="q-number-unit">{{ unit }}</span>
    <div class="q-number-steps">
      <button type="button" class="step up" :disabled="disabled" aria-label="增加" @click="stepBy(1)">＋</button>
      <button type="button" class="step down" :disabled="disabled" aria-label="减少" @click="stepBy(-1)">−</button>
    </div>
  </div>
</template>

<style scoped>
.q-number {
  display: inline-flex;
  align-items: stretch;
  height: 38px;
  border: 1px solid var(--border-subtle);
  border-radius: 10px;
  background: var(--bg-inset);
  overflow: hidden;
  transition: border-color 0.18s, box-shadow 0.18s, background 0.18s;
}
.q-number:focus-within {
  border-color: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-soft);
  background: var(--bg-surface);
}
.q-number.mono .q-number-input {
  font-family: var(--mono);
  font-size: 13px;
  letter-spacing: 0.02em;
}
.q-number-input {
  flex: 1;
  min-width: 0;
  width: 56px;
  padding: 0 10px;
  border: none;
  background: transparent;
  color: var(--text-strong);
  font-size: 14px;
  line-height: 1.5;
  outline: none;
}
.q-number-input::placeholder {
  color: var(--text-muted);
}
.q-number-input:disabled {
  opacity: 0.5;
}
.q-number-steps {
  display: flex;
  flex-direction: column;
  border-left: 1px solid var(--border-subtle);
}
/* 单位紧贴数值右侧：读「24 小时」不用回头看分区标题 */
.q-number-unit {
  display: inline-flex;
  align-items: center;
  padding-right: 8px;
  color: var(--text-muted);
  font-size: 12px;
  white-space: nowrap;
}
.step {
  flex: 1;
  /* 触屏命中：22px 太窄（手指点不准），放宽到 30px；键盘操作不受影响 */
  width: 30px;
  padding: 0;
  border: none;
  background: transparent;
  color: var(--text-secondary);
  font-size: 11px;
  line-height: 1;
  cursor: pointer;
  touch-action: manipulation;
  transition: background 0.15s, color 0.15s;
}
.step:hover:not(:disabled) {
  background: var(--accent-soft);
  color: var(--accent);
}
.step:disabled {
  opacity: 0.4;
  cursor: default;
}
.step.up {
  border-bottom: 1px solid var(--border-subtle);
}
</style>
