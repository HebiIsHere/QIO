<script setup lang="ts">
/**
 * 「上次退出时这条消息还没有执行」入口。
 *
 * 为什么需要它：被后端接受过、但没执行完的消息以前**彻底消失**（排队中的 turn
 * 只活在进程内存里）。后端现在把原文留在 `turn_journal` 台账里并如实下发
 * `interrupted_turns`，但界面从来没有渲染过它 —— 用户看到的是「我说过的话不见了」。
 *
 * 三条刻意的设计：
 *
 * 1. **不自动重发**：进程退出可能正是用户的意思，自动重放会重复花钱、重复产生回答。
 *    这里只负责把「这条没有被执行」说清楚，继续 / 忽略都由用户点。
 * 2. **不抢焦点**：和 ApprovalEntry 一样是一行常驻入口（不是弹窗、不 autofocus），
 *    默认收起；展开、收起都只动这一块自己的状态。
 * 3. **措辞不模糊**：不写「有新任务」这种话，直接写「上次退出时没有执行」，
 *    并给出原文（视觉截断 + 省略号，DOM 里保留全文供读屏软件读全）。
 */
import { computed, ref } from "vue";
import { useSessionStore } from "../stores/session";
import type { InterruptedTurn } from "../services/api";

const session = useSessionStore();
const open = ref(false);

const items = computed(() => session.interruptedTurns);
const count = computed(() => items.value.length);
const busyId = computed(() => session.interruptedBusyId);
const notice = computed(() => session.interruptedNotice);

/** 收起时的那一行：说清数量与「它没有被执行」，不猜也不含糊。 */
const summary = computed(() =>
  count.value === 1
    ? "有 1 条消息在上次退出时没有执行"
    : `有 ${count.value} 条消息在上次退出时没有执行`,
);
const entryLabel = computed(() =>
  `${summary.value}，展开可以继续发送或者忽略`,
);

function toggle() {
  open.value = !open.value;
}

/**
 * Esc 收起这一块（不动任何记录）。stopPropagation：这是最外层的常驻入口，
 * 别让按键顺带关掉用户正在用的别的东西。
 */
function onKeydown(event: KeyboardEvent) {
  if (event.key !== "Escape" || !open.value) return;
  event.stopPropagation();
  open.value = false;
}

/**
 * 何时被中断。台账里的时间都是 UTC ISO，用户的判断依据是本地时间。
 * 同一天只说时分；跨天补上日期 —— 看不出是哪天，用户没法判断这条还要不要。
 */
function formatWhen(item: InterruptedTurn): string {
  const raw = item.ended_at || item.updated_at || item.created_at;
  if (!raw) return "时间未知";
  const d = new Date(raw);
  if (Number.isNaN(d.getTime())) return "时间未知";
  const p = (n: number) => String(n).padStart(2, "0");
  const clock = `${p(d.getHours())}:${p(d.getMinutes())}`;
  const now = new Date();
  const sameDay =
    d.getFullYear() === now.getFullYear() &&
    d.getMonth() === now.getMonth() &&
    d.getDate() === now.getDate();
  return sameDay ? `今天 ${clock}` : `${d.getMonth() + 1} 月 ${d.getDate()} 日 ${clock}`;
}

/** 后端给的人话说明；没有就给一句不带猜测的兜底。 */
function reasonOf(item: InterruptedTurn): string {
  return item.reason_text?.trim() || "这条消息没有被执行";
}

function resume(item: InterruptedTurn) {
  void session.resumeInterruptedTurn(item.turn_id);
}

function dismiss(item: InterruptedTurn) {
  void session.dismissInterruptedTurn(item.turn_id);
}
</script>

