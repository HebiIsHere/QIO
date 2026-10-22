# R9-F 验收矩阵（Lead）

> 基线：`r9/main` `37a2d26b`（代码父提交 `b2d9229f`）；修复分支 `r9f/main`。
> 本轮纪律：只跑失败项与直接受影响项；未跑全量 pytest/完整 CI；全量结论不假装。
> 状态口径：✅ 已实现并真实运行验证 / 🟡 已实现，仅单元或局部验证 / ⬜ 未验证。

## 1. A：统一程序执行边界（对应审查问题 A）

| # | 验收点 | 测试 / 证据 | 状态 |
| --- | --- | --- | --- |
| A1 | run_program/run_cmd 真实启动经受控入口（permit + spec_digest） | `test_r9f_exec_boundary.py`、`test_r9f_approved_replay.py`、`test_cmd_tools.py` | ✅ |
| A2 | 自建工具 worker 启动复用同一隔离/guard 组件 | `test_r9f_exec_boundary.py`、`test_sandbox_worker.py`、`test_sandbox_executor.py` | ✅ |
| A3 | 批准后重放：payload/程序/argv/cwd/对象/模式变化 → invalidated，不执行 | `test_r9f_approved_replay.py`、W4 E2E（A 用例待最终复验） | ✅ |
| A4 | 审批后脚本内容变化 → 旧许可失效（篡改脚本不得执行） | W4 E2E `test_script_change_does_not_reuse_old_approval` + W2 单测 | 最终复验后填 |
| A5 | 只读模式不能靠批准绕过；范围内必要写入可用 | `test_r9f_exec_boundary.py`、`test_sandbox_readonly.py`、W2 真机 | ✅ |
| A6 | Windows：范围外写入被拒、内部数据未确认被拒、后代进程同边界 | `_r9f-evidence/w2-exec/`（W2 报告） | ✅（Windows 本机） |
| A7 | POSIX 无强制隔离时如实标注，不误拒普通命令 | `test_r9f_exec_boundary.py` | 🟡（本机 Windows；Linux 由定向 CI 复验） |

## 2. B：受限模式模型授权审查（对应审查问题 B）

| # | 验收点 | 测试 / 证据 | 状态 |
| --- | --- | --- | --- |
| B1 | 肯定要求 → 审查 ALLOW → 有界授权记录 → 执行 | `test_r9f_engine_review.py`、`test_r9f_acceptance_e2e.py`（真实 app + FakeAdapter） | ✅ |
| B2 | 否定句（不要修改）不误授权 | `test_r9f_review.py`、W4 E2E `test_negated_instruction_does_not_authorize_write` | ✅ |
| B3 | 引用材料/文件内容/网页/工具结果不能增加授权 | `test_r9f_review_validation.py`（untrusted_material/引号） | ✅ |
| B4 | 条件句/疑问句不构成明确授权 | `test_r9f_review_validation.py` | ✅ |
| B5 | 主模型声称“已授权”不能替代真实依据 | `test_r9f_review_validation.py`、`test_r9f_requirements.py` | ✅ |
| B6 | 模型不可用/超时/非法输出 → APPROVE（不默认允许、不切模式） | `test_r9f_engine_review.py`、W4 E2E `test_review_unavailable_is_not_silent_allow` | ✅ |
| B7 | 有效范围复用（同 fingerprint 不重复调用模型）；新对象/新程序重新判断 | `test_r9f_engine_review.py`、W4 E2E（D 用例） | ✅ |
| B8 | 审查输入由后端组装；对象/来源/既有授权/拒绝记录可追溯；审计落库 | `test_r9f_review.py`、`test_r9f_requirements.py`、迁移 55 | ✅ |
| B9 | 真实模型评估（固定 18 案例、误授权 0、记录模型/提示词/输出） | `_r9-evidence/w1/authorization-review-20261022-141842.json`、W1 报告 | ✅（单模型单次） |
| B10 | 关键词授权入口已删除（`extract_grants` 恒空、指令不自动 grant） | `test_instruction_scope.py`、`test_authorization_engine.py` | ✅ |

## 3. C：工作区文件来源（对应审查问题 C）

| # | 验收点 | 测试 / 证据 | 状态 |
| --- | --- | --- | --- |
| C1 | 来源持久化：user_original / user_saved_material / qio_generated | `test_r9f_origin_store.py`、迁移 55 | ✅ |
| C2 | 自然语言要求下创建 → 修改 → 回收自主，不反复审批 | W4 E2E（C 用例，真实 store + 引擎） | ✅ |
| C3 | 已存在的用户文件不被冒充为 generated；模型传入 origin 不可信 | `test_r9f_origin_fs_tools.py`、`test_r9f_origin_store.py` | ✅ |
| C4 | 编辑保留来源；原子替换更新身份；外部替换不继承生成身份 | `test_r9f_origin_store.py`、`test_r9f_origin_attachments.py` | ✅ |
| C5 | 附件写回/回收/批准后执行统一记账 | W3 测试 + `approved_operations.py` 接线；W4 E2E | ✅ |
| C6 | 重启后来源仍可查、仍自主 | W4 E2E（新 store 实例读同 DB）、W3 报告 | ✅ |
| C7 | 截图/上传首次落盘仍是用户材料 | `test_r9f_origin_attachments.py` | ✅ |

