# QIO 电脑操控能力设计文档

日期：2026-09-09
状态：设计评审稿（待用户确认后进入实现计划）

## 1. 背景与目标

qio 是自托管编码型 agent。当前它能联网搜索、读网页、管理记忆和话题、开发工具，但**无法直接操控运行它的电脑**（读写任意文件、执行命令、查看进程）。本设计为 qio 增加"护栏式"的电脑操控能力，让它能真正"做事"，同时保持安全可控。

### 目标

- 新增第一梯队工具：**文件系统 / 命令执行 / 进程与系统信息**。
- 建立**分级授权**模型：低危自动放行、高危走审批、敏感路径直接拒绝。
- 复用 qio 现有 `ApprovalService` 和 `ServiceRegistry`，不另造一套审批。
- 默认受限（工作区圈定 + 敏感文件拦截），容器隔离作为可选项。
- 严格遵守 qio 设计风格，工具命名贴合现有 snake_case 描述式（如 `memory_search`）。

### 非目标（本轮不做）

- **不做应用/桌面控制**（鼠标/键盘/浏览器自动化）。已单独排期后续轮次。
- 不绕过任何操作系统安全边界；不做未经授权的"随便执行"。
- 不做跨机器的分布式控制。

## 2. 调研结论（决定设计的依据）

三路调研（权限、沙箱、工具集）一致指向同一范式：**受限 + 分级授权 + 沙箱/隔离 + 高危审批 + 可显式放行**。没有任何主流 agent 做"默认全域自由控制"。关键共识：

- **审批与沙箱是两件分离的事**：审批管"被允许做什么"，沙箱管"能跑到哪"，两者由不用角色负责。
- **分级权限**：读/查低危自动放；写/删/改装高危走审批；`.env`、`.git/`、凭据、内网/云元数据为绝对红线。
- **权限模式档位**：常用 `default / acceptEdits / plan / bypassPermissions` 四档；`bypassPermissions` 只建议在隔离环境用。
- **敏感路径拦截**：即使在工作区内，`.git/`、凭据、cookie、SSH key 也要拒。
- **命令执行**：要么沙箱隔离，要么让模型按命令/参数动态打"是否需审批"标记。

qio 现状：已有 `ApprovalService`（新建话题/工具走审批）、`ServiceRegistry`（依赖注入）、`SandboxExecutor`（detect docker/subprocess）、`dev_workspaces`（受限工作区）。本设计复用这些，而非重建。

## 3. 权限模型（核心）

按危险度分三档，复用 `ApprovalService.request(kind, payload)`：

### 3.1 低危（自动放行，无需审批）

- 读文件、列目录、找文件。
- 查系统信息、查进程列表、查文档。
- 低危命令（`git status`、`ls`、`pwd`、`which`、`find`、`grep`）。

### 3.2 高危（走审批，复用 ApprovalService）

- 写/改/删文件（patch 编辑）。
- 执行"可能改变状态"的命令（`rm`、`sudo`、`pip install`、`npm install`、写系统配置、启动/停止服务）。
- 可配置为"工作区之外"的所有操作。

### 3.3 绝对红线（NeverAllow，直接拒绝，不弹审批）

- 访问 `.env`、`.git/`、SSH 私钥、cookie、浏览器状态、`credentials`。
- 访问内网地址 / 云元数据地址（如 `169.254.169.254`）。
- 这些即使在工作区内也一律拒绝。

### 3.4 权限模式（随会话，非逐次点按钮）

提供一个会话级档位（默认 `default`）：

- `default`：低危自动，高危审批。
- `plan`（只读）：只读，绝不写/执行（用于"先看计划"）。
- `accept-edits`：文件编辑自动放行，但命令仍审批。
- `bypass`（全放行）：**警告**，仅建议在隔离容器/专用机上用。

档位存 `SettingsStore`（键 `computer.permission_mode`），默认 `default`。

## 4. 工具集（第一梯队）

### 4.1 文件系统（`agent/tools/fs_tools.py`）

- `fs_read(path)`：读文件（自动）。
- `fs_write(path, content)`：写文件（审批；patch 式，见下）。
- `fs_patch(path, old, new)`：基于 diff 的结构化编辑（审批）。
- `fs_list(dir)`：列目录（自动）。
- `fs_find(query, dir)`：按名字查找（自动）。
- `fs_info(path)`：文件大小/时间/编码（自动）。

**编辑形态**：用 `fs_patch`（结构化 diff），不整文件覆盖。这样给模型的是"改哪几行"，更可控、更省 token。

**敏感文件拦截**：`fs_read`/`fs_write`/`fs_patch` 在访问前先解析规范化路径，拒绝红线路径。

**工作区根**：可配置（键 `computer.root_dir`），默认指向 qio 数据目录下一个"工作区"子目录；`..`/符号链接穿越防解析。工作区外访问默认走审批。

### 4.2 命令执行（`agent/tools/cmd_tools.py`）

