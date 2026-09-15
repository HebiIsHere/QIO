<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from "vue";
import { useApprovalsStore } from "../stores/approvals";
import type { ApprovalItem } from "../stores/approvals";
import { usePresence } from "../composables/usePresence";
import QNumber from "./ui/QNumber.vue";

const approvals = useApprovalsStore();
/** 退出动画期间还要渲染内容：保留一份「最后显示的审批」快照 */
const shownItem = ref<ApprovalItem | null>(null);
watch(
  () => approvals.current,
  (cur) => {
    if (cur) shownItem.value = cur;
  },
  { immediate: true },
);
const presence = usePresence(() => !!approvals.current && approvals.visible, 150);
const item = computed(
  () => (approvals.visible ? approvals.current : null) ?? (presence.mounted.value ? shownItem.value : null),
);
const dialogRef = ref<HTMLElement | null>(null);
/** 提交中的 approval_id（与 store.responding 同步；用于按钮文案/禁用） */
const submitting = computed(() => !!item.value && approvals.responding === item.value.approval_id);

const KIND_LABELS: Record<string, string> = {
  tool_create: "工具创建审批",
  credential_grant: "凭据授权审批",
  high_impact_knowledge: "高影响知识确认",
};
/** 标题一律说人话：后端出现新 kind 时也不要显示英文枚举名给用户 */
function kindTitle(kind: string): string {
  return KIND_LABELS[kind] ?? "需要你确认的操作";
}

const isSubagentCreate = computed(() => {
  const p = (item.value?.payload ?? {}) as Record<string, unknown>;
  return item.value?.kind === "tool_create" && p.tool_type === "subagent";
});

// 子 agent 预算（编辑后批准时随 overrides 提交）
const budgetForm = ref({
  max_iterations: 5,
  max_tokens: 100000,
  output_limit_chars: 2000,
});
watch(
  () => item.value?.approval_id,
  () => {
    const p = (item.value?.payload ?? {}) as Record<string, unknown>;
    const b = (p.subagent_budget ?? {}) as Record<string, unknown>;
    budgetForm.value = {
      max_iterations: Number(b.max_iterations ?? 5),
      max_tokens: Number(b.max_tokens ?? 100000),
      output_limit_chars: Number(b.output_limit_chars ?? 2000),
    };
  },
  { immediate: true },
);

/**
 * 提交失败时把焦点收回对话框：失败会保留待审批项，用户应当能立刻用键盘重试，
 * 而不是焦点掉到 body（实测：点「拒绝」失败后焦点标签是 BODY）。
 */
watch(
  () => approvals.error,
  async (err) => {
    if (!err) return;
    await nextTick();
    dialogRef.value?.focus();
  },
);

function approveWithBudget() {
  approvals.respond("approved", {
    subagent_budget: {
      max_iterations: budgetForm.value.max_iterations ?? 5,
      max_tokens: budgetForm.value.max_tokens ?? 100000,
      output_limit_chars: budgetForm.value.output_limit_chars ?? 2000,
    },
  });
}

const payloadView = computed(() => {
  const p = (item.value?.payload ?? {}) as Record<string, unknown>;
  const lines: { label: string; value: string }[] = [];
  if (p.name) lines.push({ label: "工具", value: String(p.name) });
  if (p.tool_type) {
    const typeLabel = p.tool_type === "subagent" ? "子 agent 型" : "函数型";
    lines.push({ label: "类型", value: typeLabel });
  }
  if (p.key_id) lines.push({ label: "凭据", value: String(p.key_id) });
  if (p.tool_name) lines.push({ label: "授权工具", value: String(p.tool_name) });
  if (p.content) lines.push({ label: "知识内容", value: String(p.content) });
  if (p.action) lines.push({ label: "建议动作", value: String(p.action) });
  if (p.example) lines.push({ label: "示例请求", value: String(p.example) });
  if (p.source_count) lines.push({ label: "相似请求数", value: String(p.source_count) });
  // 说明/原因/测试摘要已由上面的结构化行（它想做什么 / 为什么需要 / 验证情况）回答，
  // 这里不再重复一遍；技术明细统一进默认折叠的「高级详情」。
  return lines.filter((l) => l.value !== intent.value);
});

