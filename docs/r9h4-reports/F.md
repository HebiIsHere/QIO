# R9-H4 F：审批完成、重放与并发（契约 §10）

- 基线：r9h3/main HEAD 68101d6261253bb742d01af73a458bab2e0a1c5a；本 worktree 起点 67fa88c6
- 分支：r9h4/f；worktree：D:/qio-dev/qio-r9h4-f
- 代码+测试提交：040a8d92；本报告提交为本分支 HEAD（提交号见提交信息/最终消息）
- 完整 CI：未触发（累计额度 2/2 已用尽）；只做受影响定向 pytest
- 未修改：approved_operations.py / services/app.py / security/engine.py / 迁移与 schema

## 0. 结论

| 契约 §10 要求 | 状态 | 证据 |
| --- | --- | --- |
| unresolved 下具体操作的有效批准可完成 | 已修复 | test_unresolved_specific_approval_completes_exactly_once |
| 已知限制优先、不解除已知禁止 | 已修复 | test_unresolved_known_prohibition_still_denies_specific_approval |
| 不清未知限制/不授予长期权限/不改写成无限制 | 已修复 | test_specific_approval_does_not_turn_unresolved_into_unrestricted |
| 结束进程审批重放完整传递原审批快照/依据 | 接口已交付，待 Lead 一行接线后生效 | ExecutionContext.replay_basis / replay_gateway_kwargs + 测试 |
| 确认阶段/状态写入 CAS，并发只有一方推进同一步 | 已修复 | test_two_connections_only_one_advances_same_confirmation_stage |
| 旧请求不覆盖新阶段/回退次数/绕过撤回 | 已修复 | test_stale_request_cannot_advance_rewind_or_bypass_cancel / test_finish_only_from_executing_and_never_overwrite_terminal |
| 状态与审计一致 | 已修复 | test_audit_sequence_is_consistent_after_controlled_races / test_audit_failure_rolls_back_confirmation |
| 期限/模式/要求/内容/确认次数/重复领取按既定规则 | 已验证 | 见 §5 对应测试 |
| 一次/两次确认不能执行、取消不执行 | 已验证 | test_three_confirmations_required_and_one_or_two_cannot_execute |

## 1. §10.1 unresolved 下具体批准完成

### 根因（先复现）

security/operations.py::ExecutionContext.authorize_started 在批准后执行前调用
engine.recheck_approved。对 unresolved/legacy 要求，引擎会返回 APPROVE
（“历史结构化限制尚未可靠恢复；执行前核对不自动放行”）。旧实现把除 ALLOW 以外的
所有结果都当成失败：用户刚批准的具体操作在真正执行前立刻被判“当前状态不再允许”，
于是永远完不成。

### 修改（security/operations.py）

- 新增 authorization_basis：每次执行前重核对写入依据（kind/reason/approval_id/
  requirement id+version/mode/确认次数），供执行结果、审批审计与界面引用；
- _approve_completes_unresolved(reason)：只有当
  1) 引擎 APPROVE 原因精确是“历史结构化限制尚未可靠恢复”（不是内部数据次数不足/
     永久删除需明确确认/范围不足/普通确认次数不足）；
  2) 审批单绑定的要求仍 active、version 与绑定一致、且该要求确实处于 legacy/unresolved；
  3) 已知限制冲突已由 recheck_approved 的 _limit_conflict 先行 DENY（走不到这里）——
  才允许这一次执行，basis.kind = specific_approval_completed_unresolved，并明确标注
  known_limits_checked / unresolved_requirement_kept / does_not_clear_unknown_limits /
  does_not_lift_known_prohibitions / does_not_grant_long_term。
- 另外补齐：绑定要求若已被撤回/修正/替换（engine._requirement_for 返回 None），
  在重核对前直接拒绝，避免退化成“没有可对照要求”的宽松分支（尤其 unrestricted）。

### 行为前后

