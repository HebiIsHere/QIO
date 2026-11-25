/**
 * 底部互动对话区（Phase 3-B / 2026-11 底部操作区重做）。
 *
 * 这里验的是**组件层真的接上了**，children 用轻量替身（MessageStream / Composer / ComposerEntries
 * 自己的行为由它们的既有测试负责）：
 * - 收起：轻量操作行（模式切换 / 常驻把手 / 板面工具槽）+ 胶囊输入区；
 * - 展开：消息区在操作行上方长出，同一根把手留在面板顶边，顶边线框与玻璃出现；
 * - 收起只改显示：不停止回复、不清草稿/附件、不动排队；
 * - 板面操作自动收起，但不打断回复；
 * - 高度拖动 / 键盘 / 记忆口径不变；
 * - 互动绑定由 props 可信传入并投影成发送选项（不拼进用户原文）。
 */
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { defineComponent, h, nextTick } from "vue";
import InteractiveConversation from "../InteractiveConversation.vue";
import { useInteractiveStore } from "../../../../stores/interactive";
import { useSessionStore } from "../../../../stores/session";
import {
  CONVERSATION_DEFAULT_HEIGHT,
  CONVERSATION_MAX_HEIGHT,
  CONVERSATION_MIN_HEIGHT,
  clampConversationHeight,
} from "../conversationLayout";

const StreamStub = defineComponent({
  name: "MessageStream",
  setup() {
    return () => h("div", { "data-stub": "stream" });
  },
});

const EntriesStub = defineComponent({
  name: "ComposerEntries",
  setup() {
    return () => h("div", { "data-stub": "entries" });
  },
});

const ComposerStub = defineComponent({
  name: "Composer",
  props: { chatBinding: { type: Object, default: undefined } },
  setup(props, { slots }) {
    return () =>
      h(
        "div",
        {
          "data-stub": "composer",
          "data-binding": props.chatBinding ? JSON.stringify(props.chatBinding) : "",
        },
        [slots.leading?.()],
      );
  },
});

let wrapper: VueWrapper | null = null;

function mountConversation(
  props: Record<string, unknown> = {},
  slots: Record<string, string> = {},
) {
  const pinia = createPinia();
  setActivePinia(pinia);
  const interactive = useInteractiveStore();
  const session = useSessionStore();
  session.currentTopicId = "A";
  wrapper = mount(InteractiveConversation, {
    props,
    slots,
    global: {
      plugins: [pinia],
      stubs: { MessageStream: StreamStub, ComposerEntries: EntriesStub, Composer: ComposerStub },
    },
    attachTo: document.body,
  });
  return { wrapper, interactive, session };
}

function pointer(type: string, x: number, y: number) {
  return new MouseEvent(type, { clientX: x, clientY: y, button: 0, bubbles: true, cancelable: true });
}

function pressEscape() {
  document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }));
}

function composerBinding(w: VueWrapper): Record<string, unknown> | null {
  const raw = w.get('[data-stub="composer"]').attributes("data-binding") ?? "";
  return raw ? (JSON.parse(raw) as Record<string, unknown>) : null;
}

/** 最近一次 emit 的第一个参数（不用 Array.prototype.at：项目 lib 目标是 ES2021）。 */
function lastEmitted(w: VueWrapper, event: string): unknown {
  const list = w.emitted(event) as unknown[][] | undefined;
  const last = list && list.length ? list[list.length - 1] : undefined;
  return last ? last[0] : undefined;
}

function panelVar(w: VueWrapper): string {
  const style = w.get('[data-ic="conversation"]').attributes("style") ?? "";
  return /--ic-panel-h:\s*([^;]+)/.exec(style)?.[1]?.trim() ?? "";
}

beforeEach(() => {
  localStorage.clear();
});

afterEach(() => {
  wrapper?.unmount();
  wrapper = null;
  document.body.innerHTML = "";
});

