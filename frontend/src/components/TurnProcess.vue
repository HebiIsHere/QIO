<script setup lang="ts">
/**
 * 一轮的**唯一**过程区域（契约 §1.5）。
 *
 * 三块内容：
 * 1. 状态行：系统事实（受理中 / 运行中 / 等待确认 / 已停止 / 已完成 · 耗时）
 *    + 工具行（「正在读取文件 · 2 项工具运行中」）+ 耗时入口；
 * 2. 当前阶段：名字 + 当前说明（运行中突出显示；legacy 记录不伪造阶段）；
 * 3. 可展开历史：之前的阶段（顺序、历次说明、关联工具）+ 没有阶段归属的工具（「整轮」）。
 *
 * 归并：散落的工具卡、NarrativeStage 平铺、interim 气泡、耗时面板都收进这里，
 * 同一内容只出现一次。
 *
 * 展开状态存在 store（stores/turnProcess.ts）：虚拟列表会卸载条目，组件内的 ref
 * 会在滚出视野时丢掉；键里带 topic / turn_id / stage_id，跨刷新也能恢复。
 * 完成 / 失败 / 停止时自动收起，但**用户手动展开过、或正在上翻阅读时不动**。
 */
import { computed, onBeforeUnmount, watch } from "vue";
import MessageItem from "./MessageItem.vue";
import NarrativeStage from "./NarrativeStage.vue";
import TurnTimingPanel from "./TurnTimingPanel.vue";
import { useApprovalsStore, approvalCapabilities, approvalIntent } from "../stores/approvals";
import {
  currentStageOf,
  groupTurnItems,
  useSessionStore,
  type StreamMessage,
  type TurnFacts,
  type TurnStage,
} from "../stores/session";
import {
  isProcessExpanded,
  isProcessManual,
  processKey,
  setProcessExpanded,
  toggleProcess,
} from "../stores/turnProcess";

const props = defineProps<{
  /** 这一轮的 turn_id（空串 = 旧历史记录，没有 turn 归属） */
  turnId: string;
  /** 过程条目（中间话 + 工具卡 + legacy 叙事行），按到达顺序 */
  items: StreamMessage[];
  /** 阶段（契约 §1.3；空数组 = legacy 平铺，不伪造阶段） */
  stages: TurnStage[];
  /** TURN_END 的权威事实（没有 = 还在跑 / 旧记录） */
  facts: TurnFacts | null;
  /** 这一轮正在运行 */
  running: boolean;
  /** 这一轮已受理但还没开始 */
  queued?: boolean;
}>();

const session = useSessionStore();
const approvals = useApprovalsStore();

/** 展开状态键：话题 + 轮（整轮级区域）—— 跨轮 / 跨话题不串 */
const stateKey = computed(() =>
  processKey(session.currentTopicId, props.turnId || props.items[0]?.id || "-"),
);
/** 用户手动开合过就尊重用户；否则运行中展开、结束后收起 */
const manual = computed(() => isProcessManual(stateKey.value));
const open = computed(() => isProcessExpanded(stateKey.value));

function toggle() {
  toggleProcess(stateKey.value, !open.value);
}

/**
 * 默认展开策略：
 * * 运行中 → 展开（工具行必须看得见，不能把「正在跑」藏起来）；
 * * 完成 / 失败 / 停止 → 收起，只留状态 + 总耗时；
 * * 用户手动开合过 → 不再自动改；
 * * 用户正在上翻阅读（没在跟随底部）→ 不强制收起，避免把正在读的内容抽走。
 */
watch(
  () => props.running,
  (now, before) => {
    if (now === before) return;
    if (manual.value) return;
    if (!now && !session.streamFollowing) return;
    setProcessExpanded(stateKey.value, now);
  },
  { immediate: true },
);

// -- 过程内容 ---------------------------------------------------------

const stages = computed(() => props.stages);
const currentStage = computed(() =>
  props.running || props.queued ? currentStageOf(stages.value) : null,
);
const historyStages = computed(() => {
  const current = currentStage.value;
  return current ? stages.value.filter((s) => s.stageId !== current.stageId) : stages.value;
});

