# R9-H13 审批与授权统一补修：最终报告

> 基线：`r9h12/main` HEAD `e68fb9b75e6dd7b6e4eaf6dc879aafec364fa635`（生产代码 `e4e478b4`）。
> 修复分支：`r9h13/main`（集成）+ `r9h13/a` `r9h13/b` `r9h13/c` `r9h13/d` `r9h13/e`（工作流）。
> 契约：`docs/r9h13-contract.md`；根因与修改地图：`docs/r9h13-root-causes.md`。
> 本轮跳过网络安全部分；不新增沙箱/文件代理/全盘扫描/后台监听；未合并 main、未发布安装包。

## 0. 基线与交付提交

- 开工前 `git fetch` 核对：`r9h12/main` 本地与远端一致；`e68fb9b7` 与用户给出的基线一致。
  用户给出的 H12 定向 CI 提交 `245f76aa` 是 `e68fb9b7` 的**祖先**（`e68fb9b7` 只是其后追加的
  docs/status/证据提交），二者不是“后续提交”关系；本轮以 `e68fb9b7` 为代码基线。
- 交付记录更正（按要求）：H12 三个绿色定向运行（40122111129 / 40122291315 / 40122468968）
  实际对应提交 `245f76aac153bc621e86559173b0519c6562def1`，而不是 `4594358f`。
  `4594358f → 0a7a430b → 245f76aa` 是同一生产代码 `e4e478b4` 的验收测试平台修正与证据提交。
- 工作流分支 HEAD（未 push）：A `405e0c5c`、B `f6239f72`、C `edf8c802`、D `1f229158`、E `d18a4275`。
- `r9h13/main` 集成提交见第 8 节；每个工作流只改自己 owner 的文件，`security/engine.py` 由 Lead 独占。

## 1. 共享契约如何成为所有真实入口的必经步骤

`docs/r9h13-contract.md` §2 冻结了 12 类共享事实的唯一含义、生产者、验证者与消费者：

要求 id/version/status/chain；动作-对象配对；用户指定操作路径；最终执行路径；身份比较路径；
对象身份与生命周期；读取前内容与摘要；拟写入内容与摘要；影响集合与完整性；当前模式；
委派范围与撤销状态；审批期限/确认阶段/单次执行状态。

落地方式（不是新增一层抽象，而是把已有结构接到同一入口）：

1. **统一授权入口**：工具/命令/附件/审批恢复/子智能体只构造 `Effect`（动作+对象+影响）并调用
   `AuthorizationEngine.assess_limit_conflict` / `authorize` / `recheck_approved`；适配层不做
   名称/路径/摘要/身份推断。H13 把此前只写在专用分支里的判定（读取前置、逐影响子范围、内部
   确认次数、模式重核对）移入/复用共同入口。
2. **读取先于内容**：C1/C2 把元数据发现与正文读取拆开，`security_gate`/`launch`/`fs_tools`/
   `cmd_tools`/`approved_operations`/`attachments` 在读取正文、计算摘要、shebang 探测、补丁
   计算与重新核验之前消费同一个读取 assessment；拒绝时读取计数为 0。
3. **逐影响核对**：D3/F4/F5 统一使用“动作-对象一一对应”的影响集合（`limit_impacts` 为权威，
   缺失时按 effect kind 配对），不再用 effect 级 operations 覆盖全部对象。
4. **确认次数由后端决定**：§8 新增 `engine.required_confirmations(effect)`，提交时覆盖调用方值，
   重核对取 max；审批存储/决定/恢复/执行器按真实对象重算阶段；模型 ALLOW 不再能完成内部维护。
5. **身份/内容三事实分离**：E2/D1 分别表达身份、读取前内容、拟写入内容；D1 删除仅以
   path/size/mtime 为键的摘要缓存，每次都读真实内容重算。
6. **操作目标与比较路径分离**：§10 删除/回收保留用户指定目录项语义，realpath 只用于身份比较与审计。
7. **恢复只使用保存事实**：A1/A2 区分“新保存绑定”和“历史恢复”；继承/重写不得重新探测；
   恢复只采纳带保存时 identity/birth 事实且当前观测一致的候选，缺失则保持未解决。
