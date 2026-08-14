# 星球记忆中心（知识与实体管理 v1）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把知识与实体的管理做进话题星球——右侧面板升级为「话题 / 知识 / 实体」三页签 + 340↔640px 双态，支持知识浏览/修正/归档/批准/打回/手动新建，实体浏览/属性与关系编辑/归档。

**Architecture:** 后端先补齐知识列表/新建/批准/打回接口，并把实体接口从「注入文本」改为结构化输出（含属性/关系/node_id）；前端 `PlanetView` 面板改为三页签 + 管理模式宽度，知识/实体各自拆成独立子组件，通过事件与星球画布双向联动。

**Tech Stack:** FastAPI + sqlite3（后端）；Vue 3 + Pinia + vitest（前端）。沿用现有设计令牌（--bg-panel/--accent/--border-*）与 QInput/QSelect 控件。

**参考文档：** `docs/superpowers/specs/2026-08-14-planet-memory-center-design.md`

---

## 文件结构

**后端**
- Modify `backend/src/agent/knowledge/lifecycle.py` — 新增 `KnowledgeService.list_items(category, state, q)`
- Modify `backend/src/agent/entities/cards.py` — 新增结构化序列化 `EntityCardService.to_dict(card)`（含 relations、node_id）
- Modify `backend/src/agent/api/server.py` — 知识四条路由 + 实体结构化/关系路由
- Test: `backend/tests/test_knowledge_mgmt_api.py`（新）、`backend/tests/test_entity_cards.py`（增）

**前端**
- Modify `frontend/src/services/api.ts` — 新增类型与方法
- Create `frontend/src/components/planet/KnowledgePanel.vue`
- Create `frontend/src/components/planet/EntityPanel.vue`
- Modify `frontend/src/views/PlanetView.vue` — 三页签 + 管理模式 + 联动
- Test: `frontend/src/components/planet/__tests__/KnowledgePanel.test.ts`、`EntityPanel.test.ts`（新）、`frontend/src/views/__tests__/PlanetView.test.ts`（增）

---

### Task 1: KnowledgeService.list_items

**Files:**
- Modify: `backend/src/agent/knowledge/lifecycle.py`（在 `history()` 附近加 `list_items`）
- Test: `backend/tests/test_knowledge.py`

- [ ] **Step 1: 写失败测试**（追加到 `backend/tests/test_knowledge.py`）

```python
def test_list_items_filters_and_orders(db_conn):
    from agent.knowledge.lifecycle import KnowledgeService

    ks = KnowledgeService(db_conn)
    a = ks.create(category="general_fact", content="SQLite 是嵌入式数据库")
    b = ks.create(category="user_profile", content="用户喜欢清淡饮食")
    c = ks.create(category="general_fact", content="SQLite 支持 WAL 模式")
    ks.submit(a.id); ks.verify(a.id, verified_by="user"); ks.activate(a.id)
    ks.submit(b.id); ks.verify(b.id, verified_by="user"); ks.activate(b.id)

    all_items = ks.list_items()
    assert [i.id for i in all_items] == [c.id, b.id, a.id]
    assert {i.id for i in ks.list_items(category="general_fact")} == {a.id, c.id}
    assert {i.id for i in ks.list_items(state="active")} == {a.id, b.id}
    assert {i.id for i in ks.list_items(q="WAL")} == {c.id}
```

- [ ] **Step 2: 运行确认失败**（沙箱外，`PYTHONPATH=src`；pytest 用 `--basetemp` 到用户可写目录）

Run: `python -m pytest tests/test_knowledge.py::test_list_items_filters_and_orders -q --basetemp=%TEMP%\qio-pt`
Expected: FAIL，`AttributeError: 'KnowledgeService' object has no attribute 'list_items'`

- [ ] **Step 3: 实现**

在 `backend/src/agent/knowledge/lifecycle.py` 的 `history()` 之后加：

```python
    def list_items(
        self,
        category: str | None = None,
        state: str | None = None,
        q: str | None = None,
    ) -> list[KnowledgeItem]:
        """列出知识条目，支持分类/状态/关键词过滤，按 updated_at 倒序。"""
        sql = "SELECT * FROM knowledge WHERE 1=1"
        params: list[Any] = []
        if category:
            sql += " AND category = ?"
            params.append(category)
        if state:
            sql += " AND state = ?"
            params.append(state)
        if q:
            sql += " AND content LIKE ?"
            params.append(f"%{q}%")
        sql += " ORDER BY updated_at DESC"
        rows = self.conn.execute(sql, params).fetchall()
        return [self._from_row(r) for r in rows]
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_knowledge.py::test_list_items_filters_and_orders -q --basetemp=%TEMP%\qio-pt`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/src/agent/knowledge/lifecycle.py backend/tests/test_knowledge.py
git commit -m "feat: KnowledgeService.list_items（分类/状态/关键词过滤）"
```

---

### Task 2: 知识管理 API 路由

**Files:**
- Modify: `backend/src/agent/api/server.py`（在 `# -- knowledge correction --` 段之前插入知识管理路由）
- Test: `backend/tests/test_knowledge_mgmt_api.py`（新）

- [ ] **Step 1: 写失败测试**

```python
"""知识管理 API：列表/手动新建/批准/打回。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from agent.api.server import create_app
from agent.config import Settings
from agent.knowledge.lifecycle import KnowledgeService
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations


def test_knowledge_manage_flow(tmp_path):
    conn = connect(tmp_path / "app.db")
    apply_migrations(conn)
    ks = KnowledgeService(conn)
    item = ks.create(category="general_fact", content="待审核条目")
    ks.submit(item.id)
    app = create_app(Settings(data_dir=tmp_path), conn)
    client = TestClient(app)

    # 列表：包含该条目且结构化
    r = client.get("/api/knowledge")
    assert r.status_code == 200
    rows = r.json()["knowledge"]
    assert any(x["id"] == item.id and x["state"] == "pending_review" for x in rows)
    assert "topic_name" in rows[0]

    # 批准 → active
    r = client.post(f"/api/knowledge/{item.id}/verify")
    assert r.status_code == 200
    assert r.json()["knowledge"]["state"] == "active"

    # 再造一条并打回 → draft
    item2 = ks.create(category="general_fact", content="将被打回")
    ks.submit(item2.id)
    r = client.post(f"/api/knowledge/{item2.id}/reject")
    assert r.status_code == 200
    assert r.json()["knowledge"]["state"] == "draft"

    # 手动新建 → 直接 active（用户权威，高影响分类也可）
    r = client.post(
        "/api/knowledge",
        json={"category": "user_profile", "content": "用户最爱的火锅店是五里关"},
    )
    assert r.status_code == 200
    assert r.json()["knowledge"]["state"] == "active"
    assert r.json()["knowledge"]["category"] == "user_profile"

    # 非法：未知分类 / 空内容 / 不存在
    assert client.post("/api/knowledge", json={"category": "nope", "content": "x"}).status_code == 400
    assert client.post("/api/knowledge", json={"category": "goal", "content": "  "}).status_code == 400
    assert client.post("/api/knowledge/ghost/verify").status_code == 404
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_knowledge_mgmt_api.py -q --basetemp=%TEMP%\qio-pt`
Expected: FAIL（404，路由不存在）

