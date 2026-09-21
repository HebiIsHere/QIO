/**
 * 分组 `atoms`：五个原子控件（QInput / QSelect / QNumber / QSlider / QConfirm）的全状态截图。
 *
 * 与其它分组的区别：这些控件的边界态在真实页面里大多到不了，所以先把状态摆在一张
 * 只读画廊页上（`frontend/ui-catalog.html` + `frontend/ui-catalog/main.ts`，在产品构建之外），
 * 再逐个状态截图。这一页只引用产品组件与产品令牌，不复制样式。
 *
 * 实例：`python scripts/ui-catalog/instance.py up --name atoms --backend-port 8837 --frontend-port 6202 --clean`
 * （这一组不读数据库，起隔离实例只是为了不和别人的事件 / 主实例互相干扰）
 *
 * 用法：node scripts/ui-catalog/atoms.mjs
 * 产物：frontend/e2e-shots/ui-catalog/atoms/*.png 与 manifest-atoms.json
 *      截图文件名带主题后缀（`-dark` / `-light`），两套主题不会互相覆盖。
 */
// lib.mjs 在 import 时读环境变量，所以先设好再动态导入
process.env.QIO_BASE = process.env.QIO_BASE || "http://127.0.0.1:6202";
process.env.QIO_API = process.env.QIO_API || "http://127.0.0.1:8837";

const { createSession, launchBrowser, runGroup, saveManifest, sleep, DEFAULT_VIEWPORT } = await import("./lib.mjs");

const fail = [];
const all = [];
/** 采集过程中观察到的产品行为（写进报告，不当成失败） */
const observations = [];

/** 一个状态失败不带崩整批（并在最终报告里如实列出） */
async function safe(label, fn) {
  try {
    await fn();
  } catch (err) {
    const message = String(err?.message ?? err).slice(0, 200);
    fail.push({ label, error: message });
    console.log(`  [FAIL] ${label} :: ${message}`);
  }
}

/** 把某个状态块滚到视口顶端：弹层（下拉菜单）在块下方展开，需要给它留出空间 */
async function topAlign(page, selector) {
  await page.locator(selector).evaluate((el) => el.scrollIntoView({ block: "start" }));
  await sleep(260);
}

