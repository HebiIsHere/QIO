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
 *
 * 每一行有三个互不混用的动作：
 * - 「继续开发」：把这件事再交给模型；
 * - 「撤销授权」：只收回「在某个环境里跑它的测试」的授权，任务还在做；
 * - 「放弃开发」：结束这项**没做完**的开发 —— 进终态、从未完成列表移除、
 *   收回执行授权；**不删**工作区文件与记录，**不动**已经注册的工具。
 */
import { computed, ref, watch } from "vue";
import QConfirm from "./ui/QConfirm.vue";
import { useSessionStore } from "../stores/session";
import type { DevTaskRow } from "../services/api";

const session = useSessionStore();
const open = ref(false);
/** 上一次「继续开发」没有交出去（后端没受理）：要如实说，不能让用户以为已经在做了 */
const resumeFailed = ref(false);
/** 撤销授权失败的原因（成功时为空）：不假装已经收回 */
const revokeFailed = ref("");
/** 正在展开确认层的那一行。同一时间只允许一个确认层（`""` = 没有） */
const confirmingId = ref("");
/**
 * 正在提交「放弃开发」请求的那一行（`""` = 没有请求在飞）。
 *
 * 放在组件里而不是 store：它表达的是**这个清单的**交互状态。非空时按钮禁用、
 * 文案变成「正在放弃…」，并且重复点击不会再发第二个请求（后端幂等，但界面上
 * 不该出现「点了两下、弹两条结果」）。
 */
const abandoningId = ref("");
/**
 * 放弃失败的原因。`null` = 没有失败。
 *
 * 带 `request` 是为了在清单下方说清**是哪一项**没放弃：清单可能很长，
 * 只说一句原因，用户对不上是哪一行。
 */
const abandonError = ref<{ id: string; request: string; message: string } | null>(null);

const items = computed(() => session.unfinishedDevTasks);

/**
 * 放弃确认层的说明。三件事缺一不可：
 * ① 结束这项开发并从未完成列表移除；② 文件与记录保留、不删已注册的工具；
 * ③ 同时收回执行授权、没回答的确认会失效。
 *
 * 开头点名**是哪一项**：清单可能很长，只说「这项开发」用户对不上是哪一行。
 */
function abandonDetail(task: DevTaskRow): string {
  return (
    `结束这项开发（${task.request}），并从未完成列表移除；` +
    "工作区文件与记录保留，不删除已注册的工具；" +
    "同时收回这个任务的执行授权，没回答的确认会失效。"
  );
}

/** 展开/收起。展开时顺手查一次授权范围：用户要看得到自己同意过什么。 */
function toggle() {
  open.value = !open.value;
  if (open.value) {
    revokeFailed.value = "";
    void session.refreshDevAuthorizations();
    return;
  }
  // 收起时把确认层一并收掉：再展开时不应看到一个「上次没回答」的确认
  confirmingId.value = "";
}

/**
 * 最后一项被移除（放弃成功、或后端列表刷新后不再有未完成任务）→ 收起面板。
 * 入口整行由 `v-if="items.length"` 一起消失：不能留下一个空面板或空入口。
 */
watch(items, (list) => {
  if (list.length) return;
  open.value = false;
  confirmingId.value = "";
  abandonError.value = null;
});

function authorizationFor(taskId: string) {
  return session.devAuthorizations.find((item) => item.task_id === taskId) ?? null;
}

/** 授权范围的一行人话：在哪儿跑、能不能联网、用哪个凭据。 */
function scopeText(taskId: string): string {
  const auth = authorizationFor(taskId);
  if (!auth) return "";
  const where = auth.isolated
    ? "容器隔离环境"
    : "本机受限子进程（同一用户权限，不是安全沙箱）";
  const parts = [`已授权在${where}里跑它的测试`];
  if (auth.filesystem.length) parts.push(`可访问目录：${auth.filesystem.join("、")}`);
  if (auth.network) {
    parts.push(
      auth.network_allow.length ? `联网：仅 ${auth.network_allow.join("、")}` : "联网：允许",
    );
  }
  if (auth.credentials.length) parts.push(`使用凭据：${auth.credentials.join("、")}`);
  return parts.join("；");
}

async function revoke(taskId: string) {
  revokeFailed.value = "";
  const ok = await session.revokeDevAuthorization(taskId);
  if (!ok) {
    revokeFailed.value =
      "没能撤销这次授权（后端没有确认），它现在仍然有效。可以再试一次。";
  }
}

/** 打开某一行的放弃确认层（先清掉上一次的失败原因）。 */
function askAbandon(taskId: string) {
  // 已经有请求在飞：不再开第二个确认层
  if (abandoningId.value) return;
  abandonError.value = null;
  confirmingId.value = taskId;
}

function cancelAbandon() {
  confirmingId.value = "";
}

