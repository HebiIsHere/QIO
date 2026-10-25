<!--
  添加菜单（子智能体 A 负责）：**文字卡 / 文件**两类材料的统一入口（图片是文件卡的附件，不是独立入口）。

  契约：docs/interactive-mode-contract.md §8.1 / §8.4.1；
       docs/superpowers/plans/2026-10-12-unified-cards-media-preview-contract.md §1.7：
  - 「代码」「网址」不再是入口：文字、链接、代码写在**同一张文字卡**里；
  - 添加菜单是这几类的统一入口，文字卡片不再单独常驻在工具栏上；
  - 会改板面状态的操作由触发方自己调 board.ts 纯函数 + store.commit（不做命令总线）。

  归属决定（题面二选一，已定）：**本组件直接调 store.commit(addCard(...), "添加…")**，
  不把选择再 emit 给工具栏转一次手 —— 添加只依赖 board.ts 与 store，工具栏只负责把它放进底部栏。

  收起时用 v-if 而不是 v-show：隐藏元素留在 DOM 里会让「按钩子驱动」的验收出现假通过
  （lead 在 task-6 里明确要求：收起后这些按钮不该存在）。

  附件提示的生命周期（0.2.5 回归修复 §二，原语见 composables/useTransientNotice.ts）：
  - **成功约 3 秒后自动移除**：移除的只是**提示**；卡片、meta 里的附件关联、已保存副本、
    预览能力与卡片内状态全部保留（回归根因就是成功态挂在 v-if 上永不清除）；
  - 等待选择 / 上传 / 处理期间是**真实进度**，不会被成功计时器提前藏掉；
  - **失败不自动消失**：简短原因 + 关闭入口 + 「重新选择文件」（对同一张卡片重走关联流程）；
  - 每次开始新操作都 begin()：旧提示、旧计时器与旧 token 一起作废，迟到结果写不进来；
  - 卸载：原语自己清计时器并作废 token，这里再用 alive 挡住「卸载后还去写板面」的迟到回调；
  - 成功文案只陈述**附件本身已保存**与「板面关联还只是本地改动」，不声称服务端已保存
    （落盘 / 提交由既有机制负责，失败也由既有机制反馈，这里不另造一套）。
-->
<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useInteractiveStore } from "../../stores/interactive";
import { addCard, ADDABLE_CARD_KINDS, CARD_KIND_LABELS, defaultCardSeed, updateCard } from "../../interactive/board";
import { cardAttachmentLinked, cardAttachmentName } from "../../interactive/materials";
import { NEW_CARD_SIZE, planNewCardSlot, rectsOverlap } from "../../interactive/cardLayout";
import { readBoardArea } from "../../interactive/viewport";
import {
  attachmentSourceNote,
  isDesktopShell,
  onPathDrop,
  pickBrowserFile,
  pickLocalSource,
  registerSourceAttachment,
  stateText,
  uploadAttachment,
  waitUntilSettled,
  type AttachmentRef,
} from "../../services/attachments";
import { mergeAttachmentIntoMeta } from "../../interactive/fileCardRef";
import { useTransientNotice, type NoticeRetry } from "../../composables/useTransientNotice";
import type { CardKind } from "../../interactive/types";

/**
 * 可添加的卡片种类：**只由 board.ts 的 ADDABLE_CARD_KINDS 决定**（契约 §1.7）。
 * 菜单里不再单列 code / url —— 历史卡片仍然能显示（BoardCard 的兜底分支），只是不能新建。
 */
type AddableKind = CardKind;
/** 需要关联真实附件的材料（2026-10-15 图片展示简化：只有文件卡，图片是它的附件）。 */
type AttachKind = "file";

interface AddItem {
  kind: AddableKind;
  label: string;
  hint: string;
}

const store = useInteractiveStore();
const open = ref(false);
const root = ref<HTMLElement | null>(null);
const trigger = ref<HTMLButtonElement | null>(null);
const popup = ref<HTMLElement | null>(null);

/**
 * 入口与说明。说明里必须写清权限边界（不能只靠颜色或图标表达）：
 * 文字卡默认**提交给 QIO**（2026-10-14 第十个问题：新建即勾选，用户可取消；
 * 取消后 QIO 看不到卡内的文字、链接与代码，提交也不会带上它）；
 * 材料（文件卡，图片是它的附件）默认在本次互动材料范围内。
 */
