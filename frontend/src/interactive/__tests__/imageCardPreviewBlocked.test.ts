/**
 * [互动模式图片展示简化 · 一期] 图片卡不许进入卡片内阅读态（store 层硬闸门）。
 *
 * 触发：图片不再有独立的 `kind="image"` 卡片类型，也不再做卡片内展开阅读。
 * 本文件把两件事钉在同一份判定口径上（materials.cardAttachmentIsImage →
 * 复用 isPreviewableImageName，不另建名单）：
 *
 *   1. store：`toggleCardPreview` 对「附件是位图的文件卡」返回 false 且**不加入**；
 *      `expandedCardIds` / `previewExpandedCount` / `isCardPreviewExpanded` /
 *      `collapseAllPreviews` 都不把图片算进去（BoardCanvas 的临时布局输入与
 *      InteractiveView 的「收起预览（N）」计数直接读这几个出口）；
 *   2. board 变化时重新判定：PDF 换成图片必须退出原有展开态（只移除这些 id，
 *      不打断其它文件正在进行的阅读）；图片换成 PDF 必须能正常展开；
 *   3. 归一化：遗留 `kind="image"` 不再迁移成 file，也不搬运 `meta.image`。
 */
import { beforeEach, describe, expect, it } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useInteractiveStore } from "../../stores/interactive";
import {
  CARD_KIND_LABELS,
  MATERIAL_KINDS,
  defaultCardSeed,
  emptyState,
  normalizeState,
} from "../board";
import { cardAttachmentIsImage, isAttachableKind } from "../materials";
import type { BoardCard, BoardState } from "../types";

const STAMP = "2026-10-15T00:00:00.000Z";

