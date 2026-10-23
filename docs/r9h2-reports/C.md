# R9-H2 C 报告：内部数据真实对象身份保护

> 分支：`r9h2/c-identity`（worktree `D:/qio-dev/qio-r9h2-c`，基线 `a6732f4c`）
> 契约：`docs/r9h2-contracts.md` §5（冻结）。只做定向测试，不跑全量/完整 CI。
> 结论：C 独占范围内已实现并通过定向测试；engine/app 接线由 B/Lead（本分支未改）。

## 1. 改动文件

| 文件 | 改动 |
| --- | --- |
| `backend/src/agent/security/internal_data_identity.py`（新增） | `InternalDataIdentityStore(conn)`：`sync/active_keys/known_paths/lookup/refresh_path/snapshot`；只信任应用维护路径；身份以 volume:file_id 为准（复用 file_identity）；目录有界枚举；原子替换退休旧身份；缺失/读失败保留登记 |
| `backend/src/agent/security/internal_data.py` | 新增 `is_internal_data_object(...)`；`effect_targets_internal_data` 扩展 `identity_keys/known_paths/identity_of`；`requires_three_confirmations/confirmations_for` 透传；旧签名完全兼容；不引入名称/后缀/关键词 |
| `backend/src/agent/services/file_identity.py` | 新增统一 helper `identity_key_of(path)`（读不到返回 None，不猜） |
| `backend/src/agent/tools/security_gate.py` | 新增 `path_identity_key(path)` 与 `with_target_identity(effect)`：只补真实 identity，不做权限判定；已有 identity 不覆盖 |
| `backend/tests/test_r9h2_internal_identity.py`（新增） | 真实 sqlite 文件 DB + 迁移 61 表 + 临时目录：登记/相对规范化/硬链接/符号链接/改名移动/普通同名/同内容复制件/原子替换退休/身份读失败/重启恢复/effect target identity/模式行为 |
| `backend/tests/test_internal_data_guard.py` | 增加对象身份参数兼容性 + known_paths 保守命中测试；原有三次确认/取消/全模式语义保留 |
| `backend/tests/test_r9h_internal_data.py` | 修正旧预期「移出注册范围自动普通」：移出路径范围后，真实对象身份登记仍保护同一对象（R9-H2 §5） |

## 2. 身份判定规则（契约 §5.1）

`is_internal_data_object(path, protected, *, identity_key, identity_keys, known_paths, identity_of)`：
1. 路径规范化后命中 AppContext 真实注册范围（含子路径）→ True；
2. 当前对象身份（target.identity 或 identity_of(path)）在 active 身份集合 → True（硬链接/改名/移动后仍是同一对象）；
3. 身份读不到、且路径在历史登记路径集合 → True（不静默降级；网络盘/不支持 file id 的文件系统保守处理）；
4. 否则 False —— 同内容不同身份的复制件不是原对象。

`effect_targets_internal_data` 对每个 path target 使用 `target.identity`（工具/审批恢复已带）或现场读取；旧签名（只传 effect/protected）保持兼容。

sync 口径：文件登记自身身份；目录登记自身 + `iter_files_limited` 有界枚举文件（max_files=5000, max_depth=3，不做全盘扫描）；同一路径出现新身份 → 旧 identity `active=0, retired_reason=replaced`、新身份 active；注册路径不存在/身份读失败 → 保留已有 active，仅计 errors；路径消失不删除登记（退出注册范围的对象仍可按身份被识别/保护）。

## 3. Windows/POSIX 能力证据

* 本机探测（Windows）：`HARDLINK True`，`SYMLINK False`（WinError 1314 客户端没有所需的特权）。
* 测试对硬链接：真实 `os.link` 成功时断言同一身份仍受保护；创建失败时不 skip，改用同身份移动验证同一判据并记录平台边界。
* 测试对符号链接：创建成功时断言身份判定保护；创建失败时用相对/规范化别名验证注册路径与身份判定不绕过（不 skip）。
* POSIX 未在本机运行：实现完全复用 `services/file_identity.py`（Windows FILE_ID_128 / POSIX st_dev+st_ino），但本轮定向测试平台为 Windows；POSIX 结果由 Lead/D CI 覆盖，未在报告中声称已验证。
* 跨卷移动：文件身份会变（OS 事实），按新对象处理；同卷 rename/move 身份不变（测试覆盖）。

