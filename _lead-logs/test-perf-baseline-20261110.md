# 后端测试性能基线（2026-11-10，E/task-5）

> 目的：在改动测试基础设施**之前**固定同机小样本基线；改造后在同一台机器、同一并发条件下重跑同一小样本对比。
> **不推算全量倍数**：本文件只有局部测量。历史全量记录（4640 tests / 1116s，见 `docs/qio-verification-ledger.md`）只作为量级参照，无同机前后完整数据，不作为倍数依据。

## 0. 环境（改造前）

- 机器：Windows 本机，16 逻辑核 / 31.5GB RAM；`NUMBER_OF_PROCESSORS=16`
- 解释器：`D:\qio-dev\qio\backend\.venv\Scripts\python.exe` = CPython 3.12.10
- pytest 9.1.1 / pytest-asyncio 1.4.0；**未安装 pytest-xdist**；SQLite 3.49.1
- 工作区：`D:\qio-dev\qio`（分支 `feat/topic-application-20261110`），backend 工作目录
- 日志级别：默认（未设 log_cli / 未改日志配置）
- 并发注记：测量时其它角色可能在跑各自的定向测试（同一工作区并行开发），因此数值有波动；同一文件内的前后对比才是有效对比。
- 当前 schema：`SCHEMA_VERSION = 76`，`MIGRATIONS` 71 条（`python -c "from agent.storage.schema import ..."`）

## 1. 固定小样本（5 类代表用例）

| 类别 | nodeid |
| --- | --- |
| 普通 db_conn 业务 | `tests/test_storage.py::test_entity_cards_table_and_flexible_edge_types` |
| 真实迁移 | `tests/test_migration_13_bindings.py::test_upgrade_from_v12_adds_tables_and_keeps_old_rows` |
| 应用接口 | `tests/test_api_auth.py::test_post_turn_returns_turn_id_immediately` |
| 异步等待 | `tests/test_events.py::test_event_bus_does_not_replay_for_new_subscribers` |
| 子进程边界 | `tests/test_sandbox_worker.py::test_sandbox_runs_tool_through_worker` |

命令（backend 目录，PowerShell，与项目约定一致）：

```powershell
cd D:\qio-dev\qio\backend
$nodeids = @(
  "tests/test_storage.py::test_entity_cards_table_and_flexible_edge_types",
  "tests/test_migration_13_bindings.py::test_upgrade_from_v12_adds_tables_and_keeps_old_rows",
  "tests/test_api_auth.py::test_post_turn_returns_turn_id_immediately",
  "tests/test_events.py::test_event_bus_does_not_replay_for_new_subscribers",
  "tests/test_sandbox_worker.py::test_sandbox_runs_tool_through_worker"
)
& .\.venv\Scripts\python.exe -m pytest @nodeids -q --durations=20
```

### 1.1 结果（改造前）

| 轮次 | 开始 UTC | 总耗时 | 退出码 | 结果 |
| --- | --- | --- | --- | --- |
| 冷（本次会话首次调用） | 2026-11-10T03:57:38Z（本地 11:57:38） | **5862 ms** | 0 | 5 passed |
| 热（紧接着再跑一次） | 2026-11-10T03:57:50Z | **5375 ms** | 0 | 5 passed |

逐项（--durations=20，冷/热）：

| 项 | 冷 | 热 | 说明 |
| --- | --- | --- | --- |
| `test_api_auth.py::test_post_turn_returns_turn_id_immediately` setup | 1.17s | 0.95s | create_app + 全量迁移 + TestClient 启动 |
| `test_migration_13_bindings.py::test_upgrade_from_v12_adds_tables_and_keeps_old_rows` call | 0.45s | 0.52s | 自建 v12 库后真实 apply_migrations |
| `test_storage.py::test_entity_cards_table_and_flexible_edge_types` setup | 0.34s | 0.36s | db_conn：connect + apply_migrations |
| `test_events.py::test_event_bus_does_not_replay_for_new_subscribers` call | 0.21s | 0.23s | 用例自身等待 0.2s（asyncio.timeout） |
| `test_sandbox_worker.py::test_sandbox_runs_tool_through_worker` call | 0.13s | 0.11s | 真子进程 worker |
| 其余 call/teardown | <0.06s | <0.06s | — |

