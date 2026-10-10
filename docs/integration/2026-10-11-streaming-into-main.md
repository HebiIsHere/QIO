# 流式回复修复线并入 main —— 集成记录（2026-10-11）

## 一、先用非技术的话说结论

这次要做的不是"把两个分支合在一起"，而是**让两条各自开发了很久的线在同一个版本里都能正常跑**。

- 一条是**主线**：可靠性、安全、恢复入口、实例归属、用量记账（已经在 `origin/main` 上）。
- 一条是**流式回复线**：真实边生成边显示、统一过程区、附件从上传到删除的完整生命周期、
  取消/断流/恢复、以及这条线自己 8 轮审计留下的全部修复。

两条线都改了同一批文件（后端运行管理、数据库、前端状态机），所以合并时在 13 个文件上撞车。
处理原则只有一条：**两边已经验证过的行为都保留，不为了省事丢掉任何一边**。

数据库是这次最需要小心的地方：两条线各自给数据库加了列和表，而且**编号撞号**。
处理办法不是二选一，而是"给流式线的三条迁移换一个更大的编号，再额外加一条把所有对象
都补齐的补偿迁移"——这样无论用户手里的库是哪一类（全新的、旧 main 的、最新 main 的、
流式线的），升级后结构都完整，而且**不删改任何用户数据**。

另外在集成过程中发现一个**真实的功能空洞**：流式调用不记用量、也不受预算上限约束
（界面上数字照常显示，所以光看界面发现不了）。已经补上并加了测试。

**这条集成分支是给评审用的，没有动 main、没有合并、没有发布。**

## 二、实际分支与基线（全部为真实 SHA）

| 项 | 值 |
| --- | --- |
| 基线（最新 main） | `84eb4b4`（含可靠性跟进线与安全/响应性线两次合并） |
| 来源（流式回复修复线线头） | `cc19b68`（`fix/process-attachment-audit-round2`） |
| 共同祖先 | `6e073e9` |
| 集成分支 | `integrate/streaming-main-20261011` |
| 集成提交 | ``0e00dbe`（合并提交；随后为集成期修复与文档提交）`（合并提交，含冲突解决与集成补丁） |
| 相对 main | 引入 324 个来源提交；main 侧 61 个提交全部保留 |

来源线的祖先关系已核对：`feat/unified-process-attachments-streaming` →
`fix/unified-process-audit` → … → `fix/process-attachment-audit-consolidation` →
`fix/process-attachment-audit-final-boundaries` → `fix/process-attachment-audit-round2`
**全部是 round2 的祖先**，因此只合并 round2 即可覆盖全部历史成果，无需重复引入早期分支。

## 三、集成范围

**完整引入（流式回复线）**

- 统一过程区：阶段由服务端判定、阶段切换自动收起前一阶段、同阶段说明更新当前展示；
- 真实流式：回答正文边生成边显示（不是前端模拟），TURN_START / 增量 / TURN_END 事件身份与顺序；
- 附件生命周期：上传/绑定/独立副本、>100MB 走路径引用、历史附件打开、删除与重发、话题切换、
  异步复制与取消、关闭时的资源清理、路径失效提示；
- 结束事实：`reason_code / stopped_by / actions` 落库，随历史/分页/RESYNC 下发，
  终态动作表只有一份读时投影（`project_terminal_actions`）；
- 取消/断流/恢复/重发：预留→准备→放行（准备期间不入队、模型零调用），
  准备期中止以后端确认为准；
- 附件准备态与发送闸门、结构化拒绝（逐条原因 + 恢复出口）。

**完整保留（main 侧）**

- 持久接受（契约 C2）：台账写不进去 → 503 `accepted=false`，绝不返回假 200；
  本次集成**扩大到附件预留路径**（`reserve()` 也走同一个持久化入口）；
- 单事务重发（契约 C3）：`claim_for_resend` 一个事务完成"老记录 recovered_at + 后继行 + recovered_by"；
- 幂等受理（契约 5）：`client_request_id` 复用、`deduplicated` 回执、by-request 查证；
- 发送回执台账：回执只单向推进、绝不复活已结束的轮次；
- 实例归属与运行恢复（instances / record_owners / owner_guard / 接管）；
- 日志与事件打码总闸、工具路由移出响应线程、命令真实退出码、进程树清理；
- 记忆/知识/实体人工纠正保护、用量记账。

**没有做的事**：没有并入互动模式线；没有做与本次集成无关的重构；没有删除任何来源分支。

## 四、冲突与解决（13 个文件，逐条）

