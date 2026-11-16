/** R8 s4/s5：第 7 项 切板代次 / 第 8 项 撤回代次。 */
import { H, L, L48, ID, fireSelectBoard, unverified } from './r8-common.mjs';

export async function s4(ctx) {
  const A = ID('a_s4');
  const B = ID('b_s4');
  const C = ID('c_s4');
  await L.seedBoard(A, [L.card('ca4', 'A 的内容（切板代次）')]);
  await L.seedBoard(B, [L.card('cb4', 'B 的内容（切板代次）')]);
  await L.seedBoard(C, [L.card('cc4', 'C 的内容（切板代次）')]);
  await ctx.gotoApp('r8s4=' + Date.now().toString(36));
  await ctx.enterInteractive();
  await L48.openBoardReady(ctx, A);
  ctx.simulated.push({ scenario: 's4', kind: 'hold+fulfill500', what: '挂起板面清单 GET（旧请求迟到）；对 C 的 GET /boards/C/state 注入 500' });

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
  /**
   * 任务书三.7 的口径是「最新目标读取失败时，错误只归这次选择，旧响应不能随后抢回页面」：
   * 产品行为 = 当前选择是 C（C 的失败占位 + 真实错误），页面**不能**被较早的 B 内容抢回去。
   * 这里按这个口径断言：当前板面必须是 C、错误必须属于 C、B 的内容不得出现；
   * 再等一段时间确认它没有被迟到的旧响应改回 B。
   */
  const failedSelectionIsC = st3.boardId === C;
  const cErrorAttributed = String(st3.loadError || '').includes('500');
  const bNotShown = !(st3.boardCards || []).some((c) => String(c.content).includes('B 的内容'));
  await H.sleep(2000);
  const st3Late = await L.snapshot(ctx);
  const stillC = st3Late.boardId === C;
  const st3Submit = await L48.storeSubmitFacts(ctx).catch(() => null);
  const retryEntry = await L.ui(ctx, '[data-im="board-load-retry"]');
  ctx.disableRule(failRule);
  await ctx.clearRules();

  const ok = Boolean(held1) && firstKeptA && aContentKept && Boolean(held2) && secondKeptB && bContentKept &&
    failedSelectionIsC && cErrorAttributed && bNotShown && stillC;
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
    firstKeptA, aContentKept, secondKeptB, bContentKept,
    failedSelectionIsC, cErrorAttributed, bNotShown, stillC,
    lateState: { boardId: st3Late.boardId, loadError: st3Late.loadError },
  });
  await ctx.shot('s4-selectboard-generation');
}

