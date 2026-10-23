/**
 * Composer 的互动绑定接口（Phase 3 §6.4）。
 *
 * 只验接口行为：可选 prop chatBinding 作为 session.send 的 options.interactive 传下去，
 * 且**不改动用户原文**；不传 → 对话页行为完全不变（请求体不带板面身份）。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";
import Composer from "../Composer.vue";

const mocks = vi.hoisted(() => ({
  sendTurn: vi.fn(async () => ({ ok: true, topic_id: null })),
  getSessionContext: vi.fn(async () => ({
    topic_id: "",
    topic_name: null,
    anchor_fragment: null,
    messages: [],
  })),
  cancelTurn: vi.fn(async (id: string) => ({ ok: true, cancelled: true, turn_id: id })),
}));

vi.mock("../../services/api", () => ({
  api: {
    sendTurn: mocks.sendTurn,
    getSessionContext: mocks.getSessionContext,
    cancelTurn: mocks.cancelTurn,
  },
}));

async function mountComposer(props: Record<string, unknown> = {}) {
  const pinia = createPinia();
  setActivePinia(pinia);
  const w = mount(Composer, { props, global: { plugins: [pinia] } });
  await nextTick();
  return w;
}

async function sendText(w: Awaited<ReturnType<typeof mountComposer>>, text: string): Promise<void> {
  await w.find("textarea").setValue(text);
  await w.find(".send-btn").trigger("click");
  await flushPromises();
}

beforeEach(() => {
  localStorage.clear();
  mocks.sendTurn.mockClear();
});

describe("Composer × 互动绑定（可信 props）", () => {
  it("不传绑定：保持对话页行为，请求不带板面身份（调用形状只有三参）", async () => {
    const w = await mountComposer();
    await sendText(w, "只发文字");
    expect(mocks.sendTurn).toHaveBeenCalledTimes(1);
    expect(mocks.sendTurn.mock.calls[0]).toEqual(["只发文字", null, []]);
    w.unmount();
  });

  it("传绑定：作为发送选项传下去，用户原文一字不改", async () => {
    const w = await mountComposer({
      chatBinding: { mode: "interactive", boardId: "b1", submissionId: "s1" },
    });
    await sendText(w, "带绑定的原文");
    expect(mocks.sendTurn).toHaveBeenCalledTimes(1);
    const call = mocks.sendTurn.mock.calls[0] as unknown[];
    expect(call[0]).toBe("带绑定的原文");
    expect(call[6]).toEqual({ mode: "interactive", boardId: "b1", submissionId: "s1" });
    w.unmount();
  });

  it("绑定里的 intentId 不进入发送选项（也不会拼进正文）", async () => {
    const w = await mountComposer({
      chatBinding: { boardId: "b1", intentId: "intent_secret" },
    });
    await sendText(w, "正文");
    const call = mocks.sendTurn.mock.calls[0] as unknown[];
    expect(call[0]).toBe("正文");
    expect(JSON.stringify(call[6])).not.toContain("intent_secret");
    w.unmount();
  });

  it("mode=normal 时如实透传（由调用方决定，不在 Composer 里猜）", async () => {
    const w = await mountComposer({ chatBinding: { mode: "normal", boardId: "b2" } });
    await sendText(w, "正文");
    const call = mocks.sendTurn.mock.calls[0] as unknown[];
    expect(call[6]).toEqual({ mode: "normal", boardId: "b2" });
    w.unmount();
  });
});