/** 该工具会访问什么（人话能力清单，来自后端 policy.describe()）。 */
const capabilities = computed<string[]>(() => {
  const p = (item.value?.payload ?? {}) as Record<string, unknown>;
  const raw = p.capabilities;
  if (!Array.isArray(raw)) return [];
  return raw.map((x) => String(x));
});

/**
 * 它想做什么：一句人类语言。
 * 顺序上先取「这个工具是干什么的」（description/工具名），把 explanation/reason
 * 留给下面单独的「为什么需要」一行，避免两行说同一句话。
 */
const intent = computed(() => {
  const p = (item.value?.payload ?? {}) as Record<string, unknown>;
  const first = [p.description, p.tool_name, p.name, p.explanation, p.reason]
    .map((v) => (typeof v === "string" ? v.trim() : ""))
    .find((v) => v.length > 0);
  return first ?? "该操作需要你的授权";
});

/** 后端 policy.describe() 里的「副作用：read|write|destructive|pure」，用于回答「它会改变什么」 */
const sideEffect = computed(() => {
  const raw = capabilities.value.find((c) => c.startsWith("副作用："));
  return raw ? raw.slice("副作用：".length).trim().toLowerCase() : "";
});

/** 它会改变什么：把内部枚举翻成具体行为，不把 pure/write 这类英文值丢给用户 */
const changeSummary = computed(() => {
  switch (sideEffect.value) {
    case "destructive":
      return "会删除或覆盖已有数据";
    case "write":
      return "会写入或修改数据";
    case "read":
      return "只读取，不修改数据";
    case "pure":
      return "不修改任何数据";
    default:
      return "";
  }
});

/** 为什么需要：解释/原因，与「它想做什么」重复时不再重复显示 */
const whyNeeded = computed(() => {
  const p = (item.value?.payload ?? {}) as Record<string, unknown>;
  const first = [p.explanation, p.reason]
    .map((v) => (typeof v === "string" ? v.trim() : ""))
    .find((v) => v.length > 0);
  return first && first !== intent.value ? first : "";
});

/**
 * 测试了吗：verified / unverified。
 * 后端在 tool_create 里给 test_summary（子 agent 型工具是 "n/a (subagent)"）。
 * 拿不到任何测试信息时返回 null —— 宁可不显示，也不假装「已验证」。
 */
const verification = computed(() => {
  const p = (item.value?.payload ?? {}) as Record<string, unknown>;
  const summary = typeof p.test_summary === "string" ? p.test_summary.trim() : "";
  const details = Array.isArray(p.test_details) ? (p.test_details as unknown[]) : [];
  if (!summary && !details.length) return null;
  const unverified = summary === "" || /^n\/a/i.test(summary);
  return {
    verified: !unverified,
    label: unverified ? "未验证" : "已验证",
    detail: unverified && /subagent/i.test(summary) ? "子 agent 型工具，没有自动测试报告" : summary,
  };
});

/**
 * 高级详情（默认折叠）：raw params / 策略指纹 / 逐条测试结果。
 * 普通用户第一眼只需要「做什么 / 会改变什么 / 为什么 / 验证了吗」。
 */
const advanced = computed(() => {
  const p = (item.value?.payload ?? {}) as Record<string, unknown>;
  const lines: string[] = [];
  if (p.policy_fingerprint) lines.push(`策略指纹：${String(p.policy_fingerprint)}`);
  const details = Array.isArray(p.test_details) ? (p.test_details as Record<string, unknown>[]) : [];
  for (const d of details) {
    const name = String(d.name ?? "未命名检查");
    const passed = d.passed ? "通过" : "失败";
    const detail = d.detail ? ` · ${String(d.detail)}` : "";
    lines.push(`测试 ${name}：${passed}${detail}`);
  }
  let raw = "";
  try {
    raw = JSON.stringify(p, null, 2);
  } catch {
    raw = String(p);
  }
  return { lines, raw };
});

