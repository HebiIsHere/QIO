/**
 * D 独立验收：历史附件的打开与重新定位入口（审计问题 5 / plan §1.6）。
 *
 * 契约来源：docs/plans/2026-10-06-audit-seven-fixes.md §0 第 5 条 + §1.6。
 * 只依据产品规则：
 *
 *   * 历史里的副本附件必须有**实际可用的打开入口**（按钮，不是标签），
 *     点击走带认证头的 fetch 去取 QIO 管理的副本（不新增无认证裸链接）；
 *   * 引用型附件必须有**重新定位**入口（文件被移动/改名后重新指定位置），
 *     点下去必须有可观察的反馈（不能是装饰按钮）；
 *   * 状态诚实：missing / changed 不得显示成「可以打开」；
 *   * 只有 id 没有元数据时如实说「元数据未加载」，不编造文件名与状态。
 *
 * DOM 锚点（B 提供，用于问题 5 的 DOM 验收）：
 *   [data-test="message-attachments"]              附件行容器
 *   [data-test="message-attachment"]               单个附件（带 data-id / data-kind / data-state）
 *   [data-test="attachment-notice"]                操作反馈
 *   .attach-note                                   无元数据兜底
 *   打开 / 重定位按钮由 C 的 AttachmentChip 提供
 *
 * 基线（e428bb9）现状：MessageItem.vue:244-260 只把附件渲染成 span（名称/大小/状态），
 * 前端 relocateAttachment 定义了但没有任何调用点 —— 因此本文件在修复前应当是**红的**。
 *
 * 运行：cd frontend; npx vitest run src/components/__tests__/HistoryAttachmentOpen.audit.verify.test.ts
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount, type DOMWrapper, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";
import MessageItem from "../MessageItem.vue";
import type { MessageAttachment, StreamMessage } from "../../stores/session";

/**
 * 可控的原生选择器替身：默认「选到了新位置」，让「重新定位」这条链路可以被真正断言
 * （浏览器里 pickLocalPath 返回 null = 没有选择器/用户取消，另有一条用例专门断言
 * 「不能假装成功」）。只替换这一个函数，其余附件服务保持真实实现。
 */
const pickLocalPathMock = vi.fn<() => Promise<string | null>>(async () => "D:\\新位置\\大素材.mov");

vi.mock("../../services/attachments", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../services/attachments")>();
  return { ...actual, pickLocalPath: (...args: []) => pickLocalPathMock(...args) };
});

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

/** 健康的引用型：只记位置、内容不保证仍在 —— **没有**重新定位需求。 */
const REFERENCE: MessageAttachment = {
  id: "att_ref_1",
  name: "大素材.mov",
  sizeBytes: 300_000_000,
  kind: "reference",
  display: "引用本地文件",
  state: "ready",
};

/** 引用失效（文件被移动/删除）：这时才需要「重新定位」。 */
const REFERENCE_MISSING: MessageAttachment = {
  id: "att_ref_missing",
  name: "被移动的大素材.mov",
  sizeBytes: 300_000_000,
  kind: "reference",
  display: "引用本地文件",
  state: "missing",
  error: "文件不在原位了（联网盘断开、被移动或被删除）；可以重新指定位置",
};

