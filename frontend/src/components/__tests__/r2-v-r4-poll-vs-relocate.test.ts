/**
 * V 组独立验证：R4（旧轮询结果不得覆盖较新的重新定位结果；别的活跃话题按原话题落盘）。
 *
 * 逆序返回由**受控 Promise**构造：先发起轮询（旧声明）、再发起重新定位（新声明），
 * 让重新定位先返回并写界面，最后才让轮询返回 —— 它必须被丢弃。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";

const TOPIC_A = "topic-v-r4c-a";
const TOPIC_B = "topic-v-r4c-b";
const ATT = "att_r4c_1";

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

function staleRef(): AttachmentRef {
  return {
    id: ATT,
    name: "r4c.bin",
    sizeBytes: 32,
    kind: "copy",
    display: COPY_LABEL,
    state: "missing",
    error: "文件不在原位",
  };
}

function readyRef(): AttachmentRef {
  return { ...staleRef(), state: "ready", error: null };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
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

function stateText(w: VueWrapper): string {
  return w.find(".chip .state").text();
}

beforeEach(() => {
  vi.useFakeTimers();
  localStorage.clear();
  for (const key of Object.keys(mocks) as (keyof typeof mocks)[]) mocks[key].mockReset();
  mocks.sendTurn.mockResolvedValue({ ok: true, accepted: true, turn_id: "t", topic_id: TOPIC_A });
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
  mocks.uploadAttachment.mockResolvedValue(readyRef());
  mocks.waitUntilSettled.mockResolvedValue(readyRef());
  mocks.removeAttachment.mockResolvedValue(undefined as never);
  mocks.restorePendingAttachments.mockResolvedValue({ items: [], dropped: [] });
  mocks.pickLocalPath.mockResolvedValue("D:/replaced/r4c.bin" as never);
  mocks.onPathDrop.mockReturnValue(() => undefined);
  mocks.openAttachment.mockResolvedValue(undefined as never);
  mocks.isDesktopShell.mockReturnValue(true);
});

describe("R4 旧轮询与新的重新定位逆序返回", () => {
  it("轮询后返回：不得覆盖重新定位写下的 ready，也不得落盘覆盖", async () => {
    savePendingAttachments(TOPIC_A, [staleRef()]);
    const w = await mountComposer();
    expect(stateText(w)).toBe("文件不在原位");

    // 1) 先发起轮询（旧声明），卡住
    const poll = deferred<AttachmentRef>();
    mocks.retryAttachment.mockReturnValueOnce(poll.promise);
    w.findComponent(AttachmentChip).vm.$emit("retry", ATT);
    await flush();

    // 2) 再发起重新定位（新声明），也卡住
    const relocate = deferred<AttachmentRef>();
    mocks.relocateAttachment.mockReturnValueOnce(relocate.promise);
    w.findComponent(AttachmentChip).vm.$emit("relocate", ATT);
    await flush();

    // 3) 重新定位先返回 → 界面变 ready
    relocate.resolve(readyRef());
    await flush();
    expect(stateText(w), "重新定位应写界面").toBe(COPY_LABEL);

    // 4) 旧轮询后返回 → 必须被丢弃
    poll.resolve(staleRef());
    await flush();
    await vi.advanceTimersByTimeAsync(10);

    expect(stateText(w), "过期的轮询结果覆盖了新的重新定位结果").toBe(COPY_LABEL);
    expect(loadPendingAttachments(TOPIC_A)[0]?.state, "过期结果也不得落盘").toBe("ready");
    w.unmount();
  });

  it("别的活跃话题的操作：结果只落它自己的话题，不写当前话题的界面", async () => {
    savePendingAttachments(TOPIC_A, [staleRef()]);
    const w = await mountComposer();
    const session = (await import("../../stores/session")).useSessionStore();

    // A 话题发起轮询（卡住）
    const poll = deferred<AttachmentRef>();
    mocks.retryAttachment.mockReturnValueOnce(poll.promise);
    w.findComponent(AttachmentChip).vm.$emit("retry", ATT);
    await flush();

    // 切到 B 话题
    session.currentTopicId = TOPIC_B;
    await flush();
    await flush();
    expect(w.findAllComponents(AttachmentChip), "B 话题界面空").toHaveLength(0);

    // 轮询返回 ready：只能落 A
    poll.resolve(readyRef());
    await flush();
    await vi.advanceTimersByTimeAsync(10);

    expect(loadPendingAttachments(TOPIC_A)[0]?.state).toBe("ready");
    expect(loadPendingAttachments(TOPIC_B), "B 话题不得被写入").toEqual([]);
    expect(w.findAllComponents(AttachmentChip), "当前话题界面不得被污染").toHaveLength(0);
    w.unmount();
  });
});
