# I2V2F · D 组验收设施

本目录只服务于互动页 v2 前端修复的**独立验收**，不是产品代码、不参与打包。由 D 组（agent-d）维护，
阶段 1 准备、阶段 2 在集成分支上运行。

## 文件

| 文件 | 作用 |
| --- | --- |
| `seed_env.py` | 通过 HTTP API 生成受控板面（8 卡 / 2 组 / 2 关系 / 4 演示意图，其中 1 个 running 且依赖材料）。不直接改 SQLite；幂等。 |
| `playwright_flows.mjs` | 真实页面连续验收：10 条用户流程，逐项记录 期望 / 实际 / PASS|FAIL / SHA，截图输出到证据目录。 |
| `run.ps1` | 一次跑完后端种子 + 10 条流程的薄封装。 |

## 环境（阶段 2，Lead 指定）

- 后端：`http://127.0.0.1:8744`（隔离数据域 `D:\qio-dev\.i2v2-data`，**不要重启它**）
- 前端：Lead 的 Vite `http://127.0.0.1:5301`；D 组独立前端固定用 **5303**（不要占用 5300/5301）：
  ```powershell
  cd D:\qio-dev\i2v2-d\frontend
  $env:VITE_QIO_BACKEND_URL = "http://127.0.0.1:8744"
  npx vite --port 5303 --strictPort --host 127.0.0.1
  ```
- Playwright：`C:\Users\zxy\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\node_modules\playwright`（脚本内绝对路径 import）
- Chrome：`C:\Program Files\Google\Chrome\Application\chrome.exe`

## 运行

```powershell
# 1) 种子（可重复执行；只改自己的验收板面）
$env:QIO_ACCEPT_API = "http://127.0.0.1:8744"
python scripts/i2v2-verify/seed_env.py --board board_d_accept

# 2) 真实页面 10 条流程（默认连 Lead 的 5301 + board_default）
$env:QIO_ACCEPT_UI = "http://127.0.0.1:5301"
$env:QIO_ACCEPT_API = "http://127.0.0.1:8744"
$env:QIO_ACCEPT_BOARD = "board_default"
node scripts/i2v2-verify/playwright_flows.mjs
# 只跑某几条：node scripts/i2v2-verify/playwright_flows.mjs 1 6 9
```

输出：`D:\qio-dev\_i2v2-evidence\D-shots\flows.json` / `flows.md` + 每步截图。

## 覆盖的 10 条流程（与 Lead 对齐的用户原文）

1. 修改运行任务的材料 → 等待影响确认 → 确认/取消 → 过期或保存失败的恢复
2. 提交 → 延迟理解 → 新意图出现 → 确认 → 状态更新 → 正式成果出现（无需刷新）
3. 读取失败保留旧任务 → 明确提示 → 重试恢复；旧响应不覆盖新响应
4. 任务与批量入口真实打开；按真实状态暂停/取消/恢复，按钮与结果一致
5. 一条卡片草稿冲突时恢复入口仍可见可打开可解决（对话收起同样可用）
6. 重新连接已有关系不改含义/方向/曲线；取消编辑不产生修改
7. 菜单移出组成员：不重叠、顺序与组框正确、撤销/重做完整
8. 搜索/书签定位已收起组内成员：目标实际显示并高亮
9. 50% / 100% / 200% 缩放及平移后拖关系胶囊；正常提交 / Esc / 中断 / 失焦
10. 意图位置保存成功/失败/取消/连续拖动/乱序；重新进入后一致

## 说明

- 选择器校准点集中在 `playwright_flows.mjs` 的 SELECTORS 语义（data-im / data-iw-chrome 等）与
  `targets()` 推导里；集成后允许只改脚本校准，不允许把行为断言放宽成「组件存在」。
- 流程 2（延迟理解）与流程 5（真实草稿冲突）依赖后端模型/草稿状态；若当前环境没有 fake provider，
  脚本会如实记录「未能验证」而不是伪造通过。
