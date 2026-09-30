<script setup lang="ts">
/**
 * 「有 N 个工具开发任务没做完」入口。
 *
 * 为什么需要它：开发任务以前只活在模型那一轮的工具调用里 —— 模型没提，
 * 或者用户刷新、重启之后，界面上就再也找不到那个任务了，用户不知道还有一件事
 * 没做完、也不知道怎么接着说。
 *
 * 这一行不打断任何事：不自动弹窗、不抢焦点，只在用户主动点开时才展开一份
 * **只读**清单。任务状态全部来自后端（`GET /api/dev/tasks`），界面不自己推断。
 */
import { computed, ref } from "vue";
import { useSessionStore } from "../stores/session";
import type { DevTaskRow } from "../services/api";

const session = useSessionStore();
const open = ref(false);
/** 上一次「继续开发」没有交出去（后端没受理）：要如实说，不能让用户以为已经在做了 */
const resumeFailed = ref(false);

const items = computed(() => session.unfinishedDevTasks);

/** 后端阶段名 → 界面说法。没见过的阶段照原样显示，不猜。 */
const PHASE_LABELS: Record<string, string> = {
  created: "刚创建",
  proposal: "待确认方案",
  building: "正在写代码",
  testing: "正在测试",
  testing_passed: "测试通过",
  testing_failed: "测试没通过",
  no_tests_required: "不用测试",
  waiting_approval: "等你确认",
  registering: "正在注册",
  ready: "已注册",
  submitted: "已提交",
  failed: "没做成",
};

function phaseLabel(phase: string | null): string {
  if (!phase) return "状态未知";
  return PHASE_LABELS[phase] ?? phase;
}

/**
 * 测试结论的三种说法，其中第三种是重点：
 * 「测试通过」只有在证据还对应**当前**文件内容时才算数 —— 通过之后又改过文件，
 * 那次结论就已经过期了，界面必须说出来，不能拿旧结论当现在的结果。
 */
function testText(task: DevTaskRow): string {
  if (task.test_passed === null) return "还没跑过测试";
  if (task.test_passed) {
    return task.test_evidence_current ? "测试通过" : "测试通过，但文件后来改过，结论不算数";
  }
  return task.test_evidence_current ? "测试没通过" : "测试没通过（文件后来改过）";
}

/** 更新时间：只显示到分钟，看的是「离现在多久」，不是精确时刻。 */
function updatedText(task: DevTaskRow): string {
  if (!task.updated_at) return "";
  const at = new Date(task.updated_at);
  if (Number.isNaN(at.getTime())) return "";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${at.getFullYear()}-${pad(at.getMonth() + 1)}-${pad(at.getDate())} ${pad(at.getHours())}:${pad(at.getMinutes())}`;
}

/** 交给模型的那句话：带上任务 id 与当前状态，模型才能接着做，而不是从头再来。 */
function resumeInstruction(task: DevTaskRow): string {
  return (
    `继续开发工具任务 ${task.id}（当前阶段：${phaseLabel(task.phase)}；测试：${testText(task)}）。` +
    `需求是：${task.request}\n` +
    "先看看这个工作区里已经写了什么、上次卡在哪一步，接着把没做完的部分做完，不要从头重写。"
  );
}

async function resume(task: DevTaskRow) {
  resumeFailed.value = false;
  const ok = await session.send(resumeInstruction(task));
  if (!ok) {
    // 没交出去就保持清单展开：用户还能再点一次，也不会误以为任务已经在做了
    resumeFailed.value = true;
    return;
  }
  open.value = false;
}
</script>

<template>
  <div v-if="items.length" class="dev-task-wrap">
    <button
      class="dev-task-entry"
      type="button"
      :aria-expanded="open"
      :aria-label="`有 ${items.length} 个工具开发任务没做完，展开查看`"
      @mousedown.prevent
      @click="open = !open"
    >
      有 {{ items.length }} 个工具开发任务没做完
    </button>
    <div v-if="open" class="dev-task-panel">
      <ul class="dev-task-list">
        <li v-for="task in items" :key="task.id" class="dev-task-row">
          <p class="dev-task-request">{{ task.request }}</p>
          <p class="dev-task-meta">
            <span>{{ phaseLabel(task.phase) }}</span>
            <span>{{ testText(task) }}</span>
            <span v-if="updatedText(task)">更新于 {{ updatedText(task) }}</span>
          </p>
          <button class="dev-task-resume" type="button" @click="resume(task)">继续开发</button>
        </li>
      </ul>
      <p v-if="resumeFailed" class="dev-task-error" role="alert">
        没能把这件事交给模型{{ session.lastError ? `：${session.lastError}` : "" }}。可以再点一次。
      </p>
    </div>
  </div>
</template>

<style scoped>
.dev-task-wrap {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
}
/* 一行浅色文字：它是「补充信息」，视觉分量要低于待确认的审批入口 */
.dev-task-entry {
  padding: 5px 12px; border-radius: var(--r-pill);
  background: var(--bg-elevated); color: var(--text-secondary);
  border: 1px solid var(--border-subtle);
  font-family: var(--sans); font-size: 12.5px;
  box-shadow: var(--shadow-1); cursor: pointer; pointer-events: auto;
  transition: color var(--dur-fast) var(--ease), border-color var(--dur-fast) var(--ease);
}
.dev-task-entry:hover { color: var(--text-strong); border-color: var(--accent); }
.dev-task-entry:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
.dev-task-panel {
  width: min(560px, calc(100vw - 32px));
  padding: 10px 12px; border-radius: var(--r-2);
  background: var(--bg-elevated); border: 1px solid var(--border-subtle);
  box-shadow: var(--shadow-2); pointer-events: auto;
  font-family: var(--sans); font-size: 12.5px; color: var(--text-primary);
  text-align: left;
}
.dev-task-list { margin: 0; padding: 0; list-style: none; display: flex; flex-direction: column; gap: 10px; }
.dev-task-row { display: flex; flex-direction: column; gap: 4px; }
.dev-task-request { margin: 0; color: var(--text-strong); }
.dev-task-meta { margin: 0; display: flex; flex-wrap: wrap; gap: 10px; color: var(--text-muted); font-size: 12px; }
.dev-task-resume {
  align-self: flex-start; margin-top: 2px;
  padding: 4px 10px; border-radius: var(--r-pill);
  background: transparent; color: var(--accent);
  border: 1px solid var(--border-subtle);
  font-family: var(--sans); font-size: 12.5px; cursor: pointer;
}
.dev-task-resume:hover { border-color: var(--accent); }
.dev-task-resume:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
.dev-task-error { margin: 8px 0 0; color: var(--danger); }
</style>
