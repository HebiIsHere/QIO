/**
 * FU4：权限模式切换请求必须带 JSON Content-Type。
 *
 * 用户实测：PUT /api/security/mode -> 422「Input should be a valid dictionary」；
 * 根因是请求体是 JSON 文本却没有 Content-Type，FastAPI 的 body: dict 把原始字节
 * 当字符串校验。这里守住「请求头/请求体」两个事实，避免再次回归。
 */
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("../backend", () => ({
  authHeaders: () => ({ Authorization: "Bearer test" }),
  resolveBackend: async () => ({ base: "http://test.local", token: "test" }),
  resetBackend: () => {},
}));

import { setSecurityMode } from "../security";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("setSecurityMode 请求契约", () => {
  it("PUT /api/security/mode 带 Content-Type: application/json，body 是 JSON 对象", async () => {
    const fetchMock = vi.fn(async (_url: string, init: RequestInit) => {
      expect(init.method).toBe("PUT");
      const headers = init.headers as Record<string, string>;
      expect(headers["Content-Type"]).toBe("application/json");
      expect(headers["Content-Type"]).not.toContain("text/plain");
      const parsed = JSON.parse(String(init.body));
      expect(parsed).toEqual({ mode: "read_only", user_confirmed: true });
      return new Response(JSON.stringify({ mode: "read_only", label: "只读模式", legacy_value: "plan" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    });
    vi.stubGlobal("fetch", fetchMock);
    const state = await setSecurityMode("read_only");
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(state.mode).toBe("read_only");
    expect(state.label).toBe("只读模式");
  });

  it("后端 422 时如实抛出（错误信息含 422，调用方不会假装切换成功）", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(JSON.stringify({ detail: [{ type: "dict_type" }] }), {
          status: 422,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );
    await expect(setSecurityMode("restricted")).rejects.toThrow(/422/);
  });
});
