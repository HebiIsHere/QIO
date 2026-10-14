/**
 * IRL-A：互动对话窗口（ChatDock）—— 精简结构 / 自由移动 / 自由尺寸 / 按板面持久化。
 *
 * 对应任务书 §4 与 §3.1/3.2/3.5/3.6/3.7/3.8/3.10。这一组用例只证明**本组件真的接上了**：
 * 1. 尺寸：四条边 / 四个角的指针拖动走公共 useResizeGesture（拖动只预览、松手提交一次、
 *    取消即恢复、阈值内算点击、最小尺寸不被压成一行）；
 * 2. 移动：标题栏是唯一把手（阈值、取消、正文区不参与）；
 * 3. 只改查看：拖动 / 调整尺寸不写板面状态、不发请求；
 * 4. 持久化：按板面身份绑定，超屏存档收敛回可见区；
 * 5. 结构：没有附件就没有附件条、帮助可开可关且内容齐全、收起再打开不丢草稿 / 光标 / 阅读位置；
 * 6. 发布：显示框变化发布给别的浮层，卸载不留幽灵矩形。
 *
 * 标注：【组件/DOM】真实挂载 ChatDock + 真实 store；附件服务按既有做法打桩。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { nextTick } from "vue";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const mocks = vi.hoisted(() => ({
  pickBrowserFile: vi.fn(),
  pickLocalPath: vi.fn(),
  prepareAttachment: vi.fn(),
  uploadAttachment: vi.fn(),
  waitUntilSettled: vi.fn(),
  removeAttachment: vi.fn(),
  retryAttachment: vi.fn(),
  openAttachment: vi.fn(),
  relocateAttachment: vi.fn(),
}));

vi.mock("../../../services/attachments", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../services/attachments")>();
  return {
    ...actual,
    pickBrowserFile: mocks.pickBrowserFile,
    pickLocalPath: mocks.pickLocalPath,
    prepareAttachment: mocks.prepareAttachment,
    uploadAttachment: mocks.uploadAttachment,
    waitUntilSettled: mocks.waitUntilSettled,
    removeAttachment: mocks.removeAttachment,
    retryAttachment: mocks.retryAttachment,
    openAttachment: mocks.openAttachment,
    relocateAttachment: mocks.relocateAttachment,
  };
});

import ChatDock from "../ChatDock.vue";
import { useInteractiveStore } from "../../../stores/interactive";
import { useSessionStore } from "../../../stores/session";
import { CHAT_FRAME_MIN, loadChatFrame, saveChatFrame } from "../../../interactive/chatFrame";
import { liveChatCollapsed, liveChatFrame, resetChatFrameState } from "../../../interactive/chatFrameState";
import { COPY_MAX_BYTES, humanSize, type AttachmentRef } from "../../../services/attachments";
import { chatScopeText } from "../../../interactive/chat";
import { MEDIA_CAPABILITY_HINT } from "../../mediaFacts";

const wrappers: VueWrapper[] = [];

function mountDock(open = true) {
  const pinia = createPinia();
  setActivePinia(pinia);
  const interactive = useInteractiveStore();
  const session = useSessionStore();
  session.currentTopicId = "A";
  interactive.chatOpen = open;
  const wrapper = mount(ChatDock, { global: { plugins: [pinia] }, attachTo: document.body });
  wrappers.push(wrapper);
  return { wrapper, interactive, session };
}

/** 面板容器的行内几何（frame → left/top/width/height）。 */
function box(wrapper: VueWrapper) {
  const el = wrapper.find('[data-im="chat-dock"]').element as HTMLElement;
  const num = (value: string) => Number.parseFloat(value || "0");
  return {
    left: num(el.style.left),
    top: num(el.style.top),
    width: num(el.style.width),
    height: num(el.style.height),
  };
}

/** jsdom 没有 PointerEvent：用 MouseEvent 带同样的字段（与既有手势用例一致）。 */
function pointer(type: string, target: EventTarget, x = 0, y = 0) {
  target.dispatchEvent(
    new MouseEvent(type, { bubbles: true, cancelable: true, clientX: x, clientY: y, button: 0 }),
  );
}

function attachment(over: Partial<AttachmentRef> = {}): AttachmentRef {
  return {
    id: "att_1",
    name: "季度报告.pdf",
    sizeBytes: 2048,
    kind: "copy",
    display: "已保存副本",
    state: "ready",
    error: null,
    ...over,
  };
}

beforeEach(() => {
  localStorage.clear();
  resetChatFrameState();
  for (const key of Object.keys(mocks) as (keyof typeof mocks)[]) mocks[key].mockReset();
  mocks.pickBrowserFile.mockResolvedValue(null);
  mocks.pickLocalPath.mockResolvedValue(null);
  mocks.uploadAttachment.mockResolvedValue(attachment());
  mocks.waitUntilSettled.mockImplementation(async (item: AttachmentRef) => item);
  mocks.removeAttachment.mockResolvedValue(undefined as never);
});

afterEach(() => {
  while (wrappers.length) wrappers.pop()?.unmount();
  localStorage.clear();
  resetChatFrameState();
  document.body.innerHTML = "";
  vi.restoreAllMocks();
});

