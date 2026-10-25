# E 报告：内部数据身份登记、替换生命周期及失败恢复（R9-H4 §8）

- 基线：`67fa88c6`（r9h3/main HEAD，含迁移 65/66 与 `internal_data_identities.birth_key`）
- 分支：`r9h4/e`（worktree `D:\qio-dev\qio-r9h4-e`）
- 范围：E 独占 `security/internal_data.py`、`security/internal_data_identity.py`、`services/file_identity.py`、`tests/test_r9h4_identity_lifecycle.py`、本报告。**未改** schema/migration/engine/app/approval；共享文件接线需求见 §5。
- 完整 CI：未触发（预算 2/2 已用尽）；只跑定向测试，模型调用无涉及（纯文件/数据库路径）。

## 1. 实现内容

### 1.1 FileIdentity 增加可验证生命周期依据（契约 §8 第 1 条）

`services/file_identity.py`：

- `FileIdentity` 新增 `birth_key: str | None`（生命周期依据）与 `link_count: int | None`（硬链接数观测；**不是身份**，只用于替换时判断旧对象是否仍可能被引用）。
- `identity_of` 同时提供 birth：
  - Windows：`GetFileInformationByHandleEx(FileBasicInfo)` 的 `CreationTime`（100ns，强依据，形如 `win:creation:<filetime>`）；API 失败时回退 Python `st_birthtime_ns/st_ctime_ns`（Windows 的 `st_ctime` 即创建时间），来源标签如实写 `win:stat_ctime`。
  - POSIX：优先 `st_birthtime_ns`（强依据 `posix:birth:<ns>`）；取不到时回退 `st_ctime_ns`（弱依据 `posix:ctime:<ns>`，chmod/chown 等元数据操作可能改变）。
  - 不可得 → None（不伪造）。
- 新增 `birth_source` / `is_strong_birth` / `lifecycle_token` / `same_lifecycle`（三态 True/False/None）/ `identity_capabilities` / `birth_key_of`。
- **不把内容/大小/mtime 当身份**：`same_file` 仍然只认 volume+file_id；`birth_key` 只用于区分「同一编号上的另一个生命周期对象」。`identity_of` 自测增加：编辑后 identity+birth 不变、同内容/同大小/同 mtime 的另一份文件 identity 与 birth 均不同。

### 1.2 InternalDataIdentityStore 登记 identity+birth，查询同时核对（第 2 条）

`security/internal_data_identity.py`：

- 登记/更新写入 `birth_key`（迁移 65 列；老库无该列时降级为不写 birth，不崩）。
- `match_identity(identity)` / `lookup_lifecycle` 返回 `same | different | uncertain | unknown | retired`：
  - identity+birth 都一致 → `same`；
  - 同一 identity、两侧都是**强** birth 且不同 → `different`（编号复用，不是旧对象）；
  - 旧行 `birth_key IS NULL`、当前 birth 未知、弱依据不一致 → `uncertain`（**不声称同一文件**，只按路径/历史保守）；
  - 被顶替但旧生命周期 token 仍受保护（`retired_reason='replaced'` 且强 birth 一致）→ `same` + `retained=True`。
- `protected_tokens()`：识别内部对象用的匹配键 —— active 行用 `identity#birth` token（有 lifecycle 依据时）；`retired_replaced` 的强 lifecycle token 继续保留（旧对象可能仍经其它路径存在）；`retired_deleted` 不再保留。旧行 birth NULL **不加入识别集合**（不凭 identity 单独声称同一对象），启动 sync 会用当前对象 birth 回填。
- `internal_data.py`：`is_internal_data_object` / `effect_targets_internal_data` / `requires_three_confirmations` / `confirmations_for` 支持
  - `identity_keys` 里的 token（identity#birth）与旧纯键（兼容旧接线/旧测试）；
  - 新增可选 `identity_lookup`（传 `store.match_identity`）：`same` → 内部；`different` → 不是旧内部对象；`uncertain` → 只有历史登记路径命中才保守保护；**查询抛错 → 保守视为内部对象（fail-closed，不静默降级）**。

### 1.3 原路径替换不直接解除旧保护（第 3 条）

- 同一路径出现新 identity 时：
  - 只有当「旧登记有强 birth + 本进程观测到旧对象硬链接数 ≤ 1 + 历史登记路径里找不到旧生命周期对象」才把旧登记退休（`retired_reason='replaced'`）；
  - 否则保留旧登记 active（硬链接仍在、无生命周期观测等），并计数 `retained`/原因；
  - 无论 active 还是 retired_replaced，旧强生命周期 token 都保留在 `protected_tokens()` → 旧对象经硬链接/移动仍被识别为内部数据；因 token 含 birth，不会把复用编号的新对象误认成旧对象。
