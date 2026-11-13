# #28 正式提交只能读取提交时的版本 —— 返修证据（backend-28）

日期：2026-11-14
worktree：D:\qio-dev\pm-qio032-integration（分支 fix/pm-qio032-integration，基线 7445170d 合并后）
写入范围：backend/src/agent/interactive/board_tools.py、materials.py、backend/src/agent/tools/attachment_tools.py、
backend/src/agent/services/turn_orchestrator.py、backend/tests/pm_qio032_*（新增/改）、
局部夹具更新见第 6 节（lead 已裁决同意）。**未改前端、未改 docs、未改迁移、未改 submission.py / intent_service.py。**

证据文件（本目录）：
- evidence-28.md（本文件：复现步骤 / 预期 / 实际 / 映射 / 断言改写说明）
- evidence-28-summary.txt（两次 fake provider 验收的机器可读摘要：工具调用参数、工具返回、模型请求标记）
- evidence-28-pytest-s.txt（pytest -s 全量输出，含上面的摘要行；明文含系统提示词，仅本地取证用）

---

## 1. 问题与复现（修改前 → 修改后）

### 1.1 绑定提交但快照不可用 → 旧实现回退当前板面（#28 主问题）

复现（旧实现，见基线 pm_qio032_material_scope_test.py::test_latest_read_falls_back_to_current_when_submission_snapshot_missing）：
1. 板面保存一张文字卡「当前板面正文」；
2. 直接插入一条只有 id/board_id/seq/status 的提交行（没有 visible/after_state 快照）；
3. 绑定该提交调用 board_read_cards / board_inspect。

预期（#28）：不得读取当前板面 → 返回可识别的失败 + 提示重新提交，且结果里没有当前正文 / 关系。
实际（旧）：ok=True、data["cards"][0]["content"] == "当前板面正文"、basis.source="current" +
basis.fallback —— 把提交快照故障显示成一次正常读取（还把提交之后才保存的 B 版当成 A 版）。
实际（新）：ok=False、status=failed、code=submission_snapshot_unavailable、
error="读不到这次提交的内容，请重新提交。"、data.basis.available=False、
data.diagnostics.snapshot 带数据库层原因；data 里没有 cards / links / counts。

对应断言（新）：backend/tests/pm_qio032_material_scope_test.py::test_bound_turn_with_missing_snapshot_fails_and_asks_resubmit
（以及 test_bound_turn_without_submission_row_never_reads_current_board、test_damaged_snapshot_is_failure_not_empty_board、
test_snapshot_from_other_board_is_recognizable_failure）。

### 1.2 显式 source=submission 把快照故障显示成「正常空结果」

复现（旧）：提交行存在、没有快照时，board_read_cards(source="submission") 返回
ok=True + cards=[] + basis.source="current"。
预期：失败（不是空板面）。实际（新）：同一 submission_snapshot_unavailable 失败；提交行缺失 / 归属别的板面时
仍是原有的 submission_not_found（同样失败，同样不给当前数据）。

### 1.3 旧记录「同名同大小」被当成内容未变

复现：提交快照没有 materialVersions（旧记录），提交后把文件换成同字节数的另一份内容。
预期（需求 3）：同名同大小不足以证明未变 → 不交付、提示重新提交。
实际（旧）：只按 size 核对 → size 相同 → 当作没变化，把新内容交付给模型。
实际（新）：SUBMISSION_MATERIAL_UNVERIFIABLE → 材料条目 read=false + reason 含「重新提交」；
read_attachment 返回 material_source_changed。

### 1.4 核验后又读一次（同名路径 TOCTOU）

旧实现：先用路径算 sha256 核对，再让解析器按同一路径再读一次；两次之间文件被替换时，
「核对的是 A、交给模型的是 B」。
新实现：verified_submission_material_bytes() **一次读取 + 校验**，返回的字节通过
content_overrides 直接交给解析器（落同后缀临时副本解析，随即清理）；图片 data URL 也用这同一份字节。
read_attachment 侧：读取前核对提交冻结的 sha256，读取后再核对一次同一路径的内容哈希；
期间版本变化（或无法再核对）→ material_source_changed，不交付读取结果。

---

## 2. 需求 → 实现 → 证据

