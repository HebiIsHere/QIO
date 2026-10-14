/**
 * 普通对话与输入链：统一滚动策略 + 宽度适配（行为级用例）。
 *
 * 覆盖本轮的验收点：
 * * 消息区 .stream 是普通对话**唯一的主要纵向滚动视口**：对话根带 data-qio-scope="chat"，
 *   接共享滚动引擎（auto + 仅纵向）——横向既不滚动也不给指示条留位置；
 * * 消息 / 气泡 / 附件 / 工具参数与输出 / 错误详情在宽度链上传播 data-qio-fit，
 *   且消息内部**不建立**任何新的滚动视口；
 * * 过程区展开历史 / 详情进入同一阅读区，不新建滚动视口（同一连续阅读区只有一层主要纵向滚动）；
 * * 输入框：空 / 短内容不产生滚动范围；到上限只纵向滚动；恢复草稿 / 切换话题后重算高度；
 *   hidden 策略不画指示条、不预留轨道空白；量高度不改写值与选区。
 *
 * 说明：jsdom 不做真实布局，所以这里用**模拟尺寸**验证行为与 DOM 契约；
 * 真实布局证据（无横向滚动范围、媒体等比适配）由复核阶段在浏览器里采集。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";
import Composer from "../Composer.vue";
import MessageItem from "../MessageItem.vue";
import MessageStream from "../MessageStream.vue";
import TurnProcess from "../TurnProcess.vue";
import ConversationView from "../../views/ConversationView.vue";
import {
  useSessionStore,
  type StreamMessage,
  type TurnFacts,
} from "../../stores/session";
import { resetProcessState } from "../../stores/turnProcess";

vi.mock("../../services/api", () => ({
  api: {
    sendTurn: vi.fn(async () => ({ ok: true, topic_id: null })),
    getSessionContext: vi.fn(async () => ({
      topic_id: "t1",
      topic_name: "话题",
      anchor_fragment: null,
      messages: [],
    })),
    cancelTurn: vi.fn(async () => ({ ok: true, cancelled: true })),
    loadHistory: vi.fn(async () => ({ messages: [], anchor: null })),
    getUISettings: vi.fn(async () => ({ typewriter_cps: 50 })),
    getTrace: vi.fn(async () => ({ turn_id: "turn_1", duration_ms: 0, phases: {} })),
    respondApproval: vi.fn(async () => ({ ok: true })),
  },
  ApiError: class ApiError extends Error {},
}));

/**
 * 虚拟滚动在 jsdom 里没有布局，不会渲染任何轮次（视口高度为 0）。
 * 这里替换成「全部渲染」的桩（与 TurnProcess.verify.test.ts 同一做法）：
 * 本文件验证的是宽度链 / 滚动策略的 DOM 契约，不是 tanstack 的虚拟化实现。
 */
vi.mock("@tanstack/vue-virtual", async () => {
  const { computed } = await import("vue");
  return {
    useVirtualizer: (options: { value: { count: number } }) =>
      computed(() => ({
        getTotalSize: () => options.value.count * 400,
        getVirtualItems: () =>
          Array.from({ length: options.value.count }, (_, index) => ({
            index,
            key: index,
            start: index * 400,
          })),
        measureElement: () => undefined,
      })),
  };
});

function freshPinia() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return pinia;
}

/** jsdom 不做布局：给元素一个可测的几何 */
function shape(el: HTMLElement, scrollHeight: number, clientHeight: number) {
  Object.defineProperty(el, "scrollHeight", { value: scrollHeight, configurable: true });
  Object.defineProperty(el, "clientHeight", { value: clientHeight, configurable: true });
}

/** 滚动引擎把测量合并到一帧里；等它落定 */
async function frame() {
  await new Promise((r) => setTimeout(r, 40));
  await nextTick();
}

function msg(partial: Partial<StreamMessage> & { id: string; role: StreamMessage["role"] }): StreamMessage {
  return {
    content: "",
    contentType: "text",
    createdAt: "2026-10-14T08:00:00+00:00",
    ...partial,
  } as StreamMessage;
}

