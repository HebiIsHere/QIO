<script setup lang="ts">
/**
 * 审批事实（契约 §1.3）：**唯一**的一份展示。
 *
 * ApprovalModal（完整窗口）与 TurnProcess 的内联卡共用这个组件 ——
 * 同一 approval 在两处说同样的话，禁止各自一套显示规则。
 *
 * 固定展示：
 *   1. 它想做什么（系统 description）；
 *   2. 模型 explanation（单独成段、标注来源；与 description **分别保留**）；
 *   3. 真实操作事实：命令 / 路径 / 工具参数（长内容折叠但入口明确）；
 *   4. 会改变什么 / 为什么需要 / 授权范围 / 验证情况（拿不到就不显示）；
 *   5. 高级详情（默认折叠）。
 *
 * 颜色一律走 `var(--*)` 设计令牌。
 */
import { computed, reactive, watch } from "vue";
import QNumber from "./ui/QNumber.vue";
import type { ApprovalItem, ApprovalBudget } from "../stores/approvals";
import { approvalFacts } from "../stores/approvals";

const props = defineProps<{
  item: ApprovalItem;
  /** 子 agent 预算的当前值（可修改后批准）；只在子 agent 型工具创建时有意义 */
  budget?: ApprovalBudget | null;
}>();

const emit = defineEmits<{ "update:budget": [ApprovalBudget] }>();

const facts = computed(() => approvalFacts(props.item));

/** 预算表单：本地编辑 + 同步回父组件（提交时随 overrides 带上） */
const budgetForm = reactive({ max_iterations: 5, max_tokens: 100000, output_limit_chars: 2000 });

watch(
  () => [
    props.item?.approval_id,
    props.budget?.maxIterations,
    props.budget?.maxTokens,
    props.budget?.outputLimitChars,
  ],
  () => {
    const source = props.budget ?? facts.value?.budget ?? null;
    budgetForm.max_iterations = source?.maxIterations ?? 5;
    budgetForm.max_tokens = source?.maxTokens ?? 100000;
    budgetForm.output_limit_chars = source?.outputLimitChars ?? 2000;
  },
  { immediate: true },
);

function syncBudget() {
  emit("update:budget", {
    maxIterations: Number(budgetForm.max_iterations) || 5,
    maxTokens: Number(budgetForm.max_tokens) || 100000,
    outputLimitChars: Number(budgetForm.output_limit_chars) || 2000,
  });
}
</script>

