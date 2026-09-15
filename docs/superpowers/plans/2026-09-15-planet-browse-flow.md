# 星球浏览景观与话题导航 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 QIO 星球从「固定坐标 + 前 16 个话题」改造成「数据层无上限、视觉层限量、随旋转流动」的话题浏览景观，并把 Topic 导航（Anchor / 选中 / 引用 / 待确认切换）收口到唯一入口。

**Architecture:** 后端提供三层数据接口（Planet Overview / Topic Detail / Fragment Raw）与一个确定性的「浏览序列 + 游标」接口；前端新增 `PlanetBrowseSession`（序列、游标、展示窗口、冷却、反向恢复）与 `PlanetLayout`（临时球面槽位），Three.js 只负责把窗口画出来，并用固定数量的渲染对象池承载不断进出的话题。

**Tech Stack:** Python 3.12 + FastAPI + SQLite（后端）、Vue 3 + TypeScript + Three.js + Vitest（前端）。

**Spec:** 用户粘贴的《QIO 第二阶段开发任务：话题逻辑与流动星球导航》。

## Global Constraints

- 保持第一阶段已经建立的正确性与安全机制：所有新增 API 走既有会话认证中间件，不得新增无认证接口。
- 不新增 Topic 关系网络、不做语义坐标 / 降维、不做程序化构筑物、不做永久空间记忆。
- 不动已有数据：`nodes.meta.layout`（旧球面坐标）、`fragments`、`messages`、`knowledge`、`entities` 一律保留；新增字段只能通过新迁移追加。
- 前端文案一律中文；星球同屏话题数量有明确上限（`VISIBLE_CAPACITY = 12`，且 ≤ `MAX_TOPICS = 16`，后者是融合环 shader 的 uniform 数组上限）。
- 动效原则：惯性，不是弹性。
- 文档同步：改完实现必须更新 `docs/status.md` 与 `docs/architecture.md`，并保持 `python scripts/check_docs.py` 通过。

---

## 文件结构

**后端（新增）**

- `backend/src/agent/services/planet.py` — Planet 数据层：Overview 聚合 + 浏览序列 / 游标。不返回 Message 原文。
- `backend/src/agent/services/navigation.py` — TopicNavigationService：进入话题 / 创建话题 / 待确认切换 / 从历史继续 的唯一入口。
- `backend/src/agent/storage/schema.py` — 迁移 11：`fragments.source_fragment_id`。

**后端（修改）**

- `backend/src/agent/api/server.py` — 新增 `/api/planet/overview`、`/api/planet/browse`、`/api/fragments/{id}/messages`、`/api/topic-switch/{confirm,reject}`；`/api/anchor` 改为走 NavigationService；`/api/graph/topics/{id}` 不再内联 Message 原文。
- `backend/src/agent/api/events.py` — 新增 `TOPIC_SWITCH_SUGGESTED` 事件类型。
- `backend/src/agent/graph/anchors.py` — `focus_fragment` 识别「接续片段 → 聚焦来源片段」。
- `backend/src/agent/services/app.py` — 装配 `PlanetBrowseService` / `TopicNavigationService`；复用 `_publish_anchor_event`。
- `backend/src/agent/services/turn_orchestrator.py` — 明确切换直接执行、推测切换只产生待确认；失败与取消不推进 Anchor。
- `backend/src/agent/tools/topic_tools.py` / `tools/continue_tool.py` — 统一走 NavigationService。

**前端（新增）**

- `frontend/src/planet/browseSession.ts` — 浏览会话：序列、游标、展示窗口、槽位回收、反向恢复、冷却、选中锁定。
- `frontend/src/planet/layoutSlots.ts` — 临时球面槽位（确定性抖动）+「背面槽位」排序。
- `frontend/src/components/TopicSwitchPrompt.vue` — 低干扰的「转到「X」？」确认条。

**前端（修改）**

