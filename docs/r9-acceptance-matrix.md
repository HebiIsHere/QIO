# R9 验收矩阵（Lead 汇总）

> 依据：docs/r9-contracts.md §12 与任务书 §十五。状态口径：`已实现` / `已运行验证`（命令与输出证据）/
> `仅源码检查` / `未完成` / `未验证`（说明原因）。证据目录：`_r9-evidence/`；命令与全量记录见
> `docs/r9-merge-notes.md`。基线 `249a6344`（v0.2.10）；契约提交 `ca95236f`。

## 一、验收矩阵

| # | 验收项 | 负责 | 实现与证据 | 状态 |
| --- | --- | --- | --- | --- |
| 1 | 桌面聊天附件与文件卡均引用源文件，小文件不再自动复制 | W3/W4 | 已实现：register_source / source_kind 登记不复制；新增测试覆盖；W4 原生 picker 走真实路径登记。 | 已实现+定向验证 |
| 2 | 无路径材料只首次落盘，正确记录为用户材料 | W3/W4 | write_material → workspace/materials，origin=user_saved_material；浏览器/剪贴板入口文案与 payload 标注。 | 已实现+定向验证 |
| 3 | 文件卡能让 QIO 定位材料并按需读取 | W3/W4 | board_tools/file_ref + attachment payload 暴露文件名/类型/引用 id/来源/可用性/说明；read_attachment 走 resolve_source。 | 已实现+定向验证 |
| 4 | 预览、读取、获准修改使用同一源引用 | W3 | preview/preview_routes/materials/attachment_tools 全部收敛到 resolve_source（报告含调用点表）。 | 已实现+定向验证 |
| 5 | 删卡不删文件；复制、撤销、重做不破坏共享引用 | W4/W3 | board.ts 复制共享 attachmentId；删卡软删除；源文件保留测试（test_file_cards_refs / 前端 board 测试）。 | 已实现+定向验证 |
| 6 | 文件移动后可确认同一文件才修复；同名不同文件不误认 | W3 | file_identity volume+file_id；同名/同大小/mtime/hash → ambiguous 需用户确认。真机 verify_r9_file_identity.py 通过。 | 已实现+真机验证 |
| 7 | 明确指令授权不重复审批；后续其他要求不自动继承外部目录写权限 | W1/W2 | intent 确定性提取 + grant 随指令关闭；test_instruction_scope + E2E test_r9_acceptance_e2e。 | 已实现+E2E 验证 |
| 8 | 只读模式无法经脚本、自建工具和后代进程写入业务文件 | W2 | read_only 拒绝任意脚本/自建工具；run_program 白名单；Low IL 写 Medium 被拒。verify_r9_execution_boundary.py PASS=4。 | 已实现+真机验证 |
| 9 | 受限模式的执行和外发不超出批准范围 | W1/W2 | 授权网关统一判定；待审批操作持久化 scope/fingerprint；执行前重核对。 | 已实现+定向验证 |
| 10 | 完全无限制模式仍保留内部运行数据三次确认 | W1 | engine 全模式三次确认 + CAS；E2E test_unrestricted_still_requires_three_confirmations_for_internal_data。 | 已实现+E2E 验证 |
| 11 | 子智能体无法扩大已有授权，自建工具注册不成为长期权限 | W2 | child_scope = 父 grant 子集；自建工具逐调用授权；test_subagent_scope。 | 已实现+定向验证 |
| 12 | 用户材料删除先按规则审批并进入系统回收站；失败不永久删除 | W3/W2b | DELETE effect + 待删除清单审批；recycle_bin SHFileOperationW；失败保留。verify_r9_recycle_bin.py 真机通过。 | 已实现+真机验证 |
| 13 | 审批原始 10 分钟期限跨刷新、暂缓和重启不变 | W1 | 600s created_at+original expires_at 持久化；刷新/重启不续期；test_approval_lifecycle/restart。 | 已实现+定向验证 |
| 14 | 待审批时可继续处理不相关要求 | W1 | submit 持久化后立即返回 pending，不占 active turn；E2E 验证工具立即返回、文件未变。 | 已实现+E2E 验证 |
| 15 | 重复批准、对象变化、执行中崩溃不会造成危险重复执行 | W1 | CAS 决定/原子领取/重核对；executing 崩溃 → interrupted_unverified 不自动重放。 | 已实现+定向验证 |
| 16 | 严格模式只限制后续新操作，宽松模式不自动批准旧请求 | W1 | 执行前 mode 重核对；待审批不因放宽自动批准；test_security_modes/approval。 | 已实现+定向验证 |
| 17 | 旧附件迁移不丢失唯一内容，迁移中断后可恢复 | W3 | attachment_migration 审计+幂等断点；有原路径/无原路径/全失效分支；test_attachment_migration。 | 已实现+定向验证 |
| 18 | 安装后无需用户额外配置即可运行基础脚本与自建工具 | W2/W5 | 内置 Python 运行时（自检 4/4）随安装包；基础脚本与自建工具在本地测试与安装版 shelllease 启动中可用。 | 已实现+安装版启动验证；依赖型工具未在安装版复跑 |
| 19 | 托盘隐藏后持续运行并可恢复窗口 | W5 | **安装版真机**：A-117a PASS（tray WM_CLOSE → 窗口隐藏、壳/sidecar/lease 存活）、A-117b PASS（OS 层 ShowWindow 恢复可见）；ask 确认框有真实 GUI 截图。托盘菜单人工左键恢复未自动化。 | 已实现+安装版真机验证（人工托盘点击除外） |
| 20 | 真正退出结束本实例管理的运行，其他实例和无关程序不受影响 | W5 | 按 pid/Job Object；真机 verify_r9_process_tree.py 10/10 PASS；后端自退出 exit=0。 | 已实现+真机验证 |
| 21 | 写库、准备附件、运行脚本及等审批时退出，状态真实且可恢复 | W5/W1/W3 | /api/lifecycle/shutdown 有界收尾+保留 waiting；审批 v2 重启恢复；安装版退出验证见收口。 | 部分：源码+定向；安装版待验 |

