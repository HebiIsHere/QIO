import { describe, expect, it, afterEach } from "vitest";
import { applyQioTableMode, syncQioTables } from "../useQioTables";

/**
 * 表格两态的 **DOM 行为**测试：可用宽度与内容真实最小宽度决定呈现，
 * 切换只改呈现（保留原表头与单元格内容、补齐可访问语义），不改数据。
 *
 * jsdom 没有布局引擎，所以这里显式注入 wrap.clientWidth 与 table 的 min-content 宽度，
 * 验的是判定→应用这条真实链路；真实像素边界由浏览器验收阶段测量。
 */
const hosts: HTMLElement[] = [];

function makeTable(opts: { cols: number; rows?: number; minContentPx: number; availablePx: number }) {
  const host = document.createElement("div");
  document.body.appendChild(host);
  hosts.push(host);
  const wrap = document.createElement("div");
  wrap.className = "table-wrap qio-table-wrap";
  wrap.setAttribute("data-mode", "table");
  const rows = opts.rows ?? 2;
  const header = Array.from({ length: opts.cols }, (_v, i) => '<th data-label="第 ' + (i + 1) + ' 列">c' + (i + 1) + "</th>").join("");
  const body = Array.from({ length: rows }, () =>
    "<tr>" + Array.from({ length: opts.cols }, (_v, i) => '<td data-label="第 ' + (i + 1) + ' 列">v' + i + "</td>").join("") + "</tr>",
  ).join("");
  wrap.innerHTML = "<table><thead><tr>" + header + "</tr></thead><tbody>" + body + "</tbody></table>";
  host.appendChild(wrap);
  Object.defineProperty(wrap, "clientWidth", { configurable: true, get: () => opts.availablePx });
  const table = wrap.querySelector("table") as HTMLElement;
  // min-content 探针会临时把 width 设成 min-content，此时返回真实最小宽度
  table.getBoundingClientRect = (() =>
    ({ width: table.style.width === "min-content" ? opts.minContentPx : opts.availablePx, height: 0, top: 0, left: 0, right: 0, bottom: 0, x: 0, y: 0, toJSON() {} }) as DOMRect) as typeof table.getBoundingClientRect;
  return wrap;
}

afterEach(() => {
  for (const host of hosts.splice(0)) host.remove();
});

describe("applyQioTableMode", () => {
  it("可用宽度充足时保持表格形态，去掉记录模式的显式角色", () => {
    const wrap = makeTable({ cols: 3, minContentPx: 600, availablePx: 900 });
    expect(applyQioTableMode(wrap)).toBe("table");
    expect(wrap.getAttribute("data-mode")).toBe("table");
    const table = wrap.querySelector("table")!;
    expect(table.hasAttribute("role")).toBe(false);
    expect(wrap.querySelector("tr")!.hasAttribute("role")).toBe(false);
  });

  it("列数多导致每列过窄时切到记录模式，并补齐表格语义", () => {
    const wrap = makeTable({ cols: 12, minContentPx: 400, availablePx: 600 });
    expect(applyQioTableMode(wrap)).toBe("records");
    expect(wrap.getAttribute("data-mode")).toBe("records");
    const table = wrap.querySelector("table") as HTMLElement;
    expect(table.getAttribute("role")).toBe("table");
    expect(wrap.querySelector("tr")!.getAttribute("role")).toBe("row");
    expect(wrap.querySelector("th")!.getAttribute("role")).toBe("columnheader");
    expect(wrap.querySelector("td")!.getAttribute("role")).toBe("cell");
    // 列名与值都还在（重排只改呈现，不丢内容）
    expect(wrap.querySelector("td")!.getAttribute("data-label")).toBe("第 1 列");
    expect(wrap.querySelector("td")!.textContent).toBe("v0");
  });

  it("内容真实最小宽度超过可用宽度时切到记录模式（不裁切、不横向滚动）", () => {
    const wrap = makeTable({ cols: 2, minContentPx: 1400, availablePx: 700 });
    expect(applyQioTableMode(wrap)).toBe("records");
  });

  it("宽回来且放得下时退回表格形态（带滞回，不振荡）", () => {
    const wrap = makeTable({ cols: 3, minContentPx: 600, availablePx: 900 });
    expect(applyQioTableMode(wrap)).toBe("table");
    Object.defineProperty(wrap, "clientWidth", { configurable: true, get: () => 500 });
    expect(applyQioTableMode(wrap)).toBe("records");
    Object.defineProperty(wrap, "clientWidth", { configurable: true, get: () => 1200 });
    expect(applyQioTableMode(wrap)).toBe("table");
  });

  it("列数超过 4 时用固定布局（保证不超宽是布局结果，而不是裁切结果）", () => {
    const wrap = makeTable({ cols: 6, minContentPx: 500, availablePx: 900 });
    applyQioTableMode(wrap);
    expect(wrap.getAttribute("data-qio-table-wide")).toBe("1");
    const narrow = makeTable({ cols: 3, minContentPx: 300, availablePx: 900 });
    applyQioTableMode(narrow);
    expect(narrow.getAttribute("data-qio-table-wide")).toBe("0");
  });
});

describe("syncQioTables", () => {
  it("扫描根节点下所有表格（普通对话与互动对话共用同一份判定）", () => {
    const root = document.createElement("div");
    document.body.appendChild(root);
    hosts.push(root);
    const a = makeTable({ cols: 2, minContentPx: 400, availablePx: 800 });
    const b = makeTable({ cols: 10, minContentPx: 300, availablePx: 500 });
    root.appendChild(a);
    root.appendChild(b);
    syncQioTables(root);
    expect(a.getAttribute("data-mode")).toBe("table");
    expect(b.getAttribute("data-mode")).toBe("records");
  });
});
