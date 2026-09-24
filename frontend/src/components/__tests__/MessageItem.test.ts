import { describe, expect, it, beforeEach, vi } from "vitest";
import { mount } from "@vue/test-utils";
import { createPinia, setActivePinia, type Pinia } from "pinia";
import MessageItem from "../MessageItem.vue";
import { useEventStore } from "../../stores/events";
import { useSessionStore } from "../../stores/session";
import { useUiStore } from "../../stores/ui";
import type { StreamMessage } from "../../stores/session";

function makeMessage(over: Partial<StreamMessage> & { role: StreamMessage["role"] }): StreamMessage {
  return {
    id: "m1",
    content: "hi",
    contentType: "text",
    createdAt: "2026-08-09T09:42:11+08:00",
    ...over,
  };
}

function mountItem(msg: StreamMessage, pinia: Pinia) {
  return mount(MessageItem, {
    props: { message: msg },
    global: { plugins: [pinia], stubs: { MarkdownContent: true } },
  });
}

beforeEach(() => {
  document.documentElement.removeAttribute("data-theme");
  localStorage.clear();
});

describe("MessageItem 用量归属（问题3）", () => {
  it("正常模式不显示 token（宁可隐藏，也不显示错误数字）", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const events = useEventStore();
    events.usageByTurn = { turn_1: { tokens: 100 } };
    const w = mountItem(makeMessage({ role: "assistant", content: "好的", turnId: "turn_1" }), pinia);
    expect(w.find(".meta").text()).not.toContain("tok");
    w.unmount();
  });

  it("开发者模式：助手消息显示自己 turn 的 token，而不是最近一次的累计值", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const events = useEventStore();
    const ui = useUiStore();
    ui.developerMode = true;
    events.usageByTurn = { turn_1: { tokens: 100 }, turn_2: { tokens: 200 } };
    const w = mountItem(makeMessage({ role: "assistant", content: "A", turnId: "turn_1" }), pinia);
    expect(w.find(".meta").text()).toContain("token 100");
    w.unmount();
  });

  it("用户消息与无归属消息不带 token", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    useUiStore().developerMode = true;
    useEventStore().usageByTurn = { turn_1: { tokens: 100 } };
    const user = mountItem(makeMessage({ role: "user", content: "问", turnId: "turn_1" }), pinia);
    expect(user.find(".meta").text()).not.toContain("tok");
    user.unmount();
    const orphan = mountItem(makeMessage({ role: "assistant", content: "A", turnId: null }), pinia);
    expect(orphan.find(".meta").text()).not.toContain("tok");
    orphan.unmount();
  });
});

describe("MessageItem 消息信息层级与状态", () => {
  it("等待中的消息只给一行轻量 metadata（等待中）", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const w = mountItem(makeMessage({ role: "user", content: "下一条", queued: true }), pinia);
    expect(w.find(".queued-tag").text()).toContain("等待中");
    w.unmount();
  });

  it("复制按钮：点击复制内容并给出短暂成功反馈", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const writeText = vi.fn(async () => {});
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    const w = mountItem(makeMessage({ role: "assistant", content: "复制我" }), pinia);
    await w.find(".copy-btn").trigger("click");
    expect(writeText).toHaveBeenCalledWith("复制我");
    expect(w.find(".copy-btn").text()).toContain("已复制");
    w.unmount();
  });
});

