# R9-H13 审批与授权统一补修：共同根因与修改地图（草稿，随实现更新）

> 基线 `r9h12/main` e68fb9b7（生产代码 e4e478b4）；修复分支 `r9h13/main` + a/b/c/d/e。
> 共同契约见 `docs/r9h13-contract.md`。本文件只记录根因与修改位置，不替代逐项验收证据。

## 0. 共同根因（跨问题）

1. **同一规则按入口各写一遍**：读取授权顺序、限制对象判定、确认次数、模式核对分散在
   engine / security_gate / fs_tools / cmd_tools / approved_operations / attachments 里；
   一个入口修好，另一个入口仍走旧逻辑（C1/C2、E2、§8、§12 都在此列）。
2. **动作与对象被拆成两个集合**：复合 effect 用 effect 级 operations 覆盖全部 path，
   丢失“哪个动作对应哪个对象”；子智能体范围（F4/F5）与命令删除影响（D3）都因此判错。
3. **文件身份、读取前内容、拟写入内容混用**：`content_sha256` 一处承载三种事实；
   写回/回收把新内容摘要与磁盘旧内容比较，把 identity 当内容摘要（E2）；摘要缓存只用
   path/size/mtime（D1）。
4. **调用入口、实际执行路径与身份比较路径混用**：审批绑定旧 path、执行重新 resolve 新 path
   （E1）；回收把链接解析成目标后移动原文件（§10）；用于比较的 realpath 改写了操作目标。
5. **固定确认要求依赖调用方填写**：`confirmations_required` 由工具/调用方传入，存储/执行只读它；
   内部对象换入口（附件写回、批量非首项）就不触发三次确认（§8）。
6. **模型授权记录与子智能体 grant 未衔接**：模型审查 ALLOW 只写 AuthorizationLedger，
   child_scope 只看指令 grants → 正常子任务无范围（F6）；反过来 scope 检查又漏 operations
   /revoked/内部数据（F1/F2/F3）。
7. **恢复/继承把“当前环境事实”当成“历史对象事实”**：amend 继承限制后重新探测 PATH
   （A1）；恢复只用“路径存在+身份可读”宣称候选已验证（A2）；去重键不含绑定对象（A3）。
8. **测试验收口径局部化**：只断言返回 verdict/错误文字，未断言“拒绝前是否读取/启动、
   最终改了哪个路径、确认了几次、批量独立项是否继续”，因此这些回归长期没有被发现。

## 1. 逐项根因与修改位置

### A1 普通补充导致旧禁止重新绑定

* 根因：`requirements.add_correction` / `set_structured_limits` / `record_user_message`
  对**整个继承集合**再次调用 `attach_program_bindings`；继承条目的
  `binding_state/binding_failure/binding_candidates/source` 没有作为“禁止再探测”的事实。
  `apply_resolution` 的 amend 走 add_correction，于是未解析的 runner 限制在 PATH 指向 B
  时被重新绑定到 B。
* 修改：A 分支 `requirement_limits.py` / `requirements.py`：新保存只对本次新增对象建绑定；
  继承条目原样保留（含未解决事实/候选/来源）；区分新增/修改对象/原样继承。
* 修改位置：`security/requirement_limits.py`（attach 入口与条目事实）、
  `security/requirements.py`（apply_resolution/add_correction/set_structured_limits 的
  增量传递）、`services/app.py`（amend 流程不整集重探测）。

### A2 缺失历史身份却自动宣称候选已验证

* 根因：`requirement_limits._verify_saved_binding_candidate` 只验证“候选路径现在存在且
  身份可读”，没有要求候选在保存时记录过 identity；于是路径 A 被替换后把 A 的新对象
  采纳为绑定，旧对象的硬链接从 UNKNOWN/拒绝变成 UNRELATED。
* 修改：恢复只采纳“保存时记录了 identity/birth_key 且当前仍在同一路径、同一身份/生命周期”
  的候选；缺历史身份 → 保持 unresolved（结构化失败原因），保留 known limits/候选/来源，
  等专用语义恢复或用户澄清；写回仍核 id/version/status/chain_digest 与采纳事实。

### A3 不同对象的同名限制被去重丢失

* 根因：`normalize_objects`/`merge_limits` 去重键只有 `(kind,value,operations)`，
  忽略 `program_binding`（path/identity/birth_key）与来源含义；两条同名同动作、绑定不同
  程序的限制保存后合并成一条，第二条对象失去限制。
