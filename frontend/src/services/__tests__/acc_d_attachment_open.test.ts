/**
 * F03 反例（成员 D · 附件打开）：noopener 下 window.open 的返回值不能用来判断成败。
 *
 * 基线行为（红）：`window.open(url, "_blank", "noopener,noreferrer")` 返回 null →
 *   判为「没打开」→ 立即 revoke 刚建好的 blob URL + 自动回退 `downloadBlob`（额外下载一次）。
 * 期望（绿）：打开请求已发出（哪怕返回 null）→ 视为已交出，不额外下载、不立即 revoke；
 *   只有**可验证的**打开失败（window.open 抛错 / 环境没有 open / 没有 blob URL 支持）
 *   才回退下载并如实说明原因。
 *
 * 模拟边界：这是「真实 service 逻辑 + 受控浏览器全局（window.open / URL.createObjectURL）」，
 * 不假装实机浏览器行为；实机无头验证另见 docs/acc/acc-d.md。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const backendMock = vi.hoisted(() => ({
  resolveBackend: vi.fn(async () => ({ base: "http://127.0.0.1:1", token: "t0ken" })),
  authHeaders: vi.fn((token: string) => ({ Authorization: "Bearer " + token })),
  resetBackend: vi.fn(),
}));
vi.mock("../backend", () => backendMock);

import { openAttachment, type AttachmentRef } from "../attachments";

function ref(over: Partial<AttachmentRef> = {}): AttachmentRef {
  return {
    id: "att_1",
    name: "图.png",
    sizeBytes: 10,
    kind: "copy",
    display: "已保存副本",
    state: "ready",
    error: null,
    ...over,
  };
}

let created: string[];
let revoked: string[];

const fetchMock = vi.fn();

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockResolvedValue({
    ok: true,
    status: 200,
    statusText: "OK",
    blob: async () => new Blob(["x"]),
  });
  created = [];
  revoked = [];
  (URL as unknown as { createObjectURL: (b: Blob) => string }).createObjectURL = vi.fn(() => {
    const url = `blob:qio-${created.length + 1}`;
    created.push(url);
    return url;
  });
  (URL as unknown as { revokeObjectURL: (u: string) => void }).revokeObjectURL = vi.fn((u: string) => {
    revoked.push(u);
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("F03 打开副本：noopener 返回值不作成败判据", () => {
  it("返回 null 但打开请求已发出：视为已交出，不额外下载、不立即 revoke", async () => {
    const open = vi.spyOn(window, "open").mockReturnValue(null);

    const result = await openAttachment(ref(), { desktop: false });

    expect(open).toHaveBeenCalledWith("blob:qio-1", "_blank", "noopener,noreferrer");
    expect(result.action).toBe("view");
    // 只建了「查看」这一个 URL：回退下载会再建一个（基线红点）
    expect(created).toHaveLength(1);
    // 不立即 revoke：新窗口可能还没读完（基线红点）
    expect(revoked).toHaveLength(0);
    // 无法同步确认的情况必须说清楚，且不能谎报失败
    expect(result.note).toContain("新标签页");
  });

  it("可验证的打开失败（window.open 抛错）：如实说明并回退下载", async () => {
    vi.spyOn(window, "open").mockImplementation(() => {
      throw new Error("blocked by policy");
    });

    const result = await openAttachment(ref(), { desktop: false });

    expect(result.action).toBe("download");
    expect(result.note).toContain("拒绝");
    expect(created).toHaveLength(2); // 打开尝试 1 个 + 下载 1 个
  });

  it("环境没有 window.open：可验证失败，回退下载", async () => {
    vi.stubGlobal("open", undefined);
    const result = await openAttachment(ref(), { desktop: false });
    expect(result.action).toBe("download");
    expect(created).toHaveLength(2);
  });

  it("blob URL 延迟释放、连续打开互不撤销", async () => {
    vi.useFakeTimers();
    vi.spyOn(window, "open").mockReturnValue(null);

    await openAttachment(ref(), { desktop: false });
    await openAttachment(ref({ id: "att_2" }), { desktop: false });

    expect(created).toHaveLength(2);
    expect(revoked).toHaveLength(0);

    vi.advanceTimersByTime(60_001);
    expect(revoked).toContain(created[0]);
    expect(revoked).toContain(created[1]);
    // 第二个 URL 没有被第一个的释放计时器提前撤销
    expect(revoked).not.toContain(undefined);
  });

  it("下载入口（不能内联查看的类型）仍然工作：一个 URL + 延迟 revoke", async () => {
    vi.useFakeTimers();
    const result = await openAttachment(ref({ name: "页.html" }), { desktop: false });
    expect(result.action).toBe("download");
    expect(result.note).toContain("不内联");
    expect(created).toHaveLength(1);
    expect(revoked).toHaveLength(0);
    vi.advanceTimersByTime(1_001);
    expect(revoked).toEqual([created[0]]);
  });
});
