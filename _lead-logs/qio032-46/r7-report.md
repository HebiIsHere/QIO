# R7 浏览器验收 harness · 交付报告（task-1）

## 交付物

| 文件 | 说明 |
|------|------|
| `_lead-logs/qio032-46/verify/run-47.mjs` | 第七轮 10 组场景浏览器验收 runner（真实 Edge headless + CDP；真实前端 store + 真实隔离后端） |
| `_lead-logs/qio032-46/verify/lib47.mjs` | **新增**辅助层（不改 `lib.mjs` / `run-46.mjs` 既有语义）：store 快照、UI 可点性断言、请求台账、按命中次序的 Fetch 规则（`sequence`）、显式编辑/完成编辑、批次拖放、浮层清场 |
| `_lead-logs/qio032-46/r7-baseline/` | **修复前基线**（HEAD 0dbc387f 独立 detached worktree，产品代码零改动）：summary.json + README.md + console.log + run.log + shots/ + materials/ |
| `_lead-logs/qio032-46/r7-final/` | **最终结果**（定稿 1b75e54a 一次完整 s1..s10）：summary.json + README.md + console.log + run.log + shots/ + materials/；中间定向复跑保留在 `r7-final-rerun/`（s3）与 `r7-final-rerun2/`（s8,s9） |
| `_lead-logs/qio032-46/r7-final-main/` | 最终完整运行的原始目录（r7-final 是其汇总副本） |

## 结果

- **r7-final：11/11 PASS（0 FAIL / 0 UNVERIFIED）**，s1..s10 + 一条 6b 补充观察。
- **r7-baseline：5 PASS / 5 FAIL / 1 UNVERIFIED**。从 FAIL → PASS 的是 1（草稿 baseRev）、3（后台冲突 7 次 PUT → 4 次）、6（撤回状态非响应式）、8（附件跨板/重试关联）、9（批量写入失败仍标成功）。

## 覆盖口径（逐条对应任务书）

1. **草稿双窗口并发**：A 读 rev1 → 切 B → 回 A → 另一窗口直接 PUT A 同键推到 rev2 → 窗口1 编辑保存；断言 PUT 的 `baseRev`/`keyVersions` 是该板版本、冲突后 server/local 两份都在、B 不受影响。读 store `draftConflicts/drafts` + 请求体。
2. **正式保存后台成功清旧草稿**：B 完成编辑后 PUT 挂起 4s → 切 A → 放行；断言草稿台账含 `removed:[key]`、后端 drafts 无该键、A 草稿保持、新输入不被旧回执清掉。
3. **后台冲突也计入上限**：B→A 后每次 PUT 回 409；断言 4 次 PUT、切板往返不新增、页面真实「重试保存」按钮可点、点击只发 1 次 PUT 200。
4. **预判乱序**：三种到达次序（旧成功先到/新成功先到/旧先失败）分别用独立板面 + 运行中任务 + 按命中次序的 CDP Fetch 延迟/失败规则；断言确认入口始终对应最新候选，点真实「确认改动」后 PUT 目标/候选/checkId/stateVersion 一致。
5. **确认提示不串板**：B 确认后切 A、B 的失败迟到；断言 A 无失败文案/无重试按钮、回 B 可见可点、点重试只发 B。
6. **撤回重试**：取消后回读失败 → 页面「重新核对服务器事实」可点，点后 store `cancelRecovery.active=false`、入口消失；6b 验证 A/B 撤回归属互不覆盖。
7. **任务隔离**：B 有运行中任务、A 首次任务读取失败；断言 A intents 为空、不显示 B 任务、A 保存不被 B 的任务预判拦住。
8. **附件状态与重试**：B 上传→切 A→完成→回 B 显示真实失败态；关联保存失败后从页面「重试关联」按钮重试，断言上传仍 1 次、复用附件 ID、后端回读 attachmentId、界面成功。
9. **批量导入**：2 份文件写入失败 → 批次项 failed（不是 succeeded）；点实际批次项「重试」→ 该项成功、后端回读 attachmentId、上传数不增加（复用）、不重复建卡、同批其他项状态不变。
10. **原始路径回归**：上传 → 打开设置 → 返回互动页，附件仍在、不重复上传。

## 驱动与证据原则

- 每个场景一条 `ctx.record`，detail 里同时给：请求台账（method/URL/status/次数/请求体）、store 快照、DOM 文本/按钮事实（exists/visible/disabled/命中元素/矩形）、时间线。
- **界面通过**必须来自真实按钮：`L.clickAsserted` 先断言「元素存在、可见、未禁用、在视口内」再派发真实鼠标事件（CDP Input.dispatchMouseEvent）；store 动作只在 `beforeClick` 类补充观察里使用，不作为通过依据。
- 失败注入全部经 CDP Fetch，且与真实请求分开标注；数据目录固定 `_lead-logs/qio032-46/e2e-data`（`env-up.ps1` 设置），从不读系统 `QIO_DATA_DIR`。
- 驱动不稳时记 UNVERIFIED（`observe` 记录）并写清尝试方式与观察，不编造 PASS。
- 只写 `_lead-logs/qio032-46/`；产品代码只读。

## 已知边界与风险

- 该 harness 用自研 CDP（非 Playwright）：没有自动等待语义，所有等待都是显式轮询；个别场景需要真实 UI 的多次点击/刷新，耗时较长（全量约 12–15 分钟）。
- 「切板」由真实 store `selectBoard()` 驱动（互动页当前没有面向用户的板面切换器）；所有重试/确认动作均为真实按钮点击。
- s4 曾在中间实现上出现「第二次编辑未落地」的驱动问题，定稿实现（1b75e54a）上三种次序均稳定跑通；若后续实现改动影响编辑入口/浮层，需重跑 s4 确认驱动仍成立。
- 未跑全量测试、未触发 CI、未 push；Windows 原生 Tauri 壳、真实多窗口并发保存仍为未验证。
