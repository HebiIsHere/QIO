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

**为什么第二层默认关**（2026-10-02 CI 实测 → 2026-10-03 定位到根因，见第 9 节）：
打标签的那次调用**返回成功但其实没落上标签**，代码又只信返回值，于是降级后工具连
自己的 scratch 目录与 mock 夹具目录都写不进去（CI 上 8 条用例变红）。现在的状态：

* 打标签改成 `icacls /setintegritylevel` + **读回核实**（`integrity_label_of` → `label_is_low`，
  必须有 `(ML;…;LW)` 才算数）；核实不了就**跳过降级**（fail-safe），并把原因写进结果 ——
  宁可少一层写边界，也不能让工具连自己的目录都写不了；
* 默认仍然**关闭**：只保留 Job Object 这一项不受写权限影响的真实强制。原因是第二个环境
  （CI windows-latest）还没有跑出三件套证据 —— 只在一种环境里成立的东西不配做默认值。

打开的正确姿势：`QIO_TOOL_LOW_INTEGRITY=1`，并保证调用方把所有「工具合法需要写」的目录
通过 `SandboxExecutor.execute(..., extra_writable_dirs=[...])` 声明出来（例如 mock 夹具目录）；
任何一个声明目录核实不到低标签，这次就整体不降级。打开开关后，
`SandboxResult.isolation.detail` 会写清哪一层是**读回核实过**的，没成的写进 `problems`。

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
---

## 7. 网络强制边界：工程判断（本轮不实现）

**结论：当前架构下没有任何一种「低成本」办法能形成网络的强制边界；真正可行的只有容器。**

| 方案 | 能不能形成强制边界 | 需要什么 | 本轮为什么不选 |
| --- | --- | --- | --- |
| Docker 容器（`--network none`） | **能**，而且是唯一已经在代码里的强制路径 | Docker daemon + 镜像 | 本机没有 Docker；且只是「有容器就用」的既有分支，不是新机制 |
| Windows 过滤平台 WFP（WFP callout / `FwpmFilterAdd`） | **能** | 管理员权限（安装过滤器驱动/策略） | 普通用户非提权，装不了；要求提权违反产品前提 |
| AppContainer（`internetClient` capability 不给） | **能** | 提权创建 profile（实测 `0x80070005`）+ 运行时目录 ACL + 原生启动器 | 属架构变更（阶段 2），本轮无预算 |
| 专用低权限用户 + 出网代理/防火墙规则 | 能 | 管理员建用户 + 防火墙策略 | 实测 `net user … /add` → `Access is denied.` |
| 低完整性（MIC） | **不能** | —— | 实测：低完整性不影响出网，只限制「写向上」 |
| Python monkeypatch（拦 `socket.connect`）/ 环境变量代理 / prompt 里叮嘱模型 | **不能** | —— | 只对「配合的代码」有效，工具可以自己 `ctypes` 直接调 Winsock；**这不是强制边界，禁止这样宣称** |

给产品的说法：**网络当前没有强制边界**；要形成边界，路径是「容器（跨平台）/ AppContainer（Windows 原生，需提权与原生启动器）」。
在这之前，任何「工具不能联网」的说法都只能是 QIO policy（声明/审批/默认不给凭据），不是 OS enforcement。

## 8. QIO policy 与 OS enforcement：文案口径（C6）

两件事必须分开说，不能混成一句「更安全」：

* **QIO policy**（QIO 承诺做什么）：能力声明与审批、默认不向工具提供凭据、默认不给用户文件路径、
  一次性临时目录、超时、输出上限。这些是**策略**，工具撒谎或绕过就没了。
* **OS enforcement**（Windows 真的挡住了什么）：Job Object 的进程内存/活动进程/关句柄收树
  （默认生效）；低完整性写入边界（默认关闭）。**读与网络没有 enforcement。**

审计结果（2026-10-03，`rg` 全仓）：

* `README.md:29`「受限子进程不是安全沙箱」✓；`docs/architecture.md` §6 明确写「读没有隔离 / 网络没有隔离」✓；
* 前端 `DevTaskEntry.vue`：「本机受限子进程（同一用户权限，不是安全沙箱）」✓；
  `ApprovalModal.vue` 用行为句 + 能力清单，未出现「无法读取用户文件」这类越界表述 ✓；
* `frontend/src` 里与隔离相关的表述共 4 处（`rg "隔离|沙箱" frontend/src`），无一处声称读/网络被强制；
* 因此本轮**没有改前端文案**（改了就得分摊 vue-tsc + vitest 的验证预算，而当前没有可修的越界表述）。

