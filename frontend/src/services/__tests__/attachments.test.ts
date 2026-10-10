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
  isBindable,
  isSendable,
  isDesktopShell,
  listAttachments,
  loadPendingAttachments,
  onPathDrop,
  openPlanFor,
  pickLocalPath,
  prepareAttachment,
  removeAttachment,
  restorePendingAttachments,
  retryAttachment,
  savePendingAttachments,
  stateText,
  toAttachmentRef,
  uploadAttachment,
  waitUntilSettled,
  fetchAttachmentContent,
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

function attachmentRef(over: Partial<AttachmentRef> = {}): AttachmentRef {
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

describe("打开方式（问题 5）：可执行 / 脚本类绝不自动执行", () => {
  it("桌面 + 普通文件 → 交给系统默认程序打开（用真实路径）", () => {
    const plan = openPlanFor(attachmentRef({ storedPath: "D:\\data\\attachments\\a.pdf" }), { desktop: true });
    expect(plan.action).toBe("open");
    expect(plan.path).toBe("D:\\data\\attachments\\a.pdf");
  });

  it("桌面 + 可执行/脚本类 → 改为「在文件夹中显示」并说明原因", () => {
    for (const name of ["安装包.exe", "脚本.ps1", "run.bat", "hook.py", "lib.dll", "setup.msi"]) {
      const plan = openPlanFor(attachmentRef({ name, storedPath: "D:\\x\\" + name }), { desktop: true });
      expect(plan.action, name).toBe("reveal");
      expect(plan.reason).toContain("不自动运行");
      expect(plan.path).toContain(name);
    }
  });

  it("浏览器 + QIO 副本 → 安全类型查看；会执行脚本的类型改为下载", () => {
    expect(openPlanFor(attachmentRef({ name: "图.png" }), { desktop: false }).action).toBe("view");
    expect(openPlanFor(attachmentRef({ name: "数据.pdf" }), { desktop: false }).action).toBe("view");
    const html = openPlanFor(attachmentRef({ name: "页.html" }), { desktop: false });
    expect(html.action).toBe("download");
    expect(html.reason).toContain("不内联");
    expect(openPlanFor(attachmentRef({ name: "图.svg" }), { desktop: false }).action).toBe("download");
  });

  it("浏览器 + 引用型 → 明确 blocked：拿不到本地路径，指引去桌面端或重新定位", () => {
    const plan = openPlanFor(
      attachmentRef({ name: "大视频.mp4", kind: "reference", sourcePath: "D:\\v\\大视频.mp4" }),
      { desktop: false },
    );
    expect(plan.action).toBe("blocked");
    expect(plan.reason).toContain("桌面端");
  });

  it("没就绪的附件不给「打开」这个假入口（准备中/失败/丢失）", () => {
    for (const state of ["prepared", "failed", "missing", "changed"] as const) {
      const plan = openPlanFor(attachmentRef({ state }), { desktop: false });
      if (state === "changed") {
        expect(plan.action).toBe("view"); // changed 仍读得到：允许打开
      } else {
        expect(plan.action, state).toBe("blocked");
      }
    }
  });
});

describe("认证 fetch 副本内容（问题 5）", () => {
  it("带认证头 GET /content，返回 Blob", async () => {
    const blob = new Blob(["hello"]);
    fetchMock.mockResolvedValueOnce({
      ok: true,
      status: 200,
      statusText: "OK",
      blob: async () => blob,
    } as unknown as Response);

    const got = await fetchAttachmentContent("att_1");

    expect(got).toBe(blob);
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toBe("http://127.0.0.1:1/api/attachments/att_1/content");
    expect((init as RequestInit).headers).toEqual({ Authorization: "Bearer t0ken" });
  });

  it("409（引用型 / 副本丢失）带出真实原因，不当成功", async () => {
    fetchMock.mockResolvedValueOnce({
      ok: false,
      status: 409,
      statusText: "Conflict",
      text: async () => JSON.stringify({ detail: "这是「引用本地文件」的附件：QIO 没有保存副本" }),
      json: async () => ({}),
    } as unknown as Response);

    await expect(fetchAttachmentContent("att_ref")).rejects.toThrow(/引用本地文件/);
  });

  it("401 会重置后端连接（令牌可能已经换了）", async () => {
    fetchMock.mockResolvedValueOnce({
      ok: false,
      status: 401,
      statusText: "Unauthorized",
      text: async () => JSON.stringify({ detail: "unauthorized" }),
      json: async () => ({}),
    } as unknown as Response);

    await expect(fetchAttachmentContent("att_1")).rejects.toThrow(/本机 API 拒绝了这次请求/);
    expect(backendMock.resetBackend).toHaveBeenCalled();
  });
});

describe("待发附件绑定话题 / 刷新后恢复（问题 3）", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it("按话题分键保存：切话题不串", () => {
    savePendingAttachments("t1", [attachmentRef({ id: "att_t1" })]);
    expect(loadPendingAttachments("t1").map((item) => item.id)).toEqual(["att_t1"]);
    expect(loadPendingAttachments("t2")).toEqual([]);
    expect(loadPendingAttachments(null)).toEqual([]);
  });

  it("恢复时逐条核对后端事实：删掉的、被别的轮次绑走的、别的类型都丢掉并报出名字", async () => {
    savePendingAttachments("t1", [
      attachmentRef({ id: "att_ok", name: "还在.txt" }),
      attachmentRef({ id: "att_gone", name: "删掉了.txt" }),
      attachmentRef({ id: "att_bound", name: "已随别的消息发出.txt" }),
      attachmentRef({ id: "att_other", name: "别的话题.txt" }),
    ]);
    fetchMock.mockImplementation(async (url: string) => {
      const id = String(url).split("/").pop();
      if (id === "att_ok") {
        return jsonResponse({
          attachment: { id, name: "还在.txt", kind: "copy", state: "ready", topic_id: "t1", stored_path: "D:\\x" },
        });
      }
      if (id === "att_bound") {
        return jsonResponse({
          attachment: { id, name: "已随别的消息发出.txt", kind: "copy", state: "ready", topic_id: "t1", turn_id: "turn_9" },
        });
      }
      if (id === "att_other") {
        return jsonResponse({
          attachment: { id, name: "别的话题.txt", kind: "copy", state: "ready", topic_id: "t_other" },
        });
      }
      return {
        ok: false,
        status: 404,
        statusText: "Not Found",
        text: async () => JSON.stringify({ detail: "没有这个附件" }),
        json: async () => ({}),
      } as unknown as Response;
    });

    const { items, dropped } = await restorePendingAttachments("t1");

    expect(items.map((item) => item.id)).toEqual(["att_ok"]);
    expect(dropped).toEqual(["删掉了.txt", "已随别的消息发出.txt", "别的话题.txt"]);
    // 恢复之后落盘的只剩真的能发的那些：刷新再看到的就是这个
    expect(loadPendingAttachments("t1").map((item) => item.id)).toEqual(["att_ok"]);
  });

  it("能从待发列表发送的状态与后端绑定校验一致（prepared/ready/changed）", () => {
    expect(isBindable(attachmentRef({ state: "ready" }))).toBe(true);
    expect(isBindable(attachmentRef({ state: "prepared" }))).toBe(true);
    expect(isBindable(attachmentRef({ state: "changed" }))).toBe(true);
    expect(isBindable(attachmentRef({ state: "failed" }))).toBe(false);
    expect(isBindable(attachmentRef({ state: "missing" }))).toBe(false);
  });
});

describe("原生能力探测（非桌面壳不假装有路径）", () => {
  it("浏览器环境：不是桌面壳，选择器与拖放都返回 null", async () => {
    expect(isDesktopShell()).toBe(false);
    await expect(pickLocalPath()).resolves.toBeNull();
    await expect(onPathDrop(() => {})).resolves.toBeNull();
  });
});
