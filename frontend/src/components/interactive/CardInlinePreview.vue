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
 *   * 「结构化预览，不是原版面」必须写在界面上（docx/xlsx/pptx 的 notice 来自渲染器模块）；
 *   * 失败 / 不支持时给**真实原因**与打开/下载入口，**不给假按钮**。
 */
import { computed, nextTick, onBeforeUnmount, ref, watch } from "vue";
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
  fetchAttachmentPreview,
  isDesktopShell,
  openAttachment,
} from "../../services/attachments";
import type { PreviewEnvelope, PreviewPosition } from "../../services/attachments";
import { loadRenderer } from "../../interactive/renderers";
import type { RenderResult } from "../../interactive/renderers/types";
import type { CodeView } from "../../interactive/renderers/code";
import type { DocxView } from "../../interactive/renderers/docx";
import type { ImageView } from "../../interactive/renderers/image";
import type { PptxView } from "../../interactive/renderers/pptx";
import type { XlsxView } from "../../interactive/renderers/xlsx";
import type { PdfFactsView, PdfHandle } from "../../interactive/renderers/pdf";
import { fitScale, stepZoom } from "../../interactive/renderers/image";

const props = defineProps<{ card: BoardCard; active: boolean; onClose: () => void }>();
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

const imageView = ref<ImageView | null>(null);
const imageUrl = ref<string | null>(null);
const imageRef = ref<HTMLImageElement | null>(null);
const zoom = ref(1);
const fitMode = ref(true);

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
const sizeBytes = computed(() => cardSizeBytes(props.card));
const plan = computed(() =>
  previewPlan(props.card, renderState.value, { desktop: isDesktopShell() }),
);
const statusText = computed(() => previewStatusText(renderState.value, sizeBytes.value));
const loading = computed(() => renderState.value.status === "loading" || renderState.value.status === "idle");
const inlineReady = computed(() => plan.value.mode === "inline" && renderState.value.status === "ready");
/** 结构化预览的"不是原版面"必须显示出来（来自渲染器模块，只有一处说法） */
const noticeText = computed(
  () => docxView.value?.notice ?? xlsxView.value?.notice ?? pptxView.value?.notice ?? "",
);
const currentSheet = computed(() => xlsxView.value?.sheets[sheetIndex.value] ?? null);
const currentSlide = computed(() => pptxView.value?.slides[slideIndex.value] ?? null);
const zoomPercent = computed(() => Math.round(zoom.value * 100) + "%");

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

function revokeImage(): void {
  const url = imageUrl.value;
  imageUrl.value = null;
  if (url && typeof URL.revokeObjectURL === "function") {
    try {
      URL.revokeObjectURL(url);
    } catch {
      /* 已经释放过就算了 */
    }
  }
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
  revokeImage();
  void releasePdf();
  data.value = null;
  viewError.value = "";
  actionNote.value = "";
  imageView.value = null;
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
  fitMode.value = true;
}

/** 收起 / 换附件 / 卸载：中止在途请求与渲染，释放一切资源。 */
function stop(): void {
  generation += 1;
  controller?.abort();
  controller = null;
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
      await mountImage(mine, ctrl);
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
  else if (renderer === "code") codeView.value = result.view as CodeView;
}

async function mountImage(mine: number, ctrl: AbortController): Promise<void> {
  const module = await loadRenderer<{ view: (value: unknown) => RenderResult<ImageView> }>("image");
  if (mine !== generation) return;
  if (!data.value) return;
  const result = module.view(data.value);
  if (!result.ok) {
    viewError.value = result.reason;
    return;
  }
  imageView.value = result.view;
  const bytes = await fetchAttachmentBytes(attachmentId.value, { signal: ctrl.signal });
  if (mine !== generation) return;
  const create = (URL as unknown as { createObjectURL?: (blob: Blob) => string }).createObjectURL;
  if (typeof create !== "function") {
    viewError.value = "这个环境不支持图片内嵌预览（可以打开或下载副本）";
    return;
  }
  revokeImage();
  imageUrl.value = create(new Blob([bytes], { type: mimeTypeFor(fileName.value) }));
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
  if (mode === "none") return;
  const position: PreviewPosition = { mode, ...extra };
  if (mode === "pdf") position.page = pdfPage.value;
  if (mode === "image") position.zoom = zoom.value;
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
  fitMode.value = false;
  zoom.value = stepZoom(zoom.value, direction);
  emitPosition();
}

function fitImage(): void {
  fitMode.value = true;
  applyFit();
  emitPosition();
}

function applyFit(): void {
  const element = imageRef.value;
  const natural =
    element && element.naturalWidth > 0
      ? { width: element.naturalWidth, height: element.naturalHeight }
      : imageView.value?.width && imageView.value?.height
        ? { width: imageView.value.width, height: imageView.value.height }
        : null;
  const box = { width: element?.clientWidth ?? 0, height: element?.clientHeight ?? 0 };
  zoom.value = natural ? fitScale(natural, { width: box.width || 640, height: box.height || 420 }) : 1;
}

