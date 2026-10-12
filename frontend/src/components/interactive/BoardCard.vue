<!--
  单张板面卡片（子智能体 A 负责）：文字注释 / 文件 / 图片 / 代码 / 网址 / QIO 结果。

  契约：docs/interactive-mode-contract.md §1.1 / §1.4 / §9.6。
  这里落地的规则：
  - 信息分层：**材料类型 → 标题 → 正文 → 元信息**（元信息含状态文字、所在组、更新时间）；
  - 勾选框只出现在**文字注释**上，而且放在「选中后才浮出的局部工具栏」里；
    材料默认就在 QIO 可查看范围内，没有这个选择框；reply 不参与勾选；
  - 局部工具栏（data-im="card-toolbar"）只放**这张卡片自己的操作**
    （编辑 / 复制 / 折叠 / 隐藏 / 书签 / 删除 / 勾选）；分组类操作在 SelectionMenu 里；
  - 选中后卡片四边出现**连接点**（data-im="connect-point"）。工具栏与整理菜单都朝卡片外侧展开，
    实测不盖标题、连接点与正在编辑的正文；
  - 状态一律「文字 + 颜色」双通道，不能只靠颜色；
  - 卡片本身不做状态计算：所有变化 emit 给 BoardCanvas，由它调 board.ts 纯函数 + store.commit。
-->
<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from "vue";
import { useInteractiveStore } from "../../stores/interactive";
import {
  cardDraftKey,
  cardDraftMetaFieldsForKind,
  readCardDraftInputForBoard,
  restoreCardDraftInput,
} from "../../interactive/drafts";
import type { CardDraftInput, CardDraftMetaInput } from "../../interactive/drafts";
import { CARD_KIND_LABELS, CHECKABLE_KINDS } from "../../interactive/board";
import {
  cardDocFields,
  docFromCard,
  parseDocText,
  serializeDocText,
  type TextBlock,
  type TextDoc,
} from "../../interactive/textCard";
import { isSafeExternalUrl, openExternal } from "../../utils/externalLink";
import {
  cardAttachmentId,
  cardAttachmentLinked,
  cardAttachmentText,
} from "../../interactive/materials";
// 卡片内真实预览（契约 §3.5）：预览主体挂在**卡片内部**，不盖住编辑区、勾选框与卡片操作。
// 渲染器按需动态加载；组件自己在收起 / 换附件 / 卸载时中止在途工作并释放资源。
import CardInlinePreview from "./CardInlinePreview.vue";
import type { PreviewPosition } from "../../services/attachments";
import {
  pickBrowserFile,
  stateText,
  uploadAttachment,
  waitUntilSettled,
} from "../../services/attachments";
import type { BoardCard, BoardGroup } from "../../interactive/types";
import SelectionMenu from "./SelectionMenu.vue";
import CardDraftHint from "./CardDraftHint.vue";
import CardAttachmentPreview from "./CardAttachmentPreview.vue";
import TextCardEditor from "../../interactive/TextCardEditor.vue";

const props = defineProps<{
  card: BoardCard;
  selected: boolean;
  /** 板内搜索定位后的短暂高亮（只是显示） */
  highlight: boolean;
  dragging: boolean;
  x: number;
  y: number;
  groupName: string | null;
  groups: BoardGroup[];
  /** 选中时局部工具栏的位置（板面容器坐标，父组件按视口换算好） */
  toolbarLeft: number;
  toolbarTop: number;
  /** 多选时：只显示共同适用的操作，并且不把单卡片编辑应用到整组 */
  multi: boolean;
  /** 正在从本卡片拖出关系线 */
  connecting: boolean;
}>();

/**
  事件签名保持不变：BoardCanvas 仍在监听 leave-group / join-group，
  分组操作改由 SelectionMenu 直接算状态并 commit（契约 §8.4.1），这两个事件保留只为不打断画布接线。
*/
const emit = defineEmits<{
  (e: "select", cardId: string, additive: boolean): void;
  (e: "drag-start", cardId: string, event: PointerEvent): void;
  (e: "patch", cardId: string, patch: Partial<BoardCard>, label: string): void;
  (e: "toggle", cardId: string, flag: "checked" | "hidden" | "folded" | "bookmarked"): void;
  (e: "remove", cardId: string): void;
  (e: "duplicate", cardId: string): void;
  (e: "leave-group", cardId: string): void;
  (e: "join-group", cardId: string, groupId: string): void;
  (e: "connect-start", cardId: string, event: PointerEvent): void;
}>();

const store = useInteractiveStore();
const editing = ref(false);
const draft = ref("");
/** 这一版编辑态是否已经按恢复规则重建过（空草稿与「没有草稿」必须能区分） */
const draftLoaded = ref(false);
/** 文字卡编辑区实例：关闭态点「编辑」要把焦点与光标落到对应行（契约 §1.7） */
const textEditor = ref<{ focusLine: (line: number) => void } | null>(null);
const metaName = ref("");
const metaLanguage = ref("");
const metaHref = ref("");
const metaTitle = ref("");

/**
 * store 与本组件在两个工作树里分别实现（约定接口见本轮契约 §3/§4）：
 * - `setCardDraftInput(cardId, { text, meta })`：把**正文与适用附加字段**作为同一份
 *   未完成输入写进同一条本机记录（唯一写者仍是 store 的草稿通道）；
 * - `boardContentConflictFor / resolveBoardContentConflict`：F3 的正文冲突选择入口。
 *
 * 两个入口合并前用可选调用：缺接口时退化为既有行为（只保存正文 / 不渲染该冲突块），
 * 不会静默使用第二套逻辑，也不会阻塞编译；合并后自动走完整路径。
 */
type CardDraftInputStoreApi = {
  setCardDraftInput?: (cardId: string, input: CardDraftInput) => void;
  boardContentConflictFor?: (cardId: string) => { local: string; server: string } | null;
};
const draftInputApi = store as unknown as CardDraftInputStoreApi;

/** 本卡片种类适用的附加字段（文字注释 / reply 没有附加字段） */
function applicableMetaFields(): (keyof CardDraftMetaInput)[] {
  return cardDraftMetaFieldsForKind(props.card.kind) as (keyof CardDraftMetaInput)[];
}

/** 当前编辑态里适用的附加字段（只收适用字段，含空串：空串是「用户明确清空」） */
function currentMetaInput(): CardDraftMetaInput {
  const meta: CardDraftMetaInput = {};
  for (const field of applicableMetaFields()) {
    if (field === "name") meta.name = metaName.value;
    else if (field === "language") meta.language = metaLanguage.value;
    else if (field === "href") meta.href = metaHref.value;
    else if (field === "title") meta.title = metaTitle.value;
  }
  return meta;
}

/** 当前这一版的完整未完成输入：正文 + 适用附加字段（N6：两者一起保存、一起恢复） */
function currentDraftInput(): CardDraftInput {
  return { text: draft.value, meta: currentMetaInput() };
}

/**
 * 保存当前这一版未完成输入：正文与附加字段**一次**写进同一条本机记录。
 * 不直接写存储、不另建写者：走 store 的草稿通道（版本、冲突、清除登记规则全部复用）。
 */
function saveDraftInput() {
  const cardId = props.card.id;
  if (typeof draftInputApi.setCardDraftInput === "function") {
    draftInputApi.setCardDraftInput(cardId, currentDraftInput());
    return;
  }
  // 过渡期（store 尚未提供合并后的入口）：只保存正文，保持既有行为不变
  store.setDraft(cardDraftKey(cardId), draft.value);
}

