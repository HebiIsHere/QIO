# R9-H3 C 报告：正常内部文件替换后身份自动更新

> 分支：`r9h3/c-identity`（worktree `D:/qio-dev/qio-r9h3-c`，基线 `8d97904f`）
> 契约：`docs/r9h3-contracts.md` §4（冻结）。只做定向测试；完整 CI 预算已用尽（2/2），本分支不触发。
> 结论：真实可信写入入口已接入身份自动更新并通过定向测试；普通工具/模型没有自报身份入口。

## 1. 内部文件生命周期调查（先调查后接线）

| 对象 | 真实写入方式 | 身份是否变化 | 处理 |
| --- | --- | --- | --- |
| `data_dir/app.db`（SQLite） | `storage.db.connect` 首次创建；正常运行 WAL 原地写；设置/记忆/审批/日志都写该库 | 原地写不变；首次创建/外部还原替换会变 | 启动 sync 登记；设置保存提交成功后立即 refresh；外部替换在下次可信保存/启动时纠正 |
| `data_dir/config.toml` | 当前 Python 代码只读（`Settings.config_path` 仅登记，无写入器） | 外部创建/替换时变 | 启动 sync 覆盖；无正常写入点可接 |
| 凭据 | `credential_store`：SQLite 元数据 + OS keyring 密钥；不落 `data_dir/credentials` 文件 | 不产生新文件身份 | DB 文件由设置保存/启动路径覆盖；无独立文件入口 |
| `data_dir/logs` | 后端只 console 日志（`main.py basicConfig`）；Python 无文件 handler/轮转；launcher（Rust）可能在进程外写日志 | 进程外变化 | 启动 sync 覆盖重启后身份；进程内 Python 无可信写入点（见边界） |
| `data_dir/instances` | `InstanceRegistry` 写 SQLite 表 | 同 app.db | 同 DB 路径 |
| `data_dir/cache`、`data_dir/tmp` | `TempFileManager`/媒体工作目录创建并清理临时文件；不原子替换长期对象 | 临时对象 | 启动 sync 登记目录；非长期保护对象，不额外扫描 |
| 内部数据直接文件维护 | 用户批准后的真实执行器 `fs.write/fs.patch/fs.delete`（`approved_operations`）**实际原子替换**内部文件 | 会变 | **本轮接入点**：执行器成功提交后立即更新身份 |

结论：Python 正常运行时**真正会原子替换受保护内部文件**的可信写入入口是"审批通过后的真实文件执行器"；设置保存是另一条真实可信写库入口（文件身份通常不变，但覆盖外部替换后的纠正）。两者都已接线，不是无人调用的 helper。

## 2. 改动文件

| 文件 | 改动 |
| --- | --- |
| `backend/src/agent/security/internal_data_identity.py` | 新增 `note_committed_path`（提交成功后读真实身份、同路径旧身份 retired(replaced)、`previous_identity` 可显式退休旧 key、身份读不到返回明确错误且保留旧登记）、`note_committed_paths`（有界，默认最多 50 个明确对象）、`retire_path`（保留历史行）。`refresh_path/sync` 保留。 |
| `backend/src/agent/services/app.py` | `AppContext` 在注册完审批执行器后安装 `_install_trusted_write_identity_hook()`：包装真实 `operations.execute`，仅当执行**成功提交**后按 `fs.write/fs.patch` payload 的明确目标路径更新身份；`tool.invoke/proc.*` 等没有可核对对象不猜。新增 `note_trusted_internal_commit(paths, source)` 供其它可信写入入口使用（只允许真实注册范围/历史登记路径，防止普通文件借入口扩大保护）；失败写入 `internal_data_identity_error`、记录 warning、并在 ExecResult.detail 写 `internal_identity_error`（真实文件写入已成功，不把成功改报失败）。 |
| `backend/src/agent/services/settings_service.py` | 设置保存（先校验→单事务提交→运行时应用）成功后调用 `ctx.note_trusted_internal_commit([db_path], source="settings_service.apply")`；校验/提交失败不调用。 |
| `backend/tests/test_r9h3_identity_refresh.py`（新增） | 真实 AppContext + 真实 sqlite 文件库（DB 位于数据目录内，启动 sync 登记）+ 临时数据目录；通过真实审批执行器 `ctx.operations.execute("fs.write", ...)` 与真实 `SettingsService.apply` 验证。 |
| `docs/r9h3-reports/C.md` | 本报告。 |

## 3. 行为（契约 §4 对照）

