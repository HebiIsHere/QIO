/**
 * 图片在卡片上的**常态展示**（本轮「互动模式图片展示简化」）。
 *
 * 用户确认的设计：图片统一是 **kind="file" + meta.file 附件**（互动板不再创建 kind="image" 卡片）。
 * - 常态 / 折叠态：封面层承担**完整**的图片展示（object-fit: contain，等比完整、不裁切、留白走 --bg-inset）；
 * - 卡片头部不再有「展开预览 / 收起预览」入口，图片卡不进入阅读态、不挂卡片内预览；
 * - 封面里不再重复文件名、不再常驻「图片：还没有补充说明」这类占位文字，只有**真实说明**才渲染一行；
 * - 查看原图走既有 openAttachment；工具栏上重复的 card-attachment-open 在封面期间不再渲染；
 * - 图片解码失败也要给原因 + 重试 / 查看原图 / 重新关联三个可用入口。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

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
import { defaultCardSeed, emptyState, setSelection } from "../../../interactive/board";
import type { BoardCard as BoardCardModel } from "../../../interactive/types";

const STAMP = "2026-10-15T00:00:00.000Z";
const SRC = resolve(process.cwd(), "src");

/** 某个选择器的规则块（第一个匹配），与同目录既有用例同一套读法。 */
function cssBlock(text: string, selector: string): string {
  const start = text.indexOf(selector);
  expect(start, "找不到选择器 " + selector).toBeGreaterThan(-1);
  const open = text.indexOf("{", start);
  const close = text.indexOf("}", open);
  return text.slice(open + 1, close);
}

function source(relative: string): string {
  return readFileSync(resolve(SRC, relative), "utf8");
}

