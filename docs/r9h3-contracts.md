# R9-H3 契约：第三轮后续补修（已知限制优先级 / 恢复完全无限制 / 内部文件身份自动更新）

> 基线：r9h2/main = 7b66092e01f57121067c48588e8c3a30237367d0（代码 8cb92eab6d9724c910ed8b7b8642c149be0c5bde；仅 docs/evidence 差异）
> 完整 CI 累计 2/2：39930482370 Success、39958699070 Success。本轮不得触发第三次完整 CI，也不能换工作流绕过预算；只做最终集成 SHA 的定向 CI。
> 修复分支：r9h3/main（worktree D:/qio-dev/qio-r9h3-main）。只修三处问题，不新增功能、不改权限政策、不合并 main、不发布。

## 1. 三处问题

1. unresolved/legacy 要求已保存可靠限制时，引擎先命中“限制未完整恢复”分支；后续模型审查允许后，已知限制检查没有执行，导致可修改 B。
2. R9-H2 的 targeted_limits_review 让完全无限制模式调用普通 ModelAuthorizationReviewer；提示词仍判断“是否覆盖操作”，标记未进入模型内容；重复核对会重复调用。这重新引入独立授权审查。
3. 正常应用原子替换内部文件后 identity store 未自动更新；新对象硬链接在手动刷新前按普通文件放行。

## 2. 修复一：已知限制优先级（A：engine.py / recheck.py）

### 2.1 判定顺序（冻结）

单项/批量/审批恢复统一：
1) 计算当前绑定要求与当前有效限制（requirement.structured_limits；legacy/unresolved 时也必须使用已保存 known limits；context.extra resolution_limits 同）。
2) 先做有效限制冲突核对；命中即 DENY，不调用模型审查、不复用旧 allow、不进入内部数据三次确认或普通审批。
3) 再做内部数据特殊规则、子智能体范围、永久删除、固定查询、模式。
4) 最后处理尚未恢复部分：unresolved/legacy 在 unrestricted 对相关操作返回具体确认（APPROVE），不调用普通独立授权审查；受限模式仍走原有审查。
- 限制只覆盖其 operations/objects；范围外操作不得因局部限制变成 DENY。
- _limit_conflict 必须读取 unresolved 状态下的 known limits，不得因 unresolved 跳过。
- 用户真实补充明确解除限制时，通过原有语义/版本/apply_resolution 更新；不得以一次操作批准视为解除。

### 2.2 入口

- AuthorizationEngine._verdict：限制冲突提前到内部数据/scope/unresolved guard 之前。
- recheck_approved：先查最新有效限制，再处理 unresolved/内部数据/永久删除；旧审批或模式不能覆盖有效禁止。
- authorize_many/authorize_batch：走同一 _verdict/_limit_conflict，逐项独立。
- _permanent_delete_decision 的旧 allow 复用不得早于/绕过有效限制。
- 不新增关键词/句式/名称判断。

### 2.3 测试（A）

tests/test_r9h3_limits_priority.py（真实 RequirementLedger + 真实 engine/RecheckService，fake reviewer）：
- unresolved + 已保存“禁止修改 B”：写 B DENY；reviewer 调用 0；旧 allow 不能改结论；普通人工批准/审批恢复/内部数据确认不能覆盖。
- 范围外写 A 不是 DENY（按模式正常结果）。
- reviewer 始终 ALLOW 也不能覆盖已知禁止。
- 用户通过真实 apply_resolution 解除 B 限制后，写 B 正常继续。
- 批量只有 B 子项 DENY，A 子项正常。
- 执行前新增/更新有效限制后，RecheckService 使用最新限制拒绝。
- 不预构造最终 DENY；真实走引擎。

## 3. 修复二：移除完全无限制模式的独立授权审查（B：review.py；A 移除 engine 路径）

### 3.1 删除

- 删除 ReviewRequest.targeted_limits_review 字段与 _validate_request_shape 的 unrestricted 例外。
- 删除 _apply_review 的 targeted 参数与 request 字段；删除 authorize/authorize_many 的 unresolved_review 分支。
- 完全无限制模式不再调用 ModelAuthorizationReviewer；普通独立授权审查仅 restricted。
- 不改名/不加标记/不保留同义审查。

### 3.2 保持

- 限制恢复仍由既有专用语义理解流程完成；只回答当前版本有哪些有效限制，不判断具体操作是否授权，不生成普通授权记录。
- 恢复成功并保存后 unrestricted 按既定规则继续；恢复失败保持 unknown，先落实已知限制，未恢复部分沿用具体确认，不静默放行、不新增常规审批。
- 不阻塞无依赖的其他要求；固定查询、普通读取按既有规则。
- 不对每次 authorize/recheck/批量子项重复启动同版本恢复；成功复用持久化状态，失败沿用已有策略，无无限重试。
- restricted 原审查正常；read_only 不新增审查；内部数据三次确认/永久删除规则不变。

