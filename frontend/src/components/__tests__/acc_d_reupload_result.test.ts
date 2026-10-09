/**
 * F04 反例（成员 D · 历史重新上传结果归属）：成功的新附件必须进入**发起话题**的待发送附件列表
 * （显示可使用入口并持久化），原历史记录保持不变。
 *
 * 基线行为（红）：MessageItem.reuploadOne 只 prepare/upload 一条新记录 + 弹「已重新登记为新的附件」，
 *   历史仍只有旧 ID，Composer 与待发送持久化都没有新 ID，刷新后也发现不了。
 * 期望（绿）：新附件进入 Composer 待发送列表并写 localStorage；历史/旧 turn 归属不变；
 *   上传失败 / 取消不产生任何待发条目。
 *
 * 模拟边界：真实 Composer + MessageItem + 真实 store + 真实 localStorage 持久化；
 *   附件 service 仅替换「网络/选择器」边界（受控 mock）。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { defineComponent, h } from "vue";

const mocks = vi.hoisted(() => ({
  sendTurn: vi.fn(async () => ({ ok: true, topic_id: null })),
  getSessionContext: vi.fn(async () => ({ topic_id: "", topic_name: null, anchor_fragment: null, messages: [] })),
  cancelTurn: vi.fn(async (id: string) => ({ ok: true, cancelled: true, turn_id: id })),
  prepareAttachment: vi.fn(),
  uploadAttachment: vi.fn(),
  removeAttachment: vi.fn(),
  retryAttachment: vi.fn(),
  getAttachment: vi.fn(),
  waitUntilSettled: vi.fn(),
  pickLocalPath: vi.fn(),
  pickBrowserFile: vi.fn(),
  onPathDrop: vi.fn(),
  openAttachment: vi.fn(),
  relocateAttachment: vi.fn(),
  restorePendingAttachments: vi.fn(),
}));

vi.mock("../../services/api", () => ({
  api: { sendTurn: mocks.sendTurn, getSessionContext: mocks.getSessionContext, cancelTurn: mocks.cancelTurn },
}));

vi.mock("../../services/attachments", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../services/attachments")>();
  return {
    ...actual,
    prepareAttachment: mocks.prepareAttachment,
    uploadAttachment: mocks.uploadAttachment,
    removeAttachment: mocks.removeAttachment,
    retryAttachment: mocks.retryAttachment,
    getAttachment: mocks.getAttachment,
    waitUntilSettled: mocks.waitUntilSettled,
    pickLocalPath: mocks.pickLocalPath,
    pickBrowserFile: mocks.pickBrowserFile,
    onPathDrop: mocks.onPathDrop,
    openAttachment: mocks.openAttachment,
    relocateAttachment: mocks.relocateAttachment,
    restorePendingAttachments: mocks.restorePendingAttachments,
    isDesktopShell: () => false,
  };
});

import Composer from "../Composer.vue";
import MessageItem from "../MessageItem.vue";
import { useSessionStore } from "../../stores/session";
import type { StreamMessage } from "../../stores/session";
import { loadPendingAttachments, type AttachmentRef } from "../../services/attachments";

function ref(over: Partial<AttachmentRef> = {}): AttachmentRef {
  return {
    id: "att_old",
    name: "旧的失败.txt",
    sizeBytes: 10,
    kind: "copy",
    display: "已保存副本",
    state: "failed",
    error: "QIO 无法从原地址恢复",
    actions: ["reupload"],
    ...over,
  };
}

const Wrapper = defineComponent({
  props: { message: { type: Object as () => StreamMessage, required: true } },
  setup(props) {
    return () => h("div", [h(Composer), h(MessageItem, { message: props.message })]);
  },
});

function historyMessage(attachments: AttachmentRef[]): StreamMessage {
  return {
    id: "m1",
    role: "user",
    content: "看看这个文件",
    contentType: "text",
    createdAt: "2026-10-06T08:00:00+00:00",
    attachments,
  } as StreamMessage;
}

async function mountBoth(message: StreamMessage, topicId: string | null = "t1") {
  const pinia = createPinia();
  setActivePinia(pinia);
  const session = useSessionStore();
  session.currentTopicId = topicId;
  const w = mount(Wrapper, { props: { message }, global: { plugins: [pinia] } });
  await flushPromises();
  return { w, session, pinia };
}

async function clickReupload(w: VueWrapper) {
  const btn = w.find("[data-test='message-attachments'] .act.reupload");
  expect(btn.exists()).toBe(true);
  await btn.trigger("click");
  await flushPromises();
}

beforeEach(() => {
  localStorage.clear();
  for (const key of Object.keys(mocks) as (keyof typeof mocks)[]) mocks[key].mockReset();
  mocks.restorePendingAttachments.mockResolvedValue({ items: [], dropped: [], unconfirmed: [] } as never);
  mocks.pickLocalPath.mockResolvedValue(null as never);
  mocks.pickBrowserFile.mockResolvedValue(null as never);
  mocks.onPathDrop.mockResolvedValue(null as never);
  mocks.waitUntilSettled.mockImplementation(async (item: AttachmentRef) => item as never);
  mocks.removeAttachment.mockResolvedValue(undefined as never);
  mocks.openAttachment.mockResolvedValue({ action: "view", note: "" } as never);
  mocks.getSessionContext.mockResolvedValue({ topic_id: "", topic_name: null, anchor_fragment: null, messages: [] } as never);
});

describe("F04 历史重新上传：结果进入发起话题的待发列表", () => {
  it("上传成功：新附件进入 Composer 待发列表并持久化，历史仍旧不变", async () => {
    mocks.pickBrowserFile.mockResolvedValue(new File(["x"], "新的.txt") as never);
    mocks.uploadAttachment.mockResolvedValue(
      ref({ id: "att_new", name: "新的.txt", state: "ready", error: null, actions: [] }) as never,
    );
    const message = historyMessage([ref()]);
    const { w } = await mountBoth(message);

    await clickReupload(w);

    // 持久化：刷新后仍能发现（不依赖组件内存）
    expect(loadPendingAttachments("t1").map((i) => i.id)).toEqual(["att_new"]);
    // Composer 待发列表出现新附件
    const composerNames = w.findAll(".composer .chip .name").map((n) => n.text());
    expect(composerNames).toContain("新的.txt");
    // 历史记录（旧 turn 的附件归属）不变
    const historyChip = w.find("[data-test='message-attachment']");
    expect(historyChip.attributes("data-id")).toBe("att_old");
    expect(w.find("[data-test='attachment-notice']").text()).toContain("加入待发送附件");
    w.unmount();
  });

  it("后台准备中：先入列，ready 后更新同一条（不产生重复）", async () => {
    mocks.pickBrowserFile.mockResolvedValue(new File(["x"], "新的.txt") as never);
    mocks.uploadAttachment.mockResolvedValue(
      ref({ id: "att_new", name: "新的.txt", state: "prepared", error: null, actions: undefined }) as never,
    );
    mocks.waitUntilSettled.mockResolvedValue(
      ref({ id: "att_new", name: "新的.txt", state: "ready", error: null, actions: [] }) as never,
    );
    const { w } = await mountBoth(historyMessage([ref()]));

    await clickReupload(w);

    const saved = loadPendingAttachments("t1");
    expect(saved.map((i) => i.id)).toEqual(["att_new"]);
    expect(saved[0].state).toBe("ready");
    w.unmount();
  });

  it("上传失败：不产生待发条目，历史不变", async () => {
    mocks.pickBrowserFile.mockResolvedValue(new File(["x"], "新的.txt") as never);
    mocks.uploadAttachment.mockRejectedValue(new Error("网络拒绝") as never);
    const { w } = await mountBoth(historyMessage([ref()]));

    await clickReupload(w);

    expect(loadPendingAttachments("t1")).toEqual([]);
    expect(w.findAll(".composer .chip")).toHaveLength(0);
    expect(w.find("[data-test='message-attachment']").attributes("data-id")).toBe("att_old");
    expect(w.find("[data-test='attachment-notice']").text()).toContain("重新上传没有成功");
    w.unmount();
  });

  it("用户取消选择：什么都不发生", async () => {
    mocks.pickBrowserFile.mockResolvedValue(null as never);
    const { w } = await mountBoth(historyMessage([ref()]));

    await clickReupload(w);

    expect(loadPendingAttachments("t1")).toEqual([]);
    expect(mocks.uploadAttachment).not.toHaveBeenCalled();
    expect(w.find("[data-test='attachment-notice']").exists()).toBe(false);
    w.unmount();
  });

  it("历史消息属于别的话题：新附件进入**点击时所在的发起话题**，不写回历史话题", async () => {
    mocks.pickBrowserFile.mockResolvedValue(new File(["x"], "新的.txt") as never);
    mocks.uploadAttachment.mockResolvedValue(
      ref({ id: "att_new", name: "新的.txt", state: "ready", error: null, actions: [] }) as never,
    );
    // 历史消息属于 t1，但用户当前在 t2 发起重传
    const { w } = await mountBoth(historyMessage([ref({ topicId: "t1" })]), "t2");

    await clickReupload(w);

    // 发起话题 = 点击时所在话题 t2
    expect(loadPendingAttachments("t2").map((i) => i.id)).toEqual(["att_new"]);
    expect(loadPendingAttachments("t1")).toEqual([]);
    expect(w.findAll(".composer .chip .name").map((n) => n.text())).toContain("新的.txt");
    w.unmount();
  });
});
