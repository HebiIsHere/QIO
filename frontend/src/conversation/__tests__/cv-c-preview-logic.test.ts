/**
 * C 预览纯逻辑定向用例（身份 / 去重 / 位置 / 状态映射 / 搜索 / 版本竞争）。
 *
 * 断言行为而不是文案实现细节：同 id 复用、同名不同 id 不合并、关闭只关一个、
 * 位置按标签独立且坏值被夹取、失败分类区分缺失/无权限/过大/解析失败/不支持、
 * 迟到请求不得覆盖新版本。
 */
import { describe, expect, it } from "vitest";
import {
  attachmentPreviewTarget,
  attachmentRefForTarget,
  classifyPreviewProblem,
  clampPreviewPosition,
  closePreviewTab,
  createPreviewVersionGuard,
  highlightSegments,
  isFileVersionChanged,
  mergePreviewPosition,
  openPreviewTab,
  positionForKind,
  previewKindForName,
  replyCodePreviewTarget,
  searchCodeView,
  searchDocxView,
  searchPdfTextPages,
  searchPptxView,
  searchTextLines,
  searchXlsxView,
  taskOutputPreviewTarget,
  PREVIEW_KIND_LABEL,
} from "../preview";
import type { PreviewTabState } from "../previewTypes";
import type { CodeView } from "../../interactive/renderers/code";
import type { DocxView } from "../../interactive/renderers/docx";
import type { PptxView } from "../../interactive/renderers/pptx";
import type { XlsxView } from "../../interactive/renderers/xlsx";

let tabSeq = 0;
function tab(id: string, title: string, position: PreviewTabState["position"] = {}): PreviewTabState {
  tabSeq += 1;
  return {
    id,
    target: {
      id,
      kind: "code",
      title,
      source: "attachment",
      attachmentId: "att-" + tabSeq,
    },
    position,
  };
}

function codeView(lines: string[]): CodeView {
  return {
    language: "python",
    languageLabel: "Python",
    lines: lines.map((text, index) => ({ number: index + 1, text })),
    totalLines: lines.length,
    encoding: "utf-8",
    truncated: false,
  };
}

describe("格式分类", () => {
  it("按文件名给出内嵌预览类别（含旧版 Office 与音视频）", () => {
    expect(previewKindForName("图.png")).toBe("image");
    expect(previewKindForName("报告.pdf")).toBe("pdf");
    expect(previewKindForName("方案.docx")).toBe("docx");
    expect(previewKindForName("数据.xlsx")).toBe("xlsx");
    expect(previewKindForName("提案.pptx")).toBe("pptx");
    expect(previewKindForName("说明.md")).toBe("markdown");
    expect(previewKindForName("a.txt")).toBe("text");
    expect(previewKindForName("a.py")).toBe("code");
    expect(previewKindForName("数据.csv")).toBe("text");
    expect(previewKindForName("旧文档.doc")).toBe("unsupported");
    expect(previewKindForName("旧表.xls")).toBe("unsupported");
    expect(previewKindForName("录音.mp3")).toBe("audio");
    expect(previewKindForName("短片.mp4")).toBe("video");
    expect(PREVIEW_KIND_LABEL.markdown).toBe("Markdown");
  });
});