describe("尺寸：四条边 + 四个角的拖动（公共手势层）", () => {
  it("拖动右下角：过程中只预览跟手，松手才落盘一次", async () => {
    const { wrapper, interactive } = mountDock();
    const before = box(wrapper);
    const handle = wrapper.find('[data-resize-handle="se"]').element;

    pointer("pointerdown", handle, 200, 200);
    pointer("pointermove", document, 260, 240);
    await nextTick();
    const during = box(wrapper);
    expect(during.width, "拖动中没有跟手").toBe(before.width + 60);
    expect(during.height).toBe(before.height + 40);
    expect(loadChatFrame(interactive.boardId), "拖动过程中不该落盘").toBeNull();

    pointer("pointerup", document, 260, 240);
    await nextTick();
    expect(loadChatFrame(interactive.boardId)?.w, "松手后没有提交这一次的尺寸").toBe(before.width + 60);
    expect(box(wrapper).width).toBe(before.width + 60);
  });

  it("阈值之内算点击：不改尺寸、不落盘", async () => {
    const { wrapper, interactive } = mountDock();
    const before = box(wrapper);
    const handle = wrapper.find('[data-resize-handle="se"]').element;

    pointer("pointerdown", handle, 200, 200);
    pointer("pointermove", document, 201, 201);
    pointer("pointerup", document, 201, 201);
    await nextTick();

    expect(box(wrapper)).toEqual(before);
    expect(loadChatFrame(interactive.boardId)).toBeNull();
  });

  it("Esc 取消：恢复拖动前那一版，松手也不再提交", async () => {
    const { wrapper, interactive } = mountDock();
    const before = box(wrapper);
    const handle = wrapper.find('[data-resize-handle="w"]').element;

    pointer("pointerdown", handle, 200, 200);
    pointer("pointermove", document, 120, 200);
    await nextTick();
    expect(box(wrapper).width, "往左拉宽没有跟手").toBe(before.width + 80);

    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await nextTick();
    expect(box(wrapper), "取消后没有恢复拖动前的框").toEqual(before);
    expect(loadChatFrame(interactive.boardId)).toBeNull();

    pointer("pointerup", document, 120, 200);
    await nextTick();
    expect(box(wrapper)).toEqual(before);
    expect(loadChatFrame(interactive.boardId)).toBeNull();
  });

  it("键盘 −/＋：一次一步、左上角不动，并且真的落盘", async () => {
    const { wrapper, interactive } = mountDock();
    const before = box(wrapper);

    await wrapper.find('[data-resize-key="w+"]').trigger("click");
    await nextTick();
    expect(box(wrapper).width).toBe(before.width + 24);
    expect(box(wrapper).left, "键盘调整不该把窗口推走").toBe(before.left);
    expect(loadChatFrame(interactive.boardId)?.w).toBe(before.width + 24);

    await wrapper.find('[data-resize-key="h-"]').trigger("click");
    await nextTick();
    expect(box(wrapper).height).toBe(before.height - 24);
  });

  it("缩到最小：收敛到 CHAT_FRAME_MIN，阅读区不被压成一行（最小高度由 CSS 兜住）", () => {
    const { wrapper } = mountDock();
    const handle = wrapper.find('[data-resize-handle="se"]').element;

    pointer("pointerdown", handle, 400, 400);
    pointer("pointermove", document, -5000, -5000);
    pointer("pointerup", document, -5000, -5000);
    return nextTick().then(() => {
      expect(box(wrapper).width).toBe(CHAT_FRAME_MIN.w);
      expect(box(wrapper).height).toBe(CHAT_FRAME_MIN.h);
      const source = readFileSync(
        resolve(process.cwd(), "src/components/interactive/ChatDock.vue"),
        "utf8",
      );
      // 消息阅读区有独立的最小高度：附件 / 帮助 / 错误 / 审批都挤不掉它
      expect(source).toMatch(/\.stream-wrap \{[\s\S]*?min-height: 72px/);
      expect(source).toMatch(/\.composer \{[\s\S]*?overflow-y: auto/);
    });
  });
});

describe("移动：标题栏是唯一把手", () => {
  it("标题栏拖动：过程中只预览，松手才落盘", async () => {
    const { wrapper, interactive } = mountDock();
    const before = box(wrapper);
    const head = wrapper.find('[data-im="chat-head"]').element;

    pointer("pointerdown", head, 600, 200);
    pointer("pointermove", document, 560, 170);
    await nextTick();
    expect(box(wrapper).left).toBe(before.left - 40);
    expect(box(wrapper).top).toBe(before.top - 30);
    expect(loadChatFrame(interactive.boardId), "拖动过程中不该落盘").toBeNull();

    pointer("pointerup", document, 560, 170);
    await nextTick();
    expect(loadChatFrame(interactive.boardId)).toMatchObject({
      x: before.left - 40,
      y: before.top - 30,
    });
  });

  it("阈值之内算点击：位置不变", async () => {
    const { wrapper } = mountDock();
    const before = box(wrapper);
    const head = wrapper.find('[data-im="chat-head"]').element;
    pointer("pointerdown", head, 600, 200);
    pointer("pointermove", document, 601, 201);
    pointer("pointerup", document, 601, 201);
    await nextTick();
    expect(box(wrapper)).toEqual(before);
  });

  it("Esc / 失焦取消：恢复拖动前的位置", async () => {
    const { wrapper, interactive } = mountDock();
    const before = box(wrapper);
    const head = wrapper.find('[data-im="chat-head"]').element;

    pointer("pointerdown", head, 600, 200);
    pointer("pointermove", document, 520, 140);
    await nextTick();
    expect(box(wrapper).left).not.toBe(before.left);
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await nextTick();
    expect(box(wrapper), "Esc 之后没有恢复").toEqual(before);

    pointer("pointerdown", head, 600, 200);
    pointer("pointermove", document, 520, 140);
    await nextTick();
    window.dispatchEvent(new Event("blur"));
    await nextTick();
    expect(box(wrapper), "失焦之后没有恢复").toEqual(before);
    expect(loadChatFrame(interactive.boardId)).toBeNull();
  });

  it("正文区不参与移动：在消息流上按指针不会动窗口", async () => {
    const { wrapper, interactive } = mountDock();
    const before = box(wrapper);
    const stream = wrapper.find('[data-im="chat-stream"]').element;

    pointer("pointerdown", stream, 600, 300);
    pointer("pointermove", document, 400, 120);
    pointer("pointerup", document, 400, 120);
    await nextTick();

    expect(box(wrapper)).toEqual(before);
    expect(loadChatFrame(interactive.boardId)).toBeNull();
  });
});

describe("只改查看：拖动与调整尺寸不写板面状态、不发请求", () => {
  it("调整尺寸 + 移动之后：store.commit / session.send 一次都没有被调用", async () => {
    const { wrapper, interactive, session } = mountDock();
    const commit = vi.spyOn(interactive, "commit");
    const send = vi.spyOn(session, "send");
    const saveNow = vi.spyOn(interactive, "saveNow");

    const handle = wrapper.find('[data-resize-handle="se"]').element;
    pointer("pointerdown", handle, 200, 200);
    pointer("pointermove", document, 280, 260);
    pointer("pointerup", document, 280, 260);

    const head = wrapper.find('[data-im="chat-head"]').element;
    pointer("pointerdown", head, 600, 200);
    pointer("pointermove", document, 560, 170);
    pointer("pointerup", document, 560, 170);
    await nextTick();

    expect(commit, "调整窗口尺寸不能写板面状态").not.toHaveBeenCalled();
    expect(saveNow).not.toHaveBeenCalled();
    expect(send, "调整窗口尺寸不能发消息 / 请求").not.toHaveBeenCalled();
    expect(loadChatFrame(interactive.boardId), "这一次调整应该只落本机偏好").not.toBeNull();
  });
});

describe("持久化：按板面身份绑定，恢复绝不回到屏幕外", () => {
  it("超屏 + 超大 + 小到不可用的存档都收敛回可见区", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const interactive = useInteractiveStore();
    saveChatFrame(interactive.boardId, { x: 5000, y: 5000, w: 3000, h: 2000 });
    const wrapper = mount(ChatDock, { global: { plugins: [pinia] }, attachTo: document.body });
    wrappers.push(wrapper);

    const frame = box(wrapper);
    expect(frame.width).toBeLessThanOrEqual(window.innerWidth);
    expect(frame.height).toBeLessThanOrEqual(window.innerHeight);
    expect(frame.left).toBeGreaterThanOrEqual(0);
    expect(frame.top).toBeGreaterThanOrEqual(0);
    expect(frame.left + frame.width).toBeLessThanOrEqual(window.innerWidth);
    expect(frame.top + frame.height).toBeLessThanOrEqual(window.innerHeight);
  });

  it("换板面各恢复各的：不在同一个键上互相覆盖", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const interactive = useInteractiveStore();
    saveChatFrame("board_a", { x: 20, y: 20, w: 320, h: 260 });
    saveChatFrame("board_b", { x: 60, y: 60, w: 360, h: 300 });
    interactive.boardId = "board_a";
    const wrapper = mount(ChatDock, { global: { plugins: [pinia] }, attachTo: document.body });
    wrappers.push(wrapper);
    expect(box(wrapper)).toMatchObject({ left: 20, top: 20, width: 320, height: 260 });

    interactive.boardId = "board_b";
    await nextTick();
    expect(box(wrapper)).toMatchObject({ left: 60, top: 60, width: 360, height: 300 });
  });
});

