# 互动页 v2 · 统一几何（Phase 2-A 交付）

> 归属：子智能体 p2-a；只新增本目录。
> 契约：`docs/interactive-page-v2-contract.md` §5.2 / §5.5；`docs/interactive-mode-contract.md` §8.2 / §11.10 / §15 / §17.1。

## 1. 文件

| 文件 | 作用 |
| --- | --- |
| `geometry.ts` | 统一几何纯函数：坐标换算、显示帧（拖动临时矩形）、确定性最小避让、组缩放槽位、原子变更计划。 |
| `__tests__/geometry.test.ts` | 定向测试（45 条）：边界、坏数、确定性、纯度、避让上限、图片比例、最小尺寸、原子性。 |

边界（与 `interactive/geometry.ts`、`interactive/resize.ts` 同一条）：只做算术，不读 DOM / store / 网络，
不改输入对象，同样输入永远同样输出。

## 2. 接口

### 2.1 坐标（screen ⇄ board ⇄ group-local）

```ts
boardViewport({ scale, surfaceLeft, surfaceTop, scrollLeft, scrollTop, panX, panY }): BoardViewport
toScreenPoint(view, point) / toBoardPoint(view, point)          // 点
toScreenRect(view, rect) / toBoardRect(view, rect)              // 矩形
boardToGroupLocal(frame, point) / groupLocalToBoard(frame, point)
boardRectToGroupLocal(frame, rect) / groupLocalRectToBoard(frame, rect)
```

- 屏幕坐标 = 板面坐标 × scale + offset；offset 已合成「画布内容原点 + 滚动 + 平移」，
  与 `interactive/viewport.ts` 的 effectiveRect + Viewport{x,y} 是同一物理量，只是合了一次。
- 缩放沿用 `viewport.ts` 的 [0.25, 2.5]（本模块只转出，不另立数字）。

### 2.2 显示帧（拖动中的临时矩形；连线与命中同源）

```ts
resolveDisplayFrames({ objects, drag?, bounds? }): { frames, movingIds }
hitTestFrames(frames, point, order?)
frameEdgePoint(frame, "left"|"right"|"top"|"bottom")
edgeAnchorToward(fromFrame, towardFrame)
```

- `objects` 是卡片 + 组（`{ id, kind: "card"|"group", frame, groupId? }`）。
- 拖动卡片只移动它；拖动组时组框与成员按同一位移显示。给了 `bounds` 先钳画布，随动成员跟随实际位移。
- **连线端点与指针命中都必须读这同一份 frames**（§11.10：拖动中线与组框实时跟随，松手前不改板面状态）。

### 2.3 确定性最小避让（卡-卡 / 卡-组 / 组-组）

```ts
avoidOverlaps({ moved, others, gap?, maxRounds?, maxShift?, bounds? })
  -> { frame, adjusted, rounds, totalShift, limited, blockers }
avoidsOverlap(frame, obstacles, gap)
```

- 每轮只挑「让开当前障碍所需位移最小」的方向（正好留出 gap），多轮迭代到不再重叠。
- 候选冲突时按「剩余碰撞少 → 位移小 → 生成顺序」选，结果确定、与输入顺序无关。
- **组与自身成员不互算障碍**：移动组跳过自己的成员（其他组成员由组框代表）；移动组内卡片跳过自己的组框与同组成员。
- **不无限推挤**：`maxRounds`（默认 8）与 `maxShift`（默认 2000）到达上限时如实返回 `limited: true`。

### 2.4 组缩放（槽位 / 图片比例 / 最小尺寸 / 不留空白）

```ts
planGroupResize({ frame, nextFrame, members, gap?, padding?, minSize?, maxSize? })
  -> { frame, slots, order, issues }
```

- 沿用当前**行结构**（用户安排不被重排打乱），行内按当前 x 排序。
- 目标尺寸 = 当前尺寸 × (目标组框 / 当前组框)，再夹进卡片最小/最大尺寸。
- 图片成员（`aspectRatio` 为正有限数时）槽位**严格保持原图比例**，不拉伸、不猜比例；`meta.display` 未知时按普通成员处理。
- 组框不小于成员最小布局所需（不够就撑大并报 `min_clamped`）；缩小后行内/行间富余空间按贴边排布分配，不留成片空白。
- 成员之间始终至少留 gap，互不重叠；`order` 与 `slots` 稳定可复现。

### 2.5 原子变更计划

```ts
buildFramePlan({ changes, currentFrames?, bounds?, minSize? })  // 全部合法才 ok=true
applyFramePlan(items, plan)                                     // 新数组，只替换计划内对象
groupResizeFrameChanges(groupId, groupResizeResult, memberIds?) // 组缩放 → 一份清单
```

- 任一项非法（坏数 / 重复 id / 低于最小尺寸 / 出界 / 未知 kind）→ 整份 `ok: false` 并列出问题；
  不存在「组框改了、成员没改」的半份变更。
- 应用一次写回（store 一次 commit / 一次保存），纯函数不改输入。

## 3. Lead 接线点

1. BoardCanvas / BoardLinkLayer / BoardGroupFrame 的渲染与命中统一读 `resolveDisplayFrames` 的 frames；
   连线端点用 `edgeAnchorToward`（收起组由 B 的 linkEndpoints 决定汇聚到组边缘）。
2. 拖动落点：`avoidOverlaps` 的 `frame` 是落点候选，`limited` 为真时要如实提示（不要假装让开）。
3. 组缩放：`planGroupResize` → `groupResizeFrameChanges` → `buildFramePlan`，一次提交组框 + 全部成员；
   图片成员的 `aspectRatio` 取 C 的 `meta.display.aspectRatio`（未知不传）。
4. 板面画布边界沿用 `board.ts` 的 `BOARD_SURFACE_W/H`，作为 `bounds` 传入（本模块不复制这两个数字）。

## 4. 已知限制（如实记）

- 行结构由**当前布局**推断（竖直相交即同一行）；用户把成员摆成重叠/斜排时按相交行的最近口径处理。
- 极端图片比例（例如 100:1）撞上最大边长时，以比例为准，可能无法同时满足最小高度；测试只覆盖常规比例。
- `avoidOverlaps` 只移动**一个**对象（不推别人，与 §15 / §17.1「绝不移动别的卡片」一致）；
  落点实在让不开时返回 limited，由调用方给原因。
- 未做视觉截图验证：纯函数由 Lead 接线后随板面一起做真实浏览器复验。

## 5. 验证

```
cd frontend
npx vue-tsc --noEmit
npx vitest run src/interactive/v2/__tests__/geometry.test.ts
```