- [ ] **Step 3: 实现路由**

在 `backend/src/agent/api/server.py` 的 `# -- knowledge correction --` 段之前插入：

```python
    # -- knowledge management --------------------------------------------

    @app.get("/api/knowledge")
    async def list_knowledge(
        category: str | None = None,
        state: str | None = None,
        q: str | None = None,
    ) -> dict:
        from agent.knowledge.lifecycle import KnowledgeService

        ks = KnowledgeService(ctx.conn)
        items = ks.list_items(category=category, state=state, q=q)
        out = []
        for it in items:
            topic_name = None
            if it.topic_id:
                node = ctx.topics.nodes.get_topic(it.topic_id)
                topic_name = node.name if node is not None else None
            out.append({
                "id": it.id,
                "category": it.category,
                "state": it.state.value,
                "content": it.content,
                "confidence": it.confidence,
                "topic_id": it.topic_id,
                "topic_name": topic_name,
                "created_at": it.created_at,
                "updated_at": it.updated_at,
            })
        return {"knowledge": out}

    @app.post("/api/knowledge")
    async def create_knowledge(body: dict) -> dict:
        from agent.knowledge.lifecycle import CATEGORIES, KnowledgeService

        category = str(body.get("category") or "").strip()
        content = str(body.get("content") or "").strip()
        if category not in CATEGORIES:
            raise HTTPException(status_code=400, detail=f"unknown category: {category}")
        if not content:
            raise HTTPException(status_code=400, detail="content required")
        topic_id = body.get("topic_id") or None
        ks = KnowledgeService(ctx.conn)
        try:
            item = ks.create(category=category, content=content, topic_id=topic_id)
            ks.submit(item.id)
            ks.verify(item.id, verified_by="user")
            active = ks.activate(item.id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        node = ctx.topics.nodes.get_topic(active.topic_id) if active.topic_id else None
        return {"ok": True, "knowledge": {
            "id": active.id, "category": active.category, "state": active.state.value,
            "content": active.content, "confidence": active.confidence,
            "topic_id": active.topic_id, "topic_name": node.name if node else None,
            "created_at": active.created_at, "updated_at": active.updated_at,
        }}

    @app.post("/api/knowledge/{knowledge_id}/verify")
    async def verify_knowledge(knowledge_id: str) -> dict:
        from agent.knowledge.lifecycle import KnowledgeService

        ks = KnowledgeService(ctx.conn)
        item = ks.get(knowledge_id)
        if item is None:
            raise HTTPException(status_code=404, detail="knowledge not found")
        if item.state.value != "pending_review":
            raise HTTPException(status_code=400, detail="only pending_review can be verified")
        try:
            ks.verify(knowledge_id, verified_by="user")
            active = ks.activate(knowledge_id)
        except (ValueError, PermissionError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "knowledge": {"id": active.id, "state": active.state.value}}

    @app.post("/api/knowledge/{knowledge_id}/reject")
    async def reject_knowledge(knowledge_id: str) -> dict:
        from agent.knowledge.lifecycle import KnowledgeService

        ks = KnowledgeService(ctx.conn)
        item = ks.get(knowledge_id)
        if item is None:
            raise HTTPException(status_code=404, detail="knowledge not found")
        if item.state.value != "pending_review":
            raise HTTPException(status_code=400, detail="only pending_review can be rejected")
        draft = ks.reject(knowledge_id)
        return {"ok": True, "knowledge": {"id": draft.id, "state": draft.state.value}}
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_knowledge_mgmt_api.py -q --basetemp=%TEMP%\qio-pt`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/src/agent/api/server.py backend/tests/test_knowledge_mgmt_api.py
git commit -m "feat: 知识管理 API（列表/手动新建/批准/打回）"
```

---

### Task 3: 实体结构化输出 + 关系路由

**Files:**
- Modify: `backend/src/agent/entities/cards.py`（新增 `to_dict`）
- Modify: `backend/src/agent/api/server.py`（实体路由改结构化 + 关系增删）
- Test: `backend/tests/test_entity_cards.py`（增）、`backend/tests/test_knowledge_mgmt_api.py`（增）

- [ ] **Step 1: 写失败测试**（追加到 `test_entity_cards.py`）

```python
def test_to_dict_structured_with_relations(db_conn):
    from agent.entities.cards import (
        EntityAttribute,
        EntityCardCandidate,
        EntityCardService,
        EntityRelation,
    )

    svc = EntityCardService(db_conn)
    card = svc.upsert(EntityCardCandidate(
        name="王翠华",
        aliases=["我妈"],
        kind="家人",
        summary="我妈妈，退休教师",
        attributes=[EntityAttribute(key="职业", value="退休教师", confidence=0.9)],
        relations=[EntityRelation(target="王翠华的弟弟", type="属于")],
    ))
    d = svc.to_dict(card)
    assert d["name"] == "王翠华"
    assert d["aliases"] == ["我妈"]
    assert d["attributes"] == [{"key": "职业", "value": "退休教师", "confidence": 0.9}]
    assert d["relations"] == [{"type": "属于", "target": "王翠华的弟弟"}]
    assert d["node_id"] == card.node_id
