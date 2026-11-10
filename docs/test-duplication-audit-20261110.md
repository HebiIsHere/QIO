# 测试重复覆盖审计（2026-11-10）

> 本轮角色 F（CI 去重 / 重复覆盖审计）。审计对象：`.github/workflows/ci.yml` 的后端测试执行、
> `.github/workflows/targeted-verify.yml` 的后端定向入口，以及 `backend/tests/` 下的测试代码。
> **本文件只出清单与建议，不删除、不合并任何测试。** 删减由 Lead 决定后执行。
> 行号是审计时（2026-11-10、改动前）的快照；改完 workflow 后步骤行号会变化，按「job / 步骤名」定位。

## 0. 判定口径（为什么不能按“名字像 / 文件多 / 历史轮次前缀”删）

判「重复」的必要条件是**同一被测提交上，同一入口、同一条件、同一断言集合被执行了不止一次**。
只要下面任何一项不同，就不能合并（即使测试名相同）：

1. 入口不同（`/api/a` vs `/api/b`；工具执行路径 vs store API；源码 worker vs 冻结 exe）；
2. 条件不同（fixture 装配、安全模式、初始化状态、数据来源、迁移基线）；
3. 断言不同（即使重叠，只要一方有另一方没有的失败条件，合并就丢覆盖）；
4. 平台不同（Linux/Windows、有/无 docker 守护进程、POSIX/Windows inode 语义）；
5. 失败路径不同（审批拒绝 vs 超时 vs 取消；断连 vs 正常结束）；
6. 并发/时序条件不同（真实进程清理、共享系统资源、真实 TCP 断连）。

本审计的等价证明只在「同入口 + 同条件 + 断言相等或一方是另一方的真子集」时给出。其余一律**保留**。
另外：`参数化` 不是“去重”，它把 N 个用例合成 1 个参数化用例，**执行量不变**（N 组参数仍然各跑一次），
只是让“只换数据”的用例集中可读；它不能当作「CI 测试变少」的理由。

### 审计方法与可复现命令

- 静态扫描：对 `backend/tests/test_*.py` 用 AST 抽出所有 `test_` 函数，把字符串常量→`S`、数字→`0`、
  去 docstring 后做体哈希，得到「规范化后完全同体」的组；再按同函数名跨文件分组，人工核对入口/fixture/断言。
  本次结果：`test_*.py` 文件 639 个、`test_` 函数 5902 个；跨文件完全同体 8 组、同文件完全同体 28 组、跨文件同名 17 组
  （数字是 2026-11-10 的实测快照，会随代码变化；复核顺序见 §4）。
- 动态证据：CI 内重复执行按「同一 job、同一次运行、同一命令覆盖同一批 node id」判定，
  逐步骤对照 pytest 的 testpaths / `-m` / 参数（`backend/pyproject.toml`：`testpaths = ["tests"]`，
  全量跑没有排除项，所以全量一定包含单独列出的文件）。

## 1. CI 内重复执行清单

### 1.1 已确认的重复（本轮消除）

