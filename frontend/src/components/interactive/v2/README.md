# 互动页 v2 布局外壳（Phase 1-B 交付）

> 归属：子智能体 p1-layout；只新增本目录与 `components/interactive/__tests__/p1b-v2-*.test.ts`。
> 契约：`docs/interactive-page-v2-contract.md` §2.1 / §2.3。Lead 负责把它们接进 InteractiveView / WindowChrome。

## 1. 文件

| 文件 | 作用 |
| --- | --- |
| `InteractiveWorkspace.vue` | 互动页布局外壳：板面主区 / 底部工具栏 / 输入区 / 向上展开的对话区 / 书签栏（桌面右侧、窄屏下侧）。只吃 slot，不读业务数据，不输出标题栏。 |
| `InteractiveChromeTools.vue` | 挂到 `#qio-interactive-chrome-extra` 的搜索 + 唯一保存状态控件；搜索结果浮层 Teleport 到 body。 |
| `layout.ts` | 响应式档位、书签栏宽度/高度、区域几何与「不覆盖主区」约束（纯函数）。 |
| `chromePlacement.ts` | 顶栏浮层的 fixed 几何，钳进视口不裁切（纯函数）。 |
| `saveStatus.ts` | 保存状态文案（真实状态 → 一句人话 + 详情）。 |
| `chromeTarget.ts` | 等 `#qio-interactive-chrome-extra` 出现的组合式函数（MutationObserver）。 |
| `index.ts` | 统一出口。 |

## 2. InteractiveWorkspace：props / slots / expose

```vue
<InteractiveWorkspace
  :bookmarks-open="bookmarksOpen"
  :chat-open="chatOpen"
  ref="workspace"
>
  <template #board><!-- BoardCanvas / 板面浮层（现有组件原样放入） --></template>
  <template #bookmarks><!-- 书签列表内容（真实 store 数据由 Lead 提供） --></template>
  <template #chat><!-- 对话内容（v2/InteractiveConversation） --></template>
  <template #toolbar><!-- 底部工具栏内容（BoardToolbar 或后续 v2 工具栏） --></template>
  <template #composer><!-- 输入区内容（后续 v2 输入组件） --></template>
</InteractiveWorkspace>
```

- **props**
  - `bookmarksOpen?: boolean`（默认 `true`）：书签栏是否展开；没有 `#bookmarks` slot 时不会渲染。
  - `chatOpen?: boolean`（默认 `false`）：对话区是否展开；没有 `#chat` slot 时不会渲染。
- **slots**：`board` / `bookmarks` / `chat` / `toolbar` / `composer`。缺省的 slot 不会留下空白区域。
- **expose**：`measure()`（重新测量）、`layout`（当前几何结果，与 `resolveWorkspaceLayout` 同源）。
- **根节点 data 属性**（供样式/测试/Lead 联动）：
  `data-iw-band`（`wide`/`compact`/`narrow`）、`data-iw-bookmark-dock`（`side`/`bottom`）、
  `data-iw-bookmarks`（`open`/`closed`）、`data-iw-chat`（`open`/`closed`）、`data-iw-layout-issues`（空间不足时如实给出）。
- **根节点 CSS 变量**：`--iw-gap`、`--iw-bookmark-size`、`--iw-chat-max-h`。
- **区域钩子**：`[data-iw-region="board|bookmarks|chat|toolbar|composer"]`。

布局口径（与纯函数一致）：

- 档位按**工作区自身宽度**：≥1024 `wide`，760–1023 `compact`，<760 `narrow`（未测量时按 `wide`，首帧不闪）。
- 书签栏：`wide` 且右侧窄栏不会把板面压到 420px 以下时用右侧（168–260px）；否则下侧（72–148px 受限高度）。
- 底部：输入区贴底，工具栏在它上方，间距 `--iw-gap`（12px）—— 明确分层、不互相覆盖。
- 对话区打开时从底部区域上方展开，高度上限 = 可用高度 × 46%，并且永远给板面保留 220px 最小高度；
  空间不足时它不占位，`data-iw-layout-issues` 里会如实出现 `chat_capped`。
