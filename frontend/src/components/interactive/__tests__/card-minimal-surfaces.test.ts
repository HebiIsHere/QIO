/**
 * 卡片极简表面（2026-10-14 用户要求）：
 *
 * 1. 文字卡：编辑态正文区里只有「格式工具栏 + 输入框」，输入框吃满卡片；
 *    没有语法说明、没有完成 / 取消按钮行、没有例行的「草稿保存中 / 已保存」。
 *    完成编辑 = 退出选中（点板面空白 / 点别的卡片）或再点一次工具栏上的编辑按钮。
 * 2. 文件卡：卡片本身只显示文件名，附件动作（重新关联 / 查看）与完成编辑都在工具栏；
 *    没有「待补充文件说明」输入框，也没有卡片内的附件信息行 / 预览。
 * 3. 编辑里换的行，关闭编辑后必须真的换行（段落 / 引用块内的 \\n 渲染成换行）。
 *
 * 标注：【组件/DOM】挂载真实 BoardCard.vue + 真实 store；只把附件系统动作换成替身。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";

const mocks = vi.hoisted(() => ({
  openAttachment: vi.fn(),
  fetchAttachmentContent: vi.fn(),
}));

vi.mock("../../../services/attachments", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../services/attachments")>();
  return {
    ...actual,
    openAttachment: mocks.openAttachment,
    fetchAttachmentContent: mocks.fetchAttachmentContent,
  };
});

import BoardCard from "../BoardCard.vue";
import { useInteractiveStore } from "../../../stores/interactive";
import { emptyState } from "../../../interactive/board";
import { parseDocText, serializeDocText } from "../../../interactive/textCard";
import type { BoardCard as BoardCardModel } from "../../../interactive/types";

const STAMP = "2026-10-13T00:00:00.000Z";

function mkCard(id: string, patch: Partial<BoardCardModel> = {}): BoardCardModel {
  return {
    id,
    kind: "text",
    content: "",
    meta: {},
    x: 0,
    y: 0,
    w: 320,
    h: 320,
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

function textCard(id: string, docText: string): BoardCardModel {
  const doc = parseDocText(docText);
  return mkCard(id, {
    kind: "text",
    content: docText.trim(),
    meta: { doc, docText: serializeDocText(doc) },
  });
}

function fileCard(id: string, patch: Partial<BoardCardModel> = {}): BoardCardModel {
  return mkCard(id, {
    kind: "file",
    content: "待补充文件说明",
    meta: { name: "简报_2026-06-30.docx", file: { name: "简报_2026-06-30.docx", attachmentId: "att_1", size: 47_100 } },
    ...patch,
  });
}

let store: ReturnType<typeof useInteractiveStore>;
let wrapper: VueWrapper | null = null;

function mountCard(card: BoardCardModel, selected = true): VueWrapper {
  store.board = { ...emptyState("board_t"), cards: [card], selection: [card.id] };
  wrapper = mount(BoardCard, {
    attachTo: document.body,
    props: {
      card,
      selected,
      highlight: false,
      dragging: false,
      x: card.x,
      y: card.y,
      groupName: null,
      groups: [],
      toolbarLeft: 0,
      toolbarTop: card.y,
      multi: false,
      connecting: false,
    },
  });
  return wrapper;
}

beforeEach(() => {
  localStorage.clear();
  setActivePinia(createPinia());
  store = useInteractiveStore();
  vi.resetAllMocks();
});

afterEach(() => {
  wrapper?.unmount();
  wrapper = null;
  document.body.innerHTML = "";
  vi.restoreAllMocks();
});

describe("文字卡：只有工具栏 + 输入框，完成编辑 = 退出选中 / 再点编辑按钮", () => {
  it("编辑态没有说明文字 / 完成取消按钮行 / 例行草稿提示；输入框吃满卡片", async () => {
    const card = textCard("c_text", "第一行\n第二行\n");
    const w = mountCard(card);
    await w.get('[data-im="card-edit"]').trigger("click");

    expect(w.find('[data-im="card-editor-toolbar"]').exists()).toBe(true);
    expect(w.findAll('[data-im="card-editor"]')).toHaveLength(1);
    expect(w.find(".editor-note").exists(), "不该再有 markdown 语法说明").toBe(false);
    expect(w.find(".draft-note").exists(), "不该再有「输入过程只保存草稿」").toBe(false);
    expect(w.find(".row").exists(), "不该再有完成 / 取消按钮行").toBe(false);
    expect(w.find('[data-im="card-draft-hint"]').exists(), "例行的草稿状态不该占卡片").toBe(false);
    // 输入框吃满：正文区是 flex 列，编辑区带 fill，min-height 交给 flex
    expect(w.get(".card-body").classes()).toContain("card-body-editing-text");
    expect(w.get('[data-im="text-card-editor"]').classes()).toContain("is-fill");
    expect(w.get('[data-im="card-editor"]').attributes("style") ?? "").toContain("min-height: 0px");
    // 编辑态里编辑按钮变成「完成编辑」
    expect(w.get('[data-im="card-edit"]').text()).toContain("完成编辑");
  });

  it("再点一次编辑按钮 = 完成编辑：有改动就形成正式变更并退出编辑", async () => {
    const card = textCard("c_text", "旧正文\n");
    const w = mountCard(card);
    await w.get('[data-im="card-edit"]').trigger("click");
    await w.get('[data-im="card-editor"]').setValue("新正文\n");

    await w.get('[data-im="card-edit"]').trigger("click");

    const patches = w.emitted("patch") as unknown[][] | undefined;
    expect(patches?.length, "完成编辑没有形成正式变更").toBeGreaterThan(0);
    expect((patches![patches!.length - 1][1] as { content?: string }).content).toBe("新正文");
    expect(w.find('[data-im="card-editor"]').exists()).toBe(false);
  });

  it("退出选中（点板面空白）也是完成编辑；没有改动时不产生板面变更", async () => {
    const changed = textCard("c_text", "旧正文\n");
    const w1 = mountCard(changed);
    await w1.get('[data-im="card-edit"]').trigger("click");
    await w1.get('[data-im="card-editor"]').setValue("退出选中的改动\n");
    await w1.setProps({ selected: false });
    const patches = w1.emitted("patch") as unknown[][] | undefined;
    expect(patches?.length, "退出选中没有形成正式变更").toBeGreaterThan(0);
    expect((patches![patches!.length - 1][1] as { content?: string }).content).toBe("退出选中的改动");
    w1.unmount();
    wrapper = null;

    const untouched = textCard("c_text", "原样不动\n");
    const w2 = mountCard(untouched);
    await w2.get('[data-im="card-edit"]').trigger("click");
    await w2.setProps({ selected: false });
    expect(w2.emitted("patch"), "只是点开看了一眼，却生成了板面变更").toBeFalsy();
    expect(w2.find('[data-im="card-editor"]').exists()).toBe(false);
  });

  it("编辑里换的行，关闭编辑后真的换行（段落 / 引用块内的换行渲染成 <br>）", () => {
    const card = textCard("c_text", "第一行\n第二行\n\n> 引用一\n> 引用二\n");
    const w = mountCard(card);

    const paragraph = w.get('[data-im="card-block-paragraph"]');
    expect(paragraph.text()).toContain("第一行");
    expect(paragraph.text()).toContain("第二行");
    expect(paragraph.html(), "段落里的换行被折叠成了一行").toContain("<br");

    const quote = w.get('[data-im="card-block-quote"]');
    expect(quote.html(), "引用块里的换行被折叠成了一行").toContain("<br");
  });
});

describe("文件卡：卡片只显示文件名，按钮全在工具栏", () => {
  it("关闭态：卡片里没有正文 / 附件信息行 / 预览，只有文件名", () => {
    const w = mountCard(fileCard("c_file"), false);

    expect(w.get('[data-im="card-title"]').text()).toBe("简报_2026-06-30.docx");
    expect(w.text()).not.toContain("待补充文件说明");
    expect(w.find(".content").exists()).toBe(false);
    expect(w.find('[data-im="card-attachment-preview"]').exists()).toBe(false);
    expect(w.text()).not.toContain("已关联已保存的附件");
  });

  it("编辑态：卡片里只有名称输入框；工具栏提供 完成编辑 / 重新关联附件 / 查看", async () => {
    const w = mountCard(fileCard("c_file"));
    await w.get('[data-im="card-edit"]').trigger("click");

    expect(w.find('[data-im="card-editor"]').exists(), "文件卡不该有「待补充文件说明」输入框").toBe(false);
    expect(w.find('[data-im="card-meta-name"]').exists()).toBe(true);
    expect(w.find(".draft-note").exists()).toBe(false);
    expect(w.find(".row").exists()).toBe(false);

    const toolbar = w.get('[data-im="card-toolbar"]');
    expect(toolbar.find('[data-im="card-edit"]').text()).toContain("完成编辑");
    expect(toolbar.find('[data-im="card-attach-file"]').text()).toContain("重新关联附件");
    expect(toolbar.find('[data-im="card-attachment-open"]').exists()).toBe(true);
  });

  it("工具栏的「查看」走既有 openAttachment，结果如实显示", async () => {
    mocks.openAttachment.mockResolvedValue({ action: "view", note: "已交给浏览器在新标签页打开" });
    const w = mountCard(fileCard("c_file"));

    await w.get('[data-im="card-attachment-open"]').trigger("click");
    await Promise.resolve();
    await Promise.resolve();

    expect(mocks.openAttachment).toHaveBeenCalledTimes(1);
    const status = w.get('[data-im="card-file-action-status"]');
    expect(status.classes()).toContain("is-ok");
    expect(status.text()).toContain("已交给浏览器");
  });

  it("工具栏的「查看」打不开时如实说打不开，不假装成功", async () => {
    mocks.openAttachment.mockRejectedValue(new Error("弹窗被拦截"));
    const w = mountCard(fileCard("c_file"));

    await w.get('[data-im="card-attachment-open"]').trigger("click");
    await Promise.resolve();
    await Promise.resolve();

    const status = w.get('[data-im="card-file-action-status"]');
    expect(status.classes()).toContain("is-error");
    expect(status.text()).toContain("打不开");
  });
});
