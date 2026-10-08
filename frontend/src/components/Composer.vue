<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useSessionStore } from "../stores/session";
import { api } from "../services/api";
import AttachmentChip from "./AttachmentChip.vue";
import {
  isDesktopShell,
  isSendable,
  onPathDrop,
  openAttachment,
  pickLocalPath,
  prepareAttachment,
  relocateAttachment,
  removeAttachment,
  restorePendingAttachments,
  retryAttachment,
  savePendingAttachments,
  stateText,
  uploadAttachment,
  waitUntilSettled,
  type AttachmentRef,
} from "../services/attachments";

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

const topicText = computed(() => session.topicName || (session.currentTopicId ? "当前话题" : "默认话题"));

const anchorText = computed(() => {
  // 只有「历史位置」才提示：成功一轮后位置推进到当前片段，提示自动消失
  if (!session.anchorHistoric) return "";
  const f = session.anchorFragment;
  if (f?.title) return `从「${f.title}」继续`;
  // 不暴露内部片段 ID，也不写 anchor 这类内部术语
  if (session.anchorFragmentId) return "从选中的历史位置继续";
  return "";
});

/**
 * 「已登记、还没落实」的接续选择。
 *
 * 与 anchorText 的区别：anchorText 说的是「位置就在一段历史上」，
 * 这一条说的是「你选了从这段历史继续，下一条消息才会落实」——
 * 界面必须说清是哪种，否则用户会以为已经建了新片段（其实什么都没建）。
 */
const continuationText = computed(() => {
  const pending = session.pendingContinuation;
  if (!pending) return "";
  return pending.sourceTitle
    ? `下一条消息将从「${pending.sourceTitle}」继续`
    : "下一条消息将从所选历史继续";
});

const cancelling = ref(false);

async function cancelContinuation() {
  if (cancelling.value) return;
  cancelling.value = true;
  try {
    await api.cancelContinuation();
    // 服务端会广播新的 ANCHOR（pending 字段为空）→ 提示自动消失；
    // 这里不自行清空，避免「本地以为取消了、服务端还留着」。
  } catch {
    /* 取消失败：提示保留，状态以服务端为准 */
  } finally {
    cancelling.value = false;
  }
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
  pending.value = pending.value.filter((item) => !ids.has(item.id));
  session.lastSendRejection = null;
  attachError.value = "";
  await submit();
}

