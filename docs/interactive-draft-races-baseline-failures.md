# 本轮反例证据：新场景在旧基线上失败（2026-10-08）

命令（旧基线实例：前端 `5421`、后端 `8921`，代码 = `224bc63`，未改动）：

```powershell
cd D:\qio-dev\qio-polish
$env:IM_APP='http://127.0.0.1:5421'; $env:IM_BACKEND='http://127.0.0.1:8921'
node scripts/interactive-verify/fe-scenarios.mjs --only=16,17
```

实际输出（原样抄录）：

```text
=== 互动板前端改版实机验收（app=http://127.0.0.1:5421 backend=http://127.0.0.1:8921）===

--- 场景 16 ---
FAIL  16 删空正文后草稿真的落盘（空正文也是一条草稿）  —— {"mark":"draft-empty","ok":false,"ms":8053}
FAIL  16 取消后重新编辑：输入框是空的（原正文不回来）  —— {"value":null,"originalHead":""}
PASS  16 未确认的空草稿不改变正式正文  —— {"now":""}

--- 场景 17 ---
PASS  17 前置：宽窗口两面板都在  —— {"mark":"wide","chat":true,"batch":true,"mode":"side-by-side"}
FAIL  17 480px 下几何变成空间不足且只显示一个面板  —— {"mark":"narrow","chat":true,"batch":true,"mode":"switched","hint":""}
FAIL  17 保留的是最近打开的面板（批量）  —— {"chat":true,"batch":true}
FAIL  17 给出可切换的提示
FAIL  17 放大回去不自动弹出第二个面板  —— {"mark":"back","chat":true,"batch":true,"mode":"side-by-side"}

合计 8 项，通过 2 项，失败 6 项
```

## 说明

- **场景 17** 与提示词描述的现象完全一致：几何已经算出 `mode: "switched"`，但 `chatOpen` 与 `batchOpen` 仍都为 true，
  两个面板都还显示着，也没有任何「可切换」提示；放大回 1200px 后同样两个都还在。
- **场景 16** 的第一条失败说明「把正文删空」这件事在旧实现里**根本没有形成一条可保存的草稿**
  （服务端草稿接口 8 秒内没有出现该卡片的空正文记录），这正属于「空草稿不是有效状态」这一族问题。
  第二条失败含脚本侧因素（取消后重新选中卡片再开编辑器的步骤在旧基线上没走通，`value: null` 表示没找到编辑器），
  修后会用同一命令复跑，并把脚本步骤按新行为对齐后再记录结论。
- 这两条场景是**按正确行为断言**的：旧基线必须失败，修复后必须通过；不存在「断言异常存在」的写法。