| # | 步骤（改动前 `ci.yml`） | 覆盖用例 | 重复原因 | 现在保留一次的位置 | 证据文件 |
| --- | --- | --- | --- | --- | --- |
| 1 | `backend` job（Linux，py3.11/3.12）步骤 **Fake worker protocol tests (Linux)**，ci.yml:224-231：`uv run --frozen pytest -q tests/test_sandbox_worker.py tests/test_tool_worker.py` | `tests/test_sandbox_worker.py`（25 个 test 函数）+ `tests/test_tool_worker.py`（14 个），参数化展开后更多 | 下一步 **Run tests**（ci.yml:232-237）`uv run --frozen pytest -q` 无 `-m` 排除、testpaths=tests → 同一批用例在**同一个 job、同一个解释器、同一个工作目录**里再执行一次（100% 重复） | 全量唯一入口 `scripts/run_backend_suite.py`：并行组 `-m "not serial"`、串行组 `-m serial`，两组合集=全部、交集=空，这两个文件只会在其中一组里执行恰好一次 | `backend/junit/pytest-parallel.xml` **或** `backend/junit/pytest-serial.xml`（由分组决定），并作为 artifact 上传 |
| 2 | 同上两个步骤的 JUnit 落盘方式：ci.yml:229 与 ci.yml:236 两步都设 `PYTEST_ADDOPTS: "--junitxml=pytest-results.xml"` | 整个后端测试集 | 同一个文件路径被第二步覆盖：第一步的失败证据在第二步开跑时即丢失（这也是历史注释里“失败时找不到 JUnit”的真实成因之一） | 分阶段 JUnit：`junit/pytest-parallel.xml`、`junit/pytest-serial.xml` 互不覆盖；失败注解按阶段读、artifact `if: always()` 上传 | 两个分阶段文件 + `backend-junit-py{3.11,3.12}` / `backend-junit-windows` artifact |
| 3 | `backend-windows` job 的 **Run tests**（ci.yml:295-298）：`uv run --frozen pytest -q -m "not requires_docker"` | 全量后端（排除 5 个 `requires_docker` 用例；AST 统计：test_container_images / test_dev_tools / test_dev_workflow_integration / test_tool_container_env / test_tool_restore 各 1 个 marker） | **本身不重复**；改走入口后仍只有一次执行，但必须保持 `--marker "not requires_docker"` 与分组叠加（并行组 `not serial and (not requires_docker)`、串行组 `serial and (not requires_docker)`），否则两组并集不再等于「全量 ∩ marker」 | 唯一入口 + `--marker` | `backend/junit/pytest-parallel.xml` / `pytest-serial.xml`（Windows job 自己的 artifact） |

**结论：** Linux backend job 从「同一 job 内跑两遍同一测试集」改为「入口脚本一次分区执行」；
Windows backend job 从「手写 pytest 命令」改为「同一入口 + marker」，两平台仍各自完整覆盖；
分阶段 JUnit 让「并行组失败」与「串行组失败」都留下可下载证据，不再互相覆盖。

**本地以 CI 同款命令做的定向冒烟（2026-11-10，Windows 本机，后端 venv）**：
入口脚本落盘后，用 `--pytest-arg` 把去重涉及的两个文件限定为小样本，跑了 Linux 形状与 Windows 形状各一次
（`--workers 2`、`--junit-dir <tmp>`，第二条再加 `--marker "not requires_docker"`）：

- 并行组：14 个用例（全部来自 `test_tool_worker.py`）通过，JUnit `pytest-parallel.xml`；
- 串行组：25 个用例（全部来自 `test_sandbox_worker.py`，该文件在 conftest 的显式串行清单里）通过，
  `test_tool_worker.py` 的 14 个被 `-m serial` 正确 deselect；JUnit `pytest-serial.xml`；
- 合计 **39 = 25 + 14**，即旧「独立 worker 测试步骤」覆盖的 39 个用例在新入口里各执行**恰好一次**，
  两份 JUnit 分别落盘、互不覆盖（`--marker` 版本同样 39 个，AND 组合未把用例漏掉或重复）。
  这只是小样本冒烟，**不是**全量前后耗时对比（全量由 Lead 推送后唯一一次 CI 与 E 的基准报告承担）。

### 1.2 看起来像重复、实际必须保留的（CI 层面）

