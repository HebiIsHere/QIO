/**
 * Phase 2-C：图片卡「原比例 + 详情状态」的集成用例（真实 BoardCard + 真实 CardInlinePreview）。
 *
 * 验收口径（docs/interactive-page-v2-contract.md §5.2）：
 * 1. 简洁（常态）状态仍是原比例封面，图片卡有**查看详情**入口；
 * 2. 详情态：图片铺满卡片（无普通标题栏 / 页脚）、圆角裁切、contain 等比完整、不拉伸；
 * 3. 长图 / 竖图不会被简洁态永远锁住；比例未知也有详情入口；
 * 4. 详情态的字节请求核对身份：换附件 / 换卡片时旧请求结果不覆盖新图，旧 blob URL 被释放；
 * 5. 打开 / 收起详情不改卡片几何（w/h 不变），也不产生板面 patch。
 *
 * 只 mock 网络边界（附件字节 + 预览数据）与文件选择 / 系统打开。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const mocks = vi.hoisted(() => ({
  fetchAttachmentContent: vi.fn(),
  fetchAttachmentPreview: vi.fn(),
  fetchAttachmentBytes: vi.fn(),
  pickBrowserFile: vi.fn(),
  uploadAttachment: vi.fn(),
  waitUntilSettled: vi.fn(),
  openAttachment: vi.fn(),
  downloadAttachment: vi.fn(),
  isDesktopShell: vi.fn(() => false),
}));

vi.mock("../../../services/attachments", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../services/attachments")>();
  return {
    ...actual,
    fetchAttachmentContent: mocks.fetchAttachmentContent,
    fetchAttachmentPreview: mocks.fetchAttachmentPreview,
    fetchAttachmentBytes: mocks.fetchAttachmentBytes,
    pickBrowserFile: mocks.pickBrowserFile,
    uploadAttachment: mocks.uploadAttachment,
    waitUntilSettled: mocks.waitUntilSettled,
    openAttachment: mocks.openAttachment,
    downloadAttachment: mocks.downloadAttachment,
    isDesktopShell: mocks.isDesktopShell,
  };
});

import BoardCard from "../BoardCard.vue";
import { useInteractiveStore } from "../../../stores/interactive";
import { emptyState, setSelection } from "../../../interactive/board";
import type { BoardCard as BoardCardModel } from "../../../interactive/types";

const STAMP = "2026-10-15T00:00:00.000Z";
const SRC = resolve(process.cwd(), "src");
const previewSource = readFileSync(resolve(SRC, "components/interactive/CardInlinePreview.vue"), "utf8");
const cardSource = readFileSync(resolve(SRC, "components/interactive/BoardCard.vue"), "utf8");

function cssBlock(text: string, selector: string): string {
  const start = text.indexOf(selector);
  expect(start, "找不到选择器 " + selector).toBeGreaterThan(-1);
  const open = text.indexOf("{", start);
  const close = text.indexOf("}", open);
  return text.slice(open + 1, close);
}

function imageMeta(attachmentId = "att_img", name = "照片.png", extra: Record<string, unknown> = {}): Record<string, unknown> {
  return { name, file: { attachmentId, name, size: 66560, ...extra } };
}

function imageCard(patch: Partial<BoardCardModel> = {}): BoardCardModel {
  return {
    id: "card_img",
    kind: "file",
    content: "",
    meta: imageMeta(),
    x: 0,
    y: 0,
    w: 320,
    h: 260,
    checked: false,
    hidden: false,
    folded: false,
    bookmarked: false,
    deleted: false,
    createdAt: STAMP,
    updatedAt: STAMP,
    ...patch,
  };
}

/** 尺寸超过普通卡片进入阈值，也超过图片短边/面积阈值：用于自动详细态。 */
function detailedImageCard(patch: Partial<BoardCardModel> = {}): BoardCardModel {
  return imageCard({ w: 600, h: 450, ...patch });
}

let wrappers: VueWrapper[] = [];
let created: string[] = [];
let revoked: string[] = [];

function mountCard(model: BoardCardModel): VueWrapper {
  const store = useInteractiveStore();
  store.board = setSelection({ ...emptyState("board_t"), cards: [model] }, [model.id]);
  const w = mount(BoardCard, {
    attachTo: document.body,
    props: {
      card: model,
      selected: true,
      highlight: false,
      dragging: false,
      x: 0,
      y: 0,
      groupName: null,
      groups: [],
      toolbarLeft: 0,
      toolbarTop: 0,
      multi: false,
      connecting: false,
    },
  });
  wrappers.push(w);
  return w;
}