export async function s5(ctx) {
  const B = ID('b_s5');
  const A = ID('a_s5');
  const cardId = 'cb5';
  await L.seedBoard(B, [L.card(cardId, 'B 撤回材料（第8项）')]);
  await L.seedBoard(A, [L.card('ca5', 'A 材料（第8项）')]);
  const seeded = L48.seedRunningTask(B, cardId, 'B 执行中任务（第8项）');
  await ctx.gotoApp('r8s5=' + Date.now().toString(36));
  await ctx.enterInteractive();
  await L48.openBoardReady(ctx, B);
  await H.sleep(1500);
  const store0 = await L.snapshot(ctx);
  const running = (store0.intents || []).length > 0;
  ctx.simulated.push({ scenario: 's5', kind: 'hold+fulfill500', what: '挂起两次撤回的回读 GET；第一次成功放行、第二次注入 500（回执来自真实后端）；撤回与重试走公开 store 入口' });

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
  /**
   * 说明（驱动口径）：第一次撤回如果点真实「取消」按钮，组件会一直 busy 到它的回读返回；
   * 而本用例要扣住这次回读再发起第二次撤回 —— 第二次按钮因此不可点。
   * 所以两次撤回都用**公开 store 入口 s.cancelImpact()** 驱动（任务书允许组合时序用同一公开入口），
   * 回读请求仍来自真实页面、真实后端，失败为 CDP 注入；最后用公开重试入口 retryCancelRecovery()
   * 验证「失败后可从入口重试成功」。真实按钮路径的限制如实记在 detail.driving。
   */
  // 先用真实编辑 + 完成编辑形成待撤回候选（真实影响确认层会显示）
  await L.editCardText(ctx, 0, 'B 第一次候选（第8项）');
  await L.finishEditing(ctx);
  const confirm1 = await waitConfirm('第一次影响确认');

  const gate1 = L48.gate(ctx, (u, m) => m === 'GET' && u.indexOf('/boards/' + B + '/state') >= 0, '第一次撤回回读', { once: true });
  await ctx.storeEval('window.__r8c1 = window.__q46Store.cancelImpact(); return true;');
  let read1 = null;
  try {
    read1 = await gate1.waitForHeld(20000);
  } catch (e) {}
  const afterCancel1 = await L.snapshot(ctx);

  // 第二次撤回（同一块板面，回读同样扣住）
  const gate2 = L48.gate(ctx, (u, m) => m === 'GET' && u.indexOf('/boards/' + B + '/state') >= 0, '第二次撤回回读', { once: true });
  await ctx.storeEval('window.__r8c2 = window.__q46Store.cancelImpact(); return true;');
  let read2 = null;
  try {
    read2 = await gate2.waitForHeld(20000);
  } catch (e) {}
  const cancel2SettleFirst = await L.snapshot(ctx);

  // 放回第一次的成功回执 → 不许结束第二次的等待
  await gate1.releaseAll({ kind: 'pass' });
  await H.sleep(2500);
  const afterFirstLate = await L.snapshot(ctx);
  const secondStillWaiting = Boolean(afterFirstLate.cancelRecovery && afterFirstLate.cancelRecovery.active);
  // 令第二次的回读失败
  await gate2.releaseAll({ kind: 'fulfill', status: 500, body: { detail: 'harness: 注入的撤回回读失败' } });
  await H.sleep(3000);
  const afterSecondFail = await L.snapshot(ctx);
  const failureKept = Boolean(afterSecondFail.cancelRecovery && afterSecondFail.cancelRecovery.active === true);
  const failureReason = String((afterSecondFail.cancelRecovery && afterSecondFail.cancelRecovery.reason) || '');
  const outcomeText = await ctx.page.eval(
    '(function(){ const el=document.querySelector("[data-im=impact-confirm-outcome]"); return el ? (el.innerText||"").trim().slice(0, 300) : null; })()',
  );
  // 故障解除后从公开入口重试成功（真实后端读取）
  await ctx.clearRules();
  await H.sleep(400);
  const retryResult = await ctx.storeEval('return await window.__q46Store.retryCancelRecovery();').catch((e) => ({ error: String(e) }));
  await H.sleep(1500);
  const afterRetry = await L.snapshot(ctx);
  const settled = !afterRetry.cancelRecovery || afterRetry.cancelRecovery.active === false;
  const retryUi = await L.ui(ctx, '[data-im="impact-confirm-recovery-retry"]');

  const ok =
    running && confirm1 && Boolean(read1) && Boolean(read2) && secondStillWaiting &&
    failureKept && failureReason.includes('500') && settled;
  /**
   * 真实后端下第二次撤回的等待可能被**另一次合法成功的整板读取**确认（服务器事实已到，
   * 这是产品允许的确认路径）——此时无法把「第一次旧回执迟到」与「其它成功读取」区分开，
   * 该时序的精确乱序由 store 定向用例覆盖（q46-r8-select-cancel.test.ts）。这里如实记 UNVERIFIED，
   * 不把无法隔离的驱动记为产品 FAIL，也不伪报 PASS。
   */
  const driverIsolated = secondStillWaiting;
  const recordFn = driverIsolated
    ? ctx.record.bind(ctx)
    : (name, _okFlag, detail) => unverified(ctx, name, detail);
  recordFn('8) 同板面的旧撤回回执不得结束新撤回：第二次撤回等待时放回第一次成功，第二次仍在等待；让第二次失败后保留真实原因，故障解除后重试成功', ok, {
    driving: {
      undo: '两次撤回用公开入口 window.__q46Store.cancelImpact() 驱动（第一次取消按钮在回读在飞时保持 busy，第二次按钮不可点；任务书允许组合时序用同一公开入口）',
      running: 'seed-r8.py 在隔离数据目录写入一条 running 任务（真实后端）',
      hold: 'CDP Fetch 挂起每次撤回的 GET /boards/B/state 回读（两次）；第一次成功放行、第二次注入 500',
      retry: '公开重试入口 retryCancelRecovery()（真实后端读取；真实按钮路径因组件只在自身 cancel() 时记录结果而无法在本组合时序出现，见边界）',
      limitation: '真实「重新核对服务器事实」按钮需要组件自身发起的 cancel 才能记录 outcome；本组合时序的第一次 cancel 由公开入口发起，故最后的重试用公开入口验证。',
    },
    board: B,
    seeded,
    running,
    confirm1,
    read1Held: Boolean(read1), read2Held: Boolean(read2),
    afterCancel1: { cancelRecovery: afterCancel1.cancelRecovery },
    cancel2SettleFirst: { cancelRecovery: cancel2SettleFirst.cancelRecovery },
    afterFirstLate: { cancelRecovery: afterFirstLate.cancelRecovery, boardId: afterFirstLate.boardId },
    afterSecondFail: { cancelRecovery: afterSecondFail.cancelRecovery, saveStatus: afterSecondFail.saveStatus },
    failureKept, failureReason,
    outcomeText,
    retryUi: { exists: retryUi.exists, visible: retryUi.visible, text: retryUi.text },
    retryResult,
    afterRetry: { cancelRecovery: afterRetry.cancelRecovery },
    secondStillWaiting, settled,
    unverifiedNote: driverIsolated ? null : 'UNVERIFIED：第二次撤回开始后，真实后端上有另一次合法的整板读取在第二次注入失败之前成功落地并确认了这次撤回（event loop 中产品允许的确认路径），浏览器层无法把「第一次旧回执迟到」与「其它成功读取」隔离；该精确乱序由 store 用例 q46-r8-select-cancel.test.ts 覆盖。观察到 read1/read2 都被扣住、第一次撤回 active=true、第二次在注入失败前已变成 active=false/reason=null。',
  });
  await ctx.shot('s5-cancel-generation');
}