8. **子范围逐动作逐对象**：F1-F6 在引擎 `_scope_limited`/`_scope_coverage`/`_grant_scope_id`/
   `child_scope` 与 `subagent_tool` 形成/校验子集；请求/授予/重试/撤销为结构化状态。

## 2. 逐项：问题、根因、修改位置、状态

### A1 普通补充导致旧禁止重新绑定（已修）
- 根因：amend 把“已合并最终集合”交给 `add_correction`，后者对整个继承集合重跑
  `attach_program_bindings`；继承条目的未解决事实没有作为“禁止再探测”的输入。
- 修改：`security/requirement_limits.py`（`attach_program_bindings` 新增 `fresh_limits`/
  `preserve_saved_facts`：只有本次新增/明确修改的对象才探测，其余逐字段原样继承）；
  `security/requirements.py`（`add_correction` 接收 fresh 范围，`apply_resolution` 只传本次新增；
  `set_structured_limits` 增加 `attach_bindings`/`observe_objects`）；`services/app.py`
  （`_mark_limits_unresolved` 传 `attach_bindings=False`，回写不整集重探测）。
- 验收：普通补充后 A 仍拒绝且 0 启动；未解决事实/候选/来源原样；同一 amend 新增的限制仍正常绑定；
  低层重写继承事实不重绑。

### A2 缺失历史身份却自动宣称候选已验证（已修）
- 根因：`_verify_saved_binding_candidate` 只验证“候选路径现在存在 + 当前身份可读”，
  不要求保存时 identity；路径对象被替换后会把新对象采纳为绑定，旧对象硬链接由拒绝变可执行。
- 修改：`security/requirement_limits.py`：缺保存时 identity 一律
  `candidate_historical_identity_missing`，不采纳/零写入；保留 known limits/候选/来源；
  `BindingRecoveryOutcome` 带结构化 reason/failure；`services/app.py` 提示“需要澄清限制对应的对象”；
  写回仍核对 id/version/status/chain_digest 与采纳事实。
- 验收：硬链接旧对象+原路径替换场景恢复 None、DB 原始 JSON 不变、旧/新对象都拒绝且 0 启动；
  用户澄清具体旧对象后，旧对象拒绝、可靠不同对象正常执行。

### A3 不同对象的同名限制被去重丢失（已修）
- 根因：去重键只有 `(operations, kind, value)`，忽略 `program_binding`/未解决事实/来源；
  精确解除也只按 `(kind,value)` 匹配。
- 修改：`security/requirement_limits.py`：去重键改为完整对象数据身份（含绑定 path/identity/birth/
  source、binding_state/failure/candidates；`observed_at` 不参与身份）；`remove_limits_precise`
  支持明确路径命中已绑定对象；一个 removed 对象命中多个不同绑定对象时抛
  `LimitRemovalAmbiguous`（调用方保留限制并标 unresolved）。规范化/序列化/合并/继承保持纯数据。
- 验收：同名同动作两条限制都保留、A/B 都拒绝 0 启动、C 正常执行；精确解除 A 后 A 放行 B 仍拒绝；
  裸名称解除保持未解决且不放宽。

### B1 未绑定的局部程序读取限制拒绝普通文件读取（已修）
- 根因：`object_state._unbound_path_decision` 在“无保存候选 + 限制名称当前无法解析”时对任意 path
  候选返回 UNKNOWN，引擎按冲突 DENY 三种模式，普通 report.txt 被永久拒绝。
- 修改：`security/object_state.py` 新增注册 basis `program_concept_file_read_out_of_scope`：
  仅当限制 kind=program_name、无有效绑定、无任何 binding_candidates、operation=read、候选是
  具体文件路径时，当前名称解析只作候选事实——解析到同一对象仍 UNBOUND（需澄清），
  解析到不同对象/解析失败 → UNRELATED（带 limit_kind/has_saved_candidates=False/read_path_scope/
  operation/candidate/candidate_path/resolution 事实，白名单校验通过）。
  execute/write/create/delete、入口名候选、有保存候选/绑定、显式 program 路径限制全部保持失败关闭。
