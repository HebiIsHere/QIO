<script setup lang="ts">
/**
 * 卡片内预览的主体（契约 §3.5：**一张已展开卡片**的预览区，不是弹出窗口）。
 *
 * 它只做三件事：
 *   1. 按 previewPlan 的结果决定"内嵌渲染 / 只能外部打开 / 这个环境看不了"；
 *   2. 内嵌渲染时拉数据、**动态 import** 对应渲染器（不用 PDF 就不加载 pdfjs-dist）；
 *   3. 收起 / 换附件 / 卸载时**真的停掉**在途工作：AbortController + 代次计数，
 *      释放 blob URL 与 canvas、销毁 pdf 文档对象，迟到结果一律丢弃。
 *
 * 三条诚实规则（契约 §0）：
 *   * 预览 ≠ 模型已读：展开、翻页、缩放都不触发模型调用、不产生表达与意图；
 *   * 结构化预览（docx/xlsx/pptx）不再在界面上写「不是原版面」那一行（2026-10-14 用户要求删除）；
 *   * 失败 / 不支持时给**真实原因**与打开/下载入口，**不给假按钮**。
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import type { BoardCard } from "../../interactive/types";
import {
  cardAttachmentId,
  cardFileName,
  cardSizeBytes,
  previewPlan,
  previewRefFor,
  previewStatusText,
} from "../../interactive/previewPlan";
import type { PreviewAction, PreviewRenderState } from "../../interactive/previewPlan";
import {
  PreviewAbortedError,
  downloadAttachment,
  fetchAttachmentBytes,
  fetchAttachmentContent,
  fetchAttachmentPreview,
  isDesktopShell,
  openAttachment,
} from "../../services/attachments";
import type { PreviewEnvelope, PreviewPosition } from "../../services/attachments";
import { disposeRenderer, loadRenderer } from "../../interactive/renderers";
import type { RenderResult } from "../../interactive/renderers/types";
import type { CodeView } from "../../interactive/renderers/code";
import type { DocxView } from "../../interactive/renderers/docx";
import type { PptxView } from "../../interactive/renderers/pptx";
import type { XlsxView } from "../../interactive/renderers/xlsx";
import type { PdfFactsView, PdfHandle } from "../../interactive/renderers/pdf";
// 只保留 PDF 仍在用的缩放步进（图片不再做卡片内阅读，fitScale 已不再需要）。
import { stepZoom } from "../../interactive/renderers/image";
// Markdown 文件卡片（2026-10-14 用户要求）：.md 按正文解析，不再逐行显示原文
import MarkdownContent from "../MarkdownContent.vue";
// 二维浏览区（图片框 / PDF 页面框 / 表格网格 / 代码框）统一用共享指示条，不各自发明滚动条样式。
import { vQioScrollbar } from "../../composables/useQioScrollbars";
// 横向自检（溢出 + scrollLeft 真能移动，两项缺一不可）：见 ./horizontalFit.ts
import { checkHorizontalFit, type HorizontalFitReport } from "./horizontalFit";
// 图片比例（Phase 2-C 冻结字段 meta.display）：只读取，不在渲染路径里猜比例
import { readImageDisplay } from "../../interactive/renderers/imageDisplay";

/**
 * 渲染上下文（**显式属性**，不靠 DOM 结构或祖先类名猜测）：
 *
 * - `"material"` —— 独立互动材料阅读（板面卡片内展开阅读）：
 *   图片 / PDF 放大、电子表格网格、代码预览可按材料类型保留必要的二维浏览，
 *   二维浏览区使用统一 QIO 指示条；
 * - `"chat"` —— 对话内渲染（互动对话、消息里的附件预览）：
 *   执行无横向规则：data-qio-fit + 软换行 + 表格按可用宽度重排，
 *   不建任何内层滚动容器（纵向滚动交给对话主体那一层）。
 *
 * 不给默认值：调用方必须明确写出自己处在哪一种上下文里。
 */
const props = withDefaults(
  defineProps<{
    card: BoardCard;
    active: boolean;
    onClose: () => void;
    context: "material" | "chat";
    /**
     * 呈现模式（Phase 2-C）：
     * - `reading`：既有卡片内阅读（图片仍不在卡片内阅读，保留诚实说明）；
     * - `image-detail`：图片**详情态** —— 图片铺满卡片、圆角裁切、等比完整显示
     *   （contain：不裁主体、不拉伸），不保留普通标题栏高度，名称与关闭入口浮在图上。
     *   它与契约 §5.2 的「详细状态」对应；普通文件卡不传，行为一字不变。
     */
    mode?: "reading" | "image-detail";
  }>(),
  { mode: "reading" },
);
/**
 * 阅读位置必须**带着归属**一起交出去：位置按「附件 + 文件版本」落库，
 * 只给一个页码，父组件无法判断它属于哪一份文件（旧版本的位置绝不能写到新版本上）。
 */
export interface PreviewPositionItem {
  attachmentId: string;
  version: string;
  position: PreviewPosition;
}

const emit = defineEmits<{ position: [item: PreviewPositionItem] }>();

const renderState = ref<PreviewRenderState>({ status: "idle" });
/** 当前这一份文件在服务端的版本指纹（位置归属要用它，见 PreviewPositionItem）。 */
const fileVersion = ref("");
const data = ref<Record<string, unknown> | null>(null);
const viewError = ref("");
const actionNote = ref("");
/**
 * 图片不再做卡片内阅读（2026-10-15 图片展示简化）时给出的可读说明。
 *
 * 它只在「极端情况下仍被要求渲染 renderer==="image"」时出现：不静默空白，
 * 给出说明 + previewPlan 的既有外部打开 / 下载动作（诚实、不假成功），
 * 也不恢复缩放 / 阅读能力，更不新增第二套图片查看器。
 */
const IMAGE_INLINE_BLOCKED_REASON =
  "图片不在卡片内展开阅读：这里不渲染画面，请用下面的入口打开或下载副本查看（预览不等于 QIO 已读到图片内容）。";
const inlineBlockedReason = ref("");

const zoom = ref(1);

const pdfFacts = ref<PdfFactsView | null>(null);
const pdfPages = ref(0);
const pdfPage = ref(1);
const pdfBusy = ref(false);
const canvasRef = ref<HTMLCanvasElement | null>(null);