| 需求 | 实现 | 证据（测试） |
| --- | --- | --- |
| 1. 绑定提交的轮次统一按该提交读取（board_inspect / board_read_cards 默认+显式 / read_attachment / 首轮材料入口 / 绑定追问） | board_tools._submission_basis 只读快照、失败不回退；BoardInspect/BoardReadCards 两路都走同一判定；materials.load_submission_snapshot 是唯一事实源；read_attachment 用 submission_attachment_scope；turn_orchestrator 首轮/追问都走 submission_materials | pm_qio032_submit_version_basis.py::test_bound_followup_turn_reads_only_submission_version（真实流水线 + fake provider，工具真实执行）、test_bound_first_round_with_missing_snapshot_marks_materials_unavailable、pm_qio032_submit_first_round_materials.py（真实受理首轮 TXT/PNG） |
| 2. 快照缺失/损坏/归属不匹配/不可读 → 可识别失败、短文案、不给当前板面、不当零张卡 | submission_snapshot_unavailable（失败，recoverable=False，next_step=resubmit）+ data.diagnostics；_snapshot_shape 加严（cards/groups/links 必须是 list）；首轮材料块 materials_status=unavailable + materials_notice 短文案 + materials_reason | material_scope_test：missing / without_row / damaged_snapshot / other_board 四个用例；submit_version_basis：test_bound_first_round_with_missing_snapshot… |
| 3. 源文件变化 / 不可读 / 旧记录无法核对 → 只交付能确认属于提交当时的材料；同名同大小不算证据；交给模型的内容就是核验过的内容；旧提交可用历史仍可查看 | materials.submission_material_verdict / verified_submission_material_bytes（旧记录无 sha → 不交付；读一次+校验同一份字节）；read_attachment 前后各核对一次 | material_scope_test：test_same_size_content_replacement_is_detected_with_stamped_versions（既有，真实受理路径）、test_submission_materials_mark_changed_source_as_needs_resubmit、test_legacy_snapshot_still_reads_available_history_cards；submit_version_basis：test_legacy_snapshot_without_frozen_versions_is_not_delivered、test_unreadable_source_is_not_delivered_and_asks_resubmit |
| 4. 普通互动/未绑定提交保持原规则；隐藏/排除/提交后新增不进绑定轮次 | _submission_basis 仅在有 submission_id 时走快照；未绑定照旧读当前板面；snapshot 可见范围来自 submission.project_snapshot / models.selectable_cards（同一授权判断） | material_scope_test::test_board_tools_reject_unbound_normal_mode、test_bound…（原三态用例）；submit_version_basis::test_unbound_interactive_turn_still_reads_current_board、test_bound_followup…（隐藏/排除/新增附件分别 not_in_submission_scope / not_readable，正文不出现） |

### fake provider 验收（硬要求）摘要

用例：pm_qio032_submit_version_basis.py::test_bound_followup_turn_reads_only_submission_version
- 本地 fake provider（_ToolThenAnswerAdapter，不联网、不用真实凭据）：第一轮模型调用发 8 个工具调用，
  第二轮带上真实工具结果并给最终回答；记录每次模型请求的完整 messages。
- 绑定轮次：提交 A（真实 submission.submit_board 受理，冻结 materialVersions）→ 保存 B（改正文、
  加新文字卡 / 新附件、把 A 的 TXT **同字节数替换**）→ 同一绑定追问轮真实执行：
  - 工具调用参数：board_inspect{}、board_read_cards{}、board_read_cards{source:"submission"}、
    read_attachment{attachment_id:<A TXT>}、read_attachment{<B-only>}、read_attachment{<A PNG>}、
    board_read_cards{source:"submission", card_ids:[<B 新卡>]}、read_attachment{<隐藏附件>}；
  - 工具返回（真实执行结果，见 evidence-28-summary.txt）：
    - board_inspect：ok=true，basis.source=submission，submission_id=sub_…，卡片正文含「A 版正文：预算 100 万」，
      关系只有「A 版关系」，提交时的 4 张卡（文字×2 + TXT + PNG），没有 B 的新卡 / 隐藏卡 / 排除卡；
    - board_read_cards（默认 latest 与显式 submission 两路）：ok=true，basis.source=submission，
      submission_id 同一值，卡片正文是 A 版；
    - read_attachment(A TXT)：ok=false，code=material_source_changed，原因说明「内容哈希与提交时不一致（大小可能相同）…请重新提交」；
    - read_attachment(B-only) / read_attachment(隐藏附件)：ok=false，code=not_in_submission_scope；
    - board_read_cards(card_ids=[B 新卡])：ok=false，code=not_readable；
    - read_attachment(A PNG)：ok=true（未变化的 A 材料继续可读）。
  - 最终模型请求（第二次请求）标记：A 正文 / A 关系 / 重新提交提示 = true；B 正文、B 替换正文、
    B-only 附件正文、排除/隐藏正文 = false；第一次请求（含互动材料块）同样只有 A 内容 + 重新提交提示，
    且 A 版 PNG 作为图片内容块交付（images=1）。
  - 逐项可复核的 JSON：D:\qio-dev\_lead-logs\integration-032\evidence-28-summary.txt
    中 PM-QIO032-28-SUMMARY {"case": "bound-followup-a-then-b", ...} 行。

