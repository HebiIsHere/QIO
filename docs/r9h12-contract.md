# R9-H12 共同契约：UNRELATED 证据标准与未绑定对象恢复依据（冻结）

> 基线 r9h11/main HEAD 503df11a（代码 d9d806b4）。
> 分支 r9h12/main；worktree qio-r9h12-main / -a / -b / -c / -d。
> 共享文件只有指定 owner 能改；engine.py 由 Lead 负责。

## 0. 环境与纪律

* 后端测试：cd <worktree>/backend; & ./.venv/Scripts/python.exe -m pytest tests/<file>.py -q -p no:cacheprovider
  （worktree 的 backend/.venv 是指向共享环境的 junction，勿改）
* 离线：不联网、不新增依赖、不跑全量 pytest/npm/CI；模型一律 fake/mock。
* TDD：先基线红（真实流程）→ 最小实现 → 绿；小步提交（中文 + 范围前缀）。
* 禁止：删除失败用例、放宽产品断言、skip/xfail 换绿、改原因文字换绿；禁止用自然语言关键词/reason 文本/
  名称相似/去后缀/版本号归并/名称词表判断权限；禁止名称字符串/basename 猜测对象相关性。
* 允许按正确契约修订旧期望（H11 的“名称不同=无关”“当前解析到别处=无关”），但必须保留原始输入与复现证据，
  并补相邻的“可靠无关仍继续”案例。

## 1. 五态与各自成立所需事实（保持五态）

| 状态 | 成立所需事实 |
| --- | --- |
| MATCH | 保存绑定 identity(+birth) 与候选一致；或路径文本/前缀/入口结构化完全匹配；或限制对象显式覆盖（空 objects、入口名相同 + 绑定对象相同） |
| UNRELATED | 只能来自 §2 白名单 basis，且附带该 basis 要求的事实；证据必须来自对象事实（保存身份、真实文件身份、路径关系、操作集合、对象类别），不能来自名称相似或当前查找环境单独推断 |
| UNBOUND | 明确禁止但保存时未形成绑定：候选与保存候选/当前澄清对象相关（同一路径、同一身份、或入口名相同），或名称候选存在但无法排除；需要专用恢复/澄清 |
| INVALID | 已保存绑定失效：绑定路径对象被替换/删除；或同一 identity 下生命周期不同；或同一来源名当前解析到别的对象/无法解析 |
| UNKNOWN | 事实不足：身份不可读、限制路径身份不可读、名称查找失败、保存无候选、当前解析到别处但无保存候选、候选存在但无法确认关系。失败关闭 |

绝对不得仅凭以下事实生成 UNRELATED（必须落到 UNBOUND/INVALID/UNKNOWN）：
1. 调用入口名称与来源名称不同；2. 原名称当前查找失败；3. 当前 PATH 将名称解析到另一个对象；
4. 限制路径的身份读取失败；5. 保存时没有形成完整绑定。

## 2. UNRELATED 白名单（唯一允许的 basis，A 实现为注册表常量并测试完整性）

| basis | 要求事实 | 说明 |
| --- | --- | --- |
| operation_out_of_scope | limit.operations 已知、本次 operation 已知且不在集合 | 操作范围先过滤（write-only 不阻止 execute） |
| object_kind_out_of_scope | 限制/候选对象类别不可能指向同一对象（service↔文件、record 等） | 类别不相交 |
| candidate_not_exists | 候选路径真实不存在（exists=False） | 不存在的对象不可能是被禁止对象 |
| path_prefix_outside | 候选解析路径不在 path_prefix/directory 内 | 路径型限制既定语义 |
| service_outside | service/url 作用域文本不同 | 服务限制语义 |
| different_object_identity | 保存绑定 identity 可读 + 候选 identity 可读 + 不相等 | 精确对象事实 |
| path_different_object | path 限制两侧身份都可读且不相等 | 精确对象事实 |
| path_different_lifecycle | path 限制同一 identity、两侧 birth 都可读且不同 | 不同生命周期对象 |
| entry_resolves_to_other_object | 已绑定 program_name：本次入口名≠限制名，且入口解析对象 identity 可读、与绑定 identity 不同 | 对象事实，不是名称差异 |

## 3. 必改分支（当前生产代码 → 目标）

