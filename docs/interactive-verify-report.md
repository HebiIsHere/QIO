# 互动模式第一阶段：独立复核报告

- 复核人：agent-verify（独立复核子智能体）
- 工作区：`D:\qio-dev\qio-int-verify`（分支 `wt/int-verify`）
- 首轮基线：`0becedafc40959745df0ac0477aba221dd54181a`
- 修复后复测基线：`8903ab177c1ac5790dc0e13aba6627d5aa255f2b`
- 约定来源：`docs/interactive-mode-contract.md`、`docs/superpowers/plans/2026-10-06-interactive-foundation.md`
- 我**没有修改任何产品代码**；本报告与 `backend/tests/test_interactive_verify.py` 是唯一产物。

**证据分级**（每条结论都标明）：

| 标记 | 含义 |
| --- | --- |
| 复现 | 我自己跑出来的（HTTP 级 / 真实界面探针 / 数据库直查），附实际输出 |
| 读码 | 只读实现推断，没有独立运行验证 |
| 未验证 | 没能验证，写明原因 |

复现用例：`backend/tests/test_interactive_verify.py`（26 项，全绿）。
临时复现脚本与界面探针步骤保留在工作区 `scratch-verify/`（**未提交**，只作本地复现依据）。

---

## 0. 结论摘要

| # | 结论 | 级别 | 首轮（0beceda） | 复测（8903ab1） |
| --- | --- | --- | --- | --- |
| 1 | 未勾选 / 明确隐藏注释的文字、id、链接不进 before/after/visibleRange | 复现 | 成立 | 成立 |
| 2 | 客户端塞未勾选卡片 id（requestedVisible）无效，服务端从已保存状态推导 | 复现 | 成立 | 成立 |
| 3 | 一名可见成员都没有的组整体不出现；组名只在有可见成员时出现 | 复现 | 成立 | 成立 |
| 4 | 保存路径（PUT /state）不调用模型 / 网络；保存不产生提交 | 复现 + 读码 | 成立 | 成立 |
| 5 | **未勾选注释借「撤回」回到 before**（文字 / id / 链接 id / 链接含义） | 复现 | **阻断（已报告）** | **已修复，复测通过** |
| 6 | 保存内容 / 草稿 / 预览 / 基准在新 app 实例里读得回来 | 复现 | 成立 | 成立 |
| 7 | 重新打开不自动提交、不自动恢复执行（running→paused，反复读不再变化） | 复现 | 成立 | 成立 |
| 8 | 提交失败保留改动与勾选、基准不提前更新、可重试 | 复现 | 成立 | 成立 |
| 9 | **批量批准被下一次意图列表刷新立即暂停** | 复现 | **阻断（已报告）** | **已修复，复测通过** |
| 10 | 暂停任务「按当前材料继续」闭环（需确认、刷新指纹、保留进度、终态不可复活） | 复现 | 不存在该入口 | 成立 |
| 11 | 冲突对同批批准被拒；依赖任务不自动开始 | 复现 | 成立 | 成立 |
| 12 | 重复提交幂等：不重复调用、不推进基准 | 复现 | 成立（状态名有出入，见 5.6） | 成立 |
| 13 | PUT /state 结构异常状态被 normalize 修正（G1–G8） | 复现 | 成立 | 成立 |
| 14 | 真实界面：加材料→写注释→只勾选一条→提交→演示意图→批量处理→失败撤回 | 复现（截图） | 走通 | 走通 |
| 15 | **材料影响闸门**：存在材料依据已失效的 paused 任务时，每次编辑都要人工确认才落库 | 复现 | 存在（本轮新发现） | 存在（未修） |
| 16 | `data-im="impact-continue"` 指向的不是「继续保存」按钮 | 复现 | 存在（本轮新发现） | 存在（未修） |
| 17 | 重新打开后界面显示「尚未保存过」，与后端实际保存序号不符 | 复现 | 存在（本轮新发现） | 存在（未修） |

---

## 1. 权限边界

### 1.1 未勾选 / 明确隐藏注释：文字、id、链接都不进载荷 —— 复现，成立

我做的事：构造 2 条注释（1 条勾选、1 条未勾选）、1 条「勾选 + 明确隐藏」的注释、2 张材料，
再挂 3 条链接（正常 / 一端未勾选 / 两端不可见）与 4 个组（可见组 / 只有隐藏成员的组 /
未勾选+材料的混合组 / 全不可见的组），经 `PUT /state` 落库后用 HTTP 提交，
并在请求体里塞 `{"requestedVisible": [未勾选 id, 隐藏 id]}`。

