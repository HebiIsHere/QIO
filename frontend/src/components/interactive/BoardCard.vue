<!--
  单张板面卡片（子智能体 A 负责）：文字注释 / 文件 / 图片 / 代码 / 网址 / QIO 结果。

  契约：docs/interactive-mode-contract.md §1.1 / §1.4 / §9.6。
  这里落地的规则：
  - 信息分层：**材料类型 → 标题 → 正文 → 元信息**（元信息含状态文字、所在组、更新时间）；
  - 卡片上的许可相关控件只保留「参与 / 不参与」；整卡许可徽章与工具栏勾选 / 说明
    （「允许 QIO 查看」）已移除，不再在卡片上展示或修改 checked；
  - 局部工具栏（data-im="card-toolbar"）只放**这张卡片自己的操作**
    （编辑 / 复制 / 折叠 / 隐藏 / 书签 / 删除）；分组类操作在 SelectionMenu 里；
  - **连接点不在卡片里**（0.2.5 回归修复）：它由 BoardCanvas 的交互层（BoardResizeLayer）
    统一渲染 —— 画在卡片内部时，四边中点正好被那一层 10px 的尺寸热区盖住，真实鼠标按不下去。
    本组件只保留「正在拖线」的 prop 接线（connecting）；工具栏与整理菜单仍朝卡片外侧展开，
    实测不盖标题与正在编辑的正文；
  - 状态一律「文字 + 颜色」双通道，不能只靠颜色；
  - 卡片本身不做状态计算：所有变化 emit 给 BoardCanvas，由它调 board.ts 纯函数 + store.commit。
-->
<script lang="ts">
/**
 * 卡片边框：**一条** evenodd 的 SVG 环形路径（2026-10-24 互动演示最终稿）。
 *
 * 为什么不沿用 CSS border + ::after：
 * - 旧实现是「1px 四边框 + 右下角 3px L 形粗条」两套笔画叠在一起，粗条只盖住一段，
 *   转角处靠 mask 渐变收口，放大后能看到接缝 / 断角，而且颜色只能跟着 border-color；
 * - 最终稿沿卡片外轮廓取 outer / inner 两条闭合轮廓，一次 evenodd 填充得到环带：
 *   离右下角越近越粗（1px → 3px），整圈颜色只有一个来源（fill），悬停 / 选中只换颜色。
 *
 * 几何口径（与演示 HTML 的 roundedSamples / ringPath 一致，只把采样改成“只在变粗区间采样”）：
 * - 外轮廓 = 卡片圆角矩形（半径与 .card 的 var(--r-lg) 相同）；
 * - 内轮廓 = 外轮廓每点沿内法线缩进 t，t 由该点到右下角的距离平滑决定；
 * - 两条轮廓都用闭合子路径，fill-rule=evenodd 只留下中间的环带；
 * - 圆角段在粗细恒定的区域用真正的 A 圆弧，只有右下角变粗区间才采样成短折线 ——
 *   采样步长 1.5 板面单位，视觉上连续，不会出现双线 / 断角。
 *
 * 这里导出的纯函数与组件模板使用同一份几何（测试可以直接钉住厚度与路径）。
 */

/** 与 .card 的 border-radius: var(--r-lg) 同一个数值（SVG d 不能读 CSS 变量）。 */
export const CARD_FRAME_RADIUS = 14;
/** 离右下角足够远时的边框厚度。 */
export const CARD_FRAME_MIN_THICKNESS = 1;
/** 右下角最粗处（“约 3px”）。 */
export const CARD_FRAME_MAX_THICKNESS = 3;
/** 从右下角往里多远的范围内开始加粗（板面单位）。 */
const CARD_FRAME_FALLOFF = 52;
/** 变粗区间的采样步长：越小越圆滑，1.5 已足够让 1–3px 的环带看不出折线。 */
const CARD_FRAME_SAMPLE_STEP = 1.5;
/** 求“到右下角距离 = 52”的交点时的二分次数（1e-9 级别，足够精确）。 */
const CARD_FRAME_BISECT_STEPS = 34;
/** 每段轮廓上找交点的粗采样数（直线 / 90° 圆弧各一段，32 已经足够）。 */
const CARD_FRAME_SCAN_STEPS = 32;

type FramePoint = { x: number; y: number };

type FrameLine = {
  kind: "line";
  x0: number;
  y0: number;
  x1: number;
  y1: number;
  nx: number;
  ny: number;
};

type FrameArc = {
  kind: "arc";
  cx: number;
  cy: number;
  r: number;
  a0: number;
  a1: number;
};

type FrameSegment = FrameLine | FrameArc;

/** 轮廓的一段：u ∈ [u0, u1]；variable=true 时才需要采样（此区间内厚度在变）。 */
type FramePiece = { u0: number; u1: number; variable: boolean };

function frameRadius(w: number, h: number): number {
  return Math.max(0, Math.min(CARD_FRAME_RADIUS, w / 2, h / 2));
}

/** 顺时针一圈：上边 → 右上圆角 → 右边 → 右下圆角 → 下边 → 左下圆角 → 左边 → 左上圆角。 */
function frameSegments(w: number, h: number, r: number): FrameSegment[] {
  const halfPi = Math.PI / 2;
  return [
    { kind: "line", x0: r, y0: 0, x1: w - r, y1: 0, nx: 0, ny: 1 },
    { kind: "arc", cx: w - r, cy: r, r, a0: -halfPi, a1: 0 },
    { kind: "line", x0: w, y0: r, x1: w, y1: h - r, nx: -1, ny: 0 },
    { kind: "arc", cx: w - r, cy: h - r, r, a0: 0, a1: halfPi },
    { kind: "line", x0: w - r, y0: h, x1: r, y1: h, nx: 0, ny: -1 },
    { kind: "arc", cx: r, cy: h - r, r, a0: halfPi, a1: Math.PI },
    { kind: "line", x0: 0, y0: h - r, x1: 0, y1: r, nx: 1, ny: 0 },
    { kind: "arc", cx: r, cy: r, r, a0: Math.PI, a1: Math.PI * 1.5 },
  ];
}

function framePointAt(segment: FrameSegment, u: number): FramePoint {
  if (segment.kind === "line") {
    return {
      x: segment.x0 + (segment.x1 - segment.x0) * u,
      y: segment.y0 + (segment.y1 - segment.y0) * u,
    };
  }
  const angle = segment.a0 + (segment.a1 - segment.a0) * u;
  return { x: segment.cx + Math.cos(angle) * segment.r, y: segment.cy + Math.sin(angle) * segment.r };
}

/** 指向卡片内部的单位法线（圆弧的圆心在卡片内侧）。 */
function frameNormalAt(segment: FrameSegment, u: number): FramePoint {
  if (segment.kind === "line") return { x: segment.nx, y: segment.ny };
  const angle = segment.a0 + (segment.a1 - segment.a0) * u;
  return { x: -Math.cos(angle), y: -Math.sin(angle) };
}

function frameSegmentLength(segment: FrameSegment): number {
  if (segment.kind === "line") return Math.hypot(segment.x1 - segment.x0, segment.y1 - segment.y0);
  return Math.abs(segment.a1 - segment.a0) * segment.r;
}

function frameDistanceToCorner(segment: FrameSegment, u: number, w: number, h: number): number {
  const point = framePointAt(segment, u);
  return Math.hypot(w - point.x, h - point.y);
}

/** 粗细：离右下角越近越粗，52 板面单位外回到 1px；平滑函数保证没有台阶。 */
export function cardFrameThickness(w: number, h: number, x: number, y: number): number {
  if (!Number.isFinite(w) || !Number.isFinite(h) || w <= 0 || h <= 0) return CARD_FRAME_MIN_THICKNESS;
  if (!Number.isFinite(x) || !Number.isFinite(y)) return CARD_FRAME_MIN_THICKNESS;
  const distance = Math.hypot(w - x, h - y);
  const k = Math.max(0, Math.min(1, (CARD_FRAME_FALLOFF - distance) / CARD_FRAME_FALLOFF));
  const eased = k * k * (3 - 2 * k);
  return CARD_FRAME_MIN_THICKNESS + (CARD_FRAME_MAX_THICKNESS - CARD_FRAME_MIN_THICKNESS) * eased;
}

function frameThicknessAt(segment: FrameSegment, u: number, w: number, h: number): number {
  const point = framePointAt(segment, u);
  return cardFrameThickness(w, h, point.x, point.y);
}

function frameInnerPointAt(segment: FrameSegment, u: number, w: number, h: number): FramePoint {
  const point = framePointAt(segment, u);
  const normal = frameNormalAt(segment, u);
  const thickness = cardFrameThickness(w, h, point.x, point.y);
  return { x: point.x + normal.x * thickness, y: point.y + normal.y * thickness };
}

function frameBisectCrossing(segment: FrameSegment, low: number, high: number, w: number, h: number): number {
  let insideLow = frameDistanceToCorner(segment, low, w, h) < CARD_FRAME_FALLOFF;
  let a = low;
  let b = high;
  for (let i = 0; i < CARD_FRAME_BISECT_STEPS; i += 1) {
    const mid = (a + b) / 2;
    const inside = frameDistanceToCorner(segment, mid, w, h) < CARD_FRAME_FALLOFF;
    if (inside === insideLow) a = mid;
    else b = mid;
  }
  return (a + b) / 2;
}

/** 把一段轮廓切成「粗细恒定 / 正在变粗」的连续区间；交点用二分求，保证两段端点严格相接。 */
function framePieces(segment: FrameSegment, w: number, h: number): FramePiece[] {
  const cuts: number[] = [0];
  let previousInside = frameDistanceToCorner(segment, 0, w, h) < CARD_FRAME_FALLOFF;
  for (let i = 1; i <= CARD_FRAME_SCAN_STEPS; i += 1) {
    const u = i / CARD_FRAME_SCAN_STEPS;
    const inside = frameDistanceToCorner(segment, u, w, h) < CARD_FRAME_FALLOFF;
    if (inside !== previousInside) cuts.push(frameBisectCrossing(segment, (i - 1) / CARD_FRAME_SCAN_STEPS, u, w, h));
    previousInside = inside;
  }
  cuts.push(1);
  const pieces: FramePiece[] = [];
  for (let i = 1; i < cuts.length; i += 1) {
    const u0 = cuts[i - 1];
    const u1 = cuts[i];
    if (u1 - u0 <= 1e-6) continue;
    pieces.push({ u0, u1, variable: frameDistanceToCorner(segment, (u0 + u1) / 2, w, h) < CARD_FRAME_FALLOFF });
  }
  return pieces;
}

/** 保留两位小数；-0 写成 0，避免 path 里出现 "-0"。 */
function frameNumber(value: number): string {
  const rounded = Math.round(value * 100) / 100;
  return Object.is(rounded, -0) ? "0" : String(rounded);
}

function framePair(point: FramePoint): string {
  return frameNumber(point.x) + " " + frameNumber(point.y);
}

/**
 * 生成卡片边框路径：外轮廓 + 内轮廓两条闭合子路径，交给 fill-rule=evenodd 填充。
 * 尺寸随 w/h 变化；右下角渐粗（1 → 3），其余三边 1px。
 */
