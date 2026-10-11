/**
 * 服务层：错误体解析（收尾轮 M4 集成点）。
 *
 * 后端用 FastAPI 的 HTTPException(detail=<dict>) 报告 stale_state / stale_check /
 * impact_confirmation_required，响应形状是 { detail: { error, reason, ... } }。
 * 前端必须**原样读到**里面的真实原因与结构，否则 409 会退化成看不懂的失败，
 * 或者因为把对象当字符串切反而抛异常。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { InteractiveApiError, submitBoard } from "../interactive";

vi.mock("../backend", () => ({
  resolveBackend: async () => ({ base: "http://127.0.0.1:1", token: "t" }),
  authHeaders: () => ({ Authorization: "Bearer t" }),
}));

const realFetch = globalThis.fetch;

beforeEach(() => {
  vi.restoreAllMocks();
});

afterEach(() => {
  globalThis.fetch = realFetch;
});

function jsonResponse(status: number, body: unknown) {
  return {
    ok: false,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

describe("409 的真实原因能被前端读到", () => {
  it("FastAPI dict detail：stale_state 的 reason 与结构都保留", async () => {
    globalThis.fetch = vi.fn(async () =>
      jsonResponse(409, {
        detail: { error: "stale_state", reason: "服务端已有更新的已保存版本（seq=9）", currentSeq: 9 },
      }),
    ) as unknown as typeof fetch;
    await expect(submitBoard("board_default", undefined, "", 3)).rejects.toBeInstanceOf(InteractiveApiError);
    await submitBoard("board_default", undefined, "", 3).catch((err: InteractiveApiError) => {
      expect(err.status).toBe(409);
      expect(err.payload?.error).toBe("stale_state");
      expect(err.payload?.currentSeq).toBe(9);
      expect(err.message).toContain("seq=9");
    });
  });

  it("impact_confirmation_required：affectedTasks 结构完整到达（供界面列出真实影响）", async () => {
    globalThis.fetch = vi.fn(async () =>
      jsonResponse(409, {
        detail: {
          error: "impact_confirmation_required",
          reason: "会改动执行中任务依赖的材料",
          affectedTasks: [{ intentId: "t1", title: "任务一", materials: ["c1"], consequence: "暂停并保留进度" }],
        },
      }),
    ) as unknown as typeof fetch;
    await submitBoard("board_default").catch((err: InteractiveApiError) => {
      expect(err.payload?.error).toBe("impact_confirmation_required");
      expect(err.payload?.affectedTasks?.[0]?.intentId).toBe("t1");
    });
  });

  it("字符串 detail 的老形状仍然可用", async () => {
    globalThis.fetch = vi.fn(async () => jsonResponse(400, { detail: "note 过长" })) as unknown as typeof fetch;
    await submitBoard("board_default").catch((err: InteractiveApiError) => {
      expect(err.message).toContain("note 过长");
    });
  });
});
