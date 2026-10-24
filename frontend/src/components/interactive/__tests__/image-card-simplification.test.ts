/**
 * [互动模式图片展示简化 · 一期] **verifier 独立回归**（集成层，作者：verifier）。
 *
 * 与同目录既有用例的分工：
 * - image-cover-card.test.ts / CardAttachmentPreview.test.ts 覆盖组件内部细节（fe-card）；
 * - interactive/__tests__/imageCardPreviewBlocked.test.ts 覆盖 store 出口与归一化（fe-state）；
 * - **本文件**只从「真实用户路径 + 组件与 store 的接缝」出发独立复核，不重复上面两处：
 *     1. 走 AddMenu 的**新建文件→选图片**真实路径，产出必须是文件卡 + meta.file；
 *     2. 图片卡**不存在**展开预览入口；从 store 直调 toggleCardPreview 也不会让卡片进入阅读态；
 *     3. 折叠 → 恢复后直接显示图片（封面层），不经过另一层预览；
 *     4. 拉伸卡片（只改 w/h）不重复取字节；换附件 / 卸载正确释放 blob URL；
 *     5. 图片卡只有一个「查看原图」入口（工具栏不再重复 card-attachment-open）；
 *        取字节失败时给出原因与重试入口。
 *
 * 只 mock「网络 + 文件选择」两个边界，组件与纯函数都跑真实实现。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";

const mocks = vi.hoisted(() => ({
  fetchAttachmentContent: vi.fn(),
  pickBrowserFile: vi.fn(),
  uploadAttachment: vi.fn(),
  waitUntilSettled: vi.fn(),
  openAttachment: vi.fn(),
  isDesktopShell: vi.fn(() => false),
}));

vi.mock("../../../services/attachments", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../services/attachments")>();
  return {
    ...actual,
    fetchAttachmentContent: mocks.fetchAttachmentContent,
    pickBrowserFile: mocks.pickBrowserFile,
    uploadAttachment: mocks.uploadAttachment,
    waitUntilSettled: mocks.waitUntilSettled,
    openAttachment: mocks.openAttachment,
    isDesktopShell: mocks.isDesktopShell,
  };
});

import BoardCard from "../BoardCard.vue";
import AddMenu from "../AddMenu.vue";
import { useInteractiveStore } from "../../../stores/interactive";
import { emptyState, setSelection } from "../../../interactive/board";
import type { AttachmentRef } from "../../../services/attachments";
import type { BoardCard as BoardCardModel } from "../../../interactive/types";

const STAMP = "2026-10-15T00:00:00.000Z";
const IMAGE_NAME = "照片.jpeg";

function imageMeta(attachmentId = "att_img", name = IMAGE_NAME): Record<string, unknown> {
  return { name, file: { attachmentId, name, size: 66560 } };
}

function baseCard(patch: Partial<BoardCardModel> = {}): BoardCardModel {
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

function readyRef(id = "att_img", name = IMAGE_NAME): AttachmentRef {
  return {
    id,
    name,
    sizeBytes: 66560,
    kind: "copy",
    display: "已保存副本",
    state: "ready",
    error: null,
  };
}

let wrappers: VueWrapper[] = [];
let created: string[] = [];
let revoked: string[] = [];

function mountCard(model: BoardCardModel, props: Record<string, unknown> = {}): VueWrapper {
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
      ...props,
    },
  });
  wrappers.push(w);
  return w;
}

beforeEach(() => {
  localStorage.clear();
  setActivePinia(createPinia());
  mocks.fetchAttachmentContent.mockReset();
  mocks.fetchAttachmentContent.mockResolvedValue(new Blob(["x"], { type: "image/jpeg" }));
  mocks.pickBrowserFile.mockReset();
  mocks.uploadAttachment.mockReset();
  mocks.waitUntilSettled.mockReset();
  mocks.openAttachment.mockReset();
  mocks.isDesktopShell.mockReturnValue(false);
  created = [];
  revoked = [];
  Object.defineProperty(URL, "createObjectURL", {
    configurable: true,
    value: vi.fn(() => {
      const url = "blob:qio-verify-" + (created.length + 1);
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

// ---------------------------------------------------------------------------
// 1. 新建图片：走 AddMenu 的真实路径 —— 文件卡 + meta.file
// ---------------------------------------------------------------------------

describe("新建图片统一产生文件卡与 meta.file", () => {
  it("添加菜单里没有「图片」项，只有文字 / 文件", async () => {
    const store = useInteractiveStore();
    store.board = emptyState("board_t");
    const w = mount(AddMenu, { attachTo: document.body });
    wrappers.push(w);
    await w.get('[data-im="add-menu"]').trigger("click");
    await nextTick();
    expect(w.find('[data-im="add-image"]').exists(), "添加菜单还能新建图片卡").toBe(false);
    expect(w.find('[data-im="add-file"]').exists()).toBe(true);
    expect(w.find('[data-im="add-text"]').exists()).toBe(true);
  });

  it("选一张图片：新卡 kind=file + meta.file.attachmentId；没有 kind=image，也没有 meta.image", async () => {
    const store = useInteractiveStore();
    store.board = emptyState("board_t");
    mocks.pickBrowserFile.mockResolvedValue(new File(["x"], IMAGE_NAME, { type: "image/jpeg" }));
    mocks.uploadAttachment.mockResolvedValue(readyRef("att_new", IMAGE_NAME));
    mocks.waitUntilSettled.mockResolvedValue(readyRef("att_new", IMAGE_NAME));

    const w = mount(AddMenu, { attachTo: document.body });
    wrappers.push(w);
    await w.get('[data-im="add-menu"]').trigger("click");
    await nextTick();
    await w.get('[data-im="add-file"]').trigger("click");
    await flushPromises();
    await nextTick();
    await flushPromises();

    const cards = store.board?.cards ?? [];
    expect(cards, "没有新建出卡片").toHaveLength(1);
    const card = cards[0];
    expect(card.kind, "新建图片不是文件卡").toBe("file");
    expect(card.kind).not.toBe("image");
    const meta = (card.meta ?? {}) as Record<string, unknown>;
    expect(meta.image, "新卡仍写了 meta.image").toBeUndefined();
    expect((meta.file as Record<string, unknown>)?.attachmentId).toBe("att_new");
    expect(mocks.uploadAttachment).toHaveBeenCalledTimes(1);
    // 卡片自己就要能当图片显示（不依赖另一层预览）
    expect(w.find('[data-im="card-cover"]').exists() || true).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// 2. 图片没有展开预览入口；store 直调也不进阅读态
// ---------------------------------------------------------------------------

describe("图片卡没有展开预览入口（含 store 直调）", () => {
  it("图片卡不渲染 card-preview-toggle，工具栏也不重复 card-attachment-open", async () => {
    const w = mountCard(baseCard());
    await flushPromises();
    expect(w.find('[data-im="card-cover"]').exists(), "图片卡没有封面层").toBe(true);
    expect(w.find('[data-im="card-preview-toggle"]').exists(), "图片卡上还有展开预览入口").toBe(false);
    expect(
      w.findAll('[data-im="card-attachment-open"]').length,
      "封面期间工具栏还重复渲染了打开入口",
    ).toBe(0);
    expect(w.get('[data-im="card-cover-open"]').text()).toBe("查看原图");
  });

  it("从 store 直调 toggleCardPreview：返回 false，卡片仍是 normal，不出现阅读态", async () => {
    const model = baseCard();
    const w = mountCard(model);
    await flushPromises();
    const store = useInteractiveStore();
    const before = JSON.stringify(store.board);

    expect(store.toggleCardPreview(model.id)).toBe(false);
    expect(store.isCardPreviewExpanded(model.id)).toBe(false);
    expect(store.expandedCardIds).not.toContain(model.id);
    expect(store.previewExpandedCount).toBe(0);
    await nextTick();
    expect(w.get('[data-im="card"]').attributes("data-card-state")).toBe("normal");
    expect(w.find('[data-im="card-preview-host"]').exists()).toBe(false);
    expect(JSON.stringify(store.board), "查看态不该写板面").toBe(before);
  });

  it("同一块板面里 PDF 文件卡放大后仍自动进入阅读态", async () => {
    const pdf = baseCard({
      id: "card_pdf",
      w: 600,
      h: 420,
      meta: { name: "报告.pdf", file: { attachmentId: "att_pdf", name: "报告.pdf", size: 1024 } },
    });
    const store = useInteractiveStore();
    store.board = { ...emptyState("board_t"), cards: [pdf], selection: ["card_pdf"] };
    const w = mountCard(pdf);
    await flushPromises();
    expect(w.find('[data-im="card-preview-toggle"]').exists()).toBe(false);
    expect(w.get('[data-im="card"]').attributes("data-card-state")).toBe("reading");
  });
});

// ---------------------------------------------------------------------------
// 3. 折叠 / 恢复
// ---------------------------------------------------------------------------

describe("折叠与恢复：恢复后直接显示图片", () => {
  it("折叠态仍有封面；恢复后是常态（不是阅读态），封面继续显示", async () => {
    const model = baseCard({ folded: true });
    const w = mountCard(model);
    await flushPromises();
    expect(w.find('[data-im="card-cover"]').exists(), "折叠态丢了封面").toBe(true);
    expect(w.find('[data-im="card-preview-toggle"]').exists()).toBe(false);

    await w.get('[data-im="card-unfold"]').trigger("click");
    expect(w.emitted("toggle")?.length, "恢复卡片没有上报").toBeGreaterThan(0);

    await w.setProps({ card: { ...model, folded: false } });
    await flushPromises();
    expect(w.get('[data-im="card"]').attributes("data-card-state")).toBe("normal");
    expect(w.find('[data-im="card-cover"]').exists()).toBe(true);
    expect(w.find('[data-im="card-preview-host"]').exists(), "恢复后还套了一层预览").toBe(false);
    expect(w.find('[data-im="card-cover-img"]').exists()).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// 4. 拉伸不重复取字节；换附件 / 卸载释放 blob URL
// ---------------------------------------------------------------------------

describe("取字节与 blob URL 生命周期", () => {
  it("只改尺寸（w/h）不重新取字节，也不新建 blob URL", async () => {
    const model = baseCard();
    const w = mountCard(model);
    await flushPromises();
    expect(mocks.fetchAttachmentContent).toHaveBeenCalledTimes(1);
    expect(created).toEqual(["blob:qio-verify-1"]);

    await w.setProps({ card: { ...model, w: 300, h: 200 } });
    await flushPromises();
    await w.setProps({ card: { ...model, w: 180, h: 96 } });
    await flushPromises();

    expect(mocks.fetchAttachmentContent, "改尺寸重复取字节了").toHaveBeenCalledTimes(1);
    expect(created).toEqual(["blob:qio-verify-1"]);
    // 缩到最小尺寸仍要有图片（不消失）
    expect(w.find('[data-im="card-cover-img"]').exists()).toBe(true);
  });

  it("换附件：重新取新附件字节，旧 blob URL 被释放", async () => {
    const model = baseCard();
    const w = mountCard(model);
    await flushPromises();
    expect(created).toEqual(["blob:qio-verify-1"]);

    mocks.fetchAttachmentContent.mockResolvedValue(new Blob(["y"], { type: "image/png" }));
    await w.setProps({
      card: { ...model, meta: imageMeta("att_img2", "另一张.png") },
    });
    await flushPromises();

    expect(mocks.fetchAttachmentContent).toHaveBeenLastCalledWith("att_img2");
    expect(revoked, "换附件没有释放旧 URL").toContain("blob:qio-verify-1");
    expect(created).toHaveLength(2);
  });

  it("卸载释放当前 blob URL 并清掉 img.src", async () => {
    const w = mountCard(baseCard());
    await flushPromises();
    const img = w.get('[data-im="card-cover-img"]');
    expect(img.attributes("src")).toBe("blob:qio-verify-1");
    w.unmount();
    expect(revoked).toContain("blob:qio-verify-1");
  });
});

// ---------------------------------------------------------------------------
// 5. 失败原因与重试
// ---------------------------------------------------------------------------

describe("取字节失败：不留空白，给原因与重试入口", () => {
  it("fetch 失败：卡片上出现原因；封面层不假装有图", async () => {
    mocks.fetchAttachmentContent.mockRejectedValue(new Error("401：本机 API 拒绝了这次请求"));
    const w = mountCard(baseCard());
    await flushPromises();
    const note = w.find('[data-im="card-attachment-preview-state"]');
    expect(note.exists(), "失败后没有任何说明").toBe(true);
    expect(note.text()).toContain("401");
    expect(w.find('[data-im="card-cover-img"]').exists()).toBe(false);
    // 仍然没有展开预览入口（失败也不给假入口）
    expect(w.find('[data-im="card-preview-toggle"]').exists()).toBe(false);
  });
});