### 3.1 P3 路径身份不可读（object_state._path_core）
- 候选路径不存在 → UNRELATED candidate_not_exists（保留，先判）。
- 候选==限制路径 → MATCH path_same（保留）。
- 限制路径身份不可读（target_observed is None）：
  * 若限制路径不存在 → UNKNOWN basis=path_limit_target_unreadable（删除/替换后旧对象可能仍在别名存在；不能仅凭路径文本不同证明无关）；
  * 若限制路径存在但身份读取失败 → UNKNOWN basis=path_limit_identity_unavailable（原 UNRELATED 改为 UNKNOWN）。
- 候选身份不可读 → UNKNOWN（保留）。
- 两侧可读且 identity/birth 比较 → MATCH / UNRELATED(path_different_object|path_different_lifecycle)。
- 显式 program 路径走同一 _path_core（共同分支）。

### 3.2 P1 未绑定 program_name 的入口/别名绕过（object_state._unbound_path_decision / _name_branch）
- 删除“入口名不同 → UNRELATED”的提前返回。
- 候选不存在 → UNRELATED candidate_not_exists（保留）。
- 保存候选（item[binding_candidates]，纯数据：path/source/state；可能含 identity 时用）参与判断：
  * 候选路径 == 某保存候选路径 → UNBOUND basis=binding_missing_saved_candidate（相关，需恢复；记录候选路径/来源 facts）；
  * 候选 identity 与某保存候选 identity 相同 → UNBOUND（basis=binding_missing_saved_candidate_identity）；
  * 有保存候选但都不匹配 → UNKNOWN basis=binding_missing_candidate_not_in_saved（不能排除硬链接/别名，失败关闭）。
- 无保存候选：
  * 当前澄清解析到同一 identity → UNBOUND（保留 binding_missing_same_object）；
  * 当前澄清解析到别的对象/生命周期不同 → UNKNOWN basis=binding_missing_resolved_elsewhere（原 UNRELATED 改 UNKNOWN）；
  * 当前澄清失败 → 入口名相同 → UNBOUND（保留）；入口名不同或缺失 → UNKNOWN basis=binding_missing_lookup_unresolved（原不同名 UNRELATED 改 UNKNOWN）；
  * 候选存在但身份不可读 → UNKNOWN（保留）。
- _name_branch（候选是裸入口名）：
  * 已绑定 + 入口名相同：身份一致 MATCH；不一致/不可解析 INVALID（保留）；
  * 已绑定 + 入口名不同：入口解析对象 identity 可读且≠绑定 identity → UNRELATED entry_resolves_to_other_object；解析失败/身份不可读 → UNKNOWN basis=entry_name_differs_unverifiable（原 UNRELATED 改 UNKNOWN）；
  * 未绑定 + 入口名相同 → UNBOUND（保留）；
  * 未绑定 + 入口名不同：有保存候选且入口解析对象与候选 identity 相同 → UNBOUND；否则 UNKNOWN basis=unbound_entry_name_differs（原 UNRELATED 改 UNKNOWN）。
- _merge_decisions 优先级必须保证 UNKNOWN 高于 UNRELATED（检查/修正 _STATE_PRIORITY）。

### 3.3 绑定状态不完整
- binding 结构不完整 → UNKNOWN（保留）。

## 4. 恢复契约（B：requirement_limits/requirements/app.py）

把“新要求保存时创建绑定”和“恢复历史未解决绑定”分成两个入口：
* 新保存：attach_program_bindings（保留现状：保存时查找 + 身份；失败记录 binding_state/binding_failure/binding_candidates；已有绑定不改写）。
* 恢复：recover_program_bindings(saved_limits, *, requirement_id, version, status, now_candidates)（新增）：
  * 只处理无绑定 program_name；已有有效绑定原样保留，不随 PATH 转移；
  * 采纳顺序：保存候选优先（candidate.path 现在存在且身份可读 → 采纳该路径/identity/birth，source=saved_candidate_verified）；
  * 保存候选存在但都不再可验证、或多个冲突 → 不写绑定（保持原 JSON 与候选，记录 notice/审计事实）；
  * 无保存候选 → 不得用当前 PATH 查找结果直接绑定；仅记录为“当前候选事实”（不落库），保持未解决；可由专用语义恢复（模型解释 + 后端核验）处理，本轮若未实现模型侧选择，则保持未解决并如实报告；
  * 采纳对象必须后端核验（路径存在 + 文件身份可读 + path/identity 一致），模型/调用方自报不能替代核验；
  * 写回前重新核验所采纳候选事实仍有效（path 未变、identity/birth 一致）；失败 → 不写；
  * 写回走 apply_binding_recovery → set_structured_limits 条件写入（id/version/status/chain_digest），source 记录真实原话 sha256 + 链 digest + 版本 + 候选来源 + 原 structured_limits_source；
  * 恢复只理解和绑定限制：不调用普通独立授权审查、不生成普通长期授权、不改变权限模式；
  * 失败/歧义：保留原限制、known limits、候选及来源，不清空为空限制；normalize/serialize/deserialize 保持纯数据。
