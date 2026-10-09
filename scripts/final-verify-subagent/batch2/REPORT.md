# 第二批独立对抗性探针报告（batch2）

- 冻结 SHA：`1d8796a931b1af3696b0fe9c7aa6c6f042a00bf5`（分支 `fix/interactive-final-closure`）
- 角色：独立对抗性复核，**不参与开发、不改产品代码、不 commit / push**
- 覆盖：**06 / 07 / 08 / 02 / 03 / 04**（第一批独立复核覆盖 01 / 12 / 12b / 16 / 17）
- 所有证据固化在 `evidence/`；一键复跑：`pwsh scripts/final-verify-subagent/batch2/run-all.ps1`

## 0. 复跑前的环境约束（已遵守）

- 起任何脚本/服务前显式设置 `$env:QIO_DATA_DIR=<临时目录>`；`run-all.ps1` 顶部会新建并设置，
  后端 08 探针内部还会在 import `agent` 之前**再设一次**。
- 本批未启动真实服务、未连接用户真实库 `D:\QIO-data`。
- 突变只做「唯一命中的精确字符串替换」，跑完**无论结果如何**都 `git checkout --` 还原；
  结束时 `git status --porcelain --untracked-files=no` 与 `git diff` 均为空（见 `evidence/git-status-after-mutations.txt`）。

## 1. 探针清单

| 编号 | 探针路径 | 形式 |
| --- | --- | --- |
| 06 | `probe-06-board-rev.probe.test.ts` | 真实 pinia store + 受控 services/interactive（单元/状态），1 例 |
| 07 | `probe-07-draft-clear-binding.probe.test.ts` | 同上，3 例（等待确认 / 保存失败 / 版本守卫） |
| 08 | `probe08_impact_gate.py` | 真实 sqlite 临时库 + 完整 ASGI 路由（TestClient）+ **直接读 sqlite 文件**核对，21 项断言 |
| 02/03/04 | `probe-02-04-send-identity.probe.test.ts` | 真实 session store + 受控 api.sendTurn（单元/状态），4 例 |

工具：`vitest.probe.config.mjs`（自建，root=frontend，绝对 include；未改 frontend 任何文件）、
`mutate-run.mjs`（前后端通用突变运行器）、`run-all.ps1`（一键复跑 + 证据）。

## 2. 未突变结果（冻结 SHA 上）

- 前端：**3 文件 / 8 用例全绿**（`evidence/batch2-vitest-unmutated.txt`）。
- 后端 08：**21/21 通过**（`evidence/batch2-probe08-unmutated.txt`）。
- 也就是说：这几条「最脆弱状态机」在冻结 SHA 上**先满足正确行为期望**，用例不是空转。

## 3. 逐条结论（未突变 → 突变 → 判定）

| 编号 | 探针路径 | 未突变结果 | 突变后结果 | 判定 |
| --- | --- | --- | --- | --- |
| 06 | `probe-06-board-rev.probe.test.ts` | 1/1 绿 | `m06_newerCandidate_false`（把 saveNow 里 `boardLocalRev !== putRev` 改成恒 false）：**1 红** —— 旧回执把板面换回第一版 | **成立**（用例承重） |
| 07 | `probe-07-draft-clear-binding.probe.test.ts` | 3/3 绿 | `m07_consume_disabled`（禁用 saveNow 里消化 `pendingDraftClears` 的循环）：**2 红 / 1 绿** —— 等待确认与保存失败两条正向清理都失败；版本守卫那条按预期仍绿 | **成立**（用例承重） |
| 08 | `probe08_impact_gate.py` | 21/21 通过 | `m08_drop_server_gate`：**10 红**（门被绕过，材料改动落库）；`m08_drop_stale_check`：**4 红**（过期 checkId 放行落库）；`m08_drop_stale_state`：**5 红**（过期提交产生 succeeded 记录并更新板面） | **成立**（三条服务端校验都承重） |
| 02 | `probe-02-04-send-identity.probe.test.ts`（02 例） | 1/1 绿 | `m02_drop_version`（成功清理去掉内容版本条件）：**1 红** —— 状态里的失败记录被旧回执删除 | **成立**（用例承重） |
| 03 | 同上（03 例 + 隔离例） | 2/2 绿 | `m03_drop_topic`（成功清理去掉话题条件）：隔离例**红**、现实流程例仍绿；`m03_drop_bind_disarm` 单独**全绿**；`m03_drop_capture_topic_check` 单独**全绿**；`m03_drop_bind_and_capture`（两层一起拆）：现实流程例**红** | **成立**（话题条件本身由隔离例证明承重；现实流程由 bind 解除 + capture 话题校验两层守着，见 §4） |
| 04 | 同上（04 例） | 1/1 绿 | 未被上面任一突变打红（该例断言的是「归属稳定 + 输入保护解耦」，对应的是迁移/归属逻辑，不在本批突变靶点内） | **成立（未突变通过）** |