| 现象 | 为什么不是重复（保留理由） |
| --- | --- |
| `backend`（py3.11）与 `backend`（py3.12）跑同一套测试 | 版本矩阵：两个 Python 版本各自的失败路径不同，是覆盖不是重复；**CI 矩阵并行**（job 之间/版本之间）与**单套测试内部并行**（xdist 把一组测试分给 4 个 worker）是两件事，前者不能替代后者。 |
| `backend`（Linux）与 `backend-windows` 跑重叠的测试 ID | 平台覆盖（发布平台 Windows、keyring / 系统命令硬超时 / 进程模型只在 Windows 真验证过）；平台不同即条件不同。 |
| Linux 跑 `requires_docker`、Windows 用 `-m "not requires_docker"` 排除 | 同一测试集在两个平台的**合法差异**（windows runner 只有 docker CLI，没有守护进程）。Windows 的排除项由 `--marker` 显式表达，不靠“碰巧没跑”。 |
| `frozen-worker` job 的 `scripts/frozen_worker_smoke.py <exe>` 与 `backend` job 的 `--script src/agent/tool_worker.py` | 被测对象不同：打包后的 onefile 产物 vs 源码；两者都证明「冻结之后仍成立」与「源码里成立」，缺一不可。 |
| `rust` 的 `cargo check` 与 `rust-windows` 的 `cargo test` | 命令与平台都不同：check 是编译检查（Linux 无 GUI 依赖环境），test 只在 Windows 跑壳单测（启动加固、杀子进程）。 |
| `docs` job 的 `check_docs.py` + `--selftest`；`check_third_party.py` + `--selftest`；`release_gate.py --selftest` | 前者是门禁，后者证明门禁**能红**（合成缺陷），不是同一检查跑两遍。 |
| `install-e2e` job 与 `frozen-worker` job 都构建 sidecar | 构建开销重复，但验收对象不同（安装/卸载/注册表/自带运行时 vs 冻结 worker 协议/进程模型）。属于可优化项，不在本轮测试执行去重范围；本轮不动。 |

### 1.3 定向入口（targeted-verify.yml）的二次重复风险（本轮不改）

- `targeted-verify.yml` 的 `backend` job 也有一条固定步骤（改动前 targeted-verify.yml:287-289）：
  `uv run --frozen pytest -q tests/test_sandbox_worker.py tests/test_tool_worker.py`。
  该 workflow 是 `workflow_dispatch` 定向复验入口，**不参与** push/PR 全量矩阵，所以它不是
  「CI 全量矩阵重复跑」；但存在两个二次重复：
  1) 每次 dispatch 都会跑这两个文件，即使本次要复验的失败项与 worker 协议无关；
  2) 若 `pytest_node_ids` 恰好包含这两个文件，同一次 dispatch 内会执行两次。
- 本轮处置：**不改** targeted-verify 的测试命令（定向入口必须保留 node-id/文件级筛选能力，
  且 E 的入口脚本是全量验收入口、不是筛选入口）。仅记录，供 Lead 决定是否后续把这条固定冒烟
  从定向入口移除（移除前要先确认它承担了“dispatch 时也证明 worker 协议没坏”的口径）。

### 1.4 收集类开销（不是重复执行）

- `scripts/test_partition_check.py` 用 3 次 `pytest --collect-only` 机器校验「并行组 ∪ 串行组 = 全量、
  交集 = 空」；新增的 `backend/tests/test_test_infra_*.py` 也会进入全量收集。两者带来的是**收集时间**
  （E 侧口径约 +1 分钟以内），不是把同一批用例再执行一遍 —— 不属本审计的「重复执行」范畴，
  属分区不变量的预期成本。

## 2. 测试代码里的重复项

> 说明：本节列出的「建议合并」都附了等价证明；**没有等价证明的一律保留**。
> 删减动作不在本审计内执行。

### 2.1 可证明等价（建议合并；删减由 Lead 决定）

