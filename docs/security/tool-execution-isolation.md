# 生成工具执行隔离：威胁模型、能力矩阵与已落地的强制措施

状态（2026-10-02，第二阶段）：**已经落地两项真实的、内核强制的措施** —— Job Object 资源边界与
Windows 低完整性（MIC）降级；**仍然没有做到**网络隔离、读隔离、以及 AppContainer 级边界。
本文把「做到什么 / 没做到什么 / 需要什么才能做到」逐条写清，不用文案掩盖。

责任范围：本文只谈「AI 生成的工具代码在哪跑、被什么挡住」。审批流程见
backend/src/agent/tools/dev_auth.py 与 lifecycle.py；执行器见 sandbox.py；
本阶段新增的强制实现在 backend/src/agent/tools/isolation.py。

---

## 1. 一句话结论

生成代码仍然跑在**同一用户权限**的独立子进程里（受限子进程）。当前**默认生效**的强制只有一层：

1. **Job Object（默认开）**：进程内存上限、活动进程数上限、关句柄即收整棵树；
2. **低完整性级别（Low IL，默认关）**：`QIO_TOOL_LOW_INTEGRITY=1` 打开后，工具**写不进**
   用户文件与 QIO 数据目录（Medium 及以上标签的对象），只能写调用方声明可写的目录；
   **读不受限制**。

**为什么第二层默认关**（2026-10-02 CI 实测）：在 windows-latest（普通完整性）上它确实生效，
但工具连**自己的 scratch 目录**与 **mock 夹具目录**都写不进去 —— 标签没有可核实的落地，
结果是 mock 服务报告、进程树标记文件写不出来（8 条用例在 CI 上变红）。修法有两条，都做了：

* 打标签之后**读回核实**（`integrity_label_of`）；核实不了就**跳过降级**（fail-safe），
  并把原因写进结果 —— 宁可少一层写边界，也不能让工具连自己的目录都写不了；
* 默认改为**关闭**：只保留 Job Object 这一项不受写权限影响的真实强制。

打开的正确姿势：`QIO_TOOL_LOW_INTEGRITY=1`，并保证调用方把所有「工具合法需要写」的目录
通过 `SandboxExecutor.execute(..., extra_writable_dirs=[...])` 声明出来（例如 mock 夹具目录）；
任何一个声明目录核实不到低标签，这次就整体不降级。

它**不是安全沙箱**：网络完全不受限，读取完全不受限，路径猜得到就访问得到。
因此界面、审批说明与文档一律按「受限子进程 + 已生效的强制项」表述，不叫「安全沙箱」。

---

## 2. 威胁模型

### 2.1 保护对象与当前状态（第二阶段实测）

| 资产 | 攻击者能做什么（无强制时） | 现在被谁挡住 | 现状 |
| --- | --- | --- | --- |
| 1 用户文件 | 读、写、删 | 写：Windows MIC（低完整性，**默认关**）；读：无 | **写默认可写、读未挡**（打开开关后写被挡） |
| 2 API Key | 读用户自己的凭据文件、打日志外带 | 凭据不注入子进程（环境白名单）；读：无 | **注入已挡、读取未挡** |
| 3 QIO 数据目录 | 读、写、删（同用户权限） | 写：Windows MIC（**默认关**）；读：无 | **写默认可写、读未挡**（打开开关后写被挡） |
| 4 网络访问 | 任意出网、横向、连本机端口 | **无** | **未实现**（需要 WFP／管理员，见 3.7） |
| 5 任意进程控制 | 无限 fork、吃光内存、常驻 | **Job Object**：内存上限、活动进程上限、关句柄即收树 | **资源被限、拉起进程本身未禁止** |

### 2.2 对手模型

* **对手 = AI 生成的工具代码本身**：包括谎报能力者（声明 PURE、实际联网/读写文件）、
  被投毒依赖带进来的代码、被提示注入诱导写出的代码。
* **对手不具备**：管理员权限（本机实测：令牌里 Administrators 是 deny-only、`net session` 返回
  System error 5，即**没有提权**）、内核漏洞利用、硬件攻击。
* **用户批准过的东西不算越权**：显式批准的 TRUSTED 能力按批准执行；本文只关心「批准之外的能力
  是否真的被挡住」。

### 2.3 信任边界

```
后端进程（可信，持有凭据/数据库）
   │  审批（用户确认执行边界）
   ▼
执行器（SandboxExecutor）
   ├─ docker 分支：容器是边界（--network none、--read-only、pids-limit…）—— 仅当守护进程可用
   └─ 受限子进程分支：
        Job Object（内核）：内存 / 活动进程数 / 关句柄即收树
        低完整性令牌（内核 MIC）：写不进 Medium 及以上的对象
        降险（非边界）：一次性临时目录、超时、输出上限、环境白名单、不注入凭据
```
---

