# 发布前第六轮：Windows 候选安装包与真实服务验证（2026-10-29）

- 分支：`fix/release-step6-native-validation-20261029`
- 基线：第五轮已验收提交 `1e8592b5275042036a8b33ffab2199273ba4ae3f`
- 构建代码 SHA：`f9d250cfecee4aef3f0703ced16ba331bbce844d`（版本 0.3.0 同步提交）
- 受测安装包：`QIO_0.3.0_x64-setup-UNSIGNED-TEST.exe`（未签名测试产物）
- 交付 HEAD：本文件所在提交（完整 SHA 以推送回执为准；见文末「提交与交付」）
- 未合并 main、未打标签、未发布、未执行 publish_release.ps1、未创建 GitHub Release、
  未上传发布资产、未修改生产更新源、未触发/重跑 CI。

## 一、候选、基线、main 与版本标签（实际核对）

| 项 | 实际值 |
| --- | --- |
| origin/main | `1b03541eedd2dcaa9b82d6102108d717c71c6fb4` |
| 候选构建提交 | `f9d250cfecee4aef3f0703ced16ba331bbce844d`（分支尖端） |
| 祖先关系 | 候选是 origin/main 的**严格后代**：`git rev-list --left-right --count origin/main...HEAD` = `0 318`（main 独有 0 / 候选独有 318），merge-base = origin/main |
| main 独立新增提交 | 无；不需要重放已验收修复，也没有把 main 新提交混入候选 |
| 版本标签 | v0.3.0 本地与远端均不存在（未打标签，无标签占用冲突）；候选相对第五轮基线只多 1 个纯版本号同步提交 |
| 远端基线核对 | `origin/fix/release-step5-integration-validation-20261029` = 1e8592b…，本轮从该提交新建分支，未改写历史 |

## 二、版本 0.3.0 同步清单（只改本项目版本字段，未升级依赖）

同步提交 `f9d250cf`，共 10 个文件、+11/−11 行：

| 层 | 文件 | 字段 |
| --- | --- | --- |
| 前端 | `frontend/package.json` | `version` |
| 前端锁文件 | `frontend/package-lock.json` | 根包与 `packages[""]` 的 `version`（仅版本信息，依赖未动） |
| 桌面壳 | `frontend/src-tauri/tauri.conf.json` | `version` |
| 桌面壳 | `frontend/src-tauri/Cargo.toml` | `[package] version` |
| 桌面壳锁文件 | `frontend/src-tauri/Cargo.lock` | 本项目 `qio` 包 `version`（依赖未动） |
| 后端 | `backend/pyproject.toml` | `[project] version` |
| 后端 | `backend/src/agent/__init__.py` | `__version__` |
| 后端 | `backend/src/agent/api/server.py` | FastAPI `app = FastAPI(..., version=...)` |
| 后端锁文件 | `backend/uv.lock` | 本项目 `qio` 可编辑包 `version`（依赖未动） |
| 设置页显示版本测试夹具 | `frontend/src/views/settings/__tests__/settingsApiMock.ts` | `app_version` 夹具同步 |

核对结果：上述 10 处全部为 `0.3.0`；构建出的安装包 NSIS 载荷版本为 0.3.0；安装目录里 `qio.exe`
与内置 sidecar 的版本事实由发布闸门 `nsis.payload` 与安装版 `/api/instance` 交叉核对（见第四、六节）。

## 三、构建身份、产物与签名条件

### 3.1 构建方式

- 检出：`D:\qio-dev\s6-lead`，HEAD = f9d250c，构建前后 `git status` 干净（构建期只在工作区出现过
  `Cargo.toml` 的 CRLF/LF 视图差异，内容与提交一致，已恢复并复核）。
- 命令（Windows PowerShell 5.1，脚本既有顺序）：

  ```powershell
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\build_installer.ps1 ^
    -UnsignedTestArtifact -DistDir D:\qio-dev\qs6-dist -TempDir C:\Users\zxy\qio-tmp\s6-build-tmp
  ```

- 顺序：准备内置模型 → 自带 Python 运行时 → 重建后端 sidecar → 编译卸载助手 → 打包前真产物校验
  → 桌面安装包（NSIS）→ 签名/更新清单/构建清单（未签名测试路径不写 .sig/latest.json）。
- 首次尝试失败原因：全新 worktree 未安装前端依赖（node_modules 缺失导致 tauri CLI 不可用）；
  按 `frontend/package-lock.json` 执行 `npm ci`（exit 0）后原样重跑，**未改产品代码或打包配置**。

