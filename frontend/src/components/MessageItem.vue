<script setup lang="ts">
import { vQioTip } from "../composables/useQioTip";
import { computed, ref } from "vue";
import MarkdownContent from "./MarkdownContent.vue";
import ToolCreationCard from "./ToolCreationCard.vue";
import AttachmentChip from "./AttachmentChip.vue";
import { mediaFactsOf } from "./mediaFacts";
import { splitSystemAnnotation, useSessionStore } from "../stores/session";
import { useEventStore } from "../stores/events";
import { useUiStore } from "../stores/ui";
import type { MessageAttachment, StreamMessage } from "../stores/session";
import {
  hasExecutionTaskLocator,
  locateExecutionTask,
} from "./conversation/taskLocator";
import {
  canPreviewAttachment,
  hasConversationPreviewOpener,
  hasConversationReplyCodeOpener,
  openConversationPreview,
  openConversationReplyCode,
  previewTargetForAttachment,
} from "./conversation/previewBridge";
import {
  addPendingAttachment,
  pickBrowserFile,
  pickLocalPath,
  prepareAttachment,
  relocateAttachment,
  retryAttachment,
  uploadAttachment,
  waitUntilSettled,
  type AttachmentRef,
} from "../services/attachments";

const props = defineProps<{ message: StreamMessage; showTopic?: boolean }>();
const session = useSessionStore();
const events = useEventStore();
const ui = useUiStore();

/**
 * token 下缀：只显示「这条消息所属 turn」的用量（turn_id 归属），
 * 且只在开发者模式显示。后端当前只提供单 turn 总 token，
 * 没有 input/output 分解就不显示分解数字（不编造）。
 */
const tokText = computed(() => {
  if (!ui.developerMode) return "";
  const usage = events.turnUsageFor(props.message.turnId);
  if (!usage) return "";
  return `token ${usage.tokens.toLocaleString("en-US")}`;
});

const open = ref(false);
/** 本次会话新产生的消息：最多一次很短的入场；历史消息不带这个标记 */
const isFresh = computed(() => session.freshIds.includes(props.message.id));
/** 复制结果：只有真的写进剪贴板才显示「已复制」 */
const copyState = ref<"idle" | "ok" | "fail">("idle");
let copyTimer: ReturnType<typeof setTimeout> | null = null;

const copyLabel = computed(() => {
  if (copyState.value === "ok") return "已复制";
  if (copyState.value === "fail") return "复制失败";
  return "复制";
});

/** 复制消息内容；剪贴板不存在或写入被拒绝时给出失败反馈，由用户手动选择复制 */
async function copyContent() {
  const text = props.message.content ?? "";
  if (!text) return;
  let ok = false;
  try {
    const cb = navigator.clipboard;
    if (cb && typeof cb.writeText === "function") {
      await cb.writeText(text);
      ok = true;
    }
  } catch {
    ok = false;
  }
  copyState.value = ok ? "ok" : "fail";
  if (copyTimer) clearTimeout(copyTimer);
  copyTimer = setTimeout(() => {
    copyState.value = "idle";
    copyTimer = null;
  }, ok ? 1600 : 2400);
}

/** 呈现优先：title 兜底工具名 */
const toolTitle = computed(
  () => props.message.presentation?.title || props.message.toolName || "工具调用",
);

/**
 * 工具卡状态：运行中就说运行中，不为「还在跑」画一个 ✓。
 * 用户必须能分辨「正在执行」与「已完成」（spec 第 32~34 条）。
 *
 * 数据模型里区分五种语义（运行中 / 成功 / 失败 / 已取消 / 结果未收到）；
 * 视觉上沿用既有的卡片状态（取消与未知复用最接近的失败外观），
 * 但文案必须说出真实状态 —— 「取消」不是「失败」，「结果未收到」也不是。
 */
const toolState = computed<"running" | "success" | "failed" | "cancelled" | "unknown">(() => {
  if (props.message.toolStatus) return props.message.toolStatus;
  // 老数据（历史 / 测试构造的消息）没有 toolStatus：按旧字段兜底
  if (props.message.toolRunning === true) return "running";
  return props.message.toolOk === false ? "failed" : "success";
});
const toolRunning = computed(() => toolState.value === "running");
const toolFailed = computed(() => !toolRunning.value && toolState.value !== "success");
const toolMark = computed(() => {
  if (toolRunning.value) return "◌";
  if (toolState.value === "cancelled") return "⊘";
  if (toolState.value === "unknown") return "?";
  return toolFailed.value ? "✕" : "✓";
});
/**
 * 卡片上的状态徽标：运行中 / 已取消由语义决定；
 * 其余（成功、失败）沿用工具自己的呈现文案，没有就不显示（不制造噪声）。
 */
const toolStateLabel = computed(() => {
  if (toolRunning.value) return "运行中";
  if (toolState.value === "cancelled") return "已取消";
  return props.message.presentation?.status || "";
});

/** 耗时：只有拿得到且有意义（≥0.1s）时才显示，不编造数字 */
const durationText = computed(() => {
  const ms = props.message.toolDurationMs;
  if (typeof ms !== "number" || !Number.isFinite(ms) || ms < 100) return "";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
});

/**
 * 工具失败时卡片上的一行结论（完整错误仍在折叠详情里）。
 *
 * 上限从 60 放到 120 字：60 字会吃掉「列目录失败：[WinError 3] …」这类真正
 * 有信息量的原因，用户只看到一句笼统的「这次执行没有成功」，等于又静默了一次。
 */
const FAILURE_LINE_LIMIT = 120;
const toolFailureLine = computed(() => {
  if (!toolFailed.value) return "";
  const raw = (props.message.toolError ?? "").trim();
  if (raw) {
    if (raw.length <= FAILURE_LINE_LIMIT) return raw;
    return `${raw.slice(0, FAILURE_LINE_LIMIT)}…（展开可看完整原因）`;
  }
  if (toolState.value === "cancelled") return "这次执行已取消";
  if (toolState.value === "unknown") return "结果未收到";
  return "这次执行没有成功";
});