| # | 原测试 A | 原测试 B | 验证行为与入口（等价证明） | 建议保留 / 替代 | 保留的失败条件 |
| --- | --- | --- | --- | --- | --- |
| 1 | `test_dev_abandon.py:1058 test_dev_tasks_list_is_empty_on_a_fresh_install(client)` | `test_dev_tasks_api.py:59 test_dev_tasks_is_empty_on_a_fresh_install(client)` | **同入口** `GET /api/dev/tasks`；两个文件的 `client` fixture 逐行相同（`create_app(settings, db_conn)` + `MemoryKeyring` + `TestClient`），`_isolated_data_dir` autouse fixture 也相同；**同断言** `== {"tasks": []}` | 保留 B（`test_dev_tasks_api.py` 是该 API 的归属文件，这条与“放弃任务”语义无关）；删除 A 不丢任何断言 | 全新库上 `/api/dev/tasks` 返回非 `{"tasks": []}`（含多出字段/预置任务） |
| 2 | `test_p2_d_migration_append_only.py:87 test_migration_numbers_are_unique_and_increasing()` | `test_p3_d_migrations.py:66 test_migration_numbers_increasing_unique()` | **同入口** `from agent.storage.schema import MIGRATIONS`（两文件 import 行相同）；**同条件**（无 fixture，直接读模块常量）；**同断言** `[int(n) for n, _ in MIGRATIONS] == sorted(set(...))`，仅 f-string 文案不同 | 保留 B，删除 A 的这条函数（A 文件里其余 append-only 断言不动） | 迁移号重复/非严格递增 |
| 3 | `test_r9h11_object_state.py:1381 test_unbound_entry_name_different_is_unrelated(...)` | `test_r9h12_object_state.py:647 test_unbound_entry_name_differs_is_unknown(...)` | **同入口** `decide_object(...)`；**同条件**（同样的 `_make_program`+`PATH`+`_candidate("program_name","other")`）；**同断言** `state is ObjectState.UNKNOWN`、`basis == "unbound_entry_name_differs"`、`fail_closed is True`。A 的额外内容只是 docstring 里的修订说明（旧期望 UNRELATED→UNKNOWN） | 保留 B（r9h12 是当前轮权威文件）；建议把 A 的修订说明搬进 B 的注释或交付文档，再删 A 的这条函数 | 无保存候选且入口名不同时被放行（应 fail-closed UNKNOWN） |
| 4 | `test_streaming_deltas.py:100 test_text_adapter_declares_no_streaming()` | `test_streaming_usage_accounting.py:139 test_text_adapter_is_not_expected_to_stream(db_conn)` | **同入口** `TextAdapter(client=object(), model="m").supports_stream`；**同断言** `is False`；B 的 `db_conn` fixture 未被断言使用（对被测行为无影响） | 保留 A（“text 档不声明流式”属于流式契约文件；B 文件关注用量计量），删除 B 的这条 | text adapter 若声明支持流式 → 前端/loop 会走不存在的流式路径 |
| 5（建议但非强证明） | `test_p2_d_migration_append_only.py:225 test_required_objects_have_compensation_statements()` | `test_p3_d_migrations.py:197 test_required_objects_have_compensation_statements()` | **同入口**（两文件都从 `agent.storage.migrate import REQUIRED_OBJECTS`、`agent.storage.schema import COMPENSATION_VERSION, MIGRATIONS`）；**同断言集合**（columns/indexes/tables 都在补偿迁移里有对应语句）。**差异**：守卫分别要求「迁移 48 已登记」/「迁移 51 已登记」，f-string 文案不同 | 默认**保留两份**；若 Lead 接受「当前补偿版本以 `COMPENSATION_VERSION` 为准（p2 文件自身也断言 `COMPENSATION_VERSION == SCHEMA_VERSION`）」，可保留 B 删 A 的这条 | REQUIRED_OBJECTS 的某项（列/索引/表）在当前补偿迁移里缺少建对象语句 |
| 6（建议但需确认） | `test_attachment_source_refs.py:419 test_delete_reference_keeps_source_file(client, tmp_path)` | `test_attachments_api.py:155 test_delete_reference_keeps_source_file(client, tmp_path)` | 两个 `client` fixture 逐行相同；**同入口** `POST /api/attachments {source_path}` + `DELETE /api/attachments/{id}`；A 的断言是 B 的真子集（removed / deleted_copy=None / kept_source / 原文件仍在 / GET 404），B 多一条「删除不存在的 id → 404」。**差异**：A 的 `_register` 额外带 `source_kind="desktop_path"` | 默认**保留两份**；若 Lead 确认 `source_kind` 对该删除语义无影响，保留 B（断言超集、API 归属文件）并删 A 的这条 | 删除 source 引用时误删用户原文件 / 误删副本 / 记录未移除 |

### 2.2 高重叠但无法证明等价 → 保留

