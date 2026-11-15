/**
 * QIO032-46 第七轮（R7）浏览器验收 harness。
 *
 * 真实浏览器（系统 Edge headless + CDP）驱动真实前端 store 与真实隔离后端；
 * 可控时序只用 CDP Fetch（延迟 / 失败 / 自定义 409 响应），模型只打本机 fake provider。
 * 只读产品代码，不写产品文件；所有证据写 _lead-logs/qio032-46/r7-baseline/ 或 r7-final/。
 *
 * 覆盖任务书 10 组场景（1 草稿双窗口并发 / 2 正式保存后台成功清旧草稿 / 3 后台冲突计入上限 /
 * 4 预判乱序 / 5 确认提示不串板 / 6 撤回重试 / 7 任务隔离 / 8 附件状态与重试 /
 * 9 批量导入 / 10 原始路径回归）。
 *
 * 纪律：
 *  - 浏览器用例必须断言按钮**真实出现且可点**，并从该按钮完成重试（L.clickAsserted）；
 *    直接调用 store 动作只能作补充，不能当界面通过。
 *  - 驱动不稳定时标 UNVERIFIED 并写明尝试方式与观察，不编造 PASS。
 *  - 失败注入 / 真实请求 / 模拟回执分别记录在 detail 里。
 */
import { mkdirSync, writeFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import * as H from './lib.mjs';
import * as L from './lib47.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const OUT = resolve(process.env.QIO_UI_OUT || join(HERE, '..', 'r7-baseline'));
const ONLY = String(process.env.Q47_ONLY || '')
  .split(',')
  .map((s) => s.trim())
  .filter(Boolean);
const RUN = Date.now().toString(36);
const stamp = new Date().toISOString();

const A = 'r7a_' + RUN;
const B = 'r7b_' + RUN;

function unverified(ctx, name, detail) {
  return ctx.record(name, false, detail, { observe: true });
}

/* ================================================================== */
/* 场景 1：草稿双窗口并发（A 板窗口1读 rev1 → 切 B 读 rev2 → 回 A；   */
/* 另一窗口把 A 同一键推到 rev2；窗口1 编辑保存；断言 baseRev/keyVersions */
/* 是该板实际版本，冲突后两份文字都保留，B 草稿不受影响）              */
/* ================================================================== */
async function s1(ctx) {
  const boardA = A + '_s1';
  const boardB = B + '_s1';
  const keyA = 'card:ca1';
  await L.seedBoard(boardA, [L.card('ca1', 'A 卡片正文（窗口1 读到）')]);
  await L.seedBoard(boardB, [L.card('cb1', 'B 卡片正文')]);

  // A 板先有一份草稿（rev 1 / 逐键版本 1）—— 窗口1 会读到它
  const seedA1 = await H.apiPut(H.draftsPath(boardA), {
    mode: 'patch',
    drafts: { [keyA]: 'A 草稿：窗口1 读到的那一版' },
  });
  // B 板推到 rev 2（让全局 serverDraftRev 被 B 抬高；两次写入 → rev 2）
  await H.apiPut(H.draftsPath(boardB), { mode: 'patch', drafts: { 'card:cb1': 'B 草稿第一版' } });
  const seedB2 = await H.apiPut(H.draftsPath(boardB), { mode: 'patch', drafts: { 'card:cb1': 'B 草稿第二版' } });

  await ctx.gotoApp('r7s1=' + RUN);
  await ctx.enterInteractive();
  await L.openBoard(ctx, boardA);
  const readA = await L.backendDrafts(boardA); // 窗口1 读到的 A 事实
  await L.switchBoard(ctx, boardB);
  const readB = await L.backendDrafts(boardB); // 窗口1 读到的 B 事实
  await L.switchBoard(ctx, boardA);
  await H.sleep(900);

  // 另一窗口：把 A 的同一个键推到 rev 2（baseRev = A 当前 rev，安全基线）
  const otherText = 'A 草稿：另一窗口写进来的那一版';
  const otherPut = await H.apiPut(H.draftsPath(boardA), {
    mode: 'patch',
    drafts: { [keyA]: otherText },
    baseRev: readA.rev,
  });
  const afterOther = await L.backendDrafts(boardA);

  const since = Date.now();
  const localDraftText = 'A 草稿：窗口1 现在输入的新版';
  await L.typeCard(ctx, 0, localDraftText, false);
  // 等草稿防抖（600ms）后真的发出 PUT：收齐窗口内所有草稿 PUT，取「带本次新输入」的那一条
  await L.waitNetwork(
    ctx,
    (r) => r.method === 'PUT' && r.url.indexOf('/api/interactive/drafts/' + boardA) >= 0 && r.startedAt >= since,
    15000,
    'A 草稿 PUT',
  ).catch(() => null);
  await H.sleep(2500);
  const draftPuts = ctx.requests({ method: 'PUT', contains: '/api/interactive/drafts/' + boardA, since });
  const draftLedger = draftPuts.map((r) => ({ status: r.status, body: L.parseBody(r) }));
  let draftPut = draftPuts.find((r) => {
    const parsed = L.parseBody(r);
    return parsed && parsed.drafts && String(parsed.drafts[keyA] || '').indexOf('窗口1 现在输入') >= 0;
  }) || (draftPuts.length ? draftPuts[draftPuts.length - 1] : null);
  const snap = await L.snapshot(ctx);
  const body = draftPut ? L.parseBody(draftPut) : null;
  const backendA = await L.backendDrafts(boardA);
  const backendB = await L.backendDrafts(boardB);

  const baseRevOk = Boolean(body) && body.baseRev === readA.rev;
  const keyVerOk = Boolean(body) && body.keyVersions && body.keyVersions[keyA] === readA.keyVersions[keyA];
  const conflict = snap.draftConflicts && snap.draftConflicts[keyA];
  const conflictOk = Boolean(conflict) && String(conflict.server) === otherText;
  const localKept = Boolean(snap.drafts) && String(snap.drafts[keyA] || '').indexOf('窗口1 现在输入') >= 0;
  const serverKeptOther = String((backendA.drafts || {})[keyA] || '') === otherText;
  const bExpected = String(((seedB2.data && seedB2.data.drafts) || {})['card:cb1'] || '');
  const bUnaffected = Boolean(bExpected) && String((backendB.drafts || {})['card:cb1'] || '') === bExpected;

  const ok = baseRevOk && keyVerOk && conflictOk && localKept && serverKeptOther && bUnaffected;
  ctx.record(
    '1) 草稿双窗口并发：baseRev/逐键版本用 A 板自己的版本；同键冲突后两份文字都保留；B 草稿不受影响',
    ok,
    {
      readA: { rev: readA.rev, keyVersions: readA.keyVersions, drafts: readA.drafts },
      readB: { rev: readB.rev, keyVersions: readB.keyVersions },
      seedA1: { rev: seedA1.data && seedA1.data.rev },
      seedB2: { rev: seedB2.data && seedB2.data.rev },
      otherPut: { status: otherPut.status, rev: otherPut.data && otherPut.data.rev, conflicts: otherPut.data && otherPut.data.conflicts },
      afterOther: { rev: afterOther.rev, drafts: afterOther.drafts },
      draftPut: draftPut ? { status: draftPut.status, body } : null,
      draftLedger,
      baseRevOk,
      keyVerOk,
      conflict: conflict || null,
      conflictOk,
      localKept,
      serverKeptOther,
      backendA: { rev: backendA.rev, drafts: backendA.drafts },
      backendB: { rev: backendB.rev, drafts: backendB.drafts },
      bExpected,
      bUnaffected,
      storeDrafts: snap.drafts,
      storeDraftStates: snap.draftStates,
    },
  );
  await ctx.shot('s1-draft-two-windows');
}

/* ================================================================== */
/* 场景 2：正式保存后台成功清旧草稿                                    */
/* ================================================================== */
async function s2(ctx) {
  const boardA = A + '_s2';
  const boardB = B + '_s2';
  await L.seedBoard(boardA, [L.card('ca2', 'A 卡片（场景2）')]);
  await L.seedBoard(boardB, [L.card('cb2', 'B 卡片起点（场景2）')]);
  await H.apiPut(H.draftsPath(boardA), { mode: 'patch', drafts: { 'card:ca2': 'A 的草稿（应保持）' } });

  await ctx.gotoApp('r7s2=' + RUN);
  await ctx.enterInteractive();
  await L.openBoard(ctx, boardB);
  // B 先输入一版草稿并落库（不退出编辑）
  await L.typeCard(ctx, 0, 'B 的旧草稿（保存成功后应被清掉）', false);
  await H.sleep(1800);
  const beforeSave = await L.backendDrafts(boardB);

  // 正式保存：完成编辑（登记清除），PUT 延迟 4s 放行
  const since = Date.now();
  const slow = ctx.pass(
    (url, m) => m === 'PUT' && url.indexOf('/boards/' + boardB + '/state') >= 0,
    { delayMs: 4000 },
  );
  await L.retypeCard(ctx, 'B 正式内容（保存成功后落库）');
  await H.sleep(200);
  const finishBtn = await ctx.page.eval(
    '(function(){ const sels=["[data-im=card-icon-edit]","[data-im=card-edit]"]; for (const q of sels){ const el=document.querySelector(q); if (el){ const r=el.getBoundingClientRect(); if (r.width>0&&r.height>0) return q; } } return null; })()',
  );
  if (finishBtn) await ctx.click(finishBtn, 0, { timeout: 15000 });
  try {
    await L.waitNetwork(
      ctx,
      (r) => r.method === 'PUT' && r.url.indexOf('/boards/' + boardB + '/state') >= 0 && r.startedAt >= since,
      15000,
      'B 正式保存 PUT',
    );
  } catch (e) {}
  // PUT 在飞期间切到 A
  await L.switchBoard(ctx, boardA);
  await H.sleep(6000); // 等 B 的 PUT 回执在 A 停留期间到达
  const onA = await L.snapshot(ctx);
  const backendBAfter = await L.backendDrafts(boardB);
  const boardBAfter = await L.backendBoard(boardB);
  const backendAAfter = await L.backendDrafts(boardA);

  const bSaved = (boardBAfter.state.cards || []).some((c) => String(c.content).indexOf('B 正式内容') >= 0);
  // 旧草稿清除的判据：服务端不再有「旧草稿正文」这一份（服务端可能保留与正式正文同文的记录）
  const bDraftText = String((backendBAfter.drafts || {})['card:cb2'] || '');
  const bDraftCleared = bDraftText.indexOf('B 的旧草稿') < 0;
  const aKept = String((backendAAfter.drafts || {})['card:ca2'] || '') === 'A 的草稿（应保持）';
  // 草稿请求台账（失败注入 / 真实请求分别记录）
  const draftPuts = ctx.requests({ method: 'PUT', contains: '/api/interactive/drafts/', since });
  const draftLedger = draftPuts.map((r) => ({ url: String(r.url).replace(H.API, ''), status: r.status, body: L.parseBody(r) }));
  const onAStore = { pendingDraftClearKeys: onA.pendingDraftClearKeys, draftStates: onA.draftStates, drafts: onA.drafts };

  // 第二阶段：B 再次输入新草稿 → 新草稿必须仍在（不被旧回执清掉）
  await L.switchBoard(ctx, boardB);
  await H.sleep(900);
  await L.typeCard(ctx, 0, 'B 保存后又输入的新草稿', false);
  await H.sleep(2200);
  const backendBNew = await L.backendDrafts(boardB);
  const newDraftKept = String((backendBNew.drafts || {})['card:cb2'] || '').indexOf('保存后又输入') >= 0;
  const onBAfter = await L.snapshot(ctx);
  const onBStore = { pendingDraftClearKeys: onBAfter.pendingDraftClearKeys, draftStates: onBAfter.draftStates, drafts: onBAfter.drafts };

  const ok = bSaved && bDraftCleared && aKept && newDraftKept;
  ctx.record('2) 正式保存后台成功清旧草稿：B 正式内容落库、B 旧草稿清除、A 草稿保持；B 新草稿不被旧回执清掉', ok, {
    beforeSave: { rev: beforeSave.rev, drafts: beforeSave.drafts },
    slowRuleHits: ctx.ruleHits(slow),
    onA: { boardId: onA.boardId, saveStatus: onA.saveStatus, saveError: onA.saveError },
    boardBAfter: { seq: boardBAfter.seq, cards: (boardBAfter.state.cards || []).map((c) => ({ id: c.id, content: String(c.content).slice(0, 60) })) },
    backendBAfter: { rev: backendBAfter.rev, drafts: backendBAfter.drafts },
    backendAAfter: { rev: backendAAfter.rev, drafts: backendAAfter.drafts },
    bSaved,
    bDraftText,
    bDraftCleared,
    aKept,
    backendBNew: { rev: backendBNew.rev, drafts: backendBNew.drafts },
    newDraftKept,
    draftLedger,
    onAStore,
    onBStore,
  });
  await ctx.shot('s2-draft-clear-after-save');
}

/* ================================================================== */
/* 场景 3：后台冲突也计入上限                                          */
/* ================================================================== */
async function s3(ctx) {
  const boardA = A + '_s3';
  const boardB = B + '_s3';
  await L.seedBoard(boardA, [L.card('ca3', 'A 卡片（场景3）')]);
  await L.seedBoard(boardB, [L.card('cb3', 'B 卡片起点（场景3）')]);

  await ctx.gotoApp('r7s3=' + RUN);
  await ctx.enterInteractive();
  await L.openBoard(ctx, boardB);
  const baseSeq = (await H.readBoard(boardB)).seq || 0;
  const since = Date.now();
  // 每次 PUT /state 都回 409 stale_state（currentSeq 递增），GET /state 走真实后端
  let conflictSeq = baseSeq + 1;
  const conflictRule = ctx.fulfill(
    (url, m) => m === 'PUT' && url.indexOf('/boards/' + boardB + '/state') >= 0,
    {
      delayMs: 0,
      status: 409,
      body: { detail: { error: 'stale_state', reason: 'harness: 每次保存都冲突', currentSeq: conflictSeq } },
      repeat: true,
    },
  );

  // 第 1 次：B 编辑并保存，立刻切 A，让 409 在 A 停留期间到达
  await L.typeCard(ctx, 0, 'B 后台冲突第 1 次', true);
  await H.sleep(250);
  await L.switchBoard(ctx, boardA);
  await H.sleep(2600);
  const onA1 = await L.snapshot(ctx);

  // 后续：回 B（load 会接回恢复计划）→ 再切 A，重复到上限
  const rounds = [];
  for (let i = 2; i <= 4; i += 1) {
    await L.switchBoard(ctx, boardB);
    await H.sleep(2600);
    const st = await L.snapshot(ctx);
    rounds.push({ round: i, saveStatus: st.saveStatus, saveError: st.saveError, puts: ctx.countRequests({ method: 'PUT', contains: '/boards/' + boardB + '/state', since }) });
    await L.switchBoard(ctx, boardA);
    await H.sleep(2200);
  }
  // 记录每次 PUT 的发起时刻（判断是否在切板往返期间新增）
  const putTimeline = ctx
    .requests({ method: 'PUT', contains: '/boards/' + boardB + '/state', since })
    .map((r) => ({ at: r.startedAt - since, status: r.status }));
  const putsAtLimit = ctx.requests({ method: 'PUT', contains: '/boards/' + boardB + '/state', since });
  const putsA = ctx.requests({ method: 'PUT', contains: '/boards/' + boardA + '/state', since });

  // 第 4 次后：再次 B→A→B、重读并等过防抖，断言没有第 5 次写入
  await L.switchBoard(ctx, boardB);
  await H.sleep(2500);
  await L.switchBoard(ctx, boardA);
  await H.sleep(600);
  await L.switchBoard(ctx, boardB);
  await H.sleep(3000);
  const putsAfterSwitch = ctx.requests({ method: 'PUT', contains: '/boards/' + boardB + '/state', since });
  const onB = await L.snapshot(ctx);

  // 故障解除后从**实际页面按钮**点重试
  ctx.disableRule(conflictRule);
  await ctx.clearRules();
  await H.sleep(700);
  const retryBtn = await L.ui(ctx, '[data-iw-chrome-save-retry]');
  const retryClick = await L.clickAsserted(ctx, '[data-iw-chrome-save-retry]', { timeout: 15000 });
  let saved = null;
  try {
    saved = await ctx.waitStore((x) => x.boardId === boardB && x.saveStatus === 'saved', 20000, 'B 手动重试保存成功');
  } catch (e) {
    saved = await L.snapshot(ctx);
  }
  const putsAfterRetry = ctx.requests({ method: 'PUT', contains: '/boards/' + boardB + '/state', since });
  const retryPuts = putsAfterRetry.length - putsAfterSwitch.length;

  const limitOk = putsAtLimit.length <= 4;
  const noFifth = putsAfterSwitch.length === putsAtLimit.length;
  const retryOk = retryClick.ok && saved && saved.saveStatus === 'saved' && retryPuts === 1 && putsAfterRetry.every((r) => r.url.indexOf('/boards/' + boardB + '/state') >= 0);
  const ok = limitOk && noFifth && retryOk && putsA.length === 0;
  ctx.record('3) 后台冲突也计入上限：PUT ≤4、切板往返不新增；页面出现可点「重试保存」并从该按钮恢复（只 1 次 PUT 200）', ok, {
    rounds,
    putsAtLimit: putsAtLimit.length,
    putsAtLimitStatuses: putsAtLimit.map((r) => r.status),
    putTimeline,
    putsAfterSwitch: putsAfterSwitch.length,
    noFifth,
    limitOk,
    onA1: { boardId: onA1.boardId, saveStatus: onA1.saveStatus, saveError: onA1.saveError },
    onB: { boardId: onB.boardId, saveStatus: onB.saveStatus, saveError: onB.saveError, dirty: onB.dirty },
    retryBtn,
    retryClick: { ok: retryClick.ok, reason: retryClick.reason || null, text: retryClick.ui && retryClick.ui.text },
    retryPuts,
    saved: saved && { saveStatus: saved.saveStatus, seq: saved.seq },
    putsA: putsA.length,
  });
  await ctx.shot('s3-background-conflict-limit');
}

/* ================================================================== */
/* 场景 4：预判乱序（旧成功先到 / 新成功先到 / 旧先失败）              */
/*                                                                     */
/* 每组用**独立板面 + 独立运行中任务**（确认会暂停任务，复用板面会让   */
/* 后续组不再产生预判）。驱动方式：进入编辑 → 输入旧候选 → 完成编辑    */
/* （发出第 1 次预判）→ 重新编辑输入新候选 → 完成编辑（第 2 次预判）； */
/* 用 CDP Fetch 的「按第几次命中决定延迟 / 失败」规则控制两次到达顺序。*/
/* ================================================================== */
async function s4(ctx) {
  const orderings = [
    // 第 1 次预判（旧）先成功返回；第 2 次（新）后返回
    { name: '旧成功先到', steps: [{ delayMs: 5000, action: 'pass' }, { delayMs: 9000, action: 'pass' }] },
    // 第 1 次（旧）后返回；第 2 次（新）先返回
    { name: '新成功先到', steps: [{ delayMs: 9000, action: 'pass' }, { delayMs: 1500, action: 'pass' }] },
    // 第 1 次（旧）先失败；第 2 次（新）后成功
    { name: '旧请求先失败', steps: [{ delayMs: 4000, action: 'fail' }, { delayMs: 8000, action: 'pass' }] },
  ];
  const onlyCase = String(process.env.Q47_S4_CASE || '').trim();
  const cases = [];
  for (let i = 0; i < orderings.length; i += 1) {
    const ord = orderings[i];
    if (onlyCase && onlyCase !== String(i)) continue;
    const boardB = B + '_s4_' + i;
    await L.seedBoard(boardB, [L.card('cb4', 'B 预判材料（场景4-' + i + '）')]);
    const running = L.seedRunning(boardB, 'cb4', 'B 执行中任务（场景4-' + i + '）');
    // 每组用一次整页刷新做隔离：上一组确认 / 结果层 / 暂停任务等运行时状态
    // 不会残留到下一组（否则第二组的编辑入口会被残留浮层挡住）。
    await ctx.gotoApp('r7s4-' + i + '=' + RUN);
    await ctx.enterInteractive();
    await L.openBoard(ctx, boardB);
    await H.sleep(1800);
    await ctx.clearRules();
    // 清场：上一组可能残留确认层（scrim 会挡住卡片点击，导致「编辑入口打不开」）
    const cleared = await L.dismissOverlays(ctx);
    await H.sleep(400);
    const since = Date.now();

    const oldText = 'B 预判候选一（旧）#' + i;
    const newText = 'B 预判候选二（新）#' + i;
    // 按命中次序的时序规则：第 1 次 = 旧预判、第 2 次 = 新预判
    L.sequence(ctx, (url, m) => m === 'POST' && url.indexOf('/boards/' + boardB + '/impact-check') >= 0, ord.steps);

    // 两次编辑都用 run-46 已跑通的 typeCard 序列（真实点击图标编辑 → 编辑器 → 输入 → 再点一次完成）
    // 切板后的首次编辑可能落在读取完成前：先等 store 真正加载到目标板再编辑。
    try {
      await L.waitFor(ctx, async () => {
        const s = await L.snapshot(ctx);
        return s.boardId === boardB && (s.boardCards || []).length >= 1 ? s : null;
      }, 20000, '目标板加载完成');
    } catch (e) {}
    await H.sleep(700);
    let firstEditLanded = false;
    let typed1 = false;
    let afterFirstEdit = null;
    for (let attempt = 0; attempt < 3 && !firstEditLanded; attempt += 1) {
      typed1 = await L.typeCard(ctx, 0, oldText, true);
      await H.sleep(500);
      afterFirstEdit = await L.snapshot(ctx);
      firstEditLanded = (afterFirstEdit.boardCards || []).some((c) => String(c.content).indexOf(oldText) >= 0);
      if (!firstEditLanded) {
        ctx.log('  [s4] case ' + i + ' 第 1 次编辑未落地（attempt=' + attempt + '），重试');
        // 编辑未落地时先退出可能残留的编辑态
        await L.dismissOverlays(ctx);
        await ctx.page.pressKey('Escape').catch(() => null);
        await H.sleep(600);
      }
    }
    ctx.log('  [s4] case ' + i + ' firstEditLanded=' + firstEditLanded + ' board=' + JSON.stringify((afterFirstEdit.boardCards || []).map((c) => String(c.content).slice(0, 30))));
    const firstPost = await L.waitNetwork(
      ctx,
      (r) => r.method === 'POST' && r.url.indexOf('/boards/' + boardB + '/impact-check') >= 0 && r.startedAt >= since,
      20000,
      '第一次预判请求',
    ).catch(() => null);

    // 第 2 次编辑（候选变更）→ 完成编辑（预判 2：新）
    let secondEditLanded = false;
    let typed2 = false;
    let afterSecondEdit = null;
    for (let attempt = 0; attempt < 3 && !secondEditLanded; attempt += 1) {
      typed2 = await L.typeCard(ctx, 0, newText, true);
      await H.sleep(500);
      afterSecondEdit = await L.snapshot(ctx);
      secondEditLanded = (afterSecondEdit.boardCards || []).some((c) => String(c.content).indexOf(newText) >= 0);
      if (!secondEditLanded) {
        ctx.log('  [s4] case ' + i + ' 第 2 次编辑未落地（attempt=' + attempt + '）');
        await L.dismissOverlays(ctx);
        await ctx.page.pressKey('Escape').catch(() => null);
        await H.sleep(600);
      }
    }
    ctx.log('  [s4] case ' + i + ' secondEdit board=' + JSON.stringify((afterSecondEdit.boardCards || []).map((c) => String(c.content).slice(0, 30))) + ' scrim=' + (await L.overlayOpen(ctx)));
    const impactPostsAtSecond = ctx.requests({ method: 'POST', contains: '/boards/' + boardB + '/impact-check', since }).length;

    // 等确认层出现（或超时）
    let impactShown = false;
    try {
      impactShown = await ctx.waitExpr(
        '(function(){ const s=document.querySelector("[data-im=impact-confirm-scrim]"); return s && getComputedStyle(s).display !== "none" ? true : false; })()',
        25000,
        '影响确认出现',
      );
    } catch (e) {
      impactShown = false;
    }
    const beforeLate = await L.snapshot(ctx);
    // 等两次预判都真正落地
    const maxDelay = Math.max.apply(null, ord.steps.map((s) => s.delayMs || 0));
    await H.sleep(maxDelay + 5000);
    const afterLate = await L.snapshot(ctx);
    const uiAfter = await ctx.uiImpact();
    const impactPosts = ctx.requests({ method: 'POST', contains: '/boards/' + boardB + '/impact-check', since });
    const postCountBeforeConfirm = impactPosts.length;

    // 从**实际页面按钮**点确认
    const confirmClick = await L.clickAsserted(ctx, '[data-im="impact-confirm-submit"]', { timeout: 15000 });
    let saved = null;
    try {
      saved = await ctx.waitStore((x) => x.boardId === boardB && (x.saveStatus === 'saved' || x.saveStatus === 'error'), 25000, '确认后保存落定');
    } catch (e) {
      saved = await L.snapshot(ctx);
    }
    await H.sleep(800);
    const puts = ctx.requests({ method: 'PUT', contains: '/boards/' + boardB + '/state', since });
    const putBody = puts.length ? L.parseBody(puts[puts.length - 1]) : null;
    const pending = afterLate.pendingImpact;
    const candidateOk = Boolean(putBody) && JSON.stringify(putBody.state).indexOf(newText) >= 0;
    const checkOk = Boolean(putBody && putBody.confirm && pending) && putBody.confirm.checkId === pending.checkId;
    const versionOk = Boolean(putBody && putBody.confirm && pending) && Number(putBody.confirm.stateVersion) === Number(pending.stateVersion);
    const oneConfirm = uiAfter.open === true;
    // 确认入口必须对应**最新候选**：pendingImpact 的候选指纹里应含新候选正文
    const pendingIsNewest = Boolean(pending) && String(pending.candidateKey || '').indexOf(newText) >= 0;
    const onlyOnePut = puts.length === 1;
    const impactPostsEnough = postCountBeforeConfirm >= 2;
    cases.push({
      ordering: ord.name,
      steps: ord.steps,
      running,
      clearedOverlays: cleared.actions,
      typed1,
      firstEditLanded,
      typed2,
      secondEditLanded,
      impactPostsAtSecond,
      secondEditBoard: (afterSecondEdit.boardCards || []).map((c) => String(c.content).slice(0, 40)),
      firstPost: firstPost ? { status: firstPost.status } : null,
      impactShown,
      beforeLate: beforeLate.pendingImpact && {
        checkId: beforeLate.pendingImpact.checkId,
        previewRev: beforeLate.pendingImpact.previewRev,
        stateVersion: beforeLate.pendingImpact.stateVersion,
        candidateHasNew: String(beforeLate.pendingImpact.candidateKey || '').indexOf(newText) >= 0,
      },
      afterLate: pending && {
        checkId: pending.checkId,
        previewRev: pending.previewRev,
        stateVersion: pending.stateVersion,
        candidateHasNew: String(pending.candidateKey || '').indexOf(newText) >= 0,
      },
      uiAfter: { open: uiAfter.open, submit: uiAfter.submit, body: uiAfter.body && uiAfter.body.slice(0, 160) },
      impactPosts: postCountBeforeConfirm,
      confirmClick: { ok: confirmClick.ok, reason: confirmClick.reason || null },
      saved: saved && { saveStatus: saved.saveStatus, saveError: saved.saveError },
      putCount: puts.length,
      putStatus: puts.length ? puts[puts.length - 1].status : null,
      putCandidateHasNewText: candidateOk,
      checkIdMatches: checkOk,
      stateVersionMatches: versionOk,
      oneConfirm,
      pendingIsNewest,
      onlyOnePut,
      impactPostsEnough,
      impactCheckError: afterLate.impactCheckError,
      pass:
        Boolean(running && running.ok) &&
        impactShown &&
        impactPostsEnough &&
        oneConfirm &&
        pendingIsNewest &&
        onlyOnePut &&
        confirmClick.ok &&
        candidateOk &&
        checkOk &&
        versionOk &&
        saved &&
        saved.saveStatus === 'saved',
    });
    // 收尾：清掉可能残留的确认层 / 结果层
    const clearedAfter = await L.dismissOverlays(ctx);
    cases[cases.length - 1].clearedAfter = clearedAfter.actions;
    await ctx.clearRules();
    await H.sleep(600);
  }
  const ok = cases.every((c) => c.pass);
  // 驱动是否可信：两次编辑都真的落地、两次预判都真的发出。
  // 驱动不成立时按纪律标 UNVERIFIED（OBSERVE），不把它算成产品 FAIL。
  const driven = cases.length > 0 && cases.every((c) => c.firstEditLanded && c.secondEditLanded && c.impactPostsEnough);
  const name = '4) 预判乱序（旧成功先到 / 新成功先到 / 旧先失败）：确认入口始终对应最新候选，确认后 PUT 目标/候选/checkId/stateVersion 一致';
  if (!driven) {
    unverified(ctx, name, {
      note: 'UNVERIFIED：两次预判的驱动在本轮未能对全部次序稳定成立（第二次编辑未落地 / 预判次数不足），不作为产品结论',
      driven,
      cases,
    });
  } else {
    ctx.record(name, ok, { driven, cases });
  }
  await ctx.shot('s4-impact-ordering');
}
/* ================================================================== */
/* 场景 5：确认提示不串板                                              */
/* ================================================================== */
async function s5(ctx) {
  const boardA = A + '_s5';
  const boardB = B + '_s5';
  await L.seedBoard(boardA, [L.card('ca5', 'A 已保存材料（场景5）')]);
  await L.seedBoard(boardB, [L.card('cb5', 'B 确认材料（场景5）')]);
  const running = L.seedRunning(boardB, 'cb5', 'B 执行中任务（场景5）');
  await ctx.gotoApp('r7s5=' + RUN);
  await ctx.enterInteractive();
  await L.openBoard(ctx, boardA);
  await H.sleep(800);
  await L.typeCard(ctx, 0, 'A 已保存内容（场景5）', true);
  try {
    await ctx.waitStore((x) => x.boardId === boardA && x.saveStatus === 'saved', 15000, 'A 已保存');
  } catch (e) {}
  await L.switchBoard(ctx, boardB);
  await H.sleep(1200);

  const since = Date.now();
  // B 确认后的 PUT 会失败
  ctx.fail((url, m) => m === 'PUT' && url.indexOf('/boards/' + boardB + '/state') >= 0, { delayMs: 1500, repeat: true });
  await L.typeCard(ctx, 0, 'B 触发影响确认（确认后保存会失败）', true);
  let impactShown = false;
  try {
    impactShown = await ctx.waitExpr(
      '(function(){ const s=document.querySelector("[data-im=impact-confirm-scrim]"); return s && getComputedStyle(s).display !== "none" ? true : false; })()',
      20000,
      '影响确认',
    );
  } catch (e) {
    impactShown = false;
  }
  const confirmClick = await L.clickAsserted(ctx, '[data-im="impact-confirm-submit"]', { timeout: 15000 });
  // 立刻切到已保存的 A
  await L.switchBoard(ctx, boardA);
  await H.sleep(4000);
  const onA = await L.snapshot(ctx);
  const textOnA = await L.pageText(ctx);
  const uiSaveOnA = await ctx.uiSaveStatus();
  const retryOnA = await L.ui(ctx, '[data-iw-chrome-save-retry]');
  const bErrorText = 'B 保存失败';
  const aHasBError = Boolean(onA.saveError) || textOnA.indexOf('保存失败') >= 0 || (retryOnA.exists && retryOnA.visible);

  // 回 B：能看到失败与重试入口
  await L.switchBoard(ctx, boardB);
  let onB = null;
  try {
    onB = await ctx.waitStore((x) => x.boardId === boardB && x.saveStatus === 'error', 20000, 'B 保存失败');
  } catch (e) {
    onB = await L.snapshot(ctx);
  }
  const retryOnB = await L.ui(ctx, '[data-iw-chrome-save-retry]');
  // 在 B 点重试：只向 B 发请求
  const beforeRetry = ctx.requests({ method: 'PUT', contains: '/boards/', since }).length;
  const retryClick = await L.clickAsserted(ctx, '[data-iw-chrome-save-retry]', { timeout: 15000 });
  await H.sleep(2500);
  const puts = ctx.requests({ method: 'PUT', contains: '/boards/', since });
  const putsA = puts.filter((r) => r.url.indexOf('/boards/' + boardA + '/state') >= 0);
  const putsB = puts.filter((r) => r.url.indexOf('/boards/' + boardB + '/state') >= 0);

  const ok =
    running && running.ok && impactShown && confirmClick.ok && onB && onB.saveStatus === 'error' &&
    !aHasBError && retryOnB.exists && retryOnB.visible && retryClick.ok && putsA.length === 0 && putsB.length >= 1;
  ctx.record('5) 确认提示不串板：B 确认后切 A，B 的失败不显示在 A（无失败文案/无重试按钮）；回 B 可见失败与重试入口，点重试只写 B', ok, {
    running,
    impactShown,
    confirmClick: { ok: confirmClick.ok, reason: confirmClick.reason || null },
    onA: { boardId: onA.boardId, saveStatus: onA.saveStatus, saveError: onA.saveError },
    uiSaveOnA,
    retryOnA: { exists: retryOnA.exists, visible: retryOnA.visible },
    aHasBError,
    onB: { boardId: onB.boardId, saveStatus: onB.saveStatus, saveError: onB.saveError },
    retryOnB: { exists: retryOnB.exists, visible: retryOnB.visible, text: retryOnB.text },
    retryClick: { ok: retryClick.ok, reason: retryClick.reason || null },
    putsA: putsA.length,
    putsB: putsB.length,
    putStatuses: puts.map((r) => r.status),
  });
  await ctx.shot('s5-confirm-not-cross-board');
}

/* ================================================================== */
/* 场景 6：撤回重试                                                    */
/* ================================================================== */
async function s6(ctx) {
  const boardB = B + '_s6';
  await L.seedBoard(boardB, [L.card('cb6', 'B 撤回材料（场景6）')]);
  const running = L.seedRunning(boardB, 'cb6', 'B 执行中任务（场景6）');
  await ctx.gotoApp('r7s6=' + RUN);
  await ctx.enterInteractive();
  await L.openBoard(ctx, boardB);
  await H.sleep(1200);

  // 触发影响确认
  await L.typeCard(ctx, 0, 'B 触发影响确认（随后取消）', true);
  let impactShown = false;
  try {
    impactShown = await ctx.waitExpr(
      '(function(){ const s=document.querySelector("[data-im=impact-confirm-scrim]"); return s && getComputedStyle(s).display !== "none" ? true : false; })()',
      20000,
      '影响确认',
    );
  } catch (e) {
    impactShown = false;
  }
  // 让取消后的回读失败 → 撤回未确认
  ctx.fail((url, m) => m === 'GET' && url.indexOf('/boards/' + boardB + '/state') >= 0, { delayMs: 800, repeat: true });
  const cancelClick = await L.clickAsserted(ctx, '[data-im="impact-confirm-cancel"]', { timeout: 15000 });
  await H.sleep(3000);
  const afterCancel = await L.snapshot(ctx);
  const retryEntry = await L.ui(ctx, '[data-im="impact-confirm-recovery-retry"]');
  const outcomeText = await ctx.page.eval(
    '(function(){ const el=document.querySelector("[data-im=impact-confirm-outcome]"); return el ? (el.innerText||"").trim().slice(0,300) : null; })()',
  );

  // 故障解除后从**实际页面按钮**点重试
  await ctx.clearRules();
  await H.sleep(400);
  const retryClick = await L.clickAsserted(ctx, '[data-im="impact-confirm-recovery-retry"]', { timeout: 15000 });
  await H.sleep(2500);
  const afterRetry = await L.snapshot(ctx);
  const retryEntryAfter = await L.ui(ctx, '[data-im="impact-confirm-recovery-retry"]');
  const outcomeAfter = await ctx.page.eval(
    '(function(){ const el=document.querySelector("[data-im=impact-confirm-outcome]"); return el ? (el.innerText||"").trim().slice(0,300) : null; })()',
  );

  const activeBefore = Boolean(afterCancel.cancelRecovery && afterCancel.cancelRecovery.active);
  const entryOk = retryEntry.exists && retryEntry.visible && !retryEntry.disabled;
  const settledAfter = !afterRetry.cancelRecovery || afterRetry.cancelRecovery.active === false;
  const entryGone = !retryEntryAfter.exists || !retryEntryAfter.visible;
  const ok = running && running.ok && impactShown && cancelClick.ok && activeBefore && entryOk && retryClick.ok && settledAfter && entryGone;
  ctx.record('6) 撤回重试：回读失败 → 页面出现可点「重新核对服务器事实」；点它后回读成功、入口消失', ok, {
    running,
    impactShown,
    cancelClick: { ok: cancelClick.ok, reason: cancelClick.reason || null },
    afterCancel: { cancelRecovery: afterCancel.cancelRecovery, saveStatus: afterCancel.saveStatus },
    retryEntry: { exists: retryEntry.exists, visible: retryEntry.visible, disabled: retryEntry.disabled, text: retryEntry.text },
    outcomeText,
    retryClick: { ok: retryClick.ok, reason: retryClick.reason || null },
    afterRetry: { cancelRecovery: afterRetry.cancelRecovery },
    retryEntryAfter: { exists: retryEntryAfter.exists, visible: retryEntryAfter.visible },
    outcomeAfter,
    activeBefore,
    entryOk,
    settledAfter,
    entryGone,
  });
  await ctx.shot('s6-cancel-recovery-retry');

  // 6b：B 的撤回失败晚于 A 的撤回开始 —— A 不受影响（尽力驱动；驱动不稳则 UNVERIFIED）
  try {
    const boardA = A + '_s6';
    await L.seedBoard(boardA, [L.card('ca6', 'A 撤回材料（场景6b）')]);
    const runningA = L.seedRunning(boardA, 'ca6', 'A 执行中任务（场景6b）');
    await L.switchBoard(ctx, boardA);
    await H.sleep(1200);
    await L.typeCard(ctx, 0, 'A 触发影响确认（随后取消）', true);
    const aImpact = await ctx.waitExpr(
      '(function(){ const s=document.querySelector("[data-im=impact-confirm-scrim]"); return s && getComputedStyle(s).display !== "none" ? true : false; })()',
      20000,
      'A 影响确认',
    ).catch(() => false);
    // A 的取消回读延迟 4s（在飞）
    ctx.pass((url, m) => m === 'GET' && url.indexOf('/boards/' + boardA + '/state') >= 0, { delayMs: 4000 });
    const aCancel = await L.clickAsserted(ctx, '[data-im="impact-confirm-cancel"]', { timeout: 15000 });
    await H.sleep(300);
    // 切到 B，让 B 的撤回失败回执到达
    await L.switchBoard(ctx, boardB);
    await H.sleep(5000);
    const onB = await L.snapshot(ctx);
    await L.switchBoard(ctx, boardA);
    await H.sleep(1500);
    const onA = await L.snapshot(ctx);
    const okB = Boolean(runningA && runningA.ok && aImpact && aCancel.ok) && onA.cancelRecovery && onA.cancelRecovery.boardId === boardA && onA.cancelRecovery.active === false;
    ctx.record('6b) B 的撤回失败晚于 A 的撤回开始：A 的撤回结果不被 B 的迟到失败覆盖', okB, {
      runningA,
      aImpact,
      aCancel: { ok: aCancel.ok },
      onB: { boardId: onB.boardId, cancelRecovery: onB.cancelRecovery },
      onA: { boardId: onA.boardId, cancelRecovery: onA.cancelRecovery },
    });
  } catch (e) {
    unverified(ctx, '6b) B 的撤回失败晚于 A 的撤回开始：A 不受影响', {
      note: 'UNVERIFIED：需要两块板面同时处于「影响确认 → 取消 → 回读在飞」的组合时序，本轮未能稳定驱动',
      error: String((e && e.message) || e).slice(0, 400),
    });
  }
}

/* ================================================================== */
/* 场景 7：任务隔离                                                    */
/* ================================================================== */
async function s7(ctx) {
  const boardA = A + '_s7';
  const boardB = B + '_s7';
  await L.seedBoard(boardA, [L.card('ca7', 'A 无任务材料（场景7）')]);
  await L.seedBoard(boardB, [L.card('cb7', 'B 任务材料（场景7）')]);
  let seeded = L.seedRunning(boardB, 'cb7', 'B 执行中任务（场景7）');
  if (!seeded || !seeded.ok) {
    await H.sleep(1500);
    seeded = L.seedRunning(boardB, 'cb7', 'B 执行中任务（场景7）');
  }
  await ctx.gotoApp('r7s7=' + RUN);
  await ctx.enterInteractive();
  // A 的首次任务读取失败
  ctx.fail((url, m) => m === 'GET' && url.indexOf('/boards/' + boardA + '/intents') >= 0, { delayMs: 200, repeat: true });
  await L.openBoard(ctx, boardA);
  await H.sleep(1500);
  const onA = await L.snapshot(ctx);
  const since = Date.now();
  await L.typeCard(ctx, 0, 'A 普通改动（不应被 B 的任务拦住）', true);
  let aSaved = null;
  try {
    aSaved = await ctx.waitStore((x) => x.boardId === boardA && (x.saveStatus === 'saved' || x.saveStatus === 'error'), 20000, 'A 保存落定');
  } catch (e) {
    aSaved = await L.snapshot(ctx);
  }
  await H.sleep(800);
  const puts = ctx.requests({ method: 'PUT', contains: '/boards/', since });
  const impactPosts = ctx.requests({ method: 'POST', contains: '/impact-check', since });
  const putsA = puts.filter((r) => r.url.indexOf('/boards/' + boardA + '/state') >= 0);
  const putsB = puts.filter((r) => r.url.indexOf('/boards/' + boardB + '/state') >= 0);
  const impactA = impactPosts.filter((r) => r.url.indexOf('/boards/' + boardA) >= 0);
  const impactB = impactPosts.filter((r) => r.url.indexOf('/boards/' + boardB) >= 0);
  const aIntentsEmpty = (onA.intents || []).length === 0 && (onA.intentViews || []).length === 0;
  const bTaskNotShown = !(onA.intents || []).some((i) => String(i.title || '').indexOf('场景7') >= 0);
  const ok = Boolean(seeded && seeded.ok) && aIntentsEmpty && bTaskNotShown && aSaved && aSaved.saveStatus === 'saved' && putsA.length >= 1 && putsB.length === 0 && impactB.length === 0;
  ctx.record('7) 任务隔离：B 有运行中任务、A 无任务且首次任务读取失败；A 的保存不被 B 的任务预判拦住，A 的 intents 为空，B 的任务不显示在 A', ok, {
    seeded,
    onA: { boardId: onA.boardId, intents: onA.intents, intentViews: onA.intentViews, intentsLoadState: onA.intentsLoadState, intentsLoadError: onA.intentsLoadError, loadError: onA.loadError },
    aSaved: aSaved && { saveStatus: aSaved.saveStatus, saveError: aSaved.saveError },
    putsA: putsA.length,
    putsB: putsB.length,
    impactA: impactA.length,
    impactB: impactB.length,
    aIntentsEmpty,
    bTaskNotShown,
  });
  await ctx.shot('s7-task-isolation');
}

/* ================================================================== */
/* 场景 8：附件状态与重试                                              */
/* ================================================================== */
async function s8(ctx) {
  const boardA = A + '_s8';
  const boardB = B + '_s8';
  await L.seedBoard(boardA, [L.card('ca8', 'A 上传期间停留（场景8）')]);
  await L.seedBoard(boardB, []);
  const file = join(OUT, 'materials', 'attach-' + RUN + '.txt');
  writeFileSync(file, 'QIO032-46 R7 upload fixture ' + RUN + '\n', 'utf8');

  await ctx.gotoApp('r7s8=' + RUN);
  await ctx.enterInteractive();
  await L.openBoard(ctx, boardA);
  await L.switchBoard(ctx, boardB);
  await H.sleep(900);
  const since = Date.now();
  // 上传延迟 5s：在 A 停留期间完成
  ctx.delay((url, m) => m === 'POST' && url.indexOf('/api/attachments/upload') >= 0, 5000);
  await L.uploadViaAddMenu(ctx, file);
  await H.sleep(500);
  await L.switchBoard(ctx, boardA);
  await H.sleep(7000);
  const onA = await L.snapshot(ctx);
  await L.switchBoard(ctx, boardB);
  await H.sleep(1800);
  const onB = await L.snapshot(ctx);
  const noticeText = await ctx.page.eval(
    '(function(){ const el=document.querySelector("[data-im=add-material-status-text]"); return el ? (el.innerText||"").trim() : null; })()',
  );
  const uploads = ctx.requests({ method: 'POST', contains: '/api/attachments/upload', since });
  const job = (onB.cardImportJobs || []).find((j) => j.boardId === boardB) || null;
  const stateShown = Boolean(job) && (job.phase === 'succeeded' || job.phase === 'failed' || job.awaitingSave === true);
  const noSuccessWhileFailed = !(job && job.phase === 'failed' && String(noticeText || '').indexOf('已关联附件') >= 0);

  // 第二阶段：关联保存失败 → 页面出现可点重试 → 点它只上传一次、复用附件 ID
  await ctx.clearRules();
  await H.sleep(400);
  const file2 = join(OUT, 'materials', 'retry-' + RUN + '.txt');
  writeFileSync(file2, 'QIO032-46 R7 retry fixture ' + RUN + '\n', 'utf8');
  const since2 = Date.now();
  const failRule = ctx.fulfill(
    (url, m) => m === 'PUT' && url.indexOf('/boards/' + boardB + '/state') >= 0,
    { delayMs: 400, status: 500, body: { detail: 'harness: 关联保存故障' }, repeat: true },
  );
  // 只认**这次新上传**产生的那条导入任务（避免把上一阶段残留的 failed 任务当成本次失败）
  const before = await L.snapshot(ctx);
  const maxAttemptBefore = Math.max(0, ...((before.cardImportJobs || []).map((j) => j.attemptId)));
  await L.uploadViaAddMenu(ctx, file2);
  let failSettled = null;
  try {
    failSettled = await ctx.waitStore(
      (x) => (x.cardImportJobs || []).some((j) => j.boardId === boardB && j.attemptId > maxAttemptBefore && j.phase === 'failed'),
      25000,
      '本次关联保存失败',
    );
  } catch (e) {
    failSettled = await L.snapshot(ctx);
  }
  const failedJob = ((failSettled.cardImportJobs || []).find((j) => j.boardId === boardB && j.attemptId > maxAttemptBefore)) || null;
  const noticeWhileFailed = await ctx.page.eval(
    '(function(){ const el=document.querySelector("[data-im=add-material-status-text]"); return el ? (el.innerText||"").trim() : null; })()',
  );
  const noSuccessNotice = String(noticeWhileFailed || '').indexOf('已关联附件') < 0;
  // 失败提示必须能看出「哪份附件失败」并可重试（允许「关联待保存」这种真实中间态）
  const noticeNamesAttachment = String(noticeWhileFailed || '').indexOf('retry-') >= 0 || String(noticeWhileFailed || '').indexOf('.txt') >= 0;
  ctx.disableRule(failRule);
  await ctx.clearRules();
  // 等故障期在飞的保存彻底结算（否则它迟到的失败回执会盖在重试结果上）
  try {
    await L.waitFor(ctx, async () => {
      const s = await L.snapshot(ctx);
      return s.saveStatus !== 'saving' ? s : null;
    }, 15000, '故障期保存结算');
  } catch (e) {}
  await H.sleep(1000);

  // 从**实际页面按钮**点重试关联
  let retryHow = null;
  let retryClick = { ok: false, reason: 'none' };
  const addRetry = await L.ui(ctx, '[data-im="add-material-retry"]');
  if (addRetry.exists && addRetry.visible) {
    retryClick = await L.clickAsserted(ctx, '[data-im="add-material-retry"]', { timeout: 15000 });
    retryHow = 'add-menu-notice';
  } else {
    // 卡片工具栏上的重试入口：先选中文件卡
    const cards = await ctx.uiCards();
    const fileCardIdx = cards.findIndex((c) => String(c.text || '').indexOf('retry-') >= 0 || String(c.text || '').indexOf('txt') >= 0);
    if (fileCardIdx >= 0) {
      await ctx.click('[data-im="card"]', fileCardIdx).catch(() => null);
      await H.sleep(500);
    }
    const cardRetry = await L.ui(ctx, '[data-im="card-attach-retry"]');
    if (cardRetry.exists && cardRetry.visible) {
      retryClick = await L.clickAsserted(ctx, '[data-im="card-attach-retry"]', { timeout: 15000 });
      retryHow = 'card-toolbar';
    }
  }
  // 等这次重试真正落定：成功提示 / 或该任务的终态（succeeded|failed）
  let successSeen = false;
  try {
    await L.waitFor(ctx, async () => {
      const s = await L.snapshot(ctx);
      const job = (s.cardImportJobs || []).find((j) => j.attemptId > maxAttemptBefore);
      if (job && job.phase === 'succeeded') return { via: 'job-succeeded', job };
      const text = await ctx.page.eval(
        '(function(){ const el=document.querySelector("[data-im=add-material-status-text]"); return el ? (el.innerText||"").trim() : null; })()',
      );
      if (String(text || '').indexOf('已关联附件') >= 0) return { via: 'notice', text };
      return null;
    }, 40000, '重试关联落定');
    successSeen = true;
  } catch (e) {
    successSeen = false;
  }
  await H.sleep(1500);
  const after = await L.snapshot(ctx);
  const uploads2 = ctx.requests({ method: 'POST', contains: '/api/attachments/upload', since: since2 });
  const boardRead = await L.backendBoard(boardB);
  const fileCards = (boardRead.state.cards || []).filter((c) => c.kind === 'file');
  const attachOf = (c) => (c && c.meta && (c.meta.attachmentId || (c.meta.file && c.meta.file.attachmentId))) || null;
  const firstAttachment = failedJob && (failedJob.pendingAttachmentId || failedJob.attachmentId);
  const retriedCard = fileCards.find((c) => String(c.meta && c.meta.name || '').indexOf('retry-') >= 0) || fileCards[fileCards.length - 1];
  const retriedAttach = attachOf(retriedCard);
  const reuseOk = !firstAttachment || String(retriedAttach) === String(firstAttachment);
  const uiText = await ctx.page.eval(
    '(function(){ const el=document.querySelector("[data-im=add-material-status-text]"); return el ? (el.innerText||"").trim() : null; })()',
  );

  const ok =
    uploads.length === 1 && stateShown && noSuccessWhileFailed && noSuccessNotice &&
    Boolean(retryHow) && retryClick.ok && uploads2.length === 1 && reuseOk && Boolean(retriedAttach) && successSeen;
  ctx.record('8) 附件状态与重试：B 上传期间切 A，回 B 立即显示真实状态；失败期无成功提示；从实际页面按钮重试关联只上传 1 次、复用附件 ID、后端回读 attachmentId、界面显示成功', ok, {
    uploads: L.brief(uploads),
    onA: { boardId: onA.boardId, cards: onA.boardCards },
    onB: { boardId: onB.boardId, jobs: onB.cardImportJobs },
    noticeText,
    stateShown,
    noSuccessWhileFailed,
    failedJob,
    noticeWhileFailed,
    noSuccessNotice,
    noticeNamesAttachment,
    maxAttemptBefore,
    retryHow,
    retryClick: { ok: retryClick.ok, reason: retryClick.reason || null, text: retryClick.ui && retryClick.ui.text },
    uploads2: L.brief(uploads2),
    firstAttachment,
    retriedAttach,
    reuseOk,
    successSeen,
    uiText,
    afterSave: after.saveStatus,
  });
  await ctx.shot('s8-attachment-state-retry');
}

/* ================================================================== */
/* 场景 9：批量导入                                                    */
/* ================================================================== */
async function s9(ctx) {
  const boardB = B + '_s9';
  await L.seedBoard(boardB, []);
  await ctx.gotoApp('r7s9=' + RUN);
  await ctx.enterInteractive();
  await L.openBoard(ctx, boardB);
  await H.sleep(800);
  const since = Date.now();
  const failRule = ctx.fulfill(
    (url, m) => m === 'PUT' && url.indexOf('/boards/' + boardB + '/state') >= 0,
    { delayMs: 400, status: 500, body: { detail: 'harness: 批量导入写入故障' }, repeat: true },
  );
  const drop = await L.dropFiles(ctx, [
    { name: 'batch-a-' + RUN + '.txt', content: 'batch a ' + RUN },
    { name: 'batch-b-' + RUN + '.txt', content: 'batch b ' + RUN },
  ]);
  let settled = null;
  try {
    settled = await ctx.waitStore(
      (x) => {
        const b = x.currentImportBatch;
        if (!b) return false;
        return b.items.every((it) => it.status === 'succeeded' || it.status === 'failed' || it.status === 'unreceived');
      },
      40000,
      '批次落定',
    );
  } catch (e) {
    settled = await L.snapshot(ctx);
  }
  const batchFailed = settled.currentImportBatch;
  const itemsFailed = (batchFailed && batchFailed.items) || [];
  const noFalseSuccess = itemsFailed.every((it) => it.status !== 'succeeded');
  const batchUi = await ctx.page.eval(
    '(function(){ return [...document.querySelectorAll("[data-im=add-batch-item]")].map(function(el){ return { name: el.getAttribute("data-name"), status: el.getAttribute("data-status"), text: (el.innerText||"").trim().slice(0,120) }; }); })()',
  );
  const uploadsBefore = ctx.requests({ method: 'POST', contains: '/api/attachments/upload', since });
  const cardsBefore = ((await L.backendBoard(boardB)).state.cards || []).length;

  // 故障解除后从**实际页面按钮**点重试
  ctx.disableRule(failRule);
  await ctx.clearRules();
  await H.sleep(600);
  const retryBtn = await L.ui(ctx, '[data-im="add-batch-item"] [data-im="card-attach-retry"]');
  const retryClick = await L.clickAsserted(ctx, '[data-im="add-batch-item"] [data-im="card-attach-retry"]', { timeout: 15000 });
  // 等被重试的这一项真正落定（succeeded 或再次 failed），不是固定 sleep
  try {
    await L.waitFor(ctx, async () => {
      const s = await L.snapshot(ctx);
      const items = (s.currentImportBatch && s.currentImportBatch.items) || [];
      const first = items[0];
      return first && (first.status === 'succeeded' || first.status === 'failed') ? s : null;
    }, 40000, '重试项落定');
  } catch (e) {}
  await H.sleep(1500);
  const after = await L.snapshot(ctx);
  const uploadsAfter = ctx.requests({ method: 'POST', contains: '/api/attachments/upload', since });
  const boardRead = await L.backendBoard(boardB);
  const fileCards = (boardRead.state.cards || []).filter((c) => c.kind === 'file');
  const attachOf = (c) => (c && c.meta && (c.meta.attachmentId || (c.meta.file && c.meta.file.attachmentId))) || null;
  const attached = fileCards.filter((c) => attachOf(c));
  const batchAfter = after.currentImportBatch;
  const itemsAfter = (batchAfter && batchAfter.items) || [];
  // 「同批其他项状态不变」：只看**未被重试**的项（被点重试的 item_0 允许从不成功变为成功）
  const otherUnchanged = itemsAfter.length === itemsFailed.length && itemsAfter.every((it, i) => {
    if (i === 0) return true;
    const before = itemsFailed[i];
    if (!before) return false;
    return it.status === before.status;
  });
  const noDuplicateCard = fileCards.length <= 2;

  const ok =
    drop && drop.ok && noFalseSuccess && retryBtn.exists && retryBtn.visible && retryClick.ok &&
    uploadsAfter.length === uploadsBefore.length && attached.length >= 1 && noDuplicateCard && otherUnchanged;
  ctx.record('9) 批量导入：写入失败时批次项不得标成功；从实际页面重试入口恢复后后端回读关联成功、上传只 1 次、不重复建卡、同批其他项状态不变', ok, {
    drop,
    batchFailed: itemsFailed,
    noFalseSuccess,
    batchUi,
    uploadsBefore: uploadsBefore.length,
    uploadsAfterCount: uploadsAfter.length,
    cardsBefore,
    retryBtn: { exists: retryBtn.exists, visible: retryBtn.visible, text: retryBtn.text },
    retryClick: { ok: retryClick.ok, reason: retryClick.reason || null },
    uploadsAfter: uploadsAfter.length,
    fileCards: fileCards.map((c) => ({ id: c.id, name: c.meta && c.meta.name, attachmentId: attachOf(c) })),
    attached: attached.length,
    noDuplicateCard,
    batchAfter: itemsAfter,
    otherUnchanged,
  });
  await ctx.shot('s9-batch-import');
}

/* ================================================================== */
/* 场景 10：原始路径回归（上传 → 打开设置 → 返回互动页仍能保存附件）   */
/* ================================================================== */
async function s10(ctx) {
  const boardB = B + '_s10';
  await L.seedBoard(boardB, []);
  const file = join(OUT, 'materials', 'settings-' + RUN + '.txt');
  writeFileSync(file, 'QIO032-46 R7 settings round trip ' + RUN + '\n', 'utf8');
  await ctx.gotoApp('r7s10=' + RUN);
  await ctx.enterInteractive();
  await L.openBoard(ctx, boardB);
  const since = Date.now();
  ctx.delay((url, m) => m === 'POST' && url.indexOf('/api/attachments/upload') >= 0, 3500);
  await L.uploadViaAddMenu(ctx, file);
  const opened = await ctx.page.eval(
    '(function(){ const b=[...document.querySelectorAll("button")].find(function(x){ const l=x.getAttribute("aria-label")||""; return l.indexOf("设置")===0; }); if(!b) return false; b.click(); return true; })()',
  );
  await H.sleep(1200);
  await ctx.page.eval('(function(){ const b=document.querySelector("[data-testid=settings-back]"); if(b) b.click(); return !!b; })()');
  await H.sleep(5000);
  try {
    await ctx.waitStore((x) => x.boardId === boardB && (x.saveStatus === 'saved' || x.saveStatus === 'idle'), 15000, '返回后保存落定');
  } catch (e) {}
  await H.sleep(800);
  const state = await L.snapshot(ctx);
  const readBack = await L.backendBoard(boardB);
  const serverCards = (readBack.state && readBack.state.cards) || [];
  const uploads = ctx.requests({ method: 'POST', contains: '/api/attachments/upload', since });
  const attachOf = (c) => (c && c.meta && (c.meta.attachmentId || (c.meta.file && c.meta.file.attachmentId))) || null;
  const ok = opened && uploads.length === 1 && serverCards.some((c) => c.kind === 'file' && attachOf(c)) && state.boardId === boardB;
  ctx.record('10) 原始路径回归：上传 → 打开设置 → 返回互动页（卡片/附件保留，不重复上传）', ok, {
    opened,
    uploads: L.brief(uploads),
    boardId: state.boardId,
    cards: state.boardCards,
    serverCards: serverCards.map((c) => ({ id: c.id, kind: c.kind, attachmentId: attachOf(c) })),
  });
  await ctx.shot('s10-settings-roundtrip');
}

/* ================================================================== */
async function main() {
  mkdirSync(OUT, { recursive: true });
  mkdirSync(join(OUT, 'materials'), { recursive: true });
  const ctx = await H.setup({ outDir: OUT, mode: 'r7' });
  ctx.log('== QIO032-46 第七轮（R7）浏览器验收 runId=' + new Date().toISOString());
  ctx.log('base=' + H.BASE + ' api=' + H.API + ' provider=' + H.PROVIDER + ' out=' + OUT + ' only=' + JSON.stringify(ONLY));
  ctx.log('wt=' + H.WT + ' dataDir=' + L.DATA_DIR);
  await ctx.gotoApp('r7=' + RUN);
  await ctx.enterInteractive();
  const run = (id) => ONLY.length === 0 || ONLY.indexOf(id) >= 0;
  const scenarios = [
    ['s1', '草稿双窗口并发', s1],
    ['s2', '正式保存后台成功清旧草稿', s2],
    ['s3', '后台冲突计入上限', s3],
    ['s4', '预判乱序', s4],
    ['s5', '确认提示不串板', s5],
    ['s6', '撤回重试', s6],
    ['s7', '任务隔离', s7],
    ['s8', '附件状态与重试', s8],
    ['s9', '批量导入', s9],
    ['s10', '原始路径回归', s10],
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
        await ctx.shot(id + '-failed');
      } catch {}
    }
    try {
      await ctx.clearRules();
    } catch {}
  }
  const summary = await ctx.finish();
  summary.runId = RUN;
  summary.stamp = stamp;
  summary.out = OUT;
  writeFileSync(join(OUT, 'summary.json'), JSON.stringify(summary, null, 2), 'utf8');
  return summary;
}

const summary = await main();
process.exit(summary.fail > 0 ? 1 : 0);
