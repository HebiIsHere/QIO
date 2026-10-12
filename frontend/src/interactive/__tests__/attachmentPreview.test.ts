/**
 * [P2 板面附件预览] 计划与执行辅助的纯函数用例（四条分支 + blob 释放）。
 *
 * 触发：互动板面上的图片 / 文件卡片此前**完全没有预览入口**（openAttachment 只在 Composer /
 * MessageItem 里被调用）。这里测的是新增的纯计划：
 *   1) 有关联（meta.image.attachmentId） → 图片可缩略图 / 文件可查看；
 *   2) 无关联                        → 不给假预览、不给假按钮，如实说读不到正文；
 *   3) 可执行类                      → 桌面只「在文件夹中显示」并说明原因；
 *   4) 浏览器回退                    → blob 新标签页；引用型（无副本）如实说打不开；
 *   5) blob 生命周期                 → 建一次、按 id 缓存、切换释放、卸载释放。
 *
 * 【纯函数 + 受控浏览器全局】不启动真浏览器，也不联网。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  attachmentPreviewActionLabel,
  attachmentPreviewUnavailableReason,
  attachmentUnlinkedText,
  attachmentCardState,
  isPreviewableImageName,
} from "../materials";
import {
  IMAGE_PREVIEW_MAX_BYTES,
  attachmentRefForPreview,
  attachmentPreviewPlan,
  createPreviewBlobHost,
  createPreviewImageState,
} from "../attachmentPreview";
import type { AttachmentRef } from "../../services/attachments";

const IMAGE_META = { name: "示意图.png", attachmentId: "att_img", size: 2048 };
const FILE_META = { name: "报告.pdf", attachmentId: "att_pdf", size: 4096 };

function blob(text: string, type = "image/png"): Blob {
  return new Blob([text], { type });
}

/** 让「取字节 → 建 URL → 更新状态」这条 promise 链跑完（比数 await 更稳）。 */
async function settled(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 0));
}

let created: string[];
let revoked: string[];