describe("MessageItem 工具卡呈现", () => {
  it("工具卡标题用中文展示名，原始工具名只放在悬停提示里", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const w = mountItem(
      makeMessage({
        role: "tool",
        toolName: "web_search",
        content: "…",
        toolOk: false,
        presentation: { title: "网络搜索", tool: "web_search", summary: "联网搜索暂不可用" },
      }),
      pinia,
    );
    expect(w.find(".tool-name").text()).toBe("网络搜索");
    expect(w.find(".tool-card").attributes("title")).toBe("web_search");
    expect(w.find(".tool-name").text()).not.toContain("web_search");
    w.unmount();
  });

  it("开发者模式下 token 单位用完整单词（中文界面不留 tok 缩写）", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const ui = useUiStore();
    ui.developerMode = true;
    const events = useEventStore();
    events.usageByTurn = { turn_1: { tokens: 1204 } };
    const w = mountItem(makeMessage({ role: "assistant", content: "答", turnId: "turn_1" }), pinia);
    expect(w.find(".meta").text()).toContain("token 1,204");
    expect(w.find(".meta").text()).not.toContain("tok 1,204");
    w.unmount();
  });

  it("无 presentation 时回退默认模板（工具名 + 原始内容）", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const w = mountItem(
      makeMessage({ role: "tool", toolName: "shell", content: "{}", toolOk: true }),
      pinia,
    );
    expect(w.find(".tool-name").text()).toBe("shell");
    expect(w.find(".tool-status").exists()).toBe(false);
    w.find(".tool-head").trigger("click");
    expect(w.find(".tool-detail pre").text()).toBe("{}");
    w.unmount();
  });

  it("有 presentation 时优先渲染 title/status/summary", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const w = mountItem(
      makeMessage({
        role: "tool",
        toolName: "memory_search",
        content: "raw preview",
        toolOk: true,
        presentation: { title: "检索记忆", status: "ok", summary: "命中 3 条" },
      }),
      pinia,
    );
    expect(w.find(".tool-name").text()).toBe("检索记忆");
    expect(w.find(".tool-status").text()).toBe("ok");
    w.find(".tool-head").trigger("click");
    expect(w.find(".tool-detail pre").text()).toBe("命中 3 条");
    w.unmount();
  });

  it("失败工具卡呈现 status 标记为 fail 并显示错误", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const w = mountItem(
      makeMessage({
        role: "tool",
        toolName: "ghost",
        content: "",
        toolOk: false,
        toolError: "unknown tool",
        presentation: { title: "失败工具", status: "fail" },
      }),
      pinia,
    );
    expect(w.find(".tool-name").text()).toBe("失败工具");
    expect(w.find(".tool-status.fail").exists()).toBe(true);
    w.find(".tool-head").trigger("click");
    expect(w.find(".tool-error").text()).toBe("unknown tool");
    w.unmount();
  });

  it("失败原因在 120 字以内原样显示（不折叠成笼统文案）", () => {
    const reason =
      "列目录失败：[WinError 3] 系统找不到指定的路径。: 'C:\\Users\\zxy\\AppData\\Roaming\\qio\\workspace'";
    expect(reason.length).toBeGreaterThan(60);
    expect(reason.length).toBeLessThanOrEqual(120);
    const pinia = createPinia();
    setActivePinia(pinia);
    const w = mountItem(
      makeMessage({ role: "tool", toolName: "fs_list", content: "", toolOk: false, toolError: reason }),
      pinia,
    );
    expect(w.find(".tool-fail-line").text()).toBe(reason);
    w.unmount();
  });

  it("超过 120 字的失败原因截断并提示展开", () => {
    const reason = "连接失败：".repeat(30);
    const pinia = createPinia();
    setActivePinia(pinia);
    const w = mountItem(
      makeMessage({ role: "tool", toolName: "web_fetch", content: "", toolOk: false, toolError: reason }),
      pinia,
    );
    const line = w.find(".tool-fail-line").text();
    expect(line).toContain("展开可看完整原因");
    expect(line.length).toBeLessThan(reason.length);
    w.unmount();
  });

  it("展开工具卡时按记录 id 取全文（参数与输出）", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const session = useSessionStore();
    const spy = vi.spyOn(session, "loadToolRecord").mockResolvedValue();
    const w = mountItem(
      makeMessage({
        role: "tool",
        toolName: "fs_list",
        content: "列目录失败：找不到路径",
        toolOk: false,
        toolError: "列目录失败：找不到路径",
        toolRecordId: "tr1",
        toolRecordLoaded: false,
      }),
      pinia,
    );
    await w.find(".tool-head").trigger("click");
    expect(spy).toHaveBeenCalledWith("m1");
    w.unmount();
  });

  it("已有全文时展开显示参数段，且不再请求", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const session = useSessionStore();
    const spy = vi.spyOn(session, "loadToolRecord").mockResolvedValue();
    const w = mountItem(
      makeMessage({
        role: "tool",
        toolName: "fs_list",
        content: "完整输出：目录为空",
        toolOk: true,
        toolRecordId: "tr1",
        toolRecordLoaded: true,
        toolArgs: '{\n  "path": "."\n}',
      }),
      pinia,
    );
    await w.find(".tool-head").trigger("click");
    expect(spy).not.toHaveBeenCalled();
    const labels = w.findAll(".sec-label").map((n) => n.text());
    expect(labels).toEqual(["参数", "输出"]);
    expect(w.find(".tool-detail").text()).toContain('"path"');
    w.unmount();
  });

  it("输出未保存 / 已清理：说清原因，不藏起已有预览", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const w = mountItem(
      makeMessage({
        role: "tool",
        toolName: "fs_read",
        content: "正文预览",
        toolOk: true,
        toolRecordId: "tr1",
        toolRecordLoaded: true,
        toolOutputMissing: true,
        toolMissingReason: "setting",
      }),
      pinia,
    );
    await w.find(".tool-head").trigger("click");
    expect(w.find(".tool-note").text()).toContain("未保存");
    expect(w.find(".tool-detail").text()).toContain("正文预览");
    w.unmount();
  });

  it("输出被截断时标注已截断", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const w = mountItem(
      makeMessage({
        role: "tool",
        toolName: "web_fetch",
        content: "很长的正文…",
        toolOk: true,
        toolRecordId: "tr1",
        toolRecordLoaded: true,
        toolTruncated: true,
      }),
      pinia,
    );
    await w.find(".tool-head").trigger("click");
    expect(w.find(".tool-note").text()).toContain("已截断");
    w.unmount();
  });
});

