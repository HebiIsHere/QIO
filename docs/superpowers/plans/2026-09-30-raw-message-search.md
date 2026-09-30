# 已保存对话的原文检索 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让「用户说过的话」都能被检索回来并读到原文 —— 包括还没封存、没有摘要的片段。

**Architecture:** 不再新建索引。`memory_search` 在原有「摘要记忆」通道之外补一条**原文通道**：直接读 `messages` 表（按当前行作答，删除/修改天然一致），用与 BM25 同一套 CJK 1/2-gram 分词做匹配，`topic_id` 是真正的过滤。两条通道的结果在工具层合并并去重：同一个片段已由摘要命中，就不再重复列一次原文。

**Tech Stack:** Python 3.11 + SQLite（`messages` / `fragments` / `nodes` 既有表，无新增迁移）；pytest（`db_conn` fixture）。

**Spec:** `docs/superpowers/specs/2026-09-29-tool-dev-spec-phase1.md`（第一阶段「所有保存对话可搜索并读取命中原文」）

## Global Constraints

- 不新增依赖、不新增数据库迁移（这条路径只读既有表）。
- 检索是只读的：不改变当前对话位置、不写任何记录。
- 找不到就说没找到；任何情况下都不编造记忆。
- 中文必须能查：用既有的 `selector/tokenize.py`（CJK 1-gram + 2-gram），不引入分词器。

---

## File Structure

- `backend/src/agent/services/retrieval.py` — 新增 `MessageHit` 与 `Retriever.search_messages()`（唯一的原文检索实现）。
- `backend/src/agent/tools/memory_search.py` — 合并两条通道、去重、标清来源、`topic_id` 真过滤。
- `backend/src/agent/prompts.py` — 工具描述写清「有原文通道」「topic_id 是限定不是偏向」。
- `backend/tests/test_message_search.py` — 覆盖开放片段、真过滤、删除一致、去重、来源标注、没有就是没有。

## Task 1: 原文检索服务

**Files:**

- Modify: `backend/src/agent/services/retrieval.py`
- Test: `backend/tests/test_message_search.py`

**Interfaces:**

- Produces: `Retriever.search_messages(query: str, *, topic_id: str | None = None, top_k: int = 3) -> list[MessageHit]`；
  `MessageHit = { message_id, fragment_id, topic_id, topic_name, role, created_at, content, score, truncated }`。

- [ ] **Step 1: 写失败测试**（开放片段能搜到并读回原文 / topic_id 是真过滤 / 删除后不再命中）
- [ ] **Step 2: 跑测试确认失败**：`cd backend; .venv\Scripts\python.exe -m pytest tests/test_message_search.py -q` → FAIL（`AttributeError: search_messages`）
- [ ] **Step 3: 实现**（LIKE 预筛 2 字以上词元 → 覆盖率打分 → 时间倒序稳定排序）
- [ ] **Step 4: 跑测试确认通过**
- [ ] **Step 5: 提交**

## Task 2: 工具层合并两条通道

**Files:**

- Modify: `backend/src/agent/tools/memory_search.py`
- Modify: `backend/src/agent/prompts.py`
- Test: `backend/tests/test_message_search.py`

**Interfaces:**

- Consumes: Task 1 的 `search_messages()`。
- Produces: 工具输出里多一段 `（以下来自**保存的对话原文**…）` + `[原文N] Fragment/Topic/Time/Text`；
  同一片段已由摘要命中则不再重复；`topic_id` 同时过滤摘要命中（实体卡除外）。

- [ ] **Step 1: 写失败测试**（来源标注 / 去重 / 真的没有时说没找到）
- [ ] **Step 2: 跑测试确认失败**：断言「原文」这一节不存在 → FAIL
- [ ] **Step 3: 实现**
- [ ] **Step 4: 跑相关测试**：上面的文件 + `tests/test_services.py` 等用到 `memory_search` 的用例
- [ ] **Step 5: 提交**
