# closure-D 独立验收 · 阶段 3：候选 SHA fd3649d 上的正式结论

- 验收角色：D（独立验收，不改产品代码）
- worktree / 分支：`qio-cl-d` / `wt/closure-d-accept`
- **冻结候选 SHA：`fd3649d4376458127dd10028ad840fb7207937f5`**
  （分支 `fix/interactive-closure-followup`，已并入 Lead 的 R1/R2/R3/R5、A 的 R4、B 的 R6、C 的界面与截图证据）
- 基线对照 SHA：`1da2172c6876c98f9b1076b4477dc75e7fe37638`

## 0. 一句话结论

六条反例在基线全部为红、在冻结候选 SHA 上全部转绿；服务端协议层（第③④层）在基线与候选上都是绿的，
说明缺陷确实在「前端丢掉服务端事实」这一侧，且本轮修复没有把服务端行为改坏。

## 1. 逐条 R1–R6 正式结论

| # | 反例 | 基线 1da2172 | 候选 fd3649d | 覆盖的层 | 关键真实数字 |
| --- | --- | --- | --- | --- | --- |
| R1 | 取消影响确认后正式板面未撤回 | **红** | **绿** | ①②⑤ | 基线 §①`expected ['被取消掉的新正文'] to equal ['服务器原文']`、§②同、§⑤真浏览器 B1.5 红（界面仍显示被取消正文）；候选三条全绿，且真浏览器「取消→再编辑→确认」B1c.1–B1c.4 全绿 |
| R2 | 迟到的 GET 覆盖已保存成功的新正式板面 | **红** | **绿** | ①② | 基线 §①/§②`expected ['旧正文'] to equal ['已保存成功的新正文']`；候选绿，且下一版保存携带服务器最新 seq=4 |
| R3 | 保住第二版却丢掉第一版保存的版本事实 | **红** | **绿** | ①④ | 基线 §①第二次 PUT 的 state.seq 仍是 3（应 4）；候选为 4 且 dirty 收敛；第④层真实 HTTP 两版落库回读 20/20 |
| R4 | 本机删除重试误变为整份草稿清除 | **红** | **绿** | ① | 基线 §① 真路径 `expected false to be true`（本机记录被写成 kind=cleared）、重开等价物 `expected undefined to be '服务器那份草稿（用户选择保留）'`（服务器稿被删空）；候选两条绿，clearDraft 对照用例绿 |
| R5 | 服务端兜底影响确认取不到 checkId → 重复同一说明 | **红** | **绿** | ①②④ | 基线 §①/§②`expected undefined to be 'chk_fresh'`（确认时 PUT 不带 confirm）；候选绿；第④层真实 HTTP 门/stale_check 不落库 20/20 |
| R6 | 确认说明未列出的新运行任务也被暂停 | **红** | **绿** | ①②④⑤ | 基线 §① `expected ['A'] to equal ['A','B']`、§② DOM `expected 1 to be 2`；候选绿；第④层真实门列出全部受影响任务并真暂停；§⑤真浏览器确认后受影响任务确实 paused（B1c.4） |

## 2. 分层覆盖矩阵（命令 / 真实输出 / 通过情况 / 未覆盖）

| 层 | 命令（在候选 worktree 内执行） | 基线 1da2172 | 候选 fd3649d | 未覆盖 |
| --- | --- | --- | --- | --- |
| ① 状态/单元 | `npx vitest run src/stores/__tests__/closure-d-r1-r4.test.ts src/stores/__tests__/closure-d-r5-r6.test.ts` | 11 条：10 红 1 绿（对照用例绿） | **12 条全绿**（含新增 R2 DOM 项） | — |
| ② 真实组件 DOM | 同上 + `src/components/interactive/__tests__/closure-d-impact-dialog.test.ts` | 4 条全红 | **4 条全绿** | — |
| ③ API/数据库 | `backend/.venv/Scripts/python.exe -m pytest -q tests/test_closure_d_acceptance.py` | 3 条通过（exit 0） | **3 条通过（exit 0）** | — |
| ④ 真实 HTTP + 真实 sqlite | `python scripts/closure-d-verify/closure-d-api-journey.py --base http://127.0.0.1:8734 --data-dir <临时目录>` | **20/20 通过** | **20/20 通过** | — |
| ⑤ 真浏览器（CDP） | `node scripts/closure-d-verify/closure-d-browser-probe.mjs --app 5199 --api 8734 ...` | **19/20**（唯一红 = B1.5，R1 缺陷） | **20/20** | R2/R3/R4 与 R5 的真浏览器深链路未覆盖（见 §4） |
| ⑥ 真实进程关闭重开 | 同上 B2 段 | 通过（B2.1） | 通过（B2.1） | R4 的「重开后本机记录不复活」用第①层等价物（同一 localStorage + 新 store），不是真进程关闭重开 |
| 类型检查 | `cd frontend && npx vue-tsc --noEmit` | exit 0 | **exit 0** | — |

证据文件（`scripts/closure-d-verify/evidence/`）：

- `phase1-all.json`（基线 ①② 11 条）/ `candidate-all.json`（候选 12 条）
- `phase1-dom-v2.json` / `phase1-store*.json`（基线分层）、`candidate-backend.txt`、`candidate-http.txt`、`candidate-browser.txt`
- `phase1-browser.txt`（基线 19/20）、`phase1-http.txt`（基线 20/20）
- `closure-d-b0-app.png` / `closure-d-b1-impact-dialog.png` / `closure-d-b2-reopened.png`（候选真浏览器截图）

## 3. 上一轮 20 项核心回归抽检结果（候选 SHA）

