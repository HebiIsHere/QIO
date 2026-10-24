# R9-H10 程序限制绑定生命周期根因

基线 r9h9/main fec13810（代码 1f460c67）。

## 1. 绑定创建为何进入普通读取流程

H9 把 program_name 的 program_binding 创建放在 requirement_limits 的对象规范化函数中；normalize_limits 同时被 serialize_limits/deserialize_limits 调用，因此一次普通读取/序列化就会按当前 PATH 调用 shutil.which 和文件身份采集，并补建绑定。数据库 JSON 未变化，但内存对象每次读取得到不同绑定。

本轮分离职责：

| 层 | 职责 |
| --- | --- |
| normalize/serialize/deserialize | 只校验、去重、保留已有字段；不查找、不采集、不补建 |
| 可信绑定创建 | RequirementLedger.set_structured_limits / record_user_message / add_correction 显式调用 attach_program_bindings，写入 requirement id/version/status/path/identity/birth/source |
| 持久化读取 | 只恢复 JSON；已保存绑定原样保留；历史无绑定不自动补建 |
| 执行核对 | 区分无关、未绑定、有效匹配、绑定失效；不命中不等于可放行 |

## 2. 执行与文件操作共用对象匹配

- 程序路径、program_name binding、普通路径限制都调用同一路径/身份/生命周期核对。
- program_name 在文件操作中使用 binding.path 作为对象路径，不再把名称当 cwd 相对路径。
- 同内容复制件身份不同不合并；硬链接/符号链接目标按对象身份处理。
- 操作集合先在共同层过滤：write-only 不阻止 execute，但仍阻止对该程序文件的 write。

## 3. 绑定失效状态

- 无关对象：不在绑定路径/身份/入口范围，允许按模式判断。
- 未绑定（历史无 binding）：不按名称直接放行；相关操作进入恢复/拒绝。
- 有效匹配：对象身份/生命周期一致，限制命中。
- 绑定失效：绑定路径上的当前对象身份/生命周期不同，或原路径替换；明确拒绝/恢复，不能当作无关程序，也不能自动绑定新对象。

## 4. 组合测试防兜底

每项限制先单独保存（仅 name+write、仅 name+read、仅 name+delete、仅 name+execute），验证独立效果；再验证组合。文件写测试不额外保存路径限制来兜底；替换测试检查错误原因是绑定失效而不是程序退出失败。

## 5. 保留规则

整体限制空对象、目录完整性、批量逐项、内部维护确认前读取边界、补丁同源快照、等待变化、未变化脚本批准执行、三模式限制优先级均保持。历史无绑定记录不清空其他可靠限制；精确解除保留剩余绑定。
