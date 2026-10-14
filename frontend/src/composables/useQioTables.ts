/**
 * 对话表格适配的 **DOM 部分**（判定部分在 utils/qioTable.ts，纯函数、有单测）。
 *
 * 职责：测量表格**实际所属正文**的可用宽度与内容真实最小宽度，按阈值（带滞回）
 * 在「表格」与「记录」两种呈现之间切换；切换只改呈现，不改数据（复制/导出仍用原数据）。
 *
 * 用法：
 * - `useQioTables(rootRef)` —— 组件里挂在渲染表格的根节点上（MarkdownContent 等）；
 * - `syncQioTables(root)`   —— 一次性扫描（测试或手动时机）。
 *
 * 观察方式：ResizeObserver（根 + 每个表格容器）+ MutationObserver（childList/subtree/characterData），
 * 合并到一帧一次测量；卸载时全部释放。不做轮询，不做全页扫描。
 */
import { onBeforeUnmount, watch, type Ref } from "vue";
import { decideQioTableMode, type QioTableMode } from "../utils/qioTable";

export const QIO_TABLE_WRAP_CLASS = "qio-table-wrap";
export const QIO_TABLE_WRAP_SELECTOR = "." + QIO_TABLE_WRAP_CLASS;
/** 记录模式标记（CSS 与测试都读它）。 */
export const QIO_TABLE_MODE_ATTR = "data-mode";

function countColumns(table: Element): number {
  const firstRow = table.querySelector("tr");
  if (!firstRow) return 0;
  return firstRow.children.length;
}

/**
 * 表格内容的真实最小宽度（min-content）。
 * 用「临时把 table 宽度设成 min-content 再读回」的方式，读到的是浏览器自己算出的
 * 真实最小宽度（不是估字符数），并且读取后立刻还原 —— 同一帧内不产生可见跳动。
 */
function measureNaturalWidth(table: HTMLElement): number {
  const prevWidth = table.style.width;
  const prevLayout = table.style.tableLayout;
  try {
    table.style.tableLayout = "auto";
    table.style.width = "min-content";
    const width = table.getBoundingClientRect().width;
    return Number.isFinite(width) ? Math.ceil(width) : 0;
  } catch {
    return 0;
  } finally {
    table.style.width = prevWidth;
    table.style.tableLayout = prevLayout;
  }
}

/** 记录模式下补齐显式语义：display 重排后仍能作为表格被辅助技术遍历。 */
function applySemantics(table: HTMLElement, mode: QioTableMode) {
  const rows = table.querySelectorAll("tr");
  const cells = table.querySelectorAll("th, td");
  if (mode === "records") {
    table.setAttribute("role", "table");
    rows.forEach((r) => r.setAttribute("role", "row"));
    table.querySelectorAll("th").forEach((c) => {
      c.setAttribute("role", "columnheader");
      if (!c.hasAttribute("scope")) c.setAttribute("scope", "col");
    });
    table.querySelectorAll("td").forEach((c) => c.setAttribute("role", "cell"));
  } else {
    table.removeAttribute("role");
    table.querySelectorAll("tr, th, td").forEach((el) => el.removeAttribute("role"));
  }
  void cells;
}

/** 判定并应用单个表格的模式。返回最终模式（便于测试断言）。 */
export function applyQioTableMode(wrap: HTMLElement): QioTableMode {
  const table = wrap.querySelector("table") as HTMLElement | null;
  if (!table) return "table";
  const availablePx = wrap.clientWidth;
  const columnCount = countColumns(table);
  const current: QioTableMode =
    wrap.getAttribute(QIO_TABLE_MODE_ATTR) === "records" ? "records" : "table";
  const naturalPx = columnCount > 1 ? measureNaturalWidth(table) : 0;
  const mode = decideQioTableMode({ availablePx, columnCount, naturalPx, current });
  if (wrap.getAttribute(QIO_TABLE_MODE_ATTR) !== mode) {
    wrap.setAttribute(QIO_TABLE_MODE_ATTR, mode);
    applySemantics(table, mode);
  }
  // 列数多时用固定布局：等宽列 + 任意位置换行，保证「不超宽」是布局结果而不是裁切结果
  wrap.setAttribute("data-qio-table-wide", columnCount > 4 ? "1" : "0");
  return mode;
}

/** 扫描根节点下的所有 QIO 表格并刷新模式。 */
export function syncQioTables(root: HTMLElement | null | undefined): void {
  if (!root) return;
  const wraps = root.querySelectorAll<HTMLElement>(QIO_TABLE_WRAP_SELECTOR);
  wraps.forEach((wrap) => applyQioTableMode(wrap));
}

export interface UseQioTablesOptions {
  /** 观测开关，默认 true。 */
  observe?: boolean;
}

/**
 * 在组件里使用：把渲染表格的根节点 ref 传进来，剩下的（测量、观察、清理）都在这。
 * 返回值 refresh() 可在必要时手动重算。
 */
export function useQioTables(
  root: Ref<HTMLElement | null | undefined>,
  options: UseQioTablesOptions = {},
): { refresh: () => void } {
  const observe = options.observe !== false;
  let rafId = 0;
  let ro: ResizeObserver | null = null;
  let mo: MutationObserver | null = null;
  let observed = new Set<Element>();

  function run() {
    rafId = 0;
    const el = root.value;
    if (!el || !el.isConnected) return;
    syncQioTables(el);
  }

  function schedule() {
    if (rafId) return;
    if (typeof requestAnimationFrame === "function") rafId = requestAnimationFrame(run);
    else rafId = setTimeout(run, 16) as unknown as number;
  }

  function syncObserved(el: HTMLElement) {
    if (!ro) return;
    const wraps = new Set<Element>(Array.from(el.querySelectorAll(QIO_TABLE_WRAP_SELECTOR)));
    wraps.add(el);
    for (const node of wraps) {
      if (!observed.has(node)) {
        ro.observe(node);
        observed.add(node);
      }
    }
    for (const node of [...observed]) {
      if (!wraps.has(node) || !node.isConnected) {
        ro.unobserve(node);
        observed.delete(node);
      }
    }
  }

  function teardown() {
    if (rafId) {
      if (typeof cancelAnimationFrame === "function") cancelAnimationFrame(rafId);
      clearTimeout(rafId as unknown as ReturnType<typeof setTimeout>);
      rafId = 0;
    }
    ro?.disconnect();
    ro = null;
    mo?.disconnect();
    mo = null;
    observed = new Set();
  }

  const stopWatch = watch(
    root,
    (el) => {
      teardown();
      if (!el) return;
      if (observe && typeof ResizeObserver === "function") {
        ro = new ResizeObserver(() => schedule());
        syncObserved(el);
      }
      if (observe && typeof MutationObserver === "function") {
        mo = new MutationObserver(() => {
          syncObserved(el);
          schedule();
        });
        mo.observe(el, { childList: true, subtree: true, characterData: true });
      }
      schedule();
    },
    { immediate: true, flush: "post" },
  );

  onBeforeUnmount(() => {
    stopWatch();
    teardown();
  });

  return { refresh: schedule };
}