/** 独立任务的状态文案（用户看到的是「独立任务」，不是内部智能体进程） */
const SUBAGENT_LABELS: Record<string, string> = {
  queued: "开始",
  running: "进行中",
  done: "已完成",
  failed: "失败",
};
const subagentLabel = computed(
  () => SUBAGENT_LABELS[props.message.taskStatus ?? "running"] ?? "进行中",
);
const subagentRunning = computed(
  () => props.message.taskStatus === "queued" || props.message.taskStatus === "running",
);

/** 原始工具名：只用于悬停提示 / 排查，不作为界面标题（界面标题是中文展示名） */
const rawToolName = computed(
  () => props.message.presentation?.tool || props.message.toolName || "工具",
);

/** 呈现优先：summary 兜底原始内容预览 */
const toolSummary = computed(
  () => props.message.presentation?.summary || props.message.content || "",
);

/** 工具调用历史的全文（参数 + 输出）：展开时才去取，避免历史页一次带上几十万字 */
const toolArgsText = computed(() => props.message.toolArgs ?? "");
const toolRecordLoading = computed(() => props.message.toolRecordLoading === true);
const toolRecordError = computed(() => props.message.toolRecordError ?? "");
/** 库里没有输出正文时，说清是「没保存」还是「按保留期清掉了」 */
const toolMissingNote = computed(() => {
  if (!props.message.toolOutputMissing) return "";
  return props.message.toolMissingReason === "setting"
    ? "完整输出未保存（设置里关闭了「保存工具输出全文」）"
    : "完整输出已按保留设置清理";
});

/**
 * 展开卡片：首次展开时按记录 id 取全文（参数 + 输出）。
 *
 * 实时与历史两种卡片走同一条路径 —— 实时事件只带 200 字预览，
 * 历史预览 400 字，全文都只在库里。
 */
function toggleTool() {
  open.value = !open.value;
  if (!open.value) return;
  const m = props.message;
  if (!m.toolRecordId || m.toolRecordLoaded || m.toolRecordLoading) return;
  void session.loadToolRecord(m.id);
}

function formatTime(iso?: string): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

const topicLine = computed(() => {
  // 只展示话题名，不暴露内部话题 ID（此前 slice(-4) 会显示"话题 #5320"这类编号，
  // 用户误以为是回答内容）
  return props.message.topicName || session.topicName || "默认话题";
});

/** 单行 metadata：时间 +（仅开发者模式）本 turn token */
const metaText = computed(() => {
  const time = formatTime(props.message.createdAt);
  return tokText.value ? `${time} · ${tokText.value}` : time;
});

/**
 * 「后端已核对」那一行：只有这一轮真的核对过（模型调了 declare_completion 且
 * 后端逐条对上）才有。它是**事实说明**，不是装饰 —— 没有就不占位置。
 */
const verifiedText = computed(() => {
  const fact = props.message.verified;
  if (!fact) return "";
  return fact.basis ? `后端已核对：${fact.basis}` : "后端已核对";
});

/**
 * 系统核对注记（后端事实，不是模型的说法）：契约 §七 C1 要求它在**独立「系统事实」区域**
 * 渲染 —— 所以这里把正文与注记切开：正文照常走 Markdown（打字机只作用于正文），
 * 注记单独成块。同一段文字只出现一次（content 里本来就只存了一份）。
 */
const answerParts = computed(() => splitSystemAnnotation(props.message.content ?? ""));
const answerBody = computed(() => answerParts.value.body);
const systemNote = computed(() => answerParts.value.annotation);

/**
 * 用户消息上的附件行。
 *
 * 只展示**真实拿到的元数据**（名称 / 大小 / 保存方式 / 可用性 / 失败原因）；
 * 只有 id 没有元数据时如实说「元数据未加载」，绝不编造文件名或状态。
 */
const attachmentChips = computed(() => props.message.attachments ?? []);

/**
 * 「在右侧查看」：只读预览（C 的 store 通过 previewBridge 注册）。
 * 身份按契约 §4.3 用 attachment id（同名不同文件是两个标签），没有 version 时
 * 身份退化为 attachment:<id>；**不用文件名当身份**。未接线时不渲染按钮。
 */