| 场景 | 修改前 | 修改后 |
| --- | --- | --- |
| unrestricted + unresolved（无已知限制冲突） | 用户批准后 invalidated，不能执行 | 执行一次，结果/审计带 basis；要求仍 unresolved |
| unresolved + 已知“禁止修改 B” | 可能因 same-unresolved 分支混淆 | 引擎先 DENY，具体批准不能覆盖（不执行） |
| 批准后用户修正/撤回要求 | 可能按“无要求”放行 | 执行前拒绝并 invalidated |
| 长期授权 | — | 不写 authorization_records（测试断言计数不变） |

## 2. §10.2 结束进程审批重放完整传递快照/操作依据

### 根因

approved_operations.execute_proc_kill 构造重放网关时只传了 security：

    security=_ApprovedReplayGateway(security)   # 没有 operation / approval_id

而 _ApprovedReplayGateway._binding_error 在没有 operation 快照时按设计返回
“没有审批记录快照，无法核对这次执行是否就是被批准的同一个操作”。因此 proc.kill
审批重放总是报没有审批记录（run_program/shell 已经传了 operation，只有 kill 漏了）。

### 已交付接口（F 独占文件，可直接用）

    # security/operations.py
    ctx.replay_basis()             # {approval_id, kind, payload, effect, fingerprint,
                                   #  instruction_id/text, requirement_id/version,
                                   #  confirmed_stage/confirmations_required,
                                   #  mode_at_request/approval, expires_at, ...}
    replay_gateway_kwargs(ctx)     # {"operation": ctx.operation,
                                   #  "approval_id": ctx.approval_id}

测试证明：用该接口构造的 _ApprovedReplayGateway(None, **replay_gateway_kwargs(ctx))
对同一 effect 返回 ALLOW（reason 含“审批记录一致”），对不一致 effect 返回 DENY
且不再报“没有审批记录”；用真实提交 → 批准 → 执行器收 ctx 的链路验证 pid 与
approval_id 完整传递，并只结束测试自己创建的进程。

### Lead 接线需求（唯一一处，共享文件不能由 F 改）

backend/src/agent/tools/approved_operations.py::execute_proc_kill：

    # 现在
    security=_ApprovedReplayGateway(security),
    # 改为
    from agent.security.operations import replay_gateway_kwargs
    security=_ApprovedReplayGateway(security, **replay_gateway_kwargs(ctx)),

接线后的验收测试（本次已准备同构断言，Lead 可直接复用/扩展）：
1) 测试自己 spawn 一个进程 → 提交 proc.kill（payload pid、fingerprint、effect 快照）→
   用户批准 → 执行器成功结束该测试进程；结果 detail 含 approval_id 与 pid；
2) 篡改 payload pid / 用别的 effect 重放 → invalidated/DENY，不结束任何进程；
3) 断言 audit 里 claimed/executed 与 operation 依据一致。
没有这行接线时，execute_proc_kill 的缺陷仍在（F 不能改共享文件）。

## 3. §10.3 状态写入 CAS/事务

storage/approvals.py 新增 _write_transaction：BEGIN IMMEDIATE 先拿写锁，把
“读当前行 → 核对前一状态 → CAS UPDATE → 写审计”放在同一串行区间；已在外层事务时
复用。真正防丢更新的条件是每一条 UPDATE 的 CAS：

- _transition：新增 expected_revision / expected_confirmed_stage，写进 WHERE；
  事务内先重读，任一不匹配 → ApprovalConflict（不写、不审计）；
- decide（approve/reject/cancel）、confirm：都带读到的 revision（confirm 另带
  confirmed_stage）；并发 confirm 同一个 stage 只有一个成功；
- claim：同一事务内核对过期/操作内容变化并 CAS（state+revision+confirmed_stage+
  有效期）；并发领取 only one；
- finish：只允许 executing 一次性收口；迟到的旧结果返回当前行、不覆盖终态；
- mark_invalidated/expired/interrupted_unverified：只在允许的状态+CAS 命中时迁移，
  否则写 *_rejected 诊断事件，不覆盖已完成/已撤回；
