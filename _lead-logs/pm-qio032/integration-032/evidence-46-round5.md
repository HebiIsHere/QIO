# #46 第五轮返修 · 保存冲突恢复（整轮重试次数 / 在途重新预判）证据

- 工作树：`D:\qio-dev\pm-qio032-integration`
- 分支：`fix/pm-qio032-integration`
- 基线（开工 fetch 核对 origin 同 SHA、工作树干净、无后续提交）：`41bd09c1b74608b01778f4c369369be53d7bd637`
- 修复 + 回归用例提交：`3cea886b244846e0e77324400da7fd0aec37df2d`
- 改动文件：`frontend/src/stores/interactive.ts`、`frontend/src/stores/__tests__/pm032-board-save-isolation.test.ts`
- 只做本地定向验证：未跑全量、未触发 CI、未合并 main、未打标签、未发布。

## 一、回归用例（同一份用例在旧代码与新代码上各跑一遍）

命令（cwd = `frontend`）：

```
npx vitest run src/stores/__tests__/pm032-board-save-isolation.test.ts
```

| 版本 | 结果 | 证据 |
| --- | --- | --- |
| 旧代码 `41bd09c1` | 22 例 / **4 failed / 18 passed** | `board-save-round5-prefix.json` |
| 新代码 `3cea886b` | 22 例 / **0 failed / 22 passed** | `board-save-round5-fixed.json` |

修复前失败断言（JSON 原文）：

1. 连续 409：`首次 PUT 之后的自动重试超过了 3 次: expected 5 to be 4`
2. 自动恢复中切板往返：`切板往返把 B 的重试次数清零了: expected 8 to be 4`
3. 在途重新预判再次 B→A→B：`再次切回 B 时对同一份恢复又发了一次重新预判: expected 2 to be 1`
4. 预判期间修改 B：`候选变化后没有按最新事实重新预判: expected 1 to be 2`

## 二、两个反例的修复前后

| # | 场景 | 旧代码 | 新代码 |
| --- | --- | --- | --- |
| 1 | 每次 PUT 都 `409 stale_state`，每次重读成功并返回更新的服务端版本，用户没有编辑 | 重试次数按每次恢复步骤的 `putRev` 计数、`clearSaveRecoveryPlan()` 又把记录一起清掉；重读+合并推进本地版本后每次从 1 重新数 → 自动 PUT 无界（4.2 秒推进后实测 5 次以上，独立复现日志为 9 次），界面一直停在「保存中」 | 整轮自动恢复按板面记账、与当前恢复步骤分开：首次 PUT 后最多再自动 3 次（合计 4 次 PUT，seq 依次 7/8/9/10 说明每次都用了重读推进后的版本）；更长时间后仍为 4 次，界面退出「保存中」→ 失败态（`saveStatus=error`、原因含「已自动重来 3 次」、`dirty=true`），候选完整（2 张）。点重试后：再撞一次 409 → 读回 seq 12 → 以 seq 12 保存成功 |
| 2 | B 的保存冲突后切到 A，计划留在 B；回到 B 后重新预判在途，再次 B→A→B（期间无编辑） | 重新预判没有登记在途操作，每次回到 B 的 `load()` 都再启动一份 → 同板、同计划、同候选发出 **2 次重新预判**，可能形成两份确认 | 恢复计划开始预判前登记在途步骤：同板、同恢复计划、同候选只发 **1 次**；再次切回 B 接回原请求。响应回来后只形成 **1 份确认**（`boardId=board_b`、`checkId=chk_round5`、受影响任务 `task_impact_1`）；确认后 PUT 目标板 B、候选 2 张、seq 8、`checkId=chk_round5`、`stateVersion=8` 一致，A 收到 0 次相关请求 |

## 三、定向验收（四类切板 / 冲突时序）

| 场景 | 实测结论 |
| --- | --- |
| 连续 409、每次重读成功且版本推进 | 恰好 4 次 PUT（seq 7/8/9/10）；推进更多时间仍 4 次；失败态保留候选与重试入口；重试后基于最新 seq 12 保存成功 |
| 自动恢复中切到 A 再切回 B | 切换前 B 已用掉 2 次；切回 B 继续用同一轮，总计恰好 4 次（旧代码 8 次）；A 的卡片 / 保存状态 / 待确认记录 / 请求全部不受影响（0 次 PUT 到 A） |
| 返回 B 后重新预判在途，再次 B→A→B | B 只有 1 次重新预判（目标 B、依据 seq 8、候选 2 张）；只形成 1 份确认；确认后再切 A/B 不发新预判；确认后 PUT 目标板 / 候选 / 版本 / 确认编号一致，A 无请求 |
| 重新预判期间修改 B，新旧结果反序返回 | 修改后按最新候选开启新步骤（第 2 次预判，候选 3 张）；新响应先回形成 `chk_new` 确认；旧响应迟到不覆盖确认、不再触发第 3 次预判；确认后 PUT 用 3 张候选、seq 8、`checkId=chk_new`、`stateVersion=8` |

## 四、本地定向命令与结果

```
cd frontend
npx vitest run src/stores/__tests__/pm032-board-save-isolation.test.ts
# 22 passed（旧代码 4 failed / 18 passed）

npx vitest run src/stores/__tests__ src/acceptance-state-recovery \
  src/components/interactive/__tests__/pm032-canvas-import-batch.test.ts \
  src/components/interactive/v2/__tests__/ImpactConfirmLayer.test.ts \
  src/components/approval/__tests__/uf2-modal-exclusive-integration.test.ts \
  src/views/__tests__/i2v2-a-impact-confirm.test.ts \
  src/views/__tests__/acceptance-i2v2-page-impact.test.ts
# 149 files / 1144 tests passed（含上轮 #46 初次预判防重、迟到 PUT 回执、
# 409 重读期间切板、impact_confirmation_required、stale_check、跨页上传只关联一次）

npx vue-tsc --noEmit
# exit 0
```

```
cd ..
python scripts/check_docs.py   # 通过
git diff --check               # 干净
```

## 五、诚实边界

- 未起隔离的本地后端重读 A / B 做真实落盘复核：证据是 store 层可控计时器 / 延迟响应用例与真实请求参数断言（mock 的是服务边界）。
- 未跑全量测试、未触发 CI、未跑完整安装包 E2E；Windows 原生 Tauri 壳与真实多窗口并发保存未验证。
- 整轮恢复记录与在途步骤按板面记在内存：整页刷新后丢失，刷新由既有 `load()` 按服务器已保存版本重建事实。