- `frontend/src/composables/usePlanetScene.ts` — 渲染对象池（固定容量）、按窗口更新、暴露回收回调与背面槽位。
- `frontend/src/views/PlanetView.vue` — 用 Overview + Browse 取代「全量 positions + 前 16 个」；搜索命中注入窗口；按钮文案区分「进入这个话题」与「从这里继续」。
- `frontend/src/views/ConversationView.vue` + `stores/session.ts` + `services/events.ts` + `services/api.ts` — 待确认切换状态、确认/拒绝。

**文档**

- `docs/architecture.md`、`docs/status.md`、`docs/release-planet-phase2.md`。

---

### Task 1: 星球数据层（Overview + 浏览序列 / 游标）

**Files:**
- Create: `backend/src/agent/services/planet.py`
- Test: `backend/tests/test_planet_browse.py`

**Interfaces:**
- Consumes: 已建好的 sqlite 连接（`conn.row_factory = sqlite3.Row`）、`nodes` / `fragments` 表。
- Produces:
  - `VISIBLE_CAPACITY: int = 12`
  - `@dataclass(frozen=True) PlanetTopic(topic_id, title, fragment_count, last_activity, summary_preview, visual_seed)`
  - `PlanetBrowseService.overview() -> list[PlanetTopic]`
  - `PlanetBrowseService.browse(cursor, direction, count, exclude, current_topic_id, seed) -> dict`
  - cursor 字符串格式：`"{seed}.{pass_index}.{index}"`