实际输出（脚本 `scratch-verify/explore1.py`）：

    ### A1: unchecked/hidden notes, links, groups via HTTP
    PUT /state -> 200 seq= 1
    saved group names: ['组 1', 'SECRET_GROUP_NAME', '组 2']
    POST /submissions -> 200
    status: succeeded | baseline: {'updated': True, 'firstSubmission': True, 'previousSeq': None, 'seq': 1}
      leak 'SECRET_UNCHECKED_TEXT'      in payload: False
      leak 'SECRET_HIDDEN_TEXT'         in payload: False
      leak 'LEAK_LINK_MEANING'          in payload: False
      leak 'LEAK_LINK_2'                in payload: False
      leak 'SECRET_GROUP_NAME'          in payload: False
      leak 'SECRET_GROUP_3'             in payload: False
      group names in after: ['组 1', '组 2'] | before: []
      links in after: 1 cards: 3 members: {'组 1': [...2 项...], '组 2': [...1 项...]}
      delivery.detail: ...客户端请求的 2 项不在本次允许查看范围内，已忽略（可见范围由服务端从已保存状态推导）
      checkedCleared: ['c_6436e7e4f51c']

结论：11 个检查点全部为 False（不泄露）；只有 1 条正常链接进入 after；只有含可见成员的组出现
且只列可见成员；`delivery.detail` 明确说明客户端塞进来的项已被忽略。

### 1.2 服务端从已保存状态推导可见范围 —— 复现，成立

见 1.1 的 `requestedVisible` 注入：被塞进去的未勾选 / 隐藏卡片 id 仍然进不了
`before` / `after` / `visibleRange`。同一场景也覆盖在
`test_unchecked_and_hidden_notes_links_and_groups_stay_out_of_payload` 与
`test_server_derives_visible_range_from_saved_state_not_request`。

### 1.3 保存路径不调用模型 / 网络，且保存 ≠ 提交 —— 复现 + 读码，成立

三重证据：

1. 读码：`backend/src/agent/interactive/` 下没有任何模型 / 网络客户端 import
   （grep `requests|httpx|aiohttp|openai|anthropic|urlopen|socket` 无命中）；
   `board_store.save_board` 只做 `normalize_state` + SQLite 写入 + 本地求差
   （`submission.refresh_pending`）；`PUT /state` 里额外调用的
   `intents.on_board_saved` 也只读数据库、改状态，无网络。
   静态断言在 `test_save_path_source_has_no_model_or_network_imports`。
2. 运行时：把 `socket.create_connection` / `socket.getaddrinfo` / `ssl.SSLContext.wrap_socket` / `httpx.*.send`
   全部换成抛错，`PUT /state` 与 `GET /state` 仍返回 200
   （`test_put_state_makes_no_network_calls_and_no_submissions`）。
3. 保存不产生提交：

       ### B: PUT /state count of submissions and no QIO
       submissions before/after 3 PUTs: 2 2

   保存 3 次后提交记录条数不变；`GET /state` / `GET /intents` 反复读取同样不产生提交（见 2.2）。

### 1.4 组名与组的可见性 —— 复现，成立

只有隐藏成员的组（`SECRET_GROUP_NAME`）与全不可见的组（`SECRET_GROUP_3`）
整体不出现；混合组只列可见成员（`['c_f3755345ffc8']`）。

### 1.5 【原阻断 1】未勾选注释借「撤回」回到 before —— 首轮复现，修复后复测通过

**首轮复现（0beceda）**：一条注释先勾选并成功提交，随后**取消勾选 + 删除**，再提交。
`submission.project_baseline` 当时是 `elif cid not in live: keep[cid] = dict(card)`，
删除优先于「未勾选」，于是该注释连文字、id、以及它的关系链接一起回到 `before`：

    ### REVOCATION EDGE (previously submitted -> unchecked -> deleted): links + pending preview
    1st submit: succeeded
    pending preview leaks REVOKED_NOTE_TEXT: True | note id: True
    2nd submit status: succeeded
      before leaks note text: True | note id: True
      before leaks link meaning: True | link id: True
      before links: [('l_662e5dfaf4', 'REVOKED_LINK_MEANING')]
      expressions: [('note_deleted', '撤回注释：REVOKED_TEXT'),
                    ('link_removed', '移除关系：REVOKED_TEXT — m.pdf（含义：REVOKED_LINK_MEANING）')]
      after leaks: False

对照实验（只取消勾选、不删除）：`before leaks text: False | id: False | link meaning: False`
—— 说明漏洞只在「删除」这一支路上。