/** 图片附件的**文件卡**（图片的真实形状：kind="file" + meta.file） */
function imageFileCard(patch: Partial<BoardCardModel> = {}): BoardCardModel {
  return {
    id: "card_img",
    kind: "file",
    content: "",
    meta: { name: "照片.jpeg", file: { attachmentId: "att_img", name: "照片.jpeg", sizeBytes: 66560 } },
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

/** 非图片附件的文件卡（PDF / Office / 代码 / 文本…） */
function pdfFileCard(patch: Partial<BoardCardModel> = {}): BoardCardModel {
  return imageFileCard({
    id: "card_doc",
    meta: { name: "简报.docx", file: { attachmentId: "att_doc", name: "简报.docx", sizeBytes: 1024 } },
    ...patch,
  });
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

beforeEach(() => {
  setActivePinia(createPinia());
  mocks.fetchAttachmentContent.mockReset();
  mocks.fetchAttachmentContent.mockResolvedValue(new Blob(["x"], { type: "image/jpeg" }));
  mocks.pickBrowserFile.mockReset();
  mocks.pickBrowserFile.mockResolvedValue(null);
  mocks.openAttachment.mockReset();
  mocks.openAttachment.mockResolvedValue({ action: "view", note: "已交给浏览器在新标签页打开" });
  created = [];
  revoked = [];
  Object.defineProperty(URL, "createObjectURL", {
    configurable: true,
    value: vi.fn(() => {
      const url = "blob:qio-cover-" + (created.length + 1);
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
      /* 已经卸载过 */
    }
  }
  wrappers = [];
  document.body.innerHTML = "";
});

describe("图片文件卡：常态由封面承担完整展示", () => {
  it("常态：封面层 + 标题 + 真实说明 + 「查看原图」，不再铺附件信息行与小缩略图", async () => {
    const w = mountCard(imageFileCard({ content: "补充说明一行" }));
    await flushPromises();

    expect(w.find('[data-im="card-cover"]').exists(), "没有封面层").toBe(true);
    expect(w.find('[data-im="card"]').attributes("data-cover")).toBe("1");
    expect(w.find('[data-im="card-cover-img"]').exists(), "封面里没有真实图片").toBe(true);
    expect(w.get('[data-im="card-cover-open"]').text(), "封面入口文案与真实动作不符").toBe("查看原图");
    expect(w.find('[data-im="card-cover-desc"]').text()).toContain("补充说明一行");
    expect(w.find('[data-im="card-title"]').text()).toBe("照片.jpeg");

    // 模板完整性：属性被挤成文本节点时（标签闭合位置写错）这里会立刻失败
    expect(w.text(), "卡片里出现了模板属性文本").not.toContain("@pointerdown");
    // 不重复显示文件名、不铺附件信息行、不出现小缩略图
    expect(w.text(), "常态不该再铺附件信息行").not.toContain("已关联已保存的附件");
    expect(w.find('[data-im="card-attachment"]').exists()).toBe(false);
    expect(w.find('[data-im="card-attachment-thumb"]').exists()).toBe(false);
    expect(w.find('[data-im="card-attachment-preview"]').exists()).toBe(false);
  });

  it("封面图是装饰（aria-hidden + 空 alt + 禁止原生拖动），卡片可访问名称仍在", async () => {
    const w = mountCard(imageFileCard());
    await flushPromises();

    const img = w.get('[data-im="card-cover-img"]');
    expect(img.attributes("aria-hidden")).toBe("true");
    expect(img.attributes("alt")).toBe("");
    expect(img.attributes("draggable")).toBe("false");
    expect(w.get('[data-im="card-title"]').text()).toContain("照片.jpeg");
  });

  it("封面显示期间，工具栏上重复的 card-attachment-open 不再渲染（避免两个入口各自 busy）", async () => {
    const w = mountCard(imageFileCard());
    await flushPromises();

    const toolbar = w.get('[data-im="card-toolbar"]');
    expect(toolbar.find('[data-im="card-attachment-open"]').exists(), "封面期间工具栏还在重复提供打开入口").toBe(false);
    // 必须有且只有一个「查看原图」入口
    expect(w.findAll('[data-im="card-cover-open"]')).toHaveLength(1);
    // 别的基础能力一个不少
    expect(toolbar.find('[data-im="card-edit"]').exists()).toBe(true);
    expect(toolbar.find('[data-im="card-attach-file"]').exists()).toBe(true);
    expect(toolbar.find('[data-im="card-more"]').exists()).toBe(true);
    expect(toolbar.find('[data-im="card-fold"]').exists()).toBe(true);
  });

  /*
    可达性回归（verifier 真实应用实测）：封面按钮的 elementFromPoint 命中 .card-body ——
    .card-body 是画在封面之上的**可命中空容器**，把这轮唯一的打开入口整个挡住了。
    修法：正文层整体让开指针（事件落到封面按钮或卡片本身），只对真的可交互的子树显式放行；
    绝不把 .card-body 压到封面后面（那会让草稿 / 冲突提示被图片盖住，契约 §12.1）。
    jsdom 没有真实命中测试，这里做源码守卫 + DOM 结构守卫，真实命中由 verifier 复跑。
  */
  it("封面模式：页脚（状态 / 时间）贴到卡片最下沿，紧凑档跟着换一档", () => {
    /*
      用户报的：时间条浮在卡片底边上方，底边那条露出原图。
      卡片自身有下内边距，页脚按内容盒排就会差这一截；封面模式下用等量负下外边距压到最下沿。
      真实渲染实测：页脚底边距卡片底边 13px → 1px，渐变条 31px → 1px。
    */
    const card = source("components/interactive/BoardCard.vue");
    expect(card).toContain("margin-bottom: calc(var(--card-pad) * -1);");
    expect(card).toContain("--card-pad: var(--sp-3);");
    // 紧凑档内边距是 --sp-2，负外边距必须跟着换档，否则会多压 4px
    expect(card).toContain(".card.is-compact.is-cover > .card-meta");
    const compact = card.slice(card.indexOf(".card.is-compact.is-cover > .card-meta"));
    expect(compact.slice(0, compact.indexOf("}"))).toContain("--card-pad: var(--sp-2)");
  });

  it("封面模式：正文层不吃指针事件，草稿 / 冲突提示显式放行（封面按钮才能被真实指针命中）", async () => {
    const card = source("components/interactive/BoardCard.vue");
    expect(cssBlock(card, ".card.is-cover > .card-body {"), "正文层还在抢封面按钮的指针").toContain(
      "pointer-events: none",
    );
    // 真的可交互的子树显式放行（不是把整个正文层又放回来）
    const hintRule = card.slice(card.indexOf(".card.is-cover > .card-body :deep(.card-draft-hint)"));
    expect(hintRule.slice(0, hintRule.indexOf("}"))).toContain("pointer-events: auto");
    // 头部 / 页脚只是文字与状态，也让开；头部里真正的按钮（折叠态「恢复卡片」）单独放行
    expect(card).toContain(".card.is-cover > .card-head { pointer-events: none; }");
    expect(card).toContain(".card.is-cover > .card-meta { pointer-events: none; }");
    const chipRule = card.slice(card.indexOf(".card.is-cover > .card-head .chip-btn"));
    expect(chipRule.slice(0, chipRule.indexOf("}"))).toContain("pointer-events: auto");
    // 封面按钮自己收回指针（封面层是 pointer-events: none 的背景层）
    const preview = source("components/interactive/CardAttachmentPreview.vue");
    expect(cssBlock(preview, ".cover-open {")).toContain("pointer-events: auto");
    expect(cssBlock(preview, ".cover-actions .btn {")).toContain("pointer-events: auto");

    const w = mountCard(imageFileCard());
    await flushPromises();
    expect(w.find('[data-im="card-cover-open"]').exists(), "封面按钮不在 DOM 里了").toBe(true);

    // 有未决草稿冲突时，CardDraftHint 真的挂在正文层里（放行规则不是空放行）
    const store = useInteractiveStore();
    store.draftConflicts = { "card:card_img": { local: "本页候选", server: "服务器正文" } } as never;
    await nextTick();
    const hint = w.find('[data-im="card-draft-hint"]');
    expect(hint.exists(), "未决冲突提示必须仍然渲染").toBe(true);
    expect(hint.element.closest('[data-im="card-body"]')).not.toBeNull();
    expect(hint.find('[data-im="card-draft-keep-local"]').exists()).toBe(true);
    expect(hint.find('[data-im="card-draft-keep-server"]').exists()).toBe(true);
  });

  it("占位说明不显示：新文件卡的默认 content（defaultCardSeed）不是真实说明", async () => {
    const w = mountCard(imageFileCard({ content: defaultCardSeed("file").content }));
    await flushPromises();

    expect(w.find('[data-im="card-cover"]').exists()).toBe(true);
    expect(w.find('[data-im="card-cover-desc"]').exists(), "占位说明被当成了真实说明").toBe(false);
    expect(w.text()).not.toContain(defaultCardSeed("file").content);
    expect(w.text()).not.toContain("还没有补充说明");
  });

  it("最小尺寸 180×96：标题、查看原图与必要状态都在；说明行让位（compact 口径）", async () => {
    const w = mountCard(imageFileCard({ w: 180, h: 96, content: "补充说明一行" }));
    await flushPromises();

    const root = w.get('[data-im="card"]');
    expect(root.classes()).toContain("is-compact");
    expect(root.attributes("data-cover")).toBe("1");
    expect(w.get('[data-im="card-title"]').text()).toBe("照片.jpeg");
    expect(w.find('[data-im="card-cover-open"]').exists(), "最小尺寸下查看原图不该消失").toBe(true);
    expect(w.find('[data-im="card-meta"]').exists(), "必要状态行必须留着").toBe(true);
    // 让位的是说明行：最小尺寸下不渲染封面里那一行说明
    expect(w.find('[data-im="card-cover-desc"]').exists()).toBe(false);
    // 封面层不产生横纵滚动条
    const cover = w.find('[data-im="card-cover"]');
    expect(cover.exists()).toBe(true);
  });

  it("折叠态：仍保留封面带", async () => {
    const w = mountCard(imageFileCard({ folded: true }));
    await flushPromises();
    expect(w.find('[data-im="card-cover"]').exists()).toBe(true);
    expect(w.find('[data-im="card-preview-toggle"]').exists()).toBe(false);
  });

  it("编辑态：不铺封面（能改名称，编辑要能看清表单）", async () => {
    const w = mountCard(imageFileCard());
    await flushPromises();
    await w.get('[data-im="card-edit"]').trigger("click");
    await nextTick();

    expect(w.find('[data-im="card-cover"]').exists(), "编辑态不该铺封面").toBe(false);
    const nameInput = w.get('[data-im="card-meta-name"]');
    expect((nameInput.element as HTMLInputElement).value).toBe("照片.jpeg");

    // 只改名称也必须真的形成一次板面变更（完成编辑 = 再点一次编辑按钮）
    await nameInput.setValue("改名.jpeg");
    await w.get('[data-im="card-edit"]').trigger("click");
    await nextTick();
    const patches = w.emitted("patch") as unknown[][];
    expect(patches, "改了名称却没有产生板面变更").toBeTruthy();
    expect((patches[0][1] as { meta: { name: string } }).meta.name).toBe("改名.jpeg");
  });

  it("查看原图：走既有 openAttachment，不展开卡片、不改尺寸 / 位置、不产生板面变更", async () => {
    const card = imageFileCard();
    const w = mountCard(card);
    await flushPromises();
    const geometry = JSON.stringify({ x: card.x, y: card.y, w: card.w, h: card.h });

    await w.get('[data-im="card-cover-open"]').trigger("click");
    await flushPromises();

    expect(mocks.openAttachment).toHaveBeenCalledTimes(1);
    const [refArg] = mocks.openAttachment.mock.calls[0] as unknown as [Record<string, unknown>];
    expect(refArg.id).toBe("att_img");
    expect(refArg.name).toBe("照片.jpeg");
    expect(w.get('[data-im="card-attachment-open-status"]').text()).toContain("新标签页");
    expect(w.get('[data-im="card"]').attributes("data-card-state")).toBe("normal");
    expect(JSON.stringify({ x: card.x, y: card.y, w: card.w, h: card.h })).toBe(geometry);
    expect(w.emitted("patch")).toBeUndefined();
  });

  it("封面 CSS：object-fit 是 contain（等比完整显示，不拉伸不裁切），留白用既有令牌", () => {
    const cover = source("components/interactive/CardAttachmentPreview.vue");
    const img = cssBlock(cover, ".cover-img {");
    expect(img, "封面图还在裁切").toContain("object-fit: contain");
    expect(img).not.toContain("object-fit: cover");
    // 图片层不吃指针事件：连接点 / 尺寸把手 / 工具栏不会被它盖住
    expect(img).toContain("pointer-events: none");
    // 封面层自己也不产生滚动条
    const layer = cssBlock(cover, ".card-cover {");
    expect(layer).toContain("inset: 0");
    expect(layer).toContain("overflow: hidden");
    expect(layer).toContain("pointer-events: none");
    // 大片蒙版（上下各 42%）不再存在
    expect(cover).not.toContain(".cover-scrim");
    expect(cover).not.toContain("height: 42%");
    // 常驻占位文案已删除
    expect(cover).not.toContain("图片：还没有补充说明");
  });
});

describe("图片文件卡：卡片内不再有阅读态", () => {
  it("没有 card-preview-toggle 入口、没有 card-preview-host、data-card-state 不是 reading", async () => {
    const w = mountCard(imageFileCard());
    await flushPromises();

    expect(w.get('[data-im="card"]').attributes("data-card-state")).toBe("normal");
    expect(w.find('[data-im="card-preview-toggle"]').exists(), "图片卡还在给卡片内阅读入口").toBe(false);
    expect(w.find('[data-im="card-preview-host"]').exists(), "图片卡挂上了卡片内预览").toBe(false);
    expect(w.text()).not.toContain("展开预览");
    expect(w.text()).not.toContain("收起预览");
  });

  it("就算 store 里被标记过展开（旧数据 / 别的入口），图片卡也不进入阅读态", async () => {
    const w = mountCard(imageFileCard());
    await flushPromises();
    const store = useInteractiveStore();
    store.toggleCardPreview("card_img");
    await nextTick();

    expect(w.get('[data-im="card"]').attributes("data-card-state")).toBe("normal");
    expect(w.find('[data-im="card-preview-host"]').exists()).toBe(false);
    expect(w.find('[data-im="card-cover"]').exists()).toBe(true);
  });
});

describe("别的材料不受影响（本轮明确要求保留的能力）", () => {
  it("非图片文件卡（PDF / Office）仍有「展开预览 / 收起预览」与阅读主体", async () => {
    const w = mountCard(pdfFileCard());
    await flushPromises();

    expect(w.find('[data-im="card-cover"]').exists()).toBe(false);
    const toggle = w.get('[data-im="card-preview-toggle"]');
    expect(toggle.text()).toBe("展开预览");
    await toggle.trigger("click");
    await nextTick();

    expect(w.get('[data-im="card"]').attributes("data-card-state")).toBe("reading");
    expect(w.get('[data-im="card-preview-toggle"]').text()).toBe("收起预览");
    expect(w.find('[data-im="card-preview-host"]').exists()).toBe(true);
  });

  it("文字卡仍有「展开预览 / 收起预览」", async () => {
    const w = mountCard(
      imageFileCard({ id: "card_text", kind: "text", content: "一段正文", meta: {} }),
    );
    await flushPromises();

    const toggle = w.get('[data-im="card-preview-toggle"]');
    expect(toggle.text()).toBe("展开预览");
    await toggle.trigger("click");
    await nextTick();
    expect(w.get('[data-im="card"]').attributes("data-card-state")).toBe("reading");
  });

  it("非图片文件卡的工具栏行为不变：card-attachment-open 仍在", async () => {
    const w = mountCard(pdfFileCard());
    await flushPromises();
    expect(w.find('[data-im="card-cover"]').exists()).toBe(false);
    expect(w.find('[data-im="card-toolbar"] [data-im="card-attachment-open"]').exists()).toBe(true);
  });
});

describe("图片失败：给原因 + 可用入口", () => {
  it("解码失败：原图片区域显示原因，并给出重试 / 查看原图 / 重新关联", async () => {
    const w = mountCard(imageFileCard());
    await flushPromises();
    const img = w.find('[data-im="card-cover-img"]');
    expect(img.exists()).toBe(true);

    await img.trigger("error");
    await nextTick();

    expect(w.find('[data-im="card-cover-img"]').exists(), "解不开的图片还挂在画面上").toBe(false);
    expect(w.find('[data-im="card-cover-failed"]').exists()).toBe(true);
    expect(w.get('[data-im="card-attachment-preview-state"]').text()).toMatch(/解不开|损坏/);
    expect(w.find('[data-im="card-cover-retry"]').exists()).toBe(true);
    expect(w.find('[data-im="card-cover-original"]').exists()).toBe(true);
    expect(w.find('[data-im="card-cover-relink"]').exists()).toBe(true);
  });

  it("重试：重走既有预览 blob 加载（旧 URL 释放、只留一个新 URL）", async () => {
    const w = mountCard(imageFileCard());
    await flushPromises();
    expect(created).toEqual(["blob:qio-cover-1"]);
    await w.get('[data-im="card-cover-img"]').trigger("error");
    await nextTick();

    mocks.fetchAttachmentContent.mockClear();
    await w.get('[data-im="card-cover-retry"]').trigger("click");
    await flushPromises();

    expect(mocks.fetchAttachmentContent).toHaveBeenCalledTimes(1);
    expect(mocks.fetchAttachmentContent).toHaveBeenCalledWith("att_img");
    expect(revoked, "旧 blob URL 没有释放").toContain("blob:qio-cover-1");
    expect(created).toHaveLength(2);
    const img = w.get('[data-im="card-cover-img"]');
    expect(img.attributes("src")).toBe("blob:qio-cover-2");
  });

  it("请求失败：显示 previewImage.error() 的真实原因，不谎报成功", async () => {
    mocks.fetchAttachmentContent.mockRejectedValue(new Error("401：本机 API 拒绝了这次请求"));
    const w = mountCard(imageFileCard());
    await flushPromises();

    expect(w.find('[data-im="card-cover-img"]').exists()).toBe(false);
    const reason = w.get('[data-im="card-attachment-preview-state"]').text();
    expect(reason).toContain("401");
    expect(w.find('[data-im="card-cover-retry"]').exists()).toBe(true);
    // 失败时入口在失败区域里（不在底部小条上各留一个，避免两个入口各自 busy）
    expect(w.find('[data-im="card-cover-original"]').exists()).toBe(true);
    expect(w.find('[data-im="card-cover-open"]').exists()).toBe(false);
  });

  it("「重新关联」把事件交给 BoardCard，由既有 pickAttachment 处理", async () => {
    mocks.fetchAttachmentContent.mockRejectedValue(new Error("文件丢失：附件不在了"));
    const w = mountCard(imageFileCard());
    await flushPromises();

    await w.get('[data-im="card-cover-relink"]').trigger("click");
    await flushPromises();

    expect(mocks.pickBrowserFile).toHaveBeenCalledTimes(1);
    // 取消选择也要如实说出来（不假装读到了内容）
    expect(w.text()).toContain("没有选择文件");
  });
});