export function cardFramePath(w: number, h: number): string {
  const width = Number.isFinite(w) && w > 0 ? w : 1;
  const height = Number.isFinite(h) && h > 0 ? h : 1;
  const radius = frameRadius(width, height);
  const segments = frameSegments(width, height, radius);

  const outer: string[] = ["M " + framePair(framePointAt(segments[0], 0))];
  for (const segment of segments) {
    if (segment.kind === "line") {
      outer.push("L " + framePair(framePointAt(segment, 1)));
    } else {
      outer.push("A " + frameNumber(radius) + " " + frameNumber(radius) + " 0 0 1 " + framePair(framePointAt(segment, 1)));
    }
  }
  outer.push("Z");

  const inner: string[] = ["M " + framePair(frameInnerPointAt(segments[0], 0, width, height))];
  for (const segment of segments) {
    for (const piece of framePieces(segment, width, height)) {
      if (!piece.variable) {
        const end = frameInnerPointAt(segment, piece.u1, width, height);
        if (segment.kind === "line") {
          inner.push("L " + framePair(end));
        } else {
          const thickness = frameThicknessAt(segment, piece.u0, width, height);
          const innerRadius = Math.max(0.01, segment.r - thickness);
          inner.push(
            "A " +
              frameNumber(innerRadius) +
              " " +
              frameNumber(innerRadius) +
              " 0 0 1 " +
              framePair(end),
          );
        }
        continue;
      }
      const pieceLength = frameSegmentLength(segment) * (piece.u1 - piece.u0);
      const steps = Math.max(2, Math.ceil(pieceLength / CARD_FRAME_SAMPLE_STEP));
      for (let i = 1; i <= steps; i += 1) {
        const u = piece.u0 + (piece.u1 - piece.u0) * (i / steps);
        inner.push("L " + framePair(frameInnerPointAt(segment, u, width, height)));
      }
    }
  }
  inner.push("Z");

  return outer.join(" ") + " " + inner.join(" ");
}
</script>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useInteractiveStore } from "../../stores/interactive";
import {
  cardDraftKey,
  cardDraftMetaFieldsForKind,
  readCardDraftInputForBoard,
  restoreCardDraftInput,
} from "../../interactive/drafts";
import type { CardDraftInput, CardDraftMetaInput } from "../../interactive/drafts";
import { CARD_KIND_LABELS, defaultCardSeed, participationOf } from "../../interactive/board";
import { shouldAutoDetail, shouldShowImageDetailFallback } from "../../interactive/cardDetail";
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
  cardAttachmentName,
  cardAttachmentText,
} from "../../interactive/materials";
// 卡片内真实预览（契约 §3.5）：预览主体挂在**卡片内部**，不盖住编辑区与卡片操作。
// 渲染器按需动态加载；组件自己在收起 / 换附件 / 卸载时中止在途工作并释放资源。
import CardInlinePreview from "./CardInlinePreview.vue";
import type { PreviewPosition } from "../../services/attachments";
import {
  openAttachment,
  pickBrowserFile,
  stateText,
  uploadAttachment,
  waitUntilSettled,
} from "../../services/attachments";
// 图片显示比例（Phase 2-C 冻结字段 meta.display）：纯函数在 renderers/imageDisplay.ts
import {
  decodeDisplayMeta,
  imageDetailHint,
  readImageDisplay,
  writeImageDisplay,
} from "../../interactive/renderers/imageDisplay";
import { useTransientNotice, type NoticeRetry } from "../../composables/useTransientNotice";
import type { BoardCard, BoardGroup } from "../../interactive/types";
import SelectionMenu from "./SelectionMenu.vue";
import CardDraftHint from "./CardDraftHint.vue";
import CardAttachmentPreview from "./CardAttachmentPreview.vue";
import { attachmentPreviewPlan, attachmentRefForPreview } from "../../interactive/attachmentPreview";
import { materialTypeLabel } from "../../interactive/previewPlan";
import { isDesktopShell } from "../../services/attachments";
import TextCardEditor from "../../interactive/TextCardEditor.vue";
// 结果卡正文（F3）：Markdown 原文交给共用渲染器 —— 与对话流同一套转义 / 白名单 / 表格适配。
import MarkdownContent from "../MarkdownContent.vue";
// 卡片正文是这张卡唯一的主要纵向视口：滚动能力保留，滚动条外观统一走 QIO 指示条。
import { vQioScrollbar } from "../../composables/useQioScrollbars";

const props = withDefaults(
  defineProps<{
    card: BoardCard;
    selected: boolean;
    /** 板内搜索定位后的短暂高亮（只是显示） */
    highlight: boolean;
    dragging: boolean;
    x: number;
    y: number;
    groupName: string | null;
    groups: BoardGroup[];
    /**
     * 局部工具栏行内 left/top 的数值，语义 = **相对 toolbarHost 定位原点**的屏幕像素
     * （由 BoardCanvas 按「卡片真实外框 − 宿主原点」算好；不再有任何板面 / 视口夹取）。
     */
    toolbarLeft: number;
    toolbarTop: number;
    /** 多选时：只显示共同适用的操作，并且不把单卡片编辑应用到整组 */
    multi: boolean;
    /** 正在从本卡片拖出关系线 */
    connecting: boolean;
    /**
     * 工具栏的独立宿主（BoardCanvas 的 .card-toolbar-host）。
     * 传 null 或未挂载时 Teleport 关闭：工具栏保持内联渲染，绝不在 (0,0) 之类的地方出现。
     */
    toolbarHost?: HTMLElement | null;
    /**
     * 所属卡片与板面可视区是否相交（BoardCanvas 测量后传入）。
     * 默认 true：直接挂载本组件的场景不做可见性裁剪，行为与改动前一致。
     * 完全不相交时只在显示层隐藏（v-show，元素仍在 DOM 里，不必重新挂载 / 重新测量）。
     */
    toolbarVisible?: boolean;
  }>(),
  { toolbarHost: null, toolbarVisible: true },
);