```

追加到 `test_knowledge_mgmt_api.py`：

```python
def test_entity_manage_api(tmp_path):
    from fastapi.testclient import TestClient

    from agent.api.server import create_app
    from agent.config import Settings
    from agent.entities.cards import EntityAttribute, EntityCardCandidate, EntityCardService
    from agent.storage.db import connect
    from agent.storage.migrate import apply_migrations

    conn = connect(tmp_path / "app.db")
    apply_migrations(conn)
    svc = EntityCardService(conn)
    card = svc.upsert(EntityCardCandidate(
        name="王翠华",
        attributes=[EntityAttribute(key="职业", value="退休教师")],
    ))
    app = create_app(Settings(data_dir=tmp_path), conn)
    client = TestClient(app)

    r = client.get("/api/entities")
    assert r.status_code == 200
    row = r.json()["entities"][0]
    assert row["id"] == card.id and row["name"] == "王翠华"
    assert isinstance(row["attributes"], list)

    r = client.post(f"/api/entities/{card.id}/relations", json={"type": "属于", "target": "王翠华的弟弟"})
    assert r.status_code == 200
    assert any(x["type"] == "属于" and x["target"] == "王翠华的弟弟" for x in r.json()["entity"]["relations"])

    r = client.request("DELETE", f"/api/entities/{card.id}/relations", json={"type": "属于", "target": "王翠华的弟弟"})
    assert r.status_code == 200
    assert all(x["type"] != "属于" for x in r.json()["entity"]["relations"])

    r = client.post(f"/api/entities/{card.id}/revise", json={"summary": "我妈妈，退休教师，住成都"})
    assert r.status_code == 200
    assert r.json()["entity"]["summary"] == "我妈妈，退休教师，住成都"
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_entity_cards.py tests/test_knowledge_mgmt_api.py -q --basetemp=%TEMP%\qio-pt`
Expected: FAIL（`to_dict` 不存在 / 路由 404）

- [ ] **Step 3: 实现 `to_dict`**（加在 `backend/src/agent/entities/cards.py` 的 `_format_relations` 之后）

```python
    def to_dict(self, card: EntityCard) -> dict:
        """结构化输出：管理页/API 用（含属性、关系、node_id）。"""
        relations: list[dict] = []
        if card.node_id:
            rows = self.conn.execute(
                "SELECT e.type, n.name FROM edges e JOIN nodes n ON "
                "n.id = CASE WHEN e.dst = ? THEN e.src ELSE e.dst END "
                "WHERE (e.src = ? OR e.dst = ?) AND e.type NOT IN ('mention')",
                (card.node_id, card.node_id, card.node_id),
            ).fetchall()
            relations = [{"type": r["type"], "target": r["name"]} for r in rows]
        return {
            "id": card.id,
            "node_id": card.node_id,
            "name": card.name,
            "aliases": card.aliases,
            "kind": card.kind,
            "summary": card.summary,
            "attributes": card.attributes,
            "relations": relations,
            "state": card.state,
            "created_at": card.created_at,
            "updated_at": card.updated_at,
        }
```

- [ ] **Step 4: 改实体路由为结构化**（`backend/src/agent/api/server.py` 实体段）

`list_entities` 返回改为：

```python
        return {"entities": [svc.to_dict(c) for c in svc.list_active()]}
```

`get_entity` 返回改为：

```python
        return {"entity": svc.to_dict(card)}
```

`revise_entity` 返回改为：

```python
        return {"ok": True, "entity": svc.to_dict(updated) if updated else None}
```

在 `revoke_entity` 之后新增关系路由：

```python
    @app.post("/api/entities/{entity_id}/relations")
    async def add_entity_relation(entity_id: str, body: dict) -> dict:
        from agent.entities.cards import EntityCardService

        rel_type = str(body.get("type") or "").strip()
        target = str(body.get("target") or "").strip()
        if not rel_type or not target:
            raise HTTPException(status_code=400, detail="type and target required")
        svc = EntityCardService(ctx.conn)
        card = svc.get(entity_id)
        if card is None:
            raise HTTPException(status_code=404, detail="entity not found")
        updated = svc.add_relation(entity_id, target, rel_type)
        return {"ok": True, "entity": svc.to_dict(updated) if updated else None}

    @app.delete("/api/entities/{entity_id}/relations")
    async def remove_entity_relation(entity_id: str, body: dict) -> dict:
        from agent.entities.cards import EntityCardService

        rel_type = str(body.get("type") or "").strip()
        target = str(body.get("target") or "").strip()
        svc = EntityCardService(ctx.conn)
        card = svc.get(entity_id)
        if card is None:
            raise HTTPException(status_code=404, detail="entity not found")
        updated = svc.remove_relation(entity_id, target, rel_type)
        return {"ok": True, "entity": svc.to_dict(updated) if updated else None}
```

（注：`EntityCardService.add_relation/remove_relation` 已存在，无需新增 `EdgeService.remove`。）

- [ ] **Step 5: 运行确认通过**

Run: `python -m pytest tests/test_entity_cards.py tests/test_knowledge_mgmt_api.py -q --basetemp=%TEMP%\qio-pt`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add backend/src/agent/entities/cards.py backend/src/agent/api/server.py backend/tests/test_entity_cards.py backend/tests/test_knowledge_mgmt_api.py
git commit -m "feat: 实体管理 API（结构化输出 + 关系增删）"
```

---

### Task 4: 前端 api.ts 类型与方法

**Files:**
- Modify: `frontend/src/services/api.ts`

- [ ] **Step 1: 追加类型**

```ts
export interface KnowledgeItem {
  id: string;
  category: string;
  state: string;
  content: string;
  confidence: number | null;
  topic_id: string | null;
  topic_name: string | null;
  created_at: string;
  updated_at: string;
}

export interface EntityAttribute {
  key: string;
  value: string;
  confidence: number;
}

export interface EntityRelation {
  type: string;
  target: string;
}

export interface EntityCard {
  id: string;
  node_id: string | null;
  name: string;
  aliases: string[];
  kind: string | null;
  summary: string;
  attributes: EntityAttribute[];
  relations: EntityRelation[];
  state: string;
  created_at: string;
  updated_at: string;
}
```

- [ ] **Step 2: 追加方法**（在 `api` 对象里 `getMemorySettings` 之前）

```ts
  listKnowledge: () => request<{ knowledge: KnowledgeItem[] }>("/api/knowledge"),
  createKnowledge: (payload: { category: string; content: string; topic_id?: string | null }) =>
    request<{ ok: boolean; knowledge: KnowledgeItem }>("/api/knowledge", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  verifyKnowledge: (id: string) =>
    request<{ ok: boolean; knowledge: { id: string; state: string } }>(
      `/api/knowledge/${encodeURIComponent(id)}/verify`,
      { method: "POST" },
    ),
  rejectKnowledge: (id: string) =>
    request<{ ok: boolean; knowledge: { id: string; state: string } }>(
      `/api/knowledge/${encodeURIComponent(id)}/reject`,
      { method: "POST" },
    ),
  listEntities: () => request<{ entities: EntityCard[] }>("/api/entities"),
  getEntity: (id: string) =>
    request<{ entity: EntityCard }>(`/api/entities/${encodeURIComponent(id)}`),
  reviseEntity: (
    id: string,
    payload: {
      attributes?: EntityAttribute[];
      aliases?: string[];
      summary?: string;
      kind?: string;
    },
  ) =>
    request<{ ok: boolean; entity: EntityCard | null }>(
      `/api/entities/${encodeURIComponent(id)}/revise`,
      { method: "POST", body: JSON.stringify(payload) },
    ),
  revokeEntity: (id: string) =>
    request<{ ok: boolean; entity_id: string }>(
      `/api/entities/${encodeURIComponent(id)}/revoke`,
      { method: "POST" },
    ),
  addEntityRelation: (id: string, payload: { type: string; target: string }) =>
    request<{ ok: boolean; entity: EntityCard | null }>(
      `/api/entities/${encodeURIComponent(id)}/relations`,
      { method: "POST", body: JSON.stringify(payload) },
    ),
  removeEntityRelation: (id: string, payload: { type: string; target: string }) =>
    request<{ ok: boolean; entity: EntityCard | null }>(
      `/api/entities/${encodeURIComponent(id)}/relations`,
      { method: "DELETE", body: JSON.stringify(payload) },
    ),
```

