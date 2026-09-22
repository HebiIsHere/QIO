/**
 * 生成给非技术读者看的界面全览页（输出：frontend/e2e-shots/ui-catalog/gallery.html）。
 *
 * 与 aggregate.mjs 生成的机器版图册的区别：
 * - 按「用户会怎么用」分区，每区先讲这一段是干什么的，再放图；
 * - 每张图下面写清楚：什么情况下会出现、深色还是浅色、窗口多大；
 * - 单独一节写「我们发现的问题」：会发生什么 → 为什么 → 影响 → 现场截图。
 *
 * 图片用相对路径，整页可以直接双击打开。
 */
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { SHOT_ROOT } from "./lib.mjs";

const merged = JSON.parse(readFileSync(path.join(SHOT_ROOT, "manifest-merged.json"), "utf-8"));

function entriesOf(group, filter) {
  const bucket = merged.groups[group];
  if (!bucket) return [];
  const list = bucket.entries.filter((e) => e.registered && e.relPath);
  return (filter ? list.filter(filter) : list).map((e) => ({ ...e, group }));
}
const findEntry = (group, id) => entriesOf(group).find((e) => e.id === id) ?? null;

const SECTIONS = [
  {
    id: "chat",
    title: "一、对话页",
    lead: "打开 QIO 之后看到的那个页面：中间是聊天记录，底部是输入框，右上角是设置入口，右下角的球是话题星球的入口。这一节按「能遇到的情况」排列：正常聊天、出问题时、等 QIO 干活时、需要你点头时。",
    items: entriesOf("conv-root"),
  },
  {
    id: "settings",
    title: "二、设置页",
    lead: "点右上角齿轮进入。左边是七个分类：外观、对话与记忆、模型与联网、工具与权限、数据与维护、凭据、高级；右边是这个分类里的开关与选项。凭据卡片的四种状态、三种弹窗、删除与撤销的确认框都在这里。",
    items: entriesOf("settings-root", (e) => e.id.startsWith("settings-")),
  },
  {
    id: "debug",
    title: "三、调试页",
    lead: "给排查问题用的只读页面：记录每一轮对话「为什么这样回答」——查了什么、读进哪些记忆、调用了哪些工具。日常使用不需要打开。",
    items: entriesOf("settings-root", (e) => e.id.startsWith("debug-")),
  },
  {
    id: "planet",
    title: "四、话题星球",
    lead: "点右下角的球进入。它把话题铺成球面上的地点：球是主体，右侧是可收起的面板，面板分「话题 / 知识 / 实体」三块；从某一轮旧对话继续聊的入口也在这里。",
    items: entriesOf("planet-root"),
  },
  {
    id: "atoms",
    title: "五、基础控件",
    lead: "输入框、下拉框、数字步进、滑块、确认框这五类最小零件。它们平时藏在各个页面里，这里把每一种状态单独摆出来看，包括平时很难遇到的「错误」「禁用」「空值」。",
    items: entriesOf("atoms"),
  },
  {
    id: "conv2",
    title: "六、另一份对话页采集（内容有重复）",
    lead: "对话页在这一次整理里被独立采了两遍。下面是第二份，张数更多，但很多是同一状态的重复拍摄；放在最后方便对照，不影响前几节阅读。",
    items: entriesOf("conv"),
  },
  {
    id: "tools",
    title: "七、采集工具的自检图",
    lead: "这几张不是产品界面，而是采集工具自己「能不能连上、能不能截到图」的检查图，放在这里只说明工具跑通了。",
    items: [...entriesOf("smoke"), ...entriesOf("probe"), ...entriesOf("probe2")],
  },
];

