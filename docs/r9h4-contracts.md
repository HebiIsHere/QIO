# R9-H4 契约：授权影响、限制生命周期、撤回/内容一致性、目录批量、内部身份、审批并发、子智能体范围

> 基线：r9h3/main HEAD = 68101d6261253bb742d01af73a458bab2e0a1c5a（代码验证 7f877bf3547b85a38c6209acb91971c453d3596d；定向 CI 39979313725/39979659563/39979919730 仅基线证据）
> 完整 CI 累计 2/2 已用尽：本轮不得触发第三次完整 CI，不得换工作流/拆分矩阵绕过；最终代码只做受影响定向测试和定向 CI。
> 修复分支：r9h4/main（worktree D:/qio-dev/qio-r9h4-main）。只完善现有功能，不新增沙箱/代理/全盘扫描/UI/语言风格。

## 1. 权限规则（必须保持）

1. 用户明确禁止在三种模式都拒绝；普通审查、旧 allow、人工批准、内部数据确认都不能覆盖；解除必须来自用户明确修改并按真实要求版本更新。
2. 只读：禁止修改/删除业务文件、启动不能证明只读的程序；人工批准不能突破；内部数据维护仍三次确认。
3. 受限：固定规则先行；需理解要求时才普通独立审查；符合可执行、冲突拒绝、无法确定才具体人工审批。
4. 完全无限制：不调用普通独立审查、不增加普通常规审批；仍遵守明确限制/撤回/内容一致性/内部数据三次确认；限制恢复只恢复有效限制，不判断具体操作授权。
5. 固定查询可用；默认删除进回收站；永久删除只读拒绝、受限明确人工确认、完全无限制允许，均不能违反用户限制。
6. 正常应用保存设置/记忆/审批/日志不额外三次确认；工具/脚本/自建工具/子智能体直接维护内部数据才适用。
7. 审批 10 分钟、重启不续期、等待不阻塞、重复领取不重复执行。
8. 禁止重新引入自然语言关键词/句式/否定词/引号/文件后缀授权判断。

## 2. 统一操作与影响语义（A/B/C/D/Lead）

- 规范操作名：read、write、create、delete、execute、install、system_change（egress 本轮排除但保留既有）。
- 规范对象 kind：path、path_prefix、directory、program、service。read 必须能从提示词/解析/保存到执行一致；program 也要能表达。
- effect 可描述多个实际影响：Effect.detail 的 limit_impacts 为 tuple/list[dict]，每项 {operation, objects:[{kind,value}]}；真实工具的 base effect 与实际影响都要检查。
- 内部数据身份是保护属性，不替代操作动作；EffectKind.INTERNAL_DATA 不得固定映射 write，应使用 detail 的 internal_operation（read/write/create/delete/execute）或按明确事实，无法确定则保留不确定。
- 相对路径限制以引擎后端已知工作位置（workspace_root；工具 detail cwd 明确时用 cwd）规范化；base 不可确定时保留不确定（不能当无限制）。
- 模型输出被解析接受后，持久化和执行必须接受同一操作/对象 kind；个别条目无法处理时保留其余可靠限制并标 unresolved，不能清空全部。
- 当前限制只取操作绑定的具体要求和有效继承链，不取最近 active、不拼全部历史。

## 3. 修复一：实际影响与限制匹配（A/B/C/D/Lead）

- A：requirement_limits normalize/validate 支持 read/program；resolution/prompts 输出契约接受 read/program/多影响；保留确定性事实校验。
- B：engine _limit_conflict 检查 base operation + limit_impacts + 程序对象；read/write/delete/program/目录；internal operation；相对路径 base；限制命中即 DENY（在审查/审批/内部确认/模式之前）。
- C：文件/附件工具为真实操作补充 limit_impacts（删除目录枚举子对象、批量逐项、附件写回/回收）；目录递归删除影响子对象；目录查询不被扩大成限制。
- D：命令/脚本/程序启动解析实际影响（已解析删除对象、程序 target、脚本 digest、cwd）；launch/spec 与 effect 一致。
- Lead：effects.py/security_gate.py 通道、approved_operations 执行器传递 impacts；真实工具验证。