| 原测试 | 差异点（为什么不合并） |
| --- | --- |
| `test_dev_abandon.py` vs `test_verify_dev_abandon.py` 的 3 个同名用例（`test_abandon_endpoint_is_idempotent` 926-938 vs 649-657；`test_abandon_revokes_task_and_long_term_authorizations` 315-331 vs 267-285；`test_file_tools_refuse_an_abandoned_task` 648-665 vs 582-597） | 入口同为 DevWorkspace/HTTP，但**断言不同**：前者多「第二次 abandon 后行快照不变」；后者多「普通授权+长期授权同时授予」「另一个任务的长期授权也被收回」「`read_only` 标志」；文件级 setup 也不同（`_workspace_with_tool` vs `_ws`）。合并会丢失败条件。 |
| `test_audit_stream_role_verify.py:283` vs `test_r4_answer_phase_verify.py:289` `test_cancel_mid_answer_keeps_published_text` | 行为/入口相同（AgentLoop 取消），但**断言与轮询条件不同**：audit 版要求累计文字以已发布内容 `startswith`（更严格）；r4 版接受 `in` 或 `startswith`，轮询条件也不同（任意 interim 正文 vs 特定正文）；turn_id 不同。两个验收轮次的判据不同，保留。 |
| `test_attachment_content_and_background.py:240` vs `test_audit_attachment_io_verify.py:387` `test_upload_without_content_length_is_bounded` | 验的是两个不同性质：前者 413 + 不落行/不落文件（TestClient 分块请求）；后者「服务器不能把 105MB 全读进内存」的有界接收证据（异步真服务器 + COPY_MAX 边界）。断言集合不同，保留。 |
| `test_interactive_migration_integration.py:336` vs `test_irm_b_migration_39.py:415` `test_repeated_start_is_idempotent` | 迁移基线/种子/断言对象不同（前者 seed chat+memory+attachments 并断言表集合；后者针对 migration 39 断言 `board_submissions` 列集合 + 用户内容）。保留。 |
| `test_fs_delete_recycle.py:209` vs `test_r9g_cmd_binding.py:218` `test_restricted_permanent_delete_requires_explicit_confirmation` | 安全条件不同：前者 `FakeGateway()` 默认 ALLOW（模拟模型允许但仍需人工确认）；后者 `FakeGateway(mode="restricted", verdict="allow")` 且多断言审批指纹绑定具体对象身份。保留。 |
| `test_r9f_acceptance_e2e.py:585` vs `test_r9g_acceptance_e2e.py:255` `test_review_unavailable_is_not_silent_allow` | 入口层级不同：前者直接构造 `ModelAuthorizationReviewer` 并断言抛 `ReviewUnavailable`；后者在 app 层用假 reviewer 断言 `Verdict.APPROVE` 且副作用未发生。保留。 |
| `test_p2_a_changes.py:65` vs `test_p2_d_change_counting.py:185` `test_maintenance_origins_are_not_counted` | 入口不同：前者是变化计数记录函数（逐 origin 断言不产生 pending 行）；后者是知识 `revise_atomic` 端到端（maintenance 来源不增加版本行）。保留。 |
| `test_final_answer_fact_check.py:143` vs `test_turn_facts.py:59` `test_accepted_declaration_suppresses_the_note` | 层级不同：前者 AgentLoop 全链路（最终回答文本）；后者 TurnFacts 断言 `annotation() is None`。保留。 |
| `test_attachment_explicit_binding.py:108/125` vs `test_audit_attachment_binding_verify.py:113/175` 的 2 个同名用例 | fixture/客户端不同（`client` vs `app_client`）、装配不同（显式 topic_id + 遗留附件 vs 走 `_make_ready_attachment`）、断言不同（前者断言 turn_note/绑定回溯；后者断言附件最终状态与“用户看到的 == 发送的”）。保留。 |
| `test_p2_d_static_boundaries.py:212` vs `test_p3_d_migrations.py:115` `test_migration_modules_export_frozen_shape` | **入口不同**：前者参数化 `migration_p2_a=46 / migration_p2_b=47`；后者检查 `migration_p3_a=49 / migration_p3_b=50`。名字相同但对象不同。保留。 |
| `test_sandbox_worker.py:277/321` 与 `test_sandbox_executor.py:310/323` 的协议严格化用例 | 入口不同：前者 fake worker 子进程路径；后者容器路径（docker CLI stub）。协议解析实现共用，但**执行路径不同**——删掉任一侧，另一条路径的严格协议就没有证据。保留。 |