const HINTS: Record<string, string> = {
  text: "文字、链接、代码写在同一张卡片里；默认提交给 QIO（可取消）",
  file: "提供的是源文件引用：桌面选择/拖放不复制内容；「作为互动材料」只控制是否纳入互动上下文",
};

const ITEMS: AddItem[] = ADDABLE_CARD_KINDS.map((kind) => ({
  kind,
  label: CARD_KIND_LABELS[kind],
  hint: HINTS[kind] ?? "材料默认在本次互动材料范围内，不需要许可",
}));

/**
 * 附件提示的生命周期全部交给同一份原语（Lead 冻结）：
 * busy 一直显示到被替换，ok 约 3 秒自动移除，error 不自动消失且可重试 / 关闭。
 * 不在这里另写 setTimeout，也不用一个裸 status 长期挂在界面上（0.2.5 的回归根因）。
 */
const { notice, begin, progress, succeed, fail, dismiss } = useTransientNotice();

/** 卸载后到达的迟到结果连板面都不该再改：原语负责提示，这个标志负责副作用。 */
let alive = true;

function toggle(): void {
  open.value = !open.value;
}

/** 收起菜单；returnFocus=true 时把焦点还给「添加」按钮（键盘用户不会掉焦点）。 */
function close(returnFocus = false): void {
  if (!open.value) return;
  open.value = false;
  if (returnFocus) void nextTick(() => trigger.value?.focus());
}

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function kindLabel(kind: AddableKind): string {
  return CARD_KIND_LABELS[kind] ?? "材料";
}

function cardOf(cardId: string) {
  return store.board?.cards.find((item) => item.id === cardId);
}

/** 失败 / 取消都能一键重来：对**同一张卡片**再走一遍 pickBrowserFile → 上传 → 关联。 */
function retryOf(kind: AttachKind, cardId: string): NoticeRetry {
  return { label: "重新选择文件", run: () => void attachRealFile(kind, cardId) };
}

/**
 * 用户取消选择的文案**必须看卡片当前的事实**（materials.ts 的 cardAttachmentLinked）：
 * 卡片已经有附件时还说「还没有关联已保存的附件」是错的 —— 原有附件一直都在，只是没被更换。
 */
function cancelText(kind: AttachKind, cardId: string): string {
  const card = cardOf(cardId);
  const linkedKind = card?.kind ?? kind;
  const label = kindLabel(linkedKind);
  if (card && cardAttachmentLinked(card.meta, linkedKind)) {
    const previous = cardAttachmentName(card.meta, linkedKind);
    return (
      "没有选择文件：这张" + label + "卡片仍然关联着原有附件" +
      (previous ? "「" + previous + "」" : "") +
      "，本次没有更换"
    );
  }
  return "没有选择文件：这张" + label + "卡片还没有关联已保存的附件，QIO 读不到它的正文";
}

/**
 * 文件卡片必须关联**真实已保存的附件**（meta.file.attachmentId），
 * 否则 QIO 只能看到文件名、读不到正文（后端会如实标 unreadable）。
 *
 * 状态一律走 useTransientNotice：每次调用 begin() 开一代新操作，
 * 之后所有写入都带这次操作的 token —— 迟到的结果不会覆盖 / 清掉别人的提示。
 */
