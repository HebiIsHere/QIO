<script setup lang="ts">
/**
 * 左侧任务栏（A 负责完整实现；这里先接真实任务快照，保证基线可用、不造假数据）。
 *
 * 完整版要求：统一平铺、真实状态/来源/时间、当前/历史两个视图、需要处理置顶、
 * 原位展开多项、长输出按需读取、任务成果打开右侧预览、只有后端支持的动作才显示、
 * 审批任务打开统一弹窗、拖动调宽/手动收起/临时窄窗口收起、恢复事项与开发任务迁移。
 */
import { onMounted, ref } from "vue";
import { TASK_SOURCE_LABEL, TASK_STATUS_LABEL, type TaskFeedItem } from "../../conversation/contracts";
import { fetchTaskFeed } from "../../services/taskFeedApi";
import { useConversationShellStore } from "../../stores/conversationShell";

const shell = useConversationShellStore();
const items = ref<TaskFeedItem[]>([]);
const loading = ref(false);
const error = ref("");

async function load() {
  loading.value = true;
  error.value = "";
  try {
    const page = await fetchTaskFeed({ view: shell.prefs.taskTab, limit: 30 });
    items.value = page.items;
  } catch (err) {
    error.value = `任务列表读取失败：${(err as Error)?.message || "未知原因"}`;
  } finally {
    loading.value = false;
  }
}

onMounted(load);
defineExpose({ load });
</script>

<template>
  <aside class="task-rail" aria-label="任务">
    <div class="side-head">
      <span class="side-title">任务</span>
      <button class="qio-btn quiet mini" type="button" :disabled="loading" @click="load">刷新</button>
    </div>
    <div class="side-tabs" role="tablist" aria-label="任务视图">
      <button
        type="button"
        role="tab"
        :aria-selected="shell.prefs.taskTab === 'current'"
        @click="shell.setTaskTab('current'); load()"
      >
        当前任务
      </button>
      <button
        type="button"
        role="tab"
        :aria-selected="shell.prefs.taskTab === 'history'"
        @click="shell.setTaskTab('history'); load()"
      >
        历史记录
      </button>
    </div>
    <div class="task-list">
      <p v-if="error" class="qio-feedback err" role="alert">{{ error }}</p>
      <p v-else-if="!loading && !items.length" class="empty">没有需要显示的任务</p>
      <button
        v-for="item in items"
        :key="item.id"
        class="task-row"
        type="button"
        :aria-label="`${item.title}，${TASK_STATUS_LABEL[item.status]}`"
      >
        <span class="task-line">
          <span class="task-title">{{ item.title }}</span>
          <span class="task-status" :data-status="item.status">{{ TASK_STATUS_LABEL[item.status] }}</span>
        </span>
        <span class="task-meta">
          <span>{{ TASK_SOURCE_LABEL[item.source] }}</span>
          <span v-if="item.origin === 'global'">公共维护</span>
          <span v-else-if="item.topicTitle">{{ item.topicTitle }}</span>
          <span v-else-if="item.origin === 'unknown'">来源未知</span>
        </span>
      </button>
    </div>
  </aside>
</template>

<style scoped>
.task-rail {
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
  height: 100%;
  background: var(--bg-surface);
  border-right: 1px solid var(--border-subtle);
}
.side-head {
  flex: none;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 12px 6px;
}
.side-title {
  font-family: var(--serif);
  font-size: var(--fs-md);
  color: var(--text-strong);
}
.side-tabs {
  flex: none;
  display: flex;
  gap: 3px;
  margin: 0 10px 8px;
  padding: 3px;
  border-radius: var(--r-sm);
  background: var(--bg-inset);
}
.side-tabs button {
  flex: 1;
  border: 0;
  border-radius: var(--r-xs);
  background: transparent;
  color: var(--text-secondary);
  font-size: var(--fs-xs);
  padding: 5px 4px;
  cursor: pointer;
}
.side-tabs button[aria-selected="true"] {
  background: var(--bg-elevated);
  color: var(--text-strong);
}
.task-list {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 0 10px 12px;
  display: flex;
  flex-direction: column;
  gap: 7px;
}
.empty {
  margin: 10px 4px;
  font-size: var(--fs-sm);
  color: var(--text-muted);
}
.task-row {
  text-align: left;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  background: var(--bg-base);
  padding: 9px 10px;
  cursor: pointer;
  color: var(--text-primary);
  display: flex;
  flex-direction: column;
  gap: 5px;
}
.task-row:hover { border-color: var(--border-strong); }
.task-row:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
.task-line {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.task-title {
  flex: 1;
  min-width: 0;
  font-size: var(--fs-sm);
  overflow-wrap: anywhere;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.task-status { flex: none; font-size: 10px; color: var(--text-secondary); }
.task-status[data-status="failed"] { color: var(--danger); }
.task-status[data-status="waiting_approval"],
.task-status[data-status="waiting_user"] { color: var(--warning); }
.task-status[data-status="running"] { color: var(--accent); }
.task-status[data-status="unknown"] { color: var(--text-muted); }
.task-meta {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  font-size: 10px;
  color: var(--text-muted);
}
</style>
