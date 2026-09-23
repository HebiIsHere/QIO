# 首次引导（欢迎页）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按 `2026-08-18-onboarding-design.md` 落地 QIO 的六步首次引导（欢迎页），并新增「本版本首次运行必展开一次」的版本触发规则。

**Architecture:** 后端新增 `OnboardingService` + 5 条 `/api/onboarding/*` 路由（状态 / 已展示 / 落库 / 完成 / 提示条开关），状态存在现有 `settings` 表；前端新增 Pinia store 承载状态，`OnboardingWizard.vue` 全屏向导在 `App.vue` 里按 `show_wizard` gating，对话页顶部加「继续设置」提示条、设置页加重跑入口。

**Tech Stack:** FastAPI + SQLite（pytest / TestClient）、Vue 3 + TypeScript + Pinia + vue-router（vitest + vue-tsc）、现有设计令牌与 `QInput` / `QSelect` 控件。

**Spec:** `docs/superpowers/specs/2026-08-18-onboarding-design.md`（六步内容、触发与完成判定、落库、接口、前端、边界）+ 2026-09-23 追加规则：**当前版本号与「已展示过的版本」不一致时，无论 onboarding 是否完成，都展开一次欢迎页**。

## Global Constraints

- 用户可见文案全部中文，沿用 spec §2 六步文案：欢迎 / 连接模型 / 认识你 / 偏好 / 目标 / 完成。
- 落库全部走幂等路径：重复走向导或重跑「设置助手」**不得**产生重复知识 / 实体卡 / 话题。
- 设置键前缀固定为 `onboarding.`：`done` / `wizard_seen` / `welcome_version` / `hint_dismissed` / `done_at` / `name`。
- `show_wizard = (not wizard_seen) or (welcome_version != app_version)`；`app_version` 取后端 `app.version`（`/api/instance` 已经暴露它）。
- 不引入新依赖；不新增路由页面；样式复用 `frontend/src/styles/tokens.css` 令牌与既有 `Q*` 控件。
- 前端验证命令（在 `frontend/` 下）：`npx vue-tsc --noEmit`、`npx vitest run`。后端验证命令（在 `backend/` 下，用 `backend/.venv`）：`python -m pytest tests -q`。
- 只改本计划列出的文件；不执行 `git push`；每个 Task 结束提交一次。

---

### Task 1: 后端 OnboardingService 与 API

**Files:**
- Create: `backend/src/agent/services/onboarding.py`
- Modify: `backend/src/agent/api/server.py`（在 `# -- knowledge management` 之前插入 onboarding 路由块）
- Test: `backend/tests/test_onboarding_api.py`

**Interfaces:**
- Consumes: `SettingsStore`（`agent.storage.settings`）、`KnowledgeService`（`agent.knowledge.lifecycle`）、`EntityCardService` / `EntityCardCandidate` / `EntityAttribute`（`agent.entities.cards`）、`NodeService`（`agent.graph.nodes`）、`ctx.conn`、`app.version`。
- Produces:
  - `OnboardingService(conn: sqlite3.Connection, app_version: str)`
  - `OnboardingService.status() -> OnboardingStatus`；`mark_seen() -> OnboardingStatus`；`complete() -> OnboardingStatus`；`set_hint_dismissed(dismissed: bool) -> OnboardingStatus`
  - `OnboardingService.save_profile(*, name: str, intro: str = "", tags: list[dict] | None = None, style: str = "", goals: list[str] | None = None) -> dict`
  - `OnboardingStatus.to_dict()`，字段固定为 `done / has_credential / has_name / wizard_seen / welcome_version / app_version / show_wizard / hint_dismissed`
  - HTTP：`GET /api/onboarding/status`、`POST /api/onboarding/seen`、`POST /api/onboarding/profile`、`POST /api/onboarding/complete`、`POST /api/onboarding/hint`

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/test_onboarding_api.py`：

```python
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from agent.api.server import create_app
from agent.config import Settings


@pytest.fixture()
def client(db_conn: sqlite3.Connection, settings: Settings):
    from agent.credentials.store import MemoryKeyring

    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as c:
        yield c