describe("标签身份：不能只用文件名", () => {
  it("附件身份带附件 id；版本只影响内容版本，不同文件名不再是身份", () => {
    const a = attachmentPreviewTarget({ attachmentId: "att1", title: "同名.pdf", version: "sha256:x" });
    expect(a.id).toBe("attachment:att1@sha256:x");
    expect(a.kind).toBe("pdf");
    const b = attachmentPreviewTarget({ attachmentId: "att2", title: "同名.pdf", version: "sha256:x" });
    expect(openPreviewTab([], a).tab.id).not.toBe(openPreviewTab([], b).tab.id);
  });

  it("同一附件同一版本再打开 → 定位已有标签（不新增）", () => {
    const target = attachmentPreviewTarget({ attachmentId: "att1", title: "a.pdf", version: "v1" });
    const first = openPreviewTab([], target);
    const second = openPreviewTab(first.tabs, target);
    expect(first.reused).toBe(false);
    expect(second.reused).toBe(true);
    expect(second.tabs).toHaveLength(1);
    expect(second.activeId).toBe(target.id);
  });

  it("同一附件的新版本复用原标签（位置保留、顺序不变），不新增第二个标签", () => {
    const older = tab("attachment:att1@v1", "a.pdf", { page: 3, zoom: 1.5 });
    older.target = attachmentPreviewTarget({ attachmentId: "att1", title: "a.pdf", version: "v1" });
    const newerTarget = attachmentPreviewTarget({ attachmentId: "att1", title: "a.pdf", version: "v2" });
    const opened = openPreviewTab([older], newerTarget);
    expect(opened.reused).toBe(true);
    expect(opened.tabs).toHaveLength(1);
    expect(opened.tabs[0].id).toBe("attachment:att1@v2");
    expect(opened.tabs[0].position).toEqual({ page: 3, zoom: 1.5 });
    expect(opened.previousId).toBe("attachment:att1@v1");
  });

  it("同名不同附件 → 两个标签，互不合并", () => {
    const first = attachmentPreviewTarget({ attachmentId: "attA", title: "同名.pdf", version: "v1" });
    const second = attachmentPreviewTarget({ attachmentId: "attB", title: "同名.pdf", version: "v1" });
    const one = openPreviewTab([], first);
    const two = openPreviewTab(one.tabs, second);
    expect(two.tabs).toHaveLength(2);
    expect(two.tabs.map((item) => item.target.attachmentId)).toEqual(["attA", "attB"]);
  });

  it("回复代码块身份包含消息与块序号：不同块各自一个标签，同一块复用", () => {
    const block0 = replyCodePreviewTarget({ messageId: "m1", blockIndex: 0, language: "python" });
    const block1 = replyCodePreviewTarget({ messageId: "m1", blockIndex: 1 });
    expect(block0.id).toBe("replycode:m1:0");
    const one = openPreviewTab([], block0);
    const two = openPreviewTab(one.tabs, block1);
    expect(two.tabs).toHaveLength(2);
    const again = openPreviewTab(two.tabs, block0);
    expect(again.tabs).toHaveLength(2);
    expect(again.activeId).toBe(block0.id);
    expect(again.reused).toBe(true);
  });

  it("任务成果身份包含任务与附件/路径：不同任务的同名成果不合并", () => {
    const a = taskOutputPreviewTarget({ taskId: "t1", title: "结果.csv", attachmentId: "att1" });
    const b = taskOutputPreviewTarget({ taskId: "t2", title: "结果.csv", attachmentId: "att1" });
    expect(a.id).toBe("task:t1:att1");
    expect(b.id).toBe("task:t2:att1");
    const one = openPreviewTab([], a);
    expect(openPreviewTab(one.tabs, b).tabs).toHaveLength(2);
  });
});

describe("关闭标签：只关这一份预览", () => {
  const tabs = [tab("a", "a"), tab("b", "b"), tab("c", "c")];

  it("关闭当前标签优先激活右邻居，其次左邻居", () => {
    expect(closePreviewTab(tabs, "b", "b")).toMatchObject({ activeId: "c", closed: true });
    expect(closePreviewTab(tabs, "c", "c")).toMatchObject({ activeId: "b", closed: true });
    expect(closePreviewTab([tab("x", "x")], "x", "x")).toMatchObject({ activeId: null, closed: true });
  });

  it("关闭非当前标签不影响激活项", () => {
    const result = closePreviewTab(tabs, "b", "c");
    expect(result.activeId).toBe("b");
    expect(result.tabs.map((item) => item.id)).toEqual(["a", "b"]);
  });

  it("关闭不存在的标签是无操作", () => {
    const result = closePreviewTab(tabs, "a", "missing");
    expect(result.closed).toBe(false);
    expect(result.tabs).toHaveLength(3);
  });
});

describe("阅读位置：每标签独立、坏值夹取", () => {
  it("非法位置字段被丢弃/夹取，不把 NaN 写进偏好", () => {
    expect(clampPreviewPosition({ scrollTop: -10, page: 0, zoom: 99, line: Number.NaN })).toEqual({
      scrollTop: 0,
      page: 1,
      zoom: 4,
      line: undefined,
    });
    expect(clampPreviewPosition({ page: 12.7, zoom: 1.25, search: "  needle  " })).toEqual({
      page: 13,
      zoom: 1.25,
      search: "needle",
    });
  });

  it("合并位置只覆盖传入的键", () => {
    expect(mergePreviewPosition({ page: 3, zoom: 2 }, { page: 5 })).toEqual({ page: 5, zoom: 2 });
  });

  it("按类别只保留相关键（PDF 页码不会带到代码标签上）", () => {
    expect(positionForKind("pdf", { page: 4, sheet: "S", search: "x", zoom: 1.5 })).toEqual({
      page: 4,
      zoom: 1.5,
      scrollTop: undefined,
    });
    expect(positionForKind("code", { page: 4, search: "x" })).toEqual({ search: "x" });
  });
});

