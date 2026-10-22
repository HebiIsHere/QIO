# W5 真实 GUI 证据（R9 §11）

本目录由 `_lead-logs/w5_gui_ask_evidence.py`（临时探针，未入库）在 **custom-protocol 调试产物**
（Tauri 壳 `frontend/src-tauri/target/debug/qio.exe` + 真实 Python 后端 + 真实 WebView2）上生成。

## 已验证（真实 GUI）
| 文件 | 内容 |
| --- | --- |
| `01-before-close.png` | 关闭前的真实窗口（标题条 + 内容区） |
| `02-ask-dialog.png` | 发送 WM_CLOSE（等价于点 X / Alt+F4）后出现的 ask 确认框：`最小化到托盘 / 退出 QIO / 取消` + `记住我的选择` |
| `03-ask-cancelled.png` | 点「取消」后确认框关闭、应用仍在运行（窗口可见、壳进程存活） |
| `ask-dialog.log` | CDP 读取到的 DOM/点击结果、壳日志中的「收到主窗口关闭请求」、以及本机环境限制的 console 证据 |

## 本机环境限制（不是产品结论）
本执行环境里 WebView2 的 Tauri IPC custom-protocol fetch 失败：

```
IPC custom protocol failed, Tauri will now use the postMessage interface instead
TypeError: Failed to fetch at Object.sendIpcMessage
```

后果：`qio_backend_info` 等应用命令 invoke 永不 resolve（CDP 实测挂起），前端停在
「正在启动 QIO 后端…」，因此：

- 事件监听/送达**正常**（ask 框能弹出、raw 事件监听能收到 `qio://close-requested`）；
- 但读不到 `ui.close_behavior`、无法调用窗口 hide/quit 命令 → tray/quit/记住选择
  这三条**无法在本机做真实 GUI 验证**（已尝试 `--no-proxy-server`、真实/重定向 LOCALAPPDATA）。

安装版（release + 正常 WebView2）必须由 Lead 最终补验（W5 报告 §5.2/§7 已列步骤）。
