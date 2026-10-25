/**
 * R9 权限模式客户端（契约 §1）—— 定向测试。
 *
 * 覆盖：旧值映射、未知值不静默提高、GET 新接口、旧接口兼容读取、
 * PUT 必须带 user_confirmed=true、失败原因、切换的生效边界文案。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../backend", () => ({
  resolveBackend: vi.fn(async () => ({ base: "http://test.local", token: "t0ken", source: "env" })),
  authHeaders: (token: string) => (token ? { Authorization: "Bearer " + token } : {}),
  resetBackend: vi.fn(),
}));

import {
  LEGACY_MODE_MAP,
  getSecurityMode,
  legacyValueFor,
  legacyValueLabel,
  normalizePermissionMode,
  permissionModeSwitchNote,
  setSecurityMode,
  SecurityRequestError,
} from "../security";

function jsonResponse(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 404 ? "Not Found" : "OK",
    text: async () => JSON.stringify(body),
    json: async () => body,
  };
}

const fetchMock = vi.fn();
function stubFetch() {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
}

beforeEach(() => {
  stubFetch();
});

describe("三模式与旧值映射", () => {
  it("新值原样接受；旧值按 LEGACY_MODE_MAP 映射；未知值落到 restricted（不静默提高）", () => {
    expect(normalizePermissionMode("read_only")).toBe("read_only");
    expect(normalizePermissionMode("restricted")).toBe("restricted");
    expect(normalizePermissionMode("unrestricted")).toBe("unrestricted");
    expect(normalizePermissionMode("plan")).toBe("read_only");
    expect(normalizePermissionMode("default")).toBe("restricted");
    expect(normalizePermissionMode("accept-edits")).toBe("restricted");
    expect(normalizePermissionMode("bypass")).toBe("unrestricted");
    expect(normalizePermissionMode("something-new")).toBe("restricted");
    expect(normalizePermissionMode(undefined)).toBe("restricted");
    expect(LEGACY_MODE_MAP.bypass).toBe("unrestricted");
  });

  it("新模式有等价旧值（兼容旧客户端），旧值说明拿不到时如实显示原值", () => {
    expect(legacyValueFor("read_only")).toBe("plan");
    expect(legacyValueFor("restricted")).toBe("default");
    expect(legacyValueFor("unrestricted")).toBe("bypass");
    expect(legacyValueLabel("default")).toContain("默认");
    expect(legacyValueLabel("legacy-unknown")).toContain("legacy-unknown");
  });
});

describe("GET /api/security/mode", () => {
  it("新接口返回 {mode,label,legacy_value}：原样收敛，compat=false", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ mode: "read_only", label: "只读模式", legacy_value: "plan" }),
    );
    const state = await getSecurityMode();
    expect(state).toEqual({
      mode: "read_only",
      label: "只读模式",
      legacyValue: "plan",
      compat: false,
    });
    expect(String(fetchMock.mock.calls[0][0])).toBe("http://test.local/api/security/mode");
    expect((fetchMock.mock.calls[0][1] as RequestInit).headers).toMatchObject({
      Authorization: "Bearer t0ken",
    });
  });

  it("旧后端没有该接口（404）→ 读 /api/settings/computer 并做旧值映射展示（compat=true）", async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ detail: "not found" }, 404))
      .mockResolvedValueOnce(jsonResponse({ root_dir: "", permission_mode: "bypass" }));
    const state = await getSecurityMode();
    expect(state.mode).toBe("unrestricted");
    expect(state.compat).toBe(true);
    expect(state.legacyValue).toBe("bypass");
    expect(String(fetchMock.mock.calls[1][0])).toContain("/api/settings/computer");
  });

  it("非 404 失败原样抛出（不把「坏了」说成旧接口兼容）", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ detail: "boom" }, 500));
    await expect(getSecurityMode()).rejects.toBeInstanceOf(SecurityRequestError);
  });
});

describe("PUT /api/security/mode（只有用户操作能调用）", () => {
  it("请求体必须带 mode + user_confirmed=true，响应按服务端事实收敛", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ mode: "unrestricted", label: "完全无限制模式", legacy_value: "bypass" }),
    );
    const state = await setSecurityMode("unrestricted");
    expect(state.mode).toBe("unrestricted");
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toBe("http://test.local/api/security/mode");
    expect((init as RequestInit).method).toBe("PUT");
    expect(JSON.parse(String((init as RequestInit).body))).toEqual({
      mode: "unrestricted",
      user_confirmed: true,
    });
  });

  it("旧值 / 未知值在客户端就被拒绝（不发请求，不发明第二套权限入口）", async () => {
    await expect(setSecurityMode("bypass" as never)).rejects.toThrow(/read_only/);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("切换生效边界文案（必须诚实）", () => {
  it("收紧：新操作立即生效；已开始的执行沿用开始权限；等待中的审批不自动批准", () => {
    const note = permissionModeSwitchNote("restricted", "read_only");
    expect(note).toContain("新操作立即生效");
    expect(note).toContain("已经开始的执行沿用开始时的权限");
    expect(note).toContain("等待中的审批不会自动批准");
  });

  it("放宽：不会自动批准等待中的审批，也不会复活已拒绝/已过期的审批", () => {
    const note = permissionModeSwitchNote("read_only", "unrestricted");
    expect(note).toContain("新操作立即生效");
    expect(note).toContain("等待中的审批不会因此自动通过");
    expect(note).toContain("内部运行数据");
    expect(permissionModeSwitchNote("restricted", "restricted")).toBe("");
  });
});
