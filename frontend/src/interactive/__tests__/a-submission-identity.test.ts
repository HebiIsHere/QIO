/**
 * 契约 §1.3：稳定提交请求身份（子智能体 A）。
 *
 * 这一层只有两件事，都必须可离线证明：
 * 1. 纯函数：生成 / 校验 / **内容没变就沿用** / 本机台账往返 / **精确匹配**判定；
 * 2. 服务层：`submitBoard` 把身份放进请求体、`fetchAcceptance` 走精确查证接口，
 *    并且 **404 不抛**（"服务端没有这条记录" 不是错误推导的成功，也不是改判失败）。
 *
 * 受理证据只有一条路径：同一个 clientRequestId 在服务端的精确记录（§1.1.4）。
 * 时间接近 / 最近一条理解记录 / 板面出现新任务都**不是**证据 —— 这些判定在 store 里
 * 已经删除，由 n7 的 store 级反例锁死。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  SUBMISSION_IDENTITY_STORAGE_PREFIX,
  acceptanceLookupText,
  acceptanceMatchesIdentity,
  canLookupAcceptance,
  identityConflictText,
  isRequestIdentityConflict,
  isUsableSubmissionRequestId,
  newSubmissionRequestId,
  parseStoredSubmissionIdentity,
  serializeSubmissionIdentity,
  submissionIdentityFor,
  submissionIdentityMatches,
  submissionIdentityStorageKey,
} from "../submission";
import { InteractiveApiError, fetchAcceptance, submitBoard } from "../../services/interactive";
import type { AcceptanceLookup } from "../types";

vi.mock("../../services/backend", () => ({
  resolveBackend: async () => ({ base: "http://127.0.0.1:1", token: "t" }),
  authHeaders: () => ({ Authorization: "Bearer t" }),
}));

const realFetch = globalThis.fetch;

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

beforeEach(() => {
  vi.restoreAllMocks();
});

afterEach(() => {
  globalThis.fetch = realFetch;
});

function acceptance(over: Partial<AcceptanceLookup> = {}): AcceptanceLookup {
  return {
    clientRequestId: "creq_abc12345_deadbeefdeadbeef",
    submissionId: "s1",
    status: "succeeded",
    delivery: { delivered: true, reason: "", detail: "", understandingId: "u1", status: "accepted" },
    understandingId: "u1",
    understandingStatus: "accepted",
    baseStateVersion: 7,
    note: "",
    createdAt: "2026-10-11T12:00:00+00:00",
    replayed: false,
    ...over,
  };
}

describe("请求身份的纯函数（契约 §1.3）", () => {
  it("生成的必须是可校验的稳定身份，且两次生成不同", () => {
    const a = newSubmissionRequestId();
    const b = newSubmissionRequestId();
    expect(isUsableSubmissionRequestId(a), "生成的身份必须能作为请求身份").toBe(true);
    expect(a).not.toBe(b);
    expect(isUsableSubmissionRequestId(""), "空字符串 = 旧客户端，不能当身份").toBe(false);
    expect(isUsableSubmissionRequestId("submission-1"), "自造的非前缀 id 不能当身份").toBe(false);
    expect(isUsableSubmissionRequestId(undefined)).toBe(false);
  });

  it("内容没变就沿用同一个身份；内容变了必须是新身份", () => {
    const first = submissionIdentityFor({ boardId: "board_default", baseStateVersion: 4, note: "" });
    const same = submissionIdentityFor({
      boardId: "board_default",
      baseStateVersion: 4,
      note: "",
      existing: first,
    });
    expect(same.clientRequestId, "同一次未决提交重启后必须还是同一身份").toBe(first.clientRequestId);
    for (const changed of [
      { boardId: "board_other", baseStateVersion: 4, note: "" },
      { boardId: "board_default", baseStateVersion: 5, note: "" },
      { boardId: "board_default", baseStateVersion: 4, note: "改了说明" },
    ]) {
      const next = submissionIdentityFor({ ...changed, existing: first });
      expect(next.clientRequestId, "内容不同就不是同一次提交：" + JSON.stringify(changed)).not.toBe(
        first.clientRequestId,
      );
    }
  });

  it("归属判定只认精确身份，且内容依据不一致时拒绝", () => {
    const identity = submissionIdentityFor({
      boardId: "board_default",
      baseStateVersion: 7,
      note: "",
      makeId: () => "creq_abc12345_deadbeefdeadbeef",
      now: 1,
    });
    expect(canLookupAcceptance(identity)).toBe(true);
    expect(canLookupAcceptance(null)).toBe(false);
    expect(acceptanceMatchesIdentity(identity, acceptance())).toBe(true);
    expect(
      acceptanceMatchesIdentity(
        identity,
        acceptance({ clientRequestId: "creq_other999_ffffffffffffffff" }),
      ),
      "别的身份的记录不算这次受理",
    ).toBe(false);
    expect(
      acceptanceMatchesIdentity(identity, acceptance({ baseStateVersion: 9 })),
      "同身份但内容依据不同：不许认领（§1.1.3）",
    ).toBe(false);
    expect(acceptanceMatchesIdentity(identity, null)).toBe(false);
    expect(submissionIdentityMatches(identity, { boardId: "board_default", note: "" })).toBe(true);
    expect(submissionIdentityMatches(identity, { note: "别的内容" })).toBe(false);
  });

  it("本机台账往返：形状不合法的记录一律当没有", () => {
    const identity = submissionIdentityFor({
      boardId: "board_default",
      baseStateVersion: 3,
      note: "",
      makeId: () => "creq_store1234_0011223344556677",
      now: 42,
    });
    const raw = serializeSubmissionIdentity(identity);
    expect(parseStoredSubmissionIdentity(raw)).toEqual(identity);
    for (const bad of ["", null, undefined, "not json", "{}", JSON.stringify({ clientRequestId: "x" })]) {
      expect(parseStoredSubmissionIdentity(bad as string | null), String(bad)).toBeNull();
    }
    expect(submissionIdentityStorageKey("board_default")).toBe(
      SUBMISSION_IDENTITY_STORAGE_PREFIX + "board_default",
    );
  });

  it("身份冲突（409 request_identity_conflict）按冲突如实说明，不认领受理", () => {
    const conflict = Object.assign(new Error("409"), {
      status: 409,
      payload: { error: "request_identity_conflict" },
    });
    expect(isRequestIdentityConflict(conflict)).toBe(true);
    expect(isRequestIdentityConflict(Object.assign(new Error("409"), { status: 409, payload: { error: "stale_state" } }))).toBe(false);
    expect(isRequestIdentityConflict(new Error("Failed to fetch"))).toBe(false);
    expect(identityConflictText()).toContain("同一个请求身份");
    expect(identityConflictText()).toContain("没有创建提交");
    expect(identityConflictText()).toContain("新的请求身份");
  });

  it("查证结论的文案：命中才说已受理；没查到必须说「结果仍然未知」", () => {
    const hit = acceptanceLookupText({ confirmed: true, replayed: true });
    expect(hit).toContain("同一个请求身份");
    expect(hit).toContain("原记录");
    const miss = acceptanceLookupText({ confirmed: false, reason: "服务端没有这条记录" });
    expect(miss).toContain("服务端没有这条记录");
    expect(miss, "查不到不许改判失败").toContain("结果仍然未知");
    expect(miss, "查不到不许诱导重复提交").toContain("不会自动重复提交");
  });
});

describe("服务层：身份随请求发出、查证走精确接口", () => {
  it("submitBoard 把 clientRequestId 放进请求体；没有身份时不带这个字段", async () => {
    const calls: { url: string; body: Record<string, unknown> }[] = [];
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({ url: String(input), body: JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown> });
      return jsonResponse(200, { status: "succeeded" });
    }) as unknown as typeof fetch;

    await submitBoard("board_default", undefined, "", 3, undefined, "creq_abc12345_deadbeefdeadbeef");
    expect(calls[0].body.clientRequestId).toBe("creq_abc12345_deadbeefdeadbeef");
    expect(calls[0].body.baseStateVersion).toBe(3);

    await submitBoard("board_default", undefined, "", 3);
    expect("clientRequestId" in calls[1].body, "旧客户端行为：不带身份").toBe(false);
  });

  it("fetchAcceptance 走精确查证路径；404 不抛，返回 acceptance: null", async () => {
    const urls: string[] = [];
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      urls.push(String(input));
      return jsonResponse(404, { acceptance: null, reason: "没有这条请求身份的记录" });
    }) as unknown as typeof fetch;

    const miss = await fetchAcceptance("board_default", "creq_abc12345_deadbeefdeadbeef");
    expect(miss.acceptance, "404 = 没有记录，不是成功").toBeNull();
    expect(miss.reason).toContain("没有这条请求身份的记录");
    expect(urls[0]).toContain("/api/interactive/boards/board_default/submissions/acceptance");
    expect(urls[0]).toContain("clientRequestId=creq_abc12345_deadbeefdeadbeef");

    globalThis.fetch = vi.fn(async () =>
      jsonResponse(200, { acceptance: acceptance() }),
    ) as unknown as typeof fetch;
    const hit = await fetchAcceptance("board_default", "creq_abc12345_deadbeefdeadbeef");
    expect(hit.acceptance?.submissionId).toBe("s1");

    globalThis.fetch = vi.fn(async () =>
      jsonResponse(500, { detail: "boom" }),
    ) as unknown as typeof fetch;
    await expect(
      fetchAcceptance("board_default", "creq_abc12345_deadbeefdeadbeef"),
      "查询失败必须抛出，由调用方继续「结果未知」",
    ).rejects.toBeInstanceOf(InteractiveApiError);
  });
});