async function submit() {
  const value = text.value.trim();
  if (!value) return;
  // 附件闸门：没准备好就不能当作「发送成功」——文本与附件都留在原地，并说清卡在哪一个
  const blocked = attachmentBlockReason();
  if (blocked) {
    attachError.value = blocked;
    return;
  }
  const draftSnapshot = text.value;
  const sentAttachments = pending.value.slice();
  // 立即反馈：先清空（这一帧就能看到「已经交出去了」），再等请求结果
  text.value = "";
  // v-model 的清空是异步写回 DOM 的：必须等这一帧之后再测量，
  // 否则量到的还是旧内容的高度，输入框发送后不会收回原尺寸。
  await nextTick();
  autosize();
  const hasAttachments = sentAttachments.length > 0;
  if (hasAttachments) {
    // 只有「带了附件 + 后端还没受理」才需要这个状态；不带附件一律不显示。
    // 防抖：秒级就绪的路径在阈值之前就受理了，界面不该闪一下。
    sendWaiting.value = true;
    preparingCancelled.value = false;
    preparingNotice.value = "";
    preparingCancelBusy.value = false;
    prepareId.value = newPrepareId();
    sendAbort = new AbortController();
    preparingTimer = setTimeout(() => {
      if (sendWaiting.value) preparingVisible.value = true;
    }, PREPARING_VISIBLE_AFTER_MS);
  }
  let ok = false;
  try {
    ok = await sendWithAttachments(
      value,
      sentAttachments.map((item) => item.id),
      sentAttachments,
      sendAbort ? { signal: sendAbort.signal, prepareId: prepareId.value } : undefined,
    );
  } finally {
    if (preparingTimer !== null) {
      clearTimeout(preparingTimer);
      preparingTimer = null;
    }
    sendWaiting.value = false;
    preparingVisible.value = false;
    preparingCancelBusy.value = false;
    sendAbort = null;
  }
  if (!ok) {
    // 发送失败：草稿放回去（用户不必重写），附件也留着（失败不是附件的错）。
    // 若期间已输入新内容则不覆盖。
    if (!text.value.trim()) {
      text.value = draftSnapshot;
      await nextTick();
      autosize();
    }
    // 用户自己中止的：如实说「这一轮没有发送」，不要伪装成失败原因
    if (preparingCancelled.value) {
      preparingNotice.value = "已中止：这一轮没有发送（文字与附件都留在输入区）";
    }
    return;
  }
  // 已被受理：后端在受理时就把这批附件绑到了这一轮，chip 可以清掉
  const sentIds = new Set(sentAttachments.map((item) => item.id));
  pending.value = pending.value.filter((item) => !sentIds.has(item.id));
  attachError.value = "";
  if (preparingCancelled.value) {
    /**
     * 竞态：用户按了中止，但请求在那一刻**已经受理**（轮次已入队）。
     * 这时不能假装「从未发送」—— 如实说「已取消」，并用既有停止入口真的取消它。
     */
    preparingNotice.value = "已取消：这一轮已经受理，已按「停止」取消";
    void stopTurn();
  }
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
/** 正在等后端受理（且这次带了附件） */
const sendWaiting = ref(false);
/** 真的显示出来的「正在准备附件…」（过了防抖阈值才置位，避免闪一下） */
const preparingVisible = ref(false);
const preparingCancelled = ref(false);
const preparingNotice = ref("");
/** 这一次发送的准备标识（契约 §1.1）：中止时用它调取消端点，**以后端确认为准** */
const prepareId = ref("");
/** 正在向后端确认中止（界面显示「正在中止…」；拿不到确认不宣称成功） */
const preparingCancelBusy = ref(false);

/** 准备标识：优先用平台 UUID，缺失时退化成随机串（两者都不含用户数据） */
function newPrepareId(): string {
  const c = globalThis.crypto;
  if (c && typeof c.randomUUID === "function") return c.randomUUID();
  return `prep_${Date.now().toString(36)}${Math.random().toString(36).slice(2, 10)}`;
}
let preparingTimer: ReturnType<typeof setTimeout> | null = null;
let sendAbort: AbortController | null = null;

/** 待发送附件（chip 列表）：发送成功后才清掉，发送失败连文本一起留着。 */
const pending = ref<AttachmentRef[]>([]);
/** 正在登记（POST 在途）：只是提示，不代表准备好 */
const attaching = ref(false);
const attachError = ref("");
const dragging = ref(false);
const pathOpen = ref(false);
const pathDraft = ref("");
const fileInputRef = ref<HTMLInputElement | null>(null);
/** 打开/重新定位之后的事实说明（例如「已在文件夹中显示：可执行文件不自动运行」） */
const attachNote = ref("");
/** 浏览器里没有原生选择器时，「粘贴路径 → 重新定位」的目标附件 id */
const relocateTargetId = ref("");
/** 「重新上传」的目标（§1.4）：QIO 没有内容也没有原地址，用户重新给一次后替换掉旧行 */
const reuploadTarget = ref<{ id: string; name: string } | null>(null);

/**
 * 重新上传这一条：走**既有**的选择流程（桌面原生选择器 → 浏览器文件选择），
 * 成功后由 finishReupload 替换掉旧的失败行。绝不假装 QIO 自己能找回内容。
 */
function reuploadOne(id: string) {
  const item = pending.value.find((a) => a.id === id);
  if (!item) return;
  reuploadTarget.value = { id, name: item.name };
  attachNote.value = `请重新选择「${item.name}」：QIO 无法从原地址恢复（选好后替换这一条）`;
  void pickFile();
}

/** 重新上传成功之后：删掉旧的失败行并如实说明（删不掉就留着，不假装替换成功）。 */
async function finishReupload() {
  const target = reuploadTarget.value;
  reuploadTarget.value = null;
  if (!target) return;
  try {
    await removeAttachment(target.id);
    pending.value = pending.value.filter((a) => a.id !== target.id);
    attachNote.value = `已重新上传：原来那条「${target.name}」已替换（QIO 无法从原地址恢复）`;
  } catch (err) {
    attachNote.value = `新的附件已经登记，但旧的失败行没有删掉（${(err as Error).message}）：可以手动移除`;
  }
}
let stopDropWatch: (() => void) | null = null;

function upsert(item: AttachmentRef) {
  const index = pending.value.findIndex((a) => a.id === item.id);
  if (index >= 0) pending.value.splice(index, 1, item);
  else pending.value.push(item);
}

/** 跟进一个附件的准备状态：prepared → ready / failed / changed / missing。 */
async function track(item: AttachmentRef) {
  upsert(item);
  try {
    const settled = await waitUntilSettled(item, { onUpdate: upsert });
    upsert(settled);
  } catch (err) {
    // 跟进失败 ≠ 附件失败：如实说「状态没跟到」，让用户刷新或重试
    attachError.value = `附件「${item.name}」的准备状态没有跟到：${(err as Error).message}`;
  }
}

/** 真实路径（拖放 / 原生选择器 / 粘贴）：登记后由后台复制或记引用。 */
async function addPaths(paths: string[]) {
  if (!paths.length) return;
  attachError.value = "";
  attaching.value = true;
  try {
    for (const path of paths) {
      try {
        const created = await prepareAttachment(path, { topicId: session.currentTopicId ?? null });
        void track(created);
      } catch (err) {
        attachError.value = `「${path}」没有登记成功：${(err as Error).message}`;
      }
    }
  } finally {
    attaching.value = false;
  }
  await finishReupload();
}

/** 浏览器回退：只有字节（没有真实路径）时走上传；绝不用 input[type=file] 的 fakepath。 */
async function addFiles(files: FileList | File[]) {
  const list = Array.from(files);
  if (!list.length) return;
  attachError.value = "";
  attaching.value = true;
  try {
    for (const file of list) {
      try {
        upsert(await uploadAttachment(file, { topicId: session.currentTopicId ?? null }));
      } catch (err) {
        attachError.value = `「${file.name}」没有上传成功：${(err as Error).message}`;
      }
    }
  } finally {
    attaching.value = false;
  }
  await finishReupload();
}

/** 点击「附件」：桌面端用原生选择器拿真实路径；失败或不支持时退回文件选择/粘贴路径。 */
async function pickFile() {
  attachError.value = "";
  if (isDesktopShell()) {
    try {
      const path = await pickLocalPath();
      if (path) {
        await addPaths([path]);
        return;
      }
      // 用户取消：什么都不做（不算失败）
      if (await desktopPickerAvailable()) return;
    } catch (err) {
      attachError.value = `原生文件选择器不可用（${(err as Error).message}）：已退回文件选择`;
    }
  }
  fileInputRef.value?.click();
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
  if (input.files?.length) void addFiles(input.files);
  input.value = ""; // 同一个文件可以再次选择
}

async function submitPath() {
  const value = pathDraft.value.trim().replace(/^"|"$/g, "");
  if (!value) return;
  pathDraft.value = "";
  pathOpen.value = false;
  // 「重新定位」模式：这条路径是某个待发附件的新位置，不是新附件
  if (relocateTargetId.value) {
    const target = relocateTargetId.value;
    relocateTargetId.value = "";
    await applyRelocate(target, value);
    return;
  }
  await addPaths([value]);
}

async function removeOne(id: string) {
  const before = pending.value;
  if (relocateTargetId.value === id) {
    relocateTargetId.value = "";
    pathOpen.value = false;
  }
  pending.value = pending.value.filter((a) => a.id !== id);
  // 用户自己把这个附件移掉了：拒绝信息里对应的那条也一起收掉（不留一条点不动的待办）
  dropFromRejection(id);
  try {
    await removeAttachment(id);
  } catch (err) {
    pending.value = before; // 没删掉就还在：不制造「已经移除」的假象
    attachError.value = `移除附件失败：${(err as Error).message}`;
  }
}

async function retryOne(id: string) {
  attachError.value = "";
  attachNote.value = "";
  try {
    void track(await retryAttachment(id));
  } catch (err) {
    attachError.value = `重试失败：${(err as Error).message}`;
  }
}

/**
 * 打开一个待发附件（问题 5）：桌面走原生（可执行/脚本类只「在文件夹中显示」），
 * 浏览器走认证 fetch 出来的 Blob（查看或下载）。失败如实说原因，不假装已打开。
 */
async function openOne(id: string) {
  const item = pending.value.find((a) => a.id === id);
  if (!item) return;
  attachError.value = "";
  attachNote.value = "";
  try {
    const result = await openAttachment(item);
    if (result.note) attachNote.value = result.note;
  } catch (err) {
    attachError.value = `打开「${item.name}」失败：${(err as Error).message}`;
  }
}

async function applyRelocate(id: string, path: string) {
  try {
    const accepted = await relocateAttachment(id, path);
    attachError.value = "";
    attachNote.value = "已受理重新定位：状态跟到 ready 才算成功（准备中不是成功）";
    void track(accepted);
  } catch (err) {
    attachError.value = `重新定位失败：${(err as Error).message}`;
  }
}

/**
 * 重新定位（问题 5）：桌面壳用原生选择器拿真实路径；浏览器里退回「粘贴真实路径」。
 * 不校验大小/方式的责任在后端（按真实大小重算 copy/reference）；这里只负责拿到路径。
 */
async function relocateOne(id: string) {
  const item = pending.value.find((a) => a.id === id);
  if (!item) return;
  attachError.value = "";
  attachNote.value = "";
  if (isDesktopShell()) {
    try {
      const path = await pickLocalPath();
      if (!path) return; // 用户取消：什么都不做（不算失败）
      await applyRelocate(id, path);
      return;
    } catch (err) {
      attachError.value = `原生选择器不可用（${(err as Error).message}）：请用「路径」粘贴真实路径`;
    }
  }
  relocateTargetId.value = id;
  pathOpen.value = true;
  attachNote.value = `把「${item.name}」的新位置粘到下面，回车即可重新定位`;
}

// ---------------------------------------------------------------------------
// 待发附件恢复（问题 3）：组件重建 / 刷新之后，看到的 == 将发送的
// ---------------------------------------------------------------------------

function pendingTopicKey(): string | null {
  return session.currentTopicId ?? null;
}

let restoring = false;

/**
 * 恢复待发列表：逐条向后端核对现在的事实（还在不在 / 属不属于本话题 / 有没有被别的轮次绑走）。
 * 对不上的丢掉并说明原因 —— 绝不出现「界面上有、其实发不出去」。
 */
async function restorePending() {
  restoring = true;
  try {
    const { items, dropped } = await restorePendingAttachments(pendingTopicKey());
    pending.value = items;
    if (dropped.length) {
      attachError.value = `这些附件已经不在待发列表里（已删除、已随别的消息发出，或不属于本话题）：${dropped.join("、")}`;
    }
  } finally {
    restoring = false;
  }
}

// 待发列表一变就落盘（按话题分键）：刷新、切话题、组件重建之后都能恢复
watch(
  pending,
  (items) => {
    if (restoring) return;
    savePendingAttachments(pendingTopicKey(), items);
  },
  { deep: true },
);

// 切话题绝不串：先把旧话题的列表落盘，再恢复新话题自己的那份
watch(
  () => session.currentTopicId,
  async (next, prev) => {
    savePendingAttachments(prev ?? null, pending.value);
    pending.value = [];
    attachNote.value = "";
    relocateTargetId.value = "";
    pathOpen.value = false;
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
  // 恢复本话题的待发附件（组件重建 / 刷新后「看到的 == 将发送的」）
  void restorePending();
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

onBeforeUnmount(() => {
  stopDropWatch?.();
  stopDropWatch = null;
});

/** 发送前的闸门：附件没准备好就不能当作发送成功（文本与附件都留着）。 */
const blockedAttachments = computed(() => pending.value.filter((a) => !isSendable(a)));

function attachmentBlockReason(): string {
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

function autosize() {
  const el = inputRef.value;
  if (!el) return;
  // 空草稿：清掉内联高度，回到浏览器自然尺寸（与刚打开时完全一致）
  if (!el.value.trim()) {
    el.style.height = "";
    return;
  }
  el.style.height = "auto";
  el.style.height = Math.min(el.scrollHeight, MAX_INPUT_PX) + "px";
}

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
  if (!sendWaiting.value || preparingCancelBusy.value) return;
  preparingCancelBusy.value = true;
  preparingNotice.value = "正在中止…（等后端确认）";
  try {
    const confirmed = await session.cancelPreparing(prepareId.value);
    if (!confirmed) {
      // 拿不到确认：不宣称成功，用户还能再点一次
      preparingNotice.value = session.lastError ?? "中止失败：没有拿到后端确认";
      return;
    }
    if (confirmed.state === "cancelled") {
      preparingCancelled.value = true;
      preparingNotice.value = "已中止：这一轮没有发送（文字与附件都留在输入区）";
      sendAbort?.abort();
    } else if (confirmed.state === "already_started") {
      preparingCancelled.value = false;
      preparingNotice.value = "已受理，已按「停止」取消";
      await stopTurn();
    } else {
      preparingNotice.value = "后端不认识这次发送（可能刚开始或已经结束）：请稍候再看结果";
    }
  } finally {
    preparingCancelBusy.value = false;
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
    @dragover.prevent="onDragOver"
    @dragleave="onDragLeave"
    @drop.prevent="onDrop"
  >
    <div class="topicbar">
      <span class="tname serif" :title="session.currentTopicId ?? undefined">{{ topicText }}</span>
      <!-- 有「待落实」的接续选择时不再并排显示「位置就在历史上」那一条：
           两句话说的是同一件事的两个阶段，并排会挤成一团、也读不清哪一句生效 -->
      <span v-if="anchorText && !continuationText" class="anchor mono">{{ anchorText }}</span>
      <!-- 已登记、还没落实的接续选择：说清「下一条消息才生效」，并允许取消 -->
      <template v-if="continuationText">
        <span class="anchor mono pending">{{ continuationText }}</span>
        <button
          class="anchor-cancel"
          type="button"
          :disabled="cancelling"
          @click="cancelContinuation"
        >
          {{ cancelling ? "取消中…" : "取消" }}
        </button>
      </template>
      <span class="spacer"></span>
      <span class="kbd-hint mono">Enter 发送 · Shift+Enter 换行</span>
    </div>

    <div
      v-if="pending.length || attaching || attachError || attachNote || pathOpen || sendRejection || preparingVisible || preparingNotice"
      class="attach-area"
    >
      <div class="attach-row">
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
      <p v-if="preparingNotice" class="attach-note" role="status" data-test="preparing-notice">
        {{ preparingNotice }}
      </p>
      <div v-if="pathOpen" class="path-row">
        <input
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
          @click="pathOpen = false; pathDraft = ''; relocateTargetId = ''"
        >
          取消
        </button>
      </div>
      <!-- 附件没附上（结构化失败）：说清是哪几个、为什么，并给一个出口 -->
      <div v-if="sendRejection" class="attach-reject" data-test="attach-reject" role="alert">
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
      <p v-if="attachError" class="attach-error" role="alert">{{ attachError }}</p>
      <p v-if="attachNote" class="attach-note" role="status">{{ attachNote }}</p>
    </div>

    <div class="input-row">
      <button
        class="attach-btn"
        type="button"
        :disabled="attaching"
        title="添加附件（桌面端选择本地文件；也可以拖入或粘贴路径）"
        aria-label="添加附件"
        @click="pickFile"
      >
        附件
      </button>
      <button
        class="attach-btn ghost"
        type="button"
        title="粘贴本地文件路径（拖入不方便时用；不会把浏览器给的假路径当路径）"
        aria-label="粘贴本地文件路径"
        @click="pathOpen = !pathOpen"
      >
        路径
      </button>
      <textarea
        ref="inputRef"
        id="composer-input"
        v-model="text"
        class="qio-input"
        aria-label="输入消息"
        placeholder="和 QIO 说点什么…"
        @keydown="onKeydown"
        @input="autosize"
      ></textarea>
      <button
        v-if="session.turnRunning"
        class="stop-btn"
        type="button"
        :disabled="stopping || !session.canStopTurn"
        :aria-label="stopLabel"
        :title="stopTitle"
        @click="stopTurn"
      >
        <span class="sq"></span>
        <span class="stop-label">{{ stopLabel }}</span>
      </button>
      <button
        class="send-btn"
        type="button"
        :disabled="!text.trim()"
        :aria-label="session.turnRunning ? '排队发送' : '发送'"
        :title="session.turnRunning ? '排队发送（Enter）' : '发送（Enter）'"
        @click="submit"
      >
        <span>↑</span>
      </button>
    </div>
    <p v-if="cancelError" class="cancel-error" role="alert">{{ cancelError }}</p>
    <!-- 浏览器回退：只拿字节上传；fakepath 绝不当路径 -->
    <input
      ref="fileInputRef"
      class="file-input"
      type="file"
      multiple
      aria-hidden="true"
      tabindex="-1"
      @change="onFileInput"
    />
    <div v-if="dragging" class="drop-hint">松开即可添加为附件</div>
  </div>
</template>

<style scoped>
/* 右下角大气泡：融入对话页 flex 布局（消息流下方、靠右），
   浅色表面 + 玫红描边 + 右下小圆角，不可拖动；
   增高时自动向上生长，消息流结束于气泡上方，天然不遮挡 */
.composer {
  /* 独立悬浮于消息流之上，与对话内容同一列（居中 860px），不参与消息流布局；
     消息流底部通过滚动缓冲让出本气泡高度，滚到底时最新消息停在气泡上方。

     左右贴边取「正文列」令牌，不再手算 `calc(50% - Npx)`：手算的偏移与列宽、
     与右侧给入口球留的通道都脱钩 —— 900–1400px 区间里它整体右移了 418px、
     右边缘溢出视口 194px（发送按钮跑到屏幕外），还盖住了停在右下角的入口球。 */
  position: fixed;
  left: var(--column-inset-left);
  right: var(--column-inset-right);
  width: auto;
  max-width: 860px;
  margin: 0 auto;
  bottom: 16px;
  z-index: 12;
  /* 默认中性边框 + 较轻阴影：不靠重描边和重阴影抢注意力 */
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-lg);
  padding: 10px 18px 14px;
  background: var(--bg-surface);
  box-shadow: var(--shadow-2);
  transition: border-color var(--dur-fast) var(--ease-1), box-shadow var(--dur-fast) var(--ease-1);
}
/* 聚焦时只加强一层：外框变强调色；内层输入框不再同时叠一层边框 + 光晕 */
.composer:focus-within { border-color: var(--accent); }
.composer textarea.qio-input:focus {
  border-color: transparent;
  box-shadow: none;
  background: transparent;
}
/* 窄窗口：整宽贴底，不缩成小气泡、不遮挡文字 */
@media (max-width: 899px) {
  .composer {
    left: var(--sp-3);
    right: var(--sp-3);
    max-width: none;
  }
}
.topicbar {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 8px;
  font-size: 12px;
}
.tname {
  font-weight: 600;
  color: var(--text-strong);
  font-size: 15px;
  max-width: 320px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.anchor {
  font-size: 10.5px;
  color: var(--text-muted);
  letter-spacing: 0.04em;
  max-width: 260px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
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
  gap: 10px;
}
.input-row textarea.qio-input {
  flex: 1;
  min-height: 46px;
  /* 随内容增长，到上限后内部滚动：长输入不会把聊天窗口挤成一条 */
  max-height: min(40vh, 320px);
  resize: none;
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
}
.send-btn {
  width: 38px;
  height: 38px;
  flex-shrink: 0;
  border: none;
  border-radius: 50%;
  background: var(--accent);
  color: var(--on-accent);
  font-size: 18px;
  line-height: 1;
  cursor: pointer;
  /* 状态切换连续：默认 → hover → pressed → disabled 都走同一层令牌 */
  transition: background var(--dur-fast) var(--ease-1), transform var(--dur-press) var(--ease-1-out),
    opacity var(--dur-fast) var(--ease-1);
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
.attach-area {
  margin-bottom: 8px;
}
/* 「正在准备附件…」：统一的一行状态 + 中止入口（不是阶段历史，也不是执行迹象） */
.preparing-status {
  display: flex;
  align-items: center;
  gap: 6px;
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
}
.path-row {
  display: flex;
  align-items: center;
  gap: 6px;
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
}
.attach-reject-why {
  margin-left: 6px;
}
.attach-error {
  margin-top: 6px;
  font-size: 12px;
  color: var(--danger);
}
/* 事实说明（例如「已在文件夹中显示：可执行文件不自动运行」）：不是错误，也不能当成功 */
.attach-note {
  margin-top: 6px;
  font-size: 12px;
  color: var(--text-secondary);
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