- 验收：report.txt 三模式可读；错误对象不误放行；程序 0 启动；显式路径限制与硬链接保护不变。
- **残留边界**：未识别程序文件本体被按普通路径读取时无法保护（没有对象事实可比较），需通过
  专用澄清/绑定解决；本报告如实列明，不用其他禁止换取该边界。

### B2 限制对象删除后无关对象持续被拒（已修）
- 根因：`_path_core` 在限制路径身份不可读/消失时一律 UNKNOWN；限制条目没有保存时对象身份可比对。
- 修改：保存层（A 分支）：`requirement_limits` 对 kind∈{path,program} 且 value 为路径的精确对象
  在首次形成条目时一次性记录 `identity/birth_key/exists/observed_at/source="requirement_save"`
  （常量 `SAVED_OBJECT_FACT_KEYS`/`SAVED_OBJECT_SOURCE`），已存在不覆盖、恢复不重采；
  `object_state._path_core`（B 分支）优先用保存身份比较：相同 → MATCH（旧硬链接/别名仍保护）、
  不同 → UNRELATED `path_different_object`、同 identity 不同 birth → UNRELATED
  `path_different_lifecycle`；缺字段保持 UNKNOWN。
- 验收：A 删除后 B 继续可读（可靠不同对象）；A 的硬链接/别名仍拒绝；历史数据缺字段仍失败关闭。

### B3 同名入口解析到独立对象被误判 INVALID（已修）
- 根因：`_name_branch` 在“已绑定 + 入口名相同 + 当前解析身份不同”时直接 INVALID，未区分
  “同名入口解析到另一个独立对象”与“绑定路径上的对象被替换”。
- 修改：`object_state._name_branch`：同名入口 + 解析对象 identity 可读且不同 + 入口解析路径≠
  绑定路径 → UNRELATED `entry_resolves_to_other_object`（H13 扩展 condition，required facts 不变）；
  解析路径==绑定路径而身份变化 → INVALID `bound_path_object_replaced`；生命周期变化 → INVALID；
  解析失败/身份不可读 → INVALID（失败关闭）。
- 验收：独立程序 B 的绝对路径与同名入口都按当前模式执行；绑定路径被替换仍拒绝；解析失败仍拒绝。

### C1 RunProgramTool 拒绝前读取被禁止文件（已修）
- 根因：工具在统一判定前采集 launch facts（程序正文摘要、参数脚本、shebang 探测）。
- 修改：`security/launch.py` 支持 `include_content=False` 元数据事实与 `content_read_targets()`；
  `tools/cmd_tools.py` 在授权前做元数据级 limit gate，在内容读取/重新核验前核对最新读取权限。
- 验收：拒绝时 content_sha256/shebang/read_bytes = 0、启动 0、marker 不存在；同一程序相邻正常启动。

### C2 附件写回在读取禁止下仍读原文件（已修）
- 根因：附件写回准备在授权前读源文件；执行器读旧摘要前无读取权限核对；write effect 未带读影响。
- 修改：`services/attachments.py`（写回效果包含 read+write 影响、读取权限先于内容读取）、
  `tools/approved_operations.py`（执行前读取 permission 核对、批准不解除读取禁止）。
- 验收：明确读禁止时写回拒绝且读取 0 次；普通写回批准后读取+修改正常；写批准 ≠ 读批准。

### D1 摘要缓存掩盖内容变化（已修）
- 根因：`launch._PROGRAM_HASH_CACHE` 以 `realpath|size|mtime_ns` 为键，同大小同 mtime 复用旧摘要。
- 修改：`security/launch.py` 删除该缓存，每次读取真实内容计算摘要；工具/审查/直接执行/审批恢复/
  执行前核对同一语义。
- 验收：审查等待期同大小同 mtime 改写 → 拒绝、0 启动、digest 变化；未变化程序正常执行。

### D2 参数脚本静默遗漏（已修）
- 根因：只按后缀名单/shebang 识别参数脚本；达到 `_MAX_ARGUMENT_FACTS` 静默 break，不声明不完整；
  `python xxx.txt` 不入绑定。
- 修改：`security/launch.py`/`tools/cmd_tools.py` 按解释器 argv 语义识别入口脚本（含 -c/-m/-File
  内联）；采集返回 (facts, complete, reason)，有界 128 候选/8MB 单文件/64MB 总量；超限
  `argument_files_complete=False` 显式标记并拒绝按完整方案执行；spec_digest 含完整性字段。
