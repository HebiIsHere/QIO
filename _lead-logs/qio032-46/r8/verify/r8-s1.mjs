/** R8 s1：第 1 项 —— 提交始终回到用户点击时的板面（含 1b 提交回执迟到）。 */
import { OUT, H, L, L48, ID, unverified } from './r8-common.mjs';

export async function s1(ctx) {
  const A = ID('a_s1');
  const B = ID('b_s1');
  await L.seedBoard(A, [L.card('ca1', 'A 原正文（提交归属测试）')]);
  await L.seedBoard(B, [L.card('cb1', 'B 原正文（提交归属测试）')]);
  const subsA0 = await L48.submissionCount(A);
  const subsB0 = await L48.submissionCount(B);

  await ctx.gotoApp('r8s1=' + Date.now().toString(36));
  await ctx.enterInteractive();
  ctx.clearNetwork();
  const openB = await L48.openBoardReady(ctx, B);
  const since = Date.now();
  ctx.simulated.push({
    scenario: 's1',
    kind: 'hold',
    what: 'CDP Fetch 挂起 B 的 saveBoardState PUT（切到 A 后放行；请求与回执都来自真实后端）',
  });
  // 1) 挂起 B 的 saveBoardState PUT
  const saveGate = L48.gate(ctx, (u, m) => m === 'PUT' && u.indexOf('/boards/' + B + '/state') >= 0, 'B 保存 PUT');
  // 2) B 编辑一张卡并「完成编辑」→ 触发正式保存（被闸门挂起）
  const typed = await L.editCardText(ctx, 0, 'B 新正文·提交归属（应只回 B）');
  const finish = await L.finishEditing(ctx);
  let saveHeld = null;
  try {
    saveHeld = await saveGate.waitForHeld(20000);
  } catch (e) {}
  // 3) 真实「提交给 QIO」按钮发起提交（此时 B 的保存还在飞）
  const submitBtn = await L.ui(ctx, '[data-im="submit"]');
  const submitClick = await L.clickAsserted(ctx, '[data-im="submit"]', { timeout: 15000 });
  await H.sleep(1200);
  // 4) 用公开入口切到已加载的 A（driving 记录在 detail）
  const switchA = await ctx.storeEval('return await s.selectBoard(' + JSON.stringify(A) + ');').catch((e) => ({ error: String(e) }));
  let aStateBefore = await ctx.storeState();
  try {
    aStateBefore = await L48.waitFor(async () => {
      const st = await ctx.storeState();
      return st.boardId === A && (st.boardCards || []).length >= 1 ? st : null;
    }, 20000, '切到 A 且卡片已加载');
  } catch (e) {}
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
  const aBackend = await L48.backendBoard(A);
  const bSavedNewText = ((bAfter.state.cards || []) || []).some((c) => String(c.content || '').indexOf('B 新正文·提交归属') >= 0);
  const aCardContent = (aBackend.state.cards || []).map((c) => String(c.content || ''));
  const aBeforeTexts = (aStateBefore.boardCards || []).map((c) => String(c.content || ''));
  const aContentStable = aBeforeTexts.length > 0 && JSON.stringify(aBeforeTexts) === JSON.stringify(aCardContent);

  const urlTargetsB = Boolean(post) && post.url.indexOf('/boards/' + B + '/submissions') >= 0;
  const versionIsB = Boolean(postBody) && Number(postBody.baseStateVersion) === Number(bAfter.seq);
  const bPlusOne = subsB1 === subsB0 + 1;
  const aUnchanged = subsA1 === subsA0;
  const stillOnA = aAfter.boardId === A;
  const aNotOverwritten = aCardContent.some((t) => t.indexOf('A 原正文') >= 0) && !aCardContent.some((t) => t.indexOf('B 新正文') >= 0);
  const ok =
    submitClick.ok && Boolean(saveHeld) && Boolean(post) && urlTargetsB && versionIsB && bPlusOne && aUnchanged &&
    stillOnA && aNotOverwritten && aContentStable && bSavedNewText;
  ctx.record('1) B 提交时切到已加载的 A：提交请求只针对 B、提交体 seq 是 B 的保存版本；回读 B 提交 +1、A 不变；A 内容不被覆盖', ok, {
    driving: {
      submit: 'real-button [data-im=submit]',
      switchToA: 'store.selectBoard(A)（公开入口；任务书允许组合时调用同一公开入口）',
      hold: 'CDP Fetch 挂起 saveBoardState PUT（injected）',
    },
    boards: { A, B },
    submitBtn: submitBtn && { exists: submitBtn.exists, visible: submitBtn.visible, disabled: submitBtn.disabled, text: submitBtn.text },
    submitClick: { ok: submitClick.ok, reason: submitClick.reason || null },
    typed,
    finish,
    saveHeld: Boolean(saveHeld),
    switchA,
    aStateBefore: { boardId: aStateBefore.boardId, seq: aStateBefore.seq, cards: (aStateBefore.boardCards || []).map((c) => String(c.content || '').slice(0, 80)) },
    releasedCount: (released || []).length,
    post: post ? { url: post.url.replace(H.API, ''), status: post.status } : null,
    postBody,
    posts: L48.brief(posts),
    putsB: L48.brief(putsB),
    bAfter: { seq: bAfter.seq, cards: (bAfter.state.cards || []).map((c) => ({ id: c.id, content: String(c.content).slice(0, 80) })) },
    subsA0, subsA1, subsB0, subsB1,
    bSavedNewText, urlTargetsB, versionIsB, bPlusOne, aUnchanged, stillOnA, aNotOverwritten,
    aBackend: { seq: aBackend.seq, cards: aCardContent.map((x) => x.slice(0, 80)) },
    aBeforeTexts,
    aContentStable,
    aStoreContent: (aAfter.boardCards || []).map((c) => String(c.content || '').slice(0, 80)),
  });
  await ctx.shot('s1-submit-stays-on-B');
  return { A, B };
}