describe("收起 / 展开：草稿、光标、阅读位置都留在原处", () => {
  it("收起再展开：消息、草稿、光标与上翻位置都还在", async () => {
    const { wrapper, interactive, session } = mountDock();
    await wrapper.find('[data-im="chat-input"]').setValue("还没发出去的草稿");
    const input = wrapper.find('[data-im="chat-input"]').element as HTMLTextAreaElement;
    input.setSelectionRange(2, 4);

    // 模拟一次「用户上翻阅读」：先解除程序化贴底的保护，再让滚动位置生效
    const stream = wrapper.find('[data-im="chat-stream"]').element as HTMLElement;
    Object.defineProperty(stream, "scrollHeight", { value: 1000, configurable: true });
    Object.defineProperty(stream, "clientHeight", { value: 200, configurable: true });
    stream.scrollTop = 300;
    stream.dispatchEvent(new WheelEvent("wheel", { bubbles: true }));
    stream.dispatchEvent(new Event("scroll"));
    await nextTick();
    expect(wrapper.find('[data-im="chat-latest"]').exists(), "上翻后应出现「回到最新」").toBe(true);

    interactive.chatOpen = false;
    await nextTick();
    expect(wrapper.find('[data-im="chat-panel"]').exists()).toBe(false);
    expect(session.draft, "收起不能丢草稿").toBe("还没发出去的草稿");

    interactive.chatOpen = true;
    await nextTick();
    await nextTick();
    const reopened = wrapper.find('[data-im="chat-input"]').element as HTMLTextAreaElement;
    expect(reopened.value, "重新展开后草稿不在输入框里").toBe("还没发出去的草稿");
    expect([reopened.selectionStart, reopened.selectionEnd], "光标位置没有恢复").toEqual([2, 4]);
    const reopenedStream = wrapper.find('[data-im="chat-stream"]').element as HTMLElement;
    Object.defineProperty(reopenedStream, "scrollHeight", { value: 1000, configurable: true });
    Object.defineProperty(reopenedStream, "clientHeight", { value: 200, configurable: true });
    expect(reopenedStream.scrollTop, "上翻的阅读位置没有恢复").toBe(300);
  });

  it("入口按钮在展开与收起两种状态下都在，收起时就地留在原处", async () => {
    const { wrapper, interactive } = mountDock();
    expect(wrapper.find('[data-im="chat-toggle"]').exists()).toBe(true);
    expect(box(wrapper)).toEqual(box(wrapper));
    const before = box(wrapper);
    interactive.chatOpen = false;
    await nextTick();
    expect(wrapper.find('[data-im="chat-toggle"]').exists()).toBe(true);
    expect(box(wrapper), "收起不该改变窗口的位置").toEqual(before);
    expect(wrapper.find('[data-im="chat-panel"]').exists()).toBe(false);
  });
});

