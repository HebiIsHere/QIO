/**
 * QIO #46 第八轮（R8）浏览器验收 harness（真实 Edge + CDP + 真实隔离后端）。
 *
 * 覆盖任务书第 1、2、5、6、7、8、9-12 项；驱动口径：
 *  - 提交 / 编辑 / 完成编辑 / 撤回 / 重试 / 批量导入一律走**真实界面按钮**（按钮先断言 exists/visible/可点）；
 *  - 板面初始选中与「切到另一块已加载的板」使用 store 的公开入口 selectBoard()（任务书允许，
 *    并在每条 detail 里注明 driving 字段）；
 *  - 失败与迟到回执用 CDP Fetch 闸门注入（gate / fulfill），detail 里区分「注入的」与「真实回执」。
 *
 * 环境变量（由 env-up-r8.ps1 起服务后传入）：
 *  QIO_E2E_BASE / QIO_E2E_API / QIO_E2E_PROVIDER / QIO_UI_OUT / QIO_UI_DATA_DIR /
 *  QIO_UI_CDP_PORT / QIO_UI_WT(默认 q46-r8) / Q48_ONLY(逗号分隔场景 id)
 */
process.env.QIO_UI_WT = process.env.QIO_UI_WT || 'D:/qio-dev/q46-r8';
process.env.QIO_BACKEND_ROOT = process.env.QIO_BACKEND_ROOT || 'D:/qio-dev/q46-r8';
process.env.QIO_UI_CDP_PORT = process.env.QIO_UI_CDP_PORT || '9334';

