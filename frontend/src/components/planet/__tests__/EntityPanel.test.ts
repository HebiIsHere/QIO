import { describe, expect, it, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";
import EntityPanel from "../EntityPanel.vue";
import type { EntityCard } from "../../../services/api";

const mocks = vi.hoisted(() => ({
  refreshPending: vi.fn(async () => {}),
  apiMock: {
    listEntities: vi.fn<() => Promise<{ entities: EntityCard[] }>>(async () => ({ entities: [] })),
    revokeEntity: vi.fn(async (_id: string) => ({ ok: true })),
    reviseEntity: vi.fn(async () => ({ ok: true, entity: null })),
  },
  candidatesMock: {
    listEntityCandidates: vi.fn(async () => ({
      entity: null,
      candidates: [],
      total: 0,
      truncated: false,
    })),
    adoptCandidate: vi.fn(async () => ({ ok: true, entity: null, adopted: null })),
    dismissCandidate: vi.fn(async () => ({ ok: true, dismissed: true })),
  },
}));

vi.mock("../../../services/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../services/api")>();
  return { ...actual, api: mocks.apiMock };
});

vi.mock("../../../services/entityCandidatesApi", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../services/entityCandidatesApi")>();
  return { ...actual, ...mocks.candidatesMock };
});

vi.mock("../../../stores/planetPending", () => ({
  usePlanetPendingStore: () => ({ count: 0, refresh: mocks.refreshPending }),
}));

const CARD: EntityCard = {
  id: "ec_1",
  node_id: "n1",
  name: "王翠华",
  aliases: ["妈"],
  kind: "家人",
  summary: "我妈妈",
  attributes: [{ key: "生日", value: "3 月 5 日", confidence: 0.9 }],
  relations: [{ type: "女儿", target: "祠莎" }],
  state: "active",
  revision: 3,
  identity: "用户家里的长辈",
  creation_reason: "持续对话出现",
  evidence_ids: ["msg_1"],
  created_at: "2026-10-01T10:00:00+08:00",
  updated_at: "2026-10-02T11:00:00+08:00",
};

function mountPanel(props: Record<string, unknown> = {}) {
  const pinia = createPinia();
  setActivePinia(pinia);
  return mount(EntityPanel, { props, global: { plugins: [pinia] } });
}