/**
  事件签名保持不变：BoardCanvas 仍在监听 leave-group / join-group，
  分组操作改由 SelectionMenu 直接算状态并 commit（契约 §8.4.1），这两个事件保留只为不打断画布接线。
*/
const emit = defineEmits<{
  (e: "select", cardId: string, additive: boolean): void;
  (e: "drag-start", cardId: string, event: PointerEvent): void;
  (e: "patch", cardId: string, patch: Partial<BoardCard>, label: string): void;
  (e: "toggle", cardId: string, flag: "checked" | "hidden" | "folded" | "bookmarked" | "participation"): void;
  (e: "remove", cardId: string): void;
  (e: "duplicate", cardId: string): void;
  (e: "leave-group", cardId: string): void;
  (e: "join-group", cardId: string, groupId: string): void;
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
 * 进入编辑（或外部换了编辑内容）时的基准文本：退出编辑时用它判断「有没有真实改动」。
 * 没有它的话，用户只是点开再看一眼、点空白退出，都会凭空白生成一次板面变更。
 */
const editStartText = ref("");
/**
 * 进入编辑（或外部换了编辑内容）时的**附加字段**基线：退出编辑时用它判断
 * 「只改了名称 / 语言 / 网址标题」这类改动有没有真的发生。
 * 没有它的话，只改名称会被判成「没有真实改动」，整次编辑被静默丢弃。
 */
const editStartMeta = ref<CardDraftMetaInput>({});

/** 遗留（非文字卡）编辑框实例：与 TextCardEditor 同一条规则，高度跟随内容。 */
const legacyEditor = ref<HTMLTextAreaElement | null>(null);

/**
 * 遗留编辑框也自动增高：编辑态只允许卡片正文（.card-body）这一层纵向滚动 ——
 * 编辑框自己再能滚，同一个连续编辑动作就会被拆成两个小窗口。
 * 只改显示高度；jsdom / 未布局时 scrollHeight 为 0，这时不写高度。
 */
function autosizeLegacyEditor(): void {
  const el = legacyEditor.value;
  if (!el) return;
  const content = el.scrollHeight;
  if (!content) return;
  el.style.height = "auto";
  el.style.height = content + "px";
}

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

/** 结果卡（kind="reply"）：正文是 QIO 产出的 Markdown 原文，用户不需要点「解析」。 */
const isReplyCard = computed(() => props.card.kind === "reply");
/** 有真实正文才提供「复制原文」：没有原文时不假装能复制出一份内容。 */
const hasReplyBody = computed(() => String(props.card.content ?? "").trim().length > 0);
/**
 * 类型标签（2026-10-14 第九个问题）：材料卡按**附件类型**显示（图片 / PDF / 表格 / 文本 / 代码…），
 * 不再区分「文件 / 图片」两种卡片；非材料卡仍用自己的标签。
 */
const kindLabel = computed(
  () => materialTypeLabel(props.card.kind, (props.card.meta ?? {}) as Record<string, unknown>) || CARD_KIND_LABELS[props.card.kind],
);

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

/**
 * 文件卡（2026-10-14 用户要求）：卡片本身只显示文件名，其余按钮全部进工具栏，
 * 也不需要「待补充文件说明」那一段正文。
 */
const isFileCard = computed(() => props.card.kind === "file");

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
  /*
    段落 / 引用块里的换行必须真的显示成换行：块文本内部用 \n 连接（parseDocText），
    但 HTML 默认把 \n 折叠成空格 —— 编辑时明明换了行、关闭后却挤成一行就是这个原因。
  */
  out = out.replace(/\n/g, "<br />");
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

/**
 * 结果卡「复制原文」：给出**完整** Markdown 原文（card.content 一字不改）。
 * 反馈口径与代码块复制一致：成功说已复制，失败如实说失败并给出手动复制退路。
 */
const sourceCopyState = ref<{ ok: boolean } | null>(null);
let sourceCopyTimer: ReturnType<typeof setTimeout> | null = null;

function sourceCopyText(): string {
  const state = sourceCopyState.value;
  if (!state) return "复制原文";
  return state.ok ? "已复制原文" : "复制失败";
}

function copySourceTitle(): string {
  const state = sourceCopyState.value;
  if (!state) return "复制这张结果卡的 Markdown 原文（整份内容逐字不变）";
  return state.ok
    ? "已复制到剪贴板"
    : "复制失败：可以选中原文后按 Ctrl+C 手动复制，或再点一次重试";
}

async function copySource(): Promise<void> {
  const ok = await writeClipboard(String(props.card.content ?? ""));
  sourceCopyState.value = { ok };
  if (sourceCopyTimer) clearTimeout(sourceCopyTimer);
  sourceCopyTimer = setTimeout(
    () => {
      sourceCopyState.value = null;
    },
    ok ? 1600 : 2400,
  );
}

onBeforeUnmount(() => {
  if (copyTimer) clearTimeout(copyTimer);
  if (sourceCopyTimer) clearTimeout(sourceCopyTimer);
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
/**
 * 附件只认 kind="file"：图片统一是文件卡的附件（meta.file），不再是独立卡片类型
 * （本轮简化：BoardCard 里所有 kind==="image" 分支都已删除）。
 */
const attachKind = computed<"file" | null>(() => (props.card.kind === "file" ? "file" : null));
/**
 * 封面式呈现（2026-10-14 第六个问题）：
 * 图片卡、以及**附件是图片**的文件卡，在常态与折叠态把图片铺满整张卡作为背景
 * （标题 / 一行补充说明 / 右下角「查看」浮在蒙版上）。编辑态与阅读态不铺封面：
 * 编辑要看清表单，阅读要让真实预览当主体。
 * 判定与 CardAttachmentPreview 用**同一个计划函数**，两边不会各说各话。
 */
const desktopShell = ref(false);
if (typeof window !== "undefined") desktopShell.value = isDesktopShell();
const coverPlan = computed(() =>
  attachmentPreviewPlan(props.card.kind, (props.card.meta ?? {}) as Record<string, unknown>, {
    desktop: desktopShell.value,
  }),
);
/**
 * 这张卡的附件是不是「能安全渲染的图片」。
 *
 * 为什么单独拿出来：互动板现在**不再创建 `kind="image"` 的卡片**
 * （`ADDABLE_CARD_KINDS = ["text", "file"]`），图片一律作为附件存在**文件卡**里，
 * 卡片类型标签由附件类型推出来（显示成「图片」，底层 kind 是 "file"）。
 * 所以「是不是图片卡」只能看附件，不能看 kind。
 */
const attachmentIsImage = computed(
  () => Boolean(attachKind.value) && coverPlan.value.imagePreviewable && coverPlan.value.state === "linked",
);

/* ------------------------------------------------------------------------
 * 图片显示比例与「详情」状态（Phase 2-C 契约 §5.2）
 *
 * 三条边界（与 docs/interactive-page-v2-contract.md §5.2 逐条对应）：
 * 1. 比例只记录**浏览器解码出的真实显示像素**（meta.display = {width,height,aspectRatio}）；
 *    0 / NaN / 非整数 / 超范围一律不写 —— 宁可没有，也不写一个猜出来的比例；
 * 2. 详细状态：图片铺满卡片、圆角裁切、不保留普通标题栏高度，等比完整显示（contain），
 *    不用 cover 遮主体、不拉伸；名称 / 尺寸事实 / 收起入口浮在图上；
 * 3. 简洁（常态）状态仍按原比例完整显示（封面层 contain），长图 / 竖图始终有详情入口，
 *    不会被简洁态永远锁住。
 *
 * 比例写入走既有 emit("patch")（由 BoardCanvas commit），不在这里改板面状态；
 * 只写 meta.display，**不改 card.w/h**（§16.3 / §17.1：图片加载不得自动改几何）。
 * ---------------------------------------------------------------------- */
const imageDisplay = computed(() =>
  readImageDisplay((props.card.meta ?? {}) as Record<string, unknown>),
);
/** 详情态是否可用：只有「已关联的图片附件」才有；其它材料沿用既有阅读展开。 */
const imageDetailAvailable = computed(() => attachmentIsImage.value);
const imageDetailOpen = ref(false);
const imageDetailVisible = computed(() => imageDetailOpen.value && imageDetailAvailable.value);
const imageAspectRatio = computed<number | null>(() => {
  const display = imageDisplay.value;
  if (!display || !(display.width > 0) || !(display.height > 0)) return null;
  return display.width / display.height;
});
/**
 * 自动详细状态：只按卡片真实尺寸（和图片比例）判断，不读板面 scale。
 * previousDetailed 用上一次结果做迟滞，避免尺寸停在阈值附近时反复切换。
 */
const autoDetailed = ref(false);
watch(
  () => [props.card.w, props.card.h, imageAspectRatio.value, imageDetailAvailable.value] as const,
  () => {
    autoDetailed.value = shouldAutoDetail({
      width: props.card.w,
      height: props.card.h,
      aspectRatio: imageDetailAvailable.value ? imageAspectRatio.value : null,
      previousDetailed: autoDetailed.value,
    });
  },
  { immediate: true },
);
/** 用户点了详细状态里的关闭：在尺寸再次变化前不再自动弹回，避免“关了又开”。 */
const detailDismissed = ref(false);
watch(
  () => [props.card.w, props.card.h] as const,
  ([width, height], [previousWidth, previousHeight]) => {
    if (width !== previousWidth || height !== previousHeight) detailDismissed.value = false;
  },
);
const autoDetailVisible = computed(() => autoDetailed.value && !detailDismissed.value);
function closeAutoDetail(): void {
  detailDismissed.value = true;
}
const imageFallbackVisible = computed(() =>
  imageDetailAvailable.value &&
  !autoDetailVisible.value &&
  shouldShowImageDetailFallback({
    width: props.card.w,
    height: props.card.h,
    aspectRatio: imageAspectRatio.value,
  }),
);
const imageDetailTitle = computed(() =>
  imageDetailHint(imageDisplay.value) || "查看完整图片（只改查看方式，不改内容与卡片尺寸）",
);
const participationIncluded = computed(() => participationOf(props.card) === "included");

function toggleImageDetail(): void {
  imageDetailOpen.value = !imageDetailOpen.value;
}

function closeImageDetail(): void {
  imageDetailOpen.value = false;
}

/**
 * 本次会话里已经写过 meta.display 的身份（卡片 + 附件 id + 附件版本）。
 * 用途：解码结果回来之前卡片可能重渲染多次，同一身份只发一次 patch ——
 * 不因为重复渲染 / 版本未回执而反复写板面、反复把用户尺寸重置成默认值。
 */
const writtenDisplayKeys = new Set<string>();

function imageDisplayKey(): string {
  const meta = (props.card.meta ?? {}) as Record<string, unknown>;
  const file = meta.file && typeof meta.file === "object" ? (meta.file as Record<string, unknown>) : {};
  return [props.card.id, cardAttachmentId(props.card.meta, props.card.kind), String(file.version ?? file.attachmentVersion ?? "")].join("|");
}

/**
 * 记录图片的真实显示比例（只写 meta.display）。
 *
 * **从哪里取真实像素**：封面层里那张已经带认证 blob URL 的 <img> 解码结果
 * （naturalWidth / naturalHeight，浏览器已按 EXIF 方向解码）。
 * 不再单独发一次请求、也不再新建一个 blob URL —— 既有取字节 / 释放路径一字不动
 * （CardAttachmentPreview 仍然只持有一个 URL，测试里的取字节次数与 URL 计数保持不变）。
 *
 * 三条边界：
 * * 只有真实解码出的正整数像素才写；0 / NaN / 负值 / 超范围 → 什么都不写（不猜、不写 0）；
 * * 同一身份（卡片 id + 附件 id + 附件版本）只写一次（writtenDisplayKeys）：
 *   重复渲染 / 回执未到都不会反复写板面，更不会借机重置用户设置的尺寸；
 * * 只改 meta.display，**不改 card.w/h**（§16.3 / §17.1）。
 */
function captureCoverDisplay(): void {
  if (!attachmentIsImage.value || imageDisplay.value) return;
  const img = cardEl.value?.querySelector<HTMLImageElement>('img[data-im="card-cover-img"]') ?? null;
  if (!img) return;
  const width = img.naturalWidth;
  const height = img.naturalHeight;
  const display = decodeDisplayMeta(width, height);
  if (!display) return; // 还没解码完 / 解码失败：不写任何值
  const key = imageDisplayKey();
  if (writtenDisplayKeys.has(key)) return;
  const nextMeta = writeImageDisplay((props.card.meta ?? {}) as Record<string, unknown>, display);
  writtenDisplayKeys.add(key);
  emit("patch", props.card.id, { meta: nextMeta }, "记录图片显示比例");
}

/**
 * 封面图解码完成 / 换图：量一次真实像素。
 *
 * 用**捕获阶段的事件委托**（load 不冒泡，但捕获阶段会经过祖先）：
 * 封面图是异步 blob URL 挂上去的，元素可能在绑定之后才出现、也可能整块被替换，
 * 逐个元素挂监听一定会漏 —— 委托到卡片根节点就与渲染时序无关。
 */
function onCoverImgLoad(event: Event): void {
  const target = event.target as Element | null;
  if (!target || target.tagName !== "IMG") return;
  if (!target.hasAttribute("data-im") || target.getAttribute("data-im") !== "card-cover-img") return;
  captureCoverDisplay();
}

function measureCoverIfReady(): void {
  const img = cardEl.value?.querySelector<HTMLImageElement>('img[data-im="card-cover-img"]') ?? null;
  if (img && img.complete && img.naturalWidth > 0) captureCoverDisplay();
}

onMounted(() => {
  cardEl.value?.addEventListener("load", onCoverImgLoad, true);
  measureCoverIfReady();
});

onBeforeUnmount(() => {
  cardEl.value?.removeEventListener("load", onCoverImgLoad, true);
});

watch(
  [
    () => props.card.id,
    () => cardAttachmentId(props.card.meta, props.card.kind),
    imageDisplay,
    attachmentIsImage,
  ],
  async () => {
    if (!imageDetailAvailable.value) imageDetailOpen.value = false;
    await nextTick();
    measureCoverIfReady();
  },
  { immediate: true },
);
watch(
  () => props.card.folded,
  (folded) => {
    if (folded) imageDetailOpen.value = false;
  },
);
const coverMode = computed(
  () =>
    // 图片卡 + 「附件是图片」的文件卡都铺封面（2026-10-14 第六个问题；
    // 2026-10-14 回归修复：按 kind 判会漏掉全部现有图片卡，因为图片都挂在文件卡上）
    attachmentIsImage.value &&
    (displayState.value === "normal" || displayState.value === "folded"),
);
/**
 * 封面底部那行说明：**只有真实说明**才渲染。
 *
 * 新文件卡的 content 默认是 board.ts 的 defaultCardSeed("file").content（当前为「待补充文件说明」），
 * 那是占位不是说明 —— 与 defaultCardSeed 比较，避免再维护第二套占位文案名单。
 */
const FILE_CONTENT_PLACEHOLDER = defaultCardSeed("file").content;
const coverDesc = computed(() => {
  const raw = String(props.card.content ?? "").trim();
  if (!raw || raw === FILE_CONTENT_PLACEHOLDER) return "";
  return raw;
});

const attachmentId = computed(() => cardAttachmentId(props.card.meta, props.card.kind));
const attachmentLinked = computed(() => cardAttachmentLinked(props.card.meta, props.card.kind));
const attachmentText = computed(() => cardAttachmentText(props.card.meta, props.card.kind));
/**
 * 附件提示与 AddMenu 共用**同一份原语**（任务书 §二.8）：
 * busy 真实进度一直显示，ok 约 3 秒自动移除（只移除提示），error 不自动消失且可关闭 / 可重来。
 * 本组件只把 patch 交给父级 commit，所以成功文案不许声称服务端已经保存。
 */
const attachNotice = useTransientNotice();
const attachStatus = attachNotice.notice;

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/** 取消选择的文案按卡片**当前**事实区分：卡片已有附件时不许说「还没有关联」。 */
function attachCancelText(): string {
  if (attachmentLinked.value) {
    const previous = cardAttachmentName(props.card.meta, props.card.kind) || name.value;
    return (
      "没有选择文件：这张卡片仍然关联着原有附件" +
      (previous ? "「" + previous + "」" : "") +
      "，本次没有更换"
    );
  }
  return "没有选择文件：这张卡片还没有关联已保存的附件，QIO 读不到它的正文";
}

/** 失败提示上的「重新选择文件」：重新走一遍 pickBrowserFile → 上传 → 关联。 */
function runAttachRetry(): void {
  attachStatus.value?.retry?.run();
}

function dismissAttachStatus(): void {
  attachNotice.dismiss();
}

/**
 * 选一份真实文件 → 走既有上传接口（不新造上传链路）→ 等附件进入最终态 →
 * 把 `{ name, attachmentId, size }` 写进卡片 meta（由父级 commit，组件不直接改板面状态）。
 * 取消与失败都如实说出来，并且显示在卡片上，不静默。
 *
 * 每次调用 begin() 开一代新操作，之后所有写入都带这次操作的 token ——
 * 迟到的结果不会覆盖 / 清掉别的提示（与 AddMenu 的规则逐字一致）。
 */
async function pickAttachment(): Promise<void> {
  const kind = attachKind.value;
  if (!kind) return;
  const cardId = props.card.id;
  const token = attachNotice.begin();
  const retry: NoticeRetry = { label: "重新选择文件", run: () => void pickAttachment() };
  attachNotice.progress(token, "正在等待选择文件…");
  let file: File | null;
  try {
    file = await pickBrowserFile();
  } catch (error) {
    attachNotice.fail(token, "选择文件失败：" + messageOf(error), retry);
    return;
  }
  // 被新操作取代 / 组件已卸载：不再继续，也不写提示与板面（isCurrent 同时覆盖这两种失效）
  if (!attachNotice.isCurrent(token)) return;
  if (!file) {
    // 用户取消：如实说明（不假装读到了内容），文案按卡片当前是否已有附件区分
    attachNotice.fail(token, attachCancelText(), retry);
    return;
  }
  const fileName = file.name || "未命名";
  attachNotice.progress(token, "正在保存附件「" + fileName + "」…");
  try {
    const uploaded = await uploadAttachment(file);
    if (!attachNotice.isCurrent(token)) return;
    // 上传完成还要等附件真的 ready（服务端处理中）：这一段同样是真实进度
    attachNotice.progress(token, "正在处理附件「" + fileName + "」…");
    const settled = await waitUntilSettled(uploaded);
    /**
     * 这里多守一次：卡片是**同一个目标**，迟到的旧操作不许把新关联覆盖回去。
     * （AddMenu 面对的是「每次添加一张新卡」，各自的附件绑各自的卡；这里只有一张卡。）
     */
    if (!attachNotice.isCurrent(token)) return;
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
    /**
     * 成功只陈述两件已确认的事实：附件本身已经保存（stateText），板面关联还只是**本地**改动
     * —— 组件只 emit patch 给父级 commit，不声称服务端已保存（任务书 §二.10）。
     */
    attachNotice.succeed(
      token,
      "已关联附件「" +
        settled.name +
        "」（" +
        stateText(settled) +
        "）；板面关联是本地改动，提交后 QIO 才可见",
    );
  } catch (error) {
    attachNotice.fail(token, "附件保存失败：" + messageOf(error), retry);
  }
}

/** 正文首行：折叠时当标题用，避免折叠后只剩一个类型标签 */
const firstLine = computed(() => (props.card.content || "").trim().split("\n")[0].trim());

/**
  标题层：材料自己的名字 / 网址标题 / 代码语言 / 文字卡的第一个标题块。

  文字注释只有折叠后才用首行顶上（折叠态只剩标题，必须认得出是哪一条）；
  常态下它的「标题」是正文里的标题块 —— 没有标题块时正文自己就是身份，
  不额外造一个和正文重复的标题。
*/
function firstHeadingText(): string {
  const heading = cardDoc.value.blocks.find((block) => block.kind === "heading");
  return String(heading?.text ?? "").trim();
}

const titleText = computed(() => {
  if (props.card.kind === "file") {
    // 卡片本身只显示文件名（图片也是文件卡）：meta.name 缺失时退回附件自己的名字，绝不显示成「未命名」
    return name.value || cardAttachmentName(props.card.meta, props.card.kind) || "未命名" + kindLabel.value;
  }
  if (props.card.kind === "url") return linkTitle.value || "（还没有网址）";
  if (props.card.kind === "code") return language.value ? language.value + " 代码" : "代码";
  if (isTextCard.value) return props.card.folded ? firstHeadingText() || firstLine.value : firstHeadingText();
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

/** 未决冲突（F3 正文冲突 / 草稿新旧冲突）：两类卡都必须留出入口，两处提示共用这一个判断。 */
const conflictPending = computed(() =>
  Boolean(store.draftConflictFor(props.card.id) || boardContentConflict(props.card.id)),
);

/**
 * 编辑态里草稿提示是否占位：
 * - 文字卡 / 文件卡：只有**真的有问题**才占一行（草稿保存失败 / 清除未同步 / 本机副本没删掉）；
 *   正常的「保存中 / 已保存」不再往卡片里塞提示 —— 这两类卡的编辑面只留内容与工具栏。
 * - 其它卡（图片 / 遗留 url / code）：沿用既有行为（保存中 / 已保存 / 失败都显示）。
 */
const editDraftHintVisible = computed(
  () => !conflictPending.value && (!(isTextCard.value || isFileCard.value) || cardDraftNeedsAttention.value),
);

/* ------------------------------------------------------------------------
 * 文件卡的工具栏动作（2026-10-14 用户要求：卡片只显示文件名，按钮全在工具栏）
 *
 * 打开附件走**既有** openAttachment（与 CardAttachmentPreview 同一条链路、同一份计划函数），
 * 结果如实显示在工具栏里：打不开就说打不开，绝不假装成功。
 * ---------------------------------------------------------------------- */
const fileActionStatus = ref<{ tone: "busy" | "ok" | "error"; text: string } | null>(null);
const fileActionBusy = ref(false);
const filePlan = computed(() =>
  attachmentPreviewPlan(props.card.kind, (props.card.meta ?? {}) as Record<string, unknown>, {
    desktop: desktopShell.value,
  }),
);
const fileLinked = computed(() => filePlan.value.state === "linked" && Boolean(filePlan.value.attachmentId));
/** 工具栏上这一行状态：打开附件的结果优先，其次是重新关联的结果 */
const fileToolbarStatus = computed(() => fileActionStatus.value ?? attachStatus.value);
const fileActionText = computed(() => {
  const action = filePlan.value.action;
  if (action === "reveal" || action === "open_in_folder") return "在文件夹中显示";
  if (action === "download") return "下载副本";
  if (action === "blocked") return "无法打开";
  if (action === "open") return "打开";
  return "查看";
});

async function openAttachmentFromToolbar(): Promise<void> {
  if (!fileLinked.value || fileActionBusy.value) return;
  const plan = filePlan.value;
  if (plan.action === "blocked" || plan.action === "open_in_folder") {
    // 计划说这条路现在走不通：如实说明原因，不执行一个注定报错的调用
    fileActionStatus.value = { tone: "error", text: plan.openNote || "这个附件现在打不开" };
    return;
  }
  fileActionBusy.value = true;
  fileActionStatus.value = { tone: "busy", text: "正在打开「" + plan.displayName + "」…" };
  try {
    const result = await openAttachment(
      attachmentRefForPreview({
        attachmentId: plan.attachmentId,
        name: plan.displayName,
        size: plan.sizeBytes,
      }),
      { desktop: desktopShell.value },
    );
    fileActionStatus.value = { tone: "ok", text: result.note };
  } catch (error) {
    fileActionStatus.value = { tone: "error", text: "打不开：" + messageOf(error) };
  } finally {
    fileActionBusy.value = false;
  }
}

/**
 * 元信息层：状态必须能读出来，不能只靠颜色。
 *
 * 2026-10-14（用户要求）：材料 / QIO 结果的说明、所在组都不再在这里重复 ——
 * 材料类型由类型标签承载，组由组框标题承载；许可徽章与工具栏勾选已移除，
 * 卡片上只剩「参与 / 不参与」。这里只留**卡片自身的显示状态**（隐藏 / 折叠 / 书签）与时间。
 */
const statusText = computed(() => {
  const parts: string[] = [];
  if (props.card.hidden) parts.push("已隐藏：退出讨论范围");
  if (props.card.folded) parts.push("已折叠：只改变显示");
  if (props.card.bookmarked) parts.push("书签：查找优先");
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

/**
 * 整理菜单 / 「更多」面板的展开方向：朝**工具栏外侧**开（不盖卡片标题与正文）。
 *
 * 2026-10-14 第二轮：工具栏固定贴在卡片上方，方向不能再拿 `props.toolbarTop < props.y` 比
 * —— 前者是相对宿主的屏幕像素、后者是板面坐标，两个坐标系混算本来就是错的。
 * 现在全部用**同一套屏幕坐标**（getBoundingClientRect）实测：
 *   可用空间 = 工具栏上沿 − 宿主（toolbarRoot.parentElement）上沿；
 *   需要空间 = 面板实测高度 + MENU_MIN_GAP。
 * 宿主裁切到板面可视区（.card-toolbar-host 的 overflow: hidden），所以贴顶时必须向下开，
 * 否则菜单会被裁掉。量不到真实几何（jsdom / 宿主未挂载）时退回改动前那条比较，行为不变。
 * **只改打开方向，绝不移动工具栏主体。** 三个入口（更多 / 说明 / 整理菜单）共用这一条判断。
 */
const MENU_MIN_GAP = 8;
const MENU_FALLBACK_H = 160;

/** 上方放得下整份菜单吗（true = 向上开）。所有方向判断都走这一条，不各写一份。 */
function menuFitsAbove(): boolean {
  const toolbar = toolbarRoot.value;
  const host = toolbar?.parentElement ?? null;
  const toolbarRect =
    toolbar && typeof toolbar.getBoundingClientRect === "function" ? toolbar.getBoundingClientRect() : null;
  const hostRect = host && typeof host.getBoundingClientRect === "function" ? host.getBoundingClientRect() : null;
  if (!toolbarRect || !hostRect || !(toolbarRect.height > 0) || !(hostRect.height > 0)) {
    // jsdom（没有真实布局）/ 宿主未挂载：沿用改动前的口径，不假装测到了几何
    return props.toolbarTop < props.y;
  }
  const spaceAbove = toolbarRect.top - hostRect.top;
  return spaceAbove >= (morePanelHeight.value || MENU_FALLBACK_H) + MENU_MIN_GAP;
}

const menuOpensUp = computed(menuFitsAbove);

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
/** 旧 store 的展开状态只作为兼容状态保留，不再有按钮入口；实际显示由卡片尺寸决定。 */

/* ------------------------------------------------------------------------
 * 卡片自己的四种显示状态（任务书 §6）：常态 / 阅读展开 / 编辑 / 折叠
 *
 * 互斥关系只在这一个计算属性里定死：卡片里同一时刻只可能出现其中一种主体。
 * 之前「展开的预览」与「正在编辑的正文」可以同时挂在卡片里，用户分不清
 * 「展开」到底指折成标题、阅读，还是打开编辑器 —— 那是入口含义难分的根源。
 *
 * 四种状态都不写板面状态、不产生表达：折叠只改显示，阅读展开只在前端内存，
 * 编辑内容进既有草稿通道。尺寸调整不在这里参与判断。
 * ---------------------------------------------------------------------- */
/**
 * 五种显示状态：常态 / 阅读展开 / 编辑 / 折叠 / **图片详情**（Phase 2-C）。
 *
 * 图片详情是「详细状态」的落地：它只属于已关联图片附件的文件卡，
 * 主体是一整张铺满卡片的图（不保留普通标题栏高度），退出即回到常态封面。
 */
type CardDisplayState = "normal" | "reading" | "editing" | "folded" | "image-detail";

/** 有没有可读材料：文件 / 图片要有**真实已保存的附件**；其它卡片的正文本身就是材料 */
const readingAvailable = computed(() => (attachKind.value ? attachmentLinked.value : true));

/** 阅读展开时挂的是真实文件预览，还是只把卡片自己的正文当成阅读主体 */
const hasPreviewMaterial = computed(() => Boolean(attachKind.value) && attachmentLinked.value);

/**
 * 卡片内阅读态入口是否适用（本轮简化）。
 *
 * 图片附件文件卡的阅读主体**就是封面本身**（常态已经把图片完整铺开），
 * 所以不再有「展开预览 / 收起预览」入口，也不会进入 reading、不挂 CardInlinePreview。
 * 其它材料（文字卡、非图片文件卡）的能力一字不变。
 */
const previewToggleAvailable = computed(() => readingAvailable.value && !attachmentIsImage.value);

const displayState = computed<CardDisplayState>(() => {
  if (props.card.folded) return "folded";
  if (editing.value) return "editing";
  // 图片详情优先于阅读展开：图片不进入阅读态。
  if (imageDetailVisible.value || (autoDetailVisible.value && imageDetailAvailable.value)) return "image-detail";
  if (autoDetailVisible.value && previewToggleAvailable.value) return "reading";
  return "normal";
});

/**
 * 窄 / 矮卡片收一档（任务书 §3.3 / §3.5）。
 *
 * 判断用的是**卡片自己的权威尺寸**（板面状态 card.w / card.h），不是视口宽度：
 * 「一张卡被拉窄」与「窗口变小」是两件事。最小尺寸（180×96）下，头部让出材料类型标签、
 * 页脚让出更新时间，换来标题、主要操作、必要状态与内容区都还在（内容区高度仍 > 0）。
 */
const compactChrome = computed(() => props.card.w < 240 || props.card.h < 140);

/**
 * 编辑区的最小高度（**显示参数**，不改变提交内容、许可与已保存尺寸）。
 *
 * 规则：编辑态是一整块连续编辑区，不能因为一个固定的最小高度把较小的卡片撑开 ——
 * 那会立刻在卡内造出「外层 .card-body + 内层 textarea」两层纵向滚动。
 * 所以这里按卡片自己的权威高度算一个**不超过剩余空间**的最小高度：
 * 头部（约 26）+ 页脚（约 16）+ 卡片内边距（2×12）+ 编辑器工具栏（约 22）
 * + 完成/取消与说明行（约 76）≈ 164px 是编辑区之外必须留出的固定高度。
 * 卡片很小时最小值收到 40px：编辑区仍然可点、可输入，超出部分由卡片正文这一层滚动接住 ——
 * 滚动层仍然只有一层（textarea 自己不再滚动，见 TextCardEditor 的自动增高）。
 */
const EDITOR_CHROME_H = 164;
const editorMinHeight = computed(() => {
  const height = Number.isFinite(props.card.h) ? props.card.h : 0;
  return Math.max(40, Math.min(160, height - EDITOR_CHROME_H));
});

/* ------------------------------------------------------------------------
 * 「更多」：低频操作（复制 / 隐藏 / 书签 / 整理 / 删除）收在一处（任务书 §6.5）
 *
 * 面板朝卡片**外侧**展开（与 SelectionMenu 同一套方向规则），不盖标题、连接点与正文；
 * 面板本身 overflow 可见，否则嵌在里面的「整理」面板会被裁掉。
 * Esc 只关这个面板（不让板面把同一次 Esc 当成取消拖动）；点到别处就收起。
 * ---------------------------------------------------------------------- */
const toolbarRoot = ref<HTMLElement | null>(null);
/** <article data-im="card"> 的真实外框：BoardCanvas 只拿它当锚点（只读，不回写板面） */
const cardEl = ref<HTMLElement | null>(null);
/**
 * 卡片外轮廓的实测尺寸：只给「高度 auto」的状态用（折叠 / 冲突未决）。
 * 普通卡片的 w/h 是板面状态里的权威尺寸，不重新测量；折叠态高度由内容决定，
 * 必须读一次真实布局，否则边框会在卡片底部多出 / 少掉一截。
 *
 * 读 clientWidth / clientHeight（布局单位，不受画布 transform: scale() 影响）；
 * 能从 getBoundingClientRect 反推出比例时用比例还原小数，避免底边差半像素。
 */
const measuredFrame = ref({ w: 0, h: 0 });
const frameSize = computed(() => {
  const width = Number.isFinite(props.card.w) && props.card.w > 0 ? props.card.w : 1;
  const autoHeight = props.card.folded || conflictExpanded.value;
  const measuredHeight = measuredFrame.value.h;
  const rawHeight = autoHeight && measuredHeight > 0 ? measuredHeight : props.card.h;
  const height = Number.isFinite(rawHeight) && rawHeight > 0 ? rawHeight : 1;
  // 实测高度可能是 52.00000000000001 这类浮点噪声：保留两位小数，viewBox 与 path 用同一份数。
  return { w: Math.round(width * 100) / 100, h: Math.round(height * 100) / 100 };
});
/** 单一路径：外轮廓 + 内轮廓，fill-rule=evenodd 填充环带（见文件顶部的几何说明）。 */
const framePath = computed(() => cardFramePath(frameSize.value.w, frameSize.value.h));
let frameResizeObserver: ResizeObserver | null = null;

function measureFrame(): void {
  const element = cardEl.value;
  if (!element) return;
  const layoutWidth = element.offsetWidth || element.clientWidth || 0;
  const layoutHeight = element.offsetHeight || element.clientHeight || 0;
  if (!(layoutWidth > 0) || !(layoutHeight > 0)) return;
  let width = layoutWidth;
  let height = layoutHeight;
  const rect =
    typeof element.getBoundingClientRect === "function" ? element.getBoundingClientRect() : null;
  if (rect && rect.width > 0) {
    const scale = rect.width / layoutWidth;
    if (Number.isFinite(scale) && scale > 0) {
      width = rect.width / scale;
      if (rect.height > 0) height = rect.height / scale;
    }
  }
  if (measuredFrame.value.w === width && measuredFrame.value.h === height) return;
  measuredFrame.value = { w: width, h: height };
}

function observeFrame(): void {
  frameResizeObserver?.disconnect();
  frameResizeObserver = null;
  measureFrame();
  const element = cardEl.value;
  if (!element || typeof ResizeObserver !== "function") return;
  frameResizeObserver = new ResizeObserver(() => measureFrame());
  frameResizeObserver.observe(element);
}

onMounted(observeFrame);
onBeforeUnmount(() => {
  frameResizeObserver?.disconnect();
  frameResizeObserver = null;
});

// 折叠 / 冲突展开 / 编辑态切换、卡片尺寸变化之后都补一次实测（ResizeObserver 在 jsdom 里没有）
watch(
  () => [props.card.w, props.card.h, props.card.folded, conflictExpanded.value, displayState.value],
  () => {
    void nextTick(measureFrame);
  },
);
/** 「更多」面板元素：打开后量一次真实高度，供菜单方向判断 */
const morePanel = ref<HTMLElement | null>(null);
const moreOpen = ref(false);
const moreOpensUp = ref(false);
/** 上一次实测到的「更多」面板高度（量不到时用 MENU_FALLBACK_H） */
const morePanelHeight = ref(0);

function toggleMore(): void {
  if (moreOpen.value) {
    moreOpen.value = false;
    return;
  }
  moreOpensUp.value = menuFitsAbove();
  moreOpen.value = true;
  /*
    面板挂上之后**再量一次**：先取面板真实高度（换行 / 文案 / 窄窗口），
    再用与上面完全同一条 menuFitsAbove() 重新判方向。nextTick 是微任务，纠正发生在绘制之前，
    所以用户看不到「先上后下」的闪动。只改方向，不动工具栏主体。
  */
  void nextTick(() => {
    const element = morePanel.value;
    const rect =
      element && typeof element.getBoundingClientRect === "function" ? element.getBoundingClientRect() : null;
    if (rect && rect.height > 0) morePanelHeight.value = rect.height;
    const shouldOpenUp = menuFitsAbove();
    if (shouldOpenUp !== moreOpensUp.value) moreOpensUp.value = shouldOpenUp;
  });
}

/** 面板打开期间几何变化（窗口 / 缩放 / 宿主裁切）也要跟着实测方向走 —— 包括传给整理菜单的偏好 */
watch(menuOpensUp, (up) => {
  if (moreOpen.value) moreOpensUp.value = up;
});

/**
 * 交给 BoardCanvas 的锚点入口：只暴露**只读的 DOM 元素**。
 * 定位只读 getBoundingClientRect()，不改板面状态、不产生保存 / 模型调用 / 审批。
 */
defineExpose({
  getCardElement: () => cardEl.value,
  getToolbarElement: () => toolbarRoot.value,
});

/** 不再是工具栏所有者时，把「更多」面板收起（面板不能跟着上一位所有者留在屏幕上） */
watch(isToolbarOwner, (owner) => {
  if (!owner) moreOpen.value = false;
});

/** 面板里的一次动作：先关闭面板，再把操作交出去（emit 给板面统一 commit） */
function moreAction(run: () => void): void {
  moreOpen.value = false;
  run();
}

function onMorePointerDown(event: PointerEvent): void {
  const target = event.target as Node | null;
  const root = toolbarRoot.value;
  if (target && root && !root.contains(target)) moreOpen.value = false;
}

function onMoreKeydown(event: KeyboardEvent): void {
  if (event.key !== "Escape" || !moreOpen.value) return;
  // 先关这个面板：同一次 Esc 不再往下传，板面不会把它当成「取消拖动」
  event.stopPropagation();
  moreOpen.value = false;
}

watch(moreOpen, (open) => {
  if (open) {
    document.addEventListener("pointerdown", onMorePointerDown, true);
    document.addEventListener("keydown", onMoreKeydown);
  } else {
    document.removeEventListener("pointerdown", onMorePointerDown, true);
    document.removeEventListener("keydown", onMoreKeydown);
  }
});

onBeforeUnmount(() => {
  document.removeEventListener("pointerdown", onMorePointerDown, true);
  document.removeEventListener("keydown", onMoreKeydown);
});

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

/**
 * Teleport 的目标：宿主没挂上时用一个不会被用到的占位（此时 :disabled 为真，占位不生效）。
 * 目标只能是本板面实例自己的 ref —— 绝不 querySelector 全局查找。
 */
const toolbarTeleportTarget = computed<HTMLElement | string>(() => props.toolbarHost ?? "body");

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

function startEdit() {
  editing.value = true;
  void nextTick(autosizeLegacyEditor);
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
  editStartText.value = draft.value;
  metaName.value = restored.meta.name ?? "";
  metaLanguage.value = restored.meta.language ?? "";
  metaHref.value = restored.meta.href ?? "";
  metaTitle.value = restored.meta.title ?? "";
  // 附加字段的基线必须与上面重建的正文在同一处登记（两者是同一份未完成输入）
  editStartMeta.value = currentMetaInput();
}

/**
 * 正文或任一附加字段变了都走同一个入口：这一版完整未完成输入（正文 + 适用附加字段）
 * 一起进本机记录。分别保存会让两者不同步（刷新后正文恢复了、网址还是旧的）。
 */
function onDraftInput() {
  autosizeLegacyEditor();
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
    if (editing.value && text !== draft.value) {
      draft.value = text;
      editStartText.value = text;
    }
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
  if (props.card.kind === "file") {
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

/**
 * 文字卡 / 文件卡的「完成编辑」：**退出选中**或**再次点编辑按钮**都走这里
 * （不再有卡片里的完成 / 取消按钮行）。
 *
 * - 未决冲突：只关闭编辑器，不替用户做选择（冲突入口与两份候选都保留）；
 * - 没有真实改动：不生产一次板面变更，只把进入编辑时登记的那份草稿收回；
 * - 有改动：与旧「完成编辑」完全同一条路径（confirmEdit）。
 */
function finishEdit() {
  if (!editing.value) return;
  if (conflictPending.value) {
    cancelEdit();
    return;
  }
  // 「没有真实改动」= 正文与**适用附加字段**都没变（只打开看一眼不该生产一次板面变更）；
  // 反过来，只改了名称 / 语言 / 网址标题也必须形成一次真实变更。
  const metaChanged = JSON.stringify(currentMetaInput()) !== JSON.stringify(editStartMeta.value);
  if (draft.value === editStartText.value && !metaChanged) {
    store.requestDraftClear(cardDraftKey(props.card.id));
    cancelEdit();
    return;
  }
  confirmEdit();
}

/** 卡片工具栏的编辑按钮：文字卡 / 文件卡在编辑态变成「完成编辑」，再点一次即完成。 */
function toggleEdit() {
  if ((isTextCard.value || isFileCard.value) && editing.value) finishEdit();
  else startEdit();
}

/**
 * 退出选中 = 完成编辑：点板面空白、点别的卡片都会让本卡 selected 变 false。
 * 只处理 true → false 这一跳；未决冲突时只关编辑器（不替用户选择）。
 */
watch(
  () => props.selected,
  (selected) => {
    if (!selected && editing.value) finishEdit();
  },
);
</script>

<template>
  <article
    ref="cardEl"
    class="card"
    :class="{
      selected,
      highlight,
      dragging,
      folded: card.folded,
      hidden: card.hidden,
      reply: card.kind === 'reply',
      'is-compact': compactChrome,
      // 封面式：图片铺满整张卡，头部 / 正文 / 页脚浮在封面之上（见 CardAttachmentPreview 的 cover 变体）
      'is-cover': coverMode,
      // 图片详情：整张卡就是一张图（无普通标题栏留白，圆角裁切，见 .card.is-image-detail）
      'is-image-detail': displayState === 'image-detail',
    }"
    :style="style"
    data-im="card"
    :data-card-id="card.id"
    :data-card-state="displayState"
    @pointerdown="onPointerDown"
    :data-cover="coverMode ? '1' : undefined"
    :data-image-detail="displayState === 'image-detail' ? '1' : '0'"
    :data-image-ratio="imageDisplay ? String(imageDisplay.aspectRatio) : undefined"
  >
    <!--
      封面层：图片铺满整张卡作为背景（纯呈现，不进板面状态）。
      编辑态 / 阅读态不铺：那两个状态的主体分别是表单与真实预览。
    -->
    <CardAttachmentPreview
      v-if="coverMode"
      variant="cover"
      :desc="coverDesc"
      :compact="compactChrome"
      :kind="card.kind"
      :meta="card.meta"
      :card-id="card.id"
      @relink-attachment="pickAttachment"
    />
    <!--
      图片详情态（Phase 2-C 契约 §5.2）：主体是**一整张图**，直接铺满卡片。
      它不挂在 .card-body 里（正文层有内边距，会在图片四周留出一圈「普通标题栏」似的空白）；
      这里用 absolute inset:0 的宿主铺满卡片，圆角由卡片本身裁切。
    -->
    <div
      v-if="displayState === 'image-detail'"
      class="card-image-detail-host"
      data-im="card-image-detail-host"
      :data-card-id="card.id"
    >
      <CardInlinePreview
        :card="card"
        :active="true"
        :on-close="closeImageDetail"
        context="material"
        mode="image-detail"
        @position="onPreviewPosition"
      />
    </div>

    <!-- 图片详情态下头部不占布局高度但也**不消失**：名称 / 参与 / 收起详情浮在图上（见 CSS） -->
    <header class="card-head" data-im="card-head">
      <!-- 材料类型标签在窄 / 矮卡片里让位：标题、主要操作与必要状态优先（§3.5） -->
      <span v-if="!compactChrome" class="kind mono">{{ kindLabel }}</span>
      <span v-if="titleText" class="title" data-im="card-title" :title="titleText">{{ titleText }}</span>
      <!-- 参与讨论：所有卡片都有的小控件；与选中/书签/阅读状态无关。图片详情态同样浮在图上。 -->
      <button
        class="participation"
        :class="{ 'is-on': participationIncluded }"
        type="button"
        data-im="card-participation"
        :data-card-id="card.id"
        :data-participation="participationIncluded ? 'included' : 'excluded'"
        :aria-pressed="participationIncluded"
        :title="participationIncluded
          ? '参与讨论：QIO 本次可读取这张卡；点击退出讨论范围'
          : '不参与：QIO 本次看不到这张卡；点击重新加入讨论'"
        @pointerdown.stop
        @click.stop="emit('toggle', card.id, 'participation')"
      >{{ participationIncluded ? "参与" : "不参与" }}</button>
      <!--
        两个显示入口**互斥**，界面上不并存（任务书 §6.4）：
        折叠态只有「恢复卡片」（把卡片展开回常态）；
        常态 / 阅读态只有「展开预览 / 收起预览」（让正文或文件预览成为主体）。
        折成标题的动作在工具栏里叫「折成标题」，与这里的「展开」不是一回事。
      -->
      <button
        v-if="displayState === 'folded'"
        class="chip-btn"
        type="button"
        data-im="card-unfold"
        :data-card-id="card.id"
        title="取消折叠：恢复标题、正文与状态（不改内容）"
        @click="emit('toggle', card.id, 'folded')"
      >
        恢复卡片
      </button>
      <!--
        图片兜底入口：正常图片会随卡片尺寸自动进入详细状态，不显示按钮。
        只有极端长图/竖图当前达不到自动阈值时，才在右上角出现一个小眼睛；
        点击只改查看方式，不改内容、不改卡片尺寸。
      -->
      <button
        v-if="displayState !== 'editing' && imageFallbackVisible"
        class="chip-btn card-image-fallback"
        type="button"
        data-im="card-image-detail-toggle"
        :data-card-id="card.id"
        aria-label="查看完整图片"
        :aria-expanded="imageDetailVisible"
        :title="imageDetailTitle"
        @click="toggleImageDetail"
      >
        <svg viewBox="0 0 16 16" aria-hidden="true" focusable="false">
          <path d="M1.8 8s2.2-3.4 6.2-3.4S14.2 8 14.2 8 12 11.4 8 11.4 1.8 8 1.8 8Z" />
          <circle cx="8" cy="8" r="1.6" />
        </svg>
      </button>
    </header>

    <!--
      卡片正文是这张卡**唯一的主要纵向视口**：
      - 常态 / 编辑 / 折叠之外的阅读态，纵向滚动都落在这一层（内层预览不再各建一层）；
      - v-qio-scrollbar：实际溢出时用统一 QIO 指示条（默认隐藏、滚动时出现、不占布局列）；
      - data-qio-fit：横向边界保护（min-width:0 + max-width:100% + overflow-x:clip），
        真正的横向不溢出靠子元素的 min-width:0 与换行规则，不靠裁切。
    -->
    <div
      v-if="displayState !== 'folded' && displayState !== 'image-detail'"
      class="card-body"
      :class="{ 'card-body-editing-text': displayState === 'editing' && isTextCard }"
      data-im="card-body"
      data-qio-fit
      v-qio-scrollbar="{ policy: 'auto', axis: 'y' }"
    >
      <!-- 编辑：整张卡只有这一个连续编辑区（任务书 §6.2） -->
      <template v-if="displayState === 'editing'">
        <!--
          未决冲突的选择入口放在编辑器**上方**（契约 §12.1：冲突入口在真实操作路径可见可点）：
          卡片有固定高度、编辑区内部滚动，提示若排在编辑器之后，打开编辑器的第一屏就看不见它。
          -->
        <CardDraftHint v-if="conflictPending" :card-id="card.id" />
        <!--
          文字卡（含迁移后的旧 code / url）：**一个连续编辑区**（契约 §1.3）。
          整张卡片正文区里只有「工具栏（加粗 / 行内代码…）+ 输入框」，输入框吃满其余全部空间：
          没有说明文字、没有完成 / 取消按钮、也没有例行的「草稿保存中 / 已保存」。
          完成编辑 = 退出选中（点板面空白 / 点别的卡片）或再点一次工具栏上的编辑按钮。
          输入即把草稿登记进既有通道；草稿文本就是 meta.docText（提交载荷也用这一份）。
        -->
        <TextCardEditor
          v-if="isTextCard"
          ref="textEditor"
          :card="card"
          :value="editorText"
          :min-height="editorMinHeight"
          fill
          :data-card-id="card.id"
          @change="onTextCardChange"
        />
        <!-- 文件卡（2026-10-14 用户要求）：编辑态只有文件名，没有「待补充文件说明」这一段正文 -->
        <textarea
          v-else-if="!isFileCard"
          ref="legacyEditor"
          v-model="draft"
          class="editor"
          data-im="card-editor"
          :data-card-id="card.id"
          rows="4"
          :aria-label="'编辑' + kindLabel"
          @input="onDraftInput"
        ></textarea>
        <label v-if="card.kind === 'file'" class="field">
          <span>名称</span>
          <input v-model="metaName" type="text" data-im="card-meta-name" :data-card-id="card.id" @input="onDraftInput" />
        </label>
        <div v-if="attachKind && !isFileCard" class="attach">
          <button class="btn" type="button" data-im="card-attach-file" :data-card-id="card.id" @click="pickAttachment">
            {{ attachmentLinked ? "重新关联已保存的附件" : "关联已保存的附件" }}
          </button>
          <span class="attach-state" data-im="card-attachment-state" :data-card-id="card.id">{{ attachmentText }}</span>
          <span
            v-if="attachStatus"
            class="attach-status"
            :class="'is-' + attachStatus.tone"
            data-im="card-attach-status"
            :title="attachStatus.text"
          >
            <span
              class="attach-status-text"
              data-im="card-attach-status-text"
              role="status"
              aria-live="polite"
            >{{ attachStatus.text }}</span>
            <button
              v-if="attachStatus.retry"
              type="button"
              class="attach-status-action"
              data-im="card-attach-retry"
              @click="runAttachRetry()"
            >{{ attachStatus.retry.label }}</button>
            <button
              v-if="attachStatus.tone !== 'ok'"
              type="button"
              class="attach-status-action attach-status-close"
              data-im="card-attach-dismiss"
              aria-label="关闭提示"
              @click="dismissAttachStatus()"
            >关闭</button>
          </span>
        </div>
        <!-- 编辑态也能预览已关联的附件（接线 P2 的组件；无关联时它只给诚实文案） -->
        <!-- 文件卡的附件动作在工具栏里（卡片只留文件名），这里不再重复铺一份预览 -->
        <CardAttachmentPreview
          v-if="attachKind && !isFileCard"
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
        <!--
          「输入过程只保存草稿」与完成 / 取消按钮行只属于其它卡（图片 / 遗留 url / code）。
          文字卡与文件卡没有这些元素：完成编辑 = 退出选中或再点一次编辑按钮。
        -->
        <p v-if="!isTextCard && !isFileCard" class="draft-note">输入过程只保存草稿；点「完成编辑」才形成有效文字状态。</p>
        <!-- 草稿保存失败不能静默：状态与重试入口就近显示（C 的组件，A 的卡片接线） -->
        <CardDraftHint v-if="editDraftHintVisible" :card-id="card.id" />
        <div v-if="!isTextCard && !isFileCard" class="row">
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
        <CardDraftHint v-if="conflictPending" :card-id="card.id" />
        <div
          v-else
          class="card-subject"
          :class="{
            'is-reading': displayState === 'reading',
            'is-compact': displayState === 'reading' && !isTextCard,
          }"
          data-im="card-subject"
        >
          <!-- 关闭态也能看到、能处理的状态（本机副本没删掉 / 清除未同步 / 草稿保存失败）：就地提示 + 重试 -->
          <CardDraftHint v-if="cardDraftNeedsAttention" :card-id="card.id" />
          <!--
            文字卡按块渲染（契约 §1.7）：标题 / 列表 / 引用 / 段落 / 代码 / 链接各自外观。
            内容模型是 meta.doc（权威）；旧数据没有 doc 时由 content 兜底成一段。
          -->
          <template v-if="isTextCard">
            <div v-if="shownBlocks.length" class="content blocks" data-im="card-text-blocks">
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
          <template v-else>
            <!--
              遗留分支（契约 §1.7）：历史 code / url 卡在迁移完成前**仍能显示**，但整体不交付
              （不进提交载荷、不计入未许可统计），界面必须写明它还没迁移。
              文字卡的块序列已经渲染过正文，这里用 v-else 分流 —— 同一句话不在卡片里出现两遍。
            -->
            <p v-if="isLegacyCard" class="legacy-note" data-im="card-legacy-note" role="status">
              旧卡片，尚未迁移：{{ kindLabel }}卡将在下次加载板面时并入统一文字卡；现在不进入提交载荷。
            </p>
            <p v-if="card.kind === 'code'" class="content code mono">{{ card.content || "// 待补充代码" }}</p>
            <p v-else-if="card.kind === 'url'" class="content url">
              <a :href="href" target="_blank" rel="noreferrer" @pointerdown.stop>{{ linkTitle || href || "（还没有网址）" }}</a>
            </p>
            <!--
              结果卡正文（契约 F3）：card.content 是 Markdown **原文**，整份交给共用的
              MarkdownContent 渲染 —— 这里**不做任何 slice / 截断**（卡片正文没有长度上限；
              后端上下文里的 600 字上限只影响模型可见的上下文，且载荷里已显式带
              textTruncated 标记，不改写卡片正文）。围栏 / 列表 / 表格因此不会被截成半段。
              未选中（常态）与阅读态都走这一支；纵向滚动仍只由 .card-body 一层承担，
              MarkdownContent 自己不建滚动容器。
            -->
            <template v-else-if="isReplyCard">
              <MarkdownContent
                v-if="hasReplyBody"
                class="reply-markdown"
                data-im="card-reply-markdown"
                :source="card.content"
              />
              <p v-else class="content empty-text" data-im="card-reply-empty">{{ kindLabel }}：还没有正文</p>
            </template>
            <!-- 封面式：正文那行已经在封面底部，不在卡片里重复一遍 -->
            <!-- 文件卡：卡片本身只显示文件名（标题），不再铺一段「补充说明」 -->
            <p v-else-if="!coverMode && !isFileCard" class="content">
              <span v-if="card.content" class="desc">{{ card.content }}</span>
              <span v-else class="empty">{{ kindLabel }}：还没有补充说明</span>
            </p>
          </template>

          <!--
            附件（任务书 §6.3）：常态给缩略图与状态；阅读展开时压成一行「外部打开」入口 ——
            文件名与说明留在标题附近，主体让给真实预览，不在预览上方重复铺开大段说明。
          -->
          <!--
            文件卡（2026-10-14 用户要求）：非图片附件只在卡片里显示文件名，附件动作都在工具栏；
            但「附件是图片」的文件卡要照旧渲染（缩略图 / 状态 / 查看），否则图片卡会变成一张空卡。
          -->
          <CardAttachmentPreview
            v-if="attachKind && !coverMode && (!isFileCard || attachmentIsImage)"
            :kind="card.kind"
            :meta="card.meta"
            :card-id="card.id"
            :compact="displayState === 'reading'"
          />

          <!--
            卡片内阅读展开的挂载点（契约 §3.5）：真实附件预览挂在这里，永远在卡片**内部**，
            不盖编辑区与卡片操作。没有真实附件时不挂任何东西（任务书 §6.4：不造假预览）——
            文字卡的阅读主体就是上面那份正文，所以这里是空的（:empty 时不占画面）。
          -->
          <div
            v-if="displayState === 'reading'"
            class="card-preview-host"
            data-im="card-preview-host"
            :data-card-id="card.id"
          >
            <!--
              渲染上下文是**显式**的（契约：不用 DOM 猜测区分「对话内渲染」与「独立材料阅读」）：
              卡片内展开阅读 = 独立互动材料阅读 → material：
              图片 / PDF 放大、电子表格网格、代码预览可按材料类型保留必要的二维浏览，
              并且用统一 QIO 指示条；对话内（ChatDock 等）必须传 chat，执行无横向规则。
            -->
            <CardInlinePreview
              v-if="hasPreviewMaterial"
              :card="card"
              :active="true"
              :on-close="closeAutoDetail"
              context="material"
              @position="onPreviewPosition"
            />
          </div>
        </div>
      </template>
    </div>

    <!--
      元信息：必要状态 + 更新时间。状态在任何尺寸下都必须留着（§3.5）；
      更新时间是次级信息，窄 / 矮卡片里让位给内容区，保证内容区高度仍然 > 0。
    -->
    <footer v-if="displayState !== 'image-detail'" class="card-meta" data-im="card-meta">
      <span class="status" role="status">{{ statusText }}</span>
      <span v-if="!compactChrome" class="time mono">{{ updatedText }}</span>
    </footer>

    <!--
      卡片边框（2026-10-24 演示最终稿）：**一条** evenodd 的 SVG 路径。
      外轮廓贴住卡片圆角，内轮廓沿内法线缩进；右下角越近越粗（1px → 3px），
      平时 / 悬停 / 选中只换 fill，不再用 CSS border 与 ::after 粗条叠出第二圈线。
      纯呈现、不接管指针；尺寸随卡片 w/h（折叠态量真实高度），封面与图片详情也在最上层。
    -->
    <svg
      class="card-frame"
      data-im="card-frame"
      aria-hidden="true"
      focusable="false"
      :viewBox="'0 0 ' + frameSize.w + ' ' + frameSize.h"
      preserveAspectRatio="none"
    >
      <path
        class="card-frame-path"
        data-im="card-frame-path"
        fill-rule="evenodd"
        :d="framePath"
      />
    </svg>
  </article>

  <!--
    卡片局部工具栏：只在选中后浮出，未选中即隐藏；由所选里的最后一张卡片渲染（不叠成一摞）。
    位置由画布按「卡片真实外框 − 宿主原点」算好（left / top 都相对 toolbarHost）。
    这里只放**这张卡片自己的操作**：常用的编辑 / 折成标题留在近处，
    复制 / 隐藏 / 书签 / 整理 / 删除收进「更多」（任务书 §6.5），能力一个不少。

    Teleport 把工具栏送进 BoardCanvas 的 .card-toolbar-host —— 那一层**不在**
    .board-surface 的 transform: scale() 里，工具栏字号 / 按钮尺寸 / 间距因此不随板面缩放；
    toolbarHost 还没挂上（或本组件被单独挂载）时 Teleport 保持 disabled，工具栏就地渲染，
    绝不会跑到 (0,0) 之类的位置去。
  -->
  <Teleport :to="toolbarTeleportTarget" :disabled="!toolbarHost">
    <div
      v-if="isToolbarOwner"
      v-show="toolbarVisible"
      ref="toolbarRoot"
      class="card-toolbar"
      :style="toolbarStyle"
      data-im="card-toolbar"
      :data-card-id="card.id"
      :data-multi="multi ? '1' : '0'"
      :data-teleported="toolbarHost ? '1' : '0'"
      role="toolbar"
      :aria-label="multi ? '所选卡片的共同操作' : kindLabel + '操作'"
      @pointerdown.stop
    >
    <!-- 多选：只显示共同适用的操作，不把单卡片编辑应用到整组 -->
    <template v-if="multi">
      <button class="tb" type="button" data-im="card-fold" :data-card-id="card.id" @click="emit('toggle', card.id, 'folded')">
        {{ card.folded ? "恢复卡片" : "折成标题" }}
      </button>
      <span class="divider" aria-hidden="true"></span>
      <SelectionMenu :open-up="menuOpensUp" />
      <button class="tb danger" type="button" data-im="delete-card" :data-card-id="card.id" @click="emit('remove', card.id)">
        删除
      </button>
      <span class="tb-note">多选：只显示共同适用的操作</span>
    </template>

    <template v-else>
      <button
        class="tb"
        type="button"
        data-im="card-edit"
        :data-card-id="card.id"
        :data-editing="editing ? '1' : '0'"
        @click="toggleEdit"
      >
        {{ (isTextCard || isFileCard) && editing ? "完成编辑" : "编辑" }}
      </button>
      <!--
        结果卡「复制原文」（契约 F3 / B4）：整份 Markdown 原文进剪贴板 ——
        卡片正文里代码块自己的复制按钮只复制那一段代码，与这里是两个层级。
      -->
      <button
        v-if="isReplyCard"
        class="tb"
        type="button"
        data-im="card-copy-source"
        :data-card-id="card.id"
        :data-copy-ok="sourceCopyState ? String(sourceCopyState.ok) : ''"
        :disabled="!hasReplyBody"
        :title="copySourceTitle()"
        @click="copySource()"
      >{{ sourceCopyText() }}</button>
      <!--
        文件卡（2026-10-14 用户要求）：卡片本身只显示文件名，附件动作全部收在工具栏。
        打开走既有 openAttachment，结果如实写在旁边的状态行里。
      -->
      <button
        v-if="isFileCard"
        class="tb"
        type="button"
        data-im="card-attach-file"
        :data-card-id="card.id"
        @click="pickAttachment"
      >
        {{ attachmentLinked ? "重新关联附件" : "关联附件" }}
      </button>
      <!--
        封面显示期间（coverMode）工具栏不再渲染这个入口：封面里已经有「查看原图」，
        两个入口各自持 busy 状态、重复打开同一个附件没有意义；非封面（非图片文件卡）行为不变。
      -->
      <button
        v-if="isFileCard && fileLinked && !coverMode"
        class="tb"
        type="button"
        data-im="card-attachment-open"
        :data-card-id="card.id"
        :data-open-action="filePlan.action ?? 'none'"
        :title="filePlan.openNote"
        :disabled="fileActionBusy"
        @click="openAttachmentFromToolbar"
      >
        {{ fileActionText }}
      </button>
      <span
        v-if="isFileCard && fileToolbarStatus"
        class="tb-status"
        :class="'is-' + fileToolbarStatus.tone"
        data-im="card-file-action-status"
        role="status"
        aria-live="polite"
        :title="fileToolbarStatus.text"
      >{{ fileToolbarStatus.text }}</span>
      <button
        v-if="isFileCard && attachStatus && attachStatus.retry"
        class="tb"
        type="button"
        data-im="card-attach-retry"
        @click="runAttachRetry()"
      >{{ attachStatus.retry.label }}</button>
      <button
        v-if="isFileCard && attachStatus && attachStatus.tone !== 'ok'"
        class="tb"
        type="button"
        data-im="card-attach-dismiss"
        aria-label="关闭提示"
        @click="dismissAttachStatus()"
      >关闭</button>
      <button class="tb" type="button" data-im="card-fold" :data-card-id="card.id" @click="emit('toggle', card.id, 'folded')">
        {{ card.folded ? "恢复卡片" : "折成标题" }}
      </button>

      <span class="divider" aria-hidden="true"></span>

      <!--
        「更多」入口：面板朝卡片外侧展开（画布把工具栏放在卡片上方时向上开），不盖标题与正文。
        面板用 v-show（不是 v-if）：DOM 里保留全部低频能力，收起时只是不占视觉位置 ——
        键盘与读屏仍能到达，也不会因为「藏在 v-if 后面」而丢操作。
      -->
      <button
        class="tb"
        type="button"
        data-im="card-more"
        :data-card-id="card.id"
        aria-haspopup="menu"
        :aria-expanded="moreOpen"
        title="更多：复制、隐藏、书签、整理与删除"
        @click="toggleMore"
      >
        更多
      </button>

      <div
        v-show="moreOpen"
        ref="morePanel"
        class="more-panel"
        :class="moreOpensUp ? 'up' : 'down'"
        data-im="card-more-panel"
        role="menu"
        aria-label="更多操作"
      >
        <button
          class="tb"
          type="button"
          role="menuitem"
          data-im="card-duplicate"
          :data-card-id="card.id"
          @click="moreAction(() => emit('duplicate', card.id))"
        >复制</button>
        <button
          class="tb"
          type="button"
          role="menuitem"
          data-im="card-hide"
          :data-card-id="card.id"
          @click="moreAction(() => emit('toggle', card.id, 'hidden'))"
        >{{ card.hidden ? "取消隐藏" : "隐藏" }}</button>
        <button
          class="tb"
          type="button"
          role="menuitem"
          data-im="card-bookmark"
          :data-card-id="card.id"
          @click="moreAction(() => emit('toggle', card.id, 'bookmarked'))"
        >{{ card.bookmarked ? "取消书签" : "书签" }}</button>
        <!-- 分组类操作（成组 / 加入组 / 移出组 / 解除组 / 序号 / 合并 / 删除所选）都在整理菜单里 -->
        <SelectionMenu :open-up="menuOpensUp" />
        <button
          class="tb danger"
          type="button"
          role="menuitem"
          data-im="delete-card"
          :data-card-id="card.id"
          @click="moreAction(() => emit('remove', card.id))"
        >删除</button>
      </div>
      </template>
    </div>
  </Teleport>
</template>

<style scoped>
/*
 * 2026-10-24 用户已确认：互动板只保留“参与 / 不参与”。
 * 旧的折叠和“重新关联附件”不再出现在卡片上。
 * 数据字段暂时保留兼容，UI 一律隐藏。
 */
.card-toolbar [data-im="card-fold"],
.card-toolbar [data-im="card-attach-file"] {
  display: none !important;
}

.card {
  position: absolute;
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  padding: var(--sp-3);
  /* 卡片自身的内边距（封面模式下的页脚要按它抵消，贴到卡片最下沿） */
  --card-pad: var(--sp-3);
  overflow: hidden;
  background: var(--bg-surface);
  /* 边框不在这里：见文件顶部 cardFramePath + .card-frame（避免 border 与 SVG 环带叠成双线）。
     圆角必须与 CARD_FRAME_RADIUS 一致：改 --r-lg 时同步改那个常量。 */
  border-radius: var(--r-lg);
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
/*
  窄 / 矮卡片收一档（任务书 §3.5）：内边距与间隙各收一档，把高度让给内容区。
  最小可用尺寸 180×96（C 的 CARD_MIN_SIZE）下：标题仍在、主要操作仍可点、必要状态仍在、
  内容区高度仍 > 0（头部与页脚 flex: none，正文 flex: 1 并 min-height: 0）。
*/
.card.is-compact { padding: var(--sp-2); gap: var(--sp-1); }

/*
  封面式图片卡（2026-10-14 第六个问题）：图片铺满整张卡（CardAttachmentPreview 的 cover 变体）。
  绝对定位的封面按**内边距盒**解析 inset:0，所以卡片自身的内边距不用改，封面天然满铺；
  头部 / 正文 / 页脚只需要透明并浮到封面之上，文字可读性由封面里的上下蒙版保证。
  这里刻意**不重复声明** .card-head / .card-body / .card-meta：源码守卫按选择器取 CSS 块，
  重复声明会把真正的规则挤掉（第一次全量就是这样挂了 4 条守卫）。
*/
.card.is-cover {
  overflow: hidden;
}
.card.is-cover > :not(.card-cover):not(.card-frame) {
  position: relative;
  z-index: 1;
  background: transparent;
}


/*
  卡片边框：只有这一条 evenodd 的 SVG 环带（2026-10-24 交互演示最终稿）。
  - .card 自身不再画 border —— 两套笔画叠在一起就是「双线 / 断角」的来源；
  - 环带由组件顶部的 cardFramePath 生成：外轮廓贴住卡片圆角，内轮廓往里缩，
    右下角越近越粗（1px → 3px），整圈没有第二段描边；
  - 颜色只换 fill：平时 --border-subtle，悬停 --border-strong，选中 --accent；
  - pointer-events: none：边框是纯呈现，不吃卡片的选择 / 拖动 / 点击。
*/
.card-frame {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  overflow: visible;
  pointer-events: none;
  z-index: 3;
}
.card-frame-path {
  fill: var(--border-subtle);
  transition: fill var(--dur-fast) var(--ease-1);
}
/* 结果卡沿用 link 色，隐藏卡弱化到半透明：都是换 fill / 透明度，不是第二圈线 */
.card.reply .card-frame-path { fill: var(--link); }
.card.hidden .card-frame-path { opacity: 0.55; }
.card:hover .card-frame-path { fill: var(--border-strong); }
.card.selected .card-frame-path { fill: var(--accent); }
.card.selected:hover .card-frame-path { fill: var(--accent); }
.card.dragging .card-frame-path { fill: var(--accent); }
.card.selected {
  background: var(--bg-accent-subtle);
}
.card.highlight { outline: 2px dashed var(--warning); outline-offset: 2px; }
.card.dragging { cursor: grabbing; opacity: 0.92; }
.participation {
  flex: none;
  height: 22px;
  padding: 0 7px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-pill);
  background: var(--bg-elevated);
  color: var(--text-muted);
  font-family: var(--sans);
  font-size: 10px;
  cursor: pointer;
}
.participation.is-on {
  color: var(--link);
  border-color: var(--accent);
  background: var(--accent-soft);
}
.participation:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 1px; }


/*
  第一层：材料类型 + 标题 + 参与控件 + 阅读展开入口。
  一行到底、不换行也不换三行：标题自己用省略号收尾，操作与状态永远留在原位。
*/
.card-head {
  display: flex;
  flex: none;
  align-items: center;
  flex-wrap: nowrap;
  gap: var(--sp-2);
  min-width: 0;
}
.card.is-compact .card-head { gap: var(--sp-1); }
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
  flex: 1 1 auto;
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
  border-radius: var(--r-lg);
  padding: 0 var(--sp-1);
  cursor: pointer;
}
.chip-btn:hover { color: var(--text-strong); border-color: var(--border-strong); }
.chip-btn:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 1px; }

/*
  第二层：正文 —— 这张卡**唯一的主要纵向视口**。

  纵向：只有这一层滚动（v-qio-scrollbar, policy auto）。内层的卡片内预览、代码块
  不再各建一层纵向滚动（原来 card-body → cip-body → .cip-pre 是三层，用户看到的
  是「同一个阅读动作要在三个小窗口里分别滚」）。展开阅读时，真实预览由 flex 定高，
  这一层自然没有可滚范围，于是纵向视口恰好剩下预览自己那一个。

  横向：overflow-x 仍然是 hidden（边界保护；QIO 自有界面里不出现横向滚动条），
  但**不是**靠它来「修」横向溢出 —— 每个子块都有 min-width: 0 + 换行规则
  （.content / .blk 的 overflow-wrap: anywhere，.code-body 的软换行），
  真正的横向溢出在布局上就不存在。data-qio-fit 另外补上 min-width/max-width 与
  overflow-x: clip（见 styles/scroll.css）。
  卡片自己的 overflow: hidden 保证任何意外都不会把卡片撑宽（任务书 §3.3）。
*/
/*
  文字卡编辑态：正文区变成 flex 列，编辑区吃满它，编辑区里的输入框再吃满剩下的高度 ——
  卡片正文区里只有工具栏与输入框（2026-10-14 用户要求：输入框占满整张卡片）。
*/
.card-body-editing-text {
  display: flex;
  flex-direction: column;
  min-height: 0;
}
.card-body {
  display: flex;
  flex-direction: column;
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  overflow-x: hidden;
}
.content {
  margin: 0;
  font-size: var(--fs-base);
  line-height: var(--lh-base);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  word-break: break-word;
}
/*
  遗留 code 卡的正文（迁移完成前仍要能读）：与 .code-body 同一条规则 ——
  长行软换行，不靠裁切把内容吃掉（white-space: pre 会横向溢出后被卡片边界裁掉）。
*/
.content.code {
  font-family: var(--mono);
  font-size: var(--fs-sm);
  line-height: var(--lh-tight);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  word-break: normal;
}

/* ---- 统一文字卡的块渲染（契约 §1.7）：每类块有自己的外观，但仍是一张卡的正文 ---- */
/*
  文字卡的正文容器同时带 .content（「卡片正文」这一层）与 .blocks（块布局）。
  容器不套用 .content 的 pre-wrap：块自己负责空白与换行，保持既有外观。
*/
.blocks {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  min-width: 0;
  white-space: normal;
}
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
  border-radius: var(--r-lg);
  padding: 0 2px;
}
/*
  代码块：语言 + 复制按钮在最上面一行。
  纵向滚动责任不在它身上：整段代码跟随卡片正文这一层（.card-body）滚 ——
  代码块自己再有纵向滚动，就会和正文滚成两个小窗口。
  横向也不滚动：长行按视觉软换行（见 .code-body），textContent 一个字符都不变，
  复制出来仍然逐字一致。外框只做圆角裁切（overflow: clip 不建立滚动容器）。
*/
.blk-code {
  margin: 0;
  min-width: 0;
  max-width: 100%;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-lg);
  background: var(--bg-inset);
  overflow: clip;
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
  border-radius: var(--r-lg);
  padding: 0 var(--sp-2);
  cursor: pointer;
}
.code-copy:hover { color: var(--text-strong); border-color: var(--border-strong); }
.code-copy:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 1px; }
/*
  代码正文：**不自己滚动**（横向与纵向都不）。
  - 纵向跟随卡片正文这一层，不再形成第二个纵向小窗口；
  - 横向按视觉软换行：长行折行显示而不是被裁掉或藏进横向滚动条里，
    原文的换行与缩进都保留（white-space: pre-wrap 只改呈现），
    textContent / 复制结果逐字不变。
*/
.code-body {
  margin: 0;
  max-width: 100%;
  padding: var(--sp-2);
  font-size: var(--fs-sm);
  line-height: var(--lh-tight);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  word-break: normal;
  overflow: visible;
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
  border-radius: var(--r-lg);
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
  border-radius: var(--r-lg);
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
  border-radius: var(--r-lg);
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
  border-radius: var(--r-lg);
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
  border-radius: var(--r-lg);
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
/* 状态里还带「重新选择 / 关闭」按钮，自己排一行；长文件名换行 / 断词，完整名称看 title */
.attach-status {
  flex: 1 1 100%;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--sp-2);
  line-height: var(--lh-tight);
}
.attach-status-text { min-width: 0; overflow-wrap: anywhere; word-break: break-word; }
.attach-status-action {
  flex: 0 0 auto;
  font: inherit;
  font-size: var(--fs-xs);
  color: var(--link);
  background: var(--bg-elevated);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-lg);
  padding: 0 var(--sp-2);
  cursor: pointer;
  white-space: nowrap;
}
.attach-status-action:hover { border-color: var(--border-strong); color: var(--text-strong); }
.attach-status-action:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 1px; }
.attach-status-close { color: var(--text-muted); }
.attach-status.is-busy { color: var(--text-muted); }
.attach-status.is-ok { color: var(--text-secondary); }
.attach-status.is-error { color: var(--danger, var(--text-strong)); }

