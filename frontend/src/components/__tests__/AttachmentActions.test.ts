/**
 * R6 §1.4：附件 chip 的按钮必须**由服务端 payload.actions 驱动**。
 *
 * 修复前红：AttachmentChip 完全不看 actions ——
 *   * 任何非 ready/prepared 都显示「重试」（浏览器字节上传没有原路径，服务端重试拿不到内容）；
 *   * 并且**总是**显示「重新定位」（浏览器上传没有 source_path，指不了任何地方）。
 * 也就是用户会看到两个必然走不通的按钮，而真正该做的「重新上传」不存在。
 *
 * 冻结取值（服务端 §1.4）：actions ⊆ {"retry" | "relocate" | "reupload"}；
 * recoverable_from_source: boolean。缺字段（老后端）按既有状态逻辑兜底。
 *
 * 运行：cd frontend; npx vitest run src/components/__tests__/AttachmentActions.test.ts
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";

vi.mock("../../services/api", () => ({
  api: {
    sendTurn: vi.fn(async () => ({ ok: true, topic_id: null })),
    getSessionContext: vi.fn(async () => ({
      topic_id: "",
      topic_name: null,
      anchor_fragment: null,
      messages: [],
    })),
    cancelTurn: vi.fn(async () => ({ ok: true, cancelled: true })),
  },
}));

import AttachmentChip from "../AttachmentChip.vue";
import Composer from "../Composer.vue";
import MessageItem from "../MessageItem.vue";
import type { AttachmentRef, AttachmentAction } from "../../services/attachments";
import type { StreamMessage } from "../../stores/session";

const {
  pickLocalPath,
  waitUntilSettled,
  prepareAttachment,
  uploadAttachment,
  retryAttachment,
  relocateAttachment,
} = vi.hoisted(() => ({
    pickLocalPath: vi.fn(async () => null as string | null),
    waitUntilSettled: vi.fn(async (item: unknown) => item),
    prepareAttachment: vi.fn(async (path: string): Promise<AttachmentRef> => ({
      id: "att_new",
      name: "report.pdf",
      sizeBytes: 2048,
      kind: "copy" as const,
      display: "已保存副本",
      state: "ready" as const,
      error: null,
      sourcePath: path,
    })),
    uploadAttachment: vi.fn(async (): Promise<AttachmentRef> => ({
      id: "att_new",
      name: "report.pdf",
      sizeBytes: 2048,
      kind: "copy" as const,
      display: "已保存副本",
      state: "ready" as const,
      error: null,
    })),
    retryAttachment: vi.fn(async (id: string): Promise<AttachmentRef> => ({
      id,
      name: "report.pdf",
      sizeBytes: 2048,
      kind: "copy" as const,
      display: "已保存副本",
      state: "ready" as const,
      error: null,
    })),
    relocateAttachment: vi.fn(async (id: string, path: string): Promise<AttachmentRef> => ({
      id,
      name: "report.pdf",
      sizeBytes: 2048,
      kind: "reference" as const,
      display: "引用本地文件",
      state: "ready" as const,
      error: null,
      sourcePath: path,
    })),
  }));

vi.mock("../../services/attachments", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../services/attachments")>();
  return {
    ...actual,
    pickLocalPath,
    waitUntilSettled,
    prepareAttachment,
    uploadAttachment,
    retryAttachment,
    relocateAttachment,
    // 浏览器文件选择器：测试里直接给一个 File，不弹真实选择框
    pickBrowserFile: vi.fn(async () => new File(["hello"], "report.pdf")),
  };
});

function ref(over: Partial<AttachmentRef> & { actions?: AttachmentAction[] } = {}): AttachmentRef {
  return {
    id: "att_1",
    name: "report.pdf",
    sizeBytes: 2048,
    kind: "copy",
    display: "已保存副本",
    state: "failed",
    error: "目标位置不可用：Not a directory",
    ...over,
  };
}

function mountChip(attachment: AttachmentRef) {
  return mount(AttachmentChip, { props: { attachment } });
}

function buttonTexts(w: ReturnType<typeof mountChip>): string[] {
  return w.findAll("button").map((b) => b.text());
}

beforeEach(() => {
  pickLocalPath.mockClear();
  prepareAttachment.mockClear();
  uploadAttachment.mockClear();
  retryAttachment.mockClear();
  relocateAttachment.mockClear();
  pickLocalPath.mockResolvedValue(null);
});

describe("chip 的按钮由 payload.actions 决定", () => {
  it("浏览器上传失败（actions=[reupload]）：只有「重新上传」，没有重新定位/重试，并说明无法自行找回", () => {
    const w = mountChip(ref({ actions: ["reupload"], sourcePath: null }));

    expect(buttonTexts(w)).toContain("重新上传");
    expect(buttonTexts(w), "浏览器上传没有原路径，不能给「重新定位」").not.toContain("重新定位");
    expect(buttonTexts(w), "服务端重试对字节上传拿不到内容，不能给必然失败的「重试」").not.toContain("重试");
    const note = w.find("[data-test='attach-no-recovery-note']");
    expect(note.exists(), "必须明说 QIO 无法从原地址恢复").toBe(true);
    expect(note.text()).toContain("QIO 无法从原地址恢复");
    // 失败原因本身要看得见
    expect(w.text()).toContain("目标位置不可用");
    w.unmount();
  });

  it("本地路径附件（actions=[retry, relocate]）：两个按钮都在，没有「重新上传」", () => {
    const w = mountChip(ref({ actions: ["retry", "relocate"], sourcePath: "D:\\docs\\report.pdf" }));

    expect(buttonTexts(w)).toContain("重试");
    expect(buttonTexts(w)).toContain("重新定位");
    expect(buttonTexts(w)).not.toContain("重新上传");
    expect(w.find("[data-test='attach-no-recovery-note']").exists()).toBe(false);
    w.unmount();
  });

  it("ready（actions 为空）：不出任何动作按钮", () => {
    const w = mountChip(ref({ state: "ready", error: null, actions: [] }));

    expect(buttonTexts(w)).not.toContain("重试");
    expect(buttonTexts(w)).not.toContain("重新定位");
    expect(buttonTexts(w)).not.toContain("重新上传");
    expect(buttonTexts(w)).toContain("×"); // 移除始终可用
    w.unmount();
  });

  it("preparing/prepared（actions 为空）：不出任何动作按钮，状态如实显示「准备中…」", () => {
    const w = mountChip(ref({ state: "prepared", error: null, actions: [] }));

    expect(w.text()).toContain("准备中…");
    expect(buttonTexts(w)).not.toContain("重试");
    expect(buttonTexts(w)).not.toContain("重新定位");
    expect(buttonTexts(w)).not.toContain("重新上传");
    w.unmount();
  });

  it("缺 actions 字段（老后端）：按既有状态逻辑兜底，不因为缺字段就没有按钮", () => {
    const w = mountChip(ref({ state: "failed", kind: "copy" }));
    expect(buttonTexts(w)).toContain("重试");
    w.unmount();
  });

  it("点击「重新上传」emit reupload", async () => {
    const w = mountChip(ref({ actions: ["reupload"] }));
    await w.find("button.reupload").trigger("click");
    expect(w.emitted("reupload")).toEqual([["att_1"]]);
    w.unmount();
  });
});

describe("历史消息里的附件行同样按 actions 渲染", () => {
  function makeMessage(over: Partial<StreamMessage>): StreamMessage {
    return {
      id: "m1",
      role: "user",
      content: "看看这个文件",
      contentType: "text",
      createdAt: "2026-10-08T00:00:00+00:00",
      ...over,
    };
  }

  it("浏览器上传失败的历史附件：只有「重新上传」+ 说明，没有「重新定位」", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const w = mount(MessageItem, {
      props: {
        message: makeMessage({
          attachments: [ref({ id: "att_browser", actions: ["reupload"], sourcePath: null })],
        }),
      },
      global: { plugins: [pinia], stubs: { MarkdownContent: true } },
    });

    const row = w.find("[data-test='message-attachments']");
    expect(row.exists()).toBe(true);
    expect(row.text()).toContain("重新上传");
    expect(row.text()).not.toContain("重新定位");
    expect(row.text()).toContain("QIO 无法从原地址恢复");
    w.unmount();
  });

  it("点「重新上传」走真实选择流程，并如实说明这一条无法自行恢复", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const w = mount(MessageItem, {
      props: {
        message: makeMessage({
          attachments: [ref({ id: "att_browser", actions: ["reupload"], sourcePath: null })],
        }),
      },
      global: { plugins: [pinia], stubs: { MarkdownContent: true } },
    });

    await w.find("button.reupload").trigger("click");
    await flushPromises();

    expect(uploadAttachment).toHaveBeenCalledTimes(1);
    const notice = w.find("[data-test='attachment-notice']");
    expect(notice.exists()).toBe(true);
    expect(notice.text()).toContain("已重新上传");
    expect(notice.text()).toContain("无法从原地址恢复");
    w.unmount();
  });
});

describe("输入区（Composer）的 chip 同样按 actions 渲染", () => {
  async function mountComposer() {
    const pinia = createPinia();
    setActivePinia(pinia);
    const w = mount(Composer, { global: { plugins: [pinia] } });
    await flushPromises();
    return w;
  }

  /** 浏览器回退路径：把文件塞进 input[type=file] 再触发 change。 */
  async function chooseFiles(w: ReturnType<typeof mount>, ...names: string[]) {
    const input = w.find("input.file-input");
    Object.defineProperty(input.element, "files", {
      value: names.map((name) => new File(["x"], name)),
      configurable: true,
    });
    await input.trigger("change");
    await flushPromises();
  }

  /** 真实路径入口：粘贴路径后回车。 */
  async function pastePath(w: ReturnType<typeof mount>, path: string) {
    await w.findAll(".attach-btn")[1].trigger("click");
    await w.find(".path-input").setValue(path);
    await w.find(".path-input").trigger("keydown.enter");
    await flushPromises();
  }

  it("pending 的浏览器上传失败 chip：只有「重新上传」+ 说明，没有「重新定位」", async () => {
    uploadAttachment.mockResolvedValueOnce(
      ref({
        id: "att_pending_browser",
        state: "failed",
        error: "目标位置不可用：Not a directory",
        actions: ["reupload"],
        sourcePath: null,
      }),
    );
    const w = await mountComposer();
    await chooseFiles(w, "报告.txt");

    const row = w.find(".attach-row");
    expect(row.exists()).toBe(true);
    expect(row.text()).toContain("重新上传");
    expect(row.text(), "浏览器上传没有原路径：不能给「重新定位」").not.toContain("重新定位");
    expect(row.text()).toContain("QIO 无法从原地址恢复");
    expect(row.text(), "失败原因要看得见").toContain("目标位置不可用");
    w.unmount();
  });

  it("pending 的本地路径 chip：仍是重试 + 重新定位", async () => {
    const failed = ref({
      id: "att_pending_local",
      state: "failed",
      error: "保存失败：权限不足",
      actions: ["retry", "relocate"],
      sourcePath: "D:\\docs\\报告.pdf",
    });
    prepareAttachment.mockResolvedValueOnce(failed);
    waitUntilSettled.mockResolvedValueOnce(failed);
    const w = await mountComposer();
    await pastePath(w, "D:\\docs\\报告.pdf");

    const row = w.find(".attach-row");
    expect(row.exists()).toBe(true);
    expect(row.text()).toContain("重试");
    expect(row.text()).toContain("重新定位");
    expect(row.text()).not.toContain("重新上传");
    w.unmount();
  });
});