const tools = computed(() => props.items.filter((m) => m.role === "tool"));
const interims = computed(() => props.items.filter((m) => m.role === "assistant"));

function toolsOf(stageId: string): StreamMessage[] {
  return tools.value.filter((m) => (m.stageId ?? "") === stageId);
}
/** 某阶段里的中间话（与阶段说明同文时不重复出现：同一段文字只渲染一次） */
function interimsOf(stage: TurnStage): StreamMessage[] {
  const notes = new Set(stage.notes.map((n) => n.text));
  return interims.value.filter(
    (m) => (m.stageId ?? "") === stage.stageId && !notes.has(m.content.trim()),
  );
}
const looseTools = computed(() => tools.value.filter((m) => !m.stageId));
const looseInterims = computed(() => interims.value.filter((m) => !m.stageId));

/** 当前阶段的说明：STAGE 的最后一条 text（模型文案，不参与任何判定） */
const currentNote = computed(() => {
  const stage = currentStage.value;
  return stage?.notes.length ? stage.notes[stage.notes.length - 1]?.text ?? "" : "";
});
/**
 * 当前阶段**更早的说明**（最新一条已经在当前阶段块里突出显示）。
 *
 * 没有它时，运行中的用户只能看到本阶段的最后一句；前几句要等转阶段后才进历史。
 * 这里把它们放进可展开历史，且与当前块不重复（同一段文字只出现一次）。
 */
const currentStageEarlierNotes = computed(() => {
  const stage = currentStage.value;
  if (!stage || stage.notes.length <= 1) return [];
  return stage.notes.slice(0, -1);
});

/** 当前阶段里的中间话（与阶段说明同文时不重复显示） */
const currentInterims = computed(() => {
  const stage = currentStage.value;
  if (!stage) return [];
  return interimsOf(stage);
});
/** legacy（没有阶段）：运行中把最后一句中间话当作「当前说明」，但不给它编一个阶段名 */
const lastLooseLine = computed(() => {
  const last = props.items[props.items.length - 1];
  if (!last || last.role !== "assistant") return "";
  return last.content.trim();
});

// -- 系统事实 ---------------------------------------------------------

const runningTools = computed(() =>
  tools.value.filter((m) => m.toolStatus === "running" || m.toolRunning === true),
);
function toolStateOf(m: StreamMessage): string {
  return m.toolStatus ?? (m.toolOk === false ? "failed" : "success");
}
/** 失败 / 取消 / 结果未收到分开数：不把「取消」说成「失败」 */
const failedTools = computed(() => tools.value.filter((m) => toolStateOf(m) === "failed"));
const cancelledTools = computed(() => tools.value.filter((m) => toolStateOf(m) === "cancelled"));
const unknownTools = computed(() => tools.value.filter((m) => toolStateOf(m) === "unknown"));
const runningToolLabel = computed(
  () => runningTools.value[0]?.presentation?.title || runningTools.value[0]?.toolName || "",
);

const STATUS_WORD: Record<string, string> = {
  completed: "已完成",
  failed: "已失败",
  cancelled: "已停止",
  stopped: "已停止",
  unavailable: "未完成",
};

const statusWord = computed(() => {
  if (props.running) {
    // 只有这条审批**确实内联在本轮过程区**时才说「等待确认」；
    // 非当前轮 / 恢复路径的审批仍由全局状态条说那句话
    if (inlineApproval.value) return "等待确认";
    return session.turnPhase === "generating" ? "运行中" : "正在处理";
  }
  if (props.queued) return "等待开始";
  if (props.facts) return STATUS_WORD[props.facts.status] ?? "已结束";
  return "已结束";
});

