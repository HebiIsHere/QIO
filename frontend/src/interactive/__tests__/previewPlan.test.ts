/**
 * 卡片内预览的**计划**（纯函数，契约 §3.5 冻结形状）。
 *
 * 测的是"这张卡现在能不能内嵌看、用什么渲染器看、看不了的时候说什么"，
 * 不涉及 DOM / 网络。三条诚实规则逐条落到断言上：
 *   * 没关联附件 → 没有预览、没有假按钮；
 *   * 失败 / 不支持 → 真实原因 + 打开/下载入口；
 *   * 拿不到后端 kind 时按扩展名先判（不会把 docx 说成"未知"）。
 */
import { describe, expect, it } from "vitest";

import {
  LEGACY_OFFICE_REASON,
  cardAttachmentId,
  cardFileName,
  cardSizeBytes,
  kindForName,
  previewPlan,
  previewPlanActions,
  previewRefFor,
  previewStatusText,
  rendererFor,
} from "../previewPlan";
import type { BoardCard } from "../types";

function card(meta: Record<string, unknown>, kind: BoardCard["kind"] = "file"): BoardCard {
  return {
    id: "c1",
    kind,
    content: "",
    meta,
    x: 0,
    y: 0,
    w: 320,
    h: 200,
    checked: false,
    hidden: false,
    folded: false,
    bookmarked: false,
    deleted: false,
    createdAt: "2026-10-12T00:00:00+00:00",
    updatedAt: "2026-10-12T00:00:00+00:00",
  };
}

function fileCard(name: string, id = "att_1"): BoardCard {
  return card({ name, file: { name, attachmentId: id, size: 2048 } });
}

function imageCard(name: string, id = "att_img"): BoardCard {
  return card({ name, image: { name, attachmentId: id, size: 4096 } }, "image");
}

const NO_STATE = { status: "idle" as const };
const BROWSER = { desktop: false };
const DESKTOP = { desktop: true };

describe("previewPlan：没有关联附件", () => {
  it("不给预览、不给假按钮，并说清「名字不等于正文」", () => {
    const plan = previewPlan(card({ name: "报告.docx" }), NO_STATE, BROWSER);
    expect(plan.mode).toBe("unsupported");
    expect(plan.renderer).toBe("none");
    expect(plan.actions).toEqual([]);
    expect(plan.reason).toContain("报告.docx");
    expect(plan.reason).toContain("读不到它的正文");
  });

  it("连文件名都没有时也有可读原因", () => {
    const plan = previewPlan(card({}), NO_STATE, DESKTOP);
    expect(plan.mode).toBe("unsupported");
    expect(plan.reason).toContain("还没有关联已保存的附件");
  });
});

describe("previewPlan：内嵌渲染", () => {
  const cases: [string, string][] = [
    ["照片.png", "image"],
    ["报告.pdf", "pdf"],
    ["说明.docx", "docx"],
    ["账目.xlsx", "xlsx"],
    ["预算.xlsm", "xlsx"],
    ["路演.pptx", "pptx"],
    ["脚本.py", "code"],
    ["说明.md", "code"],
    ["配置.json", "code"],
  ];
  for (const [name, renderer] of cases) {
    it(name + " → inline / " + renderer, () => {
      const plan = previewPlan(fileCard(name), { status: "ready", kind: kindForName(name) }, BROWSER);
      expect(plan.mode).toBe("inline");
      expect(plan.renderer).toBe(renderer);
    });
  }

  it("后端还没回答时也先知道该用哪个渲染器（按扩展名）", () => {
    const plan = previewPlan(fileCard("报告.docx"), { status: "loading" }, BROWSER);
    expect(plan.mode).toBe("inline");
    expect(plan.renderer).toBe("docx");
  });

  it("html/xml/svg 走「转换层返回文本」，绝不按 HTML 内嵌", () => {
    for (const name of ["页面.html", "数据.xml", "图标.svg"]) {
      const plan = previewPlan(fileCard(name), { status: "ready", kind: "code" }, BROWSER);
      expect(plan.mode).toBe("inline");
      expect(plan.renderer).toBe("code");
    }
  });

  it("后端给的 kind 优先于扩展名（文件被改名也按真实内容渲染）", () => {
    const plan = previewPlan(fileCard("未命名.bin"), { status: "ready", kind: "docx" }, BROWSER);
    expect(plan.renderer).toBe("docx");
  });
});