- 所有区域互不相交：**没有覆盖板面的浮层**（旧的可拖动对话窗已删除；当前互动页把工具栏与输入区
  放进「悬浮对话区」，板面保持完整高度，见 views/InteractiveView.vue 的 .im-floating-chat）。

Lead 集成注意：

1. **唯一保存状态**：接上 `InteractiveChromeTools` 后，请移除 InteractiveView 顶部旧的保存状态
   （`data-im="save-status"`），否则同一个状态会出现两处，与契约 §2.1「互动页只有一处保存状态」冲突。
2. **不新增第二标题栏**：InteractiveView 顶部的 `header.im-top`（板面名/状态/入口）属于待迁走的内容；
   v2 外壳本身不画标题栏。板面名与入口建议并入全局栏容器或板面浮层，由 Lead 决定。
3. **区域顺序（自上而下）**：板面 → 对话区（打开时）→ 下侧书签栏（窄档）→ 工具栏 → 输入区；
   桌面的右侧书签栏与板面并排（在 body 行内），不参与底部堆叠。
4. **输入区/工具栏的视觉表面由 slot 内容负责**：外壳只保证分层、间距与高度预算，不重复画背景与边框。

## 3. InteractiveChromeTools：挂载点与搜索浮层

```vue
<!-- 由 A 的 WindowChrome 在互动路由渲染：位于品牌与设置之间 -->
<div id="qio-interactive-chrome-extra"></div>

<!-- 页面侧（例如 InteractiveView 或 InteractiveWorkspace 外层）挂一次即可 -->
<InteractiveChromeTools />
```

- props：`target?: string`（默认 `#qio-interactive-chrome-extra`，测试/特殊集成可覆盖）。
- 挂载点不存在时**什么都不渲染**（不造一条假顶栏）；出现后由 MutationObserver 自动接上。
- 控件内容：唯一保存状态（`role="status"`，真实 store 的 `saveStatus/dirty/saveError`）+ 板内搜索按钮。
- 搜索：真实 store 板面 + `interactive/board.ts` 的 `searchCards`；点结果 dispatch
  `qio:interactive:locate-card`（沿用互动页既有的定位事件名；旧的独立搜索面板已删除），显示「已在板面上定位」。
- 浮层：`Teleport to body` + `position: fixed`；位置由 `placeChromePopover` 钳进视口
  （右对齐搜索按钮、宽度按视口收窄、`max-height` 收到视口下沿），不被全局栏 `overflow` 裁切。
- 键盘：打开即聚焦输入框；Esc 收起并把焦点还给搜索按钮；点击外部收起（不抢板面焦点）。
- 无障碍：按钮 `aria-expanded/aria-controls/aria-haspopup="dialog"`，浮层 `role="dialog"` 带名字。

## 4. 纯函数 API（Lead / 测试可直接复用）

```ts
import {
  workspaceBand, sideBookmarkWidth, bottomBookmarkHeight, bookmarkDock,
  resolveWorkspaceLayout, GAP, MAIN_MIN_WIDTH, MAIN_MIN_HEIGHT,
} from "./v2/layout";
import { placeChromePopover } from "./v2/chromePlacement";
import { saveStatusView } from "./v2/saveStatus";
```

- `resolveWorkspaceLayout({ width, height, bookmarksOpen, chatOpen, toolbarHeight, composerHeight })`
  返回 `band / bookmarkDock / bookmarkSize / chatMaxHeight / board / bookmarks / chat / toolbar / composer / issues`。
  区域矩形 `{x,y,w,h}` 任意两个互不相交；尺寸不足时先压书签栏/对话区并写 `issues`。
