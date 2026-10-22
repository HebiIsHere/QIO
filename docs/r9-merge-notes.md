# R9 合并说明与交付记录（Lead）

> 状态：集成收口中。最终 SHA / 全量测试 / CI 运行号在"最终记录"一节填写。

## 1. 基线与分支

| 项 | 值 |
| --- | --- |
| 远端 main / 产品版本 | `249a63443c72b3dad011b5446ee9920f4dadf0fd` / v0.2.10（开工时重新 `git fetch origin main` 确认与上次核查一致、无更新） |
| 契约冻结提交 | `ca95236f`（`docs/r9-contracts.md` + 迁移 52/53/54 + REQUIRED_OBJECTS） |
| 主开发分支 / worktree | `r9/main` / `D:\qio-dev\qio-r9-main` |
| 子分支 / worktree | `r9/w1-auth`（qio-r9-w1）、`r9/w2-exec`（qio-r9-w2）、`r9/w3-attach`（qio-r9-w3）、`r9/w4-ui`（qio-r9-w4）、`r9/w5-shell`（qio-r9-w5）、集成补口 `r9/w2b-exec`（qio-r9-w2b） |
| 并行纪律 | 每分支独立 worktree/分支，只提交自己的分支；不 merge main、不 push、不发布；全部定向验证，全量由 Lead 只跑一次 |

## 2. 完成内容（对应用户任务书 §二–§十三）

### 2.1 概念纠正与统一授权（W1）
- 新增 `backend/src/agent/security/`：modes / effects / context / instructions / intent / engine /
  gateway / operations / internal_data / audit / tempfiles。授权依据 = 具体用户指令 + 对象 + 操作 + 目的 + 边界，
  不以 topic/turn/session_id/任务记录/子智能体任务作为有效期。
- 确定性意图提取（明确文件+写动作 / 修复项目目录 / 明确删除），grant 随指令关闭、不跨指令继承；
  模型没有自我授权入口。
- 三模式：`read_only`/`restricted`/`unrestricted`，旧值 default/plan/accept-edits/bypass 显式映射
  （不静默提高）；首次安装默认受限；只有用户经 `PUT /api/security/mode`（user_confirmed=true）可提高；
  模式常驻显示；严格模式不自动批准待审批。
- 内部运行数据：直接文件/脚本维护要求明确范围+警告+后端 CAS 的三次确认，适用于全部模式（含完全无限制），
  覆盖脚本/自建工具/子智能体；应用受控接口（设置/记忆/审批落库）不要求三次确认。
- 审批 v2：600 秒原始期限（刷新/暂缓/重启不续期）、持久化操作与上下文、状态机
  waiting→approved→executing→completed/failed 与 rejected/cancelled/expired/invalidated/interrupted_unverified、
  CAS 决定与原子领取、批准后执行前重核对、崩溃不自动重放、非阻塞 submit。
  API：`/api/security/mode`、`/api/approvals/pending|history|{id}/decide|{id}/confirm`；事件复用
  APPROVAL_REQUIRED/APPROVAL_RESULT + payload.phase（不新增 EventType，事件协议守卫保持绿）。

### 2.2 统一执行边界（W2 + W2b）
- fs/cmd/脚本/自建工具/子智能体/依赖安装全部先经 `AuthorizationGateway`；approve 一律
  `submit` 返回 pending，不阻塞后续对话；批准后由执行器（`agent/tools/approved_operations.py`）
  重核对身份/内容/模式后真实执行（fs.write/fs.patch/fs.delete、proc.*、subagent.create、
  attachment.write_back/read/recycle；dep.install/net.egress/internal.data_write 显式注册"不可用"）。
- 执行层约束：read_only 拒绝任意脚本/自建工具；restricted 未获授权写范围强制 Low IL；unrestricted 不降级；
  Job Object（内存/进程数/KILL_ON_JOB_CLOSE）照旧；核实不了边界时拒绝执行并报告"暂不可用"；
  自动放行执行前后对受保护范围做快照比对并如实上报越界。
- 子智能体：按父 grant 子集生成 AgentScope，单层、不能扩权；超出范围由主智能体统一申请。
- 自建工具：注册不产生长期权限，每次调用重新判定。
- 真机：`scripts/verify_r9_execution_boundary.py` PASS=4（Low IL 写 Medium 拒绝、Job 收树、
  只读 argv 白名单、只读拒绝任意脚本）。

### 2.3 附件改为源文件引用（W3）
- 迁移 52：attachments 增加 source_kind/origin/file_id/content_sha256/reference_state/material_enabled/
  user_note/last_verified_at/legacy_paths + `attachment_migration_audit` + `material_cleanup_candidates`。