describe("收起 / 展开：轻量操作行 + 常驻把手 + 胶囊输入", () => {
  it("默认收起：没有消息区；操作行里有唯一的常驻把手，输入区与入口行始终挂载", () => {
    const { wrapper: w } = mountConversation();
    expect(w.get('[data-ic="conversation"]').attributes("data-ic-expanded")).toBe("false");
    expect(w.find('[data-stub="stream"]').exists()).toBe(false);
    // 同一根把手：开合 + 调高合成一个控件（不再有分开的收起按钮 / 分隔条）
    expect(w.findAll('[data-ic-toggle]').length).toBe(1);
    const lever = w.get('[data-ic-toggle]');
    expect(lever.attributes("data-ic-resize")).toBeDefined();
    expect(lever.attributes("aria-expanded")).toBe("false");
    expect(lever.attributes("aria-label")).toBe("展开对话");
    expect(w.find('[data-ic-close]').exists()).toBe(false);
    expect(w.find('[data-stub="composer"]').exists()).toBe(true);
    expect(w.find('[data-stub="entries"]').exists()).toBe(true);
    // 没有真实状态时状态行安静
    expect(w.find('[data-ic-status]').exists()).toBe(false);
    // 收起时线框不可见（隐藏），玻璃不可见
    expect(w.get('[data-ic-frame]').attributes("hidden")).toBeDefined();
  });

  it("点击把手展开：消息区出现，把手 aria 关系与新视觉层齐全", async () => {
    const { wrapper: w } = mountConversation();
    await w.get('[data-ic-toggle]').trigger("click");

    expect(w.emitted("update:expanded")?.[0]).toEqual([true]);
    expect(w.emitted("expand")).toHaveLength(1);
    expect(w.get('[data-ic="conversation"]').attributes("data-ic-expanded")).toBe("true");
    expect(w.find('[data-stub="stream"]').exists()).toBe(true);
    const lever = w.get('[data-ic-toggle]');
    expect(lever.attributes("aria-expanded")).toBe("true");
    expect(lever.attributes("aria-label")).toBe("收起对话");
    // aria-controls 指向真正承载会话流的元素
    expect(lever.attributes("aria-controls")).toBe(w.get('[data-ic-stream]').attributes("id"));
    // 顶边线框：两条镜像路径 + 玻璃；展开到底时揭示进度为 1、玻璃不透明。
    // jsdom 量不到真实面板宽度（几何为安全零几何），路径字符串由浏览器 E2E 逐帧核对；
    // 这里守 data 契约与玻璃透明度。
    expect(w.get('[data-ic="conversation"]').attributes("data-ic-reveal")).toBe("1.0000");
    expect(w.get('[data-ic-frame-path="left"]').attributes("data-ic-frame-path")).toBe("left");
    expect(w.get('[data-ic-frame-path="right"]').attributes("data-ic-frame-path")).toBe("right");
    expect(w.get('[data-ic="conversation"]').attributes("data-ic-frame-path-length")).toBeDefined();
    expect(w.get('[data-ic-glass]').attributes("style")).toMatch(/opacity:\s*1(\.0+)?/);
    // 消息区在操作行上方：DOM 顺序 = 消息区 → 操作行 → 输入区
    const html = w.html();
    expect(html.indexOf("data-ic-stream")).toBeLessThan(html.indexOf("data-ic-rail"));
    expect(html.indexOf("data-ic-rail")).toBeLessThan(html.indexOf("data-ic-composer"));
  });

  it("点击 / 聚焦输入区自动展开；同一根把手再次点击收起", async () => {
    const { wrapper: w } = mountConversation();
    await w.get(".ic-composer").trigger("focusin");
    await nextTick();
    expect(w.get('[data-ic="conversation"]').attributes("data-ic-expanded")).toBe("true");

    await w.get('[data-ic-toggle]').trigger("click");
    expect(w.get('[data-ic="conversation"]').attributes("data-ic-expanded")).toBe("false");
    expect(w.get('[data-ic-toggle]').attributes("aria-label")).toBe("展开对话");
  });

  it("展开后再次收起：会话流保留挂载但不可见（阅读位置与已渲染内容保留）", async () => {
    const { wrapper: w } = mountConversation({ expanded: true });
    expect(w.find('[data-stub="stream"]').isVisible()).toBe(true);
    await w.get('[data-ic-toggle]').trigger("click");
    expect(w.emitted("update:expanded")?.[0]).toEqual([false]);
    expect(w.emitted("collapse")).toHaveLength(1);
    expect(w.find('[data-stub="stream"]').exists()).toBe(true);
    expect(w.find('[data-stub="stream"]').isVisible()).toBe(false);
  });

  it("props.expanded 变化时跟着更新（v-model 可用）", async () => {
    const { wrapper: w } = mountConversation();
    await w.setProps({ expanded: true });
    expect(w.find('[data-stub="stream"]').exists()).toBe(true);
    await w.setProps({ expanded: false });
    expect(w.find('[data-stub="stream"]').isVisible()).toBe(false);
  });
});