### 2.3 名字像但入口/条件不同 → 保留（全部逐一核对过）

| 测试 | 结论 |
| --- | --- |
| 404 三连：`test_tool_record_api.py:120`（`/api/tool-records/...`）、`test_topic_detail_fields.py:128`（`/api/graph/topics/...`）、`test_trace_api.py:30`（`/api/traces/...`） | 规范化后同体（都是「GET 不存在 id 得 404」），但**路由不同**。保留。 |
| `test_onboarding_api.py:97 test_profile_requires_name` vs `test_onboarding_submit.py:132 test_submit_requires_name` | 路由不同（`/api/onboarding/profile` vs `/api/onboarding/submit`）。保留。 |
| `test_settings_search_api.py:57 test_put_rejects_invalid_int` vs `test_settings_ui_api.py:41 test_ui_put_rejects_non_int` | 路由不同（`/api/settings/search` vs `/api/settings/ui`）。保留。 |
| `test_api_routes.py` 的 credential test 无 secret vs `test_verify_dev_abandon.py` 的 abandon 404 | 路由不同（凭据测试接口 vs 放弃任务接口）。保留。 |
| `test_r9f_origin_fs_tools.py:177` vs `test_r9f_origin_store.py:105` `test_external_replacement_does_not_inherit_generated` | 入口不同：前者 FsWriteTool 执行效果 + 写路径身份重核对；后者 store 记录/查找 API。保留。 |

## 3. 「只换数据」用例 → 参数化建议（★参数化不减少执行量）

下面这些是**同文件内规范化后完全同体**的用例：只差输入数据或期望值。它们不是重复执行，
合并成 `pytest.mark.parametrize` 的收益是可读性与防漏项，**不是** CI 时间（参数化后仍逐个参数执行）。
本审计不建议在本轮（测试提速轮）做这种改写，避免与 E 的 conftest/并行改造冲突；列出来供后续维护预算安排。

| 文件 | 用例 | 参数化空间 |
| --- | --- | --- |
| `test_text_adapter_health.py`（5 个） | `test_plain_text_answer_is_success` / `test_empty_answer_is_success` / `test_valid_tool_json_is_success` / `test_malformed_protocol_attempt_is_failure` / `test_broken_json_fence_is_failure` | `(chunk, expected_success_rate)` |
| `test_import_order.py`（3 个） | `test_tools_can_be_imported_before_core` / `test_registry_can_be_imported_first` / `test_core_can_still_be_imported_first` | `(module_to_import)`；每个参数仍单独起子进程 |
| `test_computer_sandbox.py`（2+3 个） | `test_classify_high_commands` / `test_classify_fail_closed_unknown`；`test_check_path_*` 三条 | `(command, expected_risk)`、`(path, expected_verdict)` |
| `test_entities_recognizer.py`（3 个） | `test_preference_binding` / `test_kinship_binding` / `test_place_binding` | `(sentence, expected_fragment)` |
| `test_tool_lifecycle.py`（2 个） | `test_approval_service_flow` / `test_approval_service_reject` | `(decision, expected)`：两段代码只差 decision 字符串与断言值 |
| `test_r9h11_object_state.py`（3 个） | `test_binding_entry_name_same_object_same_matches` / `..._different_alias_resolves_bound` / `test_binding_program_kind_bare_name_uses_entry_branch` | `(entry, candidate, expected_state, expected_basis)` |
| `test_sr_a_computer.py`（多组 2-3 个） | cat/grep/escape token 分类用例 | `(command, expected_effect)` |
| `test_trace_redact.py`（2 个）/ `test_sr_verify_redaction.py`（2 个） | bearer/QIO_KEY 等脱敏用例 | `(text, leaked_fragment)`（**脱敏断言不能只合并成一条**，每个输入仍要单独断言不泄漏） |
| `test_tool_router.py`（2 个） | `test_route_ranks_by_similarity` / `test_route_no_embedding_uses_token_overlap` | `(query, must_rank_before)` |
| `test_web_fetch_tool.py`、`test_entity_extract.py`、`test_derivation_recovery.py`、`test_settings_ui_api.py`、`test_tool_feedback.py`、`test_tool_worker.py`、`test_sandbox_worker.py` 等各 2 个 | 同体不同数据 | 同上原则：只换数据的参数化；**不同失败路径/不同入口的不要硬合并**（例如 worker 的“非 bool ok”与“worker 在读请求前退出”是两种失败，参数化只能合并 setup，断言要按 case 区分） |