- **撤销旧登记 + 建立新登记在同一事务里完成**：任一步失败整体回滚，旧保护原样保留，`note_committed_path` 返回 `ok=False / code=identity_update_failed`，`last_error` 记录真实原因；失败后快照与失败前一致（测试注入失败验证）。
- 新登记失败（身份读不到）→ `identity_unavailable` + 「已保留旧登记」，不删除/不降级。

### 1.4 真正删除 + 编号复用（第 4 条）

- `note_committed_delete(paths)`（供 Lead 在真实删除执行器提交后接线）：只有「强 birth + 观测硬链接数 ≤ 1 + 历史登记路径找不到旧生命周期对象」才退休为 `deleted`；仍被硬链接引用/无法验证 → `retained`/`uncertain`，不因「路径没了」解除保护。
- 编号复用：同一 identity、强 birth 不同 → 行更新为新生命周期对象并记录 `reuse_events`；旧 lifecycle token 不在识别集合 → 复用编号的新对象**不得**被认成旧内部对象；不永久保护全部历史编号（保护键是 identity+birth，不是编号）。
- 不做全盘扫描：只处理 AppContext 注册路径 + 有界目录枚举 + 已登记历史路径（测试 `sync(max_files=2)` 验证有界）。
- 无法可靠区分（birth 缺失/弱依据不一致/旧行 NULL）→ `uncertain` 明确返回，按路径/历史保守；边界见 §6。

### 1.5 失败不静默降级 / 可信提交接线保留 / refresh 只补充（第 5 条）

- `note_committed_path` / `note_committed_paths`（R9-H3 可信写入自动更新入口）保持原签名与调用方式，内部改走生命周期登记：提交成功后立即登记新 identity+birth，不需要手动刷新/重启；失败明确返回并写 `last_error`。
- `refresh_path` 只补充：读不到身份返回 None 且保留已有登记（测试覆盖）；不做「刷新即解除保护」。
- `health()` 暴露 `ready/birth_column/last_error/observed/reuse_events`；查询失败会设置 `last_error`，不静默当「没有登记」。

### 1.6 内部数据三次确认识别一致（第 6 条）

- 同一个真实对象经**原路径 / 规范化别名 / 硬链接 / 改名 / 移动**都得到 `confirmations_for(...) == 3`；同内容不同身份的普通复制件为 1。
- 真实 `AuthorizationEngine`（`internal_data_paths` + `identity_keys=store.protected_tokens()`）对硬链接/移动对象：WRITE → APPROVE 且原因含「三次确认」；READ（受限）→ DENY「内部运行数据」（测试覆盖）。
- 创建审批时写几次确认、恢复/开始前的最低要求由 B/F/Lead 接线（见 §5），E 提供准确识别函数；已知禁止优先级在 engine/B 侧（本轮未改）。

## 2. 改动文件

| 文件 | 说明 |
| --- | --- |
| `backend/src/agent/services/file_identity.py` | FileIdentity.birth_key/link_count、Windows/POSIX 生命周期读取、token/三态/能力函数、自测扩展 |
| `backend/src/agent/security/internal_data_identity.py` | 生命周期登记/匹配/替换保留/删除核对/复用识别/事务/健康状态（保留既有公开 API：sync/lookup/active_keys/known_paths/note_committed_path(s)/refresh_path/retire_path/snapshot） |
| `backend/src/agent/security/internal_data.py` | token/identity_lookup 识别、fail-closed 查询失败、confirmations 透传（旧参数/旧签名兼容） |
| `backend/tests/test_r9h4_identity_lifecycle.py` | 新增 24 个定向用例（见 §3） |
| `docs/r9h4-reports/E.md` | 本报告 |

未改：schema/migration（迁移 65 已由 Lead 完成）、engine.py、app.py、approval/approved_operations、security_gate.py。

## 3. 验证（定向测试，全部 exit 0）

命令（`cd backend`）：