## 3. Windows 可用能力矩阵（本机实测，2026-10-02，非提权普通账户）

### 3.1 Job Object —— 已采用

| 能力 | 本机实测结果 | 挡住什么 | 挡不住什么 | 需要管理员？ |
| --- | --- | --- | --- | --- |
| 创建 / 设限 / 指派 | CreateJobObjectW 返回句柄、SetInformationJobObject 与 AssignProcessToJobObject 均 True | —— | —— | **不需要** |
| 进程内存上限 64MB | 工具分配 200MB → MemoryError after 60 MB（没有把整机吃光） | 单个工具进程吃光内存 | 磁盘、句柄、CPU 时间 | 不需要 |
| 活动进程上限 3 | 工具连拉 5 个 → 1 个成功、4 个 WinError 1816（配额不足） | fork 炸弹 / 进程风暴 | 单个子进程内部资源滥用 | 不需要 |
| KILL_ON_JOB_CLOSE | 关句柄后 tasklist 显示 worker 与孙进程都不在了 | QIO 退出/被强杀后的残留进程 | —— | 不需要 |

产品里生效的参数：isolation.JOB_PROCESS_MEMORY_BYTES = 1 GiB、JOB_ACTIVE_PROCESS_LIMIT = 32、
kill_on_close = True；每次工具调用结束调用 isolation.release(process) 关句柄。

### 3.2 Windows ACL —— 不作为边界

实测：icacls 加 deny 之后写确实被拒（PermissionError），但**同一用户是所有者**，
icacls /remove:d 返回 0 之后又能写 —— 所有者可以改回 DACL。
所以 ACL 挡不住「以同一用户身份运行的恶意工具」，只能用于加固。

### 3.3 restricted token —— 可用但不够

实测：OpenProcessToken → True、CreateRestrictedToken(DISABLE_MAX_PRIVILEGE) → True、
CreateProcessAsUserW → True（成功跑起 cmd /c echo hi），**不需要管理员**。
但受限令牌只是禁用特权/去掉 SID，**不改变文件系统访问权**（同一个用户 SID）。
真正让它成为边界的是把令牌的完整性级别降到 Low —— 这一条已单独实现并采用（见 3.6）。
用它启动新进程需要改 sandbox 的启动路径（本阶段只允许追加式钩子），因此没有采用。

### 3.4 AppContainer —— 需要提权，本机不可行

实测：CreateAppContainerProfile 返回 **0x80070005（拒绝访问）**，profile 未创建；
本机 userenv.dll 里也没有 GetAppContainerSidFromAppContainerName。
结论：AppContainer 需要提权与额外的清单/ACL 授权，属阶段 2 的架构变更（原生启动器 + 真机验收）。

### 3.5 专用低权限用户 —— 不可行

实测：net user qio_isolated_probe … /add → Access is denied.（exit 2）；
当前令牌里 Administrators 是 deny-only，net session → System error 5。
普通用户机器上不能建专用用户，也不该要求用户提权。**不作为产品路径。**
### 3.6 低完整性级别（MIC）—— 已实现，但**默认关闭**

实测（Medium 父进程 → 子进程降级；同一次实验里有不降级的对照组）：

```
不降级（对照）                     降级到 Low
child 降级前: S-1-16-8192          child 降级后: S-1-16-4096
scratch    WRITE-OK                scratch    WRITE-OK     （scratch 打了 Low 标签）
user_files WRITE-OK                user_files WRITE-DENIED PermissionError
qio_data   WRITE-OK                qio_data   WRITE-DENIED PermissionError
```

实现：父进程在子进程创建后 OpenProcessToken + SetTokenInformation(TokenIntegrityLevel, Low)；
同时把这次调用需要可写的目录打上 Low 标签（SDDL S:(ML;OICI;NW;;;LW)，回退 icacls /T），
并**读回核实**（integrity_label_of → label_is_low）。已接进 _execute_subprocess（追加式钩子），
SandboxResult.isolation 里能看到实际用了哪些机制、哪些没生效、以及原因。

**默认关闭**：`QIO_TOOL_LOW_INTEGRITY=1` 打开。CI 实测（windows-latest）降级一旦生效，
工具连自己的 scratch 与 mock 夹具目录都写不进去 —— 说明标签在这些环境下没有可核实的落地。
打开后的契约（也是测试断言的契约）：

* `scratch_dir` 与调用方声明的 `extra_writable_dirs` 必须都核实为低标签，否则**整体不降级**
  （fail-safe：宁可少一层写边界，也不能让工具连自己的目录都写不了）；
* 降级成功后，工具写这些目录正常，写 Medium 及以上标签的对象被内核拒绝；
* 工具声明了文件系统能力（policy.filesystem 非空）时同样跳过降级。

**没有挡住**：读（Low 只限制「写向上」）、网络、以及工具自己猜到的路径。

