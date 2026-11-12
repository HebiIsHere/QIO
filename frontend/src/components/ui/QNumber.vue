<script setup lang="ts">
/**
 * QNumber —— 自定义数字步进器（设计系统 v2）：
 * 隐藏原生 spinner，提供 − / + 按钮 + 数值输入；步进按 step、clamp min/max。
 * emit：update:modelValue(number|null)、change（Enter/失焦提交，透传给父级保存）。
 *
 * 必填语义（设置页的保留天数 / 间隔 / 条数 / 预算都是必填数字）：
 * - 清空只是编辑过程，**不会** emit null（父级也就不会把空值当 0 保存）；
 *   离开输入框时提示「必填」并把显示恢复为当前值；
 * - 非数字（"abc" / "1e"）与低于下限（"-1"）不提交、不覆盖父级值，显示可读提示；
 * - 高于上限仍按既有步进器契约收口到上限后提交；
 * - 显式的 0 是合法值（min<=0 时），必须原样提交，而不是被当成「空」。
 * 可选语义（required=false，审批预算 / 凭据用量上限用）：保持旧契约 —— 空 = null，
 * 由调用方决定「不限」，其它调用方的行为不变。
 */
import { ref, watch } from "vue";
import QIcon from "./QIcon.vue";

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
    /** 必填：空值不提交、非法输入不提交，显示可读提示（默认 false＝旧契约） */
    required?: boolean;
  }>(),
  { min: -Infinity, max: Infinity, step: 1, placeholder: "", mono: false, label: "", disabled: false, unit: "", required: false },
);
const emit = defineEmits<{ "update:modelValue": [value: number | null]; change: [] }>();

let hintUid = 0;
const hintId = "q-number-hint-" + (hintUid += 1) + "-" + Math.random().toString(36).slice(2, 7);
const hint = ref("");

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
function parseDraft(): { kind: "empty" } | { kind: "invalid" } | { kind: "number"; value: number } {
  const raw = draft.value.trim();
  if (raw === "") return { kind: "empty" };
  // Number("1e") 是 NaN、Number("0x10") 是 16：只接受普通的十进制写法，
  // 避免把用户看不懂的「科学计数/十六进制」悄悄当成合法值保存。
  if (!/^[+-]?\d+(?:\.\d+)?$/.test(raw)) return { kind: "invalid" };
  const n = Number(raw);
  if (!Number.isFinite(n)) return { kind: "invalid" };
  return { kind: "number", value: n };
}

/** 把当前值写回输入框（父级拒绝这次输入时的「最近值」显示） */
function restoreCurrent(): void {
  draft.value = props.modelValue == null ? "" : String(props.modelValue);
  if (draft.value === "") {
    // 父级当前没有值（尚未读取）：保持空白，不伪造一个 0
    draft.value = "";
  }
}

/** 把当前 draft 收口成提交值（空 → null；非法 → null；否则 step + clamp） */
function normalize(): number | null {
  const parsed = parseDraft();
  if (parsed.kind !== "number") return null;
  return clamp(toStep(parsed.value));
}