关键观察：小样本里 **db_conn setup（0.34–0.36s）≈ 一次完整迁移重放的成本**；应用接口用例的 1.17s setup 里也含一次迁移 + 应用装配。

## 2. 迁移重放成本（直接测量，不推算）

命令（backend，venv 解释器，5 个全新临时库逐次 connect + apply_migrations）：

```powershell
cd D:\qio-dev\qio\backend
$env:PYTHONPATH = "src"
& .\.venv\Scripts\python.exe -c "<逐次新建 tmp 库并计时>"
```

结果：**332.7 / 359.3 / 318.2 / 331.5 / 328.2 ms，均值 ≈ 334 ms / 每个全新数据库**（含 connect 的 WAL 设置与全部 71 步迁移、schema_version=76）。这正是改动前 `db_conn` 每用例的固定开销。

## 3. 模板库 + sqlite3 backup 克隆原型（同机预研）

原型：先 `connect(模板)` + `apply_migrations` + `PRAGMA wal_checkpoint(TRUNCATE)` 生成空白模板，再从只读模板连接用 `sqlite3.Connection.backup()` 克隆到独立文件，最后用生产 `connect()` 建连接。结果：

| 项 | 数值 |
| --- | --- |
| 模板构建（含真实迁移） | 349.2 ms（每 worker 一次） |
| 迁移后 WAL 大小 → checkpoint 后 | 1,981,752 B WAL → 0，主库 974,848 B |
| 克隆（备份目标 `synchronous=OFF` + `connect()`） | 最小 4.37 / 中位 **5.28** / 最大 8.51 ms |
| 克隆（仅 backup，默认同步） | 最小 13.57 / 中位 16.01 / 最大 19.61 ms |
| 克隆（只读模板连接 backup + connect） | 最小 16.55 / 中位 17.98 / 最大 21.18 ms |
| 克隆后连接 PRAGMA 核对 | foreign_keys=1、journal_mode=wal、busy_timeout=5000、schema_version=76 ✅ |

即 **每用例 DB 准备从 ~334ms 降到个位数毫秒**（本机实测中位 ~5.3ms，含生产连接设置），并且克隆出来的库结构与当前迁移完全一致（版本号 76）。

## 4. 诚实边界

- 本文件没有任何「全量前后对比」；全量由 Lead 唯一执行。
- 克隆原型数据是在并行开发负载下测的，只用于选型；最终「改造后」数据见 §5（同机、同命令、同并发条件重跑）。
- 异步等待用例的 0.2s 是用例语义（等待窗口）本身，不会被基础设施优化掉；子进程边界用例的 ~0.1s 是真子进程，也不能靠模板库优化。
- 历史台账的全量数字（4640 tests / 1116s）来自另一日期的记录，只说明量级，不用于倍数推算。

## 5. 改造后复测（同一台机、同一命令、同一并发口径）

> 全量前后对比 **NOT RUN**（按契约由 Lead 唯一执行）；本文件只报告局部测量，不推算全量倍数。

### 5.1 同一条小样本命令（5 nodeids，单进程）

| 轮次 | 总耗时 | 退出码 |
| --- | --- | --- |
| 改造前 · 冷 | 5862 ms | 0 |
| 改造前 · 热 | 5375 ms | 0 |
| 改造后 · #1 | **4793 ms** | 0 |
| 改造后 · #2 | **4781 ms** | 0 |

逐项（改造后）：storage setup 从 0.34–0.36s 变成本次进程首条 db_conn（付一次模板构建 ~0.35s）；api 用例 setup 0.79s（应用装配为主）。
诚实结论：这 5 条样本里只有 2 条 db_conn 用例，模板构建成本（每进程一次）抵消了一部分收益，样本约 -11%~-18%；**样本太小，不代表全量**。

### 5.2 受控 A/B（同一文件、同一命令，唯一变量 = 是否走真实迁移）

用 `QIO_TEST_REAL_MIGRATIONS=1` 强制全部 db_conn 走真实迁移（=改造前等价行为），与默认模板克隆对比：