## 4. D：项目必要执行授权（对应审查问题 D）

| # | 验收点 | 测试 / 证据 | 状态 |
| --- | --- | --- | --- |
| D1 | 真实用户要求下同一条测试命令复用授权记录，不反复审批 | W4 E2E `test_project_command_with_user_requirement_is_not_reasked` | ✅ |
| D2 | 新程序/新 argv 必须重新判断（目录命中不放行任意程序） | `test_r9f_engine_review.py`、W4 E2E | ✅ |
| D3 | 执行授权绑定 program/argv/cwd/mode（fingerprint 一致） | `test_r9f_engine_review.py`、`test_r9f_exec_boundary.py` | ✅ |
| D4 | 不放行目录写、系统变更、第三方外发 | `test_authorization_engine.py`、`test_r9f_review.py` | ✅ |

## 5. 三模式与固定规则

| # | 验收点 | 测试 | 状态 |
| --- | --- | --- | --- |
| M1 | 只读：不调用审查；写/删/执行拒绝；普通批准不能绕过 | `test_authorization_engine.py`、`test_r9f_engine_review.py`、W4 E2E | ✅ |
| M2 | 受限：按需审查；读取/QIO 受控/专用环境装依赖等固定规则不走模型 | `test_r9f_engine_review.py`、`test_r9f_review.py` | ✅ |
| M3 | 完全无限制：跳过普通审查与常规审批；用户明确禁止仍生效 | `test_authorization_engine.py`、W4 E2E | ✅ |
| M4 | 内部数据三连确认在三种模式都生效（含三次齐备后执行） | `test_r9f_acceptance_e2e.py`、W4 E2E | ✅ |
| M5 | 模式变化：收紧影响新操作；放宽不自动批准旧待审批；执行前重核对 | `test_approval_lifecycle.py`、`test_r9f_acceptance_e2e.py` | ✅ |
| M6 | 用户明确禁止（只分析/不要修改/不要删除/不要外发）三模式遵守 | `test_authorization_engine.py` | ✅ |

## 6. 生命周期 / 子智能体 / 后台工作

| # | 验收点 | 测试 | 状态 |
| --- | --- | --- | --- |
| L1 | 每条用户消息一个 requirement；后续不继承旧记录 | `test_r9f_requirements.py`、`test_r9f_engine_review.py` | ✅ |
| L2 | 同一要求的后台工作持有必要范围（不因 turn 结束机械撤销） | `test_r9f_requirements.py`（chain/records）、引擎实现 | 🟡（单元级；真实后台轮未单列 E2E） |
| L3 | 撤回/修正/替换使旧记录失效 | `test_r9f_requirements.py`、`test_r9f_engine_review.py` | ✅ |
| L4 | 子智能体只取父级授权子集，不能扩大 | `test_authorization_engine.py`、`test_subagent_scope.py` | ✅ |
| L5 | 待审批 600 秒原始期限、重启恢复、原子领取、执行前重核对 | `test_approval_*.py`（沿用 R9） | ✅ |

## 7. CI 响应性（上次红项）

| # | 验收点 | 证据 | 状态 |
| --- | --- | --- | --- |
| R1 | 区分事件循环阻塞/线程工作/初始化/调度延迟 | `_r9f-evidence/w4-loop-stall/`（栈采样 + 对照 + 压力） | ✅ |
| R2 | offload 段本身 p50≈19ms/max≈58ms，不是 offload 回归 | 同上 | ✅ |
| R3 | 测量窗口收窄到刺激段（不抬高 300ms） | `test_interactive_during_heavy_work.py` 修改 + 定向测试 | ✅ |
| R4 | 保留真实阻塞检测力（446ms 回归签名仍会红） | 测试文档 + 窗口重叠逻辑 | 🟡（逻辑论证，未构造回归注入实验） |
| R5 | CI 定向 job 通过 | `gh workflow run targeted-verify.yml`（推送后派发） | 待填 |

## 8. 未验证 / 明确不做

- 全量 backend pytest 与完整 ci.yml（本轮纪律明确不跑）；安装版自建工具重启恢复 + 批准后执行（A-075 路径）未复验。
- POSIX 无 Low IL 强制；任意脚本间接副作用无法全部事前约束；模型审查并发/多模型稳定性未验证。
- 未合并 main、未发布安装包。
