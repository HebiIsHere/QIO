/**
 * [P2 板面附件预览] CardAttachmentPreview 组件级用例。
 *
 * 触发：板面上的图片 / 文件卡片此前只有一行「还没有关联已保存的附件」，没有任何预览入口。
 * 这个文件测的是**组件这一层**真的做了什么：
 *   有关联   → 带认证 fetch 的 blob 缩略图（img 真的拿到 blob URL）；
 *   无关联   → 诚实文案，一个请求都不发（不显示假预览）；
 *   可执行类 → 桌面只「在文件夹中显示」并写明不自动运行；浏览器说清只是下载；
 *   浏览器回退 → 带认证 fetch + blob 新标签页（交给浏览器的那句话不谎报「已打开」）；
 *   释放     → 组件卸载时 revoke 缩略图 blob URL 并清掉 img.src；
 *   失败 / 超限 → 显示原因，不留空白。
 *
 * 模拟边界：真实组件 + 真实 attachmentPreview 模块；只替换「网络」与「Tauri 命令」两个边界。
 * 附件服务用 importOriginal 保留全部真实实现，仅替换 fetchAttachmentContent（假字节）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";

const mocks = vi.hoisted(() => ({
  invoke: vi.fn(async () => undefined),
  fetchAttachmentContent: vi.fn(),
}));

vi.mock("@tauri-apps/api/core", () => ({ invoke: mocks.invoke }));

vi.mock("../../../services/attachments", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../services/attachments")>();
  return { ...actual, fetchAttachmentContent: mocks.fetchAttachmentContent };
});

import CardAttachmentPreview from "../CardAttachmentPreview.vue";
import type { AttachmentOpenResult, AttachmentRef } from "../../../services/attachments";

/** 组件 props 的形状（open 是组件预留的测试缝，签名与真实的 openAttachment 一致）。 */
type PreviewProps = {
  kind: string;
  meta: Record<string, unknown> | null;
  cardId?: string;
  open?: (ref: AttachmentRef, options: { desktop: boolean }) => Promise<AttachmentOpenResult>;
};

function imageCard(extra: Record<string, unknown> = {}): PreviewProps {
  return {
    kind: "image",
    meta: { name: "示意图.png", image: { name: "示意图.png", attachmentId: "att_img", size: 2048, ...extra } },
    cardId: "c1",
  };
}

function fileCard(name: string, id = "att_file"): PreviewProps {
  return { kind: "file", meta: { name, file: { name, attachmentId: id, size: 10 } }, cardId: "c1" };
}

const SVG_BLOB = "PNGDATA";

let created: string[];
let revoked: string[];

function useDesktopShell(on: boolean): void {
  const host = window as unknown as Record<string, unknown>;
  if (on) host.__TAURI_INTERNALS__ = {};
  else delete host.__TAURI_INTERNALS__;
}

