<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from "vue";
import { useSessionStore } from "../stores/session";
import { MEDIA_CAPABILITY_HINT } from "./mediaFacts";
import AttachmentChip from "./AttachmentChip.vue";
import {
  attachmentRemovalBarrier,
  clearAttachmentTombstones,
  forgetAttachmentRemoved,
  getAttachment,
  isAttachmentDead,
  isDesktopShell,
  isSendable,
  loadPendingAttachments,
  markAttachmentRemoved,
  markAttachmentsSent,
  onPathDrop,
  openAttachment,
  pendingRevision,
  pickLocalPath,
  prepareAttachment,
  relocateAttachment,
  removeAttachment,
  restorePendingAttachments,
  retryAttachment,
  savePendingAttachments,
  stateText,
  subscribePendingAttachment,
  uploadAttachment,
  waitUntilSettled,
  type AttachmentRef,
  type RestorePendingOutcome,
} from "../services/attachments";
import {
  attachmentOpToken,
  beginAttachmentOp,
  bumpTopicEpoch,
  decideAttachmentWrite,
  invalidateAttachmentIdentity,
  mergeRestorePatch,
  registerAttachmentIdentity,
  restoreAttachmentIdentity,
  type AttachmentOpCapture,
  type PendingRestorePatch,
} from "../composables/attachmentOps";
import { vQioScrollbar } from "../composables/useQioScrollbars";

const session = useSessionStore();
/**
 * 草稿属于会话状态，不属于本组件：`text` 读写 store.draft，
 * 因此打开设置/星球再返回时草稿仍在（组件会被卸载重建）。
 */
const text = computed({
  get: () => session.draft,
  set: (v: string) => {
    session.draft = v;
  },
});
const inputRef = ref<HTMLTextAreaElement | null>(null);
/** 输入行（textarea + 按钮）：观察它的宽度变化来重算输入框高度 */
const inputRowRef = ref<HTMLElement | null>(null);
/** 停止请求进行中（取消是可观察动作，不能假装已停） */
const stopping = ref(false);
const cancelError = ref("");

/** 输入框最大高度：不超过 40vh，也不超过 320px（超出后内部滚动） */
const MAX_INPUT_PX = 320;

/**
 * 停止按钮的文案。
 *
 * 提交之后、服务端 TURN_START 到达之前，界面知道「有任务在跑」但没有 turn_id。
 * 这段时间不再是死按钮：停止动作会退化为「取消后端当前的 active turn」，
 * 它只作用于真正在跑的那一轮，不会误伤排队消息。
 */
const stopLabel = computed(() => {
  if (stopping.value || session.cancelling) return "正在停止";
  return "停止";
});
const stopTitle = computed(() => {
  if (stopping.value || session.cancelling) return "正在停止…";
  if (!session.activeTurnId) return "停止当前任务（取消后端正在执行的那一轮）";
  return "停止当前任务";
});

/**
 * 输入区的「内容」与「是否满足发送条件」必须分开判断（契约 §6）：
 * - 有文本或附件 = 存在内容 → 共用按钮处于「发送」态（回复期间提交会进入队列）；
 * - 有内容但附件还在准备 / 只带附件没有文字 → 仍是发送态，但校验会如实说明卡在哪一步，
 *   而不是把按钮变成停止、也不是假装可以发。
 * - 回复中且没有任何内容 → 共用按钮才是「停止」；空闲且无内容 → 禁用发送。
 */
const hasText = computed(() => text.value.trim().length > 0);
const hasContent = computed(() => hasText.value || pending.value.length > 0);
type PrimaryMode = "send" | "stop" | "disabled";
const primaryMode = computed<PrimaryMode>(() => {
  if (hasContent.value) return "send";
  if (session.turnRunning) return "stop";
  return "disabled";
});
/** 发送校验的真实原因（空串 = 可以立即发送）。只用于解释，不替用户做决定。 */
const sendGateReason = computed(() => {
  if (!hasText.value && pending.value.length) {
    return "只带附件还不能发送：请写一句话一起发（附件会随这条消息发出）";
  }
  if (!hasText.value) return "先写点什么或添加附件再发送";
  const blocked = attachmentBlockReason();
  if (blocked) return blocked;
  if (prepareInFlight()) {
    return "上一次带附件的发送还没有被后端受理：等它受理，或先按它的「中止」结束它";
  }
  return "";
});
/** 「已经满足发送条件」：与「存在内容」分开，按钮据此表达真实状态。 */
const sendReady = computed(() => primaryMode.value === "send" && !sendGateReason.value);
const primaryLabel = computed(() => {
  if (primaryMode.value === "stop") return stopLabel.value;
  if (!hasText.value && pending.value.length) return "发送";
  return session.turnRunning ? "排队发送" : "发送";
});
const primaryTitle = computed(() => {
  if (primaryMode.value === "stop") return stopTitle.value;
  if (primaryMode.value === "disabled") return "输入内容或添加附件后可以发送";
  if (sendGateReason.value) return `暂时不能发送：${sendGateReason.value}`;
  return session.turnRunning ? "排队发送（Enter）：QIO 正在回复，这条会进入队列" : "发送（Enter）";
});
const primaryDisabled = computed(() => {
  if (primaryMode.value === "stop") return !session.canStopTurn || stopping.value;
  if (primaryMode.value === "disabled") return true;
  return false;
});
function primaryAction() {
  if (primaryMode.value === "stop") {
    void stopTurn();
    return;
  }
  if (primaryMode.value === "disabled") return;
  void submit();
}

/**
 * 上一次发送被后端拒掉的附件（结构化失败）：逐条原因 + 一个「移除这些附件后发送」出口。
 *
 * 严格语义（契约 §1.2）：rejected 非空时整轮不入队；文本与附件都留在输入区，
 * 用户不必重写，也不会出现「看起来发出去了、其实附件没带上」。
 */
const sendRejection = computed(() => session.lastSendRejection);

/** 某个附件已经不在待发列表里：把拒绝信息里的对应行去掉（空则整块收掉）。 */
function dropFromRejection(id: string) {
  const rejection = session.lastSendRejection;
  if (!rejection) return;
  const left = rejection.rejected.filter((row) => row.id !== id);
  session.lastSendRejection = left.length ? { ...rejection, rejected: left } : null;
}

function nameOfAttachment(id: string): string {
  return pending.value.find((item) => item.id === id)?.name ?? id;
}

/** 移除被拒的附件后立刻重发（草稿还在；不删服务端记录，只把它们从这次发送里去掉）。 */
async function removeRejectedAndSend() {
  const rejected = sendRejection.value?.rejected ?? [];
  const ids = new Set(rejected.map((row) => row.id));
  const topicId = currentTopicId();
  // 用户显式「移除这些附件后发送」：同样写 tombstone，晚到的恢复不得把它们放回来
  for (const id of ids) {
    invalidateAttachmentIdentity(id);
    markAttachmentRemoved(topicId, id);
  }
  commitToTopic(topicId, (list) => list.filter((item) => !ids.has(item.id)));
  session.lastSendRejection = null;
  attachError.value = "";
  await submit();
}

async function submit() {
  const value = text.value.trim();
  // 「存在内容」≠「满足发送条件」：只带附件（没有文字）时后端不会受理，
  // 这里如实说清卡在哪一步，文字与附件都留在输入区（不丢能力、不假装已发出）。
  if (!value) {
    if (pending.value.length) {
      attachError.value = "只带附件还不能发送：请写一句话一起发（附件会随这条消息发出）";
    }
    return;
  }
  // 附件闸门：没准备好就不能当作「发送成功」——文本与附件都留在原地，并说清卡在哪一个
  const blocked = attachmentBlockReason();
  if (blocked) {
    attachError.value = blocked;
    return;
  }
  const draftSnapshot = text.value;
  const sentAttachments = pending.value.slice();
  const hasAttachments = sentAttachments.length > 0;
  /**
   * R7 / I4：上一次带附件发送**还没被后端受理**时，第二次带附件发送在派发前拒绝。
   * 草稿与附件都留在输入区、给出可见原因，第一次的界面与状态一动不动；
   * 第一次受理（准备状态收尾）之后这里不再拦截，既有排队发送语义不变。
   */
  if (hasAttachments && prepareInFlight()) {
    attachError.value =
      "上一次带附件的发送还没有被后端受理：等它受理，或先按它的「中止」结束它，再发送这一条（这条消息的文字与附件都留在输入区）";
    return;
  }
  // 归属：这次发送携带的附件属于**发起时刻**的话题（之后切话题也不改这一事实）
  const sendTopicId = currentTopicId();
  /**
   * 归属在**点击这一刻**定下（契约 §10.1）：清空输入框、等待回执期间切换话题，
   * 都不许改变这条消息的去向，也不许把失败原文放进别的话题的输入框。
   */
  const attribution = session.sendAttribution();
  // 立即反馈：先清空（这一帧就能看到「已经交出去了」），再等请求结果
  text.value = "";
  // v-model 的清空是异步写回 DOM 的：必须等这一帧之后再渲染，
  // 否则量到的还是旧内容的高度，输入框发送后不会收回原尺寸。
  await nextTick();
  autosize();
  // 只有「带了附件 + 后端还没受理」才需要准备状态；不带附件一律没有。
  // 这一次发送拿到的 prep 就是**它自己那一份**（I1）：收尾只清这一份（I2）。
  const prep = hasAttachments ? beginPrepare() : null;
  let ok = false;
  try {
    ok = await sendWithAttachments(
      value,
      sentAttachments.map((item) => item.id),
      sentAttachments,
      prep ? { signal: prep.abort.signal, prepareId: prep.id } : undefined,
    );
  } finally {
    endPrepare(prep);
  }
  if (!ok) {
    // 发送失败：草稿放回去（用户不必重写），附件也留着（失败不是附件的错）。
    // 若期间已输入新内容则不覆盖。
    // 例外（契约 5）：发送停在「正在确认」时**不**恢复草稿 —— 那条消息还留在对话流里，
    // 确认条的【重试 / 放弃】才是接管入口；把原文放回输入框，用户再按 Enter 就会以
    // 另一个请求身份再发一次，恰好绕开幂等保护。
    if (!session.pendingSendConfirm && !text.value.trim()) {
      text.value = draftSnapshot;
      await nextTick();
      autosize();
    }
    // 互动线：显示**本次请求**的真实原因，并按点击那一刻的归属找回失败原文。
    // 会话层只在「还在原话题 + 输入框为空」时才动手 —— 不覆盖新草稿、不污染别的话题。
    /**
     * 未决发送（受理情况未知）**不自动把原文放回输入框**（契约 5 + F03）：
     * 放回去以后用户顺手按 Enter 就会用**新的请求身份**再发一次，恰好绕开幂等保护 ——
     * 那正是「同一件事被后端执行两次」的来源。原文保存在独立的未决记录里，
     * 由「查看原文 / 同身份重试 / 明确放弃」提供明确入口；用户真要取回，点「找回原文」即可。
     *
     * 文案也随之分开：未决时说「发送未确认 + 原文保存在哪里」（输入框为空时
     * 绝不声称「输入已保留」）；明确失败时才说「输入已保留，可以重试」。
     */
    /**
     * 未决发送（受理情况未知）不自动把原文放回输入框：那会让用户顺手再发一次、
     * 用新身份绕开幂等保护。原文保存在未决记录里，处理入口在输入区上方的「发送异常」。
     * 明确失败时才按归属找回原文（store 只在「还在原话题 + 输入框为空」时动手）。
     */
    if (!session.pendingSendConfirm) {
      session.retryFailedSend(attribution.draftId);
    }
    // 用户自己中止的：如实说「这一轮没有发送」，不要伪装成失败原因
    if (prep?.cancelled) {
      preparingNotice.value = "已中止：这一轮没有发送（文字与附件都留在输入区）";
    }
    return;
  }
  // 已被受理：后端在受理时就把这批附件绑到了这一轮，chip 可以清掉（只删自己那一份）。
  // K1.5：这批 id 进入 sent 失效集、话题世代前进、tombstone 清理 —— 晚到的旧结果不得再入待发列表。
  const sentIds = new Set(sentAttachments.map((item) => item.id));
  for (const id of sentIds) invalidateAttachmentIdentity(id);
  markAttachmentsSent(sendTopicId, [...sentIds]);
  clearAttachmentTombstones(sendTopicId, [...sentIds]);
  bumpTopicEpoch(sendTopicId);
  commitToTopic(sendTopicId, (list) => list.filter((item) => !sentIds.has(item.id)));
  attachError.value = "";
  if (prep?.cancelled) {
    /**
     * 竞态：用户按了中止，但请求在那一刻**已经受理**（轮次已入队）。
     * 这时不能假装「从未发送」—— 如实说「已取消」，并用既有停止入口真的取消它。
     */
    preparingNotice.value = "已取消：这一轮已经受理，已按「停止」取消";
    void stopTurn();
  }
  await nextTick();
  autosize();
}

