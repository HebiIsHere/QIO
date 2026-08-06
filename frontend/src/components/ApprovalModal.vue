<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { useApprovalsStore } from "../stores/approvals";

const approvals = useApprovalsStore();
const item = computed(() => approvals.current);

const KIND_LABELS: Record<string, string> = {
  tool_create: "工具创建审批",
  credential_grant: "凭据授权审批",
  high_impact_knowledge: "高影响知识确认",
};

const isSubagentCreate = computed(() => {
  const p = (item.value?.payload ?? {}) as Record<string, unknown>;
  return item.value?.kind === "tool_create" && p.tool_type === "subagent";
});

// 子 agent 预算（编辑后批准时随 overrides 提交）
const budgetForm = ref({
  max_iterations: 5,
  max_tokens: 100000,
  output_limit_chars: 2000,
});
watch(
  () => item.value?.approval_id,
  () => {
    const p = (item.value?.payload ?? {}) as Record<string, unknown>;
    const b = (p.subagent_budget ?? {}) as Record<string, unknown>;
    budgetForm.value = {
      max_iterations: Number(b.max_iterations ?? 5),
      max_tokens: Number(b.max_tokens ?? 100000),
      output_limit_chars: Number(b.output_limit_chars ?? 2000),
    };
  },
  { immediate: true },
);

function approveWithBudget() {
  approvals.respond("approved", {
    subagent_budget: {
      max_iterations: budgetForm.value.max_iterations,
      max_tokens: budgetForm.value.max_tokens,
      output_limit_chars: budgetForm.value.output_limit_chars,
    },
  });
}

const payloadView = computed(() => {
  const p = (item.value?.payload ?? {}) as Record<string, unknown>;
  const lines: { label: string; value: string }[] = [];
  if (p.explanation) lines.push({ label: "创建解释", value: String(p.explanation) });
  if (p.name) lines.push({ label: "工具", value: String(p.name) });
  if (p.description) lines.push({ label: "描述", value: String(p.description) });
  if (p.tool_type) {
    const typeLabel = p.tool_type === "subagent" ? "子 agent 型" : "函数型";
    lines.push({ label: "类型", value: typeLabel });
  }
  if (p.test_summary) lines.push({ label: "测试", value: String(p.test_summary) });
  if (p.key_id) lines.push({ label: "凭据", value: String(p.key_id) });
  if (p.tool_name) lines.push({ label: "授权工具", value: String(p.tool_name) });
  if (p.content) lines.push({ label: "知识内容", value: String(p.content) });
  if (p.action) lines.push({ label: "建议动作", value: String(p.action) });
  if (p.reason) lines.push({ label: "原因", value: String(p.reason) });
  if (p.example) lines.push({ label: "示例请求", value: String(p.example) });
  if (p.source_count) lines.push({ label: "相似请求数", value: String(p.source_count) });
  // 技术明细（代码/测试细节）默认不展示，用户只需看用途与测试摘要
  return lines;
});
</script>

<template>
  <div v-if="item" class="modal-mask">
    <div class="modal">
      <h3>{{ KIND_LABELS[item.kind] ?? item.kind }}</h3>
      <div class="body">
        <div v-for="line in payloadView" :key="line.label" class="row">
          <span class="label">{{ line.label }}</span>
          <span class="value">{{ line.value }}</span>
        </div>
        <p v-if="!payloadView.length" class="hint">无附加信息</p>
        <div v-if="isSubagentCreate" class="budget-box">
          <div class="budget-title">子 agent 执行预算（可修改后批准）</div>
          <div class="budget-row">
            <label>最大迭代</label>
            <input v-model.number="budgetForm.max_iterations" type="number" min="1" max="50" />
            <label>最大 token</label>
            <input v-model.number="budgetForm.max_tokens" type="number" min="1000" step="1000" />
            <label>输出上限(字)</label>
            <input v-model.number="budgetForm.output_limit_chars" type="number" min="100" />
          </div>
        </div>
      </div>
      <div class="actions">
        <button class="reject" :disabled="!!approvals.responding" @click="approvals.respond('rejected')">
          拒绝
        </button>
        <button
          v-if="isSubagentCreate"
          class="approve"
          :disabled="!!approvals.responding"
          @click="approveWithBudget"
        >
          批准（含预算）
        </button>
        <button v-else class="approve" :disabled="!!approvals.responding" @click="approvals.respond('approved')">
          批准
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.modal-mask {
  position: fixed; inset: 0; z-index: 200; background: var(--bg-overlay);
  display: flex; align-items: center; justify-content: center;
}
.modal {
  width: 480px; max-width: 90vw; background: var(--bg-elevated); border: 1px solid var(--border-strong);
  border-radius: 14px; padding: 18px 20px; color: var(--text-primary);
}
.modal h3 { margin: 0 0 12px; font-size: 16px; color: var(--text-strong); }
.body { max-height: 320px; overflow-y: auto; }
.row { display: flex; gap: 10px; margin-bottom: 8px; font-size: 13px; }
.label { color: var(--text-secondary); min-width: 64px; flex-shrink: 0; }
.value { color: var(--text-primary); }
.hint { color: var(--text-muted); font-size: 12px; }
.budget-box { margin-top: 10px; padding: 10px; border: 1px solid var(--border-subtle); border-radius: 8px; }
.budget-title { font-size: 12px; color: var(--text-secondary); margin-bottom: 8px; }
.budget-row { display: flex; align-items: center; gap: 8px; font-size: 12px; flex-wrap: wrap; }
.budget-row label { color: var(--text-secondary); }
.budget-row input { width: 90px; padding: 4px 6px; border: 1px solid var(--border-strong); border-radius: 6px; background: var(--bg-base); color: var(--text-primary); }
.actions { display: flex; justify-content: flex-end; gap: 10px; margin-top: 16px; }
.actions button { border: none; border-radius: 18px; padding: 8px 26px; cursor: pointer; font-size: 13px; }
.approve { background: var(--approve-bg); color: var(--on-accent); }
.reject { background: var(--reject-bg); color: var(--on-accent); }
.actions button:disabled { opacity: 0.5; }
</style>