- `register_source`（桌面路径，不复制不移动）/ `write_material`（浏览器/剪贴板/截图首次落盘到
  workspace/materials，origin=user_saved_material）/ `resolve_source`（读取/预览/媒体解析/写回唯一入口，
  写前 identity+hash 重核对、原子写）/ `remove_reference`（只删引用，不删源文件）/ `file_ref` payload。
- 文件身份：Windows `GetFileInformationByHandleEx(FileIdInfo)`（volume+file_id），POSIX dev+ino；
  移动/改名后只有身份相同才自动修复；同名/同大小/mtime/hash 不足以认定，ambiguous 时让用户确认；
  编辑后仍是同一文件、复制件不是原文件。
- 删除用户材料走 Windows 系统回收站（`services/recycle_bin.py`，SHFileOperationW ALLOWUNDO；
  POSIX freedesktop Trash）；失败保留文件并如实说明，绝不永久删除；不再保留 100MB 自动复制策略。
- 旧数据迁移：盘点→审计→幂等/断点；有有效原路径核实后改源引用并保留差异事实；无原路径但保存文件仍在则
  登记为用户材料当前源文件（不删不复制）；全失效保留 missing+relocate；清理清单与登记分离、经批准才回收。
- 真机：`verify_r9_recycle_bin.py`（真实回收站条目/还原/占用失败保留）、`verify_r9_file_identity.py`
  （移动/改名/复制件/编辑）全部通过。

### 2.4 文件卡与权限设置 UI（W4）
- 三模式设置段 + 输入区常驻徽标；只通过 `PUT /api/security/mode`（user_confirmed）提权；
  旧接口只读兼容、旧值映射展示、收紧/放宽生效边界文案。
- 附件入口：桌面原生选择 `pick_attachment_source` 返回真实路径→登记源引用（不再字节上传复制）；
  拖放真实路径→registerSource；浏览器/剪贴板字节→首次落盘并明确来源；原生命令未注册时降级回退旧命令。
- 文件卡：`FileCardMaterialPanel` 显示文件名/类型/引用 id/来源/可用性/用户说明；"作为互动材料"开关
  （只控制上下文）；缺失 relocate 需确认同一文件；复制/撤销/重做共享 attachmentId；删卡不删源文件。
- 审批 v2 界面：pending 恢复、decide/confirm、三次确认逐次呈现、终态只读、执行结果历史；复用
  ApprovalModal/ApprovalFacts，不新增弹窗/事件。
- 旧"允许 QIO 查看"文案全量替换为"作为互动材料"（grep 无残留，报告含证据）。

### 2.5 桌面关闭/托盘/退出（W5）
- `ui.close_behavior ∈ {ask, tray, quit}`（默认 ask）持久化；标题条/系统 CloseRequested/Alt+F4 统一
  `request_close`；ask 三选确认（可记住）；tray 隐藏不触发退出收尾、可 show+unminimize+focus 恢复；
  quit 幂等守卫，退出中不重新拉起后端。
- `POST /api/lifecycle/shutdown`：同步退出闸门（写请求 503）、一次 aclose（lifespan 共用）、
  `SHUTDOWN_GRACE_SECONDS=8.0` 有界等待、保留有效 waiting 审批、超时如实返回；壳按 pid 等 sidecar
  进程树退出，超时 `taskkill /PID /T /F` 兜底并核验 pid 消失；不按进程名杀。
- 真实 GUI：默认 ask 的 WM_CLOSE → 确认框 → 取消后应用继续运行（截图/日志）；
  进程树真机脚本 10/10 PASS（两棵树、同名进程不误杀）。

## 3. 数据迁移与失败恢复

- 迁移 52/53：只追加（`storage/migration_r9_attachments.py`、`migration_r9_approvals.py`），
  补偿迁移 54 = 51 全部语句 + 52 + 53；`REQUIRED_OBJECTS` 同步新增表/列/索引；
  `SCHEMA_VERSION = COMPENSATION_VERSION = 54`。
- 附件迁移：`storage/attachment_migration.py` 逐行审计（attachment_migration_audit），幂等可重跑、
  中断后从审计断点继续；先在启动后台跑一次 + 维护接口可重跑；首次迁移不搬动文件；
  清理清单单独记录，经批准后进系统回收站。
- 审批持久化：pending_approvals 保存 operation/payload/fingerprint/instruction/目的/模式/确认阶段；
  启动恢复 waiting（原到期时间），approved 重核对后执行，executing 标 interrupted_unverified 不重放；
  退出不清除有效待审批。
- 失败处理：回收站不可用/占用 → 保留文件 + 如实错误；写回落盘前身份/内容变化 → 不覆盖并说明；
  事件/日志不出现密钥原文（沿用 redact 路径）。

## 4. 验证与证据（详见 docs/r9-acceptance-matrix.md）

