import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { usePlanetPendingStore } from "../../../stores/planetPending";

const mocks = vi.hoisted(() => ({
  listMemoryPending: vi.fn(),
  listAllCandidates: vi.fn(),
}));

vi.mock("../../../services/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../services/api")>();
  return {
    ...actual,
    api: { ...actual.api, listMemoryPending: mocks.listMemoryPending },
  };
});

vi.mock("../../../services/entityCandidatesApi", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../services/entityCandidatesApi")>();
  return { ...actual, listAllCandidates: mocks.listAllCandidates };
});

function memoryListing(total: number) {
  return {
    items: new Array(total).fill(null).map((_, i) => ({ id: "m" + i })),
    total,
    truncated: false,
    maintenance_available: true,
    warnings: [],
  };
}
function candidateListing(total: number) {
  return { candidates: [], total, shown: 0, truncated: false };
}

beforeEach(() => {
  vi.clearAllMocks();
  const pinia = createPinia();
  setActivePinia(pinia);
  mocks.listMemoryPending.mockResolvedValue(memoryListing(2));
  mocks.listAllCandidates.mockResolvedValue(candidateListing(3));
});

describe("planetPending store：真实未处理数 = 记忆待确认 + 实体更新候选", () => {
  it("refresh 合并两类真实 total", async () => {
    const store = usePlanetPendingStore();
    await store.refresh();
    expect(store.count).toBe(5);
    expect(store.memoryCount).toBe(2);
    expect(store.candidateCount).toBe(3);
    expect(store.status).toBe("ready");
  });

  it("单侧失败保留上一次真实数量，并把状态标成 partial", async () => {
    const store = usePlanetPendingStore();
    await store.refresh();
    mocks.listAllCandidates.mockRejectedValueOnce(new Error("offline"));
    await store.refresh();
    expect(store.memoryCount).toBe(2);
    expect(store.candidateCount).toBe(3);
    expect(store.count).toBe(5);
    expect(store.status).toBe("partial");
    expect(store.error).toContain("offline");
  });

  it("两侧都失败不会把数量清零成假事实", async () => {
    const store = usePlanetPendingStore();
    await store.refresh();
    mocks.listMemoryPending.mockRejectedValueOnce(new Error("boom"));
    mocks.listAllCandidates.mockRejectedValueOnce(new Error("offline"));
    await store.refresh();
    expect(store.count).toBe(5);
    expect(store.status).toBe("error");
  });

  it("并发 refresh 单飞：只发一次请求", async () => {
    const store = usePlanetPendingStore();
    await Promise.all([store.refresh(), store.refresh(), store.refresh()]);
    expect(mocks.listMemoryPending).toHaveBeenCalledTimes(1);
    expect(mocks.listAllCandidates).toHaveBeenCalledTimes(1);
  });
});
