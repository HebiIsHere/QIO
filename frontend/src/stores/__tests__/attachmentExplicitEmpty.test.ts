/**
 * 问题 3 的前端一半（Lead 裁决 task-6）：`attachment_ids` 的**存在性**即语义。
 *
 * * 空数组 = 显式「这条消息没有附件」→ 请求体里必须真的带 `attachment_ids: []`；
 * * 只有**缺字段**才表示旧客户端、才允许后端走兜底绑定。
 *
 * 修复前红：
 *   * `session.send` 在空数组时**不传第三个参数** → api.ts 省略该字段（问题 3 复现）；
 *   * 即便调用方传了 `[]`，api.ts 目前仍按 `attachmentIds?.length` 判断（C 的文件）。
 *
 * 本文件**不 mock api**：断言的是真实请求体（跨模块契约 §1.4），
 * 所以它同时覆盖 session.ts（我的）与 services/api.ts（C 的）两端。
 *
 * 运行：cd frontend; npx vitest run src/stores/__tests__/attachmentExplicitEmpty.test.ts
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { api } from "../../services/api";
import { useSessionStore } from "../session";

interface Captured {
  url: string;
  body: Record<string, unknown> | null;
}

const captured: Captured[] = [];

function okResponse(payload: Record<string, unknown>): Response {
  return {
    ok: true,
    status: 200,
    json: async () => payload,
    text: async () => "",
  } as unknown as Response;
}

beforeEach(() => {
  captured.length = 0;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: unknown, init?: RequestInit) => {
      captured.push({
        url: String(url),
        body: init?.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : null,
      });
      return okResponse({ ok: true, accepted: true, turn_id: "turn_new", status: "accepted", topic_id: "topic_1" });
    }),
  );
});

function turnsRequest(): Captured {
  const found = captured.find((c) => c.url.includes("/api/turns"));
  expect(found, "要能找到发送请求（POST /api/turns）").toBeTruthy();
  return found as Captured;
}

describe("契约 §1.4：空附件也是显式语义", () => {
  it("没有附件时请求体里确实带 attachment_ids: []（不是缺字段）", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const session = useSessionStore();

    const ok = await session.send("纯文字，不带附件");
    expect(ok).toBe(true);

    const body = turnsRequest().body ?? {};
    expect(body.message).toBe("纯文字，不带附件");
    expect(
      Object.prototype.hasOwnProperty.call(body, "attachment_ids"),
      "空数组也必须是显式字段：缺字段会被后端当成旧客户端，绑上遗留附件",
    ).toBe(true);
    expect(body.attachment_ids).toEqual([]);
  });

  it("带附件时照常带上这一轮的附件 id", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const session = useSessionStore();

    await session.send("带一个附件", ["att_1"]);
    expect(turnsRequest().body?.attachment_ids).toEqual(["att_1"]);
  });

  it("api.sendTurn 收到空数组也要写出 attachment_ids（调用方语义不被吞掉）", async () => {
    await api.sendTurn("直接调用", "topic_1", []);
    const body = turnsRequest().body ?? {};
    expect(Object.prototype.hasOwnProperty.call(body, "attachment_ids")).toBe(true);
    expect(body.attachment_ids).toEqual([]);
  });
});
