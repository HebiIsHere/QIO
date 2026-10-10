# 独立验证报告 · 阶段一：R1—R7 基线反例复现（fb-E）

- 计划：docs/plans/2026-10-10-final-boundaries-r1-r7.md（冻结契约 K1—K3、§一 范围、§四 验收）
- 验证者：fb-e（复用成员 acc-f2；本轮**只新增验证资产，不改产品实现**）
- worktree / 分支：D:\qio-dev\qio-acc-f @ wt/fb-e，基线提交 **9e53736**（代码等同复核提交 43a9fcb）
- 原始输出留档：docs/verification-shots-fb/phase1-raw/backend-fb-e-baseline.txt、.../frontend-fb-e-baseline.txt
- 环境：Windows / PowerShell；后端 uv run --frozen（Python 3.11）；前端 vitest 4.1.10（jsdom）+ @vue/test-utils

> 本阶段只做一件事：在基线上把 R1—R7 **逐项复现成会失败的反例**（先红）。
> 所有断言都写成**正确行为**（例如「已删除的成员不得被报成已绑定」），不是「断言缺陷存在」；
> 诊断性断言（只为把缺陷看清楚）在最终验收前必须转成正确行为断言，本文件的用例已经全部是后者。

## 〇、结论速览（基线 9e53736 实测）

| 项 | 层级 | 反例文件 | 基线判定 | 结果 |
| --- | --- | --- | --- | --- |
| R1 | 真实附件服务 + 真实文件 + 确定性闸门 | backend/tests/test_fb_e_r1_relocate_part_window.py | **仍成立** | 1 红 + 1 绿守卫 |
| R2 | 真实附件服务 + 集合级提交 + 确定性闸门 | backend/tests/test_fb_e_r2_set_release_delete.py | **仍成立** | 1 红 + 1 绿守卫 |
| R3① | 真实 Composer + 真实 store + 受控服务时序 | frontend/src/components/__tests__/fb_e_r3_poll_removed.test.ts | **仍成立** | 1 红 |
| R3② | 真实 attachments service + 真实 localStorage + 受控响应 | frontend/src/services/__tests__/fb_e_r3_restore_tombstone.test.ts | **仍成立** | 1 红 |
| R4 | 真实 Composer + 真实 store + 受控选择器时序 | frontend/src/components/__tests__/fb_e_r4_picker_topic.test.ts | **仍成立** | 1 红 |
| R5 | 真实 store 历史转换 + 真实 MessageItem 渲染 | frontend/src/stores/__tests__/fb_e_r5_history_annotation.test.ts | **仍成立**（legacy 内联形态已 OK） | 1 红 + 1 绿守卫 |
| R6 | 真实 events store + 真实 session store | frontend/src/stores/__tests__/fb_e_r6_answer_identity.test.ts | **仍成立** | 4 红 |
| R7 | 真实 FastAPI + AgentLoop + 假 provider hold + 真实 resend 路由 | backend/tests/test_fb_e_r7_cancel_action.py | **仍成立**（**已知限制复核**） | 2 红 |

汇总命令结果：

- 后端：uv run --frozen pytest -q --tb=short <R1,R2,R7> → **4 failed, 2 passed**，退出码 1。
  （R1/R2 各有一条绿守卫，证明装置与断言本身可达；红的是缺陷路径。）
- 前端：npx vitest run <R3①,R3②,R4,R5,R6> → Test Files **5 failed**；Tests **8 failed | 1 passed**，退出码 1。
  （R5 的 legacy 内联形态守卫通过。）

## 一、逐项记录

### R1 — 旧重新定位任务改写/破坏最新一次定位（共用同名 .part + 检查与提交之间的窗口）

- **触发条件**：同一条附件先定位到 B（旧代际，复制在后台线程），只对它**源文件的读取**设确定性闸门把它卡住；
  用户在此时改主意定位到 C（新代际）。同一附件的两次定位共用同一个副本目标与**同名 .part**。