/** 状态行里的工具 / 阶段事实（一句话，不堆状态） */
const statusDetail = computed(() => {
  const bits: string[] = [];
  if (runningTools.value.length) {
    const label = runningToolLabel.value || "工具";
    bits.push(`${label} · ${runningTools.value.length} 项工具运行中`);
  } else if (!props.running && stages.value.length) {
    // 当前阶段块会显示阶段名，这里只在收起后（没有当前阶段块）才补一句
    const last = stages.value[stages.value.length - 1];
    if (last?.name) bits.push(last.name);
  }
  if (failedTools.value.length) bits.push(`${failedTools.value.length} 项失败`);
  if (cancelledTools.value.length) bits.push(`${cancelledTools.value.length} 项已取消`);
  if (unknownTools.value.length) bits.push(`${unknownTools.value.length} 项结果未收到`);
  return bits.join(" · ");
});

const dataState = computed(() => {
  if (props.running) return "running";
  if (props.queued) return "waiting";
  if (props.facts?.status === "failed") return "failed";
  if (props.facts?.status === "cancelled" || props.facts?.status === "stopped") return "stopped";
  return "ready";
});

// -- 展开历史 ---------------------------------------------------------

const hasDrawer = computed(() => props.items.length > 0 || stages.value.length > 0);
const drawerSummary = computed(() => {
  const bits: string[] = [];
  if (stages.value.length) bits.push(`${stages.value.length} 个阶段`);
  if (tools.value.length) bits.push(`${tools.value.length} 次调用`);
  return bits.join(" · ");
});

/**
 * legacy 平铺的条目：运行中把最后一句中间话留给上面的「当前说明」显示，
 * 避免同一段文字在折叠头与历史里各出现一次。
 */
const legacyItems = computed(() => {
  if (!props.running || !lastLooseLine.value) return props.items;
  const last = props.items[props.items.length - 1];
  return last ? props.items.filter((m) => m.id !== last.id) : props.items;
});

/** legacy 平铺：一行叙事收纳它之后的调用卡（不伪造阶段） */
const legacyGroups = computed(() =>
  groupTurnItems(legacyItems.value).map((group) =>
    group.kind === "stage"
      ? { key: group.narrative.id, narrative: group.narrative, lines: [] as StreamMessage[], calls: group.calls }
      : {
          key: `loose_${group.items[0]?.id ?? "x"}`,
          narrative: null,
          lines: group.items,
          calls: [] as StreamMessage[],
        },
  ),
);

// -- 内联审批（复用既有 approvals store，绝不自己发请求） --------------

const inlineApproval = computed(() => {
  if (!props.running || !props.turnId) return null;
  // 只有「当前正在跑的这一轮」才内联；非当前轮 / 过期 / 恢复路径仍走全局入口
  if (session.activeTurnId !== props.turnId) return null;
  const current = approvals.current;
  if (!current) return null;
  if (current.turnId && current.turnId !== props.turnId) return null;
  return current;
});

const APPROVAL_KIND_LABELS: Record<string, string> = {
  tool_create: "工具创建审批",
  credential_grant: "凭据授权审批",
  high_impact_knowledge: "高影响知识确认",
  tool_execution: "需要你确认的操作",
  computer: "电脑操作审批",
  dependency_install: "安装依赖",
};

const approvalTitle = computed(() =>
  inlineApproval.value
    ? (APPROVAL_KIND_LABELS[inlineApproval.value.kind] ?? "需要你确认的操作")
    : "",
);
const approvalLine = computed(() =>
  inlineApproval.value ? approvalIntent(inlineApproval.value.payload) : "",
);
const approvalCaps = computed(() =>
  inlineApproval.value ? approvalCapabilities(inlineApproval.value.payload) : [],
);
const approvalBusy = computed(
  () => !!inlineApproval.value && approvals.responding === inlineApproval.value.approval_id,
);

function respond(decision: "approved" | "rejected") {
  const item = inlineApproval.value;
  if (!item) return;
  // 按 approval_id 应答：绝不误伤队列里的下一项（重复点击由 store 挡住）
  void approvals.respondById(item.approval_id, decision);
}