- `run_cmd(cmd, cwd=None, timeout=30)`：执行命令，捕获 stdout/stderr，禁止 shell 注入。
- `sys_info()`：平台/CPU/内存/磁盘（自动）。
- `proc_list()`：列出进程（自动）。
- `proc_kill(pid)`：结束进程（审批）。

**命令风险判定**：内置一个"风险判定器"（`CommandRisk`），按命令名/参数判定 `low / high / danger`：

- `low`：`git status`、`ls`、`pwd`、`which`、`find`、`grep`、`cat`（只读）→ 自动放行。
- `high`：`rm`、`mv`、`sudo`、`pip install`、`npm install`、`curl|sh`、写系统配置 → 走审批。
- 默认保守：**未匹配到白名单的命令视为 `high`**（fail-closed，匹配不上就审批，而非放行）。

### 4.3 共享服务（`agent/services/computer.py`）

一个 `ComputerSandbox` 服务，统一负责：

- 路径规范化 + 工作区根解析 + 敏感路径拦截。
- 命令风险判定。
- 权限档位读取。
- 供应商 `permission` 判定给工具（`inject = ["computer"]`）。

工具不自己实现安全逻辑，统一走 `ComputerSandbox`，保证边界一致。

## 5. 数据流

```
主 agent 决定调用 fs_read / run_cmd / proc_list ...
    │
    ▼
ToolRegistry.execute → 工具.run
    │
    ▼
计算机具调用 ComputerSandbox.check(action)
    ├─ 红线路径 ──► 直接拒绝（NeverAllow，不弹审批）
    ├─ 低危 ──► 自动放行
    └─ 高危 ──► ApprovalService.request("computer", ...) 
                    ├─ approved ──► 执行
                    ├─ rejected ──► 不执行，返回"未获批准"
                    └─ timeout ──► 不执行，返回"审批超时"
    │
    ▼
结果回传给主 agent
```

## 6. 审批与呈现

- 复用 `ApprovalService`（`kind="computer"`），触发 `APPROVAL_REQUIRED` 事件，前端审批弹窗处理。
- 审批弹窗展示：**动作类型（读/写/命令）、目标路径或命令、风险等级、为什么需要批准**。
- 工具在前端以"工具卡"呈现（复用 `TOOL_END`/presentation），collapsed 默认收起，符合 qio 现有工具卡风格。
- 审批超时/拒绝返回明确结果，**绝不产生副作用或编造**。

## 7. 配置存储（SettingsStore）

- `computer.root_dir`：允许访问的工作区根（默认 = `data_dir / "workspace"`）。
- `computer.permission_mode`：`default | plan | accept-edits | bypass`（默认 `default`）。
- `computer.cmd_allowlist`：额外追加的低危命令白名单（可选）。

后端新增 `GET/PUT /api/settings/computer`，前端设置页「偏好」下加"电脑操控"卡片。

## 8. 前端

- `SettingsView.vue` 偏好页新增「电脑操控」section：
  - 工作区根目录（文本输入）。
  - 权限模式（QSelect，四档）。
- `api.ts` 新增 `getComputerSettings` / `updateComputerSettings`。
- 遵循 qio 设计风格：`var(--*)`、三声部字体、卡片样式参照现有 pref 卡片。

## 9. 测试策略

### 后端（pytest）

- `test_fs_tools.py`：`fs_read`/`fs_write`/`fs_patch`/`fs_list`/`fs_find` 读写、规范化路径防穿越、红线路径拦截。
- `test_cmd_tools.py`：`run_cmd` 捕获输出、超时、命令风险判定（low/high/danger、fail-closed）。
- `test_computer_sandbox.py`：路径规范化、工作区根、敏感拦截、权限档位判定。
- `test_computer_sandbox_permission.py`（或并入）：低危自动放行、高危走审批、拒绝/超时无副作用。

### 前端（vitest + vue-tsc）

- 更新 `SettingsView.test.ts`：电脑操控卡片渲染、读写、权限模式选择、保存 notice。
- `vue-tsc --noEmit` 通过。

## 10. 设计风格约定（必须遵守）

- 后端：继承 `Tool`、`ServiceRegistry` 注入、`ToolRegistry.register` 挂载；共享服务 `ComputerSandbox` 统一安全逻辑。
- 前端：颜色一律 `var(--*)`，禁止硬编码；三声部字体；弹窗/卡片参照 `ApprovalModal.vue`、`CredentialCard.vue`；选中态 `--accent` + `--accent-soft`。
- 工具命名 snake_case 描述式：`fs_read`、`run_cmd`、`proc_list`（对齐 `memory_search`、`web_search`）。

## 11. 已确认决策（2026-09-09）

1. **文件编辑**：patch 式（结构化 diff），不整文件覆盖。
2. **命令执行审批**：按风险分级——低危自动放、高危走审批；未匹配白名单视为高危（fail-closed）。
3. **默认工作区根**：可配置，默认 `data_dir / "workspace"`；工作区外默认询问。
4. **应用/桌面控制**：本轮不做。

用户已确认方向（护栏式控制），进入 `writing-plans` 生成实现计划。
