/**
 * F24 反例（成员 D · 前端等待准备最终态）：后端说「已受理、正在准备」时前端不能就此收手。
 *
 * 基线行为（红）：waitUntilSettled 只看归一化后的 state === "prepared"。
 *   若后端对「缺失副本重试」先返回 state:"missing" + preparing:true，
 *   toAttachmentRef 归一化成 missing → 循环立即结束 → UI 停在旧状态、不再更新。
 * 期望（绿）：响应含 preparing / processing 标识、或 state 是非最终态别名（preparing/processing/
 *   pending/queued/running/accepted/copying/uploading）、或 final === false → 继续等待；
 *   真正的最终态（ready/failed/missing/changed/cancelled，且没有「还在准备」标识）才停止；
 *   状态完全缺失时有界等待，不永久等待、也不谎报失败。
 *
 * 假设（E 的字段定稿前）：兼容多种形态，不依赖单一字段名；定稿后可收窄。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const backendMock = vi.hoisted(() => ({
  resolveBackend: vi.fn(async () => ({ base: "http://127.0.0.1:1", token: "t0ken" })),
  authHeaders: vi.fn((token: string) => ({ Authorization: "Bearer " + token })),
  resetBackend: vi.fn(),
}));
vi.mock("../backend", () => backendMock);

import { waitUntilSettled, type AttachmentRef } from "../attachments";

function ref(over: Partial<AttachmentRef> = {}): AttachmentRef {
  return {
    id: "att_1",
    name: "a.txt",
    sizeBytes: 1,
    kind: "copy",
    display: "已保存副本",
    state: "prepared",
    error: null,
    ...over,
  };
}

function payload(attachment: Record<string, unknown>) {
  return {
    ok: true,
    status: 200,
    statusText: "OK",
    text: async () => JSON.stringify({ attachment }),
    json: async () => ({ attachment }),
  } as unknown as Response;
}

const fetchMock = vi.fn();

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("F24 等待准备最终态", () => {
  it("missing + preparing:true（后端已受理正在准备）→ 继续等到 ready", async () => {
    fetchMock
      .mockResolvedValueOnce(payload({ id: "att_1", kind: "copy", state: "missing", preparing: true }))
      .mockResolvedValueOnce(payload({ id: "att_1", kind: "copy", state: "ready" }));

    const settled = await waitUntilSettled(ref(), { intervalMs: 1, timeoutMs: 500 });

    expect(settled.state).toBe("ready");
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("非最终态别名（processing / pending / queued / running）继续等待", async () => {
    fetchMock
      .mockResolvedValueOnce(payload({ id: "att_1", kind: "copy", state: "processing" }))
      .mockResolvedValueOnce(payload({ id: "att_1", kind: "copy", state: "pending" }))
      .mockResolvedValueOnce(payload({ id: "att_1", kind: "copy", state: "queued" }))
      .mockResolvedValueOnce(payload({ id: "att_1", kind: "copy", state: "running" }))
      .mockResolvedValueOnce(payload({ id: "att_1", kind: "copy", state: "ready" }));

    const settled = await waitUntilSettled(ref(), { intervalMs: 1, timeoutMs: 500 });

    expect(settled.state).toBe("ready");
    expect(fetchMock).toHaveBeenCalledTimes(5);
  });

  it("final:false 覆盖 ready：仍继续等待", async () => {
    fetchMock
      .mockResolvedValueOnce(payload({ id: "att_1", kind: "copy", state: "ready", final: false }))
      .mockResolvedValueOnce(payload({ id: "att_1", kind: "copy", state: "ready", final: true }));

    const settled = await waitUntilSettled(ref(), { intervalMs: 1, timeoutMs: 500 });

    expect(settled.state).toBe("ready");
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("明确最终 missing（没有准备标识）不再等待：有界返回", async () => {
    fetchMock.mockResolvedValueOnce(payload({ id: "att_1", kind: "copy", state: "missing" }));

    const settled = await waitUntilSettled(ref(), { intervalMs: 1, timeoutMs: 500 });

    expect(settled.state).toBe("missing");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("状态完全缺失：有界等待，超时后如实停在准备中（不永久等待、不谎报失败）", async () => {
    fetchMock.mockImplementation(async () => payload({ id: "att_1", kind: "copy" }));

    const settled = await waitUntilSettled(ref(), { intervalMs: 5, timeoutMs: 200 });

    expect(settled.state).toBe("prepared");
    expect(fetchMock.mock.calls.length).toBeGreaterThan(1);
  });

  it("输入已经是最终态：不额外请求", async () => {
    const ready = ref({ state: "ready" });
    const settled = await waitUntilSettled(ready, { intervalMs: 1, timeoutMs: 500 });
    expect(settled).toBe(ready);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
