/**
 * 字体真的加载了吗：三声部（衬线 / 无衬线 / 等宽）逐个查 CDP 平台字体 + 字体文件请求状态。
 *
 * 背景：本工作区的 frontend/node_modules 是指向别的检出的 junction，Vite 默认 fs.allow
 * 挡不住解析后的真实路径，@fontsource 的 woff2 会 403。字体没加载就做排版验收是不可信的，
 * 所以这一步单独做成脚本，结果直接进报告。
 *
 * 用法：node scripts/closure-c-verify/fontcheck.mjs [--app=http://127.0.0.1:5421]
 */
import { writeFileSync } from "node:fs";
import { Browser } from "./cdp.mjs";

const arg = (name, fallback) => {
  const hit = process.argv.find((a) => a.startsWith("--" + name + "="));
  return hit ? hit.split("=").slice(1).join("=") : fallback;
};
const APP = arg("app", "http://127.0.0.1:5421");
const THEME = arg("theme", "light");

const browser = await new Browser().launch();
try {
  await browser.setViewport(1440, 900);
  await browser.navigate(APP + "/#/interactive", 5000);
  await browser.evalJs(`document.documentElement.setAttribute('data-theme', '${THEME}'); 'ok'`);
  await browser.evalJs("(async () => { const b = document.querySelector('[data-im=\"chat-toggle\"]'); if (b && !document.querySelector('[data-im=\"chat-panel\"]')) b.click(); await new Promise(r => setTimeout(r, 600)); return 'chat'; })()");
  await browser.evalJs(`(async () => { await document.fonts.ready; const probe = '互动板面 QIO 0123'; await Promise.all([
    document.fonts.load('600 19px "Noto Serif SC"', probe),
    document.fonts.load('400 14.5px "Segoe UI"', probe),
    document.fonts.load('400 11px "Cascadia Mono"', probe),
  ]); return 'loaded'; })()`);
  const faces = await browser.evalJs(`JSON.stringify([...document.fonts].map((f) => ({ family: f.family, weight: f.weight, status: f.status })).filter((f) => /Serif|Mono|Sans/i.test(f.family)))`);
  const checks = await browser.evalJs(`JSON.stringify({
    serif: document.fonts.check('600 19px "Noto Serif SC"', '互动板面'),
    sans: document.fonts.check('400 14.5px "Segoe UI"', '互动板面'),
    mono: document.fonts.check('400 11px "Cascadia Mono"', '0123'),
    titleFamily: getComputedStyle(document.querySelector('.im-title')).fontFamily,
    titleText: (document.querySelector('.im-title') || {}).textContent
  })`);
  const titleFonts = await browser.platformFonts(".im-title");
  const bodyFonts = await browser.platformFonts('[data-im="chat-scope"] .scope-line');
  await browser.shotFile("fontcheck-" + THEME + ".png");
  const faceList = JSON.parse(faces);
  const faceSummary = faceList.reduce((acc, f) => {
    const key = `${f.family}/${f.weight}/${f.status}`;
    acc[key] = (acc[key] ?? 0) + 1;
    return acc;
  }, {});
  const report = {
    app: APP,
    theme: THEME,
    faceCount: faceList.length,
    faceSummary,
    checks: JSON.parse(checks),
    fontResponseSummary: browser.fontResponses.reduce((acc, f) => {
      const key = `${f.status} ${f.url.replace(/^.*\//, "").slice(0, 28)}`;
      acc[key] = (acc[key] ?? 0) + 1;
      return acc;
    }, {}),
    platformFonts: { title: titleFonts, scopeLine: bodyFonts },
    fontResponses: browser.fontResponses,
    httpFails: [...new Set(browser.httpFails)],
    consoleErrors: browser.consoleErrors,
  };
  const outFile = arg("out", "");
  if (outFile) writeFileSync(outFile, JSON.stringify(report, null, 2), "utf8");
  console.log(JSON.stringify({ theme: THEME, faceCount: report.faceCount, faceSummary: report.faceSummary, checks: report.checks, platformFonts: report.platformFonts, fontResponseSummary: report.fontResponseSummary, httpFails: report.httpFails }, null, 2));
  const bad = report.fontResponses.filter((f) => f.status >= 400);
  console.log(bad.length ? `\n✗ 字体请求失败 ${bad.length} 条` : `\n✓ 字体请求全部 2xx（${report.fontResponses.length} 条）`);
} finally {
  await browser.close();
}
