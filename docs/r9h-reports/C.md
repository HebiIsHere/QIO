# R9-H C 报告：名称判定清理 + 结构化限制持久化/继承/恢复

> 分支：`r9h/c-limits`（worktree `D:/qio-dev/qio-r9h-c`，基线 `39c0f48e`）
> 契约：`docs/r9h-contracts.md` §5、§6（冻结）。只做定向测试，不跑全量 pytest/完整 CI。
> 结论：C 独占范围内已实现并通过定向测试（含迁移套件）。引擎最终限制判定接线由 Lead；
> `test_r9g_engine_core` 两条结构化 limits 用例仍按基线预期为红（Lead 负责适配，本分支未改该文件）。

## 1. 改动文件

| 文件 | 改动 |
| --- | --- |
| `backend/src/agent/security/internal_data.py` | 删除 `SENSITIVE_DIR_NAMES/SENSITIVE_FILE_NAMES/SENSITIVE_SUFFIXES/is_sensitive_credential_path` 及全部调用；内部数据只按 AppContext 真实注册路径 + 路径规范化识别，保留 `is_internal_data_path/effect_targets_internal_data/requires_three_confirmations` |
| `backend/src/agent/services/computer.py` | 删除 `REDLINE_NAMES/REDLINE_SUFFIXES/_is_redline` 及 check_path、git -C、命令路径操作数里的名称红线分支；工作区内普通路径 auto、根外 approve（真实保护由上层 internal_data/引擎按注册路径判断） |
| `backend/src/agent/security/requirement_limits.py`（新增） | 结构化限制纯函数：`normalize_limits/serialize_limits/deserialize_limits/merge_limits/limits_from_requirement` + 状态常量；结构校验/去重/上限；非法抛 ValueError、NULL/非法 JSON 归为历史未知 |
| `backend/src/agent/storage/migration_r9h_limits.py`（新增） | 迁移 59：`user_requirements.structured_limits`（TEXT，NULL=历史未知）、`structured_limits_state`（TEXT NOT NULL DEFAULT legacy）、`structured_limits_source` |
| `backend/src/agent/storage/schema.py` | 追加迁移 59；补偿 60 = 补偿 58 全部语句 + 59（去重）；`SCHEMA_VERSION=COMPENSATION_VERSION=60` |
| `backend/src/agent/storage/migrate.py` | `REQUIRED_OBJECTS[columns]` 增加 `user_requirements.structured_limits/ structured_limits_state/ structured_limits_source` |
| `backend/src/agent/security/requirements.py` | `UserRequirement` 增加 structured_limits/state/source；`_from_row` 读取（老库缺列兼容）；`record_user_message` 写 state=structured、[]、source=user_message；`add_correction` 继承/合并父限制、支持明确 removed、父 legacy/unresolved 保留未解决；`set_structured_limits`/`structured_limits_for`/`legacy_requirements` 恢复与查询入口；`apply_resolution` new/amend/withdraw 持久化结构化限制（`removed_limits` 用 getattr 兼容 A 的 resolution.py 追加）；`AppliedResolution` 追加 `limits_state/structured_limits` |
| `backend/tests/test_r9h_internal_data.py`（新增） | 名称清理源码级检查、普通 .key/.pem/.crt/credentials/.config 不被名称拒绝、真实注册内部数据读取拒绝/写入三次确认（三模式）、相对路径/../改名/符号链接规范化不绕过、移出注册范围不再保护 |
| `backend/tests/test_r9h_limits.py`（新增） | 真实 sqlite + 真实迁移：新要求 structured/[]、legacy NULL 不等于无限制、unresolved 保留已知新增但不声称完整、关闭连接后新 ledger 恢复、amend 继承/追加/明确解除、父 legacy 保持未解决、withdraw 失效、引擎按持久化限制拒绝、无关要求不串入、纯函数校验、迁移 59/补偿 60/REQUIRED_OBJECTS 真实核对 |
| 适配既有测试 | `test_internal_data_guard.py`（删除 is_sensitive 断言，改断言词表已移除 + 普通名称非内部数据）；`test_computer_sandbox.py`（普通同名文件按根内 auto/根外 approve；plan/accept-edits 语义按模式不变）；`test_sr_a_computer.py`（git -C 普通名根内 auto/根外审批；cat 普通 .env 根内 auto；根外 .env 走审批而非名称 deny） |

## 2. 名称判定清理（契约 §5）

* 删除两个模块里的名称/后缀词表与判定函数；全仓源码已无 `SENSITIVE_*`/`is_sensitive_credential_path`/`REDLINE_*` 引用（测试用源码级断言兜底）；
* `check_path` 现在只返回 auto（根内）/approve（根外）/deny（路径无法规范化）；模式语义（plan 拒绝写、accept-edits 根内自动）保持不变；
* `git -C` 与命令文件操作数：只核对路径关系（根内/根外），根外走审批；不新增名称词表/正则/全盘扫描；
* 真实内部数据仍由 AppContext 注册路径 + `internal_data.is_internal_data_path`（规范化 + `is_relative_to`）在上层判断：读取拒绝（unrestricted 允许）、写入/删除/脚本维护三模式三次确认；
* 名称不再产生允许/拒绝/确认（普通 .env/.git/.ssh/.config/credentials/.pem 在工作区按普通路径）。

## 3. 结构化限制（契约 §6）

### 3.1 数据与状态