- _ensure_waiting_binding：操作内容被改时，按 revision+confirmed_stage CAS 重置
  确认次数并提交后才报错，不会把并发中已推进的阶段回退；
- 审计 _audit(strict=True) 与状态迁移同事务：审计写失败 → 抛错回滚（状态不变）。
  审计 seq 在同一事务内 MAX+1，受写锁保护，不丢条/不跳号。

## 4. §10.4 并发验证方式（不用随机 sleep）

两个真实 SQLite 连接（ApprovalStore ×2，同一数据库）+ threading.Barrier 让两个请求
同时进入；断言的是不变量（恰一方成功、revision 只 +1、审计只记一次），而不是执行顺序。
覆盖：同阶段并发确认、approve/cancel 竞争、并发领取、迟到 finish、内容篡改重置、
审计失败回滚、审计序号连续。

## 5. 测试命令与结果

    cd D:/qio-dev/qio-r9h4-f/backend

    # F 新测试（19 条，真实 ApprovalService/Store/operations，fake 执行器，无模型）
    uv run --frozen python -m pytest tests/test_r9h4_approval.py -o addopts='' -q
    # -> 19 passed

    # 受影响既有审批/安全批次（13 文件）
    uv run --frozen python -m pytest
      tests/test_r9h4_approval.py tests/test_approval_lifecycle.py tests/test_approval_restart.py
      tests/test_approval_binding.py tests/test_approval_api.py tests/test_approval_persistence.py
      tests/test_approval_session_identity.py tests/test_final_c_approval.py
      tests/test_internal_data_guard.py tests/test_r9f_approved_replay.py
      tests/test_r9g_acceptance_e2e.py tests/test_r9h3_limits_priority.py
      tests/test_approved_operations.py
      -o addopts='' -q --tb=short
    # -> 158 passed, 2 warnings in 92.09s

（未跑全量 pytest、未触发任何 CI workflow。）

## 6. 已修改文件

| 文件 | 内容 |
| --- | --- |
| backend/src/agent/security/operations.py | authorization_basis / replay_basis / replay_gateway_kwargs；unresolved 具体批准完成；绑定要求失效拒绝 |
| backend/src/agent/storage/approvals.py | BEGIN IMMEDIATE 事务；decide/confirm/claim/finish/mark_* CAS；审计 strict 同事务；终态保护 |
| backend/src/agent/tools/approval.py | 执行结果/事件/审计附 authorization_basis |
| backend/tests/test_r9h4_approval.py | §10 全部场景 19 条测试 |

## 7. 未完成项 / 边界（如实）

1. proc.kill 实际重放仍需 Lead 接线（approved_operations.py 一行，见 §2）；
   接线后 F 的接口测试可直接扩展为真执行器验收；未接线前该执行器缺陷仍在。
2. _approve_completes_unresolved 通过引擎现有 reason 文本“历史结构化限制尚未可靠恢复”
   识别该 APPROVE 分支。若 B/Lead 后续改动该文案，需同步这里（已在测试中断言行为；
   建议契约冻结该 reason 或在 AuthDecision 加结构化标记，本轮不改共享 effects.py）。
3. SQLite 的并发边界是单写者 + busy_timeout（5s）+ BEGIN IMMEDIATE；这是本机多连接/
   同进程并发语义，不是分布式锁。多进程同时写审批仍由 SQLite 串行化。
4. 旧 v1 审批（request()/respond() 无 operation/state 的行）走既有兼容路径；
   本次 CAS/审计改造不改变旧行迁移语义（is_v2 判定不变）。
5. 状态/审计失败时会话会如实失败（finish 的审计失败抛 ApprovalError，行保持
   executing，启动恢复会标 interrupted_unverified），不静默成功。
6. 未触发完整 CI（预算 2/2），未改 docs/status.md 与 r9h4-merge-notes（按归属由 Lead 汇总）。
