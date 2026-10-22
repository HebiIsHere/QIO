/**
 * 文件卡源引用纯函数（R9 契约 §6 / §8）—— 定向测试。
 *
 * 覆盖：payload → 卡片 meta 的只引用不复制、卡片复制/撤销重做共享同一 attachmentId、
 * 删卡只标记卡片（源文件信息不丢失）、来源/可用性/材料开关的诚实文案。
 */
import { describe, expect, it } from "vitest";
import type { AttachmentRef } from "../../services/attachments";
import {
  MATERIAL_SWITCH_HINT,
  fileCardRefFromAttachment,
  fileCardRefFromMeta,
  fileMetaFromAttachment,
  materialEnabledOf,
  mediaTypeLabel,
  mergeAttachmentIntoMeta,
  readFileBlock,
  relocateConfirmDetail,
  relocateConfirmTitle,
  withMaterialEnabled,
} from "../fileCardRef";
import { addCard, duplicateCard, emptyState, removeCard } from "../board";

function ref(overrides: Partial<AttachmentRef> = {}): AttachmentRef {
  return {
    id: "att_1",
    name: "季度报告.docx",
    sizeBytes: 2048,
    kind: "reference",
    display: "引用本地文件",
    state: "ready",
    error: null,
    attachmentId: "att_1",
    filename: "季度报告.docx",
    mediaKind: "document",
    origin: "user_original",
    sourceKind: "desktop_path",
    availability: "available",
    referenceId: "ref_1",
    materialEnabled: true,
    userNote: "第三季度材料",
    ...overrides,
  };
}

describe("附件事实 → 卡片 meta（只存引用，不复制内容）", () => {
  it("meta 里只有引用 id 与展示事实；来源/归属/可用性/用户说明原样带上", () => {
    const block = fileMetaFromAttachment(ref());
    expect(block.attachmentId).toBe("att_1");
    expect(block.referenceId).toBe("ref_1");
    expect(block.sourceKind).toBe("desktop_path");
    expect(block.origin).toBe("user_original");
    expect(block.availability).toBe("available");
    expect(block.userNote).toBe("第三季度材料");
    expect(JSON.stringify(block)).not.toContain("PK");
  });

  it("合并进卡片 meta 时保留其它字段（doc / title 不被吃掉）", () => {
    const meta = { doc: { blocks: [] }, title: "原卡片标题", file: { materialEnabled: false, attachmentId: "old" } };
    // payload 没给 material_enabled（老数据 / 兼容回退）时才沿用卡片上的选择
    const merged = mergeAttachmentIntoMeta(meta, { ...ref(), materialEnabled: undefined });
    expect(merged.title).toBe("原卡片标题");
    expect(merged.doc).toEqual({ blocks: [] });
    expect(readFileBlock(merged)?.attachmentId).toBe("att_1");
    // 用户在卡片上手动关掉的开关，不能因为一次重新关联被悄悄打开
    expect(materialEnabledOf(merged)).toBe(false);
  });

  it("material_enabled=false 优先于卡片上的旧选择；默认开启", () => {
    const merged = mergeAttachmentIntoMeta({}, ref({ materialEnabled: false }));
    expect(materialEnabledOf(merged)).toBe(false);
    expect(materialEnabledOf({})).toBe(true);
    const off = withMaterialEnabled(merged, true);
    expect(materialEnabledOf(off)).toBe(true);
  });
});