describe("结构：顶部精简、底部一个输入区、帮助可开可关", () => {
  it("顶部只保留标题 / 状态 / 尺寸入口 / 帮助 / 收起", async () => {
    const { wrapper } = mountDock();
    const head = wrapper.find('[data-im="chat-head"]');
    expect(head.find('.panel-title').text()).toBe("对话");
    expect(head.find('[data-im="chat-status"]').text().length).toBeGreaterThan(0);
    expect(head.find('[data-im="chat-help-toggle"]').exists()).toBe(true);
    expect(head.find('[data-im="chat-collapse"]').exists()).toBe(true);
    // 尺寸入口：键盘 −/＋ 就在标题栏里（另一组是盖住面板的拖动热区）
    expect(head.find('[data-resize-key="w-"]').exists()).toBe(true);
    expect(head.find('[data-resize-key="h+"]').exists()).toBe(true);
    expect(wrapper.findAll('[data-resize-handle]').length).toBe(8);
  });

  it("帮助：默认收起，点「帮助」打开后有可见范围 / 板面手势 / 媒体读取 / 附件限制 / 快捷键，再点收起", async () => {
    const { wrapper } = mountDock();
    const scope = wrapper.find('[data-im="chat-scope"]');
    expect(scope.exists(), "帮助入口不在").toBe(true);
    expect(scope.find("details").exists(), "帮助必须是可打开的结构").toBe(true);
    const details = scope.find("details").element as HTMLDetailsElement;
    expect(details.open).toBe(false);

    const body = scope.text();
    expect(body, "缺少「发送不提交板面」的短提示").toContain("文字发送不会提交板面");
    expect(body, "缺少完整可见范围说明").toContain(chatScopeText());
    expect(body, "缺少媒体读取说明").toContain(MEDIA_CAPABILITY_HINT.image);
    expect(body, "缺少附件限制").toContain(humanSize(COPY_MAX_BYTES));
    expect(body, "缺少快捷键说明").toContain("Enter 发送");
    expect(body, "缺少草稿说明").toContain("不会清掉草稿");
    // 板面顶部那条常驻提示已改成「没用过查看手势时显示一次」：
    // 完整说明的唯一常驻处就是这里，缺了它就等于说明丢了
    expect(body, "缺少板面手势说明").toContain("板面手势");
    expect(body, "板面手势缺了框选那一句").toContain("按住空格");
    expect(body, "板面手势缺了缩放那一句").toContain("缩放中心");

    await wrapper.find('[data-im="chat-help-toggle"]').trigger("click");
    await nextTick();
    expect((wrapper.find('[data-im="chat-scope"] details').element as HTMLDetailsElement).open).toBe(true);
    await wrapper.find('[data-im="chat-help-toggle"]').trigger("click");
    await nextTick();
    expect((wrapper.find('[data-im="chat-scope"] details').element as HTMLDetailsElement).open).toBe(false);
  });

  it("常驻说明不再出现：没有 submitNote / understandingNote 的长说明块", async () => {
    const { wrapper, interactive } = mountDock();
    interactive.submitNote = "已受理：QIO 已经可靠收下这次提交，正在理解。";
    interactive.understandingNote = "这次理解还在进行中，请稍候。";
    await nextTick();
    expect(wrapper.find('[data-im="chat-process-note"]').exists()).toBe(false);
    expect(wrapper.find('[data-im="chat-acceptance-note"]').exists()).toBe(false);
    expect(wrapper.text(), "受理长说明应该归提交入口").not.toContain("可靠收下这次提交");
  });
});

describe("过程：默认只留一句极简状态", () => {
  it("没有理解状态时不占位；正在提交时只显示一句，不显示受理长说明", async () => {
    const { wrapper, interactive } = mountDock();
    expect(wrapper.find('[data-im="chat-process"]').exists(), "没有过程事实时不该占位").toBe(false);

    interactive.submitStatus = "submitting";
    interactive.submitNote = "已受理：QIO 已经可靠收下这次提交，正在理解。";
    await nextTick();
    const block = wrapper.find('[data-im="chat-process"]');
    expect(block.exists()).toBe(true);
    expect(block.find('[data-im="chat-process-status"]').text()).toContain("正在提交");
    expect(block.text(), "受理长说明应该归提交入口").not.toContain("可靠收下这次提交");
  });
});

describe("附件条：没有附件就不占位", () => {
  it("未选择附件：不渲染附件条；选定之后才出现，移除后消失", async () => {
    const { wrapper } = mountDock();
    expect(wrapper.find('[data-im="chat-attach-strip"]').exists()).toBe(false);
    expect(wrapper.find('[data-im="chat-attachment"]').exists()).toBe(false);
    expect(wrapper.find('[data-im="chat-attach-error"]').exists()).toBe(false);

    mocks.pickBrowserFile.mockResolvedValue(new File(["x"], "季度报告.pdf"));
    mocks.uploadAttachment.mockResolvedValue(attachment({ id: "att_ok", name: "季度报告.pdf" }));
    await wrapper.find('[data-im="chat-attach-file-btn"]').trigger("click");
    await flushPromises();
    expect(wrapper.find('[data-im="chat-attach-strip"]').exists(), "有附件却没有附件条").toBe(true);
    expect(wrapper.find('[data-im="chat-attachment"]').text()).toContain("季度报告.pdf");

    await wrapper.find('[data-im="chat-attachment-remove"]').trigger("click");
    await flushPromises();
    expect(wrapper.find('[data-im="chat-attach-strip"]').exists(), "移除之后附件条还在占位").toBe(false);
  });
});