const docxView = ref<DocxView | null>(null);
const xlsxView = ref<XlsxView | null>(null);
const pptxView = ref<PptxView | null>(null);
const codeView = ref<CodeView | null>(null);
const sheetIndex = ref(0);
const slideIndex = ref(0);

const attachmentId = computed(() => cardAttachmentId(props.card));
const fileName = computed(() => cardFileName(props.card) || "未命名附件");
/**
 * 图片详情态（Phase 2-C）：只在调用方显式传 mode="image-detail" 且这张卡确实是图片附件时成立。
 * 普通文件卡 / 对话内渲染都不受影响。
 */
const imageDetailMode = computed(() => props.mode === "image-detail");
/** 卡片 meta.display 的真实比例（没有就是 null，界面不猜、不写 0）。 */
const cardDisplay = computed(() => readImageDisplay((props.card.meta ?? {}) as Record<string, unknown>));
const imageShapeText = computed(() => {
  const display = cardDisplay.value;
  return display ? display.width + "×" + display.height + " 像素（原比例）" : "原图比例未知（按完整画面显示，不裁切）";
});

/*
 * 图片详情态的字节加载（Phase 2-C）。
 *
 * 三条硬约束：
 * 1. 身份核对：附件 id、卡片 id、附件版本三项都要与这次请求发起时一致，
 *    任何一项变了都丢弃结果 —— 旧请求不覆盖新图；
 * 2. 释放：换附件 / 收起 / 卸载时 revoke 当前 blob URL（只持有一个）；
 * 3. 失败如实说：读不出字节就给原因与重试入口，不假装加载中。
 */
const imageDetailUrl = ref("");
const imageDetailError = ref("");
const imageDetailLoading = ref(false);
let imageSeq = 0;
let imageObjectUrl: string | null = null;

function releaseImageDetail(): void {
  imageSeq += 1;
  if (imageObjectUrl) {
    try {
      URL.revokeObjectURL(imageObjectUrl);
    } catch {
      /* 已经释放过：释放失败不该打断收起 */
    }
  }
  imageObjectUrl = null;
  imageDetailUrl.value = "";
  imageDetailLoading.value = false;
}

function imageVersionKey(): string {
  const meta = (props.card.meta ?? {}) as Record<string, unknown>;
  const file = meta.file && typeof meta.file === "object" ? (meta.file as Record<string, unknown>) : {};
  return String(file.version ?? file.attachmentVersion ?? meta.version ?? "");
}

async function loadImageDetail(): Promise<void> {
  const id = attachmentId.value;
  if (!imageDetailMode.value || !props.active || !id) {
    releaseImageDetail();
    return;
  }
  releaseImageDetail();
  const mine = imageSeq;
  const cardId = props.card.id;
  const version = imageVersionKey();
  imageDetailLoading.value = true;
  imageDetailError.value = "";
  try {
    const blob = await fetchAttachmentContent(id);
    // 身份 / 版本 / 卡片存在三项核对（缺一项就丢弃，绝不写进另一张卡）
    if (
      mine !== imageSeq ||
      !props.active ||
      cardId !== props.card.id ||
      id !== attachmentId.value ||
      version !== imageVersionKey()
    ) {
      return;
    }
    const create = (URL as unknown as { createObjectURL?: (value: Blob) => string }).createObjectURL;
    if (typeof create !== "function") {
      imageDetailError.value = "这个环境不支持显示图片：可以打开或下载原图";
      return;
    }
    const url = create(blob);
    if (mine !== imageSeq) {
      try {
        URL.revokeObjectURL(url);
      } catch {
        /* 同上 */
      }
      return;
    }
    imageObjectUrl = url;
    imageDetailUrl.value = url;
  } catch (err) {
    if (mine !== imageSeq) return;
    if (isAbort(err)) return;
    imageDetailError.value = "原图没有读出来：" + messageOf(err) + "（可以重试）";
  } finally {
    if (mine === imageSeq) imageDetailLoading.value = false;
  }
}

function retryImageDetail(): void {
  void loadImageDetail();
}
const sizeBytes = computed(() => cardSizeBytes(props.card));
const plan = computed(() =>
  previewPlan(props.card, renderState.value, { desktop: isDesktopShell() }),
);
const statusText = computed(() => previewStatusText(renderState.value, sizeBytes.value));
const loading = computed(() => renderState.value.status === "loading" || renderState.value.status === "idle");
const inlineReady = computed(
  () => plan.value.mode === "inline" && renderState.value.status === "ready" && !inlineBlockedReason.value,
);
/** 不能内嵌时显示的原因：图片的诚实降级说明优先，其余用 previewPlan 给的真实原因。 */
const externalReason = computed(() => inlineBlockedReason.value || plan.value.reason);
/**
 * Markdown 正文：后端给的是 `code` 形状的文本行（.md 的分类没变），
 * 拼回原文后交给 MarkdownContent 的 remark 管线解析 —— 只有一处解析实现。
 */
const markdownSource = computed(() => (codeView.value ? codeView.value.lines.map((line) => line.text).join("\n") : ""));
const currentSheet = computed(() => xlsxView.value?.sheets[sheetIndex.value] ?? null);
const currentSlide = computed(() => pptxView.value?.slides[slideIndex.value] ?? null);
const zoomPercent = computed(() => Math.round(zoom.value * 100) + "%");

/**
 * 横向自检（复核钩子）。
 *
 * 验收要的是「不横向溢出」，而不是「源码里写了 overflow: hidden」：
 * 这里按**真实渲染结果**量一次（口径见 ./horizontalFit.ts：内容溢出 + scrollLeft 真的能移动，
 * 两项缺一不可），结论同时挂在根节点属性 data-qio-h-fit 上，浏览器复核可以直接读它，
 * 也可以调用暴露出来的 verifyHorizontalFit() 拿完整报告。
 *
 * - chat：外层与查看区都不许溢出（溢出即不通过，哪怕滚动条被藏起来）；
 * - material：外层不许溢出；查看区允许二维浏览，但「溢出又动不了」= 内容被裁掉，同样不通过。
 */
const rootRef = ref<HTMLElement | null>(null);
const horizontalFit = ref<HorizontalFitReport | null>(null);
const horizontalFitState = computed<"unknown" | "ok" | "overflow" | "clipped">(() => {
  const report = horizontalFit.value;
  if (!report) return "unknown";
  if (report.clipped.length) return "clipped";
  if (!report.ok) return "overflow";
  return "ok";
});

