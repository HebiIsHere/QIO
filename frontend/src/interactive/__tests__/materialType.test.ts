/**
 * 互动模式不再区分「文件 / 图片」卡片（2026-10-14 第九个问题；2026-10-15 图片展示简化）。
 *
 * 材料只有**文件卡**：图片与其它文件都是它的附件，卡片样式与标签一律按附件类型决定。
 * - 遗留的 `kind="image"` **不再迁移**（本轮明确不做旧图片卡兼容）：按既有未知 kind 兜底成
 *   `text`，`meta.image` 原样保留但不再当作附件来源；
 * - 添加菜单不再有「图片」一项；
 * - 类型标签按附件类型显示：图片 / PDF / 表格 / 演示文稿 / 文档 / 文本 / 代码 / 其它。
 */
import { describe, expect, it } from "vitest";
import { attachmentPreviewPlan } from "../attachmentPreview";
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

  it("遗留 kind=image 不再迁移成 file（按未知 kind 兜底成 text），也不搬 meta.image", () => {
    const state = normalizeState({
      ...emptyState("board_t"),
      cards: [rawCard({ kind: "image", meta: { name: "照片.jpeg", image: { attachmentId: "att_1", name: "照片.jpeg" } } })],
    } as never);
    const card = state.cards[0];
    expect(card.kind).toBe("text");
    expect((card.meta as Record<string, unknown>).file).toBeUndefined();
    // 老键原样保留（不丢信息），但不再是附件来源
    expect((card.meta as Record<string, unknown>).image).toEqual({ attachmentId: "att_1", name: "照片.jpeg" });
  });

  it("归一化是幂等的：同一份遗留数据再跑一遍结果不变", () => {
    const once = normalizeState({ ...emptyState("board_t"), cards: [rawCard({ kind: "image", meta: { image: { name: "a.png" } } })] } as never);
    const twice = normalizeState(once);
    expect(twice.cards[0].kind).toBe("text");
    expect(twice.cards[0].meta).toEqual(once.cards[0].meta);
  });

  /*
    回归（2026-10-14 用户报「图片预览点一下就消失」）：
    能不能当图片预览只看**附件扩展名**，与 kind 无关（材料卡现在只有 file）。
  */
  it("文件卡上的图片附件仍然可预览（kind 不再决定预览）", () => {
    const photo = { attachmentId: "att_1", name: "照片.jpeg" };
    const asFile = attachmentPreviewPlan("file", { file: photo }, { desktop: false });
    expect(asFile.state).toBe("linked");
    expect(asFile.imagePreviewable).toBe(true);
    // 旧键 meta.image 不再是附件来源（本轮不做旧图片卡兼容）
    const legacyKey = attachmentPreviewPlan("file", { image: photo }, { desktop: false });
    expect(legacyKey.state).toBe("unlinked");
    expect(legacyKey.imagePreviewable).toBe(false);
    // 非图片扩展名不许当图片预览
    const pdf = attachmentPreviewPlan("file", { file: { attachmentId: "att_2", name: "报告.pdf" } }, { desktop: false });
    expect(pdf.imagePreviewable).toBe(false);
  });

  it("类型标签按附件类型显示；旧 kind=image 不再抢标签", () => {
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
    // 没有附件时仍是「文件」；非材料卡与旧 kind 不抢标签
    expect(materialTypeLabel("file", {})).toBe("文件");
    expect(materialTypeLabel("text", { file: { name: "a.png" } })).toBe("");
    expect(materialTypeLabel("image", { file: { name: "a.png" } })).toBe("");
  });
});