- **证据命令**：cd backend; uv run --frozen pytest -q --tb=short tests/test_fb_e_r1_relocate_part_window.py
- **基线实际结果（原始摘要）**：
  AssertionError: ('旧的在飞定位任务不得让最新一次定位失败', {'state': 'failed',
  'error': '没有权限写入（...att_xxxx__同名.bin）：副本没有保存（可以换目录或检查权限后重试）',
  'source_path': '...c.bin'})
  —— 收敛后：metadata.source_path 指向**最新来源 C**，磁盘副本却仍是**上一份 A**，state=failed，
  且用户最新一次定位被旧任务毁掉（C 的复制因同名 .part 被旧任务占用而在 Windows 上以「没有权限写入」失败）。
- **判定**：**仍成立**。失败信息与期望行为都写在断言里：最新一次定位必须 ready、磁盘字节必须是最新来源 C、
  sha256 与 C 对应、不得留下无人认领的 .part；另外还有一条绿守卫（没有并发旧任务时两次串行定位收敛到 C）。
- **与 Lead 描述的差异（如实）**：Lead 给的复现预期是「元数据指向 C、实际是 B」。在基线上实测到的是
  「元数据指向 C、实际是上一份 A，state=failed」——旧任务不是把自己的字节盖上去，而是**卡住同名 .part 把新任务打失败**。
  两者违反的是同一条用户可见不变量（收敛后元数据与磁盘必须自洽、最新定位必须胜出），因此反例按该不变量断言，未按猜测的中间态断言。

### R2 — 集合提交缺少整组最终复核：等待期间被删除的前项仍被报成已绑定

- **触发条件**：集合 = [a（本话题草稿，走普通绑定，无 await）, b（已绑定在 turn_old 上，走重试复用克隆，克隆里有 await）]；
  调用 bind_for_turn(turn_new, [a, b], retry_of_turn_id=turn_old)：
  先提交 a，随后 b 的克隆 I/O 被确定性闸门挡住；**闸门期间删除 a**；再放行 b 完成集合提交。
- **证据命令**：cd backend; uv run --frozen pytest -q --tb=short tests/test_fb_e_r2_set_release_delete.py
- **基线实际结果（原始摘要）**：
  AssertionError: ('已删除的附件仍被集合回执报成绑定成功（缺整组最终复核）',
  {'bound': ['att_...df9', 'att_...422'], 'rejected': [], 'a_exists': False})
  —— 回执把已从库里删掉的 a 报成 bound，rejected 为空，即整组「成功」。
- **判定**：**仍成立**。断言要求：回执里每个 ID 在收敛时刻都必须仍然存在且归属本轮；
  成员在等待期间被删除时不得以「成功」收尾（K1.5/K1.6 集合级提交，无半绑）；并配一条绿守卫（无删除交错时整组成功）。

### R3① — 准备中（poll 在途）被移除，旧 poll 结果把它 upsert 回来

- **触发条件**：真实 Composer 挂载；上传一个附件后进入 waitUntilSettled 轮询（受控 deferred 卡住）；
  用户点 chip 上的「×」移除；随后放行轮询的最终 ready 结果。
- **证据命令**：cd frontend; npx vitest run src/components/__tests__/fb_e_r3_poll_removed.test.ts
- **基线实际结果（原始摘要）**：
  AssertionError: 已移除的附件被旧 poll 结果重新加回列表: expected [ '准备中.txt' ] to not include '准备中.txt'
  —— 根因位置：Composer.vue track() 一开始就 removedIds.delete(item.id)，并且 onUpdate / 最终 commitToTopic
  都无条件 upsert，移除标记被旧任务重新清除。
- **判定**：**仍成立**。断言按 K1.4：移除必须进入 removed/tombstone，之后任何在途轮询结果都不得复活它
  （列表与持久化都不得再出现）。

### R3② — 恢复在途时移除，恢复写回把已删 ID 复活

- **触发条件**：真实 attachments service + 真实 localStorage；持久化里有 att_1、att_2；
  restorePendingAttachments("A") 期间 getAttachment(att_1) 被受控 deferred 卡住；
  期间模拟 removeOne 把 att_1 从持久化移除；再放行响应。
- **证据命令**：cd frontend; npx vitest run src/services/__tests__/fb_e_r3_restore_tombstone.test.ts
- **基线实际结果（原始摘要）**：
  AssertionError: expected [ 'att_1', 'att_2' ] to not include 'att_1'
  —— 根因位置：restorePendingAttachments 落盘用「进入时的 stored 快照」重建 merged（K1.6 禁止的整表回写），
  看不到在途发生的移除。