describe("发布显示框：供审批托盘与展开卡片避让", () => {
  it("挂载 / 预览 / 收起 / 卸载各发布一次，收起时标记为收起", async () => {
    const { wrapper, interactive } = mountDock();
    const mounted = box(wrapper);
    expect(liveChatFrame.value, "挂载后没有发布显示框").toMatchObject({
      x: mounted.left,
      y: mounted.top,
      w: mounted.width,
      h: mounted.height,
    });
    expect(liveChatCollapsed.value).toBe(false);

    const handle = wrapper.find('[data-resize-handle="se"]').element;
    pointer("pointerdown", handle, 200, 200);
    pointer("pointermove", document, 260, 240);
    await nextTick();
    expect(liveChatFrame.value?.w, "拖动预览没有发布").toBe(mounted.width + 60);
    pointer("pointerup", document, 260, 240);
    await nextTick();

    interactive.chatOpen = false;
    await nextTick();
    expect(liveChatCollapsed.value, "收起后没有标记收起（别人会按它避让一块不存在的空间）").toBe(true);

    wrapper.unmount();
    expect(liveChatFrame.value, "卸载后留下了幽灵矩形").toBeNull();
  });
});

/* ---------------------------------------------------------------------------
 * 全局滚动 / 横向溢出：面板里只保留一层主要纵向滚动，且任何层级都没有横向滚动
 * ------------------------------------------------------------------------- */

/** 取出某个选择器的规则块（第一个匹配）；用来证明旧的「限高小滚动区」真的没有了 */
function cssBlock(source: string, selector: string): string {
  const start = source.indexOf(selector);
  expect(start, "找不到选择器 " + selector).toBeGreaterThan(-1);
  const open = source.indexOf("{", start);
  const close = source.indexOf("}", open);
  return source.slice(open + 1, close);
}

/** 等一帧：共享滚动引擎把测量合并到一帧一次（rAF），断言前必须让它跑完 */
function nextFrame(): Promise<void> {
  return new Promise((resolve) => {
    requestAnimationFrame(() => resolve());
  });
}

describe("横向：面板内不存在横向滚动容器", () => {
  it("面板根声明 chat 作用域，关键容器都带 data-qio-fit 边界保护", async () => {
    const { wrapper, interactive } = mountDock();
    const panel = wrapper.find('[data-im="chat-panel"]');
    expect(panel.attributes("data-qio-scope"), "面板根没有声明对话作用域").toBe("chat");
    expect(panel.attributes("data-qio-fit"), "面板根没有横向边界保护").toBeDefined();

    // 过程块要有真实事实才出现：给它一次「正在提交」
    interactive.submitStatus = "submitting";
    await nextTick();

    for (const hook of ["chat-stream", "chat-composer", "chat-process", "chat-notices", "chat-scope"]) {
      const el = wrapper.find('[data-im="' + hook + '"]');
      expect(el.exists(), hook + " 不在面板里").toBe(true);
      expect(el.attributes("data-qio-fit"), hook + " 缺 data-qio-fit（横向边界保护）").toBeDefined();
    }
  });

  it("源码里没有横向滚动声明，四个旧的小滚动区已收敛为 content", () => {
    const source = readFileSync(
      resolve(process.cwd(), "src/components/interactive/ChatDock.vue"),
      "utf8",
    );
    // 修的不是「把横向滚动条藏起来」：整份文件里一个横向滚动容器都不许有
    expect(source, "ChatDock 里出现了横向滚动容器").not.toMatch(/overflow-x:\s*(auto|scroll)/);
    expect(source, "ChatDock 里出现了 auto/scroll 的 overflow 简写").not.toMatch(/overflow:\s*(auto|scroll)\b/);
    /*
      旧口径：这些区域各自「限高 + 自己滚一层」，同一段阅读流被切成多个小滚动窗口。
      新口径是 content：不设 max-height、不建 overflow 容器，内容自然展开 + 折叠/展开入口。
    */
    for (const selector of [".panel-notices {", ".process {", ".attach-list {", ".help-body {"]) {
      const body = cssBlock(source, selector);
      expect(body, selector + " 仍然是「限高 + 自己滚一层」的小滚动区").not.toMatch(/(max-height|overflow)/);
    }
  });

  it("消息、过程、提示同属一条阅读流：.stream 是唯一的主要纵向滚动视口", async () => {
    const { wrapper, interactive } = mountDock();
    interactive.submitStatus = "submitting";
    await nextTick();

    const stream = wrapper.find('[data-im="chat-stream"]');
    // 过程与提示就在消息流**里面**（不是面板里另开的滚动窗口）
    expect(stream.find('[data-im="chat-process"]').exists(), "过程块没有并进阅读流").toBe(true);
    expect(stream.find('[data-im="chat-notices"]').exists(), "提示块没有并进阅读流").toBe(true);
    // 共享滚动指令真的接上了：宿主拿到宿主类与 policy
    expect(stream.classes(), "消息流没有接上共享滚动指示条").toContain("qio-scroll-host");
    expect(stream.attributes("data-qio-scroll")).toBe("auto");

    // 制造真实溢出：纵向溢出被识别；横向内容更宽也不会建出可滚动范围（只声明 y 轴）
    const el = stream.element as HTMLElement;
    Object.defineProperty(el, "scrollHeight", { value: 1200, configurable: true });
    Object.defineProperty(el, "clientHeight", { value: 200, configurable: true });
    Object.defineProperty(el, "scrollWidth", { value: 900, configurable: true });
    Object.defineProperty(el, "clientWidth", { value: 300, configurable: true });
    el.dispatchEvent(new Event("scroll"));
    await nextTick();
    await nextFrame();

    expect(stream.attributes("data-qio-range"), "纵向溢出没有被识别").toBe("y");
    expect(stream.findAll('.qio-scroll-rail[data-axis="x"]').length, "面板里出现了横向指示条").toBe(0);
    expect(stream.findAll('.qio-scroll-rail[data-axis="y"]').length).toBe(1);
  });
});