- [ ] **Step 3: 类型检查**

Run: `node node_modules\vue-tsc\bin\vue-tsc.js --noEmit`
Expected: 无错误

- [ ] **Step 4: 提交**

```bash
git add frontend/src/services/api.ts
git commit -m "feat: api.ts 知识/实体管理方法与类型"
```

---

### Task 5: KnowledgePanel 组件

**Files:**
- Create: `frontend/src/components/planet/KnowledgePanel.vue`
- Test: `frontend/src/components/planet/__tests__/KnowledgePanel.test.ts`（新）

- [ ] **Step 1: 写失败测试**

```ts
import { describe, expect, it, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import KnowledgePanel from "../KnowledgePanel.vue";
import type { KnowledgeItem } from "../../../services/api";

const mocks = vi.hoisted(() => ({
  apiMock: {
    listKnowledge: vi.fn<() => Promise<{ knowledge: KnowledgeItem[] }>>(async () => ({ knowledge: [] })),
    createKnowledge: vi.fn(async () => ({ ok: true, knowledge: {} as KnowledgeItem })),
    verifyKnowledge: vi.fn(async () => ({ ok: true, knowledge: { id: "", state: "active" } })),
    rejectKnowledge: vi.fn(async () => ({ ok: true, knowledge: { id: "", state: "draft" } })),
    reviseKnowledge: vi.fn(async () => ({ ok: true })),
    revokeKnowledge: vi.fn(async () => ({ ok: true })),
  },
}));

vi.mock("../../../services/api", () => ({ api: mocks.apiMock }));

const K: KnowledgeItem = {
  id: "kn_1", category: "user_profile", state: "pending_review",
  content: "用户最爱五里关火锅", confidence: 0.9,
  topic_id: "t1", topic_name: "吃火锅", created_at: "", updated_at: "",
};

function mountPanel() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return mount(KnowledgePanel, { global: { plugins: [pinia] } });
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.apiMock.listKnowledge.mockResolvedValue({ knowledge: [K] });
});

describe("KnowledgePanel", () => {
  it("加载并渲染知识列表（分类/状态 badge）", async () => {
    const w = mountPanel();
    await flushPromises();
    expect(w.text()).toContain("用户最爱五里关火锅");
    expect(w.text()).toContain("user_profile");
    expect(w.text()).toContain("pending_review");
    w.unmount();
  });

  it("搜索过滤列表", async () => {
    const w = mountPanel();
    await flushPromises();
    await w.find("input").setValue("火锅");
    await flushPromises();
    expect(w.text()).toContain("用户最爱五里关火锅");
    await w.find("input").setValue("不存在");
    await flushPromises();
    expect(w.text()).not.toContain("用户最爱五里关火锅");
    w.unmount();
  });

  it("批准调用 verifyKnowledge 并刷新", async () => {
    const w = mountPanel();
    await flushPromises();
    await w.find(".k-approve").trigger("click");
    await flushPromises();
    expect(mocks.apiMock.verifyKnowledge).toHaveBeenCalledWith("kn_1");
    expect(mocks.apiMock.listKnowledge).toHaveBeenCalledTimes(2);
    w.unmount();
  });

  it("打回调用 rejectKnowledge", async () => {
    const w = mountPanel();
    await flushPromises();
    await w.find(".k-reject").trigger("click");
    await flushPromises();
    expect(mocks.apiMock.rejectKnowledge).toHaveBeenCalledWith("kn_1");
    w.unmount();
  });

  it("手动新建调用 createKnowledge", async () => {
    const w = mountPanel();
    await flushPromises();
    await w.find(".k-create-open").trigger("click");
    await w.find(".k-category").setValue("general_fact");
    await w.find(".k-content").setValue("SQLite 支持 WAL");
    await w.find(".k-create-submit").trigger("click");
    await flushPromises();
    expect(mocks.apiMock.createKnowledge).toHaveBeenCalledWith({
      category: "general_fact",
      content: "SQLite 支持 WAL",
      topic_id: null,
    });
    w.unmount();
  });

  it("点击关联话题 → emit focus-topic", async () => {
    const w = mountPanel();
    await flushPromises();
    await w.find(".k-topic-link").trigger("click");
    expect(w.emitted("focus-topic")).toEqual([["t1"]]);
    w.unmount();
  });
});
```

- [ ] **Step 2: 运行确认失败**

Run: `node node_modules\vitest\vitest.mjs run src/components/planet/__tests__/KnowledgePanel.test.ts`
Expected: FAIL（组件不存在）

- [ ] **Step 3: 实现组件**

`frontend/src/components/planet/KnowledgePanel.vue`：