### 3.2 构建产物

| 项 | 值 |
| --- | --- |
| 安装包 | `QIO_0.3.0_x64-setup-UNSIGNED-TEST.exe` |
| 位置 | `D:\qio-dev\qs6-dist\`（独立产物目录，未入库） |
| 字节数 | 128,950,995 |
| SHA256 | `3c323894e63acf4d4d1baf8eb4dd9edffa06d057b2aba018cb669d55cf3924f4` |
| 构建清单 | `QIO_0.3.0_x64-setup-UNSIGNED-TEST.exe.build.json` |
| 清单 commit / version / signing | f9d250cf… / 0.3.0 / `unsigned-test` |
| updater_artifacts / config_overrides | false / `{"bundle":{"createUpdaterArtifacts":false}}` |
| 内置 sidecar | `qio-backend-x86_64-pc-windows-msvc.exe`，63,015,746 字节，sha256 `cbb3d20851ab6f559a8b18494edf07a50294e736a3adc4996bce71a5e1fe20b1` |
| 内置运行时 | Python 3.11.15，728 个文件（venv/pip 四项自检通过） |
| 内置模型 | bge-small-zh-v1.5 fp32 `model.onnx`（94,851,877 字节，sha256 与清单一致） |
| 卸载助手 | `qio-uninstall-helper.exe`，419,840 字节 |
| `.sig` / `latest.json` | **不存在**（未签名测试产物，按设计不生成） |
| Authenticode | NotSigned（无 Windows 代码签名证书） |

### 3.3 签名条件（本轮缺失，如实记录）

- 更新签名：本机没有 `TAURI_SIGNING_PRIVATE_KEY(_PATH)` 与口令（用户环境变量、当前进程、本机
  安全配置均未提供；仅存在历史公钥/私钥文件但没有可用口令）。**没有尝试猜测或更换信任公钥。**
- Windows 代码签名：当前用户证书存储无代码签名证书，`signtool` 不在 PATH，产物 Authenticode = NotSigned。
- 因此本轮产物是脚本既有 `-UnsignedTestArtifact` 路径产出的 **UNSIGNED-TEST 测试包**：
  无 `.sig`、无 `latest.json`、无证书签名，**不能认定为正式可发布包**。
- 取得条件（不要求用户把私钥/口令贴进聊天）：在构建机安全配置中提供与 `tauri.conf.json` 内嵌公钥成对的
  更新私钥 + 口令（以及可选的代码签名证书）后，按不带 `-UnsignedTestArtifact` 的方式重跑构建即可。

## 四、实际产物发布闸门（release_gate.py）

命令（在构建提交 f9d250c、git status 干净时执行，未修改闸门）：

```powershell
python scripts\release_gate.py --repo D:\qio-dev\s6-lead --dist D:\qio-dev\qs6-dist ^
  --installer D:\qio-dev\qs6-dist\QIO_0.3.0_x64-setup-UNSIGNED-TEST.exe ^
  --no-history --json D:\qio-dev\s6-evidence\gate\gate.json