function onImageLoad(): void {
  if (fitMode.value) applyFit();
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

function mimeTypeFor(name: string): string {
  const suffix = name.slice(name.lastIndexOf(".")).toLowerCase();
  const table: Record<string, string> = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
  };
  return table[suffix] ?? "application/octet-stream";
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
onBeforeUnmount(stop);
</script>

<template>
  <section class="cip" data-testid="cip-root">
    <header class="cip-head">
      <div class="cip-ident">
        <span class="cip-name" :title="fileName">{{ fileName }}</span>
        <span class="cip-status" data-testid="cip-status">{{ statusText }}</span>
      </div>
      <button type="button" class="cip-close" data-testid="cip-close" @click="props.onClose()">
        收起预览
      </button>
    </header>

    <p v-if="renderState.truncated" class="cip-warn" data-testid="cip-truncated">
      内容超出预览上限：这里只显示前一部分（已截断，完整内容请打开或下载）
    </p>
    <p v-if="noticeText" class="cip-notice" data-testid="cip-notice">{{ noticeText }}</p>
    <p v-if="viewError" class="cip-error" data-testid="cip-view-error">{{ viewError }}</p>

    <!-- 内嵌渲染 -->
    <div v-if="inlineReady" class="cip-body" :class="'cip-body-' + plan.renderer">
      <!-- 图片：等比 + 放大 / 缩小 / 适应 -->
      <div v-if="plan.renderer === 'image'" class="cip-image">
        <div class="cip-tools">
          <button type="button" data-testid="cip-zoom-out" @click="changeZoom(-1)">缩小</button>
          <span class="cip-zoom" data-testid="cip-zoom">{{ zoomPercent }}</span>
          <button type="button" data-testid="cip-zoom-in" @click="changeZoom(1)">放大</button>
          <button type="button" data-testid="cip-zoom-fit" @click="fitImage()">适应</button>
        </div>
        <div class="cip-image-box">
          <img
            v-if="imageUrl"
            ref="imageRef"
            class="cip-image-el"
            data-testid="cip-image"
            :src="imageUrl"
            :alt="fileName"
            :style="{ transform: 'scale(' + zoom + ')' }"
            @load="onImageLoad"
          />
        </div>
        <p v-if="imageView" class="cip-fact" data-testid="cip-image-fact">{{ imageView.shape }}</p>
      </div>

      <!-- PDF：pdf.js 逐页渲染（原页面外观） -->
      <div v-else-if="plan.renderer === 'pdf'" class="cip-pdf">
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
          <button type="button" data-testid="cip-pdf-zoom-out" @click="changeZoom(-1)">缩小</button>
          <span class="cip-zoom" data-testid="cip-pdf-zoom">{{ zoomPercent }}</span>
          <button type="button" data-testid="cip-pdf-zoom-in" @click="changeZoom(1)">放大</button>
        </div>
        <div class="cip-canvas-box">
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

      <!-- 代码 / 文本：行号 + 等宽 + 语言 + 只读 -->
      <div v-else-if="plan.renderer === 'code' && codeView" class="cip-code">
        <p class="cip-fact" data-testid="cip-code-facts">
          {{ codeView.languageLabel }} · 共 {{ codeView.totalLines }} 行 · 编码 {{ codeView.encoding }} · 只读
        </p>
        <pre class="cip-pre" data-testid="cip-pre" tabindex="0"><code><span
          v-for="line in codeView.lines"
          :key="line.number"
          class="cip-line"
          data-testid="cip-line"
        ><span class="cip-lineno">{{ line.number }}</span>{{ line.text }}
</span></code></pre>
      </div>

      <!-- DOCX：结构化正文（不是原版面） -->
      <div v-else-if="plan.renderer === 'docx' && docxView" class="cip-docx" data-testid="cip-docx">
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
      <div v-else-if="plan.renderer === 'xlsx' && xlsxView" class="cip-xlsx" data-testid="cip-xlsx">
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
        <div v-if="currentSheet" class="cip-grid-box">
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
        <p class="cip-fact" data-testid="cip-xlsx-facts">{{ xlsxView.notice }}</p>
      </div>

      <!-- PPTX：幻灯片切换 + 文本/表格/图片清单 -->
      <div v-else-if="plan.renderer === 'pptx' && pptxView" class="cip-pptx" data-testid="cip-pptx">
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
        <p class="cip-fact" data-testid="cip-pptx-facts">{{ pptxView.notice }}</p>
      </div>
    </div>

    <!-- 加载中 -->
    <p v-else-if="loading && attachmentId" class="cip-loading" data-testid="cip-loading">
      正在生成预览…（只读取文件，不调用模型）
    </p>

    <!-- 不能内嵌 / 看不了：给真实原因 + 打开/下载入口 -->
    <div v-else class="cip-external" data-testid="cip-external">
      <p class="cip-reason" data-testid="cip-reason">{{ plan.reason }}</p>
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
.cip-notice,
.cip-warn,
.cip-empty,
.cip-loading {
  font-size: var(--fs-sm);
  color: var(--text-secondary);
}
.cip-notice {
  color: var(--warning);
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
.cip-body {
  flex: 1 1 auto;
  min-height: 0;
  overflow: auto;
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
.cip-image-box,
.cip-canvas-box {
  display: flex;
  align-items: flex-start;
  justify-content: center;
  overflow: auto;
  background: var(--bg-inset);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  padding: var(--sp-2);
  max-height: 60vh;
}
.cip-image-el {
  display: block;
  max-width: 100%;
  transform-origin: top center;
  transition: transform var(--dur-base) var(--ease);
}
.cip-canvas {
  display: block;
}
.cip-canvas-box canvas {
  max-width: 100%;
}
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
  overflow: auto;
  max-height: 60vh;
  tab-size: 4;
}
.cip-line {
  display: block;
  white-space: pre;
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
.cip-table,
.cip-grid {
  border-collapse: collapse;
  font-size: var(--fs-sm);
  margin: var(--sp-2) 0;
}
.cip-table td,
.cip-grid th,
.cip-grid td {
  border: 1px solid var(--border-subtle);
  padding: var(--sp-1) var(--sp-2);
  vertical-align: top;
  text-align: left;
}
.cip-grid-box {
  overflow: auto;
  max-height: 50vh;
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
@media (prefers-reduced-motion: reduce) {
  .cip *,
  .cip *::before,
  .cip *::after {
    transition: none !important;
    animation: none !important;
  }
}
</style>
