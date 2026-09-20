import { describe, expect, it, vi, afterEach } from "vitest";
import { invoke } from "@tauri-apps/api/core";
import { isSafeExternalUrl, openExternal } from "../externalLink";

vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn(async () => undefined) }));

describe("外链 scheme 白名单", () => {
  it("放行 https / http / mailto", () => {
    expect(isSafeExternalUrl("https://example.com/a")).toBe(true);
    expect(isSafeExternalUrl("http://127.0.0.1:8734/x")).toBe(true);
    expect(isSafeExternalUrl("HTTPS://EXAMPLE.COM")).toBe(true);
    expect(isSafeExternalUrl("mailto:someone@example.com")).toBe(true);
    expect(isSafeExternalUrl("  https://example.com  ")).toBe(true);
  });

  it("拒绝危险 scheme 与伪装", () => {
    for (const bad of [
      "javascript:alert(1)",
      "JaVaScRiPt:alert(1)",
      "java\nscript:alert(1)",
      "java\tscript:alert(1)",
      "\u0000javascript:alert(1)",
      "data:text/html;base64,PHNjcmlwdD4=",
      "file:///C:/Users/zxy/.ssh/id_rsa",
      "vbscript:msgbox(1)",
      "about:blank",
      "//example.com/path",
      "/relative/path",
      "ftp://example.com/x",
      "",
    ]) {
      expect(isSafeExternalUrl(bad), bad).toBe(false);
    }
  });

  it("非安全 URL 不会被打开", async () => {
    const spy = vi.fn();
    vi.stubGlobal("open", spy);
    expect(await openExternal("javascript:alert(1)")).toBe(false);
    expect(spy).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
  });

  it("浏览器环境用 window.open（noopener）", async () => {
    const spy = vi.fn(() => ({}) as Window);
    vi.stubGlobal("open", spy);
    expect(await openExternal("https://example.com")).toBe(true);
    expect(spy).toHaveBeenCalledWith("https://example.com", "_blank", "noopener,noreferrer");
    vi.unstubAllGlobals();
  });

  it("打开被拦截时如实返回失败", async () => {
    vi.stubGlobal("open", vi.fn(() => null));
    expect(await openExternal("https://example.com")).toBe(false);
    vi.unstubAllGlobals();
  });
});

describe("桌面壳内走 Tauri 官方 open", () => {
  afterEach(() => {
    delete (globalThis as unknown as { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__;
    vi.unstubAllGlobals();
    vi.mocked(invoke).mockClear();
  });

  it("存在 __TAURI_INTERNALS__ 时调用 plugin:shell|open", async () => {
    (globalThis as unknown as { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__ = {};
    expect(await openExternal("https://example.com")).toBe(true);
    expect(invoke).toHaveBeenCalledWith("plugin:shell|open", { path: "https://example.com" });
  });

  it("桌面壳内拒绝危险 scheme（不会 invoke）", async () => {
    (globalThis as unknown as { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__ = {};
    expect(await openExternal("file:///C:/secret.txt")).toBe(false);
    expect(invoke).not.toHaveBeenCalled();
  });
});