describe("失败 / 不支持：分类与可用入口", () => {
  it("404 / 缺失：明确的缺失态，不给假打开入口", () => {
    const problem = classifyPreviewProblem({
      httpStatus: 404,
      message: "没有这个附件",
      hasOpenTarget: true,
    });
    expect(problem.kind).toBe("missing");
    expect(problem.openable).toBe(false);
    expect(problem.retryable).toBe(false);
  });

  it("401/403 是无权限（可重试），409 引用型与副本丢失分开", () => {
    expect(classifyPreviewProblem({ httpStatus: 403, message: "本机 API 拒绝" }).kind).toBe("permission");
    const reference = classifyPreviewProblem({
      httpStatus: 409,
      message: "这是「引用本地文件」的附件：QIO 没有保存副本",
      hasOpenTarget: true,
    });
    expect(reference.kind).toBe("reference_only");
    expect(reference.openable).toBe(true);
    expect(reference.downloadable).toBe(false);
    const gone = classifyPreviewProblem({
      httpStatus: 409,
      message: "QIO 保存的副本文件已经不在了",
      hasOpenTarget: true,
    });
    expect(gone.kind).toBe("content_missing");
  });

  it("过大 / 解析失败 / 旧版 Office / 普通不支持 各自区分", () => {
    expect(
      classifyPreviewProblem({ status: "failed", message: "这个文件 80 MB 超过预览转换上限 64 MB" }).kind,
    ).toBe("too_large");
    expect(
      classifyPreviewProblem({ status: "failed", message: "这个文件的内容含二进制字节（NUL），不是文本" }).kind,
    ).toBe("parse_failed");
    const legacy = classifyPreviewProblem({
      status: "unsupported",
      message: "旧版 Office（.doc/.xls/.ppt）是二进制格式，不能内嵌预览",
      hasOpenTarget: true,
    });
    expect(legacy.kind).toBe("legacy_office");
    expect(legacy.openable).toBe(true);
    expect(classifyPreviewProblem({ status: "unsupported", message: "不支持这种格式", hasOpenTarget: true }).kind).toBe(
      "unsupported",
    );
  });

  it("更新/正文被清理时单独说明，不让用户以为文件是空的", () => {
    const archived = classifyPreviewProblem({
      unavailableReason: "这份任务输出已被保留策略清理（超过保留天数）",
      hasCopyText: false,
    });
    expect(archived.kind).toBe("archived");
    expect(archived.detail).toContain("保留策略");
    expect(archived.retryable).toBe(false);
  });

  it("网络超时是可重试的网络态", () => {
    const network = classifyPreviewProblem({ message: "请求在 90 秒内没有响应" });
    expect(network.kind).toBe("network");
    expect(network.retryable).toBe(true);
  });
});

describe("版本竞争：旧请求不得覆盖新版本", () => {
  it("后发起的请求序号更大；旧序号的结果一律丢弃", () => {
    const guard = createPreviewVersionGuard();
    const first = guard.begin();
    const second = guard.begin();
    expect(guard.isCurrent(first)).toBe(false);
    expect(guard.isCurrent(second)).toBe(true);
    guard.reset();
    expect(guard.latest()).toBe(0);
    const afterReset = guard.begin();
    expect(guard.isCurrent(afterReset)).toBe(true);
  });

  it("只有真实不同的版本才算更新（空版本不能误报文件已更新）", () => {
    expect(isFileVersionChanged("sha256:a", "sha256:b")).toBe(true);
    expect(isFileVersionChanged("sha256:a", "sha256:a")).toBe(false);
    expect(isFileVersionChanged("sha256:a", "")).toBe(false);
    expect(isFileVersionChanged("", "sha256:a")).toBe(false);
  });
});

