/** 排查用最小探针：起浏览器 → 打开应用 → 打印主题 / 关键元素 / 控制台错误。 */
import { APP } from "./config.mjs";
import { Browser } from "./cdp.mjs";

const browser = new Browser();
try {
  await browser.launch();
  await browser.setViewport(1440, 900);
  await browser.navigate(APP + "/", 4000);
  await browser.evalJs(
    "(async () => { const t0 = Date.now(); for (;;) { if (document.querySelector('[data-im=\"board-toolbar\"]')) return 'ready'; if (Date.now() - t0 > 12000) return 'timeout'; await new Promise(r => setTimeout(r, 150)); } })()",
  );
  const info = await browser.evalJs(
    "(() => ({ title: document.title, theme: document.documentElement.getAttribute('data-theme'), bodyTheme: document.body.getAttribute('data-theme'), url: location.href, toolbar: Boolean(document.querySelector('[data-im=\"board-toolbar\"]')), cards: document.querySelectorAll('[data-im=\"card\"]').length, text: document.body.innerText.replace(/\\s+/g,' ').slice(0, 600) }))()",
  );
  console.log(JSON.stringify(info, null, 2));
  console.log("httpFails:", browser.httpFails.slice(0, 8));
  console.log("consoleErrors:", browser.consoleErrors.slice(0, 8));
  const file = await browser.shotFile("probe/1440x900-smoke.png");
  console.log("shot:", file);
} finally {
  await browser.close();
}