async function attachRealFile(
  kind: AttachKind,
  cardId: string,
  preset?:
    | { path: string; sourceKind: "desktop_path" | "dropped_path" }
    | { file: File },
): Promise<void> {
  const token = begin();
  progress(token, preset ? "正在登记拖放的源文件…" : "正在等待选择文件…");
  let created: AttachmentRef | null = null;
  let fileName = "";
  let fallbackNote = "";
  try {
    if (preset && "path" in preset) {
      // 有真实路径（桌面拖放）：**只登记源引用**，不复制、不移动、不走字节上传
      fileName = preset.path.split(/[\\/]/).pop() || "未命名";
      progress(token, "正在登记拖放的源文件「" + fileName + "」…");
      created = await registerSourceAttachment(preset.path, preset.sourceKind);
    } else if (preset && "file" in preset) {
      // 浏览器拖放：只有字节 → 首次落盘为工作区材料源文件（来源如实标注）
      fileName = preset.file.name || "未命名";
      progress(token, "正在保存附件「" + fileName + "」…（首次落盘为工作区材料源文件）");
      created = await uploadAttachment(preset.file, { sourceKind: "browser_upload" });
    } else if (isDesktopShell()) {
      try {
        const picked = await pickLocalSource();
        if (!picked) {
          if (!alive) return;
          fail(token, cancelText(kind, cardId), retryOf(kind, cardId));
          return;
        }
        fileName = picked.name || picked.path.split(/[\\/]/).pop() || "未命名";
        progress(token, "正在登记源文件「" + fileName + "」…");
        created = await registerSourceAttachment(picked.path, "desktop_path");
      } catch (error) {
        fallbackNote = "原生选择器不可用（" + messageOf(error) + "），改用文件上传：";
      }
    }
    if (!created) {
      let file: File | null;
      try {
        file = await pickBrowserFile();
      } catch (error) {
        fail(token, "选择文件失败：" + messageOf(error), retryOf(kind, cardId));
        return;
      }
      if (!alive) return; // 组件已卸载：迟到结果不写提示，也不写板面
      if (!file) {
        // 用户取消：如实说明（不假装读到了内容），文案按卡片当前是否已有附件区分
        fail(token, (fallbackNote ? fallbackNote + "\n" : "") + cancelText(kind, cardId), retryOf(kind, cardId));
        return;
      }
      fileName = file.name || "未命名";
      progress(token, fallbackNote + "正在保存附件「" + fileName + "」…（首次落盘为工作区材料源文件）");
      created = await uploadAttachment(file, { sourceKind: "browser_upload" });
    }
    if (!alive) return;
    // 登记/上传完成还要等附件真的 ready（服务端处理中）：这一段同样是真实进度
    progress(token, "正在处理附件「" + fileName + "」…");
    const settled = await waitUntilSettled(created);
    if (!alive) return;
    if (settled.state !== "ready") {
      throw new Error(
        "附件「" +
          settled.name +
          "」当前不可用：" +
          stateText(settled) +
          (settled.error ? "（" + settled.error + "）" : ""),
      );
    }
    const current = store.board;
    const card = current?.cards.find((item) => item.id === cardId);
    if (!current || !card) {
      fail(token, "卡片已经不在了，附件没有关联上（附件本身已经保存）");
      return;
    }
    const meta = mergeAttachmentIntoMeta(card.meta, settled, kind);
    store.commit(updateCard(current, cardId, { meta }), "关联附件：" + settled.name);
    /**
     * 成功只陈述已确认的事实：附件本身已登记/保存，来源如实标注（引用本地源文件 /
     * 已保存为工作区材料）；板面关联还只是**本地**改动，提交后 QIO 才可见。
     */
    succeed(
      token,
      "已关联附件「" +
        settled.name +
        "」：" +
        attachmentSourceNote(settled) +
        "；板面关联是本地改动，提交后 QIO 才可见",
    );
  } catch (error) {
    fail(token, "附件保存失败：" + messageOf(error), retryOf(kind, cardId));
  }
}

/** 失败提示上的「重新选择文件」：重跑的是**重新走一遍那次关联**，不是只重发一次请求。 */
function runRetry(): void {
  notice.value?.retry?.run();
}

/**
 * 拖放进来、等待随新文件卡一起登记的**真实路径**。
 * 只在这一个「添加」动作内有效：add() 消费后立即清空，不泄漏到下一次点击。
 */
let pendingDroppedPath: string | null = null;
/** 浏览器拖放进来、等待随新文件卡一起上传的文件对象（同样是消费即清）。 */
let pendingDroppedFile: File | null = null;

