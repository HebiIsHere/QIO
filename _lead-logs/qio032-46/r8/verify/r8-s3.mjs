/** R8 s3a/s3b：第 5/6 项 —— 一块板的失败/冲突不得堵住其他板的草稿。 */
import { H, L, L48, ID, probeResponsive, driveStep } from './r8-common.mjs';

/* 第 5 项：A 的失败请求之后排队的 B、以及随后的 C 必须继续自动保存。 */
export async function s3a(ctx) {
  const A = ID('a_s3a');
  const B = ID('b_s3a');
  const C = ID('c_s3a');
  const keyA = 'card:ca3a';
  const keyB = 'card:cb3a';
  const keyC = 'card:cc3a';
  await L.seedBoard(A, [L.card('ca3a', 'A 卡片（第5项）')]);
  await L.seedBoard(B, [L.card('cb3a', 'B 卡片（第5项）')]);
  await L.seedBoard(C, [L.card('cc3a', 'C 卡片（第5项）')]);

  await ctx.gotoApp('r8s3a=' + Date.now().toString(36));
  await ctx.enterInteractive();
  ctx.clearNetwork();
  await L48.openBoardReady(ctx, A);
  const since = Date.now();
  ctx.simulated.push({ scenario: 's3a', kind: 'hold+fulfill503', what: 'CDP Fetch 挂起 A 的新草稿 PUT，再以 503 放行（注入失败；请求来自真实页面）' });
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
  const switchB = await ctx.storeEval('return await s.selectBoard(' + JSON.stringify(B) + ');').catch((e) => ({ error: String(e) }));
  await H.sleep(600);
  const typedB = await L.editCardText(ctx, 0, 'B 草稿·排在 A 失败请求之后');
  await H.sleep(1600);
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
  const switchC = await ctx.storeEval('return await s.selectBoard(' + JSON.stringify(C) + ');').catch((e) => ({ error: String(e) }));
  await H.sleep(600);
  const typedC = await L.editCardText(ctx, 0, 'C 草稿·应继续自动保存（第5项）');
  await H.sleep(5000);
  const responsive2 = await probeResponsive(ctx);
  const draftsC1 = await L48.backendDrafts(C);
  const cSaved = String((draftsC1.drafts || {})[keyC] || '').indexOf('C 草稿·应继续') >= 0;
  const putsC1 = ctx.requests({ method: 'PUT', contains: '/api/interactive/drafts/' + C, since });
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
    putsA1: L48.brief(putsA1), putsB1: L48.brief(putsB1), putsC1: L48.brief(putsC1),
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

/* 第 6 项：B 真实同键冲突后，A/C 的草稿必须继续；页面不能卡死。 */
export async function s3b(ctx) {
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

  await ctx.gotoApp('r8s3b=' + Date.now().toString(36));
  await ctx.enterInteractive();
  ctx.clearNetwork();
  await L48.openBoardReady(ctx, B);
  const since = Date.now();
  ctx.simulated.push({ scenario: 's3b', kind: 'hold', what: 'CDP Fetch 挂起带本页新文字的 B 草稿 PUT；harness 直接写一版更新的服务端草稿制造真实同键冲突' });
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
  const draftsB0 = await L48.backendDrafts(B);
  const serverWrite = await H.apiPut(H.draftsPath(B), {
    mode: 'patch',
    drafts: { [keyB]: 'B 服务器上的另一份（真实并发写）' },
    baseRev: draftsB0.rev,
  });
  await bGate.releaseAll({ kind: 'pass' });
  const conflictStep = await driveStep('等待 B 真实同键冲突状态', () =>
    L48.waitFor(async () => {
      const s = await L.snapshot(ctx);
      return s.draftConflicts && s.draftConflicts[keyB] ? s : null;
    }, 15000, 'B 真实同键冲突'),
  );
  const bConflict = conflictStep.ok ? conflictStep.value : null;
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
  const storeLeaf = hangDetected ? null : await L.snapshot(ctx).catch(() => null);
  ctx.log(
    '  [s3b] 分步：' + stepFacts.map((s) => s.label + '=' + (s.ok ? 'ok' : s.timeout ? 'TIMEOUT' : 'fail')).join(' / ') +
      '，responsive=' + JSON.stringify(responsive) + '，bConflict=' + Boolean(bConflict),
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
    putsA: L48.brief(putsA), putsB: L48.brief(putsB), putsC: L48.brief(putsC),
    storeLeaf: storeLeaf ? { boardId: storeLeaf.boardId, draftStates: storeLeaf.draftStates, draftConflicts: storeLeaf.draftConflicts } : null,
  });
  await Promise.race([ctx.shot('s3b-draft-conflict-skip'), H.sleep(6000)]).catch(() => null);
}