describe("MessageItem 复制反馈（P0：失败不能报成功）", () => {
  it("剪贴板写入失败：按钮显示「复制失败」，不显示「已复制」", async () => {
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: vi.fn(async () => { throw new Error("denied"); }) },
    });
    const pinia = createPinia();
    setActivePinia(pinia);
    const w = mountItem(makeMessage({ role: "assistant", content: "需要复制的正文" }), pinia);
    await w.find(".copy-btn").trigger("click");
    await new Promise((r) => setTimeout(r, 0));
    expect(w.find(".copy-btn").text()).toBe("复制失败");
    expect(w.find(".copy-btn").classes()).toContain("fail");
    w.unmount();
  });

  it("剪贴板 API 不存在：同样显示「复制失败」", async () => {
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: undefined });
    const pinia = createPinia();
    setActivePinia(pinia);
    const w = mountItem(makeMessage({ role: "assistant", content: "需要复制的正文" }), pinia);
    await w.find(".copy-btn").trigger("click");
    await new Promise((r) => setTimeout(r, 0));
    expect(w.find(".copy-btn").text()).toBe("复制失败");
    w.unmount();
  });

  it("写入成功：显示「已复制」", async () => {
    const writeText = vi.fn(async () => {});
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    const pinia = createPinia();
    setActivePinia(pinia);
    const w = mountItem(makeMessage({ role: "assistant", content: "需要复制的正文" }), pinia);
    await w.find(".copy-btn").trigger("click");
    await new Promise((r) => setTimeout(r, 0));
    expect(writeText).toHaveBeenCalledWith("需要复制的正文");
    expect(w.find(".copy-btn").text()).toBe("已复制");
    w.unmount();
  });
});

describe("MessageItem 生成中的可访问性（任务02 D）", () => {
  it("流式生成中的正文标记 aria-busy，并且不设逐字播报的 live 区域", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const w = mountItem(makeMessage({ role: "assistant", content: "正在生成", streaming: true }), pinia);
    const bubble = w.find(".assist-bubble");
    expect(bubble.attributes("aria-busy")).toBe("true");
    expect(bubble.attributes("aria-live")).toBe("off");
    w.unmount();
  });

  it("落定后的正文不再标记 busy（按正常文档流阅读即可）", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const w = mountItem(makeMessage({ role: "assistant", content: "已经完成" }), pinia);
    const bubble = w.find(".assist-bubble");
    expect(bubble.attributes("aria-busy")).toBeUndefined();
    expect(bubble.attributes("aria-live")).toBeUndefined();
    w.unmount();
  });
});

describe("中间话与最终回答在视觉上分开", () => {
  it("interim 消息带「◈ 过程」标记", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const w = mountItem(makeMessage({ role: "assistant", content: "我先查一下", interim: true }), pinia);
    expect(w.find(".interim-tag").text()).toContain("过程");
    expect(w.find(".assist-bubble").classes()).toContain("interim");
    w.unmount();
  });

  it("最终回答不带「过程」标记", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const w = mountItem(makeMessage({ role: "assistant", content: "检查完成，真正的问题是…" }), pinia);
    expect(w.find(".interim-tag").exists()).toBe(false);
    expect(w.find(".assist-bubble").classes()).not.toContain("interim");
    w.unmount();
  });
});