describe("提示：默认「简述 + 展开入口」，完整原文按需展开", () => {
  it("长会话警告默认只给简述与「展开全文」，展开后是完整原文", async () => {
    const { wrapper, session } = mountDock();
    const long = "还没有配置可用的模型凭据：" + "请先在设置里填写后重试。".repeat(6);
    session.warning = long;
    await nextTick();

    const warn = wrapper.find('[data-im="chat-warning"]');
    expect(warn.exists(), "警告没有显示").toBe(true);
    expect(warn.text(), "长警告默认没有收敛成简述").not.toContain(long);
    expect(warn.text().length, "简述不该是空的").toBeGreaterThan(0);

    const toggle = wrapper.find('[data-im="chat-warning-toggle"]');
    expect(toggle.exists(), "长警告没有展开入口").toBe(true);
    expect(toggle.attributes("aria-expanded")).toBe("false");

    await toggle.trigger("click");
    await nextTick();
    expect(wrapper.find('[data-im="chat-warning"]').text(), "展开后看不到完整原文").toContain(long);
    expect(wrapper.find('[data-im="chat-warning-toggle"]').attributes("aria-expanded")).toBe("true");
  });

  it("短警告不出现多余的展开入口（不为了入口而入口）", async () => {
    const { wrapper, session } = mountDock();
    session.warning = "还没有配置可用的模型凭据。";
    await nextTick();
    expect(wrapper.find('[data-im="chat-warning"]').text()).toBe("还没有配置可用的模型凭据。");
    expect(wrapper.find('[data-im="chat-warning-toggle"]').exists()).toBe(false);
  });

  it("待审批条目不需要展开过程详情就可见（不藏掉审批入口）", async () => {
    const { wrapper, interactive } = mountDock();
    interactive.submitStatus = "submitting";
    interactive.acceptedUnderstandingId = "u1";
    interactive.intents = [
      {
        id: "i1",
        boardId: "b1",
        submissionId: null,
        title: "整理季度材料",
        summary: "",
        status: "pending",
        preview: {},
        impact: {},
        dependsOn: [],
        conflictsWith: [],
        conflictKey: "",
        materialRefs: [],
        progress: { done: 0, total: 0, text: "" },
        reason: "",
        demo: false,
        understandingId: "u1",
        createdAt: "",
        updatedAt: "",
      },
    ] as never;
    await nextTick();

    // 没有点开「过程详情」（processOpen 默认 false）：审批入口与批准 / 拒绝按钮就已经在
    const approval = wrapper.find('[data-im="chat-approval"]');
    expect(approval.exists(), "待审批条目没有出现").toBe(true);
    expect(approval.text(), "待审批条目不显示任务标题").toContain("整理季度材料");
    expect(wrapper.find('[data-im="chat-approve"]').exists(), "批准入口被藏掉了").toBe(true);
    expect(wrapper.find('[data-im="chat-reject"]').exists(), "拒绝入口被藏掉了").toBe(true);
  });

  it("轮次错误始终完整可见（重要错误不因为「默认简述」被藏起来）", async () => {
    const { wrapper, session } = mountDock();
    const err = "后端没有可用的模型凭据：请在设置里填写后再试。".repeat(4);
    session.lastError = err;
    await nextTick();
    const block = wrapper.find('[data-im="chat-turn-error"]');
    expect(block.exists(), "轮次错误没有显示").toBe(true);
    expect(block.text(), "轮次错误被截断/折叠了").toContain(err);
  });
});

/* ---------------------------------------------------------------------------
 * 上翻阅读时的「一步可达」：固定入口说清下面还有什么
 *
 * 过程与提示并进消息流之后（同一阅读流只留一层滚动），它们会随内容滚出视野。
 * 因此固定入口必须把「下面还有待处理项」说出来 —— 简化滚动不允许变成藏掉审批或重要错误。
 * ------------------------------------------------------------------------- */

/** 造一条等待审批的真实意图（判定口径与 store.understandingIntents 一致） */
function pendingIntentFixture(id = "i1", title = "整理季度材料") {
  return {
    id,
    boardId: "b1",
    submissionId: null,
    title,
    summary: "",
    status: "pending",
    preview: {},
    impact: {},
    dependsOn: [],
    conflictsWith: [],
    conflictKey: "",
    materialRefs: [],
    progress: { done: 0, total: 0, text: "" },
    reason: "",
    demo: false,
    understandingId: "u1",
    createdAt: "",
    updatedAt: "",
  } as never;
}

/** 让消息流真的有可滚范围，并模拟一次「用户自己上翻」（滚轮先到，解除程序化贴底的保护） */
async function scrollUp(wrapper: VueWrapper, scrollHeight = 1600, clientHeight = 300): Promise<HTMLElement> {
  // 挂载期那次「程序化贴底」（onMounted 里的 await scrollToBottom）必须先跑完：
  // 它会把 following 设回 true，跑在模拟上翻之后就会把这次上翻抹掉。
  await flushPromises();
  const stream = wrapper.find('[data-im="chat-stream"]').element as HTMLElement;
  Object.defineProperty(stream, "scrollHeight", { value: scrollHeight, configurable: true });
  Object.defineProperty(stream, "clientHeight", { value: clientHeight, configurable: true });
  stream.scrollTop = 0;
  stream.dispatchEvent(new WheelEvent("wheel", { bubbles: true }));
  stream.dispatchEvent(new Event("scroll"));
  await nextTick();
  return stream;
}