| 定向文件 | 真实迁移（改造前等价） | 模板克隆（改造后） | 变化 |
| --- | --- | --- | --- |
| tests/test_turn_journal_facts.py（9 条 db_conn 用例） | 4895 ms | **2586 ms** | -47.2% |
| tests/test_turn_journal.py + tests/test_storage.py + tests/test_events.py（27 条） | 17570 ms | **13038 ms** | -25.8% |

后一组剩余的耗时来自用例自身的 app 装配 / HTTP 客户端 / 用例内等待（异步等待 0.2s 等），不是 DB 准备。
单次 DB 准备：真实迁移重放 **~334 ms**（§2）→ 模板克隆中位 **5.27 ms**（§3）。

### 5.3 并行组单独数据（小样本，只说明开销，不外推）

同样 4 条非 serial 样本（去掉被标 serial 的 sandbox 用例）：

| 方式 | 总耗时 |
| --- | --- |
| 单进程 | 4711 ms |
| `-n 4` | 6122 ms |

结论：4 条小样本下 xdist 启动/分发开销（约 1.4s）大于并行收益；并行收益只有在全量上才体现。**全量耗时由 Lead 唯一执行后以其记录为准**。

### 5.4 worker 配置与线程上限

- backend dev extras 增加 `pytest-xdist>=3.6`；`backend/uv.lock` 锁定 **pytest-xdist 3.8.0 + execnet 2.1.2**（与 pytest 9.1.1 实装验证通过；requires-python >=3.11 满足）。
- 入口默认/CI 参数：`--workers 4`（本机 16 逻辑核建议 4；机器更小用 2；**禁止 auto**）。
- `backend/tests/conftest.py` 顶部（任何 numpy / onnxruntime / tokenizers 导入之前）用 setdefault 设置 OMP/OPENBLAS/MKL/NUMEXPR/VECLIB 线程上限=1；只影响测试进程，生产不加载本文件，可用同名环境变量覆盖。

### 5.5 串行组清单（113 条；显式 marker serial + 逐条原因）

注册位置：`backend/tests/conftest.py` 的 SERIAL_MODULES（文件级）/ SERIAL_NODEIDS（用例级）。由 pytest_collection_modifyitems 在收集期落成 pytest.mark.serial（不改各测试文件本身）。

| 文件 / 用例 | 条数 | 标 serial 的原因 |
| --- | --- | --- |
| tests/test_sandbox_worker.py | 25 | 真实 worker 子进程的超时/取消进程树清理协议 |
| tests/test_sandbox_executor.py | 23 | 容器/受限子进程路径的进程与输出保护（真实子进程） |
| tests/test_tool_isolation.py | 21 | 真实 Job Object / 句柄关闭的进程树清理断言 |
| tests/test_interactive_during_heavy_work.py | 19 | 固定端口 8731/8732 真 uvicorn + 严格事件循环停顿阈值 + 真实 ONNX 模型 |
| tests/test_main_closeout_process_group_scope.py | 11 | POSIX 进程组清理语义（Linux 按进程组收割；Windows skip） |
| tests/test_sr_b_process_cleanup.py | 4 | 真实 Windows taskkill 进程树清理 + 同名诱饵存活断言 |
| tests/test_acc_a_f21_scheduling.py | 4 | 严格时序：慢读取 0.3s 注入后断言事件循环停摆 < 0.25s |
| tests/test_sr_verify_process.py | 3 | 真实进程树超时/取消收割 + tasklist 取证 + 固定宽窗口 |
| tests/test_dev_workflow_integration.py::test_full_dev_workflow_registers_tool | 1 | requires_docker：真实容器执行，共享 docker 守护进程资源 |
| tests/test_dev_tools.py::test_dev_run_tests_pass_and_fail | 1 | requires_docker：真实容器执行，共享 docker 守护进程资源 |
| tests/test_container_images.py::test_real_docker_lists_and_removes_only_orphan_images | 1 | requires_docker：真实 docker 镜像构建/删除，共享 docker 守护进程资源 |

注意：**同 xdist_group 不等于独占整机**；serial 只表示该用例不能与并行组同时跑，串行阶段内多条 serial 用例仍顺序共用一台机器。标记只加在真正需要独占的用例上，不用来掩盖失败。

### 5.6 real_migrations 清单（逐次对独立空库真实迁移；只加 marker/import，不改断言）