```

结果：**13 PASS / 2 FAIL / 1 WARN，exit 1**。FAIL 的两项正是未签名测试产物的应然项
（不是构建失败，也不能被解释为可发布）：

| 检查项 | 结果 | 实际原因 |
| --- | --- | --- |
| tauri.conf.json | PASS | version=0.3.0 |
| repo.commit | PASS | f9d250cf（D:\qio-dev\s6-lead） |
| installer | PASS | 123.0 MB |
| installer.sha256 | PASS | 3c323894…24f4 |
| sha256sums | PASS | 与 SHA256SUMS.txt 一致 |
| **latest.json** | **FAIL** | 未签名测试产物按设计不生成更新清单 |
| bundle.config | PASS | 源码配置自洽；实际签名状态由 updater.artifacts 判定 |
| uninstall.contract | PASS | 卸载钩子清理安装信息、保留 DbBaseline、installMode=currentUser |
| nsis.payload | PASS | NSIS 结构、产品名与版本号 0.3.0 |
| **installer.sig** | **FAIL** | 未签名测试产物没有 .sig |
| sidecar | PASS | 60.1 MB，sha256 cbb3d208… |
| sidecar.fresh | PASS | sidecar 与 release 构建后端逐字节一致 |
| models | PASS | bge-small-zh-v1.5 与清单逐一对上 |
| build.manifest | PASS | commit/version/signing/overrides 与构建事实一致 |
| **updater.artifacts** | **WARN** | 产物确实没有更新签名且被显式标成 unsigned-test，不是可发布产物 |

独立交叉核对（非闸门自检）：dist 安装包与 `target/release/bundle/nsis` 原始产物字节/哈希一致；
构建出的 `qio.exe` 含 0.3.0、不含 0.2.10 版本串；签名结构检查与真实信任公钥验签是两件事，
本轮**没有做真实验签**（无签名条件）。

## 五、安装版六项验收（第 1/2/3/6 项）

### 5.1 环境与隔离方法（诚实边界）

- 环境：Windows 11 家庭版 10.0.26200（64 位），交互桌面会话 session 2；用户 `admin\zxy` 为**非管理员**。
- 隔离：本机不能新建隔离 Windows 用户（非管理员、也没有可用虚拟机），因此采用与既有
  `scripts/install_e2e.py` 头注释相同的等价条件：**独立安装目录 + 独立 QIO_DATA_DIR（全新或副本）+
  白名单环境（不继承 PYTHONPATH / VIRTUAL_ENV / QIO_DEV_* 等开发变量）+ TEMP/TMP/LOCALAPPDATA 指向隔离目录**。
  这不是「干净 Windows 用户环境」，结论里不把它说成更强的东西。
- 本机既有正式安装 `D:\QIO`（0.2.10）与用户数据 `D:\QIO-data` 全程**未升级、未卸载、未启动、未写入**：
  收尾逐文件 sha256 核对 `D:\QIO` 的 744 个文件与开工基线一致；安装/卸载注册表项还原为
  `QIO / 0.2.10 / "D:\QIO"`；`D:\QIO-data\app.db` 本体大小与 mtime 未变（仅 WAL/SHM 因只读探测/只读备份被访问）。
- 装置偏差（必须记账，不是产品行为）：`QIO_DISABLE_DB_CHECK=1`（避免测试写用户 HKCU 的数据库身份基线；
  该启动自检路径本轮未覆盖）；TEMP/TMP/LOCALAPPDATA 指到隔离目录（本机沙箱下 PyInstaller onefile 需要可写 TEMP）。

### 5.2 第 1 项 全新安装与首次启动

| 子项 | 结果 | 证据/说明 |
| --- | --- | --- |
| 静默安装到隔离目录（/S /D=…） | **PASS** | exit=0；737 个文件 / 208.4 MB；注册表写入 DisplayVersion=0.3.0、InstallLocation=隔离目录 |
| 内置模型文件与清单 | **PASS** | `model.onnx` 94,851,877 字节，bytes+sha256 与清单逐一对上 |
| 真外壳启动 + sidecar lease | **PASS** | qio.exe 拉起后出现 `sidecar.lease.json`；shell/backend 的 pid+创建时间+exe 与内核枚举逐项一致 |
| 安装目录后端 /api/health | **PASS** | 200；按端口反查回答者路径就是安装目录里的 `qio-backend.exe` |
| 不依赖开发环境 Python/Node（内置运行时） | **PASS** | 内置 `python-runtime\python.exe` 自报 3.11.15；能建 venv；venv 内 pip 24.0 可用；pyvenv.cfg home 指向内置运行时 |
| 内置模型真实加载（非静默回退 BM25） | **PASS** | 后端日志：`onnx embedding backend: loaded bge-small-zh-v1.5（model.onnx，512 dims，identity=onnx:…fp32）`；回退迹象=无 |
| 实际检索路径 | **PASS** | 两轮合成对话，第二轮发给模型的上下文中出现第一轮唯一标记（用户原话不含该标记）；检索由内置模型路径真实命中 |
| 安装版界面可达 对话 / 互动 / 设置 | **PASS** | 真实安装版 WebView2（CDP），URL `http://tauri.localhost/#/`、`#/interactive`、`#/settings`；三页截图见证据 |
| 完成现有首次引导（全流程） | **NOT VERIFIED** | 欢迎页→「开始」、连接模型步骤「跳过」、「认识你」页面渲染均成功；在必填项「称呼」处被产品校验拦下（提示「称呼是完成设置的必填项」），自动填充未成功，未伪造个人资料。属装置/自动填充限制，**不写成产品缺陷** |

结论：**安装与首启主链路 PASS；引导全流程完成 NOT VERIFIED**（页面可达与前三步交互已实测）。

