# R9-H11 程序对象状态判定与跨操作一致性根因

基线 r9h10/main 80b0630874b6749df5ddf90bafc12e2d31f5eced（代码 0a0df3a1f84eec1f0182885efa2af563382a07f4）。

## 1. 四种对象状态此前为何仍退化为布尔不匹配

R9-H10 已经把 program_name 的绑定放进 structured limits JSON，也补了“替换失效”判断；
但生产代码仍用**布尔返回值**衔接对象判定，状态只在局部 helper 的文档/原因里出现：

| 旧入口 | 旧返回 | 退化后的后果 |
| --- | --- | --- |
| `_limit_program_matches` | True/False | 没有 program_binding → False（不是“未绑定”，是“不匹配”）；identity 一致但 birth 不同 → False（不是“另一生命周期对象”）。 |
| `_program_binding_path_replaced` | True/False | 只比较 identity 编号；编号相同、生命周期不同 → False（=未替换），新对象被放行（问题三）。 |
| `_program_name_unresolved` | str/None | 只处理 EXECUTE；name 入口不匹配、又没有 binding 时返回 None（=无此问题），绝对路径/文件读取绕过（问题一）。 |
| `_limit_path_matches` | True/False | 文件操作把 binding.path 当普通路径比较，重新取该路径**当前**对象身份；原路径替换后旧硬链接身份 != 当前对象 → False（问题二）。 |
| `recheck_approved` / 工具读取边界 | 复用上述 bool / reason | 同一条限制在不同入口得到不同结论：执行拒绝、读取放行；批准恢复只看 reason 字符串。 |

共同根因不是缺状态说明，而是**状态的消费者是布尔判断**：UNBOUND/INVALID/UNKNOWN 都被折叠成
False（“无关”），随后被 unrestricted/restricted/固定查询/审批复用等后续规则放行。

## 2. 本轮共同结果如何区分无关、未绑定、失效和无法确认

新增 `agent/security/object_state.py`（A，冻结契约 §2）：

* **ObjectState**：MATCH / UNRELATED / UNBOUND / INVALID / UNKNOWN；
* **ObjectDecision**：state + 稳定 basis 码 + operation + 限制对象 + 结构化 facts + 说明文字
  （文字只用于展示，判定只看 state/basis/facts）；
* **IdentityComparison**：`compare_bound_identity` 是唯一身份+生命周期比较入口：
  * identity 不可读 → identity_unavailable；
  * identity 不同 → different_object；
  * identity 相同且两侧 birth 都在且不同 → different_lifecycle（编号复用=另一对象）；
  * identity 相同、保存有 birth 但当前取不到 → lifecycle_unverifiable（不能默认一致）；
  * 保存侧没有 birth → 弱依据 same，并在 fact 标 lifecycle_basis=binding_without_birth。
* **resolve_program_name**：只读澄清（当前 PATH 查找），不写库、不补建绑定、不改历史 JSON。

引擎 `assess_limit_conflict`（Lead）把“限制条目 × 实际影响对象”逐项交给 `decide_object`，
聚合优先级 **MATCH > INVALID/UNBOUND > UNKNOWN > UNRELATED**：
有效匹配/绑定失效/绑定缺失/事实不足都 DENY（普通批准不能解除），只有**可靠无关**才继续按模式判定。
拒绝携带 decision_code（program_name_requires_object_binding / program_binding_invalidated /
object_state_unconfirmed）与 `AuthDecision.object_decisions` 结构化依据。

## 3. 文件操作为何此前没有真正使用保存身份，本轮如何统一

问题二的根因在 `_limit_matches_impact` 的程序名称+路径分支：

```
reference_path = binding["path"]
_limit_path_matches(candidate, {"kind": "path", "value": reference_path})
```

它把“保存时固化的对象身份”换成了“binding.path 这个字符串”，再由路径匹配重新 stat 该路径
**当前**指向的对象。原路径被替换后：

* 旧硬链接仍指向原对象，身份 == 保存身份，但“路径比较”取到的是替换后的新对象身份 → 不匹配 → 放行；
* 新对象在原路径上 → 路径文本相等 → 被当成“同一对象”直接命中或按旧规则失效，语义混淆。

本轮所有文件读取/摘要/修改/删除/执行/审批恢复都走同一 `decide_object`：
候选对象（真实工具给出的路径）用 `compare_bound_identity(binding.identity, binding.birth_key)`
与**保存身份**直接核对，不再重新取 binding.path 的当前身份：

* 旧对象别名（硬链接/符号链接目标）身份一致 → MATCH，继续受限制；
* 原路径新对象 identity 不同且路径 == binding.path → INVALID（绑定失效，拒绝/恢复，不自动继承或清除禁止）；
* 不同对象其它路径 → UNRELATED（可靠无关，继续按模式）；
* 身份不可读 → UNKNOWN（失败关闭）；候选不存在且不是绑定路径 → UNRELATED（不存在不可能是已存在绑定对象）。

路径型限制（没有保存身份，只有路径）保留既定语义：精确路径/前缀/目录按路径关系判断，
硬链接按两侧当前身份核对；不做全盘别名扫描，不把同内容复制件合并。

## 4. 生命周期差异如何同时影响匹配与失效处理

旧实现只在 `_limit_program_matches` 里比较 birth，而且比较失败返回 False（=无关）；
`_program_binding_path_replaced` 只比 identity 编号，漏掉生命周期。于是编号复用（同 identity、
不同 birth）时：匹配侧说“不匹配”（放行），失效侧说“没替换”（也不拦）。

本轮 `compare_bound_identity` 是**同一**比较结果，匹配与失效都消费它：

* 保存 identity/birth 与候选 identity/birth 一致 → MATCH；
* identity 相同、birth 都已知且不同 → different_lifecycle → INVALID（另一生命周期对象，
  原路径上发生时进入绑定失效/恢复流程，不当作无关，也不自动绑定新对象）；
* 保存有 birth、当前取不到 → lifecycle_unverifiable → UNKNOWN（不能默认一致）；
* 生命周期依据只来自文件身份（Windows 创建时间 / POSIX birthtime/ctime+来源标注），
  禁止用内容摘要/mtime/名称相似/调用方自报代替。

## 5. 保留的既有规则（防局部修复）

整体限制空对象/空操作含义；三模式已知限制优先于固定查询/历史授权/模型允许/人工批准；
程序禁止不经普通审批解除；目录完整性独立执行前提；批量逐项；内部维护确认前读取边界；
补丁同源快照与读取顺序；未变化脚本批准后成功执行、变化脚本不执行；保存/读取/继承/部分解除/
恢复/执行完整保留绑定字段；历史原始 JSON 在未恢复时字节不变、不隐式补建绑定。
