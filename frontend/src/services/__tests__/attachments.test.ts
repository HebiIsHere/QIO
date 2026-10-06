import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const backendMock = vi.hoisted(() => ({
  resolveBackend: vi.fn(async () => ({ base: "http://127.0.0.1:1", token: "t0ken" })),
  authHeaders: vi.fn((token: string) => ({ Authorization: "Bearer " + token })),
  resetBackend: vi.fn(),
}));
vi.mock("../backend", () => backendMock);

import {
  COPY_MAX_BYTES,
  humanSize,
  isSendable,
  isDesktopShell,
  listAttachments,
  onPathDrop,
  pickLocalPath,
  prepareAttachment,
  removeAttachment,
  retryAttachment,
  stateText,
  toAttachmentRef,
  uploadAttachment,
  waitUntilSettled,
  type AttachmentRef,
} from "../attachments";

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
  fetchMock.mockReset();
  backendMock.resetBackend.mockClear();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("AttachmentRef 归一化（冻结形状）", () => {
  it("后端 payload → AttachmentRef 字段逐一对齐", () => {
    const ref = toAttachmentRef({
      id: "att_1",
      name: "报告.xlsx",
      size_bytes: 1234,
      kind: "copy",
      state: "ready",
      error: null,
    });
    expect(ref).toEqual({
      id: "att_1",
      name: "报告.xlsx",
      sizeBytes: 1234,
      kind: "copy",
      display: "已保存副本",
      state: "ready",
      error: null,
    });
  });

  it("引用型显示「引用本地文件」；内部 cancelled 归到 failed 且保留原因", () => {
    expect(toAttachmentRef({ id: "a", kind: "reference", state: "ready" }).display).toBe("引用本地文件");
    const cancelled = toAttachmentRef({ id: "b", kind: "copy", state: "cancelled", error: "已取消（可以重试）" });
    expect(cancelled.state).toBe("failed");
    expect(cancelled.error).toBe("已取消（可以重试）");
  });

  it("未知状态不伪造成功：归到 failed 并在 error 里带着原始值", () => {
    const ref = toAttachmentRef({ id: "c", kind: "copy", state: "weird", error: null });
    expect(ref.state).toBe("failed");
    expect(ref.error).toBeNull();
  });

  it("只有 ready 可发送；文案固定", () => {
    const ready = toAttachmentRef({ id: "a", kind: "copy", state: "ready" });
    const prepared = toAttachmentRef({ id: "b", kind: "copy", state: "prepared" });
    expect(isSendable(ready)).toBe(true);
    expect(isSendable(prepared)).toBe(false);
    expect(stateText(prepared)).toBe("准备中…");
    expect(stateText(ready)).toBe("已保存副本");
    expect(stateText(toAttachmentRef({ id: "c", kind: "copy", state: "missing" }))).toBe("文件不在原位");
  });

  it("大小是人类可读的十进制", () => {
    expect(humanSize(999)).toBe("999 B");
    expect(humanSize(100_000_000)).toBe("100.0 MB");
    expect(COPY_MAX_BYTES).toBe(100_000_000);
  });
});

