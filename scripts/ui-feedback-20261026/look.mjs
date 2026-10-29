import { launch, shot, api, settle, writeJson, centerOffset, measureRadii, nativeAudit, overflowAudit } from "./lib.mjs";
const OUT = "D:\\qio-dev\\_uf-shots\\look\\";
const r = { shots: [], geometry: {}, audits: {} };
const { browser, page, logs } = await launch({ width: 1280, height: 820 });
async function pub(type, data) { return api("/api/events/test?event_type=" + type, "POST", data ?? {}); }
const approval = (id, name, purpose) => ({ approval: { approval_id: id, kind: "tool_create", label: "创建工具", purpose, state: "waiting", requires_confirmations: 1, created_at: new Date().toISOString(), expires_at: new Date(Date.now()+1800000).toISOString(), payload: { name, description: "统一改造截图用示例申请", source: "main_agent", capabilities: ["访问：受控临时目录", "副作用：无", "网络：不允许"] } } });
try {
  await page.goto("http://127.0.0.1:5199/#/conversation", { waitUntil: "domcontentloaded" });
  await page.waitForSelector(".conversation-workspace"); await settle(page, 3000);
  r.shots.push(await shot(page, "look-01-conversation", OUT));
  // open two approvals while not typing -> auto modal
  await pub("APPROVAL_REQUIRED", approval("look-a1", "整理本周材料并生成摘要", "读取受控目录，生成一份摘要"));
  await page.waitForTimeout(200);
  await pub("APPROVAL_REQUIRED", approval("look-a2", "把发布清单与依赖说明合并成对比表", "读取两份材料，输出对比表"));
  await page.waitForTimeout(1200);
  const hasModal = await page.evaluate(() => Boolean(document.querySelector(".ua-modal")));
  r.geometry.modalOpen = hasModal;
  if (hasModal) {
    r.shots.push(await shot(page, "look-02-approval-modal-1280", OUT));
    r.geometry.title = await centerOffset(page, ".ua-modal", "#ua-title");
    r.geometry.close = await page.evaluate(() => { const m=document.querySelector(".ua-modal"); const c=m?.querySelector(".ua-close"); if(!m||!c) return null; const mr=m.getBoundingClientRect(), cr=c.getBoundingClientRect(); return { rightGap: Math.round(mr.right-cr.right), topGap: Math.round(cr.top-mr.top), size: Math.round(cr.width)+"x"+Math.round(cr.height) }; });
    r.geometry.radii = await measureRadii(page, [".ua-modal", ".ua-btn", ".ua-tab", ".ua-chip", ".ua-nav-btn"]);
    for (const w of [1024, 480, 320]) {
      await page.setViewportSize({ width: w, height: 820 });
      await page.waitForTimeout(600);
      r.shots.push(await shot(page, "look-03-approval-modal-" + w, OUT));
      r.geometry["overflow_" + w] = (await overflowAudit(page)).slice(0, 10);
    }
    await page.setViewportSize({ width: 1280, height: 820 });
    await page.waitForTimeout(400);
    await page.keyboard.press("Escape");
    await page.waitForTimeout(500);
  }
  // dark theme
  await page.evaluate(() => localStorage.setItem("qio-theme", "dark"));
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.waitForSelector(".conversation-workspace"); await settle(page, 2500);
  r.shots.push(await shot(page, "look-04-conversation-dark", OUT));
  const convAudit = { native: await nativeAudit(page), radii: (await measureRadii(page, [".qio-btn", ".entry-btn", ".qio-badge", ".qio-state", ".qio-input"])).slice(0, 30), overflow: (await overflowAudit(page)).slice(0, 20) };
  r.audits.conversation = convAudit;
  // settings
  await page.goto("http://127.0.0.1:5199/#/settings", { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(2500);
  r.shots.push(await shot(page, "look-05-settings-dark", OUT));
  r.audits.settings = { native: await nativeAudit(page), overflow: (await overflowAudit(page)).slice(0, 20) };
  // interactive
  await page.goto("http://127.0.0.1:5199/#/interactive", { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(3000);
  r.shots.push(await shot(page, "look-06-interactive-dark", OUT));
  r.audits.interactive = { native: await nativeAudit(page), overflow: (await overflowAudit(page)).slice(0, 20) };
  r.logs = logs.slice(0, 20);
} catch (e) { r.error = String(e && e.stack || e); console.error(r.error); }
finally { r.evidence = writeJson("look-results", r); console.log(JSON.stringify({ modal: r.geometry.modalOpen, title: r.geometry.title, close: r.geometry.close, radii: r.geometry.radii, nativeConv: r.audits.conversation?.native?.nativeControls?.length, titleAttrsConv: r.audits.conversation?.native?.titleElements?.length, overflowConv: r.audits.conversation?.overflow?.length }, null, 2)); await browser.close(); }
