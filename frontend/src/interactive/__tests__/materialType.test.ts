/**
 * 互动模式不再区分「文件 / 图片」卡片（2026-10-14 第九个问题）。
 *
 * 两者是同一种材料：卡片样式与标签一律**按附件类型**决定。
 * - 遗留的 `kind="image"` 在归一化时逐张迁移成 `file`，`meta.image` 搬到 `meta.file`
 *   （与历史上 code / url → text 同一套做法；后端本来就两种都认）；
 * - 添加菜单不再有「图片」一项；
 * - 类型标签按附件类型显示：图片 / PDF / 表格 / 演示文稿 / 文档 / 文本 / 代码 / 其它。
 */
import { describe, expect, it } from "vitest";
import { ADDABLE_CARD_KINDS, emptyState, normalizeState } from "../board";
import { materialTypeLabel } from "../previewPlan";

const STAMP = "2026-10-14T00:00:00.000Z";

function rawCard(patch: Record<string, unknown>) {
  return {
    id: "c1",
    kind: "file",
    content: "",
    meta: {},
    x: 10,
    y: 10,
    w: 260,
    h: 170,
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

describe("文件与图片合并成同一种材料", () => {
  it("添加菜单不再提供「图片」", () => {
    expect(ADDABLE_CARD_KINDS).toEqual(["text", "file"]);
  });

  it("遗留 kind=image 迁移成 file，并把 meta.image 搬到 meta.file", () => {
    const state = normalizeState({
      ...emptyState("board_t"),
      cards: [rawCard({ kind: "image", meta: { name: "照片.jpeg", image: { attachmentId: "att_1", name: "照片.jpeg" } } })],
    } as never);
    const card = state.cards[0];
    expect(card.kind).toBe("file");
    expect((card.meta as Record<string, unknown>).file).toEqual({ attachmentId: "att_1", name: "照片.jpeg" });
    // 老键保留（读老数据仍然可用），不丢信息
    expect((card.meta as Record<string, unknown>).image).toEqual({ attachmentId: "att_1", name: "照片.jpeg" });
  });

  it("已经迁移过的卡片不会被再改一次", () => {
    const once = normalizeState({ ...emptyState("board_t"), cards: [rawCard({ kind: "image", meta: { image: { name: "a.png" } } })] } as never);
    const twice = normalizeState(once);
    expect(twice.cards[0].kind).toBe("file");
    expect(twice.cards[0].meta).toEqual(once.cards[0].meta);
  });

  it("类型标签按附件类型显示", () => {
    const label = (name: string) => materialTypeLabel("file", { file: { name, attachmentId: "att" } });
    expect(label("照片.jpeg")).toBe("图片");
    expect(label("报告.pdf")).toBe("PDF");
    expect(label("台账.xlsx")).toBe("表格");
    expect(label("数据.csv")).toBe("表格");
    expect(label("讲稿.pptx")).toBe("演示文稿");
    expect(label("简报.docx")).toBe("文档");
    expect(label("main.py")).toBe("代码");
    expect(label("readme.md")).toBe("文本");
    expect(label("archive.7z")).toBe("文件");
    // 没有附件时仍是「文件」；非材料卡不抢标签
    expect(materialTypeLabel("file", {})).toBe("文件");
    expect(materialTypeLabel("text", { file: { name: "a.png" } })).toBe("");
  });
});