- 验收：python job.txt 同大小同 mtime 改写被拒；18 个真实 shell 脚本第 18 个改写被拒且 0 marker；
  控制组 18 个真实执行；200 候选时显式不完整+原因。

### D3 递归删除命令遗漏子对象（已修）
- 根因：命令执行只带 `detect_delete_intent` 的显式对象；递归删除目录未展开子对象。
- 修改：`security/deletion.py`（`DeleteIntent.recursive`：rm -r/-R/--recursive、rd/del/erase /s、
  git rm -r）、`tools/cmd_tools.py` 复用 `fs_tools.directory_impact(include_content=False)` 与内部
  对象检查，把真实子对象写进 effect targets/limit_impacts/审批指纹/执行前核对；不完整 →
  `delete_impact_incomplete` 拒绝。
- 验收：`rd /s /q`（rm -rf）命中被禁止子文件即拒绝且目录保留；fs_delete 同一影响同样拒绝；
  控制组真删；大目录未枚举到被禁止文件时拒绝且全保留。

### E1 检查旧路径、实际写入新路径（已修）
- 根因：执行器按审批 payload 旧 path/源引用核对，写入时重新 resolve 当前引用写新 path。
- 修改：`tools/approved_operations.py`：最终真实目标（路径、用途、范围）逐项参与最新限制、
  路径范围与审批绑定核对；identity 一致不等于路径/用途/范围一致；不得检查一个路径执行另一个路径。
- 验收：审批等待期把附件引用改到禁止目录同一对象硬链接 → 执行前拒绝、0 写、目标不变。

### E2 正常写回/回收被误判内容变化（已修）
- 根因：`content_sha256` 同时承载读取前内容/拟写入内容/identity；执行器把新内容摘要与磁盘旧内容
  比较；回收把 identity 当 content_sha256。
- 修改：`tools/approved_operations.py`/`security/recheck.py`/`services/attachments.py`：
  分别表达与核对 identity、读取前内容、拟写入内容；`RecheckService.content_digests` 不再传新内容或
  identity；直接允许与审批恢复同一规则。
- 验收：文件未变化时正常写回/回收完成；真实变化/身份变化仍使旧方案失效；无外部变化不再误报。

### E3 一个子项失效导致全部失效（已修）
- 根因：批量执行器把共同条件与逐项条件混在一起，共同条件之外仍整批失败；逐项 effect 没有逐项重核对。
- 修改：`tools/approved_operations.py`：整批只核对共同条件（要求 active/版本、期限、模式、确认
  基础）；每项使用自己的真实效果/对象/内容/限制/确认要求；共同条件撤回/过期停止整批；单项失效
  只影响该项，保留独立有效项的执行与真实结果。
- 验收：只对 B 新增禁止时 A 继续执行并报告 partial；全部失效/共同条件失效按规则停止。

### §8 内部维护三次确认统一强制（已修）
- 根因：确认次数来自调用方 `confirmations_required`；受限模式模型 ALLOW 可完成内部维护；
  存储/决定/执行前只读存储值，历史错误 1 次记录被当完成；批量/非首项未按真实对象重算。
- 修改（Lead 引擎）：`engine.required_confirmations(effect)` 按真实影响对象给出最低次数
  （内部身份登记读取失败按 3 保守处理）；`engine.submit` 覆盖调用方值；`recheck_approved`
  取 max 且“需要 1 次”保持原语义（确认本身即完成）；内部维护不进入模型审查（跳过 ALLOW）。
  修改（D 审批管线）：`storage/approvals.py` 在 decide/confirm 按真实对象校正历史行并重算
  operation_digest；`tools/approval.py`/`tools/approved_operations.py` 提交/批准/恢复/执行前
  按实际 confirmed_stage 核对；附件写回/回收/批量内部对象都接入；内部维护只核元数据不读正文。
- 验收：内部对象最低 3 次（调用方填 1 也覆盖）；模型 ALLOW 不能完成；历史 1 次记录不能当 3 次；
  批量内部对象非首项同样命中；普通对象仍 1 次；三次确认不覆盖用户禁止、不扩大子智能体、无第四次。