describe("previewPlan：看不了的时候", () => {
  it("转换失败 → external + 后端给的真实原因 + 打开/下载入口", () => {
    const plan = previewPlan(
      fileCard("损坏.docx"),
      { status: "failed", kind: "docx", reason: "这个文件不是有效的 Office 压缩包" },
      BROWSER,
    );
    expect(plan.mode).toBe("external");
    expect(plan.renderer).toBe("none");
    expect(plan.reason).toBe("这个文件不是有效的 Office 压缩包");
    expect(plan.actions.map((a) => a.kind)).toContain("view");
    expect(plan.actions.map((a) => a.kind)).toContain("download");
  });

  it("旧版 Office → external + 旧版 Office 的真实原因", () => {
    const plan = previewPlan(fileCard("年度总结.doc"), { status: "ready", kind: "legacy_office" }, BROWSER);
    expect(plan.mode).toBe("external");
    expect(plan.reason).toBe(LEGACY_OFFICE_REASON);
    expect(plan.actions.length).toBeGreaterThan(0);
  });

  it("未支持的类型 → external + 原因，且仍有打开/下载入口", () => {
    const plan = previewPlan(
      fileCard("归档.zip"),
      { status: "unsupported", kind: "unsupported", reason: "压缩包不解压：不做内嵌预览（可以打开或下载）" },
      BROWSER,
    );
    expect(plan.mode).toBe("external");
    expect(plan.reason).toContain("压缩包");
    // .zip 不在可内联查看名单里：浏览器里只剩「下载副本」这一条真动作
    expect(plan.actions.map((a) => a.kind)).toEqual(["download"]);
  });

  it("后端没给原因时也有兜底原因（不让界面空着）", () => {
    const plan = previewPlan(fileCard("神秘.xyz"), { status: "unsupported" }, BROWSER);
    expect(plan.reason.length).toBeGreaterThan(0);
  });
});

describe("previewPlan：动作与环境一致", () => {
  it("浏览器 + 副本：查看 + 下载", () => {
    const actions = previewPlanActions(previewRefFor(fileCard("报告.docx")), BROWSER);
    expect(actions.map((a) => a.kind)).toEqual(["view", "download"]);
    expect(actions[0].note).toContain("副本");
  });

  it("内部类型（.zip）在浏览器里只能下载 —— 不假装能内联看", () => {
    const actions = previewPlanActions(previewRefFor(fileCard("归档.zip")), BROWSER);
    expect(actions.map((a) => a.kind)).toEqual(["download"]);
  });

  it("卡片上没有本地路径时，桌面端也只能查看/下载副本（不猜一个会报错的路径）", () => {
    const actions = previewPlanActions(previewRefFor(fileCard("报告.docx")), DESKTOP);
    expect(actions.map((a) => a.kind)).toEqual(["view", "download"]);
  });
});

describe("previewPlan：meta 读取", () => {
  it("image 优先于 file，且下划线写法也认", () => {
    expect(cardAttachmentId(imageCard("图.png", "att_a"))).toBe("att_a");
    expect(cardAttachmentId(card({ file: { attachment_id: "att_b" } }))).toBe("att_b");
    expect(cardAttachmentId(card({}))).toBe("");
  });

  it("文件名与大小按同一优先级取", () => {
    expect(cardFileName(imageCard("图.png"))).toBe("图.png");
    expect(cardSizeBytes(imageCard("图.png"))).toBe(4096);
    expect(cardSizeBytes(card({}))).toBeNull();
  });

  it("kindForName / rendererFor 对未知类型给 unsupported + none（不猜）", () => {
    expect(kindForName("a.xyz")).toBe("unsupported");
    expect(rendererFor(undefined, "a.xyz")).toBe("none");
    expect(rendererFor("legacy_office", "a.doc")).toBe("none");
  });
});

describe("previewStatusText：五件事分开说", () => {
  it("把附件已保存 / 预览可用 / 不可用分开报", () => {
    expect(previewStatusText({ status: "idle" }, 2048)).toBe("附件已保存（2.0 KB）");
    expect(previewStatusText({ status: "loading" }, null)).toBe("正在生成预览…");
    expect(previewStatusText({ status: "ready" }, 2048)).toBe("预览可用（2.0 KB）");
    expect(previewStatusText({ status: "failed" }, null)).toBe("预览不可用");
    expect(previewStatusText({ status: "unsupported" }, null)).toBe("不做内嵌预览");
  });
});