/* ---------- 1b：B 的提交回执在 A 打开时才到 ---------- */
export async function s1b(ctx, boards) {
  const A2 = ID('a_s1b');
  const B2 = ID('b_s1b');
  await L.seedBoard(A2, [L.card('ca1b', 'A2 原正文（迟到回执）')]);
  await L.seedBoard(B2, [L.card('cb1b', 'B2 原正文（迟到回执）')]);
  await ctx.gotoApp('r8s1b=' + Date.now().toString(36));
  await ctx.enterInteractive();
  ctx.clearNetwork();
  await L48.openBoardReady(ctx, B2);
  ctx.simulated.push({ scenario: 's1b', kind: 'hold', what: 'CDP Fetch 挂起 B2 的提交 POST（切到 A2 后再放行；回执来自真实后端）' });
  const postGate = L48.gate(ctx, (u, m) => m === 'POST' && u.indexOf('/boards/' + B2 + '/submissions') >= 0, 'B2 提交 POST');
  await L.editCardText(ctx, 0, 'B2 新正文·迟到回执');
  await L.finishEditing(ctx);
  await H.sleep(2000);
  const submitClick2 = await L.clickAsserted(ctx, '[data-im="submit"]', { timeout: 15000 });
  let postHeld = null;
  try {
    postHeld = await postGate.waitForHeld(25000);
  } catch (e) {}
  const a2Before = await L48.storeSubmitFacts(ctx);
  const switchA2 = await ctx.storeEval('return await s.selectBoard(' + JSON.stringify(A2) + ');').catch((e) => ({ error: String(e) }));
  await L48.waitFor(async () => {
    const st = await ctx.storeState();
    return st.boardId === A2 ? st : null;
  }, 20000, '切到 A2').catch(() => null);
  const releasedPost = await postGate.releaseAll({ kind: 'pass' });
  const postResp = await L48.waitFor(
    () => ctx.network.find((r) => r.method === 'POST' && r.url.indexOf('/boards/' + B2 + '/submissions') >= 0 && r.status),
    30000,
    '提交响应',
  ).catch(() => null);
  await H.sleep(5000);
  const a2After = await L48.storeSubmitFacts(ctx);
  const a2HasReceipt = String(a2After.submitStatus) !== 'idle' && String(a2After.submitStatus) !== 'undefined';
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
    a2After,
    a2SubmitStatus: a2After.submitStatus,
    b2State,
    b2GotReceipt,
  });
  await ctx.shot('s1b-late-submit-receipt');
}