/* 第四层：元信息（状态文字 + 时间）。比正文小一档，但仍是可读文本，不用装饰级颜色。 */
.card-meta {
  display: flex;
  flex: none;
  align-items: baseline;
  gap: var(--sp-2);
  min-width: 0;
  font-size: var(--fs-xs);
  color: var(--text-muted);
}
.status { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.time { flex: none; color: var(--text-faint); }

/*
  卡片内主体（任务书 §6.1 / §6.3）：常态是简短内容与缩略图；
  阅读展开时同一个容器升级成阅读主体 —— 正文只渲染一份，
  预览上方不再重复铺开文件摘要、缩略图与大段说明。挂载点始终在卡片**内部**，
  不是浮层：不盖编辑区、不盖连接点、不盖局部工具栏。
*/
.card-subject {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  min-width: 0;
  min-height: 0;
}
/* 阅读展开：正文成为主体（行距放宽，长文更好读）；真实预览在这里占满剩余空间 */
.card-subject.is-reading { flex: 1 1 auto; gap: var(--sp-3); }
/*
  预览挂载点：有真实附件时占满卡片剩余空间；文字卡没有可挂的东西时它是空的，
  不占任何画面（:empty —— Vue 在 v-if 为假时只留注释节点，不影响 :empty 匹配）。
*/
.card-preview-host {
  display: flex;
  flex-direction: column;
  flex: 1 1 auto;
  min-width: 0;
  min-height: 0;
}
.card-preview-host:empty { display: none; }
/*
  结果卡正文（F3 / B3）：Markdown 渲染器自己**不带**滚动容器，这里只保证它受卡片正文
  宽度约束；纵向滚动仍然只由 .card-body 这一层承担。阅读态的行距与行宽跟文字卡同一口径
  （排版规则在 MarkdownContent 里，只有一份，不在这里重写第二套）。
*/
.reply-markdown { min-width: 0; max-width: 100%; }
.card-subject.is-reading .blk { line-height: var(--lh-loose); }
.card-subject.is-reading .blk-paragraph,
.card-subject.is-reading .blk-list,
.card-subject.is-reading .blk-quote { max-width: var(--measure); }
/* 材料卡阅读展开：说明压到两行以内，主体让给预览（文件名与状态留在标题附近） */
.card-subject.is-compact .desc {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.preview-placeholder {
  margin: 0;
  padding: var(--sp-3);
  border: 1px dashed var(--border-subtle);
  border-radius: var(--r-lg);
  color: var(--text-muted);
  font-size: var(--fs-xs);
  line-height: var(--lh-base);
}

/*
  连接点（data-im="connect-point"）**不在这里**：0.2.5 起由 BoardCanvas 的交互层
  （BoardResizeLayer，不随画布缩放）渲染 —— 卡片内部处在 .board-surface 的缩放层叠上下文里，
  四边中点会被同层的 10px 尺寸热区盖住，真实鼠标按不下去。见 BoardResizeLayer.vue 的说明。

  局部工具栏：浮在板面上，不随卡片缩放，位置由父组件换算。
  按钮字号用 --fs-sm：编辑 / 删除这些是主要操作，不缩成元信息字号。
*/
.card-toolbar {
  position: absolute;
  z-index: 60;
  /*
    宿主（.card-toolbar-host）整层 pointer-events: none：工具栏这一块自己收回事件，
    只有工具栏与已打开的面板 / 菜单接收指针，其余地方照旧穿透到板面。
  */
  pointer-events: auto;
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--sp-1);
  max-width: min(560px, calc(100vw - var(--sp-4)));
  padding: var(--sp-1) var(--sp-2);
  background: var(--bg-elevated);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-lg);
  box-shadow: var(--elev-floating, var(--shadow-2));
  font-size: var(--fs-sm);
}
/*
  「更多」面板：朝卡片外侧展开（工具栏在卡片上方时向上开），不盖标题、连接点与正文。
  面板不做裁切（overflow 可见），否则嵌在里面的「整理」面板会被裁掉。
*/
.more-panel {
  position: absolute;
  right: 0;
  top: calc(100% + var(--sp-1));
  z-index: 4;
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
  min-width: 132px;
  padding: var(--sp-1);
  background: var(--bg-elevated);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-lg);
  box-shadow: var(--elev-floating, var(--shadow-2));
}
.more-panel.up { top: auto; bottom: calc(100% + var(--sp-1)); }
.more-panel .tb { text-align: left; }
.divider { width: 1px; height: 16px; background: var(--border-subtle); flex: none; }
.tb {
  font: inherit;
  font-size: var(--fs-sm);
  color: var(--text-secondary);
  background: var(--bg-surface);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-lg);
  padding: 1px var(--sp-2);
  cursor: pointer;
  white-space: nowrap;
}
.tb:hover { color: var(--text-strong); border-color: var(--border-strong); }
.tb:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 1px; }
.tb.danger { color: var(--danger); }
.tb-note { color: var(--text-muted); white-space: nowrap; }
/* 文件卡工具栏里的动作状态：只在真有话要说时占位（结果如实显示，不谎报成功） */
.tb-status {
  max-width: 320px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: var(--fs-xs);
  color: var(--text-muted);
}
.tb-status.is-ok { color: var(--text-secondary); }
.tb-status.is-error { color: var(--danger, var(--text-strong)); }