用例：pm_qio032_submit_version_basis.py::test_bound_first_round_with_missing_snapshot_marks_materials_unavailable
- 绑定首轮 + 提交行没有快照：第一次模型请求（互动材料块）含 "materials_status": "unavailable" +
  "读不到这次提交的内容，请重新提交。"，且**不含**当前板面正文；工具真实执行 board_inspect /
  board_read_cards 返回 submission_snapshot_unavailable，结果里没有当前板面正文。

---

## 3. 跑了什么命令、结果

工作目录 D:\qio-dev\pm-qio032-integration\backend，环境变量：
$env:QIO_DATA_DIR='D:\qio-dev\pm-qio032-integration\.tmp-test-data'; $env:QIO_DISABLE_DB_CHECK='1'

1) 基线（修改前，4 个既有文件）：pytest tests/pm_qio032_material_scope_test.py tests/pm_qio032_submit_material_versions.py tests/pm_qio032_submit_first_round_materials.py tests/pm_qio032_submit_authorization_facts.py -q
   → **34 passed**（起点状态记录）。

2) 修改后定向（5 个文件，含新增）：
   pytest tests/pm_qio032_material_scope_test.py tests/pm_qio032_submit_material_versions.py tests/pm_qio032_submit_first_round_materials.py tests/pm_qio032_submit_authorization_facts.py tests/pm_qio032_submit_version_basis.py -q
   → **43 passed**（无失败；中间失败全部定位到本次改动并修完，无跳过、无 xfail）。

3) 契约变更波及的既有用例（夹具更新后）：
   pytest tests/test_p2_d_participation_endpoints_display_version.py tests/test_p3_b_tools.py tests/test_p3_b_service.py tests/test_p3_d_tools_registry.py tests/test_p3_d_intent_resume_retry.py tests/test_fu2_topic_attribution.py tests/test_fu2_acceptance_topic_attribution.py tests/test_release_step2_d.py -q
   → **164 passed**。

4) 证据输出：pytest tests/pm_qio032_submit_version_basis.py -q -s → 5 passed，输出写入
   evidence-28-pytest-s.txt，摘要行单独写入 evidence-28-summary.txt。

5) 未跑：全量 pytest（按纪律由 lead 统一跑）；vue-tsc（未改前端）；真实 provider（按要求一律 fake/mock）。

### 与 #28 无关的既有失败（记录，未动）
tests/test_p3_d_static_boundaries.py::test_frontend_has_waiting_and_resume_entries：断言前端源码含「续作」，
当前 worktree 前端没有该词。我没有改前端；lead 已回复该条由他查（前端文案归属）。

---

## 4. 改了哪些文件（逐个）+ 意图

### backend/src/agent/interactive/board_tools.py
- _submission_basis()：绑定提交时只读提交当时冻结的快照；快照不可用 → available=False +
  空 state，**删除回退当前板面的分支**（这是 #28 的主违规点）。basis 失败时 source="submission"、
  available=False、reason/fallback 写可读原因。
- 新增 _submission_unavailable_failure()：统一失败结果（failed/submission_snapshot_unavailable/
  短文案 SUBMISSION_READ_FAILED_NOTICE/next_step=resubmit/data.diagnostics），不带任何卡片、关系、计数。
- BoardInspectTool._run()：basis 不可用直接失败，不构造任何当前板面信息。
- BoardReadCardsTool._run()：
  - source="latest"：basis 不可用 → 同一失败（不再回退当前板面）；
  - source="submission"：快照不可用且提交行存在 → 同一失败（不再返回空 cards + source="current"）；
    提交行缺失 / 归属不匹配 → 原有 submission_not_found，同样失败、同样短文案、同样不带当前数据；
  - 成功结果新增 submission_id 字段（只增），显式/默认两路都能核对提交身份。

### backend/src/agent/interactive/materials.py
- _snapshot_shape()：cards/groups/links 必须是 list，否则视为「不可用」（损坏快照不当空板面）。
- _material_content_sha256(..., respect_read_grants=)：区分「交付读取」（查授权）与「范围计算」
  （不查授权；范围计算是授权的前置判断，不能被授权结果反过来判死）。