19 条领域用例（14 个文件）；另有 1 条基础设施自测（tests/test_test_infra_template_db.py::test_marked_case_gets_a_real_migration_connection）也带该标记以验证标记路径生效：

| 文件 | 用例 / 说明 |
| --- | --- |
| tests/test_storage.py | test_migrations_apply_and_idempotent、test_all_tables_present（迁移幂等/当前结构） |
| tests/test_migrations_append_only_verify.py | test_attachments_table_is_created_by_an_appended_migration（只追加迁移建表） |
| tests/test_p1_b_migration_and_storage.py | test_migration_44_is_append_only_and_idempotent、test_migration_44_preserves_existing_rows |
| tests/test_p2_b_migration_store.py | test_migration_apply_is_idempotent、test_migration_does_not_write_schema_version |
| tests/test_p3_d_migrations.py | test_migration_50_provides_intent_versions_and_operation_ledger |
| tests/test_p3_d_intent_resume_retry.py | test_migration_69_adds_display_retry_and_intent_turn_tables |
| tests/test_fu2_acceptance_notifications.py | test_migration_71_and_compensation_72_create_table |
| tests/test_conv030_tool_failure_task_feed.py | test_tool_records_gains_structured_fact_columns（迁移追加列） |
| tests/test_turn_journal_facts.py | test_migration_adds_fact_columns_and_keeps_legacy_rows_readable、test_upgrade_path_from_pre_fact_schema_keeps_existing_rows |
| tests/test_settings_migration_models.py | test_fresh_database_has_model_catalog_shape（DB 初始化的当前结构） |
| tests/test_db_identity.py | test_instance_endpoint_reports_database_integrity（DB 身份/首次初始化） |
| tests/test_r9h2_state_migration.py | test_migration_corrects_only_evidenced_failure_records（迁移 61 数据纠正） |
| tests/test_r9h3_state_migration.py | test_revoke_only_targeted_unrestricted_review_records（迁移 63 数据纠正） |
| tests/test_rm_a_r04_persistence.py | test_accepted_write_failure_raises_journal_write_error、test_accept_write_failure_leaves_no_partial_row（持久性失败语义） |

其他既有迁移/升级/损坏自愈类测试本来就直接 `connect(tmp_path/...)` + `apply_migrations`（不走 db_conn），因此**不需要**改动，仍逐次真实迁移。

### 5.7 分区与入口实测（12:2x）

- `python scripts/test_partition_check.py`（collect-only，3 次收集，19.6s）：
  全量 selected = 6392 / 并行组 6279 / 串行组 113 / 并集 6392 / 交集 0 / 漏收 0 / 多收 0 → **RESULT OK**。
  （用例总数随并行开发实时变化；这里记录的是当次运行值，不是承诺数字。）
- `python scripts/run_backend_suite.py --workers 2 --junit-dir <tmp> --pytest-arg tests/test_storage.py --pytest-arg tests/test_acc_a_f21_scheduling.py`：
  - 阶段一（parallel）：rc=0，4.1s，tests=7，junit=pytest-parallel.xml
  - 阶段二（serial）：rc=0，3.1s，tests=4，junit=pytest-serial.xml
  - 汇总：stages=2 tests=11 failed=0 → RESULT OK，exit 0
- 迁移相关定向测试（11 文件 98 条，含全部 real_migrations 用例）：exit 0（29.8s）。
- 基础设施自测（tests/test_test_infra_template_db.py / test_test_infra_partition.py / test_test_infra_runner.py，共 12 条）：exit 0。

## 6. 改造后仍未做 / 已知边界

- 全量前后对比 NOT RUN（Lead 唯一执行）；本文件不给倍数推算。
- 第三方声明：本机 uv 缓存损坏，用独立 UV_CACHE_DIR 完成 uv lock；离线生成声明会缺 285 个元数据（rust crates + numpy py<3.12 条目），已用 `build_third_party.py --refresh` 联网补齐并重新生成；`check_third_party.py` 通过（锁文件组件 916 个 / 覆盖 920 条 / 0 UNKNOWN），`--selftest` 7/7；`licenses/overrides.json` 的 fetched 元数据随生成物更新。
- 模板库收益取决于 db_conn 用例占比；小样本/单条用例会被每进程一次的模板构建成本抵消（诚实记录）。