/*
  封面之上的两条**窄蒙版**（本轮：不再用封面里上下各 42% 的大蒙版）：
  头部（标题 / 参与控件 / 折叠入口）与页脚（必要状态 / 更新时间）各贴一条，
  文字压在图片上时始终清晰可读。规则放在样式表末尾：前面 .card-head / .card-meta
  的基线规则不能被挤掉（源码守卫按选择器取第一个 CSS 块）。
*/
.card.is-cover > .card-head,
.card.is-cover > .card-meta {
  background: color-mix(in srgb, var(--bg-surface) 88%, transparent);
  border-radius: var(--r-lg);
  padding: 0 var(--sp-1);
}

/*
  封面模式下的**命中层叠**（可达性回归修复）。

  现象：elementFromPoint(封面「查看原图」按钮中心) === [data-im="card-body"]，真实鼠标点不动；
  根因：上面 `.card.is-cover > :not(.card-cover) { z-index: 1 }` 让 .card-body / .card-head /
  .card-meta 都画在 .card-cover（z-index: 0）之上，而 .card-body 是**可命中的空容器**，
  于是把这轮唯一的打开入口整个挡住了（DOM 直击 .click() 正常，纯层叠命中问题）。

  规则：封面模式下正文层与头部 / 页脚**让开指针** —— 事件穿到封面按钮，其余地方穿到卡片本身
  （选中 / 拖动 / 双击手势照旧）；只对**真的需要交互**的子树显式放行：
    * 正文层里的草稿 / 冲突提示（契约 §12.1：可见可点）；
    * 头部里真正的按钮（折叠态「恢复卡片」等在 .chip-btn 上）。
  **不**把 .card-body 压到封面后面：那会让草稿 / 冲突提示被图片盖住。
*/
.card.is-cover > .card-body { pointer-events: none; }
.card.is-cover > .card-body :deep(.card-draft-hint) { pointer-events: auto; }
.card.is-cover > .card-head { pointer-events: none; }
.card.is-cover > .card-meta { pointer-events: none; }
.card.is-cover > .card-head .chip-btn { pointer-events: auto; }