describe("收起不打断回复 / 不清草稿附件 / 不丢排队", () => {
  it("回复中收起：turnRunning、草稿、排队与输入区都原样保留", async () => {
    const { wrapper: w, session } = mountConversation({ expanded: true });
    session.draft = "还没发出去的草稿";
    session.turnRunning = true;
    session.turnQueue = {
      running: null,
      queued: [{ turn_id: "t1", message: "排队的一条" }],
      cancelled: [],
    } as never;

    await w.get('[data-ic-toggle]').trigger("click");

    expect(session.turnRunning).toBe(true);
    expect(session.draft).toBe("还没发出去的草稿");
    expect(session.turnQueue.queued).toHaveLength(1);
    expect(session.turnQueue.queued[0].turn_id).toBe("t1");
    // 输入区始终挂载：收起不是清空
    expect(w.find('[data-stub="composer"]').exists()).toBe(true);
    // 操作行左侧如实说「回复没有被收起打断」
    expect(w.get('[data-ic-status]').text()).toContain("正在回复");
    expect(w.get('[data-ic-status]').text()).toContain("已排队 1 条");
  });

  it("板面操作（lastOpLabel 前进）自动收起，但回复继续", async () => {
    const { wrapper: w, interactive, session } = mountConversation({ expanded: true });
    session.turnRunning = true;
    interactive.lastOpLabel = "移动卡片";
    await nextTick();
    expect(w.emitted("update:expanded")?.[0]).toEqual([false]);
    expect(w.get('[data-ic="conversation"]').attributes("data-ic-expanded")).toBe("false");
    expect(session.turnRunning).toBe(true);
  });

  it("autoCollapseOnBoardAction=false 时板面操作不会收起", async () => {
    const { wrapper: w, interactive } = mountConversation({
      expanded: true,
      autoCollapseOnBoardAction: false,
    });
    interactive.lastOpLabel = "移动卡片";
    await nextTick();
    expect(w.emitted("update:expanded")).toBeUndefined();
    expect(w.get('[data-ic="conversation"]').attributes("data-ic-expanded")).toBe("true");
  });
});

describe("高度调整：拖动 / 键盘 / 记忆（口径不变）", () => {
  it("初始高度按 props 渲染并写成 CSS 变量", () => {
    const { wrapper: w } = mountConversation({ expanded: true, height: 500, binding: { boardId: "b1" } });
    expect(panelVar(w)).toBe("500px");
    expect(localStorage.getItem("qio-interactive-conversation:b1:height")).toBe("500");
    const vm = w.vm as unknown as { height: number };
    expect(vm.height).toBe(500);
  });

  it("拖动同一根把手改变高度（向上拖 = 变高），松手发 resize-end", async () => {
    const { wrapper: w } = mountConversation({ expanded: true, height: 400 });
    const handle = w.get('[data-ic-resize]').element;
    handle.dispatchEvent(pointer("pointerdown", 100, 500));
    document.dispatchEvent(pointer("pointermove", 100, 400));
    await nextTick();
    expect(w.emitted("resize-start")).toHaveLength(1);
    expect(lastEmitted(w, "update:height")).toBe(500);
    expect(panelVar(w)).toBe("500px");
    document.dispatchEvent(pointer("pointerup", 100, 400));
    await nextTick();
    expect(lastEmitted(w, "resize-end")).toBe(500);
  });

  it("小于阈值的移动不算拖动（点击仍然开合）", async () => {
    const { wrapper: w } = mountConversation({ expanded: true, height: 400 });
    const handle = w.get('[data-ic-resize]').element;
    handle.dispatchEvent(pointer("pointerdown", 100, 500));
    document.dispatchEvent(pointer("pointermove", 100, 498));
    await nextTick();
    expect(w.emitted("update:height")).toBeUndefined();
    document.dispatchEvent(pointer("pointerup", 100, 498));
    await nextTick();
    expect(w.emitted("resize-end")).toBeUndefined();
  });

  it("指针取消（pointercancel / Esc）恢复拖动前高度，不把中间态留下", async () => {
    const { wrapper: w } = mountConversation({ expanded: true, height: 400 });
    const handle = w.get('[data-ic-resize]').element;
    handle.dispatchEvent(pointer("pointerdown", 100, 500));
    document.dispatchEvent(pointer("pointermove", 100, 450));
    await nextTick();
    expect(panelVar(w)).toBe("450px");
    pressEscape();
    await nextTick();
    expect(panelVar(w)).toBe("400px");
    expect(w.emitted("resize-end")).toBeUndefined();
  });

  it("键盘调整：上下方向键 ±步长，Home/End 到两端（都夹在口径内）", async () => {
    const { wrapper: w } = mountConversation({ expanded: true, height: 400 });
    const handle = w.get('[data-ic-resize]');
    await handle.trigger("keydown", { key: "ArrowDown" });
    expect(lastEmitted(w, "update:height")).toBe(376);
    await handle.trigger("keydown", { key: "ArrowUp" });
    expect(lastEmitted(w, "update:height")).toBe(400);
    await handle.trigger("keydown", { key: "Home" });
    expect(lastEmitted(w, "update:height")).toBe(CONVERSATION_MIN_HEIGHT);
    await handle.trigger("keydown", { key: "End" });
    // End 到「口径内的最大高度」：视口较矮时是视口比例上限，而不是组件上限
    expect(lastEmitted(w, "update:height")).toBe(
      clampConversationHeight(CONVERSATION_MAX_HEIGHT, { viewportHeight: window.innerHeight }),
    );
  });

  it("收起态按 ↑ 会先展开再调高；键位夹在口径内", async () => {
    const { wrapper: w } = mountConversation();
    await w.get('[data-ic-resize]').trigger("keydown", { key: "ArrowUp" });
    await nextTick();
    expect(w.get('[data-ic="conversation"]').attributes("data-ic-expanded")).toBe("true");
    expect(lastEmitted(w, "update:height")).toBe(
      clampConversationHeight(CONVERSATION_DEFAULT_HEIGHT + 24, { viewportHeight: window.innerHeight }),
    );
  });
});