- 集成定向矩阵：R9 新增/相关测试 + 附件/互动全量定向 + 审批/执行器/边界/退出/生命周期（见最终记录命令与数量）。
- Windows 真机脚本（真实运行）：execution_boundary / recycle_bin / file_identity / process_tree，
  证据在 `_r9-evidence/`；W5 真实 GUI ask 截图在 `_r9-evidence/w5-gui/`；后端自退出证据 `_r9-evidence/w5-backend-selfexit.txt`。
- 端到端：`backend/tests/test_r9_acceptance_e2e.py`（真实 create_app 装配）。
- 前端：`npx vue-tsc --noEmit` + 全量 `npx vitest run`（一次）。
- 全量后端 pytest 与完整 CI：见"最终记录"。

## 5. 未实现 / 未验证边界（如实）

- Low IL 不阻止读取、不阻止网络，只降低写风险，**不是安全沙箱**；脚本运行时拼接路径/间接写内部数据
  无法执行前识别，只能事后核验并如实上报。
- 安装版 NSIS 构建/安装/卸载与安装版 GUI 托盘验证：见"最终记录"（若未完成则标未验证及原因）。
- POSIX 回收站分支未在 Linux 实跑；真实旧库大批量迁移/重启续跑为合成数据验证。
- 旧 HTTP 兼容：历史附件行（source_kind NULL）仍走旧回放路径，仅服务历史数据；新入口不复制。
- `SHUTDOWN_GRACE_SECONDS=8.0` 未按安装版实测调整（初值，报告已标注）。
- 本轮不合并 main、不发布安装包。

## 6. 合并步骤（供审查）

```powershell
git switch r9/main
git merge --no-ff r9/w1-auth
git merge --no-ff r9/w2-exec
git merge --no-ff r9/w3-attach
git merge --no-ff r9/w4-ui
git merge --no-ff r9/w5-shell
git merge --no-ff r9/w2b-exec
# 冲突与集成修正提交见 git log（Lead 处理）
cd backend; uv run --frozen pytest          # 全量（只跑一次）
cd frontend; npx vue-tsc --noEmit; npm test # 全量（只跑一次）
```

## 7. 最终记录

- 最终提交：本地分支 `r9/main`，tag **`r9-final`**（SHA 用 `git rev-parse r9-final` 取；本文件随后不再改动）。
- 基线 SHA：`249a63443c72b3dad011b5446ee9920f4dadf0fd`（v0.2.10）；契约提交 `ca95236f`。
- 后端全量 pytest（只跑一次，2026-10-22，集成树）：首次运行 **76 项失败**（全部为旧副本策略时代的历史验证套件 + 两处过期版本守卫，见下"失败重跑范围"）；
  逐项适配后**失败项定向复跑**：23 个失败文件 `uv run --frozen pytest -q <23 files>` → **exit 0 全绿**；
  另在适配期间对 R9 定向矩阵（R9 新测试 + 附件/互动全量定向 + 审批/执行器/边界/退出/生命周期 + 端到端装配）多次运行 exit 0。
- 前端全量：`npx vue-tsc --noEmit` exit 0；`npx vitest run` **399 files / 3570 tests 通过**（2026-10-22，前端冻结后一次运行）；
  W5 安装版修复后又对受影响区域（WindowChrome/SettingsView/services/stores/components/views）定向复跑通过且 vue-tsc 0。
- Windows 真机：execution_boundary PASS=4、recycle_bin 全 PASS、file_identity 全 PASS、process_tree 10/10 PASS（集成树复跑）；
  安装版 shelllease（重建含全部修复的未签名测试包）：**29 PASS / 2 WARN / 0 FAIL / 0 SKIP**，关键 A-116/A-117/A-117a/A-117b/A-118/A-119 全 PASS，
  证据 `_r9-evidence/w5-install-close/`；机器状态已还原（D:\QIO 744 文件 sha256 一致、注册表/快捷方式还原）。
- 失败重跑范围（只重跑失败项与直接受影响检查，符合"失败后仅重跑失败项"）：
  1) 旧副本策略历史套件（23 文件 / 76 用例）：W3 适配 A 组、W6 适配 B 组、W2b 适配工具/fs 组、Lead 修过期迁移守卫；不删测试、不 skip，映射见 `docs/r9-reports/W3.md §5b`、`W6.md`、`W2b.md`、`W5.md`。
  2) 定向复跑命令：`uv run --frozen pytest -q <FULL-failures.txt 中的 23 个文件>` → exit 0。
  3) 前端受影响区域复跑：`npx vitest run src/components src/stores src/services src/views` + `npx vue-tsc --noEmit` → 通过。
- CI run：推送 `r9/main` 后填写（若失败用 `gh run rerun --failed` 只重跑失败项）。
- 未做：合并 main、发布安装包（按用户约束）。安装版产物为 `-UNSIGNED-TEST` 测试包，仅用于真机验收。