const ISSUES = [
  {
    title: "1. 「QIO 想记住」这张卡片被输入框压住，三个按钮鼠标点不到",
    what: "当 QIO 提出「这条信息以后可能有用，要不要记住」时，对话最底部会出现一张卡片，上面有三个按钮：保存、修改、忽略。这张卡片会被底部的输入框盖住，只露出上边缘，三个按钮都点不到。",
    why: "输入框是固定在窗口最底部的悬浮层，而这张卡片在页面排版里排在输入框下面，没有为它留出空间，于是被压住。",
    impact: "同一个原因还影响另外两处：问「要不要把这段内容转到别的话题」的提示条，以及「当前模型不支持某些功能，已用兼容模式」的提示条。",
    numbers:
      "怎么确认的：我们量了这两块的位置——卡片上的按钮正好落在输入框覆盖的范围里。再在那个按钮的位置上试一次「点下去会点到谁」，结果点到的是输入框，不是按钮。这就是「看得见却点不到」的原因。",
    evidence: [
      { group: "conv-root", id: "conv-53-candidate-covered", caption: "被压住的现场：卡片几乎全在输入框下面" },
      { group: "conv-root", id: "conv-51-knowledge-editing", caption: "同一张卡片进入「修改内容」之后：能看到的仍然只有上半部分，按钮还是在输入框下面" },
    ],
    fixed:
      "已修复。底部三块（知识候选卡 / 兼容模式提示 / 话题切换提示）按输入区的实际高度整体让位；" +
      "实测按钮中点命中的元素已经从输入框变回按钮本身。",
    fix_evidence: [
      { group: "conv-root", id: "conv-130-candidate-above-composer", caption: "候选卡完整位于输入区上方，三个按钮可点" },
      { group: "conv-root", id: "conv-132-topic-switch-above-composer", caption: "话题切换提示条同样抬到输入区上方（下沿 752px ≤ 输入框上沿 761px）" },
    ],
  },
  {
    title: "2. 用 Esc 键关不掉小确认框",
    what: "在知识面板或实体面板里点「归档」时，会弹出一个就地确认框。这时按 Esc 键关不掉它；必须先点一下框里的内容，再按 Esc 才有反应。",
    why: "这个确认框的「按 Esc 取消」只在两种情况下生效：一种是你点过它（焦点在框里），另一种是那种带灰色遮罩的大确认框。普通的就地确认框两者都不是。",
    impact: "用键盘操作的人会以为界面卡住了；鼠标用户不受影响。",
    evidence: [
      { group: "atoms", id: "atoms-qconfirm-inline-normal-dark", caption: "就地确认框（普通）" },
      { group: "atoms", id: "atoms-qconfirm-popover-normal-dark", caption: "浮在触发点旁边的确认框（同样受影响）" },
    ],
    fixed: "已修复。三档确认框一律全局监听 Esc，关闭后焦点归还触发元素（已不在时还给同卡片相邻项）。外观没有改动，所以仍用上面两张图看形态。",
    fix_evidence: [
      {
        group: "atoms",
        id: "atoms-qconfirm-layer-danger-dark",
        caption: "确认层外观未变；行为修复由 test：QConfirmEsc.test.ts 覆盖（三档 + 重开场景）",
      },
    ],
  },
  {
    title: "3. 「从这里继续」在一部分话题上会失败",
    what: "在星球里选一个「已经有正在进行中的片段」的话题，点「从这里继续」，操作会失败。只有当这个话题的历史片段都已经封存时，这个按钮才好用。",
    why: "这条操作会在话题里新开一个片段，但每个话题只允许有一个「进行中」的片段，于是撞车。而真实使用中，想接着聊的话题往往正处在「有进行中片段」的状态。",
    impact: "用户在星球上「接着旧话题聊」的主要入口会失败。现有自动化测试只覆盖了没有进行中片段的那一种情况，所以一直没被发现。",
    evidence: [
      { group: "planet-root", id: "planet-16-fragment-selected", caption: "面板底部就是「进入某个话题」这个按钮" },
      { group: "planet-root", id: "planet-18-start-here", caption: "点下去之后的结果" },
    ],
    fixed:
      "已修复。点击历史只登记接续意图；真正有消息执行时才在一个事务里封存旧段并建立接续段，来源指向所选历史。",
    fix_evidence: [
      { group: "conv-root", id: "conv-133-continuation-hint", caption: "点击之后输入区显示「下一条消息将从「…」继续」+ 取消（不再直接报错）" },
      { group: "conv-root", id: "conv-134-continuation-cancelled", caption: "发送前取消：提示消失，不留任何空片段" },
    ],
  },
  {
    title: "4. 新打开页面时，会看到上一次留下的提示和排队信息",
    what: "打开应用或刷新页面后，页面上可能出现上一次留下的错误提示条、排队条、甚至「想记住」的卡片。这些内容不是这一次发生的。",
    why: "服务器会把最近发生过的一批消息，重新发一份给每一个新打开的页面；只有「需要你批准的操作」这一类被排除，其余都会重发。",
    impact: "用户可能重复处理已经处理过的错误，或者看到并不存在的排队任务。这条可以自己复验：先在页面里制造一条错误提示和一段排队信息，关掉它，再新开一个页面，什么都不做——提示和排队又会出现。",
    evidence: [
      { group: "conv-root", id: "conv-120-replay-residue", caption: "新页面（全程没有做任何操作）里出现的上一条错误提示与排队条" },
    ],
    fixed:
      "已修复。新连接不再重放历史事件（只有带游标的重连才补发），前端在连接建立时主动拉一次权威快照。",
    fix_evidence: [
      {
        group: "conv-root",
        id: "conv-135-resync-quiet",
        caption: "新打开的页面不再带出上一次的提示；连接抖动快速同步完成后也不留痕",
      },
    ],
  },
  {
    title: "5. 凭据被暂停或撤销后，对话页不告诉用户",
    what: "把某个模型凭据停用或撤销之后，依赖它的功能会不可用，但对话页不会说明原因。只有「一个凭据都没有」这一种情况，页面才会提示去设置里添加。",
    why: "这两种状态在后台确实被记录下来并送到了页面，但页面上没有任何地方把它显示出来。",
    impact: "用户会看到功能不好使，却不知道是凭据被自己停用了。",
    evidence: [
      { group: "conv-root", id: "conv-66-cred-paused", caption: "暂停凭据后对话页的样子（图中那条提示是「一个凭据都没有」才有的话，与暂停无关）" },
      { group: "conv-root", id: "conv-67-cred-revoked", caption: "撤销凭据后对话页的样子" },
    ],
    fixed: "已修复。凭据暂停 / 失效会在对话页用人话说明，并给出「前往设置」的恢复入口；恢复后提示自己消失。",
    fix_evidence: [
      { group: "conv-root", id: "conv-131-credential-paused-notice", caption: "暂停凭据后出现的提示：说明影响与去处" },
    ],
  },
  {
    title: "6. 有两种提醒，用户看不到",
    what: "第一种：网络连接出现抖动、正在重新同步状态时，页面不会说明。第二种：在「读取历史记录」还没结束时到达的提醒，会被随后的加载过程清掉，最终也不显示。",
    why: "第一种只改了内部状态、没有对应的显示位置；第二种是加载流程会把提醒内容重置。",
    impact: "用户遇到「界面像卡了一下」或「上一条提醒没看到」时，得不到任何解释。",
    evidence: [
      { group: "conv-root", id: "conv-68-resync", caption: "触发「连接抖动」后，页面没有任何提示" },
    ],
    fixed:
      "已修复（按「不制造噪音」的方式）。同步只在真的卡住超过 1.5 秒时才提示，完成后自己撤下；" +
      "同步期间新到的提醒不再被顺手清掉。",
    fix_evidence: [
      { group: "conv-root", id: "conv-135-resync-quiet", caption: "快速同步：页面保持干净，不闪一下又不留痕" },
    ],
  },
  {
    title: "7. 环境不支持 3D 时，说明文字叠在对话内容上",
    what: "在关闭或不支持 3D 绘图的环境里打开星球，会出现「无法启用 3D 星球」的说明。这段说明直接盖在对话内容上，没有独立区域，看起来像界面坏了。",
    why: "这段说明的显示位置没有铺底和留白，文字直接叠在下面的页面上。",
    impact: "这类环境下的用户会以为应用出错；其实话题数据仍然可用，只是显示方式有问题。",
    evidence: [
      { group: "planet-root", id: "planet-70-webgl-fallback", caption: "说明文字与对话内容叠在一起" },
    ],
    fixed: "已修复。降级说明改成一块独立卡片（不透明底 + 内边距 + 圆角），并压在画布之上；球态下不再显示。",
    fix_evidence: [
      { group: "planet-root", id: "planet-80-webgl-fallback-card", caption: "现在是一块独立卡片，明确写了「话题数据仍可用」" },
    ],
  },
];

