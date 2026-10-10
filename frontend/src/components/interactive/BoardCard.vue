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
import { computed, ref, watch } from "vue";
import { useInteractiveStore } from "../../stores/interactive";
import {
  cardDraftKey,
  cardDraftMetaFieldsForKind,
  readCardDraftInputForBoard,
  restoreCardDraftInput,
} from "../../interactive/drafts";
import type { CardDraftInput, CardDraftMetaInput } from "../../interactive/drafts";
import { CARD_KIND_LABELS, CHECKABLE_KINDS } from "../../interactive/board";
import type { BoardCard, BoardGroup } from "../../interactive/types";
import SelectionMenu from "./SelectionMenu.vue";
import CardDraftHint from "./CardDraftHint.vue";

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
    parts.push(props.card.checked ? "已勾选：本次允许 QIO 查看（仍需提交）" : "未勾选：QIO 看不到它的文字");
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
  /**
   * 恢复来源（N6）：正文与附加字段是**同一份未完成输入**，一次重建。
   * - 正文：按「记录是否存在」判断（空草稿是有效编辑状态，§10.4），存在就用草稿
   *   （哪怕是空串），不存在才回落到正式正文；
   * - 附加字段：取自**同一板面**的本机记录（旧记录没有附加快照时回退正式值）。
   */
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
  metaName.value = restored.meta.name ?? "";
  metaLanguage.value = restored.meta.language ?? "";
  metaHref.value = restored.meta.href ?? "";
  metaTitle.value = restored.meta.title ?? "";
  /**
   * 冲突未决时不写草稿、不排保存（§12.1），等用户明确选择；
   * 其余情况把这一版完整输入登记进草稿通道（正文 + 适用附加字段一起）。
   */
  if (conflict) return;
  saveDraftInput();
}

/**
 * 正文或任一附加字段变了都走同一个入口：这一版完整未完成输入（正文 + 适用附加字段）
 * 一起进本机记录。分别保存会让两者不同步（刷新后正文恢复了、网址还是旧的）。
 */
function onDraftInput() {
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

/** 确认编辑才形成有效文字状态；输入过程只存草稿，不调用 QIO。 */
function confirmEdit() {
  const patch: Partial<BoardCard> = { content: draft.value };
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
    </header>

    <div v-if="!card.folded" class="card-body">
      <template v-if="editing">
        <!--
          未决冲突的选择入口放在编辑器**上方**（契约 §12.1：冲突入口在真实操作路径可见可点）：
          卡片有固定高度、编辑区内部滚动，提示若排在编辑器之后，打开编辑器的第一屏就看不见它。
          -->
        <CardDraftHint v-if="store.draftConflictFor(card.id) || boardContentConflict(card.id)" :card-id="card.id" />
        <textarea
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
          <p v-if="card.kind === 'text'" class="content">{{ card.content || "（还没有内容，选中后用工具栏的「编辑」写下来）" }}</p>
          <p v-else-if="card.kind === 'code'" class="content code mono">{{ card.content || "// 待补充代码" }}</p>
          <p v-else-if="card.kind === 'url'" class="content url">
            <a :href="href" target="_blank" rel="noreferrer" @pointerdown.stop>{{ linkTitle || href || "（还没有网址）" }}</a>
          </p>
          <p v-else class="content">
            <span v-if="card.content" class="desc">{{ card.content }}</span>
            <span v-else class="empty">{{ kindLabel }}：还没有补充说明</span>
          </p>
        </template>
      </template>
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
    <label v-if="checkable" class="check" title="本次允许 QIO 查看（默认未勾选）">
      <input
        type="checkbox"
        :checked="card.checked"
        data-im="check"
        :data-card-id="card.id"
        @change="emit('toggle', card.id, 'checked')"
      />
      <span>本次允许 QIO 查看</span>
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
  /* 拖动要跟手：卡片上不加过渡动画 */
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
