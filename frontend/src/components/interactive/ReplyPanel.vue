<!--
  QIO 回复与任务区域（子智能体 C）。

  契约 §1.6 / §4.4：
  - 区域可收起；待审批的每一项都有虚线预览与就近的批准 / 拒绝入口；
  - 同一批达到 4 项时提供简洁列表（部分 / 全部、批量批准或拒绝、点击定位）；
  - 需要更新的预览写明原因并禁止批准；影响说明列出对象、任务、后果并提供继续 / 取消；
  - 所有判定（冲突、依赖、材料变化）由服务端给出，这里只显示结果；
  - 演示内容一律写明「演示」。
-->
<script setup lang="ts">
import { computed, ref } from "vue";
import { useInteractiveStore } from "../../stores/interactive";
import {
  BATCH_MIN,
  impactSections,
  previewBounds,
  revertSections,
  statusText,
} from "../../interactive/approval";
import type { Intent } from "../../interactive/types";
import DemoIntentEntry from "./DemoIntentEntry.vue";
import IntentBatchList from "./IntentBatchList.vue";
import IntentImpactNotice from "./IntentImpactNotice.vue";
import IntentPreviewCard from "./IntentPreviewCard.vue";

const store = useInteractiveStore();

const selection = ref<string[]>([]);
/** 已经处理过「等待你决定」提示的意图（本阶段只结束提示，不自动改动板面） */
const decided = ref<string[]>([]);
const focused = ref<string | null>(null);
const note = ref("");
const error = ref<string | null>(null);
const busy = ref(false);

const pending = computed(() => store.pendingIntents);
const active = computed(() => store.activeIntents);
const settled = computed(() => store.settledIntents);
const showBatch = computed(() => store.batchAvailable || pending.value.length >= BATCH_MIN);

const decisionNotices = computed(() =>
  settled.value.filter(
    (intent) =>
      (intent.revert?.pendingDecision?.length ?? 0) > 0 && !decided.value.includes(intent.id),
  ),
);

const openCount = computed(() => pending.value.length);
const runningCount = computed(() => active.value.length);

function settledLine(intent: Intent): string {
  const text = statusText(intent);
  const reason = intent.revert?.reasonText ?? intent.reason ?? "";
  return reason ? text.label + "：" + reason : text.label + "：" + text.detail;
}

async function onApprove(intentId: string, confirmDependency: boolean): Promise<void> {
  busy.value = true;
  error.value = null;
  try {
    const result = await store.approve(intentId, confirmDependency);
    note.value = result.ok
      ? result.detail ?? "已批准。"
      : "没有批准：" + (result.detail ?? result.reason ?? "服务端未说明原因");
  } catch (err) {
    error.value = (err as Error).message;
  } finally {
    busy.value = false;
  }
}

async function onReject(intentId: string): Promise<void> {
  busy.value = true;
  error.value = null;
  try {
    const result = await store.reject(intentId);
    note.value = result.ok
      ? result.detail ?? "已拒绝：预览消失，板面原内容保留。"
      : "没有拒绝：" + (result.detail ?? result.reason ?? "服务端未说明原因");
  } catch (err) {
    error.value = (err as Error).message;
  } finally {
    busy.value = false;
  }
}

async function onDecide(payload: { approve: string[]; reject: string[] }): Promise<void> {
  if (!payload.approve.length && !payload.reject.length) {
    note.value = "没有可提交的选项：未选中的项继续等待。";
    return;
  }
  busy.value = true;
  error.value = null;
  try {
    const result = await store.decideBatch(payload.approve, payload.reject);
    const failed = result.results.filter((item) => !item.ok);
    note.value =
      "批量处理完成：批准 " +
      result.approved.length +
      " 项，拒绝 " +
      result.rejected.length +
      " 项；未选中的继续等待。" +
      (failed.length
        ? "未成功 " +
          failed.length +
          " 项（" +
          failed.map((item) => item.detail ?? item.reason ?? "服务端未说明").join("；") +
          "）。"
        : "");
    selection.value = [];
  } catch (err) {
    error.value = (err as Error).message;
  } finally {
    busy.value = false;
  }
}

/** 定位：面板内滚动到该卡片，并通知板面（若板面监听了这个事件）预览的位置。 */
function locate(intentId: string): void {
  focused.value = intentId;
  const intent = store.intentById(intentId);
  const bounds = intent ? previewBounds(intent.preview) : null;
  if (typeof window !== "undefined" && typeof window.dispatchEvent === "function") {
    window.dispatchEvent(
      new CustomEvent("qio:interactive:locate-preview", {
        detail: { intentId, bounds },
      }),
    );
  }
  if (typeof document !== "undefined") {
    const selector = '[data-im="intent"][data-intent-id="' + intentId + '"]';
    const element = document.querySelector(selector);
    if (element && typeof element.scrollIntoView === "function") {
      element.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }
  }
  if (bounds) {
    note.value =
      "已定位：" +
      intentId +
      " 的虚线预览在板面坐标 (" +
      bounds.x +
      ", " +
      bounds.y +
      ")，大小 " +
      bounds.w +
      "×" +
      bounds.h +
      "。";
  }
}

function dismissDecision(intentId: string): void {
  decided.value = [...decided.value, intentId];
  note.value = "已结束这条提示：板面没有任何自动改动。";
}
</script>