function esc(text) {
  return String(text ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

const themeLabel = (theme) => (theme === "light" ? "浅色" : "深色");
const sizeLabel = (vp) => (vp ? `${vp.width}×${vp.height}` : "未知尺寸");

/**
 * 图注里不出现技术写法：含这些词的备注直接不显示（它们在 INDEX.md / AUDIT.md 里仍可查）。
 * 面向非技术读者，宁可少一句，也不要让人看不懂。
 */
const TECH = /(px|TEXTAREA|BUTTON|elementFromPoint|clip=|断言|status=|GET |POST |WebGL|manifest|API|DOM|store|事件总线)/i;
function plainNote(note) {
  if (!note) return "";
  if (TECH.test(note)) return "";
  return note.replace(/（局部）/g, "").trim();
}

function figure(e, caption, { showNote = true } = {}) {
  if (!e) return "";
  const title = caption ?? e.title;
  const noteText = showNote ? plainNote(e.note) : "";
  const note = noteText ? `<p class="note">${esc(noteText)}</p>` : "";
  return [
    '<figure>',
    `  <a href="${esc(e.relPath)}" target="_blank"><img loading="lazy" src="${esc(e.relPath)}" alt="${esc(title)}" /></a>`,
    `  <figcaption><b>${esc(title)}</b>${note}<span class="meta">${themeLabel(e.theme)}主题 · 窗口 ${sizeLabel(e.viewport)}</span></figcaption>`,
    '</figure>',
  ].join("\n");
}

function issueBlock(issue) {
  const evidence = issue.evidence
    .map((ref) => figure(findEntry(ref.group, ref.id), ref.caption, { showNote: false }))
    .join("\n");
  const fixed = (issue.fix_evidence ?? [])
    .map((ref) =>
      figure(
        ref.synthetic ?? findEntry(ref.group, ref.id),
        ref.caption,
        { showNote: false },
      ),
    )
    .join("\n");
  return [
    '<article class="issue">',
    `  <h3>${esc(issue.title)}</h3>`,
    `  <p><b>会发生什么：</b>${esc(issue.what)}</p>`,
    `  <p><b>为什么会这样：</b>${esc(issue.why)}</p>`,
    `  <p><b>影响范围：</b>${esc(issue.impact)}</p>`,
    issue.numbers ? `  <p class="numbers">${esc(issue.numbers)}</p>` : "",
    `  <p class="state-note"><b>当前状态：</b>${esc(issue.fixed ?? "尚未修复")}</p>`,
    `  <p class="state-note">修复前的现场：</p>`,
    `  <div class="evidence">${evidence}</div>`,
    fixed ? `  <p class="state-note">修复之后（同一状态重新采集）：</p>` : "",
    fixed ? `  <div class="evidence">${fixed}</div>` : "",
    '</article>',
  ]
    .filter(Boolean)
    .join("\n");
}

const allEntries = Object.values(merged.groups).flatMap((g) => g.entries);
const totalRegistered = allEntries.filter((e) => e.registered).length;
const unregistered = allEntries.filter((e) => !e.registered).length;

const nav = SECTIONS.filter((s) => s.items.length)
  .map((s) => `<a href="#${s.id}">${esc(s.title)}（${s.items.length} 张）</a>`)
  .join("");

const CSS = [
  ":root { color-scheme: light; }",
  "* { box-sizing: border-box; }",
  'body { margin: 0; padding: 40px 24px 80px; background: #f7f5f7; color: #221a20; font-family: "Microsoft YaHei", "Noto Sans SC", system-ui, sans-serif; font-size: 16px; line-height: 1.75; }',
  "main { max-width: 1180px; margin: 0 auto; }",
  "h1 { font-size: 32px; margin: 0 0 8px; }",
  ".sub { color: #6b5c66; font-size: 15px; margin-bottom: 24px; }",
  "h2 { font-size: 24px; margin: 56px 0 10px; padding-top: 26px; border-top: 2px solid #e6dde3; }",
  "h3 { font-size: 19px; margin: 0 0 12px; }",
  ".lead { color: #4a3f46; margin: 0 0 20px; }",
  ".card { background: #fff; border: 1px solid #e6dde3; border-radius: 12px; padding: 20px 22px; margin: 18px 0; }",
  "nav { display: flex; flex-wrap: wrap; gap: 10px; margin: 18px 0 8px; }",
  "nav a { background: #fff; border: 1px solid #e6dde3; border-radius: 999px; padding: 7px 14px; font-size: 14px; color: #8a1055; text-decoration: none; }",
  ".grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(330px, 1fr)); gap: 20px; margin-top: 18px; }",
  "figure { margin: 0; background: #fff; border: 1px solid #e6dde3; border-radius: 12px; padding: 12px; display: flex; flex-direction: column; gap: 8px; }",
  "figure img { width: 100%; height: auto; border-radius: 8px; display: block; background: #171015; }",
  "figcaption { font-size: 14px; line-height: 1.6; }",
  "figcaption b { display: block; font-size: 15px; }",
  "figcaption .note { margin: 4px 0 0; color: #5c4f57; font-size: 13px; }",
  ".meta { display: block; margin-top: 6px; color: #8d7d87; font-size: 12.5px; }",
  ".issue { background: #fff; border: 1px solid #e6dde3; border-left: 5px solid #c51b7d; border-radius: 12px; padding: 22px 24px; margin: 22px 0; }",
  ".issue p { margin: 8px 0; }",
  ".numbers { background: #faf3f7; border-radius: 8px; padding: 10px 12px; font-size: 14.5px; color: #4a3f46; }",
  ".evidence { display: grid; grid-template-columns: repeat(auto-fill, minmax(330px, 1fr)); gap: 18px; margin-top: 14px; }",
  "ul { padding-left: 22px; }",
  "li { margin: 6px 0; }",
  ".foot { margin-top: 60px; padding-top: 22px; border-top: 2px solid #e6dde3; color: #5c4f57; font-size: 14.5px; }",
  "code { background: #efe8ee; border-radius: 5px; padding: 1px 6px; font-size: 13.5px; }",
].join("\n");

const sectionHtml = SECTIONS.filter((s) => s.items.length)
  .map((s) =>
    [
      `<h2 id="${s.id}">${esc(s.title)}（${s.items.length} 张）</h2>`,
      `  <p class="lead">${esc(s.lead)}</p>`,
      '  <div class="grid">',
      s.items.map((e) => figure(e)).join("\n"),
      "  </div>",
    ].join("\n"),
  )
  .join("\n");

const html = [
  "<!doctype html>",
  '<html lang="zh-CN">',
  "<head>",
  '<meta charset="utf-8" />',
  '<meta name="viewport" content="width=device-width, initial-scale=1" />',
  "<title>QIO 界面全览（通俗版）</title>",
  `<style>${CSS}</style>`,
  "</head>",
  "<body>",
  "<main>",
  "  <h1>QIO 界面全览</h1>",
  '  <p class="sub">把 QIO 现在所有的界面，按「用户会怎么用」分成七块，一块一块看过去；最后一节写我们发现的问题。</p>',
  '  <div class="card">',
  "    <h3>这一页是什么</h3>",
  "    <ul>",
  `      <li>这是把 QIO 的界面一个个截下来做成的图册，一共 <b>${totalRegistered} 张</b>，按使用场景分成七节。</li>`,
  "      <li>每张图下面两行字：<b>什么情况下会出现</b>（标题）、<b>深色还是浅色 + 窗口多大</b>（灰色小字）。</li>",
  '      <li>只想看结论，直接跳到 <a href="#issues">「八、我们发现的问题」</a>，那里每条都配了现场截图。</li>',
  "      <li>图都可以点开看大图。</li>",
  "    </ul>",
  "  </div>",
  '  <div class="card">',
  "    <h3>三个阅读要点</h3>",
  "    <ul>",
  "      <li><b>深色 / 浅色</b>：QIO 有两套配色（暗紫晶、净白），功能完全一样，同一个状态常常各有一张。</li>",
  "      <li><b>窗口 1440×900</b> 是普通电脑窗口；<b>820×900 / 620×860</b> 是把窗口拉窄之后的样子，用来检查排版会不会挤坏。</li>",
  "      <li>标题里带 <b>「局部」</b> 的是只截了界面的一小块；带 <b>「已裁切」</b> 的是长图只截了上半部分。</li>",
  "    </ul>",
  "  </div>",
  `  <nav>${nav}<a href="#issues">八、发现的问题（${ISSUES.length} 条）</a></nav>`,
  sectionHtml,
  '  <h2 id="issues">八、我们发现的问题（7 条）</h2>',
  '  <p class="lead">每一条都写成三段：会发生什么、为什么会这样、影响范围，并附上当时的截图。这些都是做这次整理时实际跑出来的，不是推测。</p>',
  ISSUES.map(issueBlock).join("\n"),
  '  <div class="foot">',
  "    <h3>关于这份材料的说明</h3>",
  "    <ul>",
  "      <li>截图来源：在本地运行 QIO（开发模式）后逐个状态截取，没有连接真实的模型服务，也没有使用任何真实密钥。</li>",
  "      <li>「调试页」里带记录的那几张，是用<b>构造的数据</b>渲染出来的：本机没有真实运行记录，也不允许为截图去调用真实模型。</li>",
  "      <li>「环境不支持 3D」那一张，是用屏蔽浏览器 3D 绘图能力的方式触发的，等同于在一台没有该能力的机器上打开。</li>",
  `      <li>另有 <b>${unregistered} 张</b>图是重复采集或第一轮的旧图，没有登记标题，所以不在这七节里；它们仍在同一个文件夹中，列在 <code>AUDIT.md</code>。</li>`,
  "      <li>本页由 <code>scripts/ui-catalog/human-page.mjs</code> 生成，图片与清单在同一个文件夹。</li>",
  "    </ul>",
  "  </div>",
  "</main>",
  "</body>",
  "</html>",
].join("\n");

const out = path.join(SHOT_ROOT, "gallery.html");
writeFileSync(out, html, "utf-8");
console.log(`通俗版页面 → ${out}`);
console.log(
  `分区张数：${SECTIONS.map((s) => `${s.title.replace(/^[一二三四五六七]、/, "")}=${s.items.length}`).join(" · ")}`,
);
console.log(`已登记 ${totalRegistered} 张；未登记 ${unregistered} 张；问题 ${ISSUES.length} 条`);