function mkCard(
  id: string,
  kind: BoardCard["kind"],
  meta: Record<string, unknown>,
  patch: Partial<BoardCard> = {},
): BoardCard {
  return {
    id,
    kind,
    content: "",
    meta,
    x: 0,
    y: 0,
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

const IMAGE_META = { name: "照片.png", file: { name: "照片.png", attachmentId: "att_img", size: 2048 } };
const PDF_META = { name: "报告.pdf", file: { name: "报告.pdf", attachmentId: "att_pdf", size: 4096 } };

function imageCard(id = "img_1"): BoardCard {
  return mkCard(id, "file", { ...IMAGE_META });
}

function pdfCard(id = "pdf_1"): BoardCard {
  return mkCard(id, "file", { ...PDF_META });
}

function seedState(cards: BoardCard[]): BoardState {
  return { ...emptyState("board_t"), seq: 3, cards };
}

function withMeta(state: BoardState, cardId: string, meta: Record<string, unknown>): BoardState {
  return { ...state, cards: state.cards.map((card) => (card.id === cardId ? { ...card, meta } : card)) };
}

// ---------------------------------------------------------------------------
// 1. 判定口径只有一份：materials.cardAttachmentIsImage（复用 isPreviewableImageName）
// ---------------------------------------------------------------------------

describe("cardAttachmentIsImage：附件是位图的文件卡才为 true", () => {
  it("file + 已关联的位图附件 → true；大小写扩展名也认", () => {
    expect(cardAttachmentIsImage(IMAGE_META, "file")).toBe(true);
    expect(cardAttachmentIsImage({ file: { name: "照片.JPG", attachmentId: "att" } }, "file")).toBe(true);
  });

  it("非位图 / 没有关联附件 / 非文件卡 → false（不给图片阅读态，也不许拿文件名冒充）", () => {
    expect(cardAttachmentIsImage(PDF_META, "file")).toBe(false);
    expect(cardAttachmentIsImage({ file: { name: "图.svg", attachmentId: "att_svg" } }, "file")).toBe(false);
    expect(cardAttachmentIsImage({ file: { name: "照片.png" } }, "file")).toBe(false);
    expect(cardAttachmentIsImage(IMAGE_META, "text")).toBe(false);
    expect(cardAttachmentIsImage(null, "file")).toBe(false);
    // 旧键 meta.image 不再是回退来源（本轮不做旧图片卡兼容）
    expect(cardAttachmentIsImage({ image: { name: "照片.png", attachmentId: "att_img" } }, "file")).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// 2. store：展开列表绝不放图片进来
// ---------------------------------------------------------------------------

describe("store：图片卡不进展开列表（不能只靠隐藏按钮）", () => {
  let store: ReturnType<typeof useInteractiveStore>;

  beforeEach(() => {
    localStorage.clear();
    setActivePinia(createPinia());
    store = useInteractiveStore();
  });

  it("kind=file + meta.file 位图：toggleCardPreview 返回 false，且不出现在任何展开出口", () => {
    store.board = seedState([imageCard()]);
    expect(store.toggleCardPreview("img_1")).toBe(false);
    expect(store.isCardPreviewExpanded("img_1")).toBe(false);
    expect(store.expandedCardIds).toEqual([]);
    expect(store.previewExpandedCount).toBe(0);
    // 反复点也不会漏进来
    expect(store.toggleCardPreview("img_1")).toBe(false);
    expect(store.previewExpandedCount).toBe(0);
  });

  it("PDF 文件卡仍能展开并计入计数（阅读能力不被本轮削掉）", () => {
    store.board = seedState([pdfCard()]);
    expect(store.toggleCardPreview("pdf_1")).toBe(true);
    expect(store.isCardPreviewExpanded("pdf_1")).toBe(true);
    expect(store.expandedCardIds).toEqual(["pdf_1"]);
    expect(store.previewExpandedCount).toBe(1);
    expect(store.collapseAllPreviews()).toBe(1);
    expect(store.previewExpandedCount).toBe(0);
  });

  it("同时存在 PDF 与图片卡：collapseAllPreviews 只收起 PDF（返回 1，不把图片算进去）", () => {
    store.board = seedState([pdfCard(), imageCard()]);
    store.toggleCardPreview("pdf_1");
    store.toggleCardPreview("img_1");
    expect(store.previewExpandedCount).toBe(1);
    expect(store.collapseAllPreviews()).toBe(1);
    expect(store.expandedCardIds).toEqual([]);
    expect(store.collapseAllPreviews()).toBe(0);
  });

  it("PDF 改成图片附件后自动退出展开；图片改回 PDF 能再展开；其它文件的阅读不被打断", () => {
    store.board = seedState([pdfCard("pdf_a"), pdfCard("pdf_b")]);
    expect(store.toggleCardPreview("pdf_a")).toBe(true);
    expect(store.toggleCardPreview("pdf_b")).toBe(true);
    expect(store.expandedCardIds).toEqual(["pdf_a", "pdf_b"]);

    // board 变化：pdf_a 重新关联成图片附件
    store.board = withMeta(store.board!, "pdf_a", { ...IMAGE_META });
    expect(store.isCardPreviewExpanded("pdf_a")).toBe(false);
    expect(store.isCardPreviewExpanded("pdf_b")).toBe(true);
    expect(store.expandedCardIds).toEqual(["pdf_b"]);
    expect(store.previewExpandedCount).toBe(1);

    // 反向：pdf_a 又换成 PDF 附件 → 可以正常展开（不是被永久拉黑）
    store.board = withMeta(store.board!, "pdf_a", { ...PDF_META });
    expect(store.toggleCardPreview("pdf_a")).toBe(true);
    expect(store.expandedCardIds).toEqual(["pdf_b", "pdf_a"]);
    expect(store.previewExpandedCount).toBe(2);
  });

  it("图片卡不进展开态，也不写板面状态、不产生表达、不发请求", () => {
    store.board = seedState([imageCard()]);
    const before = JSON.stringify(store.board);
    store.toggleCardPreview("img_1");
    store.collapseAllPreviews();
    expect(JSON.stringify(store.board)).toBe(before);
    expect(store.dirty).toBe(false);
    expect(store.submissions).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// 3. 归一化：遗留 kind="image" 不再迁移，也不搬运 meta.image
// ---------------------------------------------------------------------------

describe("归一化：本轮不做旧图片卡兼容", () => {
  it("遗留 kind=image 不再产出 file（按既有未知 kind 兜底成 text），也不搬 meta.image", () => {
    const state = normalizeState({
      ...emptyState("board_t"),
      cards: [
        {
          ...mkCard("c1", "file", {}),
          kind: "image",
          meta: { name: "照片.jpeg", image: { attachmentId: "att_1", name: "照片.jpeg" } },
        },
      ],
    } as never);
    const card = state.cards[0];
    expect(card.kind).toBe("text");
    expect((card.meta as Record<string, unknown>).file).toBeUndefined();
    // 老键原样保留（不丢信息，但也不当作附件来源）
    expect((card.meta as Record<string, unknown>).image).toEqual({ attachmentId: "att_1", name: "照片.jpeg" });
  });

  it("kind=file 的卡片不再从 meta.image 搬一份到 meta.file", () => {
    const state = normalizeState({
      ...emptyState("board_t"),
      cards: [{ ...mkCard("c1", "file", {}), meta: { name: "照片.png", image: { attachmentId: "att_1" } } }],
    } as never);
    expect((state.cards[0].meta as Record<string, unknown>).file).toBeUndefined();
  });

  it("类型表里没有 image：MATERIAL_KINDS 只剩 file，标签表没有图片项，isAttachableKind 不认 image", () => {
    expect(MATERIAL_KINDS).toEqual(["file"]);
    expect(Object.keys(CARD_KIND_LABELS)).not.toContain("image");
    expect(isAttachableKind("image")).toBe(false);
    expect(isAttachableKind("file")).toBe(true);
    // defaultCardSeed 不再有 image 分支（未知 kind 走既有兜底，不自动变成文件卡）
    const seed = defaultCardSeed("image" as never);
    expect(seed.kind).toBe("image");
    expect(seed.meta).toEqual({});
    expect(seed.content).toBe("");
    // fe-card 依赖的占位正文不能变
    expect(defaultCardSeed("file").content).toBe("待补充文件说明");
  });
});
