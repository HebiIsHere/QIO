/**
 * W6 · A04 客户端接线：`services/entityCandidatesApi.ts`（契约 §3.3）。
 *
 * 受控替身（本地 fetch）验证四件事：
 * 1. 路径与查询参数、请求体严格按契约写（后端 W2 会照着挂路由）；
 * 2. 409 是**可被上层识别**的冲突错误（带 status / current_revision），不是普通失败；
 * 3. 失败一律抛出可读错误，不静默吞掉、不返回假成功；
 * 4. 读可以有限重试，写（采纳 / 丢弃）绝不自动重发。
 *
 * 注意：这些断言只覆盖前端与服务端之间的**契约形状**，真实后端尚未接线，
 * 端到端（真库、真候选）未验证。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  EntityCandidateConflictError,
  EntityCandidatesApiError,
  adoptCandidate,
  dismissCandidate,
  isCandidateConflict,
  listAllCandidates,
  listEntityCandidates,
} from "../entityCandidatesApi";

vi.mock("../backend", () => ({
  resolveBackend: vi.fn(async () => ({
    base: "http://127.0.0.1:8734",
    token: "tok_test",
    source: "default" as const,
  })),
  authHeaders: (token: string) => (token ? { Authorization: `Bearer ${token}` } : {}),
  resetBackend: vi.fn(),
}));

const fetchMock = vi.fn();

function jsonResponse(status: number, body: unknown) {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => JSON.stringify(body),
  };
}

function lastCall() {
  const [url, init] = fetchMock.mock.calls[fetchMock.mock.calls.length - 1] as [
    string,
    RequestInit,
  ];
  return { url, init, body: init.body ? JSON.parse(String(init.body)) : undefined };
}

function candidate(overrides: Record<string, unknown> = {}) {
  return {
    candidate_id: "cand_1",
    entity_id: "ec_1",
    entity_name: "王翠华",
    card_state: "active",
    field: "attributes.生日",
    field_label: "生日",
    kind: "attribute",
    current_value: "",
    candidate_value: "1961-03-02",
    reason: "user_value_conflict",
    reason_label: "你已经手动写过这个字段，自动结果不会覆盖它",
    created_at: "2026-10-10T02:00:00+00:00",
    card_revision: 7,
    adoptable: true,
    blocked_reason: "",
    ...overrides,
  };
}

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("列表：路径与查询参数", () => {
  it("跨卡片清单默认带上归档卡片，并原样返回 total/truncated", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(200, {
        candidates: [candidate(), candidate({ candidate_id: "cand_2", card_state: "revoked" })],
        total: 5,
        shown: 2,
        truncated: true,
      }),
    );

    const listing = await listAllCandidates(true);

    expect(lastCall().url).toBe(
      "http://127.0.0.1:8734/api/entities/candidates?include_archived=true&limit=200",
    );
    expect(lastCall().init.method).toBe("GET");
    expect((lastCall().init.headers as Record<string, string>).Authorization).toBe(
      "Bearer tok_test",
    );
    expect(listing.candidates).toHaveLength(2);
    // total / truncated 如实：不受 limit 影响，界面才能说「还有 N 条未显示」
    expect(listing.total).toBe(5);
    expect(listing.shown).toBe(2);
    expect(listing.truncated).toBe(true);
  });

  it("include_archived=false 与自定义 limit 都照传", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, { candidates: [], total: 0, shown: 0, truncated: false }));
    await listAllCandidates(false, 50);
    expect(lastCall().url).toContain("include_archived=false");
    expect(lastCall().url).toContain("limit=50");
  });

  it("单卡片清单：实体 id 进路径，卡片现状一并带回", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(200, {
        entity: { id: "ec_1", name: "王翠华", state: "active" },
        candidates: [candidate()],
      }),
    );

    const r = await listEntityCandidates("ec_1");

    expect(lastCall().url).toBe("http://127.0.0.1:8734/api/entities/ec_1/candidates");
    expect(r.entity?.id).toBe("ec_1");
    expect(r.candidates).toHaveLength(1);
    // 后端没给 total 时不夸大：就是这次真的取回来的条数
    expect(r.total).toBe(1);
    expect(r.truncated).toBe(false);
  });
});

describe("采纳 / 丢弃：请求体与幂等语义", () => {
  it("采纳带 expected_revision，并把后端结果原样返回", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(200, {
        ok: true,
        entity: { id: "ec_1", name: "王翠华" },
        adopted: { field: "attributes.生日", value: "1961-03-02" },
      }),
    );

    const r = await adoptCandidate("ec_1", "cand_1", 7);

    expect(lastCall().url).toBe(
      "http://127.0.0.1:8734/api/entities/ec_1/candidates/cand_1/adopt",
    );
    expect(lastCall().init.method).toBe("POST");
    expect(lastCall().body).toEqual({ expected_revision: 7 });
    expect(r.ok).toBe(true);
    expect(r.adopted?.value).toBe("1961-03-02");
  });

  it("候选已被处理过（already_resolved）是幂等成功，不是错误", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(200, { ok: true, entity: null, adopted: null, already_resolved: true }),
    );
    const r = await adoptCandidate("ec_1", "cand_1", 7);
    expect(r.ok).toBe(true);
    expect(r.already_resolved).toBe(true);
  });

  it("丢弃不改实体值：只发这一条候选的解决请求", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, { ok: true, dismissed: true }));
    const r = await dismissCandidate("ec_1", "cand_1", 7);
    expect(lastCall().url).toBe(
      "http://127.0.0.1:8734/api/entities/ec_1/candidates/cand_1/dismiss",
    );
    expect(lastCall().body).toEqual({ expected_revision: 7 });
    expect(r.dismissed).toBe(true);
  });
});

describe("409 必须能被上层识别", () => {
  it("采纳遇到 revision 不符：抛带 status=409 的冲突错误，附当前 revision", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(409, {
        ok: false,
        conflict: true,
        current_revision: 9,
        reason: "stale_revision",
      }),
    );

    const err = await adoptCandidate("ec_1", "cand_1", 7).catch((e: unknown) => e);

    expect(err).toBeInstanceOf(EntityCandidateConflictError);
    const conflict = err as EntityCandidateConflictError;
    expect(conflict.status).toBe(409);
    expect(conflict.currentRevision).toBe(9);
    expect(conflict.conflictReason).toBe("stale_revision");
    expect(isCandidateConflict(err)).toBe(true);
    expect(conflict.message).toContain("409");
  });

  it("丢弃遇到 409 同样可识别（不重试、不吞掉）", async () => {
    fetchMock.mockResolvedValue(jsonResponse(409, { ok: false, conflict: true, reason: "resolved" }));
    const err = await dismissCandidate("ec_1", "cand_1", 7).catch((e: unknown) => e);
    expect(isCandidateConflict(err)).toBe(true);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("普通失败带 status 与原因，不会变成「成功但空数据」", async () => {
    fetchMock.mockResolvedValue(jsonResponse(500, { reason: "内部错误" }));
    const err = (await adoptCandidate("ec_1", "cand_1", 7).catch((e: unknown) => e)) as EntityCandidatesApiError;
    expect(err).toBeInstanceOf(EntityCandidatesApiError);
    expect(err.status).toBe(500);
    expect(err.message).toContain("内部错误");
  });

  it("响应不是 JSON：如实报错，不返回 undefined 冒充结果", async () => {
    fetchMock.mockResolvedValue({ ok: true, status: 200, text: async () => "<html>oops</html>" });
    const err = (await dismissCandidate("ec_1", "cand_1", 7).catch((e: unknown) => e)) as EntityCandidatesApiError;
    expect(err).toBeInstanceOf(EntityCandidatesApiError);
    expect(err.message).toContain("合法 JSON");
  });
});

describe("读可重试、写不重发", () => {
  it("读遇到网络层失败重试一次后成功", async () => {
    fetchMock
      .mockRejectedValueOnce(new TypeError("network down"))
      .mockResolvedValueOnce(jsonResponse(200, { candidates: [], total: 0, shown: 0, truncated: false }));

    const listing = await listAllCandidates(true);

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(listing.candidates).toEqual([]);
  });

  it("采纳的网络层失败绝不自动重发（避免变成第二次决定）", async () => {
    fetchMock.mockRejectedValue(new TypeError("network down"));
    await expect(adoptCandidate("ec_1", "cand_1", 7)).rejects.toThrow();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("409 不触发重试（那是结论，不是抖动）", async () => {
    fetchMock.mockResolvedValue(jsonResponse(409, { ok: false, conflict: true, reason: "stale" }));
    await expect(listEntityCandidates("ec_1")).rejects.toBeInstanceOf(EntityCandidateConflictError);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