/**
 * 用户确认放弃。
 *
 * 结果**只以后端为准**：`ok === true` 才移除条目（由 store 做），否则条目留在
 * 列表里、原因就地显示、按钮恢复可点可以重试。绝不做乐观移除 —— 把「请求发出去了」
 * 当成「已经放弃了」会让用户以为任务结束了，而它其实还在跑。
 */
async function confirmAbandon(task: DevTaskRow) {
  // 防重复提交：按钮在提交期间是禁用的，这里再挡一次 —— QConfirm 的确认按钮
  // 在请求返回前仍然可点，没有这道判断就会出现第二个请求。
  if (abandoningId.value) return;
  confirmingId.value = "";
  abandoningId.value = task.id;
  abandonError.value = null;
  try {
    const res = await session.abandonDevTask(task.id);
    if (!res.ok) {
      // 后端拒绝 / 请求没送达：这一条还在，如实说原因，允许重试
      abandonError.value = { id: task.id, request: task.request, message: res.message };
      return;
    }
    // 成功：store 已按后端结果移除条目；最后一项没了就收起面板
    if (!items.value.length) open.value = false;
  } finally {
    abandoningId.value = "";
  }
}

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
  abandoned: "已放弃",
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
      @click="toggle"
    >
      有 {{ items.length }} 个工具开发任务没做完
    </button>
    <div v-if="open" class="dev-task-panel">
      <!--
        列表自身是滚动容器：任务多、需求文字长时只在这里滚，背后的对话不动。
        tabindex + aria-label 让它可以被键盘聚焦后用方向键 / PageDown 滚动。
      -->
      <ul
        class="dev-task-list"
        tabindex="0"
        :aria-label="`未完成的工具开发任务，共 ${items.length} 项，可用方向键或 PageDown 滚动`"
      >
        <li v-for="task in items" :key="task.id" class="dev-task-row">
          <p class="dev-task-request">{{ task.request }}</p>
          <p class="dev-task-meta">
            <span>{{ phaseLabel(task.phase) }}</span>
            <span>{{ testText(task) }}</span>
            <span v-if="updatedText(task)">更新于 {{ updatedText(task) }}</span>
          </p>
          <p v-if="scopeText(task.id)" class="dev-task-scope">
            <span>{{ scopeText(task.id) }}</span>
            <button class="dev-task-revoke" type="button" @click="revoke(task.id)">
              撤销授权
            </button>
          </p>
          <div class="dev-task-actions">
            <button class="dev-task-resume" type="button" @click="resume(task)">继续开发</button>
            <button
              class="dev-task-abandon"
              type="button"
              :disabled="!!abandoningId"
              @click="askAbandon(task.id)"
            >
              {{ abandoningId === task.id ? "正在放弃…" : "放弃开发" }}
            </button>
          </div>
          <!-- 就地确认：说清结束开发 / 保留文件 / 不动已注册工具 / 收回授权 -->
          <QConfirm
            v-if="confirmingId === task.id"
            :open="true"
            variant="inline"
            tone="danger"
            title="放弃这项开发？"
            :detail="abandonDetail(task)"
            confirm-text="放弃开发"
            cancel-text="取消"
            @confirm="confirmAbandon(task)"
            @cancel="cancelAbandon"
          />
        </li>
      </ul>
      <!--
        失败原因放在滚动容器**之外**：清单滚到哪儿都看得见，不会被滚出视野。
        `role="alert"` 让读屏用户也能立刻听到「这次没有放弃」。
      -->
      <div v-if="abandonError" class="dev-task-failure">
        <p class="dev-task-error" role="alert">{{ abandonError.message }}</p>
        <p class="dev-task-error-which">
          「{{ abandonError.request }}」没有被放弃，它还在上面的列表里；可以再点一次「放弃开发」重试。
        </p>
      </div>
      <p v-if="revokeFailed" class="dev-task-error" role="alert">{{ revokeFailed }}</p>
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
  max-width: 100%;
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
  /*
   * 面板高度上限**随窗口可用高度变化**，不是写死的像素：
   * 视口高度 - 顶部提示条让位（--qio-top-notes-offset，由 ConversationView 按
   * 「还没设置完」横幅与「上次没执行」横幅的实测高度写进 :root，见 App.vue 的
   * .top-notes）- 这段预留。
   *
   * 预留的构成（逐项相加，别写成一个说不清来源的数）：
   *   入口行 30 + 入口与面板间距 8 = 38
   *   面板上下内边距 20
   *   错误提示区（在滚动容器之外，必须始终可见）42
   *   底部呼吸 16                    → 116
   * 再留 20 余量：同一个 .top-notes 容器里可能还有一行待确认的审批。
   * 窗口越矮，列表越早开始滚动 —— 入口常驻可见，最后一项始终滚得到。
   */
  --dev-panel-reserve: 136px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-height: calc(100vh - var(--qio-top-notes-offset, 12px) - var(--dev-panel-reserve));
  /* 动态视口高度更贴合真机（地址栏/工具栏收起时不再被裁掉）；旧内核落到上一行 */
  max-height: max(180px, calc(100dvh - var(--qio-top-notes-offset, 12px) - var(--dev-panel-reserve)));
  width: min(560px, calc(100vw - 32px));
  padding: 10px 12px; border-radius: var(--r-2);
  background: var(--bg-elevated); border: 1px solid var(--border-subtle);
  box-shadow: var(--shadow-2); pointer-events: auto;
  font-family: var(--sans); font-size: 12.5px; color: var(--text-primary);
  text-align: left;
}
/* 内部滚动容器：滚它不带动背后的对话（overscroll-behavior: contain）。 */
.dev-task-list {
  flex: 1 1 auto; min-height: 0;
  margin: 0; padding: 0 6px 12px 0; list-style: none;
  display: flex; flex-direction: column; gap: 10px;
  overflow-y: auto; overscroll-behavior: contain;
  /* 细而安静的滚动条：沿用现有 token，不引入新颜色 */
  scrollbar-width: thin; scrollbar-color: var(--border-strong) transparent;
}
.dev-task-list:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
.dev-task-list::-webkit-scrollbar { width: 8px; }
.dev-task-list::-webkit-scrollbar-track { background: transparent; }
.dev-task-list::-webkit-scrollbar-thumb {
  background: var(--border-strong); border-radius: var(--r-pill);
}
.dev-task-list::-webkit-scrollbar-thumb:hover { background: var(--text-faint); }
.dev-task-row { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
/* 长需求文字在窄窗口里换行而不是撑出横向滚动（也不许把按钮挤出可见区） */
.dev-task-request {
  margin: 0; color: var(--text-strong);
  overflow-wrap: anywhere; word-break: break-word;
}
.dev-task-meta { margin: 0; display: flex; flex-wrap: wrap; gap: 10px; color: var(--text-muted); font-size: 12px; }
/* 授权范围：只读的一行事实 + 一个「撤销授权」动作 */
.dev-task-scope {
  margin: 0; display: flex; flex-wrap: wrap; align-items: center; gap: 8px;
  color: var(--text-muted); font-size: 12px;
  overflow-wrap: anywhere; word-break: break-word;
}
.dev-task-revoke {
  padding: 3px 9px; border-radius: var(--r-pill);
  background: transparent; color: var(--text-secondary);
  border: 1px solid var(--border-subtle);
  font-family: var(--sans); font-size: 12px; cursor: pointer;
}
.dev-task-revoke:hover { color: var(--danger); border-color: var(--danger); }
.dev-task-revoke:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
/* 动作行：窄窗口换行，不横向溢出，也不把按钮挤出可见区 */
.dev-task-actions {
  display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-top: 2px;
}
.dev-task-resume {
  padding: 4px 10px; border-radius: var(--r-pill);
  background: transparent; color: var(--accent);
  border: 1px solid var(--border-subtle);
  font-family: var(--sans); font-size: 12.5px; cursor: pointer;
}
.dev-task-resume:hover { border-color: var(--accent); }
.dev-task-resume:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
/* 与「撤销授权」同档的安静按钮：危险的是确认层里的那一下，不是入口本身 */
.dev-task-abandon {
  padding: 4px 10px; border-radius: var(--r-pill);
  background: transparent; color: var(--text-secondary);
  border: 1px solid var(--border-subtle);
  font-family: var(--sans); font-size: 12.5px; cursor: pointer;
  transition: color var(--dur-fast) var(--ease), border-color var(--dur-fast) var(--ease);
}
.dev-task-abandon:hover { color: var(--danger); border-color: var(--danger); }
.dev-task-abandon:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
.dev-task-abandon:disabled {
  color: var(--text-faint); border-color: var(--border-subtle); cursor: default;
}
/* 失败原因：在滚动容器之外，任何滚动位置都看得见 */
.dev-task-failure { flex: none; margin-top: 2px; }
/*
 * 确认层的说明里会出现需求原文（可能是一长串没有空格的文字）。
 * `.qio-confirm` 是子组件的根元素、会带上本组件的 scope id，所以这里能命中它；
 * 内部元素用 :deep —— 否则长串会把面板撑宽、撑出横向滚动。
 */
.qio-confirm { min-width: 0; max-width: 100%; }
.qio-confirm :deep(.qio-confirm__detail) {
  overflow-wrap: anywhere; word-break: break-word;
}
.dev-task-error {
  flex: none; margin: 8px 0 0; color: var(--danger);
  overflow-wrap: anywhere; word-break: break-word;
}
.dev-task-failure .dev-task-error { margin-top: 0; }
.dev-task-error-which {
  flex: none; margin: 2px 0 0; color: var(--text-muted); font-size: 12px;
  overflow-wrap: anywhere; word-break: break-word;
}
</style>