/**
 * 第四阶段：卡片家族统一。
 *
 * Tool / Subagent / Tool Creation / 知识候选四类卡片的共同契约是
 * 「.qio-card 骨架 + data-state 状态 + .qio-state 徽章 + 可展开详情」，
 * 状态推进必须原位发生，所以状态只能体现在 data-state 上，不能靠换一张卡。
 */
describe("MessageItem 卡片家族契约（第四阶段）", () => {
  it("工具卡：家族骨架 + data-state 原位状态 + 状态徽章", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const w = mountItem(
      makeMessage({
        role: "tool",
        toolName: "web_search",
        content: "…",
        toolOk: true,
        presentation: { title: "网络搜索", status: "ok", summary: "命中 3 条" },
      }),
      pinia,
    );
    const card = w.find(".tool-card");
    expect(card.classes()).toContain("qio-card");
    expect(card.attributes("data-state")).toBe("ready");
    expect(w.find(".tool-status").classes()).toContain("qio-state");
    w.unmount();
  });

  it("工具卡：运行中/失败用同一张卡的 data-state 表达，不产生第二张卡", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const running = mountItem(
      makeMessage({ role: "tool", toolName: "shell", content: "…", toolRunning: true }),
      pinia,
    );
    expect(running.findAll(".tool-card")).toHaveLength(1);
    expect(running.find(".tool-card").attributes("data-state")).toBe("running");
    expect(running.find(".tool-status").text()).toBe("运行中");
    running.unmount();

    const failed = mountItem(
      makeMessage({ role: "tool", toolName: "ghost", content: "", toolOk: false, toolError: "boom" }),
      pinia,
    );
    expect(failed.findAll(".tool-card")).toHaveLength(1);
    expect(failed.find(".tool-card").attributes("data-state")).toBe("failed");
    failed.unmount();
  });

  it("工具卡：取消与「结果未收到」有自己的文案，不冒充失败或成功", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const cancelled = mountItem(
      makeMessage({
        role: "tool",
        toolName: "run_cmd",
        content: "",
        toolOk: false,
        toolStatus: "cancelled",
        toolError: "已取消",
      }),
      pinia,
    );
    expect(cancelled.find(".tool-status").text()).toBe("已取消");
    expect(cancelled.find(".tool-mark").text()).not.toBe("✓");
    expect(cancelled.find(".tool-fail-line").text()).toContain("取消");
    cancelled.unmount();

    const unknown = mountItem(
      makeMessage({
        role: "tool",
        toolName: "fs_read",
        content: "",
        toolOk: false,
        toolStatus: "unknown",
        toolError: "结果未收到",
      }),
      pinia,
    );
    expect(unknown.find(".tool-status").exists()).toBe(false);
    expect(unknown.find(".tool-fail-line").text()).toContain("结果未收到");
    expect(unknown.find(".tool-card").attributes("data-state")).toBe("failed");
    unknown.unmount();
  });

  it("工具卡展开详情有真实高度过渡（不是 v-show 瞬切）", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const w = mountItem(makeMessage({ role: "tool", toolName: "shell", content: "{}", toolOk: true }), pinia);
    const wrap = w.find(".tool-detail-wrap");
    expect(wrap.exists()).toBe(true);
    expect(wrap.classes()).not.toContain("open");
    await w.find(".tool-head").trigger("click");
    expect(w.find(".tool-detail-wrap").classes()).toContain("open");
    expect(w.find(".tool-detail-wrap .tool-detail pre").text()).toBe("{}");
    w.unmount();
  });

  it("独立任务卡：家族骨架 + 状态徽章（运行中不只是文字变色）", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const w = mountItem(
      makeMessage({ role: "subagent", toolName: "整理资料", taskStatus: "running" }),
      pinia,
    );
    const card = w.find(".subagent-card");
    expect(card.classes()).toContain("qio-card");
    expect(card.attributes("data-state")).toBe("running");
    const state = w.find(".sub-state");
    expect(state.classes()).toContain("qio-state");
    expect(w.find(".sub-kind").classes()).toContain("qio-tag");
    w.unmount();
  });
});