* 修改：去重键包含绑定对象/身份/生命周期/来源；精确解除单个对象保留其他；恢复/继承/序列化
  不静默丢失不同对象；正常化/序列化保持纯数据。

### B1 未绑定的局部程序读取限制拒绝普通文件读取

* 根因：`object_state._unbound_path_decision` 在“无保存候选 + 限制名称当前无法解析”时对
  任意 path 候选返回 UNKNOWN（`binding_missing_lookup_unresolved`）；引擎把 UNKNOWN 当
  冲突 → 三种模式都拒绝 report.txt。程序概念限制没有形成任何保存对象，却被扩大到全部文件读取。
* 修改（B 分支）：限定为软件概念限制的范围规则——仅 program_name 限制、无绑定且无候选、
  本次 read、候选是普通文件路径，且当前解析不指向该候选时，判 UNRELATED（新增注册 basis，
  带对象事实）；execute/写/删除/入口名候选仍失败关闭，显式路径限制仍走 `_path_core`。
  未识别程序文件本体被路径读取时无法保护，作为残留边界在报告中列明，需澄清绑定解决。

### B2 限制对象删除后无关对象持续被拒

* 根因：路径型限制的 `_path_core` 在限制路径消失/身份不可读时一律 UNKNOWN；限制条目
  没有保存时的对象身份事实可比对，无法证明 B 与旧 A 不同。
* 修改：A 分支在要求保存时给精确对象条目记录 `identity/birth_key/exists/observed_at/source`
  （首次形成条目时采集一次，不重新查找）；B 分支 object_state 用保存身份比较：
  身份不同→UNRELATED，同 identity 不同 birth→UNRELATED，缺字段→UNKNOWN/需澄清；
  保存时 exists=false 的事实可用于判定“当时没有可保护对象”。

### B3 同名入口解析到独立对象被误判 INVALID

* 根因：`object_state._name_branch` 在“已绑定 + 入口名相同 + 当前解析身份不同”时直接
  INVALID；没有区分“同名入口解析到另一个独立对象”与“绑定路径上的对象被替换”。
* 修改：比较入口解析路径与绑定路径：路径不同且两侧身份可读且不同 → UNRELATED
  `entry_resolves_to_other_object`（条件按 H13 扩展）；解析路径==绑定路径而身份/生命周期
  变化 → INVALID；解析失败/身份不可读 → UNKNOWN。

### C1 RunProgramTool 拒绝前读取被禁止文件

* 根因：`RunProgramTool.run` 先 `_execution_facts`（launch.build_spec_facts / 参数脚本 /
  shebang 探测）读取程序与脚本正文，再调用 `_authorize_execution`；拒绝时内容读取已经发生。
* 修改（C 分支）：先做元数据发现与读取权限 assessment（正文/摘要/shebang/补丁之前），
  拒绝时内容读取 0 次；覆盖直接允许、模型等待、人工审批恢复、重启恢复。

### C2 附件写回在读取禁止下仍读原文件

* 根因：`services/attachments.write_back_source/resolve_source(for_write=True)` 在授权前
  读源文件；执行器读旧摘要前无读取权限核对；write effect 未携带必要读取影响。
* 修改（D 分支）：写回操作事实包含 read+write 影响；读取权限先于任何正文/摘要读取；
  写批准不解除读取禁止；拒绝时读取 0 次。

### D1 摘要缓存掩盖内容变化

* 根因：`launch._PROGRAM_HASH_CACHE` 以 `realpath|size|mtime_ns` 为键，同大小同 mtime 的
  内容替换复用旧摘要；工具/审查/执行/审批恢复都走它。
* 修改（C 分支）：每次对程序内容做真实读取计算摘要（或身份+内容快照的可靠变化依据）；
  审查、直接执行、审批恢复、执行前核对同一语义；未变化程序正常执行。

### D2 参数脚本静默遗漏

* 根因：`launch._argument_file_facts` 只按后缀名单/shebang/“像路径”识别脚本；
  `_MAX_ARGUMENT_FACTS` 达到上限静默 break，不声明“不完整”；`python xxx.txt` 不在绑定。
* 修改（C 分支）：已知解释器按真实启动语义识别入口脚本（解释器 argv 位置）；
  `spec_digest`/`argument_files` 记录覆盖完整性；达到上限明确标记不完整（影响不完整→
  不得当完整执行/审批），并提供有界/流式/分级采集方案。

### D3 递归删除命令遗漏子对象

