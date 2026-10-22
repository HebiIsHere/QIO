# R9-F 修复合并说明（Lead）

> 基线：`r9/main` = `37a2d26b94df113b45fbc8d0702801c2d1a06cf3`（代码父提交 `b2d9229f`）。
> 修复分支：`r9f/main`（worktree `D:\qio-dev\qio-r9f-main`）。
> 冻结契约：`docs/r9f-contracts.md`；验收矩阵：`docs/r9f-acceptance-matrix.md`。
> 纪律：不合并 main、不发布安装包；本轮只做失败项与直接受影响项定向验证，未跑全量 pytest/完整 CI（按用户要求）。

## 1. 分支与提交

| 分支 / worktree | 负责 | 最终提交 |
| --- | --- | --- |
| `r9f/main` / qio-r9f-main | Lead：schema/迁移、engine/gateway/app 接线、用户禁止、测量方式修正、集成与文档 | 见文末最终记录 |
| `r9f/w1-review` / qio-r9f-w1 | W1：模型授权审查、需求台账、授权记录生命周期、真实模型评估 | `486631439a19bcede73e2cb465df506ea09441e9` |
| `r9f/w2-exec` / qio-r9f-w2 | W2：统一受控启动、cmd/shell/自建工具/审批后重放 | 见 W2 报告与最终记录 |
| `r9f/w3-origin` / qio-r9f-w3 | W3：文件来源持久化与工作区自主操作 | `6de8e390` |
| `r9f/w4-verify` / qio-r9f-w4 | W4：真实链路 E2E、循环停顿诊断、定向验证清单 | `800feee969a206ac65b706f6b2ee065598d6f952` |

## 2. 四个问题的修复

- **A 普通程序/shell 无统一约束**：新增 `security/launch.py`（LaunchSpec/LaunchPermit/ControlledLauncher）；
  `cmd_tools` 的 run_program/run_cmd、自建工具 worker 启动、依赖安装路径统一经受控启动；执行前重算
  spec_digest（程序内容 sha256 / argv / cwd / 脚本内容 / shell COMSPEC 身份）与许可一致，执行后再核对；
  审批后重放先按 payload/fingerprint/对象身份/模式重核对，不一致 → `invalidated`，不执行；
  `_ApprovedReplayGateway.authorize()` 不再无条件 ALLOW；若干路径（无法可靠事前约束的副作用）明确
  “不支持/需确认”，不静默降级。事后 guard 只作补充审计，区分 QIO 自身写库/日志（按启动前快照具体条目）与越界。
- **B 关键词否定误授权**：`intent.extract_grants` 永久返回空（保留导入兼容），指令不再自动产生 grant；
  新增 `security/review.py`（ReviewRequest/ReviewOutcome/validate_outcome/ModelAuthorizationReviewer/
  ReviewAuditLedger）与 `security/requirements.py`（UserRequirement/RequirementLedger/AuthorizationRecord/
  AuthorizationLedger，迁移 55）。受限模式需要自然语言理解的操作 → 引擎渲染 `ReviewOperation` → 审查器一次
  独立模型调用（无工具/文件/设置能力）→ 后端校验（结构、requirement 真实且 active、对象/操作是请求子集、
  ALLOW 必须覆盖目标且给出 limits/purpose、否定/条件/引用材料确定性兜底）→ 有界授权记录 → ALLOW；
  CONFIRM → 普通审批；DENY → 拒绝（不转审批）；ReviewUnavailable/非法输出 → APPROVE（不默认允许、不切模式）。
  同 requirement + fingerprint 复用记录，不反复调用模型；新要求/新对象/新程序/模式变化 → 重新判断。
- **C 工作区来源不可达**：新增 `services/file_origins.py`（FileOriginStore，迁移 55 `file_origins`）；
  fs_write/fs_patch/fs_delete 授权前查后端来源并把核实值放 effect.detail；创建成功后记 qio_generated
  （已有文件不得冒充）；编辑保留来源；附件首次落盘/桌面登记/写回/回收/原子替换统一记账；批准后执行
  （approved_operations）写前快照、写后记账；外部替换不继承旧生成身份；重启后仍可查。
- **D 项目执行授权不匹配**：删除“目录命中就放行任意程序”的关键词路径；执行审查记录绑定
  program/argv/cwd/mode fingerprint；同命令复用记录不重复审批；新程序/新参数重新审查；目录/项目授权
  不放行目录写、系统变更或第三方外发。

## 3. 三模式与模型审查实际行为

| 模式 | 普通授权审查 | 固定规则 | 内部数据三连 | 用户明确禁止 |
| --- | --- | --- | --- | --- |
| read_only | 不调用 | 读允许；写/删/执行拒绝；普通审批不能绕过 | 要求（APPROVE） | DENY |
| restricted | 按需调用（固定规则先判：读取/QIO 受控/专用环境装依赖/已批准范围不调用） | 生效 | 要求（APPROVE） | DENY（优先于审查） |
| unrestricted | 不调用 | 跳过常规审批 | 要求（APPROVE） | DENY（遵守用户要求） |

