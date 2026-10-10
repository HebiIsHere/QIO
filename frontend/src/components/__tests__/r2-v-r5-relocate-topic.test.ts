/**
 * V 组独立验证：R5（原生选择器失败 → 粘贴路径：结果只属于发起话题 A）。
 *
 * 时序（全部受控，无随机等待）：
 *   A 话题点「重新定位」→ 原生选择器挂住 → 切到 B 话题 → 选择器失败回退 → 粘贴路径提交
 *   → 结果只落到 A，当前话题 B 的界面不受污染。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";

const TOPIC_A = "topic-v-r5-a";
const TOPIC_B = "topic-v-r5-b";
const ATT = "att_r5_1";

const mocks = vi.hoisted(() => ({
  cancelTurn: vi.fn(),
  cancelActiveTurn: vi.fn(),
  cancelPreparing: vi.fn(),
  sendTurn: vi.fn(),
  getSessionContext: vi.fn(),
  uploadAttachment: vi.fn(),
  prepareAttachment: vi.fn(),
  waitUntilSettled: vi.fn(),
  removeAttachment: vi.fn(),
  retryAttachment: vi.fn(),
  relocateAttachment: vi.fn(),
  restorePendingAttachments: vi.fn(),
  pickLocalPath: vi.fn(),
  onPathDrop: vi.fn(),
  openAttachment: vi.fn(),
  isDesktopShell: vi.fn(),
}));

vi.mock("../../services/api", () => {
  const base: Record<string, unknown> = {
    sendTurn: mocks.sendTurn,
    cancelTurn: mocks.cancelTurn,
    cancelActiveTurn: mocks.cancelActiveTurn,
    cancelPreparing: mocks.cancelPreparing,
    getSessionContext: mocks.getSessionContext,
  };
  return {
    api: new Proxy(base, {
      get(target, prop: string) {
        if (prop in target) return target[prop];
        return vi.fn(async () => ({}));
      },
    }),
  };
});

vi.mock("../../services/attachments", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../services/attachments")>();
  return {
    ...actual,
    uploadAttachment: mocks.uploadAttachment,
    prepareAttachment: mocks.prepareAttachment,
    waitUntilSettled: mocks.waitUntilSettled,
    removeAttachment: mocks.removeAttachment,
    retryAttachment: mocks.retryAttachment,
    relocateAttachment: mocks.relocateAttachment,
    restorePendingAttachments: mocks.restorePendingAttachments,
    pickLocalPath: mocks.pickLocalPath,
    onPathDrop: mocks.onPathDrop,
    openAttachment: mocks.openAttachment,
    isDesktopShell: mocks.isDesktopShell,
  };
});

import Composer from "../Composer.vue";
import AttachmentChip from "../AttachmentChip.vue";
import {
  COPY_LABEL,
  loadPendingAttachments,
  savePendingAttachments,
  type AttachmentRef,
} from "../../services/attachments";

function ref(overrides: Partial<AttachmentRef> = {}): AttachmentRef {
  return {
    id: ATT,
    name: "r5.bin",
    sizeBytes: 32,
    kind: "copy",
    display: COPY_LABEL,
    state: "missing",
    error: "文件不在原位",
    ...overrides,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

const flush = () => vi.advanceTimersByTimeAsync(0);

async function mountComposer(): Promise<VueWrapper> {
  const pinia = createPinia();
  setActivePinia(pinia);
  const session = (await import("../../stores/session")).useSessionStore(pinia);
  session.currentTopicId = TOPIC_A;
  const w = mount(Composer, { global: { plugins: [pinia] } });
  await flush();
  return w;
}

async function switchTopic(session: { currentTopicId: string | null }, topic: string) {
  session.currentTopicId = topic;
  await flush();
  await flush();
}

beforeEach(() => {
  vi.useFakeTimers();
  localStorage.clear();
  for (const key of Object.keys(mocks) as (keyof typeof mocks)[]) mocks[key].mockReset();
  mocks.sendTurn.mockResolvedValue({ ok: true, accepted: true, turn_id: "t", topic_id: TOPIC_A });
  mocks.cancelTurn.mockResolvedValue({ ok: true, cancelled: true });
  mocks.cancelActiveTurn.mockResolvedValue({ ok: true, cancelled: true });
  mocks.cancelPreparing.mockResolvedValue({ ok: true, cancelled: true });
  mocks.getSessionContext.mockResolvedValue({
    topic_id: TOPIC_A,
    topic_name: null,
    anchor_fragment: null,
    messages: [],
    turn_facts: [],
    tool_records: [],
    has_more: false,
    next_before: null,
  });
  mocks.uploadAttachment.mockResolvedValue(ref({ state: "ready", error: null }));
  mocks.waitUntilSettled.mockResolvedValue(ref({ state: "ready", error: null }));
  mocks.removeAttachment.mockResolvedValue(undefined as never);
  mocks.restorePendingAttachments.mockResolvedValue({ items: [], dropped: [] });
  mocks.onPathDrop.mockReturnValue(() => undefined);
  mocks.openAttachment.mockResolvedValue(undefined as never);
  mocks.isDesktopShell.mockReturnValue(true); // 桌面壳：走原生选择器（可能失败）
});

describe("R5 重新定位归属发起话题", () => {
  it("A 启动 → 切到 B → 选择器失败 → 粘贴路径提交：结果只落到 A", async () => {
    savePendingAttachments(TOPIC_A, [ref()]);
    const w = await mountComposer();
    const session = (await import("../../stores/session")).useSessionStore();
    expect(w.findAllComponents(AttachmentChip), "A 话题应当看到这条待发附件").toHaveLength(1);

    // 原生选择器挂住（受控）
    const picker = deferred<string | null>();
    mocks.pickLocalPath.mockReturnValueOnce(picker.promise);

    w.findComponent(AttachmentChip).vm.$emit("relocate", ATT);
    await flush();

    // 选择器还没返回，用户切到 B 话题
    await switchTopic(session, TOPIC_B);
    expect(w.findAllComponents(AttachmentChip), "B 话题没有这条附件").toHaveLength(0);

    // 选择器失败 → 回退到「粘贴路径」
    picker.reject(new Error("原生选择器不可用"));
    await flush();
    expect(w.find("input.path-input").exists(), "失败后必须给出粘贴路径的入口").toBe(true);

    // 粘贴新位置并提交（受控）
    const relocate = deferred<AttachmentRef>();
    mocks.relocateAttachment.mockReturnValueOnce(relocate.promise);
    await w.find("input.path-input").setValue("D:/新位置/r5.bin");
    await w.find("input.path-input").trigger("keydown.enter");
    await flush();

    expect(mocks.relocateAttachment, "必须带上发起话题的那条附件 id").toHaveBeenCalledWith(
      ATT,
      "D:/新位置/r5.bin",
    );

    relocate.resolve(ref({ state: "ready", error: null, name: "r5.bin" }));
    await flush();
    await vi.advanceTimersByTimeAsync(10);

    // 结果只属于 A：按 A 落盘
    const storedA = loadPendingAttachments(TOPIC_A);
    expect(storedA.map((item) => item.id)).toEqual([ATT]);
    expect(storedA[0]?.state, "A 的持久化拿到重新定位后的新事实").toBe("ready");
    // B 一个字节都没被写
    expect(loadPendingAttachments(TOPIC_B)).toEqual([]);
    // 当前话题 B 的界面不受污染
    expect(w.findAllComponents(AttachmentChip)).toHaveLength(0);
    w.unmount();
  });

  it("取消粘贴路径行：清掉发起身份，之后不再提交任何重新定位", async () => {
    savePendingAttachments(TOPIC_A, [ref()]);
    const w = await mountComposer();

    const picker = deferred<string | null>();
    mocks.pickLocalPath.mockReturnValueOnce(picker.promise);
    w.findComponent(AttachmentChip).vm.$emit("relocate", ATT);
    await flush();
    picker.reject(new Error("原生选择器不可用"));
    await flush();
    expect(w.find("input.path-input").exists()).toBe(true);

    // 用户取消这一行
    await w.find("input.path-input").setValue("D:/不要提交.bin");
    const cancelBtn = w.findAll("button.path-btn").find((b) => b.text() === "取消");
    expect(cancelBtn, "必须有取消按钮").toBeTruthy();
    await cancelBtn!.trigger("click");
    await flush();
    expect(w.find("input.path-input").exists()).toBe(false);

    // 再提交也不该走重新定位
    await w.find("textarea").setValue("x");
    expect(mocks.relocateAttachment).not.toHaveBeenCalled();
    w.unmount();
  });

  it("卸载后在途结果仍按发起话题 A 落盘（不写界面）", async () => {
    savePendingAttachments(TOPIC_A, [ref()]);
    const w = await mountComposer();

    const picker = deferred<string | null>();
    mocks.pickLocalPath.mockReturnValueOnce(picker.promise);
    w.findComponent(AttachmentChip).vm.$emit("relocate", ATT);
    await flush();
    picker.reject(new Error("原生选择器不可用"));
    await flush();

    const relocate = deferred<AttachmentRef>();
    mocks.relocateAttachment.mockReturnValueOnce(relocate.promise);
    await w.find("input.path-input").setValue("D:/卸载后/r5.bin");
    await w.find("input.path-input").trigger("keydown.enter");
    await flush();

    w.unmount();
    await flush();

    relocate.resolve(ref({ state: "ready", error: null }));
    await flush();
    await vi.advanceTimersByTimeAsync(10);

    const storedA = loadPendingAttachments(TOPIC_A);
    expect(storedA.map((item) => item.id)).toEqual([ATT]);
    expect(storedA[0]?.state).toBe("ready");
  });
});
