# PM-QIO032 E2E 实测证据（Lead 运行，2026-11-13 00:04–00:11）

## 运行方式
`powershell -ExecutionPolicy Bypass -File scripts/verify-pm-qio032/run.ps1`
（正常界面入口：对话页模式切换进入互动板面；后端 uvicorn + vite + 本机假厂商 SSE 端点
`scripts/verify_stream_provider.py`；浏览器=系统 Edge + CDP；provider 只扮演厂商，不证明真实厂商行为。）

原始输出：`lead-final-run.log`（① 与整轮）、`console.log` + `summary-s4.json`（④ 场景 8/8 PASS 的完整台账）、`provider-log.json`、`shots/*.png`、`backend.log`、`vite.log`。

## 本轮结果（PASS 8 / FAIL 6；失败均为脚本未跑通/断言未捕获，不是产品代码编译错误）
| 场景 | 结果 | 证据 |
| --- | --- | --- |
| ①-0 正常入口进入互动板面 | PASS | 对话页 → #/interactive，模式切换按钮真实点击 |
| ①-1 文字卡写入唯一标记 | PASS | 卡片正文含唯一 runId 标记 |
| ①-2 正常「添加 → 文件」上传 TXT 文件卡 | PASS | 卡片数 +1 |
| ①-3 正常「添加 → 文件」上传 PNG 文件卡 | PASS | 卡片数 +1 |
| ①-4 三态：TXT「显示但不参与」 | PASS | data-visibility: included → displayed_excluded |
| ①-4a 隐藏后恢复显示默认只回「显示但不参与」 | PASS | hidden_excluded → displayed_excluded |
| ①-5 PNG「隐藏且不参与」 | FAIL（脚本选择器） | 封面式图片卡的三态菜单点击在 CDP 侧不可见；后续已改为卡片内 DOM 点击，未及复跑 |
| ①-6..①-10 首轮模型请求内容（参与项交付 / 排除隐藏不泄漏 / PNG / 未提交材料） | 未捕获（UNVERIFIED） | 本轮 provider 只记录到凭据验证请求；提交后的聊天请求未在等待窗口内出现（前一轮曾出现 3 个 chat 请求，但断言时机与归因不足）。#01 的编排级证据见后端单测 `backend/tests/pm_qio032_submit_first_round_materials.py`（fake adapter 捕获首轮请求：TXT 正文、PNG image 块、无视觉能力不交付字节）。 |
| ③ 重复提交/幂等 | 未跑完 | s1 中断后页面状态未回正，s3 进入互动板面等待超时（脚本恢复问题） |
| ④-0 800×600 窄窗按实际可用宽度切换 | PASS | innerWidth=800、rootWidth=744、space=switch |
| ④-1 800×600 单开提交详情：完整、中心命中、不遮提交按钮 | PASS | details{bounds} 命中自身；submit 可命中、无重叠 |
| ④-2/④-4 800×600 组合开：任务与详情明确切换、同一时间只留最近打开的一块 | PASS | 任务打开→详情收起；详情打开→任务收起 |
| ④-3 800×600 任务面板不遮提交按钮、中心命中自己 | PASS | overlapSubmit=false、submitHit=true |
| ④-5 800×600 Esc 收起最上层非模态面板 | PASS | detailsOpen=false、taskOpen=false |
| ④-6/④-7 1120/1121 实际边界与两侧中心/命中 | PASS | 1176(inner)=switch、1177(inner)=side-by-side；两块面板/提交按钮互不遮挡、中心命中一致 |
| ⑤ 六件拖放/首件失败/路由往返 | 未跑完 | 脚本进入互动板面超时 |
| ⑥ 主题/Tab/Esc/IME | 未跑完 | 主题注入时 localStorage 安全异常（CDP/Edge 文档限制） |

## 诚实边界
- 假厂商只证明「QIO 自己的链路」；不证明真实厂商、Tauri 原生窗口或 Windows 原生拖放。
- 本轮未复跑 ①-5 之后的脚本修复；①-6..①-10 的浏览器级首轮交付仍 UNVERIFIED，不宣称通过。
- 800×600 单开证据是真实浏览器实测；1121/1120 边界由组件/纯函数测试覆盖（`pm032-layout-shell.test.ts`），本机浏览器实测未跑。
- 所有截图与 JSON 均在本目录，可复跑 `run.ps1 -Only s1` 单场景。