- 模式变化：进入只读后新修改直接拒绝；进入无限制后普通新操作不再审查；已有待审批仍按原期限等待；
  过期/拒绝/取消不复活；执行前重新核对模式与授权有效性。
- 用户明确禁止由后端从用户要求原话/限制中识别（只做拒绝、不产生许可；误判方向为更保守）。
- 需求生命周期：每条用户消息一个 active requirement；后续新消息不继承旧记录；修正/替换/撤回使旧记录失效；
  同一要求的后台工作复用其范围；子智能体只取父级授权子集。

## 4. 数据迁移与恢复

- 迁移 55：`user_requirements` / `authorization_records` / `authorization_review_audit` / `file_origins`；
  补偿迁移 56（54 全部 + 55）；`REQUIRED_OBJECTS` 同步；`SCHEMA_VERSION = COMPENSATION_VERSION = 56`。
- 全部 IF NOT EXISTS，可重放；不修改历史迁移；重启后审查记录、需求、文件来源均可重建。
- 审批 v2 原语义不变：批准后重放不重问，但对象/程序/脚本/范围变化即失效。

## 5. 验证记录（定向，未跑全量）

- 修复分支定向批次（均 exit 0）：
  1) W1：`tests/test_r9f_requirements.py tests/test_r9f_review_validation.py tests/test_r9f_review.py`（94 passed）；
     既有关联安全+提示词契约测试（100 passed）。
  2) Lead 引擎接入：`tests/test_r9f_engine_review.py`（9 passed：三态/不可用/无效输出/复用/新要求/只读与无限制不审查/来源自主/执行 fingerprint）。
  3) W3：`tests/test_r9f_origin_store.py tests/test_r9f_origin_fs_tools.py tests/test_r9f_origin_attachments.py`（16 passed）+ 相关附件/FS 既有套件。
  4) W2：`tests/test_r9f_exec_boundary.py tests/test_r9f_approved_replay.py` 及受影响 cmd/sandbox/approval 套件。
  5) W4：`tests/test_r9f_acceptance_e2e.py`（真实 create_app/引擎/注册表/工具；模型审查用 FakeAdapter；C/D/B 已转绿，A 待 W2 收紧）；
     `scripts/diagnose_loop_stall.py` 同机对照 30 次/格证据在 `_r9f-evidence/w4-loop-stall/`。
  6) 集成后 Lead 广域定向批次（31 个文件：R9-F 全部 + 授权/审批/执行/附件/沙箱/重负载响应性）exit 0。
- Windows 真机：`_r9f-evidence/w2-exec/`（执行边界 PASS/FAIL 明细与命令见 W2 报告）。
- 真实模型评估（W1）：18 条固定标注案例，模型 `deepseek/deepseek-v4.1-flash`（用户已配置主模型，经 `ctx.build_adapter`），
  完全一致 17、**误授权 0**、漏授权 0、不可用 0；证据 `_r9-evidence/w1/authorization-review-20261022-141842.json`。
- 响应性测量修正：把整轮窗口收窄为“与慢检索刺激段重叠的 gap”，保留 446ms 回归签名检测力；依据 W4 测量
  （offload 段 p50≈19ms/max≈58ms；压力下调度停顿可达 300ms+ 且栈在 selector/线程等待），未抬高 300ms 阈值。

## 6. 未完成 / 未验证（如实）

- 未跑全量 pytest/完整 CI（按本轮纪律）；CI 只派发与本轮改动直接相关的定向 job。
- 安装版自建工具“重启恢复 + 批准后执行”未复验（A-075 路径）；安装包未重打/未发布。
- POSIX 无 Low IL 强制，只有事后核验；任意脚本的间接副作用无法全部事前约束；模型审查为单模型单次 18 条案例，
  未做多模型/多次稳定性统计；并发审查未验证。
- 未合并 main、未发布安装包。

## 7. 服务端接线位置（便于审查）

- `agent/security/engine.py`：restricted 的 APPROVE 候选 → `_apply_review`（复用 ledger / 调 reviewer / validate /
  record）；`_enrich_origin` 用 `origin_provider` 覆盖来源；`_user_prohibition` 三模式生效。
- `agent/services/app.py`：构造 RequirementLedger/AuthorizationLedger/ReviewAuditLedger/ModelAuthorizationReviewer，
  注入引擎；每轮用户消息 `record_user_message`；`origin_provider` 查 W3 store。
- `agent/services/file_origins.py`：store 与进程级查询；`AttachmentsService` 设为默认 store；
  `server.py` 注册 `origins` 供 fs_tools 注入。
- `agent/tools/approved_operations.py`：批准后 fs 写入/删除/附件写回/回收的来源记账；
  proc.* 重放走 `ControlledLauncher` 与重核对。

## 8. 最终记录

- 最终 SHA：见 tag/交付说明（推送后补）；CI 定向运行号与状态：见交付说明。
