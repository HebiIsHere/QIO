/**
 * F10 反例（成员 D · 待发附件恢复）：暂时性失败不能当永久无效清理持久化。
 *
 * 基线行为（红）：restorePendingAttachments 对**任何**异常都 `dropped.push(name)`，
 *   最后 `savePendingAttachments(topicId, items)` 把查询失败的附件从 localStorage 里删掉。
 *   查询返回 500 / 超时 / 离线 / 鉴权失败都会导致「已持久化的可用附件 ID 被清空」。
 * 期望（绿）：只有**确认永久无效**（404 / 已绑定别的轮 / 不属于本话题）才清理；
 *   暂时失败保留身份与原持久化数据，返回 unconfirmed 供界面展示可重试状态；
 *   恢复期间新写入的附件不能被旧恢复抹掉；再次恢复成功后附件回来且没有重复。
 *
 * 模拟边界：真实 service 逻辑 + 受控 fetch（按状态码/异常构造后端响应）。
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

describe("F10 恢复：暂时失败保留持久化", () => {
  it("500：不清理持久化，返回 unconfirmed，不报成 dropped", async () => {
    savePendingAttachments("t1", [ref({ id: "att_keep", name: "还在.txt" })]);
    fetchMock.mockResolvedValueOnce(jsonResponse({ detail: "内部错误" }, 500));

    const out = await restorePendingAttachments("t1");

    expect(out.dropped).toEqual([]);
    expect(out.unconfirmed.map((i) => i.id)).toEqual(["att_keep"]);
    expect(loadPendingAttachments("t1").map((i) => i.id)).toEqual(["att_keep"]);
  });

  it("超时：保留身份，不清空", async () => {
    savePendingAttachments("t1", [ref({ id: "att_timeout" })]);
    fetchMock.mockRejectedValueOnce(new DOMException("The operation was aborted.", "AbortError"));

    const out = await restorePendingAttachments("t1");

    expect(out.dropped).toEqual([]);
    expect(out.unconfirmed.map((i) => i.id)).toEqual(["att_timeout"]);
    expect(loadPendingAttachments("t1").map((i) => i.id)).toEqual(["att_timeout"]);
  });

  it("离线（网络异常）：保留身份，不清空", async () => {
    savePendingAttachments("t1", [ref({ id: "att_offline" })]);
    fetchMock.mockRejectedValueOnce(new TypeError("Failed to fetch"));

    const out = await restorePendingAttachments("t1");

    expect(out.unconfirmed.map((i) => i.id)).toEqual(["att_offline"]);
    expect(loadPendingAttachments("t1").map((i) => i.id)).toEqual(["att_offline"]);
  });

  it("鉴权失败（401）：不当作永久无效，保留并重置后端连接", async () => {
    savePendingAttachments("t1", [ref({ id: "att_auth" })]);
    fetchMock.mockResolvedValueOnce(jsonResponse({ detail: "unauthorized" }, 401));

    const out = await restorePendingAttachments("t1");

    expect(out.dropped).toEqual([]);
    expect(out.unconfirmed.map((i) => i.id)).toEqual(["att_auth"]);
    expect(loadPendingAttachments("t1").map((i) => i.id)).toEqual(["att_auth"]);
    expect(backendMock.resetBackend).toHaveBeenCalled();
  });

  it("404：确认永久无效，清理持久化并报出名字", async () => {
    savePendingAttachments("t1", [ref({ id: "att_gone", name: "删掉了.txt" })]);
    fetchMock.mockResolvedValueOnce(jsonResponse({ detail: "没有这个附件" }, 404));

    const out = await restorePendingAttachments("t1");

    expect(out.dropped).toEqual(["删掉了.txt"]);
    expect(out.items).toEqual([]);
    expect(loadPendingAttachments("t1")).toEqual([]);
  });

  it("混合列表：确认的用新事实、404 清理、暂时失败原样保留", async () => {
    savePendingAttachments("t1", [
      ref({ id: "att_ok", name: "在的.txt", state: "prepared" }),
      ref({ id: "att_gone", name: "没了.txt" }),
      ref({ id: "att_500", name: "查不到.txt" }),
    ]);
    fetchMock.mockImplementation(async (url: string) => {
      const id = String(url).split("/").pop();
      if (id === "att_ok") {
        return jsonResponse({ attachment: { id, name: "在的.txt", kind: "copy", state: "ready", topic_id: "t1" } });
      }
      if (id === "att_500") return jsonResponse({ detail: "内部错误" }, 500);
      return jsonResponse({ detail: "没有这个附件" }, 404);
    });

    const out = await restorePendingAttachments("t1");

    expect(out.items.map((i) => i.id)).toEqual(["att_ok"]);
    expect(out.items[0].state).toBe("ready");
    expect(out.dropped).toEqual(["没了.txt"]);
    expect(out.unconfirmed.map((i) => i.id)).toEqual(["att_500"]);
    const saved = loadPendingAttachments("t1").map((i) => i.id);
    expect(saved).toContain("att_ok");
    expect(saved).toContain("att_500");
    expect(saved).not.toContain("att_gone");
    expect(new Set(saved).size).toBe(saved.length); // 没有重复
  });

  it("恢复期间新增的附件不会被旧恢复抹掉", async () => {
    savePendingAttachments("t1", [ref({ id: "att_slow" })]);
    let release!: () => void;
    const gate = new Promise<void>((r) => {
      release = r;
    });
    fetchMock.mockImplementation(async () => {
      await gate;
      return jsonResponse({ attachment: { id: "att_slow", kind: "copy", state: "ready", topic_id: "t1" } });
    });

    const running = restorePendingAttachments("t1");
    // 恢复在途时用户又加了一个附件（真实持久化写入）
    savePendingAttachments("t1", [ref({ id: "att_slow" }), ref({ id: "att_new", name: "后来加的.txt" })]);
    release();
    const out = await running;

    const saved = loadPendingAttachments("t1").map((i) => i.id);
    expect(saved).toContain("att_slow");
    expect(saved).toContain("att_new");
    expect(out.unconfirmed).toEqual([]);
  });

  it("再次恢复成功：附件回到原话题且没有重复", async () => {
    savePendingAttachments("t1", [ref({ id: "att_retry" })]);
    fetchMock.mockResolvedValueOnce(jsonResponse({ detail: "内部错误" }, 500));
    await restorePendingAttachments("t1");

    fetchMock.mockResolvedValueOnce(
      jsonResponse({ attachment: { id: "att_retry", kind: "copy", state: "ready", topic_id: "t1" } }),
    );
    const second = await restorePendingAttachments("t1");

    expect(second.items.map((i) => i.id)).toEqual(["att_retry"]);
    expect(second.unconfirmed).toEqual([]);
    const saved = loadPendingAttachments("t1").map((i) => i.id);
    expect(saved).toEqual(["att_retry"]);
  });
});
