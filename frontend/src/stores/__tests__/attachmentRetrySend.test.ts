/**
 * R4 问题二（前端一半）：重试必须带上 retry_of_turn_id，并且以**真实回执**为准。
 *
 * 修复前红：
 * 1. session.retryTurn 把原轮的 attachment_ids 原样交给新轮、**不带 retry_of_turn_id**
 *    → 后端只看到「附件已绑定在别的一轮」→ 静默丢掉 → 新轮 0 附件；
 * 2. 前端不看回执，标签照旧显示「已带上」→ 界面有附件、模型实际没有；
 * 3. 结构化 409（附件没附上）没有可恢复信息，界面给不出原因。
 *
 * 运行：cd frontend; npx vitest run src/stores/__tests__/attachmentRetrySend.test.ts
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { ApiError } from "../../services/api";
import type { AttachmentRef } from "../../services/attachments";
import { useSessionStore } from "../session";

const { sendTurn } = vi.hoisted(() => ({
  sendTurn: vi.fn(async () => ({
    ok: true,
    accepted: true,
    turn_id: "turn_new",
    status: "accepted",
    topic_id: null as string | null,
  })),
}));

vi.mock("../../services/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../services/api")>();
  return { ...actual, api: { ...actual.api, sendTurn } };
});

function ref(over: Partial<AttachmentRef> = {}): AttachmentRef {
  return {
    id: "att_1",
    name: "报告.txt",
    sizeBytes: 2048,
    kind: "copy",
    display: "已保存副本",
    state: "ready",
    error: null,
    ...over,
  };
}

function setup() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return useSessionStore();
}

function userWithAttachment(session: ReturnType<typeof useSessionStore>, turnId: string) {
  session.pushUser("看看这个文件", ["att_1"], [ref()]);
  const mine = session.messages[session.messages.length - 1];
  if (mine) mine.turnId = turnId;
  return mine;
}

function rejectionBody() {
  return {
    detail: {
      code: "attachment_binding_failed",
      message: "有 1 个附件没有附上：att_1（这个附件已经属于别的一轮了）",
      rejected: [{ id: "att_1", reason: "这个附件已经属于别的一轮了" }],
      bound_attachment_ids: [],
    },
  };
}

beforeEach(() => {
  sendTurn.mockClear();
  sendTurn.mockResolvedValue({
    ok: true,
    accepted: true,
    turn_id: "turn_new",
    status: "accepted",
    topic_id: null,
  });
});

describe("重试提交方式：必须让后端知道「这是哪一轮的重试」", () => {
  it("retryTurn 带 retry_of_turn_id（否则原轮附件会被静默丢掉）", async () => {
    const session = setup();
    userWithAttachment(session, "turn_old");

    const ok = await session.retryTurn("turn_old");

    expect(ok).toBe(true);
    expect(sendTurn).toHaveBeenCalledTimes(1);
    expect(sendTurn.mock.calls[0]?.slice(0, 4)).toEqual(["看看这个文件", null, ["att_1"], "turn_old"]);
  });
});

describe("以真实回执为准：未绑定的附件不得再显示为已带上", () => {
  it("回执说 0 个绑上：消息上的附件被摘掉，并说明为什么", async () => {
    sendTurn.mockResolvedValueOnce({
      ok: true,
      accepted: true,
      turn_id: "turn_new",
      status: "accepted",
      topic_id: null,
      bound_attachment_ids: [],
      rejected: [{ id: "att_1", reason: "这个附件已经属于别的一轮了" }],
    } as never);
    const session = setup();
    userWithAttachment(session, "turn_old");

    await session.retryTurn("turn_old");

    const last = session.messages[session.messages.length - 1];
    expect(last?.role).toBe("user");
    expect(last?.attachmentIds ?? [], "回执说没绑上就不能再显示成带上了").toEqual([]);
    expect(last?.attachments ?? []).toEqual([]);
    expect(last?.attachmentNotice ?? "").toContain("没有附上");
  });

  it("只有旧形状回执（attachments 列表）时也以它为准：列表里没有的附件不算带上", async () => {
    sendTurn.mockResolvedValueOnce({
      ok: true,
      accepted: true,
      turn_id: "turn_new",
      status: "accepted",
      topic_id: null,
      attachments: [],
    } as never);
    const session = setup();

    await session.send("看看这个文件", ["att_1"], [ref()]);

    const last = session.messages[session.messages.length - 1];
    expect(last?.attachmentIds ?? []).toEqual([]);
    expect(last?.attachmentNotice ?? "").toContain("没有附上");
  });

  it("回执说绑上了：标签保留（不误伤正常路径）", async () => {
    sendTurn.mockResolvedValueOnce({
      ok: true,
      accepted: true,
      turn_id: "turn_new",
      status: "accepted",
      topic_id: null,
      bound_attachment_ids: ["att_1"],
      rejected: [],
    } as never);
    const session = setup();
    userWithAttachment(session, "turn_old");

    await session.retryTurn("turn_old");

    const last = session.messages[session.messages.length - 1];
    expect(last?.attachmentIds).toEqual(["att_1"]);
    expect(last?.attachments?.map((a) => a.id)).toEqual(["att_1"]);
    expect(last?.attachmentNotice ?? "").toBe("");
  });
});

describe("结构化失败：保留文本与可恢复信息", () => {
  it("409 + attachment_binding_failed：原因与「哪些附件」都留下来", async () => {
    const body = rejectionBody();
    sendTurn.mockRejectedValueOnce(
      new ApiError(409, "/api/turns", JSON.stringify(body), body),
    );
    const session = setup();

    const ok = await session.send("看看这个文件", ["att_1"], [ref()]);

    expect(ok).toBe(false);
    expect(session.lastSendRejection?.rejected).toEqual([
      { id: "att_1", reason: "这个附件已经属于别的一轮了" },
    ]);
    expect(session.lastSendRejection?.message).toContain("没有附上");
    // 乐观消息撤掉（草稿与附件由 Composer 放回），绝不留下「看起来发出去了」的消息
    expect(session.messages.some((m) => m.role === "user")).toBe(false);
  });

  it("普通失败（没有结构化 detail）：不伪造附件原因，仍按原样报错", async () => {
    sendTurn.mockRejectedValueOnce(new ApiError(500, "/api/turns", "boom"));
    const session = setup();

    const ok = await session.send("看看这个文件", ["att_1"], [ref()]);

    expect(ok).toBe(false);
    expect(session.lastSendRejection).toBeNull();
    expect(session.lastError).toContain("boom");
  });

  it("下一次成功发送会清掉上一次的附件拒绝信息", async () => {
    const body = rejectionBody();
    sendTurn.mockRejectedValueOnce(new ApiError(409, "/api/turns", JSON.stringify(body), body));
    const session = setup();
    await session.send("第一次", ["att_1"], [ref()]);
    expect(session.lastSendRejection).not.toBeNull();

    await session.send("第二次", [], []);

    expect(session.lastSendRejection).toBeNull();
  });
});