**修复后复测（8903ab1）**：

- 单元/接口级：`test_revocation_keeps_before_after_expressions_pending_and_range_clean`
  断言 `before / after / expressions / visibleRange / pending` 五处都不得出现
  `REVOKED_TEXT、注释 id、REVOKED_LINK、链接 id`，通过。
- 真实界面：在界面上「勾选 → 提交 → 取消勾选 → 删除 → 再提交」，再直查数据库：

      --- submissions ---
      seq 1 succeeded | note text leaked: True  | note id leaked: True  | after cards: [...3 项...]
      seq 2 empty     | note text leaked: True  | note id leaked: True  | before cards: [...3 项...]
      seq 3 empty     | note text leaked: False | note id leaked: False | before cards: [...2 张材料...]
      --- board state now ---
      note in board: {"id":"c_muwjyfj23pi5m", ... "checked":false, "deleted":true ...}
      pending leaked: False | pending kinds: []

  seq 3 就是「取消勾选 + 删除」之后的那次提交：载荷里不再有该注释的文字、id，也没有它的链接；
  未提交改动预览（pending）同样干净。注释仍在板面上（`deleted=true`），只是不交给 QIO。
- 对照（修复后仍然保留撤回语义，`test_deleting_a_still_checked_note_still_expresses_withdrawal`
  与 `test_deleting_a_material_still_expresses_withdrawal`）：
  仍勾选时删除 → `before` 仍保留该注释并生成 `note_deleted`；
  材料删除 → 仍生成 `material_removed`。

结论：修复方向正确（`models.visible_except_deleted` + 删除分支改判据），
权限边界的硬边界现在**在字面上也成立**。

---

## 2. 恢复行为

### 2.1 保存内容 / 草稿 / 预览 / 基准在新实例里读回 —— 复现，成立

我做的事：写入 1 条勾选注释 + 2 张材料 + 2 条草稿，提交一次，生成 4 项演示意图，
记录状态；然后**关闭连接、用同一个数据库文件重新 `create_app`**（新 instance_id），重新读取。

实际输出（`scratch-verify/explore4.py`）：

    ### REOPEN (new app instance, same DB): read back everything
    content preserved: True
    positions preserved: True
    drafts: {'drafts': {'note-1': '还没提交的草稿内容', 'note-2': '第二条草稿'}, 'updatedAt': ...}
    pending expressions count: 0 | baselineSeq: 1
    baseline summary: {'seq': 1, 'submittedAt': '2026-10-06T10:15:43.924140+00:00'}
    submissions after reopen: [(1, 'succeeded')]
    checked flags: [('勾选的注释', False)]
    intents after reopen: [...4 项，状态与重启前一致...]
    preview cards preserved: [(1, '对比摘要（演示预览）...'), (2, '材料一说明（演示预览）...'), ...]
    preview identical to before restart: True
    submission row count after reopen: 1

结论：内容、位置、草稿、提交基准、提交记录、意图状态与**待审批预览**全部读回，预览逐字节一致。

### 2.2 重新打开不自动提交、不自动恢复执行 —— 复现，成立

    ### NO AUTO SUBMIT on reopen: repeated GET /state and /intents
    submission rows before/after 3x GET: 1 1
    statuses: [...全部保持原状态...]

配合 `test_reopen_does_not_auto_submit_or_resume`：新建实例后 running 必须是 paused，
再连读 3 次 `GET /intents` + `GET /state` 状态不再变化，提交记录不增加。

### 2.3 提交失败保留改动与勾选、基准不提前更新 —— 复现（注入失败），成立

说明：第一阶段没有真实模型调用，`failed` 分支只能由异常触发。
我用 `monkeypatch` 让 `submission.content_fingerprint` 抛错
（走的是产品代码自己的 `except` 分支，不是绕开它）：

    failed submit -> HTTP 200 status: failed
    baseline: {'updated': False, 'firstSubmission': True, 'previousSeq': None, 'seq': None}
    delivery: 提交未成功：没有调用 QIO，基准未更新，改动与本次注释选择已保留，可以再试一次
    baseline after failure: None
    checked preserved: [('c_33078f73a1', '要被保留的勾选注释', True)]
    submission rows: [(1, 'failed')]
    retry after failure: succeeded baseline: {'updated': True, 'firstSubmission': True, 'previousSeq': None, 'seq': 2}
    checks cleared on success: ['c_33078f73a111']

结论：失败不改基准、不丢改动与勾选，重试即成功并清勾选。**这条是注入异常下的验证**，真实模型失败路径第一阶段不存在。

