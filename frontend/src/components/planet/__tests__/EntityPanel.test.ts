import { describe, expect, it, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import EntityPanel from "../EntityPanel.vue";
import type { EntityCard } from "../../../services/api";

const mocks = vi.hoisted(() => ({
  apiMock: {
    listEntities: vi.fn<() => Promise<{ entities: EntityCard[] }>>(async () => ({ entities: [] })),
    revokeEntity: vi.fn(async (_id: string) => ({ ok: true })),
  },
}));

vi.mock("../../../services/api", () => ({ api: mocks.apiMock }));

const CARD: EntityCard = {
  id: "ec_1", node_id: "n1", name: "王翠华", aliases: ["妈"], kind: "家人",
  summary: "我妈妈", attributes: [], relations: [], state: "active",
  created_at: "", updated_at: "",
};

function mountPanel() {
  return mount(EntityPanel);
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.apiMock.listEntities.mockResolvedValue({ entities: [CARD] });
});

describe("EntityPanel 空态与失败区分（任务05 E）", () => {
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
    await w.find("input").setValue("不存在");
    await flushPromises();
    expect(w.find(".e-empty").text()).toContain("没有匹配");
    await w.find(".e-clear-search").trigger("click");
    await flushPromises();
    expect(w.find(".e-item").exists()).toBe(true);
    w.unmount();
  });
});

/**
 * 第四阶段：实体卡默认阅读、按需编辑；归档走 QIO 自己的确认层。
 */
describe("EntityPanel 阅读态与确认层（第四阶段）", () => {
  it("打开卡片默认是阅读态：没有输入框，只显示摘要与字段", async () => {
    const w = mountPanel();
    await flushPromises();
    await w.find(".e-item").trigger("click");
    await flushPromises();
    expect(w.find(".e-detail").exists()).toBe(true);
    expect(w.find(".e-read-summary").text()).toBe("我妈妈");
    // 阅读态不铺满编辑控件
    expect(w.find(".e-detail textarea").exists()).toBe(false);
    expect(w.find(".e-detail .qio-inline-edit").exists()).toBe(false);
    w.unmount();
  });

  it("点「修正」就地进入编辑态，取消后回到阅读态", async () => {
    const w = mountPanel();
    await flushPromises();
    await w.find(".e-item").trigger("click");
    await flushPromises();
    await w.find(".e-edit-open").trigger("click");
    expect(w.find(".e-summary").exists()).toBe(true);
    await w.find(".e-cancel").trigger("click");
    expect(w.find(".e-read-summary").exists()).toBe(true);
    w.unmount();
  });

  it("归档先用确认层说明影响，确认后才调用接口", async () => {
    mocks.apiMock.revokeEntity.mockClear();
    const w = mountPanel();
    await flushPromises();
    await w.find(".e-item").trigger("click");
    await flushPromises();
    await w.find(".e-revoke").trigger("click");
    const confirm = w.find(".e-detail .qio-confirm");
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