- **判定**：**仍成立**。断言按 K1.6：恢复只允许**新增**，绝不复活 removed/已发送的 ID、绝不整表回写旧快照。

### R4 — 选择器等待期间切换话题，结果注册进新话题

- **触发条件**：真实 Composer（桌面壳分支）；点击「附件」触发原生选择器 pickLocalPath（受控 deferred 卡住）；
  期间把 currentTopicId 从 A 切到 B；再放行路径。
- **证据命令**：cd frontend; npx vitest run src/components/__tests__/fb_e_r4_picker_topic.test.ts
- **基线实际结果（原始摘要）**：
  AssertionError: 结果必须归发起话题 A，而不是返回时的当前话题 B: expected 'B' to be 'A'
  —— 根因位置：Composer.vue pickFile() 在 await pickLocalPath() 之前没有捕获 topicId，
  之后由 addPaths() 里的 currentTopicId() 在**返回时刻**决定归属（K1.1 明确禁止）。
- **判定**：**仍成立**。断言按 K1.1：发起时刻的 topic 必须被捕获；返回后不得再读 currentTopicId 决定归属；
  结果只落发起话题 A 的持久化，不进 B。

### R5 — 系统核对注记在历史恢复后丢失（历史转换只读 raw.verified）

- **触发条件**：用真实 session store 的历史转换 _historyMessage 处理一条真实历史形态的记录：
  raw = {"annotation": "—— 系统核对…（含结论句）"}，content 为纯正文；再用真实 MessageItem 渲染。
  同时有 legacy 内联形态（正文末尾含固定表头）作为绿守卫。
- **证据命令**：cd frontend; npx vitest run src/stores/__tests__/fb_e_r5_history_annotation.test.ts
- **基线实际结果（原始摘要）**：
  AssertionError: raw.annotation 形态的注记在历史恢复后丢失（系统事实区域不存在）: expected false to be true
  —— 根因位置：session.ts _historyMessage 只调用 parseVerifiedRaw(m.raw)（raw.verified），从不读 raw.annotation。
  （legacy 内联形态守卫通过：正文里的固定表头能被 splitSystemAnnotation 拆出来渲染。）
- **判定**：**仍成立**。断言按 K2.3/K2.4：字段存在就用字段、缺失且正文含内联表头才拆分；
  系统事实区域独立渲染、注记恰好一次、正文恰好一次、归属原 turn。

### R6 — 最终正文校准仍按文字相似度判断（同身份 12→13 出两条、长→短被忽略）

- **触发条件**：真实 events store + session store 复放三轮事件：
  (1) 同一 delta_id 的回答先到 12，TURN_END 带 answer_id 且 final_content=13；
  (2) 同身份「长→短」；
  (3) 旧事件没有 answer_id，final_content 与流式正文完全不同；
  (4) 旧事件没有 answer_id 且完全无关。
- **证据命令**：cd frontend; npx vitest run src/stores/__tests__/fb_e_r6_answer_identity.test.ts
- **基线实际结果（原始摘要）**：
  AssertionError: 同一回答身份被校准成了两条正式回答: expected [ …2 条… ] to have a length of 1 but got 2
  —— 根因位置：session.ts applyFinalAnswer 走 mergeFinalBody（全文/前缀比较）；
  12→13 既不相等也不互为前缀 → pushAssistant 追加第二条；长→短因 existing.startsWith(body) 直接返回旧长文，
  缩短被静默忽略。
- **判定**：**仍成立**。断言按 K2.2：有 answer_id 只更新该身份那一条、不新建；无 answer_id 校准该 turn
  最后一条已发布的正式回答；不得比较全文/前缀；缩短与非前缀改写都必须生效。

### R7 — 活动任务停止后「重新发送」返回 409（cancelled 却给 resend）——**已知限制复核**

- **本轮定性**：这是上一轮（F12/acc-b2）留下的**已知限制**复核：当时裁定「排队取消 → retry、active 取消保持 resend」，
  本轮 K3 把 active 取消也改判为 retry。本项按「已知限制复核」登记，不是新发现。
- **触发条件**：真实 FastAPI + AgentLoop + 假 provider hold 卡住活动 turn；POST /api/turns/{id}/cancel 取消它；
  取 TURN_END 的 actions；随后直接调用 POST /api/turns/{id}/resend。
