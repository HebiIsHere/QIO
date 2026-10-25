/**
 * 底部互动对话区（Phase 3-B，契约 §6.4）。
 *
 * 这里验的是**组件层真的接上了**，children 用轻量替身（MessageStream / Composer / ComposerEntries
 * 自己的行为由它们的既有测试负责）：
 * - 从输入区向上展开 / 收起、高度拖动与键盘调整；
 * - 收起只改显示：不停止回复、不清草稿/附件、不动排队；
 * - 板面操作自动收起，但不打断回复；
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
  CONVERSATION_MIN_HEIGHT,
  CONVERSATION_MAX_HEIGHT,
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

describe("从输入区向上展开 / 收起", () => {
  it("默认收起：只有输入区与入口条，没有会话流与高度分隔条", () => {
    const { wrapper: w } = mountConversation();
    expect(w.get('[data-ic="conversation"]').attributes("data-ic-expanded")).toBe("false");
    expect(w.find('[data-stub="stream"]').exists()).toBe(false);
    expect(w.find('[data-ic-resize]').exists()).toBe(false);
    expect(w.find('[data-stub="composer"]').exists()).toBe(true);
    expect(w.find('[data-stub="entries"]').exists()).toBe(true);
    expect(w.get('[data-ic-toggle]').attributes("aria-expanded")).toBe("false");
    expect(w.get('[data-ic-status]').text()).toContain("点开继续对话");
  });

  it("展开后会话流出现在输入区上方，分隔条与无障碍状态齐全", async () => {
    const { wrapper: w } = mountConversation();
    await w.get('[data-ic-toggle]').trigger("click");

    expect(w.emitted("update:expanded")?.[0]).toEqual([true]);
    expect(w.emitted("expand")).toHaveLength(1);
    expect(w.get('[data-ic="conversation"]').attributes("data-ic-expanded")).toBe("true");
    expect(w.find('[data-stub="stream"]').exists()).toBe(true);
    const separator = w.get('[data-ic-resize]');
    expect(separator.attributes("role")).toBe("separator");
    expect(separator.attributes("aria-orientation")).toBe("horizontal");
    expect(Number(separator.attributes("aria-valuenow"))).toBeGreaterThan(0);
    expect(w.get('[data-ic-toggle]').attributes("aria-expanded")).toBe("true");
    // aria-controls 指向真正承载会话流的元素
    const stream = w.get('[data-ic-stream]');
    expect(w.get('[data-ic-toggle]').attributes("aria-controls")).toBe(stream.attributes("id"));
  });

  it("点击/聚焦输入区自动展开，右上角圆形按钮收起", async () => {
    const { wrapper: w } = mountConversation();
    await w.get(".ic-composer").trigger("focusin");
    await nextTick();
    expect(w.get('[data-ic="conversation"]').attributes("data-ic-expanded")).toBe("true");
    expect(w.find('[data-ic-close]').exists()).toBe(true);
    await w.get('[data-ic-close]').trigger("click");
    expect(w.get('[data-ic="conversation"]').attributes("data-ic-expanded")).toBe("false");
  });

  it("展开后再次收起：会话流隐藏但不卸载（阅读位置与已渲染内容保留）", async () => {
    const { wrapper: w } = mountConversation({ expanded: true });
    expect(w.find('[data-stub="stream"]').isVisible()).toBe(true);
    await w.get('[data-ic-toggle]').trigger("click");
    expect(w.emitted("update:expanded")?.[0]).toEqual([false]);
    expect(w.emitted("collapse")).toHaveLength(1);
    // 仍然挂载，只是不可见
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
    // 收起条如实说「回复没有被收起打断」
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

describe("高度调整：拖动 / 键盘 / 记忆", () => {
  it("初始高度按 props 渲染并写成 CSS 变量", () => {
    const { wrapper: w } = mountConversation({ expanded: true, height: 500, binding: { boardId: "b1" } });
    expect(panelVar(w)).toBe("500px");
    expect(w.get('[data-ic-resize]').attributes("aria-valuenow")).toBe("500");
    expect(localStorage.getItem("qio-interactive-conversation:b1:height")).toBe("500");
  });

  it("拖动分隔条改变高度（向上拖 = 变高），松手发 resize-end", async () => {
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

  it("指针取消（pointercancel / Esc）恢复拖动前高度，不把中间态留下", async () => {
    const { wrapper: w } = mountConversation({ expanded: true, height: 400 });
    const handle = w.get('[data-ic-resize]').element;
    handle.dispatchEvent(pointer("pointerdown", 100, 500));
    document.dispatchEvent(pointer("pointermove", 100, 450));
    await nextTick();
    expect(panelVar(w)).toBe("450px");
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await nextTick();
    expect(panelVar(w)).toBe("400px");
    expect(w.emitted("resize-end")).toBeUndefined();
  });

  it("键盘调整：上下方向键 ±步长，Home/End 到两端（都夹在口径内）", async () => {
    const { wrapper: w } = mountConversation({ expanded: true, height: 400 });
    const separator = w.get('[data-ic-resize]');
    await separator.trigger("keydown", { key: "ArrowDown" });
    expect(lastEmitted(w, "update:height")).toBe(376);
    await separator.trigger("keydown", { key: "ArrowUp" });
    expect(lastEmitted(w, "update:height")).toBe(400);
    await separator.trigger("keydown", { key: "Home" });
    expect(lastEmitted(w, "update:height")).toBe(CONVERSATION_MIN_HEIGHT);
    await separator.trigger("keydown", { key: "End" });
    // End 到「口径内的最大高度」：视口较矮时是视口比例上限，而不是组件上限
    expect(lastEmitted(w, "update:height")).toBe(
      clampConversationHeight(CONVERSATION_MAX_HEIGHT, { viewportHeight: window.innerHeight }),
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
  it("header-extra 与 leading 槽位分别进入状态条与输入区", () => {
    const { wrapper: w } = mountConversation({}, {
      "header-extra": '<span data-slot="header-extra">模式切换</span>',
      leading: '<span data-slot="leading">星球</span>',
    });
    expect(w.get('[data-ic-bar] [data-slot="header-extra"]').text()).toBe("模式切换");
    // leading 由 Composer 渲染：替身组件把它渲染在自己的根节点里
    expect(w.get('[data-stub="composer"] [data-slot="leading"]').text()).toBe("星球");
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