- `placeChromePopover(anchor, viewport, { margin, gap, width, maxWidth })` 返回 `{ left, top, width, maxHeight }`。
- `saveStatusView({ status, dirty, error })` 返回 `{ text, tone: "ok"|"busy"|"err", detail }`。

## 5. Phase 3-B：底部互动对话区（InteractiveConversation.vue）

契约：`docs/interactive-page-v2-contract.md` §6.4。复用现有 `MessageStream / Composer / ComposerEntries`
与会话 store，**不新建简化模型**。

```vue
<InteractiveConversation
  v-model:expanded="conversationOpen"
  :height="conversationHeight"
  :binding="{ boardId, submissionId, intentId }"
  :auto-collapse-on-board-action="true"
  @update:height="conversationHeight = $event"
>
  <template #leading><!-- 模式切换等（Composer 的 leading 槽） --></template>
  <template #header-extra><!-- 收起条右侧的附加控件 --></template>
</InteractiveConversation>
```

- **props**
  - `expanded?: boolean`（默认 false）：展开 / 收起；支持 `v-model:expanded`，不传也能自己切换。
  - `height?: number`（默认 0 = 本机记忆 / 默认 420）：展开面板高度。
  - `minHeight? / maxHeight?`（默认 220 / 720）：高度口径；视口较矮时按 80% 收紧。
  - `autoCollapseOnBoardAction?: boolean`（默认 true）：**板面操作**（interactive store 的 `lastOpLabel`
    前进）时自动收起；只收显示，不停止回复、不清草稿附件、不动排队。
  - `title?: string`（默认「对话」）、`storageKey?: string`（高度记忆键，缺省按 boardId）。
  - `binding?: { mode?: "interactive"|"normal"; boardId?: string; submissionId?: string; intentId?: string }`
    —— **可信 props**：只投影成 `session.send` 的 `options.interactive`（mode/boardId/submissionId），
    不拼进用户原文；`intentId` 目前只保留在 props 上（会话层还没有该发送字段）。
- **emits**：`update:expanded`、`expand`、`collapse`、`update:height`、`resize-start`、`resize-end`。
- **slots**：`leading`（传给 Composer 的 leading 槽）、`header-extra`（收起条右侧）。
- **expose**：`expand() / collapse() / toggle() / focusInput() / expanded / height`。
- **行为**
  - 会话流在输入区**上方**；第一次展开才挂载，之后 `v-show` 保留滚动位置与已渲染内容。
  - Composer 始终挂载：收起不清草稿 / 附件 / 排队，不停止回复（会话 store 一个方法都不调用）。
  - 分隔条：指针拖动（向上拖变高）+ 键盘 ↑/↓（±24）、Home/End；Esc / pointercancel 恢复拖动前高度。
  - 高度记在本机 `localStorage`（按板面），损坏值忽略。
  - 收起条用真实会话状态说话：正在回复/已排队 N 条/草稿已保留/点开继续对话。

### Composer 的绑定接口

- `Composer.vue` 新增可选 prop `chatBinding`：只作为 `session.send` 的 `options.interactive` 传下去；
  不传 → 对话页行为完全不变（请求体不带板面身份）。Composer **不读 interactive store**。
- 互动页由 `v2/InteractiveConversation` 传入 `store.chatBinding`（旧 ChatDock 的 `binding` prop 已随组件删除）。

## 6. 验证

- `npx vue-tsc --noEmit`
- `npx vitest run src/components/interactive/__tests__/p1b-v2-layout.test.ts src/components/interactive/__tests__/p1b-v2-workspace.test.ts src/components/interactive/__tests__/p1b-v2-chrome-tools.test.ts`
- Phase 3-B：`npx vitest run src/components/interactive/v2/__tests__/conversationLayout.test.ts src/components/interactive/v2/__tests__/InteractiveConversation.test.ts src/components/__tests__/p3b-composer-binding.test.ts src/components/interactive/__tests__/p3b-chatdock-binding.test.ts`