### 2.4 【原阻断 2】批量批准被下一次列表刷新立即暂停 —— 首轮复现，修复后复测通过

**首轮复现（0beceda）**：`batch_decide` 调 `approve_intent(conn, intent_id)`
时没有传 `instance_id`（`intents.py:875`），`/intents/batch` 路由也没有传
（`api/interactive_intents.py:150`）。于是 progress 里没有 `__ownerInstance`，
紧接着的 `GET /intents` 走 `recover_running_intents(instance_id=当前进程)`，
把 `_owner_instance(row)=None` 判成「别的进程遗留」→ paused：

    ### BUG CHECK: batch approve a plain intent, then GET intents
    batch approve resp status: running | approved: ['i_d9270f82a510']
    DB immediately: status= running progress= {"done":0,"total":1,"text":"执行中","__materialWatch":{...}}
    after ONE GET /intents -> {... 'i_d9270f82a5': 'paused'}
    DB after GET: status= paused progress= {"done":0,"total":1,"text":"已暂停：等待你确认继续",...}

    ### Control: single /approve path stores owner instance
    single approve -> status: running progress: {...,"__ownerInstance":"qio_1966a27ed164450d"}
    after GET -> {... 该任务仍是 running ...}

真实界面同样复现（v-04 截图）：点击「批量批准（1）」后立刻出现
「上次没有结束的任务已经暂停（1 项）」横幅，该意图显示「已暂停」，数据库里它的 progress 有
`__materialWatch` 但没有 `__ownerInstance`。
前端 `stores/interactive.ts` 的 `decideBatch()` 之后立刻 `settleAfterIntentChange()` → `loadIntents()`，
所以真实界面里「批量批准 = 刚批准就暂停」。

**修复后复测（8903ab1）**：

- HTTP 级（`test_batch_approve_survives_same_process_but_pauses_in_new_instance`）：
  同实例刷新仍是 running、`recovery == {"paused": []}`、progress 里有 `__ownerInstance`；
  换一个实例（新 app）再读才变 paused 且 `recovery.paused == [该 id]`。
- 真实界面（截图 v-11 / v-12b）：
  * 点「批量批准（1）」后浏览器内立刻刷新：意图显示「执行中」，`pauseBanner: false`，
    `批量处理完成：批准 1 项，拒绝 0 项；未选中的继续等待。`
  * 刷新页面（同一后端进程）后仍是「执行中」。
- 真实跨进程：直接重启 uvicorn（同一数据库）后查询：

      BEFORE RESTART: i_3b1568cda53e running   recovery: {"paused":[]}
      AFTER  RESTART: i_3b1568cda53e paused    recovery: {"paused":["i_3b1568cda53e"]}
      DB: i_3b1568cda53e | running | owner= True   （重启前）

### 2.5 暂停任务「按当前材料继续」闭环 —— 复现（修复后新增），成立

- HTTP 级（`test_paused_resume_requires_confirm_and_refreshes_material_watch`）：
  running → 改材料保存 → 服务端把它置 paused（保留进度、清 owner）→
  不带确认 approve：`ok=False, reason=confirm_required`，仍是 paused →
  带 `confirmDependency=true`：`ok=True`、status=running、
  `__materialWatch` 已刷新成当前材料指纹、`done/total` 保留、重新记录 `__ownerInstance`。
- 终态不可复活（`test_closed_intents_cannot_be_revived_by_approve`，4 组参数化）：
  rejected / failed / cancelled → `reason=closed`；done → `reason=done`；状态都不变。
- 真实界面（截图 v-13 / v-14）：暂停后按钮变成 `data-im="resume"`「继续（按当前材料）」，
  点击后回到「执行中」，提示「已按当前材料继续；不会自动重试，也不会重新执行已完成的部分。」，
  没有误报「上次没有结束的任务已经暂停」。

### 2.6 失败撤回（真实界面）—— 复现，成立

界面路径：单条批准（执行中，刷新页面后仍执行中）→ 演示「成功」→ 板面新增 `QIO 结果` 卡片 →
用户随后勾选了一条注释 → 演示「失败（撤回）」。

实际输出与数据库：

    eval: {"notice":"任务失败，已撤回本任务造成的改动 1 项。","impact":true}
    board cards: [file, file, text(checked=True), text(checked=False), reply(deleted=True)]
    i_e40cd8de4c9f failed | revert: {"reverted":["卡片 c_cb4e3f431f44（QIO 结果...）：本任务新增的结果卡片，已撤回"],
                                    "kept":[],"pendingDecision":[],"reasonText":"任务失败，已撤回本任务造成的改动 1 项。"}

