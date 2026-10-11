# 互动板改前／改后对照截图

这些图是**审美与布局**改动的可查看证据，和交互正确性的证据（`docs/interactive-ui-verify.md`）分开。
采集脚本：`scripts/interactive-verify/ui-screens.mjs`（Edge 驱动，见该文件头部的用法）。

## 怎么复现

```powershell
# 改前（基线 4538578 的实例）
node scripts/interactive-verify/ui-screens.mjs --label=before --app=http://127.0.0.1:5421 --backend=http://127.0.0.1:8921 --group-name="组 1"
# 改后（本分支的实例）
node scripts/interactive-verify/ui-screens.mjs --label=after  --app=http://127.0.0.1:5299 --backend=http://127.0.0.1:8791 --group-name="默认组名"
```

采集前脚本会用产品接口把板面铺成**同一份固定数据**（两张注释、一个组、一张长代码卡、一张超长名称卡、一条关系），
并生成一批演示意图；因此同一档位、同一主题下的改前／改后图可以直接对比。

## 命名

`<before|after>-<宽>x<高>-<dark|light>-<场景>.png`

| 场景 | 说明 |
| --- | --- |
| `normal` | 常态布局（含长代码与超长名称） |
| `selected` | 选中注释卡片（卡片局部工具栏 / 选择菜单） |
| `grouped` | 重叠成组后的组框与组名 |
| `approval` | 待审批意图的虚线预览与单项审批入口 |
| `overlays` | 聊天面板与批量列表**同时展开** |
| `error` | 错误状态（保存失败） |

## 诚实标注

- 场景**布置**用的是页面内合成指针事件与接口铺数据（为了同一数据可复现、跑得快）；
  交互本身的正确性由 `scripts/interactive-verify/fe-scenarios.mjs` 的**真实鼠标/键盘/滚轮**覆盖，不靠这里的截图。
- 图里不含任何真实凭据、私人材料或敏感日志：卡片内容都是脚本写入的示例文本。