def test_status_first_run_shows_wizard(client):
    body = client.get("/api/onboarding/status").json()
    assert body["done"] is False
    assert body["has_credential"] is False
    assert body["has_name"] is False
    assert body["wizard_seen"] is False
    assert body["welcome_version"] == ""
    assert body["show_wizard"] is True
    assert body["hint_dismissed"] is False


def test_seen_stamps_current_version_and_stops_forcing(client):
    version = client.get("/api/instance").json()["version"]
    seen = client.post("/api/onboarding/seen").json()
    assert seen["wizard_seen"] is True
    assert seen["welcome_version"] == version
    assert seen["show_wizard"] is False
    # 幂等：再打一次仍然稳定
    assert client.post("/api/onboarding/seen").json()["show_wizard"] is False


def test_new_version_forces_wizard_once_even_when_done(client, db_conn):
    client.post("/api/onboarding/seen")
    client.post("/api/onboarding/complete")
    assert client.get("/api/onboarding/status").json()["show_wizard"] is False

    # 模拟「刚更新到这版」：已展示版本落后于当前版本
    from agent.storage.settings import SettingsStore

    SettingsStore(db_conn).set("onboarding.welcome_version", "0.0.0")
    body = client.get("/api/onboarding/status").json()
    assert body["done"] is True
    assert body["show_wizard"] is True
    assert client.post("/api/onboarding/seen").json()["show_wizard"] is False


def test_profile_writes_knowledge_card_and_topics_idempotently(client, db_conn):
    payload = {
        "name": "小舟",
        "intro": "在做本地优先的个人助手",
        "tags": [{"key": "职业", "value": "独立开发者"}],
        "style": "简洁",
        "goals": ["学习", "写作"],
    }
    first = client.post("/api/onboarding/profile", json=payload)
    assert first.status_code == 200
    summary = first.json()
    assert summary["name"] == "小舟"
    assert sorted(summary["topics"]) == ["写作", "学习"]

    # 重复提交不产生重复知识 / 实体卡 / 话题
    client.post("/api/onboarding/profile", json=payload)
    from agent.entities.cards import EntityCardService
    from agent.graph.nodes import NodeService
    from agent.knowledge.lifecycle import KnowledgeService

    profiles = [
        item
        for item in KnowledgeService(db_conn).list_items(category="user_profile")
        if item.state.value == "active"
    ]
    assert len(profiles) == 1
    assert "小舟" in profiles[0].content
    assert len([c for c in EntityCardService(db_conn).list_active() if c.name == "小舟"]) == 1
    assert len([t for t in NodeService(db_conn).list_topics() if t.name == "学习"]) == 1

    status = client.get("/api/onboarding/status").json()
    assert status["has_name"] is True


def test_profile_requires_name(client):
    assert client.post("/api/onboarding/profile", json={"name": "  "}).status_code == 400


