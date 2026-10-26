<script setup lang="ts">
/**
 * 入口④：已提交的排队消息（B）。
 *
 * 数据来自 store.turnQueue（服务端权威快照）与「本机已提交、后端还没确认」的乐观消息。
 * 两条底线：
 * - 取消请求 ≠ 已经取消：只显示「已提交取消请求（以后端确认为准）」，不宣称已停止；
 * - 投递不明与排队分开：本机行没有 turn_id，不能凭它取消；受理未确认的发送只提示去
 *   入口①处理（不在两处放重复的提交按钮）。
 */
import { computed, ref } from "vue";
import { useSessionStore } from "../../stores/session";
import { api } from "../../services/api";
import { queueRows } from "./entries";

const props = defineProps<{ active?: boolean }>();
const emit = defineEmits<{ openSendIssues: [] }>();

const session = useSessionStore();

/** 本机已提交、但还没有 turn_id 的乐观排队消息（不能当「服务端已接受」展示） */
const localQueued = computed(() =>
  session.messages
    .filter((message) => message.queued && !message.turnId)
    .map((message) => ({ id: message.id, content: message.content })),
);

const rows = computed(() => queueRows(session.turnQueue, localQueued.value));

/** 当前话题下受理未确认的发送：数量只做提示，动作在入口① */
const unresolved = computed(() => session.unresolvedSendsForTopic(session.currentTopicId).length);

/** 正在等待后端确认取消的 turn_id（取消请求 ≠ 已取消） */
const cancellingId = ref<string | null>(null);
const cancelError = ref("");
const notice = ref("");

async function cancelTurn(turnId: string, label: string): Promise<void> {
  if (cancellingId.value) return;
  cancellingId.value = turnId;
  cancelError.value = "";
  notice.value = "";
  try {
    await api.cancelTurn(turnId);
    notice.value = `已提交「${label}」的取消请求（以后端确认为准）`;
  } catch (e) {
    cancelError.value = `取消没有成功：${(e as Error).message}（任务状态未改变，可以重试）`;
  } finally {
    cancellingId.value = null;
  }
}

const STATE_LABEL: Record<string, string> = {
  running: "运行中",
  queued: "排队中",
  cancelled: "已取消",
  local: "本机已提交",
};

void props;
</script>

<template>
  <div class="queue-panel" data-panel-section="queue">
    <p class="lead">
      这里只列出已经交给 QIO 的消息。取消是请求，以后端确认为准。
    </p>

    <ul v-if="rows.length" class="list">
      <li
        v-for="row in rows"
        :key="row.key"
        class="row"
        :data-state="row.state"
        :data-item="row.key"
      >
        <div class="row-main">
          <span class="badge mono" :class="row.state">
            {{ STATE_LABEL[row.state] }}<template v-if="row.state === 'queued' && row.position"> · 第 {{ row.position }} 位</template>
          </span>
          <span class="text">{{ row.message }}</span>
        </div>
        <div class="row-actions">
          <button
            v-if="row.state === 'running' && row.turnId"
            class="qio-btn mini quiet stop"
            type="button"
            :disabled="Boolean(cancellingId)"
            @click="cancelTurn(row.turnId!, '停止当前任务')"
          >
            {{ cancellingId === row.turnId ? "正在停止…" : "停止" }}
          </button>
          <button
            v-else-if="row.state === 'queued' && row.turnId"
            class="qio-btn mini quiet"
            type="button"
            :disabled="Boolean(cancellingId)"
            @click="cancelTurn(row.turnId!, '取消排队')"
          >
            {{ cancellingId === row.turnId ? "正在取消…" : "取消排队" }}
          </button>
          <span v-else-if="row.state === 'local'" class="hint">
            受理回执还没回来，不能按身份取消
          </span>
        </div>
      </li>
    </ul>
    <p v-else class="empty">现在没有排队或运行中的消息。</p>

    <p v-if="cancelError" class="error" role="alert">{{ cancelError }}</p>
    <p v-else-if="notice" class="notice" role="status">{{ notice }}</p>

    <button
      v-if="unresolved > 0"
      class="unresolved"
      type="button"
      @click="emit('openSendIssues')"
    >
      另有 {{ unresolved }} 条发送未确认（在「发送异常与草稿」里处理）
    </button>
  </div>
</template>

<style scoped>
.queue-panel {
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-width: 0;
}
.lead {
  margin: 0;
  color: var(--text-muted);
  font-size: var(--fs-xs);
  line-height: 1.65;
  overflow-wrap: anywhere;
}
.list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-width: 0;
}
.row {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
  padding: 8px 10px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--qio-ui-radius);
  background: var(--bg-inset);
}
.row[data-state="running"] {
  border-color: var(--accent);
  background: var(--accent-soft);
}
.row[data-state="cancelled"] {
  opacity: 0.72;
}
.row-main {
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 8px;
  min-width: 0;
}
.badge {
  flex: none;
  font-size: 10.5px;
  color: var(--text-muted);
  letter-spacing: 0.03em;
}
.badge.running {
  color: var(--accent);
}
.badge.queued {
  color: var(--text-secondary);
}
.badge.local {
  color: var(--warning);
}
.text {
  flex: 1 1 12ch;
  min-width: 0;
  color: var(--text-primary);
  font-size: var(--fs-sm);
  line-height: 1.6;
  overflow-wrap: anywhere;
  white-space: normal;
}
.row-actions {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}
.row-actions .qio-btn {
  height: auto;
  padding: 3px 10px;
  font-size: 11.5px;
  border-radius: var(--qio-ui-radius);
}
.hint {
  color: var(--text-muted);
  font-size: var(--fs-xs);
  overflow-wrap: anywhere;
}
.empty {
  margin: 0;
  color: var(--text-muted);
  font-size: var(--fs-xs);
}
.error {
  margin: 0;
  color: var(--danger);
  font-size: var(--fs-xs);
  overflow-wrap: anywhere;
}
.notice {
  margin: 0;
  color: var(--text-secondary);
  font-size: var(--fs-xs);
  overflow-wrap: anywhere;
}
.unresolved {
  align-self: flex-start;
  padding: 3px 0;
  border: none;
  background: none;
  color: var(--link);
  font-family: var(--sans);
  font-size: var(--fs-xs);
  cursor: pointer;
  text-align: left;
  overflow-wrap: anywhere;
}
.unresolved:hover {
  text-decoration: underline;
}
</style>