### 3.7 网络隔离 —— 未实现

低完整性不影响出网；非提权账户也装不了 WFP 过滤器（需要管理员）。
容器路径下由 --network none 提供，但本机没有 Docker。如实写「未实现」。

### 3.8 filesystem staging + 一次性目录 —— 已实现，并做了渗透验证

渗透测试（backend/tests/test_tool_isolation.py）：后端 env 里确实设了 QIO_DATA_DIR，
工具主动去找它的**指针**，实测结果：

```
env_has_data_dir = False
env_keys = [COMSPEC, PATH, PATHEXT, PYTHONIOENCODING, PYTHONUTF8, SYSTEMDRIVE, SYSTEMROOT, TEMP, TMP, WINDIR]
cwd = 每次调用的一次性目录（不在数据目录下）
syspath_hits = []
argv_tail = []
```

**边界**：这证明的是「没有任何指针递给它」，**不是「访问不到」**。工具仍可按常见路径去猜，
也能读它猜到的任何文件（读没有被隔离）。

---

## 4. 明确没有做到的事（不要只看第 3 节的好消息）

* **网络没有任何强制隔离**：工具可以出网。
* **读没有隔离**：工具知道路径就能读用户文件与 QIO 数据目录。
* **路径猜不到 ≠ 访问不到**：指针不泄露只降低「顺手拿到」的概率。
* **Job 只包含「指派之后」创建的后代**：指派之前已存在的孙进程不在 job 里，关句柄也收不掉。
  真实工具执行里没有这个窗口 —— worker 读到请求之前不执行工具代码，而请求是在 harden() 之后
  才写进 stdin 的（时序由 test_closing_the_job_handle_reaps_descendants_created_after_assignment 锁住；
  边界由 test_a_child_created_before_assignment_is_not_contained 老实记录）。
* **POSIX 上不提供任何隔离**：isolation.harden() 返回 unsupported，行为与改动前一致。
* **低完整性降级默认关闭**：不设 QIO_TOOL_LOW_INTEGRITY（或设 0）时只有 Job Object 生效；
  此时 SandboxResult.isolation.problems 里会写明「默认关闭 + 怎么开」。
* **打开低完整性后，调用方必须声明工具要写的目录**（extra_writable_dirs）：目前 sandbox 侧已支持，
  但 mock 夹具目录还没有接上（tester/mock_services 不在本次改动范围），打开开关前需要先接。

---

## 5. 分阶段实现

### 阶段 0（上一阶段完成）

* isolation_for_executor / isolation_label / unprotected_surfaces：隔离等级、用户可见说法、
  没有保护的面只有一个来源；
* default_policy_for(definition, executor=...) 按真实执行器推导隔离等级并进指纹；
* 执行审批逐字段绑定身份，并明确列出没有保护的面；
* 授权生命周期三种（本次执行 / 当前开发任务 / 长期授权），没有「不撤销就永久有效」；
* 「受限子进程」不允许被叫成「安全沙箱」（isolation_label + 测试锁住）。

### 阶段 1（本阶段完成）

* Job Object：内存 / 活动进程数上限 + 关句柄即收整棵树（tools/isolation.py）；
* 低完整性降级：工具写不进用户文件与 QIO 数据目录，自己的一次性目录仍可写；
* staging 渗透验证：指针不泄露（env / cwd / sys.path / argv）有真实用例；
* 可观测：每次执行的实际隔离结果随 SandboxResult.isolation 一起交出去。

验收：backend/tests/test_tool_isolation.py（真实内核 API，不是 mock）。

### 阶段 2（架构变更，仍未做）

* 原生启动器（Rust，随 Tauri 打包）用 AppContainer 启动 worker：只授权一次性目录（写）与
  只读的必要系统路径，默认无网络 capability。前置条件：
  1) 提权创建 AppContainer profile（本机实测 0x80070005），或改用受限令牌 + 低完整性组合；
  2) 给 Python 运行时目录授 AppContainer SID 读权限（否则解释器起不来）；
  3) frozen（打包后）真机验收。
* 网络隔离：Windows 需要 WFP（管理员）或容器（本机没有 Docker）。

### 阶段 3（跨平台收口）

* POSIX 以容器为默认隔离，CI 用 ubuntu runner 复现「容器内拒绝越界」；
* Windows 上 AppContainer 与容器按探测结果二选一，保持同样的诚实文案。

---

## 6. 明确不做 / 不声称的事

* 不把「受限子进程」叫成「安全沙箱」——界面、审批说明、文档一律按 isolation_label 说。
* 不声称「数据不会被读出」「不会联网」：读与网络都没有被隔离。
* 不用「用户已批准」代替隔离：批准的是能力范围，不是「代码一定守规矩」。
* 不声称 AppContainer / 受限令牌已经接上：前者本机被拒（0x80070005），后者需要改启动路径。