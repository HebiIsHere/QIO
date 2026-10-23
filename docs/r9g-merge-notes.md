# R9-G 合并与验收材料（审批与授权机制修复）

> 分支：r9g/main（worktree D:/qio-dev/qio-r9g-main）
> 基线：r9f/main = 9211652650e6141d1dd32dc731fad450f8df71b5（开工 fetch 核对：远端一致；未合并 main）
> 契约：docs/r9g-contracts.md；状态：docs/status.md P75
> 本轮不合并 main、不发布安装包；不新增运行期沙箱/文件范围隔离/网络隔离/子进程隔离。

## 1. 基线与集成

| 项 | 值 |
| --- | --- |
| 基线 SHA | 9211652650e6141d1dd32dc731fad450f8df71b5（r9f/main，远端一致） |
| 修复分支 | r9g/main（origin/r9g/main 已推送） |
| 契约/迁移提交 | 97777b55（契约 + 迁移 57/补偿 58 + 子分支 worktree） |
| 子分支 | r9g/w1-review = 698d54a4；r9g/w2-exec = 41814dc6；r9g/w3-origin = 9fc632fe；r9g/w4-verify = 58c3c0d9 |
| 集成提交 | 9005b205（G1-G4 修复）；89411097（W3/W4 合并）；76208a11（W2 合并）；44bbc0df（W1 合并）；1ea2d3ac（集成修复）；fdc070ea（关键词模块删除/审计/文档，最终提交） |
| 最终提交 | fdc070eaa93893910821e3d585ae14fb58c7ccb5（origin/r9g/main） |

集成方式：Lead 从基线建 r9g/main，四个并行分支各自 worktree/独占文件；Lead 负责 engine/gateway/context/app/recheck/迁移/审批存储/最终集成与交叉验证。approved_operations.py 由 W2（proc/delete）与 W3（fs/附件）分段修改，git 自动合并后由 Lead 跑定向回归；迁移只追加 57，原有历史迁移未改。

## 2. 五项修复与关键入口

1. **全面移除关键词/句式授权判断**
   - 删除 backend/src/agent/security/intent.py 整体（不是改名/恒返回空保留）；test_instruction_scope.py 改为断言模块不存在。
   - security/review.py 删除 deterministic_allow_guard 及动作词/拒绝词/条件/问号/引号全部规则；validate_outcome 只校验结构、来源（要求 id/active/version）、对象/操作子集、范围可落实，语义交给模型。
   - security/engine.py 删除 _MUTATION_PROHIBITIONS/_DELETE_PROHIBITIONS/_EGRESS_PROHIBITIONS 与 _user_prohibition；改为模型解析出的结构化 limits（对象/操作/原因）匹配，旧纯文本 limits 不参与判定。
   - tools/cmd_tools.py 删除 _command_tokens 全文搜索式永久删除判断与旧只读证明；固定查询/永久删除改结构化事实。
   - 证据：git grep 在 src 无命中（deterministic_allow_guard/_user_prohibition/_command_tokens/security.intent）；scripts/audit_r9g_decision_paths.py 静态核对 + W1 语义测试（test_r9g_semantics.py 断言这些符号不存在）。
2. **批量逐项核对**
   - 新增 security/batch.py：BatchItem/BatchItemDecision/BatchAuthorization，逐项状态 allowed/denied/waiting/invalidated/completed/failed；AuthorizationEngine.authorize_batch 逐项调用统一判定，不聚合。
   - fs_tools.py 的 fs_delete、附件回收/批量写回逐项 effect；需确认项合并为一个审批操作（payload.items 列明对象/动作/内容摘要），已允许项直接执行，被拒/失效项保留；恢复只继续未执行且仍获准项；移动/重命名核对源+目的（当前文件工具无 move/rename/copy 入口，未伪造）；外发/上传由命令/网络路径核对发送方/接收方。
   - 证据：tests/test_r9g_batch.py、test_r9g_fs_binding.py、W4 验收批量用例；审计脚本动态验证两项得到两个独立结论。
