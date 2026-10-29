/**
 * 管理区纯逻辑单测（_MP-COMMON §3.5 / A 交付 §2~§6）。
 *
 * 这些规则最容易悄悄退化：已结束话题混进主列表、搜索只搜当前标签页、
 * 分页拼接把第一页重复、落点映射把「待处理」和「记忆」搞混。
 * 纯函数直接断言，不挂组件树。
 */
import { describe, expect, it } from "vitest";
import {
  buildSearchGroups,
  formatWhen,
  groupTopics,
  hasMoreMessages,
  isCurrentTopic,
  matchesTopic,
  mergeMessagePage,
  resolveInitialRequest,
  searchGroupsEmpty,
  SEARCH_GROUP_LIMIT,
} from "../../../planet/manageViews";
import type { EntityCard, KnowledgeItem, TopicFingerprint } from "../../../services/api";

const topic = (over: Partial<TopicFingerprint> = {}): TopicFingerprint => ({
  topic_id: "t1",
  title: "话题 A",
  keywords: [],
  fragment_count: 1,
  last_activity: null,
  summary_preview: null,
  ...over,
});

describe("话题分组与搜索", () => {
  const list = [
    topic({ topic_id: "a", title: "开发 QIO", keywords: ["QIO"] }),
    topic({ topic_id: "b", title: "选课", ended: true, keywords: ["学期"] }),
    topic({ topic_id: "c", title: "旅行计划", summary_preview: "去看海" }),
  ];

  it("主列表只放进行中的话题，已结束单独成组（结束不是删掉）", () => {
    const groups = groupTopics(list);
    expect(groups.active.map((t) => t.topic_id)).toEqual(["a", "c"]);
    expect(groups.ended.map((t) => t.topic_id)).toEqual(["b"]);
  });

  it("搜索同时作用于两组：已结束的话题依然能被搜到", () => {
    const groups = groupTopics(list, "学期");
    expect(groups.ended.map((t) => t.topic_id)).toEqual(["b"]);
    expect(groups.active).toEqual([]);
    const all = groupTopics(list, "");
    expect(all.active.length + all.ended.length).toBe(3);
  });

  it("关键词 / 摘要预览也参与匹配，不只标题", () => {
    expect(matchesTopic(list[0], "qio")).toBe(true);
    expect(matchesTopic(list[2], "看海")).toBe(true);
    expect(matchesTopic(list[2], "不存在")).toBe(false);
  });
});

describe("全局搜索分组（话题 / 知识 / 实体）", () => {
  const source = {
    topics: [topic({ topic_id: "t1", title: "薄荷种植" }), topic({ topic_id: "t2", title: "别的" })],
    knowledge: [
      { id: "k1", content: "薄荷需要充足阳光", category: "general_fact", state: "active", confidence: null, topic_id: null, topic_name: "薄荷种植", created_at: "", updated_at: "" } as KnowledgeItem,
      { id: "k2", content: "无关内容", category: "general_fact", state: "active", confidence: null, topic_id: null, topic_name: null, created_at: "", updated_at: "" } as KnowledgeItem,
    ],
    entities: [
      { id: "e1", node_id: "n1", name: "薄荷", aliases: ["mentha"], kind: null, summary: "一种香草", attributes: [], relations: [], state: "active", created_at: "", updated_at: "" } as EntityCard,
      { id: "e2", node_id: "n2", name: "罗勒", aliases: [], kind: null, summary: "另一种香草", attributes: [], relations: [], state: "active", created_at: "", updated_at: "" } as EntityCard,
    ],
  };

  it("一次搜索覆盖三类，并按三类分组（与当前标签页无关）", () => {
    const groups = buildSearchGroups(source, "薄荷");
    expect(groups.topics.map((h) => h.id)).toEqual(["t1"]);
    expect(groups.knowledge.map((h) => h.id)).toEqual(["k1"]);
    expect(groups.entities.map((h) => h.id)).toEqual(["e1"]);
    expect(groups.total).toBe(3);
    expect(searchGroupsEmpty(groups)).toBe(false);
  });

  it("空关键词不返回结果（不是「全部」）", () => {
    const groups = buildSearchGroups(source, "   ");
    expect(groups.total).toBe(0);
    expect(searchGroupsEmpty(groups)).toBe(true);
  });

  it("命中列表完整、真实总数单独给出（第九条与最后一条仍然存在并可点）", () => {
    const many = {
      topics: Array.from({ length: 30 }, (_, i) => topic({ topic_id: `t${i}`, title: `薄荷 ${i}` })),
      knowledge: [],
      entities: [],
    };
    const groups = buildSearchGroups(many, "薄荷");
    // 展示层才分批；这里不再把「每组 8 条」当成全部结果（否则总数是假的、第九条不可达）。
    expect(groups.topics.length).toBe(30);
    expect(groups.counts.topics).toBe(30);
    expect(groups.total).toBe(30);
    expect(groups.topics[SEARCH_GROUP_LIMIT].kind).toBe("topic");
    expect(groups.topics[SEARCH_GROUP_LIMIT].id).toBe(`t${SEARCH_GROUP_LIMIT}`);
    expect(groups.topics[29].id).toBe("t29");
  });

  it("别名也参与实体匹配", () => {
    const groups = buildSearchGroups(source, "mentha");
    expect(groups.entities.map((h) => h.id)).toEqual(["e1"]);
  });

  it("结果行第二行不再重复类型名，改成有用信息", () => {
    const topics = buildSearchGroups(
      { topics: [topic({ topic_id: "t1", title: "薄荷种植", fragment_count: 7, last_activity: "2026-10-27T03:00:00Z" })], knowledge: [], entities: [] },
      "薄荷",
    );
    expect(topics.topics[0].note).toContain("7 片段");
    expect(topics.topics[0].note).toContain("最近活动");
    expect(topics.topics[0].note).not.toBe("话题");

    const knowledge = buildSearchGroups(
      {
        topics: [],
        knowledge: [
          { id: "k1", category: "user_profile", state: "verified", content: "薄荷偏好", confidence: null, topic_id: null, topic_name: "薄荷种植", created_at: "", updated_at: "" } as KnowledgeItem,
        ],
        entities: [],
      },
      "薄荷",
    );
    expect(knowledge.knowledge[0].note).toBe("用户画像 · 已验证 · 薄荷种植");

    const entities = buildSearchGroups(
      {
        topics: [],
        knowledge: [],
        entities: [
          { id: "e1", node_id: "n1", name: "薄荷", aliases: [], kind: null, summary: "", attributes: [{ key: "a", value: "b", confidence: 1 }, { key: "c", value: "d", confidence: 1 }], relations: [{ type: "属于", target: "香草" }], state: "active", created_at: "", updated_at: "" } as EntityCard,
        ],
      },
      "薄荷",
    );
    expect(entities.entities[0].note).toBe("2 属性 · 1 关系");
  });
});

