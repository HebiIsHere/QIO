/**
 * D 独立验收：历史附件的打开与重新定位入口（审计问题 5 / plan §1.6）。
 *
 * 契约来源：docs/plans/2026-10-06-audit-seven-fixes.md §0 第 5 条 + §1.6。
 * 只依据产品规则：
 *
 *   * 历史里的副本附件必须有**实际可用的打开入口**（按钮，不是标签），
 *     点击走带认证头的 fetch 去取 QIO 管理的副本（不新增无认证裸链接）；
 *   * 引用型附件必须有**重新定位**入口（文件被移动/改名后重新指定位置）；
 *   * 状态诚实：missing / changed 不得显示成「可以打开」。
 *
 * 基线（e428bb9）现状：MessageItem.vue:244-260 只把附件渲染成 span（名称/大小/状态），
 * 前端 relocateAttachment 定义了但没有任何调用点 —— 因此本文件在修复前应当是**红的**。
 *
 * 运行：cd frontend; npx vitest run src/components/__tests__/HistoryAttachmentOpen.audit.verify.test.ts
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";
import MessageItem from "../MessageItem.vue";
import type { MessageAttachment, StreamMessage } from "../../stores/session";

vi.mock("../../services/backend", () => ({
  resolveBackend: vi.fn(async () => ({ base: "http://127.0.0.1:8799", token: "tk_audit" })),
  authHeaders: vi.fn((token: string) => (token ? { Authorization: "Bearer " + token } : {})),
  resetBackend: vi.fn(),
}));

vi.mock("../../services/api", () => {
  const payload = () => ({
    tasks: [],
    messages: [],
    approvals: [],
    tools: [],
    narratives: [],
    instance_id: "inst_audit",
    turn_queue: { running: null, queued: [], cancelled: [], revision: 0, instance_id: "inst_audit" },
  });
  return {
    api: new Proxy({}, { get: () => vi.fn(async () => payload()) }),
    ApiError: class ApiError extends Error {},
  };
});

const COPY: MessageAttachment = {
  id: "att_copy_1",
  name: "季度报告.pdf",
  sizeBytes: 123456,
  kind: "copy",
  display: "已保存副本",
  state: "ready",
};

const REFERENCE: MessageAttachment = {
  id: "att_ref_1",
  name: "大素材.mov",
  sizeBytes: 300_000_000,
  kind: "reference",
  display: "引用本地文件",
  state: "ready",
};

const MISSING: MessageAttachment = {
  id: "att_missing_1",
  name: "被移动走了.txt",
  sizeBytes: 1024,
  kind: "copy",
  display: "已保存副本",
  state: "missing",
  error: "文件不在原位了",
};

const fetchCalls: { url: string; headers: Record<string, string> }[] = [];

function installFetch() {
  fetchCalls.length = 0;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      fetchCalls.push({
        url: String(url),
        headers: (init?.headers as Record<string, string>) ?? {},
      });
      return new Response(new Blob(["副本内容"]), {
        status: 200,
        headers: { "Content-Type": "application/octet-stream" },
      });
    }),
  );
}

function userMessage(attachments: MessageAttachment[]): StreamMessage {
  return {
    id: "m_user_1",
    role: "user",
    content: "这是历史消息",
    contentType: "text",
    createdAt: "2026-10-06T08:00:00+00:00",
    attachmentIds: attachments.map((a) => a.id),
    attachments,
  } as StreamMessage;
}

async function settle(): Promise<void> {
  await flushPromises();
  await nextTick();
  await nextTick();
  await new Promise((resolve) => setTimeout(resolve, 0));
  await nextTick();
}

async function mountItem(attachments: MessageAttachment[]): Promise<VueWrapper> {
  const pinia = createPinia();
  setActivePinia(pinia);
  const wrapper = mount(MessageItem, {
    props: { message: userMessage(attachments) },
    global: { plugins: [pinia] },
  });
  await settle();
  return wrapper;
}

function buttons(wrapper: VueWrapper) {
  return wrapper.findAll("button").filter((b) => !b.classes().includes("copy-btn"));
}

function findEntry(wrapper: VueWrapper, pattern: RegExp) {
  return buttons(wrapper).find((button) => {
    const label = button.text() + " " + String(button.attributes("aria-label") ?? "") + " " + String(button.attributes("title") ?? "");
    return pattern.test(label);
  });
}

beforeEach(() => {
  installFetch();
});

describe("契约 §1.6：历史附件必须有真实入口", () => {
  it("副本附件有可点击的打开入口，并且真的去取 /content（带认证头）", async () => {
    const wrapper = await mountItem([COPY]);
    const open = findEntry(wrapper, /打开|查看|下载/);
    expect(
      open,
      "历史里的副本附件必须有可用的打开入口（不能只有一个标签）",
    ).toBeTruthy();
    expect(open!.attributes("disabled"), "ready 的副本必须可以打开").toBeUndefined();

    await open!.trigger("click");
    await settle();

    const hit = fetchCalls.find((c) => c.url.includes("/api/attachments/att_copy_1/content"));
    expect(
      hit,
      "打开入口必须走 /api/attachments/{id}/content 取 QIO 管理的副本，不能只是装饰",
    ).toBeTruthy();
    const headers = Object.keys(hit!.headers).join(",").toLowerCase();
    expect(headers, "取副本必须带认证头（不新增无认证裸链接）").toMatch(/authorization/);
    wrapper.unmount();
  });

  it("引用型附件有重新定位入口（文件被移动之后要能重新指定位置）", async () => {
    const wrapper = await mountItem([REFERENCE]);
    const relocate = findEntry(wrapper, /重新定位|指定位置|重新指定|重新选择/);
    expect(
      relocate,
      "引用型附件必须提供「重新定位」入口（前端 relocateAttachment 目前没有任何调用点）",
    ).toBeTruthy();
    wrapper.unmount();
  });

  it("状态诚实：missing 的附件不得被当成可以打开", async () => {
    const wrapper = await mountItem([MISSING]);
    const open = findEntry(wrapper, /打开|查看|下载/);
    if (open) {
      expect(
        open.attributes("disabled") !== undefined || /不|失败|缺失|无法/.test(open.attributes("title") ?? ""),
        "打开入口存在但文件已经不在了 → 必须禁用或明确说明原因",
      ).toBe(true);
    }
    const text = wrapper.text();
    expect(text, "必须如实说明文件不在原位").toMatch(/不在了|不在原位|缺失|missing/);
    wrapper.unmount();
  });
});
