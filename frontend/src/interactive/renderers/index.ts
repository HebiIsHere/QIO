/**
 * 渲染器注册表：**每个格式一个模块，按需动态 import**（契约 §3.5）。
 *
 * 这样 pdfjs-dist（几百 KB）与其它格式的视图代码都不会进首屏包：
 * 只有真的展开一张 PDF / 文档卡片时才会加载对应模块。
 *
 * 每个模块导出：
 *   * RENDERER —— 渲染器名（与契约 §3.5 的 renderer 取值一致）
 *   * view(data) —— 后端结构化数据 → 视图模型（形状不对就返回 { ok:false, reason }）
 * pdf 额外导出 openPdf(bytes)（逐页渲染）。
 */

export type { RendererName, RenderResult } from "./types";

/** 每个渲染器一个动态 import 入口（静态可分析，打包器据此拆包）。 */
export const RENDERER_LOADERS = {
  image: () => import("./image"),
  pdf: () => import("./pdf"),
  docx: () => import("./docx"),
  xlsx: () => import("./xlsx"),
  pptx: () => import("./pptx"),
  code: () => import("./code"),
} as const;

export type RendererKey = keyof typeof RENDERER_LOADERS;

export function isRendererKey(name: string): name is RendererKey {
  return Object.prototype.hasOwnProperty.call(RENDERER_LOADERS, name);
}

/** 动态加载一个渲染器模块（名字不认识时抛可读错误，不返回半个模块）。 */
export async function loadRenderer<M = unknown>(name: string): Promise<M> {
  if (!isRendererKey(name)) {
    throw new Error("没有这个渲染器：" + name);
  }
  return (await RENDERER_LOADERS[name]()) as M;
}

/**
 * 释放一个渲染器持有的一次性资源（契约 §3.5：收起 / 换附件 / 卸载时释放）。
 *
 * 渲染器模块**可以**导出 `dispose()`；没有导出就是没有需要释放的东西 ——
 * 这里不制造一个假接口，也不因为释放失败打断收起（释放是收尾，不是功能）。
 * 调用方（CardInlinePreview）仍然负责自己那一层：blob URL、canvas、pdf 文档对象。
 */
export function disposeRenderer(name: string): void {
  if (!isRendererKey(name)) return;
  void (async () => {
    try {
      const module = (await RENDERER_LOADERS[name]()) as { dispose?: () => void };
      module.dispose?.();
    } catch {
      /* 模块加载不了 / 释放抛错：收起不该因此失败 */
    }
  })();
}