function verifyHorizontalFit(): HorizontalFitReport | null {
  const report = checkHorizontalFit(rootRef.value, props.context);
  horizontalFit.value = report;
  return report;
}

/** 是否独立材料阅读（只有这一种上下文允许按材料类型做二维浏览）。 */
const materialContext = computed(() => props.context === "material");

/**
 * 纵向滚动责任落在谁身上（**每个渲染器明确一次**，不允许层层各建视口）：
 *
 * - `"content"`（对话内）：一个滚动容器都不建，内容自然展开，滚动交给对话主体那一层；
 * - `"self"`    （材料 + 文档/幻灯片/外部打开）：预览正文这一层是唯一视口；
 * - `"viewer"`  （材料 + PDF/表格/代码）：材料自带的二维浏览区是唯一视口
 *                （默认适应区域 / 网格自适应，只有真实超出时才滚动）。
 *                （图片不再内嵌阅读，所以不在这份名单里。）
 */
type PreviewViewport = "content" | "self" | "viewer";
const VIEWER_RENDERERS: readonly string[] = ["pdf", "xlsx", "code"];
const bodyViewport = computed<PreviewViewport>(() => {
  if (props.context !== "material") return "content";
  return VIEWER_RENDERERS.includes(plan.value.renderer) ? "viewer" : "self";
});

// ---------------------------------------------------------------------------
// 在途工作与释放：代次计数 + AbortController（迟到结果一律丢弃）
// ---------------------------------------------------------------------------

let generation = 0;
let controller: AbortController | null = null;
let pdfHandle: PdfHandle | null = null;

function isAbort(err: unknown): boolean {
  if (err instanceof PreviewAbortedError) return true;
  const name = err instanceof Error ? err.name : "";
  return name === "PdfAbortedError" || name === "AbortError";
}