describe("讨论记录分页拼接", () => {
  const msg = (id: string) => ({ id, role: "user", content: id, content_type: "text", created_at: "" });
  const page = (ids: string[], total: number) => ({ messages: ids.map(msg), total });
  const ids = (state: { items: { id: string }[] }) => state.items.map((m) => m.id);

  it("offset=0 是换页（替换），否则是继续读取（追加）", () => {
    const first = mergeMessagePage({ items: [], total: 0 }, page(["a", "b"], 4), 0);
    expect(ids(first)).toEqual(["a", "b"]);
    const second = mergeMessagePage(first, page(["c", "d"], 4), 2);
    expect(ids(second)).toEqual(["a", "b", "c", "d"]);
    const replaced = mergeMessagePage(second, page(["e"], 1), 0);
    expect(ids(replaced)).toEqual(["e"]);
  });

  it("读满 total 后不再无限读", () => {
    expect(hasMoreMessages({ items: [1, 2], total: 2 })).toBe(false);
    expect(hasMoreMessages({ items: [1], total: 2 })).toBe(true);
  });
});

describe("对话当前话题 vs 正在浏览的话题", () => {
  it("只有两者相同才算「已在该话题」，否则需要「继续讨论」", () => {
    expect(isCurrentTopic("t1", "t1")).toBe(true);
    expect(isCurrentTopic("t2", "t1")).toBe(false);
    expect(isCurrentTopic("t1", null)).toBe(false);
    expect(isCurrentTopic(null, null)).toBe(false);
  });
});

describe("外部落点映射", () => {
  it("topic → 管理区记忆标签；knowledge/entity → 对应标签", () => {
    expect(resolveInitialRequest("topic")).toEqual({ view: "tabs", tab: "memory" });
    expect(resolveInitialRequest("knowledge")).toEqual({ view: "tabs", tab: "knowledge" });
    expect(resolveInitialRequest("entity")).toEqual({ view: "tabs", tab: "entity" });
  });

  it("外部的 memory 沿用「待处理」语义（与管理区自己的「记忆」标签同名不同义）", () => {
    expect(resolveInitialRequest("memory")).toEqual({ view: "pending", tab: "memory" });
    expect(resolveInitialRequest(null)).toEqual({ view: "tabs", tab: "memory" });
  });
});

describe("时间文案", () => {
  const now = new Date("2026-10-27T10:00:00").getTime();
  it("今天 / 昨天 / N 天前 / 日期", () => {
    expect(formatWhen(new Date("2026-10-27T09:05:00").toISOString(), now)).toContain("今天");
    expect(formatWhen(new Date("2026-10-26T09:05:00").toISOString(), now)).toContain("昨天");
    expect(formatWhen(new Date("2026-10-20T09:05:00").toISOString(), now)).toBe("7 天前");
    expect(formatWhen(new Date("2026-01-02T09:05:00").toISOString(), now)).toBe("2026-01-02");
    expect(formatWhen(null, now)).toBe("—");
    expect(formatWhen("不是时间", now)).toBe("—");
  });
});
