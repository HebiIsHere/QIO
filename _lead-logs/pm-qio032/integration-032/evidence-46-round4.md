# #46 第四轮返修 · 跨板保存全流程（预判 / PUT / 409 重读 / 重新预判 / 确认）证据

- 工作树：`D:\qio-dev\pm-qio032-integration`
- 分支：`fix/pm-qio032-integration`
- 基线（开工 fetch 核对 origin 同 SHA、工作树干净）：`5cb50ff0b1d0a946048c033b4db52a45381030ce`
- 修复 + 回归用例提交：`9564abe42aae6924ebfcd2e784232ee9e7493c72`
- 改动文件（2）：`frontend/src/stores/interactive.ts`、`frontend/src/stores/__tests__/pm032-board-save-isolation.test.ts`
- 只做本地定向验证：未跑全量、未触发 CI、未合并 main、未打标签、未发布。

## 一、回归用例（同一份用例在旧代码与新代码上各跑一遍）

命令（cwd = `frontend`）：

```
npx vitest run src/stores/__tests__/pm032-board-save-isolation.test.ts
```

| 版本 | 结果 | 证据 |
| --- | --- | --- |
| 旧代码 `5cb50ff0` | 18 例 / **6 failed / 12 passed** | `board-save-round4-prefix.json` |
| 新代码 `9564abe4` | 18 例 / **0 failed / 18 passed** | `board-save-round4-fixed.json` |

修复前 6 个失败断言（JSON 原文）：

1. 预判在飞时 B → A → B：`切回 B 时对同一份候选又发了一次预判: expected 2 to be 1`
2. 409 重读在途、重读失败：`B 的重读失败写进了 A 的保存状态: expected 'error' to be 'saved'`
3. 409 重读在途、重读成功：`B 的重读结果写进了 A: expected 'error' to be 'saved'`
4. impact_confirmation_required 重新预判在飞：`B 的待确认状态落到了 A`（A 上出现 `checkId=chk_away`）
5. stale_check 重新预判在飞：`B 的待确认状态落到了 A`（A 上出现 `checkId=chk_stale_away`）
6. 确认已显示后切板：`expected undefined to be 'board_b'`（确认记录没有板面归属）

## 二、三个已知反例的修复前后

| # | 场景 | 旧代码 | 新代码 |
| --- | --- | --- | --- |
| 1 | B 正在影响预判，按 B → A → B | 同一份候选发出 **2 次 B 的预判**；最终 1 次 PUT 只是因为代次把第一个判过期 | **1 次 B 的预判 + 1 次 B 的 PUT**（候选 2 张、依据 seq 3）；另测切回 B 后新增编辑：旧预判迟到不产生 PUT，第二次预判用最新候选（3 张），最终 1 次 PUT |
| 2 | B 收到 `409 stale_state` 开始重读，切到 A，B 的重读失败 | A 从 `saved` 变成 `error`、显示 B 的错误、`dirty` 置真（重读成功时同样污染 A） | A 状态 / 错误 / 未保存标记 / 卡片 / 请求全部不变（发往 A 的预判与保存均为 0）；失败原因与候选归 B，切回 B 可见 error 与 2 张卡，读取落定后按 seq 8 继续保存成功 |
| 3 | B 的 PUT 要求影响确认，恢复重读期间切到 A | 向 A 发影响预判，并把 B 的受影响任务 + A 的 `checkId` 写进 A 的待确认状态 | A 的 `pendingImpact` 仍为 null、无发往 A 的预判 / 保存；回 B 后重新核对当前候选（2 张、seq 8），确认记录 `boardId=board_b` / `checkId=chk_b2` / 任务 `task_impact_1`；确认后 PUT 目标 B、候选 2 张、seq 8、`checkId=chk_b2` 一致 |

## 三、每个切板时点的实测结论

| 切板时点 | 实测结论 |
| --- | --- |
| 预判已发出、未返回 | B → A → B 无编辑：1 次 B 预判 + 1 次 B PUT，A 请求 0；切回 B 后新增编辑：旧预判不产生 PUT，第二次预判用最新候选，最终 1 次 PUT |
| PUT 已发出、未返回 | 上一轮两个反例复跑通过（B 迟到回执只改 B；切回 B 只等原 PUT）；在途新编辑时迟到回执只吸收版本事实，后续 PUT 用最新候选与已确认 seq |
| 已收到 409、原板重读在途 | 重读成功 / 失败各一例：A 保持 saved / error=null / dirty=false / 卡片不变 / 0 请求；B 的失败与候选归 B，切回后读取落定即继续保存 |
| 恢复后重新预判已发出、未返回 | impact_confirmation_required 与 stale_check 各一例：切 A 后放回 B 的结果，A 无确认 / 无预判 / 无保存；回 B 重新核对（目标 B、seq 8、候选 2 张），确认任务 / checkId / 候选都属于 B，最终 PUT 目标 / 候选 / seq / checkId 一致 |
| 确认已显示、切板或继续编辑 | A 上确认不产生保存（0 次 PUT 到 A、A 状态不变）；B 确认取回，候选新增后重新核对（目标 B、3 张、seq 7）并换发 `chk_2`，再次确认后最终 PUT 目标 B、3 张、seq 7、`chk_2` |

## 四、本地定向命令与结果

```
cd frontend
npx vitest run src/stores/__tests__/pm032-board-save-isolation.test.ts
# 18 passed（旧代码 6 failed / 12 passed）

npx vitest run src/stores/__tests__/pm032-import-board-identity.test.ts \
  src/stores/__tests__/pm032-state-duplicate-submit.test.ts \
  src/components/interactive/__tests__/pm032-canvas-import-batch.test.ts
# 35 passed（跨页上传只上传并关联一次、A/B 卡片各自保存、待提交改动与提交基准成对）

npx vitest run src/stores/__tests__ src/acceptance-state-recovery \
  src/components/interactive/__tests__/pm032-canvas-import-batch.test.ts \
  src/components/interactive/v2/__tests__/ImpactConfirmLayer.test.ts \
  src/components/approval/__tests__/uf2-modal-exclusive-integration.test.ts \
  src/views/__tests__/i2v2-a-impact-confirm.test.ts \
  src/views/__tests__/acceptance-i2v2-page-impact.test.ts
# 149 files / 1140 tests passed

npx vue-tsc --noEmit
# exit 0
```

```
cd ..
python scripts/check_docs.py   # 通过（121 个里程碑条目）
git diff --check               # 干净
```

## 五、诚实边界

- 未起隔离的本地后端重读 A / B 做真实落盘复核：证据是 store 层可控延迟 / 409 用例与真实请求参数断言（mock 的是服务边界，不是 store 逻辑），不能当作真实浏览器 / 真实后端证据。
- Windows 原生 Tauri 壳、真实厂商模型、真实多窗口并发保存未验证。
- 恢复计划与重试预算按板面记在内存：整页刷新后丢失，刷新会被既有 `load()` 当作新的整板事实处理。