describe("复制 / 撤销重做 / 删卡：共享引用不被破坏", () => {
  const base = addCard(emptyState("b1"), {
    kind: "file",
    content: "待补充文件说明",
    meta: { name: "季度报告.docx", file: { name: "季度报告.docx", attachmentId: "att_1", size: 2048 } },
  });
  const cardId = base.cards[0].id;

  it("复制卡片共享同一个 attachmentId（副本不改名、不复制内容）", () => {
    const copied = duplicateCard(base, cardId);
    expect(copied.cards).toHaveLength(2);
    const [source, copy] = copied.cards;
    expect(copy.id).not.toBe(source.id);
    expect(readFileBlock(copy.meta)?.attachmentId).toBe("att_1");
    expect(readFileBlock(source.meta)?.attachmentId).toBe("att_1");
  });

  it("撤销/重做是整份 JSON 快照：恢复后 references 仍然指向同一个 attachmentId", () => {
    const before = JSON.stringify(base);
    const after = duplicateCard(base, cardId);
    // 撤销 = 回到 before 快照；重做 = 再前进到 after 快照
    const undone = JSON.parse(before) as typeof base;
    expect(undone.cards).toHaveLength(1);
    expect(readFileBlock(undone.cards[0].meta)?.attachmentId).toBe("att_1");
    const redone = JSON.parse(JSON.stringify(after)) as typeof after;
    expect(redone.cards.map((c) => readFileBlock(c.meta)?.attachmentId)).toEqual(["att_1", "att_1"]);
  });

  it("删卡只把卡片标为 deleted：引用信息仍在，源文件不会被删除", () => {
    const removed = removeCard(base, cardId);
    expect(removed.cards[0].deleted).toBe(true);
    expect(readFileBlock(removed.cards[0].meta)?.attachmentId).toBe("att_1");
  });
});

describe("来源 / 可用性 / 类型 / 材料开关文案", () => {
  it("桌面选择 → 引用本地源文件（不复制不移动）；浏览器上传 → 工作区材料并标注来源", () => {
    const desktop = fileCardRefFromAttachment(ref());
    expect(desktop.sourceLabel).toBe("桌面选择");
    expect(desktop.sourceNote).toContain("引用本地源文件");
    expect(desktop.sourceNote).toContain("不复制");
    expect(desktop.originLabel).toBe("你的原文件");

    const upload = fileCardRefFromAttachment(
      ref({ sourceKind: "browser_upload", origin: "user_saved_material", availability: "available" }),
    );
    expect(upload.sourceLabel).toBe("浏览器上传");
    expect(upload.sourceNote).toContain("已保存为工作区材料源文件（来源：浏览器上传）");
    expect(upload.originLabel).toBe("QIO 首次落盘的材料");
  });

  it("缺失 / 待确认时明确说需要重新指定位置，不假装可用", () => {
    const missing = fileCardRefFromAttachment(
      ref({ availability: "missing", missingReason: "文件不在原位" }),
    );
    expect(missing.missing).toBe(true);
    expect(missing.availabilityText).toContain("缺失");
    expect(missing.availabilityText).toContain("文件不在原位");
    const ambiguous = fileCardRefFromAttachment(ref({ availability: "ambiguous" }));
    expect(ambiguous.availabilityText).toContain("需要你确认");
    expect(ambiguous.missing).toBe(true);
  });

  it("类型标签按 media_kind / 扩展名；引用 id 与材料开关语义固定", () => {
    expect(mediaTypeLabel("a.pdf")).toBe("PDF");
    expect(mediaTypeLabel("a.xlsx")).toBe("表格");
    expect(mediaTypeLabel("a.png")).toBe("图片");
    expect(mediaTypeLabel("a.unknown", "document")).toBe("文档");
    const view = fileCardRefFromAttachment(ref());
    expect(view.referenceId).toBe("ref_1");
    expect(view.materialEnabled).toBe(true);
    expect(MATERIAL_SWITCH_HINT).toContain("不是文件访问权限");
    expect(MATERIAL_SWITCH_HINT).toContain("互动上下文");
  });

  it("没有关联附件时能从 meta 兜底（明确不是最新服务端核对结果）", () => {
    const fallback = fileCardRefFromMeta({
      file: { name: "旧材料.pdf", attachmentId: "att_old", sourceKind: "browser_upload", availability: "missing" },
    });
    expect(fallback?.attachmentId).toBe("att_old");
    expect(fallback?.sourceLabel).toBe("浏览器上传");
    expect(fileCardRefFromMeta({})).toBeNull();
  });

  it("重新指定位置的确认文案写出新路径与「同一文件」判断", () => {
    expect(relocateConfirmTitle()).toContain("同一个文件");
    const detail = relocateConfirmDetail("D:\\材料\\报告.pdf");
    expect(detail).toContain("D:\\材料\\报告.pdf");
    expect(detail).toContain("同一个文件");
    expect(detail).toContain("取消后原引用保持不变");
  });
});