## 4. 必须如实说明的两点（不承重 / 假通过）

1. **03 的「成功清理话题条件」不是现实流程的单独承重点。**
   三条突变矩阵实测：只去掉 `_clearFailedSendsAccepted` 里的 `sameTopic` → 现实流程例仍绿；
   只去掉 `bind()` 的 `disarmResend()` → 仍绿；只去掉 `captureAttribution` 的
   `armed.topicId === host.currentTopicId` → 仍绿；**两层一起拆才变红**。
   结论：现实反例 03 由「切话题解除关联」+「capture 时话题校验」两层独立挡住，
   成功清理中的话题条件是有意的纵深防御。为了让它**单独承重**，探针里加了隔离例
   （直接给成功清理喂一条「身份属 B、话题算 A」的归属），该例在 `m03_drop_topic` 下变红。
2. **02 最初版本的断言会「假通过」。** 第一版只看 `failedSendsForTopic()`：即使成功清理真的
   删掉了记录，`failedSend` 镜像仍会被该函数兜底返回，于是去掉版本条件也显示绿。
   改为断言 `session.failedSends`（状态列表）后，`m02_drop_version` 才稳定变红。
   （附带观察：镜像清理分支同样带版本条件，所以这条缺陷不会以「用户可见清单」的形式暴露，
   但状态层确实依赖主循环的版本条件。）

## 5. 08 的独立性说明

- 不使用开发者写好的 pytest 夹具；脚本自己 `mkdtemp` → `connect` → `apply_migrations` 建**真实 sqlite 文件库**，
  再用 `create_app(settings, conn)` 挂上完整 FastAPI 路由。
- 「不落库」不靠响应体推断，而是**直接对 sqlite 文件**执行 `SELECT seq/state/count` 核对
  （seq、材料正文、`board_state_snapshots` 数量、`board_submissions` 成功记录数）。
- 对照组（当前版本提交正常、`delivery.delivered` 未被伪造）证明 409 不是「接口整体坏了」。
- 与 Lead 的 `scripts/final-lead-verify/final-lead-api-journey.py`（真实 HTTP + 临时库）互为独立实现。

## 6. 本批独立结论

**未发现新的反例。** 06 / 07 / 08 / 02 / 03 / 04 在冻结 SHA
`1d8796a931b1af3696b0fe9c7aa6c6f042a00bf5` 上均先满足正确行为期望，并各自有可复现的突变证明用例承重
（03 的现实流程用例需要在拆掉两层守卫后才承重，已在 §4 如实标注）。
本批没有改动任何产品代码，也没有执行任何 git 写操作（创建/删除探针文件除外）。

## 7. 证据文件

| 文件 | 内容 |
| --- | --- |
| `evidence/batch2-vitest-unmutated.txt` | 未突变前端 3 文件 / 8 用例全绿 |
| `evidence/batch2-probe08-unmutated.txt` | 未突变后端 21/21 |
| `evidence/mut-m06_newerCandidate_false.txt` | 06 突变（1 红） |
| `evidence/mut-m07_consume_disabled.txt` | 07 突变（2 红 / 1 绿） |
| `evidence/mut-m02_drop_version.txt` | 02 突变（1 红） |
| `evidence/mut-m03_drop_topic.txt` | 03 突变（隔离例红） |
| `evidence/mut-m03_drop_capture_topic_check.txt` | 03 单层拆解（全绿 → 不单独承重） |
| `evidence/mut-m03_drop_bind_disarm.txt` | 03 单层拆解（全绿 → 不单独承重） |
| `evidence/mut-m03_drop_bind_and_capture.txt` | 03 两层同拆（现实流程例红） |
| `evidence/mut-m08_drop_server_gate.txt` | 08 服务端门突变（10 红） |
| `evidence/mut-m08_drop_stale_check.txt` | 08 stale_check 突变（4 红） |
| `evidence/mut-m08_drop_stale_state.txt` | 08 stale_state 突变（5 红） |
| `evidence/git-status-after-mutations.txt` | 全部突变后工作区状态（仅新增本目录） |
