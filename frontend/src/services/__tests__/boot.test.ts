/**
 * 启动握手：先拿后端地址与令牌，再确认后端真的在应答。
 *
 * 背景（2026-09-24 实测）：外壳先把窗口显示出来，后端要几秒才起来。这期间
 * 任何"调接口 → 失败"都让界面看起来像卡死（白屏）。把"等后端"做成一个状态，
 * 界面才能说清在等什么、等不到怎么办。
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { waitForBackend } from "../boot";

const backend = vi.hoisted(() => ({ resolveBackend: vi.fn() }));
vi.mock("../backend", () => ({ resolveBackend: backend.resolveBackend }));

beforeEach(() => {
  vi.clearAllMocks();
  backend.resolveBackend.mockResolvedValue({ base: "http://127.0.0.1:5555", token: "tk" });
});

function healthOk() {
  return { ok: true, status: 200 } as Response;
}

describe("waitForBackend", () => {
  it("后端还没应答时一直重试，应答后返回地址与令牌", async () => {
    const fetchMock = vi
      .fn()
      .mockRejectedValueOnce(new Error("ECONNREFUSED"))
      .mockResolvedValueOnce({ ok: false, status: 503 })
      .mockResolvedValueOnce(healthOk());
    vi.stubGlobal("fetch", fetchMock);

    const ready = await waitForBackend({ timeoutMs: 3000, intervalMs: 5, probeTimeoutMs: 50 });

    expect(ready).toEqual({ base: "http://127.0.0.1:5555", token: "tk" });
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(String(fetchMock.mock.calls[0][0])).toContain("/api/health");
  });

  it("等不到后端时给出原因（含最后一次错误），不假装成功", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockRejectedValue(new Error("Failed to fetch")),
    );

    await expect(
      waitForBackend({ timeoutMs: 60, intervalMs: 5, probeTimeoutMs: 20 }),
    ).rejects.toThrow(/Failed to fetch/);
  });

  it("外壳还没发布令牌时也算没就绪（不缓存失败）", async () => {
    backend.resolveBackend
      .mockRejectedValueOnce(new Error("backend did not publish a session token"))
      .mockResolvedValueOnce({ base: "http://127.0.0.1:5555", token: "tk" });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(healthOk()));

    const ready = await waitForBackend({ timeoutMs: 3000, intervalMs: 5, probeTimeoutMs: 50 });

    expect(ready.token).toBe("tk");
    expect(backend.resolveBackend).toHaveBeenCalledTimes(2);
  });

  it("会把等待进度回报给调用方（界面显示已等多久）", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockRejectedValueOnce(new Error("x")).mockResolvedValue(healthOk()),
    );
    const ticks: number[] = [];

    await waitForBackend({
      timeoutMs: 3000,
      intervalMs: 5,
      probeTimeoutMs: 20,
      onTick: (ms) => ticks.push(ms),
    });

    expect(ticks.length).toBeGreaterThan(0);
  });
});