// ---------------------------------------------------------------------------
// 附件（选择 / 拖放 / 粘贴路径 → 登记 → 准备状态 → 发送前移除 / 失败重试）
// ---------------------------------------------------------------------------

/**
 * 「正在准备附件…」：带附件发送后、后端**受理返回之前**的状态。
 *
 * 为什么需要：准备期间后端什么都没跑（不入队、不发 TURN_START、模型 0 次调用），
 * 所以这里绝不能出现「正在思考 / 正在执行」或阶段历史 —— 只能说「在准备附件」。
 * 中止 = 真的把这次请求 abort 掉（后端据此 abandon 预留，见契约 §1.1），
 * 不是「前端不等了」（那样后端照常受理并执行，用户按了中止却看到它跑起来）。
 */
const PREPARING_VISIBLE_AFTER_MS = 200;

/**
 * 一次「带附件发送、后端尚未受理」的准备状态（R7 / I1）。
 *
 * **每次发送各持一份**：准备标识、取消目标（AbortController）、防抖定时器、
 * 中止状态都属于产生它的那次发送 —— 没有跨发送共享的可变标量；
 * 某次发送的收尾只清掉自己那一份（I2），别的发送的状态一律不动。
 */
interface PrepareState {
  /** 准备标识（契约 §1.1）：中止时用它调取消端点，**以后端确认为准** */
  readonly id: string;
  /** 这次发送自己的取消目标：abort 只作用于这一次请求 */
  readonly abort: AbortController;
  /** 用户已经中止（后端确认 cancelled） */
  cancelled: boolean;
  /**
   * 这次中止已经**有了后端确认的结论**（已中止 / 已放行 / 不认识）。
   * 结论定了就不再重复问后端：重复点击不该重复询问、更不该重复取消别的轮次。
   * 拿不到确认（请求失败）时保持 false —— 用户还能再点一次。
   */
  handled: boolean;
  /** 正在向后端确认中止（界面显示「正在中止…」；拿不到确认不宣称成功） */
  cancelBusy: boolean;
  /** 真的显示出来了（过了防抖阈值才置位，避免闪一下） */
  visible: boolean;
}

/** 在途的准备状态：每次发送一份；这次发送收尾时移除**自己那一份**。 */
const prepares = ref<PrepareState[]>([]);
/** 准备定时器：按 prepareId 存放（定时器句柄不进响应式对象） */
const prepareTimers = new Map<string, ReturnType<typeof setTimeout>>();
/** 最近一次准备写下的用户可见结论/提示（由产生它的那次发送写） */
const preparingNotice = ref("");

/** 界面上的「正在准备附件…」：有任一份准备已过防抖阈值就显示。 */
const preparingVisible = computed(() => prepares.value.some((p) => p.visible));
/**
 * 「中止」入口绑定的那一份（I3）：永远取**最新仍未受理**的那一份。
 * 它不会被别的发送的收尾清掉 —— 所以始终可达，且永远绑定自己的 prepareId。
 */
const preparingActive = computed<PrepareState | null>(
  () => prepares.value[prepares.value.length - 1] ?? null,
);
/** 正在向后端确认中止（界面显示「正在中止…」） */
const preparingCancelBusy = computed(() => preparingActive.value?.cancelBusy ?? false);

/** 准备标识：优先用平台 UUID，缺失时退化成随机串（两者都不含用户数据） */
function newPrepareId(): string {
  const c = globalThis.crypto;
  if (c && typeof c.randomUUID === "function") return c.randomUUID();
  return `prep_${Date.now().toString(36)}${Math.random().toString(36).slice(2, 10)}`;
}
/** 开始一次准备：新的一份状态 + 它自己的定时器（绝不复用别人的）。 */
function beginPrepare(): PrepareState {
  const prep = reactive<PrepareState>({
    id: newPrepareId(),
    abort: new AbortController(),
    cancelled: false,
    handled: false,
    cancelBusy: false,
    visible: false,
  });
  preparingNotice.value = "";
  prepares.value = [...prepares.value, prep];
  prepareTimers.set(
    prep.id,
    setTimeout(() => {
      // 只在自己那一份仍在途时置位：迟到的回调不会点亮别的发送的准备状态
      if (prepares.value.some((p) => p.id === prep.id)) prep.visible = true;
    }, PREPARING_VISIBLE_AFTER_MS),
  );
  return prep;
}

/** 收尾一次准备：只清**自己那一份**的定时器与状态（I2）。 */
function endPrepare(prep: PrepareState | null): void {
  if (!prep) return;
  const timer = prepareTimers.get(prep.id);
  if (timer !== undefined) {
    clearTimeout(timer);
    prepareTimers.delete(prep.id);
  }
  prep.visible = false;
  prep.cancelBusy = false;
  prepares.value = prepares.value.filter((p) => p.id !== prep.id);
}

/** 是否还有带附件的发送停在「后端尚未受理」（I4 的闸门）。 */
function prepareInFlight(): boolean {
  return prepares.value.length > 0;
}

/** 待发送附件（chip 列表）：发送成功后才清掉，发送失败连文本一起留着。 */
const pending = ref<AttachmentRef[]>([]);
/** 正在登记（POST 在途）：只是提示，不代表准备好 */
const attaching = ref(false);
const attachError = ref("");
const dragging = ref(false);
const pathOpen = ref(false);
const pathDraft = ref("");
const pathInputRef = ref<HTMLInputElement | null>(null);
const fileInputRef = ref<HTMLInputElement | null>(null);
/** 回形针菜单：把「选择文件」与「从路径添加」收进同一个圆形图标按钮，减少文字按钮。 */
const attachMenuOpen = ref(false);
const attachMenuRef = ref<HTMLElement | null>(null);

function toggleAttachMenu() {
  attachMenuOpen.value = !attachMenuOpen.value;
}
function chooseLocalFile() {
  attachMenuOpen.value = false;
  void pickFile();
}
async function choosePathEntry() {
  attachMenuOpen.value = false;
  pathOpen.value = true;
  await nextTick();
  pathInputRef.value?.focus();
}
/** 点空白处收起回形针菜单（不改变任何附件/路径状态）。 */
function onDocumentPointerDown(event: PointerEvent) {
  if (!attachMenuOpen.value) return;
  const root = attachMenuRef.value;
  const target = event.target as Node | null;
  if (root && target && root.contains(target)) return;
  attachMenuOpen.value = false;
}
/** 打开/重新定位之后的事实说明（例如「已在文件夹中显示：可执行文件不自动运行」） */
const attachNote = ref("");
/** 浏览器里没有原生选择器时，「粘贴路径 → 重新定位」的目标附件 id */
const relocateTargetId = ref("");
/**
 * 「粘贴路径 → 重新定位」的**发起身份**（R5）：在入口（点「重新定位」）冻结，
 * 原生选择器失败回退到粘贴路径时一起带上，直到提交那一刻 ——
 * 这样即使选择器挂住期间用户切了话题，重新定位的结果也只属于发起话题。
 */
let relocateCapture: AttachmentOpCapture | null = null;
/** 「重新上传」的目标（§1.4）：绑定**具体旧 ID + 发起话题**，只有新附件 ready 且入列才替换。 */
const reuploadTarget = ref<{ id: string; name: string; topicId: string | null } | null>(null);

/**
 * 重新上传这一条：走**既有**的选择流程（桌面原生选择器 → 浏览器文件选择）。
 * 结果归属（F04/F09）：新附件进入**发起话题**的待发送列表；只有它**就绪并成功入列**才删除旧条目。
 */
function reuploadOne(id: string) {
  const item = pending.value.find((a) => a.id === id);
  if (!item) return;
  reuploadTarget.value = { id, name: item.name, topicId: currentTopicId() };
  attachNote.value = `请重新选择「${item.name}」：QIO 无法从原地址恢复（新的附件就绪并进入待发送列表后才替换这一条）`;
  void pickFile();
}

/** 当前话题（待发附件与异步操作都按它归属）。 */
function currentTopicId(): string | null {
  return session.currentTopicId ?? null;
}

/** 某个操作捕获的话题是否仍是当前话题（切走后不更新当前 UI）。 */
function isCurrentTopic(topicId: string | null): boolean {
  return String(topicId ?? "") === String(currentTopicId() ?? "");
}

