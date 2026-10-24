# R9-H12 审批与授权补修：UNRELATED 证据标准与未绑定对象恢复依据（根因）

> 基线 r9h11/main 503df11a（代码 d9d806b4）。修复分支 r9h12/main。
> 冻结契约 docs/r9h12-contract.md。

## 1. 共同根因：UNRELATED 被当成推断结果，而不是对象事实的结论

H11 已把五项对象状态做成结构化结果，但 UNRELATED 的证据标准没有冻结，生产代码把下面这些“**没有证明关系**”的
事实当成了“**证明无关**”：

| 生产分支（H11 基线） | 被当作无关的事实 | 实际只说明 |
| --- | --- | --- |
| object_state._unbound_path_decision 提前返回 different_entry_name | 本次入口名 ≠ 限制名称 | 名称不同，但对象可能是同一真实文件（硬链接/别名） |
| object_state._unbound_path_decision binding_missing_resolved_elsewhere | 当前 PATH 把名称解析到另一个对象 | 当前环境事实；不能证明保存时用户限制指向该对象，也不能排除本次对象 |
| object_state._name_branch different_entry_name（未绑定） | 入口名不同 | 同上 |
| object_state._name_branch different_entry_name（已绑定 + 解析失败） | 名称查找失败 | 查找失败不是对象事实 |
| object_state._path_core path_limit_identity_unavailable | 限制路径身份读取失败 | 身份不可读 ≠ 对象不同；硬链接仍可能是同一对象 |
| requirement_limits.attach_program_bindings（恢复路径复用） | 当前 PATH 查找结果 | 当前查找是候选事实，不是历史用户限制的对象证据 |
| app._recover_structured_binding_limits（直接调用 attach） | 恢复时重新 which | 会静默排除保存候选并把禁止转移到当前 PATH 对象 |

这些分支共同构成：**“没有足够依据”被默认成“无关”，随后被模式规则放行**。

## 2. 错误契约如何进入测试（为什么旧测试保住了错误规则）

* H11 单测把实现细节固化成契约：`test_path_limit_identity_unreadable_is_unrelated`、
  `test_unbound_resolved_elsewhere_is_unrelated`、`test_unbound_entry_name_different_is_unrelated`、
  `test_binding_entry_name_different_unresolvable_is_unrelated` 直接断言 UNRELATED——它们测的是
  “当前实现怎么返回”，不是“什么事实足以证明无关”。
* H11 独立验收把“不同入口名放行/当前解析到别处放行”当作正确行为写成 guard；这些 guard 在基线红→绿过程中
  被用来证明“无关对象不被阻塞”，却把“未证明关系”一起放行了。
* H7/H8 的旧引擎期望（不同版本入口 ALLOW）被继承；它们依赖“名称不同即不同来源”，没有对象事实。
* 验收通常只断言最终 verdict/错误文字，没有断言“拒绝发生在读取/启动之前”和“恢复前后对象没有转移”，
  所以裸入口名/唯一路径的绕过没有被发现。

本轮处理：保留原始输入与复现步骤，把期望改为正确契约（UNKNOWN/UNBOUND/INVALID），并补
“可靠无关仍继续”的相邻案例；不删除用例、不跳过、不改原因文字换绿。

## 3. UNRELATED 分支逐一审查（证据条件 → 消费者）

（由 Lead 在集成后按 UNRELATED_BASES 注册表复核；每个 basis 要求的事实见契约 §2，消费者为
engine.assess_limit_conflict → authorize/recheck_approved 与 fs/cmd/approval 适配层。）