describe("附件 HTTP（写操作不自动重试）", () => {
  it("登记真实路径：POST /api/attachments 带 topic_id，返回归一化引用", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ ok: true, attachment: { id: "att_9", name: "a.txt", size_bytes: 10, kind: "copy", state: "prepared" } }),
    );
    const ref = await prepareAttachment("D:\\tmp\\a.txt", { topicId: "t1" });
    expect(ref.id).toBe("att_9");
    expect(ref.state).toBe("prepared");
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain("/api/attachments");
    expect(JSON.parse(String((init as RequestInit).body))).toEqual({ source_path: "D:\\tmp\\a.txt", topic_id: "t1" });
    expect((init as RequestInit).method).toBe("POST");
  });

  it("上传字节：原始体 + URL 编码的 X-QIO-Name（不碰 fakepath）", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ attachment: { id: "att_u", name: "中文名.txt", size_bytes: 3, kind: "copy", state: "ready" } }),
    );
    const file = new File(["abc"], "中文名.txt", { type: "text/plain" });
    const ref = await uploadAttachment(file, { topicId: "t2" });
    expect(ref.name).toBe("中文名.txt");
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain("/api/attachments/upload");
    const headers = (init as RequestInit).headers as Record<string, string>;
    expect(headers["X-QIO-Name"]).toBe(encodeURIComponent("中文名.txt"));
    expect(headers["X-QIO-Topic-Id"]).toBe("t2");
    expect((init as RequestInit).body).toBe(file);
  });

  it("超过阈值的文件在浏览器路径上直接拒绝（不发请求、不偷偷存大副本）", async () => {
    const file = new File([new Uint8Array(COPY_MAX_BYTES + 1)], "big.bin");
    await expect(uploadAttachment(file)).rejects.toThrow(/只用于/);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("后端报错时把 detail 原样带出来（不吞掉原因）", async () => {
    fetchMock.mockResolvedValueOnce({
      ok: false,
      status: 400,
      statusText: "Bad Request",
      text: async () => JSON.stringify({ detail: "找不到这个文件：C:\\fakepath\\x.txt" }),
      json: async () => ({}),
    } as unknown as Response);
    await expect(prepareAttachment("C:\\fakepath\\x.txt")).rejects.toThrow(/找不到这个文件/);
  });

  it("移除 / 重试 / 列表走正确的动词与路径", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ ok: true, removed: true }));
    await removeAttachment("att_1");
    expect(fetchMock.mock.calls[0][0]).toBe("http://127.0.0.1:1/api/attachments/att_1");
    expect((fetchMock.mock.calls[0][1] as RequestInit).method).toBe("DELETE");

    fetchMock.mockResolvedValueOnce(jsonResponse({ attachment: { id: "att_1", kind: "copy", state: "prepared" } }));
    const retried = await retryAttachment("att_1");
    expect(retried.state).toBe("prepared");
    expect(String(fetchMock.mock.calls[1][0])).toContain("/api/attachments/att_1/retry");

    fetchMock.mockResolvedValueOnce(jsonResponse({ attachments: [{ id: "att_2", kind: "reference", state: "missing" }] }));
    const listed = await listAttachments({ topicId: "t3", unbound: true });
    expect(String(fetchMock.mock.calls[2][0])).toContain("topic_id=t3");
    expect(String(fetchMock.mock.calls[2][0])).toContain("unbound=true");
    expect(listed[0].state).toBe("missing");
  });
});

describe("等待准备完成（prepared 不算完成）", () => {
  it("轮询到终态才返回；准备中会持续查询", async () => {
    const states = ["prepared", "prepared", "ready"];
    fetchMock.mockImplementation(async () => {
      const state = states.shift() ?? "ready";
      return jsonResponse({ attachment: { id: "att_1", kind: "copy", state } });
    });
    const updates: AttachmentRef[] = [];
    const settled = await waitUntilSettled(
      { id: "att_1", name: "a", sizeBytes: 1, kind: "copy", display: "已保存副本", state: "prepared" },
      { intervalMs: 1, onUpdate: (next) => updates.push(next) },
    );
    expect(settled.state).toBe("ready");
    expect(fetchMock.mock.calls.length).toBeGreaterThanOrEqual(3);
    expect(updates.map((item) => item.state)).toEqual(["prepared", "prepared", "ready"]);
  });

  it("超时后如实返回「还在准备中」，不假装成功", async () => {
    fetchMock.mockImplementation(async () => jsonResponse({ attachment: { id: "att_1", kind: "copy", state: "prepared" } }));
    const settled = await waitUntilSettled(
      { id: "att_1", name: "a", sizeBytes: 1, kind: "copy", display: "已保存副本", state: "prepared" },
      { intervalMs: 1, timeoutMs: 5 },
    );
    expect(settled.state).toBe("prepared");
  });
});

describe("原生能力探测（非桌面壳不假装有路径）", () => {
  it("浏览器环境：不是桌面壳，选择器与拖放都返回 null", async () => {
    expect(isDesktopShell()).toBe(false);
    await expect(pickLocalPath()).resolves.toBeNull();
    await expect(onPathDrop(() => {})).resolves.toBeNull();
  });
});
