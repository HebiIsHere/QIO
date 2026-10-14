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

  /*
    回归（2026-10-14 用户报「图片预览点一下就消失」）：
    P48 把 kind=image 迁移成 file 之后，图片预览判定仍写着 `isImage = kind === "image"`，
    于是第一次客户端写入触发迁移后，图片卡就再也不可预览 —— 预览「消失」。
    正确口径：能不能当图片预览只看**附件扩展名**，与 kind 无关。
  */
  it("合并成 file 之后，图片附件仍然可预览（kind 不再决定预览）", () => {
    const photo = { attachmentId: "att_1", name: "照片.jpeg" };
    const asFile = attachmentPreviewPlan("file", { file: photo }, { desktop: false });
    expect(asFile.state).toBe("linked");
    expect(asFile.imagePreviewable).toBe(true);
    // 遗留形状（kind=image + meta.image）也仍然可预览
    const legacy = attachmentPreviewPlan("image", { image: photo }, { desktop: false });
    expect(legacy.imagePreviewable).toBe(true);
    // 只有 kind 迁移、meta 还是老键时也要读得到（与后端同一口径）
    const halfMigrated = attachmentPreviewPlan("file", { image: photo }, { desktop: false });
    expect(halfMigrated.state).toBe("linked");
    expect(halfMigrated.imagePreviewable).toBe(true);
    // 非图片扩展名不许当图片预览
    const pdf = attachmentPreviewPlan("file", { file: { attachmentId: "att_2", name: "报告.pdf" } }, { desktop: false });
    expect(pdf.imagePreviewable).toBe(false);
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