async function main() {
  const browser = await launchBrowser();

  for (const theme of ["dark", "light"]) {
    const s = await createSession(browser, {
      group: "atoms",
      name: `原子控件 · ${theme}`,
      theme,
      viewport: DEFAULT_VIEWPORT,
      // 这一组不需要动效，统一按「减少动画」拍：状态切换后立即稳定，不会拍在过渡中间
      motion: "reduced",
    });
    const page = s.page;

    await s.goto({ path: "/ui-catalog.html", waitFor: "#atoms-qinput-default" });
    await page.waitForFunction(() => document.documentElement.dataset.galleryReady === "1", null, { timeout: 15000 });
    await sleep(400);

    const applied = await page.evaluate(() => document.documentElement.dataset.theme);
    if (applied !== theme) throw new Error(`主题没生效：期望 ${theme}，实际 ${applied}`);
    console.log(`\n== ${theme} 主题（data-theme=${applied}）==`);

    /** 画廊页里的 DOM id（不带主题） */
    const bid = (state) => `atoms-${state}`;
    /** 截图 id：带主题后缀，两套主题各存一份 */
    const sid = (state) => `${bid(state)}-${theme}`;
    const shotBlock = (state, title, opts = {}) =>
      s.shotEl(`#${bid(state)}`, sid(state), `${title} · ${theme}`, opts);

    // ------------------------------------------------------------ QInput
    await safe("QInput", async () => {
      for (const [state, title] of [
        ["qinput-default", "QInput 默认（带值）"],
        ["qinput-placeholder", "QInput 占位（空值）"],
        ["qinput-error", "QInput 校验错误"],
        ["qinput-mono", "QInput 等宽（mono）"],
        ["qinput-password", "QInput 密码（type=password）"],
        ["qinput-disabled", "QInput 禁用"],
      ]) {
        await topAlign(page, `#${bid(state)}`);
        await shotBlock(state, title);
      }
      // 聚焦态：焦点确实落在输入框里（:focus 的高亮环可见）
      await topAlign(page, `#${bid("qinput-focus")}`);
      await page.focus(`#${bid("qinput-focus")} .qio-input`);
      const focused = await page.evaluate(() => document.activeElement?.className ?? "");
      await sleep(200);
      await shotBlock("qinput-focus", "QInput 聚焦", { note: `document.activeElement=${focused}` });
    });

    // ------------------------------------------------------------ QSelect
    await safe("QSelect", async () => {
      for (const [state, title] of [
        ["qselect-closed", "QSelect 关闭（未选）"],
        ["qselect-selected", "QSelect 已选中（收起）"],
        ["qselect-empty", "QSelect 空选项"],
        ["qselect-disabled", "QSelect 禁用"],
      ]) {
        await topAlign(page, `#${bid(state)}`);
        await shotBlock(state, title);
      }

      // 空选项：点了也不展开（如实记录观察结果）
      await topAlign(page, `#${bid("qselect-empty")}`);
      await page.click(`#${bid("qselect-empty")} .qio-select`);
      await sleep(300);
      const emptyOpen = await page.evaluate(
        () => document.querySelector("#atoms-qselect-empty .qio-select")?.classList.contains("open") ?? null,
      );
      observations.push(`QSelect 空选项：点击后 open=${emptyOpen}（期望 false）`);
      console.log(`  [check] 空选项点击后 open=${emptyOpen}`);

      // 打开态
      await topAlign(page, `#${bid("qselect-open")}`);
      await page.click(`#${bid("qselect-open")} .qio-select`);
      await sleep(320);
      await shotBlock("qselect-open", "QSelect 打开（菜单展开）", { pad: 170 });
      await page.keyboard.press("Escape");
      await sleep(220);

      // 打开 + 键盘高亮：↓ 两次把高亮移到第三项（与「已选中」是两种标记）
      await topAlign(page, `#${bid("qselect-highlight")}`);
      await page.click(`#${bid("qselect-highlight")} .qio-select`);
      await sleep(260);
      await page.keyboard.press("ArrowDown");
      await page.keyboard.press("ArrowDown");
      await sleep(260);
      const hlText = await page.evaluate(
        () => document.querySelector("#atoms-qselect-highlight .qio-select-menu .opt.hl")?.textContent?.trim() ?? "",
      );
      console.log(`  [check] 高亮项=${hlText}`);
      await shotBlock("qselect-highlight", "QSelect 打开 + 键盘高亮", {
        pad: 170,
        note: `方向键把高亮移到「${hlText}」；高亮（hover / 键盘）与「已选中」（✓）是两种标记`,
      });
      await page.keyboard.press("Escape");
      await sleep(220);
    });

    // ------------------------------------------------------------ QNumber
    await safe("QNumber", async () => {
      for (const [state, title] of [
        ["qnumber-default", "QNumber 常规"],
        ["qnumber-unit", "QNumber 带单位"],
        ["qnumber-empty", "QNumber 空值"],
        ["qnumber-disabled", "QNumber 禁用"],
      ]) {
        await topAlign(page, `#${bid(state)}`);
        await shotBlock(state, title);
      }

      // 聚焦态
      await topAlign(page, `#${bid("qnumber-focus")}`);
      await page.focus(`#${bid("qnumber-focus")} .q-number-input`);
      await sleep(200);
      await shotBlock("qnumber-focus", "QNumber 聚焦");

      // 上限夹取：已经到 max=10，再点 ＋ 仍然停在 10
      await topAlign(page, `#${bid("qnumber-clamp")}`);
      const before = await page.inputValue(`#${bid("qnumber-clamp")} .q-number-input`);
      await page.click(`#${bid("qnumber-clamp")} .step.up`);
      await sleep(260);
      const after = await page.inputValue(`#${bid("qnumber-clamp")} .q-number-input`);
      observations.push(`QNumber 上限夹取：max=10 时点 ＋ 前=${before}、后=${after}（期望不越过 10）`);
      console.log(`  [check] 上限夹取：点 ＋ 前=${before} 后=${after}`);
      await shotBlock("qnumber-clamp", "QNumber 上限夹取（max=10）", {
        note: `点 ＋ 前=${before}，点 ＋ 后=${after}（不越过上限）`,
      });
    });

    // ------------------------------------------------------------ QSlider
    await safe("QSlider", async () => {
      for (const [state, title] of [
        ["qslider-zero", "QSlider 最小值（0）"],
        ["qslider-mid", "QSlider 中值"],
        ["qslider-full", "QSlider 最大值（1）"],
        ["qslider-disabled", "QSlider 禁用"],
      ]) {
        await topAlign(page, `#${bid(state)}`);
        await shotBlock(state, title);
      }
    });

    // ------------------------------------------------------------ QConfirm
    await safe("QConfirm", async () => {
      /**
       * inline / popover 档：确认层就地渲染在块的流程里。
       *
       * 顺带核实第四阶段定的键盘约定「Esc 一律取消」：
       * 点击触发按钮后触发按钮被移除、焦点落回 body —— 这时 Esc 到不了确认层。
       * 所以先量「点开后直接 Esc」与「焦点进入确认层后 Esc」两种结果，再重新打开截图。
       */
      for (const [state, title] of [
        ["qconfirm-inline-normal", "QConfirm inline 档 · 普通"],
        ["qconfirm-inline-danger", "QConfirm inline 档 · 危险"],
        ["qconfirm-popover-normal", "QConfirm popover 档 · 普通"],
        ["qconfirm-popover-danger", "QConfirm popover 档 · 危险"],
      ]) {
        await topAlign(page, `#${bid(state)}`);
        const sel = `#${bid(state)} .qio-confirm`;
        await page.click(`#${bid(state)} [data-role="trigger"]`);
        await sleep(300);
        if ((await page.locator(sel).count()) !== 1) throw new Error(`${state}：确认层没有出现`);

        // 量键盘行为（这一轮只为测量，随后会重新打开再拍）
        await page.keyboard.press("Escape");
        await sleep(240);
        const escFromBody = await page.locator(sel).count();
        if (escFromBody !== 0) {
          await page.locator(`${sel} .qio-btn`).first().focus();
          await page.keyboard.press("Escape");
          await sleep(240);
        }
        const escFromInside = await page.locator(sel).count();
        // 用真实交互（点「取消」）收尾，保证后面的状态不受影响
        if ((await page.locator(sel).count()) !== 0) {
          await page.click(`${sel} .qio-btn`);
          await sleep(260);
        }
        if ((await page.locator(sel).count()) !== 0) throw new Error(`${state}：确认层没能关掉`);
        observations.push(
          `${state}：点开后直接按 Esc ${escFromBody === 0 ? "会关闭" : "不关闭（焦点在 body，Esc 到不了确认层）"}；` +
            `焦点移入确认层后按 Esc ${escFromInside === 0 ? "关闭" : "仍不关闭"}`,
        );
        console.log(
          `  [check] ${state} Esc(body)=${escFromBody === 0 ? "关闭" : "未关闭"} ` +
            `Esc(层内焦点)=${escFromInside === 0 ? "关闭" : "未关闭"}`,
        );

        // 真实状态：重新打开后再拍
        await page.click(`#${bid(state)} [data-role="trigger"]`);
        await sleep(320);
        await shotBlock(state, title, {
          note: "点「取消」可关闭；Esc 的两种情形见 REPORT（焦点在 body 时不生效）",
        });
        await page.click(`${sel} .qio-btn`);
        await sleep(260);
      }

      // layer 档：带遮罩的浮动层，铺满整个视口 → 取整屏
      for (const [state, title] of [
        ["qconfirm-layer-normal", "QConfirm layer 档 · 普通（全屏遮罩）"],
        ["qconfirm-layer-danger", "QConfirm layer 档 · 危险（全屏遮罩）"],
      ]) {
        await topAlign(page, `#${bid(state)}`);
        await page.click(`#${bid(state)} [data-role="trigger"]`);
        await page.waitForSelector(".qio-confirm-scrim", { timeout: 5000 });
        await sleep(360);
        await s.shot(sid(state), `${title} · ${theme}`, { note: "layer 档铺满视口，取整屏；Esc 可关闭（焦点被自动放进确认层）" });
        await page.keyboard.press("Escape");
        await sleep(320);
        const left = await page.locator(".qio-confirm-scrim").count();
        if (left !== 0) throw new Error(`Esc 没有关掉 layer 确认层（剩余 ${left}）`);
      }
    });

    const errors = s.consoleErrors;
    if (errors.length) console.log(`  [console] ${errors.length} 条错误，例如：${errors[0]}`);
    observations.push(`${theme} 主题：控制台错误 ${errors.length} 条`);
    all.push(...s.entries.splice(0, s.entries.length));
    await s.close({ save: false });
  }

  await browser.close();
  saveManifest("atoms", all);
  console.log(`\n合计 ${all.length} 张图，失败 ${fail.length} 个状态`);
  for (const f of fail) console.log(`  - ${f.label}: ${f.error}`);
  console.log("\n观察记录：");
  for (const o of observations) console.log(`  - ${o}`);
}

await runGroup(main);