/** 组件是否还活着（卸载后异步结果只落持久化，不写 UI）。 */
let alive = true;
/** 在途「登记/上传」操作计数：不用共享 loading 布尔值（旧操作结束不会清掉新操作的 loading） */
let attachOpCount = 0;
/**
 * 浏览器文件对话框：**点击那一刻**捕获的发起身份（R4）。
 * 对话框返回后不得再读 currentTopicId —— 用户可能已经切到别的话题，
 * 结果必须落到发起话题 A，而不是污染当前话题 B。
 */
let pickCapture: AttachmentOpCapture | null = null;

function beginAttach(): void {
  attachOpCount += 1;
  attaching.value = true;
}
function endAttach(): void {
  attachOpCount = Math.max(0, attachOpCount - 1);
  attaching.value = attachOpCount > 0;
}

/**
 * 更新一个话题的待发列表：只有「组件存活 + 发起话题仍是当前话题 + 调用方允许写 UI」才更新 UI；
 * 任何情况都持久化到**发起话题**（结果不属于当前话题时也不丢 —— 落到它自己的话题，不制造孤儿）。
 * 已移除（tombstone）/已发送（sent）的条目一律不写回去（K1.3）。
 */
function commitToTopic(
  topicId: string | null,
  mutate: (list: AttachmentRef[]) => AttachmentRef[],
  allowUi = true,
): void {
  const ui = allowUi && alive && isCurrentTopic(topicId);
  const next = mutate(ui ? pending.value : loadPendingAttachments(topicId)).filter(
    (item) => !isAttachmentDead(topicId, item.id),
  );
  if (ui) pending.value = next;
  savePendingAttachments(topicId, next);
}

/**
 * 把一个附件结果写到它该去的地方（K1.3）：
 *   * ui          → 当前 UI + 持久化；
 *   * persistence → 只落**发起话题**的持久化（不写当前 UI，也不丢弃）；
 *   * drop        → 已移除 / 已发送：静默丢弃，绝不 upsert 回来。
 */
function commitAttachment(capture: AttachmentOpCapture, item: AttachmentRef): void {
  const verdict = decideAttachmentWrite(capture, {
    id: item.id,
    isCurrentTopic: alive && isCurrentTopic(capture.topicId),
  });
  if (verdict.target === "drop") return;
  commitToTopic(capture.topicId, (list) => upsertIn(list, item), verdict.target === "ui");
}

/** 按 id 就地更新/追加一条（返回新数组，避免共享引用被误改）。 */
function upsertIn(list: AttachmentRef[], item: AttachmentRef): AttachmentRef[] {
  const index = list.findIndex((a) => a.id === item.id);
  const next = list.slice();
  if (index >= 0) next[index] = item;
  else next.push(item);
  return next;
}

/**
 * 跟进一个附件的准备状态（prepared → ready / failed / changed / missing），按**发起话题**落地。
 * F24：由 waitUntilSettled 保证「已受理、正在准备」会继续等待；这里不自行判断最终态。
 * 返回最终引用（状态没跟到时返回 null，旧条目保留）。
 */
async function track(item: AttachmentRef, capture: AttachmentOpCapture): Promise<AttachmentRef | null> {
  commitAttachment(capture, item);
  try {
    const settled = await waitUntilSettled(item, {
      onUpdate: (next) => commitAttachment(capture, next),
    });
    commitAttachment(capture, settled);
    return settled;
  } catch (err) {
    // 跟进失败 ≠ 附件失败：只在它所属话题仍是当前话题时提示，不干扰别的话题
    if (alive && isCurrentTopic(capture.topicId)) {
      attachError.value = `附件「${item.name}」的准备状态没有跟到：${(err as Error).message}`;
    }
    return null;
  }
}

let stopDropWatch: (() => void) | null = null;

/** 真实路径（拖放 / 原生选择器 / 粘贴）：登记后由后台复制或记引用。 */
async function addPaths(paths: string[], entryCapture?: AttachmentOpCapture) {
  if (!paths.length) return;
  // 归属：发起时刻冻结（调用方在点击/拖放入口捕获；缺省才用当前话题兜底）
  const capture = entryCapture ?? beginAttachmentOp({ kind: "prepare", topicId: currentTopicId() });
  const topicId = capture.topicId;
  const target = reuploadTargetFor(topicId);
  attachError.value = "";
  beginAttach();
  try {
    for (let i = 0; i < paths.length; i += 1) {
      const path = paths[i];
      const replace = target && i === 0 ? target : null;
      try {
        const created = await prepareAttachment(path, { topicId });
        // 新附件的身份 = 本次操作（显式重新添加：同时清理移除痕迹与 sent 标记）
        registerAttachmentIdentity(topicId, created.id, capture.opToken);
        if (replace) await trackAndReplace(created, replace, capture);
        else void track(created, capture);
      } catch (err) {
        if (alive && isCurrentTopic(topicId)) {
          attachError.value = `「${path}」没有登记成功：${(err as Error).message}`;
          if (replace) {
            attachNote.value = `「${replace.name}」没有被替换：新的附件没有登记成功（旧附件保留，可以再试）`;
          }
        }
      }
    }
  } finally {
    endAttach();
  }
}

/** 浏览器回退：只有字节（没有真实路径）时走上传；绝不用 input[type=file] 的 fakepath。 */
async function addFiles(files: FileList | File[], entryCapture?: AttachmentOpCapture) {
  const list = Array.from(files);
  if (!list.length) return;
  // 归属：优先用「打开文件对话框那一刻」捕获的发起身份（R4），否则用当前话题兜底
  const capture = entryCapture ?? beginAttachmentOp({ kind: "upload", topicId: currentTopicId() });
  const topicId = capture.topicId;
  const target = reuploadTargetFor(topicId);
  attachError.value = "";
  beginAttach();
  try {
    for (let i = 0; i < list.length; i += 1) {
      const file = list[i];
      const replace = target && i === 0 ? target : null;
      try {
        const created = await uploadAttachment(file, { topicId });
        registerAttachmentIdentity(topicId, created.id, capture.opToken);
        if (replace) await trackAndReplace(created, replace, capture);
        else void track(created, capture);
      } catch (err) {
        if (alive && isCurrentTopic(topicId)) {
          attachError.value = `「${file.name}」没有上传成功：${(err as Error).message}`;
          if (replace) {
            attachNote.value = `「${replace.name}」没有被替换：新的附件没有上传成功（旧附件保留，可以再试）`;
          }
        }
      }
    }
  } finally {
    endAttach();
  }
}

/** 取当前操作对应的替换目标：只认**同一发起话题**的目标（切话题后不再替换）。 */
function reuploadTargetFor(topicId: string | null): { id: string; name: string; topicId: string | null } | null {
  const target = reuploadTarget.value;
  if (!target) return null;
  return String(target.topicId ?? "") === String(topicId ?? "") ? target : null;
}

/**
 * F09：重传替换的提交条件（契约 C5）。
 * 只有**指定的新附件**达到 ready 且成功进入目标话题的待发列表，才删除旧条目；
 * 准备失败 / 未就绪 / 未入列 / 删除失败都保留旧条目并如实说明。
 */
async function trackAndReplace(
  created: AttachmentRef,
  target: { id: string; name: string; topicId: string | null },
  capture: AttachmentOpCapture,
): Promise<void> {
  const topicId = capture.topicId;
  const settled = await track(created, capture);
  if (!settled) return; // 状态没跟到：旧附件保留
  if (settled.state !== "ready") {
    if (alive && isCurrentTopic(topicId)) {
      attachNote.value = `新的附件还没就绪（${stateText(settled)}）：「${target.name}」保留，等它就绪后再替换；也可以手动移除`;
    }
    return;
  }
  const listed =
    loadPendingAttachments(topicId).some((a) => a.id === settled.id) ||
    (alive && isCurrentTopic(topicId) && pending.value.some((a) => a.id === settled.id));
  if (!listed) {
    if (alive && isCurrentTopic(topicId)) {
      attachNote.value = `新的附件已就绪（${settled.name}），但没有进入待发送列表：「${target.name}」保留`;
    }
    return;
  }
  await commitReplacement(target, settled, capture);
}

/** 提交替换：删除**指定旧 ID**；删除失败不报告无条件成功，保留旧条目可恢复。 */
async function commitReplacement(
  target: { id: string; name: string; topicId: string | null },
  replacement: AttachmentRef,
  capture: AttachmentOpCapture,
): Promise<void> {
  const topicId = capture.topicId;
  // 双次重传竞态：旧条目已经被上一轮替换动作删掉时，不重复 DELETE。
  const stillThere =
    loadPendingAttachments(topicId).some((a) => a.id === target.id) ||
    (alive && isCurrentTopic(topicId) && pending.value.some((a) => a.id === target.id));
  if (!stillThere) return;
  try {
    await removeAttachment(target.id);
  } catch (err) {
    if (alive && isCurrentTopic(topicId)) {
      attachNote.value = `新的附件已就绪（${replacement.name}），但旧的「${target.name}」没有删掉（${(err as Error).message}）：旧条目保留，可以手动移除`;
    }
    return;
  }
  invalidateAttachmentIdentity(target.id);
  markAttachmentRemoved(topicId, target.id);
  commitToTopic(topicId, (list) => list.filter((a) => a.id !== target.id));
  if (reuploadTarget.value && reuploadTarget.value.id === target.id) reuploadTarget.value = null;
  if (alive && isCurrentTopic(topicId)) {
    attachError.value = "";
    attachNote.value = `已重新上传：原来那条「${target.name}」已替换（QIO 无法从原地址恢复）`;
  }
}

/**
 * 点击「附件」：**在点击入口**捕获发起身份（R4）。
 * 桌面端用原生选择器拿真实路径、浏览器/失败时退回文件选择 —— 两条路共用同一个捕获，
 * 所以对话框返回后即使用户已经切了话题，结果仍落到发起话题（不读 currentTopicId）。
 */
async function pickFile() {
  attachError.value = "";
  pickCapture = null;
  const capture = beginAttachmentOp({ kind: "prepare", topicId: currentTopicId() });
  if (isDesktopShell()) {
    try {
      const path = await pickLocalPath();
      if (path) {
        await addPaths([path], capture);
        return;
      }
      // 用户取消：只清理自己的选择意图（不留重传/替换目标），不算失败
      if (await desktopPickerAvailable()) {
        cancelPickIntent();
        return;
      }
    } catch (err) {
      attachError.value = `原生文件选择器不可用（${(err as Error).message}）：已退回文件选择`;
    }
  }
  pickCapture = capture;
  fileInputRef.value?.click();
}