<template>
  <div v-if="facts" class="approval-facts" data-test="approval-facts" :data-kind="facts.kind">
    <!-- 1) 它想做什么：系统事实 -->
    <p class="intent">{{ facts.description }}</p>

    <!-- 2) 它会访问什么：具体行为标签（不是 Low/Medium/High） -->
    <div v-if="facts.risks.length" class="risk-row" data-test="approval-risks">
      <span v-for="r in facts.risks" :key="r" class="risk qio-state warn">{{ r }}</span>
    </div>

    <!-- 3) QIO 的说明（模型生成，单独成段并标注来源；缺失时不渲染） -->
    <div v-if="facts.explanation" class="qio-explanation" data-test="approval-explanation">
      <div class="tag mono">◈ QIO 的说明</div>
      <p>{{ facts.explanation }}</p>
    </div>

    <!-- 4) 会改变什么 / 为什么需要 / 授权范围 / 验证了吗 -->
    <dl class="facts">
      <template v-if="facts.changeSummary">
        <dt>会改变什么</dt>
        <dd>{{ facts.changeSummary }}</dd>
      </template>
      <template v-if="facts.why">
        <dt>为什么需要</dt>
        <dd>{{ facts.why }}</dd>
      </template>
      <dt>授权范围</dt>
      <dd data-test="approval-scope">{{ facts.scopeLabel }}</dd>
      <template v-if="facts.verification">
        <dt>验证情况</dt>
        <dd>
          <span class="verdict qio-state" :class="facts.verification.verified ? 'ok' : 'warn'">
            {{ facts.verification.label }}
          </span>
          <span v-if="facts.verification.detail" class="verdict-detail">{{ facts.verification.detail }}</span>
        </dd>
      </template>
    </dl>

    <div v-for="line in facts.rows" :key="line.label" class="row">
      <span class="label">{{ line.label }}</span>
      <span class="value">{{ line.value }}</span>
    </div>
    <p v-if="!facts.rows.length && !facts.capabilities.length" class="hint">无附加信息</p>

    <div v-if="facts.access.length || facts.capabilityList.length" class="cap-box">
      <div class="cap-title">它会访问什么</div>
      <ul class="cap-list" data-test="approval-access">
        <template v-if="facts.access.length">
          <li v-for="a in facts.access" :key="a">{{ a }}</li>
        </template>
        <template v-else>
          <li v-for="c in facts.capabilityList" :key="c">{{ c }}</li>
        </template>
      </ul>
    </div>

    <!-- 5) 真实操作事实：命令 / 路径（长技术明细折叠，但入口明确写出来） -->
    <div v-if="facts.command || facts.paths.length || facts.detail" class="af-ops" data-test="approval-operation">
      <div class="af-ops-title">这次会实际执行什么</div>
      <p v-if="facts.command" class="af-op">
        <span class="af-op-label mono">命令</span>
        <code class="af-op-value mono" data-test="approval-command">{{ facts.command }}</code>
      </p>
      <p v-if="facts.paths.length" class="af-op">
        <span class="af-op-label mono">路径</span>
        <span class="af-op-value mono" data-test="approval-paths">{{ facts.paths.join(" · ") }}</span>
      </p>
      <details v-if="facts.detail && facts.detail !== facts.command" class="af-detail" data-test="approval-detail">
        <summary>完整技术明细</summary>
        <pre class="af-pre mono">{{ facts.detail }}</pre>
      </details>
    </div>

    <!-- 工具参数：默认折叠，但入口写清楚是「完整参数」 -->
    <details v-if="facts.params" class="af-params" data-test="approval-params">
      <summary>工具参数（完整）</summary>
      <pre class="af-pre mono">{{ facts.params }}</pre>
    </details>

    <!-- 6) 高级详情：策略指纹 / 逐条测试结果 / raw params，默认折叠 -->
    <details v-if="facts.advanced.lines.length || facts.advanced.raw" class="adv" data-test="approval-advanced">
      <summary>高级详情</summary>
      <ul v-if="facts.advanced.lines.length" class="adv-list">
        <li v-for="l in facts.advanced.lines" :key="l">{{ l }}</li>
      </ul>
      <pre class="adv-raw mono">{{ facts.advanced.raw }}</pre>
    </details>

    <!-- 子 agent 执行预算：可改后批准（内联卡与弹窗都有这个入口） -->
    <div v-if="facts.budget" class="budget-box">
      <div class="budget-title">子 agent 执行预算（可修改后批准）</div>
      <div class="budget-row">
        <label>最大迭代</label>
        <QNumber v-model="budgetForm.max_iterations" :min="1" :max="50" mono label="最大迭代" @update:model-value="syncBudget" />
        <label>最大 token</label>
        <QNumber v-model="budgetForm.max_tokens" :min="1000" :step="1000" mono label="最大 token" @update:model-value="syncBudget" />
        <label>输出上限(字)</label>
        <QNumber v-model="budgetForm.output_limit_chars" :min="100" mono label="输出上限" @update:model-value="syncBudget" />
      </div>
    </div>
  </div>
</template>