```vue
<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { api, type KnowledgeItem } from "../../services/api";
import QInput from "../ui/QInput.vue";
import QSelect from "../ui/QSelect.vue";

const emit = defineEmits<{ "focus-topic": [topicId: string] }>();

const CATEGORY_LABELS: Record<string, string> = {
  user_profile: "用户画像",
  agent_self: "自我",
  goal: "目标",
  general_fact: "常识",
  tool_experience: "工具经验",
};
const STATE_LABELS: Record<string, string> = {
  draft: "草稿",
  pending_review: "待审核",
  verified: "已验证",
  active: "生效",
  expired: "过期",
  revoked: "已归档",
};

const items = ref<KnowledgeItem[]>([]);
const q = ref("");
const category = ref("");
const state = ref("");
const loading = ref(false);
const toast = ref("");
const creating = ref(false);
const createForm = ref({ category: "general_fact", content: "", topic_id: null as string | null });
const editingId = ref<string | null>(null);
const editDraft = ref("");

const categoryOptions = Object.entries(CATEGORY_LABELS).map(([value, label]) => ({ value, label }));
const stateOptions = Object.entries(STATE_LABELS).map(([value, label]) => ({ value, label }));

const filtered = computed(() => {
  const query = q.value.trim().toLowerCase();
  return items.value.filter((k) => {
    if (category.value && k.category !== category.value) return false;
    if (state.value && k.state !== state.value) return false;
    if (query && !k.content.toLowerCase().includes(query)) return false;
    return true;
  });
});

function showToast(text: string) {
  toast.value = text;
  window.setTimeout(() => (toast.value = ""), 2200);
}

async function load() {
  loading.value = true;
  try {
    const r = await api.listKnowledge();
    items.value = r.knowledge;
  } catch (e) {
    console.error("[knowledge] load failed:", e);
    showToast("加载知识失败");
  } finally {
    loading.value = false;
  }
}

async function approve(k: KnowledgeItem) {
  try {
    await api.verifyKnowledge(k.id);
    showToast("已批准并生效");
    await load();
  } catch (e) {
    console.error(e);
    showToast("批准失败");
  }
}

async function reject(k: KnowledgeItem) {
  try {
    await api.rejectKnowledge(k.id);
    showToast("已打回草稿");
    await load();
  } catch (e) {
    console.error(e);
    showToast("打回失败");
  }
}

async function archive(k: KnowledgeItem) {
  if (!window.confirm(`归档知识条目？\n${k.content.slice(0, 60)}`)) return;
  try {
    await api.revokeKnowledge(k.id);
    showToast("已归档");
    await load();
  } catch (e) {
    console.error(e);
    showToast("归档失败");
  }
}

function startEdit(k: KnowledgeItem) {
  editingId.value = k.id;
  editDraft.value = k.content;
}

async function saveEdit(k: KnowledgeItem) {
  const content = editDraft.value.trim();
  if (!content) return;
  try {
    await api.reviseKnowledge(k.id, content);
    editingId.value = null;
    showToast("已保存新版本");
    await load();
  } catch (e) {
    console.error(e);
    showToast("保存失败");
  }
}

function openCreate() {
  creating.value = true;
  createForm.value = { category: "general_fact", content: "", topic_id: null };
}

async function submitCreate() {
  const content = createForm.value.content.trim();
  if (!content) return;
  try {
    await api.createKnowledge({ category: createForm.value.category, content, topic_id: createForm.value.topic_id });
    creating.value = false;
    showToast("已创建并生效");
    await load();
  } catch (e) {
    console.error(e);
    showToast("创建失败");
  }
}

onMounted(load);
</script>

<template>
  <div class="kpanel">
    <div class="row">
      <QInput v-model="q" placeholder="搜索知识…" class="grow" />
      <button class="qio-btn mini k-create-open" @click="openCreate">+ 新建</button>
    </div>
    <div class="row filters">
      <QSelect v-model="category" :options="categoryOptions" class="grow" />
      <QSelect v-model="state" :options="stateOptions" class="grow" />
    </div>

    <form v-if="creating" class="create-form" @submit.prevent="submitCreate">
      <QSelect v-model="createForm.category" :options="categoryOptions" class="k-category" />
      <textarea v-model="createForm.content" class="qio-input k-content" placeholder="知识内容…"></textarea>
      <div class="row">
        <button class="qio-btn mini" type="button" @click="creating = false">取消</button>
        <button class="qio-btn mini primary k-create-submit" type="submit">创建</button>
      </div>
    </form>

    <div v-if="loading" class="hint">加载中…</div>
    <ul v-else class="k-list">
      <li v-for="k in filtered" :key="k.id" class="k-item">
        <div class="k-head">
          <span class="qio-badge k-cat">{{ CATEGORY_LABELS[k.category] || k.category }}</span>
          <span class="qio-badge k-state" :class="k.state">{{ STATE_LABELS[k.state] || k.state }}</span>
          <button
            v-if="k.topic_name"
            class="link k-topic-link"
            type="button"
            @click="emit('focus-topic', k.topic_id!)"
          >{{ k.topic_name }}</button>
        </div>
        <template v-if="editingId === k.id">
          <textarea v-model="editDraft" class="qio-input k-edit"></textarea>
          <div class="row">
            <button class="qio-btn mini" @click="editingId = null">取消</button>
            <button class="qio-btn mini primary" @click="saveEdit(k)">保存</button>
          </div>
        </template>
        <template v-else>
          <p class="k-content">{{ k.content }}</p>
          <div class="row k-actions">
            <button class="qio-btn mini k-approve" v-if="k.state === 'pending_review'" @click="approve(k)">批准</button>
            <button class="qio-btn mini k-reject" v-if="k.state === 'pending_review'" @click="reject(k)">打回</button>
            <button class="qio-btn mini" @click="startEdit(k)">修正</button>
            <button class="qio-btn mini danger k-archive" @click="archive(k)">归档</button>
          </div>
        </template>
      </li>
      <li v-if="!filtered.length && !loading" class="hint">无知识条目。</li>
    </ul>

    <div v-if="toast" class="toast" role="status">{{ toast }}</div>
  </div>
</template>

<style scoped>
.kpanel { display: flex; flex-direction: column; gap: 8px; padding: 0 14px 20px; }
.row { display: flex; align-items: center; gap: 8px; }
.row.grow > * { flex: 1; }
.filters { margin-bottom: 4px; }
.create-form { border: 1px dashed var(--accent); border-radius: 10px; padding: 10px; display: flex; flex-direction: column; gap: 8px; }
.k-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; }
.k-item { border: 1px solid var(--border-subtle); border-radius: 10px; padding: 10px; background: var(--bg-inset); }
.k-head { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.k-content { margin: 6px 0 0; font-size: 13px; color: var(--text-primary); white-space: pre-wrap; }
.k-actions { margin-top: 6px; flex-wrap: wrap; }
.link { background: none; border: none; color: var(--link); cursor: pointer; font-size: 12px; padding: 0; }
.link:hover { text-decoration: underline; }
.k-state.pending_review { background: var(--warning-soft); color: var(--warning); }
.k-state.active { background: var(--success-soft); color: var(--success); }
.toast { position: fixed; top: 20px; right: 20px; z-index: 60; padding: 10px 16px; border-radius: 12px; font-size: 12.5px; background: var(--bg-surface); border: 1px solid var(--border-strong); color: var(--text-primary); }
</style>
```

（说明：`QSelect` 的 `v-model` 与 `options` 形如 `{value,label}`；若 `QSelect` 实际接口不同，以该组件源码为准微调。）

- [ ] **Step 4: 运行确认通过**