const previewAvailable = hasConversationPreviewOpener();
/** 回复里的代码块 → 右侧只读预览（身份 replycode:<messageId>:<blockIndex>）。 */
const replyCodePreviewAvailable = hasConversationReplyCodeOpener();
interface CodeBlockRef {
  index: number;
  language: string;
  text: string;
}
const codeBlocks = computed<CodeBlockRef[]>(() => {
  if (props.message.role !== "assistant" || props.message.interim) return [];
  const source = String(props.message.content || "");
  const blocks: CodeBlockRef[] = [];
  const fence = /(?:^|\n)(`{3,}|~{3,})([^\n]*)\n([\s\S]*?)\n\1/g;
  let match: RegExpExecArray | null;
  let index = 0;
  while ((match = fence.exec(source)) !== null && blocks.length < 4) {
    blocks.push({ index, language: String(match[2] || "").trim(), text: match[3] });
    index += 1;
  }
  return blocks;
});
const replyCodeNotice = ref("");
function previewCodeBlock(block: CodeBlockRef): void {
  const ok = openConversationReplyCode({
    messageId: String(props.message.id || ""),
    blockIndex: block.index,
    text: block.text,
    language: block.language || undefined,
    // 标签本身会带上类别（「代码」）；这里只给语言，避免出现「ts 代码代码」。
    title: block.language || "回复中的代码",
  });
  // 成功不弹说明：右侧预览打开本身就是反馈；只在真的没接上时保留失败原因。
  replyCodeNotice.value = ok ? "" : "右侧预览还没有接上（不会打开空白面板）";
}
function canPreview(attachment: MessageAttachment): boolean {
  return previewAvailable && canPreviewAttachment({ name: attachment.name });
}
function previewOne(attachment: MessageAttachment): void {
  const ok = openConversationPreview(
    previewTargetForAttachment({
      id: attachment.id,
      name: attachment.name,
      topicId: attachment.topicId ?? null,
      storedPath: attachment.storedPath ?? null,
      sourcePath: attachment.sourcePath ?? null,
    }),
  );
  attachNotice.value = ok
    ? `已在右侧打开「${attachment.name}」的只读预览`
    : "这个版本还没有接上右侧预览（不会打开空白面板）";
}

/**
 * 简短执行记录 → 左侧任务栏定位（A 的 store 通过 taskLocator 注册）。
 * 没有注册时不渲染按钮；定位失败会如实说明，不假装已经定位。
 */
const locateAvailable = hasExecutionTaskLocator();
const locateNotice = ref("");
const locateRef = computed(() => {
  if (props.message.role === "tool") {
    // 任务栏的工具执行身份是 tool_records.id（不是 callId）；优先用真实记录 id，
    // 没有记录 id 时才退回 callId（定位会如实显示「没有找到」，不假装成功）。
    const sourceId = props.message.toolRecordId || props.message.callId || props.message.id;
    return sourceId
      ? { source: "tool" as const, sourceId, turnId: props.message.turnId ?? null, title: toolTitle.value }
      : null;
  }
  if (props.message.role === "subagent") {
    const sourceId = props.message.taskId || props.message.id;
    return sourceId
      ? { source: "subagent" as const, sourceId, turnId: props.message.turnId ?? null, title: props.message.toolName ?? undefined }
      : null;
  }
  return null;
});
const canLocate = computed(() => locateAvailable && Boolean(locateRef.value));
function locateTask(): void {
  const ref = locateRef.value;
  if (!ref) return;
  const ok = locateExecutionTask(ref);
  locateNotice.value = ok
    ? "已在左侧任务栏定位这条执行记录"
    : "左侧任务栏里没有找到这条执行记录（它可能已经被清理）";
}


// -- 多媒体附件的读取事实（契约 §4.4）：只显示已经发生的事 ------------------

/**
 * 多媒体事实行（与附件行分开）：每一条都带**真实原因**与**时间位置**。
 *
 * 五态词是分开报告的（附件已保存 → 预览可用 → 内容已处理 → 已交付模型 → 回答已完成）：
 * 这里只说「本次读到/没读到什么」，绝不把「已交付」说成「模型已理解」。
 */
const mediaFactRows = computed(() => {
  const list = props.message.mediaFacts ?? [];
  return list.map((raw, index) => {
    const fact = mediaFactsOf({
      source: raw.source,
      reason: raw.reason,
      transcript: raw.transcript,
      range: raw.range ?? null,
      frames: raw.frames ?? null,
      engine: raw.engine ?? null,
      bytes: raw.bytes,
    });
    const id = String(raw.id ?? "");
    const attached = id ? attachmentChips.value.find((item) => item.id === id) : undefined;
    return {
      key: (id || "media") + "-" + index,
      name: attached?.name || String(raw.label ?? "") || "未命名媒体",
      fact,
      at: formatTime(raw.at),
    };
  });
});

// -- 附件行（契约 §1.6）：显示交给 AttachmentChip，动作接在这里 ----------

/** 正在处理的附件 id（打开 / 重新定位 / 重试共用）：防重复点击 */
const attachBusyId = ref<string | null>(null);
/** 附件操作的结果：失败必须留在这一行上，不能只写控制台 */
const attachNotice = ref("");

/** 就地更新一条附件（重新定位 / 重试会返回后端确认过的新状态） */
function applyAttachment(next: AttachmentRef) {
  const list = props.message.attachments;
  if (!list) return;
  const index = list.findIndex((a) => a.id === next.id);
  if (index >= 0) list[index] = next;
}

/**
 * 打开附件副本（契约 §1.6）：走附件服务（浏览器 fetch 副本 / 桌面原生打开），
 * **绝不接受任意路径**。服务还没提供这个入口时如实说明，不给一个假动作。
 */
async function openOne(id: string) {
  const ref = attachmentChips.value.find((a) => a.id === id);
  if (!ref || attachBusyId.value) return;
  attachBusyId.value = id;
  attachNotice.value = "";
  try {
    const mod = (await import("../services/attachments")) as unknown as {
      openAttachment?: (attachment: AttachmentRef) => Promise<void> | void;
    };
    if (typeof mod.openAttachment !== "function") {
      attachNotice.value = "这个版本还没有提供「打开副本」的入口（缺少附件内容接口）";
      return;
    }
    await mod.openAttachment(ref);
  } catch (e) {
    attachNotice.value = `打开没有成功：${(e as Error).message}（可以重试）`;
  } finally {
    attachBusyId.value = null;
  }
}

/**
 * 重新定位引用型附件（契约 §1.6）：原生选择器给新路径 → 后端重新校验
 * 大小 / 保存方式 / 状态；失败如实显示，仍可再试。
 */
async function relocateOne(id: string) {
  const ref = attachmentChips.value.find((a) => a.id === id);
  if (!ref || attachBusyId.value) return;
  attachBusyId.value = id;
  attachNotice.value = "";
  try {
    const path = await pickLocalPath();
    if (!path) return; // 用户取消：什么也没发生，不能假装成功
    const updated = await relocateAttachment(id, path);
    applyAttachment(updated);
    attachNotice.value = `已重新定位：${updated.name}`;
  } catch (e) {
    attachNotice.value = `重新定位没有成功：${(e as Error).message}（可以重试）`;
  } finally {
    attachBusyId.value = null;
  }
}

/**
 * 「重新上传」（契约 §1.4，浏览器字节上传）：QIO 手里没有内容、也没有原地址，
 * 只能由用户重新给一次文件。
 *
 * 结果归属（F04）：新附件进入**发起操作话题**的待发送附件列表（显示可使用入口并持久化），
 * 原历史记录与旧 turn 的附件归属**保持不变** —— 不只是在后端建一条记录 + 弹一句成功。
 */
async function reuploadOne(id: string) {
  const ref = attachmentChips.value.find((a) => a.id === id);
  if (!ref || attachBusyId.value) return;
  attachBusyId.value = id;
  attachNotice.value = "";
  // 发起话题：优先当前话题，退化才用附件自身记录的话题事实
  const topicId = session.currentTopicId ?? ref.topicId ?? null;
  try {
    let created: AttachmentRef | null = null;
    const path = await pickLocalPath();
    if (path) {
      created = await prepareAttachment(path, { topicId, name: ref.name });
    } else {
      const file = await pickBrowserFile();
      if (!file) return; // 用户取消：什么也没发生，不假装成功
      created = await uploadAttachment(file, { topicId });
    }
    if (!created) return;
    // 成功创建后立即进入发起话题的待发送列表（持久化 + 广播给 Composer）
    addPendingAttachment(topicId, created);
    attachNotice.value =
      `已重新上传并加入待发送附件：${created.name}；原来那条「${ref.name}」的历史记录保留（QIO 无法从原地址恢复）`;
    // 还在后台准备的：继续跟进，就绪后更新待发送列表里的同一条
    if (created.state === "prepared") {
      void waitUntilSettled(created)
        .then((settled) => addPendingAttachment(topicId, settled))
        .catch(() => {
          /* 状态没跟到：待发列表里的 chip 会显示准备中，不谎报成功 */
        });
    }
  } catch (e) {
    attachNotice.value = `重新上传没有成功：${(e as Error).message}（可以重试）`;
  } finally {
    attachBusyId.value = null;
  }
}

/** 失败 / 变化后重试同一行（不新建附件） */
async function retryOne(id: string) {
  if (attachBusyId.value) return;
  attachBusyId.value = id;
  attachNotice.value = "";
  try {
    const updated = await retryAttachment(id);
    applyAttachment(updated);
  } catch (e) {
    attachNotice.value = `重试没有成功：${(e as Error).message}`;
  } finally {
    attachBusyId.value = null;
  }
}

/**
 * 已发出消息上的「移除」：附件是这条消息的一部分，不能在这里抹掉。
 * 说清楚而不是让按钮看起来能点却没反应。
 */
function removeOne(id: string) {
  const ref = attachmentChips.value.find((a) => a.id === id);
  attachNotice.value = `这条消息已经发出：${ref?.name ?? "附件"} 是消息的一部分，不能在这里移除`;
}
</script>

<template>
  <div class="message" :class="[message.role, { fresh: isFresh }]" data-qio-fit>
    <!--
      消息宽度链根：data-qio-fit = min-width:0 + max-width:100% + overflow-x:clip。
      横向溢出的真正修复在下面的布局（min-width:0 / 换行 / 收缩），clip 只是边界保护。
    -->
    <template v-if="message.role === 'user'">
      <div class="bubble user-bubble" data-qio-fit>
        <div class="plain">{{ message.content }}</div>
      </div>
      <!--
        附件行（契约 §1.6）：显示与「打开 / 重新定位 / 重试 / ×」入口由 C 的
        AttachmentChip 负责，这里把事件接到真实动作，并把结果就地反馈。
      -->
      <div
        v-if="attachmentChips.length"
        class="attach-row"
        data-qio-fit
        data-test="message-attachments"
      >
        <span
          v-for="a in attachmentChips"
          :key="a.id"
          class="attach-item"
          data-test="message-attachment"
          :data-id="a.id"
          :data-kind="a.kind"
          :data-state="a.state"
        >
          <AttachmentChip
            :attachment="a"
            :busy="attachBusyId === a.id"
            :previewable="canPreview(a)"
            @open="openOne"
            @relocate="relocateOne"
            @retry="retryOne"
            @reupload="reuploadOne"
            @remove="removeOne"
            @preview="previewOne(a)"
          />
        </span>
      </div>
      <!--
        多媒体附件的读取事实（契约 §4.4）：已交付 / 抽样 / 转写 / 未支持，
        各带**真实原因**与时间位置。这里只报告已经发生的事，不推断模型理解了什么。
      -->
      <div
        v-if="mediaFactRows.length"
        class="media-facts"
        data-qio-fit
        data-test="message-media-facts"
      >
        <p
          v-for="row in mediaFactRows"
          :key="row.key"
          class="media-fact"
          :data-test="'media-fact'"
          :data-source="row.fact.source"
          :data-read="row.fact.read ? 'true' : 'false'"
        >
          <span class="media-fact-head">
            <span class="media-fact-name" data-test="media-fact-name">{{ row.name }}</span>
            <span class="media-fact-state" data-test="media-fact-label">{{ row.fact.label }}</span>
            <span v-if="row.at" class="media-fact-at mono" data-test="media-fact-at">{{ row.at }}</span>
          </span>
          <span class="media-fact-detail" data-test="media-fact-detail">{{ row.fact.detail }}</span>
          <span
            v-if="row.fact.transcript"
            class="media-fact-transcript"
            data-test="media-fact-transcript"
          >{{ row.fact.transcript }}</span>
        </p>
      </div>
      <!-- 受理回执与界面不一致：这条消息实际没带上附件（绝不假装带上了） -->
      <p
        v-if="message.attachmentNotice"
        class="attach-notice"
        role="status"
        data-qio-fit
        data-test="attachment-receipt-notice"
      >
        {{ message.attachmentNotice }}
      </p>
      <p v-if="attachNotice" class="attach-notice" role="status" data-qio-fit data-test="attachment-notice">
        {{ attachNotice }}
      </p>
      <p
        v-else-if="!attachmentChips.length && message.attachmentIds?.length"
        class="attach-note mono"
        data-qio-fit
      >
        带了 {{ message.attachmentIds.length }} 个附件（元数据未加载）
      </p>
      <div class="meta mono" data-qio-fit>
        <span v-if="message.queued" class="queued-tag">等待中</span>
        <span class="ts">{{ formatTime(message.createdAt) }}</span>
        <button v-if="!message.queued" class="copy-btn" type="button" :class="{ fail: copyState === 'fail' }" @click="copyContent">
          {{ copyLabel }}
        </button>
      </div>
    </template>

    <template v-else-if="message.role === 'tool'">
      <div
        class="tool-card qio-card"
        data-qio-fit
        :class="{ fail: toolFailed, running: toolRunning, open }"
        :data-state="toolRunning ? 'running' : toolFailed ? 'failed' : 'ready'"
        v-qio-tip="rawToolName"
      >
        <button
          class="tool-head"
          type="button"
          @click="toggleTool"
          :aria-expanded="open"
          v-qio-tip="open ? '收起' : '展开'"
        >
          <span
            class="tool-mark"
            :class="toolRunning ? 'running' : toolFailed ? 'fail' : 'ok'"
          >
            {{ toolMark }}
          </span>
          <span class="tool-name mono">{{ toolTitle }}</span>
          <span
            v-if="toolRunning || toolStateLabel"
            class="tool-status qio-state"
            :class="toolRunning ? 'running info' : toolFailed ? 'fail err' : 'ok'"
          >
            {{ toolStateLabel }}
          </span>
          <span v-if="durationText" class="tool-duration mono">{{ durationText }}</span>
          <span class="tool-time mono">{{ formatTime(message.createdAt) }}</span>
          <span class="tool-chev">{{ open ? "▾" : "▸" }}</span>
        </button>
        <!-- 执行记录 → 左侧任务栏定位（A/Lead 注册后才出现） -->
        <button
          v-if="canLocate"
          class="task-locate"
          type="button"
          v-qio-tip="'在左侧任务栏定位这条执行记录（已归档的会定位到历史）'"
          @click="locateTask"
        >
          在任务栏查看
        </button>
        <p v-if="locateNotice" class="task-locate-note" role="status">{{ locateNotice }}</p>
        <!-- 结论写在卡面上：失败不能只藏在折叠详情里 -->
        <p v-if="toolFailureLine" class="tool-fail-line" role="status" data-qio-fit>
          {{ toolFailureLine }}
        </p>
        <!-- 展开/收起必须连续：用 0fr→1fr 网格行做真实高度过渡，
             不再 v-show 瞬切（detail-wrap 不设内边距，否则收起时会留下细条） -->
        <div class="tool-detail-wrap" :class="{ open }">
          <div class="tool-detail-clip">
            <div class="tool-detail" data-qio-fit>
              <!-- 参数与输出分开：参数是「它想做什么」，输出是「结果是什么」 -->
              <div v-if="toolArgsText" class="tool-detail-sec" data-qio-fit>
                <div class="sec-label mono">参数</div>
                <pre data-qio-fit>{{ toolArgsText }}</pre>
              </div>
              <div class="tool-detail-sec" data-qio-fit>
                <div class="sec-label mono">输出</div>
                <pre data-qio-fit>{{ toolSummary }}</pre>
              </div>
              <p v-if="toolRecordLoading" class="tool-note mono" role="status" data-qio-fit>
                正在读取完整输出…
              </p>
              <p v-else-if="toolRecordError" class="tool-note err" role="status" data-qio-fit>
                {{ toolRecordError }}
                <button class="link" type="button" @click="session.loadToolRecord(message.id)">
                  重试
                </button>
              </p>
              <p v-else-if="toolMissingNote" class="tool-note mono" role="status" data-qio-fit>
                {{ toolMissingNote }}
              </p>
              <p v-else-if="message.toolTruncated" class="tool-note mono" role="status" data-qio-fit>
                输出过长，已截断（单条上限 4 万字）
              </p>
              <p v-if="message.toolError" class="tool-error" data-qio-fit>{{ message.toolError }}</p>
            </div>
          </div>
        </div>
      </div>
    </template>

    <!-- 独立任务：与「普通工具」明确区分（spec 第 61~65 条） -->
    <template v-else-if="message.role === 'subagent'">
      <div
        class="subagent-card qio-card"
        data-qio-fit
        :class="{ fail: message.taskStatus === 'failed' }"
        :data-state="subagentRunning ? 'running' : message.taskStatus === 'failed' ? 'failed' : 'ready'"
      >
        <div class="sub-head">
          <span class="sub-kind qio-tag">独立任务</span>
          <span class="sub-name serif">{{ message.toolName || "未命名任务" }}</span>
          <span
            class="sub-state qio-state"
            :class="subagentRunning ? 'running info' : message.taskStatus === 'failed' ? 'err' : 'ok'"
          >{{ subagentLabel }}</span>
        </div>
        <button
          v-if="canLocate"
          class="task-locate"
          type="button"
          v-qio-tip="'在左侧任务栏定位这项独立任务'"
          @click="locateTask"
        >
          在任务栏查看
        </button>
        <p v-if="locateNotice" class="task-locate-note" role="status">{{ locateNotice }}</p>
        <p class="sub-line">
          QIO 正在单独处理这项任务{{ subagentRunning ? "" : "（已完成）" }}
        </p>
        <p v-if="message.taskGoal" class="sub-goal" data-qio-fit>目标：{{ message.taskGoal }}</p>
        <p v-if="!subagentRunning && message.content" class="sub-result" data-qio-fit>
          {{ message.content }}
        </p>
        <p v-if="message.taskStatus === 'failed'" class="sub-fail" role="status" data-qio-fit>
          这项独立任务没有完成：{{ message.toolError || "展开后可让 QIO 继续处理" }}
        </p>
      </div>
    </template>

    <!-- 工具创建：一条流程一张卡，原地推进 -->
    <template v-else-if="message.role === 'tool_creation'">
      <ToolCreationCard :message="message" />
    </template>

    <!--
      中间话（interim）**不是气泡**：它是过程说明，统一由过程区（TurnProcess）渲染。
      这里保留同一条渲染路径，所以整段文字在 DOM 里只出现一次。
    -->
    <template v-else-if="message.role === 'assistant' && message.interim">
      <p class="process-line" data-qio-fit data-test="process-line">{{ message.content }}</p>
    </template>

    <template v-else>
      <!-- 流式生成中的正文不逐字播报给辅助阅读工具（aria-busy + 不设 live 区域），
           落定后由正常文档流阅读即可。 -->
      <div
        class="bubble assist-bubble"
        data-qio-fit
        :aria-busy="message.streaming ? 'true' : undefined"
        :aria-live="message.streaming ? 'off' : undefined"
      >
        <div v-if="showTopic" class="tname serif">{{ topicLine }}</div>
        <!--
          打字机只用于「一次整段到达」的文本（旧后端整段推送）。
          已经被增量更新过的消息（assistantGrew）必须立刻显示收到的全文：
          增量本身就是节奏，再叠一层逐字点亮就等于让用户看不到已经到达的回答。
        -->
        <MarkdownContent
          :source="answerBody"
          :reveal="!!message.streaming && !message.assistantGrew"
          :cps="ui.typewriterCps"
          :pace-ms="message.paceMs ?? null"
        />
        <p v-if="verifiedText" class="verified-note mono" role="note" data-qio-fit>
          {{ verifiedText }}
        </p>
        <div
          v-if="codeBlocks.length && replyCodePreviewAvailable"
          class="code-preview-row"
          data-qio-fit
          data-test="reply-code-preview"
        >
          <button
            v-for="block in codeBlocks"
            :key="block.index"
            class="code-preview-btn"
            type="button"
            :aria-label="'在右侧查看这段' + (block.language || '') + '代码（只读）'"
            v-qio-tip="'在右侧只读预览这段' + (block.language || '') + '代码'"
            @click="previewCodeBlock(block)"
          >
            <span class="code-preview-glyph" aria-hidden="true">
              <svg viewBox="0 0 24 24">
                <path d="M8 6l-5 6 5 6M16 6l5 6-5 6" />
              </svg>
            </span>
            <span class="code-preview-lang mono">{{ block.language || "代码" }}</span>
            <span class="code-preview-eye" aria-hidden="true">
              <svg viewBox="0 0 24 24">
                <path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7S1 12 1 12z" />
                <circle cx="12" cy="12" r="3" />
              </svg>
            </span>
          </button>
        </div>
        <p v-if="replyCodeNotice" class="code-preview-notice" role="status" data-qio-fit>
          {{ replyCodeNotice }}
        </p>
      </div>
      <!--
        系统核对注记 = 后端事实，独立成块（不进正文 Markdown）：
        正文与注记各出现一次，也不参与打字机动画（它是在回答落定之后才到的）。
      -->
      <div
        v-if="systemNote"
        class="system-fact"
        data-qio-fit
        data-test="answer-system-note"
        role="note"
      >
        <span class="sf-kind mono">系统事实</span>
        <p class="sf-body">{{ systemNote }}</p>
      </div>
      <div class="meta mono" data-qio-fit>
        <span class="ts">{{ metaText }}</span>
        <button class="copy-btn" type="button" :class="{ fail: copyState === 'fail' }" @click="copyContent">
          {{ copyLabel }}
        </button>
      </div>
    </template>
  </div>
</template>

<style scoped>
/* ==========================================================================
   宽度链（对话不得横向滚动）
   真正的修复在布局：每一层都 min-width: 0 让 flex 子项可以收缩、长文本换行、
   按钮空间不足时换行重排；data-qio-fit（overflow-x: clip）只是边界保护。
   ========================================================================== */
.message,
.bubble,
.attach-row,
.media-facts,
.system-fact,
.process-line,
.attach-notice,
.attach-note,
.meta {
  min-width: 0;
}
/* 图片 / 视频 / 画布按消息可用宽度等比适配（只改呈现，不改数据、不裁切） */
.message :where(img, video, canvas) {
  max-width: 100%;
  height: auto;
}
/* 附件名 / 超长路径 / 连续字符：视觉换行，不改字符序列 */
.attach-row .attach-item {
  min-width: 0;
}
.system-fact .sf-body,
.process-line,
.attach-notice,
.attach-note,
.sub-result,
.sub-goal,
.tool-error,
.tool-note,
.tool-fail-line,
.sub-fail {
  overflow-wrap: anywhere;
}
.media-facts {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
  margin: var(--sp-1) 0 0;
  padding-left: var(--sp-2);
  border-left: 2px solid var(--border-subtle);
}
.media-fact {
  display: flex;
  flex-direction: column;
  gap: 2px;
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--text-secondary);
}
.media-fact-head {
  display: flex;
  align-items: baseline;
  gap: var(--sp-2);
  flex-wrap: wrap;
}
.media-fact-name {
  color: var(--text-primary);
}
.media-fact-state {
  color: var(--text-muted);
}
.media-fact-at,
.media-fact-transcript {
  font-family: var(--mono);
  font-size: var(--fs-tech);
  color: var(--text-muted);
}
.media-fact-transcript {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  color: var(--text-primary);
}

.message {
  display: flex;
  flex-direction: column;
  /* 左半边完整显示：比原 640px 更宽，宽屏下消息占左侧更充分 */
  max-width: min(760px, 100%);
  /* 收缩到可用宽度：内容再宽也不许把消息列撑出去 */
  min-width: 0;
  margin: 6px 0;
  font-size: 14.5px;
  line-height: 1.75;
}
/* 本次会话新产生的消息最多一次很短的入场；历史消息没有这个类，不会重播 */
.message.fresh {
  animation: msg-in var(--dur-menu) var(--ease-out) both;
}
@keyframes msg-in {
  from { opacity: 0; transform: translateY(3px); }
  to { opacity: 1; transform: none; }
}
.message.user {
  margin-left: auto;
  align-items: flex-end;
  /* 用户消息收窄：靠 alignment + 字色区分角色，不用整块色填充 */
  max-width: min(620px, 88%);
}
.message.assistant {
  margin-right: auto;
  align-items: flex-start;
}
/* 系统核对注记：独立「系统事实」区域（契约 §七 C1），与正文明确分开 */
.system-fact {
  margin: 6px 0 0;
  padding: 6px 10px;
  max-width: min(760px, 100%);
  border-left: 2px solid var(--border-strong);
  background: var(--bg-inset);
  border-radius: var(--qio-ui-radius);
}
.sf-kind {
  display: block;
  font-size: 10.5px;
  letter-spacing: 0.06em;
  color: var(--text-faint);
}
.sf-body {
  margin: 2px 0 0;
  font-size: 12px;
  line-height: 1.7;
  color: var(--text-secondary);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
/* 「后端已核对」：贴在回答内部的一行事实，克制、不抢正文 */
.verified-note {
  margin: 8px 0 0;
  padding-left: 8px;
  border-left: 2px solid var(--border-strong);
  color: var(--text-secondary);
  font-size: 12px;
  line-height: 1.5;
}
.bubble {
  padding: 10px 16px;
}
/* 用户侧：无底色，右侧玫红细规线 + 右对齐位置 —— editorial 对话而非 IM 气泡 */
.user-bubble {
  background: transparent;
  color: var(--text-strong);
  border-right: 2px solid var(--accent);
  border-radius: 0;
  padding: 2px 14px 2px 16px;
  text-align: left;
}
.user-bubble .plain {
  white-space: pre-wrap;
  /* 用户消息是**纯文本**通道（不经过 markdown），长 URL / 路径 / 连续字符必须自己换行：
     气泡带 data-qio-fit（overflow-x: clip），不换行就等于「文字被裁掉且滚不动」——
     复核实测 clientW 588 / scrollW 2251（422 字符 URL 渲染宽度 3152px）。 */
  overflow-wrap: anywhere;
  word-break: break-word;
}
.assist-bubble {
  background: var(--bg-elevated);
  border: 1px solid var(--border-subtle);
  border-radius: 14px 14px 14px 4px;
}
.tname {
  font-size: 16px;
  font-weight: 600;
  color: var(--text-strong);
  margin-bottom: 6px;
}
/* 过程说明行：安静的一行文字，不是气泡（interim 不再单独成气泡） */
.process-line {
  margin: 1px 0 1px 6px;
  padding-left: 10px;
  border-left: 1px solid var(--border-subtle);
  max-width: min(760px, 100%);
  font-family: var(--sans);
  font-size: var(--fs-sm);
  line-height: 1.75;
  color: var(--text-secondary);
  white-space: pre-wrap;
}
/* 附件行：用户消息下方的一排小签，数据用等宽字体 */
.attach-row {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  justify-content: flex-end;
  margin-top: 4px;
  max-width: min(620px, 100%);
}
.attach-item {
  display: inline-flex;
  align-items: center;
  max-width: 100%;
}
/* 附件操作的结果（打开 / 重新定位 / 重试）：留住，不静默 */
.attach-notice {
  margin: 4px 0 0;
  font-size: var(--fs-xs);
  color: var(--text-secondary);
  text-align: right;
  max-width: min(620px, 100%);
  overflow-wrap: anywhere;
}
.attach-note {
  margin: 4px 0 0;
  font-size: 10.5px;
  color: var(--text-muted);
  text-align: right;
}
/* 元数据：低调存在，hover / focus 时才完全显形（第一眼只看内容） */
.meta {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
  margin-top: 6px;
  font-size: 10.5px;
  color: var(--text-muted);
  letter-spacing: 0.05em;
  opacity: 0.62;
  transition: opacity var(--dur-fast) var(--ease);
}
.message:hover .meta,
.message:focus-within .meta {
  opacity: 1;
}
.ts {
  color: var(--text-muted);
}
.queued-tag {
  color: var(--text-secondary);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-pill);
  padding: 0 8px;
  letter-spacing: 0.06em;
}
.copy-btn {
  border: none;
  background: none;
  padding: 0 2px;
  font: inherit;
  color: var(--text-muted);
  cursor: pointer;
  opacity: 0;
  transition: opacity var(--dur-fast) var(--ease), color var(--dur-fast) var(--ease);
}
.message:hover .copy-btn,
.copy-btn:focus-visible {
  opacity: 1;
}
/* 触屏没有 hover：复制入口必须默认可见 */
@media (hover: none) {
  .copy-btn { opacity: 1; }
}
.copy-btn:hover {
  color: var(--accent);
}
.copy-btn.fail {
  color: var(--danger);
  opacity: 1;
}
.copy-btn:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 2px;
  border-radius: var(--qio-ui-radius);
}
/* ---- 工具卡 ---- */
/* 卡片家族共用契约：`.qio-card`（底/边/圆角/层级）+ `data-state`（running/waiting/ready/failed）。
   工具卡是紧凑行式卡片，所以在这里覆盖内边距并把圆角对齐到家族值。 */
.tool-card {
  margin: 4px 0;
  padding: 0;
  border-radius: var(--qio-ui-radius);
  /* 纯裁剪（圆角 + 收起详情）：overflow: clip 不建立滚动容器 ——
     程序性滚动也做不到，不存在「看不见但能左右滚」的路径。 */
  overflow: clip;
  font-size: 12.5px;
}
.tool-card.fail {
  border-color: var(--border-danger);
}
/* 运行中的卡不抢注意力：只用细边框 + 文字表达「还在跑」 */
.tool-card.running {
  border-style: dashed;
}
.tool-head {
  display: flex;
  align-items: center;
  /* 空间不足时（窄窗口 / 长工具名）操作信息换行重排，而不是把卡片撑宽 */
  flex-wrap: wrap;
  row-gap: 4px;
  gap: 10px;
  width: 100%;
  min-width: 0;
  padding: 8px 12px;
  background: transparent;
  border: none;
  cursor: pointer;
  text-align: left;
  font-size: 11px;
  color: var(--text-secondary);
}
.tool-head:hover {
  background: var(--layer-hover);
}
.tool-mark {
  font-size: 12px;
}
.tool-mark.ok {
  color: var(--success);
}
.tool-mark.fail {
  color: var(--danger);
}
.tool-mark.running {
  color: var(--link);
}
.tool-name {
  min-width: 0;
  overflow-wrap: anywhere;
  color: var(--text-strong);
  letter-spacing: 0.02em;
}
.tool-status {
  padding: 1px 8px;
  border-radius: var(--qio-ui-radius);
  font-family: var(--mono);
  font-size: 10px;
  letter-spacing: 0.04em;
  border: 1px solid currentColor;
}
.tool-status.ok {
  color: var(--success);
}
.tool-status.fail {
  color: var(--danger);
}
.tool-status.running {
  color: var(--link);
}
.tool-duration {
  font-size: 10px;
  color: var(--text-muted);
  letter-spacing: 0.04em;
}
.tool-fail-line {
  margin: 0;
  padding: 0 12px 8px;
  color: var(--danger);
  font-size: 12px;
  /* 失败原因可能是一整条路径（没有空格可断行）：必须允许任意位置折行，
    否则窄窗口下会把卡片撑出横向溢出。 */
  overflow-wrap: anywhere;
}
.tool-detail-sec + .tool-detail-sec {
  margin-top: 8px;
}
.sec-label {
  font-size: 11px;
  color: var(--text-muted);
  letter-spacing: 0.08em;
}
.tool-note {
  margin: 6px 0 0;
  font-size: 12px;
  color: var(--text-muted);
}
.tool-note.err {
  color: var(--danger);
}
.tool-time {
  margin-left: auto;
  color: var(--text-muted);
  letter-spacing: 0.05em;
}
.tool-chev {
  color: var(--text-muted);
  flex-shrink: 0;
  transition: transform var(--mo-1-state) var(--ease-1);
}
.tool-card.open .tool-chev { transform: rotate(90deg); }
/* 展开/收起：真实高度过渡（0fr → 1fr），收起后不占位、不拦截点击 */
.tool-detail-wrap {
  display: grid;
  grid-template-rows: 0fr;
  transition: grid-template-rows var(--mo-2-in) var(--ease-2);
}
.tool-detail-wrap.open { grid-template-rows: 1fr; }
.tool-detail-clip {
  /* 展开/收起的高度过渡需要裁掉超出部分：纯裁剪 → overflow: clip，
     不建立任何可程序性滚动的容器（0fr→1fr 过渡照旧）。 */
  overflow: clip;
  min-height: 0;
  opacity: 0;
  transition: opacity var(--mo-2-in) var(--ease-2);
}
.tool-detail-wrap.open .tool-detail-clip { opacity: 1; }
.tool-detail {
  border-top: 1px solid var(--border-subtle);
  padding: 10px 12px;
  background: var(--bg-inset);
}
.tool-detail pre {
  font-size: 11px;
  /* 软换行：保留原始换行与缩进，长路径 / 长参数在视觉上折行（不改内容） */
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  word-break: break-word;
  color: var(--text-secondary);
  margin: 0;
  font-family: var(--mono);
}
.tool-error {
  color: var(--danger);
  font-size: 12px;
  margin: 4px 0 0;
  overflow-wrap: anywhere;
}
/* ---- 独立任务卡 ---- */
.subagent-card {
  margin: 4px 0;
  border-left: 2px solid var(--link);
  border-radius: var(--qio-ui-radius);
  padding: 10px 12px;
  font-size: 12.5px;
}
.subagent-card.fail {
  border-color: var(--border-danger);
  border-left-color: var(--danger);
}
.sub-head {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  row-gap: 4px;
  gap: 10px;
  min-width: 0;
}
.sub-kind {
  font-family: var(--mono);
  font-size: 10px;
  letter-spacing: 0.06em;
  color: var(--text-muted);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-pill);
  padding: 0 8px;
}
.sub-name {
  min-width: 0;
  overflow-wrap: anywhere;
  color: var(--text-strong);
  font-size: 14px;
}
.sub-state {
  margin-left: auto;
  font-size: 11px;
  color: var(--text-secondary);
}
.sub-state.running {
  color: var(--link);
}
.sub-line,
.sub-goal,
.sub-result,
.sub-fail {
  margin: 6px 0 0;
  color: var(--text-secondary);
  line-height: 1.6;
}
.sub-result {
  color: var(--text-primary);
  white-space: pre-wrap;
}
.sub-fail {
  color: var(--danger);
}

/* ---- 执行记录定位 / 右侧只读预览：安静的次要动作，不抢卡片主体 ---- */
.task-locate,
.preview-open {
  align-self: flex-start;
  margin-top: 6px;
  padding: 2px 9px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-pill);
  background: transparent;
  color: var(--text-muted);
  font-family: var(--sans);
  font-size: 11px;
  cursor: pointer;
  transition: color var(--dur-fast) var(--ease-1), border-color var(--dur-fast) var(--ease-1);
}
.task-locate:hover,
.preview-open:hover {
  color: var(--link);
  border-color: var(--link);
}
.task-locate:focus-visible,
.preview-open:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 1px;
}
.task-locate-note {
  margin: 4px 0 0;
  color: var(--text-muted);
  font-size: 11px;
  line-height: 1.6;
  overflow-wrap: anywhere;
}
.preview-open {
  flex: none;
}
.code-preview-row {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 8px;
}
/* 代码块预览：单胶囊 + 代码符号 + 语言 + 眼睛；不再用「在右侧查看…」文字药丸 */
.code-preview-btn {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  height: 32px;
  padding: 3px 5px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-pill);
  background: var(--bg-surface);
  color: var(--text-secondary);
  font-family: var(--sans);
  font-size: var(--fs-xs);
  cursor: pointer;
  transition: border-color var(--dur-fast) var(--ease), color var(--dur-fast) var(--ease);
}
.code-preview-btn:hover {
  border-color: var(--accent);
  color: var(--text-strong);
}
.code-preview-btn:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 2px;
}
.code-preview-glyph {
  width: 24px;
  height: 24px;
  border-radius: 50%;
  background: var(--accent-soft);
  color: var(--link);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex: none;
}
.code-preview-glyph svg {
  width: 13px;
  height: 13px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.6;
  stroke-linecap: round;
  stroke-linejoin: round;
}
.code-preview-lang {
  font-size: 10.5px;
  color: var(--text-muted);
  white-space: nowrap;
}
.code-preview-eye {
  width: 26px;
  height: 26px;
  border-radius: 50%;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: var(--text-muted);
  flex: none;
  transition: color var(--dur-fast) var(--ease), background-color var(--dur-fast) var(--ease);
}
.code-preview-btn:hover .code-preview-eye {
  color: var(--accent);
  background: var(--layer-hover);
}
.code-preview-eye svg {
  width: 14px;
  height: 14px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.6;
  stroke-linecap: round;
  stroke-linejoin: round;
}
.code-preview-notice {
  margin: 4px 0 0;
  color: var(--text-muted);
  font-size: 11px;
  overflow-wrap: anywhere;
}
</style>