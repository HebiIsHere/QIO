/**
 * 文件卡源引用信息面板（R9 契约 §6 / §8）—— 定向测试。
 *
 * 覆盖：文件名/类型/引用 id/来源/可用性/用户说明都可见；「作为互动材料」开关只调
 * PATCH /api/attachments/{id}/material 并把事实写回卡片 meta；缺失时「重新指定位置」
 * 必须先让用户确认「这是同一个文件」，确认后才带 confirmed_identity=true。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import type { AttachmentRef } from "../../../services/attachments";

const mocks = vi.hoisted(() => ({
  getAttachment: vi.fn(),
  setAttachmentMaterialEnabled: vi.fn(),
  pickLocalSource: vi.fn(),
  relocateAttachment: vi.fn(),
}));

vi.mock("../../../services/attachments", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../services/attachments")>();
  return {
    ...actual,
    getAttachment: mocks.getAttachment,
    setAttachmentMaterialEnabled: mocks.setAttachmentMaterialEnabled,
    pickLocalSource: mocks.pickLocalSource,
    relocateAttachment: mocks.relocateAttachment,
  };
});

import FileCardMaterialPanel from "../FileCardMaterialPanel.vue";

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

const META = {
  name: "季度报告.docx",
  file: {
    name: "季度报告.docx",
    attachmentId: "att_1",
    size: 2048,
    referenceId: "ref_1",
    sourceKind: "desktop_path",
    origin: "user_original",
    availability: "available",
    materialEnabled: true,
    userNote: "第三季度材料",
  },
};

function mountPanel() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return mount(FileCardMaterialPanel, {
    props: { cardId: "card_1", meta: META, attachmentId: "att_1", desktop: true },
    global: { plugins: [pinia] },
  });
}

beforeEach(() => {
  for (const fn of Object.values(mocks)) fn.mockReset();
  mocks.getAttachment.mockResolvedValue(ref());
});

describe("源引用信息展示", () => {
  it("文件名 / 类型 / 引用 id / 来源 / 可用性 / 用户说明都可见", async () => {
    const w = mountPanel();
    await flushPromises();
    expect(w.get('[data-im="file-card-name"]').text()).toContain("季度报告.docx");
    expect(w.get('[data-im="file-card-name"]').text()).toContain("文档");
    expect(w.get('[data-im="file-card-reference-id"]').text()).toBe("ref_1");
    const source = w.get('[data-im="file-card-source"]').text();
    expect(source).toContain("桌面选择");
    expect(source).toContain("你的原文件");
    expect(source).toContain("引用本地源文件");
    expect(source).toContain("不复制");
    expect(w.get('[data-im="file-card-availability"]').text()).toContain("可用");
    expect(w.get('[data-im="file-card-user-note"]').text()).toContain("第三季度材料");
    expect(mocks.getAttachment).toHaveBeenCalledWith("att_1");
    w.unmount();
  });

  it("还没关联附件时如实说 QIO 读不到正文（不拿文件名冒充）", async () => {
    const w = mount(FileCardMaterialPanel, {
      props: { cardId: "card_2", meta: {}, attachmentId: "", desktop: true },
      global: { plugins: [createPinia()] },
    });
    expect(w.text()).toContain("还没有关联源文件");
    expect(w.text()).toContain("读不到它的正文");
    w.unmount();
  });
});

describe("「作为互动材料」开关：只控制互动上下文", () => {
  it("点击后调 PATCH 接口，并把 material_enabled=false 写回卡片 meta", async () => {
    const w = mountPanel();
    await flushPromises();
    mocks.setAttachmentMaterialEnabled.mockResolvedValue(ref({ materialEnabled: false }));

    const toggle = w.get('[data-im="file-card-material-switch"]');
    expect(toggle.attributes("aria-checked")).toBe("true");
    await toggle.trigger("click");
    await flushPromises();

    expect(mocks.setAttachmentMaterialEnabled).toHaveBeenCalledWith("att_1", false);
    const emitted = w.emitted("patch-meta") as [Record<string, any>, string][];
    expect(emitted.length).toBeGreaterThan(0);
    const meta = emitted[emitted.length - 1][0];
    expect(meta.file.materialEnabled).toBe(false);
    expect(w.get('[data-im="file-card-material-label"]').text()).toContain("不主动纳入");
    w.unmount();
  });

  it("接口失败：开关恢复原值并说明没保存（不假装已关闭）", async () => {
    const w = mountPanel();
    await flushPromises();
    mocks.setAttachmentMaterialEnabled.mockRejectedValue(new Error("PATCH -> 500：boom"));

    await w.get('[data-im="file-card-material-switch"]').trigger("click");
    await flushPromises();
    expect(w.get('[data-im="file-card-material-switch"]').attributes("aria-checked")).toBe("true");
    expect(w.get('[data-im="file-card-action-error"]').text()).toContain("开关没有保存");
    w.unmount();
  });
});

describe("源文件缺失：重新指定位置（需用户确认同一文件）", () => {
  it("展示原因与入口；选择新路径后先确认，确认才带 confirmed_identity=true 重新定位", async () => {
    mocks.getAttachment.mockResolvedValue(
      ref({ availability: "missing", missingReason: "文件不在原位" }),
    );
    mocks.pickLocalSource.mockResolvedValue({ path: "D:\\新位置\\季度报告.docx", name: "季度报告.docx", sizeBytes: 2048 });
    mocks.relocateAttachment.mockResolvedValue(
      ref({ availability: "relocated", missingReason: null, sourceKind: "desktop_path" }),
    );

    const w = mountPanel();
    await flushPromises();
    const availability = w.get('[data-im="file-card-availability"]').text();
    expect(availability).toContain("缺失");
    expect(availability).toContain("文件不在原位");

    await w.get('[data-im="file-card-relocate"]').trigger("click");
    await flushPromises();
    expect(mocks.pickLocalSource).toHaveBeenCalledTimes(1);
    expect(w.text()).toContain("这是同一个文件吗？");
    expect(w.text()).toContain("D:\\新位置\\季度报告.docx");
    // 未确认前不得调用 relocate
    expect(mocks.relocateAttachment).not.toHaveBeenCalled();

    const confirm = w.findAll("button").find((b) => b.text().includes("是，继续引用这个文件"));
    expect(confirm).toBeTruthy();
    await confirm!.trigger("click");
    await flushPromises();
    expect(mocks.relocateAttachment).toHaveBeenCalledWith(
      "att_1",
      "D:\\新位置\\季度报告.docx",
      { confirmedIdentity: true },
    );
    expect(w.get('[data-im="file-card-action-note"]').text()).toContain("已重新指定位置");
    w.unmount();
  });

  it("取消确认：不发请求，原引用保持不变", async () => {
    mocks.getAttachment.mockResolvedValue(ref({ availability: "missing" }));
    mocks.pickLocalSource.mockResolvedValue({ path: "D:\\x\\a.docx", name: "a.docx", sizeBytes: 1 });
    const w = mountPanel();
    await flushPromises();
    await w.get('[data-im="file-card-relocate"]').trigger("click");
    await flushPromises();
    const cancel = w.findAll("button").find((b) => b.text() === "取消");
    expect(cancel).toBeTruthy();
    await cancel!.trigger("click");
    await flushPromises();
    expect(mocks.relocateAttachment).not.toHaveBeenCalled();
    w.unmount();
  });
});