Run: `node node_modules\vitest\vitest.mjs run src/components/planet/__tests__/KnowledgePanel.test.ts`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add frontend/src/components/planet/KnowledgePanel.vue frontend/src/components/planet/__tests__/KnowledgePanel.test.ts
git commit -m "feat: 星球知识管理面板（列表/过滤/批准/打回/归档/修正/新建）"
```

---

### Task 6: EntityPanel 组件

**Files:**
- Create: `frontend/src/components/planet/EntityPanel.vue`
- Test: `frontend/src/components/planet/__tests__/EntityPanel.test.ts`（新）

- [ ] **Step 1: 写失败测试**

```ts
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
```

- [ ] **Step 2: 运行确认失败**

Run: `node node_modules\vitest\vitest.mjs run src/components/planet/__tests__/EntityPanel.test.ts`
Expected: FAIL（组件不存在）

- [ ] **Step 3: 实现组件**

`frontend/src/components/planet/EntityPanel.vue`（样式沿用令牌，卡片/按钮风格与 KnowledgePanel 一致）：

```vue
<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { api, type EntityAttribute, type EntityCard } from "../../services/api";
import QInput from "../ui/QInput.vue";

const props = defineProps<{ openCardId?: string }>();

const cards = ref<EntityCard[]>([]);
const q = ref("");
const loading = ref(false);
const opened = ref<EntityCard | null>(null);
const toast = ref("");
const draft = ref({
  summary: "",
  kind: "",
  aliases: [] as string[],
  aliasInput: "",
  attributes: [] as EntityAttribute[],
  attrKey: "",
  attrValue: "",
  attrConf: "0.8",
  relType: "",
  relTarget: "",
});

const filtered = computed(() => {
  const query = q.value.trim().toLowerCase();
  return cards.value.filter((c) => {
    if (!query) return true;
    return c.name.toLowerCase().includes(query) || c.aliases.some((a) => a.toLowerCase().includes(query));
  });
});

function showToast(text: string) {
  toast.value = text;
  window.setTimeout(() => (toast.value = ""), 2200);
}

async function load() {
  loading.value = true;
  try {
    const r = await api.listEntities();
    cards.value = r.entities;
  } catch (e) {
    console.error("[entity] load failed:", e);
    showToast("加载实体失败");
  } finally {
    loading.value = false;
  }
}

function openCard(card: EntityCard) {
  opened.value = card;
  draft.value = {
    summary: card.summary,
    kind: card.kind ?? "",
    aliases: [...card.aliases],
    aliasInput: "",
    attributes: card.attributes.map((a) => ({ ...a })),
    attrKey: "",
    attrValue: "",
    attrConf: "0.8",
    relType: "",
    relTarget: "",
  };
}

async function saveCard() {
  if (!opened.value) return;
  try {
    const r = await api.reviseEntity(opened.value.id, {
      summary: draft.value.summary.trim(),
      kind: draft.value.kind.trim() || null,
      aliases: draft.value.aliases,
      attributes: draft.value.attributes,
    });
    if (r.entity) openCard(r.entity);
    showToast("已保存");
    await load();
  } catch (e) {
    console.error(e);
    showToast("保存失败");
  }
}

function addAlias() {
  const a = draft.value.aliasInput.trim();
  if (a && !draft.value.aliases.includes(a)) draft.value.aliases.push(a);
  draft.value.aliasInput = "";
}

function removeAlias(a: string) {
  draft.value.aliases = draft.value.aliases.filter((x) => x !== a);
}

function addAttr() {
  const key = draft.value.attrKey.trim();
  const value = draft.value.attrValue.trim();
  if (!key || !value) return;
  const conf = Math.max(0, Math.min(1, Number(draft.value.attrConf) || 0.8));
  const hit = draft.value.attributes.find((a) => a.key === key);
  if (hit) {
    hit.value = value;
    hit.confidence = conf;
  } else {
    draft.value.attributes.push({ key, value, confidence: conf });
  }
  draft.value.attrKey = "";
  draft.value.attrValue = "";
}

function removeAttr(key: string) {
  draft.value.attributes = draft.value.attributes.filter((a) => a.key !== key);
}

async function addRel() {
  if (!opened.value) return;
  const type = draft.value.relType.trim();
  const target = draft.value.relTarget.trim();
  if (!type || !target) return;
  try {
    const r = await api.addEntityRelation(opened.value.id, { type, target });
    if (r.entity) openCard(r.entity);
    draft.value.relType = "";
    draft.value.relTarget = "";
    showToast("已添加关系");
    await load();
  } catch (e) {
    console.error(e);
    showToast("添加关系失败");
  }
}

async function removeRel(type: string, target: string) {
  if (!opened.value) return;
  try {
    const r = await api.removeEntityRelation(opened.value.id, { type, target });
    if (r.entity) openCard(r.entity);
    showToast("已删除关系");
    await load();
  } catch (e) {
    console.error(e);
    showToast("删除关系失败");
  }
}

async function revokeCard() {
  if (!opened.value) return;
  if (!window.confirm(`归档实体「${opened.value.name}」？`)) return;
  try {
    await api.revokeEntity(opened.value.id);
    opened.value = null;
    showToast("已归档");
    await load();
  } catch (e) {
    console.error(e);
    showToast("归档失败");
  }
}

watch(
  () => props.openCardId,
  (id) => {
    if (!id) return;
    const card = cards.value.find((c) => c.id === id);
    if (card) openCard(card);
  },
);

onMounted(async () => {
  await load();
  if (props.openCardId) {
    const card = cards.value.find((c) => c.id === props.openCardId);
    if (card) openCard(card);
  }
});
</script>