function add(kind: AddableKind): void {
  const current = store.board;
  if (!current) return;
  const droppedPath = kind === "file" ? pendingDroppedPath : null;
  const droppedFile = kind === "file" ? pendingDroppedFile : null;
  if (kind === "file") {
    pendingDroppedPath = null;
    pendingDroppedFile = null;
  }
  // 默认内容统一由 board.ts 提供：同一个「添加文件」不能有两种默认值
  const seed = defaultCardSeed(kind);
  const before = new Set(current.cards.map((card) => card.id));
  /**
   * 新卡落点（任务书 §7.7）：优先放在**当前可用区域的空白处**。
   * 可用区域由画布发布（已扣平移后的可视起点、自绘标题栏、底部工具栏、对话窗口、审批浮层），
   * 落点再避开已有卡片 —— 平移 / 缩放之后也不会把新卡放到视口之外。
   *
   * 为什么不用 board.ts 的「固定四列 + 数量取模」兜底：那套排法在平移 / 缩放后会把
   * 新卡放在用户看不到的地方，并反复落在同一处已有的卡上。这里的兜底是「读不到可用区域
   * （画布未挂载 / 还没算过）就不传坐标」，此时行为与改动前逐字节一致。
   */
  const area = readBoardArea();
  const slot = area
    ? planNewCardSlot({
        size: NEW_CARD_SIZE,
        area,
        cards: current.cards.filter((card) => !card.deleted),
      })
    : null;
  /**
   * 落点**仍然**压着已有卡片：只有「扩大搜索范围之后都放不下」才会发生
   * （planNewCardSlot 会先向右 / 向下延伸一整张卡再搜）。
   * 这里只负责如实说明，绝不假装放得下 —— 不静默、也不改板面状态。
   */
  const crowded = Boolean(
    slot && current.cards.some((card) => !card.deleted && rectsOverlap(slot, card)),
  );
  /**
   * 新建文字卡默认**提交给 QIO**（2026-10-14 第十个问题）：整卡一个许可，
   * 覆盖卡内文字、链接与代码；用户可以在卡片工具栏里取消勾选，取消后整体不交付。
   * 材料卡不需要这个勾选（默认在本次互动材料范围内），沿用 board.ts 的默认值。
   */
  store.commit(
    addCard(current, {
      kind,
      content: seed.content,
      meta: seed.meta,
      ...(kind === "text" ? { checked: true } : {}),
      ...(slot ? { x: slot.x, y: slot.y, w: slot.w, h: slot.h } : {}),
    }),
    "添加" + CARD_KIND_LABELS[kind],
  );
  close(true);
  const added = store.board?.cards.find((card) => !before.has(card.id));
  // 落点退化时如实说明（正常情况下 planNewCardSlot 已经找到零重叠落点，不会有这条提示）
  if (crowded) {
    succeed(begin(), "板面已经没有零重叠的空位了：新卡先放在重叠最少的位置，请先整理一下板面。");
  }
  // 文件卡：拖放有真实路径 → 登记源引用；否则让用户选择（桌面原生路径 / 浏览器字节上传）
  if (kind === "file" && added) {
    void attachRealFile(
      kind,
      added.id,
      droppedPath
        ? { path: droppedPath, sourceKind: "dropped_path" }
        : droppedFile
          ? { file: droppedFile }
          : undefined,
    );
  }
}

/** 点菜单外面收起。 */
function onDocumentPointerDown(event: PointerEvent): void {
  const target = event.target as Node | null;
  if (target && root.value && !root.value.contains(target)) close(false);
}

/** 菜单开着时 Esc 只收起菜单，不让板面把它当成「取消拖动 / 清空选择」。 */
function onDocumentKeydown(event: KeyboardEvent): void {
  if (event.key !== "Escape") return;
  event.stopPropagation();
  close(true);
}

watch(open, async (value) => {
  if (value) {
    document.addEventListener("pointerdown", onDocumentPointerDown, true);
    document.addEventListener("keydown", onDocumentKeydown);
    await nextTick();
    popup.value?.querySelector("button")?.focus();
  } else {
    document.removeEventListener("pointerdown", onDocumentPointerDown, true);
    document.removeEventListener("keydown", onDocumentKeydown);
  }
});

/**
 * 桌面拖放：Tauri 核心事件给的是**真实路径**（不是字节）—— 每份材料新建一张文件卡，
 * 并用 registerSourceAttachment 登记源引用（source_kind=dropped_path，不复制内容）。
 */
async function attachDroppedPaths(paths: string[]): Promise<void> {
  if (!paths.length) return;
  for (const path of paths.slice(0, 5)) {
    if (!alive) return;
    pendingDroppedPath = path;
    add("file");
    await nextTick();
  }
}

