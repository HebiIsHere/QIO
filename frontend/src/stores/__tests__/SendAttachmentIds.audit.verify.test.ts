/**
 * D 独立验收：附件字段必须随请求发送（审计问题 3 的前端一半 / plan §1.4）。
 *
 * 契约来源：docs/plans/2026-10-06-audit-seven-fixes.md §0 第 3 条 + §1.4。
 * 只依据产品规则：
 *
 *   请求体里 attachment_ids 的**存在性**即语义：**出现**（含空列表）= 显式声明没有附件；
 *   **缺字段** = 旧客户端，后端才走兜底（把话题下未绑定的待发附件绑到这一轮）。
 *
 * 于是前端有两条**刻意分开**的路径（Lead 2026-10-06 裁决）：
 *
 *   1. **真实发送路径** `session.send(text)`（没有待发附件）：一律发送该字段 →
 *      请求体必须含 `attachment_ids: []`，后端因此**不会**兜底误绑；
 *   2. **旧调用形状** `api.sendTurn(msg, topic)`（不传第三参 = 省略字段）：这是保留给
 *      旧客户端的兼容形状，「不传第三参 ⇒ 省略字段」正是后端兜底路径的**隔离依据**
 *      （后端那条兜底由 backend/tests/test_audit_attachment_binding_verify.py 的
 *      绿守卫 `test_missing_field_still_falls_back_for_old_clients` 覆盖）。
 *      第 2 条**不是**漏发，也不是缺陷 —— 不要把它改回「总是带上空数组」。
 *
 * 基线（e428bb9）现状：services/api.ts 用
 *   ...(attachmentIds?.length ? { attachment_ids: attachmentIds } : {})
 * 取值，且真实发送路径 session.send 在没有附件时**根本不传第三参**（第 4254 行附近的
 * 「没有附件时保持既有调用形状」）—— 因此第 1 条用例在修复前应当是**红的**。
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
  it("真实发送路径：纯文字消息（没有待发附件）线上请求带 attachment_ids: []", async () => {
    setup();
    const { useSessionStore } = await import("../session");
    const session = useSessionStore();
    await session.send("只发纯文字");

    const bodies = turnBodies();
    expect(bodies.length, "必须真的发出了一次 /api/turns").toBe(1);
    expect(
      Object.prototype.hasOwnProperty.call(bodies[0], "attachment_ids"),
      "真实发送路径必须发送该字段：后端靠「字段存在」区分「显式没有附件」与「旧客户端没给」",
    ).toBe(true);
    expect(bodies[0].attachment_ids, "没有附件 → 空数组，后端因此不会兜底误绑").toEqual([]);
  });

  it("旧调用形状（api.sendTurn 不传第三参）：**省略**字段 —— 这是后端兜底的隔离依据，不是漏发", async () => {
    setup();
    const { api } = await import("../../services/api");
    await api.sendTurn("旧客户端的纯文字消息", null);

    const bodies = turnBodies();
    expect(bodies.length, "必须真的发出了一次 /api/turns").toBe(1);
    expect(
      Object.prototype.hasOwnProperty.call(bodies[0], "attachment_ids"),
      "旧形状（不传第三参）必须省略该字段：这是「旧客户端走兜底」的隔离依据，"
        + "刻意与真实发送路径分开；把它改成「总是带空数组」会让旧客户端兜底路径消失",
    ).toBe(false);
    expect(bodies[0].message, "消息本身照常发送").toBe("旧客户端的纯文字消息");
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