* 根因：`cmd_tools._execute_effect` 只用 `deletion.detect_delete_intent` 解析出的显式对象，
  递归删除目录时没有展开真实子对象影响；普通文件工具与命令入口对同一删除影响得出相反结论。
* 修改（C 分支）：递归删除的 delete intent 携带真实目录子对象影响（有界枚举+完整性标记），
  复用目录完整性/对象身份/用户限制/内部对象检查；不完整时不得按完整方案执行；
  不通过永久删除绕过回收/确认政策。

### E1 检查旧路径、实际写入新路径

* 根因：执行器按审批 payload 的旧 path/源引用核对，`_write_back_source_now` 重新 resolve
  当前引用后写新 path；最终真实目标没有参与最新限制/路径范围/审批绑定核对。
* 修改（D 分支）：最终真实目标（路径、用途、范围）逐项参与最新限制、路径范围与审批绑定；
  身份一致不代表路径/用途/范围一致；不得检查一个路径执行另一个路径。

### E2 正常写回/回收被误判内容变化

* 根因：字段语义混用：`content_sha256` 同时承载“读取前内容”“拟写入内容”“身份”；
  执行器拿新内容摘要与磁盘旧内容比较；回收流程把 identity 当 content_sha256。
* 修改（D/C 分支）：分别表达并核对 identity、读取前内容、拟写入内容；直接允许与审批恢复
  同一规则；未变化正常完成，真实变化仍使旧方案失效。

### E3 一个子项失效导致全部失效

* 根因：批量执行器把共同条件与逐项条件混在一起，共同条件之外还整批 return failure；
  逐项 effect 没有传入逐项 recheck，某一项限制变化使整批失败。
* 修改（D 分支）：整批只核对共同条件（要求 active/版本、期限、模式、确认基础）；
  每项使用自己的真实效果/对象/内容/限制/确认要求；共同条件撤回/过期停止整批；
  单项失效只影响该项，保留独立有效项的执行与真实结果。

### §8 内部维护三次确认未统一强制

* 根因：确认次数来自调用方 `confirmations_required`（附件写回/批量子项可写 1）；
  restricted 下模型审查 ALLOW 在内部数据分支之后仍可完成内部维护；存储/决定/执行前
  只读存储值，历史错误 1 次记录被当完成；批量/非首项未按真实对象重算。
* 修改（Lead 引擎 + D 审批管线）：`engine.required_confirmations(effect)` 按真实影响对象
  给出最低次数（提交时覆盖调用方值，重核对取 max）；内部维护不进入模型审查；提交/批准/
  恢复/最终执行按真实对象重算阶段；历史 1 次记录按不完整处理；批量逐项取最大；
  三次确认不覆盖用户禁止、不扩大子智能体；不增加第四次。

### §10 路径比较改变实际操作语义

* 根因：回收流程把用户指定的链接 resolve 成目标后移动原文件，链接留在原处；
  用于身份比较的 realpath 被当成操作目标。
* 修改（C 分支）：删除链接时删除目录项本身；比较路径与操作目标分离；读取/写入/删除
  分别确定链接与目标的真实影响；覆盖普通文件/符号链接/目录链接/失效链接/目标受限场景。

### §11 子智能体权限

* 根因：`_scope_limited` READ 分支只查 path 匹配不查 operations/revoked（F1/F2），
  且内部数据分支在范围检查之前（F3）；复合 effect 用 effect 级 operation 覆盖全部 path
  （F4/F5）；`_grant_scope_id` 只看指令 grants、`child_scope` 只看父 grants（F6）。
* 修改（Lead 引擎 + E 分支）：逐 impact（operation, object）核对；scope 检查先于内部数据；
  READ 要求 read 操作；`_grant_scope_id` 合并 agent_scope 已验证 grants；
  `child_scope` 追加当前要求下未撤销/未过期的模型审查 allow 记录形成的委派 grants
  （对象/操作/条件/版本边界保留；撤销/过期/版本不符不追加）。

### §12 模式变化按当前规则判断

* 根因：三处把“模式字符串不同”直接当失效：`security/recheck.py` 的模式比较、
  `security/operations.py` 批量共同条件、`tools/approved_operations.py`
  unrestricted→restricted 整批 invalidated；没有按当前模式重新评估。
* 修改（D 分支 + Lead 引擎语义）：删除“仅字符串不同即失效”；按当前规则（要求/限制/
  对象/内容/委派范围/模式）重新核对：放宽且当前规则允许→继续且只执行一次；
  收紧且当前规则不允许→停止；read_only 仍一律停止；不扩大用户禁止。