function facts(partial: Partial<TurnFacts> = {}): TurnFacts {
  const base: TurnFacts = {
    turnId: "turn_1",
    status: "completed",
    durationMs: null,
    queueMs: null,
    startedAt: null,
    endedAt: null,
    reason: null,
    reasonCode: null,
    stoppedBy: null,
    actions: [],
    errorText: null,
  };
  return { ...base, ...partial };
}

beforeEach(() => {
  localStorage.clear();
  resetProcessState();
});

describe("消息区：普通对话唯一的主要纵向滚动视口", () => {
  it("对话根带 data-qio-scope=chat，并接共享指示条（auto + 仅纵向）", async () => {
    const pinia = freshPinia();
    const w = mount(MessageStream, { global: { plugins: [pinia] } });
    await nextTick();
    const stream = w.find(".stream").element as HTMLElement;

    // 对话根：整套对话适配（换行 / 媒体适配 / 按钮重排）挂在这里
    expect(stream.getAttribute("data-qio-scope")).toBe("chat");
    // 共享滚动引擎接管：不再由组件自己写原生滚动条样式
    expect(stream.classList.contains("qio-scroll-host")).toBe(true);
    expect(stream.dataset.qioScroll).toBe("auto");

    // 纵向溢出 → 指示条可被引擎建立；横向不声明 → 永远没有横向指示条
    shape(stream, 1200, 400);
    stream.dispatchEvent(new Event("scroll"));
    await frame();
    expect(stream.dataset.qioRange).toBe("y");
    expect(stream.querySelector('.qio-scroll-rail[data-axis="y"]')).not.toBeNull();
    expect(stream.querySelector('.qio-scroll-rail[data-axis="x"]')).toBeNull();

    // 内容装得下时：不显示指示条，也不留下任何范围标记
    shape(stream, 400, 400);
    stream.dispatchEvent(new Event("scroll"));
    await frame();
    expect(stream.dataset.qioRange).toBe("none");
    expect(stream.querySelector('.qio-scroll-rail.is-visible')).toBeNull();

    w.unmount();
  });

  it("消息子树在宽度链上带 data-qio-fit，且消息内部不建立任何滚动视口", async () => {
    const pinia = freshPinia();
    const session = useSessionStore();
    const w = mount(MessageStream, { global: { plugins: [pinia] } });
    await nextTick();
    // 先给视口一个可测几何，再推消息：虚拟列表按视口高度决定渲染哪些轮次
    shape(w.find(".stream").element as HTMLElement, 1200, 400);
    session.pushUser("用户消息");
    session.pushAssistant("回答正文");
    await frame();

    const stream = w.find(".stream").element as HTMLElement;
    // 一轮的宽度链
    const turn = stream.querySelector(".turn");
    expect(turn?.getAttribute("data-qio-fit")).not.toBeNull();

    const messages = Array.from(stream.querySelectorAll(".message"));
    expect(messages.length).toBeGreaterThan(0);
    for (const m of messages) {
      expect(m.getAttribute("data-qio-fit"), "每条消息都是宽度链的一层").not.toBeNull();
    }
    // 消息区里除消息区自己之外，不得再有第二个滚动视口
    const hosts = Array.from(stream.querySelectorAll("[data-qio-scroll]"));
    expect(hosts.map((h) => h.className)).toEqual([]);
    w.unmount();
  });

  it("附件 / 正文 / 工具参数与输出 / 错误与恢复详情都在宽度链上（真实组件挂载）", async () => {
    const pinia = freshPinia();
    useSessionStore();

    // 用户消息 + 超长附件名（不省略、不撑宽、不横向滚动）
    const user = mount(MessageItem, {
      props: {
        message: msg({
          id: "m_user",
          role: "user",
          content: "看看这个文件 " + "C:/一个很长的目录/".repeat(12),
          attachments: [
            {
              id: "att_long",
              name: "一个非常长的附件名".repeat(6) + ".pdf",
              sizeBytes: 2048,
              kind: "copy",
              display: "已保存副本",
              state: "ready",
              error: null,
            },
          ],
        }),
      },
      global: { plugins: [pinia] },
    });
    expect(user.find(".message").attributes("data-qio-fit")).not.toBeUndefined();
    expect(user.find(".bubble.user-bubble").attributes("data-qio-fit")).not.toBeUndefined();
    expect(user.find("[data-test='message-attachments']").attributes("data-qio-fit")).not.toBeUndefined();
    expect(user.find(".chip").attributes("data-qio-fit")).not.toBeUndefined();
    // 消息内部不建立滚动视口
    expect(user.find("[data-qio-scroll]").exists()).toBe(false);
    expect(user.find(".qio-scroll-host").exists()).toBe(false);
    user.unmount();

    // 回答正文
    const assistant = mount(MessageItem, {
      props: { message: msg({ id: "m_a", role: "assistant", content: "回答正文" }) },
      global: { plugins: [pinia], stubs: { MarkdownContent: true } },
    });
    expect(assistant.find(".bubble.assist-bubble").attributes("data-qio-fit")).not.toBeUndefined();
    assistant.unmount();

    // 工具卡：失败结论 + 参数 / 输出（展开后）都在宽度链上，且不建小滚动窗口
    const tool = mount(MessageItem, {
      props: {
        message: msg({
          id: "m_tool",
          role: "tool",
          content: "工具输出预览",
          toolName: "read_file",
          toolStatus: "failed",
          toolError: "读取失败：" + "C:/一个很长的目录/".repeat(8) + "文件不存在",
          toolArgs: JSON.stringify({ path: "C:/一个很长的目录/" + "y".repeat(200) }),
        }),
      },
      global: { plugins: [pinia] },
    });
    expect(tool.find(".tool-card").attributes("data-qio-fit")).not.toBeUndefined();
    expect(tool.find(".tool-fail-line").attributes("data-qio-fit")).not.toBeUndefined();
    await tool.find(".tool-head").trigger("click");
    await nextTick();
    expect(tool.find(".tool-detail").attributes("data-qio-fit")).not.toBeUndefined();
    const pres = tool.findAll(".tool-detail pre");
    expect(pres.length).toBeGreaterThan(0);
    for (const p of pres) expect(p.attributes("data-qio-fit")).not.toBeUndefined();
    expect(tool.find("[data-qio-scroll]").exists()).toBe(false);
    tool.unmount();

    // 独立任务失败详情
    const sub = mount(MessageItem, {
      props: {
        message: msg({
          id: "m_sub",
          role: "subagent",
          content: "",
          taskStatus: "failed",
          toolError: "这项独立任务没有完成的真实原因",
        }),
      },
      global: { plugins: [pinia] },
    });
    expect(sub.find(".subagent-card").attributes("data-qio-fit")).not.toBeUndefined();
    sub.unmount();
  });
});