function messageOf(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

async function releasePdf(): Promise<void> {
  const handle = pdfHandle;
  pdfHandle = null;
  if (handle) {
    try {
      await handle.destroy();
    } catch {
      /* 已销毁 / 已中止：释放失败不该打断收起 */
    }
  }
  const canvas = canvasRef.value;
  if (canvas) {
    try {
      canvas.getContext?.("2d")?.clearRect?.(0, 0, canvas.width, canvas.height);
    } catch {
      /* jsdom 里没有真实 2D 上下文：清不掉就算了 */
    }
    canvas.width = 0;
    canvas.height = 0;
  }
}

function resetView(): void {
  void releasePdf();
  releaseImageDetail();
  data.value = null;
  viewError.value = "";
  actionNote.value = "";
  inlineBlockedReason.value = "";
  pdfFacts.value = null;
  docxView.value = null;
  xlsxView.value = null;
  pptxView.value = null;
  codeView.value = null;
  pdfPages.value = 0;
  pdfPage.value = 1;
  sheetIndex.value = 0;
  slideIndex.value = 0;
  zoom.value = 1;
}

/** 收起 / 换附件 / 卸载：中止在途请求与渲染，释放一切资源。 */
function stop(): void {
  generation += 1;
  controller?.abort();
  controller = null;
  // 结构化渲染器的一次性资源（有就释放；没有 dispose 的模块不受影响）
  const renderer = plan.value.renderer;
  if (renderer !== "none") disposeRenderer(renderer);
  resetView();
  renderState.value = { status: "idle" };
}

function applyEnvelope(envelope: PreviewEnvelope): void {
  fileVersion.value = String(envelope.version || "");
  renderState.value = {
    status: envelope.status,
    kind: envelope.kind,
    reason: envelope.reason,
    truncated: envelope.truncated,
    version: envelope.version,
  };
  data.value = envelope.data;
}

// ---------------------------------------------------------------------------
// 开始一次预览
// ---------------------------------------------------------------------------

async function start(): Promise<void> {
  resetView();
  const id = attachmentId.value;
  if (!id || !props.active) {
    renderState.value = { status: "idle" };
    return;
  }
  const mine = ++generation;
  const ctrl = new AbortController();
  controller = ctrl;
  renderState.value = { status: "loading" };
  try {
    const envelope = await fetchAttachmentPreview(id, { signal: ctrl.signal });
    if (mine !== generation) return; // 已收起 / 已换附件：迟到结果丢弃
    applyEnvelope(envelope);
    if (renderState.value.status !== "ready") return;
    const renderer = plan.value.renderer;
    if (renderer === "none") return;
    if (renderer === "image") {
      if (imageDetailMode.value) {
        // 图片详情态（Phase 2-C）：真实字节 → blob URL → 等比完整铺满卡片
        await loadImageDetail();
        return;
      }
      // 图片不再做卡片内阅读：给出可读说明，外部打开 / 下载动作照旧（不静默空白、不假成功）
      inlineBlockedReason.value = IMAGE_INLINE_BLOCKED_REASON;
      return;
    }
    if (renderer === "pdf") {
      await mountPdf(mine, ctrl);
      return;
    }
    await mountStructured(renderer, mine);
  } catch (err) {
    if (mine !== generation) return;
    if (isAbort(err)) return; // 主动中止不是失败
    renderState.value = { status: "failed", reason: messageOf(err) };
  }
}

async function mountStructured(renderer: string, mine: number): Promise<void> {
  const module = await loadRenderer<{
    view: (value: unknown) => RenderResult<unknown>;
  }>(renderer);
  if (mine !== generation) return;
  const result = module.view(data.value);
  if (mine !== generation) return;
  if (!result.ok) {
    viewError.value = result.reason;
    return;
  }
  if (renderer === "docx") docxView.value = result.view as DocxView;
  else if (renderer === "xlsx") xlsxView.value = result.view as XlsxView;
  else if (renderer === "pptx") pptxView.value = result.view as PptxView;
  else if (renderer === "code" || renderer === "markdown") codeView.value = result.view as CodeView;
}

async function mountPdf(mine: number, ctrl: AbortController): Promise<void> {
  const module = await loadRenderer<{
    view: (value: unknown) => RenderResult<PdfFactsView>;
    openPdf: (bytes: ArrayBuffer) => Promise<PdfHandle>;
  }>("pdf");
  if (mine !== generation) return;
  if (data.value) {
    const facts = module.view(data.value);
    if (facts.ok) pdfFacts.value = facts.view;
  }
  const bytes = await fetchAttachmentBytes(attachmentId.value, { signal: ctrl.signal });
  if (mine !== generation) return;
  const handle = await module.openPdf(bytes);
  if (mine !== generation) {
    try {
      await handle.destroy(); // 已经收起了：打开出来的文档立刻销毁
    } catch {
      /* 忽略 */
    }
    return;
  }
  pdfHandle = handle;
  pdfPages.value = handle.pages;
  pdfPage.value = 1;
  await nextTick();
  await renderPdfPage(mine);
}

async function renderPdfPage(mine: number): Promise<void> {
  const handle = pdfHandle;
  const canvas = canvasRef.value;
  if (!handle || !canvas || mine !== generation) return;
  pdfBusy.value = true;
  try {
    await handle.renderPage(pdfPage.value, canvas, zoom.value, controller?.signal);
  } catch (err) {
    if (mine === generation && !isAbort(err)) viewError.value = messageOf(err);
  } finally {
    if (mine === generation) pdfBusy.value = false;
  }
}

// ---------------------------------------------------------------------------
// 交互（每个动作都会如实把阅读位置报给父组件；不写板面状态）
// ---------------------------------------------------------------------------

function emitPosition(extra: Partial<PreviewPosition> = {}): void {
  if (!props.active || plan.value.mode !== "inline") return;
  const mode = plan.value.renderer;
  // Markdown 没有页码 / 缩放 / 工作表 / 幻灯片这类阅读位置：不报位置，也不写一个空记录
  if (mode === "none" || mode === "markdown") return;
  const position: PreviewPosition = { mode, ...extra };
  if (mode === "pdf") position.page = pdfPage.value;
  if (mode === "xlsx" && currentSheet.value) position.sheet = currentSheet.value.name;
  if (mode === "pptx" && currentSlide.value) position.slide = currentSlide.value.index;
  // 归属一起交出去：附件 id 来自卡片 meta，版本来自这一次真实取到的预览数据。
  // 没有版本就不报位置（宁可少存一次，也不把位置写到不确定的版本上）。
  const id = attachmentId.value;
  const version = fileVersion.value;
  if (!id || !version) return;
  emit("position", { attachmentId: id, version, position });
}

function changeZoom(direction: 1 | -1): void {
  zoom.value = stepZoom(zoom.value, direction);
  emitPosition();
}

function goPage(page: number): void {
  const next = Math.min(Math.max(1, page), pdfPages.value || 1);
  if (next === pdfPage.value) return;
  pdfPage.value = next;
  void renderPdfPage(generation);
  emitPosition();
}

function changeSheet(index: number): void {
  sheetIndex.value = Math.min(Math.max(0, index), (xlsxView.value?.sheets.length ?? 1) - 1);
  emitPosition();
}

function changeSlide(index: number): void {
  slideIndex.value = Math.min(Math.max(0, index), (pptxView.value?.slides.length ?? 1) - 1);
  emitPosition();
}

async function runAction(action: PreviewAction): Promise<void> {
  actionNote.value = action.note;
  const ref = previewRefFor(props.card);
  try {
    const result =
      action.kind === "download"
        ? await downloadAttachment(ref)
        : await openAttachment(ref, { desktop: isDesktopShell() });
    if (result?.note) actionNote.value = result.note;
  } catch (err) {
    actionNote.value = messageOf(err); // 打不开就如实说，不谎报"已打开"
  }
}

watch(
  () => props.active,
  (active) => {
    if (active) void start();
    else stop();
  },
  { immediate: true },
);
watch(
  () => attachmentId.value,
  () => {
    if (props.active) void start();
  },
);
watch(
  () => props.card.id,
  () => {
    if (props.active) void start();
  },
);
/* ---------------------------------------------------------------------------
 * 横向自检的触发时机与释放
 * ------------------------------------------------------------------------- */

/** 内容 / 上下文一变就重新量一次（post：DOM 已经更新；读 scrollWidth 会强制同步布局）。 */
watch(
  [inlineReady, bodyViewport, () => plan.value.mode, () => plan.value.renderer, codeView, xlsxView, docxView, pptxView, pdfFacts],
  () => {
    verifyHorizontalFit();
  },
  { flush: "post" },
);

/** 窗口尺寸变化会改变可用宽度（图片适应、表格换行都跟它相关），重新量一次。 */
function onWindowResize(): void {
  verifyHorizontalFit();
}

onMounted(() => {
  verifyHorizontalFit();
  window.addEventListener("resize", onWindowResize);
});

onBeforeUnmount(() => {
  window.removeEventListener("resize", onWindowResize);
  stop();
  releaseImageDetail();
});

/** 详情态下卡片被换附件 / 改名 / 换版本：重新取图（旧请求由身份核对丢弃）。 */
watch(
  () => [props.card.id, attachmentId.value, imageVersionKey(), imageDetailMode.value] as const,
  () => {
    if (!imageDetailMode.value || !props.active) {
      releaseImageDetail();
      return;
    }
    void loadImageDetail();
  },
);

defineExpose({
  /** 复核入口：返回完整报告（外层 / 查看区各自的溢出与可移动性）。 */
  verifyHorizontalFit,
  horizontalFitReport: horizontalFit,
});
</script>

<template>
  <!--
    图片详情态（Phase 2-C 契约 §5.2）：
    图片铺满卡片（position:absolute; inset:0，圆角裁切），**不保留普通标题栏高度**；
    object-fit: contain —— 等比完整显示，不裁主体（不用 cover 伪装）、不拉伸；
    名称与关闭入口浮在图上（各自带一条窄蒙版），不吃图片本身的指针事件。
  -->
  <div
    v-if="imageDetailMode && plan.renderer === 'image'"
    class="cip-img-detail"
    data-testid="cip-img-detail"
    data-im="card-image-detail"
    :data-card-id="props.card.id"
    :data-image-state="imageDetailUrl ? 'ready' : imageDetailError ? 'failed' : 'loading'"
    :data-image-ratio="cardDisplay ? String(cardDisplay.aspectRatio) : ''"
  >
    <img
      v-if="imageDetailUrl && !imageDetailError"
      class="cip-img-detail-img"
      data-testid="cip-img-detail-img"
      data-im="card-image-detail-img"
      :src="imageDetailUrl"
      :alt="'图片：' + fileName"
      draggable="false"
    />
    <div v-else class="cip-img-detail-state" data-im="card-image-detail-state" role="status">
      <p v-if="imageDetailLoading || loading" data-testid="cip-img-detail-loading">正在读取原图…</p>
      <template v-else>
        <p class="cip-img-detail-reason" data-testid="cip-img-detail-error">
          {{ imageDetailError || plan.reason || viewError || "这张图片暂时显示不出来（原因未说明）" }}
        </p>
        <div class="cip-img-detail-actions">
          <button type="button" data-testid="cip-img-detail-retry" data-im="card-image-detail-retry" @click="retryImageDetail">
            重试
          </button>
          <button
            v-if="plan.actions.length"
            type="button"
            data-testid="cip-img-detail-open"
            data-im="card-image-detail-open"
            @click="runAction(plan.actions[0])"
          >{{ plan.actions[0].label }}</button>
        </div>
      </template>
    </div>
    <!--
      底部只留一条真实尺寸事实（等宽、贴左下角）：名称 / 许可 / 收起详情由卡片头部浮在图上，
      这里不重复一遍，也不占据图片中央的观看区域。
    -->
    <div class="cip-img-chrome">
      <p class="cip-img-shape mono" data-im="card-image-detail-shape">{{ imageShapeText }}</p>
    </div>
  </div>

  <section
    v-else
    ref="rootRef"
    class="cip"
    data-testid="cip-root"
    data-im="card-inline-preview"
    data-qio-fit
    :data-qio-context="context"
    :data-qio-h-fit="horizontalFitState"
  >
    <header class="cip-head">
      <div class="cip-ident">
        <span class="cip-name" data-testid="cip-name" :title="fileName">{{ fileName }}</span>
        <span class="cip-status" data-testid="cip-status">{{ statusText }}</span>
      </div>
      <button type="button" class="cip-close" data-testid="cip-close" @click="props.onClose()">
        收起预览
      </button>
    </header>

    <p v-if="renderState.truncated" class="cip-warn" data-testid="cip-truncated">
      内容超出预览上限：这里只显示前一部分（已截断，完整内容请打开或下载）
    </p>
    <p v-if="viewError" class="cip-error" data-testid="cip-view-error">{{ viewError }}</p>

    <!-- 内嵌渲染：纵向滚动责任由 data-qio-viewport 明确（见 bodyViewport 的注释） -->
    <div
      v-if="inlineReady"
      class="cip-body"
      :class="'cip-body-' + plan.renderer"
      :data-qio-viewport="bodyViewport"
      :data-qio-context="context"
    >
      <!--
        图片不做卡片内阅读（2026-10-15 图片展示简化）：这里**不渲染画面**，
        由下方的 cip-external 给出可读说明 + 既有打开 / 下载动作。
      -->

      <!-- PDF：pdf.js 逐页渲染（原页面外观） -->
      <div v-if="plan.renderer === 'pdf'" class="cip-pdf" data-im="cip-pdf">
        <div class="cip-tools">
          <button type="button" data-testid="cip-page-prev" :disabled="pdfPage <= 1" @click="goPage(pdfPage - 1)">
            上一页
          </button>
          <span class="cip-pageno" data-testid="cip-page">
            {{ pdfPage }} / {{ pdfPages || "?" }}
          </span>
          <button
            type="button"
            data-testid="cip-page-next"
            :disabled="pdfPages > 0 && pdfPage >= pdfPages"
            @click="goPage(pdfPage + 1)"
          >
            下一页
          </button>
          <input
            class="cip-jump"
            data-testid="cip-page-jump"
            type="number"
            min="1"
            :max="pdfPages || 1"
            :value="pdfPage"
            @change="goPage(Number(($event.target as HTMLInputElement).value))"
          />
          <!-- 缩放同样只属于独立材料阅读：对话内按可用宽度适应，不产生横向浏览 -->
          <template v-if="materialContext">
            <button type="button" data-testid="cip-pdf-zoom-out" @click="changeZoom(-1)">缩小</button>
            <span class="cip-zoom" data-testid="cip-pdf-zoom">{{ zoomPercent }}</span>
            <button type="button" data-testid="cip-pdf-zoom-in" @click="changeZoom(1)">放大</button>
          </template>
        </div>
        <div
          class="cip-canvas-box"
          v-qio-scrollbar="{ policy: 'auto', axis: 'both' }"
          :data-qio-pan="materialContext ? 'both' : 'none'"
        >
          <canvas ref="canvasRef" data-testid="cip-canvas"></canvas>
          <p v-if="pdfBusy" class="cip-busy" data-testid="cip-pdf-busy">正在渲染这一页…</p>
        </div>
        <p v-if="pdfFacts" class="cip-fact" data-testid="cip-pdf-facts">
          <template v-if="pdfFacts.title">标题：{{ pdfFacts.title }} · </template>
          共 {{ pdfFacts.pages ?? "未知" }} 页 · 可提取文本 {{ pdfFacts.textChars }} 字符
          <template v-if="pdfFacts.textChars === 0">
            （这一份没有可提取的文字层，可能是扫描件；页面仍然按原样渲染）
          </template>
        </p>
      </div>

      <!-- Markdown：按正文解析（标题 / 列表 / 表格 / 代码块），不再逐行显示原文 -->
      <div
        v-else-if="plan.renderer === 'markdown' && codeView"
        class="cip-markdown"
        data-im="cip-markdown"
        data-testid="cip-markdown"
        v-qio-scrollbar="{ policy: 'auto', axis: 'y' }"
      >
        <p class="cip-fact" data-testid="cip-markdown-facts">
          Markdown · 共 {{ codeView.totalLines }} 行 · 编码 {{ codeView.encoding }} · 已解析为正文 · 只读
        </p>
        <MarkdownContent :source="markdownSource" />
      </div>

      <!-- 代码 / 文本：行号 + 等宽 + 语言 + 只读 -->
      <div v-else-if="plan.renderer === 'code' && codeView" class="cip-code" data-im="cip-code">
        <p class="cip-fact" data-testid="cip-code-facts">
          {{ codeView.languageLabel }} · 共 {{ codeView.totalLines }} 行 · 编码 {{ codeView.encoding }} · 只读
        </p>
        <pre
          class="cip-pre"
          data-testid="cip-pre"
          tabindex="0"
          v-qio-scrollbar="{ policy: 'auto', axis: 'both' }"
          :data-qio-pan="materialContext ? 'both' : 'none'"
        ><code><span
          v-for="line in codeView.lines"
          :key="line.number"
          class="cip-line"
          data-testid="cip-line"
        ><span class="cip-lineno">{{ line.number }}</span>{{ line.text }}
</span></code></pre>
      </div>

      <!-- DOCX：结构化正文（不是原版面） -->
      <div v-else-if="plan.renderer === 'docx' && docxView" class="cip-docx" data-testid="cip-docx" data-im="cip-docx">
        <p v-if="docxView.empty" class="cip-empty">这份文档没有可显示的正文段落（可能是空文档或只含图片）</p>
        <template v-for="(block, index) in docxView.blocks" :key="index">
          <h4 v-if="block.type === 'heading'" class="cip-h" :class="'cip-h' + block.level" data-testid="cip-h">
            {{ block.text }}
          </h4>
          <p v-else-if="block.type === 'list'" class="cip-li" data-testid="cip-li">
            <span class="cip-marker" :title="block.ordered === null ? '编号未知（文件里没有说明编号格式）' : ''">{{
              block.marker
            }}</span>
            <span><template v-for="(run, runIndex) in block.runs" :key="runIndex"><strong
              v-if="run.bold"
            >{{ run.text }}</strong><em v-else-if="run.italic">{{ run.text }}</em><template v-else>{{ run.text }}</template></template></span>
          </p>
          <p v-else class="cip-p" data-testid="cip-p">
            <template v-for="(run, runIndex) in block.runs" :key="runIndex"><strong
              v-if="run.bold"
            >{{ run.text }}</strong><em v-else-if="run.italic">{{ run.text }}</em><template v-else>{{ run.text }}</template></template>
          </p>
        </template>
        <table v-for="(table, tableIndex) in docxView.tables" :key="'t' + tableIndex" class="cip-table" data-testid="cip-table">
          <tbody>
            <tr v-for="(row, rowIndex) in table.rows" :key="rowIndex">
              <td v-for="(cell, cellIndex) in row" :key="cellIndex">{{ cell }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- XLSX：工作表 + 单元格地址 + 公式/缓存值分开 -->
      <div v-else-if="plan.renderer === 'xlsx' && xlsxView" class="cip-xlsx" data-testid="cip-xlsx" data-im="cip-xlsx">
        <p v-if="xlsxView.empty" class="cip-empty">这份表格没有可显示的单元格</p>
        <div v-else class="cip-tabs">
          <button
            v-for="(sheet, index) in xlsxView.sheets"
            :key="sheet.name"
            type="button"
            class="cip-tab"
            :class="{ 'cip-tab-on': index === sheetIndex }"
            data-testid="cip-sheet-tab"
            @click="changeSheet(index)"
          >
            {{ sheet.name }}
          </button>
        </div>
        <!--
          二维查看由这一个网格区域承担（唯一的滚动视口）：
          行号列 + 列标表头始终在结构里，单元格地址与值一一对应，不展开成「记录」列表 ——
          合并单元格之类的结构必须保住真实跨度与对应关系，展开成逐项记录会误导。
        -->
        <div
          v-if="currentSheet"
          class="cip-grid-box"
          v-qio-scrollbar="{ policy: 'auto', axis: 'both' }"
          :data-qio-pan="materialContext ? 'both' : 'none'"
        >
          <table class="cip-grid" data-testid="cip-grid">
            <thead>
              <tr>
                <th class="cip-corner"></th>
                <th v-for="column in currentSheet.columns" :key="column">{{ column }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in currentSheet.rows" :key="row.index">
                <th class="cip-rownum">{{ row.index }}</th>
                <td v-for="(cell, index) in row.cells" :key="index" data-testid="cip-cell">
                  <template v-if="cell">
                    <span class="cip-cell-ref">{{ cell.ref }}</span>
                    <span class="cip-cell-value" :class="'cip-cell-' + cell.kind">{{ cell.display }}</span>
                    <span v-if="cell.formulaNote" class="cip-cell-note">{{ cell.formulaNote }}</span>
                  </template>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <!-- PPTX：幻灯片切换 + 文本/表格/图片清单 -->
      <div v-else-if="plan.renderer === 'pptx' && pptxView" class="cip-pptx" data-testid="cip-pptx" data-im="cip-pptx">
        <div class="cip-tabs">
          <button
            v-for="(slide, index) in pptxView.slides"
            :key="slide.index"
            type="button"
            class="cip-tab"
            :class="{ 'cip-tab-on': index === slideIndex }"
            data-testid="cip-slide-tab"
            @click="changeSlide(index)"
          >
            第 {{ slide.index }} 页
          </button>
        </div>
        <div v-if="currentSlide" class="cip-slide">
          <h4 v-if="currentSlide.title" class="cip-h cip-h1" data-testid="cip-slide-title">
            {{ currentSlide.title }}
          </h4>
          <p v-for="(text, index) in currentSlide.texts" :key="'x' + index" class="cip-p" data-testid="cip-slide-text">
            {{ text }}
          </p>
          <div v-for="(table, tableIndex) in currentSlide.tables" :key="'t' + tableIndex" class="cip-slide-table">
            <table class="cip-table">
              <tbody>
                <tr v-for="(row, rowIndex) in table" :key="rowIndex">
                  <td v-for="(cell, cellIndex) in row" :key="cellIndex">{{ cell }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <ul v-if="currentSlide.images.length" class="cip-list" data-testid="cip-slide-images">
            <li v-for="image in currentSlide.images" :key="image.name">
              图片清单：{{ image.name }}（{{ image.size }}）—— 这里只列清单，不显示画面
            </li>
          </ul>
          <div v-if="currentSlide.notes.length" class="cip-notes" data-testid="cip-slide-notes">
            <p v-for="(note, index) in currentSlide.notes" :key="'n' + index" class="cip-p">备注：{{ note }}</p>
          </div>
        </div>
      </div>
    </div>

    <!-- 加载中 -->
    <p v-else-if="loading && attachmentId" class="cip-loading" data-testid="cip-loading">
      正在生成预览…（只读取文件，不调用模型）
    </p>

    <!-- 不能内嵌 / 看不了：给真实原因 + 打开/下载入口 -->
    <div v-else class="cip-external" data-testid="cip-external" data-im="cip-external">
      <p class="cip-reason" data-testid="cip-reason">{{ externalReason }}</p>
      <div class="cip-actions">
        <button
          v-for="action in plan.actions"
          :key="action.kind + action.label"
          type="button"
          class="cip-action"
          :data-testid="'cip-action-' + action.kind"
          :title="action.note"
          @click="runAction(action)"
        >
          {{ action.label }}
        </button>
      </div>
      <p v-if="actionNote" class="cip-fact" data-testid="cip-action-note">{{ actionNote }}</p>
    </div>
  </section>
</template>

<style scoped>
.cip {
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
  height: 100%;
  /* 卡片内展开时由 .card-preview-host 的 flex 定高；height: 100% 给非 flex 宿主兜底 */
  flex: 1 1 auto;
  min-width: 0;
  min-height: 0;
  padding: var(--sp-3);
  background: var(--bg-surface);
  color: var(--text-primary);
  font-family: var(--sans);
  font-size: var(--fs-base);
  line-height: var(--lh-base);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-lg);
  box-sizing: border-box;
}
.cip-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--sp-3);
}
.cip-ident {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
  min-width: 0;
}
.cip-name {
  font-family: var(--serif);
  font-size: var(--fs-md);
  color: var(--text-strong);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.cip-status,
.cip-fact,
.cip-warn,
.cip-empty,
.cip-loading {
  font-size: var(--fs-sm);
  color: var(--text-secondary);
}
.cip-warn {
  color: var(--warning);
}
.cip-error,
.cip-reason {
  color: var(--danger);
  font-size: var(--fs-sm);
}
.cip-close,
.cip-action,
.cip-tools button,
.cip-tab {
  font-family: var(--sans);
  font-size: var(--fs-sm);
  color: var(--text-primary);
  background: var(--bg-elevated);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-sm);
  padding: var(--sp-1) var(--sp-2);
  cursor: pointer;
  transition: background var(--dur-fast) var(--ease), color var(--dur-fast) var(--ease);
}
.cip-tools button:disabled {
  color: var(--text-faint);
  cursor: not-allowed;
}
/*
  * 材料主体：**纵向滚动责任只有一个**，由 data-qio-viewport 明确（见脚本 bodyViewport）：
  *   self    —— 材料 + 文档 / 幻灯片 / 外部打开：这一层是唯一视口；
  *   viewer  —— 材料 + 图片 / PDF / 表格 / 代码：自带二维浏览区是唯一视口，
  *              这一层只负责把它撑满剩余高度，自己绝不滚动；
  *   content —— 对话内：一个滚动容器都不建（无横向规则），滚动交给对话主体那一层。
  * 这样从卡片正文到最内层预览最多只有**一个**纵向滚动面。
  */
.cip-body {
  display: flex;
  flex-direction: column;
  flex: 1 1 auto;
  min-width: 0;
  min-height: 0;
}
.cip-body[data-qio-viewport="self"] {
  overflow-y: auto;
  overflow-x: hidden;
}
.cip-body[data-qio-viewport="viewer"],
.cip-body[data-qio-viewport="content"] {
  overflow: visible;
}
/* 承担视口的渲染器：占满剩余高度，滚动只发生在它自己的盒子内部 */
.cip-body[data-qio-viewport="viewer"] > * {
  display: flex;
  flex-direction: column;
  flex: 1 1 auto;
  min-width: 0;
  min-height: 0;
}
.cip-tools {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  flex-wrap: wrap;
  margin-bottom: var(--sp-2);
}
.cip-zoom,
.cip-pageno,
.cip-cell-ref,
.cip-lineno,
.cip-marker,
.cip-rownum,
.cip-corner {
  font-family: var(--mono);
  font-size: var(--fs-tech);
  color: var(--text-muted);
}
.cip-jump {
  width: 64px;
  font-family: var(--mono);
  font-size: var(--fs-sm);
  color: var(--text-primary);
  background: var(--bg-inset);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-xs);
  padding: var(--sp-1);
}
/*
  * PDF 页面框：
  * - data-qio-pan="both"（独立材料阅读）：二维浏览区，占满剩余高度，只有放大到超出时才滚动，
  *   指示条走统一实现；
  * - data-qio-pan="none"（对话内）：执行无横向规则 —— 页面按可用宽度适应，不滚动。
  */
