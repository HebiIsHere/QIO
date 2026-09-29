# 凭据 Key 泄漏与摘要数量上限 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 编辑已有凭据改服务地址时，不再把原 Key 提前发到用户尚未确认的新地址；摘要里实体/关键词超过上限时，本地去重截断后继续生成摘要，而不是整条失败。

**Architecture:** 前端 `useCredentialForm.refreshModels` 只在「当前地址仍是这条凭据自己的地址」时才用 `keyId` 问模型列表；后端 `/api/credentials/models` 增加同样的约束作为纵深防御（地址不一致就不解密、不外发已存 Key）。摘要侧把数量上限从「校验失败」改成「本地去重 + 截断」，并把上限写进提示词。

**Tech Stack:** Python 3.12 / FastAPI / pydantic / pytest；Vue 3 + vitest + @vue/test-utils。

**Spec:** 无独立设计文档；需求来自评审报告「严重 bug」两条（2026-09-29 评审，基线 `abce846`）。

## Global Constraints

- 密钥原文不得入库、不得进日志；本次改动只减少外发面，不新增任何密钥落盘路径。
- 「保存与验证分离」「改地址必须重填 Key 并显式确认」两条既有契约不变。
- 摘要失败仍不能拖垮对话（片段保持「已封存、无摘要」）；本次只是让「数量超限」不再算失败。
- 不写会过期的硬编码数字到文档；文案一律中文。
- 工作树是多路并行状态：只改本计划列出的文件，不碰 `backend/src/agent/eval/*`、`backend/evals/*`。
- 收尾验证：后端 `uv run --frozen pytest`、前端 `npx vue-tsc --noEmit` + `npm test`、`python scripts/check_docs.py`。

---

### Task 1: 前端 · 改地址后不再用已存的 Key 取模型列表

**Files:**
- Modify: `frontend/src/services/credentials.ts`（`refreshModels`）
- Test: `frontend/src/components/credentials/__tests__/CredentialForm.test.ts`

**Interfaces:**
- Consumes: 既有 `targetChanged`（比较 `initial.endpoint`/`baselineKind` 与当前表单值）
- Produces: 无新导出；行为契约 = 「`mode !== "create"` 且当前地址与这条凭据落库地址不一致时，`api.listCredentialModels` 不带 `keyId`」

- [ ] **Step 1: 写失败用例** —— 编辑模式改地址后等过防抖，断言 `api.listCredentialModels` 一次都没被调用；再补一条「地址没变时仍然用 keyId 取列表」。
- [ ] **Step 2: 跑测试确认失败**：`npx vitest run src/components/credentials/__tests__/CredentialForm.test.ts -t "不拿已存的 Key"`
- [ ] **Step 3: 最小实现**：`refreshModels` 里把「要不要带 keyId」改成「当前地址是否仍是这条凭据自己的地址」。
- [ ] **Step 4: 跑测试确认通过**（同一条命令；再跑整个文件）。

### Task 2: 后端 · `/api/credentials/models` 拒绝把已存 Key 发到别的地址

**Files:**
- Modify: `backend/src/agent/api/server.py`（`list_credential_models`）
- Test: `backend/tests/test_credential_endpoint_api.py`

**Interfaces:**
- Consumes: `ctx.credentials.get_metadata(key_id)`（含 `endpoint`）
- Produces: 行为契约 = 「`key_id` 的落库地址与请求 `endpoint` 不一致时不解密、不调用 `list_models`，返回空列表 + 说明」

- [ ] **Step 1: 写失败用例** —— 用 spy 顶替 `agent.services.verify.list_models`，请求 `key_id=k1&endpoint=<别的地址>`，断言 spy 没被调用且不返回模型；再补一条「地址一致时照旧用已存 Key」。
- [ ] **Step 2: 跑测试确认失败**：`uv run --frozen pytest tests/test_credential_endpoint_api.py -q`
- [ ] **Step 3: 最小实现**：地址（去尾斜杠、小写）不一致就直接返回「需要重新填写 Key」的空结果。
- [ ] **Step 4: 跑测试确认通过**。

### Task 3: 摘要 · 数量超限本地去重截断，不再整条失败

**Files:**
- Modify: `backend/src/agent/memory/summary.py`（`FragmentSummary` 校验 + 三个提示词）
- Test: `backend/tests/test_extraction.py`

**Interfaces:**
- Consumes: 无
- Produces: `validate_summary_text` 对超限的 `entities`/`keywords` 返回**已去重截断**的摘要（不再返回 schema 错误）；新增模块级常量 `MAX_ENTITIES`/`MAX_KEYWORDS`。

- [ ] **Step 1: 写失败用例** —— 79 个实体的模型输出应得到 `summary`（不是 `None`），且 `len(entities) == 50`；重复项应被去重。
- [ ] **Step 2: 跑测试确认失败**：`uv run --frozen pytest tests/test_extraction.py -q`
- [ ] **Step 3: 最小实现**：解析后先规范化再构造 `FragmentSummary`；提示词写明数量上限。
- [ ] **Step 4: 跑测试确认通过**。
