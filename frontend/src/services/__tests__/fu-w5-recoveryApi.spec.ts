/**
 * `services/recoveryApi.ts`（A01 + A03 的前端接口层）。
 *
 * 守三件事：
 * 1. 五个动作都打到**契约里写明的**端点与请求体上（路径/字段不能自己发明）；
 * 2. 失败**抛可读错误**，不静默吞 —— 调用方才有东西显示给用户；
 * 3. 服务端给的形状被如实归一化（`actions` / `owner_note` / 截断计数），
 *    形状不对就报错，而不是编一条看起来很正常的记录出来。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

const request = vi.fn();

vi.mock("../api", () => ({
  ApiError: class ApiError extends Error {
    constructor(
      readonly status: number,
      readonly path: string,
      detail: string,
    ) {
      super(`${path} -> ${status}: ${detail}`);
      this.name = "ApiError";
    }
  },
  API_TIMEOUT_MS: { read: 1, bulk: 2, write: 3, long: 4 },
  request: (...args: unknown[]) => request(...args),
  api: {
    // 与 services/api.ts 里的接线一一对应：这里只做「参数怎么进请求」的对照，
    // 真正打到哪个路径由 api.ts 自己保证（下面断言的是它被调用时的实参）。
    getRecoveryRecords: (suffix: string) =>
      request(`/api/recovery/records${suffix}`, { timeoutMs: 2 }),
    continueRecovery: (recordId: string, expected: Record<string, unknown>) =>
      request(`/api/recovery/records/${recordId}/continue`, {
        method: "POST",
        body: JSON.stringify(expected),
      }),
    repairOrphanRecord: (recordId: string, expected: Record<string, unknown>) =>
      request(`/api/recovery/records/${recordId}/repair`, {
        method: "POST",
        body: JSON.stringify(expected),
      }),
    ignoreRecoveryRecord: (recordId: string, expected: Record<string, unknown>) =>
      request(`/api/recovery/records/${recordId}/ignore`, {
        method: "POST",
        body: JSON.stringify(expected),
      }),
    requeueDerivedRecord: (recordId: string, expected: Record<string, unknown>) =>
      request(`/api/recovery/records/${recordId}/requeue`, {
        method: "POST",
        body: JSON.stringify(expected),
      }),
  },
}));

const {
  continueRecovery,
  fetchRecoveryRecords,
  ignoreRecovery,
  isRecoveryConflict,
  repairOrphan,
  requeueDerived,
} = await import("../recoveryApi");

const RECORD = {
  record_id: "turn_a",
  kind: "user_turn",
  state_class: "ready",
  status: "interrupted",
  message: "帮我把发布闸门跑一遍",
  topic_id: "topic_1",
  reason: "queued_at_restart",
  created_at: "2026-10-02T10:00:00+00:00",
  updated_at: "2026-10-02T10:05:00+00:00",
  owner_instance_id: null,
  owner_state: "none",
  owner_note: "这条消息在上次退出时没有开始执行",
  claim_generation: null,
  attempts: null,
  last_error: null,
  actions: [
    { id: "continue", label: "继续发送这条", enabled: true, reason: "" },
    { id: "ignore", label: "忽略", enabled: true, reason: "" },
  ],
};

beforeEach(() => {
  request.mockReset();
});

describe("列举收件箱", () => {
  it("把 limit / kinds / classes 拼进查询串，并归一化服务端形状", async () => {
    request.mockResolvedValue({ records: [RECORD], total: 1, shown: 1, truncated: false });

    const listing = await fetchRecoveryRecords({ limit: 50, kinds: ["user_turn"], classes: ["ready"] });

    expect(request).toHaveBeenCalledTimes(1);
    const [path] = request.mock.calls[0] as [string];
    expect(path).toContain("/api/recovery/records?");
    expect(path).toContain("limit=50");
    expect(path).toContain("kinds=user_turn");
    expect(path).toContain("classes=ready");
    expect(listing.records[0]?.record_id).toBe("turn_a");
    expect(listing.records[0]?.actions.map((a) => a.id)).toEqual(["continue", "ignore"]);
    expect(listing.records[0]?.owner_note).toContain("没有开始执行");
  });

  it("服务端说截断（total > shown）时如实告诉界面「还有未显示」", async () => {
    request.mockResolvedValue({ records: [RECORD], total: 7, shown: 1, truncated: false });

    const listing = await fetchRecoveryRecords();

    expect(listing.truncated).toBe(true);
    expect(listing.total).toBe(7);
  });

  it("没有标识的记录是坏响应：报错，不编一条假记录出来", async () => {
    request.mockResolvedValue({ records: [{ status: "running" }], total: 1, shown: 1 });

    await expect(fetchRecoveryRecords()).rejects.toThrow(/没有标识/);
  });

  it("请求失败原样抛出（调用方要能显示原因）", async () => {
    request.mockRejectedValue(new Error("network down"));

    await expect(fetchRecoveryRecords()).rejects.toThrow("network down");
  });
});

describe("四个动作打到契约端点", () => {
  it("继续：带 expected_class / expected_status", async () => {
    request.mockResolvedValue({ ok: true, record_id: "turn_a", turn_id: "turn_new", status: "queued" });

    const res = await continueRecovery("turn_a", {
      expected_class: "ready",
      expected_status: "interrupted",
    });

    expect(request.mock.calls[0]?.[0]).toBe("/api/recovery/records/turn_a/continue");
    const body = (request.mock.calls[0]?.[1] as { body: string }).body;
    expect(JSON.parse(body)).toEqual({
      expected_class: "ready",
      expected_status: "interrupted",
    });
    expect(res.turn_id).toBe("turn_new");
  });

  it("修复孤儿：只带 expected_class", async () => {
    request.mockResolvedValue({ ok: true, repaired: true, record_id: "turn_a" });

    const res = await repairOrphan("turn_a");

    expect(request.mock.calls[0]?.[0]).toBe("/api/recovery/records/turn_a/repair");
    expect(JSON.parse((request.mock.calls[0]?.[1] as { body: string }).body)).toEqual({
      expected_class: "orphaned_claim",
    });
    expect(res.repaired).toBe(true);
  });

  it("忽略：带 expected_class", async () => {
    request.mockResolvedValue({ ok: true, ignored: true });

    await ignoreRecovery("turn_a", "ready");

    expect(request.mock.calls[0]?.[0]).toBe("/api/recovery/records/turn_a/ignore");
    expect(JSON.parse((request.mock.calls[0]?.[1] as { body: string }).body)).toEqual({
      expected_class: "ready",
    });
  });

  it("重新排队：带 expected_state / expected_generation（迟到写由 generation 挡住）", async () => {
    request.mockResolvedValue({ ok: true, state: "pending" });

    await requeueDerived("task_1", { expected_state: "running", expected_generation: 3 });

    expect(request.mock.calls[0]?.[0]).toBe("/api/recovery/records/task_1/requeue");
    expect(JSON.parse((request.mock.calls[0]?.[1] as { body: string }).body)).toEqual({
      expected_state: "running",
      expected_generation: 3,
    });
  });

  it("409（已经被处理过）能被调用方识别出来", async () => {
    const { ApiError } = await import("../api");
    const conflict = new ApiError(409, "/api/recovery/records/turn_a/continue", "conflict");
    request.mockRejectedValueOnce(conflict);
    request.mockRejectedValueOnce(new Error("boom"));

    await expect(
      continueRecovery("turn_a", { expected_class: "ready" }),
    ).rejects.toBe(conflict);
    expect(isRecoveryConflict(conflict)).toBe(true);

    try {
      await repairOrphan("turn_a");
      throw new Error("should have thrown");
    } catch (e) {
      expect(isRecoveryConflict(e)).toBe(false);
    }
  });

  it("没有标识的记录不能提交（否则会打到 .../records//continue 上）", () => {
    expect(() => continueRecovery("  ", { expected_class: "ready" })).toThrow(/没有标识/);
    expect(request).not.toHaveBeenCalled();
  });
});