describe("过程区：展开进同一阅读区，不新建滚动视口", () => {
  it("过程区 / 详情在宽度链上带 data-qio-fit；展开历史与详情都不产生第二个滚动视口", async () => {
    const pinia = freshPinia();
    const session = useSessionStore();
    // 重试入口要求「这一轮的用户消息」还在：先补上，才能验证可用操作没被藏起来
    session.pushUser("帮我核对实现");
    const mine = session.messages[session.messages.length - 1];
    if (mine) mine.turnId = "turn_1";
    const items: StreamMessage[] = [
      msg({ id: "p_tool", role: "tool", content: "调用输出", toolName: "read_file", toolStatus: "success" }),
      msg({ id: "p_note", role: "assistant", content: "先看一下目录结构" }),
    ];
    const w = mount(TurnProcess, {
      props: {
        turnId: "turn_1",
        items,
        stages: [],
        facts: facts({
          status: "failed",
          reason: "模型服务没有响应",
          reasonCode: "upstream_502",
          errorText: "upstream 502 " + "E".repeat(400),
          actions: ["retry"],
        }),
        running: false,
      },
      global: { plugins: [pinia] },
    });
    await nextTick();

    const tp = w.find("[data-test='turn-process']");
    expect(tp.attributes("data-qio-fit")).not.toBeUndefined();
    expect(tp.element.querySelectorAll("[data-qio-scroll]").length).toBe(0);
    expect(tp.element.querySelectorAll(".qio-scroll-host").length).toBe(0);

    // 结束详情：全文在同一个阅读区里展开（不再套 160px 的小滚动窗口）
    const pre = tp.find(".tp-outcome-pre");
    expect(pre.exists()).toBe(true);
    expect(pre.attributes("data-qio-fit")).not.toBeUndefined();
    expect(pre.text()).toContain("upstream 502");

    // 展开过程历史：内容进入同一阅读区，仍然没有第二个滚动视口
    await tp.find("[data-test='turn-process-toggle']").trigger("click");
    await nextTick();
    expect(tp.find("[data-test='turn-process-history']").exists()).toBe(true);
    expect(tp.element.querySelectorAll("[data-qio-scroll]").length).toBe(0);
    expect(tp.element.querySelectorAll(".qio-scroll-rail").length).toBe(0);

    // 审批入口与可用操作保持既有规则（不因为重排被藏起来）
    expect(tp.find("[data-test='turn-process-reason']").text()).toContain("模型服务没有响应");
    expect(tp.find("[data-test='turn-process-action-retry']").exists()).toBe(true);
    w.unmount();
  });
});

