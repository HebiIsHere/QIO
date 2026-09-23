import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import PlanetView from "../PlanetView.vue";
import { api, type TopicFingerprint } from "../../services/api";

vi.mock("../../services/events", () => ({ useEventStore: () => ({ connect: vi.fn() }) }));

const activeTopic: TopicFingerprint = {
  topic_id: "t_active",
  title: "开发 QIO",
  keywords: ["QIO"],
  fragment_count: 3,
  last_activity: null,
  summary_preview: null,
  ended: false,
};
const endedTopic: TopicFingerprint = {
  topic_id: "t_ended",
  title: "选课",
  keywords: ["选课"],
  fragment_count: 1,
  last_activity: null,
  summary_preview: null,
  ended: true,
};

function mountView() {
  vi.spyOn(api, "listTopics").mockResolvedValue({ topics: [{ ...activeTopic }, { ...endedTopic }] });
  vi.spyOn(api, "planetOverview").mockResolvedValue({
    topics: [],
    ended_topics: [],
    total: 0,
    visible_capacity: 24,
  } as never);
  vi.spyOn(api, "planetBrowse").mockResolvedValue({ items: [], next_cursor: null, seed: 1 } as never);
  vi.spyOn(api, "endTopic").mockResolvedValue({ ok: true, topic_id: "t_active" });
  vi.spyOn(api, "resumeTopic").mockResolvedValue({ ok: true, topic_id: "t_ended" });
  const pinia = createPinia();
  setActivePinia(pinia);
  return mount(PlanetView, {
    props: { seq: 1, open: true },
    global: {
      plugins: [pinia],
      stubs: {
        PlanetOrb: true,
        PlanetBoot: true,
        TopicDetail: true,
        KnowledgePanel: true,
        EntityPanel: true,
      },
    },
  });
}

describe("星球的已结束分组", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("主视图只列进行中的话题，已结束的单独成组", async () => {
    const w = mountView();
    await flushPromises();
    const main = w.find(".topic-list:not(.ended-list)");
    expect(main.text()).toContain("开发 QIO");
    expect(main.text()).not.toContain("选课");
    expect(w.find(".ended-list").text()).toContain("已结束");
    expect(w.find(".ended-list").text()).toContain("选课");
  });

  it("可以把进行中的话题标成结束", async () => {
    const w = mountView();
    await flushPromises();
    await w.find(".topic-toggle-ended").trigger("click");
    await flushPromises();
    expect(api.endTopic).toHaveBeenCalledWith("t_active");
  });

  it("可以恢复已结束的话题", async () => {
    const w = mountView();
    await flushPromises();
    await w.find(".topic-resume").trigger("click");
    await flushPromises();
    expect(api.resumeTopic).toHaveBeenCalledWith("t_ended");
  });
});