describe("上翻时新出现的待审批 / 错误：固定入口一步可达", () => {
  it("已上翻 → 新的待审批出现：入口在滚动内容之外、写明「待审批 N 项」、一次点击回到最新", async () => {
    const { wrapper, interactive } = mountDock();
    const stream = await scrollUp(wrapper);
    expect(wrapper.find('[data-im="chat-latest"]').exists(), "上翻后没有「回到最新」入口").toBe(true);

    // 用户还在上面读的时候，来了新的待审批
    interactive.submitStatus = "submitting";
    interactive.acceptedUnderstandingId = "u1";
    interactive.intents = [pendingIntentFixture()];
    await nextTick();

    const latest = wrapper.find('[data-im="chat-latest"]');
    expect(latest.exists(), "有待审批却看不到固定入口").toBe(true);
    // 钉在滚动内容之外：不会被内容滚走（这正是「一步可达」成立的前提）
    expect(
      wrapper.find('[data-im="chat-stream"]').find('[data-im="chat-latest"]').exists(),
      "「回到最新」跑进了滚动内容，会被滚走",
    ).toBe(false);

    // 文案说清下面还有什么（不是一句含糊的「回到最新」）
    const attention = latest.find('[data-im="chat-latest-attention"]');
    expect(attention.exists(), "入口没有说明下面有待处理项").toBe(true);
    expect(attention.text()).toContain("待审批 1 项");
    expect(latest.attributes("title")).toContain("待审批 1 项");

    // 一步可达：点一次就回到最新，审批入口随之进入视野（它是阅读流的最后一段）
    await latest.trigger("click");
    await nextTick();
    await nextTick();
    expect(stream.scrollTop, "点击后没有回到最新").toBe(1600);
    expect(wrapper.find('[data-im="chat-latest"]').exists(), "回到最新后入口应该收起").toBe(false);
    expect(wrapper.find('[data-im="chat-approval"]').exists()).toBe(true);
    expect(wrapper.find('[data-im="chat-approve"]').exists(), "回到最新后批准入口不可达").toBe(true);
  });

  it("已上翻 → 新的轮次错误出现：固定入口写明「有错误待查看」", async () => {
    const { wrapper, session } = mountDock();
    await scrollUp(wrapper);
    session.lastError = "后端没有可用的模型凭据：请在设置里填写后再试。";
    await nextTick();

    const latest = wrapper.find('[data-im="chat-latest"]');
    expect(latest.exists(), "有错误却看不到固定入口").toBe(true);
    const attention = latest.find('[data-im="chat-latest-attention"]');
    expect(attention.exists(), "有错误却没有提示").toBe(true);
    expect(attention.text()).toContain("有错误待查看");

    await latest.trigger("click");
    await nextTick();
    await nextTick();
    expect(wrapper.find('[data-im="chat-turn-error"]').exists(), "回到最新后错误块不可达").toBe(true);
  });

  it("没有待处理项、又在跟随时：不显示这个入口（不制造多余提示）", async () => {
    const { wrapper } = mountDock();
    expect(wrapper.find('[data-im="chat-latest"]').exists()).toBe(false);
  });
});

/* ---------------------------------------------------------------------------
 * task-7 复验：FailedSendNotice 收敛为 content 之后，面板阅读流只剩一层主要纵向滚动
 * ------------------------------------------------------------------------- */