- 新增 current_material_content_sha256()（read_attachment 读后复核用）、_material_path()。
- 删除只按大小核对的分支，新增：
  - SUBMISSION_MATERIAL_UNVERIFIABLE：旧记录没有冻结 sha256 → 同名同大小不算证据 → 不交付 + 重新提交提示；
  - submission_material_verdict()：只认可核对事实（不存在/非 ready/无冻结哈希/哈希不一致/算不出）；
  - verified_submission_material_bytes()：**一次读取 + 校验**，返回允许交付的同一份字节；
  - _read_grant_reason()：授权被拒不等于「源文件不可读 / 内容变了」；
  - material_change_reason() 保留为兼容入口（只回答原因）。
- submission_attachment_scope()：用同一核验函数（respect_read_grants=False），allowed 里**只增**
  sha256 字段（read_attachment 读后复核用）。
- submission_materials()：先「一次读取 + 校验」得到 verified_bytes，再通过 content_overrides
  交给 resolve_materials / resolve_images；陈旧/无法核对的材料不进解析、只作为 unreadable +
  重新提交提示；图片交付也用同一份核验字节。
- resolve_materials/resolve_images/_resolve_card/_file_entry/_attachment_entry/_deliverable_image：
  新增可选 content_overrides / verified 参数（默认 None = 行为不变）；有核验字节时解析/交付
  都用它（临时同后缀副本解析后立即清理），不再按路径读第二次。
- 新增 SUBMISSION_READ_FAILED_NOTICE（短文案唯一来源）。

### backend/src/agent/tools/attachment_tools.py
- _board_scope_denial() → _submission_material_guard()：返回 (拒绝结果|None, 核对事实)；
  提交范围不可用 / 材料 stale / 不在范围 → 结构化失败（protocol_version=1 + code + data.next_step），
  范围不可用时错误里带短文案与诊断。
- _read() 变薄包装：先核对范围 → 执行 _read_content() → **读后再核对一次内容哈希**；
  期间版本变化 / 无法再核对 → material_source_changed，不交付读取结果（避免「核对 A、交付 B」）。
- _read_content()：原 _read() 主体（行为不变），不再自己调用范围核对。

### backend/src/agent/services/turn_orchestrator.py
- 首轮 / 绑定向后的材料入口（submission_materials 调用处）：
  - 可用：materials_status="available"；
  - 不可用：materials_status="unavailable" + materials_notice（短文案）+ materials_reason（诊断），
    仍带空 materials 列表但明确不是「正常空结果」；
  - 入口缺失 / 交付异常：同样标 unavailable + 短文案 + 可读原因（不再静默没有 materials 键）。

### backend/tests
- 修改 pm_qio032_material_scope_test.py：反转快照缺失用例（见第 5 节），新增
  missing-row / damaged / other-board / legacy-history 用例；_record_submission 增加 svc 参数，
  默认按生产路径冻结 materialVersions（6 处调用传入 svc）。
- 新增 pm_qio032_submit_version_basis.py（5 个用例）：fake provider 绑定追问轮、首轮材料不可用、
  旧记录不交付、源文件不可读、未绑定互动照旧。
- 三个既有文件只动夹具（lead 已裁决，见第 6 节）。

---

## 5. 既有断言被改写 & 为什么不算降低覆盖

1) pm_qio032_material_scope_test.py::test_latest_read_falls_back_to_current_when_submission_snapshot_missing
   → 重写为 test_bound_turn_with_missing_snapshot_fails_and_asks_resubmit。
   原断言（ok=True、basis.source="current"、cards[0].content=="当前板面正文"）验证的正是 #28 要禁止的
   回退行为，保留它会与需求 2 直接冲突。新用例断言更严：失败状态 / 失败码 / 短文案 / basis.available=False /
   诊断原因 / 结果不含当前正文与关系 / 不含 cards、links、counts。

2) pm_qio032_material_scope_test.py::_record_submission（测试夹具，非断言）增加 svc 参数：
   生产受理路径（submission.py）自 #28 起一定冻结 materialVersions；旧夹具造的是「没有冻结版本」的旧记录。
   夹具改为与生产同形后，原有用例继续覆盖正常交付路径；「旧记录无法核对」另立专门用例覆盖
   （test_legacy_snapshot_without_frozen_versions_is_not_delivered），覆盖不减反增。

