/**
 * ChatDock 的互动绑定 props（Phase 3 §6.4）。
 *
 * 既有行为：不传 props 时内部用 store.chatBinding（旧调用方不受影响）；
 * 新行为：Lead 可以把**可信投影**通过 props 传进来，发送时以 props 为准。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";

const mocks = vi.hoisted(() => ({
  pickBrowserFile: vi.fn(async () => null),
  pickLocalPath: vi.fn(async () => null),
  prepareAttachment: vi.fn(),
  uploadAttachment: vi.fn(),
  waitUntilSettled: vi.fn(),
  removeAttachment: vi.fn(),
  retryAttachment: vi.fn(),
  openAttachment: vi.fn(),
  relocateAttachment: vi.fn(),
}));

vi.mock("../../../services/attachments", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../services/attachments")>();
  return { ...actual, ...mocks };
});

import ChatDock from "../ChatDock.vue";
import { useInteractiveStore } from "../../../stores/interactive";
import { useSessionStore } from "../../../stores/session";
import { resetChatFrameState } from "../../../interactive/chatFrameState";

const wrappers: VueWrapper[] = [];

function mountDock(props: Record<string, unknown> = {}) {
  const pinia = createPinia();
  setActivePinia(pinia);
  const interactive = useInteractiveStore();
  const session = useSessionStore();
  session.currentTopicId = "A";
  interactive.chatOpen = true;
  const w = mount(ChatDock, {
    props,
    global: { plugins: [pinia] },
    attachTo: document.body,
  });
  wrappers.push(w);
  return { w, interactive, session };
}

beforeEach(() => {
  localStorage.clear();
  resetChatFrameState();
  for (const key of Object.keys(mocks) as (keyof typeof mocks)[]) {
    (mocks[key] as { mockReset?: () => void }).mockReset?.();
  }
  mocks.pickBrowserFile.mockResolvedValue(null);
  mocks.pickLocalPath.mockResolvedValue(null);
});

afterEach(() => {
  for (const w of wrappers.splice(0)) w.unmount();
  document.body.innerHTML = "";
});

async function sendViaDock(w: VueWrapper): Promise<void> {
  await w.get('[data-im="chat-input"]').setValue("和 QIO 说一句");
  await w.get("button.send").trigger("click");
  await flushPromises();
}

describe("ChatDock × 可信互动绑定", () => {
  it("不传 props：沿用 store.chatBinding（旧调用方不变）", async () => {
    const { w, session } = mountDock();
    const spy = vi.spyOn(session, "send").mockResolvedValue(true);
    await sendViaDock(w);
    expect(spy).toHaveBeenCalled();
    const options = spy.mock.calls[0][3] as { interactive?: Record<string, unknown> };
    expect(options.interactive?.mode).toBe("interactive");
    expect(options.interactive?.boardId).toBe("board_default");
  });

  it("传 props：发送时以 props 为准（board/submission 原样、intent 不外泄）", async () => {
    const { w, session } = mountDock({
      binding: { boardId: "b1", submissionId: "s1", intentId: "i1" },
    });
    const spy = vi.spyOn(session, "send").mockResolvedValue(true);
    await sendViaDock(w);
    const options = spy.mock.calls[0][3] as { interactive?: Record<string, unknown> };
    expect(options.interactive).toEqual({
      mode: "interactive",
      boardId: "b1",
      submissionId: "s1",
    });
    // 用户原文没有被拼上任何绑定信息
    expect(spy.mock.calls[0][0]).toBe("和 QIO 说一句");
  });
});
