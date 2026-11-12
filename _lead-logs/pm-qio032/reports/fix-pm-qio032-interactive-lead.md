# PM-QIO032 互动模式修复 · Lead 交付报告

- 父分支：`fix/pm-qio032-interactive`（worktree `D:\qio-dev\pm032-interactive`）
- 审计基线 / 开工 SHA：`7445170d0f51756b2740eb7209d798540d8042a2`
- 分支与 worktree 检查：开工时同名父分支与 worktree 已存在（HEAD=基线、工作区干净），按用户要求
  **保留已有工作、未重建**；6 个子分支各自从基线新建 worktree，互不覆盖。
- 交付纪律：所有工作提交只进入本组分支；未 push、未触发远端 CI/PR/发布。

## 1. 子分支与合并

| 子分支 | worktree | 分支尖端（合并前） | 合并进父分支 |
| --- | --- | --- | --- |
| fix/pm-qio032-i-material | pm-qio032-i-material | 32908699 | e578a531 / 978958bc |
| fix/pm-qio032-i-submit | pm-qio032-i-submit | ab579d68 | 38e9dbdc / 603581b3 / 4174d480 带入 i-material |
| fix/pm-qio032-i-state | pm-qio032-i-state | 5e7b244f | e6deeaf7 / 6396d2a5 |
| fix/pm-qio032-i-canvas | pm-qio032-i-canvas | 10e20bf5 | 16235392 / 52f3831d |
| fix/pm-qio032-i-layout | pm-qio032-i-layout | 3cd5e695 | c7d553fe / 2ff8d579 |
| fix/pm-qio032-i-tasks | pm-qio032-i-tasks | 5fe32cd4 | 57f1c854 / 6f490059 / 3eac7fd8 |

Lead 集成修复（父分支）：`1da8eb64`（BoardToolbar 样式注释缺 `/*` 导致互动页动态 import 失败 —— E2E 组发现）、
互动数据契约的空提交门/失败重试/轮次状态口径调整（`store.interactive.ts`）、窗口帧契约
（InteractiveView 不再用裸 `window.innerWidth`）、合并冲突（i2v2-a-intent-read-failure 断言口径）与
5 个既有测试的「新行为断言」更新（#13 按钮禁用、#59 组收起显示状态提交、#07 失败入口迁移）。

每个子分支的报告在 `_lead-logs/pm-qio032/reports/`：
`fix-pm-qio032-i-material.md` / `...-i-submit.md` / `...-i-state.md` / `...-i-canvas.md` /
`...-i-layout.md` / `...-i-tasks.md`。

## 2. 逐项结果（按用户任务书编号）

已修复（产品代码 + 定向测试证据）：
#01（后端材料数据源 + turn_orchestrator 首轮交付/附件授权接线；浏览器级首轮请求捕获 UNVERIFIED）、
#02（后端板面工具无绑定拒绝 + 提交轮只读 A 版；前端按基线已验证）、
#03（后端真实轮次绑定/事件事实已有；前端 roundStatus 唯一主状态 + 单调守卫 + UI 一处呈现）、
#04（工具栏按可用矩形换边/内收）、#06（基线已满足 + 契约说明与测试证据）、
#07（三类读取失败独立、任务入口一处标记、面板一处可重试）、
#08/#54（三态唯一入口：显示并参与/显示但不参与/隐藏且不参与；后端 card_access_state 唯一映射 +
  提交/关系范围/工具读卡/附件读取同一授权；恢复隐藏默认只回显示但不参与；旧卡迁移不扩大范围）、
#09（关系端点用户命名 > 文件名 > 非占位，同名第 N 张）、#13（后端空白卡不计/关系删除计入；
  前端 canSubmit/submitBlockedReason 与按钮禁用提示）、#14、#15、#16、#18、#20（数据门禁 + 页面只读/重试/返回对话）、
#26（后端 INTERACTIVE_INTENTS_CHANGED 事件 + 前端幂等订阅/虚线卡/任务数，端到端 UNVERIFIED）、
#27（后端 authorization_invalidated/authorization_changes 真实差异 + 前端待确认/需重新确认展示）、
#28（提交轮只读 A 版快照：board_inspect/read_cards/read_attachment 三路一致；源文件变化提示重新提交）、
#40、#41（提交详情读取三态 + 重试）、#43、#55、#56、#58、#59（组收起偏好写显示状态、刷新恢复）、
#71（空间纯函数与单开实测）、#73、#74、#76、#80（基线幂等/重放已满足 + 前端「已提交过/查看原进度」）、
#81（基线查证已满足 + 前端短状态/查提交结果）、#84、#85、#86、#88；14 处文案全部落地（含精确措辞）。

