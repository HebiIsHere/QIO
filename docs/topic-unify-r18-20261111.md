# R18 · 复核补修交付记录（2026-11-11 · 第七轮）

> 本文件记录本轮**实际做了什么、证据在哪、边界是什么**，数据均来自本轮实际命令，不是推断。
> 唯一共享契约是 `docs/topic-unify-r18-contract-20261111.md`（在 R16 v2 / R17 契约之上只做两处补修）；
> 结构语义补充见 `docs/architecture.md` §22.10。本轮不合并 main、不打标签、不发布。

## 1. 基线、分支与提交

- 仓库 `HebiIsHere/QIO`；受检分支 `fix/topic-review-20261110`。
- 开工核实（fetch 后）：`origin/fix/topic-review-20261110` = 本地 = `2384a0af`（ahead/behind 0/0，
  工作树干净，无后续远端修复）。本契约提交是补修轮的**脚手架基线**。
- 开发隔离：Lead 在 `D:\qio-dev\qio-r14` 集成；A/B 两条角色 worktree（`r18-a` / `r18-b`）从脚手架提交拉出；
  共享 `backend\.venv` junction；原始证据在 `D:\qio-dev\r18-evidence\<role>\`，不写进工作树。
- 提交序列（相对 `2384a0af`）：

| 提交 | 内容 |
| --- | --- |
| `6b3f8be5` | 冻结 R18 共享契约（脚手架基线） |
| `0cc88aa4` | A：新增互动板提交受理依据验收用例（基线红→修复绿） |
| `e10301c6` | A：提取共享受理依据快照 `services/turn_basis.submission_basis`（`server._submission_basis` 改为同一来源委托） |
| `b82b4652` | A：互动板提交受理时冻结受理依据，旧轮次不再覆盖用户新导航（P1） |
| `9c72807f` | B：普通回答保存失败同事务写暂存与待恢复记录，复用现有「重试回答」入口（P2） |
| `6cfb15f1` / `e3e38739` | Lead：分别合并 r18/a、r18/b |
| 本文件所在提交 | Lead：集成验收用例 + 交付记录 + 架构 §22.10 + 状态 P107（`[skip ci]`） |

- **集成代码受测 SHA：`e3e38739`**（A/B 全部合并后、本文件所在 docs 提交之前）。
  最终推送 SHA 以交付回复为准；本文件所在提交之后只改文档，不再改生产代码。

## 2. 两项问题的复现与修复

### 2.1 P1 互动板旧轮次覆盖新位置

- 复现（基线 `2384a0af`，Lead 真实 HTTP 用例 `test_r18_lead_integration.py::test_r18_p1_...`）：
  ① 一条普通轮先占住执行槽（fake 模型第 1 次调用被闸门挂住）；
  ② 互动板提交 `POST /api/interactive/boards/{board}/submissions` 被受理并在其后排队
     （`interactive_store.submit` 内的 `accept_turn` 调用 `ctx.turns.submit(message, ctx.current_topic())`，
     **未传受理依据**）；
  ③ 用户手动 `POST /api/anchor` 切到话题 B；
  ④ 放行旧轮：互动提交轮 `begin` 的轮首补锚临时读取实时 `position_version` 做 CAS，CAS 通过，
     把权威位置从 B 改回受理话题 A 并广播 `ANCHOR`（基线红断言：`position_state.read().topic_id == A`，
     期望 B；日志 `D:\qio-dev\r18-evidence\lead\baseline-red.log`）。
- 修复：
  - 新增 `backend/src/agent/services/turn_basis.py::submission_basis(ctx)`：与 `/api/turns` 完全同一份
    受理快照读取（`topic_id/fragment_id/intent_id/intent_version/nav_version/position_version`）；
    `api/server.py::_submission_basis` 改为一行委托，普通发送与重发行为不变。
  - `interactive_store.submit` 的 `accept_turn` 在受理这一刻（`ctx.turns.submit` 之前）**只读一次**
    该快照：轮次原话题与 `turn.board_topic_id` 都取快照 topic，`ctx.turns.submit(message, topic, basis=basis)`；
    `intent_id` 仍不传（互动板提交不是落实接续，不消耗待接续选择）。依据读取失败时如实告警并按旧路径降级。
  - 旧的排队轮次沿用 R17 依据链 CAS：失效后轮首补锚/轮末推进被拒（trace `begin_anchor_superseded`、
    `position_basis_superseded`），零位置副作用零广播；用户消息与正式回答仍按受理归属写入话题 A。
- 验证：A 新用例 `test_r18_a_interactive_basis.py` 4 passed（A1 排队→切 B→旧轮放行；A1b 共享快照；
  A2 无导航正常路径；A3 依据读取失败如实降级）；Lead 集成验收 `test_r18_p1_...` 1 passed（真实 HTTP：
  归属 A、权威位置 B、position_version 不推进、切换后不再出现 A 的 ANCHOR）。

### 2.2 P2 普通回答保存失败后无法重试

- 复现（基线 `2384a0af`，Lead 真实 HTTP 用例 `test_r18_p2_...` + fake 模型）：
  普通回答生成成功但保存失败时，`save_normal_answer` 的失败路径只把正文写进
  `topic_answer_runs.staged_*`，**不建 `topic_recovery_records` 记录**；`GET /api/recovery/records`
  看不到任何待恢复事项，现有 `POST /api/recovery/records/{id}/retry-answer` 入口因此没有任何可重试对象
  （基线红断言：保存失败后 45s 内无记录，日志 `D:\qio-dev\r18-evidence\lead\baseline-red.log`）。
- 修复：保存失败时在**同一事务**内完成：
  (a) 共享暂存唯一权威 `topic_answer_runs`（`staged_answer/staged_at/staged_model`、`phase=staged`、
  `status=pending`、释放 owner/租约）；(b) upsert 一条 `topic_recovery_records` pending 记录
  （`kind=RECOVERY_KIND`、`failed_stage="answer"`、turn/topic/user_message/fragment 如实、
  `answer_key` 与暂存同源、`recovery_nav_version` 取失败时刻真实导航版本、可读脱敏说明）。
  同一 `answer_key` 已有 pending 记录时复用其 `record_id` 更新（不产生多条）；已 answered 不复活；
  另一活跃执行者持有写入权/已 answered/执行权表不可用时**不写别人**、不建记录、仍抛原异常；
  两部分任一步失败整体回滚（不留 staged 半截），暂存/记录异常不掩盖原异常。
  没有改 schema/迁移、没有改路由与清单协议、没有改 `save_normal_answer` 冻结签名。
- 验证：B 新用例 `test_r18_b_answer_recovery.py` 9 passed（B1 失败→暂存+记录同事务；B1b 重复失败复用
  单条记录；B2 现有入口只补存不调模型；B3a/B3b/B3c 重复/并发/非执行者返回同一 answer_id；
  B4a 记录写失败回滚暂存；B4b 活跃执行者边界零写入；B4c 已 answered 不复活）；
  Lead 集成验收 `test_r18_p2_...` 1 passed（真实 HTTP + fake 模型注入一次写失败 → 解除故障 →
  `retry-answer` 补存：只一条正式回答、模型调用次数不增、原问题不重复、记录 answered 且 answer_id 正确、
  run 行 answered 且暂存清空；重复重试同一 answer_id）。

## 3. 本轮定向验证（命令、次数、受测 SHA）

全部命令在集成工作树 `D:\qio-dev\qio-r14`，使用共享 `.venv` / 临时 SQLite / fake provider，
受测代码 SHA = `e3e38739`。命令模板：

```
cd D:\qio-dev\qio-r14\backend
$env:PYTHONPATH='D:\qio-dev\qio-r14\backend\src'
& .\.venv\Scripts\python.exe -m pytest <files> -o addopts= -q -p no:cacheprovider
```

（仓库 `pyproject.toml` 默认 `addopts="-q"`；再叠一个 `-q` 会变成 `-qq` 隐藏汇总行，
所以证据命令用 `-o addopts=` 覆盖默认值，确保汇总行落进日志。）

| # | 范围 | 结果 | 证据 |
| --- | --- | --- | --- |
| 1 | Lead 集成验收 `test_r18_lead_integration.py` + A 新用例 + B 新用例（3 文件） | **15 passed，exit 0**（14.65s） | `r18-evidence\lead\FINAL-acceptance.log` |
| 2 | A 直接相邻回归（`test_r17_a_turn_basis.py`、`test_r17_b_resend_basis.py`、`test_r17_lead_integration.py`、`test_interactive_submission.py`） | **56 passed，exit 0**（19.29s） | `r18-evidence\lead\FINAL-adjacent-a.log` |
| 3 | B 直接相邻回归（`test_r17_d_answer_identity.py`、`test_r14_f_recovery.py`、`test_r14_b_f03.py`） | **28 passed，exit 0**（20.51s） | `r18-evidence\lead\FINAL-adjacent-b.log` |
| 4 | 互动提交受理/续作相邻（`test_interactive_intents.py`、`test_p3_d_submission_resume.py`） | **45 passed，exit 0**（13.09s） | `r18-evidence\lead\FINAL-adjacent-c.log` |
| 5 | Lead 基线红（受测 `2384a0af`，同一集成用例） | 2 failed（P1 位置被改回 A；P2 无待恢复记录） | `r18-evidence\lead\baseline-red.log` |

- 各角色自身证据（其分支 SHA，非最终集成 SHA）：A 新用例 `r18-evidence\a\green\r18_a_green.log`
  4 passed、相邻 23 + 68 passed、基线红 `r18-evidence\a\baseline-red\`；B 新用例
  `r18-evidence\b\green-run4.log` 9 passed、回归 `regression.log` 40 passed 与
  `regression-extra.log` 22 passed、基线红 `r18-evidence\b\baseline-red\`。
- **本轮未运行**：全量 pytest、前端测试/类型检查、生产构建、CI workflow；未调用真实/付费模型、未联网。

## 4. 已知限制与剩余验证范围

- 真实模型中文语义判断未验证：fake/scripted provider 只证明接线、事件顺序、事务与归属。
- 补修提交**未跑全量批次与 CI**（按任务书）；不把定向通过写成全量通过。
- `start_interactive_turn`（意图确认/续作/启动恢复）、`app.run_turn`、notify 轮仍 `basis=None`
  （R17 §7 已知边界保留；用户本轮只要求互动板提交入口）。
- 普通回答：外部模型调用不保证严格 exactly-once（崩溃在生成与暂存之间仍可能再次调用）；
  另一活跃执行者持有写入权时失败暂存会跳过且不建记录（由该执行者完成答案）；普通保存创建执行行后
  若正式消息被外部删除，恢复会按执行行返回历史 answer_id（产品无删除消息路径）。
- 本轮独立验证子代理在完成前失败（未产出独立用例/评审）；P1/P2 验收由 **Lead 自写的真实 HTTP +
  fake provider 集成用例**完成（`test_r18_lead_integration.py`），并复核两处实现接缝
  （A：受理时刻只读一次快照并传给 TurnManager；B：暂存+记录同一事务、同 answer_key 复用记录）。
  不宣称已获得独立第三方评审。

## 5. 明确声明

**未合并 main、未打标签、未发布、未改变生产更新源、未操作真实应用数据库、未调用真实或付费模型。**
补修提交供复核；合并与发布留待后续决定。