const H = await import('../../verify/lib.mjs');
const L = await import('../../verify/lib47.mjs');
const L48 = await import('./lib48.mjs');
import { mkdirSync, writeFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const OUT = resolve(process.env.QIO_UI_OUT || join(HERE, '..', 'run'));
const ONLY = String(process.env.Q48_ONLY || '')
  .split(',')
  .map((s) => s.trim())
  .filter(Boolean);
const RUN = Date.now().toString(36);
const STAMP = new Date().toISOString();
const ID = (s) => 'r8' + s + '_' + RUN;

function unverified(ctx, name, detail) {
  return ctx.record(name, false, detail, { observe: true });
}

/**
 * 给可能因页面卡死而永远不返回的驱动步骤套一层超时（CDP Runtime.evaluate 默认 60s 超时，
 * 太久了；这里 20s 就判「页面失去响应」，并继续记录证据而不是让整个场景崩掉）。
 */
async function driveStep(label, fn, ms = 20000) {
  const started = Date.now();
  const TIMEOUT = { __timeout: true };
  const p = Promise.resolve()
    .then(fn)
    .catch((e) => ({ __error: String((e && e.message) || e).slice(0, 300) }));
  const v = await Promise.race([p, H.sleep(ms).then(() => TIMEOUT)]);
  if (v === TIMEOUT) return { label, ok: false, timeout: true, ms: Date.now() - started };
  if (v && v.__error) return { label, ok: false, error: v.__error, ms: Date.now() - started };
  return { label, ok: true, value: v, ms: Date.now() - started };
}

/** 页面是否仍能同步执行 JS（浏览器是否卡死）。返回 { ok, ms }。 */
async function probeResponsive(ctx, ms = 5000) {
  const started = Date.now();
  try {
    const value = await Promise.race([
      ctx.page.eval('(function(){ return 21 * 2; })()'),
      H.sleep(ms).then(() => 'TIMEOUT'),
    ]);
    return { ok: value === 42, value, ms: Date.now() - started };
  } catch (e) {
    return { ok: false, error: String(e).slice(0, 200), ms: Date.now() - started };
  }
}

/* ================================================================== */
/* s1：第 1 项 —— 提交始终回到用户点击时的板面（B 提交、切 A、放行 B  */ 
/* 保存；以及 B 的提交回执在 A 打开时才到）                            */
/* ================================================================== */
async function s1(ctx) {
  const A = ID('a_s1');
  const B = ID('b_s1');
  await L.seedBoard(A, [L.card('ca1', 'A 原正文（提交归属测试）')]);
  await L.seedBoard(B, [L.card('cb1', 'B 原正文（提交归属测试）')]);
  const subsA0 = await L48.submissionCount(A);
  const subsB0 = await L48.submissionCount(B);

  await ctx.gotoApp('r8s1=' + RUN);
  await ctx.enterInteractive();
  ctx.clearNetwork();
  const openB = await L48.openBoardReady(ctx, B);
  const bSeq0 = openB.state ? openB.state.seq : null;
  const since = Date.now();

  // 1) 挂起 B 的 saveBoardState PUT
  const saveGate = L48.gate(ctx, (u, m) => m === 'PUT' && u.indexOf('/boards/' + B + '/state') >= 0, 'B 保存 PUT');
  // 2) B 编辑一张卡并「完成编辑」→ 触发正式保存（被闸门挂起）
  const typed = await L.editCardText(ctx, 0, 'B 新正文·提交归属（应只回 B）');
  const finish = await L.finishEditing(ctx);
  let saveHeld = null;
  try {
    saveHeld = await saveGate.waitForHeld(20000);
  } catch (e) {
    saveHeld = null;
  }
  // 3) 用真实「提交给 QIO」按钮发起提交（此时 B 的保存还在飞）
  const submitBtn = await L.ui(ctx, '[data-im="submit"]');
  const submitClick = await L.clickAsserted(ctx, '[data-im="submit"]', { timeout: 15000 });
  await H.sleep(1200);
  // 4) 用公开入口切到已加载的 A（driving 记录在 detail）
  const switchA = await ctx.storeEval('return await s.selectBoard(' + JSON.stringify(A) + ');').catch((e) => ({ error: String(e) }));
  await L48
    .waitFor(async () => {
      const st = await ctx.storeState();
      return st.boardId === A ? st : null;
    }, 20000, '切到 A')
    .catch(() => null);
  const aStateBefore = await ctx.storeState();
  // 5) 放行 B 的保存
  const released = await saveGate.releaseAll({ kind: 'pass' });
  // 6) 等提交请求与回执落定
  const post = await L48
    .waitFor(() => ctx.network.find((r) => r.method === 'POST' && r.url.indexOf('/submissions') >= 0), 25000, '提交 POST')
    .catch(() => null);
  await H.sleep(6000);
  const posts = ctx.requests({ method: 'POST', contains: '/submissions', since });
  const postBody = post ? L48.bodyOf(post) : null;
  const putsB = ctx.requests({ method: 'PUT', contains: '/boards/' + B + '/state', since });
  const bAfter = await L48.backendBoard(B);
  const subsA1 = await L48.submissionCount(A);
  const subsB1 = await L48.submissionCount(B);
  const aAfter = await ctx.storeState();
  const aCardText = ((bAfter.state.cards || []) || []).map((c) => String(c.content || '')).join('|');
  const bSavedNewText = aCardText.indexOf('B 新正文·提交归属') >= 0;
  const aCardContent = (aAfter.cards || []).map((c) => String(c.content || ''));

  const urlTargetsB = Boolean(post) && post.url.indexOf('/boards/' + B + '/submissions') >= 0;
  const versionIsB = Boolean(postBody) && Number(postBody.baseStateVersion) === Number(bAfter.seq);
  const bPlusOne = subsB1 === subsB0 + 1;
  const aUnchanged = subsA1 === subsA0;
  const stillOnA = aAfter.boardId === A;
  const aNotOverwritten = aCardContent.some((t) => t.indexOf('A 原正文') >= 0) && !aCardContent.some((t) => t.indexOf('B 新正文') >= 0);
  const ok = submitClick.ok && Boolean(saveHeld) && Boolean(post) && urlTargetsB && versionIsB && bPlusOne && aUnchanged && stillOnA && aNotOverwritten && bSavedNewText;
  ctx.record('1) B 提交时切到已加载的 A：提交请求只针对 B、提交体 seq 是 B 的保存版本；回读 B 提交 +1、A 不变；A 内容不被覆盖', ok, {
    driving: {
      submit: 'real-button [data-im=submit]',
      switchToA: 'store.selectBoard(A)（公开入口；任务书允许组合时调用同一公开入口）',
      hold: 'CDP Fetch 挂起指头/save PUT（injected）',
    },
    boards: { A, B },
    submitBtn: submitBtn && { exists: submitBtn.exists, visible: submitBtn.visible, disabled: submitBtn.disabled, text: submitBtn.text },
    submitClick: { ok: submitClick.ok, reason: submitClick.reason || null },
    typed,
    finish,
    saveHeld: Boolean(saveHeld),
    switchA,
    aStateBefore: { boardId: aStateBefore.boardId, seq: aStateBefore.seq, cards: (aStateBefore.cards || []).map((c) => c.content) },
    releasedCount: (released || []).length,
    post: post ? { url: post.url.replace(H.API, ''), status: post.status } : null,
    postBody,
    posts: L48.brief(posts),
    putsB: L48.brief(putsB),
    bAfter: { seq: bAfter.seq, cards: (bAfter.state.cards || []).map((c) => ({ id: c.id, content: String(c.content).slice(0, 80) })) },
    subsA0, subsA1, subsB0, subsB1,
    bSavedNewText, urlTargetsB, versionIsB, bPlusOne, aUnchanged, stillOnA, aNotOverwritten,
  });
  await ctx.shot('s1-submit-stays-on-B');

  /* ---------- 1b：B 的提交回执在 A 打开时才到 ---------- */
  const A2 = ID('a_s1b');
  const B2 = ID('b_s1b');
  await L.seedBoard(A2, [L.card('ca1b', 'A2 原正文（迟到回执）')]);
  await L.seedBoard(B2, [L.card('cb1b', 'B2 原正文（迟到回执）')]);
  await ctx.gotoApp('r8s1b=' + RUN);
  await ctx.enterInteractive();
  ctx.clearNetwork();
  const openB2 = await L48.openBoardReady(ctx, B2);
  const since2 = Date.now();
  // 放行 B2 保存；挂起 B2 的提交 POST → 切到 A2 后再放行（回执在 A2 打开时到达）
  const postGate = L48.gate(ctx, (u, m) => m === 'POST' && u.indexOf('/boards/' + B2 + '/submissions') >= 0, 'B2 提交 POST');
  await L.editCardText(ctx, 0, 'B2 新正文·迟到回执');
  await L.finishEditing(ctx);
  await H.sleep(2000);
  const submitClick2 = await L.clickAsserted(ctx, '[data-im="submit"]', { timeout: 15000 });
  let postHeld = null;
  try {
    postHeld = await postGate.waitForHeld(25000);
  } catch (e) {
    postHeld = null;
  }
  const a2Before = await L48.storeSubmitFacts(ctx);
  const switchA2 = await ctx.storeEval('return await s.selectBoard(' + JSON.stringify(A2) + ');').catch((e) => ({ error: String(e) }));
  await L48
    .waitFor(async () => {
      const st = await ctx.storeState();
      return st.boardId === A2 ? st : null;
    }, 20000, '切到 A2')
    .catch(() => null);
  const releasedPost = await postGate.releaseAll({ kind: 'pass' });
  // 等回执真正到达（POST 响应 + 理解查询）
  const postResp = await L48
    .waitFor(() => ctx.network.find((r) => r.method === 'POST' && r.url.indexOf('/boards/' + B2 + '/submissions') >= 0 && r.status), 30000, '提交响应')
    .catch(() => null);
  await H.sleep(5000);
  const a2After = await L48.storeSubmitFacts(ctx);
  const a2SubmitStatus = a2After.submitStatus;
  const a2HasReceipt = String(a2SubmitStatus) !== 'idle' && String(a2SubmitStatus) !== 'undefined';
  // 回 B2：回执应归 B2（回执到达时 B2 是原板）
  const backB2 = await ctx.storeEval('return await s.selectBoard(' + JSON.stringify(B2) + ');').catch((e) => ({ error: String(e) }));
  await H.sleep(2500);
  const b2State = await L48.storeSubmitFacts(ctx);
  const b2GotReceipt = ['succeeded', 'empty', 'duplicate', 'unknown', 'failed'].indexOf(String(b2State.submitStatus)) >= 0;
  const ok1b = submitClick2.ok && Boolean(postHeld) && Boolean(postResp) && a2After.boardId === A2 && !a2HasReceipt && b2GotReceipt;
  ctx.record('1b) B 的提交回执在 A 打开时才到：A 的提交状态不被 B 的回执改写；回 B 能看到这次回执（归属原板）', ok1b, {
    driving: { submit: 'real-button', switchToA: 'store.selectBoard(A2)', hold: 'CDP Fetch 挂起 B2 的 POST /submissions 请求，切到 A2 后再放行（注入时序，回执来自真实后端）' },
    boards: { A2, B2 },
    submitClick2: { ok: submitClick2.ok, reason: submitClick2.reason || null },
    postHeld: Boolean(postHeld),
    releasedPost: (releasedPost || []).length,
    postResp: postResp ? { status: postResp.status } : null,
    a2Before: { boardId: a2Before.boardId, submitStatus: a2Before.submitStatus, submitNote: a2Before.submitNote },
    a2After: a2After,
    a2SubmitStatus,
    b2State: b2State,
    b2GotReceipt,
  });
  await ctx.shot('s1b-late-submit-receipt');
}

/* ================================================================== */
/* s2：第 2 项 —— 两个真实标签页：旧正式保存不得删除另一标签页的新恢复副本 */
/* ================================================================== */
async function s2(ctx) {
  const B = ID('b_s2');
  const cardId = 'cb2';
  await L.seedBoard(B, [L.card(cardId, 'B 卡片起点（双标签页）')]);
  await ctx.gotoApp('r8s2=' + RUN);
  await ctx.enterInteractive();
  await L48.openBoardReady(ctx, B);
  const tab2 = await L48.openTab(ctx, 'r8s2tab2=' + RUN, { outDir: OUT });
  await tab2.enterInteractive();
  await L48.openBoardReady(tab2, B);
  // 两页都从干净的本机草稿开始（同一浏览器共享 localStorage）
  await L48.clearLocalDrafts(ctx.page);
  await L48.clearLocalDrafts(tab2.page);

  // 页1：进入编辑、输入正式内容 → 完成编辑（正式保存挂起）
  // 多标签页下后台标签的计时器会被节流，驱动前把页1 激活到前台。
  const sinceS2 = Date.now();
  await L48.activate(ctx);
  const saveGate = L48.gate(ctx, (u, m) => m === 'PUT' && u.indexOf('/boards/' + B + '/state') >= 0, '页1正式保存');
  const typed1 = await L.editCardText(ctx, 0, '页1正式内容（双标签页）');
  await H.sleep(800); // 让草稿先同步到后端
  const finish1 = await L.finishEditing(ctx);
  let saveHeld = null;
  try {
    saveHeld = await saveGate.waitForHeld(20000);
  } catch (e) {}
  const afterFinish1 = await L.snapshot(ctx);
  const puts1 = ctx.requests({ method: 'PUT', contains: '/boards/' + B + '/state', since: sinceS2 });
  // 页2：输入新草稿，且其草稿 PUT 被挂起（暂未同步到后端）
  await L48.activate(tab2);
  const draftGate = L48.gate(tab2, (u, m) => m === 'PUT' && u.indexOf('/api/interactive/drafts/' + B) >= 0, '页2草稿同步');
  const typed2 = await L.editCardText(tab2, 0, '页2新草稿·未同步（旧回执不许删）');
  let draftHeld = null;
  try {
    draftHeld = await draftGate.waitForHeld(15000);
  } catch (e) {}
  await H.sleep(400);
  const beforeLocal = await L48.readLocalRecord(tab2.page, cardId);
  const beforeTab2 = await tab2.storeState();
  // 放行页1的保存回执 → 旧正式保存成功，可能错误清理本机恢复副本
  await saveGate.releaseAll({ kind: 'pass' });
  await H.sleep(5000);
  const afterLocal = await L48.readLocalRecord(tab2.page, cardId);
  const afterTab2 = await tab2.storeState();
  const backendDraft = await L48.backendDrafts(B);
  const bAfter = await L48.backendBoard(B);
  await ctx.shot('s2-two-tabs-local-record');

  const textOf = (r) => (r && typeof r.text === 'string' ? r.text : '');
  const recordKept = Boolean(afterLocal) && textOf(afterLocal).indexOf('页2新草稿') >= 0;
  const versionKept = Boolean(afterLocal) && typeof afterLocal.version === 'number' && afterLocal.version > 0;
  const memoryKept = JSON.stringify(afterTab2.drafts || {}).indexOf('页2新草稿') >= 0;
  const editorValueOpen = await tab2.page.eval('(function(){ const el=document.querySelector("[data-im=card-editor]"); return el ? el.value : null; })()');
  // 「重新打开编辑器」的更强路径：页2 整页重载（真实用户重开），只有本机恢复记录能找回新文字
  let reloaded = false;
  let restoredAfterReload = false;
  let editorAfterReload = null;
  try {
    await tab2.page.reload();
    reloaded = true;
    await H.sleep(1200);
    await L48.activate(tab2);
    await tab2.enterInteractive();
    await L48.openBoardReady(tab2, B);
    editorAfterReload = await L48.openCardEditor(tab2, 0);
    restoredAfterReload = Boolean(editorAfterReload && editorAfterReload.ok) && String(editorAfterReload.value || '').indexOf('页2新草稿') >= 0;
  } catch (e) {
    editorAfterReload = { ok: false, error: String(e).slice(0, 300) };
  }

  const ok = Boolean(saveHeld) && Boolean(draftHeld) && recordKept && versionKept && memoryKept;
  ctx.record('2) 两个真实标签页：页1 正式保存回执到达后，页2 尚未同步的新草稿本机恢复副本仍在（正文/版本都在），不要求页2重输', ok, {
    driving: {
      tabs: 'CDP Target.createTarget 同浏览器默认上下文（同源共享 localStorage）',
      tab1FormalSave: '真实「完成编辑」按钮触发正式保存；CDP Fetch 挂起回执',
      tab2Draft: '真实卡片编辑器输入；CDP Fetch 挂起其草稿 PUT（模拟尚未同步）',
      reopen: '页2 再次进入编辑（真实编辑按钮）',
    },
    board: B,
    saveHeld: Boolean(saveHeld),
    saveGateHits: saveGate.hits,
    typed1,
    finish1,
    afterFinish1: { boardId: afterFinish1.boardId, saveStatus: afterFinish1.saveStatus, saveError: afterFinish1.saveError, dirty: afterFinish1.dirty, cards: (afterFinish1.boardCards || []).map((c) => String(c.content).slice(0, 80)) },
    puts1: L48.brief(puts1),
    draftHeld: Boolean(draftHeld),
    typed2,
    beforeLocal,
    beforeTab2: { boardId: beforeTab2.boardId, drafts: beforeTab2.drafts, draftStates: beforeTab2.draftStates },
    afterLocal,
    afterTab2: { boardId: afterTab2.boardId, drafts: afterTab2.drafts, draftStates: afterTab2.draftStates },
    backendDraft: { drafts: backendDraft.drafts, rev: backendDraft.rev },
    bAfter: { seq: bAfter.seq, cards: (bAfter.state.cards || []).map((c) => String(c.content).slice(0, 80)) },
    recordKept,
    versionKept,
    memoryKept,
    editorValueOpen,
    reloaded,
    restoredAfterReload,
    editorAfterReload,
  });
  await ctx.shot('s2b-two-tabs-reopen');

  /* ---------- 2b：页2 新稿已同步后，页1 旧回执不得删除后端更新的草稿 ---------- */
  // 页2 放行草稿同步（真实后端）；重载后重新输入一版并等它真的上传成功
  await draftGate.releaseAll({ kind: 'pass' });
  await H.sleep(600);
  await L48.activate(tab2);
  if (restoredAfterReload) {
    await L.retypeCard(tab2, '页2新草稿·已同步（旧回执不许删）').catch(() => null);
  }
  // 若重载路径没把编辑器带回，退回到在页2 直接输入（仍是真实输入，只是驱动退一步）
  if (!restoredAfterReload) {
    await L.editCardText(tab2, 0, '页2新草稿·已同步（旧回执不许删）').catch(() => null);
  }
  await L48
    .waitFor(async () => {
      const d = await L48.backendDrafts(B);
      return String((d.drafts || {})['card:' + cardId] || '').indexOf('页2新草稿·已同步') >= 0 ? d : null;
    }, 20000, '页2 新稿同步到后端')
    .catch(() => null);
  const backendSynced = await L48.backendDrafts(B);
  const syncedText = String((backendSynced.drafts || {})['card:' + cardId] || '');
  const tab2Synced = syncedText.indexOf('页2新草稿') >= 0;
  // 页1 再做一次正式编辑并挂起回执（它的待清登记基于页1看到的旧版本）
  await L48.activate(ctx);
  const since2b = Date.now();
  const saveGate2 = L48.gate(ctx, (u, m) => m === 'PUT' && u.indexOf('/boards/' + B + '/state') >= 0, '页1第二次正式保存');
  let typed1b = await L.editCardText(ctx, 0, '页1第二次正式内容（双标签页）');
  if (!typed1b || !typed1b.ok) {
    await L48.clickBlankBoardArea(ctx).catch(() => null);
    await H.sleep(500);
    typed1b = await L.editCardText(ctx, 0, '页1第二次正式内容（双标签页）');
  }
  await H.sleep(600);
  const finish1b = await L.finishEditing(ctx);
  let held2 = null;
  try {
    held2 = await saveGate2.waitForHeld(20000);
  } catch (e) {}
  const afterFinish1b = await L.snapshot(ctx);
  const puts2 = ctx.requests({ method: 'PUT', contains: '/boards/' + B + '/state', since: since2b });
  await saveGate2.releaseAll({ kind: 'pass' });
  await H.sleep(5000);
  const backendAfter = await L48.backendDrafts(B);
  const keptText = String((backendAfter.drafts || {})['card:' + cardId] || '');
  const backendNewDraftKept = keptText.indexOf('页2新草稿·已同步') >= 0;
  const b2After = await L48.backendBoard(B);
  const ok2 = tab2Synced && Boolean(held2) && backendNewDraftKept;
  ctx.record('2b) 页2 新稿已同步到后端后，页1 的旧正式保存回执不得清掉后端更新的草稿（API 回读仍是页2的新文字）', ok2, {
    driving: {
      page1SecondSave: '真实「完成编辑」；CDP Fetch 挂起回执后放行（回执来自真实后端）',
      page2Sync: '放行页2 真实草稿 PUT（真实后端接受）',
    },
    backendSynced: { rev: backendSynced.rev, drafts: backendSynced.drafts },
    tab2Synced,
    held2: Boolean(held2),
    saveGate2Hits: saveGate2.hits,
    typed1b,
    finish1b,
    afterFinish1b: { boardId: afterFinish1b.boardId, saveStatus: afterFinish1b.saveStatus, saveError: afterFinish1b.saveError, dirty: afterFinish1b.dirty, cards: (afterFinish1b.boardCards || []).map((c) => String(c.content).slice(0, 80)) },
    puts2: L48.brief(puts2),
    backendAfter: { rev: backendAfter.rev, drafts: backendAfter.drafts },
    keptText,
    backendNewDraftKept,
    b2After: { seq: b2After.seq, cards: (b2After.state.cards || []).map((c) => String(c.content).slice(0, 80)) },
  });
  await ctx.shot('s2c-two-tabs-backend-draft');
  // 清场：关掉第二标签页并把页1 激活回前台（后续场景继续用页1）
  await L48.activate(ctx);
  await L48.closeTab(tab2);
  await H.sleep(500);
}

/* ================================================================== */
/* s3a：第 5 项 —— 一块板的保存失败不得堵住其他板的草稿                 */
/*                                                                     */
/* 驱动口径（与任务书复现等价，注明原因）：让 A 的草稿请求先在飞并挂起   */
/* （失败落点），此时 B 输入草稿只能排队在同一串行队列后面；把 A 的请求   */
/* 放行为 503 后，B 的草稿必须继续被发出。                              */
/* ================================================================== */
async function s3a(ctx) {
  const A = ID('a_s3a');
  const B = ID('b_s3a');
  const C = ID('c_s3a');
  const keyA = 'card:ca3a';
  const keyB = 'card:cb3a';
  const keyC = 'card:cc3a';
  await L.seedBoard(A, [L.card('ca3a', 'A 卡片（第5项）')]);
  await L.seedBoard(B, [L.card('cb3a', 'B 卡片（第5项）')]);
  await L.seedBoard(C, [L.card('cc3a', 'C 卡片（第5项）')]);

  await ctx.gotoApp('r8s3a=' + RUN);
  await ctx.enterInteractive();
  ctx.clearNetwork();
  await L48.openBoardReady(ctx, A);
  const since = Date.now();
  // 1) 只挂起「带本页新文字」的那条 A 草稿 PUT（它就是队列头部的失败落点）
  const typedAText = 'A 草稿·第一次 503（第5项）';
  const aGate = L48.gate(
    ctx,
    (u, m, body) => m === 'PUT' && u.indexOf('/api/interactive/drafts/' + A) >= 0 && String(body || '').indexOf(typedAText) >= 0,
    'A 新草稿 PUT',
    { delayMatch: (u, m) => m === 'PUT' && u.indexOf('/api/interactive/drafts/' + A) >= 0 },
  );
  const typedA = await L.editCardText(ctx, 0, typedAText);
  let aHeld = null;
  try {
    aHeld = await aGate.waitForHeld(20000);
  } catch (e) {}
  // 2) 切到 B 并输入草稿：B 的草稿必须排在 A 的失败请求之后
  const switchB = await ctx.storeEval('return await s.selectBoard(' + JSON.stringify(B) + ');').catch((e) => ({ error: String(e) }));
  await H.sleep(600);
  const typedB = await L.editCardText(ctx, 0, 'B 草稿·排在 A 失败请求之后');
  // 等 B 的防抖到点、它这一轮 flush 真正排在 A 的在飞请求后面，再把 A 放行为 503
  await H.sleep(1600);
  // 3) 放行 A 的请求为 503（注入一次失败；之后同 predicate 直接放行）
  const aRelease = await aGate.releaseFirst({
    kind: 'fulfill',
    status: 503,
    body: { detail: 'harness: 注入的草稿保存失败（503，仅第一次）' },
  });
  await H.sleep(7000);
  const responsive1 = await probeResponsive(ctx);
  const draftsA1 = await L48.backendDrafts(A);
  const draftsB1 = await L48.backendDrafts(B);
  const bSaved = String((draftsB1.drafts || {})[keyB] || '').indexOf('B 草稿·排在') >= 0;
  const aFailed = String((draftsA1.drafts || {})[keyA] || '').indexOf(typedAText) < 0;
  const putsA1 = ctx.requests({ method: 'PUT', contains: '/api/interactive/drafts/' + A, since });
  const putsB1 = ctx.requests({ method: 'PUT', contains: '/api/interactive/drafts/' + B, since });
  const aStateAfterFail = await L.snapshot(ctx);
  // 4) C 的草稿也必须继续（浏览器还能输入 / 自动保存）
  const switchC = await ctx.storeEval('return await s.selectBoard(' + JSON.stringify(C) + ');').catch((e) => ({ error: String(e) }));
  await H.sleep(600);
  const typedC = await L.editCardText(ctx, 0, 'C 草稿·应继续自动保存（第5项）');
  await H.sleep(5000);
  const responsive2 = await probeResponsive(ctx);
  const draftsC1 = await L48.backendDrafts(C);
  const cSaved = String((draftsC1.drafts || {})[keyC] || '').indexOf('C 草稿·应继续') >= 0;
  const putsC1 = ctx.requests({ method: 'PUT', contains: '/api/interactive/drafts/' + C, since });
  // 5) 故障解除后回 A 重试：优先真实按钮，退回到公开 retryDraftSave（注明）
  await ctx.clearRules();
  await H.sleep(400);
  const switchA2 = await ctx.storeEval('return await s.selectBoard(' + JSON.stringify(A) + ');').catch((e) => ({ error: String(e) }));
  await H.sleep(1000);
  const retryUi = await L.ui(ctx, '[data-im="card-draft-retry"]');
  let retryHow = null;
  let retryClick = { ok: false, reason: 'none' };
  if (retryUi.exists && retryUi.visible) {
    retryClick = await L.clickAsserted(ctx, '[data-im="card-draft-retry"]', { timeout: 15000 });
    retryHow = 'real-button [data-im=card-draft-retry]';
  } else {
    const rr = await ctx.storeEval('return await s.retryDraftSave();').catch((e) => ({ error: String(e) }));
    retryClick = { ok: true, reason: 'store.retryDraftSave()（页面没有可见的卡片草稿重试入口）', result: rr };
    retryHow = 'store-public-action';
  }
  await H.sleep(5000);
  const draftsA2 = await L48.backendDrafts(A);
  const aRetriedSaved = String((draftsA2.drafts || {})[keyA] || '').indexOf(typedAText) >= 0;
  const putsA2 = ctx.requests({ method: 'PUT', contains: '/api/interactive/drafts/' + A, since });

  const ok = Boolean(aHeld) && bSaved && cSaved && aFailed && responsive1.ok && responsive2.ok && aRetriedSaved;
  ctx.record('5) 一块板的失败不堵其他板：A 的草稿请求失败（注入 503）后，排在其后的 B 与随后 C 的草稿仍自动保存且各归原板；浏览器仍响应；故障解除后 A 重试补存成功', ok, {
    driving: {
      drafts: '真实卡片编辑器输入（草稿防抖自动保存）',
      switchBoard: 'store.selectBoard(公开入口)',
      failInjection: 'CDP Fetch 挂起 A 的草稿 PUT，切到 B 输入草稿后再以 503 放行（注入失败；请求来自真实页面）',
      retry: retryHow,
    },
    boards: { A, B, C },
    typedA, typedB, typedC,
    aHeld: Boolean(aHeld),
    aRelease: aRelease ? { url: aRelease.url.replace(H.API, ''), method: aRelease.method } : null,
    switchB, switchC, switchA2,
    responsive1, responsive2,
    draftsA1: { rev: draftsA1.rev, drafts: draftsA1.drafts },
    draftsB1: { rev: draftsB1.rev, drafts: draftsB1.drafts },
    draftsC1: { rev: draftsC1.rev, drafts: draftsC1.drafts },
    bSaved, cSaved, aFailed,
    putsA1: L48.brief(putsA1),
    putsB1: L48.brief(putsB1),
    putsC1: L48.brief(putsC1),
    aStateAfterFail: { boardId: aStateAfterFail.boardId, draftStates: aStateAfterFail.draftStates },
    retryUi: { exists: retryUi.exists, visible: retryUi.visible, text: retryUi.text },
    retryHow,
    retryClick: { ok: retryClick.ok, reason: retryClick.reason || null },
    draftsA2: { rev: draftsA2.rev, drafts: draftsA2.drafts },
    aRetriedSaved,
    putsA2: L48.brief(putsA2),
  });
  await ctx.shot('s3a-draft-failure-isolation');
}

/* ================================================================== */
/* s3b：第 6 项 —— 只剩“等用户选择”的冲突板必须被跳过，后面的板继续    */
/* 复现：B 制造真实同键双窗口冲突后，A/C 输入草稿；必须继续发出保存。   */
/* ================================================================== */
async function s3b(ctx) {
  const A = ID('a_s3b');
  const B = ID('b_s3b');
  const C = ID('c_s3b');
  const keyA = 'card:ca3b';
  const keyB = 'card:cb3b';
  const keyC = 'card:cc3b';
  const typedBText = 'B 本页草稿·应冲突（第6项）';
  await L.seedBoard(A, [L.card('ca3b', 'A 卡片（第6项）')]);
  await L.seedBoard(B, [L.card('cb3b', 'B 卡片（第6项）')]);
  await L.seedBoard(C, [L.card('cc3b', 'C 卡片（第6项）')]);

  await ctx.gotoApp('r8s3b=' + RUN);
  await ctx.enterInteractive();
  ctx.clearNetwork();
  await L48.openBoardReady(ctx, B);
  const since = Date.now();
  // 1) 只挂起「带本页新文字」的那条 B 草稿请求（宽谓词交给 cdp 兜底，窄谓词看请求体）
  const bGate = L48.gate(
    ctx,
    (u, m, body) => m === 'PUT' && u.indexOf('/api/interactive/drafts/' + B) >= 0 && String(body || '').indexOf(typedBText) >= 0,
    'B 新草稿 PUT',
    { delayMatch: (u, m) => m === 'PUT' && u.indexOf('/api/interactive/drafts/' + B) >= 0 },
  );
  const typedB = await L.editCardText(ctx, 0, typedBText);
  let bHeld = null;
  try {
    bHeld = await bGate.waitForHeld(20000);
  } catch (e) {}
  // 2) 真实并发写：harness 直接写一版更新的服务端草稿（基于当前 rev）
  const draftsB0 = await L48.backendDrafts(B);
  const serverWrite = await H.apiPut(H.draftsPath(B), {
    mode: 'patch',
    drafts: { [keyB]: 'B 服务器上的另一份（真实并发写）' },
    baseRev: draftsB0.rev,
  });
  // 3) 放行本页请求 → 真实后端返回同键冲突（服务器保留自己那份）
  await bGate.releaseAll({ kind: 'pass' });
  const conflictStep = await driveStep('等待 B 真实同键冲突状态', () =>
    L48.waitFor(async () => {
      const s = await L.snapshot(ctx);
      return s.draftConflicts && s.draftConflicts[keyB] ? s : null;
    }, 15000, 'B 真实同键冲突'),
  );
  const bConflict = conflictStep.ok ? conflictStep.value : null;
  // 4) A 与 C 的草稿：B 只剩「等用户选择」的冲突时，必须继续被保存
  //    页面如果在这一步失去响应（第 6 项的反例现象），每一步都要有超时并留下「卡在哪一步」。
  const stepSwitchA = await driveStep('切到 A', () => ctx.storeEval('return await s.selectBoard(' + JSON.stringify(A) + ');'));
  const stepTypedA = stepSwitchA.ok
    ? await driveStep('在 A 输入草稿', async () => {
        await H.sleep(600);
        return L.editCardText(ctx, 0, 'A 草稿·应继续保存（第6项）');
      })
    : { label: '在 A 输入草稿', ok: false, skipped: true };
  const stepSwitchC = await driveStep('切到 C', () => ctx.storeEval('return await s.selectBoard(' + JSON.stringify(C) + ');'));
  const stepTypedC = stepSwitchC.ok
    ? await driveStep('在 C 输入草稿', async () => {
        await H.sleep(600);
        return L.editCardText(ctx, 0, 'C 草稿·冲突待选时仍应保存（第6项）');
      })
    : { label: '在 C 输入草稿', ok: false, skipped: true };
  const stepWait = await driveStep('等待草稿队列', () => H.sleep(9000).then(() => true), 15000);
  const responsive = await probeResponsive(ctx);
  const hangSteps = [stepSwitchA, stepTypedA, stepSwitchC, stepTypedC, stepWait].filter((s) => s.timeout || s.ok === false);
  const hangDetected = hangSteps.length > 0 || !responsive.ok;
  const stepFacts = [stepSwitchA, stepTypedA, stepSwitchC, stepTypedC, stepWait].map((s) => ({ label: s.label, ok: s.ok, timeout: Boolean(s.timeout), skipped: Boolean(s.skipped), error: s.error || null, ms: s.ms || null, value: s.value || null }));
  const switchA = stepSwitchA.value || null;
  const typedA = stepTypedA.value || null;
  const switchC = stepSwitchC.value || null;
  const typedC = stepTypedC.value || null;
  const draftsA = await L48.backendDrafts(A);
  const draftsB = await L48.backendDrafts(B);
  const draftsC = await L48.backendDrafts(C);
  const aSaved = String((draftsA.drafts || {})[keyA] || '').indexOf('A 草稿·应继续') >= 0;
  const cSaved = String((draftsC.drafts || {})[keyC] || '').indexOf('C 草稿·冲突待选') >= 0;
  const bServerText = String((draftsB.drafts || {})[keyB] || '');
  const bConflictKept = bServerText.indexOf('B 服务器上的另一份') >= 0;
  const putsA = ctx.requests({ method: 'PUT', contains: '/api/interactive/drafts/' + A, since });
  const putsB = ctx.requests({ method: 'PUT', contains: '/api/interactive/drafts/' + B, since });
  const putsC = ctx.requests({ method: 'PUT', contains: '/api/interactive/drafts/' + C, since });
  const bNotResent = putsB.length <= 2;
  // 页面一旦卡死，后续取 store 快照也会 CDP 超时：直接跳过，保留「卡在哪一步」的证据
  const storeLeaf = hangDetected ? null : await L.snapshot(ctx).catch(() => null);
  ctx.log(
    '  [s3b] 分步：' +
      stepFacts.map((s) => s.label + '=' + (s.ok ? 'ok' : s.timeout ? 'TIMEOUT' : 'fail')).join(' / ') +
      '，responsive=' + JSON.stringify(responsive) +
      '，bConflict=' + Boolean(bConflict),
  );
  const ok = Boolean(bHeld) && Boolean(bConflict) && bConflictKept && aSaved && cSaved && responsive.ok && bNotResent && !hangDetected;
  ctx.record('6) 一块板存在草稿冲突时跳过等待用户选择的条目：B 真实同键冲突后，A/C 的草稿仍自动保存；冲突项不反复重发；浏览器仍响应', ok, {
    hangDetected,
    hangSteps: hangSteps.map((s) => ({ label: s.label, timeout: Boolean(s.timeout), error: s.error || null })),
    stepFacts,
    driving: {
      drafts: '真实卡片编辑器输入（草稿防抖自动保存）',
      conflict: 'CDP Fetch 挂起带本页新文字的 B 草稿 PUT + harness 直接写真实后端更新版本（真实并发冲突）',
      switchBoard: 'store.selectBoard(公开入口)',
    },
    boards: { A, B, C },
    bHeld: Boolean(bHeld),
    draftsB0: { rev: draftsB0.rev, drafts: draftsB0.drafts },
    serverWrite: { status: serverWrite.status, rev: serverWrite.data && serverWrite.data.rev, conflicts: serverWrite.data && serverWrite.data.conflicts },
    bConflict: bConflict ? { conflicts: bConflict.draftConflicts, states: bConflict.draftStates } : null,
    conflictStep: { ok: conflictStep.ok, timeout: Boolean(conflictStep.timeout), error: conflictStep.error || null },
    typedA, typedC, typedB,
    switchA, switchC,
    responsive,
    draftsA: { rev: draftsA.rev, drafts: draftsA.drafts },
    draftsB: { rev: draftsB.rev, drafts: draftsB.drafts },
    draftsC: { rev: draftsC.rev, drafts: draftsC.drafts },
    aSaved, cSaved, bConflictKept, bNotResent,
    putsA: L48.brief(putsA),
    putsB: L48.brief(putsB),
    putsC: L48.brief(putsC),
    storeLeaf: storeLeaf ? { boardId: storeLeaf.boardId, draftStates: storeLeaf.draftStates, draftConflicts: storeLeaf.draftConflicts } : null,
  });
  await Promise.race([ctx.shot('s3b-draft-conflict-skip'), H.sleep(6000)]).catch(() => null);
}

/* ================================================================== */
/* s4：第 7 项 —— 较早的切板请求不得覆盖较新的选择                     */
/*                                                                     */
/* 驱动口径：任务结果入口（IntentExecutionMessage.vue 的「查看结果」按钮）*/
/* 调用的就是同一条公开入口 store.selectBoard(target)；组合时序按任务书   */
/* 允许直接调用同一公开入口，驱动方式记录在 detail。                    */
/* ================================================================== */
async function fireSelectBoard(ctx, boardId) {
  await ctx.ensureStore();
  return ctx.page.eval(
    '(function(){ window.__r8selResult = null; var p = window.__q46Store.selectBoard(' +
      JSON.stringify(String(boardId)) +
      '); window.__r8sel = p; p.then(function(r){ window.__r8selResult = r; }).catch(function(e){ window.__r8selResult = { error: String(e) }; }); return true; })()',
  );
}

async function s4(ctx) {
  const A = ID('a_s4');
  const B = ID('b_s4');
  const C = ID('c_s4');
  await L.seedBoard(A, [L.card('ca4', 'A 的内容（切板代次）')]);
  await L.seedBoard(B, [L.card('cb4', 'B 的内容（切板代次）')]);
  await L.seedBoard(C, [L.card('cc4', 'C 的内容（切板代次）')]);
  await ctx.gotoApp('r8s4=' + RUN);
  await ctx.enterInteractive();
  await L48.openBoardReady(ctx, A);

  // 1) 先发起选 B（挂起板面清单），随后选 A 并完成加载，再放行 B 的旧响应
  const gate1 = L48.gate(ctx, (u, m) => m === 'GET' && /\/api\/interactive\/boards$/.test(u), '板面清单 #1', { once: true });
  await fireSelectBoard(ctx, B);
  let held1 = null;
  try {
    held1 = await gate1.waitForHeld(15000);
  } catch (e) {}
  const openA = await L48.openBoardReady(ctx, A);
  await gate1.releaseAll({ kind: 'pass' });
  await H.sleep(3000);
  const st1 = await L.snapshot(ctx);
  const firstKeptA = st1.boardId === A;
  const aContentKept = (st1.boardCards || []).some((c) => String(c.content).includes('A 的内容'));

  // 2) 交换顺序：先发起选 A（挂起清单），完成选 B，再放行 A 的旧响应
  const gate2 = L48.gate(ctx, (u, m) => m === 'GET' && /\/api\/interactive\/boards$/.test(u), '板面清单 #2', { once: true });
  await fireSelectBoard(ctx, A);
  let held2 = null;
  try {
    held2 = await gate2.waitForHeld(15000);
  } catch (e) {}
  const openB = await L48.openBoardReady(ctx, B);
  await gate2.releaseAll({ kind: 'pass' });
  await H.sleep(3000);
  const st2 = await L.snapshot(ctx);
  const secondKeptB = st2.boardId === B;
  const bContentKept = (st2.boardCards || []).some((c) => String(c.content).includes('B 的内容'));

  // 3) 最新目标读取失败：错误只归这次选择，不能把页面从已加载的 B 抢走
  const failRule = ctx.fulfill((u, m) => m === 'GET' && u.indexOf('/boards/' + C + '/state') >= 0, {
    delayMs: 0,
    status: 500,
    body: { detail: 'harness: 注入的板面读取失败' },
    repeat: true,
  });
  await fireSelectBoard(ctx, C);
  await H.sleep(4000);
  const st3 = await L.snapshot(ctx);
  const stayedOnB = st3.boardId === B;
  const bStillShown = (st3.boardCards || []).some((c) => String(c.content).includes('B 的内容'));
  const st3Submit = await L48.storeSubmitFacts(ctx).catch(() => null);
  const retryEntry = await L.ui(ctx, '[data-im="board-load-retry"]');
  ctx.disableRule(failRule);
  await ctx.clearRules();

  const ok = Boolean(held1) && firstKeptA && aContentKept && Boolean(held2) && secondKeptB && bContentKept && stayedOnB && bStillShown;
  ctx.record('7) 较早的切板请求不得覆盖较新的选择：先 B 后 A（及交换顺序）时当前板始终是最后真正加载完成的 A/B；最新目标读取失败也不会把页面抢走', ok, {
    driving: {
      entry: '任务结果入口 IntentExecutionMessage.vue 的 openBoardResult() 调用的就是 store.selectBoard(target)；组合时序直接调用同一公开入口（任务书允许）',
      selectBoard: 'store.selectBoard(公开入口)',
      hold: 'CDP Fetch 挂起板面清单 GET /api/interactive/boards（旧请求迟到，注入时序）',
      failInjection: 'CDP Fetch 对 C 的 GET /boards/C/state 回 500（注入失败）',
    },
    boards: { A, B, C },
    held1: Boolean(held1), held2: Boolean(held2),
    openA: openA && { ok: openA.ok, reason: openA.reason },
    openB: openB && { ok: openB.ok, reason: openB.reason },
    firstState: { boardId: st1.boardId, cards: (st1.boardCards || []).map((c) => String(c.content).slice(0, 60)) },
    secondState: { boardId: st2.boardId, cards: (st2.boardCards || []).map((c) => String(c.content).slice(0, 60)) },
    failState: { boardId: st3.boardId, loadError: st3.loadError, cards: (st3.boardCards || []).map((c) => String(c.content).slice(0, 60)) },
    st3Submit: st3Submit && { submitStatus: st3Submit.submitStatus, submitError: st3Submit.submitError },
    retryEntry: { exists: retryEntry.exists, visible: retryEntry.visible, text: retryEntry.text },
    firstKeptA, aContentKept, secondKeptB, bContentKept, stayedOnB, bStillShown,
  });
  await ctx.shot('s4-selectboard-generation');
}

/* ================================================================== */
/* s5：第 8 项 —— 同板面的旧撤回回执不得结束新撤回                     */
/*                                                                     */
/* 「撤回」= 影响确认层里的取消（cancelImpact）：它必须回读服务器事实   */
/* 才能确认撤回落地。驱动：真实「取消」按钮 + 真实运行中任务触发确认层。 */
/* ================================================================== */
async function s5(ctx) {
  const B = ID('b_s5');
  const A = ID('a_s5');
  const cardId = 'cb5';
  await L.seedBoard(B, [L.card(cardId, 'B 撤回材料（第8项）')]);
  await L.seedBoard(A, [L.card('ca5', 'A 材料（第8项）')]);
  const seeded = L48.seedRunningTask(B, cardId, 'B 执行中任务（第8项）');
  await ctx.gotoApp('r8s5=' + RUN);
  await ctx.enterInteractive();
  await L48.openBoardReady(ctx, B);
  await H.sleep(1500);
  const store0 = await L.snapshot(ctx);
  const running = (store0.intents || []).length > 0;

  // 等待影响确认层出现（真实保存前的影响检查 → 确认层）
  const waitConfirm = async (label) => {
    try {
      await ctx.waitExpr(
        '(function(){ const s=document.querySelector("[data-im=impact-confirm-scrim]"); return !!(s && getComputedStyle(s).display !== "none"); })()',
        25000,
        label,
      );
      return true;
    } catch (e) {
      return false;
    }
  };
  // 1) 第一次撤回：编辑 → 完成编辑 → 确认层 → 点取消 → 挂起它的回读 GET
  await L.editCardText(ctx, 0, 'B 第一次候选（第8项）');
  await L.finishEditing(ctx);
  const confirm1 = await waitConfirm('第一次影响确认');
  const gate1 = L48.gate(ctx, (u, m) => m === 'GET' && u.indexOf('/boards/' + B + '/state') >= 0, '第一次撤回回读', { once: true });
  const cancel1 = await L.clickAsserted(ctx, '[data-im="impact-confirm-cancel"]', { timeout: 15000 });
  let read1 = null;
  try {
    read1 = await gate1.waitForHeld(20000);
  } catch (e) {}
  const afterCancel1 = await L.snapshot(ctx);
  // 2) 第二次撤回：再次编辑 → 完成编辑 → 确认层 → 点取消 → 挂起它的回读 GET
  await L.editCardText(ctx, 0, 'B 第二次候选（第8项）');
  await L.finishEditing(ctx);
  const confirm2 = await waitConfirm('第二次影响确认');
  const gate2 = L48.gate(ctx, (u, m) => m === 'GET' && u.indexOf('/boards/' + B + '/state') >= 0, '第二次撤回回读', { once: true });
  const cancel2 = await L.clickAsserted(ctx, '[data-im="impact-confirm-cancel"]', { timeout: 15000 });
  const cancel2SettleFirst = await L.snapshot(ctx);
  let read2 = null;
  try {
    read2 = await gate2.waitForHeld(20000);
  } catch (e) {}
  // 3) 放回第一次的成功回执 → 不许结束第二次的等待
  await gate1.releaseAll({ kind: 'pass' });
  await H.sleep(2500);
  const afterFirstLate = await L.snapshot(ctx);
  const secondStillWaiting = Boolean(afterFirstLate.cancelRecovery && afterFirstLate.cancelRecovery.active);
  // 4) 令第二次的回读失败 → 最新撤回保留原因与「重新核对」入口
  await gate2.releaseAll({ kind: 'fulfill', status: 500, body: { detail: 'harness: 注入的撤回回读失败' } });
  await H.sleep(3000);
  const afterSecondFail = await L.snapshot(ctx);
  const retryUi = await L.ui(ctx, '[data-im="impact-confirm-recovery-retry"]');
  const outcomeText = await ctx.page.eval(
    '(function(){ const el=document.querySelector("[data-im=impact-confirm-outcome]"); return el ? (el.innerText||"").trim().slice(0, 300) : null; })()',
  );
  // 5) 故障解除后从真实按钮重试成功
  await ctx.clearRules();
  await H.sleep(400);
  const retryClick = await L.clickAsserted(ctx, '[data-im="impact-confirm-recovery-retry"]', { timeout: 15000 });
  await H.sleep(2500);
  const afterRetry = await L.snapshot(ctx);
  const settled = !afterRetry.cancelRecovery || afterRetry.cancelRecovery.active === false;
  const retryGone = await L.ui(ctx, '[data-im="impact-confirm-recovery-retry"]');

  const ok =
    running &&
    confirm1 &&
    cancel1.ok &&
    Boolean(read1) &&
    confirm2 &&
    cancel2.ok &&
    Boolean(read2) &&
    secondStillWaiting &&
    afterSecondFail.cancelRecovery &&
    afterSecondFail.cancelRecovery.active === true &&
    Boolean(afterSecondFail.cancelRecovery.reason) &&
    retryUi.exists && retryUi.visible && !retryUi.disabled &&
    retryClick.ok && settled && !retryGone.visible;
  ctx.record('8) 同板面的旧撤回回执不得结束新撤回：第二次撤回等待时放回第一次成功，第二次仍在等待；让第二次失败后保留原因与「重新核对」入口，点它重试成功', ok, {
    driving: {
      undo: '真实影响确认层的「取消」按钮（impact-confirm-cancel）',
      running: 'seed-r8.py 在隔离数据目录写入一条 running 任务（真实后端）',
      hold: 'CDP Fetch 挂起每次撤回的 GET /boards/B/state 回读（两次）；第一次成功放行、第二次注入 500',
      retry: '真实「重新核对服务器事实」按钮 impact-confirm-recovery-retry',
    },
    board: B,
    seeded,
    running,
    confirm1, confirm2,
    cancel1: { ok: cancel1.ok, reason: cancel1.reason || null },
    cancel2: { ok: cancel2.ok, reason: cancel2.reason || null },
    read1Held: Boolean(read1), read2Held: Boolean(read2),
    afterCancel1: { cancelRecovery: afterCancel1.cancelRecovery },
    cancel2SettleFirst: { cancelRecovery: cancel2SettleFirst.cancelRecovery },
    afterFirstLate: { cancelRecovery: afterFirstLate.cancelRecovery, boardId: afterFirstLate.boardId },
    afterSecondFail: { cancelRecovery: afterSecondFail.cancelRecovery, saveStatus: afterSecondFail.saveStatus },
    retryUi: { exists: retryUi.exists, visible: retryUi.visible, disabled: retryUi.disabled, text: retryUi.text },
    outcomeText,
    retryClick: { ok: retryClick.ok, reason: retryClick.reason || null },
    afterRetry: { cancelRecovery: afterRetry.cancelRecovery },
    retryGone: { exists: retryGone.exists, visible: retryGone.visible },
    secondStillWaiting, settled,
  });
  await ctx.shot('s5-cancel-generation');
}

/* ================================================================== */
/* s6a：第 9 项 —— 替换文件失败后，重试处理用户刚选的文件              */
/*                                                                     */
/* 真实界面路径：文件卡工具栏「关联文件」选 new.txt → 上传失败（注入）   */
/* → 卡片上出现失败提示与重试入口 → 真实点重试并再次提供 new.txt →      */
/* 回读后端卡片 attachmentId 必须是新上传的那一份（不是 old.txt 的）。   */
/* ================================================================== */
function cardAttachOf(card) {
  const meta = (card && card.meta) || {};
  return meta.attachmentId || (meta.file && meta.file.attachmentId) || null;
}
async function waitBackendFileCards(boardId, timeout) {
  return L48.waitFor(async () => {
    const b = await L48.backendBoard(boardId);
    const cards = (b.state.cards || []).filter((c) => !c.deleted);
    return cards.length ? { b, cards } : null;
  }, timeout || 30000, '后端板面文件卡落库');
}

async function s6a(ctx) {
  const B = ID('b_s6a');
  await L.seedBoard(B, []);
  const oldName = 'old-r8-' + RUN + '.txt';
  const newName = 'new-r8-' + RUN + '.txt';
  const oldPath = join(OUT, 'materials', oldName);
  const newPath = join(OUT, 'materials', newName);
  writeFileSync(oldPath, 'old attachment content ' + RUN + '\n', 'utf8');
  writeFileSync(newPath, 'new attachment content ' + RUN + '\n', 'utf8');
  await ctx.gotoApp('r8s6a=' + RUN);
  await ctx.enterInteractive();
  await L48.openBoardReady(ctx, B, { cards: 0 });
  const since = Date.now();

  // 1) 真实「添加 → 文件」把 old.txt 关联到板面（真实上传 + 真实保存）
  await L.uploadViaAddMenu(ctx, oldPath);
  const settledOld = await L48.waitFor(async () => {
    const b = await L48.backendBoard(B);
    const file = (b.state.cards || []).find((c) => c.kind === 'file' && cardAttachOf(c));
    return file ? { b, file } : null;
  }, 30000, 'old.txt 关联落库').catch(() => null);
  const oldAttach = settledOld ? cardAttachOf(settledOld.file) : null;
  const cardId = settledOld ? settledOld.file.id : null;
  const uploadsBefore = ctx.requests({ method: 'POST', contains: '/api/attachments/upload', since });

  // 2) 选中该文件卡，真实点工具栏「关联文件」，选 new.txt；注入上传失败
  await ctx.click('[data-im="card"]', 0).catch(() => null);
  await H.sleep(400);
  const newUploadGate = L48.gate(
    ctx,
    (u, m, body) => m === 'POST' && u.indexOf('/api/attachments/upload') >= 0 && String(body || '').indexOf(newName) >= 0,
    'new.txt 上传',
    { delayMatch: (u, m) => m === 'POST' && u.indexOf('/api/attachments/upload') >= 0 },
  );
  const chooser1 = ctx.page.setFilesOnChooser([newPath], { timeout: 20000 });
  const attachBtn = await L.clickAsserted(ctx, '[data-im="card-attach-file"]', { timeout: 15000 });
  let chooserOk = false;
  let heldUpload = null;
  try {
    await chooser1;
    chooserOk = true;
  } catch (e) {}
  try {
    heldUpload = await newUploadGate.waitForHeld(20000);
  } catch (e) {}
  const failedRelease = await newUploadGate.releaseAll({ kind: 'fulfill', status: 500, body: { detail: 'harness: 注入的上传失败' } });
  const retryUi = await L48.waitFor(async () => {
    const el = await L.ui(ctx, '[data-im="card-attach-retry"]');
    return el.exists && el.visible ? el : null;
  }, 20000, '替换失败后的重试入口').catch(() => null);
  const noticeText = await ctx.page.eval(
    '(function(){ const el=document.querySelector("[data-im=card-attach-status-text]"); return el ? (el.innerText||"").trim().slice(0,300) : null; })()',
  );
  const midBackend = await L48.backendBoard(B);
  const midCard = (midBackend.state.cards || []).find((c) => c.id === cardId) || null;
  const keptOldWhileFailed = midCard ? cardAttachOf(midCard) === oldAttach : false;

  // 3) 真实点「重新选择文件」重试，并再次提供 new.txt
  const uploadsBeforeRetry = ctx.requests({ method: 'POST', contains: '/api/attachments/upload', since });
  const chooser2 = ctx.page.setFilesOnChooser([newPath], { timeout: 20000 });
  const retryClick = await L.clickAsserted(ctx, '[data-im="card-attach-retry"]', { timeout: 15000 });
  let chooser2Ok = false;
  try {
    await chooser2;
    chooser2Ok = true;
  } catch (e) {}
  const replaced = await L48.waitFor(async () => {
    const b = await L48.backendBoard(B);
    const card = (b.state.cards || []).find((c) => c.id === cardId) || null;
    const att = cardAttachOf(card);
    return att && att !== oldAttach ? { att, b } : null;
  }, 35000, '替换后新附件落库').catch(() => null);
  await H.sleep(800);
  const uploadsAfter = ctx.requests({ method: 'POST', contains: '/api/attachments/upload', since });
  const newUploads = uploadsAfter.filter((r) => String(r.postData || '').indexOf(newName) >= 0);
  const backendAfter = await L48.backendBoard(B);
  const finalCard = (backendAfter.state.cards || []).find((c) => c.id === cardId) || null;
  const finalAttach = cardAttachOf(finalCard);
  const finalNotice = await ctx.page.eval(
    '(function(){ const el=document.querySelector("[data-im=card-attach-status-text]"); return el ? (el.innerText||"").trim().slice(0,300) : null; })()',
  );
  const ok =
    Boolean(oldAttach) && Boolean(cardId) && attachBtn.ok && chooserOk && Boolean(heldUpload) &&
    Boolean(retryUi) && retryClick.ok && chooser2Ok && Boolean(replaced) &&
    Boolean(finalAttach) && finalAttach !== oldAttach && newUploads.length >= 2 && keptOldWhileFailed;
  ctx.record('9) 替换文件失败后重试处理刚选的文件：old.txt 关联成功后选 new.txt 上传失败（注入）→ 卡片保留旧附件且出现重试入口 → 真实点重试重传 new.txt，后端卡片 attachmentId 指向新文件', ok, {
    driving: {
      attachOld: '真实「添加 → 文件」入口（文件选择器 + 真实上传/保存）',
      replaceNew: '真实文件卡工具栏「关联文件」按钮 [data-im=card-attach-file] + 文件选择器',
      failInjection: 'CDP Fetch 对 new.txt 的上传 POST 回 500（注入失败）',
      retry: '真实失败提示上的重试按钮 [data-im=card-attach-retry]（重新选择文件）',
    },
    board: B, cardId, oldAttach, finalAttach,
    attachBtn: { ok: attachBtn.ok, reason: attachBtn.reason || null },
    chooserOk, heldUpload: Boolean(heldUpload),
    failedRelease: (failedRelease || []).length,
    retryUi: retryUi && { exists: retryUi.exists, visible: retryUi.visible, text: retryUi.text },
    noticeText, finalNotice,
    keptOldWhileFailed,
    midAttach: midCard ? cardAttachOf(midCard) : null,
    retryClick: { ok: retryClick.ok, reason: retryClick.reason || null },
    chooser2Ok,
    uploadsBefore: L48.brief(uploadsBefore),
    uploadsBeforeRetry: L48.brief(uploadsBeforeRetry),
    uploadsAfter: L48.brief(uploadsAfter),
    newUploadCount: newUploads.length,
    replaced: Boolean(replaced),
    backendAfter: { cards: (backendAfter.state.cards || []).map((c) => ({ id: c.id, kind: c.kind, name: (c.meta || {}).name || null, attachmentId: cardAttachOf(c) })) },
    finalAttach,
  });
  await ctx.shot('s6a-attachment-replace-retry');
}

/* ================================================================== */
/* s7a：第 11 项 —— 批量导入的反馈和重试入口归原板                    */
/* ================================================================== */
async function s7a(ctx) {
  const A = ID('a_s7a');
  const B = ID('b_s7a');
  await L.seedBoard(A, [L.card('ca7a', 'A 已有内容（批量归板）')]);
  await L.seedBoard(B, []);
  await ctx.gotoApp('r8s7a=' + RUN);
  await ctx.enterInteractive();
  await L48.openBoardReady(ctx, B, { cards: 0 });
  const since = Date.now();
  // 1) B 批量导入两个文件；关联保存失败（注入 500）→ 批次项失败
  const failRule = ctx.fulfill((u, m) => m === 'PUT' && u.indexOf('/boards/' + B + '/state') >= 0, {
    delayMs: 300,
    status: 500,
    body: { detail: 'harness: 注入的批量关联保存失败' },
    repeat: false,
  });
  const drop = await L.dropFiles(ctx, [
    { name: 'batch-a-' + RUN + '.txt', content: 'batch a ' + RUN },
    { name: 'batch-b-' + RUN + '.txt', content: 'batch b ' + RUN },
  ]);
  const settled = await L48.waitFor(async () => {
    const s = await L.snapshot(ctx);
    const b = s.currentImportBatch;
    if (!b || !b.items.length) return null;
    return b.items.every((it) => it.status === 'succeeded' || it.status === 'failed' || it.status === 'unreceived') ? s : null;
  }, 40000, 'B 批次落定').catch(() => null);
  const batchFailed = settled ? settled.currentImportBatch : null;
  const itemsFailed = (batchFailed && batchFailed.items) || [];
  const batchUiOnB = await ctx.page.eval(
    '(function(){ return [...document.querySelectorAll("[data-im=add-batch-item]")].map(function(el){ return { name: el.getAttribute("data-name"), status: el.getAttribute("data-status") }; }); })()',
  );
  const uploadsBeforeSwitch = ctx.requests({ method: 'POST', contains: '/api/attachments/upload', since });
  const cardsBBefore = ((await L48.backendBoard(B)).state.cards || []).length;

  // 2) 切到 A：A 不得显示 B 的批次 / 重试入口
  const switchA = await ctx.storeEval('return await s.selectBoard(' + JSON.stringify(A) + ');').catch((e) => ({ error: String(e) }));
  await H.sleep(1800);
  const onA = await L.snapshot(ctx);
  const batchUiOnA = await ctx.page.eval(
    '(function(){ return [...document.querySelectorAll("[data-im=add-batch-item]")].map(function(el){ return { name: el.getAttribute("data-name"), status: el.getAttribute("data-status") }; }); })()',
  );
  const retryOnA = await L.ui(ctx, '[data-im="add-batch-item"] [data-im="card-attach-retry"]');
  const pageTextOnA = await L.pageText(ctx);
  const aShowsBBatch = batchUiOnA.length > 0 || (retryOnA.exists && retryOnA.visible) || String(pageTextOnA).indexOf('batch-a-') >= 0;
  const aBatchState = onA.currentImportBatch;

  // 3) 回 B：批次项与重试入口仍在；点真实重试，只 B 成功、不重复上传、不重复建卡
  const switchB = await ctx.storeEval('return await s.selectBoard(' + JSON.stringify(B) + ');').catch((e) => ({ error: String(e) }));
  await H.sleep(1800);
  const backB = await L.snapshot(ctx);
  const retryBtn = await L.ui(ctx, '[data-im="add-batch-item"] [data-im="card-attach-retry"]');
  await ctx.clearRules();
  await H.sleep(400);
  const retryClick = await L.clickAsserted(ctx, '[data-im="add-batch-item"] [data-im="card-attach-retry"]', { timeout: 15000 }).catch((e) => ({ ok: false, reason: String(e).slice(0, 200) }));
  await L48.waitFor(async () => {
    const s = await L.snapshot(ctx);
    const b = s.currentImportBatch;
    const first = b && b.items && b.items[0];
    return first && (first.status === 'succeeded' || first.status === 'failed') ? s : null;
  }, 40000, '重试项落定').catch(() => null);
  await H.sleep(1500);
  const uploadsAfterRetry = ctx.requests({ method: 'POST', contains: '/api/attachments/upload', since });
  const boardAAfter = await L48.backendBoard(A);
  const boardBAfter = await L48.backendBoard(B);
  const fileCardsB = (boardBAfter.state.cards || []).filter((c) => c.kind === 'file' && !c.deleted);
  const cardsA = (boardAAfter.state.cards || []).filter((c) => !c.deleted);
  const onlyBHasNewCards = fileCardsB.length >= 1 && cardsA.length === 1;
  const noReupload = uploadsAfterRetry.length === uploadsBeforeSwitch.length;
  const noDuplicateCards = fileCardsB.length <= 2;
  const backBItems = (backB.currentImportBatch && backB.currentImportBatch.items) || [];
  const batchReachableOnB = backBItems.length === itemsFailed.length && retryBtn.exists && retryBtn.visible;
  const ok =
    drop && drop.ok && itemsFailed.length >= 1 && itemsFailed.every((it) => it.status !== 'succeeded') &&
    Boolean(switchA && switchA.ok !== false) && !aShowsBBatch && !aBatchState &&
    Boolean(switchB && switchB.ok !== false) && batchReachableOnB && retryClick.ok &&
    onlyBHasNewCards && noReupload && noDuplicateCards;
  ctx.record('11) 批量导入的反馈和重试入口归原板：B 的批次/重试不出现在 A；回 B 后批次仍可恢复，真实点重试只让 B 建卡、不重复上传、不重复建卡', ok, {
    driving: {
      import: '真实拖放两个文件到「添加」入口（DataTransfer drop）',
      failInjection: 'CDP Fetch 对 B 的关联保存 PUT 回 500（注入失败）',
      switchBoard: 'store.selectBoard(公开入口)',
      retry: '真实批次项上的重试按钮 [data-im=add-batch-item] [data-im=card-attach-retry]',
    },
    boards: { A, B },
    drop, settled: Boolean(settled),
    batchFailed: itemsFailed,
    batchUiOnB,
    uploadsBeforeSwitch: L48.brief(uploadsBeforeSwitch),
    cardsBBefore,
    switchA, switchB,
    batchUiOnA,
    retryOnA: { exists: retryOnA.exists, visible: retryOnA.visible },
    aShowsBBatch,
    aBatchState,
    backBItems,
    batchReachableOnB,
    retryBtn: { exists: retryBtn.exists, visible: retryBtn.visible, text: retryBtn.text },
    retryClick: { ok: retryClick.ok, reason: retryClick.reason || null },
    uploadsAfterRetry: L48.brief(uploadsAfterRetry),
    fileCardsB: fileCardsB.map((c) => ({ id: c.id, name: (c.meta || {}).name, attachmentId: cardAttachOf(c) })),
    cardsA: cardsA.map((c) => c.id),
    onlyBHasNewCards, noReupload, noDuplicateCards,
    aPageTextSample: String(pageTextOnA || '').slice(0, 300),
  });
  await ctx.shot('s7a-batch-board-attribution');
}

/* ================================================================== */
async function main() {
  mkdirSync(OUT, { recursive: true });
  mkdirSync(join(OUT, 'materials'), { recursive: true });
  const ctx = await H.setup({ outDir: OUT, mode: 'r8', cdpPort: Number(process.env.QIO_UI_CDP_PORT || 9334) });
  ctx.log('== QIO #46 第八轮（R8）浏览器验收 runId=' + new Date().toISOString());
  ctx.log('base=' + H.BASE + ' api=' + H.API + ' provider=' + H.PROVIDER + ' out=' + OUT + ' only=' + JSON.stringify(ONLY));
  ctx.log('wt=' + H.WT + ' dataDir=' + L48.DATA_DIR + ' cdp=' + (process.env.QIO_UI_CDP_PORT || 9334));
  await ctx.gotoApp('r8=' + RUN);
  await ctx.enterInteractive();
  const run = (id) => ONLY.length === 0 || ONLY.indexOf(id) >= 0;
  const scenarios = [
    ['s1', '第1项 提交始终回到点击时的板面', s1],
    ['s2', '第2项 双标签页旧回执不删新副本', s2],
    ['s3a', '第5项 单板失败不堵其他板草稿', s3a],
    ['s3b', '第6项 冲突板被跳过、后续板继续', s3b],
    ['s4', '第7项 切板代次', s4],
    ['s5', '第8项 撤回代次', s5],
    ['s6a', '第9项 附件替换重试', s6a],
    ['s7a', '第11项 批量导入归板', s7a],
  ];
  for (const [id, label, fn] of scenarios) {
    if (!run(id)) continue;
    ctx.log('');
    ctx.log('---- ' + id + ' ' + label + ' ----');
    try {
      await fn(ctx);
    } catch (error) {
      ctx.record(id + ' 场景未跑完', false, { error: String((error && error.stack) || error).slice(0, 1500) });
      try {
        await Promise.race([ctx.shot(id + '-failed'), H.sleep(6000)]);
      } catch {}
    }
    try {
      await ctx.clearRules();
    } catch {}
  }
  const summary = await ctx.finish();
  summary.runId = RUN;
  summary.stamp = STAMP;
  summary.out = OUT;
  writeFileSync(join(OUT, 'summary.json'), JSON.stringify(summary, null, 2), 'utf8');
  return summary;
}

const summary = await main();
process.exit(summary.fail > 0 ? 1 : 0);