3. **操作绑定具体用户要求及版本**
   - 迁移 57：user_requirements.version、pending_approvals.requirement_id/requirement_version、authorization_records.requirement_version、authorization_review_audit.requirement_version、表 requirement_resolutions；补偿 58。验证：apply_migrations 后版本 58，表/列/索引齐全。
   - 新增 security/resolution.py：主模型单次调用理解 new/amend/withdraw + 目标要求 + 修正 + 结构化 limits；后端校验目标存在/active/版本，失败抛 RequirementResolutionUnavailable 并写失败审计，不关键词兜底。
   - security/requirements.py：version、RequirementBinding、binding_for/validate_binding/candidates/apply_resolution；引擎只使用 AuthContext.requirement/contextvar 绑定，缺失/失效即需确认，不再调用 requirements.current()。
   - 子智能体 scope 继承父 requirement；审批单/执行器/恢复携带同一绑定；后台/重启恢复按绑定核对。
4. **审查/复用/批准/执行内容一致**
   - 审查请求、授权记录、审批单、执行器共用 operation_fingerprint（对象身份/程序/argv/cwd/脚本摘要/接收方/模式）与要求版本；源文件/脚本内容/内联参数/cwd/目的地变化后旧记录不命中。
   - 审批单提交时写独立 operation_digest（存 fingerprint 列，独立于 operation 列）；批准/领取/执行前重算比对，operation 被改动即 invalidated；执行器只执行审批单绑定的内容，不重新生成脚本。
   - 三次确认按序且绑定同一操作内容：内容变化时下一次确认/批准返回 409 并把已累计确认重置为 0。
   - 证据：W4 test_argv_change_in_approval_record_is_not_executed、test_confirmations_bind_same_object_and_content、test_script_change_invalidates_approval；W3 执行器逐项重核对测试。
5. **每项实际操作开始前重核对**
   - 新增 security/recheck.py：OperationBinding/RecheckResult/RecheckService；顺序：绑定要求 active/版本一致；用当前模式重走统一判定（固定规则/固定查询/永久删除/审批政策）；操作指纹；内容/前置状态摘要；spec_digest；条件证据；审批有效期/确认；委派范围。通过后立即开始，不再长期排队。
   - 覆盖：fs_write/patch/delete、cmd run_program/run_cmd、自建工具/审批执行器、子智能体（继承绑定）、批量子项、模型审查返回、人工批准恢复、后台继续、重启恢复。
   - 过期审批：存储 decide/confirm/claim 与执行器都核对 expires_at，过期不执行、重启不续期；重复点击/回调/恢复由 CAS 去重。
   - 证据：tests/test_r9g_engine_core.py（重核对 ok/要求版本变化/内容变化/无绑定）、W3/W2 定向测试、W4 模式降级/过期审批用例。

## 3. 模式与命令行为（实测）

| 场景 | read_only | restricted | unrestricted |
| --- | --- | --- | --- |
| 固定查询（程序版本/pwd/系统信息/目录列表/文件属性，结构化判定） | ALLOW（不审查） | ALLOW（不审查/不弹确认） | ALLOW |
| 普通业务写/安装/一般执行 | DENY | 固定规则/有效授权直接执行；需理解时模型审查 | ALLOW（跳过常规审查/审批） |
| 永久删除业务文件 | DENY | 必须明确人工确认（模型 ALLOW 不可替代；审批单写明对象/永久/不进回收站/不可恢复） | ALLOW |
| 内部运行数据直接维护 | 三次确认 | 三次确认 | 三次确认 |
| 用户结构化限制 | 生效 | 生效（优先于模型结论） | 生效（来自要求解析，不新增独立授权审查） |
| 固定查询 + 重定向/管道/串联/同名替身/解释器脚本 | 不豁免（普通流程） | 不豁免 | 不豁免 |