/** 取消选择：只清掉本次选择意图本身（含重传/替换目标），不假装发生过什么。 */
function cancelPickIntent() {
  pickCapture = null;
  reuploadTarget.value = null;
}

/** 桌面壳是否真的有原生选择命令（没有就退回字节上传，不假装能拿路径）。 */
let pickerProbe: Promise<boolean> | null = null;
async function desktopPickerAvailable(): Promise<boolean> {
  if (!isDesktopShell()) return false;
  if (!pickerProbe) {
    pickerProbe = import("@tauri-apps/api/core")
      .then(async ({ invoke }) => {
        const available = await invoke<boolean>("pick_attachment_file_available");
        return Boolean(available);
      })
      .catch(() => false);
  }
  return pickerProbe;
}

function onFileInput(e: Event) {
  const input = e.target as HTMLInputElement;
  // R4：用「打开对话框那一刻」捕获的身份，而不是这里重新读 currentTopicId
  const capture = pickCapture;
  pickCapture = null;
  if (input.files?.length) void addFiles(input.files, capture ?? undefined);
  input.value = ""; // 同一个文件可以再次选择
}

/** 原生文件对话框被取消（现代浏览器发 cancel 事件）：清掉这次的选择意图。 */
function onFileInputCancel() {
  cancelPickIntent();
}

async function submitPath() {
  const value = pathDraft.value.trim().replace(/^"|"$/g, "");
  if (!value) return;
  pathDraft.value = "";
  pathOpen.value = false;
  // 「重新定位」模式：这条路径是某个待发附件的新位置，不是新附件
  if (relocateTargetId.value) {
    const target = relocateTargetId.value;
    // R5：身份用**发起时冻结**的那一份（选择器失败 → 粘贴路径这条回退路径），
    // 绝不用提交时刻的 currentTopicId 重新冻结 —— 否则切话题后结果会写进新话题。
    const capture = relocateCapture;
    relocateTargetId.value = "";
    relocateCapture = null;
    await applyRelocate(target, value, capture ?? undefined);
    return;
  }
  // 入口捕获：路径提交这一刻的话题就是发起话题
  await addPaths([value], beginAttachmentOp({ kind: "prepare", topicId: currentTopicId() }));
}

/** 收起路径行：这次粘贴路径的意图（含重新定位的发起身份）一并清掉，不留悬挂捕获。 */
function cancelPathRow() {
  pathOpen.value = false;
  pathDraft.value = "";
  relocateTargetId.value = "";
  relocateCapture = null;
}

async function removeOne(id: string) {
  const topicId = currentTopicId();
  const item =
    pending.value.find((a) => a.id === id) ??
    loadPendingAttachments(topicId).find((a) => a.id === id) ??
    null;
  if (relocateTargetId.value === id) {
    relocateTargetId.value = "";
    relocateCapture = null;
    pathOpen.value = false;
  }
  if (reuploadTarget.value?.id === id) reuploadTarget.value = null;
  // K1.4：移除 = 使旧操作失效（token 前进）+ 写 tombstone（持久化，跨重挂载有效）
  const previousToken = attachmentOpToken(id);
  invalidateAttachmentIdentity(id);
  markAttachmentRemoved(topicId, id);
  commitToTopic(topicId, (list) => list.filter((a) => a.id !== id));
  // 用户自己把这个附件移掉了：拒绝信息里对应的那条也一起收掉（不留一条点不动的待办）
  dropFromRejection(id);
  try {
    await removeAttachment(id);
  } catch (err) {
    // K1.7：删除失败只恢复**这一条**，并保留它的操作身份（暂时失败不改身份）
    forgetAttachmentRemoved(topicId, id);
    restoreAttachmentIdentity(id, previousToken);
    if (item) commitToTopic(topicId, (list) => upsertIn(list, item));
    if (alive && isCurrentTopic(topicId)) attachError.value = `移除附件失败：${(err as Error).message}`;
  }
}

async function retryOne(id: string) {
  const item = pending.value.find((a) => a.id === id);
  const capture = beginAttachmentOp({ kind: "poll", topicId: currentTopicId(), attachmentIds: [id] });
  attachError.value = "";
  attachNote.value = "";
  if (!item) return;
  if (item.unconfirmed) {
    // 暂时无法确认（F10）的重试 = 重新向后端核对事实，不是重新复制内容。
    await verifyOne(item, capture);
    return;
  }
  try {
    const accepted = await retryAttachment(id);
    void track(accepted, capture);
  } catch (err) {
    if (alive && isCurrentTopic(capture.topicId)) attachError.value = `重试失败：${(err as Error).message}`;
  }
}

/** 重新向后端核对一条「暂时无法确认」的附件：确认后清掉标记，仍失败就继续保留。 */
async function verifyOne(item: AttachmentRef, capture: AttachmentOpCapture): Promise<void> {
  const topicId = capture.topicId;
  try {
    const fresh = await getAttachment(item.id);
    const next: AttachmentRef = {
      ...fresh,
      name: fresh.name || item.name,
      error: fresh.error ?? item.error ?? null,
    };
    delete next.unconfirmed;
    // 核对结果同样受身份约束：已经移除/已发送/换了话题都不得写当前 UI
    commitAttachment(capture, next);
    if (alive && isCurrentTopic(topicId) && !isAttachmentDead(topicId, item.id)) {
      attachError.value = "";
      attachNote.value = `已重新核对「${next.name}」：${stateText(next)}`;
    }
  } catch (err) {
    if (alive && isCurrentTopic(topicId)) {
      attachError.value = `「${item.name}」还是无法确认（${(err as Error).message}）：已保留，可以稍后再试`;
    }
  }
}

/**
 * 打开一个待发附件（问题 5）：桌面走原生（可执行/脚本类只「在文件夹中显示」），
 * 浏览器走认证 fetch 出来的 Blob（查看或下载）。失败如实说原因，不假装已打开。
 */
async function openOne(id: string) {
  const item = pending.value.find((a) => a.id === id);
  if (!item) return;
  const topicId = currentTopicId();
  attachError.value = "";
  attachNote.value = "";
  try {
    const result = await openAttachment(item);
    if (result.note && alive && isCurrentTopic(topicId)) attachNote.value = result.note;
  } catch (err) {
    if (alive && isCurrentTopic(topicId)) attachError.value = `打开「${item.name}」失败：${(err as Error).message}`;
  }
}

async function applyRelocate(id: string, path: string, entryCapture?: AttachmentOpCapture) {
  // 归属：重新定位的结果属于**发起话题**（入口捕获；没有就现在冻结）
  const capture =
    entryCapture ?? beginAttachmentOp({ kind: "relocate", topicId: currentTopicId(), attachmentIds: [id] });
  const topicId = capture.topicId;
  try {
    const accepted = await relocateAttachment(id, path);
    if (alive && isCurrentTopic(topicId)) {
      attachError.value = "";
      attachNote.value = "已受理重新定位：状态跟到 ready 才算成功（准备中不是成功）";
    }
    void track(accepted, capture);
  } catch (err) {
    if (alive && isCurrentTopic(topicId)) attachError.value = `重新定位失败：${(err as Error).message}`;
  }
}

/**
 * 重新定位（问题 5）：桌面壳用原生选择器拿真实路径；浏览器里退回「粘贴真实路径」。
 * 不校验大小/方式的责任在后端（按真实大小重算 copy/reference）；这里只负责拿到路径。
 */
async function relocateOne(id: string) {
  const item = pending.value.find((a) => a.id === id);
  if (!item) return;
  // 入口捕获：原生选择器返回后不得再读 currentTopicId（R4）
  const capture = beginAttachmentOp({ kind: "relocate", topicId: currentTopicId(), attachmentIds: [id] });
  const topicId = capture.topicId;
  attachError.value = "";
  attachNote.value = "";
  if (isDesktopShell()) {
    try {
      const path = await pickLocalPath();
      if (!path) return; // 用户取消：什么都不做（不算失败），也不留任何替换/重定位目标
      await applyRelocate(id, path, capture);
      return;
    } catch (err) {
      if (alive && isCurrentTopic(topicId)) {
        attachError.value = `原生选择器不可用（${(err as Error).message}）：请用「路径」粘贴真实路径`;
      }
    }
  }
  relocateTargetId.value = id;
  // R5：回退到「粘贴路径」时把**发起身份**一起带上（原生选择器可能挂住很久，
  // 期间用户可能已经切走；这条粘贴路径的归属仍只由发起时刻决定）
  relocateCapture = capture;
  pathOpen.value = true;
  if (alive && isCurrentTopic(topicId)) {
    attachNote.value = `把「${item.name}」的新位置粘到下面，回车即可重新定位`;
  }
}
// ---------------------------------------------------------------------------
// 待发附件恢复（问题 3）：组件重建 / 刷新之后，看到的 == 将发送的
// ---------------------------------------------------------------------------

function pendingTopicKey(): string | null {
  return session.currentTopicId ?? null;
}

/** 恢复序号：只有最新一次恢复能写当前 UI（F08）。 */
let restoreToken = 0;

/** 把持久化基线先摆到界面上：恢复在途时用户就能看到（也才能移除）它。 */
function seedFromPersistence(topicId: string | null): void {
  if (!alive || !isCurrentTopic(topicId)) return;
  pending.value = loadPendingAttachments(topicId).filter((item) => !isAttachmentDead(topicId, item.id));
}

/** 恢复候选 = 持久化里的 + 当前界面上已有的（去重，顺序稳定）。 */
function restoreCandidates(topicId: string | null): string[] {
  const ids: string[] = [];
  const seen = new Set<string>();
  const collect = (items: readonly AttachmentRef[]) => {
    for (const item of items) {
      if (seen.has(item.id)) continue;
      seen.add(item.id);
      ids.push(item.id);
    }
  };
  collect(loadPendingAttachments(topicId));
  if (alive && isCurrentTopic(topicId)) collect(pending.value);
  return ids;
}

/** 把恢复返回值（真实补丁 / 旧三态）归一成补丁，便于统一合并。 */
function toRestorePatch(
  outcome: Awaited<ReturnType<typeof restorePendingAttachments>>,
  topicId: string | null,
  revision: number,
): PendingRestorePatch {
  const value = outcome as Partial<PendingRestorePatch> & Partial<RestorePendingOutcome>;
  const byId = new Map<string, AttachmentRef>();
  for (const item of value.restored ?? value.items ?? []) byId.set(item.id, item);
  for (const item of value.unconfirmed ?? []) byId.set(item.id, { ...item, unconfirmed: true });
  return {
    topicId: value.topicId ?? topicId,
    revision: typeof value.revision === "number" ? value.revision : revision,
    restored: [...byId.values()],
    missing: value.missing ?? value.dropped ?? [],
    missingIds: value.missingIds ?? [],
  };
}

