import { describe, expect, it, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import EntityPanel from "../EntityPanel.vue";
import type { EntityCard } from "../../../services/api";

const mocks = vi.hoisted(() => ({
  apiMock: {
    listEntities: vi.fn<() => Promise<{ entities: EntityCard[] }>>(async () => ({ entities: [] })),
    reviseEntity: vi.fn(async () => ({ ok: true, entity: null })),
    revokeEntity: vi.fn(async () => ({ ok: true, entity_id: "" })),
    addEntityRelation: vi.fn(async () => ({ ok: true, entity: null })),
    removeEntityRelation: vi.fn(async () => ({ ok: true, entity: null })),
  },
}));

vi.mock("../../../services/api", () => ({ api: mocks.apiMock }));

const CARD: EntityCard = {
  id: "ec_1", node_id: "n1", name: "王翠华", aliases: ["我妈"], kind: "家人",
  summary: "我妈妈，退休教师", state: "active",
  attributes: [{ key: "职业", value: "退休教师", confidence: 0.9 }],
  relations: [{ type: "属于", target: "王翠华的弟弟" }],
  created_at: "", updated_at: "",
};

function mountPanel(openId = "") {
  const pinia = createPinia();
  setActivePinia(pinia);
  return mount(EntityPanel, { props: { openCardId: openId }, global: { plugins: [pinia] } });
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.apiMock.listEntities.mockResolvedValue({ entities: [CARD] });
});

describe("EntityPanel", () => {
  it("渲染实体列表", async () => {
    const w = mountPanel();
    await flushPromises();
    expect(w.text()).toContain("王翠华");
    expect(w.text()).toContain("家人");
    w.unmount();
  });

  it("点击条目打开详情并可改摘要", async () => {
    const w = mountPanel();
    await flushPromises();
    await w.find(".e-item").trigger("click");
    await flushPromises();
    expect(w.text()).toContain("退休教师");
    await w.find(".e-summary").setValue("我妈妈，退休教师，住成都");
    await w.find(".e-save").trigger("click");
    await flushPromises();
    expect(mocks.apiMock.reviseEntity).toHaveBeenCalledWith(
      "ec_1",
      expect.objectContaining({ summary: "我妈妈，退休教师，住成都" }),
    );
    w.unmount();
  });

  it("属性可增删（置信度可编辑）", async () => {
    const w = mountPanel();
    await flushPromises();
    await w.find(".e-item").trigger("click");
    await flushPromises();
    await w.find(".e-attr-key").setValue("居住地");
    await w.find(".e-attr-value").setValue("成都");
    await w.find(".e-attr-conf").setValue("0.8");
    await w.find(".e-attr-add").trigger("click");
    await flushPromises();
    expect(mocks.apiMock.reviseEntity).toHaveBeenCalledWith(
      "ec_1",
      expect.objectContaining({
        attributes: expect.arrayContaining([
          expect.objectContaining({ key: "居住地", value: "成都", confidence: 0.8 }),
        ]),
      }),
    );
    w.unmount();
  });

  it("关系可增删", async () => {
    const w = mountPanel();
    await flushPromises();
    await w.find(".e-item").trigger("click");
    await flushPromises();
    await w.find(".e-rel-type").setValue("喜欢去");
    await w.find(".e-rel-target").setValue("五里关火锅");
    await w.find(".e-rel-add").trigger("click");
    await flushPromises();
    expect(mocks.apiMock.addEntityRelation).toHaveBeenCalledWith("ec_1", { type: "喜欢去", target: "五里关火锅" });
    await w.find(".e-rel-del").trigger("click");
    await flushPromises();
    expect(mocks.apiMock.removeEntityRelation).toHaveBeenCalledWith("ec_1", { type: "属于", target: "王翠华的弟弟" });
    w.unmount();
  });

  it("归档调用 revokeEntity", async () => {
    const w = mountPanel();
    await flushPromises();
    await w.find(".e-item").trigger("click");
    await flushPromises();
    const spy = vi.spyOn(window, "confirm").mockReturnValue(true);
    await w.find(".e-revoke").trigger("click");
    await flushPromises();
    expect(mocks.apiMock.revokeEntity).toHaveBeenCalledWith("ec_1");
    spy.mockRestore();
    w.unmount();
  });

  it("openCardId 变化时自动打开对应卡", async () => {
    const w = mountPanel("");
    await flushPromises();
    await w.setProps({ openCardId: "ec_1" });
    await flushPromises();
    expect(w.text()).toContain("退休教师");
    w.unmount();
  });
});
