<script setup lang="ts">
/**
 * 高影响知识候选的确认卡（spec 第 14~19、95 条）。
 *
 * 为什么需要它：高影响知识（用户长期偏好 / 用户画像 / 长期目标 / Agent 对自己的
 * 长期认知）以前只进 `pending_review`，用户得自己想到去 Knowledge Panel 才能发现。
 * 现在它在**回答完成之后**以低干扰方式出现在对话里，用户可以保存 / 修改 / 忽略。
 *
 * 约束：
 * - 不打断回答：只有 TURN_END 之后组件才会拿到候选（store 里缓冲）；
 * - 不用弹窗、不抢焦点、不遮罩；
 * - 修改是很轻的内联编辑，不跳转到 Knowledge Panel；
 * - 三个动作都有「进行中 / 成功 / 失败」，失败留在卡上用中文说明并可重试。
 */
import { computed, ref } from "vue";
import { useSessionStore, type KnowledgeCandidate } from "../stores/session";

const props = defineProps<{ candidate: KnowledgeCandidate }>();
const session = useSessionStore();

const editing = ref(false);
const draft = ref("");

const busy = computed(() => session.candidateState[props.candidate.knowledgeId] === "busy");
const error = computed(() => session.candidateError[props.candidate.knowledgeId] ?? "");

function startEdit() {
  draft.value = props.candidate.content;
  editing.value = true;
}

async function saveEdit() {
  const text = draft.value.trim();
  if (!text) return;
  const ok = await session.editCandidate(props.candidate.knowledgeId, text);
  if (session.candidateState[props.candidate.knowledgeId] !== "failed") editing.value = false;
  void ok;
}
</script>

<template>
  <div
    class="candidate qio-card"
    :class="{ editing }"
    :data-state="error ? 'failed' : busy ? 'running' : 'waiting'"
    role="group"
    aria-label="QIO 想记住的信息"
  >
    <div class="head">
      <span class="kind qio-tag">QIO 想记住</span>
      <span class="hint">这条以后会影响它的回答</span>
    </div>
    <template v-if="editing">
      <textarea
        v-model="draft"
        class="qio-inline-edit edit"
        rows="2"
        aria-label="修改要记住的内容"
        :disabled="busy"
      ></textarea>
      <div class="actions">
        <button class="qio-btn mini quiet" type="button" :disabled="busy" @click="editing = false">
          取消
        </button>
        <button class="qio-btn mini primary save" type="button" :disabled="busy" @click="saveEdit">
          {{ busy ? "保存中…" : "保存" }}
        </button>
      </div>
    </template>
    <template v-else>
      <p class="content">「{{ candidate.content }}」</p>
      <p v-if="candidate.reason" class="reason">{{ candidate.reason }}</p>
      <div class="actions">
        <button
          class="qio-btn mini primary keep"
          type="button"
          :disabled="busy"
          @click="session.saveCandidate(candidate.knowledgeId)"
        >
          {{ busy ? "处理中…" : "保存" }}
        </button>
        <button class="qio-btn mini quiet edit-btn" type="button" :disabled="busy" @click="startEdit">
          修改
        </button>
        <button
          class="qio-btn mini quiet ignore"
          type="button"
          :disabled="busy"
          @click="session.ignoreCandidate(candidate.knowledgeId)"
        >
          忽略
        </button>
      </div>
    </template>
    <p v-if="error" class="err qio-feedback err" role="alert">{{ error }}</p>
  </div>
</template>

<style scoped>
.candidate {
  max-width: min(760px, 100%);
  margin: 6px 0;
  padding: var(--sp-3) var(--sp-4);
  /* 候选卡是「需要你决定」的对象，所以边框用强一档（状态仍由 data-state 表达） */
  border-color: var(--border-strong);
  border-style: dashed;
  font-size: 13px;
}
.candidate.editing { border-style: solid; }
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
.edit {
  width: 100%;
  font-size: 13px;
  resize: vertical;
}
.actions {
  display: flex;
  gap: 8px;
  margin-top: 8px;
}
.qio-btn.mini {
  height: auto;
  padding: 4px 12px;
  font-size: 11.5px;
  border-radius: 8px;
}
.err {
  margin: 6px 0 0;
  font-size: 11.5px;
  color: var(--danger);
}
</style>
