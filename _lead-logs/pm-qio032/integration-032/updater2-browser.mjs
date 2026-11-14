/**
 * QIO 0.3.2 返修轮 · 更新失败按阶段说话（task-7）真实浏览器验收
 *
 * 覆盖：
 *   A 真实浏览器（无 Tauri 更新器）：只点「检查更新」→ 主界面只说「检查更新失败」，
 *     不出现安装失败 / 完全退出 / 文件写入示例；诊断默认折叠。
 *   B 模拟检查请求失败：主行「检查更新失败」+ 原因「更新源请求失败」；点「重试」真的重新检查。
 *   C 模拟下载 / 校验失败：主行「下载更新失败」，按钮「重试下载安装」；点它真的重新下载安装。
 *   D 模拟安装失败且后端已恢复：主行「安装更新失败」，下一步说「应用已恢复运行，可以继续使用」。
 *   E 模拟安装失败且后端未恢复：主行「安装更新失败」，下一步说「完全退出 QIO 再重新启动」，
 *     按钮只做它真正会做的事（重新检查更新），并进全局异常入口。
 *
 * 边界（如实）：B–E 用**注入的 UpdaterApi 替身**驱动真实组件（浏览器里没有 Tauri 更新器，
 * 真实壳里由 Rust 侧实现）；这里只验证界面行为与前端编排，不证明 Windows 安装 / 重开时序。
 * 只读驱动：不改产品代码。
 */
