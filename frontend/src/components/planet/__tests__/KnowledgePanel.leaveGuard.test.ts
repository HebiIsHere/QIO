import { describe, expect, it, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";
import KnowledgePanel from "../KnowledgePanel.vue";
import type { KnowledgeItem, TopicFingerprint } from "../../../services/api";

/**
 * PM-QIO032 #29：知识草稿的**离开统一检查**。
 *
 * 三条入口（搜索结果 requestOpenKnowledge / 列表点击 / 页面内返回）必须同一套逻辑：
 * 没改就直接跳；改了先确认。保存并打开失败（含 409）不跳转、不丢草稿。
 */

const mocks = vi.hoisted(() => ({
  refreshPending: vi.fn(async () => {}),
  apiMock: {
    listKnowledge: vi.fn<() => Promise<{ knowledge: KnowledgeItem[] }>>(async () => ({ knowledge: [] })),
    listTopics: vi.fn<() => Promise<{ topics: TopicFingerprint[] }>>(async () => ({ topics: [] })),
    createKnowledge: vi.fn(async () => ({ ok: true, knowledge: {} as KnowledgeItem })),
    verifyKnowledge: vi.fn(async () => ({ ok: true, knowledge: { id: "", state: "active" } })),
    rejectKnowledge: vi.fn(async () => ({ ok: true, knowledge: { id: "", state: "draft" } })),
    reviseKnowledge: vi.fn(async () => ({ ok: true, knowledge_id: "kn_new", version: 2 })),
    revokeKnowledge: vi.fn(async () => ({ ok: true })),
    endKnowledge: vi.fn(async () => ({ ok: true, knowledge: {} as KnowledgeItem })),
    resumeKnowledge: vi.fn(async () => ({ ok: true, knowledge: {} as KnowledgeItem })),
    setKnowledgeScope: vi.fn(async () => ({ ok: true, knowledge: {} as KnowledgeItem })),
  },
}));

vi.mock("../../../services/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../services/api")>();
  return { ...actual, api: mocks.apiMock };
});

vi.mock("../../../stores/planetPending", () => ({
  usePlanetPendingStore: () => ({ count: 0, refresh: mocks.refreshPending }),
}));

function item(id: string, content: string, version = 1): KnowledgeItem {
  return {
    id,
    category: "user_profile",
    state: "active",
    content,
    confidence: 0.9,
    topic_id: null,
    topic_name: null,
    source: "引导",
    scope: "全局（你）",
    version,
    evidence_ids: [],
    epistemic_status: "user_explicit",
    created_at: "2026-10-01T10:00:00+08:00",
    updated_at: "2026-10-02T11:00:00+08:00",
  };
}

const K1 = item("kn_1", "第一条知识");
const K2 = item("kn_2", "第二条知识");
const K3 = item("kn_3", "第三条知识");

type Exposed = {
  hasUnsavedDraft(): boolean;
  requestOpenKnowledge(id: string): Promise<boolean>;
};

function mountPanel(props: Record<string, unknown> = {}, attach = false) {
  const pinia = createPinia();
  setActivePinia(pinia);
  return mount(KnowledgePanel, {
    props,
    global: { plugins: [pinia] },
    attachTo: attach ? document.body : undefined,
  });
}

function exposed(w: ReturnType<typeof mountPanel>): Exposed {
  return w.vm as unknown as Exposed;
}

async function openFirst(w: ReturnType<typeof mountPanel>) {
  await flushPromises();
  await w.find('[data-k-id="kn_1"]').trigger("click");
  await nextTick();
}

async function startDirty(w: ReturnType<typeof mountPanel>, text = "改过但没保存的内容") {
  await w.find('[data-k-edit]').trigger("click");
  await w.find('[data-k-draft] textarea').setValue(text);
  await nextTick();
}

function detailText(w: ReturnType<typeof mountPanel>): string {
  return w.find('[data-k-detail]').text();
}