## 4. 修复二：限制继承与解除（A）

- 父 unresolved 但保存已知限制时，amend 子要求必须继承 known；恢复失败不得用空集合覆盖；new/reboot/后台继续保持。
- merge_limits/resolution removed_limits 支持精确移除部分对象或部分操作：解除 B 的 write 不清除 A，也不误删 B 的 delete；无法精确确定时保留原限制并标 unresolved。
- 恢复结果写回前核对要求 id/version/status/完整链摘要；等待期间修改/撤回丢弃旧结果。
- 可识别错误记录只追加迁移纠正，保留原话/来源/审计；不修改历史迁移。
- 提示词只改要求理解/限制表达/输出契约，不改全局语言风格。

## 5. 修复三：撤回与执行前核对（B/F/Lead）

- 绑定要求撤回/替换/版本不一致时，engine/recheck 不得当作“没有要求”进入完全无限制允许；应明确拒绝/失效。
- _apply_review 模型返回后重新读当前要求、版本、状态和操作事实；失效则不采用旧结果，不能建立有效授权。
- 直接执行、人工批准恢复、后台继续、子智能体、批量逐项都核对。
- 固定查询/普通受控读取/应用正常功能保持可用；没有依据不伪造绑定或最近要求。
- 不用再次普通授权模型替代确定性的要求有效性核对。

## 6. 修复四：审查内容与实际执行一致（B/C/D/Lead）

- 审查对象/内容/前置状态/程序/脚本/argv/cwd 必须与最终执行一致；执行前读取真实状态比较，不能只重复旧摘要。
- 内容变化不得把新内容套用旧许可；不得重新生成新许可冒充原允许。
- 统一重核对不得丢调用方对象身份/原内容依据；正确读取 RecheckResult.decision（不是错误字段导致普通失败）；需新确认进入现有确认流程。
- 文件写入/补丁/附件写回/程序/脚本启动覆盖；变化只失效受影响操作。
- 最终检查与提交尽量同受控路径；说明操作系统并发边界，不新增沙箱/监听。
- 测试用等待中的 fake 审查制造真实文件/脚本变化，检查实际文件和程序输出。

## 7. 修复五：目录与批量（B/C/Lead）

- 删除目录按实际子对象影响判断；包含受保护子文件/内部数据不能只查目录本身；递归修改/删除与普通目录查询区分。
- 审批与执行使用可比较的目录影响依据（有界枚举+对象身份+内容/条目摘要）；新增/移除/替换内容变化使旧批准失效；有界达到上限时明确不确定，不忽略剩余影响。
- 批量一次合并确认但每项保留依据/内容/状态/结果；执行前逐项核对；仅 B 失效时 A 仍可执行；不得先整批判废；不得改写已批准内容伪装。
- 路径去重按平台文件系统语义（POSIX 大小写敏感，Windows 归一化）；汇总所有项，不漏项报告成功。

## 8. 修复六：内部数据确认与身份生命周期（E/B/F/Lead）

- 确认次数由后端按实际操作/对象识别决定；原路径/硬链接/改名/移动同一对象一致；创建审批设置次数，恢复/开始前再核对最低要求；不能只凭审批单记一次确认；已知禁止优先；不额外第四次确认；正常应用写入不触发。
- FileIdentity 增加可验证生命周期依据（如 birth/creation token），InternalDataIdentityStore 登记 identity+birth；同一 identity 但 birth 不同（编号复用）不得误认为旧内部对象。
- 原路径替换不足以证明旧对象不存在：旧对象仍经其他路径存在时保持保护；新登记失败时保住已有保护；撤销旧登记与建立新登记可靠一起完成；失败报告必须与真实状态一致。
- 真正删除+编号复用不长期误保护；不永久保护全部历史编号；不把内容/大小/mtime 当永久身份；无法可靠区分时明确不确定/处理边界；不全盘扫描。
- 可信提交后自动更新身份接线保留；显式刷新只是补充。
- 迁移由 Lead 统一分配，只追加。

