<script setup lang="ts">
import { computed } from "vue";
import { useApprovalsStore } from "../stores/approvals";

const approvals = useApprovalsStore();
const item = computed(() => approvals.current);

const KIND_LABELS: Record<string, string> = {
  tool_create: "工具创建审批",
  credential_grant: "凭据授权审批",
  high_impact_knowledge: "高影响知识确认",
};

const payloadView = computed(() => {
  const p = (item.value?.payload ?? {}) as Record<string, unknown>;
  const lines: { label: string; value: string }[] = [];
  if (p.explanation) lines.push({ label: "创建解释", value: String(p.explanation) });
  if (p.name) lines.push({ label: "工具", value: String(p.name) });
  if (p.description) lines.push({ label: "描述", value: String(p.description) });
  if (p.test_summary) lines.push({ label: "测试", value: String(p.test_summary) });
  if (p.key_id) lines.push({ label: "凭据", value: String(p.key_id) });
  if (p.tool_name) lines.push({ label: "授权工具", value: String(p.tool_name) });
  if (p.test_details && Array.isArray(p.test_details)) {
    lines.push({
      label: "测试明细",
      value: (p.test_details as { name: string; passed: boolean }[])
        .map((t) => `${t.passed ? "✓" : "✗"} ${t.name}`).join(" · "),
    });
  }
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
      </div>
      <div class="actions">
        <button class="reject" :disabled="!!approvals.responding" @click="approvals.respond('rejected')">
          拒绝
        </button>
        <button class="approve" :disabled="!!approvals.responding" @click="approvals.respond('approved')">
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
.actions { display: flex; justify-content: flex-end; gap: 10px; margin-top: 16px; }
.actions button { border: none; border-radius: 18px; padding: 8px 26px; cursor: pointer; font-size: 13px; }
.approve { background: var(--approve-bg); color: var(--on-accent); }
.reject { background: var(--reject-bg); color: var(--on-accent); }
.actions button:disabled { opacity: 0.5; }
</style>