<template>
  <!--
    注意根条件是 `count || notice`：409（这条已经被处理过）之后后端会说"没它了"，
    列表会变成空 —— 如果整块跟着一起消失，用户点完「继续」什么都看不到，
    那就是任务书禁止的"静默失败"。操作结果必须留在屏幕上。
  -->
  <div v-if="count || notice" class="interrupted-turns" @keydown="onKeydown">
    <button
      v-if="count"
      class="entry"
      type="button"
      :aria-expanded="open"
      :aria-label="entryLabel"
      @click="toggle"
    >
      <span class="mark" aria-hidden="true">↺</span>
      <span class="text">{{ summary }}</span>
      <span class="chev" :class="{ open }" aria-hidden="true">⌄</span>
    </button>

    <div v-if="open && count" class="panel" role="region" aria-label="上次退出时没有执行的消息">
      <p class="lead">
        这些消息已经被 QIO 收下，但没有执行。你可以继续发送，或者忽略。
      </p>
      <ul class="list">
        <li v-for="item in items" :key="item.turn_id" class="item">
          <!-- 视觉上两行截断（省略号可见）；DOM 里是全文，读屏软件听到的也是全文 -->
          <p class="message">{{ item.message }}</p>
          <p class="meta">
            <span class="when mono">{{ formatWhen(item) }}</span>
            <span class="reason">{{ reasonOf(item) }}</span>
          </p>
          <div class="actions">
            <button
              class="qio-btn primary btn-resume"
              type="button"
              :disabled="Boolean(busyId)"
              :aria-label="`继续发送这条：${item.message}`"
              @click="resume(item)"
            >
              {{ busyId === item.turn_id ? "提交中…" : "继续发送这条" }}
            </button>
            <button
              class="qio-btn quiet btn-dismiss"
              type="button"
              :disabled="Boolean(busyId)"
              :aria-label="`忽略这条：${item.message}`"
              @click="dismiss(item)"
            >
              忽略
            </button>
          </div>
        </li>
      </ul>
      <div v-if="count > 1" class="bulk">
        <button
          class="qio-btn quiet btn-dismiss-all"
          type="button"
          :disabled="Boolean(busyId)"
          :aria-label="`忽略全部 ${count} 条没有执行的消息`"
          @click="session.dismissAllInterruptedTurns()"
        >
          {{ busyId === "ALL" ? "处理中…" : `全部忽略（${count} 条）` }}
        </button>
      </div>
    </div>
    <!-- 操作结果放在面板外：列表被清空、面板收起时它仍然在 -->
    <p v-if="notice" class="notice" role="status" aria-live="polite">{{ notice }}</p>
  </div>
</template>

<style scoped>
/*
 * 位置由对话页负责（它是一条正常流的横幅，出现时把顶部固定提示条让开）；
 * 这里只描述这一块自己为什么长这样。
 */
.interrupted-turns {
  display: flex; flex-direction: column; align-items: center; gap: var(--sp-2);
  margin: var(--sp-3) auto var(--sp-2);
  max-width: min(560px, calc(100vw - 32px));
  font-family: var(--sans);
}
.entry {
  display: inline-flex; align-items: center; gap: 8px;
  padding: 6px 14px; border-radius: var(--r-pill);
  background: var(--bg-elevated); color: var(--text-strong);
  border: 1px solid var(--border-strong); font-size: 12.5px;
  box-shadow: var(--shadow-1); cursor: pointer;
  transition: transform var(--dur-press) var(--ease-out), border-color var(--dur-fast) var(--ease);
}
.entry:hover { border-color: var(--accent); }
.entry:active { transform: translateY(var(--press-shift)); }
.entry:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
.mark {
  display: inline-flex; align-items: center; justify-content: center;
  width: 16px; height: 16px; border-radius: 50%;
  background: var(--accent-soft); color: var(--accent);
  font-family: var(--mono); font-size: 11px; font-weight: 700;
}
.chev { color: var(--text-muted); transition: transform var(--dur-fast) var(--ease); }
.chev.open { transform: rotate(180deg); }

.panel {
  width: 100%; padding: var(--sp-3) var(--sp-4);
  border: 1px solid var(--border-subtle); border-radius: var(--r-lg);
  background: var(--bg-elevated); box-shadow: var(--shadow-2);
}
.lead { margin: 0 0 var(--sp-2); font-size: var(--fs-xs); color: var(--text-secondary); line-height: 1.6; }
.list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--sp-3); }
.item { display: flex; flex-direction: column; gap: 6px; }
.message {
  margin: 0; font-size: var(--fs-sm); color: var(--text-primary); line-height: 1.6;
  /* 截断可见：两行之外给省略号，而不是把话悄悄吞掉 */
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
  overflow: hidden; overflow-wrap: anywhere;
}
.meta { margin: 0; display: flex; flex-wrap: wrap; gap: 8px; align-items: baseline; font-size: var(--fs-xs); }
.when { color: var(--text-muted); }
.reason { color: var(--text-secondary); }
.actions { display: flex; gap: var(--sp-2); }
.bulk { margin-top: var(--sp-3); display: flex; justify-content: flex-end; }
.notice {
  margin: var(--sp-2) 0 0; font-size: var(--fs-xs); color: var(--text-secondary); line-height: 1.6;
}
</style>