/**
 * 恢复待发列表（K1.6）：服务**按话题返回补丁**（不自行写持久化），由这里在修订号一致时合并。
 *
 * F08/F10 归属与版本：
 *   * 捕获**发起话题**、候选、修订号与恢复序号；被更新的恢复取代、或已切走/卸载后，
 *     只按补丁合并到原话题持久化，绝不写当前 UI；
 *   * 合并一律**只新增**：绝不复活 removed/sent，绝不覆盖更晚的列表状态；
 *   * 恢复在途期间有人写过（修订号变了）时，连「确认永久无效」的剔除都先不做 —— 交给下一次恢复；
 *   * 暂时无法确认的附件保留身份并带 unconfirmed（界面显示可重试状态）。
 */
async function restorePending(): Promise<void> {
  const topicId = currentTopicId();
  const token = ++restoreToken;
  const revision = pendingRevision(topicId);
  // 移除水位：这次核对开始之后才被移除的条目，一律不许被补丁复活
  const removedBarrier = attachmentRemovalBarrier();
  let outcome: Awaited<ReturnType<typeof restorePendingAttachments>>;
  try {
    outcome = await restorePendingAttachments(topicId, {
      candidateIds: restoreCandidates(topicId),
      revision,
    });
  } catch (err) {
    if (alive && isCurrentTopic(topicId) && token === restoreToken) {
      attachError.value = `待发附件恢复失败：${(err as Error).message}（没有清理任何记录，可以刷新重试）`;
    }
    return;
  }
  if (token !== restoreToken) return; // 被更新的恢复取代：旧快照一个字节都不写

  const patch = toRestorePatch(outcome, topicId, revision);
  const editing = alive && isCurrentTopic(topicId);
  const merged = mergeRestorePatch(patch, editing ? pending.value : loadPendingAttachments(topicId), {
    // 修订号变了 = 恢复在途期间有别的写入：只允许新增，不剔除任何条目
    dropMissing: patch.revision === revision,
    removedBarrier,
  });
  if (editing) pending.value = merged.list;
  savePendingAttachments(topicId, merged.list);
  if (!editing) return; // 已切走 / 卸载：只落原话题持久化

  if (patch.missing.length) {
    attachError.value = `这些附件已经不在待发列表里（已删除、已随别的消息发出，或不属于本话题）：${patch.missing.join("、")}`;
  }
  const unconfirmed = merged.list.filter((item) => item.unconfirmed);
  if (unconfirmed.length) {
    attachNote.value = `这些附件暂时无法确认（服务没有响应）：${unconfirmed
      .map((a) => a.name)
      .join("、")}。已保留，点「重试」重新核对后再发送`;
  }
}

// 切话题绝不串：先把旧话题的列表落盘，再恢复新话题自己的那份。
// 旧话题在途的恢复/跟进结果只落它自己的持久化，绝不写当前 UI（F08）。
watch(
  () => session.currentTopicId,
  async (_next, prev) => {
    savePendingAttachments(prev ?? null, pending.value);
    restoreToken += 1; // 立刻让在途旧恢复失效
    pending.value = [];
    attachNote.value = "";
    attachError.value = "";
    relocateTargetId.value = "";
    relocateCapture = null;
    pathOpen.value = false;
    reuploadTarget.value = null;
    // 注意：**不清** pickCapture —— 文件对话框可能还开着，切换话题后返回的结果仍属发起话题（R4）
    seedFromPersistence(currentTopicId());
    await restorePending();
  },
);

function onDragOver(e: DragEvent) {
  e.preventDefault();
  dragging.value = true;
}

function onDragLeave(e: DragEvent) {
  const el = e.currentTarget as HTMLElement | null;
  const next = e.relatedTarget as Node | null;
  if (!el || !next || !el.contains(next)) dragging.value = false;
}

function onDrop(e: DragEvent) {
  e.preventDefault();
  dragging.value = false;
  // 桌面壳里拖放的真实路径由 Tauri 的 onDragDropEvent 给出（DOM 拿不到路径）；
  // 浏览器里 dataTransfer 只有字节，走上传。
  if (!isDesktopShell() && e.dataTransfer?.files?.length) void addFiles(e.dataTransfer.files);
}

onMounted(async () => {
  // 组件（重）挂载 = 话题世代前进（K1.2），并把持久化基线先摆到界面上；
  // 然后逐条向后端核对（看到的 == 将发送的）。
  bumpTopicEpoch(currentTopicId());
  seedFromPersistence(currentTopicId());
  void restorePending();
  // 历史消息里的「重新上传」成功后，把新附件送进**发起话题**的待发列表（F04）。
  // 不属于当前话题时已经持久化，这里不碰 UI（切过去自然会恢复出来）。
  stopPendingInbox = subscribePendingAttachment(({ topicId, attachment }) => {
    if (!alive || !isCurrentTopic(topicId)) return;
    // K1.3：已移除 / 已发送的 id 绝不复活 —— 迟到的历史重传结果也一视同仁
    if (isAttachmentDead(topicId, attachment.id)) return;
    commitToTopic(topicId, (list) => upsertIn(list, attachment));
    attachNote.value = `已把「${attachment.name}」加入待发送附件（来自历史消息的重新上传）`;
  });
  // Tauri 核心拖放事件：给的是真实路径，不需要新 crate
  const stop = await onPathDrop(
    (paths) => {
      dragging.value = false;
      void addPaths(paths);
    },
    (state) => {
      dragging.value = state === "over";
    },
  );
  if (stop) stopDropWatch = stop;
});

/** 待发附件「收件箱」退订（F04）：组件卸载时取消，避免重复订阅。 */
let stopPendingInbox: (() => void) | null = null;

onBeforeUnmount(() => {
  // F08：卸载后晚到的异步结果只落持久化，不再写 UI。
  alive = false;
  /**
   * R7 / I5：卸载只清掉**所有**准备定时器与本地准备状态登记，一个悬挂定时器都不留。
   *
   * 这里**不 abort** 在途请求：abort 是用户的显式动作（「中止」/「停止」），
   * 不该由「离开对话页」这种导航副作用触发 —— 用户已经点过发送的消息必须继续跑完，
   * 「离开页面不得丢失已发送的消息」比「卸载时放掉引用」重要得多。
   * 卸载后的异步结果照既有 alive 语义只落持久化、不写 UI。
   */
  for (const timer of prepareTimers.values()) clearTimeout(timer);
  prepareTimers.clear();
  for (const prep of prepares.value) {
    prep.visible = false;
    prep.cancelBusy = false;
  }
  prepares.value = [];
  // R5：卸载不留悬挂的重新定位发起身份
  relocateCapture = null;
  stopPendingInbox?.();
  stopPendingInbox = null;
  stopDropWatch?.();
  stopDropWatch = null;
});

/** 发送前的闸门：附件没准备好就不能当作发送成功（文本与附件都留着）。 */
const blockedAttachments = computed(() => pending.value.filter((a) => !isSendable(a)));

function attachmentBlockReason(): string {
  // 暂时无法确认（F10）：状态未知，先重新核对再发送（不是「准备中」也不是「失败」）
  const unconfirmed = pending.value.filter((a) => a.unconfirmed);
  if (unconfirmed.length) {
    return `这些附件暂时无法确认（服务没有响应）：${unconfirmed
      .map((a) => `「${a.name}」`)
      .join("、")}。已保留，点「重试」重新核对后再发送`;
  }
  const preparing = pending.value.filter((a) => a.state === "prepared");
  if (preparing.length) {
    return `附件还在准备中（${preparing.map((a) => a.name).join("、")}）：准备好再发送，或先移除`;
  }
  const blocked = blockedAttachments.value;
  if (blocked.length) {
    return `这些附件没有准备好，不能当作发送成功：${blocked
      .map((a) => `「${a.name}」${stateText(a)}`)
      .join("；")}`;
  }
  return "";
}

/**
 * session.send 的第二/第三参数由 B 按 Lead 裁决加（send(text, attachmentIds?, attachments?)）。
 * 本 worktree 里 session.ts 还是单参，这里做一次最小收窄适配：多传的参数在 B 的改动落地前
 * 会被 JS 忽略，而**后端受理时会把本话题下尚未绑定的附件绑到这一轮**，所以附件不会静默丢失。
 * 集成（B 的 session.ts 落地）之后可以直接删掉这个适配器。
 */
type SendWithAttachments = (
  text: string,
  attachmentIds?: string[],
  attachments?: AttachmentRef[],
  /** prepareId：准备标识（契约 §1.1），中止时用它调取消端点，以后端确认为准 */
  options?: { signal?: AbortSignal; prepareId?: string },
) => Promise<boolean>;
const sendWithAttachments = session.send as unknown as SendWithAttachments;

function onKeydown(e: KeyboardEvent) {
  // IME：选词/组字过程中的 Enter 属于输入法，不能当发送
  if (e.isComposing || e.keyCode === 229) return;
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    void submit();
  }
}

/**
 * 输入框高度重算（软换行 + 随内容增高 + 到上限后只纵向滚动）。
 *
 * 三条规则：
 * 1. 空草稿清掉内联高度 → 回到浏览器自然尺寸：空 / 短内容**不产生任何滚动范围**；
 * 2. 量高度前先把 height 归位成 auto，否则量到的是上一次的上限高度（越写越不准）；
 * 3. 上限内按内容自然高度设置，超过上限由 CSS 的 max-height 夹住，只保留纵向滚动
 *    （textarea 是软换行，横向永远没有滚动范围）。
 *
 * 只写 style.height：不碰 value、选区、撤销栈 —— 输入法组合中的文字不受影响。
 * 量高度会把滚动位置夹到 0，所以量完复位，到上限后在底部继续输入时光标不跳。
 */
function autosize() {
  const el = inputRef.value;
  if (!el) return;
  // 空草稿：清掉内联高度，回到浏览器自然尺寸（与刚打开时完全一致）
  if (!el.value.trim()) {
    el.style.height = "";
    return;
  }
  const keepTop = el.scrollTop;
  el.style.height = "auto";
  // box-sizing: border-box 下 scrollHeight 不含边框：补上偏移，
  // 短内容不会因为 1–2px 的取整误差冒出滚动范围。
  // 量不到布局信息的环境（offsetHeight/clientHeight 不是数字）按 0 处理。
  const borderY = el.offsetHeight - el.clientHeight;
  const fix = Number.isFinite(borderY) && borderY > 0 ? borderY : 0;
  el.style.height = Math.min(el.scrollHeight + fix, MAX_INPUT_PX) + "px";
  if (el.scrollTop !== keepTop) el.scrollTop = keepTop;
}