/** 风险描述：不用 Low/Medium/High，用具体行为描述（来自后端能力清单）。 */
const risks = computed<string[]>(() => {
  const out: string[] = [];
  const has = (prefix: string) => capabilities.value.some((c) => c.startsWith(prefix));
  const yes = (prefix: string) => capabilities.value.some((c) => c.startsWith(prefix) && c.includes("是"));
  if (yes("联网：")) out.push("会联网");
  if (yes("写入文件：")) out.push("会修改文件");
  if (capabilities.value.some((c) => /读取文件：是/.test(c))) out.push("会读取文件");
  if (yes("启动进程：")) out.push("会执行命令");
  if (capabilities.value.some((c) => c.startsWith("使用凭据：") && !c.endsWith("无"))) {
    out.push("会使用凭据");
  }
  if (!out.length && capabilities.value.length) out.push("只读");
  return out;
});

/**
 * 高风险（会联网 / 写文件 / 执行命令 / 用凭据）：
 * 用于给批准按钮降调 —— 高风险操作不该看起来像「推荐你点它」。
 */
const highRisk = computed(() => risks.value.some((r) => r !== "只读" && r !== "会读取文件"));

/**
 * 「它会访问什么」的展示清单：去掉「副作用：write」这类内部枚举，
 * 因为「会改变什么」已经用中文回答过了；原始值仍可在高级详情里看到。
 */
const capabilityList = computed(() => capabilities.value.filter((c) => !c.startsWith("副作用：")));

/**
 * 焦点管理：
 * - 审批出现时把焦点移进对话框（失败后重试也走这里）；
 * - 审批关闭时把焦点还给「被打断之前正在操作的元素」。
 *   后台审批可能叠在别的表单上，关掉之后焦点落到 body 会让用户
 *   既不知道回到哪里，也无法接着敲键盘。
 */
let restoreFocusTo: HTMLElement | null = null;
watch(
  // 注意：这里必须盯真实的队列项与「窗口是否可见」，而不是带退出快照的 item，
  // 否则「队列清空 / 稍后处理」这一步的变化会被快照抹平，焦点就还不回去。
  () => [approvals.current?.approval_id ?? null, approvals.visible] as const,
  async ([id], prev) => {
    // immediate 调用时没有旧值，必须给默认值
    const [prevId, prevVisible] = prev ?? [null, false];
    if (id && approvals.visible) {
      if (!prevId || !prevVisible) {
        const active = document.activeElement;
        // 如果焦点本来就在对话框里（稍后处理再打开），不要把它记成「被打断的元素」
        if (!dialogRef.value?.contains(active as Node)) {
          restoreFocusTo = active instanceof HTMLElement ? active : null;
        }
      }
      await nextTick();
      dialogRef.value?.focus();
      return;
    }
    const target = restoreFocusTo;
    restoreFocusTo = null;
    if (target && target.isConnected) {
      await nextTick();
      target.focus();
    }
  },
  { immediate: true },
);

onMounted(async () => {
  await nextTick();
  dialogRef.value?.focus();
});

/**
 * 键盘行为：Tab 在对话框内循环；Esc 不做决定，但按最上层处理——收起窗口并保留
 * 待审批项（同时亮出「有 N 项操作等待确认」入口），不是「看不见的待办」。
 */
function onKeydown(e: KeyboardEvent) {
  if (e.key === "Escape") {
    e.preventDefault();
    approvals.defer();
    return;
  }
  if (e.key !== "Tab") return;
  const root = dialogRef.value;
  if (!root) return;
  // summary 也是键盘可达元素（高级详情），必须参与 Tab 循环，否则焦点可能从它溜到对话框外
  const focusables = Array.from(
    root.querySelectorAll<HTMLElement>(
      'button:not([disabled]), input:not([disabled]), summary, [href], [tabindex]:not([tabindex="-1"])',
    ),
  );
  if (!focusables.length) return;
  const first = focusables[0];
  const last = focusables[focusables.length - 1];
  const active = document.activeElement as HTMLElement | null;
  if (e.shiftKey && (active === first || active === root)) {
    e.preventDefault();
    last.focus();
  } else if (!e.shiftKey && active === last) {
    e.preventDefault();
    first.focus();
  }
}
</script>

