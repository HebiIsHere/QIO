/**
 * D 独立验收：附件字段必须随请求发送（审计问题 3 的前端一半 / plan §1.4）。
 *
 * 契约来源：docs/plans/2026-10-06-audit-seven-fixes.md §0 第 3 条 + §1.4。
 * 只依据产品规则：
 *
 *   请求体里 attachment_ids 的**存在性**即语义（出现含空列表 = 显式声明没有附件）；
 *   前端**一律发送该字段**（即使为空），否则后端只能走「缺字段」的旧客户端兜底，
 *   把话题下未绑定的待发附件误绑到一条纯文字消息上。
 *
 * 基线（e428bb9）现状：services/api.ts:434 是
 *   ...(attachmentIds?.length ? { attachment_ids: attachmentIds } : {})
 * 空列表被整个丢掉 —— 因此本文件在修复前应当是**红的**。
 *
 * 验证方式：不 mock api.ts，只 mock 后端地址解析 + 记录真实 fetch 的请求体。
 *
 * 运行：cd frontend; npx vitest run src/stores/__tests__/SendAttachmentIds.audit.verify.test.ts
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";

vi.mock("../../services/backend", () => ({
  resolveBackend: vi.fn(async () => ({ base: "http://127.0.0.1:8799", token: "tk_audit" })),
  authHeaders: vi.fn((token: string) => (token ? { Authorization: "Bearer " + token } : {})),
  resetBackend: vi.fn(),
}));

const calls: { url: string; body: Record<string, unknown> | null }[] = [];

function installFetch() {
  calls.length = 0;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      let body: Record<string, unknown> | null = null;
      if (init?.body && typeof init.body === "string") body = JSON.parse(init.body) as Record<string, unknown>;
      calls.push({ url: String(url), body });
      return new Response(
        JSON.stringify({
          ok: true,
          accepted: true,
          turn_id: "turn_audit",
          status: "accepted",
          topic_id: null,
          messages: [],
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    }),
  );
}

function setup() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return pinia;
}

function turnBodies(): Record<string, unknown>[] {
  return calls.filter((c) => c.url.includes("/api/turns")).map((c) => c.body ?? {});
}

beforeEach(() => {
  installFetch();
});

describe("契约 §1.4：attachment_ids 一律发送", () => {
  it("api.sendTurn 不带附件时，请求体里仍然有 attachment_ids（空数组）", async () => {
    setup();
    const { api } = await import("../../services/api");
    await api.sendTurn("只发纯文字", null);

    const bodies = turnBodies();
    expect(bodies.length, "必须真的发出了一次 /api/turns").toBe(1);
    expect(
      Object.prototype.hasOwnProperty.call(bodies[0], "attachment_ids"),
      "空列表也必须出现在请求体里：后端要靠「字段存在」区分「显式没有附件」与「旧客户端没给」",
    ).toBe(true);
    expect(bodies[0].attachment_ids).toEqual([]);
  });

  it("会话发送纯文字（没有待发附件）时，线上请求也带 attachment_ids: []", async () => {
    setup();
    const { useSessionStore } = await import("../session");
    const session = useSessionStore();
    await session.send("只发纯文字");

    const bodies = turnBodies();
    expect(bodies.length).toBe(1);
    expect(bodies[0].attachment_ids, "发送路径必须把这个字段传到底").toEqual([]);
  });

  it("用户看到的附件 == 发送的附件：纯文字消息的本地消息里没有附件", async () => {
    setup();
    const { useSessionStore } = await import("../session");
    const session = useSessionStore();
    await session.send("只发纯文字");

    const user = [...session.messages].reverse().find((m) => m.role === "user");
    expect(user, "乐观消息必须已经落到消息流里").toBeTruthy();
    expect(user!.attachmentIds ?? [], "没有发附件就不该在本地消息上记附件").toEqual([]);
    expect(user!.attachments ?? [], "也不该伪造附件元数据").toEqual([]);
  });
});
