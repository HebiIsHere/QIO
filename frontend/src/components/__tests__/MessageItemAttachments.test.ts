/**
 * 问题 5（前端主入口）：历史消息上的附件必须能打开 / 重新定位，状态如实显示。
 *
 * 修复前红：MessageItem 只渲染名称 / 大小 / 状态文字，没有任何打开或重新定位入口，
 * 事件也没有接到附件服务上。
 *
 * 运行：cd frontend; npx vitest run src/components/__tests__/MessageItemAttachments.test.ts
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia, type Pinia } from "pinia";
import MessageItem from "../MessageItem.vue";
import AttachmentChip from "../AttachmentChip.vue";
import type { StreamMessage } from "../../stores/session";
import type { AttachmentRef } from "../../services/attachments";

const {
  pickLocalPath,
  relocateAttachment,
  retryAttachment,
  openAttachment,
} = vi.hoisted(() => ({
  pickLocalPath: vi.fn(async () => "D:\\docs\\report.pdf"),
  relocateAttachment: vi.fn(async (id: string, path: string) => ({
    id,
    name: "report.pdf",
    sizeBytes: 2048,
    kind: "reference" as const,
    display: "引用本地文件",
    state: "ready" as const,
    error: null,
    // 便于断言真的是用新路径重新校验过
    sourcePath: path,
  })),
  retryAttachment: vi.fn(async (id: string) => ({
    id,
    name: "report.pdf",
    sizeBytes: 2048,
    kind: "copy" as const,
    display: "已保存副本",
    state: "ready" as const,
    error: null,
  })),
  openAttachment: vi.fn(async (_ref: AttachmentRef) => undefined),
}));

vi.mock("../../services/attachments", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../services/attachments")>();
  return {
    ...actual,
    pickLocalPath,
    relocateAttachment,
    retryAttachment,
    openAttachment,
  };
});

function makeMessage(over: Partial<StreamMessage> & { role: StreamMessage["role"] }): StreamMessage {
  return {
    id: "m1",
    content: "看看这个文件",
    contentType: "text",
    createdAt: "2026-10-06T08:00:00+00:00",
    ...over,
  };
}

function attachment(over: Partial<AttachmentRef> = {}): AttachmentRef {
  return {
    id: "att_1",
    name: "report.pdf",
    sizeBytes: 2048,
    kind: "copy",
    display: "已保存副本",
    state: "ready",
    error: null,
    ...over,
  };
}

function mountItem(msg: StreamMessage, pinia: Pinia) {
  return mount(MessageItem, {
    props: { message: msg },
    global: { plugins: [pinia], stubs: { MarkdownContent: true } },
  });
}

beforeEach(() => {
  pickLocalPath.mockClear();
  relocateAttachment.mockClear();
  retryAttachment.mockClear();
  openAttachment.mockClear();
  pickLocalPath.mockResolvedValue("D:\\docs\\report.pdf");
  document.documentElement.removeAttribute("data-theme");
});

describe("历史消息的附件行：入口与状态", () => {
  it("每个附件都有可识别的锚点与真实状态；名称/大小/保存方式都来自附件事实", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const w = mountItem(
      makeMessage({
        role: "user",
        attachments: [
          attachment(),
          attachment({ id: "att_2", name: "gone.pdf", state: "missing", kind: "reference", display: "引用本地文件" }),
          attachment({ id: "att_3", name: "big.iso", state: "ready", kind: "reference", display: "引用本地文件" }),
        ],
      }),
      pinia,
    );

    const row = w.find("[data-test='message-attachments']");
    expect(row.exists()).toBe(true);
    const chips = row.findAll("[data-test='message-attachment']");
    expect(chips).toHaveLength(3);
    expect(chips[0]?.attributes("data-id")).toBe("att_1");
    expect(chips[0]?.attributes("data-state")).toBe("ready");
    expect(chips[0]?.attributes("data-kind")).toBe("copy");
    expect(chips[1]?.attributes("data-state")).toBe("missing");
    expect(chips[1]?.attributes("data-kind")).toBe("reference");
    // 状态如实显示：就绪的写保存方式，不在原位的写「文件不在原位」（不假装就绪）
    expect(row.text()).toContain("已保存副本");
    expect(row.text()).toContain("引用本地文件");
    expect(row.text()).toContain("文件不在原位");
    expect(chips[1]?.attributes("data-state")).toBe("missing");
    w.unmount();
  });

  it("「打开」接到附件服务（不新增任意路径读取入口）", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const w = mountItem(makeMessage({ role: "user", attachments: [attachment()] }), pinia);

    const chip = w.findComponent(AttachmentChip);
    expect(chip.exists()).toBe(true);
    chip.vm.$emit("open", "att_1");
    await flushPromises();

    expect(openAttachment).toHaveBeenCalledTimes(1);
    expect(openAttachment.mock.calls[0]?.[0]).toMatchObject({ id: "att_1", state: "ready" });
    w.unmount();
  });

  it("引用型「重新定位」：原生选择器 → 重新校验 → 就地更新这一行的状态", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const w = mountItem(
      makeMessage({ role: "user", attachments: [attachment({ id: "att_ref", kind: "reference", display: "引用本地文件", state: "missing" })] }),
      pinia,
    );

    w.findComponent(AttachmentChip).vm.$emit("relocate", "att_ref");
    await flushPromises();

    expect(pickLocalPath).toHaveBeenCalledTimes(1);
    expect(relocateAttachment).toHaveBeenCalledWith("att_ref", "D:\\docs\\report.pdf");
    const chip = w.find("[data-test='message-attachment']");
    expect(chip.attributes("data-state")).toBe("ready");
    expect(w.find("[data-test='attachment-notice']").text()).toContain("已重新定位");
    w.unmount();
  });

  it("「重试」作用在同一行附件上，失败如实留在这一行（可再试）", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    retryAttachment.mockRejectedValueOnce(new Error("复制失败：磁盘已满"));
    const w = mountItem(
      makeMessage({ role: "user", attachments: [attachment({ id: "att_bad", state: "failed", error: "准备失败" })] }),
      pinia,
    );

    w.findComponent(AttachmentChip).vm.$emit("retry", "att_bad");
    await flushPromises();
    expect(w.find("[data-test='attachment-notice']").text()).toContain("磁盘已满");

    w.findComponent(AttachmentChip).vm.$emit("retry", "att_bad");
    await flushPromises();
    expect(retryAttachment).toHaveBeenCalledTimes(2);
    w.unmount();
  });

  it("已发出消息上的「移除」不静默：说清附件是消息的一部分", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const w = mountItem(makeMessage({ role: "user", attachments: [attachment()] }), pinia);
    w.findComponent(AttachmentChip).vm.$emit("remove", "att_1");
    await flushPromises();
    expect(w.find("[data-test='attachment-notice']").text()).toContain("已经发出");
    w.unmount();
  });

  it("只有 id 没有元数据时如实说「元数据未加载」，不编造文件名", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const w = mountItem(makeMessage({ role: "user", attachmentIds: ["att_x"] }), pinia);
    expect(w.find("[data-test='message-attachments']").exists()).toBe(false);
    expect(w.find(".attach-note").text()).toContain("元数据未加载");
    w.unmount();
  });
});