- [ ] **Step 1: 写失败测试**（`backend/tests/test_planet_browse.py`）：概览不读原文、同 seed 确定性、分页不重复、反向拿回上一屏、首批含当前话题、跨圈会换顺序且避开近期展示。
- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend; uv run --frozen python -m pytest tests/test_planet_browse.py -q`
Expected: FAIL（`ModuleNotFoundError: agent.services.planet`）

- [ ] **Step 3: 实现 `planet.py`**

要点：

1. `overview()` 用**一条聚合 SQL**（`nodes LEFT JOIN fragments GROUP BY`）+ 一条「每话题最近摘要」查询，绝不读 `messages`。
2. `visual_seed_of(topic_id)` = blake2b(topic_id) 取 4 字节整数（本阶段只提供，不消费）。
3. `_sequence(seed, topics, current_topic_id)`：`order_value = _unit_hash(seed, topic_id) - 0.15 * recency_rank`，`recency_rank ∈ [0,1]` 由 `last_activity` 排序得出；`pass_index == 0` 时当前话题额外减 0.5，保证它进第一批。
4. `browse()`：解析 cursor → `(seed, pass_index, index)`；`forward` 取 `sequence[index:index+count]`，`backward` 取 `sequence[max(0, index-count):index]`；`prev_cursor` / `next_cursor` 都按窗口边界写回（反向浏览因此能拿回上一屏）。
5. 越界时 `pass_index += 1`、新一圈 `seed = seed + pass_index`，响应里 `pass_changed: true`；`exclude`（近期展示）在新一圈排到序列尾部。
6. 返回结构固定：`{seed, pass_index, cursor, prev_cursor, next_cursor, has_more, pass_changed, total, visible_capacity, items}`。

- [ ] **Step 4: 跑测试确认通过** → `uv run --frozen python -m pytest tests/test_planet_browse.py -q`
- [ ] **Step 5: 提交**

```bash
git add backend/src/agent/services/planet.py backend/tests/test_planet_browse.py
git commit -m "feat(planet): lightweight overview + deterministic browse cursor"
```

---

### Task 2: 三层数据接口（Planet Overview / Topic Detail / Fragment Raw）

**Files:**
- Modify: `backend/src/agent/api/server.py`
- Test: `backend/tests/test_planet_api_layers.py`

**Interfaces:**
- Consumes: Task 1 的 `PlanetBrowseService`。
- Produces:
  - `GET /api/planet/overview` → `{topics, total, visible_capacity}`
  - `POST /api/planet/browse` → Task 1 `browse()` 的返回
  - `GET /api/graph/topics/{topic_id}` → 片段**不再含** `messages`，`message_count` 是真实总数（不再被 `LIMIT 50` 截断）
  - `GET /api/fragments/{fragment_id}/messages?offset=0&limit=50` → `{fragment_id, messages, total, offset, limit}`

- [ ] **Step 1: 写失败测试**：概览无原文、详情不内联原文、原文按需分页。
- [ ] **Step 2: 跑测试确认失败** → `uv run --frozen python -m pytest tests/test_planet_api_layers.py -q`
- [ ] **Step 3: 改 `server.py`**：详情片段改成聚合计数查询；新增三个路由（参数校验：`direction ∈ {forward, backward}`，`count` 夹到 `1..32`，`limit` 夹到 `1..200`）。认证沿用既有中间件，不加例外。
- [ ] **Step 4: 跑测试确认通过** → `uv run --frozen python -m pytest tests/test_planet_api_layers.py tests/test_api_routes.py -q`
- [ ] **Step 5: 提交** `git commit -m "feat(api): planet overview / browse / fragment raw layering"`

---

### Task 3: 统一 Topic 导航入口（Anchor 只能有一个写入者）

**Files:**
- Create: `backend/src/agent/services/navigation.py`
- Modify: `backend/src/agent/api/server.py`、`services/app.py`、`storage/schema.py`、`tools/topic_tools.py`、`tools/continue_tool.py`
- Test: `backend/tests/test_topic_navigation.py`

**Interfaces:**
- Produces:
  - `@dataclass NavigationResult(topic_id, fragment_id, fragment_title, historic, created_fragment_id, source_fragment_id)`
  - `await TopicNavigationService.enter_topic(topic_id, *, fragment_id=None) -> NavigationResult`
  - `await TopicNavigationService.continue_from_history(topic_id, fragment_id) -> NavigationResult`
  - `await TopicNavigationService.create_topic(name, *, description="") -> NavigationResult`
  - `request_switch(topic_id, *, reason="") -> dict` / `await confirm_switch()` / `reject_switch()` / `pending_switch()`

- [ ] **Step 1: 写失败测试**：进入话题写入 Anchor；预测与检索不写 Anchor；从历史继续创建新片段并保存来源、旧片段零改动；待确认切换在确认前不动 Anchor、拒绝后保持、确认后才切。
- [ ] **Step 2: 跑测试确认失败** → `uv run --frozen python -m pytest tests/test_topic_navigation.py -q`
- [ ] **Step 3: 加迁移 11（`ALTER TABLE fragments ADD COLUMN source_fragment_id TEXT`）并实现 `navigation.py`**
  - `enter_topic`：话题必须存在 → 片段必须属于该话题 → `AnchorService.set_active` → `_publish_anchor_event`。
  - `continue_from_history`：来源片段必须已封块 → 新建**开放**片段（`source_fragment_id = 来源`）→ Anchor 指向新片段；旧片段只读。
  - `request_switch` 只写 `anchor_type = 'pending'` 行，不触碰 active；`confirm_switch` 才 `confirm_pending()` 并广播 ANCHOR。
- [ ] **Step 4: `/api/anchor` 与工具层改走 NavigationService**（body 增加 `continue_from_history`）；`grep -n "set_active" backend/src` 只应剩 `graph/anchors.py` 自身与 `services/navigation.py`。
- [ ] **Step 5: 跑测试确认通过** → `uv run --frozen python -m pytest tests/test_topic_navigation.py tests/test_anchor_lifecycle.py tests/test_continue_fragment.py tests/test_topic_tools.py -q`
- [ ] **Step 6: 提交** `git commit -m "feat(nav): single writer for anchor, continuation fragments"`

---

### Task 4: 明确切换 / 推测切换 / 失败取消不推进 Anchor

**Files:**
- Modify: `backend/src/agent/services/navigation.py`、`services/turn_orchestrator.py`、`api/events.py`、`api/server.py`
- Test: `backend/tests/test_topic_switch_policy.py`、`backend/tests/test_anchor_no_advance.py`

**Interfaces:**
- Produces:
  - `detect_explicit_navigation(message: str, titles: list[str]) -> str | None`
  - 事件 `TOPIC_SWITCH_SUGGESTED`：`{from_topic_id, topic_id, topic_name, reason}`
  - `POST /api/topic-switch/confirm` → `{ok, topic_id}`；`POST /api/topic-switch/reject` → `{ok}`

- [ ] **Step 1: 写失败测试**：明确切换直接生效且无 pending；推测切换只发事件、Anchor 不变；确认后 = 目标、拒绝后 = 原话题；turn 失败 / 取消不改变 Anchor、片段封块状态与历史行数量。
- [ ] **Step 2: 跑测试确认失败** → `uv run --frozen python -m pytest tests/test_topic_switch_policy.py -q`
- [ ] **Step 3: 实现**：`detect_explicit_navigation` 用「切换动词（切到/切换到/回到/转到/继续之前的）+ 已知话题名」匹配；命中即 `enter_topic`；否则 `prediction.suggested_switch` 为真时 `request_switch` 并发事件。Predictor 保持只读；`advance_anchor` 只在成功路径调用。
- [ ] **Step 4: 跑测试确认通过** → `uv run --frozen python -m pytest tests/test_topic_switch_policy.py tests/test_anchor_no_advance.py tests/test_turn_lifecycle_protocol.py -q`
- [ ] **Step 5: 提交** `git commit -m "feat(topic): explicit switch direct, inferred switch needs confirmation"`

---

### Task 5: 前端浏览会话（序列 / 游标 / 展示窗口 / 冷却 / 反向）

**Files:**
- Create: `frontend/src/planet/browseSession.ts`
- Test: `frontend/src/planet/__tests__/browseSession.test.ts`

**Interfaces:**
- Produces:
  - `VISIBLE_CAPACITY = 12`、`interface BrowseTopic`
  - `class PlanetBrowseSession`：
    - `setSequence(items: BrowseTopic[], opts?: { append?: boolean }): void`
    - `windowSlots(): (BrowseTopic | null)[]`（长度固定 = capacity）
    - `takeSwap(direction: 1 | -1, backSlots: number[], nowMs: number): { slot: number; topic: BrowseTopic | null } | null`
    - `pinTopic(topic: BrowseTopic): number`
    - `lock(topicId: string | null): void`
    - `needsMore(): boolean`

- [ ] **Step 1: 写失败测试**：同屏不超过容量；前进一次只换一个槽位、其余话题位置不变；短距离反向把刚离开的话题放回原槽位；被选中的话题不被回收；冷却窗口内不立刻重复出现；未满 `minLifetimeMs` 不回收；搜索命中注入窗口不挤掉起点话题。
- [ ] **Step 2: 跑测试确认失败** → `cd frontend; npx vitest run src/planet/__tests__/browseSession.test.ts`
- [ ] **Step 3: 实现浏览会话**：内部状态 `queue / head / slots / exitStack / recent / locked / lastSwapAt`；`takeSwap(+1)` 从队列取下一个（跳过冷却中的，塞回队尾）并写进 `backSlots[0]`，被顶替的话题压入 `exitStack`；`takeSwap(-1)` 弹出 `exitStack` 还原。
- [ ] **Step 4: 跑测试确认通过**
- [ ] **Step 5: 提交** `git commit -m "feat(planet): browse session window with cooldown and reverse restore"`

---

### Task 6: 临时球面槽位（PlanetLayout）

**Files:**
- Create: `frontend/src/planet/layoutSlots.ts`
- Test: `frontend/src/planet/__tests__/layoutSlots.test.ts`

**Interfaces:**
- Produces:
  - `slotPositions(capacity: number, sessionSeed: number): THREE.Vector3[]`（单位球面、避开极区、最小角间距 ≥ 0.18 rad）
  - `backSlotOrder(dirs: THREE.Vector3[], camDir: THREE.Vector3): number[]`

- [ ] **Step 1: 写失败测试**：不重叠、不集中极区、同 seed 完全一致、不同 seed 有差别、背面排序把最背的排最前。
- [ ] **Step 2: 跑测试确认失败**
- [ ] **Step 3: 实现**：环形带 `polar ∈ [55°, 125°]`，`azimuth = 2πi/capacity + jitter(seed, i)`，jitter 用确定性哈希且幅度 < 半个槽距。
- [ ] **Step 4: 跑测试确认通过**
- [ ] **Step 5: 提交** `git commit -m "feat(planet): deterministic temporary sphere layout slots"`

---

### Task 7: Three.js 渲染对象池与窗口驱动

**Files:**
- Modify: `frontend/src/composables/usePlanetScene.ts`
- Create: `frontend/src/planet/dotPool.ts`（可测的纯逻辑部分）
- Test: `frontend/src/planet/__tests__/dotPool.test.ts`

- [ ] **Step 1: 写失败测试**：池大小固定 = 容量；`applyWindow` 复用同一批对象（不 new / 不 dispose）；离开窗口的话题标记 `visible = false` 而不是移除。
- [ ] **Step 2: 跑测试确认失败**
- [ ] **Step 3: 实现**：`loadTopics()` → `setWindow(items)`；`capacity` 个 mesh 只创建一次、共享材质；每帧算背面槽位顺序回调给浏览会话；回收只改 `userData.topicId` / 位置 / 标题；标签 DOM 只保留 hover 与选中两个。
- [ ] **Step 4: 跑测试 + `npx vue-tsc --noEmit`**
- [ ] **Step 5: 提交** `git commit -m "perf(planet): fixed render pool, window-driven dots"`

---

### Task 8: PlanetView 接线（浏览流 / 搜索注入 / 进入话题 ≠ 选中）

**Files:**
- Modify: `frontend/src/views/PlanetView.vue`、`frontend/src/services/api.ts`
- Test: `frontend/src/views/__tests__/PlanetView.test.ts`（扩展既有文件）

- [ ] **Step 1: 写失败测试**：选中话题不调用 `setAnchor`；「进入这个话题」才调用；累计旋转到阈值后 `browse` 被调用且窗口话题改变；搜索命中不在窗口的话题时它进入窗口并被聚焦。
- [ ] **Step 2: 跑测试确认失败** → `npx vitest run src/views/__tests__/PlanetView.test.ts`
- [ ] **Step 3: 实现**：`loadData` 改调 `api.planetOverview()` + `api.planetBrowse()`；位置由场景提供；文案区分「进入这个话题」（未选片段）与「从这里继续（历史位置）」。
- [ ] **Step 4: 跑测试确认通过** + `npx vue-tsc --noEmit`
- [ ] **Step 5: 提交** `git commit -m "feat(planet): flowing browse window in PlanetView"`

---

### Task 9: 待确认切换的前端 UI

**Files:**
- Create: `frontend/src/components/TopicSwitchPrompt.vue`
- Modify: `frontend/src/views/ConversationView.vue`、`stores/session.ts`、`services/events.ts`、`services/api.ts`
- Test: `frontend/src/components/__tests__/TopicSwitchPrompt.test.ts`、`src/stores/__tests__/session.test.ts`

- [ ] **Step 1: 写失败测试**：收到 `TOPIC_SWITCH_SUGGESTED` 出现「转到「X」？」；「保留当前」调 reject 且不切；「转到这里」调 confirm 且 Anchor 更新；不使用模态弹窗与 `window.confirm`。
- [ ] **Step 2: 跑测试确认失败**
- [ ] **Step 3: 实现**：`session.pendingSwitch` 状态 + 两个动作；组件放输入区上方，细边无遮罩、`aria-live="polite"`。
- [ ] **Step 4: 跑测试确认通过**
- [ ] **Step 5: 提交** `git commit -m "feat(ui): low-noise pending topic switch prompt"`

---

### Task 10: 文档同步与验收报告

**Files:**
- Modify: `docs/status.md`、`docs/architecture.md`
- Create: `docs/release-planet-phase2.md`

- [ ] **Step 1: 更新 `architecture.md`**：Planet = 浏览景观；Topic Position = 当前展示布局；Anchor = 当前真实对话位置；Selected Topic = 当前正在浏览的话题；三段式数据接口。
- [ ] **Step 2: 更新 `status.md`**：M8 / M11 增加第二阶段实现与已知限制，同步「尚未完成」。
- [ ] **Step 3: 跑 `python scripts/check_docs.py`** → 期望 0 失败
- [ ] **Step 4: 全量验证**：`cd backend; uv run --frozen pytest -q`；`cd frontend; npx vue-tsc --noEmit; npm test`
- [ ] **Step 5: 写验收报告**：逐条对照 spec 第 90 节勾选；视觉验证与 Tauri 打包未跑就如实写「未运行」。
- [ ] **Step 6: 提交** `git commit -m "docs: planet browse landscape architecture and phase-2 report"`

---

## Self-Review

**1. Spec coverage**

| spec 段落 | 落在哪个 Task |
| --- | --- |
| 一~五（产品定义、可见容量、解除 16 上限、旋转 = 推动话题流） | Task 1（数据层无上限）、Task 5（窗口限量）、Task 8（接线） |
| 六~七（流动窗口、替换不可感知） | Task 5 + Task 7（只在背面回收） |
| 八（拖动与滚动职责） | Task 7 + Task 8 |
| 九~十三（浏览序列、Planet 会话、排序与新鲜度） | Task 1 + Task 5 |
| 十四~十七（无永久坐标、临时布局、确定性抖动） | Task 6（`nodes.meta.layout` 保留不删） |
| 十八~二十（信息克制、标签、点不做图表） | Task 7（标签只有 hover / 选中）+ Task 8 |
| 二十一~二十三（三种 Topic 状态分离、点击 = 查看） | Task 8 + 既有实现 |
| 二十四（搜索后让 Planet 展示它） | Task 5 `pinTopic` + Task 8 |
| 二十五~三十二（Reference / Anchor / Predictor / 待确认 / 统一入口） | Task 3 + Task 4 |
| 三十三~三十五（Fragment 不可改、从历史继续、分支来源） | Task 3 |
| 三十六~三十九（失败取消不推进、消息归属、检索不等于导航） | Task 4 |
| 四十~四十五（三层接口、分页、下一批接口） | Task 2 + Task 1 |
| 四十六~五十二（重复抑制、最小寿命、选中锁定） | Task 5 |
| 五十三~五十四（进入话题、从片段继续） | Task 3 + Task 8 |
| 五十五~五十九（不做关系线 / 语义坐标 / 永久记忆 / 构筑物 / visual_seed） | Global Constraints；`visual_seed` 在 Task 1 提供但不消费 |
| 六十~六十一（身份一致、列表直达） | Task 2（同一 `topic_id`）+ 既有列表 |
| 六十二~六十四（性能、对象池、标签数量） | Task 7 |
| 六十五~七十八（测试项） | Task 1~9 的测试步骤 + Task 10 全量验证 |
| 七十九（真实视觉测试） | Task 10（如实标注「未运行」或执行结果） |
| 八十~八十三（动效） | Task 5（最小寿命与反向恢复）+ Task 7 |
| 八十四~八十五（代码结构、模块划分） | 「文件结构」一节 |
| 八十六~八十八（不破坏第一阶段、数据兼容、文档同步） | Global Constraints + Task 10 |
| 八十九~九十（报告与验收） | Task 10 |

**2. Placeholder scan**：无 TBD /「稍后实现」；每个 Task 都给了测试项、命令与关键实现规则。

**3. Type consistency**：`PlanetTopic`（Python）与 `BrowseTopic`（TS）字段一一对应（`topic_id / title / fragment_count / last_activity / summary_preview / visual_seed`）；游标字符串 `{seed}.{pass}.{index}` 在 Task 1 定义、Task 2 透传、Task 5 只当不透明字符串处理。