结论：任务新增的结果卡片被撤回（`deleted=true`），用户后续的勾选修改保留（`checked=True`）。

---

## 3. 真实界面走查（截图）

服务：我的独立后端 `127.0.0.1:8792`（`QIO_DEV_INSECURE=1`、`QIO_DATA_DIR=%TEMP%\qio-verify`）
+ 前端 `127.0.0.1:5298`（没有占用 8734/5199/8791/5299，也没有杀别人的进程）。

用 `node scripts/interactive-verify/run-probe.mjs <steps.json>` 驱动真实 Chrome 走完整流程：

| 截图 | 内容 |
| --- | --- |
| v-01-authored-one-checked | 2 张材料 + 2 条注释，只勾选第一条；底部显示「本次允许 QIO 查看：注释 1 条、材料 2 项。另有 1 张卡片不在其中」 |
| v-02-submitted | 提交成功：「本次 3 项改动已记录；第一阶段没有接入 QIO 模型调用…提交成功后已自动取消 1 条注释的勾选（不是删除或撤回）」 |
| v-03-demo-intents | 4 项演示意图 + 演示徽标 + 板面虚线预览（20 个 preview 元素，16 个虚线）|
| v-11-batch-approve-fixed | 批量批准后同一进程内仍「执行中」，无暂停横幅 |
| v-13-paused-with-resume | 暂停后出现「继续（按当前材料）」|
| v-14-after-resume | 继续后回到「执行中」 |
| v-09-done-reply-on-board / v-10-failed-revert | 成功落地结果卡片 / 失败撤回后卡片消失、用户改动保留 |
| v-16 / v-17 | 取消勾选 + 删除注释，再提交：界面显示「没有可提交内容…未更新基准、未调用 QIO」 |

截图目录：`%TEMP%\qio-visual\shots`。

对 Lead 的 `scripts/interactive-verify/ui-scenarios.mjs` 的独立判断：
脚本覆盖「加材料 / 写注释 / 只勾选一条 / 提交 / 演示意图 / 虚线预览 / 批量条件」，
**没有覆盖**批量批准的实际点击、依赖等待、失败撤回与材料影响闸门；
断言多为「元素存在 / 文本包含」，对「操作是否真的落库」只在少数几处用后端接口交叉核对。
我另外亲手走通了批量批准、暂停继续、失败撤回（见 2.4 / 2.5 / 2.6）。

---

## 4. 对抗性尝试

| 尝试 | 结果 | 证据 |
| --- | --- | --- |
| 提交请求里塞未勾选卡片 id | 无效，被忽略并说明 | 1.1 |
| 连续两次提交同样内容 | 第二次起 `empty`（不会重复调用、不推进基准）；`succeeded` 记录只有 1 条 | 4.1 |
| 同一窗口「删除又加回同样内容」 | `duplicate`，基准不更新 | 4.1 |
| 冲突对一起送进批量批准 | 两项都不批准 | 4.2 |
| 依赖任务不带 `confirmDependency` | 只到 `waiting_dependency`；前项完成后 `waiting_confirm`；仍需确认 | 4.2 |
| PUT /state 传结构异常状态 | normalize 修正 G1–G8 | 4.3 |

### 4.1 重复提交 / duplicate —— 复现，成立（状态命名有出入）

    ### DUPLICATE submit (same visible content twice)
    1st: succeeded baseline: {'updated': True, 'firstSubmission': True, 'seq': 1}
    2nd (identical): empty | delivery: 没有调用 QIO（empty）：没有可提交内容...
    3rd: empty
    rows: [(3,'empty'), (2,'empty'), (1,'succeeded')] | succeeded 行数: 1

    ### DUPLICATE (single-window delete+re-add identical)
    1st: succeeded
    same-window delete+readd -> status: duplicate | baseline: {'updated': False, ..., 'seq': 1}
      delivery: 没有调用 QIO（duplicate）：与上次成功提交内容一致，未重复提交、未更新基准
      expressions: ['material_added', 'material_removed']

结论：**重复点击不会重复调用、不会推进基准**（契约要求的部分成立）；
但「连续两次提交同样内容」返回的是 `empty` 而不是 `duplicate`
（因为前后两份投影相同 → 表达式为空 → 先命中 `empty`）。两种状态都不更新基准，
行为安全，只是状态名与「重复点击」的直觉不符。见问题 5.6。