部分修复 / 已知边界：
- #01 PNG 浏览器级交付取决于链路视觉能力；本机假厂商未捕获首轮聊天请求（编排级 fake adapter 测试通过，浏览器级 UNVERIFIED）。
- #05/#71 宽窗并排未新增并排容器（几何不重叠）；800×600 单开实测 PASS，任务+提交组合开脚本未跑通（UNVERIFIED）。
- #10/#11 画布侧交互、#17 部分、#44–46 UI 接线、#60–62、#72 静态省略号、#09 同名线索细节为部分或未做。
- #08/#54 为兼容基线用例保留 hidden/aria-hidden 旧材料开关节点（视觉与无障碍不可达，用户只看三态入口）。
- #28 同大小内容替换检测不到（以真实文件大小/可用性为准，界面如实提示重新提交）。
- 恢复受阻/部分完成等文案按服务端账本口径实现；#72 未做双态省略号。

## 3. 验证（命令与结果）

后端（父分支，QIO_DATA_DIR 隔离）：
```
cd backend
python scripts/run_backend_suite.py --workers 4 --junit-dir <...>
```
- 并行组：6745 passed / 39 skipped，1 failed：`tests/test_sandbox_readonly.py::test_read_only_run_program_allows_provably_readonly_argv`
  （xdist 负载下报「本机无法核实低完整性写边界」；单文件复跑与该文件串行复跑均通过 → 环境/负载敏感波动，非本轮代码回归）。
- 串行组：101 passed / 12 skipped（exit 0）。
- 新增后端测试：`pm_qio032_material_scope_test.py`、`pm_qio032_submit_authorization_facts.py`、
  `pm_qio032_submit_empty_semantics.py`、`pm_qio032_submit_intents_event.py`、`pm_qio032_submit_first_round_materials.py`。

前端（父分支）：
```
cd frontend
npx vue-tsc --noEmit        # exit 0（最终）
npx vitest run              # 全量：583 个测试文件 / 5065 项，5060 passed；
                            # 4 个文件 5 项失败：windowFramePlacement 在审计基线 worktree 上同样失败（既有问题，
                            # 与本轮无关）；router-aliases / recovery-a-integration-n6 / settings-search 三个文件
                            # 在隔离复跑时通过（并行负载下的超时/顺序波动）。见 _lead-logs/pm-qio032/final3-frontend.log
```
- 本轮新增/更新的相关测试全部通过（含专测 #03/#07/#13/#20/#26/#27/#41/#44–46/#59/#80–81、三态、工具栏几何、
  删除隔离、组收起、任务面板三态/审批历史/恢复等）。

真实界面 E2E（正常入口 + fake provider，见 `scripts/verify-pm-qio032/README.md` 与
`_lead-logs/pm-qio032/e2e/SUMMARY.md`）：
- PASS：①-0 正常入口进入互动板面；①-1 文字卡；①-2/①-3 正常「添加→文件」上传 TXT/PNG；
  ①-4 三态「显示但不参与」；①-4a 隐藏后恢复显示默认「显示但不参与」；
  ④-0..④-7（800×600 单开/组合开互相切换、中心命中、不遮提交按钮、Esc 收最上层、
  1120/1121 实际边界两侧中心与命中一致）—— ④ 场景 8 项全 PASS（`e2e/console.log`、`e2e/summary-s4.json`）。
- UNVERIFIED（脚本未跑通/未捕获）：①-5 PNG 三态菜单（封面卡点击选择器）、①-6..①-10 首轮模型请求内容、
  ③ 重复提交、④ 组合开、⑤ 六件拖放/首件失败/路由往返、⑥ 主题/Tab/Esc/IME。
- 边界：fake provider 只证明 QIO 自身链路；Windows 原生拖放缺环境单列待验；1121/1120 由组件/纯函数测试覆盖。

## 4. 交付
- 父分支 `fix/pm-qio032-interactive`；最终提交 SHA 见交付消息与 `git log`。
- `docs/status.md` 已更新本轮「最后核对」；`python scripts/check_docs.py` 通过。
- 未 push / 未 PR / 未触发远端 CI / 未发布；`fix/pm-*` 分支不在 CI push 自动矩阵内。
