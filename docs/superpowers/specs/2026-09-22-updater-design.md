# 应用内联网更新（Updater）设计

> 状态：待评审。日期：2026-09-22。上游决定：更新源走 **A 方案 = GitHub Releases**。

## 1. 目标与边界

用户在应用内点一个按钮，完成「检查 → 下载 → 校验 → 安装 → 重启」，不需要打开浏览器。

这等于新增一条「远端代码进入本机」的路径，所以边界先写清楚：

- 不静默更新：可以静默**检查**，但下载与安装必须用户点击；
- 信任根是签名密钥：下载到的安装包必须通过 minisign 校验才会被安装；
- 私钥不进仓库、不进构建产物，由拥有者保存在本机；
- 不提供「关闭校验」的开关 —— 能关掉校验的更新器等于没有更新器。

## 2. 现状事实（2026-09-22 核对）

| 事实 | 影响 |
| --- | --- |
| Tauri 2（tauri-cli 2.11.4）+ NSIS 安装包 | 用官方 tauri-plugin-updater / tauri-plugin-process，不自造下载器 |
| tauri signer generate / sign 可用 | 签名与校验走官方工具链 |
| 仓库公开（匿名 git ls-remote 成功） | GitHub Releases 可作更新源，客户端不需要 token |
| gh CLI 已登录（HebiIsHere，repo scope） | 发布可脚本化 |
| tauri.conf.json 无 plugins 段 | 需要新增 updater 配置与插件注册 |
| CSP 仅放行 self + localhost | **不放宽**：更新请求在 Rust 侧，不进 CSP |
| 安装包无 Authenticode 签名 | 更新安装时 Windows 仍可能弹「已保护你的电脑」 |
| 现有发布物 dist/QIO_*.exe + SHA256SUMS.txt | 继续维护这一份，另外多产 latest.json |

## 3. 用户可见行为（应用内一键更新）

两处入口，同一个状态机：

1. 设置 → 数据与维护 → 更新：显示当前版本 + 「检查更新」按钮（常驻、随时可点）；
2. 启动后静默检查发现新版本时：设置入口（⚙）亮一个小圆点 + 一条不打断的提示（不弹模态、不抢焦点）。

状态与按钮可用性一一对应，不允许「看着能点其实没反应」：

| 状态 | 显示 | 可点动作 |
| --- | --- | --- |
| idle | 当前版本 + 「检查更新」 | 检查更新 |
| checking | 正在检查… | 无 |
| up-to-date | 已是最新版本（含检查时间） | 再次检查 |
| available | 发现新版本 + 说明摘要 | 下载并安装 / 稍后 |
| downloading | 进度百分比 + 已下载/总量 | 取消 |
| ready | 已下载，重启后生效 | 立即重启 / 稍后 |
| failed | 失败原因（网络 / 校验 / 安装器） | 重试 |

设计约束：默认不自动下载；启动后 10 秒检查一次、之后每 24 小时一次（设置里可关）；
断网或 GitHub 不可达只影响更新功能本身；校验失败明确告知并放弃安装，不重试安装。

## 4. 信任模型

- 用 tauri signer generate 生成 minisign 密钥对（**由你执行，我不代生成、不保存私钥**）；
- 公钥写进 tauri.conf.json 的 plugins.updater.pubkey（可提交，且必须提交）；
- 私钥与口令只存在于构建环境，或由你从密码管理器临时注入；
- 更新源是 HTTPS 的 GitHub Releases；下载后由 updater 校验签名，失败即拒绝安装；
- 诚实声明（写进文档与发布说明）：私钥泄露 = 所有已安装的 QIO 都能被投毒。
  这是自更新机制的固有代价，不是可以绕开的实现细节。

## 5. 运行时设计

依赖：tauri-plugin-updater + @tauri-apps/plugin-updater；tauri-plugin-process + @tauri-apps/plugin-process。

配置（frontend/src-tauri/tauri.conf.json）：

```json
{
  "bundle": { "createUpdaterArtifacts": true },
  "plugins": {
    "updater": {
      "endpoints": [
        "https://github.com/HebiIsHere/QIO/releases/latest/download/latest.json"
      ],
      "pubkey": "<tauri signer generate 产出的公钥>"
    }
  }
}
```

权限（frontend/src-tauri/capabilities/default.json）：新增 updater:default 与 process:allow-restart。

前端：新增 frontend/src/services/updater.ts（唯一调用插件的地方，便于测试与替换）+
stores/updater.ts（状态机）+ 设置页「数据与维护」的更新区块；组件只渲染状态。
relaunch() 只在用户点「立即重启」时调用。

错误分类必须分开：网络不可达 / 无新版本 / 校验失败 / 安装器启动失败。
「检查失败」不得显示成「已是最新」。

## 6. 发布侧设计

1. scripts/build_installer.ps1 增加第 4 步「签名 + 生成更新清单」：
   用 tauri signer sign 生成 exe 的 .sig；生成 latest.json
   （version / pub_date / platforms["windows-x86_64"] = { signature, url }）；
   产物落 dist/：exe、exe.sig、latest.json，并更新 dist/SHA256SUMS.txt。
   **私钥缺失时构建失败**，不产出没有签名的更新包。
2. 新增 scripts/publish_release.ps1：gh release create vX.Y.Z --notes-file docs/releases/vX.Y.Z.md，
   上传 exe / exe.sig / latest.json 三个资产，完成后打印更新源 URL 与 latest.json 内容。
3. docs/SETUP.md 补一节「发一版更新」：改版本号 → 构建 → 签名 → 上传的完整顺序。

## 7. 一次性迁移（必须写进发布说明）

当前已发布的 0.1.2 里没有任何更新器代码，所以：

- 0.1.2 → 0.1.3 这一次必须手动安装（下载 dist/QIO_0.1.3_x64-setup.exe）；
- 从 0.1.3 起，之后每一版都能在应用内一键更新；
- 这条限制写进 v0.1.3 发布说明，避免用户以为「点了按钮没反应」。

## 8. 非目标

不做静默自动安装；不做差分更新；不做 beta/stable 多通道；不放宽 CSP；
不引入 Authenticode 代码签名（另议，与更新签名是两件事）；不做回滚。

## 9. 测试与验证

自动化：updater 服务的纯函数（版本比较、状态机迁移、错误映射）用 vitest 覆盖；
store 覆盖 idle → checking → available → downloading → ready 与 failed 两条路径；
组件覆盖「按钮文案与可用性随状态变化」「检查失败不显示成已是最新」；后端跑全量回归确认无副作用。

手动（真机，不能只看单测）：本地假更新源指向高版本安装包，跑通发现 → 下载 → 安装 → 重启后版本变化；
签名改坏必须拒绝；断网给出网络不可达；关掉自动检查后启动不再请求更新源；最后用真实 GitHub Release 端到端跑一遍。

## 10. 风险

| 风险 | 处理 |
| --- | --- |
| 私钥泄露 → 全量投毒 | 私钥不进仓库/产物；文档明确它是信任根 |
| GitHub 不可达 | 失败文案明确、可重试；不阻塞应用启动 |
| 安装期间应用退出 | 明确告知「安装期间应用会重启」，安装模式用 updater 默认 |
| 更新包约 107MB | 显示进度与总量；不做差分（非目标） |
| 首次迁移必须手动装 | 写进 0.1.3 发布说明（第 7 节） |
