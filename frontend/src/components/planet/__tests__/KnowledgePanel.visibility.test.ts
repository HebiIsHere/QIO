import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import KnowledgePanel from "../KnowledgePanel.vue";
import { api, type KnowledgeItem } from "../../../services/api";

const item: KnowledgeItem = {
  id: "kn_1",
  category: "user_profile",
  state: "active",
  content: "用户称呼：祠莎",
  confidence: 0.9,
  topic_id: null,
  topic_name: null,
  source: "引导",
  scope: "全局（你）",
  ended: false,
  ended_at: null,
  created_at: "2026-09-23T00:00:00+00:00",
  updated_at: "2026-09-23T00:00:00+00:00",
};

function mountPanel(items: KnowledgeItem[] = [{ ...item }]) {
  vi.spyOn(api, "listKnowledge").mockResolvedValue({ knowledge: items });
  vi.spyOn(api, "listTopics").mockResolvedValue({ topics: [] });
  vi.spyOn(api, "endKnowledge").mockResolvedValue({
    ok: true,
    knowledge: { ...item, ended: true, ended_at: "2026-09-23T01:00:00+00:00" },
  });
  vi.spyOn(api, "resumeKnowledge").mockResolvedValue({ ok: true, knowledge: { ...item } });
  return mount(KnowledgePanel);
}

describe("知识看得懂：来源、范围与结束", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("每条知识显示来源与适用范围", async () => {
    const w = mountPanel();
    await flushPromises();
    expect(w.find(".k-source").text()).toBe("引导");
    expect(w.find(".k-scope").text()).toBe("全局（你）");
  });

  it("可以按来源筛选", async () => {
    const w = mountPanel();
    await flushPromises();
    const vm = w.vm as unknown as { source: string };
    vm.source = "对话";
    await flushPromises();
    expect(w.findAll(".k-item")).toHaveLength(0);
    vm.source = "引导";
    await flushPromises();
    expect(w.findAll(".k-item")).toHaveLength(1);
  });

  it("可以把一条标成已结束，再恢复", async () => {
    const w = mountPanel();
    await flushPromises();
    await w.find(".k-toggle-ended").trigger("click");
    await flushPromises();
    expect(api.endKnowledge).toHaveBeenCalledWith("kn_1");

    const w2 = mountPanel([{ ...item, ended: true }]);
    await flushPromises();
    expect(w2.text()).toContain("已结束");
    await w2.find(".k-toggle-ended").trigger("click");
    await flushPromises();
    expect(api.resumeKnowledge).toHaveBeenCalledWith("kn_1");
  });
});