describe("输入区：软换行 + 高度重算 + 到上限只纵向滚动", () => {
  /** 让 textarea 的几何与内容成真实关系（jsdom 不排版）：
   *  空 / 单行 46px，每多一行 +25px；超长单行按软换行给一个远超上限的高度。 */
  function shapeTextarea(ta: HTMLTextAreaElement) {
    Object.defineProperty(ta, "scrollHeight", {
      configurable: true,
      get: () => {
        const value = ta.value;
        if (!value) return 46;
        if (value.length > 400) return 2000;
        return 46 + (value.split("\n").length - 1) * 25;
      },
    });
    Object.defineProperty(ta, "clientHeight", {
      configurable: true,
      get: () => (ta.style.height ? Number.parseInt(ta.style.height, 10) : 46),
    });
  }

  function mountComposer() {
    const pinia = freshPinia();
    const session = useSessionStore();
    const w = mount(Composer, { global: { plugins: [pinia] } });
    const ta = w.find("textarea.qio-input").element as HTMLTextAreaElement;
    shapeTextarea(ta);
    return { w, session, ta };
  }

  it("空 / 短内容不产生滚动范围；到上限后只纵向滚动", async () => {
    const { w, ta } = mountComposer();
    // 空草稿：没有内联高度，也没有滚动范围
    expect(ta.style.height).toBe("");
    expect(ta.scrollHeight).toBeLessThanOrEqual(ta.clientHeight);

    // 单行短内容：高度贴合内容，不产生滚动范围
    await w.find("textarea.qio-input").setValue("短消息");
    expect(ta.style.height).toBe("46px");
    expect(ta.scrollHeight).toBeLessThanOrEqual(ta.clientHeight);

    // 多行：随内容增高，仍然不产生滚动范围
    await w.find("textarea.qio-input").setValue("1\n2\n3");
    expect(ta.style.height).toBe("96px");
    expect(ta.scrollHeight).toBeLessThanOrEqual(ta.clientHeight);

    // 超长内容（软换行后远超上限）：夹在上限，只有纵向滚动范围
    await w.find("textarea.qio-input").setValue("x".repeat(500));
    expect(ta.style.height).toBe("320px");
    expect(ta.scrollHeight).toBeGreaterThan(ta.clientHeight);
    w.unmount();
  });

  it("隐藏策略：不画指示条、不预留轨道空白（textarea 不进入指示条体系）", async () => {
    const { w, ta } = mountComposer();
    await w.find("textarea.qio-input").setValue("1\n2\n3");
    // 共享引擎的 hidden 档：保留滚动与定位能力，但始终不画指示条
    expect(ta.classList.contains("qio-scroll-host")).toBe(true);
    expect(ta.dataset.qioScroll).toBe("hidden");
    expect(ta.dataset.qioRange).toBe("none");
    expect(ta.querySelector(".qio-scroll-rail")).toBeNull();
    // 软换行 + 宽度链
    expect(ta.getAttribute("wrap")).toBe("soft");
    expect(ta.getAttribute("data-qio-fit")).not.toBeNull();
    w.unmount();
  });

  it("恢复草稿 / 切换话题后重新计算高度（不是等下一次输入）", async () => {
    const { w, session, ta } = mountComposer();
    // 恢复草稿（发送失败放回 / 打开设置后回来）：草稿从 store 写回输入框
    session.draft = "1\n2\n3";
    await nextTick();
    await nextTick();
    expect(ta.value).toBe("1\n2\n3");
    expect(ta.style.height).toBe("96px");

    // 切话题：草稿字符串没变（不会触发草稿 watch），但高度仍按内容重算
    ta.style.height = "999px";
    session.currentTopicId = "t2";
    await nextTick();
    await nextTick();
    expect(ta.style.height).toBe("96px");
    w.unmount();
  });

  it("量高度不改写输入值与选区（输入法 / 光标 / 撤销语义不受影响）", async () => {
    const { w, ta } = mountComposer();
    await w.find("textarea.qio-input").setValue("abc def");
    ta.setSelectionRange(1, 3);
    ta.dispatchEvent(new Event("input"));
    await nextTick();
    expect(ta.value).toBe("abc def");
    expect(ta.selectionStart).toBe(1);
    expect(ta.selectionEnd).toBe(3);
    w.unmount();
  });
});