.cip-canvas-box {
  display: flex;
  align-items: flex-start;
  justify-content: center;
  min-width: 0;
  background: var(--bg-inset);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  padding: var(--sp-2);
}
.cip-canvas-box[data-qio-pan="both"] {
  flex: 1 1 auto;
  min-height: 0;
  overflow: auto;
}
.cip-canvas-box[data-qio-pan="none"] {
  overflow: visible;
  max-width: 100%;
}
.cip-canvas {
  display: block;
}
.cip-canvas-box canvas {
  max-width: 100%;
  height: auto;
}
/*
  * 代码预览：纵向滚动责任写死在这一处 —— 独立材料阅读里它自己就是那一个视口
  * （横向保留必要的代码浏览，指示条走统一实现）；对话内软换行、不建滚动容器。
  */
.cip-pre {
  margin: 0;
  padding: var(--sp-2);
  background: var(--bg-inset);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  font-family: var(--mono);
  font-size: var(--fs-sm);
  line-height: var(--lh-tight);
  color: var(--text-primary);
  min-width: 0;
  tab-size: 4;
}
.cip-pre[data-qio-pan="both"] {
  flex: 1 1 auto;
  min-height: 0;
  overflow: auto;
}
.cip-pre[data-qio-pan="none"] {
  overflow: visible;
  max-width: 100%;
}
.cip-line {
  display: block;
  white-space: pre;
}
/* 对话内：代码软换行（只改呈现，textContent 与复制结果逐字不变） */
.cip-pre[data-qio-pan="none"] .cip-line {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  word-break: normal;
}
.cip-lineno {
  display: inline-block;
  width: 3.5em;
  text-align: right;
  padding-right: var(--sp-2);
  color: var(--text-faint);
  user-select: none;
}
.cip-h {
  font-family: var(--serif);
  color: var(--text-strong);
  margin: var(--sp-3) 0 var(--sp-1);
}
.cip-h1 { font-size: var(--fs-lg); }
.cip-h2 { font-size: var(--fs-md); }
.cip-h3 { font-size: var(--fs-base); }
.cip-p,
.cip-li {
  margin: 0 0 var(--sp-2);
  max-width: var(--measure);
}
.cip-li {
  display: flex;
  gap: var(--sp-2);
}
.cip-marker {
  min-width: 1.6em;
}
/*
  * 交付口径（Lead 裁决，2026-10-14）：
  * **记录模式仅用于存在真实表头的表格；合并单元格 / 电子表格保留真实结构；
  *   二维浏览只出现在独立材料阅读上下文（context="material"），对话内一律不横向。**
  *
  * 因此这里的表格按可用宽度重排（固定布局 + 换行），保证任何上下文里都不横向溢出、
  * 也不靠裁切 —— 行与列的对应关系留在真实结构里。
  *
  * 为什么不套共享的「记录模式」（useQioTables / .qio-table-wrap）：
  * 记录模式靠 td[data-label] 提供列名，而 docx / pptx 的表格数据里**没有表头行**，
  * 硬套只会给出一列编造的列名；电子表格更是靠行号列 + 列标表头维持二维对应关系，
  * 展开成逐条「列名：值」会破坏单元格的跨度与位置关系（合并单元格尤其明显）。
  * 所以这里只改布局（固定列宽 + 换行），结构、顺序、跨度与对应关系一个都不动。
  */