| 文件 | 冲突内容 | 处理 |
| --- | --- | --- |
| `backend/src/agent/storage/schema.py` | main 的 29/30 与来源线的 26/27/28 号段 | 来源线迁移**改号 31/32/33**（SQL 逐字保留），并新增**补偿迁移 34**（见第五节） |
| `backend/src/agent/storage/turn_journal.py` | 导入行；归属守卫/记账 vs 结束事实 helper | 并集：`transaction + RECORD_TURN + redact_text`；`_owner_guard`/`JournalWriteError` 与 `_redacted_or_none`/`project_terminal_actions` 全保留 |
| `backend/src/agent/storage/turn_journal.py`（补丁） | `claim_for_resend` 与 `reserve()` 的关系 | 新行插入改**幂等 upsert**（只覆盖仍为 queued 的行）：重发路径上这一行可能已由 `reserve()` 落过 |
| `backend/src/agent/core/turn.py` | `__init__` 字段；提交参数 | 并集：`_request_index`（幂等）与 `_reserved/_prepares/...`（预留）全保留；`submit` 带 `turn_id + request_id` |
| `backend/src/agent/core/turn.py`（补丁） | `reserve()` 未遵守 C2 | 抽出唯一持久化入口 `_persist_accept()`，`submit()` 与 `reserve()` 同故障同结果（都抛 `TurnAcceptError` → 503） |
| `backend/src/agent/core/loop.py` | 记账 helper vs 流式实现；`_plan`；错误脱敏 | 并集：来源线流式方法 + main 的 `_usage_accounting`；`_plan` 取来源线（已含 tool_routing 分区），调用点补 `await`；错误走 `sanitize_error_text` |
| `backend/src/agent/api/server.py` | `/api/turns` 与 resend 的受理顺序 | 取来源线"预留→准备→放行"，并在预留处保留 main 的 C2（503）与 C3（`claim_for_resend` 单事务消费 claim）+ F03 派发失败标记 |
| `backend/src/agent/adapters/{anthropic,native,text}.py` | 类属性与 prompt | 并集：`accounts_requests` 与 `supports_stream` 并存；text 档 `observation_note` 与 `CONTENT_ROLE_PROTOCOL` 并存 |
| `frontend/src/stores/session.ts` | 7 块：import / state / getters / 重发后处理 / 发送路径 / 失败分流 | 并集：回执台账与过程区状态并存；发送用 `startIdentity.topicId` + 显式附件数组；失败统一走 `_onSendFailure`（4xx=明确拒绝，5xx/无 status=结果未知） |
| `frontend/src/components/Composer.vue` | submit 的失败恢复 vs 附件准备态；模板 | 并集：准备态包裹发送、失败恢复补上"正在确认时不恢复草稿"的例外；确认条与浏览器回退上传入口都保留 |
| `frontend/src/services/api.ts` | `sendTurn` 签名与请求体 | 6 参签名（附件/重试/中止/准备标识）+ `client_request_id` + `retry_of_turn_id` + 显式 `attachment_ids`（空数组也成字段） |
| `frontend/src/stores/events.ts` | 导入行 | 并集：`api` 与 `TurnFactsRow` |
| `docs/status.md` | 两轮各自的里程碑章节 | 两段都保留（main 的 F01–F10 与来源线的附件边界收口） |

**没有使用 `ours`/`theirs` 整体取舍，没有删除测试、跳过校验、禁用功能或吞异常。**

## 五、数据库兼容

**号段与处理**

- main：迁移 1–25、**29、30**（26–28 被同基线的其它分支占用，因此 29 起步）；
- 来源线：迁移 1–25、**26（attachments 表 + 3 个索引）、27（source_attachment_id）、28（turn_journal 结束事实三列）**；
- 合并后：1–25、29、30、**31、32、33**（来源线三条改号）+ **34**（补偿迁移），`SCHEMA_VERSION = COMPENSATION_VERSION = 34`。

**为什么改号而不是保留 26–28**：`apply_migrations` 只按版本号前进（`target <= 已记录版本` 就整段跳过）。
任何已经记到 30 的存量库（就是最新 main 用户手里的库）都会把 26–28 跳过，
`attachments` 表永远不会被建出来。改号到 30 以上是唯一保证它**必然被应用一次**的做法。

**补偿迁移 34**：把 main 的实例归属对象（instances / record_owners / owner_instance_id /
knowledge 链列 / entity_cards 修订列）与本次集成新引入的 attachments / 结束事实对象合并成
一份**幂等**语句（CREATE TABLE IF NOT EXISTS + 列已存在就跳过）。`verify_required_objects()`
在迁移跑完后按**对象**复核，缺哪个补哪个，仍缺就抛 `SchemaIncompleteError`（不静默放过）。

**四种库状态都验证过（见第六节测试）**