def test_complete_and_hint_roundtrip(client):
    assert client.post("/api/onboarding/complete").json()["done"] is True
    assert client.post("/api/onboarding/hint", json={"dismissed": True}).json()["hint_dismissed"] is True
    assert client.post("/api/onboarding/hint", json={"dismissed": False}).json()["hint_dismissed"] is False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_onboarding_api.py -q`（工作目录 `backend/`，用 `backend/.venv` 的解释器）
Expected: FAIL —— `404 Not Found`（路由与 `OnboardingService` 还不存在）

- [ ] **Step 3: 实现 `OnboardingService`**

创建 `backend/src/agent/services/onboarding.py`：

```python
"""OnboardingService：首次引导（欢迎页）的状态与落库。

spec: docs/superpowers/specs/2026-08-18-onboarding-design.md
追加规则（2026-09-23）：只要「本版本还没展示过欢迎页」，即便 onboarding 已完成，
也强制展开一次 —— `show_wizard = (not wizard_seen) or (welcome_version != app_version)`。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from agent.entities.cards import EntityAttribute, EntityCardCandidate, EntityCardService
from agent.graph.nodes import NodeService
from agent.knowledge.lifecycle import KnowledgeService
from agent.storage.settings import SettingsStore

DONE_KEY = "onboarding.done"
SEEN_KEY = "onboarding.wizard_seen"
WELCOME_VERSION_KEY = "onboarding.welcome_version"
HINT_DISMISSED_KEY = "onboarding.hint_dismissed"
DONE_AT_KEY = "onboarding.done_at"
NAME_KEY = "onboarding.name"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class OnboardingStatus:
    done: bool
    has_credential: bool
    has_name: bool
    wizard_seen: bool
    welcome_version: str
    app_version: str
    show_wizard: bool
    hint_dismissed: bool

    def to_dict(self) -> dict:
        return {
            "done": self.done,
            "has_credential": self.has_credential,
            "has_name": self.has_name,
            "wizard_seen": self.wizard_seen,
            "welcome_version": self.welcome_version,
            "app_version": self.app_version,
            "show_wizard": self.show_wizard,
            "hint_dismissed": self.hint_dismissed,
        }


class OnboardingService:
    def __init__(self, conn: sqlite3.Connection, app_version: str) -> None:
        self.conn = conn
        self.app_version = app_version
        self.settings = SettingsStore(conn)

    # -- 状态 -------------------------------------------------------------

    def status(self) -> OnboardingStatus:
        credential_count = self.conn.execute(
            "SELECT COUNT(*) AS n FROM credentials"
        ).fetchone()["n"]
        wizard_seen = self.settings.get_bool(SEEN_KEY, False)
        welcome_version = self.settings.get(WELCOME_VERSION_KEY) or ""
        return OnboardingStatus(
            done=self.settings.get_bool(DONE_KEY, False),
            has_credential=credential_count > 0,
            has_name=bool((self.settings.get(NAME_KEY) or "").strip()),
            wizard_seen=wizard_seen,
            welcome_version=welcome_version,
            app_version=self.app_version,
            show_wizard=(not wizard_seen) or (welcome_version != self.app_version),
            hint_dismissed=self.settings.get_bool(HINT_DISMISSED_KEY, False),
        )

    def mark_seen(self) -> OnboardingStatus:
        """向导一旦打开就记「这个版本已经展示过欢迎页」。"""
        self.settings.set(SEEN_KEY, "1")
        self.settings.set(WELCOME_VERSION_KEY, self.app_version)
        return self.status()

    def complete(self) -> OnboardingStatus:
        self.settings.set(DONE_KEY, "1")
        self.settings.set(DONE_AT_KEY, _now())
        return self.status()

    def set_hint_dismissed(self, dismissed: bool) -> OnboardingStatus:
        self.settings.set(HINT_DISMISSED_KEY, "1" if dismissed else "0")
        return self.status()

    # -- 落库 -------------------------------------------------------------

    def save_profile(
        self,
        *,
        name: str,
        intro: str = "",
        tags: list[dict] | None = None,
        style: str = "",
        goals: list[str] | None = None,
    ) -> dict:
        clean_name = (name or "").strip()
        if not clean_name:
            raise ValueError("name is required")
        clean_tags = [
            {"key": str(t.get("key", "")).strip(), "value": str(t.get("value", "")).strip()}
            for t in (tags or [])
            if str(t.get("key", "")).strip() and str(t.get("value", "")).strip()
        ]
        clean_goals = [str(g).strip() for g in (goals or []) if str(g).strip()]

        knowledge_id = self._write_profile_knowledge(clean_name, intro.strip())
        entity_id = self._write_self_card(clean_name, intro.strip(), clean_tags, style.strip())
        topics = self._seed_topics(clean_goals)
        self.settings.set(NAME_KEY, clean_name)
        return {
            "name": clean_name,
            "knowledge_id": knowledge_id,
            "entity_id": entity_id,
            "topics": topics,
        }

    def _write_profile_knowledge(self, name: str, intro: str) -> str:
        """`user_profile` 知识：内容不变则复用，内容变了则 supersede 旧的。"""
        knowledge = KnowledgeService(self.conn)
        active = [
            item
            for item in knowledge.list_items(category="user_profile")
            if item.state.value == "active"
        ]
        content = f"用户称呼：{name}"
        if intro:
            content = f"{content}；自我介绍：{intro}"
        for item in active:
            if item.content == content:
                return item.id
        supersedes = active[0].id if active else None
        item = knowledge.create(
            category="user_profile",
            content=content,
            provenance={"source": "onboarding"},
            supersedes_id=supersedes,
        )
        knowledge.submit(item.id)
        knowledge.verify(item.id, verified_by="user")
        knowledge.activate(item.id)
        return item.id

    def _write_self_card(self, name: str, intro: str, tags: list[dict], style: str) -> str:
        cards = EntityCardService(self.conn)
        existing = cards.find_by_name(name)
        attributes = [
            EntityAttribute(key=t["key"], value=t["value"], confidence=1.0) for t in tags
        ]
        if style:
            attributes.append(EntityAttribute(key="回答风格", value=style, confidence=1.0))
        card = cards.upsert(
            EntityCardCandidate(
                name=name,
                kind="person",
                summary=intro,
                attributes=attributes,
            )
        )
        return card.id if existing is None else existing.id

    def _seed_topics(self, goals: list[str]) -> list[str]:
        nodes = NodeService(self.conn)
        existing = {topic.name for topic in nodes.list_topics()}
        created: list[str] = []
        for goal in goals:
            if goal in existing:
                continue
            nodes.create_topic(goal)
            existing.add(goal)
            created.append(goal)
        return created
```

- [ ] **Step 4: 接上 HTTP 路由**

在 `backend/src/agent/api/server.py` 的 `# -- knowledge management --------------------------------------------` 之前插入：

```python
    # -- onboarding（首次引导 / 欢迎页）----------------------------------

    def _onboarding():
        from agent.services.onboarding import OnboardingService

        return OnboardingService(ctx.conn, app.version)

    @app.get("/api/onboarding/status")
    async def onboarding_status() -> dict:
        return _onboarding().status().to_dict()

    @app.post("/api/onboarding/seen")
    async def onboarding_seen() -> dict:
        """向导打开即记「本版本已展示过欢迎页」。"""
        return _onboarding().mark_seen().to_dict()

    @app.post("/api/onboarding/profile")
    async def onboarding_profile(body: dict) -> dict:
        try:
            return _onboarding().save_profile(
                name=str(body.get("name", "")),
                intro=str(body.get("intro", "")),
                tags=body.get("tags") or [],
                style=str(body.get("style", "")),
                goals=body.get("goals") or [],
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/api/onboarding/complete")
    async def onboarding_complete() -> dict:
        return _onboarding().complete().to_dict()

    @app.post("/api/onboarding/hint")
    async def onboarding_hint(body: dict) -> dict:
        return _onboarding().set_hint_dismissed(bool(body.get("dismissed", False))).to_dict()
```

（`HTTPException` 已在 `server.py` 顶部导入；若没有则补 `from fastapi import HTTPException`。）

- [ ] **Step 5: 跑测试确认通过**

Run: `python -m pytest tests/test_onboarding_api.py -q`
Expected: PASS（6 passed）

- [ ] **Step 6: 跑后端全量回归**

Run: `python -m pytest tests -q`
Expected: 全绿（既有用例不回归）

- [ ] **Step 7: 提交**

```bash
git add backend/src/agent/services/onboarding.py backend/src/agent/api/server.py backend/tests/test_onboarding_api.py
git commit -m "feat(onboarding): 首次引导状态与落库 API + 版本更新强制展开"
```

---

### Task 2: 前端 API 客户端与 onboarding store

**Files:**
- Modify: `frontend/src/services/api.ts`（在 `api` 对象里追加 5 个方法 + 顶部接口类型）
- Create: `frontend/src/stores/onboarding.ts`
- Test: `frontend/src/stores/__tests__/onboarding.test.ts`

**Interfaces:**
- Consumes: Task 1 的五个端点；`api.ts` 内部 `request` helper。
- Produces:
  - 类型 `OnboardingStatus`、`OnboardingProfilePayload`
  - `api.getOnboardingStatus()`、`api.markOnboardingSeen()`、`api.saveOnboardingProfile(payload)`、`api.completeOnboarding()`、`api.setOnboardingHint(dismissed)`
  - store `useOnboardingStore()`：state `{ status, loaded, saving, error, dismissed }`；getters `showWizard` / `needsSetup` / `hintVisible`；actions `load()`、`markSeen()`、`saveProfile(payload)`、`complete()`、`setHintDismissed(v)`、`closeForSession()`

- [ ] **Step 1: 写失败测试**

创建 `frontend/src/stores/__tests__/onboarding.test.ts`：

```ts
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useOnboardingStore } from "../onboarding";
import { api } from "../../services/api";

const baseStatus = {
  done: false,
  has_credential: false,
  has_name: false,
  wizard_seen: false,
  welcome_version: "",
  app_version: "0.1.6",
  show_wizard: true,
  hint_dismissed: false,
};

describe("onboarding store", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.restoreAllMocks();
  });

  it("load() 拉状态并暴露 showWizard", async () => {
    vi.spyOn(api, "getOnboardingStatus").mockResolvedValue({ ...baseStatus });
    const store = useOnboardingStore();
    await store.load();
    expect(store.showWizard).toBe(true);
    expect(store.needsSetup).toBe(true);
  });

  it("markSeen() 用后端返回覆盖本地状态（版本更新后再关掉）", async () => {
    vi.spyOn(api, "getOnboardingStatus").mockResolvedValue({ ...baseStatus });
    vi.spyOn(api, "markOnboardingSeen").mockResolvedValue({
      ...baseStatus,
      wizard_seen: true,
      welcome_version: "0.1.6",
      show_wizard: false,
    });
    const store = useOnboardingStore();
    await store.load();
    await store.markSeen();
    expect(store.showWizard).toBe(false);
  });

  it("saveProfile() 把 payload 原样交给 API，失败时落下 error", async () => {
    const spy = vi.spyOn(api, "saveOnboardingProfile").mockRejectedValue(new Error("boom"));
    const store = useOnboardingStore();
    await expect(store.saveProfile({ name: "小舟" })).rejects.toThrow("boom");
    expect(spy).toHaveBeenCalledWith({ name: "小舟" });
    expect(store.error).toBeTruthy();
  });

  it("setHintDismissed() 更新提示条状态", async () => {
    vi.spyOn(api, "setOnboardingHint").mockResolvedValue({
      ...baseStatus,
      hint_dismissed: true,
    });
    const store = useOnboardingStore();
    await store.setHintDismissed(true);
    expect(store.status?.hint_dismissed).toBe(true);
  });
});
```

- [ ] **Step 2: 跑测试确认失败**

Run: `npx vitest run src/stores/__tests__/onboarding.test.ts`
Expected: FAIL —— 找不到模块 `../onboarding`

- [ ] **Step 3: 补 API 客户端**

在 `frontend/src/services/api.ts` 的类型区加：

```ts
export interface OnboardingStatus {
  done: boolean;
  has_credential: boolean;
  has_name: boolean;
  wizard_seen: boolean;
  welcome_version: string;
  app_version: string;
  show_wizard: boolean;
  hint_dismissed: boolean;
}

export interface OnboardingProfilePayload {
  name: string;
  intro?: string;
  tags?: { key: string; value: string }[];
  style?: string;
  goals?: string[];
}
```

并在 `export const api = { ... }` 里追加：

```ts
  getOnboardingStatus: () => request<OnboardingStatus>("/api/onboarding/status"),
  markOnboardingSeen: () => request<OnboardingStatus>("/api/onboarding/seen", { method: "POST" }),
  saveOnboardingProfile: (payload: OnboardingProfilePayload) =>
    request<{ name: string; knowledge_id: string; entity_id: string; topics: string[] }>(
      "/api/onboarding/profile",
      { method: "POST", body: JSON.stringify(payload) },
    ),
  completeOnboarding: () => request<OnboardingStatus>("/api/onboarding/complete", { method: "POST" }),
  setOnboardingHint: (dismissed: boolean) =>
    request<OnboardingStatus>("/api/onboarding/hint", {
      method: "POST",
      body: JSON.stringify({ dismissed }),
    }),
```

- [ ] **Step 4: 写 store**

创建 `frontend/src/stores/onboarding.ts`：

```ts
/**
 * 首次引导（欢迎页）状态。
 *
 * `show_wizard` 由后端判定：真正首次启动，或「本版本还没展示过欢迎页」
 * （刚更新到这个版本的用户会被强制展开一次，追加规则 2026-09-23）。
 */
import { defineStore } from "pinia";
import { api, type OnboardingProfilePayload, type OnboardingStatus } from "../services/api";

export const useOnboardingStore = defineStore("onboarding", {
  state: () => ({
    status: null as OnboardingStatus | null,
    loaded: false,
    saving: false,
    error: "",
    /** 本轮会话内用户手动关掉向导后的兜底（后端 seen 已经记过，这里只保证 UI 立刻消失） */
    dismissed: false,
  }),
  getters: {
    showWizard: (state): boolean => !state.dismissed && Boolean(state.status?.show_wizard),
    needsSetup: (state): boolean => !state.status?.done,
    hintVisible: (state): boolean =>
      Boolean(state.status && !state.status.done && !state.status.hint_dismissed),
  },
  actions: {
    async load() {
      try {
        this.status = await api.getOnboardingStatus();
        this.error = "";
      } catch (error) {
        this.error = error instanceof Error ? error.message : String(error);
      } finally {
        this.loaded = true;
      }
    },
    async markSeen() {
      try {
        this.status = await api.markOnboardingSeen();
      } catch (error) {
        this.error = error instanceof Error ? error.message : String(error);
      }
    },
    async saveProfile(payload: OnboardingProfilePayload) {
      this.saving = true;
      try {
        await api.saveOnboardingProfile(payload);
        await this.load();
      } catch (error) {
        this.error = error instanceof Error ? error.message : String(error);
        throw error;
      } finally {
        this.saving = false;
      }
    },
    async complete() {
      this.status = await api.completeOnboarding();
      this.dismissed = true;
    },
    async setHintDismissed(dismissed: boolean) {
      this.status = await api.setOnboardingHint(dismissed);
    },
    closeForSession() {
      this.dismissed = true;
    },
  },
});
```

- [ ] **Step 5: 跑测试确认通过 + 类型检查**

Run: `npx vitest run src/stores/__tests__/onboarding.test.ts` → PASS（4 passed）
Run: `npx vue-tsc --noEmit` → exit 0

- [ ] **Step 6: 提交**

```bash
git add frontend/src/services/api.ts frontend/src/stores/onboarding.ts frontend/src/stores/__tests__/onboarding.test.ts
git commit -m "feat(onboarding): 前端 API 与 onboarding store"
```

---

### Task 3: 六步向导组件 `OnboardingWizard.vue`

**Files:**
- Create: `frontend/src/components/onboarding/OnboardingWizard.vue`
- Create: `frontend/src/components/onboarding/steps.ts`
- Test: `frontend/src/components/onboarding/__tests__/OnboardingWizard.test.ts`

**Interfaces:**
- Consumes: `useOnboardingStore()`（Task 2）、`api.listCredentials()` / `api.createCredential()` / `api.testCredential()`（复用既有凭据能力）、`QInput` / `QSelect`（`frontend/src/components/ui/`）。
- Produces: `OnboardingWizard` 组件，`emit("done")` 通知父级关闭；`steps.ts` 导出 `ONBOARDING_STEPS`、`GOAL_OPTIONS`、`STYLE_OPTIONS`、`TAG_SUGGESTIONS`。

- [ ] **Step 1: 写失败测试**

创建 `frontend/src/components/onboarding/__tests__/OnboardingWizard.test.ts`，至少覆盖：

```text
1) 首屏是「欢迎」步骤：出现品牌与定位，底部有「开始设置」
2) 点「开始设置」进入「连接模型」；点「跳过」进入「认识你」
3) 「认识你」不填称呼点「下一步」→ 停在原地并提示「称呼是完成设置的必填项」
4) 填称呼 + 勾一个目标 → 走到「完成」→ 出现「进入对话」，点击后 emit("done")
5) 步骤进度条有 6 段，当前段带 aria-current="step"
6) 挂载时会调用 store.markSeen()（打开即记「本版本已展示」）
```

测试用 `mount(OnboardingWizard, { global: { plugins: [createPinia()] } })`；先把 store 的 API 依赖 `vi.spyOn(api, ...)` 打好（`getOnboardingStatus` / `markOnboardingSeen` / `saveOnboardingProfile` / `completeOnboarding`）。

- [ ] **Step 2: 跑测试确认失败**

Run: `npx vitest run src/components/onboarding/__tests__/OnboardingWizard.test.ts`
Expected: FAIL —— 找不到组件

- [ ] **Step 3: 写 `steps.ts`**

```ts
export type StepKey = "welcome" | "credential" | "profile" | "preference" | "goal" | "done";

export const ONBOARDING_STEPS: { key: StepKey; title: string }[] = [
  { key: "welcome", title: "欢迎" },
  { key: "credential", title: "连接模型" },
  { key: "profile", title: "认识你" },
  { key: "preference", title: "偏好" },
  { key: "goal", title: "目标" },
  { key: "done", title: "完成" },
];

export const GOAL_OPTIONS = ["学习", "写作", "编程", "生活", "陪聊"];
export const STYLE_OPTIONS = [
  { value: "简洁", label: "简洁" },
  { value: "详细", label: "详细" },
  { value: "俏皮", label: "俏皮" },
];
export const TAG_SUGGESTIONS = ["身份", "职业", "兴趣", "习惯"];
```

- [ ] **Step 4: 写组件**

`OnboardingWizard.vue` 结构要求（样式用 `tokens.css` 变量，全屏覆盖层）：

- 根节点 `.onboarding`（`position: fixed; inset: 0;`，双主题下成立）。
- 顶部 `.onboarding-steps`：6 个 `.step`，当前步 `.step.current` 且带 `aria-current="step"`。
- 中部 `.onboarding-body` 六段：
  1. 欢迎 —— QIO 品牌 + 「你的私人记忆星球」+ 按钮「开始设置」。
  2. 连接模型 —— API Key `QInput` +「保存并测试」；成功 `.ok`、失败 `.err`；可跳过。
  3. 认识你 —— 称呼 `QInput`（必填，空则提示「称呼是完成设置的必填项」并停在原地）+ 一句话介绍 + 标签 chips（`TAG_SUGGESTIONS` 候选，可增删）+ 回答风格 `QSelect`（`STYLE_OPTIONS`）。
  4. 偏好 —— 主题亮/暗，写入 `qio-theme`（复用 `utils/theme.ts`）。
  5. 目标 —— `GOAL_OPTIONS` 多选。
  6. 完成 —— 汇总称呼/目标/主题 + 按钮「进入对话」→ `store.complete()` → `emit("done")`。
- 底部 `.onboarding-actions`：统一「跳过 / 上一步 / 下一步」；第一步不显示「上一步」。
- 组件 `onMounted` 调用 `store.markSeen()`。

- [ ] **Step 5: 跑测试 + 类型检查**

Run: `npx vitest run src/components/onboarding/__tests__/OnboardingWizard.test.ts` → PASS
Run: `npx vue-tsc --noEmit` → exit 0

- [ ] **Step 6: 提交**

```bash
git add frontend/src/components/onboarding
git commit -m "feat(onboarding): 六步欢迎向导组件"
```

---

### Task 4: 挂载 gating、对话页提示条与设置页入口

**Files:**
- Modify: `frontend/src/App.vue`
- Modify: `frontend/src/views/ConversationView.vue`
- Modify: `frontend/src/views/SettingsView.vue`
- Test: `frontend/src/views/__tests__/OnboardingGating.test.ts`

**Interfaces:**
- Consumes: `useOnboardingStore()`、`OnboardingWizard`（Task 3）。
- Produces: `App.vue` 按 `onboarding.showWizard` 挂载向导；对话页 `.setup-hint` 提示条（「继续设置」+ 关闭）；设置页「重新运行设置助手」入口。

- [ ] **Step 1: 写失败测试**

`frontend/src/views/__tests__/OnboardingGating.test.ts` 覆盖：

```text
1) status.show_wizard = true → 渲染出 .onboarding
2) status.show_wizard = false → 不渲染 .onboarding
3) done=true 但 show_wizard=true（刚更新版本）→ 仍然渲染向导
4) done=false 且 hint_dismissed=false → 对话页出现「继续设置」，点 × 调 api.setOnboardingHint(true)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `npx vitest run src/views/__tests__/OnboardingGating.test.ts`
Expected: FAIL

- [ ] **Step 3: `App.vue` 挂载**

```vue
<script setup lang="ts">
import { onMounted } from "vue";
import { useEventStore } from "./stores/events";
import { useUiStore } from "./stores/ui";
import { useOnboardingStore } from "./stores/onboarding";
import OnboardingWizard from "./components/onboarding/OnboardingWizard.vue";

const events = useEventStore();
const ui = useUiStore();
const onboarding = useOnboardingStore();
onMounted(() => {
  events.connect();
  void ui.load();
  void onboarding.load();
});
</script>

<template>
  <div class="app-shell">
    <router-view />
    <ApprovalEntry />
    <ApprovalModal />
    <OnboardingWizard v-if="onboarding.showWizard" @done="onboarding.closeForSession()" />
  </div>
</template>
```

- [ ] **Step 4: 对话页提示条 + 设置页入口**

- `ConversationView.vue`：顶部加 `.setup-hint`（`v-if="onboarding.hintVisible"`），文案「还没设置完 QIO —— 继续设置」，右侧「继续设置」按钮（`@click="onboarding.dismissed = false"`）与关闭「×」（`@click="onboarding.setHintDismissed(true)"`）。
- `SettingsView.vue`：在「偏好」区加一行「重新运行设置助手」按钮，点击后 `onboarding.dismissed = false`。

- [ ] **Step 5: 跑测试 + 类型检查 + 全量前端测试**

Run: `npx vitest run` → 全绿
Run: `npx vue-tsc --noEmit` → exit 0

- [ ] **Step 6: 提交**

```bash
git add frontend/src/App.vue frontend/src/views/ConversationView.vue frontend/src/views/SettingsView.vue frontend/src/views/__tests__/OnboardingGating.test.ts
git commit -m "feat(onboarding): 启动 gating、继续设置提示与设置页入口"
```

---

### Task 5: 预览产物（控制器执行，不派发子代理）

**Files:**
- Create: `scripts/ui-catalog/onboarding-preview.mjs`
- 产物：`frontend/e2e-shots/ui-catalog/onboarding/*.png`（git-ignored）

- [ ] **Step 1:** 用 `scripts/e2e_up.py` 起后端（8734）+ Vite（5199），数据目录用干净的临时目录（`QIO_DATA_DIR`）。
- [ ] **Step 2:** 复用 `scripts/ui-catalog/lib.mjs` 的 `createSession`，逐步骤截图：欢迎 / 连接模型 / 认识你 / 偏好 / 目标 / 完成，另加「done=true + 新版本」强制展开一帧。
- [ ] **Step 3:** 把 6 张 PNG 作为预览交付（附路径），并在最终消息报告验证命令与结果。

## Self-Review

- Spec §1 触发与完成 → Task 1（`show_wizard` / `done` / `has_name` / `has_credential`）+ Task 4 提示条与重跑入口。
- Spec §2 六步 → Task 3。
- Spec §3 落库 → Task 1 Step 3（`_write_profile_knowledge` / `_write_self_card` / `_seed_topics`）。
- Spec §4 接口 → Task 1 Step 4。
- Spec §5 前端 → Task 2 / 3 / 4。
- Spec §6 边界（不做账号 / 导入 / 多语言 / 强制完成）→ 未引入相关任务。
- 追加规则（版本更新强制展开一次）→ Task 1（`WELCOME_VERSION_KEY` + `show_wizard`）、Task 2（`markSeen`）、Task 4（gating 用例 3）。