.cip-table,
.cip-grid {
  border-collapse: collapse;
  font-size: var(--fs-sm);
  margin: var(--sp-2) 0;
  width: 100%;
  max-width: 100%;
  table-layout: fixed;
}
.cip-table :where(td, th),
.cip-grid :where(td, th) {
  overflow-wrap: anywhere;
  word-break: break-word;
  min-width: 0;
}
.cip-table td,
.cip-grid th,
.cip-grid td {
  border: 1px solid var(--border-subtle);
  padding: var(--sp-1) var(--sp-2);
  vertical-align: top;
  text-align: left;
}
/* 电子表格：唯一一个「明确网格区域」承担二维查看（行号列 + 列标表头都在结构里） */
.cip-grid-box {
  min-width: 0;
}
.cip-grid-box[data-qio-pan="both"] {
  flex: 1 1 auto;
  min-height: 0;
  overflow: auto;
}
.cip-grid-box[data-qio-pan="none"] {
  overflow: visible;
  max-width: 100%;
}
.cip-grid th {
  background: var(--bg-elevated);
  font-family: var(--mono);
  font-size: var(--fs-tech);
  color: var(--text-secondary);
  position: sticky;
  top: 0;
}
.cip-cell-ref {
  display: block;
}
.cip-cell-value {
  color: var(--text-primary);
}
.cip-cell-formula {
  font-family: var(--mono);
  color: var(--link);
}
.cip-cell-empty {
  color: var(--text-faint);
}
.cip-cell-note,
.cip-fact {
  display: block;
  color: var(--text-muted);
  font-size: var(--fs-tech);
}
.cip-tabs {
  display: flex;
  gap: var(--sp-1);
  flex-wrap: wrap;
  margin-bottom: var(--sp-2);
}
.cip-tab-on {
  background: var(--accent-soft);
  border-color: var(--accent);
  color: var(--text-strong);
}
.cip-list {
  margin: var(--sp-2) 0;
  padding-left: var(--sp-5);
  color: var(--text-secondary);
  font-size: var(--fs-sm);
}
.cip-notes {
  border-left: 2px solid var(--border-strong);
  padding-left: var(--sp-3);
  color: var(--text-secondary);
}
.cip-actions {
  display: flex;
  gap: var(--sp-2);
  flex-wrap: wrap;
  margin-top: var(--sp-2);
}
/*
 * ---- 图片详情态（Phase 2-C 契约 §5.2）--------------------------------------
 * 铺满卡片（absolute inset:0）、圆角裁切（border-radius: inherit + overflow: hidden）、
 * 等比完整显示（object-fit: contain：不裁主体、不拉伸，也不用 cover 伪装）。
 * 这一层不铺普通标题栏：名称 / 尺寸事实 / 收起入口都浮在图上，各自带一条窄蒙版。
 * 图片层与整层都不吃指针事件：拖动、选中、连接点与缩放手柄照旧落在卡片上；
 * 只有名称条里的按钮自己收事件。
 */