### 5.3 第 2 项 从现有正式版升级及重开

第 2 项使用**真实旧版安装程序**建立合成样本后覆盖升级（全部隔离）：

| 步骤/断言 | 结果 | 证据 |
| --- | --- | --- |
| 旧版安装包指纹 | **PASS** | 使用正式 `QIO_0.2.10_x64-setup.exe`（127,489,828 字节，sha256 137077D8…5491） |
| 0.2.10 静默安装到隔离目录 | **PASS** | exit=0；注册表 DisplayVersion=0.2.10、InstallLocation=隔离目录；`/api/instance` 报 0.2.10 |
| 旧版真实 API 造合成样本 | **PASS** | 合成凭据引用（key_id=s6-upgrade-synthetic，secret 为一次性合成串）、设置 typewriter_cps=75、知识 1 条并激活、板面 1 个、跑通一轮 turn 产生话题+消息 1 条；全部经旧版真实 HTTP API 写入 |
| 升级前快照 | **PASS** | 知识/板面/话题/消息/凭据/设置计数与 id 已记录；`app.db` 只读快照（schema user_version=51） |
| 0.3.0 覆盖安装同一目录 | **PASS** | exit=0；注册表 DisplayVersion=0.3.0；`/api/instance` 报 0.3.0 |
| 话题与消息保留 | **PASS** | 话题 id `topic_568d34ff4117` 一致；消息内容/条数一致 |
| 知识/板面保留 | **PASS** | 知识 id `kn_2dd1922f8429`、板面 id `board_039add2a2865` 一致 |
| 凭据引用保留 | **PASS** | key_id `s6-upgrade-synthetic` 存在，列表只含掩码、不回显密钥 |
| 设置保留 | **PASS** | 旧设置 `typewriter_cps=75` 保留；0.3.0 新增 `close_behavior` 取升级后默认值 `ask`（属新增设置，不是旧版保留项） |
| schema 迁移只追加 | **PASS** | user_version 51→74；旧 schema_version 行集合是新集合的子集（旧 48 / 新 69），历史迁移未被改写 |
| 完全退出后二次重开（幂等） | **PASS** | 第二次启动健康、版本 0.3.0、迁移计数不变（74/69）、知识/板面/消息条数不重复增长 |
| 升级后卸载默认保留数据 | **PASS** | 静默卸载后安装目录消失、`data\app.db` 仍在 |
| 既有正式安装不受影响 | **PASS** | `D:\QIO` 744 文件 sha256 与基线一致；`D:\QIO-data` 顶层元数据未变；注册表最后还原为 0.2.10 / `D:\QIO` |
| 实体保留 | **SKIP** | 0.2.10 样本中没有实体，无可保留对象 |

一个 WARN 是测试装置问题，不是产品：清理合成凭据 keyring 条目时旧版 API 返回 409 Conflict（该合成凭据由旧版创建，新版按幂等规则拒绝重复删除），条目只存在于测试 keyring，未影响数据保留断言。


### 5.4 第 3 项 原生窗口与进程

| 子项 | 结果 | 证据/说明 |
| --- | --- | --- |
| 关闭行为设为托盘后 WM_CLOSE | **PASS** | 窗口已隐藏=True；壳与 sidecar 存活；lease 保留；未触发退出收尾 |
| 托盘恢复（show + restore） | **PASS** | 隐藏后恢复可见；壳存活 |
| 关闭行为设为直接退出后 WM_CLOSE | **PASS** | 外壳**自然退出 exit=0**（未 taskkill） |
| 退出后后台进程 | **PASS** | 按路径枚举安装目录内后端进程全部消失；无孤儿 |
| 退出后归属记录 | **PASS** | `sidecar.lease.json` 已删除；无 lease 临时文件残留 |
| 安装目录内无残留活动进程 | **PASS** | 独立枚举为空 |
| 真正退出后重启（外壳级） | **NOT VERIFIED** | native-install 的 shell 级重启脚本存在端口发现 bug（已留档），未作为产品结论；同数据目录的**后端级**二次启动幂等已在 5.3 验证 PASS |
| 不影响既有安装 | **PASS（单实例+静态证据）** | 本机既有 `D:\QIO` 744 文件 sha256 全程一致；静默安装的「诱饵卸载器」实验证明安装器不会执行既有安装的卸载器；卸载器所有权守卫按 lease+进程三要素只收本实例（源码与既有测试）；F 阶段未观察到对既有安装/另一份安装的改动 |
| 两份 0.3.0 安装并存（coinstall 双壳） | **NOT VERIFIED** | 双实例驱动下两份外壳都没有写出 lease、未正常起来；未定位到产品因果（装置脚本对双实例启动的环境/hook 不确定），不冒充通过；wrapper 已还原注册表并核对 `D:\QIO` 744 文件未变 |
| 关闭询问（默认 ask）实际弹窗交互 | **NOT VERIFIED** | 设置页真实显示「每次询问 / 最小化到托盘 / 直接退出」；本轮无人值守脚本通过 API 切到 tray/quit 后验证，ask 确认框的点击交互未单独验证 |