function conflictError(version = 2) {
  return Object.assign(new Error("/api/knowledge/kn_1/revise -> 409: 版本冲突"), {
    status: 409,
    body: { conflict: true, current_version: version, current_id: "kn_v2", error: "版本冲突" },
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.apiMock.listKnowledge.mockResolvedValue({ knowledge: [K1, K2, K3] });
  mocks.apiMock.listTopics.mockResolvedValue({ topics: [] });
  mocks.apiMock.reviseKnowledge.mockResolvedValue({ ok: true, knowledge_id: "kn_new", version: 2 });
});

describe("#29 hasUnsavedDraft", () => {
  it("只有真的改过内容才算未保存草稿", async () => {
    const w = mountPanel();
    await openFirst(w);
    expect(exposed(w).hasUnsavedDraft()).toBe(false);

    await w.find('[data-k-edit]').trigger("click");
    expect(exposed(w).hasUnsavedDraft(), "进入编辑但没改，不算未保存").toBe(false);

    await w.find('[data-k-draft] textarea').setValue("改了内容");
    expect(exposed(w).hasUnsavedDraft()).toBe(true);

    await w.find('[data-k-cancel]').trigger("click");
    expect(exposed(w).hasUnsavedDraft()).toBe(false);
    w.unmount();
  });
});

describe("#29 requestOpenKnowledge：无草稿直接打开", () => {
  it("resolve true 并打开目标知识", async () => {
    const w = mountPanel();
    await openFirst(w);

    const opened = await exposed(w).requestOpenKnowledge("kn_2");
    await flushPromises();

    expect(opened).toBe(true);
    expect(detailText(w)).toContain("第二条知识");
    expect(w.find('[data-unsaved-guard]').exists()).toBe(false);
    w.unmount();
  });

  it("目标不存在时 resolve false，且不丢当前详情", async () => {
    const w = mountPanel();
    await openFirst(w);

    const opened = await exposed(w).requestOpenKnowledge("kn_missing");
    await flushPromises();

    expect(opened).toBe(false);
    expect(detailText(w)).toContain("第一条知识");
    expect(w.text()).toContain("暂时不在当前列表");
    w.unmount();
  });
});

describe("#29 requestOpenKnowledge：有草稿先确认", () => {
  it("显示同一确认层与三个真实动作；继续编辑 resolve false 且草稿还在", async () => {
    const w = mountPanel();
    await openFirst(w);
    await startDirty(w, "我的修改");

    const request = exposed(w).requestOpenKnowledge("kn_2");
    await flushPromises();

    const guard = w.find('[data-unsaved-guard]');
    expect(guard.exists()).toBe(true);
    expect(guard.text()).toContain("还有未保存的修改");
    expect(w.find('[data-unsaved-save-open]').text()).toBe("保存并打开");
    expect(w.find('[data-unsaved-discard]').text()).toBe("放弃并打开");
    expect(w.find('[data-unsaved-keep]').text()).toBe("继续编辑");
    // 不再重复解释「返回会丢修改」
    expect(guard.text()).not.toContain("已保存的内容不受影响");

    await w.find('[data-unsaved-keep]').trigger("click");
    expect(await request).toBe(false);
    await flushPromises();
    expect(w.find('[data-unsaved-guard]').exists()).toBe(false);
    expect((w.find('[data-k-draft] textarea').element as HTMLTextAreaElement).value).toBe("我的修改");
    // 仍停在原条目（详情编号 kn_1，且没有跳到第二条）
    expect(detailText(w)).toContain("kn_1");
    expect(detailText(w)).not.toContain("第二条知识");
    expect(mocks.apiMock.reviseKnowledge).not.toHaveBeenCalled();
    w.unmount();
  });

  it("放弃并打开：丢弃草稿、跳到目标、resolve true（不保存）", async () => {
    const w = mountPanel();
    await openFirst(w);
    await startDirty(w);

    const request = exposed(w).requestOpenKnowledge("kn_2");
    await flushPromises();
    await w.find('[data-unsaved-discard]').trigger("click");
    expect(await request).toBe(true);
    await flushPromises();

    expect(detailText(w)).toContain("第二条知识");
    expect(w.find('[data-unsaved-guard]').exists()).toBe(false);
    expect(w.find('[data-k-draft]').exists()).toBe(false);
    expect(mocks.apiMock.reviseKnowledge).not.toHaveBeenCalled();
    w.unmount();
  });

  it("保存并打开：先保存（带 CAS），成功后才跳转并 resolve true", async () => {
    const w = mountPanel();
    await openFirst(w);
    await startDirty(w, "要保存的修改");

    const request = exposed(w).requestOpenKnowledge("kn_2");
    await flushPromises();
    await w.find('[data-unsaved-save-open]').trigger("click");
    expect(await request).toBe(true);
    await flushPromises();

    expect(mocks.apiMock.reviseKnowledge).toHaveBeenCalledTimes(1);
    expect(mocks.apiMock.reviseKnowledge).toHaveBeenCalledWith("kn_1", "要保存的修改", {
      expectedVersion: 1,
    });
    expect(detailText(w)).toContain("第二条知识");
    w.unmount();
  });

  it("保存失败（网络/服务错误）：停留原条目、保留草稿、显示真实原因、resolve false", async () => {
    mocks.apiMock.reviseKnowledge.mockRejectedValueOnce(new Error("offline"));
    const w = mountPanel();
    await openFirst(w);
    await startDirty(w, "没保存成功的修改");

    const request = exposed(w).requestOpenKnowledge("kn_2");
    await flushPromises();
    await w.find('[data-unsaved-save-open]').trigger("click");
    expect(await request).toBe(false);
    await flushPromises();

    expect(w.find('[data-unsaved-guard]').exists()).toBe(false);
    expect(detailText(w)).toContain("offline");
    expect((w.find('[data-k-draft] textarea').element as HTMLTextAreaElement).value).toBe(
      "没保存成功的修改",
    );
    expect(w.find('[data-k-draft]').exists()).toBe(true);
    w.unmount();
  });

  it("保存 409：停留原条目、保留草稿、显示服务端当前版本，resolve false", async () => {
    mocks.apiMock.reviseKnowledge.mockRejectedValueOnce(conflictError(2));
    mocks.apiMock.listKnowledge
      .mockResolvedValueOnce({ knowledge: [K1, K2, K3] })
      .mockResolvedValueOnce({
        knowledge: [{ ...K1, content: "别处改过的内容", version: 2 }, K2, K3],
      });
    const w = mountPanel();
    await openFirst(w);
    await startDirty(w, "我的修改");

    const request = exposed(w).requestOpenKnowledge("kn_2");
    await flushPromises();
    await w.find('[data-unsaved-save-open]').trigger("click");
    expect(await request).toBe(false);
    await flushPromises();

    expect((w.find('[data-k-draft] textarea').element as HTMLTextAreaElement).value).toBe("我的修改");
    expect(detailText(w)).toContain("被更新过");
    expect(detailText(w)).toContain("别处改过的内容");
    expect(detailText(w)).not.toContain("第二条知识");
    w.unmount();
  });
});

describe("#29 确认期间再次请求另一目标", () => {
  it("只认最后一次明确选择：旧请求 resolve false，最终打开新的目标", async () => {
    const w = mountPanel();
    await openFirst(w);
    await startDirty(w, "草稿");

    const first = exposed(w).requestOpenKnowledge("kn_2");
    await flushPromises();
    const second = exposed(w).requestOpenKnowledge("kn_3");
    await flushPromises();

    expect(await first).toBe(false);
    // 确认层仍然只有一层，动作作用于最后的目标
    expect(w.findAll('[data-unsaved-guard]')).toHaveLength(1);

    await w.find('[data-unsaved-discard]').trigger("click");
    expect(await second).toBe(true);
    await flushPromises();
    expect(detailText(w)).toContain("第三条知识");
    expect(detailText(w)).not.toContain("第二条知识");
    w.unmount();
  });
});

describe("#29 键盘可达性与焦点", () => {
  it("确认层出现时接管焦点；Esc 等同继续编辑，焦点回到草稿输入框", async () => {
    const w = mountPanel({}, true);
    await openFirst(w);
    await startDirty(w, "键盘修改");

    const request = exposed(w).requestOpenKnowledge("kn_2");
    await flushPromises();
    const guard = w.find('[data-unsaved-guard]');
    expect(guard.exists()).toBe(true);
    expect(guard.element.contains(document.activeElement)).toBe(true);
    expect(guard.attributes("aria-modal")).toBe("true");

    await guard.trigger("keydown", { key: "Escape" });
    expect(await request).toBe(false);
    await flushPromises();
    expect(w.find('[data-unsaved-guard]').exists()).toBe(false);
    expect(document.activeElement).toBe(w.find('[data-k-draft] textarea').element);
    w.unmount();
  });

  it("页面内返回也走同一确认层（动作按返回语义命名）", async () => {
    const w = mountPanel();
    await openFirst(w);
    await startDirty(w);

    await w.find('[data-k-back]').trigger("click");
    await flushPromises();
    const guard = w.find('[data-unsaved-guard]');
    expect(guard.exists()).toBe(true);
    expect(guard.text()).toContain("保存并返回");
    expect(guard.text()).toContain("放弃并返回");
    expect(w.find('[data-k-list]').element as HTMLElement).toHaveProperty("style");

    await w.find('[data-unsaved-discard]').trigger("click");
    await flushPromises();
    expect((w.find('[data-k-list]').element as HTMLElement).style.display).not.toBe("none");
    w.unmount();
  });
});