### §10 路径比较不得改变实际操作语义（已修）
- 根因：回收先把用户指定的链接 resolve 成目标再移动原文件，链接留在原处；realpath 被当作操作目标。
- 修改：`tools/fs_tools.py`/`security/deletion.py`/`tools/approved_operations.py`：
  `_is_link_entry`（符号链接/Windows junction）；链接操作目标为目录项本身，解析目标只用于身份与
  审计；目录链接不枚举目录、不读目标正文；审批恢复按 `payload.is_link_entry/requested_path`
  回收目录项本身。
- 验收：请求回收符号链接/目录链接/失效链接时只回收目录项、目标与内容保留；目标受限时链接仍可回收
  且 0 次正文读取；普通文件删除语义不变。

### F1-F6 子智能体权限（已修）
- 根因：READ 只比对象不比操作/撤销（F1/F2）；内部数据分支先于范围检查（F3）；复合 effect 用
  effect 级 operation 覆盖全部对象（F4/F5）；模型审查 ALLOW 只写台账、不形成子 scope（F6）。
- 修改（Lead 引擎）：`_scope_impacts`/`_grant_covers_impact`/`_scope_coverage` 逐动作逐对象核对
  （`limit_impacts` 权威；缺失按 effect kind 配对；EXECUTE 的 cwd 不参与授权）；`_scope_limited`
  对 READ 要求 read 操作且移到内部数据分支之前；`_grant_scope_id` 合并 agent_scope 已验证 grants；
  `child_scope` 追加当前要求下未撤销/未过期的 allow 记录形成的委派 grants（对象/操作/版本边界保留）。
  修改（E 分支）：`security/instructions.py`（Grant 边界字段/绑定 verified grant/describe_scope/
  scope_covers/ScopeRequestLedger 状态机）、`security/review.py`（记录→可委派事实/有效性）、
  `tools/subagent_tool.py`（子范围形成与结构化事实、request_agent_scope、撤销实时核对、
  结果 data 带 delegation/scope_requests）。
- 验收：write-only 不能读；撤销后读取立即失败；unrestricted 不能绕过内部数据子范围；双动作复合
  删除 ALLOW、execute-only 不授权 delete；verified 授权形成明确子集且不重复审批；撤销/版本变化不
  继承旧授权；请求→授予→重试→撤销状态机；read_only 升级 user；研究型子任务空范围不误拒。

### §12 模式变化按当前规则判断（已修）
- 根因：`security/recheck.py`、`security/operations.py` 的批量共同条件、`tools/approved_operations.py`
  把“模式字符串不同”直接当失效（含 unrestricted→restricted 整批 invalidated）。
- 修改：删除三处“仅字符串不同即失效”；改为按当前规则（要求/限制/对象/内容/委派范围/模式）
  重新核对：放宽且当前规则允许 → 继续（只执行一次）；收紧且当前规则不允许 → 停止；
  read_only 仍一律停止；不扩大用户禁止；批量只保留真正共同条件（期限/要求版本/确认基础），
  模式交给逐项。
- 验收：restricted 批准后切 unrestricted 继续且只执行一次；切 read_only 必须停止；
  unrestricted 执行中内容变化仍失效；批量单项失效不影响其他项。

## 3. 移除/合并的重复判断

- `security/recheck.py`：删除“审批/审查时模式 != 当前模式 → 失效”的字符串比较。
- `security/operations.py`：删除批量 `mode_at_approval != current_mode` 整批终止；只保留
  期限/要求 active+版本/确认基础等共同条件。
- `tools/approved_operations.py`：删除 `binding_mode == "unrestricted"` 且模式变化整批 invalidated；
  read_only 保留；其余按当前模式逐项重核对。
- `security/object_state.py`：删除/替换“入口名不同/当前解析到别处/限制路径身份不可读”作为 UNRELATED
  的旧分支（H12 已删除入口名分支，H13 将其余改为对象事实或 UNKNOWN）；B1 用注册 basis 表达范围规则。
- `tools/security_gate.py`/`tools/fs_tools.py`/`tools/cmd_tools.py`：读取授权顺序与影响核对统一到
  同一 assessment，不再各写一份。