- **证据命令**：cd backend; uv run --frozen pytest -q --tb=short tests/test_fb_e_r7_cancel_action.py
- **基线实际结果（原始摘要）**：
  (1) AssertionError: ('活动取消的可用动作必须是 retry（resend 只认 interrupted 行）',
      {'actions': ['resend'], 'reason_code': 'user_stopped', 'status': 'cancelled'}) —— assert ['resend'] == ['retry']
  (2) AssertionError: ('动作表列出的动作必须真的可用：cancelled 行上的 resend 是死按钮',
      {'actions': ['resend'], 'resend_status': 409, 'reason_code': 'user_stopped'})
  —— 根因位置：core/turn.py ACTIONS_BY_REASON["user_stopped"] = ("resend",)；
  journal 对该行落 cancelled，/api/turns/{id}/resend 只接受 interrupted + 可 claim → 必然 409。
- **判定**：**仍成立**。断言按 K3：user_stopped → retry（且 resend 不得出现在动作表里）；
  「动作表只列当前确实可用的操作」——列进去的必须真的点得通（第二条断言把这个不变量钉死）。

## 二、验证层级与模拟边界（诚实声明）

| 维度 | 本轮实际覆盖 | 未覆盖 |
| --- | --- | --- |
| 模型 | 全部 fake/mock provider（FakeStreamAdapter 脚本化流；R7 用 hold 把活动轮钉死）；无联网、无真实密钥 | 真实厂商端点 |
| R1/R2 | 真实 AttachmentService + 真实文件系统 + 真实 sqlite（pytest 夹具 db_conn/tmp_path）；确定性闸门只控制 I/O 时序 | HTTP 路由级（/api/attachments/{id}/relocate）未在本轮覆盖 |
| R3/R4 | 真实 Composer 组件 + 真实 Pinia store + 真实 localStorage；网络边界受控（deferred），不用 sleep | 真实浏览器与真实后端联调（留给阶段二实机取证） |
| R5 | 真实 store 历史转换 + 真实 MessageItem 渲染（jsdom）；raw 形态按 K2.3 构造 | 真实后端历史 API 返回的完整 raw（阶段二用假 provider → 真实历史 API 复核） |
| R6 | 真实 events store + 真实 session store（事件序列复放） | 真实 SSE 与页面渲染 |
| R7 | 真实 FastAPI（ASGI transport）+ 真实 AgentLoop/TurnManager + 真实取消/resend 路由 | 前端按钮真实点击（阶段二实机） |
| 桌面 | 无 | Windows/Tauri 原生壳、安装包 |

## 三、为验证而新增的文件（只新增，未改产品实现）

| 文件 | 用途 |
| --- | --- |
| backend/tests/test_fb_e_r1_relocate_part_window.py | R1 反例 + 串行定位绿守卫 |
| backend/tests/test_fb_e_r2_set_release_delete.py | R2 反例 + 无删除交错绿守卫 |
| backend/tests/test_fb_e_r7_cancel_action.py | R7 反例（动作表 + 动作可用性两条） |
| frontend/src/components/__tests__/fb_e_r3_poll_removed.test.ts | R3① 反例 |
| frontend/src/services/__tests__/fb_e_r3_restore_tombstone.test.ts | R3② 反例 |
| frontend/src/components/__tests__/fb_e_r4_picker_topic.test.ts | R4 反例 |
| frontend/src/stores/__tests__/fb_e_r5_history_annotation.test.ts | R5 反例 + legacy 内联绿守卫 |
| frontend/src/stores/__tests__/fb_e_r6_answer_identity.test.ts | R6 反例（4 条） |
| docs/verification-shots-fb/phase1-raw/*.txt | 上述两项命令的原始输出 |

## 四、留给阶段二

- 等 Lead 通知集成 SHA 后 merge，复跑本目录全部 fb_e 反例（应转绿）+ 跨模块组合 + 全量后端/前端 + vue-tsc + check_docs。
- 实机取证（Playwright + msedge 无头，参考 scripts/verify-acc-phase2.ps1）：取消后的按钮、附件移除/选择话题切换、
  注记历史恢复、校准后唯一正文、窄窗口与代码块溢出。
- 逐项写「旧红 / 修复后绿」、真实命令与结果、未实测项，产出 docs/verification-final-boundaries-phase2.md。
