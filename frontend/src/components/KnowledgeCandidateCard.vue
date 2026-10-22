<script setup lang="ts">
/**
 * 高影响知识候选卡（对话内）。
 *
 * 这张卡只负责「让你看见 + 打开统一审批弹窗」，不再自己放保存/修改/忽略按钮：
 * 同一个待审批知识只能有一套提交入口（统一弹窗），否则同一候选会出现两处按钮、
 * 两条提交路径。批准 / 修改后保存 / 不保存都在统一弹窗里完成。
 */
import { computed } from "vue";
import { useSessionStore, type KnowledgeCandidate } from "../stores/session";
import { useUnifiedApprovalsStore } from "../stores/unifiedApprovals";

const props = defineProps<{ candidate: KnowledgeCandidate }>();
const session = useSessionStore();
const unified = useUnifiedApprovalsStore();

const busy = computed(() => session.candidateState[props.candidate.knowledgeId] === "busy");
const error = computed(() => session.candidateError[props.candidate.knowledgeId] ?? "");
const inQueue = computed(() =>
  unified.knowledge.some((item) => item.id === props.candidate.knowledgeId),
);

function openApproval() {
  unified.ingestKnowledgeCandidate({
    knowledgeId: props.candidate.knowledgeId,
    category: props.candidate.category,
    content: props.candidate.content,
    reason: props.candidate.reason,
  });
  unified.open("knowledge:" + props.candidate.knowledgeId);
}
</script>

<template>
  <div
    class="candidate qio-card"
    data-qio-fit
    :data-state="error ? 'failed' : busy ? 'running' : 'waiting'"
    role="group"
    aria-label="QIO 想记住的信息"
  >
    <div class="head">
      <span class="kind qio-tag">QIO 想记住</span>
      <span class="hint">这条以后会影响它的回答</span>
    </div>
    <p class="content">「{{ candidate.content }}」</p>
    <p v-if="candidate.reason" class="reason">{{ candidate.reason }}</p>
    <div class="actions">
      <button
        class="qio-btn mini primary open-approval"
        type="button"
        data-test="knowledge-candidate-open"
        @click="openApproval"
      >
        查看并决定
      </button>
      <span class="waiting mono">{{ inQueue ? "在待确认里" : "确认后才生效" }}</span>
    </div>
    <p v-if="error" class="err qio-feedback err" role="alert">{{ error }}</p>
  </div>
</template>

<style scoped>
.candidate {
  max-width: min(760px, 100%);
  margin: 6px 0;
  padding: var(--sp-3) var(--sp-4);
  border-color: var(--border-strong);
  border-style: dashed;
  font-size: 13px;
}
.head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 6px;
}
.kind {
  font-size: 11.5px;
  color: var(--text-strong);
  letter-spacing: 0.03em;
}
.hint {
  font-family: var(--mono);
  font-size: 10px;
  color: var(--text-muted);
  letter-spacing: 0.04em;
}
.content {
  margin: 0;
  color: var(--text-primary);
  line-height: 1.7;
  overflow-wrap: anywhere;
}
.reason {
  margin: 4px 0 0;
  font-size: 11.5px;
  color: var(--text-muted);
}
.actions {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 8px;
}
.qio-btn.mini {
  height: auto;
  padding: 4px 12px;
  font-size: 11.5px;
  border-radius: 8px;
}
.waiting {
  font-size: 10px;
  color: var(--text-muted);
  letter-spacing: 0.04em;
}
.err {
  margin: 6px 0 0;
  font-size: 11.5px;
  color: var(--danger);
}
</style>