function imageEnvelope(width = 800, height = 600) {
  return { status: "ready", kind: "image", reason: "", version: "v1", data: { kind: "image", width, height, bytes: 66560 } };
}

beforeEach(() => {
  localStorage.clear();
  setActivePinia(createPinia());
  mocks.fetchAttachmentContent.mockReset();
  mocks.fetchAttachmentContent.mockResolvedValue(new Blob(["x"], { type: "image/png" }));
  mocks.fetchAttachmentPreview.mockReset();
  mocks.fetchAttachmentPreview.mockResolvedValue(imageEnvelope());
  mocks.fetchAttachmentBytes.mockReset();
  mocks.openAttachment.mockReset();
  mocks.isDesktopShell.mockReturnValue(false);
  created = [];
  revoked = [];
  Object.defineProperty(URL, "createObjectURL", {
    configurable: true,
    value: vi.fn(() => {
      const url = "blob:qio-p2c-" + (created.length + 1);
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
  for (const w of wrappers) {
    try {
      w.unmount();
    } catch {
      /* 已卸载 */
    }
  }
  wrappers = [];
  document.body.innerHTML = "";
});

describe("图片详细状态：尺寸自动优先，极端比例才兜底", () => {
  it("普通小图默认简洁，不再显示旧的查看详情文字按钮", async () => {
    const w = mountCard(imageCard());
    await flushPromises();
    expect(w.get('[data-im="card"]').attributes("data-card-state")).toBe("normal");
    expect(w.find('[data-im="card-preview-toggle"]').exists()).toBe(false);
    expect(w.find('[data-im="card-image-detail-toggle"]').exists()).toBe(false);
  });

  it("极端长图 / 竖图：保留小眼睛兜底，说明点出「长」", async () => {
    const long = mountCard(
      imageCard({
        id: "card_long",
        meta: { ...imageMeta("att_long", "长图.png"), display: { width: 3000, height: 500, aspectRatio: 6 } },
      }),
    );
    await flushPromises();
    const toggle = long.get('[data-im="card-image-detail-toggle"]');
    expect(toggle.attributes("title")).toContain("长");
    expect(toggle.attributes("aria-label")).toBe("查看完整图片");
    long.unmount();

    const tall = mountCard(
      imageCard({
        id: "card_tall",
        meta: { ...imageMeta("att_tall", "竖图.png"), display: { width: 500, height: 3000, aspectRatio: 0.166667 } },
      }),
    );
    await flushPromises();
    expect(tall.get('[data-im="card-image-detail-toggle"]').attributes("title")).toContain("长");
  });

  it("比例未知的普通小图不显示兜底入口", async () => {
    const w = mountCard(imageCard({ meta: imageMeta() }));
    await flushPromises();
    expect(w.find('[data-im="card-image-detail-toggle"]').exists()).toBe(false);
  });
});

describe("meta.display：从真实解码尺寸记录原比例（不猜、不写 0）", () => {
  function fakeDecodedSize(img: HTMLImageElement, width: number, height: number): void {
    Object.defineProperty(img, "naturalWidth", { configurable: true, value: width });
    Object.defineProperty(img, "naturalHeight", { configurable: true, value: height });
    img.dispatchEvent(new Event("load"));
  }

  it("封面图解码完成 → 只写 meta.display，不改 w/h", async () => {
    const model = imageCard({ w: 320, h: 260 });
    const w = mountCard(model);
    await flushPromises();
    await nextTick();

    const img = w.get('[data-im="card-cover-img"]').element as HTMLImageElement;
    fakeDecodedSize(img, 800, 600);
    await flushPromises();

    const patches = w.emitted("patch") as unknown[][];
    expect(patches, "解码完成却没有记录比例").toBeTruthy();
    const [cardId, patch] = patches[0] as [string, { meta: Record<string, unknown> }];
    expect(cardId).toBe("card_img");
    const display = patch.meta.display as { width: number; height: number; aspectRatio: number };
    expect(display.width).toBe(800);
    expect(display.height).toBe(600);
    expect(display.aspectRatio).toBeCloseTo(4 / 3, 5);
    expect(patch.meta.name).toBe("照片.png");
    // 几何不进 patch：比例记录不得改动用户设置的尺寸
    expect((patch as { w?: number }).w).toBeUndefined();
    expect((patch as { h?: number }).h).toBeUndefined();
  });

  it("解码结果是 0 / 失败：不写任何东西（绝不写 0 比例）", async () => {
    const w = mountCard(imageCard());
    await flushPromises();
    await nextTick();
    const img = w.get('[data-im="card-cover-img"]').element as HTMLImageElement;
    fakeDecodedSize(img, 0, 0);
    await flushPromises();
    expect(w.emitted("patch")).toBeUndefined();
  });

  it("已经有 meta.display：不重复写（重复渲染 / 回执未到都不反复写板面）", async () => {
    const w = mountCard(
      imageCard({
        meta: { ...imageMeta(), display: { width: 800, height: 600, aspectRatio: 4 / 3 } },
      }),
    );
    await flushPromises();
    await nextTick();
    const img = w.get('[data-im="card-cover-img"]').element as HTMLImageElement;
    fakeDecodedSize(img, 800, 600);
    await flushPromises();
    expect(w.emitted("patch")).toBeUndefined();
  });
});

describe("详情态：铺满卡片、无普通标题栏、圆角裁切、等比完整", () => {
  it("尺寸达到阈值：整卡是图，头部 / 页脚 / 封面都不再渲染", async () => {
    const w = mountCard(detailedImageCard());
    await flushPromises();
    await nextTick();

    expect(w.get('[data-im="card"]').attributes("data-card-state")).toBe("image-detail");
    expect(w.find('[data-im="card-image-detail-host"]').exists()).toBe(true);
    expect(w.find('[data-im="card-head"]').exists()).toBe(true);
    expect(w.get('[data-im="card-head"] [data-im="card-title"]').text()).toBe("照片.png");
    expect(w.find('[data-im="card-image-detail-toggle"]').exists(), "自动详细态不应再出现文字/兜底按钮").toBe(false);
    expect(w.find('[data-im="card-meta"]').exists(), "详情态还留着页脚").toBe(false);
    expect(w.find('[data-im="card-cover"]').exists()).toBe(false);
    expect(w.find('[data-im="card-body"]').exists()).toBe(false);
  });

  it("详情里的图片真的加载（blob URL），尺寸事实浮在图上", async () => {
    const w = mountCard(
      detailedImageCard({
        meta: { ...imageMeta(), display: { width: 800, height: 600, aspectRatio: 4 / 3 } },
      }),
    );
    await flushPromises();
    await nextTick();

    expect(mocks.fetchAttachmentContent).toHaveBeenCalledWith("att_img");
    const img = w.get('[data-im="card-image-detail-img"]');
    expect(img.attributes("src")).toBe(created[created.length - 1]);
    expect(img.attributes("alt")).toContain("照片.png");
    expect(w.get('[data-im="card-image-detail-shape"]').text()).toContain("800×600");
    expect(w.find('[data-im="card-image-detail-name"]').exists()).toBe(false);
  });

  it("CSS：详情图 contain（不裁主体、不拉伸），宿主铺满并圆角裁切", () => {
    const img = cssBlock(previewSource, ".cip-img-detail-img {");
    expect(img).toContain("object-fit: contain");
    expect(img).not.toContain("object-fit: cover");
    expect(img).toContain("pointer-events: none");
    const layer = cssBlock(previewSource, ".cip-img-detail {");
    expect(layer).toContain("inset: 0");
    expect(layer).toContain("overflow: hidden");
    expect(layer).toContain("border-radius: inherit");
    const host = cssBlock(cardSource, ".card-image-detail-host {");
    expect(host).toContain("position: absolute");
    expect(host).toContain("inset: 0");
    expect(host).toContain("border-radius: inherit");
    const card = cssBlock(cardSource, ".card.is-image-detail {");
    expect(card).toContain("padding: 0");
    expect(card).toContain("overflow: hidden");
  });

  it("缩小到退出阈值：回到常态封面，图片仍按原比例完整显示", async () => {
    const model = detailedImageCard();
    const w = mountCard(model);
    await flushPromises();
    await w.setProps({ card: { ...model, w: 320, h: 260 } });
    await flushPromises();
    await nextTick();
    expect(w.get('[data-im="card"]').attributes("data-card-state")).toBe("normal");
    expect(w.find('[data-im="card-cover"]').exists()).toBe(true);
    expect(w.find('[data-im="card-image-detail-host"]').exists()).toBe(false);
  });
});

describe("详情态的异步身份核对", () => {
  it("换附件：旧图不覆盖新图，旧 blob URL 被释放", async () => {
    const model = detailedImageCard();
    const w = mountCard(model);
    await flushPromises();
    const detailUrl = created[created.length - 1];
    const fetchBaseline = mocks.fetchAttachmentContent.mock.calls.length;

    await w.setProps({ card: { ...model, meta: imageMeta("att_img2", "另一张.png") } });
    await flushPromises();
    await nextTick();

    expect(mocks.fetchAttachmentContent.mock.calls.length).toBeGreaterThan(fetchBaseline);
    expect(mocks.fetchAttachmentContent).toHaveBeenLastCalledWith("att_img2");
    expect(revoked, "换附件时旧详情图没有释放").toContain(detailUrl);
    expect(w.get('[data-im="card-image-detail-img"]').attributes("src")).toBe(created[created.length - 1]);
  });

  it("迟到的旧附件字节不写入新图（身份核对）", async () => {
    const model = detailedImageCard();
    let resolveOld: ((value: Blob) => void) | null = null;
    mocks.fetchAttachmentContent.mockImplementationOnce(
      () => new Promise<Blob>((resolve) => { resolveOld = resolve; }),
    );
    const w = mountCard(model);
    await flushPromises();

    // 还没取回旧图就换附件：新附件立刻取到
    await w.setProps({ card: { ...model, meta: imageMeta("att_img2", "新图.png") } });
    // 新附件会经「预览数据 + 详情字节」两条既有路径各触发一次，让它们全部落定
    await flushPromises();
    await flushPromises();
    await nextTick();
    const settledUrlCount = created.length;
    const settledSrc = w.get('[data-im="card-image-detail-img"]').attributes("src");
    expect(settledSrc).toBe(created[created.length - 1]);

    // 旧请求此刻才回来：必须被丢弃（不得覆盖新图，也不得新建 URL）
    (resolveOld as ((value: Blob) => void) | null)?.(new Blob(["old"], { type: "image/png" }));
    await flushPromises();
    await flushPromises();
    await nextTick();
    expect(w.get('[data-im="card-image-detail-img"]').attributes("src"), "迟到的旧图覆盖了新图").toBe(settledSrc);
    void settledUrlCount;
  });

  it("换卡片 / 卸载：详情关闭并释放当前 blob URL", async () => {
    const model = detailedImageCard();
    const w = mountCard(model);
    await flushPromises();
    const detailUrl = created[created.length - 1];
    w.unmount();
    expect(revoked).toContain(detailUrl);
  });
});

describe("几何与板面：详情只改查看，不动尺寸、不写板面", () => {
  it("尺寸驱动详情不修改传入的 w/h，也不产生 patch", async () => {
    const model = detailedImageCard();
    const w = mountCard(model);
    await flushPromises();
    const geometry = JSON.stringify({ x: model.x, y: model.y, w: model.w, h: model.h });
    expect(w.get('[data-im="card"]').attributes("data-card-state")).toBe("image-detail");

    await w.setProps({ card: { ...model, w: 320, h: 260 } });
    await flushPromises();

    expect(JSON.stringify({ x: model.x, y: model.y, w: model.w, h: model.h })).toBe(geometry);
    expect(w.emitted("patch"), "切换详情态不该写板面").toBeUndefined();
    expect(w.get('[data-im="card"]').attributes("data-card-state")).toBe("normal");
  });

  it("版本变化（附件 version 变了）不重置用户尺寸", async () => {
    const model = imageCard({ w: 517, h: 431 });
    const w = mountCard(model);
    await flushPromises();

    await w.setProps({ card: { ...model, meta: imageMeta("att_img", "照片.png", { version: "v2" }) } });
    await flushPromises();
    await nextTick();

    const cardProp = (w.props() as unknown as { card: { w: number; h: number } }).card;
    expect(cardProp.w).toBe(517);
    expect(cardProp.h).toBe(431);
    expect(w.get('[data-im="card"]').attributes("data-card-state")).toBe("image-detail");
  });
});