/** 浏览器拖放：只有字节（没有原位置）→ 每份文件新建一张文件卡并首次落盘为工作区材料。 */
async function onBrowserDrop(event: DragEvent): Promise<void> {
  const files = Array.from(event.dataTransfer?.files ?? []);
  if (!files.length) return;
  for (const file of files.slice(0, 5)) {
    if (!alive) return;
    pendingDroppedFile = file;
    add("file");
    await nextTick();
  }
}

let stopPathDrop: (() => void) | null = null;

onMounted(async () => {
  // 只有桌面壳会给出真实路径；浏览器由模板里的 drop 事件兜底（字节 → 首次落盘）
  stopPathDrop = await onPathDrop((paths) => {
    void attachDroppedPaths(paths);
  });
});

onBeforeUnmount(() => {
  alive = false;
  stopPathDrop?.();
  stopPathDrop = null;
  document.removeEventListener("pointerdown", onDocumentPointerDown, true);
  document.removeEventListener("keydown", onDocumentKeydown);
});
</script>

<template>
  <div ref="root" class="add" @dragover.prevent @drop.prevent="onBrowserDrop">
    <button
      ref="trigger"
      class="tb-btn"
      type="button"
      data-im="add-menu"
      aria-haspopup="menu"
      :aria-expanded="open"
      title="添加文字卡或材料：文字卡（文字 / 链接 / 代码） / 文件（图片是文件附件）"
      @click="toggle"
    >
      <span class="plus" aria-hidden="true">＋</span>添加
    </button>

    <!--
      附件状态与菜单共处**同一个悬浮列**（列在「添加」按钮上方，工具栏贴底、向下没有空间）：
      提示在上、菜单在下（菜单仍紧贴按钮，位置不因提示变化），两者各占一行、永不互相遮挡。
      这一层不吃指针事件，只有里面的菜单项与「重新选择 / 关闭」各自打开 ——
      所以无论是长文件名还是失败提示，都不会挡住「添加」按钮或工具栏上的其它操作。
    -->
    <div class="add-float" data-im="add-float">
      <!-- 附件上传 / 成功 / 失败状态：渲染在菜单外面，菜单收起后仍然可见（不静默） -->
      <p
        v-if="notice"
        class="add-status"
        :class="'is-' + notice.tone"
        data-im="add-material-status"
        :title="notice.text"
      >
        <span class="status-text" data-im="add-material-status-text" role="status" aria-live="polite">{{ notice.text }}</span>
        <button
          v-if="notice.retry"
          type="button"
          class="status-action"
          data-im="add-material-retry"
          @click="runRetry()"
        >{{ notice.retry.label }}</button>
        <button
          v-if="notice.tone !== 'ok'"
          type="button"
          class="status-action status-close"
          data-im="add-material-dismiss"
          aria-label="关闭提示"
          @click="dismiss()"
        >关闭</button>
      </p>

      <div
        v-if="open"
        ref="popup"
        class="add-pop"
        data-qio-fit
        data-im="add-menu-list"
        role="menu"
        aria-label="添加到板面"
      >
        <p class="pop-title">添加到板面</p>
        <button
          v-for="item in ITEMS"
          :key="item.kind"
          class="pop-item"
          type="button"
          role="menuitem"
          :data-im="'add-' + item.kind"
          :title="item.hint"
          @click="add(item.kind)"
        >
          <span class="item-label">{{ item.label }}</span>
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.add { position: relative; display: inline-flex; }
/* 与 BoardToolbar 的 .tb-btn 同一套兜底样式：scoped 样式不能跨组件，
   所以这里必须自带一份，保证菜单没被父级包住时也是同一个样子。 */
.tb-btn {
  font: inherit;
  font-size: var(--fs-sm);
  line-height: 1.7;
  color: var(--text-secondary);
  background: var(--bg-surface);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-lg);
  padding: 0 var(--sp-2);
  cursor: pointer;
  white-space: nowrap;
}
.tb-btn:hover { color: var(--text-strong); border-color: var(--border-strong); }
.tb-btn:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 1px; }
.tb-btn[aria-expanded="true"] { color: var(--text-strong); border-color: var(--border-strong); background: var(--bg-inset); }
.plus { color: var(--link); }