### 5.5 第 6 项 卸载与数据选择

| 子项 | 结果 | 证据/说明 |
| --- | --- | --- |
| 隔离样本上静默卸载（默认行为） | **PASS** | 安装目录被清理（等待目录消失/稳定）；注册表安装信息清理并还原测试前状态 |
| 默认保留用户数据 | **PASS** | 卸载后 `QIO_DATA_DIR\app.db` 仍在、可读；升级样本中的数据未被删除 |
| 不影响另一份安装（本机正式版） | **PASS** | `D:\QIO` 744 文件 sha256 全程一致；卸载器所有权守卫按 lease + 进程三要素只收本实例进程（静默安装「诱饵卸载器」实验证明不会执行既有安装的卸载器） |
| 用户数据保留/删除**选择**（勾选「删除应用数据」） | **NOT VERIFIED** | 静默模式跳过勾选页；本机无可用虚拟机/隔离账号，破坏性验证会删除真实用户 `%APPDATA%\com.qio.app` / `%LOCALAPPDATA%\com.qio.app`（WebView 数据），不做。静态证据见 `install\upgrade\evidence\uninstall-*.txt` 与 `frontend/src-tauri/nsis/installer-hooks.nsh`：模板复选框删除的是应用数据目录，QIO 后端数据目录（`QIO_DATA_DIR`）不在该分支清理范围内；不借验收重新设计产品行为 |
| 在用户日常数据上试验卸载 | **未执行（禁止）** | 本轮未对 `D:\QIO` / `D:\QIO-data` 做任何卸载或删除 |


## 六、真实模型关键操作（第 4 项）

> 验收对象：本轮安装包安装目录 `fresh2\install` 的 `qio.exe`（sha256 0AB49228…D237）拉起的
> `qio-backend.exe`（sha256 CBB3D208…20B1，与 build manifest 一致）；隔离数据目录
> `s6-work\model-data\run\data`（真实 0.2.10 副本清空个人内容、保留 2 条真实凭据引用，首次启动真实迁移
> schema 51→74）。**不是开发页**；界面证据来自安装版 WebView2（CDP 9222，页面 tauri.localhost）。

### 6.1 真实厂商（用户已配置、仅少量调用）：被真实账户/服务状态阻塞

| 服务 | 实际结果 | 判定 |
| --- | --- | --- |
| Command Code（`https://api.commandcode.ai/provider/v1`，模型 `deepseek/deepseek-v4.1-flash`） | 真实 POST /api/turns → TURN_END failed / provider_error；厂商返回 **401 Invalid Authorization header or token** | 真实成功路径 **NOT VERIFIED** |
| DeepSeek（`https://api.deepseek.com/v1`） | 认证通过；`/api/models/discover` 返回 3 个模型（`deepseek-flash` 声明 capabilities=[text, vision]、`deepseek-v4-pro`、`deepseek-v4-flash`）；真实调用 → TURN_END failed / internal_error，厂商返回 **402 Insufficient Balance** | 真实成功路径 **NOT VERIFIED** |

- 这两条是**用户账户/服务当前状态**（401 鉴权失败、402 余额不足），不是 QIO 代码路径的失败；
  本轮因此不能声明任何「真实厂商成功/返回质量」结论。QIO 侧行为被如实记录：凭据不能读回原文、
  错误进入 TURN_END、界面/事件保留原始信息。
- 真实观察（不改代码）：402（Insufficient Balance）被产品归类为 `internal_error`（异常类名不在
  provider 错误家族白名单内）。这是现有分类口径事实，是否为缺陷留给发布决策。

### 6.2 同 Key 不同模型与三种后台用途的实际归属（受控假厂商，逐请求模型级证据）：PASS

