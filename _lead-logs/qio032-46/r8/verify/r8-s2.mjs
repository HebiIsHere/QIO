/** R8 s2：第 2 项 —— 两个真实标签页：旧正式保存不得删除另一标签页的新恢复副本。 */
import { OUT, H, L, L48, ID, unverified } from './r8-common.mjs';

export async function s2(ctx) {
  const B = ID('b_s2');
  const cardId = 'cb2';
  await L.seedBoard(B, [L.card(cardId, 'B 卡片起点（双标签页）')]);
  await ctx.gotoApp('r8s2=' + Date.now().toString(36));
  await ctx.enterInteractive();
  await L48.openBoardReady(ctx, B);
  const tab2 = await L48.openTab(ctx, 'r8s2tab2=' + Date.now().toString(36), { outDir: OUT });
  await tab2.enterInteractive();
  await L48.openBoardReady(tab2, B);
  await L48.clearLocalDrafts(ctx.page);
  await L48.clearLocalDrafts(tab2.page);

  // ---- 2.1 页2 新稿尚未同步：页1 的旧正式保存回执不得删掉它的本机恢复副本 ----
  await L48.activate(ctx);
  const sinceS2 = Date.now();
  ctx.simulated.push({ scenario: 's2', kind: 'hold', what: 'CDP Fetch 分别挂起页1 的正式保存 PUT 与页2 的草稿 PUT（回执来自真实后端）' });
  const saveGate = L48.gate(ctx, (u, m) => m === 'PUT' && u.indexOf('/boards/' + B + '/state') >= 0, '页1正式保存');
  const typed1 = await L.editCardText(ctx, 0, '页1正式内容（双标签页）');
  await H.sleep(800);
  const finish1 = await L.finishEditing(ctx);
  let saveHeld = null;
  try {
    saveHeld = await saveGate.waitForHeld(20000);
  } catch (e) {}
  const puts1 = ctx.requests({ method: 'PUT', contains: '/boards/' + B + '/state', since: sinceS2 });
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
  await saveGate.releaseAll({ kind: 'pass' });
  await H.sleep(5000);
  const afterLocal = await L48.readLocalRecord(tab2.page, cardId);
  const afterTab2 = await tab2.storeState();
  const backendDraft1 = await L48.backendDrafts(B);
  const bAfter = await L48.backendBoard(B);
  await ctx.shot('s2-two-tabs-local-record');

  const textOf = (r) => (r && typeof r.text === 'string' ? r.text : '');
  const recordKept = Boolean(afterLocal) && textOf(afterLocal).indexOf('页2新草稿') >= 0;
  const versionKept = Boolean(afterLocal) && typeof afterLocal.version === 'number' && afterLocal.version > 0;
  const memoryKept = JSON.stringify(afterTab2.drafts || {}).indexOf('页2新草稿') >= 0;
  const editorValueOpen = await tab2.page.eval('(function(){ const el=document.querySelector("[data-im=card-editor]"); return el ? el.value : null; })()');
  const ok = Boolean(saveHeld) && Boolean(draftHeld) && recordKept && versionKept && memoryKept;
  ctx.record('2) 两个真实标签页：页1 正式保存回执到达后，页2 尚未同步的新草稿本机恢复副本仍在（正文/版本都在），不要求页2重输', ok, {
    driving: {
      tabs: 'CDP Target.createTarget 同浏览器默认上下文（同源共享 localStorage）',
      tab1FormalSave: '真实「完成编辑」按钮触发正式保存；CDP Fetch 挂起回执',
      tab2Draft: '真实卡片编辑器输入；CDP Fetch 挂起其草稿 PUT（模拟尚未同步）',
    },
    board: B,
    saveHeld: Boolean(saveHeld), saveGateHits: saveGate.hits,
    typed1, finish1,
    puts1: L48.brief(puts1),
    draftHeld: Boolean(draftHeld), typed2,
    beforeLocal,
    beforeTab2: { boardId: beforeTab2.boardId, drafts: beforeTab2.drafts, draftStates: beforeTab2.draftStates },
    afterLocal,
    afterTab2: { boardId: afterTab2.boardId, drafts: afterTab2.drafts, draftStates: afterTab2.draftStates },
    backendDraft1: { drafts: backendDraft1.drafts, rev: backendDraft1.rev, keyVersions: backendDraft1.keyVersions },
    bAfter: { seq: bAfter.seq, cards: (bAfter.state.cards || []).map((c) => String(c.content).slice(0, 80)) },
    recordKept, versionKept, memoryKept, editorValueOpen,
  });
  await ctx.shot('s2b-two-tabs-local-record');

  // 清场：关掉页2（它的在飞草稿请求随页面一起作废），2.2 用一个**新开的真实标签页**做「新稿已同步」
  await L48.activate(ctx);
  await L48.closeTab(tab2);
  await H.sleep(500);

  // ---- 2.2 页2（新标签页）新稿已同步后，页1 的旧正式保存回执不得清掉后端更新的草稿 ----
  const tab3 = await L48.openTab(ctx, 'r8s2tab3=' + Date.now().toString(36), { outDir: OUT });
  await tab3.enterInteractive();
  await L48.openBoardReady(tab3, B);
  await L48.activate(tab3);
  const syncText = '页2新稿·已同步（旧回执不许删）';
  const beforeSync = await L48.backendDrafts(B);
  const oldKeyVersion = Number((beforeSync.keyVersions || {})['card:' + cardId] ?? 0);
  const typed3 = await L.editCardText(tab3, 0, syncText);
  /**
   * 编辑器输入后立即明确冲一次草稿队列（公开入口），避免第三个标签页的防抖计时器
   * 在后台节流下迟迟不触发，导致「新稿已同步」这个前提没有成立。
   */
  try {
    await tab3.page.eval('(function(){ var s=window.__q46Store; return s.flushDrafts ? s.flushDrafts() : null; })()');
  } catch (e) {}
  const pollSynced = async (ms, label) => {
    try {
      return await L48.waitFor(async () => {
        const d = await L48.backendDrafts(B);
        const text = String((d.drafts || {})['card:' + cardId] || '');
        const kv = Number((d.keyVersions || {})['card:' + cardId] ?? 0);
        return text.indexOf('页2新稿·已同步') >= 0 && kv > oldKeyVersion ? { d, kv } : null;
      }, ms, label);
    } catch (e) {
      return null;
    }
  };
  let syncedNow = await pollSynced(15000, '页2 新稿同步到后端（真实编辑器输入）');
  let syncHow = syncedNow ? 'new-tab-real-editor' : '';
  if (!syncedNow) {
    /**
     * 驱动兜底：真实编辑器在第三个标签页里偶发没能把草稿发出去（页面刚激活 / 焦点未进入编辑器）。
     * 这里改用**同一个公开 store 入口**写入草稿并 flush（任务书允许组合时序调用同一公开入口），
     * 仍需等后端 drafts + keyVersions 真正前进才认为「已同步」；并把驱动方式如实记进证据。
     */
    try {
      await tab3.page.eval(
        '(function(){ var s=window.__q46Store; s.setCardDraftInput(' + JSON.stringify(cardId) +
          ', { text: ' + JSON.stringify(syncText) + ' }); return s.flushDrafts ? s.flushDrafts() : null; })()',
      );
    } catch (e) {}
    syncedNow = await pollSynced(20000, '页2 新稿同步到后端（公开入口兜底）');
    syncHow = syncedNow ? 'store-public-action-fallback' : 'failed';
  }
  const backendSynced = await L48.backendDrafts(B);
  const syncedKV = Number((backendSynced.keyVersions || {})['card:' + cardId] ?? 0);
  const tab2Synced = Boolean(syncedNow) && String((backendSynced.drafts || {})['card:' + cardId] || '').indexOf('页2新稿·已同步') >= 0;

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
  const puts2 = ctx.requests({ method: 'PUT', contains: '/boards/' + B + '/state', since: since2b });
  const draftRequests2b = ctx.requests({ method: 'PUT', contains: '/api/interactive/drafts/' + B, since: since2b });
  await saveGate2.releaseAll({ kind: 'pass' });
  await H.sleep(5000);
  const backendAfter = await L48.backendDrafts(B);
  const keptText = String((backendAfter.drafts || {})['card:' + cardId] || '');
  const keptKV = Number((backendAfter.keyVersions || {})['card:' + cardId] ?? 0);
  const backendNewDraftKept = keptText.indexOf('页2新稿·已同步') >= 0 && keptKV >= syncedKV;
  const b2After = await L48.backendBoard(B);
  const ok2 = tab2Synced && Boolean(held2) && backendNewDraftKept;
  const detail2b = {
    driving: {
      page2Sync: '新开真实标签页，真实卡片编辑器输入；轮询后端 drafts + keyVersions 确认同步成立',
      page1SecondSave: '真实「完成编辑」；CDP Fetch 挂起回执后放行（回执来自真实后端）',
    },
    beforeSync: { rev: beforeSync.rev, drafts: beforeSync.drafts, keyVersions: beforeSync.keyVersions },
    oldKeyVersion,
    typed3,
    syncHow,
    backendSynced: { rev: backendSynced.rev, drafts: backendSynced.drafts, keyVersions: backendSynced.keyVersions },
    syncedKV,
    tab2Synced,
    held2: Boolean(held2), saveGate2Hits: saveGate2.hits,
    typed1b, finish1b,
    puts2: L48.brief(puts2),
    draftRequests2b: L48.brief(draftRequests2b),
    backendAfter: { rev: backendAfter.rev, drafts: backendAfter.drafts, keyVersions: backendAfter.keyVersions },
    keptText, keptKV,
    backendNewDraftKept,
    b2After: { seq: b2After.seq, cards: (b2After.state.cards || []).map((c) => String(c.content).slice(0, 80)) },
  };
  if (!tab2Synced) {
    unverified(ctx, '2b) 页2 新稿已同步到后端后，页1 的旧正式保存回执不得清掉后端更新的草稿', {
      note: 'UNVERIFIED：本轮页2 新稿未能在驱动中同步到后端（真实编辑器输入后 25s 内 drafts/keyVersions 未前进），2b 不作为产品结论',
      detail: detail2b,
    });
  } else {
    ctx.record('2b) 页2 新稿已同步到后端后，页1 的旧正式保存回执不得清掉后端更新的草稿（API 回读仍是页2的新文字、keyVersion 未回退）', ok2, detail2b);
  }
  await ctx.shot('s2c-two-tabs-backend-draft');
  await L48.closeTab(tab3);
  await H.sleep(300);
}
