/**
 * fb-B 恢复补丁服务（R3 第二入口 · 契约 K1.6）。
 *
 * 修复前红：restorePendingAttachments 自己 merge + savePendingAttachments，
 *   旧快照会把恢复期间被用户移除的 ID 重新写回持久化（服务端按旧快照重建）。
 * 修复后绿：patch 模式**不自行写持久化**，只按话题返回补丁
 *   （restored / missing / missingIds / revision），由 Composer 在修订号一致时合并；
 *   tombstone（已移除）候选直接跳过；暂时失败保留身份并带 unconfirmed。
 *
 * 模拟边界：真实 service 逻辑 + 受控 fetch；持久化用真实 localStorage。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const backendMock = vi.hoisted(() => ({
  resolveBackend: vi.fn(async () => ({ base: "http://127.0.0.1:1", token: "t0ken" })),
  authHeaders: vi.fn((token: string) => ({ Authorization: "Bearer " + token })),
  resetBackend: vi.fn(),
}));
vi.mock("../backend", () => backendMock);

import {
  loadPendingAttachments,
  markAttachmentRemoved,
  pendingRevision,
  restorePendingAttachments,
  savePendingAttachments,
  type AttachmentRef,
} from "../attachments";

function ref(over: Partial<AttachmentRef> = {}): AttachmentRef {
  return {
    id: "att_1",
    name: "报告.pdf",
    sizeBytes: 10,
    kind: "copy",
    display: "已保存副本",
    state: "ready",
    error: null,
    ...over,
  };
}

function jsonResponse(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: "OK",
    text: async () => JSON.stringify(body),
    json: async () => body,
  } as unknown as Response;
}

const fetchMock = vi.fn();
const PENDING_KEY = "qio.pending-attachments.v1";

beforeEach(() => {
  localStorage.clear();
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  localStorage.clear();
});

describe("K1.6 restorePendingAttachments 补丁模式", () => {
  it("只返回补丁：不自行写持久化（localStorage 一字未动）", async () => {
    savePendingAttachments("t1", [ref({ id: "att_keep", name: "还在.txt" })]);
    const before = localStorage.getItem(PENDING_KEY);
    const revision = pendingRevision("t1");
    fetchMock.mockResolvedValue(
      jsonResponse({ attachment: { id: "att_keep", name: "还在.txt", kind: "copy", state: "ready", topic_id: "t1" } }),
    );

    const patch = await restorePendingAttachments("t1", { candidateIds: ["att_keep"], revision });

    expect(patch.topicId).toBe("t1");
    expect(patch.revision).toBe(revision);
    expect(patch.restored.map((i) => i.id)).toEqual(["att_keep"]);
    expect(patch.restored[0].state).toBe("ready");
    expect(patch.missing).toEqual([]);
    expect(patch.missingIds).toEqual([]);
    expect(localStorage.getItem(PENDING_KEY)).toBe(before);
    expect(loadPendingAttachments("t1").map((i) => i.id)).toEqual(["att_keep"]);
  });

  it("404：确认永久无效 → missing（名字）与 missingIds，仍然不写持久化", async () => {
    savePendingAttachments("t1", [ref({ id: "att_gone", name: "删掉了.txt" })]);
    const before = localStorage.getItem(PENDING_KEY);
    fetchMock.mockResolvedValueOnce(jsonResponse({ detail: "没有这个附件" }, 404));

    const patch = await restorePendingAttachments("t1", { candidateIds: ["att_gone"], revision: pendingRevision("t1") });

    expect(patch.restored).toEqual([]);
    expect(patch.missing).toEqual(["删掉了.txt"]);
    expect(patch.missingIds).toEqual(["att_gone"]);
    expect(localStorage.getItem(PENDING_KEY)).toBe(before);
  });

  it("暂时失败（500）：身份保留在 restored 里且带 unconfirmed，不清持久化", async () => {
    savePendingAttachments("t1", [ref({ id: "att_500", name: "查不到.txt" })]);
    fetchMock.mockResolvedValueOnce(jsonResponse({ detail: "内部错误" }, 500));

    const patch = await restorePendingAttachments("t1", { candidateIds: ["att_500"], revision: pendingRevision("t1") });

    expect(patch.missing).toEqual([]);
    expect(patch.restored.map((i) => i.id)).toEqual(["att_500"]);
    expect(patch.restored[0].name).toBe("查不到.txt");
    expect(patch.restored[0].unconfirmed).toBe(true);
    expect(loadPendingAttachments("t1").map((i) => i.id)).toEqual(["att_500"]);
  });

  it("已移除（tombstone）的候选直接跳过：不发请求、不出现在补丁里", async () => {
    savePendingAttachments("t1", [ref({ id: "att_removed" })]);
    markAttachmentRemoved("t1", "att_removed");
    fetchMock.mockRejectedValue(new Error("不应该发出任何请求"));

    const patch = await restorePendingAttachments("t1", { candidateIds: ["att_removed"], revision: pendingRevision("t1") });

    expect(patch.restored).toEqual([]);
    expect(patch.missing).toEqual([]);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("candidateIds 不在持久化里时也能核对（以内存列表为准，名字回退到 id）", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ attachment: { id: "att_mem", kind: "copy", state: "ready", topic_id: "t1" } }),
    );

    const patch = await restorePendingAttachments("t1", { candidateIds: ["att_mem"], revision: 0 });

    expect(patch.restored.map((i) => i.id)).toEqual(["att_mem"]);
    expect(patch.missing).toEqual([]);
  });

  it("旧调用形状（不带 options）仍然工作：只读持久化候选并返回三态 outcome", async () => {
    savePendingAttachments("t1", [ref({ id: "att_ok" })]);
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ attachment: { id: "att_ok", kind: "copy", state: "ready", topic_id: "t1" } }),
    );

    const out = await restorePendingAttachments("t1");

    expect(out.items.map((i) => i.id)).toEqual(["att_ok"]);
    expect(out.dropped).toEqual([]);
    expect(out.unconfirmed).toEqual([]);
  });
});
