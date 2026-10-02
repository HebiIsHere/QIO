# 后端进程生命周期：onefile 进程模型与 ownership 验证

这份文档记录 **PyInstaller onefile backend 的真实进程模型**、退出行为，以及「壳退出后
不留孤儿」这条保证是靠什么成立的。结论全部来自真实冻结产物上的实验，脚本可重跑：

    python scripts/verify_backend_process_model.py --exe <qio-backend.exe>
    python scripts/verify_backend_process_model.py --exe <exe> --case 2      # 单个 case
    python scripts/verify_backend_process_model.py --exe <exe> --case job    # job 语义四个实验

## 1. 进程模型（实测）

onefile 产物是**两个进程、同一个可执行文件**：

| 角色 | 说明 |
| --- | --- |
| launcher | 壳 `sidecar().spawn()` 拿到的那个 pid；负责把压缩包解到临时目录 |
| child | launcher 之后创建（实测晚约 1.5s），真正跑 python / 监听端口 |

两者 `ExecutablePath` 完全相同，所以**只按名字或路径分不出父子** —— 判定归属必须靠
`ParentProcessId` + 创建时间，或者靠 Job Object / 按 pid 的 `taskkill /T`。

## 2. 六个生命周期 case 的结论

| case | 操作 | 结果 |
| --- | --- | --- |
| 1 | 对后端控制台发 Ctrl+C（正常退出） | launcher 与 child 都退出，端口关闭，无残留 |
| 2 | 只结束 launcher | **child 仍活着并继续监听端口** —— 孤儿是真的 |
| 3 | 结束 child | launcher 以退出码 1 结束，无残留 |
| 4 | 硬杀 child（崩溃） | launcher 结束，无残留 |
| 5 | 快速重复启动 / 撞同一端口 | 每个实例各自一对 launcher+child；撞端口那个绑定失败（WinError 10048）后干净退出 |
| 6 | 运行中替换/删除 exe | 改名可以；**删除与原地覆盖被拒**（WinError 5 / EACCES）；结束进程后三种都成功 |

case 6 是「安装器报 Can't write」的直接原因：只要还有一个进程映射着 `qio-backend.exe`，
NSIS 就删不掉、也写不进去；而 case 2 说明**只杀 launcher 不足以**解除这个锁。

## 3. Job Object 到底有没有生效（实测）

`frontend/src-tauri/src/main.rs` 在 `main()` 里建 job（`KILL_ON_JOB_CLOSE`），在
`backend_launch()` 之后**立刻**把 launcher 的 pid 放进去。四个实验：

| 实验 | 做法 | 结果 |
| --- | --- | --- |
| 立即 assign（与壳同序） | spawn 后马上 assign | job 里同时有 launcher 与 **child**；关掉 job 句柄后全部消失、端口关闭 |
| 等 child 出现再 assign（反证） | 等第二个进程出现才 assign | child **不在** job 里；关掉句柄后 child 还在监听 —— 孤儿复现 |
| 宿主已有 job（嵌套） | 先放进 J1 再放进 J2 | 两次 assign 都成功，child 仍在 job 里，关闭 J2 后全清 |
| 壳被强杀 | 建 job + assign 后进程直接退出 | 无残留、端口关闭（句柄随进程消失 → 系统清理） |

结论：**已有的 Job Object 修复是真的生效的**，前提是 assign 必须紧跟 spawn —— onefile 的
child 是之后才创建的，Windows 只把「指派之后创建的后代」自动收进 job。这条时序是修复的
一部分，代码里已写明，并由 `backend_job::tests::descendants_created_before_assignment_are_not_captured`
钉住（谁把 assign 挪到 child 创建之后，测试就会红）。

## 4. 兜底路径（2026-10-02 补齐）

job 建不出来或 assign 失败（例如宿主 job 不允许嵌套）时，退出路径过去只调 `child.kill()`，
也就是**只杀 launcher** —— 正是 case 2 的孤儿形状。现在 `RunEvent::Exit` 会看 job 是否真的
生效：没生效就退回 `kill_tree_by_pid()`（`taskkill /PID <pid> /T /F`，带硬超时）。

**ownership 原则**：只按我们自己拉起的 pid 杀整棵树，绝不按进程名批量杀 —— 开发实例、
测试实例、其它安装实例都不能被误伤。

## 5. 已知边界（诚实清单）

* 本机实验是**直接拉起 onefile 产物**（不经过 Tauri 壳的 GUI），壳那一侧的行为由代码路径 +
  Job Object 语义实验 + Rust 单测覆盖；真机安装后的完整 E2E 仍必须按发布流程执行。
* 本会话进程是 Low 完整性，写 HKCU 会被拒（数据库身份基线默认在注册表里），所以脚本用仓库
  自带的 `QIO_DB_BASELINE` 把基线指到实验目录；这不改变产品行为，只是让隔离实例能起来。
* 删除/改名/覆盖的结论来自 Windows 的映像文件语义；换文件系统或换平台要重新验证。