/*
  悬浮列：菜单与附件提示从「添加」按钮上方展开。
  实测（800×600）：五条「标题 + 说明」竖着排是 320×369，正好压住卡片工具栏与卡片本身。
  改成**一行五个入口 + 一行说明**：约 62px 高，说明文字改为读屏可见（sr-only）＋ 悬停提示。
  0.2.5 起这一列还负责「提示不遮挡菜单」：两者在同一列里各占一行（提示在上、菜单紧贴按钮）。
  整层 pointer-events: none：提示是纯信息，绝不挡住「添加」按钮或其它主要操作。
*/
.add-float {
  position: absolute;
  left: 0;
  bottom: calc(100% + var(--sp-2));
  z-index: 3;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: var(--sp-2);
  pointer-events: none;
}
.add-pop {
  /* 菜单自己要能点：在这一层内部单独打开指针事件 */
  pointer-events: auto;
  width: max-content;
  max-width: min(520px, calc(100vw - var(--sp-6)));
  display: flex;
  flex-direction: row;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--sp-1);
  padding: var(--sp-2);
  background: var(--bg-elevated);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-lg);
  box-shadow: var(--elev-floating, var(--shadow-2));
}
.pop-title { margin: 0 var(--sp-1) 0 0; font-size: var(--fs-xs); color: var(--text-faint); }
.pop-item {
  display: inline-flex;
  align-items: center;
  font: inherit;
  background: var(--bg-surface);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-lg);
  padding: 2px var(--sp-2);
  cursor: pointer;
}
.pop-item:hover { border-color: var(--border-strong); }
.pop-item:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 1px; }
.item-label { font-size: var(--fs-sm); color: var(--text-strong); }
/* 视觉上收起、语义上保留：读屏与按文字断言都还能读到这条说明 */
.item-hint {
  position: absolute;
  width: 1px;
  height: 1px;
  margin: -1px;
  padding: 0;
  overflow: hidden;
  clip-path: inset(50%);
  white-space: nowrap;
}
.pop-note {
  flex: 1 1 100%;
  margin: var(--sp-1) 0 0;
  padding-top: var(--sp-1);
  border-top: 1px solid var(--border-subtle);
  font-size: var(--fs-xs);
  color: var(--text-muted);
}
/* 状态条：与菜单同一列的上一行（菜单收起后仍看得到上传结果 / 失败原因） */
.add-status {
  display: flex;
  flex-direction: row;
  flex-wrap: wrap;
  align-items: flex-start;
  gap: var(--sp-2);
  margin: 0;
  width: max-content;
  max-width: min(360px, calc(100vw - var(--sp-6)));
  padding: var(--sp-2);
  background: var(--bg-elevated);
  border: 1px solid var(--border-strong);
  border-left: 3px solid var(--border-strong);
  border-radius: var(--r-lg);
  box-shadow: var(--elev-floating, var(--shadow-2));
  font-size: var(--fs-xs);
  line-height: var(--lh-normal, 1.5);
  color: var(--text-secondary);
  /* 长文件名：在提示内部换行断词，不把提示撑成一条盖住别处的横幅；完整名称看 title */
  overflow-wrap: anywhere;
  word-break: break-word;
  white-space: normal;
}
/* 文本是唯一可换行的部分：按钮保持整块，不被名字挤断 */
.status-text { min-width: 0; overflow-wrap: anywhere; word-break: break-word; }
.status-action {
  /* 提示层整体不吃指针事件，可操作按钮自己打开 */
  pointer-events: auto;
  flex: 0 0 auto;
  font: inherit;
  font-size: var(--fs-xs);
  color: var(--link);
  background: var(--bg-surface);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-lg);
  padding: 0 var(--sp-2);
  cursor: pointer;
  white-space: nowrap;
}
.status-action:hover { border-color: var(--border-strong); color: var(--text-strong); }
.status-action:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 1px; }
.status-close { color: var(--text-muted); }
.add-status.is-busy { border-left-color: var(--link); }
.add-status.is-ok { border-left-color: var(--success, var(--link)); }
.add-status.is-error { border-left-color: var(--danger); color: var(--text-strong); }
</style>