前端 13 个文件、后端 5 个文件（覆盖草稿冲突、发送归属/互换、清除版本守卫、长稿不截短、
审批材料失效、去重、成组、定位、失焦复位、通知可见性等）：

- 前端：`npx vitest run <13 个 final-*/closure-* 文件> --reporter=json`
  → **13 文件 / 91 用例，91 passed / 0 failed**（`evidence/candidate-regression-frontend.json`）
- 后端：`pytest -q tests/test_final_c_impact.py tests/test_final_c_drafts.py tests/test_final_c_dedup.py tests/test_final_c_preview_apply.py tests/test_final_c_approval.py`
  → **36 个用例全部通过（exit 0）**（`evidence/candidate-regression-backend.txt`）

如实说明：这是**抽检**（按 D.md 第 4 条的重点清单选文件），不是 20 项逐项全矩阵复跑。

## 4. 未覆盖项与限制（如实）

1. **真浏览器深链路未覆盖**：R2（乱序 GET 注入）、R3（连续两次真实保存）、R4（冲突→删除失败→重开点击流）、
   R5（前端未预料到→服务端门兜底）在真浏览器上尝试后**未采用**：这些长点击/注入序列实测不稳定
   （同一探针重跑出现过卡片渲染计数 0、B1.1/B1.2 随机红），不稳定证据不进交付物。
   这几条由 ①②（R2/R3/R4/R5）与 ④（R5/R6 服务端侧）覆盖；R2/R3 已补进第②层 DOM 做确定性验证。
2. **第⑥层范围**：真浏览器关闭重开验证的是本机数据持久化（B2.1 = 结束进程 → 同一用户目录新进程 → 数据仍在）；
   R4 的「重开后本机记录不复活」目前是第①层「同一 localStorage + 新 pinia/store」等价物，**不是**真进程关闭重开。
3. **C 的界面/审美改动**不在此报告范围内（视觉证据由 C 的截图与 lead 的门禁覆盖），我只复跑了行为层。
4. 第④层 20 条里的板面/任务种子是脚本自建（真实 API），不是用户手工操作。

## 5. 并发敏感用例的独立归因（lead 特别要求）

`tests/test_interactive_during_heavy_work.py::test_health_probe_stays_responsive_while_slow_prediction_runs`
断言「慢推理期间事件循环被占住 < 100ms」。我的独立观测：

- 首次抽样（机器上同时还跑着别的验收任务）：基线 3 次中 1 次失败（观测 **109 ms**）；
  候选 3 次中 2 次失败（观测 **124 ms / 124 ms**）。
- 随后**交错抽样**（同机、基线/候选交替、各 5 轮，共 10 次）：**10/10 全部通过**（exit 0）
  （`evidence/flaky-sampling.txt`）。

结论（如实）：该用例对机器负载敏感、阈值紧（100ms），**在基线上也会失败**，因此**不是本轮引入的缺陷**；
但候选侧在大负载下的观测值（124ms）略高于基线（109ms），样本太小、不能据此下「无回归」或「有回归」的结论。
按要求：既不算作本轮缺陷，也不放过——记录在案，建议门禁里单独标记为负载敏感用例。
证据：`evidence/candidate-flaky-heavy-work.txt`、`evidence/baseline-flaky-heavy-work.txt`、`evidence/flaky-sampling.txt`。

## 6. 关于「冻结 SHA 之后只有文档改动」的独立核对

我在只读方式下核对了 `git diff --name-status fd3649d..fix/interactive-closure-followup`（当时的分支 HEAD）：

- 冻结 SHA 之后的提交：`d8dcb12`（C 的**测试**文件，lead 代提交）、我的 4 个验收提交、以及
  `7ae8a4c` Merge `wt/closure-d-accept`。
- 改动文件**全部是新增的测试/验收文件**（`frontend/src/**/__tests__/*.test.ts`、`backend/tests/test_closure_d_acceptance.py`、
  `scripts/closure-d-verify/**`），**没有 `backend/src/**`、没有非测试的 `frontend/src/**`、没有 `docs/**`**。

如实说明：**我检查时分支上还没有出现「仅文档」的收尾提交**；因此本报告只能确认上一条事实
（冻结后至今没有产品代码改动）。等 lead 的文档提交落地后，如需要我可以再核对一次
`git diff --name-status fd3649d..<最终HEAD>` 是否只含 `docs/**`。

## 7. 环境与安全声明

- 所有服务（后端 8734 / 前端 5199 / Chrome CDP）启动前都显式设置**独立临时 QIO_DATA_DIR**
  （`scripts/closure-d-verify/tmp/run-*`），第④层脚本第一步断言 `<临时目录>/app.db` 存在并直接读该文件核对落库。
- 全程只用真人界面与真实 HTTP；**没有调用任何真实模型**、没有真实 API Key、没有联网依赖。
- `backend/.venv` 原本缺 pytest，只在 worktree 内执行 `uv sync --frozen --extra dev`（只动 gitignore 的 .venv）。
- 已知环境现象（不算缺陷）：webfont 走 worktree 外 junction 被 Vite 403，截图用系统回落字体；
  真浏览器探针已按已知现象排除该项请求。

## 8. 临时 worktree 说明（非交付物）

`D:\qio-dev\qio-cl-d-verify-tmp` 是我为「在不污染自己 worktree 的前提下、按分支/提交复跑探针」而建的
**临时 detached git worktree**（`node_modules` 为 junction，`backend/.venv` 由我 `uv sync` 生成）。
它不属于交付物、没有提交任何东西；lead 收尾时可整个删除（`git worktree remove --force` + 删目录）。
