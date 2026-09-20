<script setup lang="ts">
/**
 * 工具创建卡（spec 第 20~31、96 条）。
 *
 * 一次工具创建是**一条流程、一张卡**：同一 `group_id`（开发工作区）在原地
 * 从「提案」走到「已创建」，而不是连续产生「创建卡 → 测试卡 → 审批卡 → 成功卡」。
 *
 * 默认只显示：工具名 + 当前状态 + 一行说明。源代码、文件路径、内部工作区
 * 这些只在「查看详情」里出现（阶段事件本身也不把它们放进首屏文案）。
 */
import { computed, ref } from "vue";
import type { StreamMessage } from "../stores/session";

const props = defineProps<{ message: StreamMessage }>();
const open = ref(false);

/** 阶段 → 用户可见文案（与后端 `tools/dev_tools.py` 的 phase 常量一一对应） */
const PHASE_LABELS: Record<string, string> = {
  proposal: "提案",
  building: "正在构建",
  testing: "正在测试",
  testing_passed: "测试通过",
  testing_failed: "测试失败",
  waiting_approval: "等待你的确认",
  registering: "正在启用",
  ready: "已创建",
  failed: "创建失败",
};

const phase = computed(() => props.message.createPhase ?? "");
const phaseLabel = computed(() => PHASE_LABELS[phase.value] ?? "正在准备");
const running = computed(() =>
  ["proposal", "building", "testing", "waiting_approval", "registering"].includes(phase.value),
);
const failed = computed(() => phase.value === "failed" || props.message.toolOk === false);
const ready = computed(() => phase.value === "ready");
const title = computed(() => props.message.createdToolName || "新工具");
const detail = computed(() => props.message.presentation?.summary ?? "");

/**
 * 失败时的可理解结论：后端给的中文明细优先（例如「测试没有通过：先让工具把
 * 测试跑绿再提交」），拿不到就说中性结论，不显示 `Error` 这种词。
 */
const failureText = computed(() => props.message.toolError || detail.value || "这次没有创建成功");

/** 创建卡上的进程点：一眼看出走到哪一步，不用读完整事件名 */
const STEPS = ["提案", "构建", "测试", "确认", "启用"] as const;
function stepState(step: string): "done" | "current" | "todo" {
  const order: Record<string, number> = {
    proposal: 0,
    building: 1,
    testing: 2,
    testing_passed: 2,
    testing_failed: 2,
    waiting_approval: 3,
    registering: 4,
    ready: 5,
    failed: 2,
  };
  const index = order[phase.value] ?? 0;
  const i = STEPS.indexOf(step as (typeof STEPS)[number]);
  if (ready.value) return "done";
  if (i < index) return "done";
  if (i === index) return "current";
  return "todo";
}
</script>

<template>
  <div
    class="create-card qio-card"
    :class="{ failed, ready, open }"
    :data-state="ready ? 'ready' : failed ? 'failed' : running ? 'running' : 'waiting'"
  >
    <div class="head">
      <span class="kind mono qio-tag">工具创建</span>
      <span class="name">{{ title }}</span>
      <span
        class="phase qio-state"
        :class="failed ? 'failed err' : ready ? 'ready ok' : running ? 'doing info' : 'waiting'"
      >{{ phaseLabel }}</span>
    </div>
    <p v-if="!failed && detail" class="line">{{ detail }}</p>
    <div class="steps">
      <span
        v-for="s in STEPS"
        :key="s"
        class="step"
        :class="stepState(s)"
      >{{ s }}</span>
    </div>
    <!-- 失败：说清结果 + 可理解原因 + 下一步（QIO 能继续修就直接说能修） -->
    <p v-if="failed" class="failure qio-feedback err" role="status">创建没有完成：{{ failureText }}</p>
    <p v-if="failed" class="next">可以让 QIO 继续修，改好后再提交给你确认。</p>
    <p v-if="ready" class="ready-line qio-feedback ok" role="status">现在可以使用了。</p>
    <button class="detail-toggle qio-btn mini quiet" type="button" :aria-expanded="open" @click="open = !open">
      {{ open ? "收起详情" : "展开详情" }}
    </button>
    <!-- 展开/收起必须是连续变化（真实高度过渡），不是 v-show 瞬切 -->
    <div class="detail-wrap" :class="{ open }">
      <div class="detail-clip">
        <div class="detail">
          <p v-if="detail">{{ detail }}</p>
          <p v-if="message.createdToolName" class="mono">工具：{{ message.createdToolName }}</p>
          <p v-if="message.groupId" class="mono">开发工作区：{{ message.groupId }}</p>
          <p class="mono">阶段：{{ phase || "—" }}</p>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.create-card {
  margin: 4px 0;
  border-radius: var(--r-lg);
  padding: var(--sp-3) var(--sp-4);
  font-size: 12.5px;
}
.create-card.failed { border-color: var(--border-danger); }
.create-card.ready { border-color: var(--success-soft); }
.head {
  display: flex;
  align-items: center;
  gap: 10px;
}
.kind {
  font-size: 10px;
  letter-spacing: 0.06em;
}
.name {
  color: var(--text-strong);
}
.phase {
  margin-left: auto;
  font-size: 11px;
}
.line,
.failure,
.next,
.ready-line {
  margin: 6px 0 0;
  color: var(--text-secondary);
  line-height: 1.6;
}
.failure {
  color: var(--danger);
}
.ready-line {
  color: var(--success);
}
.steps {
  display: flex;
  gap: 10px;
  margin-top: 8px;
  font-size: 10.5px;
  color: var(--text-muted);
}
.step.done {
  color: var(--text-secondary);
}
.step.current {
  color: var(--text-strong);
}
.detail-toggle {
  margin-top: 8px;
}
.detail-wrap {
  display: grid;
  grid-template-rows: 0fr;
  transition: grid-template-rows var(--mo-2-in) var(--ease-2);
}
.detail-wrap.open { grid-template-rows: 1fr; }
.detail-clip {
  overflow: hidden;
  min-height: 0;
  opacity: 0;
  transition: opacity var(--mo-2-in) var(--ease-2);
}
.detail-wrap.open .detail-clip { opacity: 1; }
.detail {
  margin-top: 6px;
  padding-top: 6px;
  border-top: 1px solid var(--border-subtle);
  font-size: 11px;
  color: var(--text-muted);
}
.detail p {
  margin: 2px 0;
  overflow-wrap: anywhere;
}
</style>