/**
 * 高度重算的触发点（缺一个就会出现「高度与实际内容不符」）：
 * * 草稿本身变化（输入 / 粘贴 / 发送后清空 / 失败后放回 / 恢复被让位的草稿）；
 * * 切换话题 —— 草稿可能恰好是同一个字符串，不会触发上面的 watch，但输入框属于新话题；
 * * 窗口或面板宽度变化（输入框变窄后同一段文字需要更多行）。
 * 观察器只认**宽度变化**：高度是自己写的，认高度会形成回环。
 */
watch(
  () => session.draft,
  () => void nextTick(autosize),
);
watch(
  () => session.currentTopicId,
  () => void nextTick(autosize),
);

/** 输入行的宽度观察句柄（窗口 / 面板 / 停止按钮出现都会改输入框可用宽度） */
let inputWidthObserver: ResizeObserver | null = null;
let lastInputWidth = -1;
let inputResizeRaf = 0;

function onWindowResize() {
  if (inputResizeRaf) return;
  if (typeof requestAnimationFrame !== "function") {
    autosize();
    return;
  }
  inputResizeRaf = requestAnimationFrame(() => {
    inputResizeRaf = 0;
    autosize();
  });
}

onMounted(() => {
  window.addEventListener("resize", onWindowResize);
  document.addEventListener("pointerdown", onDocumentPointerDown, true);
  if (typeof ResizeObserver === "function") {
    inputWidthObserver = new ResizeObserver((entries) => {
      const width = entries[entries.length - 1]?.contentRect.width ?? -1;
      if (width === lastInputWidth) return;
      lastInputWidth = width;
      autosize();
    });
    if (inputRowRef.value) inputWidthObserver.observe(inputRowRef.value);
  }
  // 挂载后先量一次：切换话题 / 恢复草稿再进来时，草稿本来就是多行的
  void nextTick(autosize);
});

onBeforeUnmount(() => {
  window.removeEventListener("resize", onWindowResize);
  document.removeEventListener("pointerdown", onDocumentPointerDown, true);
  if (inputResizeRaf && typeof cancelAnimationFrame === "function") {
    cancelAnimationFrame(inputResizeRaf);
  }
  inputResizeRaf = 0;
  inputWidthObserver?.disconnect();
  inputWidthObserver = null;
});

/**
 * 中止「正在准备附件」的这一轮：**先调后端取消端点，以后端确认为准**（契约 §1.1）。
 *
 * 早先的实现只做 `AbortController.abort()`：abort 是客户端行为，不能当后端证据 ——
 * 用户看到「已中止」，后端却照常受理并执行了这一轮。现在：
 *
 * * 后端确认 `cancelled` → 才算中止，并如实说「这一轮没有发送」；
 * * 后端回 `already_started` → 走**既有停止流程**，如实说「已受理，已按停止取消」；
 * * 拿不到确认（请求失败）→ 保留原因与可用操作，**不提前宣称成功**。
 * * 确认之后才 abort 掉连接（它只是释放连接，不是取消证据）。
 */
async function cancelPreparing() {
  // 中止永远只作用于**那一份准备状态自己**（I3）：它绑定自己的 prepareId 与取消目标
  const prep = preparingActive.value;
  if (!prep || prep.cancelBusy || prep.handled) return;
  prep.cancelBusy = true;
  preparingNotice.value = "正在中止…（等后端确认）";
  try {
    const confirmed = await session.cancelPreparing(prep.id);
    if (!confirmed) {
      // 拿不到确认：不宣称成功，用户还能再点一次（prep.handled 保持 false）
      preparingNotice.value = session.lastError ?? "中止失败：没有拿到后端确认";
      return;
    }
    prep.handled = true;
    if (confirmed.state === "cancelled") {
      prep.cancelled = true;
      preparingNotice.value = "已中止：这一轮没有发送（文字与附件都留在输入区）";
      prep.abort.abort();
    } else if (confirmed.state === "already_started") {
      prep.cancelled = false;
      // 后端说它已经放行：这一轮不再处于「准备中」，准备状态该收起来（如实）
      prep.visible = false;
      if (!confirmed.turnId) {
        /**
         * 身份缺失：**不得**静默退回 stopActiveTurn()（那会停到别的任务上，契约 §1.2）。
         * 如实说明，并给出可用出口。
         */
        preparingNotice.value =
          "后端已放行这一轮，但没有给出可确认的轮次标识：请用「停止」按钮停止当前任务，或刷新后确认这一轮的状态";
        return;
      }
      await stopConfirmedTurn(confirmed.turnId);
    } else {
      preparingNotice.value =
        "后端不认识这次发送（可能已经开始或已经结束）：请看会话里的实际状态，这里不宣称「没有发送」";
    }
  } finally {
    prep.cancelBusy = false;
  }
}

/**
 * 停止**取消确认返回的那一轮**（契约 §1.2）。
 *
 * 绝不拿 session.activeTurnId 顶替：另一轮在跑时那会停错对象。
 * 文案依事实：**发出停止请求 ≠ 已经停止**；失败不宣称成功；
 * 已经结束的轮次如实说明（不说「没有发送」）。
 */
async function stopConfirmedTurn(turnId: string) {
  if (stopping.value) return;
  stopping.value = true;
  cancelError.value = "";
  session.cancelling = turnId;
  try {
    const result = await session.stopTurnById(turnId);
    if (!result) {
      cancelError.value = session.lastError ?? "停止失败";
      session.cancelling = null;
      preparingNotice.value = `已受理，但停止请求失败：${cancelError.value}（这一轮可能仍在运行，可再试）`;
    } else if (!result.cancelled) {
      session.cancelling = null;
      preparingNotice.value = "这一轮已经结束（停止请求没有可取消的目标）——请看会话里的实际结果";
    } else {
      preparingNotice.value = "已受理：已发出停止请求（等这一轮真正结束）";
    }
  } finally {
    stopping.value = false;
  }
}

/** 停止当前 active turn：显示「正在停止」直到后端真正结束（TURN_END） */
async function stopTurn() {
  if (stopping.value) return;
  const target = session.activeTurnId;
  stopping.value = true;
  cancelError.value = "";
  session.cancelling = target ?? "active";
  try {
    const ok = await session.stopActiveTurn();
    if (!ok) {
      cancelError.value = session.lastError ?? "停止失败";
      // 失败才复位：成功时要一直显示「正在停止」，直到后端真的发出 TURN_END
      session.cancelling = null;
    }
  } finally {
    stopping.value = false;
  }
}
</script>

