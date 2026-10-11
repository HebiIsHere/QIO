# scripts/interactive-real-model-verify（子智能体 E 的定向验收工具）

只给「真实模型理解与真实执行」这一轮做独立验收用，**不属产品代码**。

## 文件

| 文件 | 说明 |
| --- | --- |
| `run_verify.py` | 主验收脚本：临时库 + 真实 uvicorn + 真实 HTTP + fake provider；56 项检查 |
| `probe_api.py` | 诊断探针：打印板面 / 意图 / 草稿 / 理解 / 执行的载荷形状（排错用） |
| `out/` | 每次运行的明细 JSON（`verify-<时间戳>.json`） |

## 口径

- **不联网、不用真实密钥**；模型调用一律由 `FakeUnderstandingAdapter` 顶替。
- 只打真实 HTTP，不直接调内部函数伪造结果（少数控制类场景用模块层构造确定性时序，
  并在检查详情里注明；HTTP 路由就是转发到同一批函数）。
- HTTP 客户端固定用 `http.client` + 显式连接：实测本环境 `urllib` 会偶发 ECONNRESET
  （服务端无异常、换 http.client 后同一条路由连续 5 次全部 200），那是 harness 问题，不是产品缺陷。
- 三态：`PASS` / `FAIL` / `NOT_VERIFIABLE`。**`NOT_VERIFIABLE` 不等于通过**。
- 有 `FAIL` 时退出码 1。

## 用法

```powershell
$env:UV_CACHE_DIR='<你的 uv 缓存目录>'
cd <仓库根>\backend
uv sync --frozen --extra dev
uv run --frozen python ..\scripts\interactive-real-model-verify\run_verify.py --list
uv run --frozen python ..\scripts\interactive-real-model-verify\run_verify.py --check B1,C1,D1,X1
uv run --frozen python ..\scripts\interactive-real-model-verify\run_verify.py --group materials
```

## 检查分组

| 组 | 覆盖 |
| --- | --- |
| `real-path` | P1–P10：提交→受理→材料→真实理解→意图→审批→真实执行→结果卡与来源引用；越权输出与非法 JSON |
| `no-model` | C1–C5：空提交 / 重复提交 / 纯位置变化 / 保存 / 聊天发送都不得调用模型 |
| `intent` | D1–D7：澄清 / 无工作 / 拒绝 / 材料变化 / 重复批准 / 依赖 / 冲突 |
| `recovery` | E1–E8：幂等受理 / 刷新 / 快照重试 / 撤回 / 迟到结果 / 重启降级 / 恢复不重放 |
| `contract` | X1–X8：§14 冻结字段、新接口、允许操作集合、迁移 37/38 |
| `materials` | F1–F7：材料来源诚实性（含图片必须如实说不支持） |
| `regression` | R1–R9：对话 / 流式 / 附件 / 话题 / 草稿 / 确认入口 / 演示隔离 |
| `visual` | V1–V6：窄窗口与主题（需要截图，脚本本身只登记） |

材料组（F）目前是**带证据占位**：等 `agent/interactive/materials.py` 落地后，由脚本用 fake
附件 / fake 抓取器把 F1–F6 改成真断言；在此之前它们只报 `NOT_VERIFIABLE`。