/** F3 的正文冲突（本页候选与服务器同一张卡片正文都有改动）：没有接口或没有冲突时为 null */
function boardContentConflict(cardId: string): { local: string; server: string } | null {
  if (typeof draftInputApi.boardContentConflictFor !== "function") return null;
  return draftInputApi.boardContentConflictFor(cardId);
}

const checkable = computed(() => CHECKABLE_KINDS.includes(props.card.kind));
const kindLabel = computed(() => CARD_KIND_LABELS[props.card.kind]);
const name = computed(() => String(props.card.meta?.name ?? ""));
const language = computed(() => String(props.card.meta?.language ?? ""));
const href = computed(() => String(props.card.meta?.href ?? ""));
const linkTitle = computed(() => String(props.card.meta?.title ?? "") || href.value);

/* ------------------------------------------------------------------------
 * 统一文字卡：编辑与显示（契约 2026-10-12 §1.3 / §1.7）
 *
 * 文字卡按**块序列**（meta.doc，权威）渲染；编辑走 TextCardEditor（一个连续编辑面）。
 * 历史 code / url 卡仍能显示（兜底分支 + 「旧卡片，尚未迁移」标注），但整体不交付。
 * ---------------------------------------------------------------------- */

/** 这张卡片用不用统一文字卡渲染：kind="text"（含迁移后的旧 code / url）。 */
const isTextCard = computed(() => props.card.kind === "text");

/** 遗留的 code / url 卡：迁移完成前兜底显示，界面必须写明它还没迁移。 */
const LEGACY_KINDS = ["code", "url"] as const;
const isLegacyCard = computed(() => (LEGACY_KINDS as readonly string[]).includes(props.card.kind));

/** 卡片的内容模型（meta.doc 权威 → meta.docText → content 兜底）。 */
const cardDoc = computed<TextDoc>(() => docFromCard(props.card));

/** 卡片自己那一份编辑区文本（meta.docText 权威 → meta.doc → content 兜底）。 */
function cardDocText(): string {
  return serializeDocText(cardDoc.value);
}

/**
 * 编辑区要显示的那一份文本：**未完成输入优先**（草稿恢复、冲突里选中的那一份）。
 *
 * `draftLoaded` 是「已经按恢复规则重建过编辑态」的标记：没有它就无法区分
 * 「还没有草稿」与「草稿就是空串」—— 后者是有效编辑状态（契约 §10.4），
 * 不能用正式正文把它顶掉。
 */
const editorText = computed(() => (editing.value && draftLoaded.value ? draft.value : cardDocText()));

/** 关闭态与「完成编辑」用的同一份文本。 */
const docText = computed(() => (editing.value && draft.value ? draft.value : cardDocText()));
const liveDoc = computed(() => parseDocText(docText.value));

/** 块的起始行号：显示里点「编辑」必须把光标落到**那一行**（契约 §1.7）。 */
function blockLineStarts(doc: TextDoc): number[] {
  const out: number[] = [];
  let line = 0;
  for (const block of doc.blocks) {
    out.push(line);
    const lines = Math.max(
      1,
      String(block.text ?? "").split("\n").filter((item) => item !== "").length,
    );
    line += block.kind === "code" ? 1 + lines + 1 : lines;
  }
  return out;
}

interface ShownBlock {
  block: TextBlock;
  index: number;
  line: number;
}

/**
 * 关闭态要显示的块：**只解析这一份文本**（docText 已经按「候选优先」给出该显示的那一份），
 * 显示与行号来自同一次解析 —— 两者用两份文本算就会错位。
 */
const shownBlocks = computed<ShownBlock[]>(() => {
  const doc = parseDocText(docText.value);
  const starts = blockLineStarts(doc);
  return doc.blocks.map((block, index) => ({
    block,
    index,
    line: starts[index] ?? 0,
  }));
});

/** 演示 / 编辑态共用的块文本（拿不到块时退回整段文本，绝不显示空）。 */
function blockText(block: TextBlock): string {
  if (block.kind === "link") return block.text || block.attrs?.href || "";
  return block.text;
}