## 9. 修复七：子智能体范围（B/Lead）

- “符合用户整体目标”和“属于子智能体分配范围”必须分别成立；普通审查绝不能覆盖范围不足。
- 范围不足先交主智能体；主智能体在现有要求和权限内明确授予时按受控流程更新；主智能体无权限才向用户申请。
- 子智能体不得自行扩权/冒充主智能体；三种模式保留委托边界，unrestricted 也不能丢范围；恢复/后台/再次委派保持范围和版本。

## 10. 修复八：审批完成、重放与并发（F/Lead）

1. unresolved 下用户对具体操作的有效批准可完成该操作（未违反已知限制、要求/内容/范围/条件一致）；不能批准后立即因同一未恢复状态失效；具体批准不清除未知限制/不清除已知禁止/不授予长期权限；不把 unresolved 改成无限制。
2. 结束进程审批重放完整传递原审批快照和操作依据，不得总报“没有审批记录”；只操作本次创建测试进程。
3. 确认阶段与审批状态更新在实际写入时核对前一状态；并发只有一个成功推进同一步，旧请求不能覆盖新阶段/回退次数/绕过撤销。
4. 状态更新与审计一致；两连接或可控交错验证，不靠随机等待。
5. 期限/模式/要求/内容/确认次数/重复领取保持既定规则。

## 11. 文件归属

| 分支 | 负责 | 独占文件 |
| --- | --- | --- |
| r9h4/main | Lead | services/app.py、tools/approved_operations.py、security/effects.py、security/security_types.py、tools/security_gate.py、storage/migration_r9h4_*.py、storage/schema.py、storage/migrate.py、docs/status、docs/r9h4-*、集成/交叉测试 |
| r9h4/a | A | security/requirements.py、security/requirement_limits.py、security/resolution.py、prompts.py、tests/test_r9h4_limits_semantics.py |
| r9h4/b | B | security/engine.py、security/recheck.py、tests/test_r9h4_engine_binding.py |
| r9h4/c | C | tools/fs_tools.py、tools/attachment_tools.py、services/attachments.py、tests/test_r9h4_fs_batch.py |
| r9h4/d | D | tools/cmd_tools.py、security/launch.py、tests/test_r9h4_cmd_program.py |
| r9h4/e | E | security/internal_data.py、security/internal_data_identity.py、services/file_identity.py、tests/test_r9h4_identity_lifecycle.py |
| r9h4/f | F | tools/approval.py、storage/approvals.py、security/operations.py、tests/test_r9h4_approval.py |

- 共享文件只由 Lead 改；各组提交接线需求/测试/报告，Lead 顺序集成，不能只交无人调用 helper。
- 不删除/skip/xfail/放宽既有测试；不改历史迁移；模型 fake/mock。
- 不触发完整 CI；仅 Lead 最终定向 CI。

## 12. 交付

- 先复现再修复；测试真实 AppContext/台账/审批/工具/重核对/执行器；模型 fake。
- 覆盖提示词列出的全部场景；同根问题可归并但材料逐项对应。
- 更新 docs/status.md（P79）、docs/r9h4-merge-notes.md，区分已修复/已验证/未验证/边界；最终中文报告含基线/分支/SHA/HEAD、每类根因/修改/前后行为、权限规则证据、确认次数/实际执行、内容/撤回/目录/批量/子智能体/身份结果、Windows/POSIX、测试命令/结果/CI 运行号/SHA、完整 CI 累计=2/2 未新增、剩余限制、合并准备。