### 4.2 冲突与依赖 —— 复现，成立

    ### ATTACK 1: batch approve a conflicting pair together
    HTTP 200 {"ok":true,"results":[{"ok":false,"reason":"conflict","detail":"与同一批里另一项互不相容：不能同时批准，两项都没有批准。"...(两项都 conflict)}],
              "approved":[],"rejected":[]}
    after: 两项都仍是 pending

    ### ATTACK 2: batch approve dependent task without confirmDependency
    HTTP 200 {... "ok":true,"intent":{... "status":"waiting_dependency" ...}}
    after: 依赖项 waiting_dependency（没有开始）

    ### 依赖链（单条路径）
    approve(前项) → running；advance(done) → 依赖项 waiting_confirm；
    不带确认 approve → ok=False, reason=confirm_required；
    带 confirmDependency → ok=True, running

界面侧：批量列表会把与已选项冲突的项归入「只能拒绝」的分支（IntentBatchList 的 approveSplit），
我点击两个冲突项的复选框时按钮显示「批量批准（1）」，只提交了一项 —— 客户端和服务端都挡住了。

### 4.3 结构异常状态的 normalize —— 复现，成立

`PUT /state` 传入：重复 id 的卡片、重复成员、指向不存在卡片的成员与链接、
空组、两张卡争抢同一组、已删除卡片进组 / 进选择、`hidden=true` 且 `checked=true`、
`reply` 卡片 `checked=true`。

    G6 hidden->checked: [False]                 # 隐藏强制取消勾选
    G7 reply->checked: [False]                  # QIO 结果恒不勾选
    G1/G3 groups: [('g_ord', ['c_a', 'c_b'])]   # 去重、清掉不存在/已删除成员、空组消失、跨组冲突只留一个
    card c_dup survivors: ['first']             # 重复 id 只留一条
    G4 links: [('l_ok', 'c_a', 'c_b')]          # 悬挂链接、重复对、已删除链接都被清掉
    G8 selection: ['c_a']                       # 已删除 / 不存在的 id 被清掉
    deleted card in groups?: False
    deleted card in selection?: False
    defaults filled for bare cards?: ['bookmarked','checked','content','createdAt','deleted','folded','h','hidden','id','kind','meta','updatedAt','w','x','y']

字段缺失会被补齐默认值，行为稳定；`test_normalize_fixes_duplicates_dangling_links_and_empty_groups` 固定了这批不变式。

---

## 5. 问题清单（按严重度）

### 5.1 阻断 1（已修复，复测通过）：未勾选注释借「撤回」回到 before

- 现象：注释先勾选并成功提交 → 取消勾选 → 删除 → 再提交，`before` 里重新出现它的
  文字、id、关系链接 id 与链接含义；`GET /state` 的 `pending` 预览同样泄露。
- 契约依据：§1.4「未勾选 = QIO 完全看不到它的文字与注释链接，**包括提交的前后状态**」。
- 复现：`scratch-verify/explore5.py`（输出见 1.5）；回归用例 `test_revocation_does_not_return_to_before`。
- 修复后复测：HTTP 级与真实界面均通过（见 1.5）。

### 5.2 阻断 2（已修复，复测通过）：批量批准被下一次列表刷新立即暂停

- 现象：`POST /intents/batch` 批准后进入 running，但紧接着一次 `GET /intents`
  就把它降级为 paused；界面上「批量批准」后立刻显示「已暂停」。
- 根因：`batch_decide` / 路由没有透传 `instance_id`，progress 里没有
  `__ownerInstance`，被 `recover_running_intents` 当成「别的进程遗留」。
- 复现：`scratch-verify/explore3.py`（输出见 2.4）；回归用例
  `test_batch_approved_intent_survives_intent_list_refresh`、
  `test_batch_approve_survives_same_process_but_pauses_in_new_instance`。
- 修复后复测：同进程 running、跨进程 paused，与要求一致（见 2.4）。

### 5.3 重要（未修，本轮新发现）：材料影响闸门 + 标记冲突，让「编辑→重开」在特定状态下静默失效

**现象**：只要存在一个「材料依据已失效」的 paused（或 running）意图，
**之后每一次板面编辑都不会自动保存**，界面停在「有改动尚未保存（保存不调用 QIO）」，
必须由用户手动点「继续：保存改动」才会落库。