<template>
  <div v-if="presence.mounted.value && item" class="modal-mask" :class="{ leaving: presence.leaving.value }">
    <div
      ref="dialogRef"
      class="modal"
      :class="{ 'risk-high': highRisk }"
      role="dialog"
      aria-modal="true"
      :aria-labelledby="'approval-title'"
      tabindex="-1"
      @keydown="onKeydown"
    >
      <h3 id="approval-title" :title="item.kind">{{ kindTitle(item.kind) }}</h3>
      <div class="body">
        <!-- 1) 它想做什么 -->
        <p class="intent">{{ intent }}</p>
        <!-- 2) 它会访问什么：具体行为，不用 Low/Medium/High -->
        <div v-if="risks.length" class="risk-row">
          <span v-for="r in risks" :key="r" class="risk">{{ r }}</span>
        </div>
        <!-- 3) 会改变什么 / 为什么需要 / 验证了吗 -->
        <dl v-if="changeSummary || whyNeeded || verification" class="facts">
          <template v-if="changeSummary">
            <dt>会改变什么</dt>
            <dd>{{ changeSummary }}</dd>
          </template>
          <template v-if="whyNeeded">
            <dt>为什么需要</dt>
            <dd>{{ whyNeeded }}</dd>
          </template>
          <template v-if="verification">
            <dt>验证情况</dt>
            <dd>
              <span class="verdict" :class="verification.verified ? 'ok' : 'warn'">
                {{ verification.label }}
              </span>
              <span v-if="verification.detail" class="verdict-detail">{{ verification.detail }}</span>
            </dd>
          </template>
        </dl>
        <div v-for="line in payloadView" :key="line.label" class="row">
          <span class="label">{{ line.label }}</span>
          <span class="value">{{ line.value }}</span>
        </div>
        <p v-if="!payloadView.length && !capabilities.length" class="hint">无附加信息</p>
        <div v-if="capabilityList.length" class="cap-box">
          <div class="cap-title">它会访问什么</div>
          <ul class="cap-list">
            <li v-for="c in capabilityList" :key="c">{{ c }}</li>
          </ul>
        </div>
        <!-- 4) 高级详情：raw params / 策略指纹 / 逐条测试结果，默认折叠 -->
        <details v-if="advanced.lines.length || advanced.raw" class="adv">
          <summary>高级详情</summary>
          <ul v-if="advanced.lines.length" class="adv-list">
            <li v-for="l in advanced.lines" :key="l">{{ l }}</li>
          </ul>
          <pre class="adv-raw mono">{{ advanced.raw }}</pre>
        </details>
        <div v-if="isSubagentCreate" class="budget-box">
          <div class="budget-title">子 agent 执行预算（可修改后批准）</div>
          <div class="budget-row">
            <label>最大迭代</label>
            <QNumber v-model="budgetForm.max_iterations" :min="1" :max="50" mono label="最大迭代" />
            <label>最大 token</label>
            <QNumber v-model="budgetForm.max_tokens" :min="1000" :step="1000" mono label="最大 token" />
            <label>输出上限(字)</label>
            <QNumber v-model="budgetForm.output_limit_chars" :min="100" mono label="输出上限" />
          </div>
        </div>
      </div>
      <p v-if="approvals.error" class="approval-error" role="alert">{{ approvals.error }}</p>
      <div class="actions">
        <button class="reject" :disabled="submitting" @click="approvals.respond('rejected')">
          拒绝
        </button>
        <button
          class="later"
          :disabled="submitting"
          title="只收起窗口，保留待审批任务：既不批准也不拒绝"
          @click="approvals.defer()"
        >
          稍后处理
        </button>
        <button
          v-if="isSubagentCreate"
          class="approve"
          :disabled="submitting"
          @click="approveWithBudget"
        >
          {{ submitting ? "提交中…" : "允许此次操作（含预算）" }}
        </button>
        <button v-else class="approve" :disabled="submitting" @click="approvals.respond('approved')">
          {{ submitting ? "提交中…" : "允许此次操作" }}
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.modal-mask {
  position: fixed; inset: 0; z-index: 200; background: var(--bg-overlay);
  display: flex; align-items: center; justify-content: center;
  /* 出现：立即有可见变化随后减速；退出：短淡出后卸载。
     注意退出期间遮罩仍拦截点击（不让误点落到下层危险操作），
     但内容本身不可交互；退出结束整块从 DOM 移除，不留透明残留。 */
  animation: approval-mask-in var(--dur-menu) var(--ease-out) both;
  transition: opacity var(--dur-exit) var(--ease-in);
}
.modal-mask.leaving { opacity: 0; }
.modal-mask.leaving .modal { pointer-events: none; }
@keyframes approval-mask-in { from { opacity: 0; } to { opacity: 1; } }
.modal {
  width: 480px; max-width: 90vw; background: var(--bg-elevated); border: 1px solid var(--border-strong);
  border-radius: var(--r-lg); padding: 18px 20px; color: var(--text-primary);
  box-shadow: 0 24px 60px rgba(0, 0, 0, 0.45);
  animation: approval-modal-in var(--dur-menu) var(--ease-out) both;
  transition: transform var(--dur-exit) var(--ease-in), opacity var(--dur-exit) var(--ease-in);
}
.modal-mask.leaving .modal { transform: translateY(2px) scale(.995); opacity: 0; }
@keyframes approval-modal-in { from { opacity: 0; transform: translateY(6px) scale(.99); } to { opacity: 1; transform: none; } }
.modal:focus { outline: none; }
.modal:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
.modal h3 { margin: 0 0 10px; font-size: 16px; color: var(--text-strong); }
/* 内容变多（.facts 与高级详情）后，320px 会把「它想做什么」挤出首屏——
   审批弹窗的第一句必须一眼可见，所以按视口给高度，仍然允许内部滚动。 */