## 4. 测试命令与结果（真实运行，Python 3.11 venv）

依赖：`cd backend; uv sync --frozen --extra dev`。

    uv run --frozen pytest tests/test_r9h2_internal_identity.py tests/test_internal_data_guard.py \
      tests/test_r9h_internal_data.py tests/test_sandbox_readonly.py tests/test_computer_sandbox.py \
      tests/test_sr_a_computer.py tests/test_fs_tools.py tests/test_fs_delete_recycle.py \
      tests/test_r9g_engine_core.py -q
    → EXIT=0（仅 1 个既有 skip）

新增测试覆盖（真实文件操作）：
* 登记：目录 + 目录内文件身份；active_keys/known_paths/lookup；
* 相对路径/..；effect target.identity 命中身份集合；普通同名 .env/.git/config/credentials/service.pem 不因名称保护；
* 同内容复制件身份不同 → 不保护；改名/移动后身份保护；硬链接（真实创建）同身份保护；
* 原子替换：旧身份 retired/replaced、新身份 active，旧 identity 不再保护；
* 身份读不到：历史登记路径保守命中；同路径被另一身份文件顶替 → 不被旧登记错误保护；
* 关闭连接重开 store 恢复 active_keys/known_paths/lookup；
* `with_target_identity` 补齐缺失 identity、不覆盖已有值；
* read_only/restricted/unrestricted 路径规则不变（读取拒绝/写入三次确认/unrestricted 读允许）。

## 5. 接线点（B/Lead；C 未改 engine/app）

1. app（B）：启动创建 `InternalDataIdentityStore(ctx.conn)`，对 `_internal_data_paths()` 调 `sync(..., source="app_startup")`；失败仅告警，路径判定仍兜底；提供 refresh 供更新路径后同步。
2. engine（B）：新增 `internal_data_identity_keys` / `internal_data_known_paths` callable 参数（缺省空），在 `_verdict` 与 `recheck_approved` 调用 `effect_targets_internal_data(effect, protected, identity_keys=..., known_paths=...)`；不改判定顺序/权限政策。
3. recheck/审批恢复（B）：重建 Effect 后调用 `security_gate.with_target_identity(effect)`（或按 target 用 `path_identity_key`）保证 path target 带真实 identity；与工具首次构造的 identity 一致。
4. 未接线时行为：`effect_targets_internal_data` 仍按路径注册判断（R9-H 行为），身份增强只在调用方传入 identity_keys/known_paths 时生效。

## 6. 契约偏差 / 未完成 / 未验证

1. `engine.py`/`app.py` 未改（B/Lead 负责）；本轮没有真实 turn/审批恢复的引擎身份接线 E2E，只验证分类器 + 真实文件操作 + 网关 helper。
2. POSIX 符号链接/硬链接/文件身份未在本机运行（Windows 定向测试）；实现复用既有 file_identity（POSIX st_dev+st_ino），跨平台结论由 Lead/D CI 验证。
3. 文件系统不支持 file id（个别网络/虚拟盘）时 `identity_of` 返回 None：路径在 known_paths 才保守保护；纯新路径无法按身份识别 —— 这是契约要求的「不猜」。
4. 跨卷移动身份会变，按新对象处理（OS 事实，非绕过）。
5. 修改 `tests/test_r9h_internal_data.py` 的上轮旧预期（该文件不在本轮独占清单，但属于上轮 C 交付且旧断言与新契约冲突；仅修正该处，未动其它断言）。
6. 未改历史迁移；迁移 61/补偿 62 由 Lead 已建；未 push / 未 merge。