.cip-img-detail {
  position: absolute;
  inset: 0;
  display: block;
  overflow: hidden;
  border-radius: inherit;
  background: var(--bg-inset);
  pointer-events: none;
}
.cip-img-detail-img {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  object-fit: contain;
  display: block;
  pointer-events: none;
  user-select: none;
  -webkit-user-drag: none;
}
.cip-img-detail-state {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: var(--sp-2);
  padding: var(--sp-4);
  text-align: center;
  color: var(--text-secondary);
  font-size: var(--fs-sm);
  line-height: var(--lh-tight);
}
.cip-img-detail-state p {
  margin: 0;
  max-width: 100%;
  padding: 2px var(--sp-2);
  border-radius: var(--r-xs);
  background: color-mix(in srgb, var(--bg-surface) 88%, transparent);
  overflow-wrap: anywhere;
}
.cip-img-detail-reason {
  color: var(--danger);
}
.cip-img-detail-actions {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2);
  justify-content: center;
}
.cip-img-detail-actions button {
  pointer-events: auto;
  font-family: var(--sans);
  font-size: var(--fs-sm);
  color: var(--text-primary);
  background: var(--bg-elevated);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-sm);
  padding: var(--sp-1) var(--sp-2);
  cursor: pointer;
}
.cip-img-detail-actions button:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 1px;
}
/*
 * 浮在图片上的**唯一**一条信息：真实尺寸事实（左下角，等宽、贴一条窄底渐变）。
 * 名称 / 许可 / 收起详情由卡片头部浮在图上（BoardCard 的 .card.is-image-detail > .card-head），
 * 两边不重复同一句话；连接点与缩放手柄由 BoardResizeLayer 画在卡片外侧，不会被这条压住。
 */
.cip-img-chrome {
  position: absolute;
  inset: 0;
  pointer-events: none;
}
.cip-img-chrome::after {
  content: "";
  position: absolute;
  left: 0;
  right: 0;
  bottom: 0;
  height: 56px;
  background: linear-gradient(
    to top,
    color-mix(in srgb, var(--bg-surface) 84%, transparent),
    transparent
  );
}
.cip-img-shape {
  position: absolute;
  left: var(--sp-3);
  bottom: var(--sp-2);
  z-index: 1;
  max-width: calc(100% - var(--sp-6));
  margin: 0;
  color: var(--text-secondary);
  font-size: var(--fs-tech);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
@media (prefers-reduced-motion: reduce) {
  .cip *,
  .cip *::before,
  .cip *::after {
    transition: none !important;
    animation: none !important;
  }
}
</style>