/** 去掉 CSS 注释：收敛后的注释里会引用旧写法（「原先 max-height:…」），不能当声明读 */
function stripCssComments(text: string): string {
  return text.replace(/\/\*[\s\S]*?\*\//g, "");
}

/** 规则块里「自建滚动层」的声明：限高，或 overflow(-x/-y): auto|scroll|hidden */
function scrollLayerDeclarations(body: string): string[] {
  const decls =
    stripCssComments(body).match(/(?:^|[\s;{])(max-height|overflow|overflow-x|overflow-y)\s*:\s*([^;}]+)/g) ?? [];
  return decls.filter((decl) => {
    if (/^[\s;{]*max-height/.test(decl)) return true;
    const value = decl.slice(decl.indexOf(":") + 1);
    return /(^|[\s(])(auto|scroll|hidden)([\s)]|$)/.test(value.trim());
  });
}

describe("task-7 复验：失败恢复收敛后，阅读流只剩一层主要纵向滚动", () => {
  const LONG_FAIL = "这段文字没有发出去，原文必须完整保留：".repeat(12);

  it("长失败恢复 + 12 条待审批同时在面板里：滚动宿主恰好两个（.stream auto + .composer hidden）", async () => {
    const { wrapper, interactive, session } = mountDock();
    // 长失败恢复：一条 12 遍重复的长原文 + 真实原因
    session.failedSends = [{ id: "f1", draftId: "d1", topicId: "A", text: LONG_FAIL }] as never;
    session.failedSendErrors = { f1: "网络中断：连接在受理之前就断了。" } as never;
    // 长过程内容：12 条等待审批的真实条目
    interactive.submitStatus = "submitting";
    interactive.acceptedUnderstandingId = "u1";
    interactive.intents = Array.from({ length: 12 }, (_, i) =>
      pendingIntentFixture("i" + i, "待审批任务 " + (i + 1)),
    );
    await nextTick();

    const panel = wrapper.find('[data-im="chat-panel"]');
    // (a) 面板内 [data-qio-scroll] 容器数量与预期一致
    const hosts = panel.findAll("[data-qio-scroll]");
    expect(hosts.length, "面板里出现了预期之外的滚动层").toBe(2);
    expect(hosts.map((h) => h.attributes("data-qio-scroll"))).toEqual(["auto", "hidden"]);
    expect(panel.find('[data-im="chat-stream"]').attributes("data-qio-scroll")).toBe("auto");
    expect(panel.find('[data-im="chat-composer"]').attributes("data-qio-scroll")).toBe("hidden");

    // 失败恢复就在这一层阅读流里：自身不再开滚动层、也没有行内限高/裁剪
    const stream = panel.find('[data-im="chat-stream"]');
    const recovery = stream.find('[data-im="chat-recovery-wrap"]');
    expect(recovery.exists(), "失败恢复不在阅读流里").toBe(true);
    expect(recovery.findAll("[data-qio-scroll]").length, "失败恢复自带滚动层").toBe(0);
    expect(recovery.findAll(".qio-scroll-host").length, "失败恢复自建滚动宿主").toBe(0);
    expect(recovery.attributes("style") ?? "", "失败恢复被套了行内限高").not.toMatch(/max-height|overflow/);
    expect(
      stream.find('[data-im="chat-recovery"]').attributes("style") ?? "",
      "恢复块被套了行内限高",
    ).not.toMatch(/max-height|overflow/);

    // 长原文一字不少地留在 DOM 里（收敛滚动 ≠ 截断内容），恢复操作仍然可达
    expect(stream.find('[data-im="chat-recovery-quote"]').text()).toBe(LONG_FAIL);
    expect(stream.find('[data-im="chat-recovery-restore"]').exists(), "恢复操作不可达").toBe(true);
    expect(stream.find('[data-im="chat-recovery-discard"]').exists()).toBe(true);

    // 长过程内容（12 条待审批）也在同一条流里，批准入口可达
    expect(stream.findAll('[data-im="chat-approval-item"]').length).toBe(12);
    expect(stream.find('[data-im="chat-approve"]').exists()).toBe(true);
  });

  it("(b) FailedSendNotice 四个区域不再是「限高 + 自己滚一层」（源码守卫；注释不算声明）", () => {
    const source = readFileSync(
      resolve(process.cwd(), "src/components/interactive/FailedSendNotice.vue"),
      "utf8",
    );
    for (const selector of [".failed-send {", ".recovery {", ".pending {", ".quote {"]) {
      const found = scrollLayerDeclarations(cssBlock(source, selector));
      expect(found, selector + " 仍然是「限高 + 自己滚一层」：" + found.join(" / ")).toEqual([]);
    }
  });
});

describe("输入区 / 纯裁剪盒子：策略与滚动语义", () => {
  it("输入区用 hidden 策略：保留滚动能力、始终不画指示条", async () => {
    const { wrapper } = mountDock();
    const composer = wrapper.find('[data-im="chat-composer"]');
    expect(composer.classes(), "输入区没有接上共享滚动宿主").toContain("qio-scroll-host");
    expect(composer.attributes("data-qio-scroll"), "输入区策略不是 hidden").toBe("hidden");
    // hidden：不测量、不建轨道 —— 一条指示条都没有
    expect(composer.findAll(".qio-scroll-rail").length, "输入区画出了指示条").toBe(0);

    // 但滚动与定位能力保留：真实溢出时 scrollTop 仍然可写（长输入到上限后靠它滚）
    const el = composer.element as HTMLElement;
    Object.defineProperty(el, "scrollHeight", { value: 900, configurable: true });
    Object.defineProperty(el, "clientHeight", { value: 200, configurable: true });
    el.scrollTop = 120;
    expect(el.scrollTop).toBe(120);
    expect(composer.findAll(".qio-scroll-rail").length).toBe(0);
  });

  it("分工明确：消息流 auto（滚动时才画），输入区 hidden（始终不画）", async () => {
    const { wrapper } = mountDock();
    expect(wrapper.find('[data-im="chat-stream"]').attributes("data-qio-scroll")).toBe("auto");
    expect(wrapper.find('[data-im="chat-composer"]').attributes("data-qio-scroll")).toBe("hidden");
  });

  it("输入框：空内容不留高度 / 滚动范围，到上限只在自己内部纵向滚", async () => {
    const { wrapper } = mountDock();
    const input = wrapper.find('[data-im="chat-input"]').element as HTMLTextAreaElement;

    await wrapper.find('[data-im="chat-input"]').setValue("");
    expect(input.style.height, "空内容不该留出滚动范围").toBe("");

    // jsdom 没有排版：直接给它一个超过上限的真实内容高度
    Object.defineProperty(input, "scrollHeight", { value: 900, configurable: true });
    await wrapper.find('[data-im="chat-input"]').setValue("很长的一段输入\n".repeat(40));
    const height = Number.parseFloat(input.style.height || "0");
    expect(height, "长输入没有真的长到上限").toBeGreaterThan(0);
    expect(height, "长输入没有按上限收敛（会一直撑高面板）").toBeLessThanOrEqual(132);

    // 输入框自己不声明任何 overflow / 滚动条样式：外观由全局唯一规则消除，纵向滚动用浏览器默认
    const source = readFileSync(resolve(process.cwd(), "src/components/interactive/ChatDock.vue"), "utf8");
    const block = cssBlock(source, ".input {");
    expect(block, "输入框自造了 overflow / 滚动条样式").not.toMatch(/overflow|scrollbar/);
  });

  it("纯裁剪盒子一律用 clip：ChatDock 里不再有 overflow: hidden", () => {
    const source = readFileSync(resolve(process.cwd(), "src/components/interactive/ChatDock.vue"), "utf8");
    // hidden 仍建立「可程序性滚动」的容器（scrollLeft 能改、focus / scrollIntoView 能滚走）
    expect(source, "还有 overflow: hidden 的纯裁剪盒子").not.toMatch(/overflow(-[xy])?:\s*hidden/);
    // 输入区的策略在源码里就是 hidden（防止后人改回 auto）
    expect(source, "输入区的策略不是 hidden").toMatch(/class="composer"[\s\S]{0,240}policy: 'hidden'/);
  });
});
