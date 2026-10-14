/**
 * 图片卡「封面式」呈现（2026-10-14 第六个问题）。
 *
 * 用户确认的设计：图片卡 + 附件是图片的文件卡，**常态把图片作为卡片背景铺满**，
 * 只留标题 + 一行补充说明 + 右下角「查看」浮层按钮；编辑态与阅读态不铺封面；
 * 折叠态保留一条封面带。附件信息行与缩略图在常态让位给封面（阅读态里仍能看到完整信息）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";

const mocks = vi.hoisted(() => ({
  fetchAttachmentContent: vi.fn(),
  pickBrowserFile: vi.fn(),
  uploadAttachment: vi.fn(),
  waitUntilSettled: vi.fn(),
  openAttachment: vi.fn(),
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
  };
});

import BoardCard from "../BoardCard.vue";
import { useInteractiveStore } from "../../../stores/interactive";
import { emptyState, setSelection } from "../../../interactive/board";
import type { BoardCard as BoardCardModel } from "../../../interactive/types";

const STAMP = "2026-10-14T00:00:00.000Z";

function baseCard(patch: Partial<BoardCardModel> = {}): BoardCardModel {
  return {
    id: "card_img",
    kind: "image",
    content: "",
    // 标题取 meta.name；附件关联信息在 meta.image（契约 §14.1）
    meta: { name: "照片.jpeg", image: { attachmentId: "att_img", name: "照片.jpeg", sizeBytes: 66560 } },
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

let wrappers: VueWrapper[] = [];

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

beforeEach(() => {
  setActivePinia(createPinia());
  mocks.fetchAttachmentContent.mockReset();
  mocks.fetchAttachmentContent.mockResolvedValue(new Blob(["x"], { type: "image/jpeg" }));
});

afterEach(() => {
  for (const w of wrappers) {
    try {
      w.unmount();
    } catch {
      /* 已经卸载过 */
    }
  }
  wrappers = [];
  document.body.innerHTML = "";
});

describe("图片卡：封面式呈现", () => {
  it("常态：图片铺满卡片作为背景，只留标题 / 一行说明 / 右下角「查看」", async () => {
    const w = mountCard(baseCard({ content: "补充说明一行" }));
    await nextTick();
    expect(w.find('[data-im="card-cover"]').exists(), "没有封面层").toBe(true);
    expect(w.find('[data-im="card-cover-open"]').exists(), "封面里没有「查看」入口").toBe(true);
    // 模板完整性：属性被挤成文本节点时（标签闭合位置写错）这里会立刻失败
    expect(w.text(), "卡片里出现了模板属性文本").not.toContain("@pointerdown");
    expect(w.find('[data-im="card"]').attributes("data-cover")).toBe("1");
    expect(w.find('[data-im="card-cover-desc"]').text()).toContain("补充说明一行");
    expect(w.text(), "标题应当仍在卡片上").toContain("照片.jpeg");
    expect(w.text(), "常态不该再铺附件信息行").not.toContain("已关联已保存的附件");
    expect(w.find('[data-im="card-attachment-thumb"]').exists(), "常态不该再出现小缩略图").toBe(false);
  });

  it("封面图是装饰（aria-hidden + 空 alt），卡片的可访问名称仍在", async () => {
    const w = mountCard(baseCard());
    await nextTick();
    const img = w.find('[data-im="card-cover-img"]');
    if (img.exists()) {
      expect(img.attributes("aria-hidden")).toBe("true");
      expect(img.attributes("alt")).toBe("");
    }
    expect(w.text()).toContain("照片");
  });

  it("附件不是图片的文件卡：不铺封面，保留原来的附件信息行", async () => {
    const w = mountCard(
      baseCard({ id: "card_doc", kind: "file", meta: { file: { attachmentId: "att_doc", name: "简报.docx", sizeBytes: 1024 } } }),
    );
    await nextTick();
    expect(w.find('[data-im="card-cover"]').exists()).toBe(false);
    expect(w.text()).toContain("已关联已保存的附件");
  });

  it("折叠态：仍保留一条封面带", async () => {
    const w = mountCard(baseCard({ folded: true }));
    await nextTick();
    expect(w.find('[data-im="card-cover"]').exists()).toBe(true);
  });

  it("编辑态：不铺封面（保留表单）", async () => {
    const w = mountCard(baseCard());
    await nextTick();
    const edit = w.find('[data-im="card-edit"]');
    expect(edit.exists(), "找不到编辑入口").toBe(true);
    await edit.trigger("click");
    await nextTick();
    expect(w.find('[data-im="card-cover"]').exists(), "编辑态不该铺封面").toBe(false);
  });

  it("阅读态：不铺封面（真实预览接管），附件信息仍在", async () => {
    const w = mountCard(baseCard());
    await nextTick();
    const toggle = w.find('[data-im="card-preview-toggle"]');
    expect(toggle.exists(), "找不到展开预览入口").toBe(true);
    await toggle.trigger("click");
    await nextTick();
    expect(w.find('[data-im="card-cover"]').exists(), "阅读态不该铺封面").toBe(false);
    expect(w.find('[data-im="card-preview-host"]').exists()).toBe(true);
  });
});