| 命令 | 结果 |
| --- | --- |
| `uv run --frozen pytest tests/test_r9h4_identity_lifecycle.py -p no:cacheprovider` | **24 passed**（7.7s） |
| `uv run --frozen pytest tests/test_r9h2_internal_identity.py tests/test_r9h3_identity_refresh.py tests/test_r9h2_parse_state.py tests/test_r9h2_real_flow.py tests/test_r9h_internal_data.py tests/test_r9h4_identity_lifecycle.py -p no:cacheprovider` | **71 passed**（32.5s） |
| `uv run --frozen pytest tests/test_attachment_identity.py tests/test_attachment_source_refs.py tests/test_attachment_migration.py tests/test_r9h2_state_migration.py tests/test_migrations_append_only_verify.py -p no:cacheprovider` | 35 passed；同批 `tests/test_r9h3_state_migration.py` 有 1 条**基线既有红**（见 §6.1） |
| `uv run --frozen pytest tests/test_approved_operations.py tests/test_r9g_batch.py tests/test_r2_v_r1_commit_boundary.py tests/test_r9h3_limits_priority.py tests/test_r9h3_unrestricted_no_review.py tests/test_r9h2_chain_recovery.py -p no:cacheprovider` | **82 passed**（37.2s） |
| `uv run --frozen pytest tests/test_r9h4_identity_lifecycle.py tests/test_r9h2_internal_identity.py tests/test_r9h3_identity_refresh.py tests/test_r9h2_parse_state.py tests/test_r9h2_real_flow.py tests/test_r9h_internal_data.py tests/test_r9h_engine_wiring.py -p no:cacheprovider` | **75 passed**（35.4s） |
| `uv run --frozen python -m compileall -q src/agent/security src/agent/services/file_identity.py` | exit 0 |

新用例覆盖（`tests/test_r9h4_identity_lifecycle.py`）：

1. 生命周期依据与能力：birth 可取得/来源标签/强依据标记/link_count；同内容+同大小+同 mtime 的复制件身份与 birth 均不同；编辑不改 identity+birth；三态（缺失→None、强不同→False、弱不同→None）。
2. 登记与查询：登记 birth；identity+birth 同时核对；旧行 birth NULL → uncertain（不声称 same）；强 birth 不同 → different；弱 birth 不同 → uncertain；旧行回填后恢复 same。
3. 替换生命周期：已知硬链接时旧登记保留 active 且旧 token 继续保护；**观测后才出现的隐藏硬链接**仍有旧 token 保护；新登记失败事务回滚、旧保护保留；成功替换旧行 retired_replaced + 新行 active；refresh 读不到不解除保护。
4. 删除/复用：模拟同 identity+不同强 birth 的编号复用 → 旧 lifecycle `different`、新 lifecycle `same`、旧 token 不再保护；可证明删除 → retired_deleted；仍有硬链接 → retained；`sync(max_files=2)` 验证有界枚举。
5. 三次确认一致性：原路径/规范化别名/硬链接或移动对象 `confirmations_for == 3`、真实引擎 WRITE=APPROVE（原因含「三次确认」）、READ=DENY；普通复制件=1。
6. 失败/重启/健康：身份读不到 → identity_unavailable + 保留旧登记；lookup 抛错 → fail-closed 保护；连接不可用时 `health().last_error` 可见；重启后 birth 持久化、旧 birth NULL 仍 uncertain。

### 平台能力记录（本机实测，Windows）

- `file_identity.identity_capabilities()`：`platform=win32, birth_source=win:creation, birth_strong=True, link_count=1`（临时文件）；
- 硬链接创建能力：**支持**（测试真实使用 `os.link` 覆盖硬链接场景）；
- POSIX 代码路径已实现（birthtime 优先、ctime 弱回退），但**本机无 POSIX 环境，未运行验证**，见 §6。

## 4. 与旧实现的兼容

- `InternalDataIdentityStore` 既有公开方法签名/返回形状保持（`sync/lookup/active_keys/known_paths/note_committed_path(s)/refresh_path/retire_path/snapshot`），`note_committed_path` 仍不接受外部传入 identity（不能自报）；
- `active_keys()` 仍是纯身份键（旧接线/旧测试不变）；birth-aware 识别用新增 `protected_tokens()`；
- `internal_data.*` 旧参数（`identity_key/identity_keys/known_paths/identity_of`）语义不变，新增参数均有默认值；
- `FileIdentity` 新字段有默认值，`attachments._identity_from_record` 等旧构造不需要改；
- 既有定向测试（R9-H2/R9-H3 身份、附件身份、已批准执行器、迁移）全部通过（除 §6.1 基线既有红外）。

## 5. 接线需求（共享文件，仅 Lead 可改）