/** 行内格式（加粗 / 斜体 / 删除线 / 行内代码）：先转义，再套标签 —— 不解析任何 HTML。 */
function escapeHtml(raw: string): string {
  return String(raw ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function inlineHtml(raw: string): string {
  let out = escapeHtml(raw);
  out = out.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  out = out.replace(/~~(.+?)~~/g, "<del>$1</del>");
  out = out.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
  out = out.replace(/`([^`]+)`/g, "<code>$1</code>");
  return out;
}

/** 链接块的可读标题与地址（标题为空时界面显示地址）。 */
function linkHref(block: TextBlock): string {
  return String(block.attrs?.href ?? "");
}
function linkBlockTitle(block: TextBlock): string {
  return String(block.text ?? "").trim() || linkHref(block);
}
function linkOpenable(block: TextBlock): boolean {
  return isSafeExternalUrl(linkHref(block));
}

/** 打开链接：走统一的外链白名单入口；不安全 scheme 与打开失败都如实说出来。 */
const linkStatus = ref<{ tone: "ok" | "error"; text: string } | null>(null);
async function openLink(block: TextBlock): Promise<void> {
  const href = linkHref(block);
  if (!href) {
    linkStatus.value = { tone: "error", text: "这条链接还没有地址：可以点「编辑」把地址补上。" };
    return;
  }
  if (!isSafeExternalUrl(href)) {
    linkStatus.value = {
      tone: "error",
      text: "这条链接的地址不是 http / https / mailto，出于安全考虑不可打开：" + href,
    };
    return;
  }
  const ok = await openExternal(href);
  linkStatus.value = ok
    ? { tone: "ok", text: "已交给系统打开：" + href }
    : { tone: "error", text: "打开失败：可以复制地址后手动打开。" };
}

/* 代码块的复制：复用 MarkdownContent 的做法 —— 受限 WebView 下 navigator.clipboard
   可能不存在、写入也可能被拒绝，两种情况都必须如实反馈，不能假装成功。 */
const copyState = ref<{ id: string; ok: boolean } | null>(null);
let copyTimer: ReturnType<typeof setTimeout> | null = null;

async function writeClipboard(text: string): Promise<boolean> {
  try {
    const clipboard = navigator.clipboard;
    if (!clipboard || typeof clipboard.writeText !== "function") return false;
    await clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

function copyTitle(block: TextBlock): string {
  const state = copyState.value;
  if (state && state.id === block.id) {
    return state.ok
      ? "已复制到剪贴板"
      : "复制失败：可以选中代码后按 Ctrl+C 手动复制，或再点一次重试";
  }
  return "复制这段代码";
}

async function copyCode(block: TextBlock): Promise<void> {
  const ok = await writeClipboard(block.text);
  copyState.value = { id: block.id, ok };
  if (copyTimer) clearTimeout(copyTimer);
  copyTimer = setTimeout(
    () => {
      copyState.value = null;
    },
    ok ? 1600 : 2400,
  );
}

onBeforeUnmount(() => {
  if (copyTimer) clearTimeout(copyTimer);
});

/**
 * 「编辑」：进入编辑态并把焦点与光标落到那一行（链接 / 代码都必须能就地改）。
 *
 * 先把编辑区**渲染出来**（等一拍），再让它自己定位 —— 顺序反了就会把光标落在旧位置。
 */
async function editBlockAt(line: number): Promise<void> {
  if (!editing.value) startEdit();
  await nextTick();
  await textEditor.value?.focusLine(line);
}

/**
 * 附件关联（契约 §14.1 的诚实口径）：文件 / 图片卡片只有把**真实已保存的附件 id**
 * 写进 meta，QIO 才读得到正文。没有 attachmentId 时必须如实显示「QIO 读不到它的正文」，
 * 绝不拿文件名当正文。
 */
const attachKind = computed<"file" | "image" | null>(() =>
  props.card.kind === "file" || props.card.kind === "image" ? props.card.kind : null,
);
const attachmentId = computed(() => cardAttachmentId(props.card.meta, props.card.kind));
const attachmentLinked = computed(() => cardAttachmentLinked(props.card.meta, props.card.kind));
const attachmentText = computed(() => cardAttachmentText(props.card.meta, props.card.kind));
const attachStatus = ref<{ tone: "busy" | "ok" | "error"; text: string } | null>(null);

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/**
 * 选一份真实文件 → 走既有上传接口（不新造上传链路）→ 等附件进入最终态 →
 * 把 `{ name, attachmentId, size }` 写进卡片 meta（由父级 commit，组件不直接改板面状态）。
 * 取消与失败都如实说出来，并且显示在卡片上，不静默。
 */
async function pickAttachment(): Promise<void> {
  const kind = attachKind.value;
  if (!kind) return;
  const cardId = props.card.id;
  attachStatus.value = { tone: "busy", text: "正在等待选择文件…" };
  let file: File | null;
  try {
    file = await pickBrowserFile();
  } catch (error) {
    attachStatus.value = { tone: "error", text: "选择文件失败：" + messageOf(error) };
    return;
  }
  if (!file) {
    attachStatus.value = {
      tone: "error",
      text: "没有选择文件：这张卡片还没有关联已保存的附件，QIO 读不到它的正文",
    };
    return;
  }
  attachStatus.value = { tone: "busy", text: "正在保存附件「" + (file.name || "未命名") + "」…" };
  try {
    const uploaded = await uploadAttachment(file);
    const settled = await waitUntilSettled(uploaded);
    if (settled.state !== "ready") {
      throw new Error(
        "附件「" +
          settled.name +
          "」当前不可用：" +
          stateText(settled) +
          (settled.error ? "（" + settled.error + "）" : ""),
      );
    }
    const meta: Record<string, unknown> = { ...(props.card.meta ?? {}) };
    meta.name = settled.name;
    meta[kind] = { name: settled.name, attachmentId: settled.id, size: settled.sizeBytes };
    metaName.value = settled.name;
    emit("patch", cardId, { meta } as Partial<BoardCard>, "关联附件：" + settled.name);
    attachStatus.value = { tone: "ok", text: "已关联已保存的附件「" + settled.name + "」" };
  } catch (error) {
    attachStatus.value = {
      tone: "error",
      text: "附件保存失败：" + messageOf(error) + "；没有关联上，可以重试",
    };
  }
}

/** 正文首行：折叠时当标题用，避免折叠后只剩一个类型标签 */
const firstLine = computed(() => (props.card.content || "").trim().split("\n")[0].trim());

/**
  标题层：材料自己的名字 / 网址标题 / 代码语言；文字注释没有独立标题（正文就是它本身），
  只有折叠后才用首行顶上，保证折叠卡片仍然认得出是哪一条。
*/
const titleText = computed(() => {
  if (props.card.kind === "file" || props.card.kind === "image") return name.value || "未命名" + kindLabel.value;
  if (props.card.kind === "url") return linkTitle.value || "（还没有网址）";
  if (props.card.kind === "code") return language.value ? language.value + " 代码" : "代码";
  return props.card.folded ? firstLine.value : "";
});

/**
  关闭态也需要用户处理的本机 / 草稿状态（本轮修复）。
  
  触发：A 的组件级探针发现，用户在**关闭态**点「用服务器上的」而本机副本删除失败时，
  失败原因与重试入口只在编辑态可见 —— 必须先进「编辑」才看得到，等于不可达。
  这里把「本机副本没删掉 / 清除未同步 / 服务器草稿保存失败 / 本机恢复副本没写成功」
  提到关闭态：提示就地渲染在正文之上（正文区 .card-body 自带滚动，不会被裁掉，
  也不撑开卡片工具栏、不挡连接点）。未决冲突仍沿用既有行为（替换正文，见模板注释）。
*/
const cardDraftNeedsAttention = computed(() => {
  const key = cardDraftKey(props.card.id);
  const state = store.draftStateFor(key);
  const local = store.draftLocalStateFor(key);
  const removal = store.draftRemovalStateFor(key);
  return (
    Boolean(store.draftLocalRemovalErrorFor(props.card.id)) ||
    removal.status === "error" ||
    state.status === "error" ||
    (!local.ok && state.status !== "saved") ||
    // F3 的正文冲突是**必须由用户决定**的：关闭态也要能看到入口（否则入口只在编辑态可达）
    Boolean(boardContentConflict(props.card.id))
  );
});

/** 元信息层：状态必须能读出来，不能只靠颜色。 */
const statusText = computed(() => {
  const parts: string[] = [];
  if (props.card.hidden) parts.push("已隐藏：退出讨论范围");
  else if (checkable.value) {
    parts.push(
      props.card.checked
        ? "已许可：QIO 可以查看这张卡（提交后仍然有效）"
        : "未许可：QIO 看不到这张卡里的任何内容",
    );
  } else if (props.card.kind === "reply") parts.push("QIO 结果：不进提交载荷");
  else parts.push("材料：默认在本次允许查看范围内（不需要勾选）");
  if (props.card.folded) parts.push("已折叠：只改变显示");
  if (props.card.bookmarked) parts.push("书签：查找优先");
  if (props.groupName) parts.push("在组「" + props.groupName + "」");
  return parts.join(" · ");
});

/** 更新时间：数据用等宽字体（三声部里的第三声部），解析失败时如实显示原值 */
const updatedText = computed(() => {
  const raw = props.card.updatedAt;
  const date = new Date(raw);
  if (Number.isNaN(date.getTime())) return raw;
  const pad = (value: number) => String(value).padStart(2, "0");
  return pad(date.getMonth() + 1) + "-" + pad(date.getDate()) + " " + pad(date.getHours()) + ":" + pad(date.getMinutes());
});

/**
  局部工具栏与整理菜单**只由一张卡片渲染**：selection 里最后点中的那张。
  之前每张选中卡片都渲染同一份工具栏、位置又相同，多选时叠成一摞，
  点击命中的是最上面那张（板面顺序最后的一张），可能作用到用户没在看的卡片上。
*/
const isToolbarOwner = computed(() => {
  if (!props.selected) return false;
  const ids = store.board?.selection ?? [];
  if (!ids.length) return false;
  return ids[ids.length - 1] === props.card.id;
});

/** 工具栏在卡片上方时，整理菜单向上展开（朝卡片外侧），不盖标题与连接点 */
const menuOpensUp = computed(() => props.toolbarTop < props.y);

/** 冲突未决的关闭态卡片显示提示与选择按钮：高度放开为 auto，不被 overflow 裁掉（契约 §12.1 可见可点） */
const conflictExpanded = computed(
  () => !props.card.folded && Boolean(store.draftConflictFor(props.card.id) || boardContentConflict(props.card.id)),
);

/**
 * 卡片内预览是否展开（契约 2026-10-12 §3.5 / §3.6）。
 *
 * 状态只有一个来源：store 的 `expandedCards`（**只在前端内存**）。
 * 这里不加新 props、不算几何：展开后的位置与尺寸由 BoardCanvas 用
 * `interactive/cardLayout.ts` 的纯函数算好，再通过 x/y 与 card 传进来。
 * 点击「展开预览」不写板面状态、不产生表达、不触发意图 / 任务。
 */
const previewExpanded = computed(() => store.isCardPreviewExpanded(props.card.id));
function togglePreview(): void {
  store.toggleCardPreview(props.card.id);
}

/**
 * 预览里的阅读位置（页码 / 幻灯片 / 行 / 缩放）—— 走预览服务落库，**不写板面状态**。
 *
 * 组件只把位置交出来，判断只有一处（store 的 savePreviewPosition）：位置按「附件 + 文件版本」
 * 归属，服务端在附件与卡片不一致时回 409；写失败只记原因，不假装成功。
 */
function onPreviewPosition(payload: {
  attachmentId: string;
  version: string;
  position: PreviewPosition;
}): void {
  void store.savePreviewPosition(props.card.id, payload);
}

const style = computed(() => ({
  left: props.x + "px",
  top: props.y + "px",
  width: props.card.w + "px",
  height: props.card.folded || conflictExpanded.value ? "auto" : props.card.h + "px",
  zIndex: props.dragging ? 30 : props.selected ? 8 : 4,
}));

const toolbarStyle = computed(() => ({
  left: props.toolbarLeft + "px",
  top: props.toolbarTop + "px",
}));

function isInteractive(target: EventTarget | null): boolean {
  const element = target as HTMLElement | null;
  if (!element || typeof element.closest !== "function") return false;
  return Boolean(element.closest("button, input, textarea, select, a, label, [data-im='card-toolbar']"));
}

function onPointerDown(event: PointerEvent) {
  // 按钮 / 输入框 / 链接上的按下不算拖动
  if (isInteractive(event.target)) return;
  emit("select", props.card.id, event.shiftKey || event.ctrlKey || event.metaKey);
  emit("drag-start", props.card.id, event);
}

/** 从连接点拖出关系线：交给 BoardCanvas 接管指针（不在这里建链）。 */
function onConnectDown(event: PointerEvent) {
  // 连接点在卡片内部，必须同时拦住冒泡与默认行为，否则会先被卡片自己的拖动接走
  event.stopPropagation();
  event.preventDefault();
  emit("connect-start", props.card.id, event);
}

function startEdit() {
  editing.value = true;
  const cardId = props.card.id;
  /**
   * 冲突未决时（契约 §12.1）：「打开编辑器」**不等于**选择本机版本。
   * 编辑框先显示本机候选，但**不写草稿、不排保存** —— 服务器那份不动，
   * 冲突提示（CardDraftHint 里的「用本机的 / 用服务器上的」）保持可见，等用户明确选择。
   * 用户选服务器后编辑框随 watch 跟随；真正输入新文字时才按正常编辑路径走。
   */
  const conflict = store.draftConflictFor(cardId);
  applyRestore();
  /**
   * 冲突未决时不写草稿、不排保存（§12.1），等用户明确选择；
   * 其余情况把这一版完整输入登记进草稿通道（正文 + 适用附加字段一起）。
   */
  if (conflict) return;
  saveDraftInput();
}

/**
 * 从当前恢复来源重建编辑态内容（N6）：正文与附加字段是**同一份未完成输入**，一次重建。
 * - 正文：按「记录是否存在」判断（空草稿是有效编辑状态，§10.4），存在就用草稿（哪怕是空串），
 *   不存在才回落到正式正文；冲突未决时显示本机候选（§12.1：打开编辑器 ≠ 做出选择）；
 * - 附加字段：取自**同一板面**的本机记录（旧记录没有附加快照时回退正式值）。
 *
 * 「选择服务器上的」会清掉本机那份候选副本，于是这里重建时附加字段自然回到正式值；
 * 「选择本机的」保留本机记录，附加字段继续是本机未完成输入 —— 两种选择共用这一条规则。
 */
function applyRestore() {
  const cardId = props.card.id;
  const conflict = store.draftConflictFor(cardId);
  const localInput = readCardDraftInputForBoard(cardId, store.boardId);
  const restored = restoreCardDraftInput({
    kind: props.card.kind,
    content: props.card.content,
    meta: (props.card.meta ?? {}) as Record<string, unknown>,
    hasUnfinishedInput: store.hasCardDraft(cardId),
    draftText: store.cardDraftText(cardId),
    draftMeta: localInput?.meta ?? null,
  });
  draft.value = conflict ? conflict.local : restored.text;
  draftLoaded.value = true;
  metaName.value = restored.meta.name ?? "";
  metaLanguage.value = restored.meta.language ?? "";
  metaHref.value = restored.meta.href ?? "";
  metaTitle.value = restored.meta.title ?? "";
}

/**
 * 正文或任一附加字段变了都走同一个入口：这一版完整未完成输入（正文 + 适用附加字段）
 * 一起进本机记录。分别保存会让两者不同步（刷新后正文恢复了、网址还是旧的）。
 */
function onDraftInput() {
  saveDraftInput();
}

/**
 * 统一文字卡的输入（契约 §1.3）：编辑区给的就是 `meta.docText`（提交载荷也用这一份）。
 *
 * 这里只做两件事：更新关闭态与「完成编辑」要用的草稿文本，并把它登记进**既有**草稿通道
 * （不新增第二个写者）。块序列由 textCard.ts 的 parseDocText 在提交时重新推导 —— 权威只有一份。
 */
function onTextCardChange(payload: { docText: string; doc: TextDoc }) {
  draft.value = payload.docText;
  saveDraftInput();
}

/**
 * 草稿内容也可能由**别处**改掉：恢复发现新旧冲突后用户选了「用服务器上的」，
 * 或刷新恢复把本机记录读回来。编辑框要跟着走，不能停在旧文字上。
 * 自己打字时 store 与输入框本来就一样，这里比较后不动，不会形成回写循环。
 */
watch(
  () => store.cardDraftText(props.card.id),
  (text) => {
    if (editing.value && text !== draft.value) draft.value = text;
  },
);

/**
 * 冲突刚刚被解决（从「有冲突」变成「没有冲突」）时，按同一条规则重建编辑态：
 * 选「服务器上的」→ 本机候选副本已被清掉，附加字段回到正式值；
 * 选「本机的」→ 本机记录还在，附加字段继续是本机未完成输入。
 * 两种选择共用 applyRestore，不各写一套判断。
 */
watch(
  () => store.draftConflictFor(props.card.id),
  (conflict, previous) => {
    if (!editing.value || conflict || !previous) return;
    applyRestore();
  },
);

/**
 * 卡片自己的正文被外部换掉时（版本更新、板面刷新、换到另一张卡），关闭态与「完成编辑」
 * 用的都是这一份 —— 不跟随就会拿旧正文去确认。
 */
watch(
  () => [props.card.id, props.card.meta, props.card.content],
  () => {
    if (editing.value) return;
    const next = cardDocText();
    if (next !== draft.value) draft.value = next;
  },
  { deep: true },
);

/**
 * 确认编辑才形成有效文字状态；输入过程只存草稿，不调用 QIO。
 *
 * 文字卡（契约 §1.4）：`meta.doc` 是权威块序列，`content` 与 `meta.docText` 都是派生 ——
 * 三者由同一次 patch 一起更新，绝不留下「doc 换了、content 还是旧 markdown」的半份状态。
 */
function confirmEdit() {
  let patch: Partial<BoardCard>;
  if (isTextCard.value) {
    const doc = parseDocText(draft.value);
    const fields = cardDocFields(doc);
    patch = {
      content: fields.content,
      meta: { ...(props.card.meta ?? {}), doc: fields.meta.doc, docText: fields.meta.docText },
    };
    emit("patch", props.card.id, patch, "编辑" + kindLabel.value);
    store.requestDraftClear(cardDraftKey(props.card.id));
    editing.value = false;
    return;
  }
  patch = { content: draft.value };
  if (props.card.kind === "file" || props.card.kind === "image") {
    patch.meta = { ...(props.card.meta ?? {}), name: metaName.value };
  } else if (props.card.kind === "code") {
    patch.meta = { ...(props.card.meta ?? {}), language: metaLanguage.value };
  } else if (props.card.kind === "url") {
    patch.meta = { ...(props.card.meta ?? {}), href: metaHref.value, title: metaTitle.value };
  }
  emit("patch", props.card.id, patch, "编辑" + kindLabel.value);
  /**
   * 确认之后草稿就该消失（契约 §10.4）：写一个空串会留下「存在但正文为空」的记录，
   * 下次打开编辑器会把刚确认的正式内容盖成空。这里登记一次清除。
   *
   * 但**不能点击时就清**（收尾轮 07）：这次板面变更可能只是进了影响确认（尚未保存），
   * 用户还可能取消。点击时先清会把内存候选、本机恢复记录与服务器那份草稿一起处理掉，
   * 取消后新输入连恢复来源都没有。清除登记给 store，等这次正式变更被服务器真实接受后，
   * 由 store 按登记的那一版执行完整清除；等待确认 / 取消 / 保存失败期间候选都保留。
   * 登记之后用户又输入了更新的草稿版本时，这次登记作废（不许清掉后来输入）。
   */
  store.requestDraftClear(cardDraftKey(props.card.id));
  editing.value = false;
}

function cancelEdit() {
  editing.value = false;
  draftLoaded.value = false;
  // 防抖可能还没触发：离开编辑器前把待保存的草稿落盘（失败会有状态与重试入口）
  store.flushDrafts();
}
</script>

<template>
  <article
    class="card"
    :class="{ selected, highlight, dragging, folded: card.folded, hidden: card.hidden, reply: card.kind === 'reply' }"
    :style="style"
    data-im="card"
    :data-card-id="card.id"
    @pointerdown="onPointerDown"
  >
    <header class="card-head">
      <span class="kind mono">{{ kindLabel }}</span>
      <span v-if="titleText" class="title">{{ titleText }}</span>
      <button
        v-if="card.folded"
        class="chip-btn"
        type="button"
        :data-card-id="card.id"
        @click="emit('toggle', card.id, 'folded')"
      >
        展开
      </button>
      <!--
        卡片内预览的入口（契约 §3.5）：展开的是**这张卡本身**，不是弹窗、不盖别的卡片。
        入口与卡片折叠互斥显示：折叠时连正文都没有，先展开卡片再说。
      -->
      <button
        v-if="!card.folded"
        class="chip-btn"
        type="button"
        data-im="card-preview-toggle"
        :data-card-id="card.id"
        :aria-expanded="previewExpanded"
        @click="togglePreview"
      >
        {{ previewExpanded ? "收起预览" : "展开预览" }}
      </button>
    </header>

    <div v-if="!card.folded" class="card-body">
      <template v-if="editing">
        <!--
          未决冲突的选择入口放在编辑器**上方**（契约 §12.1：冲突入口在真实操作路径可见可点）：
          卡片有固定高度、编辑区内部滚动，提示若排在编辑器之后，打开编辑器的第一屏就看不见它。
          -->
        <CardDraftHint v-if="store.draftConflictFor(card.id) || boardContentConflict(card.id)" :card-id="card.id" />
        <!--
          文字卡（含迁移后的旧 code / url）：**一个连续编辑区**（契约 §1.3）。
          输入即把草稿登记进既有通道；草稿文本就是 meta.docText（提交载荷也用这一份）。
        -->
        <TextCardEditor
          v-if="isTextCard"
          ref="textEditor"
          :card="card"
          :value="editorText"
          :data-card-id="card.id"
          @change="onTextCardChange"
        />
        <textarea
          v-else
          v-model="draft"
          class="editor"
          data-im="card-editor"
          :data-card-id="card.id"
          rows="4"
          :aria-label="'编辑' + kindLabel"
          @input="onDraftInput"
        ></textarea>
        <label v-if="card.kind === 'file' || card.kind === 'image'" class="field">
          <span>名称</span>
          <input v-model="metaName" type="text" data-im="card-meta-name" :data-card-id="card.id" @input="onDraftInput" />
        </label>
        <div v-if="attachKind" class="attach">
          <button class="btn" type="button" data-im="card-attach-file" :data-card-id="card.id" @click="pickAttachment">
            {{ attachmentLinked ? "重新关联已保存的附件" : "关联已保存的附件" }}
          </button>
          <span class="attach-state" data-im="card-attachment-state" :data-card-id="card.id">{{ attachmentText }}</span>
          <span
            v-if="attachStatus"
            class="attach-status"
            :class="'is-' + attachStatus.tone"
            data-im="card-attach-status"
            role="status"
          >{{ attachStatus.text }}</span>
        </div>
        <!-- 编辑态也能预览已关联的附件（接线 P2 的组件；无关联时它只给诚实文案） -->
        <CardAttachmentPreview
          v-if="attachKind"
          :kind="card.kind"
          :meta="card.meta"
          :card-id="card.id"
        />
        <label v-if="card.kind === 'code'" class="field">
          <span>语言</span>
          <input v-model="metaLanguage" type="text" data-im="card-meta-language" :data-card-id="card.id" @input="onDraftInput" />
        </label>
        <template v-if="card.kind === 'url'">
          <label class="field">
            <span>网址</span>
            <input v-model="metaHref" type="text" data-im="card-meta-href" :data-card-id="card.id" @input="onDraftInput" />
          </label>
          <label class="field">
            <span>标题</span>
            <input v-model="metaTitle" type="text" data-im="card-meta-title" :data-card-id="card.id" @input="onDraftInput" />
          </label>
        </template>
        <p class="draft-note">输入过程只保存草稿；点「完成编辑」才形成有效文字状态。</p>
        <!-- 草稿保存失败不能静默：状态与重试入口就近显示（C 的组件，A 的卡片接线） -->
        <CardDraftHint v-if="!store.draftConflictFor(card.id) && !boardContentConflict(card.id)" :card-id="card.id" />
        <div class="row">
          <button class="btn primary" type="button" @click="confirmEdit">完成编辑</button>
          <button class="btn" type="button" @click="cancelEdit">取消</button>
        </div>
      </template>
      <template v-else>
        <!--
          冲突未决时提示在**关闭态卡片**上也要可见可点（契约 §12.1）：
          刷新恢复出冲突后，用户不必先点「编辑」也能直接做出选择。
          卡片高度固定且 overflow 隐藏，提示不能追加在正文后面（会被裁掉）——
          确有冲突时直接**替换**正文行：两份内容都保留着（打开编辑或任一选择都能回到），这里先让用户做选择。
        -->
        <CardDraftHint v-if="store.draftConflictFor(card.id) || boardContentConflict(card.id)" :card-id="card.id" />
        <template v-else>
          <!-- 关闭态也能看到、能处理的状态（本机副本没删掉 / 清除未同步 / 草稿保存失败）：就地提示 + 重试 -->
          <CardDraftHint v-if="cardDraftNeedsAttention" :card-id="card.id" />
          <!--
            文字卡按块渲染（契约 §1.7）：标题 / 列表 / 引用 / 段落 / 代码 / 链接各自外观。
            内容模型是 meta.doc（权威）；旧数据没有 doc 时由 content 兜底成一段。
          -->
          <template v-if="isTextCard">
            <div v-if="shownBlocks.length" class="blocks" data-im="card-text-blocks">
              <template v-for="item in shownBlocks" :key="item.block.id + '-' + item.index">
                <h4
                  v-if="item.block.kind === 'heading'"
                  class="blk blk-heading"
                  data-im="card-block-heading"
                  :data-level="item.block.level ?? 1"
                  v-html="inlineHtml(blockText(item.block))"
                ></h4>
                <ul v-else-if="item.block.kind === 'list'" class="blk blk-list" data-im="card-block-list">
                  <li v-for="(line, lineIndex) in blockText(item.block).split('\n')" :key="lineIndex">
                    <span class="marker mono" aria-hidden="true">{{ item.block.ordered ? (lineIndex + 1) + '.' : '•' }}</span>
                    <span v-html="inlineHtml(line)"></span>
                  </li>
                </ul>
                <blockquote
                  v-else-if="item.block.kind === 'quote'"
                  class="blk blk-quote"
                  data-im="card-block-quote"
                  v-html="inlineHtml(blockText(item.block))"
                ></blockquote>
                <template v-else-if="item.block.kind === 'code'">
                  <figure class="blk blk-code" data-im="card-block-code">
                    <figcaption class="code-head">
                      <span class="mono" data-im="card-code-language">{{ item.block.attrs?.language || '代码' }}</span>
                      <button
                        class="code-copy"
                        type="button"
                        data-im="card-code-copy"
                        :data-copy-ok="copyState && copyState.id === item.block.id ? String(copyState.ok) : ''"
                        :title="copyTitle(item.block)"
                        @click.stop="copyCode(item.block)"
                      >
                        {{ copyState && copyState.id === item.block.id ? (copyState.ok ? '已复制' : '复制失败') : '复制' }}
                      </button>
                    </figcaption>
                    <pre class="mono code-body">{{ item.block.text }}</pre>
                    <p
                      v-if="copyState && copyState.id === item.block.id && !copyState.ok"
                      class="code-hint"
                      data-im="card-code-copy-hint"
                      role="status"
                    >
                      当前环境没有可用的剪贴板：请选中代码后按 Ctrl+C 手动复制，或再点一次重试。
                    </p>
                  </figure>
                </template>
                <div v-else-if="item.block.kind === 'link'" class="blk blk-link" data-im="card-block-link">
                  <button
                    class="link-open"
                    type="button"
                    data-im="card-link-open"
                    :data-href="linkHref(item.block)"
                    :data-openable="linkOpenable(item.block) ? '1' : '0'"
                    :title="linkOpenable(item.block) ? '在系统里打开这个地址' : '这个地址不可打开'"
                    @click.stop="openLink(item.block)"
                  >
                    {{ linkBlockTitle(item.block) }}
                  </button>
                  <span class="link-href mono">{{ linkHref(item.block) || '（还没有地址）' }}</span>
                  <span v-if="!linkOpenable(item.block)" class="link-warn" data-im="card-link-warning">
                    不可打开：地址不是 http / https / mailto
                  </span>
                  <button
                    class="link-edit"
                    type="button"
                    data-im="card-link-edit"
                    title="在编辑区里改这一行（光标会落到链接所在行）"
                    @click.stop="editBlockAt(item.line)"
                  >
                    编辑
                  </button>
                </div>
                <p
                  v-else
                  class="blk blk-paragraph"
                  data-im="card-block-paragraph"
                  v-html="inlineHtml(blockText(item.block))"
                ></p>
              </template>
            </div>
            <div v-else class="blocks-empty">
              <p class="content empty-text">（还没有内容，选中后用工具栏的「编辑」写下来）</p>
              <p v-if="(liveDoc.issues ?? []).length" class="doc-issue" data-im="card-doc-issue" role="status">
                {{ (liveDoc.issues ?? []).join('；') }}
              </p>
            </div>
            <p v-if="linkStatus" class="link-status" :class="'is-' + linkStatus.tone" data-im="card-link-status" role="status">
              {{ linkStatus.text }}
            </p>
          </template>
          <!--
            遗留分支（契约 §1.7）：历史 code / url 卡在迁移完成前**仍能显示**，但整体不交付
            （不进提交载荷、不计入未许可统计），界面必须写明它还没迁移。
          -->
          <p v-if="isLegacyCard" class="legacy-note" data-im="card-legacy-note" role="status">
            旧卡片，尚未迁移：{{ kindLabel }}卡将在下次加载板面时并入统一文字卡；现在不进入提交载荷。
          </p>
          <p v-if="card.kind === 'code'" class="content code mono">{{ card.content || "// 待补充代码" }}</p>
          <p v-else-if="card.kind === 'url'" class="content url">
            <a :href="href" target="_blank" rel="noreferrer" @pointerdown.stop>{{ linkTitle || href || "（还没有网址）" }}</a>
          </p>
          <p v-else class="content">
            <span v-if="card.content" class="desc">{{ card.content }}</span>
            <span v-else class="empty">{{ kindLabel }}：还没有补充说明</span>
          </p>
          <!-- 附件状态在关闭态也要看得见：有关联则给真实预览入口，没关联只给诚实文案（不许用文件名冒充） -->
          <CardAttachmentPreview
            v-if="attachKind"
            :kind="card.kind"
            :meta="card.meta"
            :card-id="card.id"
          />
        </template>
      </template>

      <!--
        卡片内展开的阅读区（契约 §3.5）：展开时这张卡本身变大，预览主体挂在**卡片内部**，
        因此不会盖住编辑区、勾选框与卡片操作。收起（含「收起全部预览」）时整块消失。
      -->
      <div
        v-if="previewExpanded"
        class="card-preview-host"
        data-im="card-preview-host"
        :data-card-id="card.id"
      >
        <CardInlinePreview
          :card="card"
          :active="previewExpanded"
          :on-close="togglePreview"
          @position="onPreviewPosition"
        />
      </div>
    </div>

    <!-- 元信息：状态文字 + 更新时间 + 组 + 书签。数量与时间退到次级视觉层。 -->
    <footer class="card-meta">
      <span class="status" role="status">{{ statusText }}</span>
      <span class="time mono">{{ updatedText }}</span>
    </footer>

    <!-- 连接点：选中后才出现；从这里拖到另一张卡片建立关系（方向与含义由用户写明） -->
    <template v-if="selected && !card.folded && !multi">
      <span
        v-for="side in ['top', 'right', 'bottom', 'left']"
        :key="side"
        class="connect-point"
        :class="side"
        data-im="connect-point"
        :data-card-id="card.id"
        :data-side="side"
        :title="'从连接点拖到另一张卡片建立关系'"
        @pointerdown="onConnectDown"
      ></span>
    </template>
  </article>

  <!--
    卡片局部工具栏：只在选中后浮出，未选中即隐藏；由所选里的最后一张卡片渲染（不叠成一摞）。
    它挂在板面容器里（不是卡片内部），位置由画布按视口换算，所以不会挡住卡片标题与正文。
    这里是**卡片自己的操作**；成组 / 移出组 / 解除组 / 序号 / 合并 / 删除所选在 SelectionMenu 里。
  -->
  <div
    v-if="isToolbarOwner"
    class="card-toolbar"
    :style="toolbarStyle"
    data-im="card-toolbar"
    :data-card-id="card.id"
    :data-multi="multi ? '1' : '0'"
    role="toolbar"
    :aria-label="multi ? '所选卡片的共同操作' : kindLabel + '操作'"
    @pointerdown.stop
  >
    <!--
      整卡许可（契约 §2.3）：一张文字卡只有一个许可，覆盖卡内文字、链接、代码。
      文案必须写明「提交后仍然有效、只有你手动取消才失效」—— 提交不再清勾选。
    -->
    <label
      v-if="checkable"
      class="check"
      title="许可：允许 QIO 查看这张卡（提交后仍然有效，只有你手动取消才失效）"
    >
      <input
        type="checkbox"
        :checked="card.checked"
        data-im="check"
        :data-card-id="card.id"
        @change="emit('toggle', card.id, 'checked')"
      />
      <span>许可：允许 QIO 查看这张卡（提交后仍然有效，只有你手动取消才失效）</span>
    </label>

    <template v-if="!multi">
      <button class="tb" type="button" data-im="card-edit" :data-card-id="card.id" @click="startEdit">编辑</button>
      <button class="tb" type="button" data-im="card-duplicate" :data-card-id="card.id" @click="emit('duplicate', card.id)">复制</button>
    </template>

    <button class="tb" type="button" data-im="card-fold" :data-card-id="card.id" @click="emit('toggle', card.id, 'folded')">
      {{ card.folded ? "展开" : "折叠" }}
    </button>
    <button class="tb" type="button" :data-card-id="card.id" @click="emit('toggle', card.id, 'hidden')">
      {{ card.hidden ? "取消隐藏" : "隐藏" }}
    </button>
    <button class="tb" type="button" :data-card-id="card.id" @click="emit('toggle', card.id, 'bookmarked')">
      {{ card.bookmarked ? "取消书签" : "书签" }}
    </button>

    <span class="divider" aria-hidden="true"></span>
    <SelectionMenu :open-up="menuOpensUp" />

    <button class="tb danger" type="button" data-im="delete-card" :data-card-id="card.id" @click="emit('remove', card.id)">
      删除
    </button>
    <span v-if="multi" class="tb-note">多选：只显示共同适用的操作</span>
  </div>
</template>

<style scoped>
.card {
  position: absolute;
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  padding: var(--sp-3);
  overflow: hidden;
  background: var(--bg-surface);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  box-shadow: var(--shadow-1);
  color: var(--text-primary);
  font-size: var(--fs-base);
  cursor: grab;
  /*
    拖动要跟手：拖动中的卡片**不参与过渡**（否则卡片会跟不上指针）。
    展开 / 收起 / 让位是低频转场，走设计令牌的非线性曲线（--ease-2 + --mo-2-move）；
    `prefers-reduced-motion` / 手动「减少动画」由 styles/base.css 的
    :root[data-motion="reduced"] 统一关掉（同一份偏好来自 utils/motion.ts）。
  */
}
.card:not(.dragging) {
  transition:
    left var(--mo-2-move) var(--ease-2),
    top var(--mo-2-move) var(--ease-2),
    width var(--mo-2-move) var(--ease-2),
    height var(--mo-2-move) var(--ease-2);
}
/* 悬停与选中必须能区分：悬停只提边界，选中加品牌色边界 + 一层很轻的外圈 + 底色 */
.card:hover { border-color: var(--border-strong); }
.card.selected {
  border-color: var(--accent);
  background: var(--bg-accent-subtle);
  box-shadow: var(--shadow-1), 0 0 0 2px var(--accent-soft);
}
.card.highlight { outline: 2px dashed var(--warning); outline-offset: 2px; }
.card.dragging { cursor: grabbing; opacity: 0.92; border-style: dashed; border-color: var(--accent); }
.card.hidden { border-style: dashed; }
.card.reply { border-left: 3px solid var(--link); }

/* 第一层：材料类型 + 标题 */
.card-head { display: flex; align-items: center; gap: var(--sp-2); min-width: 0; }
.kind {
  flex: none;
  font-size: var(--fs-tech, var(--fs-xs));
  letter-spacing: 0.02em;
  color: var(--text-muted);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-pill);
  padding: 0 var(--sp-2);
}
.title {
  min-width: 0;
  font-family: var(--serif);
  font-size: var(--fs-md);
  line-height: var(--lh-tight);
  color: var(--text-strong);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.chip-btn {
  flex: none;
  margin-left: auto;
  font: inherit;
  font-size: var(--fs-xs);
  color: var(--text-secondary);
  background: var(--bg-elevated);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-xs);
  padding: 0 var(--sp-2);
  cursor: pointer;
}
.chip-btn:hover { color: var(--text-strong); border-color: var(--border-strong); }
.chip-btn:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 1px; }

/* 第二层：正文。长名称 / 网址 / 代码要么换行要么自己滚动，不许横向溢出卡片 */
.card-body { flex: 1; min-height: 0; overflow: auto; }
.content {
  margin: 0;
  font-size: var(--fs-base);
  line-height: var(--lh-base);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  word-break: break-word;
}
.content.code {
  font-family: var(--mono);
  font-size: var(--fs-sm);
  line-height: var(--lh-tight);
  white-space: pre;
  overflow-wrap: normal;
  word-break: normal;
}

/* ---- 统一文字卡的块渲染（契约 §1.7）：每类块有自己的外观，但仍是一张卡的正文 ---- */
.blocks { display: flex; flex-direction: column; gap: var(--sp-2); }
.blk { margin: 0; font-size: var(--fs-base); line-height: var(--lh-base); overflow-wrap: anywhere; }
.blk-heading { font-family: var(--serif); font-weight: 600; color: var(--text-strong); line-height: var(--lh-tight); }
.blk-heading[data-level="1"] { font-size: var(--fs-lg); }
.blk-heading[data-level="2"] { font-size: var(--fs-md); }
.blk-heading[data-level="3"] { font-size: var(--fs-base); }
.blk-list { padding-left: var(--sp-4); }
.blk-list li { display: flex; gap: var(--sp-2); align-items: baseline; }
.blk-list .marker { flex: none; color: var(--text-muted); }
.blk-quote {
  padding-left: var(--sp-3);
  border-left: 3px solid var(--border-strong);
  color: var(--text-secondary);
}
.blk-paragraph { color: var(--text-primary); }
.blk :deep(code) {
  font-family: var(--mono);
  font-size: var(--fs-sm);
  background: var(--bg-inset);
  border-radius: var(--r-xs);
  padding: 0 2px;
}
/* 代码块：语言 + 复制按钮在最上面一行；正文自己滚动，绝不横向撑破卡片 */
.blk-code {
  margin: 0;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-sm);
  background: var(--bg-inset);
  overflow: hidden;
}
.code-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-2);
  padding: 2px var(--sp-2);
  border-bottom: 1px solid var(--border-subtle);
  font-size: var(--fs-xs);
  color: var(--text-muted);
}
.code-copy {
  font: inherit;
  font-size: var(--fs-xs);
  color: var(--text-secondary);
  background: var(--bg-surface);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-xs);
  padding: 0 var(--sp-2);
  cursor: pointer;
}
.code-copy:hover { color: var(--text-strong); border-color: var(--border-strong); }
.code-copy:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 1px; }
.code-body {
  margin: 0;
  padding: var(--sp-2);
  font-size: var(--fs-sm);
  line-height: var(--lh-tight);
  white-space: pre;
  overflow: auto;
}
.code-hint { margin: 0; padding: var(--sp-1) var(--sp-2); font-size: var(--fs-xs); color: var(--text-secondary); }
/* 链接块：标题（可点开）与地址（等宽，永远看得见） */
.blk-link { display: flex; flex-wrap: wrap; align-items: baseline; gap: var(--sp-2); }
.link-open {
  font: inherit;
  font-size: var(--fs-base);
  color: var(--link);
  background: none;
  border: none;
  padding: 0;
  text-align: left;
  cursor: pointer;
  overflow-wrap: anywhere;
}
.link-open:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 1px; }
.link-open[data-openable="0"] { color: var(--text-secondary); text-decoration: line-through; cursor: default; }
.link-href { font-size: var(--fs-xs); color: var(--text-faint); overflow-wrap: anywhere; }
.link-warn { flex: 1 1 100%; font-size: var(--fs-xs); color: var(--danger, var(--text-strong)); }
.link-edit {
  font: inherit;
  font-size: var(--fs-xs);
  color: var(--text-secondary);
  background: var(--bg-surface);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-xs);
  padding: 0 var(--sp-2);
  cursor: pointer;
}
.link-edit:hover { color: var(--text-strong); border-color: var(--border-strong); }
.link-status { margin: var(--sp-2) 0 0; font-size: var(--fs-xs); line-height: var(--lh-tight); }
.link-status.is-ok { color: var(--text-secondary); }
.link-status.is-error { color: var(--danger, var(--text-strong)); }
.blocks-empty { display: flex; flex-direction: column; gap: var(--sp-1); }
.empty-text { color: var(--text-muted); }
.doc-issue { margin: 0; font-size: var(--fs-xs); color: var(--text-secondary); line-height: var(--lh-tight); }
/* 遗留 code / url 卡：仍能显示，但必须写明还没迁移、不进提交载荷 */
.legacy-note {
  margin: 0 0 var(--sp-2);
  padding: 2px var(--sp-2);
  border: 1px dashed var(--warning);
  border-radius: var(--r-xs);
  font-size: var(--fs-xs);
  color: var(--text-secondary);
  line-height: var(--lh-tight);
}
.content.url a { color: var(--link); overflow-wrap: anywhere; }
.desc { display: block; white-space: pre-wrap; overflow-wrap: anywhere; }
.empty { color: var(--text-muted); }
.editor {
  width: 100%;
  box-sizing: border-box;
  font: inherit;
  font-size: var(--fs-base);
  color: var(--text-primary);
  background: var(--bg-inset);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-xs);
  padding: var(--sp-2);
  resize: vertical;
}
.field { display: flex; align-items: center; gap: var(--sp-2); margin-top: var(--sp-2); font-size: var(--fs-sm); }
.field input {
  flex: 1;
  min-width: 0;
  font: inherit;
  font-size: var(--fs-sm);
  color: var(--text-primary);
  background: var(--bg-inset);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-xs);
  padding: 2px var(--sp-2);
}
.draft-note { margin: var(--sp-2) 0 0; font-size: var(--fs-xs); color: var(--text-muted); line-height: var(--lh-tight); }
.row { display: flex; gap: var(--sp-2); margin-top: var(--sp-2); }
.btn {
  font: inherit;
  font-size: var(--fs-sm);
  color: var(--text-primary);
  background: var(--bg-elevated);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-xs);
  padding: 2px var(--sp-3);
  cursor: pointer;
}
.btn:hover { border-color: var(--border-strong); }
.btn:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 1px; }
.btn.primary { color: var(--on-accent); background: var(--accent); border-color: var(--accent); }

/* 附件关联：编辑态的按钮 + 状态；关闭态的「有没有关联 / 能不能预览」由 CardAttachmentPreview 自带样式 */
.attach {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--sp-2);
  margin-top: var(--sp-2);
  font-size: var(--fs-xs);
}
.attach-state { color: var(--text-muted); line-height: var(--lh-tight); }
.attach-status { flex: 1 1 100%; line-height: var(--lh-tight); }
.attach-status.is-busy { color: var(--text-muted); }
.attach-status.is-ok { color: var(--text-secondary); }
.attach-status.is-error { color: var(--danger, var(--text-strong)); }

/* 第四层：元信息（状态文字 + 时间）。比正文小一档，但仍是可读文本，不用装饰级颜色。 */
.card-meta {
  display: flex;
  align-items: baseline;
  gap: var(--sp-2);
  min-width: 0;
  font-size: var(--fs-xs);
  color: var(--text-muted);
}
.status { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.time { flex: none; color: var(--text-faint); }

/*
  卡片内展开的阅读区：占卡片里正文之后的剩余空间，自己滚动。
  它是卡片内部的一块排版区域，不是浮层：不盖编辑区、不盖连接点、不盖局部工具栏。
*/
.card-preview-host {
  margin-top: var(--sp-3);
  padding-top: var(--sp-3);
  border-top: 1px dashed var(--border-subtle);
  min-height: 140px;
  overflow: auto;
}
.preview-placeholder {
  margin: 0;
  padding: var(--sp-3);
  border: 1px dashed var(--border-subtle);
  border-radius: var(--r-sm);
  color: var(--text-muted);
  font-size: var(--fs-xs);
  line-height: var(--lh-base);
}

/* 连接点：四边中点，拖出去建关系 */
.connect-point {
  position: absolute;
  width: 12px;
  height: 12px;
  border-radius: var(--r-pill);
  background: var(--bg-elevated);
  border: 1.5px solid var(--accent);
  cursor: crosshair;
  z-index: 2;
}
/* 卡片是 overflow: hidden，连接点必须留在卡片内侧边缘，否则会被裁掉、真实鼠标点不到 */
.connect-point.top { left: 50%; top: 0; transform: translateX(-50%); }
.connect-point.right { right: 0; top: 50%; transform: translateY(-50%); }
.connect-point.bottom { left: 50%; bottom: 0; transform: translateX(-50%); }
.connect-point.left { left: 0; top: 50%; transform: translateY(-50%); }

/*
  局部工具栏：浮在板面上，不随卡片缩放，位置由父组件换算。
  按钮字号用 --fs-sm：编辑 / 删除这些是主要操作，不缩成元信息字号。
*/
.card-toolbar {
  position: absolute;
  z-index: 60;
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--sp-1);
  max-width: min(560px, calc(100vw - var(--sp-4)));
  padding: var(--sp-1) var(--sp-2);
  background: var(--bg-elevated);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-sm);
  box-shadow: var(--elev-floating, var(--shadow-2));
  font-size: var(--fs-sm);
}
.check { display: inline-flex; align-items: center; gap: var(--sp-1); color: var(--text-secondary); white-space: nowrap; }
.check input { accent-color: var(--accent); }
.divider { width: 1px; height: 16px; background: var(--border-subtle); flex: none; }
.tb {
  font: inherit;
  font-size: var(--fs-sm);
  color: var(--text-secondary);
  background: var(--bg-surface);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-xs);
  padding: 1px var(--sp-2);
  cursor: pointer;
  white-space: nowrap;
}
.tb:hover { color: var(--text-strong); border-color: var(--border-strong); }
.tb:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 1px; }
.tb.danger { color: var(--danger); }
.tb-note { color: var(--text-muted); white-space: nowrap; }
</style>