- 同一 Key 配置 4 个模型；`fragment_max_turns=1` 触发三种后台派生。
- 实际请求归属：主对话/通用对话 = `fake-model`；记忆整理 = `fake-model-pro`；知识提炼 = `fake-model-ultra`；
  实体提炼 = `fake-model-bg`；`derived_tasks`（summary/knowledge/entities）全部 completed；
  `fragments.summary_model=fake-model-pro`；用量增量只落在该 Key。
- **真实 Key 上的 4 个模型用途分配被后端接受**（配置层事实）；因 402 无成功调用，真实归属 **NOT VERIFIED**。

### 6.3 普通对话关键操作（受控 SSE / 受控 500，仅证明 QIO 链路）

| 操作 | 结果 | 证据 |
| --- | --- | --- |
| 流式输出 | **PASS** | 4 个 ASSISTANT 流式累计快照；首个正文增量比 TURN_END 早 2.101 秒；TURN_END completed |
| 停止 | **PASS** | 首正文后取消 → TURN_END status=cancelled / reason_code=user_stopped / stopped_by=user |
| 失败后重试 | **PASS** | 厂商 500 → failed/provider_error；同文带 `retry_of_turn_id` 重试 → completed；假厂商请求台账对应 |

### 6.4 互动提交、确认、执行、恢复及结果定位（受控假厂商）

| 子项 | 结果 | 证据 |
| --- | --- | --- |
| 提交→提出意图→确认→执行→结果定位 | **PASS** | 提交后 `board_propose_intents`、确认后续作 `board_create_card`+`board_finish_intent`，意图 completed；`/results` 1 条；ledger applied=1 |
| 执行记录无需切页更新 | **PASS（API/数据事实）** | 执行记录事件/数据在同话题可读；界面自动出现属第五轮修复，本轮未重复做切页 UI 断言 |
| 恢复路径（partial→recover） | **NOT VERIFIED** | 时限内未取得可信的 completed(version=2) 证据，不冒充通过 |
| 普通消息区无内部绑定文字 | **PASS** | 提交轮 user 消息 `content_type=board_submission`；前端 `MessageItem.vue` 对 board_submission 整条 `v-if` 不渲染；最终 CDP 检查所有页面 `has_submission_binding=false` |

### 6.5 看图与附件

| 子项 | 结果 | 说明 |
| --- | --- | --- |
| 支持看图的真实模型读图 | **NOT VERIFIED** | 真实服务 401/402；DeepSeek 只声明能力，未取得成功视觉调用 |
| 附件小样本读取 | **PASS（受控）** | 小 txt 上传 ready、sha256 与样本一致、read_attachment 返回正文含唯一 marker |
| 附件失败恢复 | **PASS（受控）** | 损坏 PNG 不崩溃、如实给元数据并标「像素尺寸未能识别（格式变体或文件损坏）」、不编造内容；正确 PNG 明确「没有视觉能力时不声称看懂」 |

### 6.6 边界

- 真实厂商返回质量、限流/超时/网络行为：**未验证**（本轮无成功真实调用）。
- 真实看图、互动恢复、首次引导全流程：**未验证**（原因分别见上）。
- 受控结果只证明 QIO 自己的链路，不代表任何真实厂商兼容性。

## 七、更新与失败恢复（第 5 项）

### 7.1 签名条件与正式发布边界

- 本机无更新签名私钥口令、无 Windows 代码签名证书 → 本轮是 UNSIGNED-TEST 测试包，
  **没有做真实信任公钥验签，也没有 Windows 证书签名**；这两项记 **SKIP**（原因：条件不具备，禁止伪造）。
- 未修改生产更新源：4 个 endpoints 与内嵌 pubkey 相对 v0.2.10 完全相同；未创建 Release、未上传资产。

### 7.2 安装版窗口级失败恢复（PASS）

在隔离实例（安装目录副本 + 数据副本 + 独立 QIO_DATA_DIR）上，用**进程级** `HTTPS_PROXY` 指向本地
只回 502 的测试代理（不转发、不接触生产更新源），由 CDP 驱动安装版界面真实点击「检查更新」：