import { appendFileSync, mkdirSync, writeFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const WT = process.env.QIO_UI_WT || 'D:/qio-dev/pm-qio032-integration';
const BASE = process.env.QIO_E2E_BASE || 'http://127.0.0.1:5199';
const OUT = resolve(process.env.QIO_UI_OUT || HERE);
const SHOTS = join(OUT, 'shots');
const CDP_PORT = Number(process.env.QIO_UI_CDP_PORT || '9334');
const RUN_ID = new Date().toISOString().replace(/[-:T.]/g, '').slice(0, 14);
const LOG_FILE = join(OUT, 'updater2-browser.log');

mkdirSync(SHOTS, { recursive: true });
writeFileSync(LOG_FILE, '', 'utf8');

const cdp = await import(pathToFileURL(join(WT, 'scripts', 'verify-pm-qio032', 'cdp.mjs')).href);
const { CdpBrowser, Page, sleep } = cdp;

let browser = null;
let page = null;
const results = [];
const simulated = [];
let stepNo = 0;

function log(line) {
  const text = String(line);
  console.log(text);
  try { appendFileSync(LOG_FILE, text + '\n', 'utf8'); } catch { /* ignore */ }
}
function record(name, ok, detail) {
  const entry = { name, ok: Boolean(ok), detail: detail === undefined ? null : detail };
  results.push(entry);
  let body = '';
  if (detail !== undefined) { try { body = ' :: ' + JSON.stringify(detail).slice(0, 1500); } catch { body = ''; } }
  log('[' + (entry.ok ? 'PASS' : 'FAIL') + '] ' + name + body);
  return entry.ok;
}
function must(cond, message) { if (!cond) throw new Error(message); }
async function shot(name) {
  stepNo += 1;
  const file = join(SHOTS, String(stepNo).padStart(2, '0') + '-' + name + '.png');
  await page.screenshot(file);
  return file;
}
async function waitExpr(expression, timeout, label) {
  const deadline = Date.now() + (timeout || 20000);
  let last = null;
  while (Date.now() < deadline) {
    try { last = await page.eval('(function(){ try { return (' + expression + '); } catch (e) { return null; } })()'); }
    catch { last = 'ERR'; }
    if (last) return last;
    await sleep(200);
  }
  throw new Error('等待超时（' + (label || expression).slice(0, 70) + '），最后值=' + JSON.stringify(last));
}
const exists = (sel) => page.exists(sel);
const click = (sel, i) => page.click(sel, i || 0, { timeout: 20000 });

async function dismissOnboarding() {
  const visible = await page.eval("(function(){ const el=document.querySelector('.onboarding'); if(!el) return false; const st=getComputedStyle(el); return st.display !== 'none' && st.visibility !== 'hidden'; })()");
  if (!visible) return { wasOpen: false };
  await click('.onboarding-close').catch(() => null);
  await sleep(600);
  const ask = await page.eval("(function(){ const el=document.querySelector('.onboarding-close-ask'); return Boolean(el && el.offsetParent !== null); })()");
  if (ask) {
    await page.eval("(function(){ const btns=[...document.querySelectorAll('.onboarding-close-ask button')]; const b=btns.find(function(x){ return (x.innerText||'').indexOf('关闭并丢弃')>=0; }); if(b){ b.click(); return true; } return false; })()");
    await sleep(700);
  }
  return { wasOpen: true };
}

async function openSettingsAbout() {
  if (!(await exists('[data-testid="settings-page"]'))) {
    const ok = await page.eval('(function(){ const b=[...document.querySelectorAll("button")].find(function(x){ const l=x.getAttribute("aria-label")||""; return l.indexOf("设置")===0; }); if(!b) return false; b.click(); return true; })()');
    must(ok, '找不到设置入口');
    await waitExpr("document.querySelector('[data-testid=\"settings-page\"]')", 20000, '设置页');
    await sleep(700);
  }
  await click('[data-settings-nav-tab="about"]');
  await sleep(900);
  await waitExpr("document.querySelector('.update-card')", 20000, '更新卡片');
}

/**
 * 把注入的 UpdaterApi 替身装进**真实**的 updater store（页面里同一个 pinia 实例）。
 * 浏览器里没有 Tauri 更新器，所以这里模拟的是「壳里 Rust 侧的行为」，界面与编排都是真的。
 */
async function installFakeApi(mode) {
  const script = `(async function(){
    const mod = await import('/src/stores/updater.ts');
    const svc = await import('/src/services/updater.ts');
    const store = mod.useUpdaterStore();
    const probe = { mode: ${JSON.stringify(mode)}, calls: [] };
    window.__qioProbe = probe;
    const fail = (message) => Promise.reject(new Error(message));
    const api = {
      currentVersion: async () => '0.3.2',
      check: async () => {
        probe.calls.push('check');
        if (probe.mode === 'check-network') throw new Error('failed to fetch: network unreachable');
        return { version: '9.9.9', notes: '模拟更新' };
      },
      downloadAndInstall: async (onProgress, options) => {
        probe.calls.push('downloadAndInstall');
        options && options.onStage && options.onStage('downloading');
        onProgress && onProgress({ downloaded: 50, total: 100, percent: 50 });
        if (probe.mode === 'download-fail') throw new Error('signature verification failed');
        options && options.onStage && options.onStage('verifying');
        onProgress && onProgress({ downloaded: 100, total: 100, percent: 100 });
        options && options.onStage && options.onStage('preparing');
        options && options.onStage && options.onStage('installing');
        if (probe.mode === 'install-unrecovered') {
          throw new svc.UpdateFailureError('更新没有安装成功，且后端未能自动恢复', {
            kind: 'installer',
            stage: 'install',
            detail: '后端未能自动恢复：sidecar spawn failed；ShellExecute failed',
            recovery: 'unrecovered',
          });
        }
        throw new svc.UpdateFailureError('更新没有安装成功；应用已恢复运行，可以重试', {
          kind: 'installer',
          stage: 'install',
          detail: '后端已重新启动；ShellExecute failed',
          recovery: 'restored',
        });
      },
      relaunch: async () => { probe.calls.push('relaunch'); },
    };
    store.configure(api);
    return { ok: true, phase: store.phase };
  })()`;
  return page.eval(script);
}

async function readCard() {
  return page.eval(`(function(){
    const card = document.querySelector('.update-card');
    if (!card) return { missing: true };
    const failure = card.querySelector('[data-update-failure]');
    const reason = card.querySelector('[data-update-reason]');
    const next = card.querySelector('[data-update-next]');
    const diag = card.querySelector('[data-update-diagnostics]');
    const retryInstall = card.querySelector('[data-action="retry-install"]');
    const checkBtn = card.querySelector('[data-action="check"]');
    const cardText = (card.innerText || '');
    return {
      failure: failure ? (failure.innerText || '').trim() : null,
      reason: reason ? (reason.innerText || '').trim() : null,
      next: next ? (next.innerText || '').trim() : null,
      hasDiagnostics: Boolean(diag),
      diagnosticsOpen: diag ? diag.hasAttribute('open') : null,
      diagnosticsText: diag ? (diag.innerText || '').trim().slice(0, 400) : null,
      retryInstall: retryInstall ? (retryInstall.innerText || '').trim() : null,
      checkButton: checkBtn ? (checkBtn.innerText || '').trim() : null,
      cardHasInstallFailureCopy: cardText.indexOf('更新没有安装') >= 0,
      cardHasQuitCopy: cardText.indexOf('完全退出') >= 0,
      cardHasWriteExample: cardText.indexOf("Can't write") >= 0 || cardText.indexOf('qio-backend.exe') >= 0,
      cardText: cardText.slice(0, 600),
    };
  })()`);
}

async function probeCalls() {
  return page.eval('(function(){ const p = window.__qioProbe; if (!p) return null; const out = {}; for (const c of p.calls) out[c] = (out[c] || 0) + 1; return out; })()');
}

/**
 * 回到「设置 → 关于与更新」并清空更新状态（不重载页面：重载会丢掉已注入的替身，
 * 也会让设置页重新挂载、状态不可控）。
 */
async function resetToAbout() {
  await page.eval(`(async function(){
    const mod = await import('/src/stores/updater.ts');
    const store = mod.useUpdaterStore();
    store.$reset();
    return true;
  })()`);
  await sleep(300);
  if (!(await exists('[data-testid="settings-page"]'))) {
    await openSettingsAbout();
    return;
  }
  await click('[data-settings-nav-tab="about"]');
  await sleep(700);
  await waitExpr("document.querySelector('.update-card')", 20000, '更新卡片');
}

async function scenarioA() {
  log('');
  log('---- A 真实浏览器（无 Tauri 更新器）：只点检查更新 ----');
  await resetToAbout();
  await click('[data-action="check"]');
  await waitExpr("document.querySelector('[data-update-failure]')", 20000, '失败行');
  await sleep(600);
  const facts = await readCard();
  record('A-1 主行只说「检查更新失败」', facts.failure === '检查更新失败', facts);
  record('A-2 失败块不出现安装失败 / 完全退出 / 文件写入示例',
    facts.cardHasInstallFailureCopy === false && facts.cardHasQuitCopy === false && facts.cardHasWriteExample === false,
    { install: facts.cardHasInstallFailureCopy, quit: facts.cardHasQuitCopy, write: facts.cardHasWriteExample });
  record('A-3 下一步是重试重新检查', Boolean(facts.next) && facts.next.indexOf('重试') >= 0 && facts.next.indexOf('重新检查') >= 0, { next: facts.next });
  record('A-4 诊断默认折叠且含原始错误',
    facts.hasDiagnostics === true && facts.diagnosticsOpen === false && Boolean(facts.diagnosticsText),
    { open: facts.diagnosticsOpen, text: facts.diagnosticsText });
  await shot('A-check-failed-real-browser');
}

async function scenarioB() {
  log('');
  log('---- B 模拟检查请求失败 ----');
  await resetToAbout();
  await installFakeApi('check-network');
  await click('[data-action="check"]');
  await waitExpr("document.querySelector('[data-update-failure]')", 20000, '失败行');
  await sleep(600);
  const facts = await readCard();
  const before = await probeCalls();
  record('B-1 主行「检查更新失败」+ 原因「更新源请求失败」',
    facts.failure === '检查更新失败' && Boolean(facts.reason) && facts.reason.indexOf('更新源请求失败') >= 0,
    { failure: facts.failure, reason: facts.reason });
  record('B-2 失败块不出现安装失败 / 完全退出 / 文件写入示例',
    facts.cardHasInstallFailureCopy === false && facts.cardHasQuitCopy === false && facts.cardHasWriteExample === false,
    { install: facts.cardHasInstallFailureCopy, quit: facts.cardHasQuitCopy, write: facts.cardHasWriteExample });
  // 折叠的 <details> 里 innerText 为空（内容不可见），这里读 textContent 才能看到诊断正文
  const diagBody = await page.eval("(function(){ const d = document.querySelector('[data-update-diagnostics]'); if (!d) return null; const ps = [...d.querySelectorAll('p')].map(function(p){ return (p.textContent||'').trim(); }); return { open: d.hasAttribute('open'), lines: ps }; })()");
  record('B-3 诊断默认折叠，原始错误只在里面',
    facts.hasDiagnostics === true && facts.diagnosticsOpen === false && Boolean(diagBody) && diagBody.lines.join(' ').indexOf('network unreachable') >= 0,
    { open: facts.diagnosticsOpen, diagBody });
  await shot('B-check-failed-simulated');

  await click('[data-action="check"]');
  await sleep(1500);
  const after = await probeCalls();
  const beforeCheck = (before && before.check) || 0;
  const afterCheck = (after && after.check) || 0;
  record('B-4 点「重试」真的重新检查（check 调用 +1，没有下载安装）',
    afterCheck === beforeCheck + 1 && !(after && after.downloadAndInstall),
    { beforeCheck, afterCheck, calls: after });
  const facts2 = await readCard();
  record('B-5 重试后仍是「检查更新失败」（不跳到安装说明）', facts2.failure === '检查更新失败', { failure: facts2.failure });
}

async function scenarioC() {
  log('');
  log('---- C 模拟下载 / 校验失败 ----');
  await resetToAbout();
  await installFakeApi('download-fail');
  await click('[data-action="check"]');
  await waitExpr("document.querySelector('[data-action=\"download\"]')", 20000, '下载按钮');
  await click('[data-action="download"]');
  await waitExpr("document.querySelector('[data-update-failure]')", 20000, '失败行');
  await sleep(600);
  const facts = await readCard();
  const before = await probeCalls();
  record('C-1 主行「下载更新失败」+ 原因「更新包校验未通过」',
    facts.failure === '下载更新失败' && Boolean(facts.reason) && facts.reason.indexOf('更新包校验未通过') >= 0,
    { failure: facts.failure, reason: facts.reason });
  record('C-2 下一步给「重试下载安装」，不说完全退出 / 不说已恢复',
    Boolean(facts.next) && facts.next.indexOf('重试下载安装') >= 0 && facts.next.indexOf('完全退出') < 0 && facts.next.indexOf('可以继续使用') < 0,
    { next: facts.next });
  record('C-3 主按钮是「重试下载安装」（不是重新检查）', facts.retryInstall === '重试下载安装', { retryInstall: facts.retryInstall, checkButton: facts.checkButton });
  await shot('C-download-failed');

  await click('[data-action="retry-install"]');
  await sleep(2000);
  const after = await probeCalls();
  const beforeDl = (before && before.downloadAndInstall) || 0;
  const afterDl = (after && after.downloadAndInstall) || 0;
  record('C-4 点「重试下载安装」真的重新下载安装（downloadAndInstall +1）',
    afterDl === beforeDl + 1,
    { beforeDl, afterDl, calls: after });
}

async function scenarioD() {
  log('');
  log('---- D 模拟安装失败且后端已恢复 ----');
  await resetToAbout();
  await installFakeApi('install-restored');
  await click('[data-action="check"]');
  await waitExpr("document.querySelector('[data-action=\"download\"]')", 20000, '下载按钮');
  await click('[data-action="download"]');
  await waitExpr("document.querySelector('[data-update-failure]')", 20000, '失败行');
  await sleep(600);
  const facts = await readCard();
  record('D-1 主行「安装更新失败」', facts.failure === '安装更新失败', { failure: facts.failure, reason: facts.reason });
  record('D-2 下一步说「应用已恢复运行，可以继续使用」并给重试下载安装',
    Boolean(facts.next) && facts.next.indexOf('应用已恢复运行') >= 0 && facts.next.indexOf('可以继续使用') >= 0 && facts.next.indexOf('重试下载安装') >= 0,
    { next: facts.next });
  const diagBodyD = await page.eval("(function(){ const d = document.querySelector('[data-update-diagnostics]'); if (!d) return null; const ps = [...d.querySelectorAll('p')].map(function(p){ return (p.textContent||'').trim(); }); return { open: d.hasAttribute('open'), lines: ps }; })()");
  record('D-3 诊断默认折叠，原始原因（ShellExecute）只在里面',
    facts.hasDiagnostics === true && facts.diagnosticsOpen === false && Boolean(diagBodyD) && diagBodyD.lines.join(' ').indexOf('ShellExecute failed') >= 0,
    { open: facts.diagnosticsOpen, diagBody: diagBodyD });
  await shot('D-install-failed-restored');
}

async function scenarioE() {
  log('');
  log('---- E 模拟安装失败且后端未恢复 ----');
  await resetToAbout();
  await installFakeApi('install-unrecovered');
  await click('[data-action="check"]');
  await waitExpr("document.querySelector('[data-action=\"download\"]')", 20000, '下载按钮');
  await click('[data-action="download"]');
  await waitExpr("document.querySelector('[data-update-failure]')", 20000, '失败行');
  await sleep(900);
  const facts = await readCard();
  record('E-1 主行「安装更新失败」', facts.failure === '安装更新失败', { failure: facts.failure, reason: facts.reason });
  record('E-2 下一步说「完全退出 QIO 再重新启动」，不说可以继续使用',
    Boolean(facts.next) && facts.next.indexOf('完全退出 QIO 再重新启动') >= 0 && facts.next.indexOf('可以继续使用') < 0,
    { next: facts.next });
  record('E-3 没有「重试下载安装」按钮；主按钮只做它真正会做的事（重新检查更新）',
    facts.retryInstall === null && facts.checkButton === '重新检查更新',
    { retryInstall: facts.retryInstall, checkButton: facts.checkButton });
  // 全局异常入口只挂在对话页 / 互动页的顶部栏（设置页不渲染），所以这里读 store 的真实状态，
  // 再回到对话页确认入口真的出现。
  const issueState = await page.eval(`(async function(){
    const mod = await import('/src/stores/globalIssues.ts');
    const store = mod.useGlobalIssuesStore();
    const issue = store.issueById('updater_install_failed');
    return issue ? { title: issue.title, reason: issue.reason, severity: issue.severity, diagnostics: Array.from(issue.diagnostics || []) } : null;
  })()`);
  record('E-4 进全局异常入口（store 里有 updater_install_failed，带原因与诊断）',
    Boolean(issueState) && issueState.severity === 'error' && issueState.reason.indexOf('更新没有安装成功') >= 0 && issueState.diagnostics.join(' ').indexOf('sidecar spawn failed') >= 0,
    { issueState });
  await shot('E-install-failed-unrecovered');
  // 回到对话页：顶部全局栏应出现「问题」入口
  await click('[data-testid="settings-back"]');
  await waitExpr("!document.querySelector('[data-testid=\"settings-page\"]')", 20000, '离开设置页');
  await sleep(1200);
  const entry = await page.eval("(function(){ const el = document.querySelector('[data-uf-issue-entry]'); return el ? { text: (el.innerText||'').trim(), label: el.getAttribute('aria-label') } : null; })()");
  record('E-5 对话页顶部栏出现「问题」入口（数量与可访问名称完整）',
    Boolean(entry) && entry.text.indexOf('问题') >= 0 && Boolean(entry.label),
    { entry });
  await shot('E-global-issue-entry');
}

async function runScenario(id, label, fn) {
  log('');
  log('---- ' + id + ' ' + label + ' ----');
  try { await fn(); }
  catch (error) {
    record(id + ' 场景未跑完', false, { error: String((error && error.stack) || error).slice(0, 1500) });
  }
}

async function main() {
  log('== QIO updater2 ui-verify == runId=' + RUN_ID);
  log('base=' + BASE + ' cdp=' + CDP_PORT);
  browser = await CdpBrowser.launch({ port: CDP_PORT });
  page = await Page.create(browser);
  await page.setViewport(1440, 900);
  await page.goto(BASE + '/?updater2=' + RUN_ID);
  await waitExpr('document.querySelector(".stream") && document.querySelector("#composer-mode-switch .mode-btn")', 60000, '应用就绪');
  await sleep(900);
  const onboarding = await dismissOnboarding();
  log('onboarding=' + JSON.stringify(onboarding));
  await sleep(600);
  simulated.push('B–E 用注入的 UpdaterApi 替身驱动真实组件（浏览器里没有 Tauri 更新器；真实壳里由 Rust 侧实现）');
  await runScenario('A', '真实浏览器：只点检查更新', scenarioA);
  await runScenario('B', '模拟检查请求失败', scenarioB);
  await runScenario('C', '模拟下载 / 校验失败', scenarioC);
  await runScenario('D', '模拟安装失败且后端已恢复', scenarioD);
  await runScenario('E', '模拟安装失败且后端未恢复', scenarioE);
}

let fatal = null;
try { await main(); }
catch (error) { fatal = error; record('主流程未跑完', false, { error: String((error && error.stack) || error).slice(0, 2000) }); }
finally {
  try { if (browser) await browser.close(); } catch { /* ignore */ }
  const pass = results.filter((r) => r.ok).length;
  const fail = results.filter((r) => !r.ok).length;
  const summary = {
    runId: RUN_ID, base: BASE, cdpPort: CDP_PORT, out: OUT,
    pass, fail, total: results.length,
    results, simulated,
    consoleErrors: page ? page.consoleErrors.slice(0, 40) : [],
    note: '真实界面驱动：系统 Edge + CDP；B–E 的更新器是注入替身，只验证界面行为与前端编排。',
  };
  writeFileSync(join(OUT, 'updater2-browser-summary.json'), JSON.stringify(summary, null, 2), 'utf8');
  log('');
  log('== 结果：PASS ' + pass + ' / FAIL ' + fail + ' / TOTAL ' + results.length + ' ==');
  if (fatal) log('FATAL=' + String((fatal && fatal.message) || fatal));
  process.exit(fail > 0 || fatal ? 1 : 0);
}