/*
  封面模式：页脚（状态 / 时间）贴到卡片**最下沿**。
  页脚原本按内容盒排，卡片的下内边距会让它离底边差一截 —— 底边那条露出的是原图，
  看起来就是「时间条没到底」。这里用等量的负下外边距把它压到内边距盒的最下沿；
  .card.is-cover 本身 overflow: hidden，负外边距不会溢出卡片。
  （尺寸跟随 --card-pad：普通卡片 12px、紧凑卡片 8px。）
*/
.card.is-cover > .card-meta {
  margin-bottom: calc(var(--card-pad) * -1);
  padding: 2px var(--sp-2);
  border-radius: 0;
}
/* 紧凑档的卡片内边距是 --sp-2，负外边距跟着换一档（只作用在封面页脚，不动那条被守卫锁定的紧凑规则） */
.card.is-compact.is-cover > .card-meta {
  --card-pad: var(--sp-2);
}

/*
  图片详情态（Phase 2-C 契约 §5.2）：整张卡就是一张图。
  - 去掉卡片内边距：否则图片四周会留出一圈看起来像「普通标题栏」的空白；
  - 圆角裁切：卡片 overflow: hidden + 宿主 border-radius: inherit；
  - 宿主 absolute inset:0 铺满卡片，图片在宿主里等比完整显示（contain）；
  - 卡片尺寸仍由 w/h 决定：这里只改外观，不动几何（§16.3 / §17.1）。
*/
.card-image-fallback {
  width: 26px;
  height: 22px;
  padding: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
.card-image-fallback svg {
  width: 15px;
  height: 15px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.4;
  stroke-linecap: round;
  stroke-linejoin: round;
}
.card.is-image-detail {
  padding: 0;
  overflow: hidden;
}
/*
  详情态的头部：**浮在图上**而不是占一行布局高度（这就是「不保留普通标题栏留白」）——
  名称、参与控件、收起详情仍在原位可读可点（参与控件后续也挂在头部），
  底部一条窄渐变保证文字压在图片上也清楚；连接点与缩放手柄在卡片外侧，不受影响。
*/
.card.is-image-detail > .card-head {
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  z-index: 2;
  padding: var(--sp-2) var(--sp-3);
  background: linear-gradient(
    to bottom,
    color-mix(in srgb, var(--bg-surface) 88%, transparent),
    transparent
  );
  pointer-events: none;
}
.card.is-image-detail > .card-head .chip-btn,
.card.is-image-detail > .card-head .participation {
  pointer-events: auto;
}
.card-image-detail-host {
  position: absolute;
  inset: 0;
  border-radius: inherit;
  overflow: hidden;
}
</style>