beforeEach(() => {
  created = [];
  revoked = [];
  Object.defineProperty(URL, "createObjectURL", {
    configurable: true,
    value: vi.fn(() => {
      const url = "blob:qio-preview-" + (created.length + 1);
      created.push(url);
      return url;
    }),
  });
  Object.defineProperty(URL, "revokeObjectURL", {
    configurable: true,
    value: vi.fn((url: string) => {
      revoked.push(url);
    }),
  });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("分支一：有关联的卡片", () => {
  it("图片 + 位图：可缩略图、可查看，状态一行说清关联了哪份附件", () => {
    const plan = attachmentPreviewPlan("image", { image: IMAGE_META }, { desktop: false });
    expect(plan.state).toBe("linked");
    expect(plan.attachmentId).toBe("att_img");
    expect(plan.displayName).toBe("示意图.png");
    expect(plan.sizeBytes).toBe(2048);
    expect(plan.imagePreviewable).toBe(true);
    expect(plan.action).toBe("view");
    expect(plan.unavailableReason).toBe("");
    expect(plan.statusText).toContain("示意图.png");
    expect(plan.actionLabel).toBe("查看「示意图.png」");
  });

  it("文件（非图片）：可查看但**不**生成缩略图，原因说得出来", () => {
    const plan = attachmentPreviewPlan("file", { file: FILE_META }, { desktop: false });
    expect(plan.state).toBe("linked");
    expect(plan.imagePreviewable).toBe(false);
    expect(plan.action).toBe("view");
    expect(plan.unavailableReason).toContain("缩略图只用于图片");
  });

  it("元信息不完整时也不编：只有 id、没有名字，就用「未命名附件」", () => {
    const plan = attachmentPreviewPlan("image", { image: { attachmentId: "att_x" } }, { desktop: false });
    expect(plan.state).toBe("linked");
    expect(plan.displayName).toBe("未命名附件");
    expect(plan.sizeBytes).toBeNull();
    expect(plan.statusText).toContain("未命名附件");
  });
});

describe("分支二：无关联的卡片（不许假装能打开）", () => {
  it("没有 attachmentId：没有动作、没有缩略图，理由是可读的一句话", () => {
    const plan = attachmentPreviewPlan("image", { image: { name: "看起来像图.png" } }, { desktop: true });
    expect(plan.state).toBe("unlinked");
    expect(plan.attachmentId).toBe("");
    expect(plan.action).toBeNull();
    expect(plan.imagePreviewable).toBe(false);
    expect(plan.openNote).toBe("");
    expect(plan.unavailableReason).toContain("没有关联已保存的附件");
    // 有文件名但没 id：必须说清「名字不是正文」
    expect(plan.statusText).toContain("看起来像图.png");
    expect(plan.statusText).toContain("读不到它的正文");
    expect(plan.actionLabel).toBe("没有关联已保存的附件，打不开");
  });

  it("完全空的 meta：同样是 unlinked，文案不带任何文件名", () => {
    const plan = attachmentPreviewPlan("file", {}, { desktop: false });
    expect(plan.state).toBe("unlinked");
    expect(plan.statusText).toBe("还没有关联已保存的附件：QIO 读不到它的正文，也打不开它");
  });

  it("非材料卡片（text / code / url / reply）不产生任何附件状态", () => {
    for (const kind of ["text", "code", "url", "reply"]) {
      const availability = attachmentCardState({ file: FILE_META }, kind);
      expect(availability.state, kind).toBe("unlinked");
      expect(availability.attachmentId, kind).toBe("");
    }
  });
});

describe("分支三：可执行 / 脚本类不自动运行", () => {
  it("桌面端：入口是「在文件夹中显示」，理由里写明不自动运行", () => {
    const plan = attachmentPreviewPlan("file", { file: { name: "setup.exe", attachmentId: "att_exe" } }, { desktop: true });
    expect(plan.state).toBe("linked");
    // meta 里没有本地路径 → 不能真的 reveal，但入口仍然只能是「在文件夹中显示」这一路
    expect(plan.action).toBe("open_in_folder");
    expect(plan.actionLabel).toBe("在文件夹中显示「setup.exe」");
    expect(plan.unavailableReason).toContain("可执行 / 脚本类附件不渲染预览");
    expect(plan.openNote).toContain("不自动运行");
    expect(plan.openNote).toContain("重新关联");
  });

  it("脚本类（.ps1）在浏览器里也不假装能运行：说清只是下载", () => {
    const plan = attachmentPreviewPlan("file", { file: { name: "deploy.ps1", attachmentId: "att_ps1" } }, { desktop: false });
    // .ps1 不在可安全内联的名单里：浏览器这条路是「下载」，而且写明不自动运行
    expect(plan.action).toBe("download");
    expect(plan.actionLabel).toBe("下载「deploy.ps1」（不自动运行）");
    expect(plan.openNote).toContain("不自动运行");
    // 缩略图理由说的是「渲染前要先执行」这一层（可执行类永远没有缩略图）
    expect(plan.unavailableReason).toContain("可执行 / 脚本类附件不渲染预览");
    expect(plan.unavailableReason).toContain("风险不可控");
  });
});

describe("分支四：浏览器回退与打不开的情形", () => {
  it("浏览器 + 有副本 → view（带认证 fetch 后的 blob 新标签页）", () => {
    const plan = attachmentPreviewPlan("file", { file: FILE_META }, { desktop: false });
    expect(plan.action).toBe("view");
    expect(plan.openNote).toContain("带认证 fetch");
  });

  it("桌面端但 meta 里没有本地路径：不猜路径、不给注定报错的入口", () => {
    const plan = attachmentPreviewPlan("file", { file: FILE_META }, { desktop: true });
    expect(plan.action).toBe("view");
    expect(plan.openNote).toContain("卡片上没有本地路径");
  });

  it("卡片 meta → 最小 AttachmentRef：只映射真实有的事实，不伪造本地路径", () => {
    const ref = attachmentRefForPreview({ attachmentId: "att_ref", name: "报告.pdf", size: 10 });
    expect(ref).toEqual({
      id: "att_ref",
      name: "报告.pdf",
      sizeBytes: 10,
      kind: "copy",
      display: "已保存副本",
      state: "ready",
      error: null,
    });
    // 关键：没有 storedPath / sourcePath —— 桌面端不许凭 meta 猜一个本地路径去 open
    expect(ref.storedPath ?? "").toBe("");
    expect(ref.sourcePath ?? "").toBe("");
  });

  it("不能内联查看的类型（html / svg / xml）不内联，也不当图片渲染", () => {
    expect(isPreviewableImageName("页.html")).toBe(false);
    expect(isPreviewableImageName("图.svg")).toBe(false);
    expect(isPreviewableImageName("图.xml")).toBe(false);
    expect(isPreviewableImageName("照片.JPG")).toBe(true);
    const plan = attachmentPreviewPlan("image", { image: { name: "图.svg", attachmentId: "att_svg" } }, { desktop: false });
    expect(plan.imagePreviewable).toBe(false);
    // svg 是图片类但不能安全渲染：理由说的是「可能带脚本」，这比「只用于图片」更准确
    expect(plan.unavailableReason).toContain("不能安全渲染缩略图");
  });
});

describe("可读文案（a11y 标签与诚实说明）", () => {
  it("标签里带对象名，可执行类带「不自动运行」；无关联时直接说打不开", () => {
    const linked = attachmentCardState({ image: IMAGE_META }, "image", { executable: () => false, viewable: () => true });
    expect(attachmentPreviewActionLabel(linked, false)).toBe("查看「示意图.png」");
    const exe = attachmentCardState({ file: { name: "a.exe", attachmentId: "id" } }, "file", { executable: () => true });
    expect(attachmentPreviewActionLabel(exe, false)).toContain("不自动运行");
    const missing = attachmentCardState({ image: { name: "x.png" } }, "image");
    expect(attachmentPreviewActionLabel(missing, true)).toContain("打不开");
  });

  it("无关联说明区分「有文件名」与「什么都没有」", () => {
    expect(attachmentUnlinkedText({ name: "x.png" })).toContain("记着文件名");
    expect(attachmentUnlinkedText({ name: "" })).toContain("还没有关联已保存的附件");
  });

  it("预览不可用的原因永远是完整句子（不留空白）", () => {
    const linked = attachmentCardState({ file: FILE_META }, "file");
    expect(attachmentPreviewUnavailableReason(linked).length).toBeGreaterThan(6);
    expect(attachmentPreviewUnavailableReason(linked)).not.toMatch(/undefined|null/);
  });
});

describe("blob 生命周期：建一次、按 id 缓存、切换 / 卸载释放", () => {
  const ref = (id: string): AttachmentRef => ({
    id,
    name: "x.png",
    sizeBytes: 10,
    kind: "copy",
    display: "已保存副本",
    state: "ready",
  });

  it("同一个附件重复 load 只取一次字节、只建一个 URL", async () => {
    const fetcher = vi.fn(async () => blob("a"));
    const host = createPreviewBlobHost(fetcher);
    const first = await host.load(ref("a1"));
    const second = await host.load(ref("a1"));
    expect(first?.url).toBe(second?.url);
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(created).toHaveLength(1);
  });

  it("并发 load 同一个附件只发一次请求", async () => {
    const fetcher = vi.fn(async () => blob("a"));
    const host = createPreviewBlobHost(fetcher);
    const [a, b] = await Promise.all([host.load(ref("a1")), host.load(ref("a1"))]);
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(a?.url).toBe(b?.url);
  });

  it("换成另一个附件：先释放上一个 URL（同一时刻只持有一个）", async () => {
    const fetcher = vi.fn(async () => blob("a"));
    const host = createPreviewBlobHost(fetcher);
    await host.load(ref("a1"));
    await host.load(ref("a2"));
    expect(created).toHaveLength(2);
    expect(revoked).toEqual([created[0]]);
    host.revoke();
    expect(revoked).toEqual([created[0], created[1]]);
  });

  it("取字节失败：如实报错、不建 URL，重复 revoke 也安全", async () => {
    const fetcher = vi.fn(async () => {
      throw new Error("附件副本不在原位");
    });
    const host = createPreviewBlobHost(fetcher);
    const result = await host.load(ref("a1"));
    expect(result).toBeNull();
    expect(host.failure()).toContain("不在原位");
    expect(created).toHaveLength(0);
    host.revoke();
    host.revoke();
    expect(revoked).toEqual([]);
  });

  it("状态机：就绪后给出 URL；release 释放并清掉 <img src> 的引用", async () => {
    const applied: string[] = [];
    let cleared = 0;
    const state = createPreviewImageState({
      fetchContent: async () => blob("bytes"),
      onUpdate: () => applied.push("update"),
      clearUrl: () => {
        cleared += 1;
      },
    });
    state.start(ref("a1"), { sizeBytes: 10, sizeLimitBytes: IMAGE_PREVIEW_MAX_BYTES });
    await settled();
    expect(state.url()).toBe(created[0]);
    expect(applied.length).toBeGreaterThan(0);
    expect(state.error()).toBeNull();
    const before = applied.length;
    cleared = 0; // start 时也清过一次（防止旧 src 继续引用），这里只数 release 这一次
    state.release();
    expect(state.url()).toBeNull();
    // release 通知一次（组件据此刷新），并且刚好一次
    expect(applied.length).toBe(before + 1);
    expect(cleared).toBe(1);
    expect(revoked).toEqual([created[0]]);
  });

  it("超限（meta 里已知 size）：**不发起**请求，直接说原因", async () => {
    const fetcher = vi.fn(async () => blob("bytes"));
    const state = createPreviewImageState({ fetchContent: fetcher });
    state.start(ref("a1"), { sizeBytes: IMAGE_PREVIEW_MAX_BYTES + 1, sizeLimitBytes: IMAGE_PREVIEW_MAX_BYTES });
    expect(fetcher).not.toHaveBeenCalled();
    expect(state.loading()).toBe(false);
    expect(state.url()).toBeNull();
    expect(state.error()).toContain("超过缩略图上限");
  });

  it("meta 里 size 缺失但真实字节超限：立刻释放已读入的字节并说清原因", async () => {
    const big = blob("x".repeat(64));
    const fetcher = vi.fn(async () => big);
    const state = createPreviewImageState({ fetchContent: fetcher });
    state.start(ref("a1"), { sizeBytes: null, sizeLimitBytes: 16 });
    await settled();
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(state.url()).toBeNull();
    expect(state.error()).toContain("实际");
    // 读进来的字节立刻被释放（不留 blob URL 占内存）
    expect(revoked).toEqual([created[0]]);
  });

  it("取字节失败：error 是真实原因，不是空白", async () => {
    const state = createPreviewImageState({
      fetchContent: async () => {
        throw new Error("401：本机 API 拒绝了这次请求");
      },
    });
    state.start(ref("a1"), { sizeBytes: 1, sizeLimitBytes: IMAGE_PREVIEW_MAX_BYTES });
    await settled();
    expect(state.url()).toBeNull();
    expect(state.error()).toContain("401");
  });
});