.body { max-height: min(58vh, 420px); overflow-y: auto; }
.intent { font-size: 13.5px; line-height: 1.6; color: var(--text-primary); margin-bottom: 8px; }
.risk-row { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 10px; }
.risk {
  font-family: var(--mono); font-size: 11px; letter-spacing: 0.03em;
  padding: 2px 9px; border-radius: var(--r-pill);
  border: 1px solid var(--border-strong); color: var(--text-secondary);
}
.row { display: flex; gap: 10px; margin-bottom: 8px; font-size: 13px; }
.label { color: var(--text-secondary); min-width: 64px; flex-shrink: 0; }
.value { color: var(--text-primary); }
.hint { color: var(--text-muted); font-size: 12px; }
/* 「会改变什么 / 为什么需要 / 验证情况」：固定两列，标签用次要色，值必须可读 */
.facts { margin: 0 0 10px; display: grid; grid-template-columns: 76px 1fr; gap: 4px 10px; }
.facts dt { color: var(--text-secondary); font-size: 12.5px; }
.facts dd { margin: 0; color: var(--text-primary); font-size: 12.5px; line-height: 1.5; }
.verdict {
  font-family: var(--mono); font-size: 11px; letter-spacing: 0.03em;
  padding: 1px 7px; border-radius: var(--r-pill); border: 1px solid var(--border-strong);
  color: var(--text-secondary);
}
.verdict.ok { color: var(--success); border-color: var(--success); }
.verdict.warn { color: var(--warning); border-color: var(--warning); }
.verdict-detail { color: var(--text-secondary); margin-left: 8px; }
/* 高级详情：默认折叠，普通用户不第一眼看到 raw params */
.adv { margin-top: 10px; border-top: 1px solid var(--border-subtle); padding-top: 8px; }
.adv > summary {
  cursor: pointer; font-size: 12.5px; color: var(--text-secondary);
  list-style: none; display: flex; align-items: center; gap: 6px;
}
.adv > summary::-webkit-details-marker { display: none; }
.adv > summary::before {
  content: "›"; display: inline-block; color: var(--text-muted);
  transition: transform var(--dur-toggle) var(--ease);
}
.adv[open] > summary::before { transform: rotate(90deg); }
.adv > summary:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
.adv-list { margin: 6px 0 0; padding-left: 18px; font-size: 12px; color: var(--text-secondary); }
.adv-raw {
  margin: 8px 0 0; padding: 8px 10px; max-height: 160px; overflow: auto;
  background: var(--bg-inset); border: 1px solid var(--border-subtle); border-radius: var(--r-sm);
  font-size: 11.5px; line-height: 1.5; color: var(--text-secondary);
  white-space: pre-wrap; word-break: break-all;
}
.budget-box { margin-top: 10px; padding: 10px; border: 1px solid var(--border-subtle); border-radius: 8px; }
.cap-box { margin-top: 10px; padding: 10px; border: 1px solid var(--border-subtle); background: var(--bg-inset); border-radius: 8px; }
.cap-title { font-size: 12.5px; color: var(--text-strong); margin-bottom: 6px; }
.cap-list { margin: 0; padding-left: 18px; font-size: 12px; color: var(--text-secondary); }
.cap-list li { margin: 2px 0; }
.budget-title { font-size: 12px; color: var(--text-secondary); margin-bottom: 8px; }
.budget-row { display: flex; align-items: center; gap: 8px; font-size: 12px; flex-wrap: wrap; }
.budget-row label { color: var(--text-secondary); }
.budget-row .q-number { width: 108px; }
.actions { display: flex; justify-content: flex-end; gap: 10px; margin-top: 16px; }
.actions button {
  border: none; border-radius: 18px; padding: 8px 26px; cursor: pointer; font-size: 13px;
  min-width: 92px; /* 处理中改文案也不会让按钮尺寸跳动 */
  transition: transform var(--dur-press) var(--ease-out), filter var(--dur-fast) var(--ease);
}
.actions button:active:not(:disabled) { transform: translateY(var(--press-shift)); }
/* 「稍后处理」：只收起窗口、保留待审批任务。外观比拒绝更轻，避免看起来像第三种决定 */
.later {
  background: transparent; color: var(--text-secondary); border: 1px dashed var(--border-strong);
}
.later:hover:not(:disabled) { color: var(--text-primary); border-color: var(--text-muted); }
/* 按钮层级（任务 04 PART C3）：
   批准＝primary（品牌强调色），拒绝＝secondary（中性描边）。
   高风险时批准按钮降调为中性实心：它是「确认执行」，不是品牌在推荐你点。 */
.approve { background: var(--accent); color: var(--on-accent); border: 1px solid transparent; }
.approve:hover:not(:disabled) { background: var(--accent-hover); }
.risk-high .approve {
  background: var(--bg-inset); color: var(--text-strong); border-color: var(--border-strong);
}
.risk-high .approve:hover:not(:disabled) { background: var(--bg-surface); }
/* 选择器带 .actions 以提高优先级：上面 `.actions button { border: none }` 会覆盖裸 .reject */
.actions .reject {
  background: transparent; color: var(--text-primary); border: 1px solid var(--border-strong);
}
.actions .reject:hover:not(:disabled) { background: var(--bg-inset); }
.actions button:disabled { opacity: 0.5; }
.actions button:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
.approval-error {
  margin-top: 12px;
  padding: 8px 12px;
  border: 1px solid var(--danger);
  border-radius: 8px;
  background: var(--danger-soft);
  color: var(--danger);
  font-size: 12.5px;
  line-height: 1.5;
}
</style>