## 4. 验证与证据

本轮对 workflow 的验证（F 执行；CI 真跑只能在 Lead 推送后）：

```powershell
# 1) YAML 可解析 + 契约断言（含触发/SHA 锁定/checkout ref/矩阵/排除项/去重入口/分阶段 JUnit）
cd D:\\qio-dev\\qio
python scripts/targeted-verify/verify_workflow_contract.py
# 2) 断言真能红（--selftest 用合成缺陷逐条证明）
python scripts/targeted-verify/verify_workflow_contract.py --selftest
# 3) SHA 判定自检（复用既有入口，不新增脚本）
python scripts/targeted-verify/verify_sha.py --selftest
```

- 复用 E 的入口：`scripts/run_backend_suite.py --workers 4 --junit-dir junit [--marker "not requires_docker"]`；
  并行组 `-m "not serial and (<marker>)"`、串行组 `-m "serial and (<marker>)"`，两组并集=「全量 ∩ marker」、交集=空
  （由 E 的 `scripts/test_partition_check.py` 机器校验，不在本文件重复实现）。
- 复核本审计的静态扫描（只读，不改测试）：在 `backend` 下用 `.venv\\Scripts\\python.exe` 跑一段
  「字符串→S、数字→0、去 docstring 后做函数体哈希」的 AST 脚本；同哈希且跨文件的组就是候选，需人工核对入口/断言。

## 5. 未验证项 / 接缝

1. **CI 只能在推送后真跑**：本地不触发 CI、不推分支。改动的 workflow 语法/结构由 PyYAML 解析与
   契约脚本断言验证；真 runner 上的行为（Windows PowerShell 步骤、`actions/upload-artifact`、
   artifact 名称冲突、4 worker 下的实际耗时）只能在 Lead 推送集成分支后验证。
2. **E 的入口脚本**：CI 命令按契约 §7 与 E 确认的接口写（`--workers/--junit-dir/--marker`、
   `pytest-parallel.xml`/`pytest-serial.xml`、两阶段都跑完再退出）。若 E 最终实现偏离，
   需要同步改 `ci.yml` 与 `verify_workflow_contract.py` 的常量（`CI_BACKEND_*`），二者是唯二口径。
3. **SETUP.md**：CI 入口步骤用多行 `run: |` 块，避免 `scripts/check_docs.py` 的「单行 run: 命令必须
   在 SETUP.md 原文可查」检查把入口命令变成精确文本耦合；SETUP §8 的入口说明由 E 提供、Lead 落盘
   （本审计不改 SETUP.md）。同时 SETUP 里旧的「fake worker 协议测试单独跑」说明需要随 CI 步骤删除而更新。
4. **审计中的合并建议只是建议**：§2.1 的 4 条强证明 + 2 条建议都未执行；Lead 决定后再删，删前建议
   至少跑一次被删文件与保留文件的定向 pytest。
5. **测试代码重复的扫描口径是“规范化后完全同体”**：语义重复但实现不同的用例（§2.2）不会被自动发现，
   本审计只对给出证据的条目下结论；未列入的用例按「保留」处理，不代表已证明不重复。
