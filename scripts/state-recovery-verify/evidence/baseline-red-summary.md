# 第八轮状态恢复收尾：基线反例冻结证据（b3245e5）

- 基线 SHA：b3245e5（工作树 D:\qio-dev\qio-src-d，分支 wt/src-d-verify）
- 角色：子智能体 D「独立验证」；本目录只包含**独立命名的验收测试与证据**，不修改任何产品代码。
- 目标：在真实基线上冻结 F1–F3、N1–N6 的「正确行为」反例，并确认它们在基线上失败（红）。
- 断言口径：**只断言正确行为**，不允许为了让基线通过而放宽断言；每条反例走真实路径
  （前端 store 真实入口 / 真实组件 DOM / 真实 ASGI 路由 + 真实临时 sqlite）。

## 1. 命令

前端（8 个文件，14 个用例）：

    cd frontend
    npx --no-install vitest run src/acceptance-state-recovery

后端（1 个文件，4 个用例）：

    cd backend
    $env:QIO_DATA_DIR = <独立临时目录>
    uv run --frozen --extra dev pytest tests/test_state_recovery_d_acceptance.py -q

> 环境提示：backend/.venv 里没有 pytest（pytest 在 pyproject 的 dev extra 中）。
> 只写「uv run --frozen pytest」会落到 PATH 上的系统 Python 3.10（缺 keyring 而导入失败），
> 必须带 --extra dev，或用 .venv\Scripts\python.exe -m pytest。
> 已用 dev extra 补装 pytest/pytest-asyncio 到本工作树的 .venv（未做全量重装）。

## 2. 基线结果

前端：Test Files 8 failed (8) | Tests 12 failed | 2 passed (14)

后端：F.FF → 3 failed | 1 passed (4)

## 3. 逐条反例与基线失败断言

| 反例 | 测试文件 / 用例 | 基线失败断言（真实输出） |
| --- | --- | --- |
| F1 | frontend/src/acceptance-state-recovery/f1-cancel-recovery-independent-changes.test.ts | AssertionError: 取消只撤回了版本记账，被取消的正文仍留在板面上（F1）: expected '被取消掉的新正文' to be '服务器原文' |
| F2 | .../f2-legacy-local-removal-replace.test.ts | AssertionError: 重试按对象删掉了另一页面后来写下的新稿（F2）：旧格式记录没有版本可证明，必须保留新稿: expected undefined to be '另一个页面在这之后写下的新稿' |
| F3 | .../f3-cross-page-stale-reconfirm.test.ts | AssertionError: 重新确认仍然用旧 seq 预判（不能只把 seq 改大，也不能绕过版本事实）: expected 3 to be 4 |
| N1（前端） | .../n1-superseded-check.test.ts | AssertionError: 被第二版取代的第一版检查迟到后仍然发出了旧候选（N1）: expected [ '第二版', '第一版' ] to deeply equal [ '第二版' ] |
| N1（后端） | backend/tests/test_state_recovery_d_acceptance.py::test_sr_n1_stale_whole_board_write_is_rejected | AssertionError: 用旧版本事实的整板写入被接受了（N1）：{"ok":true,...} → assert 200 == 409 |
| N2 | .../n2-reconfirm-result.test.ts | AssertionError: 确认返回后界面宣称「已确认 / 已保存 / 已暂停」，但真实结果仍是等待确认（N2）: expected true to be false |
| N3 | .../n3-pending-revert-decision.test.ts（3 用例） | 「继续」没有发出任何撤回执行请求（N3）: expected 0 to be greater than 0；「取消」关不掉提示（N3）；Escape 关不掉提示（N3） |
| N4 | backend/...::test_sr_n4_revert_rest_only_processes_explicitly_chosen_decisions | AssertionError: 未被这次展示/选择的决定项被顺带处理了（N4）: assert True is False |
| N4 | backend/...::test_sr_n4_revert_rest_rechecks_content_before_executing | AssertionError: 旧决定把等待期间新增了内容的卡片顺带删掉了（N4）: assert True is False |
| N5 | .../n5-submit-move-checked.test.ts | AssertionError: 提交真实清掉的勾选没有生效：本页仍带着旧勾选（N5）: expected true to be false |
| N6 | .../n6-incomplete-input-fields.test.ts（3 用例） | 附加字段 href / language / name 没有随未完成输入一起恢复（N6） |

## 4. 基线已通过、本轮只做回归保护的用例

这些用例断言的是**既有正确行为**，基线已通过；保留它们是为了防止本轮修复把这些行为改坏：

1. F1 恢复回读失败路径（f1-...test.ts 第 2 个用例）：saveStatus=error、原因可见、dirty=true，
   不假装撤回成功。
2. N1 版本一致时普通保存照常成功（后端 test_sr_n1_matching_seq_still_saves）。
3. N6 兼容旧的「仅正文」记录（n6-...test.ts 最后 1 个用例）：正文恢复、附加字段沿用卡片原值。

## 5. 证据分层（本轮已做到的层）

| 反例 | 分层 |
| --- | --- |
| F1 / F2 / F3 / N1(前端) / N5 | ①状态/单元：真实 Pinia store 真实入口（saveNow / cancelImpact / confirmImpact / submit / retryDraftSave）+ 受控服务替身与真实 localStorage |
| N2 / N3 / N6 | ②真实组件 DOM：真实 ImpactConfirmDialog / BoardCard 组件 + 真实 store/服务层；N2/N3 的传输层由假 fetch 替代（请求体真实断言） |
| N1(后端) / N4 | ③ASGI/API + 真实临时 sqlite（FastAPI TestClient + 真实路由） |

尚未覆盖（留给第二步 E1/E2）：④真实前后端 HTTP、⑤真浏览器、⑥实际进程关闭重开。
mock 部分已标注：F1/F3/N1/N5 的 API 替身、N2/N3 的假传输、F2 的存储故障注入。

## 6. 原始日志

- evidence/baseline-b3245e5-frontend-red.txt（vitest 完整输出）
- evidence/baseline-b3245e5-backend-red.txt（pytest 完整输出）