| 子项 | 结果 | 证据 |
| --- | --- | --- |
| 检查更新失败可见、不伪装「已是最新」 | **PASS** | 界面显示 `更新失败（原始信息：error sending request for url (https://github.com/…/latest.json)）`，`upToDate=false` |
| 失败后应用仍可用 | **PASS** | 回到对话页，输入框在、正文正常 |
| 关闭后不带代理重启 | **PASS** | 重启窗口 529ms 出现；显示「当前版本 0.3.0」；对话页正常 |
| 正常网络对照 | **PASS** | 显示「已是最新版本（检查于 23:42:06）」；随后真正退出，后端树 verified、lease 删除、无残留 |
| 下载失败 / 被篡改 / 不可信产物被拒绝后旧版仍可启动 | **NOT VERIFIED** | 需要「有可用更新且签名可校验」的环境；不能改生产更新源、不能伪造签名，本轮无法构造 |
| 真实插件对未签名包的拒绝安装 | **NOT VERIFIED** | 同上，无法在不伪造包的前提下构造「有更新」场景 |
| 生产更新源 / 下载链接 / 发布资产检查 | **留给正式发布阶段** | 本轮未发布、未上传，不能写成已通过 |

## 八、必要修复与定向复验

- **产品代码**：本轮未发现需要在安装包验收中修复的产品阻塞；因此没有新增产品代码提交，构建 SHA 保持
  `f9d250cf`，也没有「旧包结果冒充新包」的问题。
- **构建/装置问题（不改产品）**：
  1. 首次构建失败原因：全新 worktree 未安装前端依赖（node_modules 缺失 → tauri CLI 不可用）；按 lockfile
     执行 `npm ci` 后重跑成功，未改产品代码或打包配置。
  2. `scripts/install_e2e.py` 首次运行时因上一阶段残留的 standalone 后端占用端口，导致 A-115 误判回答者、
     close/tray/quit 断言连锁失败（脚本自己在 2026-10-22 记录过同类装置问题）。用干净单实例复跑后
     A-116a/b、A-117a/b、A-116/117/118/119 全部 PASS；失败项不重复计入产品结论。
  3. `scripts/verify_install_desktop.py` 收尾只还原 Uninstall 键、把 `Software\qio\QIO` 默认值留成测试目录；
     native-install 已按跑前备份还原并与基线逐值一致。属装置脚本问题，非产品。
  4. native-install 自定义 close harness 的端口发现 bug（从 lease 猜端口）导致连锁 FAIL，已作废、不计数。
  5. 多实例并存驱动（`install_e2e_multi.py` 包装）下两份外壳未写出 lease、未正常起来；未定位到产品因果，
     记 **NOT VERIFIED**，不改产品、不为保绿改断言；wrapper 已还原注册表并核对 `D:\QIO` 744 文件未变。
- **定向复验**：以上装置问题修复后只复跑直接受影响阶段（干净单实例安装/首启/窗口、更新失败窗口级），
  未重跑前五轮验收、未跑全量测试、未触发 CI。


## 九、未验证事项、待用户条件与正式发布后检查

### 9.1 本轮未验证（如实保留，不写成通过）

1. 真实厂商成功路径与返回质量（Command Code 401 / DeepSeek 402，真实账户或服务状态；无成功真实调用）。
2. 真实看图（无成功真实视觉调用；受控看图本轮未执行）。
3. 互动恢复路径（partial → recover → completed(version=2)）。
4. 首次引导全流程完成（页面可达、前三步可操作；必填项「称呼」未被自动驱动填入）。
5. 更新签名验签、Windows 代码签名 / SmartScreen（无私钥口令、无证书；禁止伪造）。
6. 更新下载/校验/安装失败恢复、未签名包被拒绝安装（无法构造有效签名可更新包）。
7. 关闭询问（默认 ask）的弹窗点击交互（tray/quit 已验证）。
8. 卸载时勾选「删除应用数据」的选择（静默模式跳过勾选页；破坏性验证会删真实用户 WebView 数据，不做）。
9. 全量测试、完整 CI、正式签名构建（本轮只做定向验证与安装版验收）。
10. 「干净 Windows 测试账号或虚拟机」：本机非管理员且没有可用虚拟机，采用独立安装目录 + 独立
    QIO_DATA_DIR + 白名单环境 + 隔离 TEMP 的等价隔离；不是干净用户环境。

### 9.2 需要用户提供的具体条件（不要求在聊天里贴密钥）

- **正式可发布包**：与 `frontend/src-tauri/tauri.conf.json` 内嵌更新公钥成对的**更新签名私钥 + 口令**
  （放到构建机安全配置/环境变量即可，不要写进代码或文档）；如公开分发，另需 **Windows 代码签名证书**。
  具备后按不带 `-UnsignedTestArtifact` 的方式重跑 `scripts/build_installer.ps1` 并重跑发布闸门。