1. **app.py `_internal_identity_keys` 改为返回 `store.protected_tokens()`**（而不是 `active_keys()`）：这是 birth-aware 识别真正生效的唯一必要接线；`_internal_identity_known_paths` 保持 `known_paths()`。旧 `active_keys()` 保留给旧测试/诊断。
2. **app.py 可信删除身份更新**：`_note_identity_after_trusted_commit` 对 `kind == "fs.delete"` 应调用 `store.note_committed_delete(paths, source=...)`（当前统一走 `note_committed_path`，删除后路径读不到身份会报 `identity_unavailable` 且内部文件删除永不退休）。返回结果并入 `internal_identity_error`，与真实状态一致。
3. **app.py 身份错误可见**：若 `store.health()["last_error"]` 非空，应写入 `ctx.internal_data_identity_error`/日志（不静默降级为「没有登记」）。
4. **engine（B）可选增强**：`_internal_identity_kwargs` 增传 `identity_lookup=lambda identity: store.match_identity(identity)`（internal_data 已支持）。不接也能工作（token 已覆盖强 birth 平台）；接了才能对 `uncertain` 状态按路径/历史保守并在弱依据平台更诚实。
5. **B/F/Lead 确认次数**：创建审批/恢复核对时用
   `internal_data.confirmations_for(effect, roots, identity_keys=store.protected_tokens(), known_paths=store.known_paths(), identity_lookup=store.match_identity)`
   决定 `confirmations_required`（硬链接/改名/移动一致得到 3；普通复制件 1）。`security_gate.match_internal_data` 目前只按路径 roots 判断内部数据，需要在 B/Lead 侧补身份+生命周期识别，否则硬链接/移动对象的审批可能只要求 1 次确认。
6. **审批恢复/开始前**：恢复或执行开始前用同一 `confirmations_for` 重新核对最低次数（B/F）；对象内容/范围变化重置确认由现有审批状态机负责（F）。

## 6. 未完成 / 边界 / 基线情况

### 6.1 基线既有红（非 E 引入，需 Lead 处理）

`tests/test_r9h3_state_migration.py::test_versions_and_compensation_are_additive_only` 断言
`SCHEMA_VERSION == COMPENSATION_VERSION == 64`，但基线 `67fa88c6` 已因迁移 65/66 变为 **66**（Lead 的契约提交）。这是基线就存在的失败，E 未改 schema/该测试；应由 Lead 在 H4 集成时更新该断言（或确认是否保留 64）。

### 6.2 平台/能力边界（如实记录）

- Windows：创建时间强依据已真机验证（本机）；硬链接能力支持并已真实测试。
- POSIX：代码路径已实现（`st_birthtime` 强 / `st_ctime` 弱回退），**本机无 POSIX 环境未运行**；Linux 3.11 的 Python `st_ctime` 是 inode 元数据变更时间，chmod/chown 等操作可能改变 → 与已登记 birth 不一致时返回 uncertain（不声称同一文件）；登记路径仍按路径保护，移动到注册范围外的对象在该场景下可能暂时只按 uncertain 处理（须 Lead 在 POSIX 环境验证/记录）。
- 文件系统不支持 file id（个别网络盘）→ `identity_of` 返回 None：按「不可确认」处理，不冒充同一文件（既有边界）。
- 硬链接在**上次观测之后**才在未知路径创建：无法不扫描地发现；本实现的处理是——旧生命周期 token 仍保留在识别集合（对象若仍存在仍被识别）；同时不把复用编号的新对象误认（token 含 birth）。这是「不扫描」约束下的诚实边界。
- 旧行 `birth_key IS NULL`：不凭 identity 单独声称同一文件，只按路径/历史保守；启动 `sync` 会用当前对象 birth 回填，回填后按 identity+birth 保护。
- 弱依据（POSIX ctime）不一致：返回 uncertain，不声称 same；登记路径仍保护，范围外移动对象的 identity 保护可能需要一次 explicit refresh/sync 重建（不静默降级为「普通文件」，但也不会无限错误保护）。

### 6.3 其它

- 未改 UI/无用户可见文案变化；无新增事件/接口。
- 报告不包含密钥；测试只用临时文件与临时 sqlite。
- 未触发完整 CI/全量矩阵；未 push/merge。

## 7. 最终提交

- 分支：`r9h4/e`
- 基线：`67fa88c6`
- 实现提交：`033c48d7`（代码 + 测试 + 本报告初版；工作树干净，不 push/不 merge）
- 本报告的 SHA 回写修正提交即本分支当前 HEAD（最终 SHA 以交接汇报中的 `git log -1` 为准）
- 改动文件：`backend/src/agent/services/file_identity.py`、`backend/src/agent/security/internal_data_identity.py`、`backend/src/agent/security/internal_data.py`、`backend/tests/test_r9h4_identity_lifecycle.py`、`docs/r9h4-reports/E.md`