<template>
  <div
    class="composer bubble"
    data-qio-fit
    @dragover.prevent="onDragOver"
    @dragleave="onDragLeave"
    @drop.prevent="onDrop"
    @keydown.esc="attachMenuOpen = false"
  >
    <!--
      输入区：与对话消息区同一套宽度规则。
      * data-qio-fit：气泡 / 输入行不得横向滚动（真正的修复在各层的 min-width:0 与换行）；
      * textarea 用共享滚动引擎的 hidden 策略：保留滚动与定位能力，但**不画**指示条、
        也不预留任何轨道空白 —— 空 / 短内容时连滚动范围都没有（见 autosize）。
    -->
    <div
      v-if="pending.length || attaching || attachError || attachNote || pathOpen || sendRejection || preparingVisible || preparingNotice"
      class="attach-area"
      data-qio-fit
    >
      <div class="attach-row" data-qio-fit>
        <AttachmentChip
          v-for="item in pending"
          :key="item.id"
          :attachment="item"
          :busy="attaching"
          @remove="removeOne"
          @retry="retryOne"
          @open="openOne"
          @relocate="relocateOne"
          @reupload="reuploadOne"
        />
        <span v-if="attaching" class="attach-hint mono">正在登记附件…</span>
      </div>
      <!-- 正在准备附件（后端受理之前）：只说准备，绝不说「已经在跑」；可中止 -->
      <p
        v-if="preparingVisible"
        class="preparing-status mono"
        role="status"
        data-qio-fit
        data-test="preparing-attachments"
      >
        <span aria-hidden="true">◌</span>
        正在准备附件…（这一轮还没有开始）
        <button
          class="act preparing-cancel"
          type="button"
          data-test="preparing-cancel"
          :disabled="preparingCancelBusy"
          :aria-busy="preparingCancelBusy ? 'true' : 'false'"
          :aria-label="
            preparingCancelBusy
              ? '正在中止：等后端确认'
              : '中止这次发送（附件还没有准备好）'
          "
          @click="cancelPreparing"
        >
          {{ preparingCancelBusy ? "正在中止…" : "中止" }}
        </button>
      </p>
      <p
        v-if="preparingNotice"
        class="attach-note"
        role="status"
        data-qio-fit
        data-test="preparing-notice"
      >
        {{ preparingNotice }}
      </p>
      <div v-if="pathOpen" class="path-row" data-qio-fit>
        <input
          ref="pathInputRef"
          v-model="pathDraft"
          class="path-input mono"
          type="text"
          aria-label="本地文件路径"
          :placeholder="
            relocateTargetId
              ? '粘贴这个附件的新位置后回车（重新定位）'
              : '粘贴本地文件路径后回车（桌面端拖入更方便）'
          "
          @keydown.enter.prevent="submitPath"
        />
        <button class="path-btn primary" type="button" @click="submitPath">
          {{ relocateTargetId ? "重新定位" : "添加" }}
        </button>
        <button
          class="path-btn"
          type="button"
          @click="cancelPathRow"
        >
          取消
        </button>
      </div>
      <!--
        附件能力提示（契约 §4.1/§4.2 只加不减）：图片 / 音频 / 视频各自的真实状态，
        不承诺「一定能看见 / 一定听得见」——交付与否由**本次真实链路**的能力决定。
      -->
      <details class="attach-capability" data-qio-fit data-test="attach-capability">
        <summary data-test="attach-capability-summary">QIO 会怎么读图片 / 音频 / 视频</summary>
        <p class="attach-capability-note" data-test="attach-capability-image">{{ MEDIA_CAPABILITY_HINT.image }}</p>
        <p class="attach-capability-note" data-test="attach-capability-audio">{{ MEDIA_CAPABILITY_HINT.audio }}</p>
        <p class="attach-capability-note" data-test="attach-capability-video">{{ MEDIA_CAPABILITY_HINT.video }}</p>
      </details>
      <!-- 附件没附上（结构化失败）：说清是哪几个、为什么，并给一个出口 -->
      <div
        v-if="sendRejection"
        class="attach-reject"
        data-qio-fit
        data-test="attach-reject"
        role="alert"
      >
        <p class="attach-reject-msg">{{ sendRejection.message }}</p>
        <ul class="attach-reject-list">
          <li v-for="row in sendRejection.rejected" :key="row.id">
            <span class="attach-reject-name">{{ nameOfAttachment(row.id) }}</span>
            <span class="attach-reject-why">{{ row.reason }}</span>
          </li>
        </ul>
        <button
          class="qio-btn quiet"
          type="button"
          data-test="attach-reject-remove"
          @click="removeRejectedAndSend"
        >
          移除这些附件后发送
        </button>
      </div>
      <!-- data-test 是**诊断可见性**：装置（D 的实机脚本）按 [data-test="attach-error"]
           读取「为什么没发出去」的原因；没有它就只能读成「什么都没发生」 -->
      <p
        v-if="attachError"
        class="attach-error"
        role="alert"
        data-qio-fit
        data-test="attach-error"
      >
        {{ attachError }}
      </p>
      <p v-if="attachNote" class="attach-note" role="status" data-qio-fit>{{ attachNote }}</p>
    </div>

    <div ref="inputRowRef" class="input-pill" data-qio-fit>
      <slot name="leading" />
      <div ref="attachMenuRef" class="attach-anchor">
        <button
          class="attach-circle"
          type="button"
          :disabled="attaching"
          :aria-expanded="attachMenuOpen ? 'true' : 'false'"
          aria-haspopup="menu"
          title="添加附件：选择本地文件，或粘贴路径"
          aria-label="添加附件"
          @click="toggleAttachMenu"
        >
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="m21 11-8.5 8.5a5 5 0 0 1-7-7L14 4a3.5 3.5 0 0 1 5 5l-8.5 8.5a2 2 0 0 1-3-3L16 7" />
          </svg>
        </button>
        <div v-if="attachMenuOpen" class="attach-menu" role="menu" aria-label="添加附件">
          <button class="attach-menu-item" type="button" role="menuitem" @click="chooseLocalFile">
            <span class="ami-title">选择本地文件</span>
            <span class="ami-note mono">拖入也可以</span>
          </button>
          <button class="attach-menu-item" type="button" role="menuitem" @click="choosePathEntry">
            <span class="ami-title">从路径添加</span>
            <span class="ami-note mono">粘贴路径后回车</span>
          </button>
        </div>
      </div>
      <textarea
        ref="inputRef"
        id="composer-input"
        v-model="text"
        rows="1"
        class="qio-input"
        data-qio-fit
        v-qio-scrollbar="'hidden'"
        wrap="soft"
        aria-label="输入消息（Enter 发送，Shift+Enter 换行）"
        aria-keyshortcuts="Enter Shift+Enter"
        title="Enter 发送 · Shift+Enter 换行"
        placeholder="继续讨论，或添加文件…"
        @keydown="onKeydown"
        @input="autosize"
      ></textarea>
      <!--
        发送 / 停止共用同一个按钮（契约 §6）：有内容 → 发送（回复期间进入队列）；
        回复中且没有内容 → 停止；空闲且没有内容 → 禁用发送。
        「有内容但还没满足发送条件」时保持发送态，点击给出真实校验原因（不假装可发）。
      -->
      <button
        class="send-btn"
        :class="{ 'stop-mode': primaryMode === 'stop', 'stop-btn': primaryMode === 'stop' }"
        type="button"
        :disabled="primaryDisabled"
        :data-mode="primaryMode"
        :data-ready="sendReady ? 'true' : 'false'"
        :aria-label="primaryLabel"
        :title="primaryTitle"
        @click="primaryAction"
      >
        <span v-if="primaryMode === 'stop'" class="sq" aria-hidden="true"></span>
        <svg v-else class="send-arrow" viewBox="0 0 24 24" aria-hidden="true">
          <path d="M12 20V4m-6 6 6-6 6 6" />
        </svg>
      </button>
    </div>
    <p class="composer-caption" data-qio-fit>Enter 发送 · Shift + Enter 换行</p>
    <p v-if="cancelError" class="cancel-error" role="alert" data-qio-fit>{{ cancelError }}</p>
    <!-- 浏览器回退：只拿字节上传；fakepath 绝不当路径 -->
    <input
      ref="fileInputRef"
      class="file-input"
      type="file"
      multiple
      aria-hidden="true"
      tabindex="-1"
      @change="onFileInput"
      @cancel="onFileInputCancel"
    />
    <div v-if="dragging" class="drop-hint">松开即可添加为附件</div>
  </div>
</template>

<style scoped>
/* 附件能力提示：折叠在「怎么读」里，不占常驻空间 */
.attach-capability summary {
  font-size: var(--fs-xs);
  color: var(--link);
  cursor: pointer;
}
.attach-capability-note {
  margin: var(--sp-1) 0 0;
  font-size: var(--fs-xs);
  color: var(--text-muted);
  overflow-wrap: anywhere;
}
/* 右下角大气泡：融入对话页 flex 布局（消息流下方、靠右），
   浅色表面 + 玫红描边 + 右下小圆角，不可拖动；
   增高时自动向上生长，消息流结束于气泡上方，天然不遮挡 */
.composer {
  /* 宽度链：气泡本身不得横向滚动（真正的修复在各层的 min-width:0 与换行重排） */
  min-width: 0;
  /* 独立悬浮于消息流之上，与对话内容同一列（居中 860px），不参与消息流布局；
     消息流底部通过滚动缓冲让出本气泡高度，滚到底时最新消息停在气泡上方。

     左右贴边取「正文列」令牌，不再手算 `calc(50% - Npx)`：手算的偏移与列宽、
     与右侧给入口球留的通道都脱钩 —— 900–1400px 区间里它整体右移了 418px、
     右边缘溢出视口 194px（发送按钮跑到屏幕外），还盖住了停在右下角的入口球。 */
  /* v2：输入区回到对话列的**正常布局**（flew 布局最底部），不再固定悬浮在视口上。
     这样右侧预览打开时输入区只占中央列，不会盖住预览栏；消息流自然也止于它上方。
     （旧实现固定在视口上并贴视口边，右侧栏一开就会压到预览上。） */
  position: relative;
  width: 100%;
  max-width: 860px;
  margin: 0 auto;
  z-index: 12;
  /* 外层只是一个透明列：真正的卡片是内部的胶囊（.input-pill）。
     这样附件行 / 路径行 / 菜单可以浮在胶囊外，而胶囊始终保持胶囊形。 */
  padding: 0;
  border: 0;
  background: transparent;
  box-shadow: none;
}
/* 输入胶囊：附件圆形按钮 + 输入 + 圆形发送，全部在胶囊内部 */
.input-pill {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  padding: 6px 7px 6px 8px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-pill);
  background: var(--bg-surface);
  /* 用户确认不要阴影：只保留 1px 边框；也避免被 .composer 的 overflow-x: clip 截断 */
  box-shadow: none;
  transition: border-color var(--dur-fast) var(--ease-1);
}
.input-pill:focus-within {
  border-color: var(--accent);
}
.composer textarea.qio-input {
  flex: 1;
  min-width: 0;
  height: 26px;
  min-height: 26px;
  max-height: min(40vh, 260px);
  padding: 2px 4px;
  border: 0;
  background: transparent;
  font-size: 14px;
  line-height: 22px;
  resize: none;
}
.composer textarea.qio-input:focus {
  border-color: transparent;
  box-shadow: none;
  background: transparent;
  outline: none;
}
.composer-caption {
  margin: 6px 0 8px;
  text-align: center;
  color: var(--text-faint);
  font-family: var(--mono);
  font-size: 10px;
  letter-spacing: 0.02em;
}
/* 窄窗口：整宽贴底，不缩成小气泡、不遮挡文字 */
@media (max-width: 899px) {
  .composer {
    width: 100%;
    max-width: none;
    /* 宽度由 ComposerDock 的 grid 列决定；这里再加左右 margin 会让胶囊比入口行窄 */
    margin: 0;
  }
}
.topicbar {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
  margin-bottom: 8px;
  min-width: 0;
  font-size: 12px;
}
.tname {
  min-width: 0;
  font-weight: 600;
  color: var(--text-strong);
  font-size: 15px;
  /* 话题名换行显示（不再单行截断 + overflow:hidden ——
     那会留下「看不见但能左右滚」的路径，也会把话题身份藏掉一部分） */
  max-width: min(320px, 100%);
  overflow-wrap: anywhere;
}
.anchor {
  min-width: 0;
  font-size: 10.5px;
  color: var(--text-muted);
  letter-spacing: 0.04em;
  /* 历史位置提示同样换行，不截断、不建立滚动范围 */
  max-width: min(260px, 100%);
  overflow-wrap: anywhere;
}
.anchor.pending { color: var(--accent); }
.anchor-cancel {
  flex-shrink: 0;
  background: none;
  border: 0;
  padding: 0;
  font: inherit;
  font-size: 10.5px;
  color: var(--link);
  cursor: pointer;
  letter-spacing: 0.04em;
}
.anchor-cancel:hover:not(:disabled) { text-decoration: underline; }
.anchor-cancel:disabled { color: var(--text-muted); cursor: default; }
.spacer {
  flex: 1;
}
.kbd-hint {
  font-size: 10.5px;
  color: var(--text-muted);
  letter-spacing: 0.05em;
  white-space: nowrap;
}
.input-row {
  display: flex;
  align-items: flex-end;
  /* 空间不够时按钮换到下一行，而不是把输入区撑宽 / 裁掉按钮 */
  flex-wrap: wrap;
  gap: 10px;
  min-width: 0;
}
.input-row textarea.qio-input {
  flex: 1;
  /* 可收缩到可用宽度：输入区不产生横向滚动范围 */
  min-width: 0;
  min-height: 46px;
  /* 随内容增长，到上限后**只纵向滚动**：长输入不会把聊天窗口挤成一条 */
  max-height: min(40vh, 320px);
  resize: none;
  /* 软换行：连续长串（路径 / URL / 无空格英文）在视觉上折行，值本身一个字符都不改 */
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  word-break: break-word;
  /* 横向是纯裁剪（软换行保证没有横向范围）→ clip 不建立滚动容器；
     纵向是真实滚动（到上限后只纵滚）→ 保留 auto。 */
  overflow-x: clip;
  overflow-y: auto;
}
.stop-btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  flex-shrink: 0;
  height: 38px;
  padding: 0 14px;
  border: 1px solid var(--border-strong);
  border-radius: var(--r-pill);
  background: transparent;
  color: var(--text-secondary);
  font-family: var(--sans);
  font-size: 12.5px;
  cursor: pointer;
  transition: border-color var(--dur-fast) var(--ease-1), color var(--dur-fast) var(--ease-1),
    background var(--dur-fast) var(--ease-1);
}
.stop-btn:hover:not(:disabled) {
  border-color: var(--danger);
  color: var(--danger);
}
.stop-btn:active:not(:disabled) {
  transform: translateY(var(--shift-1));
}
.stop-btn:disabled {
  opacity: 0.55;
  cursor: default;
}
.stop-btn .sq {
  width: 8px;
  height: 8px;
  border-radius: 2px;
  background: currentColor;
}
.stop-label {
  white-space: nowrap;
}
.cancel-error {
  margin-top: 8px;
  font-size: 12px;
  color: var(--danger);
  overflow-wrap: anywhere;
}
.send-confirm {
  margin-top: 8px;
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
  min-width: 0;
  font-size: 12px;
}
.confirm-notice {
  color: var(--text-muted);
  letter-spacing: 0.02em;
  min-width: 0;
  /* 回执状态文案不再单行截断：说清「未确认」比省一行更重要；换行而不是横向滚动 */
  overflow-wrap: anywhere;
}
/* 「发送未确认」是比「正在确认」更重的一态：用强调色把话说重一点 */
.confirm-notice.unknown {
  color: var(--warning, var(--accent));
  font-weight: 600;
}
.confirm-actions {
  flex-shrink: 0;
  display: inline-flex;
  gap: 6px;
}
.confirm-btn {
  background: none;
  border: 1px solid var(--border-strong);
  border-radius: var(--r-pill);
  padding: 3px 10px;
  font: inherit;
  font-size: 11px;
  color: var(--text-secondary);
  cursor: pointer;
  transition: border-color var(--dur-fast) var(--ease-1), color var(--dur-fast) var(--ease-1);
}
.confirm-btn:hover:not(:disabled) {
  border-color: var(--accent);
  color: var(--accent);
}
.confirm-btn:disabled {
  opacity: 0.55;
  cursor: default;
}
.confirm-btn.abandon:hover:not(:disabled) {
  border-color: var(--danger);
  color: var(--danger);
}
/* 聊天草稿保存失败：辅助层里的一条，可读、可点，不抢输入行 */
.draft-error {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-top: 8px;
  font-size: 12.5px;
  color: var(--danger);
}
.draft-retry {
  flex-shrink: 0;
  border: 1px solid var(--border-strong);
  border-radius: var(--r-pill);
  background: transparent;
  color: var(--text-secondary);
  font-family: var(--sans);
  font-size: 12px;
  padding: 2px 10px;
  cursor: pointer;
  transition: border-color var(--dur-fast) var(--ease-1), color var(--dur-fast) var(--ease-1);
}
.draft-retry:hover { border-color: var(--link); color: var(--link); }
.draft-retry.ghost { border-color: var(--border-subtle); color: var(--text-muted); }
.displaced-draft {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-top: 8px;
  font-size: 12.5px;
  color: var(--text-secondary);
}
.displaced-notice {
  margin-top: 6px;
  font-size: 12px;
  color: var(--text-muted);
  overflow-wrap: anywhere;
}
.draft-retry:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
/* 发送 / 停止共用按钮：胶囊内部的圆形图标按钮，两种模式共用同一层动效令牌 */
.send-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 40px;
  height: 40px;
  padding: 0;
  flex-shrink: 0;
  border: 1px solid transparent;
  border-radius: 50%;
  background: var(--accent);
  color: var(--on-accent);
  font-family: var(--sans);
  font-size: 12.5px;
  line-height: 1;
  cursor: pointer;
  /* 状态切换连续：默认 → hover → pressed → disabled 都走同一层令牌 */
  transition: background var(--dur-fast) var(--ease-1), transform var(--dur-press) var(--ease-1-out),
    opacity var(--dur-fast) var(--ease-1), border-color var(--dur-fast) var(--ease-1),
    color var(--dur-fast) var(--ease-1);
}
.send-btn .send-arrow {
  width: 18px;
  height: 18px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.8;
  stroke-linecap: round;
  stroke-linejoin: round;
}
/* 有内容但还没满足发送条件（附件准备中 / 只带附件没有文字）：仍然可点 ——
   点击给出真实校验原因，比一个点不动的灰按钮更有用 */