## 二、Windows 真机证据（集成树实测）

| 脚本 | 结论 | 证据 |
| --- | --- | --- |
| `scripts/verify_r9_execution_boundary.py` | PASS=4 FAIL=0：只读拒绝脚本、Low IL 写 Medium 文件 PermissionError、Job Object 收进程树、只读 argv 白名单 | `_r9-evidence/w2-execution-boundary.txt` |
| `scripts/verify_r9_recycle_bin.py` | 全部通过：真实 $I/$R 回收站条目、还原、占用失败保留（错误码 0x20，未永久删除） | `_r9-evidence/w3-recycle-bin.txt` |
| `scripts/verify_r9_file_identity.py` | 全部通过：Windows FileIdInfo 身份、移动/改名不变、复制件不同、编辑后仍是同一文件 | `_r9-evidence/w3-file-identity.txt` |
| `scripts/verify_r9_process_tree.py` | 10/10 PASS：两棵独立进程树、同名进程不误杀、只按 pid 名单清理、不存在的 pid 不误报 | `_r9-evidence/w5-process-tree.txt` |
| W5 真实 GUI（custom-protocol 调试产物 + 真后端 + WebView2） | 默认 ask：WM_CLOSE → 确认框 → 取消后应用继续运行（截图+日志） | `_r9-evidence/w5-gui/` |
| 后端自退出 | `POST /api/lifecycle/shutdown` → done/clean，进程 exit=0，lifespan 完成 db_closed | `_r9-evidence/w5-backend-selfexit.txt` |

## 三、端到端应用装配验证（Lead）

`backend/tests/test_r9_acceptance_e2e.py`：真实 `create_app` + ToolRegistry + ApprovalService：
只读拒绝且无待审批；受限写用户文件 → pending（文件未变）→ 用户批准 → 后台执行成功；
明确指令授权同轮直接写、指令结束后重新需要审批；无限制模式下内部运行数据仍要求三次确认。

## 四、全量测试与 CI

- 后端全量 pytest（2026-10-22，集成树，只跑一次）：首次运行 4640+ 项中 **76 项失败**，全部来自旧副本策略时代的历史验证套件与两处过期守卫；逐项归因后由 W3/W6/W2b/Lead 适配到 R9 新契约（不删测试、不 skip，旧断言→新断言映射见各报告），随后**失败项定向复跑（23 个文件）exit 0 全绿**。
- 前端全量：`npx vue-tsc --noEmit` exit 0；`npx vitest run` 399 files / 3570 tests 通过（W5 安装版修复后对受影响组件/服务定向复跑 216 项通过 + vue-tsc 0）。
- CI（GitHub Actions）：run `39771507126`（代码提交 `b2d9229f`）**9/10 jobs PASS**，含 docs、frontend、backend Linux 双版本、rust、install e2e（真实 NSIS 安装/启动）；唯一失败为 Windows 后端负载敏感计时用例（373ms vs 300ms 预算），已按规则 `gh run rerun --failed` 只重跑该 job。详见 `docs/r9-merge-notes.md` 第 7 节。

## 五、未验证 / 未完成边界

- 安装版完整 all-stages（api/dev/restart/reinstall/uninstall/coinstall）未复跑；shelllease 关键阶段 29 PASS / 0 FAIL。首轮 A-075（dev_add 重启后不出现）未在后续轮复跑，本地自建工具重启恢复+批准执行测试通过，安装版该项标"未验证"。
- 托盘菜单人工左键恢复/视觉外观未自动化（安装版只验证 OS 层窗口恢复与托盘隐藏）。
- SHUTDOWN_GRACE_SECONDS 仍为 8.0 初值（安装版实测退出 1–4s，未触发强制兜底）。
- Low IL 不阻止读取与网络，不是安全沙箱；脚本运行时拼接路径/间接写只能事后核验。
- POSIX 回收站分支未在 Linux 实跑（Windows 已实跑）。
- 旧附件迁移的真实大批量旧库/重启续跑仅合成数据与维护接口验证；首次迁移不搬动现存文件。