**证据**（真实界面探针，`scratch-verify/steps-net.json` / `steps-real.json` / `steps-rearm.json`）：

    eval: {"cards":1,"saveStatus":"有改动尚未保存（保存不调用 QIO）"}
    eval: clicked [data-im="impact-continue"]
    eval: {"calls":[...GET state 200, POST material-impact 200, GET state 200...],
           "saveStatus":"有改动尚未保存（保存不调用 QIO）","noticeGone":false,
           "err":"这次改动还没有保存：它会影响到正在执行的任务。［演示］把材料整理成可执行清单
                  这项任务已经因为材料变化暂停：继续保存不会自动继续它，需要你确认；..."}
    → 点击后 **没有任何 PUT**，闸门仍在。

    eval: clicked 文本以「继续：保存改动」开头的按钮（InteractiveView 里真正的那个）
    eval: {"calls":[...,{"u":".../state","m":"PUT","s":200},{"u":".../visible-range","m":"GET","s":200}],
           "saveStatus":"已保存（19:03:22），保存不调用 QIO","noticeGone":true}
    → 真的按钮有效：PUT 200、已保存；重开页面后卡片还在（cardsAfterReopen: 1）。

    eval: {"gateReArmed":true,"realContinueButtons":1,"markedContinueButtons":1,"markedNoticeSections":1}
    → 保存完再编辑一次，闸门**再次出现**（因为 paused 任务的材料依据永远不一致，直到用户继续/拒绝它）。

**附带问题（标记冲突）**：真正的「继续保存」按钮（`frontend/src/views/InteractiveView.vue:88`）
**没有任何 `data-im` 标记**；而 `data-im="impact-continue"` /
`data-im="impact-notice"` 属于 `components/interactive/ReplyPanel.vue` 里
**另一个用途**的 `IntentImpactNotice`（`@confirm="revertRest(...)"`、信息提示）。
任何按标记驱动的自动化都会点错按钮 —— 我第一次也踩了这个坑。

**连带影响（可复现）**：Lead 自己的验收脚本在同一份代码上，结果完全取决于后端状态：

| 后端状态 | `ui-scenarios.mjs` 结果 |
| --- | --- |
| 干净数据目录 | **18 项全过**（`合计 18 项，通过 18 项，失败 0 项`） |
| 库里存在一个材料依据失效的 paused 意图 | **1 项通过、5 项失败**（加卡片后不落库 →「重新打开…卡片 0」→ 后续全崩） |

命令：

    $env:IM_APP='http://127.0.0.1:5298'; $env:IM_BACKEND='http://127.0.0.1:8792'
    node scripts/interactive-verify/ui-scenarios.mjs

**与契约的关系**：契约 §1.6 说的是「**执行中**用户要改相关材料：先说明受影响任务并要求确认」。
服务端对已暂停任务明确「只报告、不重复暂停」（`intents.on_board_saved` 注释与实现），
前端却仍然把保存拦下来。前端比服务端更严，且对 paused 任务是**无限期反复拦**。
另外脚本没有回答这个闸门的步骤，所以「编辑→重开恢复」这条链在演示环境里会静默断掉。

**判级理由**：不是权限边界硬边界，也不是主流程必挂；但它会让用户在不知情时丢改动
（辅助区在右侧可收起，提示在板面上方，容易被忽略），也让验收脚本不可复现，所以判「重要」。

### 5.4 次要（未修）：重新打开后显示「尚未保存过」，与后端保存序号不符

- 现象：板面已保存到 seq 12+，重新打开页面时底部显示「尚未保存过（保存不调用 QIO）」。
- 原因（读码）：`GET /api/interactive/boards/{id}/state` 返回了 `seq` 但**没有 `savedAt`**；
  前端 `stores/interactive.ts:191-201 refreshBoardFromServer()` 只把 `saveStatus` 置为 `idle`，
  从不回填 `lastSavedAt`，于是 `SubmitPanel` 渲染成「尚未保存过」。
- 证据：探针重开后 `{"cards":1,"saveStatus":"尚未保存过（保存不调用 QIO）"}`（板面上有已保存的卡片）。
- 影响：用户可能误判自己的板面从未保存。属显示准确性问题，不影响落库。

### 5.5 次要（未修）：「暂停」的任务没有「放弃 / 取消」出口

- 现象：暂停的任务不能拒绝（`DECIDABLE_STATUSES` 不含 paused → `reason=not_rejectable`；
  前端 `approval.ts` 也把 paused 的拒绝置为不可用）。要放弃只能先「继续」再走演示推进到 cancelled。
- 证据：读码（`intents.reject_intent`、`approval.ts`）+ 修复后新增的前端用例
  `暂停的任务可以「按当前材料继续」，但必须先确认` 里也断言了 `rejectAvailability(paused).allowed === false`。
- 说明：契约只写了「拒绝后预览消失、原内容保留」，没说暂停态能否拒绝；第一阶段演示环境可以走通，
  真实执行接入后「暂停了但我不想继续」会没有出口。**这条是读码 + 已有用例交叉验证，不是我独立跑的端到端。**

### 5.6 次要（观察）：重复点击提交的状态名是 `empty` 而不是 `duplicate`

见 4.1。契约要求的不重复调用 / 不更新基准都成立，只是状态名与「重复点击」的直觉不符；
`duplicate` 只在「同一窗口内撤回又加回同样内容」时才出现。建议要么改名，要么在
`empty` 上补一个 `unchanged` 之类的提示。

---

## 6. 没能验证 / 明确未覆盖

1. **真实模型失败路径**：第一阶段没有接入 QIO 模型调用，提交失败只能用注入异常复现（见 2.3）。
   「模型超时 / 额度不足」这类真实失败没有验证，也无法验证。
2. **前端全量静态检查**：我没有跑 `npx vue-tsc --noEmit` 与前端全量 `vitest`
   （不属于我的复核范围，由 Lead 验收）。我只跑了后端 5 个互动相关测试文件（**150 项全绿**）与我自己新增的 26 项。
3. **前端纯函数语义**：`frontend/src/interactive/*.ts` 的排序 / 分组 / 预览比较等纯函数
   没有由我独立跑用例核对（只按需读码）。
4. **多板面 / 并发编辑**：契约 §7 明确第一阶段不做，我也没验证。
5. **性能与长板面**：没有做规模测试（快照上限 200、草稿上限等只在读码时看到）。
6. **环境说明**：这个工作区的 `frontend/node_modules` 是指向主检出的 junction，
   `scripts/interactive-verify/vite.e2e.config.ts` 里的 `import { defineConfig } from "vite"`
   在 `scripts/` 目录下解析不到，直接按 Lead 给的方式起前端会
   `Error: Cannot find module 'vite'`。我在工作区根建了一个
   `node_modules` junction（已被 `.gitignore` 覆盖，不会提交）才跑起来。

---

## 7. 复现方式

    # 后端（我的端口 8792，未占用 8734/5199/8791/5299）
    cd D:\qio-dev\qio-int-verify
    git merge --ff-only feat/interactive-foundation     # 复核时 HEAD = 8903ab1
    cd backend
    .venv\Scripts\python.exe -m pytest tests/test_interactive_verify.py -p no:cacheprovider -q

    # 全部互动相关后端测试
    .venv\Scripts\python.exe -m pytest tests/test_interactive_verify.py tests/test_interactive_submission.py tests/test_interactive_intents.py tests/test_interactive_board.py tests/test_interactive_integration.py -q

    # 真实界面
    $env:QIO_DEV_INSECURE=1; $env:QIO_DATA_DIR="$env:TEMP\qio-verify"; $env:PYTHONPATH="D:\qio-dev\qio-int-verify\backend\src"
    .venv\Scripts\python.exe -m uvicorn agent.main:create_app --factory --host 127.0.0.1 --port 8792
    $env:VITE_QIO_BACKEND_URL='http://127.0.0.1:8792'
    node frontend/node_modules/vite/bin/vite.js --config scripts/interactive-verify/vite.e2e.config.ts --port 5298 --strictPort --host 127.0.0.1
    node scripts/interactive-verify/run-probe.mjs scratch-verify/steps-ui-b.json      # 批量批准
    node scripts/interactive-verify/run-probe.mjs scratch-verify/steps-ui-e.json      # 暂停 → 继续
    node scripts/interactive-verify/run-probe.mjs scratch-verify/steps-ui-c.json      # 成功 → 失败撤回

（`scratch-verify/*.json`、`scratch-verify/*.py` 是本次复核的临时复现脚本，**未提交**，
只留在工作区；报告里的输出都是从它们与数据库直查里抄下来的原文。）

---

## 8. 修复后复测结论

- 8903ab1 上，首轮报告的两条阻断缺陷（5.1、5.2）**都已确认修复**：HTTP 级 + 真实界面 + 跨进程重启三重验证。
- 权限边界的字面硬边界（含链接、含 `pending` 预览）现在成立。
- 新增的「暂停任务按当前材料继续」闭环成立，终态不可复活。
- 新发现 1 条重要问题（5.3，材料影响闸门 + `data-im` 标记冲突，会连带让验收脚本不可复现）
  与 2 条次要问题（5.4、5.5），**我没有修改产品代码**，只在此报告。