- **真实厂商验证**：Command Code 的可用凭据（当前 401）、DeepSeek 账户可用余额（当前 402）；
  或指定另一家已配置且可用的服务。只需授权少量合成调用。
- **卸载破坏性验证（可选）**：如确需验证「勾选删除应用数据」分支，需要一个隔离 Windows 账号/虚拟机，
  或用户明确批准在本机删除 `%APPDATA%\com.qio.app` / `%LOCALAPPDATA%\com.qio.app`（WebView 数据）。

### 9.3 正式发布阶段才需要检查（本轮不做、不写成已通过）

- 生产更新源（endpoints 保持原样）在签名包发布后的真实更新链路、下载链接与发布资产完整性。
- `publish_release.ps1`、v0.3.0 标签、GitHub Release、正式发布资产上传。
- 签名构建产物的 `.sig` / `latest.json` 与真实信任公钥验签、升级保留。
- 合并 main 前的完整 CI、全量测试与正式签名包安装验收。

## 十、提交、交付与证据位置

- 版本同步提交：`f9d250cfecee4aef3f0703ced16ba331bbce844d`（0.3.0，10 文件 +11/−11）
- 构建代码 SHA：`f9d250cfecee4aef3f0703ced16ba331bbce844d`（本轮安装包由该提交的干净检出构建）
- 实际受测安装包：`D:\qio-dev\qs6-dist\QIO_0.3.0_x64-setup-UNSIGNED-TEST.exe`
  （SHA256 `3c323894e63acf4d4d1baf8eb4dd9edffa06d057b2aba018cb669d55cf3924f4`，128,950,995 字节）
- 交付 HEAD：本文件所在文档提交（完整 SHA 与推送回执见交付回复；本提交只改文档，不改变产品树）
- 分支：`fix/release-step6-native-validation-20261029`（仅推送该分支；未合并/推送 main、未打标签）
- 全部新增提交带 `[skip ci]`；未触发 CI、未执行 publish_release.ps1、未创建 GitHub Release、未上传发布资产。

证据位置（大产物不入库）：

| 内容 | 位置 |
| --- | --- |
| 安装包 + build manifest + SHA256SUMS | `D:\qio-dev\qs6-dist\` |
| 发布闸门 JSON / 逐项输出 / 产物独立核对 / 基线 | `D:\qio-dev\s6-evidence\gate\` |
| 安装/首启/窗口/升级/卸载证据 | `D:\qio-dev\s6-evidence\install\` |
| 真实模型与受控服务证据、安装版界面截图 | `D:\qio-dev\s6-evidence\model\` |
| 构建记录（命令、SHA、清单） | `D:\qio-dev\s6-evidence\build\build-record.md` |
| 文档内截图（节选） | `docs/verification-shots-step6/` |

## 十一、三项分开判断（不合并成一个 PASS）

| 判断 | 结论 | 依据 |
| --- | --- | --- |
| **安装功能是否通过** | **有条件通过**：安装/首启主链路、内置运行时与模型真实加载、检索、托盘/退出、0.2.10→0.3.0 升级与数据保留、默认卸载保留数据均 PASS；首次引导全流程、ask 弹窗点击、外壳级重启、双实例并存、卸载显式删除选择 **未验证**（均写明原因） | 第五节、第六节、第七节 |
| **签名及更新是否通过** | **未通过（本产物不可发布）**：无更新签名、无 Windows 证书签名，闸门 `latest.json` / `installer.sig` FAIL、`updater.artifacts` WARN；真实验签 **SKIP**。安装版「检查更新失败可见、失败后可用、重启正常」PASS；下载/篡改拒绝与生产更新源/资产 **未验证** | 第三节、第四节、第七节 |
| **是否满足进入正式合并发布阶段** | **不满足**。需先：①取得更新签名私钥+口令（如需公开分发再加代码签名证书），重建带签名产物并让闸门 0 FAIL；②真实厂商成功路径在有效凭据/余额下复验；③补齐或明确接受剩余未验证项（首次引导全流程、ask 交互、外壳级重启、双实例并存、显式删除选择）；④按维护者口径决定是否要求全量测试/CI；⑤正式发布阶段检查生产更新源、下载链接与发布资产 | 第九节 |

本轮只完成了**候选测试包**的构建与可验收范围内的验证：未合并 main、未打标签、未发布、未执行 publish_release.ps1、
未创建 GitHub Release、未上传正式发布资产、未修改生产更新源。