<style scoped>
.approval-facts {
  font-family: var(--sans);
  color: var(--text-primary);
}
.intent {
  font-size: 13.5px;
  line-height: 1.6;
  color: var(--text-primary);
  margin: 0 0 8px;
}
.risk-row {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 10px;
}
.risk {
  font-family: var(--mono);
  letter-spacing: 0.03em;
}
/* QIO 的说明：模型文案，与系统事实在视觉上明确分开 */
.qio-explanation {
  margin: 10px 0 12px;
  padding: 10px 12px;
  border: 1px solid var(--accent-soft);
  background: var(--accent-softer);
  border-radius: var(--r-md);
}
.qio-explanation .tag {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 5px;
  font-size: 10px;
  letter-spacing: 0.08em;
  color: var(--link);
}
.qio-explanation p {
  margin: 0;
  font-size: 13px;
  line-height: 1.65;
  color: var(--text-primary);
}
.row {
  display: flex;
  gap: 10px;
  margin-bottom: 8px;
  font-size: 13px;
}
.label {
  color: var(--text-secondary);
  min-width: 64px;
  flex-shrink: 0;
}
.value {
  color: var(--text-primary);
  overflow-wrap: anywhere;
}
.hint {
  color: var(--text-muted);
  font-size: 12px;
}
.facts {
  margin: 0 0 10px;
  display: grid;
  grid-template-columns: 76px 1fr;
  gap: 4px 10px;
}
.facts dt {
  color: var(--text-secondary);
  font-size: 12.5px;
}
.facts dd {
  margin: 0;
  color: var(--text-primary);
  font-size: 12.5px;
  line-height: 1.5;
}
.verdict {
  font-family: var(--mono);
  letter-spacing: 0.03em;
}
.verdict-detail {
  color: var(--text-secondary);
  margin-left: 8px;
}
/* 真实操作事实：命令 / 路径用等宽字体，长内容可折行不撑破卡片 */
.af-ops {
  margin-top: 10px;
  padding: 10px;
  border: 1px solid var(--border-subtle);
  background: var(--bg-inset);
  border-radius: var(--r-md);
}
.af-ops-title {
  font-size: 12.5px;
  color: var(--text-strong);
  margin-bottom: 6px;
}
.af-op {
  display: flex;
  gap: 8px;
  margin: 0 0 4px;
  font-size: 12px;
  line-height: 1.6;
}
.af-op-label {
  flex: 0 0 auto;
  color: var(--text-muted);
  font-size: 11px;
}
.af-op-value {
  color: var(--text-primary);
  overflow-wrap: anywhere;
  word-break: break-word;
}
.af-detail,
.af-params {
  margin-top: 8px;
  border-top: 1px solid var(--border-subtle);
  padding-top: 6px;
}
.af-detail > summary,
.af-params > summary {
  cursor: pointer;
  font-size: 12px;
  color: var(--text-secondary);
  list-style: none;
  display: flex;
  align-items: center;
  gap: 6px;
}
.af-detail > summary::-webkit-details-marker,
.af-params > summary::-webkit-details-marker {
  display: none;
}
.af-detail > summary::before,
.af-params > summary::before {
  content: "›";
  display: inline-block;
  color: var(--text-muted);
  transition: transform var(--dur-toggle) var(--ease);
}
.af-detail[open] > summary::before,
.af-params[open] > summary::before {
  transform: rotate(90deg);
}
.af-detail > summary:focus-visible,
.af-params > summary:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 2px;
}
.af-pre {
  margin: 6px 0 0;
  padding: 8px 10px;
  max-height: 160px;
  overflow: auto;
  background: var(--bg-inset);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-sm);
  font-size: 11.5px;
  line-height: 1.5;
  color: var(--text-secondary);
  white-space: pre-wrap;
  word-break: break-all;
}
/* 高级详情：默认折叠，普通用户不第一眼看到 raw params */
.adv {
  margin-top: 10px;
  border-top: 1px solid var(--border-subtle);
  padding-top: 8px;
}
.adv > summary {
  cursor: pointer;
  font-size: 12.5px;
  color: var(--text-secondary);
  list-style: none;
  display: flex;
  align-items: center;
  gap: 6px;
}
.adv > summary::-webkit-details-marker {
  display: none;
}
.adv > summary::before {
  content: "›";
  display: inline-block;
  color: var(--text-muted);
  transition: transform var(--dur-toggle) var(--ease);
}
.adv[open] > summary::before {
  transform: rotate(90deg);
}
.adv > summary:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 2px;
}
.adv-list {
  margin: 6px 0 0;
  padding-left: 18px;
  font-size: 12px;
  color: var(--text-secondary);
}
.adv-raw {
  margin: 8px 0 0;
  padding: 8px 10px;
  max-height: 160px;
  overflow: auto;
  background: var(--bg-inset);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-sm);
  font-size: 11.5px;
  line-height: 1.5;
  color: var(--text-secondary);
  white-space: pre-wrap;
  word-break: break-all;
}
.cap-box {
  margin-top: 10px;
  padding: 10px;
  border: 1px solid var(--border-subtle);
  background: var(--bg-inset);
  border-radius: 8px;
}
.cap-title {
  font-size: 12.5px;
  color: var(--text-strong);
  margin-bottom: 6px;
}
.cap-list {
  margin: 0;
  padding-left: 18px;
  font-size: 12px;
  color: var(--text-secondary);
}
.cap-list li {
  margin: 2px 0;
  overflow-wrap: anywhere;
}
.budget-box {
  margin-top: 10px;
  padding: 10px;
  border: 1px solid var(--border-subtle);
  border-radius: 8px;
}
.budget-title {
  font-size: 12px;
  color: var(--text-secondary);
  margin-bottom: 8px;
}
.budget-row {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  flex-wrap: wrap;
}
.budget-row label {
  color: var(--text-secondary);
}
.budget-row .q-number {
  width: 108px;
}
</style>