<template>
  <section class="reply-panel" data-im="reply-panel">
    <header class="panel-head">
      <h2 class="panel-title">QIO 回复与任务</h2>
      <p class="counts">
        待审批 {{ openCount }} 项 · 执行中 {{ runningCount }} 项 · 已结束
        {{ settled.length }} 项
      </p>
      <button
        class="link-btn"
        type="button"
        data-im="reply-collapse"
        @click="store.auxOpen = false"
      >
        收起
      </button>
    </header>

    <p v-if="store.recoverNotice.length" class="line warn" role="status">
      上次没有结束的任务已经暂停（{{ store.recoverNotice.length }} 项）：重新打开不会自动继续。
    </p>
    <p v-if="note" class="line" role="status" data-im="reply-note">{{ note }}</p>
    <p v-if="error" class="line err" role="alert">操作失败：{{ error }}</p>

    <section class="block">
      <h3 class="block-title">待审批（{{ openCount }} 项）</h3>
      <p v-if="!pending.length" class="line">现在没有待审批的预览。</p>
      <div v-for="intent in pending" :key="intent.id" class="item">
        <IntentPreviewCard
          :intent="intent"
          :all-intents="store.intents"
          :active="focused === intent.id"
          @approve="onApprove"
          @reject="onReject"
          @locate="locate"
        />
      </div>
      <IntentBatchList
        v-if="showBatch && pending.length"
        :intents="pending"
        :selected="selection"
        @update:selected="selection = $event"
        @decide="onDecide"
        @locate="locate"
      />
    </section>

    <section v-if="decisionNotices.length" class="block">
      <h3 class="block-title">等待你决定的撤回</h3>
      <IntentImpactNotice
        v-for="intent in decisionNotices"
        :key="intent.id"
        :title="'「' + intent.title + '」失败后还有改动没有撤回'"
        :sections="revertSections(intent.revert)"
        tone="warning"
        confirm-label="继续（保留这些改动）"
        cancel-label="取消（暂不撤回）"
        note="本阶段没有单独的「决定」接口：这两个按钮只结束提示、不会自动改动板面。要撤回其余部分，可以先在板面上手动删除。"
        @confirm="dismissDecision(intent.id)"
        @cancel="dismissDecision(intent.id)"
      />
    </section>

    <section class="block">
      <h3 class="block-title">执行中 / 已暂停（{{ runningCount }} 项）</h3>
      <p v-if="!active.length" class="line">现在没有执行中的任务。</p>
      <div v-for="intent in active" :key="intent.id" class="item">
        <IntentPreviewCard
          :intent="intent"
          :all-intents="store.intents"
          :active="focused === intent.id"
          @approve="onApprove"
          @reject="onReject"
          @locate="locate"
        />
        <IntentImpactNotice
          v-if="intent.impact.tasks.length"
          :title="'执行中要改相关材料时，会先说明影响'"
          :sections="impactSections(intent.impact)"
          tone="info"
          confirm-label="继续（改动生效，任务暂停并保留进度）"
          cancel-label="取消（不改动，任务继续）"
          :note="'影响说明只展示，不会自动改动板面；任务「' + intent.title + '」当前状态：' + statusText(intent).label + '。'"
          @confirm="note = '已了解影响：本阶段不会自动改动板面，需要你在板面上修改后再提交。'"
          @cancel="note = '已取消：板面没有改动，任务状态保持不变。'"
        />
      </div>
    </section>

    <section class="block">
      <h3 class="block-title">已结束（{{ settled.length }} 项）</h3>
      <p v-if="!settled.length" class="line">还没有结束的任务。</p>
      <ul class="settled">
        <li
          v-for="intent in settled"
          :key="intent.id"
          class="settled-row"
          data-im="intent"
          :data-intent-id="intent.id"
          :data-intent-status="intent.status"
        >
          <span class="settled-title">
            <span v-if="intent.demo" class="badge demo">演示</span>
            {{ intent.title }}
          </span>
          <span class="settled-text">{{ settledLine(intent) }}</span>
        </li>
      </ul>
    </section>

    <DemoIntentEntry />
  </section>
</template>

<style scoped>
.reply-panel {
  padding: var(--sp-3) var(--sp-4) var(--sp-5);
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
}
.panel-head {
  display: flex;
  align-items: baseline;
  gap: var(--sp-2);
  flex-wrap: wrap;
}
.panel-title {
  margin: 0;
  font-family: var(--serif);
  font-size: var(--fs-md);
  color: var(--text-strong);
  flex: 1;
}
.counts {
  margin: 0;
  flex-basis: 100%;
  font-size: var(--fs-xs);
  color: var(--text-muted);
}
.link-btn {
  font: inherit;
  font-size: var(--fs-xs);
  color: var(--link);
  background: none;
  border: none;
  padding: 0;
  cursor: pointer;
}
.link-btn:hover { color: var(--accent-hover); }
.link-btn:focus-visible { outline: 2px solid var(--link); outline-offset: 2px; }
.block {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}
.block-title {
  margin: 0;
  font-family: var(--serif);
  font-size: var(--fs-sm);
  color: var(--text-secondary);
}
.item {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}
.line {
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--text-muted);
  line-height: var(--lh-base);
}
.line.warn { color: var(--warning); }
.line.err { color: var(--danger); }
.settled {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
}
.settled-row {
  display: flex;
  flex-direction: column;
  gap: 2px;
  border-top: 1px solid var(--border-subtle);
  padding-top: var(--sp-1);
}
.settled-title {
  font-size: var(--fs-xs);
  color: var(--text-primary);
}
.settled-text {
  font-size: var(--fs-xs);
  color: var(--text-muted);
  line-height: var(--lh-base);
}
.badge {
  font-size: var(--fs-xs);
  border-radius: var(--r-pill);
  padding: 0 var(--sp-1);
  border: 1px solid var(--warning);
  color: var(--warning);
  background: var(--warning-soft);
}
</style>