- `security/engine.py`：内部确认次数不再由调用方决定（`required_confirmations` 唯一来源）；
  子范围不再用 effect 级 operations 拍平对象。

## 4. 误放行与过严各自的修复证据（摘要）

误放行（应拒绝却允许）：
- A1：amend 后 runner 限制转移到 PATH 新对象 B，A 实际执行成功 → 修复后 A 仍拒绝且 0 启动。
- A2：原路径替换后旧对象硬链接由拒绝变可执行 → 修复后恢复零写入，旧/新对象都拒绝。
- B3：同名入口解析到独立对象被误判 INVALID（过严）；同时“绑定路径替换”仍 INVALID（不过宽）。
- C1/C2：拒绝前已读取正文/摘要（C 分支 evidence：read 计数 0 才通过）。
- D1：同大小同 mtime 内容改写沿旧摘要执行新内容 → 修复后拒绝、0 启动。
- D2：第 18 个参数脚本变化仍执行 → 修复后拒绝且 0 marker。
- D3：递归删除目录仍删除被禁止子文件 → 修复后拒绝、目录保留。
- E1：检查旧 path、写入新 path（禁止目录硬链接）→ 修复后执行前拒绝、0 写。
- F1/F3/F5：write-only 读取、unrestricted 内部日志读取、execute-only 删除 → 修复后均 DENY。
- §8：附件写回内部日志一次确认即修改；模型 ALLOW 完成内部替换 → 修复后最低 3 次、模型 ALLOW 不完成。
- §10：回收链接时移动原文件 → 修复后只回收目录项。
- §12：批准后模式放宽仍整体失效（过严）→ 修复后按当前规则继续且只执行一次。

过严（应允许却拒绝）：
- B1：report.txt 三模式被拒 → 修复后按当前模式可读（相邻错误对象仍拒绝）。
- B2：A 删除后无关 B 被拒 → 修复后 B 可读，A 硬链接仍保护。
- B3：同名入口（独立对象）被拒 → 修复后按对象事实执行。
- F4/F6：已授予的复合删除被拒、模型审查授权不能形成子范围 → 修复后正常子任务可完成。

## 5. 正常场景仍可完成的证据

- 普通读取/写入/删除/回收在各模式按既有规则正常；未变化程序审批后真实启动（D1 控制组）。
- A1 修复后同一 amend 中新增的限制仍正常绑定；A3 精确解除一个对象后另一对象仍受限、无关对象正常。
- B1 普通 report.txt 三模式可读；B2 无关 B 可读；B3 独立程序绝对路径与同名入口可执行。
- C1 元数据阶段不拒绝的正常程序照常启动；C2 普通附件写回批准后正常完成。
- D2 18 个脚本控制组真实执行；D3 无禁止对象时递归删除正常。
- E 系列：普通写回/回收不再误判变化；批量全部有效时全部执行；独立有效项在单项失效时继续。
- §8 普通对象仍 1 次确认；三次确认完成后正常放行，无第四次。
- §12 模式放宽后已批准操作继续且只执行一次。

## 6. 真实验证与模拟的区别

- 真实：真实 AuthorizationEngine/SQLite 台账与迁移/RequirementLedger/ApprovalService 状态机/
  真实工具装配（FsRead/FsWrite/FsPatch/FsDelete/RunProgram/附件写回）/真实临时文件与目录/
  真实硬链接与 samefile 断言/真实 PATH 查找/真实程序启动与 marker/SQLite 审批 decide 与后台执行/
  真实 user 澄清路径。
- 模拟（均在用例 docstring 标注）：模型一律 fake/mock（无网络、无真实 API Key）；文件身份提供器
  边界注入（identity_of 对指定路径返回 None，用于复现“身份不可读”）；
  B2 保存结构在 object_state 消费测试中用构造保存事实/测试侧注入（A 合入后已有真实保存流程端到端）；
  §10 回收站替身（记录收到路径并移动目录项，非 OS 回收站复现）；审批 builder 的 fake reviewer。
- 本机限制：Windows 无 SeCreateSymbolicLinkPrivilege，文件符号链接分支在 POSIX CI 执行；
  目录 junction/失效 junction/普通文件/目标受限场景本机真实覆盖。