.send-btn[data-ready="false"] {
  opacity: 0.72;
}
/* 停止模式：安静的描边按钮，不与品牌色发送按钮抢注意力 */
.send-btn.stop-mode {
  background: transparent;
  border-color: var(--border-strong);
  color: var(--text-secondary);
}
.send-btn.stop-mode:hover:not(:disabled) {
  background: transparent;
  border-color: var(--danger);
  color: var(--danger);
  transform: none;
}
.send-btn.stop-mode .sq {
  width: 8px;
  height: 8px;
  border-radius: 2px;
  background: currentColor;
}
.send-btn:hover:not(:disabled) {
  background: var(--accent-hover);
  transform: translateY(calc(var(--shift-1) * -1));
}
.send-btn:active:not(:disabled) {
  transform: translateY(var(--shift-1));
}
.send-btn:disabled {
  opacity: 0.45;
  cursor: default;
}
/* ---- 附件：入口按钮 / chip 行 / 路径输入 / 拖放提示 ---- */
.attach-btn {
  flex-shrink: 0;
  height: 38px;
  padding: 0 12px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-pill);
  background: transparent;
  color: var(--text-secondary);
  font-family: var(--sans);
  font-size: 12.5px;
  cursor: pointer;
  transition: border-color var(--dur-fast) var(--ease-1), color var(--dur-fast) var(--ease-1);
}
.attach-btn:hover:not(:disabled) {
  border-color: var(--accent);
  color: var(--accent);
}
.attach-btn:disabled {
  opacity: 0.55;
  cursor: default;
}
.attach-anchor {
  position: relative;
  flex: none;
}
.attach-circle {
  width: 38px;
  height: 38px;
  flex: none;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 1px solid var(--border-subtle);
  border-radius: 50%;
  background: var(--bg-elevated);
  color: var(--text-secondary);
  cursor: pointer;
  transition: border-color var(--dur-fast) var(--ease-1), color var(--dur-fast) var(--ease-1),
    background var(--dur-fast) var(--ease-1);
}
.attach-circle svg {
  width: 17px;
  height: 17px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.6;
  stroke-linecap: round;
  stroke-linejoin: round;
}
.attach-circle:hover:not(:disabled),
.attach-circle[aria-expanded="true"] {
  border-color: var(--accent);
  color: var(--accent);
  background: var(--layer-hover);
}
.attach-circle:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 2px;
}
.attach-circle:disabled {
  opacity: 0.55;
  cursor: default;
}
.attach-menu {
  position: absolute;
  left: 0;
  bottom: calc(100% + 8px);
  z-index: 30;
  min-width: 188px;
  padding: 4px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  background: var(--bg-elevated);
  box-shadow: var(--shadow-2);
}
.attach-menu-item {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  width: 100%;
  padding: 8px 9px;
  border: 0;
  border-radius: var(--r-sm);
  background: transparent;
  color: var(--text-secondary);
  font-family: var(--sans);
  font-size: var(--fs-sm);
  text-align: left;
  cursor: pointer;
}
.attach-menu-item:hover,
.attach-menu-item:focus-visible {
  background: var(--layer-hover);
  color: var(--text-strong);
  outline: none;
}
.ami-note {
  flex: none;
  color: var(--text-muted);
  font-size: 10px;
}
.attach-area {
  margin-bottom: 8px;
}
/* 「正在准备附件…」：统一的一行状态 + 中止入口（不是阶段历史，也不是执行迹象） */
.preparing-status {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  min-width: 0;
  margin: 6px 0 0;
  font-size: var(--fs-xs);
  color: var(--text-secondary);
}
.preparing-status .act {
  border: 0;
  background: none;
  padding: 0 2px;
  font: inherit;
  color: var(--link);
  cursor: pointer;
}
.preparing-status .act:hover {
  text-decoration: underline;
}
.attach-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
}
.attach-hint {
  font-size: 10.5px;
  color: var(--text-muted);
  letter-spacing: 0.04em;
  overflow-wrap: anywhere;
}
.path-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  min-width: 0;
  margin-top: 6px;
}
.path-input {
  flex: 1;
  min-width: 0;
  height: 28px;
  padding: 0 8px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-xs);
  background: var(--bg-inset);
  color: var(--text-primary);
  font-size: 11.5px;
}
.path-input:focus {
  outline: none;
  border-color: var(--accent);
}
.path-btn {
  flex-shrink: 0;
  height: 28px;
  padding: 0 10px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-xs);
  background: transparent;
  color: var(--text-secondary);
  font-family: var(--sans);
  font-size: 11.5px;
  cursor: pointer;
}
.path-btn:hover {
  border-color: var(--border-strong);
  color: var(--text-primary);
}
/* 附件没附上：原因逐条列出，出口只有一个（移除后重发） */
.attach-reject {
  min-width: 0;
  margin-top: var(--sp-2);
  padding: var(--sp-3);
  border: 1px solid var(--warning);
  border-radius: var(--r-md);
  background: var(--bg-elevated);
}
.attach-reject-msg {
  margin: 0 0 6px;
  font-size: var(--fs-sm);
  color: var(--text-strong);
  line-height: 1.6;
}
.attach-reject-list {
  margin: 0 0 var(--sp-2);
  padding-left: 18px;
  font-size: var(--fs-xs);
  color: var(--text-secondary);
}
.attach-reject-list li {
  margin: 2px 0;
  line-height: 1.6;
}
.attach-reject-name {
  color: var(--text-primary);
  overflow-wrap: anywhere;
}
.attach-reject-why {
  margin-left: 6px;
  overflow-wrap: anywhere;
}
.attach-error {
  margin-top: 6px;
  font-size: 12px;
  color: var(--danger);
  overflow-wrap: anywhere;
}
/* 事实说明（例如「已在文件夹中显示：可执行文件不自动运行」）：不是错误，也不能当成功 */
.attach-note {
  margin-top: 6px;
  font-size: 12px;
  color: var(--text-secondary);
  overflow-wrap: anywhere;
}
.file-input {
  display: none;
}
/* 拖放提示：只在真的把文件拖到输入区上方时出现 */
.drop-hint {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  border: 1px dashed var(--accent);
  border-radius: var(--r-lg);
  background: var(--accent-soft);
  color: var(--text-strong);
  font-family: var(--sans);
  font-size: 12.5px;
  pointer-events: none;
}
</style>