/** 内联声明：本组件显示这条审批的按钮时，全局入口 / 弹窗让位（按 approval_id 门控） */
const claimedId = computed(() => inlineApproval.value?.approval_id ?? null);
watch(
  claimedId,
  (now, before) => {
    if (before && before !== now) approvals.releaseInline(before);
    if (now) approvals.claimInline(now);
  },
  { immediate: true },
);
onBeforeUnmount(() => {
  const id = claimedId.value;
  if (id) approvals.releaseInline(id);
});
</script>

<template>
  <section
    class="turn-process"
    :class="{ open, running, failed: failedTools.length > 0 }"
    :data-state="dataState"
    :data-turn="turnId || undefined"
    data-test="turn-process"
    aria-label="本轮过程"
  >
    <!-- 状态行：系统事实。展开按钮与耗时入口是两个独立控件（不能嵌套按钮） -->
    <div class="tp-status" data-test="turn-process-status">
      <button
        class="tp-toggle"
        type="button"
        data-test="turn-process-toggle"
        :aria-expanded="open ? 'true' : 'false'"
        :title="open ? '收起过程历史' : '展开过程历史'"
        @click="toggle"
      >
        <span class="tp-dot" aria-hidden="true"></span>
        <span class="tp-state">{{ statusWord }}</span>
        <span v-if="statusDetail" class="tp-detail">{{ statusDetail }}</span>
        <span v-if="hasDrawer" class="tp-chev" aria-hidden="true">{{ open ? "▾" : "▸" }}</span>
      </button>
      <span v-if="turnId && !running" class="tp-duration" data-test="turn-process-duration">
        <TurnTimingPanel
          :turn-id="turnId"
          :duration-ms="facts?.durationMs ?? null"
          :status="facts?.status ?? null"
        />
      </span>
      <span v-if="hasDrawer && drawerSummary" class="tp-count mono">{{ drawerSummary }}</span>
    </div>

    <!-- 当前阶段：名字 + 当前说明，突出显示；当前阶段的工具也在这里（运行中看得见） -->
    <div v-if="currentStage" class="tp-current">
      <div class="tp-cur-name serif">{{ currentStage.name || "当前阶段" }}</div>
      <p v-if="currentNote" class="tp-cur-text">{{ currentNote }}</p>
      <MessageItem v-for="m in currentInterims" :key="m.id" :message="m" />
      <MessageItem v-for="m in toolsOf(currentStage.stageId)" :key="m.id" :message="m" />
    </div>
    <div v-else-if="running && lastLooseLine" class="tp-current">
      <p class="tp-cur-text">{{ lastLooseLine }}</p>
    </div>

    <!-- 内联审批：同一过程区域内自动展开，保持可见直到用户选择 -->
    <div v-if="inlineApproval" class="tp-approval" data-test="turn-process-approval" role="group">
      <div class="tp-ap-head">
        <span class="tp-ap-mark" aria-hidden="true">!</span>
        <span class="tp-ap-title serif">{{ approvalTitle }}</span>
      </div>
      <p class="tp-ap-intent">{{ approvalLine }}</p>
      <p v-if="approvalCaps.length" class="tp-ap-caps mono">{{ approvalCaps.join(" · ") }}</p>
      <p v-if="approvals.error" class="tp-ap-err" role="status">{{ approvals.error }}</p>
      <div class="tp-ap-actions">
        <button
          class="qio-btn primary"
          type="button"
          :disabled="approvalBusy"
          :aria-busy="approvalBusy ? 'true' : undefined"
          @click="respond('approved')"
        >
          允许
        </button>
        <button
          class="qio-btn"
          type="button"
          :disabled="approvalBusy"
          :aria-busy="approvalBusy ? 'true' : undefined"
          @click="respond('rejected')"
        >
          拒绝
        </button>
      </div>
    </div>

    <!-- 可展开历史：阶段顺序 + 历次说明 + 关联工具；legacy 记录平铺。
         收起时整块不渲染（DOM 里不存在）——「同一内容只出现一次」也包括不可见的重复。 -->
    <div v-if="hasDrawer && open" class="tp-drawer open" data-test="turn-process-history">
      <div class="tp-drawer-clip">
        <div class="tp-drawer-body">
          <template v-if="stages.length">
            <section v-for="stage in historyStages" :key="stage.stageId" class="tp-stage">
              <div class="tp-stage-head">
                <span class="tp-stage-index mono">{{ stage.index }}</span>
                <span class="tp-stage-name serif">{{ stage.name || "过程" }}</span>
                <span class="tp-stage-state mono" :data-state="stage.status">
                  {{ stage.status === "done" ? "已完成" : "进行中" }}
                </span>
              </div>
              <p v-for="note in stage.notes" :key="note.narrativeId || note.text" class="tp-note" :data-kind="note.kind">
                {{ note.text }}
              </p>
              <MessageItem v-for="m in interimsOf(stage)" :key="m.id" :message="m" />
              <MessageItem v-for="m in toolsOf(stage.stageId)" :key="m.id" :message="m" />
            </section>
            <!-- 当前阶段更早的说明：运行中也能回看（最新一条在上面突出显示） -->
            <section v-if="currentStageEarlierNotes.length" class="tp-stage" data-test="turn-process-current-notes">
              <div class="tp-stage-head">
                <span v-if="currentStage" class="tp-stage-index mono">{{ currentStage.index }}</span>
                <span class="tp-stage-name serif">{{ currentStage?.name || "当前阶段" }}</span>
                <span class="tp-stage-state mono" data-state="running">更早的说明</span>
              </div>
              <p
                v-for="note in currentStageEarlierNotes"
                :key="note.narrativeId || note.text"
                class="tp-note"
                :data-kind="note.kind"
              >
                {{ note.text }}
              </p>
            </section>
            <section v-if="looseTools.length || looseInterims.length" class="tp-stage">
              <div class="tp-stage-head">
                <span class="tp-stage-name serif">整轮</span>
                <span class="tp-stage-state mono" data-state="loose">没有阶段归属</span>
              </div>
              <MessageItem v-for="m in looseInterims" :key="m.id" :message="m" />
              <MessageItem v-for="m in looseTools" :key="m.id" :message="m" />
            </section>
          </template>
          <template v-else>
            <div v-for="group in legacyGroups" :key="group.key" class="tp-legacy">
              <NarrativeStage v-if="group.narrative" :narrative="group.narrative" :calls="group.calls">
                <MessageItem v-for="m in group.calls" :key="m.id" :message="m" />
              </NarrativeStage>
              <template v-else>
                <MessageItem v-for="m in group.lines" :key="m.id" :message="m" />
              </template>
            </div>
          </template>
        </div>
      </div>
    </div>
  </section>