## 7. 测试命令、结果、失败与跳过原因

环境：`cd backend; .\.venv\Scripts\python.exe -m pytest <files> -q -p no:cacheprovider`
（`.venv` 为共享环境 junction，不改依赖；全程离线、模型 fake/mock；不跑全量 pytest/完整 CI）。

- 工作流定向（基线红→修复后绿）：
  - A：`tests/test_r9h13_a_requirements.py` 9 passed（基线同文件 A1/A2/A3/B2 红）。
  - B：`tests/test_r9h13_b_object_state.py` 10 passed（基线 5 red / 4 guard）。
  - C：`tests/test_r9h13_c_execution.py` 8 passed（基线同文件 8 failed）。
  - D：`tests/test_r9h13_d_approval.py` 22 passed（pristine e68fb9b7 上同文件 15 failed / 7 passed）。
  - E：`tests/test_r9h13_e_subagent.py` 13 passed（基线同文件 12 failed / 1 passed）。
  - Lead：`tests/test_r9h13_lead.py` 6 passed（基线 4 failed）。
- 最终受影响集合：48 个受测文件、728 项 collected，pytest exit 0（含 H11/H12 object_state/binding/
  cross/regressions、H2/H3/H4/H5-H10 邻近套件、审批 lifecycle/binding/restart/api、approved_operations、
  附件 service/api/identity/source_refs、fs_delete_recycle、authorization_engine、subagent_scope、
  r9g_acceptance_e2e 等）。
- 审批/附件邻近补充：`test_approval_*` + `test_approved_operations` + `test_attachments_*` +
  `test_fs_delete_recycle` + `test_r9g_acceptance_e2e` + `test_r9h4_fs_batch` 等 15 套件通过；
  附件邻近 19 套件 205 passed / 1 skipped（套件既有平台条件）。
- 已知基线既有失败（非本轮引入，基线源码上同样失败）：
  `tests/test_r9h_content_binding.py::test_script_content_change_prevents_reuse`
  （RunProgramTool 审查对象 script_sha256 为 None，Windows 共享 venv junction 环境导致；
  H11/H12 merge notes 记录同一失败）。
- docs 一致性：`python scripts/check_docs.py` 通过（97 个里程碑条目）、`--selftest` 通过。
- `agent.eval.run`：输出与 H12 基线（`_r9h12-eval-baseline.json`）逐字段完全一致。
- 本轮未跑全量 pytest、未跑完整 CI（历史完整 CI 额度 2/2 已用尽）；只跑受影响测试与必要邻近回归，
  失败后只重跑失败项与直接受影响项。

### 7.1 验收覆盖对照（提示词第十三条）

- new/amend/withdraw、完整要求链及来源：`test_r9h13_a_requirements.py`、`test_r9h2_chain_recovery.py`。
- 标题补充与限制继承：A1 用例（普通补充不重绑、新增限制仍绑定、继承事实原样）。
- 绑定缺失、候选替换和硬链接：B2/B3 用例、H11/H12 object_state/binding_recovery/cross_operation。
- 不同绑定的同名对象：A3 去重/精确解除用例。
- 正常读取与局部未知：B1 用例（report.txt 三模式可读 + 错误对象/执行仍拒绝）。
- 限制路径消失后的无关操作：B2 用例（B 可读、A 硬链接仍保护、历史数据失败关闭）。
- 绝对路径、名称入口及链接入口：B3/H12 cross-operation/对象判定用例。
- 普通和递归删除：D3 用例（`rd /s /q`、rm -rf、fs_delete 一致、不完整拒绝）、`test_fs_delete_recycle.py`。
- 常规内容变化及保持元数据的变化：D1 用例（同大小同 mtime 改写被拒）、E2 正常写回/回收控制组。
- 参数采集边界及非标准后缀脚本：D2 用例（python job.txt、18 脚本/200 候选边界）。
- 普通文件和附件的直接允许、模型允许、人工恢复：D 审批执行用例、`test_approved_operations.py`、
  `test_r9g_acceptance_e2e.py`。