固定查询只按真实程序身份/argv/cwd/shell 结构判定，不搜索 --version 字符串、不整程序放行；引擎从 effect.detail 中真实执行事实重算，不采信工具自报 fixed_query。证据：tests/test_r9g_fixed_queries.py、test_r9g_deletion.py、W2 Windows 真机 _r9g-evidence/w2-exec/verify_r9g_exec_policy.json（20/20）。

## 4. 测试与 CI

（本节数字来自本轮实际命令；统计与运行号以最终结果为准）

- 定向回归（开发期，分批）：R9-G 新增测试（语义/要求解析/绑定/固定查询/永久删除/命令绑定/批量/重核对/验收/引擎核心）与被影响既有套件；
  W1 分支 206 passed；W2 分支 59 + 131 + 119 passed 与真机 20/20；W3 分支 10 + 7 + 受影响套件全绿；W4 分支 39 用例（集成后去掉 xfail 全绿）。
- **全量 pytest（本轮预算最多 2 次）**：见 §7 运行记录（最终树）。
- **完整 CI（本轮预算最多 2 次）**：targeted-verify targets=all（等价 ci.yml），run id/结论见 §7。
- 失败后只重跑失败/直接影响项，不重复整套；未用跳过/放宽测试取得通过。

## 5. 判定路径审计

- docs/r9g-reports/W4-audit.md：9 个判定入口 + 5 个批准/恢复入口 + 8 个执行点的审计表（判定依据/绑定字段/执行点/缺口）。
- scripts/audit_r9g_decision_paths.py：静态（关键词模块/符号必须不存在；固定查询/删除/重核对/批量/解析模块必须存在）+ 动态（真实 create_app：固定查询只读允许且无审批、批量两项两个结论、RecheckService 通过、缺绑定审批批准后 invalidated、判定审计有记录）。证据：_r9g-evidence/lead/audit_r9g_decision_paths.json。
- 审计结论：所有授权判定走统一 AuthorizationEngine；判定记录含 mode/effect/verdict/reason/requirement 绑定；最终执行点（工具/审批恢复/批量子项/子智能体/重启）接入执行前重核对；未发现第二条绕过统一入口的授权路径。

## 6. 历史数据兼容与边界

- 迁移只追加（57/58），不改历史迁移；旧审批/授权记录缺 requirement 绑定字段时不默认补成宽泛有效授权：执行前重核对会以 invalidated/needs_confirmation 处理并写明原因（W4 用例实测），不静默清空用户记录、不重置模式、不扩大跨重启授权。
- 旧版纯文本 limits 不参与新判定（避免关键词误判）；结构化 limits 在本轮上下文生效，跨重启持久化受当前 schema 限制（需要在后续迁移追加结构化列），已在报告如实标注。
- 旧关键词相关数据可保留只读，但不再参与任何判定。

## 7. 未验证/未完成与能力边界（诚实清单）

- 运行期不是安全沙箱：Low IL 只阻止写、不阻止读/网络；POSIX 无 Low IL；解释器内联代码、xargs、sudo、嵌套 shell、shell 展开等无法结构性约束，按普通审批流程处理，不宣称任意程序副作用受强制约束。
- 固定查询身份依赖系统程序路径与 ACL；同名替身/可写临时程序路径已拒绝，但被系统 ACL 篡改的 .cmd/.bat 等边界未全部覆盖。
- 结构化 limits 跨重启持久化未实现（schema 限制），重启后仅依赖要求绑定与重新审查；旧文本限制不解析。
- 前端只做只读文案检查（未改前端代码、未跑前端构建）：审批界面未暴露实现术语；W4 建议两处措辞优化未应用。
- 安装版/真实桌面路径未复验；真实模型评估为固定合成案例（W1：审查 26 例、解析 12 例），未做多模型/多次稳定性统计。
- 条件请求：模型理解条件并进入审查/条件证据核对；条件无法可靠证实时不执行（重核对 condition 检查），但当前主要覆盖结构化条件，复杂跨步骤条件由模型语义 + 审查承担，未做形式化验证。
- 本轮全量 pytest/CI 具体运行号、计数与结论见下方（由 Lead 在结果返回后补记）。