| basis | 证据条件 | 消费者/调用链 | 结论 |
| --- | --- | --- | --- |
| operation_out_of_scope | limit.operations 已知且不含本次 operation | 引擎聚合、工具适配 | 保留（可靠） |
| object_kind_out_of_scope | 限制/候选对象类别不相交（service↔文件等） | 引擎聚合 | 保留（可靠） |
| candidate_not_exists | 候选路径真实不存在 | 引擎聚合 | 保留（可靠） |
| path_prefix_outside | 候选不在 path_prefix/directory 内 | 引擎聚合 | 保留（可靠，路径型限制语义） |
| service_outside | service/url 作用域文本不同 | 引擎聚合 | 保留（可靠） |
| different_object_identity | 保存绑定 identity 可读 + 候选 identity 可读 + 不同 | 引擎聚合 | 保留（对象事实） |
| path_different_object | path 限制两侧身份可读且不同 | 引擎聚合 | 保留（对象事实） |
| path_different_lifecycle | path 限制同 identity、两侧 birth 可读且不同 | 引擎聚合 | 保留（对象事实） |
| entry_resolves_to_other_object | 已绑定 program_name + 入口解析对象 identity 可读且≠绑定 | 引擎聚合 | 保留（对象事实） |
| ~~different_entry_name~~（未绑定/解析失败） | 仅名称差异/查找失败 | 引擎聚合 | **改为 UNKNOWN** |
| ~~binding_missing_resolved_elsewhere~~ | 仅当前解析到别处 | 引擎聚合 | **改为 UNKNOWN** |
| ~~path_limit_identity_unavailable~~ | 限制路径身份不可读 | 引擎聚合 | **改为 UNKNOWN** |
| （新增）binding_missing_saved_candidate | 候选路径/身份命中保存候选 | 引擎聚合 | UNBOUND（相关，需恢复） |
| （新增）binding_missing_candidate_not_in_saved | 有保存候选但都不匹配 | 引擎聚合 | UNKNOWN（别名无法排除） |

## 4. 三项原始复现（修复前 → 修复后）

1. **未绑定限制经硬链接 alternate 绕过**：保存 program_name=runner 执行禁止（保存时未形成绑定）→
   原名称查找失败时，绝对路径被拒但 `program='alternate'`（真实硬链接入口）实际启动。
   修复后：名称差异不再证明无关；无保存候选/候选不可确认 → UNKNOWN；有保存候选 → UNBOUND；
   两条入口都不启动（marker 不存在、子进程 0 次），并进入专用恢复/澄清。
2. **恢复把历史限制转移到当前 PATH 对象**：保存候选 A（身份读取失败）→ PATH 改为 B →
   未恢复时读 A 被允许（当前解析到 B 被当无关）；专用恢复把绑定写成 B；A 之后可执行。
   修复后：当前解析只作候选事实，不作为排除依据；有保存候选时按保存候选核验采纳（A 被保护）；
   无保存候选/候选不可验证 → 保持未解决零写入；恢复前后对象不转移。
3. **显式路径身份不可读时误放行**：对明确程序路径保存 read 禁止 + 真实硬链接 → 限制路径身份读取失败时
   读取硬链接返回正文。修复后：身份不可读 → UNKNOWN（不按路径文本不同判无关），拒绝发生在正文/摘要/
   补丁读取之前（0 次读取）；正常不同对象/同内容复制件仍按对象事实处理。

## 5. 恢复如何保留历史候选并处理歧义

* 新保存与历史恢复分成两个入口：新保存用当前查找创建绑定并记录候选/失败原因；恢复只承认**保存候选**
  （路径现在存在且身份可读 → 采纳并记录 source=saved_candidate_verified）。
* 无保存候选、候选不可验证、多个候选冲突：不写绑定、不改 JSON，保持未解决（限制/known/候选/来源保留）；
  可由专用语义恢复（模型解释用户限制 + 后端核验候选事实）处理，普通授权审查/批准不能替代。
* 写回条件：id/version/status/chain_digest + 采纳候选事实仍然有效；不生成普通长期授权、不改权限模式。

## 6. 既有规则保留

三模式职责；已知禁止优先于模式/固定查询/历史授权/模型允许/人工批准/内部三次确认；操作范围过滤；
空 operations/objects 含义；保存身份+生命周期核对；旧对象硬链接保护；目录完整性独立检查；
读取权限先于内容读取；内部维护确认前不读正文；等待期间变化使旧方案失效；批量逐项；审批期限与单次执行；
未变化脚本批准后正常执行。