唯一需要 Lead 落笔的地方（那 5 个文档不归我）：`docs/status.md` / `docs/release-qualification.md`
里的安全边界段可以补一句「Job Object 已读回核实；低完整性默认关；读与网络无强制边界」。

## 9. C1/C2 定位记录：为什么有的环境 scratch 写不了（2026-10-03 最小实验）

实验脚本：`scripts/low_integrity_probe.py`（不依赖 QIO 工具系统，stdlib + ctypes，逐步打印事实）。
关键前提：**必须用普通完整性的解释器当父进程** —— 本机 `uv run` 出来的 python 自己在 Low 完整性，
降级是 no-op，永远复现不了 CI 的现象（实测：`uv run` python = `S-1-16-4096`，系统 Python312 = `S-1-16-8192`）。

原始输出（Windows 10.0.26200 / NTFS / Medium 父进程 S-1-16-8192）：

```
标签方式：SDDL
  scratch 标签 设置前='（没有标签 ACE）' 调用=SetNamedSecurityInfoW rc=0 设置后='S:AINO_ACCESS_CONTROL'
  scratch icacls 标签行: []
  child 降级前 IL: S-1-16-8192 | 降级: ok | 降级后 IL: S-1-16-4096
  scratch=WRITE-DENIED PermissionError      ← 标签没落上：目录仍是 Medium，Low 写不进去
  user=WRITE-DENIED PermissionError
  qio=WRITE-DENIED PermissionError

标签方式：icacls
  scratch 标签 设置前='S:AINO_ACCESS_CONTROL' 调用=icacls exit=0 设置后='S:AI(ML;OICI;NW;;;LW)'
  scratch icacls 标签行: ['Mandatory Label\Low Mandatory Level:(OI)(CI)(NW)']
  child 降级前 IL: S-1-16-8192 | 降级: ok | 降级后 IL: S-1-16-4096
  scratch=WRITE-OK                          ← 标签真的落上了
  user=WRITE-DENIED PermissionError
  qio=WRITE-DENIED PermissionError

对照：不降级
  child IL: S-1-16-8192 -> S-1-16-8192
  scratch=WRITE-OK / user=WRITE-OK / qio=WRITE-OK
```

**C2 的答案（可判定条件）**：scratch 写不写得进去，不取决于环境「某些环境」，而取决于
**那个目录的强制标签是否真的落下**：

* 目录的标签读回是 `(ML;…;LW)` → Low 工具能写它（并且能写自己）；
* 目录没有标签 ACE（沿用父容器的 Medium）→ Low 工具写不了，无论调用返回什么。
**判定动作 = 读回标签**，不是看 `SetNamedSecurityInfoW` 的返回值 —— 它在本次实验里返回 0
但产生了 `S:AINO_ACCESS_CONTROL`（没有标签 ACE）。

**C3 修复**：`label_low()` 改成只用 `icacls /setintegritylevel (OI)(CI)L /T` + 读回核实；
`label_is_low()` 收紧为「必须有强制标签 ACE **且** 是 LW」。
用**生产代码**在 Medium 父进程下复验（`scripts/low_integrity_real_check.py`）：

```
父进程完整性: S-1-16-8192
label_low(scratch) -> 'icacls'
读回 scratch 标签: 'S:AI(ML;OICI;NW;;;LW)'   label_is_low: True
harden outcome: {"applied": true, "mechanisms": ["job_object", "low_integrity"], ... "problems": []}
child 降级后 IL: S-1-16-4096
scratch=WRITE-OK / user=WRITE-DENIED PermissionError / qio=WRITE-DENIED PermissionError
```

## 10. C5 fail-safe：声称的隔离必须与事实一致（已修）

改成「读回核实之后才算数」：

* Job Object：`AssignProcessToJobObject` 之后再 `IsProcessInJob` 核实；核实不了就不写进 mechanisms
  （踩过的坑：查询需要 `PROCESS_QUERY_LIMITED_INFORMATION`，少了它以 err=5 假失败）；
* 低完整性：`SetTokenInformation` 之后再 `integrity_of_process(pid)` 读回，必须是 `S-1-16-4096`，
  否则写进 problems 并按**未降级**处理；
* `IsolationOutcome.detail` 只描述**核实过**的机制（哪一层没成就说哪一层没成），不再出现
  「已强制：内存/进程上限」这种在 job 失败时也照写的模板句。
对应测试：`backend/tests/test_tool_isolation.py` 的
`test_low_integrity_is_not_claimed_when_the_token_did_not_downgrade`、
`test_job_object_is_not_claimed_when_the_assignment_fails`、
`test_label_low_refuses_when_the_readback_does_not_show_low`、
`test_label_is_low_only_accepts_a_real_label_ace`、
`test_the_declared_triple_holds_with_a_non_low_parent`。