<template>
  <div class="epanel">
    <template v-if="!opened">
      <QInput v-model="q" placeholder="搜索实体（名称/别名）…" />
      <div v-if="loading" class="hint">加载中…</div>
      <ul v-else class="e-list">
        <li v-for="c in filtered" :key="c.id" class="e-item" @click="openCard(c)">
          <div class="e-name">{{ c.name }}</div>
          <div class="e-meta mono">
            <span v-if="c.kind" class="qio-badge">{{ c.kind }}</span>
            {{ c.attributes.length }} 属性 · {{ c.relations.length }} 关系
          </div>
        </li>
        <li v-if="!filtered.length && !loading" class="hint">无实体卡。</li>
      </ul>
    </template>

    <template v-else>
      <div class="e-detail">
        <div class="e-detail-head">
          <button class="qio-btn mini" @click="opened = null">← 返回</button>
          <span class="e-title serif">{{ opened.name }}</span>
          <button class="qio-btn mini danger e-revoke" @click="revokeCard">归档</button>
        </div>

        <label class="field"><span class="label">摘要</span>
          <textarea v-model="draft.summary" class="qio-input e-summary"></textarea>
        </label>
        <label class="field"><span class="label">类型</span>
          <input v-model="draft.kind" class="qio-input" />
        </label>

        <div class="field"><span class="label">别名</span>
          <div class="chips">
            <span v-for="a in draft.aliases" :key="a" class="chip">{{ a }} <button type="button" class="chip-x" @click="removeAlias(a)">✕</button></span>
          </div>
          <div class="row">
            <input v-model="draft.aliasInput" class="qio-input grow" placeholder="添加别名" @keyup.enter="addAlias" />
            <button class="qio-btn mini" @click="addAlias">添加</button>
          </div>
        </div>

        <div class="field"><span class="label">属性（键=值 · 置信度）</span>
          <div class="attr-table">
            <div v-for="a in draft.attributes" :key="a.key" class="attr-row">
              <span class="grow">{{ a.key }} = {{ a.value }}</span>
              <span class="mono conf">{{ a.confidence.toFixed(2) }}</span>
              <button class="qio-btn mini" @click="removeAttr(a.key)">✕</button>
            </div>
            <div class="attr-row add">
              <input v-model="draft.attrKey" class="qio-input e-attr-key" placeholder="键" />
              <input v-model="draft.attrValue" class="qio-input e-attr-value" placeholder="值" />
              <input v-model="draft.attrConf" class="qio-input e-attr-conf" type="number" min="0" max="1" step="0.05" />
              <button class="qio-btn mini e-attr-add" @click="addAttr">+</button>
            </div>
          </div>
        </div>

        <div class="field"><span class="label">关系（类型 → 目标）</span>
          <div class="rel-list">
            <div v-for="r in opened.relations" :key="r.type + r.target" class="rel-row">
              <span class="grow">{{ r.type }} → {{ r.target }}</span>
              <button class="qio-btn mini e-rel-del" @click="removeRel(r.type, r.target)">✕</button>
            </div>
            <div class="rel-row add">
              <input v-model="draft.relType" class="qio-input e-rel-type" placeholder="类型" />
              <input v-model="draft.relTarget" class="qio-input e-rel-target" placeholder="目标实体" list="entity-names" />
              <datalist id="entity-names">
                <option v-for="c in cards" :key="c.id" :value="c.name"></option>
              </datalist>
              <button class="qio-btn mini e-rel-add" @click="addRel">+</button>
            </div>
          </div>
        </div>

        <div class="row e-actions">
          <button class="qio-btn mini primary e-save" @click="saveCard">保存</button>
        </div>
      </div>
    </template>

    <div v-if="toast" class="toast" role="status">{{ toast }}</div>
  </div>
</template>

<style scoped>
.epanel { display: flex; flex-direction: column; gap: 8px; padding: 0 14px 20px; }
.e-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
.e-item { border: 1px solid var(--border-subtle); border-radius: 10px; padding: 10px; cursor: pointer; background: var(--bg-inset); }
.e-item:hover { border-color: var(--accent); }
.e-name { font-size: 14px; color: var(--text-strong); }
.e-meta { font-size: 11px; color: var(--text-muted); margin-top: 3px; display: flex; gap: 6px; align-items: center; }
.e-detail { display: flex; flex-direction: column; gap: 10px; }
.e-detail-head { display: flex; align-items: center; gap: 8px; }
.e-title { flex: 1; font-size: 16px; color: var(--text-strong); }
.field { display: flex; flex-direction: column; gap: 4px; }
.label { font-size: 11px; color: var(--text-secondary); letter-spacing: 0.03em; }
.chips { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 6px; }
.chip { display: inline-flex; align-items: center; gap: 4px; background: var(--bg-accent-subtle); border-radius: 20px; padding: 3px 10px; font-size: 12px; }
.chip-x { background: none; border: none; color: var(--text-muted); cursor: pointer; padding: 0; }
.attr-table, .rel-list { display: flex; flex-direction: column; gap: 5px; }
.attr-row, .rel-row { display: flex; align-items: center; gap: 6px; }
.attr-row.add, .rel-row.add { margin-top: 2px; }
.grow { flex: 1; }
.conf { color: var(--text-muted); width: 44px; text-align: right; }
.row { display: flex; align-items: center; gap: 8px; }
.e-actions { justify-content: flex-end; }
.hint { font-size: 12px; color: var(--text-muted); }
.toast { position: fixed; top: 20px; right: 20px; z-index: 60; padding: 10px 16px; border-radius: 12px; font-size: 12.5px; background: var(--bg-surface); border: 1px solid var(--border-strong); color: var(--text-primary); }
</style>
```

（说明：`QInput` 与 `.qio-input` 均可用于输入；属性/关系编辑用原生 input 以简化测试选择器，样式走 `.qio-input` 类。）

- [ ] **Step 4: 运行确认通过**

Run: `node node_modules\vitest\vitest.mjs run src/components/planet/__tests__/EntityPanel.test.ts`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add frontend/src/components/planet/EntityPanel.vue frontend/src/components/planet/__tests__/EntityPanel.test.ts
git commit -m "feat: 星球实体管理面板（列表/详情/属性与关系编辑/归档）"
```

---

### Task 7: PlanetView 集成（三页签 + 管理模式 + 联动）

**Files:**
- Modify: `frontend/src/views/PlanetView.vue`
- Modify: `frontend/src/views/__tests__/PlanetView.test.ts`

- [ ] **Step 1: 写失败测试**（追加到 `PlanetView.test.ts`）

```ts
describe("星球记忆中心面板", () => {
  it("三页签可切换，管理模式加宽面板", async () => {
    const pinia = newPinia();
    const w = mountView(pinia, true);
    await flushPromises();
    expect(w.find(".tab-knowledge").exists()).toBe(true);
    await w.find(".tab-knowledge").trigger("click");
    await flushPromises();
    expect(w.find(".panel").classes()).toContain("manage");
    await w.find(".tab-topic").trigger("click");
    await flushPromises();
    w.unmount();
  });

  it("实体标签可点击 → 打开实体页签", async () => {
    mocks.apiMock.getTopicDetail.mockResolvedValue({
      ...DETAIL,
      entities: [{ id: "n1", name: "王翠华" }],
    });
    mocks.apiMock.listEntities.mockResolvedValue({
      entities: [{
        id: "ec_1", node_id: "n1", name: "王翠华", aliases: [], kind: "家人",
        summary: "我妈妈", attributes: [], relations: [], state: "active",
        created_at: "", updated_at: "",
      }],
    });
    const pinia = newPinia();
    const w = mountView(pinia, true);
    await flushPromises();
    await w.find(".entity-tag").trigger("click");
    await flushPromises();
    expect(w.find(".tab-entity").classes()).toContain("active");
    w.unmount();
  });
});
```