function detailVisible(w: ReturnType<typeof mountPanel>): boolean {
  return (w.find('[data-e-detail]').element as HTMLElement).style.display !== "none";
}
function listVisible(w: ReturnType<typeof mountPanel>): boolean {
  return (w.find('[data-e-list]').element as HTMLElement).style.display !== "none";
}
async function openFirst(w: ReturnType<typeof mountPanel>) {
  await flushPromises();
  await w.find('[data-e-id="ec_1"]').trigger("click");
  await nextTick();
  return w;
}
function conflictError(revision = 4) {
  return Object.assign(new Error("/api/entities/ec_1/revise -> 409: 版本冲突"), {
    status: 409,
    body: { conflict: true, current_revision: revision, error: "版本冲突" },
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.apiMock.listEntities.mockResolvedValue({ entities: [CARD] });
  mocks.apiMock.reviseEntity.mockResolvedValue({ ok: true, entity: null });
  mocks.candidatesMock.listEntityCandidates.mockResolvedValue({
    entity: null,
    candidates: [],
    total: 0,
    truncated: false,
  });
});

describe("EntityPanel 空态与失败区分", () => {
  it("加载失败：显示可读错误与重试入口，不显示「无实体卡」", async () => {
    mocks.apiMock.listEntities.mockRejectedValueOnce(new Error("offline"));
    const w = mountPanel();
    await flushPromises();
    const err = w.find(".e-load-error");
    expect(err.exists()).toBe(true);
    expect(err.text()).toContain("offline");
    expect(w.text()).not.toContain("暂无实体卡");

    mocks.apiMock.listEntities.mockResolvedValueOnce({ entities: [CARD] });
    await err.find("button").trigger("click");
    await flushPromises();
    expect(w.find(".e-load-error").exists()).toBe(false);
    expect(w.text()).toContain("王翠华");
    w.unmount();
  });

  it("「暂无实体卡」与「没有匹配」用不同文案，并能一键清除搜索", async () => {
    mocks.apiMock.listEntities.mockResolvedValue({ entities: [] });
    const empty = mountPanel();
    await flushPromises();
    expect(empty.find(".e-empty").text()).toContain("暂无实体卡");
    empty.unmount();

    mocks.apiMock.listEntities.mockResolvedValue({ entities: [CARD] });
    const w = mountPanel();
    await flushPromises();
    await w.find('[data-e-search]').setValue("不存在");
    await flushPromises();
    expect(w.find(".e-empty").text()).toContain("没有匹配");
    await w.find(".e-clear-search").trigger("click");
    await flushPromises();
    expect(w.find('[data-e-id="ec_1"]').exists()).toBe(true);
    w.unmount();
  });
});

describe("EntityPanel 列表 → 详情 → 返回", () => {
  it("点条目打开详情，返回保留搜索与滚动位置", async () => {
    const w = mountPanel();
    await flushPromises();
    await w.find('[data-e-search]').setValue("王");
    await flushPromises();
    const root = w.find('[data-scroll="entity-list"]').element as HTMLElement;
    root.scrollTop = 96;

    await w.find('[data-e-id="ec_1"]').trigger("click");
    await nextTick();
    expect(detailVisible(w)).toBe(true);
    expect(listVisible(w)).toBe(false);

    await w.find('[data-e-back]').trigger("click");
    await flushPromises();
    await nextTick();
    expect(listVisible(w)).toBe(true);
    expect((w.find('[data-e-search]').element as HTMLInputElement).value).toBe("王");
    expect(root.scrollTop).toBe(96);
    w.unmount();
  });

  it("阅读态保留基线能力：摘要/类别/别名/属性（含置信度）/关系/身份/创建依据/证据数/修订", async () => {
    const w = mountPanel();
    await openFirst(w);
    const text = w.find('[data-e-detail]').text();
    expect(text).toContain("我妈妈");
    expect(text).toContain("家人");
    expect(text).toContain("妈");
    expect(text).toContain("生日 = 3 月 5 日");
    expect(text).toContain("0.90");
    expect(text).toContain("女儿 → 祠莎");
    expect(text).toContain("用户家里的长辈");
    expect(text).toContain("持续对话出现");
    expect(text).toContain("依据 1 条");
    expect(text).toContain("v3");
    // 阅读态没有输入框
    expect(w.find('[data-e-detail] textarea').exists()).toBe(false);
  });

  it("归档先用确认层说明影响，确认后才调用接口", async () => {
    const w = mountPanel();
    await openFirst(w);
    await w.find(".e-revoke").trigger("click");
    const confirm = w.find('[data-e-detail] .qio-confirm');
    expect(confirm.exists()).toBe(true);
    expect(confirm.text()).toContain("归档这张实体卡？");
    expect(confirm.text()).toContain("王翠华");
    expect(mocks.apiMock.revokeEntity).not.toHaveBeenCalled();
    await confirm.findAll("button")[1].trigger("click");
    await flushPromises();
    expect(mocks.apiMock.revokeEntity).toHaveBeenCalledWith("ec_1");
    w.unmount();
  });
});

describe("EntityPanel 草稿化编辑（§6 关键要求）", () => {
  it("进入编辑只是本地草稿：不提交任何请求", async () => {
    const w = mountPanel();
    await openFirst(w);
    await w.find('[data-e-edit]').trigger("click");
    expect(w.find('[data-e-draft]').exists()).toBe(true);
    expect(mocks.apiMock.reviseEntity).not.toHaveBeenCalled();
    await w.find(".e-summary").setValue("我的妈妈（改）");
    expect(mocks.apiMock.reviseEntity).not.toHaveBeenCalled();
    w.unmount();
  });

  it("添加属性只改本地草稿（基线会立刻 saveCard，必须改掉）", async () => {
    const w = mountPanel();
    await openFirst(w);
    await w.find('[data-e-edit]').trigger("click");
    await w.find(".e-attr-key").setValue("住址");
    await w.find(".e-attr-value").setValue("成都");
    await w.find(".e-attr-add").trigger("click");
    await nextTick();
    expect(mocks.apiMock.reviseEntity).not.toHaveBeenCalled();
    expect(w.find('[data-e-draft]').text()).toContain("住址");
    w.unmount();
  });

  it("增删关系只改本地草稿", async () => {
    const w = mountPanel();
    await openFirst(w);
    await w.find('[data-e-edit]').trigger("click");
    // 删掉已有关系
    await w.find(".e-rel-del").trigger("click");
    // 添加新关系
    await w.find(".e-rel-type").setValue("照顾");
    await w.find(".e-rel-target").setValue("祠莎");
    await w.find(".e-rel-add").trigger("click");
    await nextTick();
    expect(mocks.apiMock.reviseEntity).not.toHaveBeenCalled();
    const draftText = w.find('[data-e-draft]').text();
    expect(draftText).toContain("照顾 → 祠莎");
    expect(draftText).not.toContain("女儿 → 祠莎");
    w.unmount();
  });

  it("保存一次性提交字段 + 关系增删，并带上 expected_revision", async () => {
    const w = mountPanel();
    await openFirst(w);
    await w.find('[data-e-edit]').trigger("click");
    await w.find(".e-summary").setValue("我妈妈（新）");
    // 已有属性的值输入框（草稿里就地可改；基线只能在「添加属性」里 upsert）
    await w.find(".e-attr-edit .grow").setValue("3 月 6 日");
    await w.find(".e-rel-del").trigger("click");
    await w.find(".e-rel-type").setValue("照顾");
    await w.find(".e-rel-target").setValue("祠莎");
    await w.find(".e-rel-add").trigger("click");
    await w.find('[data-e-save]').trigger("click");
    await flushPromises();

    expect(mocks.apiMock.reviseEntity).toHaveBeenCalledTimes(1);
    const [id, payload] = mocks.apiMock.reviseEntity.mock.calls[0];
    expect(id).toBe("ec_1");
    expect(payload.summary).toBe("我妈妈（新）");
    expect(payload.attributes).toEqual([{ key: "生日", value: "3 月 6 日", confidence: 0.9 }]);
    expect(payload.expected_revision).toBe(3);
    expect(payload.relations).toEqual({
      add: [{ type: "照顾", target: "祠莎" }],
      remove: [{ type: "女儿", target: "祠莎" }],
    });
    expect(mocks.refreshPending).toHaveBeenCalled();
    w.unmount();
  });

  it("取消丢弃本次修改，不调用接口", async () => {
    const w = mountPanel();
    await openFirst(w);
    await w.find('[data-e-edit]').trigger("click");
    await w.find(".e-summary").setValue("乱改的摘要");
    await w.find('[data-e-cancel]').trigger("click");
    await nextTick();
    expect(mocks.apiMock.reviseEntity).not.toHaveBeenCalled();
    expect(w.find('[data-e-draft]').exists()).toBe(false);
    expect(w.find('[data-e-detail]').text()).toContain("我妈妈");
    w.unmount();
  });

  it("保存失败：草稿与可读错误都保留", async () => {
    mocks.apiMock.reviseEntity.mockRejectedValueOnce(new Error("offline"));
    const w = mountPanel();
    await openFirst(w);
    await w.find('[data-e-edit]').trigger("click");
    await w.find(".e-summary").setValue("我妈妈（新）");
    await w.find('[data-e-save]').trigger("click");
    await flushPromises();
    expect(w.find('[data-e-draft]').exists()).toBe(true);
    expect((w.find(".e-summary").element as HTMLTextAreaElement).value).toBe("我妈妈（新）");
    expect(w.text()).toContain("offline");
    expect(w.text()).toContain("草稿还在");
    w.unmount();
  });

  it("409 版本冲突：提示冲突、保留草稿、展示服务端当前摘要", async () => {
    mocks.apiMock.reviseEntity.mockRejectedValueOnce(conflictError(4));
    mocks.apiMock.listEntities
      .mockResolvedValueOnce({ entities: [CARD] })
      .mockResolvedValueOnce({
        entities: [{ ...CARD, revision: 4, summary: "别处改过的摘要" }],
      });
    const w = mountPanel();
    await openFirst(w);
    await w.find('[data-e-edit]').trigger("click");
    await w.find(".e-summary").setValue("我的修改");
    await w.find('[data-e-save]').trigger("click");
    await flushPromises();
    expect((w.find(".e-summary").element as HTMLTextAreaElement).value).toBe("我的修改");
    expect(w.text()).toContain("被改动过");
    expect(w.text()).toContain("别处改过的摘要");
    w.unmount();
  });

  it("未保存修改时返回先确认：继续编辑 / 放弃修改", async () => {
    const w = mountPanel();
    await openFirst(w);
    await w.find('[data-e-edit]').trigger("click");
    await w.find(".e-summary").setValue("没保存的修改");
    await w.find('[data-e-back]').trigger("click");
    await nextTick();
    expect(w.find('[data-unsaved-guard]').exists()).toBe(true);
    expect(listVisible(w)).toBe(false);

    await w.find('[data-unsaved-keep]').trigger("click");
    await nextTick();
    expect(w.find('[data-unsaved-guard]').exists()).toBe(false);
    expect(w.find('[data-e-draft]').exists()).toBe(true);

    await w.find('[data-e-back]').trigger("click");
    await nextTick();
    await w.find('[data-unsaved-discard]').trigger("click");
    await flushPromises();
    await nextTick();
    expect(listVisible(w)).toBe(true);
    expect(mocks.apiMock.reviseEntity).not.toHaveBeenCalled();
    w.unmount();
  });
});

describe("EntityPanel 外部定位", () => {
  it("openCardId 打开对应卡片", async () => {
    const w = mountPanel({ openCardId: "ec_1" });
    await flushPromises();
    await nextTick();
    expect(detailVisible(w)).toBe(true);
    expect(w.find('[data-e-detail]').text()).toContain("我妈妈");
    w.unmount();
  });

  it("focusId 打开对应卡片", async () => {
    const w = mountPanel({ focusId: "ec_1" });
    await flushPromises();
    await nextTick();
    expect(detailVisible(w)).toBe(true);
    w.unmount();
  });
});