* `structured_limits`：JSON 数组；NULL=历史未知；[]=明确无限制；
* `structured_limits_state`：legacy（历史未理解）/structured（已理解并核对）/unresolved（重新理解失败/无法可靠恢复）；
* `structured_limits_source`：理解依据（模型+提示词版本/错误码/恢复入口），不含推理全文与密钥；
* 限制条目：`{operations: [...], objects: [{kind: path|path_prefix|directory|program|service, value}], reason}`。

### 3.2 写入/继承/恢复规则

* 新要求：`record_user_message` 写 state=structured、[]、source=user_message（不把旧展平文字当限制）；`apply_resolution(new)` 用 resolution.limits 覆盖为本次理解结果（空数组=明确无限制；非法结构 → state=unresolved + 审计）；
* amend：父 state=structured → `merge(parent, added=resolution.limits, removed=resolution.removed_limits)`，state=structured；父 legacy/unresolved → 子保持 legacy/unresolved，可保存已知新增但 `limits_from_requirement` 返回 None（不得声称完整）；
* 用户只补充一句话：`add_correction` 默认继承父结构化限制，不清空；只有 resolution.removed_limits 明确解除才移除（按 operations+objects 匹配，reason 不参与匹配）；
* withdraw：目标 status=withdrawn，`binding_for` 返回 None，旧限制不再被绑定/复用；
* 恢复入口：`RequirementLedger.set_structured_limits(...)`（可设 structured/unresolved/legacy）、`structured_limits_for`、`legacy_requirements()`（供 Lead/A 的模型重新理解接线）；
* 引擎读取：基线已预接线 `requirement.structured_limits` / `_limits_unresolved` / `_limit_conflict`；本分支提供真实持久化字段与恢复语义，`limits_from_requirement` 对 legacy/unresolved 返回 None（引擎按未解决处理，不当作无限制）；
* 限制只约束不授权：requirement_limits 模块没有 authorize/grant/allow 入口；引擎只用它拒绝。

### 3.3 迁移

* 迁移 59 只追加三列（幂等由 ADD COLUMN 预检保护）；补偿 60 = 补偿 58 全部语句 + 59，去重；
* `SCHEMA_VERSION = COMPENSATION_VERSION = 60`；`REQUIRED_OBJECTS` 增加三列；
* 历史行默认 state=legacy、structured_limits=NULL（不是明确无限制）。

## 4. 测试命令与结果（真实运行，本机 Python 3.11 venv）

依赖：`cd backend; uv sync --frozen --extra dev`。

    # 新测试 + 名称清理直接受影响测试
    uv run --frozen pytest tests/test_r9h_internal_data.py tests/test_r9h_limits.py \
      tests/test_internal_data_guard.py tests/test_computer_sandbox.py tests/test_sr_a_computer.py -q
    → EXIT=0

    # 迁移套件（真实迁移必须通过）
    uv run --frozen pytest tests/test_fu_w3_b01_schema.py tests/test_main_closeout_migration_objects.py \
      tests/test_p2_d_migration_append_only.py tests/test_p3_d_migrations.py \
      tests/test_p1_d_migration_contract.py tests/test_migrations_append_only_verify.py \
      tests/test_p4_d_oldpaths_static.py tests/test_migration_atomicity.py tests/test_storage.py \
      tests/test_rm_lead_migration_discipline.py -q
    → EXIT=0

    # 其他相关既有套件
    uv run --frozen pytest tests/test_fs_tools.py tests/test_fs_containment.py tests/test_sandbox_readonly.py \
      tests/test_r9g_resolution.py tests/test_r9f_requirements.py tests/test_r9f_review_validation.py \
      tests/test_r9g_binding.py tests/test_r9g_acceptance_e2e.py -q
    → EXIT=0

证据：新增 `backend/tests/test_r9h_internal_data.py`、`backend/tests/test_r9h_limits.py`；迁移断言在 test_r9h_limits 内真实跑迁移 59/60 并核对列/版本/REQUIRED_OBJECTS。

## 5. 与契约偏差 / 未完成 / 未验证

1. `test_r9g_engine_core.py::test_structured_limit_denies_matching_write_in_unrestricted` 与 `test_limit_matches_only_structured_objects` 仍为红：基线即预期红，由 Lead 适配引擎判定；本分支未修改该文件。
2. 引擎最终使用 context 本轮限制 + requirement.structured_limits 的完整判定（`_limit_conflict` 的 context 部分）由 Lead 接线；C 已提供持久化字段、状态与恢复入口，并有真实 DB 测试证明重载后可被引擎读取。
3. 模型重新理解历史 legacy 要求（提示词/模型调用）由 Lead/A 接线；C 提供 `legacy_requirements()`/`set_structured_limits()` 与状态语义，未在本分支调用真实模型（测试用 fake/直接恢复入口）。
4. 新进程恢复用关闭连接 + 重新打开同一 sqlite 文件模拟；真实独立进程重启链路由 Lead E2E/R9 验收覆盖（未在本分支起子进程）。
5. 审批等待/批准恢复/后台继续/执行前重核对的**真实 turn E2E** 由 Lead/A 集成验证；C 覆盖了这些环节依赖的持久化/重载/amend+withdraw/父未解决语义，但没有测试预先登记要求再手动 set context 作为证据（契约禁止这种写法）。
6. 未改 `engine.py/review.py/app.py/cmd_tools.py/固定查询`；未改历史迁移；未删除/skip/放宽既有测试（仅按新契约更新名称红线相关断言，语义更强）。
7. 未 push / 未 merge。