</template>

<style scoped>
/* 过程区 = 一轮里唯一的「过程」容器：一条极淡的轨，内容收在里面 */
.turn-process {
  margin: 2px 0 6px;
  border-left: 1px solid var(--border-subtle);
  padding-left: 2px;
}
.turn-process[data-state="failed"] {
  border-left-color: var(--border-danger);
}
.tp-status {
  display: flex;
  align-items: baseline;
  gap: var(--sp-3);
  flex-wrap: wrap;
  padding: 3px 2px;
}
.tp-toggle {
  display: inline-flex;
  align-items: baseline;
  gap: var(--sp-2);
  background: none;
  border: none;
  padding: 3px 4px;
  font: inherit;
  color: inherit;
  text-align: left;
  cursor: pointer;
  border-radius: var(--r-xs);
}
.tp-toggle:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 2px;
}
.tp-dot {
  flex: 0 0 auto;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--text-faint);
  align-self: center;
}
.turn-process[data-state="running"] .tp-dot {
  background: var(--link);
}
.turn-process[data-state="waiting"] .tp-dot {
  background: var(--warning);
}
.turn-process[data-state="failed"] .tp-dot {
  background: var(--danger);
}
.turn-process[data-state="stopped"] .tp-dot {
  background: var(--text-faint);
}
.turn-process[data-state="ready"] .tp-dot {
  background: var(--success);
}
.tp-state {
  font-family: var(--sans);
  font-size: var(--fs-sm);
  color: var(--text-secondary);
}
.turn-process[data-state="running"] .tp-state {
  color: var(--text-strong);
}
.turn-process[data-state="failed"] .tp-state {
  color: var(--danger);
}
.tp-detail {
  font-family: var(--sans);
  font-size: var(--fs-sm);
  color: var(--text-muted);
}
.tp-chev {
  font-size: 10px;
  color: var(--text-faint);
}
.tp-count {
  font-size: var(--fs-xs);
  color: var(--text-faint);
}
.tp-duration {
  display: inline-flex;
  align-items: baseline;
}
/* 当前阶段：突出但不喧哗 */
.tp-current {
  margin: 2px 0 2px 4px;
  padding-left: var(--sp-3);
  border-left: 1px solid var(--border-strong);
}
.tp-cur-name {
  font-size: var(--fs-base);
  color: var(--text-strong);
  line-height: 1.6;
}
.tp-cur-text {
  margin: 1px 0 2px;
  font-size: var(--fs-sm);
  color: var(--text-secondary);
  line-height: 1.7;
}
/* 内联审批：真实操作信息 + 同一套 store 的按钮 */
.tp-approval {
  margin: var(--sp-2) 0 var(--sp-2) var(--sp-3);
  padding: var(--sp-3);
  max-width: min(560px, 100%);
  background: var(--bg-elevated);
  border: 1px solid var(--warning);
  border-radius: var(--r-md);
  box-shadow: var(--shadow-1);
}
.tp-ap-head {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
}
.tp-ap-mark {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 16px;
  height: 16px;
  border-radius: 50%;
  background: var(--warning);
  color: var(--bg-base);
  font-family: var(--mono);
  font-size: 11px;
  font-weight: 700;
}
.tp-ap-title {
  font-size: var(--fs-base);
  color: var(--text-strong);
}
.tp-ap-intent {
  margin: var(--sp-2) 0 0;
  font-size: var(--fs-sm);
  color: var(--text-primary);
  line-height: 1.7;
}
.tp-ap-caps {
  margin: 2px 0 0;
  font-size: var(--fs-xs);
  color: var(--text-muted);
  word-break: break-word;
}
.tp-ap-err {
  margin: var(--sp-2) 0 0;
  font-size: var(--fs-sm);
  color: var(--danger);
}
.tp-ap-actions {
  display: flex;
  gap: var(--sp-2);
  margin-top: var(--sp-3);
}
/* 展开历史：0fr→1fr 网格行做真实高度过渡（与工具卡一致） */
.tp-drawer {
  display: grid;
  grid-template-rows: 0fr;
  transition: grid-template-rows var(--dur-toggle) var(--ease);
}
.tp-drawer.open {
  grid-template-rows: 1fr;
}
.tp-drawer-clip {
  overflow: hidden;
}
.tp-drawer-body {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  padding: var(--sp-2) 0 var(--sp-1) var(--sp-3);
}
.tp-stage {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.tp-stage-head {
  display: flex;
  align-items: baseline;
  gap: var(--sp-2);
}
.tp-stage-index {
  font-size: var(--fs-xs);
  color: var(--text-faint);
}
.tp-stage-name {
  font-size: var(--fs-sm);
  color: var(--text-secondary);
}
.tp-stage-state {
  font-size: var(--fs-xs);
  color: var(--text-faint);
}
.tp-stage-state[data-state="running"] {
  color: var(--link);
}
.tp-note {
  margin: 0 0 0 var(--sp-4);
  font-size: var(--fs-sm);
  color: var(--text-muted);
  line-height: 1.7;
}
.tp-note[data-kind="warning"] {
  color: var(--warning);
}
.tp-legacy {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
@media (prefers-reduced-motion: reduce) {
  .tp-drawer {
    transition: none;
  }
}
</style>