describe("互动绑定：props 可信传入，只做发送选项", () => {
  it("传入 board/submission 时投影给 Composer，intentId 不进入发送选项", () => {
    const { wrapper: w } = mountConversation({
      binding: { boardId: "b1", submissionId: "s1", intentId: "i1" },
    });
    expect(composerBinding(w)).toEqual({
      mode: "interactive",
      boardId: "b1",
      submissionId: "s1",
    });
    expect(JSON.stringify(composerBinding(w))).not.toContain("i1");
  });

  it("不传绑定 → Composer 收到的就是 undefined（普通聊天不带板面身份）", () => {
    const { wrapper: w } = mountConversation();
    expect(composerBinding(w)).toBeNull();
  });

  it("绑定不会进入界面文本（用户原文与界面都不出现板面 id）", () => {
    const { wrapper: w } = mountConversation({ binding: { boardId: "board_secret_1" } });
    expect(w.text()).not.toContain("board_secret_1");
  });
});

describe("槽位与实例方法", () => {
  it("模式切换在操作行左侧、工具在右侧、leading 进输入区", () => {
    const { wrapper: w } = mountConversation(
      {},
      {
        "mode-switch": '<span class="im-mode-switch" data-slot="mode">模式</span>',
        toolbar: '<span data-slot="toolbar">工具</span>',
        leading: '<span data-slot="leading">星球</span>',
      },
    );
    expect(w.get('[data-ic-rail] .ic-rail-left [data-slot="mode"]').text()).toBe("模式");
    expect(w.get('[data-ic-rail] .ic-rail-right [data-slot="toolbar"]').text()).toBe("工具");
    // leading 由 Composer 渲染：替身组件把它渲染在自己的根节点里
    expect(w.get('[data-stub="composer"] [data-slot="leading"]').text()).toBe("星球");
    // 旧槽位已移除
    expect(w.find('[data-ic="conversation"] .ic-composer [data-slot="mode"]').exists()).toBe(false);
  });

  it("expose：toggle / expand / collapse 只改显示", async () => {
    const { wrapper: w, session } = mountConversation();
    const vm = w.vm as unknown as { toggle: () => void; expand: () => void; collapse: () => void };
    vm.toggle();
    await nextTick();
    expect(w.get('[data-ic="conversation"]').attributes("data-ic-expanded")).toBe("true");
    vm.collapse();
    await nextTick();
    expect(w.get('[data-ic="conversation"]').attributes("data-ic-expanded")).toBe("false");
    expect(session.turnRunning).toBe(false);
  });
});