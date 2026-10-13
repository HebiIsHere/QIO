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

  it("帮助：默认收起，点「帮助」打开后有可见范围 / 媒体读取 / 附件限制 / 快捷键，再点收起", async () => {
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
