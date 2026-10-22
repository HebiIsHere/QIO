<script setup lang="ts">
/**
 * 知识保存申请的正文（D）。
 *
 * 只展示后端真实给的事实：内容、来源、范围、确定程度、依据条数、类别、原因。
 * 拿不到就写「未记录」，不编造；「修改后保存」的编辑框只影响本次提交内容。
 */
import { computed } from "vue";
import {
  EPISTEMIC_LABELS,
  KNOWLEDGE_CATEGORY_LABELS,
  type KnowledgeApprovalItem,
} from "../../stores/unifiedApprovals";

const props = defineProps<{
  item: KnowledgeApprovalItem;
  editing: boolean;
  draft: string;
  busy: boolean;
}>();

const emit = defineEmits<{
  "update:draft": [value: string];
  edit: [];
  "cancel-edit": [];
  retry: [];
}>();

const facts = computed(() => {
  const item = props.item;
  return [
    { label: "来源", value: item.source || "未记录" },
    { label: "范围", value: item.scope.length ? item.scope.join(" · ") : "未记录" },
    {
      label: "确定程度",
      value: item.epistemicStatus
        ? EPISTEMIC_LABELS[item.epistemicStatus] ?? item.epistemicStatus
        : "未记录",
    },
    {
      label: "依据",
      value: item.evidenceIds.length ? item.evidenceIds.length + " 条依据" : "未记录",
    },
    {
      label: "类别",
      value: item.category ? KNOWLEDGE_CATEGORY_LABELS[item.category] ?? item.category : "未记录",
    },
  ];
});

const detailLoading = computed(
  () => props.item.detailStatus === "loading" && !props.item.scope.length && !props.item.epistemicStatus,
);
const detailFailed = computed(() => props.item.detailStatus === "failed");
</script>

<template>
  <div class="knowledge-approval-body" data-test="knowledge-approval-body">
    <p class="lead">
      QIO 想把这句话长期记住。确认之前它不会生效；保存后会影响之后的回答。
    </p>

    <template v-if="editing">
      <label class="edit-label" for="knowledge-approval-draft">修改要保存的内容</label>
      <textarea
        id="knowledge-approval-draft"
        class="edit-area"
        :value="draft"
        rows="4"
        :disabled="busy"
        aria-label="修改要保存的知识内容"
        @input="emit('update:draft', ($event.target as HTMLTextAreaElement).value)"
      ></textarea>
      <p class="edit-note">保存的是你修改后的内容；原候选会作为历史保留，不会覆盖其他修改。</p>
    </template>
    <template v-else>
      <p class="content" data-test="knowledge-approval-content">「{{ item.content }}」</p>
      <p v-if="item.reason" class="reason">{{ item.reason }}</p>
    </template>

    <p v-if="detailLoading" class="detail-loading">正在读取这条申请的完整信息…</p>
    <p v-else-if="detailFailed" class="detail-error" role="status">
      完整信息没能读到：{{ item.detailError || "未知原因" }}
      <button class="detail-retry" type="button" @click="emit('retry')">重试</button>
    </p>

    <dl class="facts" data-test="knowledge-approval-facts">
      <template v-for="row in facts" :key="row.label">
        <dt>{{ row.label }}</dt>
        <dd>{{ row.value }}</dd>
      </template>
    </dl>
  </div>
</template>

<style scoped>
.knowledge-approval-body {
  font-family: var(--sans);
  color: var(--text-primary);
  font-size: 13px;
  line-height: 1.65;
}
.lead {
  margin: 0 0 10px;
  color: var(--text-secondary);
  font-size: 12.5px;
}
.content {
  margin: 0;
  padding: 12px 14px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  background: var(--bg-inset);
  color: var(--text-primary);
  font-size: 13.5px;
  line-height: 1.75;
  overflow-wrap: anywhere;
}
.reason {
  margin: 8px 0 0;
  color: var(--text-muted);
  font-size: 11.5px;
  overflow-wrap: anywhere;
}
.edit-label {
  display: block;
  margin-bottom: 6px;
  color: var(--text-secondary);
  font-size: 12px;
}
.edit-area {
  width: 100%;
  min-height: 104px;
  padding: 10px 12px;
  border: 1px solid var(--border-strong);
  border-radius: var(--r-md);
  background: var(--bg-inset);
  color: var(--text-primary);
  font-family: var(--sans);
  font-size: 13px;
  line-height: 1.7;
  resize: vertical;
  overflow-wrap: anywhere;
}
.edit-area:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 1px;
}
.edit-note {
  margin: 6px 0 0;
  color: var(--text-muted);
  font-size: 11.5px;
}
.detail-loading {
  margin: 8px 0 0;
  color: var(--text-muted);
  font-size: 11.5px;
}
.detail-error {
  margin: 8px 0 0;
  color: var(--danger);
  font-size: 11.5px;
}
.detail-retry {
  margin-left: 8px;
  border: 1px solid var(--border-strong);
  border-radius: var(--r-sm);
  background: transparent;
  color: var(--text-secondary);
  font-size: 11.5px;
  padding: 2px 8px;
  cursor: pointer;
}
.detail-retry:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 2px;
}
.facts {
  display: grid;
  grid-template-columns: 64px 1fr;
  gap: 4px 10px;
  margin: 12px 0 0;
}
.facts dt {
  color: var(--text-secondary);
  font-size: 12px;
}
.facts dd {
  margin: 0;
  color: var(--text-primary);
  font-size: 12px;
  overflow-wrap: anywhere;
}
</style>