function commit() {
  const parsed = parseDraft();
  if (parsed.kind === "empty") {
    if (props.required) {
      // 清空只是编辑过程：离开时提示必填，并回显当前值（不是 0，也不写库）
      hint.value = "这项必填，已恢复为当前值";
      restoreCurrent();
      return;
    }
    const next: null = null;
    draft.value = "";
    if (next === committed) return;
    committed = next;
    echoed = undefined;
    hint.value = "";
    emit("update:modelValue", next);
    emit("change");
    return;
  }
  if (parsed.kind === "invalid") {
    if (props.required) {
      hint.value = "只能填写数字，已恢复为当前值";
      restoreCurrent();
      return;
    }
    // 旧契约：非法文本按「清空」处理（审批预算等调用方自己兜底）
    const next: null = null;
    draft.value = "";
    if (next === committed) return;
    committed = next;
    echoed = undefined;
    hint.value = "";
    emit("update:modelValue", next);
    emit("change");
    return;
  }
  if (parsed.value < props.min) {
    if (props.required) {
      // 低于下限不按「收口到下限」提交：把 -1 天存成 0（永久保留）会改变语义，
      // 用户并没有表达这个意思。提示范围并回显当前值。
      hint.value = "不能小于 " + props.min + "，已恢复为当前值";
      restoreCurrent();
      return;
    }
  }
  const next = normalize();
  draft.value = next == null ? "" : String(next);
  hint.value = "";
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
  hint.value = "";
  echoed = undefined;
  emit("update:modelValue", next);
  // +/- 与 Enter/blur 用同一套 commit 语义：确实变化才算一次提交
  if (next !== committed) {
    committed = next;
    emit("change");
  }
}
function stepBase(): number {
  const parsed = parseDraft();
  if (parsed.kind === "number") return parsed.value;
  if (props.modelValue != null) return props.modelValue;
  return Number.isFinite(props.min) ? props.min : 0;
}
function onInput(e: Event) {
  draft.value = (e.target as HTMLInputElement).value;
  hint.value = "";
  const parsed = parseDraft();
  if (parsed.kind === "empty") {
    if (!props.required) {
      echoed = null;
      emit("update:modelValue", null);
    }
    return;
  }
  if (parsed.kind === "invalid") return;
  // 必填模式下不把「低于下限」的中间输入交给父级：否则父级会先拿到 -1，
  // 失焦时回显的「当前值」就变成了 -1，界面会短暂显示一个未被接受的数字。
  if (props.required && parsed.value < props.min) return;
  echoed = parsed.value;
  emit("update:modelValue", parsed.value); // 输入过程不 clamp，失焦/change 再收口
}
</script>

<template>
  <div class="q-number-field">
    <div class="q-number" :class="{ mono }" data-qio-fit>
      <input
        class="q-number-input"
        :value="draft"
        inputmode="numeric"
        :placeholder="placeholder"
        :disabled="disabled"
        :aria-label="label"
        :aria-invalid="hint ? 'true' : undefined"
        :aria-describedby="hint ? hintId : undefined"
        @input="onInput"
        @keydown.up.prevent="stepBy(1)"
        @keydown.down.prevent="stepBy(-1)"
        @keydown.enter.prevent="commit"
        @blur="commit"
        @change="commit"
      />
      <span v-if="unit" class="q-number-unit">{{ unit }}</span>
      <div class="q-number-steps">
        <button type="button" class="step up" :disabled="disabled" aria-label="增加" @click="stepBy(1)">
          <QIcon name="plus" :size="12" />
        </button>
        <button type="button" class="step down" :disabled="disabled" aria-label="减少" @click="stepBy(-1)">
          <QIcon name="minus" :size="12" />
        </button>
      </div>
    </div>
    <p v-if="hint" :id="hintId" class="q-number-hint" role="alert">{{ hint }}</p>
  </div>
</template>

<style scoped>
/* 外层只负责「输入框 + 可读提示」的纵向排布；输入框本身仍是同一套尺寸与令牌。 */
.q-number-field {
  display: inline-flex;
  flex-direction: column;
  gap: 4px;
  max-width: 100%;
  min-width: 0;
}
.q-number {
  display: inline-flex;
  align-items: stretch;
  height: 38px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--qio-ui-radius);
  background: var(--bg-inset);
  overflow: hidden;
  transition: border-color var(--mo-1-state) var(--ease-1), box-shadow var(--mo-1-state) var(--ease-1),
    background var(--mo-1-state) var(--ease-1);
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
/* 非法输入：边框只做可读性提示，提示文字同时用语义色与文字说明（不只靠颜色） */
.q-number-field:has(.q-number-input[aria-invalid="true"]) .q-number {
  border-color: var(--warning);
}
.q-number-hint {
  margin: 0;
  font-size: 11px;
  line-height: 1.5;
  color: var(--warning);
  overflow-wrap: anywhere;
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
  transition: background var(--mo-1-state) var(--ease-1), color var(--mo-1-state) var(--ease-1);
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