3) 新增用例（不替代任何断言）：
   - test_bound_turn_with_missing_snapshot_fails_and_asks_resubmit
   - test_bound_turn_without_submission_row_never_reads_current_board
   - test_damaged_snapshot_is_failure_not_empty_board
   - test_snapshot_from_other_board_is_recognizable_failure
   - test_legacy_snapshot_still_reads_available_history_cards
   - pm_qio032_submit_version_basis.py 的 5 个用例（含 fake provider 绑定追问轮、源文件不可读、旧记录不交付）

pm_qio032_submit_material_versions.py / pm_qio032_submit_first_round_materials.py /
pm_qio032_submit_authorization_facts.py **未改断言，全部通过**。

---

## 6. 契约变更导致的既有夹具更新（lead 已裁决同意；只动夹具，断言一条不减）

#28 之前这些用例之所以通过，是因为「绑定提交但没有快照 → 回退当前板面」；现在这是被禁止的行为。

1) backend/tests/test_p3_b_tools.py::test_inspect_and_read_respect_visibility_and_versions
   bound(board_id=BOARD) 默认绑定 sub_p3_1，而 test_p3_b_support.add_submission 只插提交行、没有快照。
   改法：在用例里给该提交补一条真实快照（submission.project_snapshot(当前可见范围) 写入
   visible / after_state，两条 SQL 参数绑定），然后把 bound 块原样保留。断言未改。

2) backend/tests/test_p3_d_tools_registry.py::test_interactive_context_reads_the_bound_board
   绑定 submission_id="sub_1" 但库里没有该提交行。
   改法：保存 seed 卡片后插入一条真实提交行（id=sub_1，visible/after_state = submission.project_snapshot(state)
   的 JSON），其余不动。断言未改。

3) backend/tests/test_p2_d_participation_endpoints_display_version.py::test_tool_inspect_filters_group_members_and_links
   该用例原本借 bound() 的默认 sub_p3_1 绑定；补快照后，快照只含参与成员，会让组参与从 partial 变 all
   （快照投影本来就不含未参与成员），与原意（读工具按可信上下文过滤组/关系）无关。
   改法：改为未绑定提交的互动轮（bound(board_id=BOARD, submission_id="")）读当前板面。断言未改。

以上三个文件都不在 #28 的「定向验收必须做」清单里，但是集成后全量 pytest 会跑到；按 lead 裁决做最小夹具修复，
避免红着进集成。

---

## 7. 我为产品行为自主做的决定（可能影响原体验）

1. **旧提交（升级前、没有冻结 materialVersions）的文件材料不再交付，提示重新提交**，
   文本卡等快照内内容仍可查看（test_legacy_snapshot_still_reads_available_history_cards）。
   依据是需求 3「旧记录缺少足够证据核对版本时，只交付能够确认属于提交时的材料；相同文件名和相同大小
   都不足以证明内容未变」。影响：升级前提交过文件材料的旧轮次，再追问时会要求重新提交。
2. **超过 64MB（MATERIAL_HASH_MAX_BYTES）的材料不交付**：无法在核对后交付，避免把无法核对的内容当提交时内容。
   影响：大文件的提交轮追问需要重新提交（此前会按路径解析交付，无法证明是提交时的内容）。
3. **失败不可在同一轮内重试**（recoverable=False，next_step=resubmit）：重新提交会创建新的轮次；
   同一轮重试只会再读到同一份不可用快照。界面短文案只说一句，诊断原因放 data.diagnostics / materials_reason。
4. **授权被拒与「源文件不可读 / 内容变了」分开表述**（新增 _read_grant_reason），不把权限问题说成版本问题。
5. **read_attachment 采用「读前核对 + 读后复核」**：读取期间文件先变后变回（双重替换）这类极小概率窗口无法识别；
   其余「核对之后换成另一版本」的情况会被读后复核拦下并整体失败。
6. **快照形状加严**：cards/groups/links 不是 list 的提交记录按「不可用」处理，不再被当成空板面。
7. board_read_cards 成功结果新增 submission_id 字段；basis 新增 available 字段；scope.allowed 新增
   sha256 字段 —— 全部只增，不改既有字段语义。

---

## 8. 未完成 / 未验证

- 全量 pytest 未跑（纪律：只跑定向；由 lead 统一跑）。
- 前端未改、vue-tsc 未跑；界面短文案目前通过工具失败 error / 交互块的 materials_notice 传递，
  若前端需要按 code 做专门展示，需要前端组另接（未改前端是本次范围约定）。
- 真实模型 / 联网路径未验证（按要求 fake provider）。
- tests/test_p3_d_static_boundaries.py::test_frontend_has_waiting_and_resume_entries 既有失败与本任务无关（前端文案），
  按 lead 指示未动。