/** 引用内容有变化：同样允许重新指定位置。 */
const REFERENCE_CHANGED: MessageAttachment = {
  id: "att_ref_changed",
  name: "换过内容的大素材.mov",
  sizeBytes: 300_000_000,
  kind: "reference",
  display: "引用本地文件",
  state: "changed",
  error: "源文件内容与登记时不同",
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

const fetchCalls: { url: string; method: string; headers: Record<string, string> }[] = [];

function installFetch() {
  fetchCalls.length = 0;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      fetchCalls.push({
        url: String(url),
        method: String(init?.method ?? "GET"),
        headers: (init?.headers as Record<string, string>) ?? {},
      });
      // 取副本内容 → 二进制；其余（relocate / 元数据）→ JSON 信封
      if (String(url).endsWith("/content")) {
        return new Response(new Blob(["副本内容"]), {
          status: 200,
          headers: { "Content-Type": "application/octet-stream" },
        });
      }
      return new Response(
        JSON.stringify({
          ok: true,
          attachment: {
            id: "att_ref_missing",
            name: "大素材.mov",
            size_bytes: 300_000_000,
            kind: "reference",
            state: "ready",
            error: null,
          },
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    }),
  );
}

function userMessage(attachments: MessageAttachment[], attachmentIds?: string[]): StreamMessage {
  return {
    id: "m_user_1",
    role: "user",
    content: "这是历史消息",
    contentType: "text",
    createdAt: "2026-10-06T08:00:00+00:00",
    attachmentIds: attachmentIds ?? attachments.map((a) => a.id),
    ...(attachments.length ? { attachments } : {}),
  } as StreamMessage;
}

async function settle(): Promise<void> {
  await flushPromises();
  await nextTick();
  await nextTick();
  await new Promise((resolve) => setTimeout(resolve, 0));
  await nextTick();
}

async function mountItem(message: StreamMessage): Promise<VueWrapper> {
  const pinia = createPinia();
  setActivePinia(pinia);
  const wrapper = mount(MessageItem, {
    props: { message },
    global: { plugins: [pinia] },
  });
  await settle();
  return wrapper;
}

/** 附件行容器（B 提供的锚点）。 */
function attachmentRow(wrapper: VueWrapper): DOMWrapper<Element> {
  const row = wrapper.find('[data-test="message-attachments"]');
  expect(row.exists(), '契约 §1.6：历史附件必须有锚点 [data-test="message-attachments"]').toBe(true);
  return row as DOMWrapper<Element>;
}

/** 单个附件（带 data-id / data-kind / data-state）。 */
function attachmentItem(wrapper: VueWrapper, id: string): DOMWrapper<Element> {
  const items = attachmentRow(wrapper).findAll('[data-test="message-attachment"]');
  expect(items.length, '每个附件都要有 [data-test="message-attachment"] 锚点').toBeGreaterThan(0);
  const found = items.find((item) => item.attributes("data-id") === id);
  expect(found, "附件锚点必须带真实 data-id：" + id).toBeTruthy();
  return found as DOMWrapper<Element>;
}

/** 在附件卡里找真实可点的按钮（先卡内，再退到整行）。 */
function findEntry(wrapper: VueWrapper, scope: DOMWrapper<Element>, pattern: RegExp) {
  for (const container of [scope, attachmentRow(wrapper)]) {
    const hit = container.findAll("button").find((button) => {
      const label =
        button.text() +
        " " +
        String(button.attributes("aria-label") ?? "") +
        " " +
        String(button.attributes("title") ?? "");
      return pattern.test(label) && button.attributes("disabled") === undefined;
    });
    if (hit) return hit;
  }
  return undefined;
}

function noticeText(wrapper: VueWrapper): string {
  const notice = wrapper.find('[data-test="attachment-notice"]');
  return notice.exists() ? notice.text() : "";
}

beforeEach(() => {
  installFetch();
  pickLocalPathMock.mockReset();
  pickLocalPathMock.mockImplementation(async () => "D:\\新位置\\大素材.mov");
});

describe("契约 §1.6：历史附件必须有真实入口", () => {
  it("副本附件：真实可点，并且真的去取 /api/attachments/{id}/content（带认证头）", async () => {
    const wrapper = await mountItem(userMessage([COPY]));
    const item = attachmentItem(wrapper, COPY.id);
    expect(item.attributes("data-kind"), "锚点要带上真实保存方式").toBe("copy");
    expect(item.attributes("data-state"), "锚点要带上真实状态").toBe("ready");

    const open = findEntry(wrapper, item, /打开|查看|下载/);
    expect(open, "历史里的副本附件必须有可用的打开入口（不能只有一个标签）").toBeTruthy();
    expect(open!.attributes("disabled"), "ready 的副本必须可以打开").toBeUndefined();

    await open!.trigger("click");
    await settle();

    const hit = fetchCalls.find((call) => call.url.includes("/api/attachments/att_copy_1/content"));
    expect(
      hit,
      "打开入口必须走 /api/attachments/{id}/content 取 QIO 管理的副本，不能只是装饰",
    ).toBeTruthy();
    const headers = Object.keys(hit!.headers).join(",").toLowerCase();
    expect(headers, "取副本必须带认证头（不新增无认证裸链接）").toMatch(/authorization/);
    expect(hit!.method.toUpperCase(), "取内容必须是只读请求").toBe("GET");
    wrapper.unmount();
  });

  it("引用失效（missing / changed）：真实可点的重新定位入口，点下去必须有可观察反馈", async () => {
    for (const fixture of [REFERENCE_MISSING, REFERENCE_CHANGED]) {
      const wrapper = await mountItem(userMessage([fixture]));
      const item = attachmentItem(wrapper, fixture.id);
      expect(item.attributes("data-kind"), "引用型必须标成 reference").toBe("reference");
      expect(item.attributes("data-state"), "失效状态必须如实写在锚点上").toBe(fixture.state);

      const relocate = findEntry(wrapper, item, /重新定位|指定位置|重新指定|重新选择/);
      expect(
        relocate,
        "契约 §1.6：引用失效时必须给「重新定位」入口（" + fixture.state + "）",
      ).toBeTruthy();

      pickLocalPathMock.mockResolvedValueOnce("D:\\新位置\\大素材.mov");
      await relocate!.trigger("click");
      await settle();

      const hit = fetchCalls.find((call) =>
        call.url.includes("/api/attachments/" + fixture.id + "/relocate"),
      );
      expect(
        hit,
        "点了「重新定位」必须把新位置真的交给后端（POST /api/attachments/{id}/relocate），不能是装饰按钮",
      ).toBeTruthy();
      expect(hit!.method.toUpperCase()).toBe("POST");
      expect(
        JSON.stringify(hit!.headers).toLowerCase(),
        "重新定位同样要带认证头",
      ).toContain("authorization");
      expect(noticeText(wrapper).trim().length, "动作之后必须给用户可观察反馈").toBeGreaterThan(0);
      wrapper.unmount();
    }
  });

  it("没有选择器 / 用户取消（pickLocalPath 返回 null）：不得假装重新定位成功", async () => {
    const wrapper = await mountItem(userMessage([REFERENCE_MISSING]));
    const item = attachmentItem(wrapper, REFERENCE_MISSING.id);
    const relocate = findEntry(wrapper, item, /重新定位|指定位置|重新指定|重新选择/);
    expect(relocate, "引用失效必须有重新定位入口").toBeTruthy();

    pickLocalPathMock.mockResolvedValueOnce(null);
    await relocate!.trigger("click");
    await settle();

    expect(
      fetchCalls.some((call) => call.url.includes("/relocate")),
      "没有拿到新路径时不得向后端提交重定位",
    ).toBe(false);
    expect(noticeText(wrapper), "不得宣称「已重新定位」").not.toMatch(/已重新定位|成功/);
    wrapper.unmount();
  });

  it("健康的引用型：说明保存方式与 caveat，且**不**给假的重新定位入口", async () => {
    const wrapper = await mountItem(userMessage([REFERENCE]));
    const item = attachmentItem(wrapper, REFERENCE.id);
    expect(item.attributes("data-kind")).toBe("reference");
    expect(item.attributes("data-state")).toBe("ready");

    expect(item.text(), "健康的引用型必须写清保存方式是「引用本地文件」").toContain("引用本地文件");
    expect(
      item.html(),
      "必须给出「历史保留的是位置，不保证内容仍然存在」的说明（契约 §1.6）",
    ).toContain("不保证内容仍然存在");
    expect(
      findEntry(wrapper, item, /重新定位|指定位置|重新指定|重新选择/),
      "内容仍在原位的引用型没有重新定位需求：不得显示一个假的入口",
    ).toBeUndefined();
    wrapper.unmount();
  });

  it("状态诚实：missing 的附件不得被当成可以打开（状态可机器检查）", async () => {
    const wrapper = await mountItem(userMessage([MISSING]));
    const item = attachmentItem(wrapper, MISSING.id);
    expect(item.attributes("data-state"), "缺失状态必须如实写在锚点上").toBe("missing");

    const open = findEntry(wrapper, item, /打开|查看|下载/);
    if (open) {
      expect(
        /不|失败|缺失|无法|重新/.test(open.text() + String(open.attributes("title") ?? "")),
        "打开入口存在但文件已经不在了 → 必须明确说明原因（或给重新定位）",
      ).toBe(true);
    }
    expect(wrapper.text(), "必须如实说明文件不在原位").toMatch(/不在了|不在原位|缺失|missing/);
    wrapper.unmount();
  });

  it("只有 id 没有元数据：如实说「元数据未加载」，不编造文件名", async () => {
    const wrapper = await mountItem(userMessage([], ["att_meta_missing"]));
    // 这条只断言「没有元数据就如实说」：行容器在有真实元数据时才必须出现，
    // 契约没有要求「只有 id」时也渲染附件行锚点。
    const note = wrapper.find(".attach-note");
    expect(note.exists(), "没有元数据时必须给兜底说明（.attach-note）").toBe(true);
    expect(note.text()).toMatch(/元数据未加载|未加载/);
    expect(note.text(), "不得编造文件名").not.toContain(".pdf");
    wrapper.unmount();
  });
});
