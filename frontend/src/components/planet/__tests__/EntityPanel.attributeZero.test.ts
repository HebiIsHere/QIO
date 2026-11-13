import { describe, expect, it, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";
import EntityPanel from "../EntityPanel.vue";
import type { EntityCard } from "../../../services/api";

/**
 * PM-QIO032 #49：显式输入置信度 0，从添加表单 → 草稿 → 请求 payload → 刷新回显
 * 全程都必须是 0；只有**没填**（空）才用默认值 0.8。
 */

const mocks = vi.hoisted(() => ({
  refreshPending: vi.fn(async () => {}),
  apiMock: {
    listEntities: vi.fn<() => Promise<{ entities: EntityCard[] }>>(async () => ({ entities: [] })),
    revokeEntity: vi.fn(async (_id: string) => ({ ok: true })),
    reviseEntity: vi.fn(
      async (_id: string, _payload: Record<string, unknown>) => ({ ok: true, entity: null }),
    ),
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
  name: "薄荷",
  aliases: [],
  kind: "animal",
  summary: "一只猫",
  attributes: [{ key: "体重(kg)", value: "4.2", confidence: 0.9 }],
  relations: [],
  state: "active",
  revision: 3,
  identity: "用户养的猫",
  creation_reason: "持续对话出现",
  evidence_ids: ["msg_1"],
  created_at: "2026-10-01T10:00:00+08:00",
  updated_at: "2026-10-02T11:00:00+08:00",
};

function mountPanel() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return mount(EntityPanel, { global: { plugins: [pinia] } });
}

async function openAndEdit(w: ReturnType<typeof mountPanel>) {
  await flushPromises();
  await w.find('[data-e-id="ec_1"]').trigger("click");
  await nextTick();
  await w.find('[data-e-edit]').trigger("click");
  await nextTick();
}

function attrInputs(w: ReturnType<typeof mountPanel>) {
  return w.findAll(".e-attr-edit .e-attr-conf");
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.apiMock.listEntities.mockResolvedValue({ entities: [CARD] });
  mocks.apiMock.reviseEntity.mockResolvedValue({ ok: true, entity: null });
});

describe("#49 添加属性：显式置信度 0 不被默认值覆盖", () => {
  it("0 进入草稿、进入 payload，并在刷新回显里仍是 0", async () => {
    // 第一次读：CARD；保存后刷新：服务端真实保存了 0。
    mocks.apiMock.listEntities
      .mockResolvedValueOnce({ entities: [CARD] })
      .mockResolvedValueOnce({
        entities: [
          {
            ...CARD,
            revision: 4,
            attributes: [
              ...CARD.attributes,
              { key: "接受抚摸", value: "0", confidence: 0 },
            ],
          },
        ],
      });
    const w = mountPanel();
    await openAndEdit(w);

    await w.find(".e-attr-key").setValue("接受抚摸");
    await w.find(".e-attr-value").setValue("0");
    await w.find('input[aria-label="新属性的置信度"]').setValue("0");
    await w.find(".e-attr-add").trigger("click");
    await nextTick();

    // 草稿里就是 0（不是 0.8）
    const row = w.findAll(".e-attr-edit").find((r) => r.text().includes("接受抚摸"))!;
    expect((row.find(".e-attr-conf").element as HTMLInputElement).value).toBe("0");

    await w.find("[data-e-save]").trigger("click");
    await flushPromises();

    expect(mocks.apiMock.reviseEntity).toHaveBeenCalledTimes(1);
    const payload = mocks.apiMock.reviseEntity.mock.calls[0][1] as {
      attributes: Array<{ key: string; value: string; confidence: number }>;
    };
    const added = payload.attributes.find((a) => a.key === "接受抚摸")!;
    expect(added.value, "属性值 '0' 不能被当成空值").toBe("0");
    expect(added.confidence).toBe(0);

    // 刷新回显：阅读态是服务端真实值 0.00
    const readRow = w.findAll(".attr-row").find((r) => r.text().includes("接受抚摸"))!;
    expect(readRow.text()).toContain("接受抚摸 = 0");
    expect(readRow.find(".conf").text()).toBe("0.00");
    w.unmount();
  });

  it("置信度留空（没填）才用默认 0.8", async () => {
    const w = mountPanel();
    await openAndEdit(w);

    await w.find(".e-attr-key").setValue("住址");
    await w.find(".e-attr-value").setValue("成都");
    await w.find('input[aria-label="新属性的置信度"]').setValue("");
    await w.find(".e-attr-add").trigger("click");
    await nextTick();
    await w.find("[data-e-save]").trigger("click");
    await flushPromises();

    const payload = mocks.apiMock.reviseEntity.mock.calls[0][1] as {
      attributes: Array<{ key: string; value: string; confidence: number }>;
    };
    expect(payload.attributes.find((a) => a.key === "住址")!.confidence).toBe(0.8);
    w.unmount();
  });

  it("已有属性就地改成 0，payload 仍是 0", async () => {
    const w = mountPanel();
    await openAndEdit(w);

    expect(attrInputs(w)).toHaveLength(1);
    await attrInputs(w)[0].setValue("0");
    await w.find("[data-e-save]").trigger("click");
    await flushPromises();

    const payload = mocks.apiMock.reviseEntity.mock.calls[0][1] as {
      attributes: Array<{ key: string; value: string; confidence: number }>;
    };
    expect(payload.attributes[0]).toEqual({ key: "体重(kg)", value: "4.2", confidence: 0 });
    w.unmount();
  });

  it("非法输入回退默认值（不静默变成 NaN）", async () => {
    const w = mountPanel();
    await openAndEdit(w);

    await w.find(".e-attr-key").setValue("体重(kg)");
    await w.find(".e-attr-value").setValue("5.0");
    await w.find('input[aria-label="新属性的置信度"]').setValue("abc");
    await w.find(".e-attr-add").trigger("click");
    await nextTick();
    await w.find("[data-e-save]").trigger("click");
    await flushPromises();

    const payload = mocks.apiMock.reviseEntity.mock.calls[0][1] as {
      attributes: Array<{ key: string; value: string; confidence: number }>;
    };
    expect(payload.attributes.find((a) => a.key === "体重(kg)")!.confidence).toBe(0.8);
    w.unmount();
  });
});
