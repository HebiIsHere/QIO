/**
 * F 独立验证（阶段一 · F03）：打开 URL 的成功被误判 + blob URL 过早 revoke。
 *
 * 契约：docs/plans/2026-10-09-process-attachment-audit-consolidation.md §C4 相关（打开外部链接 /
 * 附件副本），产品规则是「失败要如实报告，成功不得被当成失败」。
 *
 * 真实浏览器语义：window.open(url, "_blank", "noopener,...") 在启用 noopener 时**返回 null**
 * （这是正常成功，不是被拦截）。基线把 null 当失败：
 *   * externalLink.openExternal 返回 false → 界面把成功打开标成「打开失败」；
 *   * attachments.openBlobInNewTab 返回 false → 立刻 revokeObjectURL（过早撤销），
 *     并退化成下载（用户点了「打开」却下载了文件）。
 *
 * 本用例用真实服务函数 + 真实 URL/window.open 语义（stub）复现，不用实现内部形状。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const backendMock = vi.hoisted(() => ({
  resolveBackend: vi.fn(async () => ({ base: "http://127.0.0.1:1", token: "t0ken" })),
  authHeaders: vi.fn(() => ({ Authorization: "Bearer t0ken" })),
  resetBackend: vi.fn(),
}));
vi.mock("../backend", () => backendMock);

import { openAttachment, type AttachmentRef } from "../attachments";
import { openExternal } from "../../utils/externalLink";

/** 真实浏览器：features 含 noopener 时 window.open 返回 null。 */
function realBrowserOpen() {
  return vi.fn((_url: string, _target?: string, features?: string) =>
    features && features.includes("noopener") ? null : ({ opener: null } as unknown as Window),
  );
}

function blobResponse(body = "PNGDATA") {
  return {
    ok: true,
    status: 200,
    statusText: "OK",
    blob: async () => new Blob([body], { type: "image/png" }),
    text: async () => body,
  } as unknown as Response;
}

const ref: AttachmentRef = {
  id: "att_accf03",
  name: "示意图.png",
  sizeBytes: 7,
  kind: "copy",
  display: "已保存副本",
  state: "ready",
};

let createSpy: ReturnType<typeof vi.fn>;
let revokeSpy: ReturnType<typeof vi.fn>;

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn(async () => blobResponse()));
  createSpy = vi.fn(() => "blob:qio/accf03");
  revokeSpy = vi.fn();
  Object.defineProperty(URL, "createObjectURL", { configurable: true, value: createSpy });
  Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: revokeSpy });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("F03 externalLink：noopener 下 window.open 返回 null 是成功，不是失败", () => {
  it("openExternal 如实返回成功", async () => {
    const open = realBrowserOpen();
    vi.stubGlobal("open", open);
    const ok = await openExternal("https://example.com");
    expect(open).toHaveBeenCalled();
    expect(ok).toBe(true);
  });
});

describe("F03 附件浏览器打开：不得过早 revoke blob URL、不得退化成下载", () => {
  it("真实 noopener 语义下 openAttachment 报告已打开且不同步 revoke", async () => {
    const open = realBrowserOpen();
    vi.stubGlobal("open", open);
    const result = await openAttachment(ref, { desktop: false });
    expect(open).toHaveBeenCalled();
    expect(result.action).toBe("view");
    expect(createSpy).toHaveBeenCalled();
    expect(revokeSpy).not.toHaveBeenCalled();
  });
});
