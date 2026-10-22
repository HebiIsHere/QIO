/**
 * PDF 渲染器：前端 pdf.js **逐页渲染**（契约 §3.1：原页面外观，页数/当前页真实）。
 *
 * 两条硬约束（都来自本项目真实环境，不是理论）：
 *
 * 1. **CSP**：生产壳是 \`script-src 'self'\`（没有 blob:），worker-src 允许 'self'。
 *    所以 worker 必须走**同源 URL**：这里用 Vite 的 \`?url\` 拿到打包产物的同源地址，
 *    绝不自己造 blob worker（会被 CSP 拦掉）。拿不到 Worker 时退到 workerSrc（仍是同源）。
 * 2. **认证**：/api/attachments/{id}/content 的会话令牌只在请求头里，pdf.js 自己去 GET
 *    这个 URL 会 401。所以字节一律由调用方用带认证的 fetch 取好，这里只用 getDocument({ data })。
 *
 * 本模块只被**动态 import**（见 ./index.ts）：不用 PDF 的会话不进首屏包。
 */
import workerAsset from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import type { PDFDocumentProxy, RenderTask } from "pdfjs-dist";
import { asBool, asNumber, asRecord, asString, fail, ok } from "./types";
import type { RenderResult } from "./types";

export const RENDERER = "pdf";

/** 渲染被调用方中止（收起卡片 / 换附件 / 卸载）：不是失败，界面静默丢弃。 */
export class PdfAbortedError extends Error {
  constructor() {
    super("PDF 渲染已中止（收起卡片或换了附件）");
    this.name = "PdfAbortedError";
  }
}

export interface PdfFactsView {
  pages: number | null;
  title: string;
  encrypted: boolean;
  textChars: number;
  /** 这份 PDF 能不能真的逐页渲染（加密 / 页数未知 → 不能） */
  renderable: boolean;
  reason: string;
}

/** 后端只给事实，这里把它们整理成界面要用的形状（不做任何推断）。 */
export function view(data: unknown): RenderResult<PdfFactsView> {
  const record = asRecord(data);
  if (!record || record.kind !== "pdf") return fail("后端没有给出 PDF 的事实信息");
  const encrypted = asBool(record.encrypted);
  const pages = asNumber(record.pages);
  return ok({
    pages,
    title: asString(record.title),
    encrypted,
    textChars: asNumber(record.textChars) ?? 0,
    renderable: !encrypted && pages !== null && pages > 0,
    reason: asString(record.reason),
  });
}

export interface PdfPageSize {
  width: number;
  height: number;
}

export interface PdfHandle {
  pages: number;
  /** 把第 page 页（1 起）渲染到 canvas；同一时刻只保留一次渲染任务。 */
  renderPage(
    page: number,
    canvas: HTMLCanvasElement,
    scale: number,
    signal?: AbortSignal,
  ): Promise<PdfPageSize>;
  /** 释放文档与 worker 侧资源（卸载 / 换附件时调用）。 */
  destroy(): Promise<void>;
  /**
   * 取某一页的**可提取文字**（用于预览栏里的逐页文字搜索）。
   *
   * 这是 2026-10-22 对话页预览的**向后兼容增量**：不改既有签名，只多一个方法。
   * 扫描件 / 没有文字层的页面返回空串（如实——不假装有文字）。
   */
  pageText(page: number, signal?: AbortSignal): Promise<string>;
}

let workerConfigured = false;

function configureWorker(pdfjs: typeof import("pdfjs-dist")): void {
  if (workerConfigured) return;
  workerConfigured = true;
  const options = pdfjs.GlobalWorkerOptions;
  if (options.workerPort || options.workerSrc) return;
  const url = String(workerAsset || "");
  if (!url) return;
  if (typeof Worker === "function") {
    try {
      // 同源 module worker：由 Vite 打包产出，满足 worker-src 'self'
      options.workerPort = new Worker(url, { type: "module" });
      return;
    } catch {
      /* 建 worker 失败：退到 workerSrc（同源），pdf.js 会走它自己的加载路径 */
    }
  }
  options.workerSrc = url;
}

export async function openPdf(
  bytes: ArrayBuffer | Uint8Array,
  options: { onProgress?: (loaded: number, total: number) => void } = {},
): Promise<PdfHandle> {
  const pdfjs = await import("pdfjs-dist");
  configureWorker(pdfjs);
  // 复制一份：pdf.js 会把字节交给 worker（可能移交并清空原 buffer），
  // 调用方之后还要用原 buffer 时必须不被掏空。
  const data = bytes instanceof Uint8Array ? new Uint8Array(bytes) : new Uint8Array(bytes.slice(0));
  const loadingTask = pdfjs.getDocument({
    data,
    // 生产 CSP 是 script-src 'self'（没有 'wasm-unsafe-eval'）：WebAssembly 编译会被拦掉，
    // 所以显式关掉 pdf.js 的 wasm 加速（它只影响少数图像解码路径，不影响页面外观）。
    useWasm: false,
    disableAutoFetch: true,
    disableStream: true,
  });
  if (options.onProgress) {
    loadingTask.onProgress = (progress: { loaded: number; total: number }) => {
      options.onProgress?.(progress.loaded, progress.total);
    };
  }
  const doc: PDFDocumentProxy = await loadingTask.promise;
  let currentTask: RenderTask | null = null;

  return {
    pages: doc.numPages,
    /**
     * 一页的可提取文字：把 textContent 里的每一项拼起来（保持原有顺序与换行）。
     * 页面没有任何文字项时返回空串 —— 扫描件没有文字层，界面据此如实说「未找到」。
     */
    async pageText(pageNumber: number, signal?: AbortSignal): Promise<string> {
      if (signal?.aborted) throw new PdfAbortedError();
      const page = await doc.getPage(pageNumber);
      if (signal?.aborted) throw new PdfAbortedError();
      const content = await page.getTextContent();
      const parts: string[] = [];
      for (const item of content.items as { str?: string }[]) {
        if (typeof item.str === "string" && item.str) parts.push(item.str);
      }
      return parts.join("\n");
    },
    async renderPage(pageNumber, canvas, scale, signal) {
      if (signal?.aborted) throw new PdfAbortedError();
      const page = await doc.getPage(pageNumber);
      const viewport = page.getViewport({ scale });
      const context = canvas.getContext("2d");
      if (!context) throw new Error("这个环境没有 canvas 2D 上下文，无法渲染 PDF 页面");
      canvas.width = Math.max(1, Math.floor(viewport.width));
      canvas.height = Math.max(1, Math.floor(viewport.height));
      currentTask?.cancel();
      const task = page.render({ canvasContext: context, canvas, viewport });
      currentTask = task;
      const onAbort = () => task.cancel();
      signal?.addEventListener("abort", onAbort, { once: true });
      try {
        await task.promise;
      } catch (err) {
        if (signal?.aborted) throw new PdfAbortedError();
        throw err;
      } finally {
        signal?.removeEventListener("abort", onAbort);
        if (currentTask === task) currentTask = null;
      }
      return { width: canvas.width, height: canvas.height };
    },
    async destroy() {
      try {
        currentTask?.cancel();
      } catch {
        /* 已经结束的渲染任务取消会抛，忽略 */
      }
      currentTask = null;
      // v6 的销毁入口在 loadingTask 上（PDFDocumentProxy 已没有 destroy()）：
      // 它会 abort 网络请求并销毁 worker。
      await loadingTask.destroy();
    },
  };
}