describe("文件内容搜索", () => {
  it("代码按行命中（同一行多处分别计数）并给定位", () => {
    const result = searchCodeView(codeView(["needle needle", "nothing", "needle"]), "needle");
    expect(result.hits).toHaveLength(3);
    expect(result.hits[0]).toMatchObject({ line: 1, label: "第 1 行" });
    expect(result.hits[2]).toMatchObject({ line: 3 });
  });

  it("高亮切片大小写不敏感且不丢原文", () => {
    expect(highlightSegments("Hello World", "world")).toEqual([
      { text: "Hello ", hit: false },
      { text: "World", hit: true },
    ]);
    expect(highlightSegments("no match", "needle")).toEqual([{ text: "no match", hit: false }]);
  });

  it("纯文本 / DOCX / XLSX / PPTX 各自给出可跳转的命中", () => {
    expect(searchTextLines(["a", "needle"], "needle").hits[0]).toMatchObject({ line: 2 });
    const docx: DocxView = {
      blocks: [
        { type: "paragraph", level: null, ordered: null, runs: [], text: "含 needle 的段落", marker: "" },
        { type: "heading", level: 1, ordered: null, runs: [], text: "标题", marker: "H1" },
      ],
      tables: [{ rows: [["普通", "needle 单元格"]], columns: 2 }],
      truncated: false,
      empty: false,
    };
    const docxHits = searchDocxView(docx, "needle");
    expect(docxHits.hits).toHaveLength(2);
    expect(docxHits.hits[1].label).toContain("表格 1");

    const xlsx: XlsxView = {
      sheets: [
        {
          name: "Sheet1",
          rows: [
            {
              index: 1,
              cells: [
                {
                  ref: "A1",
                  column: "A",
                  row: 1,
                  value: "needle",
                  formula: null,
                  cached: null,
                  kind: "value",
                  display: "needle",
                  formulaNote: "",
                },
                {
                  ref: "B1",
                  column: "B",
                  row: 1,
                  value: null,
                  formula: "SUM(A1:A2)",
                  cached: "10",
                  kind: "formula",
                  display: "=SUM(A1:A2)",
                  formulaNote: "缓存值：10",
                },
              ],
            },
          ],
          columns: ["A", "B"],
          rowCount: 1,
          columnCount: 2,
        },
      ],
      truncated: false,
      empty: false,
    };
    expect(searchXlsxView(xlsx, "needle").hits[0]).toMatchObject({ sheet: "Sheet1", cell: "A1" });
    expect(searchXlsxView(xlsx, "SUM").hits).toHaveLength(1);

    const pptx: PptxView = {
      slides: [
        { index: 1, title: "第一页", texts: [], notes: [], images: [], tables: [] },
        { index: 2, title: "第二页", texts: ["讲到了 needle"], notes: [], images: [], tables: [] },
      ],
      truncated: false,
      empty: false,
    };
    expect(searchPptxView(pptx, "needle").hits[0]).toMatchObject({ page: 2, label: "第 2 张幻灯片" });
  });

  it("PDF 逐页文字搜索：按页命中、没搜完的页如实标 truncated", () => {
    const result = searchPdfTextPages(["封面", "第一页有 needle", "needle 和 needle"], "needle", { maxPages: 3 });
    expect(result.hits).toHaveLength(3);
    expect(result.hits[0]).toMatchObject({ page: 2, label: "第 2 页" });
    expect(result.hits[2]).toMatchObject({ page: 3 });
    expect(result.totalPages).toBe(3);
    expect(result.truncated).toBe(false);
    // 超过 maxPages 时只搜前 N 页，并明确报告（不假装搜了全文）
    const capped = searchPdfTextPages(["needle", "needle", "needle"], "needle", { maxPages: 2 });
    expect(capped.scannedPages).toBe(2);
    expect(capped.truncated).toBe(true);
    expect(capped.hits.every((hit) => (hit.page ?? 0) <= 2)).toBe(true);
  });

  it("命中超过上限时如实标 truncated（不把前 200 条当全部）", () => {
    const lines = Array.from({ length: 250 }, () => "needle");
    const result = searchCodeView(codeView(lines), "needle");
    expect(result.hits).toHaveLength(200);
    expect(result.truncated).toBe(true);
  });
});

describe("外部打开用的附件引用", () => {
  it("附件目标 → 已保存副本；只有路径的任务成果 → 引用型；回复代码没有可打开文件", () => {
    const attachment = attachmentPreviewTarget({ attachmentId: "att1", title: "a.pdf", size: 10 });
    expect(attachmentRefForTarget(attachment)).toMatchObject({ id: "att1", kind: "copy", state: "ready" });
    const fileTarget = taskOutputPreviewTarget({
      taskId: "t1",
      title: "结果.md",
      filePath: "C:/work/out.md",
      kind: "markdown",
    });
    expect(attachmentRefForTarget(fileTarget)).toMatchObject({
      kind: "reference",
      storedPath: "C:/work/out.md",
    });
    const reply = replyCodePreviewTarget({ messageId: "m1", blockIndex: 0 });
    expect(attachmentRefForTarget(reply)).toBeNull();
  });
});