1. **提交成功后立即更新**：审批执行器返回 `ok=True` 后，按实际 payload 目标调用 `note_committed_path`；旧身份 `active=0, retired_reason=replaced`，新身份 `active=1`。
2. **原子替换即可见**：测试在写入后**不调用** `refresh_internal_data_identities`、不重启，直接给新对象建硬链接；`internal_data.is_internal_data_object` 与真实 `AuthorizationEngine` 均立即按内部数据处理（读取 DENY、写入 APPROVE+三次确认）。
3. **新建/重建**：对不存在的内部路径经真实执行器创建后，新身份立即登记；同路径再次替换按原子替换处理。
4. **失败不静默降级**：身份更新失败（返回失败或抛异常）时真实文件操作保持成功，但 AppContext 记录 `internal_data_identity_error`、日志 warning、结果 detail 带 `internal_identity_error`；已有登记不清空，路径判定兜底。`note_committed_path` 自身在身份读不到时返回 `identity_unavailable` 且保留旧 active 登记。
5. **有界、只对变更对象**：只更新 payload 明确目标（最多 50）；不做全盘扫描/新增沙箱；启动 sync 与显式 refresh 保留但正常保护不依赖。
6. **可信与普通工具区分**：只有 AppContext 包装的真实执行器与显式可信入口（设置保存）调用 store；普通工具/模型没有传入身份键或自报变更的接口；`note_trusted_internal_commit` 还会按注册范围/历史登记过滤，普通文件传进来也不会被登记。
7. **旧对象生命周期**：原子替换旧身份行保留（active=0, replaced）；旧对象若仍被硬链接引用，历史行保留但不再作为当前受保护身份（不扫描引用关系，见边界）。
8. **不误伤**：同内容复制件、普通同名 `.env` 等不因内容/名称被登记或保护。

## 4. 测试命令与结果（真实运行，Python 3.11 venv）

依赖：`cd backend; uv sync --frozen --extra dev`。

    uv run --frozen pytest tests/test_r9h3_identity_refresh.py \
      tests/test_approved_operations.py tests/test_r9f_approved_replay.py \
      tests/test_rm_e_settings_service.py tests/test_settings.py \
      tests/test_app_integration.py tests/test_app_lifecycle.py \
      tests/test_approval_persistence.py tests/test_r9h2_internal_identity.py \
      tests/test_internal_data_guard.py tests/test_r9h_internal_data.py \
      tests/test_sandbox_readonly.py tests/test_computer_sandbox.py -q
    → EXIT=0（仅既有 DeprecationWarning，无 FAILED/ERROR）

新增测试覆盖：
* 真实审批执行器原子替换后（不刷新/不重启）新对象硬链接立即被引擎按内部数据 DENY（读）/APPROVE 三次确认（写），旧身份 retired/replaced；
* 真实执行器新建内部文件、原路径/相对/规范化/硬链接/改名移动/符号链接（不支持时硬链接+规范化别名，不 skip）判定一致；
* 真实设置保存提交成功后触发身份通知（source=settings_service.apply、DB 路径）；校验失败不触发；DB 原地写身份幂等仍 active；
* 身份更新失败明确返回/记录（detail.internal_identity_error + AppContext error），旧登记保留；
* 旧对象仍被引用时历史行保留；普通复制件/同名材料不误登记；
* `_trusted_commit_paths` 只认 fs.write/fs.patch、删除批量最多 50；工具执行不猜；`note_committed_path` 不接受外部 identity 自报。

## 5. Windows/POSIX 能力与证据

* 本轮定向测试在 Windows 运行：硬链接真实 `os.link` 成功；符号链接创建失败（WinError 1314 客户端没有所需特权），测试走硬链接+相对/规范化别名分支且**不 skip**，能力边界如实记录。
* POSIX 未在本机运行：实现只依赖既有 `services/file_identity.py`（POSIX st_dev+st_ino）；符号链接/硬链接机制由该模块与 Lead/D 的 Linux 定向 CI 验证，本报告不声称已在 POSIX 实机验证。

## 6. 契约偏差 / 未完成 / 未验证

1. **凭据保存未单独接线**：调查确认凭据元数据写同一 SQLite，密钥走 OS keyring，不产生 `data_dir` 新文件；DB 文件身份由启动 sync 与设置保存可信入口覆盖，因此没有为每个凭据方法单独挂通知。若后续凭据改为独立文件存储，需要在该真实写入入口补调用。
2. **进程外日志轮转**：Python 后端无文件日志 handler；launcher（Rust）可能在进程外写/轮转 `data_dir/logs`，Python 无法感知进程外变化；启动 sync 会在下次重启登记，进程内不轮询/不扫描（不新增全盘扫描）。
3. **旧对象仍被硬链接引用**：原子替换按契约退休旧身份（保留历史行），但不扫描全盘引用关系判断"旧对象是否仍可达"；旧内容若仍被硬链接引用，不会被 identity 保护（路径仍在注册范围时仍按路径保护）。这是有界更新的边界，未声称全盘对象追踪。
4. **真实 turn/审批恢复 E2E**：本轮验证真实执行器与真实设置服务的提交后更新；完整"用户消息→要求绑定→引擎→审批→执行"端到端由 Lead 定向验收覆盖。
5. 未触发完整 CI / 全量矩阵（累计预算 2/2 已用尽）；未改历史迁移、engine.py、review.py；未 push/merge。