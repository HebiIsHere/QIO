/**
 * #09（PM-QIO032）：关系端点命名。
 * 规则：用户命名 > 真实文件名 > 正文首行；同名补区分线索；空正文占位文案不能当端点名。
 * 这里直接读 buildDisplayLinks 产出的无障碍名称（画布连线层用的唯一标签来源）。
 */
import { describe, expect, it } from "vitest";
import { buildDisplayLinks, endpointCardName } from "../../../interactive/linkEndpoints";
import type { BoardCard, BoardLink } from "../../../interactive/types";

const STAMP = "2026-10-14T00:00:00.000Z";

function mkCard(id: string, patch: Partial<BoardCard> = {}): BoardCard {
  return {
    id,
    kind: "text",
    content: "",
    meta: {},
    x: 0,
    y: 0,
    w: 240,
    h: 160,
    checked: false,
    hidden: false,
    folded: false,
    bookmarked: false,
    deleted: false,
    createdAt: STAMP,
    updatedAt: STAMP,
    ...patch,
  };
}

function mkLink(id: string, src: string, dst: string): BoardLink {
  return {
    id,
    src,
    dst,
    meaning: "关联",
    direction: false,
    curve: { x: 0, y: 0 },
    deleted: false,
    createdAt: STAMP,
    updatedAt: STAMP,
  };
}

function labels(cards: BoardCard[], links: BoardLink[]): string[] {
  const result = buildDisplayLinks({ links, cards });
  return result.links.map((link) => link.ariaLabel);
}

describe("#09 端点命名优先级", () => {
  it("用户命名优先于真实文件名", () => {
    expect(
      endpointCardName({ kind: "file", meta: { title: "我的命名", file: { name: "原文件.pdf" } } }),
    ).toBe("我的命名");
  });

  it("没有用户命名时用真实文件名", () => {
    expect(endpointCardName({ kind: "file", meta: { file: { name: "季度报告.docx" } } })).toBe("季度报告.docx");
  });

  it("空正文不拿占位文案当名字", () => {
    expect(endpointCardName({ kind: "text", content: "   \n  " })).toBe("");
  });

  it("正文首行可以作为端点名（真实内容）", () => {
    expect(endpointCardName({ kind: "text", content: "会议纪要\n第二行" })).toBe("会议纪要");
  });
});

describe("#09 关系端点标签（aria 名称）", () => {
  it("用户命名 / 文件名显示在关系两端，空正文端点只给类型线索", () => {
    const cards = [
      mkCard("c1", { content: "会议纪要" }),
      mkCard("c2", { kind: "file", meta: { file: { name: "季度报告.docx" } } }),
      mkCard("c3", { content: "   " }),
    ];
    const result = labels(cards, [mkLink("l1", "c1", "c2"), mkLink("l2", "c1", "c3")]);
    expect(result[0]).toContain("会议纪要");
    expect(result[0]).toContain("季度报告.docx");
    expect(result[1]).not.toContain("空白");
    expect(result[1]).not.toContain("无内容");
    expect(result[1]).toContain("文字注释");
  });

  it("同名端点补「第 N 张」区分线索", () => {
    const cards = [
      mkCard("c1", { content: "周报" }),
      mkCard("c2", { content: "周报" }),
    ];
    const result = labels(cards, [mkLink("l1", "c1", "c2")]);
    expect(result[0]).toContain("第1张");
    expect(result[0]).toContain("第2张");
  });
});