* app _recover_structured_binding_limits 改为使用上述恢复入口；只有真正采纳并写回成功才把新绑定给本轮使用；否则本轮仍按 UNBOUND/UNKNOWN 失败关闭。
* 对象条目新增字段（binding_state/binding_failure/binding_candidates）必须在 normalize/serialize/deserialize/merge/remove 中完整保留（纯数据）。

## 5. 适配入口（C）

* 读取正文/摘要/补丁前置内容前消费 assessment；拒绝时正文/摘要读取次数 0、程序 0 次启动。
* read/write/delete/execute 与审批恢复消费同一结论；UNBOUND/INVALID/UNKNOWN 一律 DENY，普通批准/确认不能解除。
* 工具/审批恢复不自动触发恢复（恢复只在服务层轮次入口）；恢复失败后仍失败关闭。
* 恢复成功后（下一轮或同一轮重新取要求）重新核对绑定版本；不得把恢复结果当普通授权/授权记录。
* 不改变三种模式职责与既有规则（§7）。

## 6. 旧测试修订（A/D/Lead）

必须修订的错误规则断言（保留原输入与复现步骤，只把期望改成正确契约）：
1. H11 object_state 单测：test_path_limit_identity_unreadable_is_unrelated → UNKNOWN；
   test_unbound_resolved_elsewhere_is_unrelated → UNKNOWN；
   test_unbound_entry_name_different_is_unrelated → UNKNOWN；
   test_binding_entry_name_different_unresolvable_is_unrelated → UNKNOWN。
2. H11 独立验收/跨操作里的“不同入口名放行/当前解析到别处放行”守卫 → 改为 UNKNOWN/DENY，
   另补“可靠无关仍继续”（操作范围外、候选不存在、已绑定不同对象、前缀外）。
3. H5/H7/H8 引擎级旧期望（Lead 负责）：unbound + 不同入口名 → 由 ALLOW 改 DENY/UNKNOWN，
   并为每个旧用例补/改用可靠无关证据（如候选不存在、操作范围外）。
不得删除用例；每次修订在报告里列出“旧期望→新期望 + 依据”。

## 7. 必须保留的既有规则

三模式职责（read_only/restricted/unrestricted）；已知禁止优先于模式/固定查询/历史授权/模型允许/人工批准/
内部三次确认；操作范围过滤；空 operations/objects 含义；保存身份+生命周期核对；旧硬链接保护；
目录完整性独立检查；读取权限先于内容读取；内部维护确认前不读正文；等待期间变化失效；批量逐项；
审批期限与单次执行；未变化脚本批准后正常执行。

## 8. 文件所有权

* Lead：engine.py、effects.py（如需）、docs/*、旧跨套件测试期望修订、最终集成/CI。
* A：security/object_state.py、tests/test_r9h11_object_state.py（修订）、tests/test_r9h12_object_state.py（新增）。
* B：security/requirement_limits.py、security/requirements.py、services/app.py、tests/test_r9h12_binding_recovery.py。
* C：tools/security_gate.py、tools/fs_tools.py、tools/cmd_tools.py、security/operations.py、security/recheck.py、
  tools/approved_operations.py、tests/test_r9h12_cross_operation.py。
* D：tests/test_r9h12_regressions.py（基线红→绿）、tests/test_r9h11_regressions.py（修订错误守卫，保留原件）、
  _r9h12-evidence/。

## 9. 交付报告（给 Lead）

分支 + commit SHA 列表；文件清单；定向命令与通过数；旧期望修订清单（旧→新依据）；未完成/已知限制；
契约冲突；真实/模拟分界。