describe("对话页：独立并列列表接入统一滚动策略", () => {
  it("知识候选列表用 QIO 指示条（auto + 仅纵向），不出现原生滚动条区域", async () => {
    const pinia = freshPinia();
    const session = useSessionStore();
    const w = mount(ConversationView, {
      global: {
        plugins: [pinia],
        stubs: { MessageStream: true, Composer: true, SettingsFloat: true, PlanetDock: true },
      },
    });
    session.knowledgeCandidates = [{ knowledgeId: "k1", category: "fact", content: "候选内容" }];
    await nextTick();
    await nextTick();

    const candidates = w.find(".candidates").element as HTMLElement;
    expect(candidates.getAttribute("data-qio-fit")).not.toBeNull();
    expect(candidates.classList.contains("qio-scroll-host")).toBe(true);
    expect(candidates.dataset.qioScroll).toBe("auto");
    expect(candidates.querySelector('.qio-scroll-rail[data-axis="x"]')).toBeNull();
    w.unmount();
  });
});

/**
 * 用户消息是**纯文本**通道（不经过 markdown）：它必须自己换行。
 * 否则气泡上的 data-qio-fit（overflow-x: clip）会把超长 URL / 连续字符直接裁掉且滚不动 ——
 * 复核实测 clientW 588 / scrollW 2251（422 字符 URL 渲染宽 3152px，约 2564px 永久看不见）。
 */
describe("用户纯文本气泡的换行（复核 F1）", () => {
  // 路径放进变量：字面量会被 Vite 当成资源 URL 重写，读不到真实文件
  const messagePath = "../MessageItem.vue";
  const scrollCssPath = "../../styles/scroll.css";
  const messageSource = readFileSync(new URL(messagePath, import.meta.url), "utf8");
  const scrollCss = readFileSync(new URL(scrollCssPath, import.meta.url), "utf8");

  function ruleBlock(css: string, selector: string): string {
    const start = css.indexOf(selector);
    if (start < 0) return "";
    const open = css.indexOf("{", start);
    const close = css.indexOf("}", open);
    return css.slice(open + 1, close);
  }

  it("MessageItem 的 .user-bubble .plain 声明 overflow-wrap（长 token 视觉折行）", () => {
    const block = ruleBlock(messageSource, ".user-bubble .plain");
    expect(block, "找不到 .user-bubble .plain 规则").not.toBe("");
    expect(block).toContain("overflow-wrap: anywhere");
  });

  it("共享规则在对话作用域根声明 overflow-wrap（继承覆盖自定义纯文本容器）", () => {
    const block = ruleBlock(scrollCss, '[data-qio-scope~="chat"]');
    expect(block).toContain("overflow-wrap: anywhere");
  });
});
