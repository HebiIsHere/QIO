# PM-QIO032 E2E 实测证据（真实界面入口 + fake provider；Lead/验收组运行）

## 运行方式
`powershell -ExecutionPolicy Bypass -File scripts/verify-pm-qio032/run.ps1 [-Only s1|s3|s4|s5|s6]`
后端 uvicorn（8734）+ vite（5199）+ 本机假厂商 SSE（8807）；浏览器=系统 Edge + CDP；
「正常界面入口」= 对话页模式切换进入互动板面；fake provider 只扮演厂商，不证明真实厂商/Tauri/原生拖放。

## 台账（多种证据放在本目录）
- `summary-merged.json`：一次全场景 run + 一次 s4 单独复跑合并后的**最终合并台账**（31/34 PASS）。
- `summary.json` / `console.log` / `provider-log.json`：s1 单独复跑 **12/12 PASS**（含首轮模型请求捕获）。
- `lead-final-run.log`、`lead-run-s1*.log`、`shots/*.png`、`backend.log`、`vite.log`：过程与截图。

## 合并结果（31 / 34 PASS）
| 场景 | 结果 | 证据要点 |
| --- | --- | --- |
| ① 首轮交付 / 三态 / 不泄漏 | **12/12 PASS**（s1 单独复跑） | 正常入口进入；文字卡/TXT/PNG 上传；「显示但不参与」「隐藏且不参与」、隐藏后恢复只回「显示但不参与」；provider 台账 chatRequests=3、含参与项正文标记；排除/隐藏/未提交内容均不出现。PNG 字节交付：本机模型 capability 未声明视觉 → 如实 image_unsupported（UNVERIFIED，不伪造）。 |
| ③ 重复提交/延迟回执/迟到事件/草稿清理失败 | 3/4 PASS | ① ③-1 双击只执行一次（provider 命中 1、提交记录 9→10）PASS；③-2 主状态停在真实受理态 PASS；③-4 延迟回执先「正在提交…」后受理态 PASS；③-5 重复/迟到事件不倒退、不重复卡 PASS；③-6 保存成功但本机草稿清理失败：保留原文 + 可操作入口 PASS。**③-3 已知缺口**：第二次提交仍新建受理记录（10→11），#80「重复提交显示已提交过、不再次执行」未完全达成。 |
| ④ 800×600 / 1120 / 1121 空间 | **8/8 PASS** | 800×600 单开详情完整、中心命中、不遮提交按钮；任务/详情组合开互相切换、同一时间只留最近一块；Esc 收最上层；1120/1121 实际边界（1176 inner=switch / 1177 inner=side-by-side）两侧中心与命中一致、两块面板与提交按钮互不遮挡。 |
| ⑤ 路由往返 / 六件拖放 / 首件失败 | 1/3 PASS | ⑤-1 互动 ↔ 对话 ↔ 互动往返后卡片与内容仍在 PASS。**⑤-2 已知缺口**：6 件拖放全部落卡（1→6），没有「只接收 5 件 + 第 6 件未接收可重导」提示（#44 UI 未完成）。**⑤-3 已知缺口**：首件上传失败没有可见失败原因与重试入口（#45/#11 UI 未完成）。 |
| ⑥ 主题 / Tab / Esc / IME | **5/5 PASS** | 净白/暗紫晶真实生效并截图；工具栏连续 Tab 焦点真实移动且可见；焦点在聊天输入框时 Esc 收提交详情且不清草稿；中文输入法组合态 Esc 不抢输入法、组合结束后中文正常落字；组合结束后 Esc 正常收起详情。 |

## 未取证（UNVERIFIED，不宣称通过）
- ② 提交 A 后保存 B 的板面/读卡/读文件三路一致：浏览器级未实现取证；后端有冻结快照与材料单测（`pm_qio032_material_scope_test.py`）。
- ⑤ 旧卡迁移（需旧 schema 板面 fixture）、隐藏成员、撤销连线（画布手势未纳入本轮脚本）。
- ① PNG 字节交付：本机链路模型 capability 未声明视觉，只有真实出现 image_url 才算交付。
- Windows 原生拖放：本机无环境，单列待验。
- 1121/1120 的窗口边界以「窗口 innerWidth 1177/1176 ↔ rootWidth 1121/1120」实测；与任务书措辞的换算关系已如实记录。

## 模拟标注
③-4 延迟回执（CDP Fetch 延迟）、③-5 重复/迟到事件（/api/events/test 注入）、③-6 草稿清理失败（Storage.removeItem 抛错）、⑤-3 首件上传失败（CDP Fetch abort 首个上传）、⑥-4 中文输入法组合态（CompositionEvent 模拟）。