| 起点 | 结果 |
| --- | --- |
| 全新库 | 1–25、29–34 全跑，对象齐全 |
| 旧 main 库（≤25） | 补齐 main 与流式线的全部对象 |
| 最新 main 库（30，无 attachments） | 应用 31–33 → 建表建列；数据不动 |
| 流式线库（28，有 attachments） | 应用 29–33；31 的 `IF NOT EXISTS` 与 32/33 的 `ADD COLUMN` 幂等跳过 |

**数据保护**：34 与 31/32/33 里没有 DROP / DELETE / UPDATE，只建对象；
没有修改任何历史迁移（迁移只追加的纪律用例仍绿）；没有靠改版本号掩盖结构差异。

## 六、测试证据

> 未通过 / 未执行的项目在第七节逐条列出。

| 项目 | 命令 | 结果 |
| --- | --- | --- |
| 后端全量 | `uv run --frozen pytest` | **3298 passed / 11 skipped / 0 failed**（exit 0，12 分 21 秒） |
| 前端类型检查 | `npx vue-tsc --noEmit` | **exit 0**（205 个测试文件的类型检查全过） |
| 前端全量 | `npm test` | **205 文件 / 1592 用例全部通过** |
| 文档一致性 | `python scripts/check_docs.py` | **exit 0** |
| 评测基线 | `uv run --frozen python -m agent.eval.run` | 与合并前基线 `da0436b` 的 **29 项指标逐项一致**（工具策略与流式改造无回归） |
| 流式记账（集成补丁新增） | `pytest tests/test_streaming_usage_accounting.py` | 3 passed |
| 定向回归（发送/附件契约） | `vitest run` 6 个相关文件 | 38 passed |

**定向验证覆盖**：发送回执顺序与幂等（`sr-e-sendReceiptOrdering`、`sr-e-sendConfirm`、
`sr-f-turn-lifecycle`）、附件准备与中止（`PreparingAttachments`）、附件显式空数组
（`attachmentExplicitEmpty`）、重发与结构化拒绝（`attachmentRetrySend`、`sendTurnAttachments`）、
起点身份（`rm-e-continuation`）、迁移纪律与补偿（`test_rm_lead_migration_discipline`、
`test_fu_w3_b01_schema`、`test_fu_verify_b01_migration`、`test_migrations_append_only_verify`）。

## 七、遗留问题与未验证项（不粉饰）

1. **未在真实 Windows 桌面上做端到端走查**（起应用、附件全流程、关闭清理、窄窗口视觉检查）。
   本轮以组件级 + store 级 + 后端全量测试替代，**不等于**桌面端已验证。
2. **Rust 侧未跑**：`cargo check` / `cargo test` 与安装包 E2E 属 CI 范围，本次未在本地执行。
3. **CI 未跑**：分支推送后由 GitHub Actions 运行；本地无法替代。
4. **两处产品口径待确认**：
   - 排队期间切话题时，附件 id 属于提交时的话题、`topic_id` 也是提交时的快照，后端按旧话题
     校验附件是否仍能命中（需要后端所有者确认）；
   - "已受理但用户点过中止"时保留附件 chip 清空的行为（来源线原语义）是否符合预期。
5. **`turn.py` 里 `_journal_accept_failed()` 在统一持久化入口后已无调用点**（死代码，行为无影响），
   建议后续单独清理，不混进本次集成。
6. **来源线遗留**：`PREPARE_HISTORY` 注释在来源线里重复一次（同值，无害）。

## 八、风险与回退

**代码回退**：本次成果全部在一个分支上，`origin/main` 未动。

```
git switch main                     # 回到未集成的 main（84eb4b4）
git push origin --delete integrate/streaming-main-20261011   # 需要撤销远端分支时
```

**数据库回退**：本次新增的迁移只建对象（31–34），**没有删改任何用户数据**，因此回退代码即可；
但如果旧版本代码运行在已经升到 34 的库上，会看到多出来的 `attachments` 表与 `turn_journal`
的三个新列 —— 旧代码不认识它们、不影响读写（列可空、表无人读）。
**升级前建议先复制一份数据目录做验证**（`QIO_DATA_DIR`），确认后再在真实数据上启动。

## 九、最终建议

- **是否达到合并 main 的标准**：代码与已完成的验证层面达到了（无遗留冲突、无残留标记、
  方向性回归已修、数据库升级路径四态齐备且幂等、后端/前端/文档本地全绿）。
- **是否存在阻塞项**：本项目前**没有 P0 阻塞**；但有三项属于"合并前应当补上"的证据缺口：
  ① Windows 桌面端实机走查；② 远端 CI 结果；③ 上面第 4 条的两处产品口径确认。
- **是否可以等待用户批准后正式合并**：可以。集成分支已经推送、PR 已创建，
  **未经批准不会合并、不会修改 main、不会发布**。