beforeEach(() => {
  mocks.invoke.mockClear();
  mocks.fetchAttachmentContent.mockReset();
  mocks.fetchAttachmentContent.mockImplementation(async () => new Blob([SVG_BLOB], { type: "image/png" }));
  created = [];
  revoked = [];
  Object.defineProperty(URL, "createObjectURL", {
    configurable: true,
    value: vi.fn(() => {
      const url = "blob:qio-card-" + (created.length + 1);
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
  useDesktopShell(false);
});

afterEach(() => {
  useDesktopShell(false);
  vi.restoreAllMocks();
});

describe("有关联：图片缩略图走带认证 fetch + blob URL", () => {
  it("渲染缩略图：img 的 src 是 blob URL，加载完不留「正在读取」的占位", async () => {
    const wrapper = mount(CardAttachmentPreview, { props: imageCard() });
    await flushPromises();

    expect(mocks.fetchAttachmentContent).toHaveBeenCalledWith("att_img");
    const img = wrapper.find('[data-im="card-attachment-thumb"]');
    expect(img.exists()).toBe(true);
    expect(img.attributes("src")).toBe("blob:qio-card-1");
    expect(img.attributes("alt")).toContain("示意图.png");
    expect(wrapper.get('[data-im="card-attachment-preview-state"]').text()).not.toContain("正在读取");
    expect(wrapper.get('[data-im="card-attachment-open"]').attributes("aria-label")).toContain("示意图.png");
    wrapper.unmount();
  });

  it("文件卡片（非图片）不请求字节，只给可读入口", async () => {
    const wrapper = mount(CardAttachmentPreview, { props: fileCard("报告.pdf") });
    await flushPromises();

    expect(mocks.fetchAttachmentContent).not.toHaveBeenCalled();
    expect(wrapper.find('[data-im="card-attachment-thumb"]').exists()).toBe(false);
    expect(wrapper.get('[data-im="card-attachment-preview-state"]').text()).toContain("缩略图只用于图片");
    expect(wrapper.find('[data-im="card-attachment-thumb"]').exists()).toBe(false);
    // 但查看入口必须在（有关联就给得出来的入口，并说明点一下会发生什么）
    expect(wrapper.get('[data-im="card-attachment-open"]').text()).toBe("查看");
    expect(wrapper.get('[data-im="card-attachment-open-note"]').text()).toContain("带认证 fetch");
    wrapper.unmount();
  });

  it("读字节失败：如实显示原因，不显示空白缩略图", async () => {
    mocks.fetchAttachmentContent.mockRejectedValue(new Error("401：本机 API 拒绝了这次请求"));
    const wrapper = mount(CardAttachmentPreview, { props: imageCard() });
    await flushPromises();

    expect(wrapper.find('[data-im="card-attachment-thumb"]').exists()).toBe(false);
    const note = wrapper.get('[data-im="card-attachment-preview-state"]').text();
    expect(note).toContain("401");
    expect(note.length).toBeGreaterThan(4);
    wrapper.unmount();
  });

  it("meta 中的 size 超过缩略图上限：不请求、说清原因（不假装加载中）", async () => {
    const props: PreviewProps = {
      kind: "image",
      meta: { image: { name: "巨图.png", attachmentId: "att_big", size: 999_000_000 } },
      cardId: "c1",
    };
    const wrapper = mount(CardAttachmentPreview, { props });
    await flushPromises();

    expect(mocks.fetchAttachmentContent).not.toHaveBeenCalled();
    const note = wrapper.get('[data-im="card-attachment-preview-state"]').text();
    expect(note).toContain("超过缩略图上限");
    wrapper.unmount();
  });
});

describe("无关联：诚实文案，一个请求都不发", () => {
  it("没有 attachmentId：没有缩略图与按钮，文案说清读不到正文", async () => {
    const wrapper = mount(CardAttachmentPreview, {
      // 名字写在 meta.image 里（卡片真实形状），但没有 attachmentId
      props: { kind: "image", meta: { image: { name: "只有名字.png" } }, cardId: "c1" },
    });
    await flushPromises();

    expect(mocks.fetchAttachmentContent).not.toHaveBeenCalled();
    const root = wrapper.get('[data-im="card-attachment-preview"]');
    expect(root.attributes("data-state")).toBe("unlinked");
    expect(root.attributes("data-open-action")).toBe("none");
    expect(wrapper.find('[data-im="card-attachment-thumb"]').exists()).toBe(false);
    // 诚实：没有关联时**不给按钮**（不给假入口），只留一句可读的说明
    expect(wrapper.find('[data-im="card-attachment-open"]').exists()).toBe(false);
    expect(wrapper.find('[data-im="card-attachment-open-status"]').exists()).toBe(false);
    const line = wrapper.get('[data-im="card-attachment"]').text();
    expect(line).toContain("读不到它的正文");
    expect(line).toContain("只有名字.png");
    wrapper.unmount();
  });

  it("非材料卡片（text）根本不渲染这个区域", () => {
    const wrapper = mount(CardAttachmentPreview, { props: { kind: "text", meta: {}, cardId: "c1" } });
    expect(wrapper.find('[data-im="card-attachment-preview"]').exists()).toBe(false);
    wrapper.unmount();
  });
});

describe("可执行 / 脚本类：绝不自动运行", () => {
  it("桌面端：按钮是在文件夹中显示，并说明不自动运行", async () => {
    useDesktopShell(true);
    const wrapper = mount(CardAttachmentPreview, { props: fileCard("setup.exe") });
    await flushPromises();

    const root = wrapper.get('[data-im="card-attachment-preview"]');
    // meta 里没有本地路径：入口只能是「在文件夹中显示」这一路，绝不出现「打开」
    expect(root.attributes("data-open-action")).toBe("open_in_folder");
    const button = wrapper.get('[data-im="card-attachment-open"]');
    expect(button.text()).toBe("在文件夹中显示");
    expect(button.attributes("aria-label")).toContain("setup.exe");
    expect(button.attributes("aria-label")).toContain("不自动运行");
    expect(wrapper.get('[data-im="card-attachment-open-note"]').text()).toContain("不自动运行");
    wrapper.unmount();
  });

  it("点「在文件夹中显示」：没有本地路径时不冒险执行，说明原因且不猜路径", async () => {
    useDesktopShell(true);
    const wrapper = mount(CardAttachmentPreview, { props: fileCard("setup.exe") });
    await flushPromises();

    mocks.invoke.mockClear();
    await wrapper.get('[data-im="card-attachment-open"]').trigger("click");
    await flushPromises();

    // 不给原生壳传一个编造的路径：这次点击只把真实原因说出来
    expect(mocks.invoke).not.toHaveBeenCalled();
    const status = wrapper.get('[data-im="card-attachment-open-status"]').text();
    expect(status).toContain("不自动运行");
    expect(status).toContain("重新关联");
    wrapper.unmount();
  });

  it("浏览器端：不假装能运行 —— 按钮是「下载副本」，并写明不自动运行", async () => {
    const injected = vi.fn(async () => ({ action: "download" as const, note: "已开始下载副本" }));
    const props = fileCard("deploy.ps1");
    props.open = injected;
    const wrapper = mount(CardAttachmentPreview, { props });
    await flushPromises();

    const button = wrapper.get('[data-im="card-attachment-open"]');
    expect(button.text()).toBe("下载副本");
    expect(button.attributes("aria-label")).toContain("不自动运行");
    const note = wrapper.get('[data-im="card-attachment-open-note"]').text();
    expect(note).toContain("不自动运行");
    expect(note).toContain("浏览器里只能下载副本");

    await button.trigger("click");
    await flushPromises();
    expect(injected).toHaveBeenCalledTimes(1);
    wrapper.unmount();
  });
});

describe("浏览器回退：带认证 fetch + blob 新标签页", () => {
  it("点「查看」：真的取字节、真的把 blob URL 交给新标签页，且不谎报已打开", async () => {
    const open = vi.spyOn(window, "open").mockReturnValue(null);
    const injected = vi.fn(async () => ({
      action: "view" as const,
      note: "已交给浏览器在新标签页打开；如果没有出现，可能是浏览器拦截了弹窗",
    }));
    const props = fileCard("报告.pdf");
    props.open = injected;
    const wrapper = mount(CardAttachmentPreview, { props });
    await flushPromises();

    await wrapper.get('[data-im="card-attachment-open"]').trigger("click");
    await flushPromises();

    expect(injected).toHaveBeenCalledTimes(1);
    const [refArg, options] = injected.mock.calls[0] as unknown as [Record<string, unknown>, { desktop: boolean }];
    expect(refArg.id).toBe("att_file");
    expect(refArg.name).toBe("报告.pdf");
    expect(options.desktop).toBe(false);
    const status = wrapper.get('[data-im="card-attachment-open-status"]').text();
    expect(status).toContain("交给浏览器");
    expect(status).not.toContain("已打开");
    expect(open).not.toHaveBeenCalled();
    wrapper.unmount();
  });

  it("打开失败（例如引用型没有副本）：错误原因如实显示", async () => {
    const props = fileCard("大文件.bin");
    props.open = async () => {
      throw new Error("引用型附件没有 QIO 副本，浏览器里打不开本地路径");
    };
    const wrapper = mount(CardAttachmentPreview, { props });
    await flushPromises();

    await wrapper.get('[data-im="card-attachment-open"]').trigger("click");
    await flushPromises();

    const status = wrapper.get('[data-im="card-attachment-open-status"]').text();
    expect(status).toContain("打不开");
    expect(status).toContain("没有 QIO 副本");
    wrapper.unmount();
  });
});

describe("blob 释放：卸载不留 URL", () => {
  it("卸载时 revoke 缩略图 blob URL 并清掉 img.src", async () => {
    const wrapper = mount(CardAttachmentPreview, { props: imageCard() });
    await flushPromises();
    const img = wrapper.get('[data-im="card-attachment-thumb"]');
    expect(img.attributes("src")).toBe("blob:qio-card-1");
    expect(revoked).toEqual([]);

    wrapper.unmount();

    expect(revoked).toEqual(["blob:qio-card-1"]);
    expect(img.attributes("src")).toBeUndefined();
  });

  it("同一张卡片重复渲染不会重复取字节 / 重复建 URL", async () => {
    const wrapper = mount(CardAttachmentPreview, { props: imageCard() });
    await flushPromises();
    await wrapper.setProps({ meta: { ...imageCard().meta, extra: 1 } });
    await flushPromises();

    expect(mocks.fetchAttachmentContent).toHaveBeenCalledTimes(1);
    expect(created).toHaveLength(1);
    wrapper.unmount();
  });

  it("换成另一个附件：先释放上一个 URL，再建新的", async () => {
    const wrapper = mount(CardAttachmentPreview, { props: imageCard() });
    await flushPromises();
    expect(created).toEqual(["blob:qio-card-1"]);

    await wrapper.setProps({
      meta: { name: "另一张.png", image: { name: "另一张.png", attachmentId: "att_img2", size: 1024 } },
    });
    await flushPromises();

    expect(mocks.fetchAttachmentContent).toHaveBeenLastCalledWith("att_img2");
    expect(revoked).toEqual(["blob:qio-card-1"]);
    expect(wrapper.get('[data-im="card-attachment-thumb"]').attributes("src")).toBe("blob:qio-card-2");
    wrapper.unmount();
    expect(revoked).toEqual(["blob:qio-card-1", "blob:qio-card-2"]);
  });
});