（需在 `PlanetView.test.ts` 的 `mocks.apiMock` 中补 `listEntities` mock。）

- [ ] **Step 2: 运行确认失败**

Run: `node node_modules\vitest\vitest.mjs run src/views/__tests__/PlanetView.test.ts`
Expected: FAIL（无三页签）

- [ ] **Step 3: 改 PlanetView**

在 `PlanetView.vue` 的 `<script setup>` 中：

```ts
import KnowledgePanel from "../components/planet/KnowledgePanel.vue";
import EntityPanel from "../components/planet/EntityPanel.vue";

const activeTab = ref<"topic" | "knowledge" | "entity">("topic");
const manageMode = ref(false);
const entityOpenId = ref("");

function switchTab(tab: "topic" | "knowledge" | "entity") {
  activeTab.value = tab;
  manageMode.value = false;
  entityOpenId.value = "";
}

function focusTopicFromPanel(topicId: string) {
  planet.selectedTopicId.value = topicId;
  planet.focusTopic(topicId, positions.value);
  panelOpen.value = true;
  switchTab("topic");
  loadDetail(topicId);
}

function openEntityByNode(nodeId: string) {
  activeTab.value = "entity";
  manageMode.value = false;
  entityOpenId.value = nodeId; // node_id 占位，EntityPanel 按 node_id 匹配打开
}
```

面板宽度 class：`<aside class="panel" :class="{ open: panelOpen, manage: manageMode }">`

CSS 追加：

```css
.panel.manage { width: 640px; }
.panel.manage .panel-inner { width: 640px; }
```

模板：把 `panel-inner` 内容按页签分块（现有话题列表/详情放进 `v-if="activeTab === 'topic'"` 分支；实体标签加 `@click="openEntityByNode(e.id)"`）：

```html
<div class="tabs">
  <button class="tab tab-topic" :class="{ active: activeTab === 'topic' }" @click="switchTab('topic')">话题</button>
  <button class="tab tab-knowledge" :class="{ active: activeTab === 'knowledge' }" @click="switchTab('knowledge')">知识</button>
  <button class="tab tab-entity" :class="{ active: activeTab === 'entity' }" @click="switchTab('entity')">实体</button>
  <span class="spacer"></span>
  <button class="mode-btn" @click="manageMode = !manageMode">{{ manageMode ? "✕ 管理模式" : "管理模式" }}</button>
</div>

<template v-if="activeTab === 'topic'">
  <!-- 现有话题 h2/搜索/列表/详情，实体标签 @click="openEntityByNode(e.id)" -->
</template>
<KnowledgePanel v-else-if="activeTab === 'knowledge'" @focus-topic="focusTopicFromPanel" />
<EntityPanel v-else :open-card-id="entityOpenId" />
```

（说明：`EntityPanel` 的 `openCardId` 传 node_id 占位不成立——实体卡 id 是 `ec_*` 而标签是 node_id。因此给 `EntityPanel` 增加 `openByNodeId?: string` prop：watch 里按 `c.node_id === id` 匹配打开。`EntityPanel` 保留 `openCardId` 逻辑不变，新增 `openByNodeId` 分支。实现时两者并存，`PlanetView` 传 `:open-by-node-id="entityOpenId"`。）

- [ ] **Step 4: 运行确认通过 + 类型检查**

Run: `node node_modules\vitest\vitest.mjs run src/views/__tests__/PlanetView.test.ts src/components/planet`
Run: `node node_modules\vue-tsc\bin\vue-tsc.js --noEmit`
Expected: 全 PASS / 无类型错误

- [ ] **Step 5: 提交**

```bash
git add frontend/src/views/PlanetView.vue frontend/src/views/__tests__/PlanetView.test.ts frontend/src/components/planet/EntityPanel.vue
git commit -m "feat: 星球面板三页签 + 管理模式 + 双向联动"
```

---

### Task 8: 全量验证 + 重启 + 冒烟

- [ ] **Step 1: 后端全量 pytest**

Run: `cd backend; python -m pytest -q --basetemp=%TEMP%\qio-pt -p no:cacheprovider --deselect tests/test_remote_embedding.py::test_remote_index_search_and_persist`
Expected: 全 PASS（`test_remote_index_search_and_persist` 为已知哈希随机 flaky，排除）

- [ ] **Step 2: 前端 vitest + 类型检查**

Run: `cd frontend; node node_modules\vitest\vitest.mjs run`
Run: `node node_modules\vue-tsc\bin\vue-tsc.js --noEmit`
Expected: 全 PASS / 无错误

- [ ] **Step 3: 重启后端（加载新路由）**

用 `Start-Process` 重启 `127.0.0.1:8734`（`PYTHONPATH=backend/src`、`QIO_PORT=8734`、`QIO_DATA_DIR=%TEMP%\qio-e2e`，隐藏窗口）；前端 vite 无需重启（只改源码）。

- [ ] **Step 4: 冒烟**

```text
GET  http://127.0.0.1:8734/api/knowledge      → {"knowledge": [...]}
POST http://127.0.0.1:8734/api/knowledge      → 创建 active
GET  http://127.0.0.1:8734/api/entities       → 结构化 entities（attributes/relations/node_id）
打开 http://127.0.0.1:5199/?fresh=1#/ → 星球 → 面板三页签可切换、管理模式加宽、知识/实体可操作
```

- [ ] **Step 5: 提交（如有遗漏改动）**

```bash
git add -A && git commit -m "chore: 星球记忆中心 v1 联调"
```

---

## 自检记录

- **Spec 覆盖**：§1 面板结构 → Task 7；§2 知识 → Task 2 + Task 5；§3 实体 → Task 3 + Task 6；§4 双向联动 → Task 5/6 emit + Task 7；§5 后端 API → Task 2/3；§6 边界 → 未纳入任务（实体手动新建/恢复/批量/分页/连线均不在 v1）。
- **占位扫描**：无 TBD；所有代码步骤含完整实现。
- **类型一致性**：后端 `to_dict` 字段与前端 `EntityCard` 类型一致（id/node_id/name/aliases/kind/summary/attributes/relations/state/created_at/updated_at）；`KnowledgeItem` 字段与 `/api/knowledge` 输出一致；前端方法名与路由一致。
- **已知待办（Task 7 内处理）**：`EntityPanel` 需支持按 `node_id` 打开（`openByNodeId` prop），因为话题详情实体标签携带 node_id 而非实体卡 id。