- 内部单项、混合批量及三次确认：Lead §8 用例 + D 用例（历史 1 次、批量非首项、三次后正常执行）。
- 批量全部有效、单项失效、全部失效和共同条件失效：D E3 用例 + `test_r9h5_regressions2.py`。
- 模式放宽与收紧：D §12 用例 + `test_r9h4_engine_binding.py::test_recheck_mode_change_needs_confirmation`。
- 子权限动作、对象、撤销、继承和申请：`test_r9h13_e_subagent.py`（F1-F6、请求→授予→重试→撤销、
  read_only 升级 user）。
- 每条拒绝用例相邻正常放行用例、每条允许用例配内容/对象/越界反例：见各文件用例结构（基线红/控制组）。

## 8. 最终 SHA、CI 运行号、HEAD 差异

- 最终受 CI 验证的提交：`17b9775cd0c538dbadf9dc201380f01af5439e88`（r9h13/main；包含实现、旧期望修订、
  契约/报告/证据）。其后回填 CI 运行号的 docs/证据提交不改变任何代码。
- 三平台定向 CI（`targeted-verify.yml`，同一 SHA `17b9775c`，同一 pytest 筛选：
  H13 六个专用文件 + H11/H12/H4/H2/H3 受影响旧套件）：
  - Linux Python 3.11：run `40177769502` success；
  - Linux Python 3.12：run `40178148732` success（首次派发 `40177772125` 在队列层被并发组
    取消、未执行任何检查，同 SHA/同筛选重派）；
  - Windows：run `40177774774` success。
  - 三个运行都通过 `verify-sha`（checkout 后断言实际 HEAD == 请求 SHA）后才执行 backend；
    summary 打印 ran=1 failed=0 cancelled=0，SKIPPED 项不计为通过。
- 基线 HEAD 差异：`17b9775c` 相对 `e68fb9b7` 包含 H13 全部实现、旧期望修订、契约/根因/报告与
  `_r9h13-evidence/**`；生产代码改动集中在
  `backend/src/agent/security/{engine,object_state,requirement_limits,requirements,launch,recheck,
  operations,deletion,instructions,review}.py`、
  `backend/src/agent/tools/{cmd_tools,fs_tools,approved_operations,security_gate,approval,subagent_tool}.py`、
  `backend/src/agent/services/{app,attachments}.py`、`backend/src/agent/storage/approvals.py`、
  `docs/**`、`backend/tests/**`。
- 未合并 main、未发布安装包；未修改历史迁移（本轮无 schema 变化）。

## 9. 未完成事项与准确边界

1. **专用“模型语义恢复”未实现**：A2/B2 的历史数据缺少保存时 identity 时保持未解决；已实现并验收
   “用户澄清具体对象后按后端对象事实恢复”，但由模型自动理解“历史限制指的是哪个对象”的专用语义
   恢复未实现（当前失败关闭，不静默放行）。
2. **B1 残留边界**：未识别程序文件本体被按普通路径读取时无法保护（无对象事实可比），需澄清/绑定；
   已在 basis 条件、docstring 与报告中列明。
3. **D2 采集为有界完整**：128 候选/8MB 单文件/64MB 总量，超限显式不完整并拒绝按完整方案执行，
   未实现无上限流式采集。
4. **§10 文件符号链接**：本机无权限，POSIX CI 覆盖；Windows 目录 junction/失效 junction 已真实验证。
5. **执行前核对到系统调用仍有竞态**（未新增沙箱/文件代理，本轮不扩大承诺）。
6. **真实回收站、安装版、真实模型、完整 CI 矩阵未验证**：完整 CI 历史额度 2/2 已用尽，本轮按要求
   只做受影响测试与三平台定向 CI；未合并 main、未发布安装包。
7. **本轮跳过网络安全部分**；不新增沙箱/文件代理/全盘扫描/后台监听。

## 10. H12 交付记录更正

三个绿色定向运行（40122111129 / 40122291315 / 40122468968）实际对应提交
`245f76aac153bc621e86559173b0519c6562def1`（生产代码 `e4e478b4`），而非 `4594358f`。
产品实现提交 = `e4e478b4`；最终测试提交 = `245f76aa`；后续 `e68fb9b7` 仅 docs/status/证据。