### 3.3 历史不当授权记录（Lead 迁移 63/补偿 64）

- 迁移 63 撤销可明确识别的 targeted 例外授权记录：authorization_records.mode='unrestricted' AND source='model_review' AND revoked_at IS NULL；保留行与审计，只置 revoked_at。
- 补偿 64 只重放追加 DDL（补偿 62 全链），不重放数据 UPDATE；SCHEMA_VERSION=COMPENSATION_VERSION=64。
- 不删除全部历史授权；无法明确判断的保留并记录边界。

### 3.4 测试（B）

tests/test_r9h3_unrestricted_no_review.py（真实引擎/台账/审批，fake reviewer）：
- unrestricted + unresolved（有无已知限制）普通审查器调用 0；批量和重复 recheck 也为 0。
- 已知限制仍拒绝；恢复成功后正常继续；恢复失败不被当无限制。
- restricted 仍调用审查；read_only 不新增；内部数据/永久删除规则不变。
- 旧 targeted 授权记录迁移撤销后不能复用；审计保留。
- 不能只断言标记进入模型输入；要证明审查器未调用。

## 4. 修复三：正常内部文件替换自动更新身份（C：internal_data_identity.py / internal_data.py / app.py / 真实可信写入入口）

### 4.1 调查与接线

- 追踪实际内部文件生命周期（DB/config/credentials/logs/instances/cache/tmp 的真实可信写入入口），找出正常创建/原子替换/重建位置。
- 在真实提交成功后的受控路径调用 InternalDataIdentityStore 身份更新；不能只加无人调用的 helper。
- 身份更新与提交可靠顺序：提交成功后立即更新；失败保留事实并明确返回/记录，不静默降低保护仍报告完整成功。
- 只针对实际变更对象有界更新，不新增全盘扫描/沙箱/隔离。
- 可信应用写入与普通工具明确区分；普通工具不能自报新增/解除/替换保护身份。
- 保留启动 sync 与显式 refresh（诊断/补充），但正常保护不依赖手动刷新/重启。
- 旧对象按既定生命周期（原子替换旧身份 retired；仍被引用时保留历史）；普通同名/同内容复制件不误登记。
- 正常保存配置/记忆/日志/审批记录不额外触发三次确认。

### 4.2 测试（C）

临时模拟数据；必须通过真实可信写入入口（如真实 settings/config 保存、credentials 保存），不能只直接调用 store.refresh_path：
- 原子替换完成后，不手动刷新/不重启，新对象硬链接立即受保护。
- 原路径/相对路径/规范化/硬链接/符号链接或平台别名/改名移动判定一致。
- 新建内部文件、重建路径正确登记。
- 身份更新失败不静默降级（返回/记录明确错误；路径保护兜底）。
- 旧对象仍被引用时生命周期正确；普通复制件/同名材料不误伤。
- Windows/POSIX 分别验证；不支持能力明确记录；不操作真实用户数据库或凭据。

## 5. 文件归属

| 分支 | 负责 | 独占文件 |
| --- | --- | --- |
| r9h3/main | Lead：迁移 63/64、schema/migrate、契约、集成、最终定向 CI、docs | storage/migration_r9h3_review_records.py、storage/schema.py、storage/migrate.py、docs/status、docs/r9h3-*、tests/test_r9h3_state_migration.py |
| r9h3/a-limits | A：已知限制优先级/执行前核对 | security/engine.py、security/recheck.py、tests/test_r9h3_limits_priority.py、docs/r9h3-reports/A.md |
| r9h3/b-unrestricted | B：移除完全无限制独立审查 | security/review.py、tests/test_r9h3_unrestricted_no_review.py、docs/r9h3-reports/B.md |
| r9h3/c-identity | C：内部文件替换身份自动更新 | security/internal_data_identity.py、security/internal_data.py、services/app.py、相关可信写入入口、tests/test_r9h3_identity_refresh.py、docs/r9h3-reports/C.md |

- 只提交自己分支；不 push、不 merge main；Lead 统一集成/推送/定向 CI。
- 不删除/skip/xfail/放宽既有测试；不改历史迁移；模型 fake/mock。
- 不得触发完整 CI；仅 Lead 在最终集成后运行定向 CI。

## 6. 验收真实流程

真实用户要求/限制 → 真实台账/版本 → 真实引擎判定 → 真实工具/审批/重核对 → 实际执行或明确拒绝。不能只断言标记或预构造 DENY；要证明已知限制在审查/审批/特殊确认之前生效。

## 7. 交付

- 更新 docs/status.md（P78）、docs/r9h3-merge-notes.md，区分已修复/已验证/未验证/边界。
- 最终报告中文：基线/分支/最终 SHA/HEAD；三问题根因/位置/真实证据；unrestricted 审查器零调用；已知限制不可覆盖；身份自动更新真实接线；定向测试/CI 结果/SHA/运行号/累计次数；未验证平台/剩余限制/完整 CI 未新增（预算用尽）；合并准备（不合并/不发布）。
