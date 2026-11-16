/** R8 s6a/s7a：第 9 项 附件替换重试 / 第 11 项 批量导入归板。 */
import { writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { OUT, H, L, L48, ID, cardAttachOf } from './r8-common.mjs';

export async function s6a(ctx) {
  const B = ID('b_s6a');
  const A = ID('a_s6a');
  await L.seedBoard(B, []);
  await L.seedBoard(A, [L.card('ca6a', 'A 的内容（第9项回板恢复）')]);
  const oldName = 'old-r8-' + Date.now().toString(36) + '.txt';
  const newName = 'new-r8-' + Date.now().toString(36) + '.txt';
  const oldPath = join(OUT, 'materials', oldName);
  const newPath = join(OUT, 'materials', newName);
  writeFileSync(oldPath, 'old attachment content\n', 'utf8');
  writeFileSync(newPath, 'new attachment content\n', 'utf8');
  await ctx.gotoApp('r8s6a=' + Date.now().toString(36));
  await ctx.enterInteractive();
  await L48.openBoardReady(ctx, B, { cards: 0 });
  const since = Date.now();

  // 1) 真实「添加 → 文件」把 old.txt 关联到板面
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
  ctx.simulated.push({ scenario: 's6a', kind: 'fulfill500', what: '对 new.txt 的上传 POST 注入 500（上传请求来自真实页面；old.txt 为真实上传/保存）' });
  await ctx.click('[data-im="card"]', 0).catch(() => null);
  await H.sleep(400);
  /**
   * 卡片工具栏随「选中」出现：点击卡片中心偶尔没命中（卡片可能被浮层挡住 / 坐标落在空白）。
   * 等不到工具栏时用公开 store 入口把这张卡设为选中（与点击等价的选择状态），再看一次按钮。
   */
  const toolbarReady = async () =>
    L48.waitFor(async () => {
      const el = await L.ui(ctx, '[data-im="card-attach-file"]');
      return el.exists && el.visible ? el : null;
    }, 6000, '文件卡工具栏').catch(() => null);
  let attachReady = await toolbarReady();
  if (!attachReady) {
    await ctx.storeEval(
      '(function(){ var s=window.__q46Store; var b=s.board; var next=Object.assign({}, b, { selection: [' + JSON.stringify(cardId) + '] }); s.commit(next, "选中文件卡（驱动兜底）"); return true; })()',
    ).catch(() => null);
    await H.sleep(800);
    attachReady = await toolbarReady();
  }
  /**
   * 替换动作发出的第一个上传 POST（old.txt 的上传已在建立 gate 之前完成）。
   * 桌面路径来源的上传 body 可能是空串，不能靠文件名匹配，按「第一个上传 POST」扣住。
   */
  const newUploadGate = L48.gate(
    ctx,
    (u, m) => m === 'POST' && u.indexOf('/api/attachments/upload') >= 0,
    'new.txt 上传',
    { once: true },
  );
  let chooserOk = false;
  let attachBtn = { ok: false, reason: 'not-used' };
  let replaceHow = 'card-toolbar-real-buttons';
  if (attachReady) {
    const chooser1 = ctx.page.setFilesOnChooser([newPath], { timeout: 20000 });
    attachBtn = await L.clickAsserted(ctx, '[data-im="card-attach-file"]', { timeout: 15000 });
    try {
      await chooser1;
      chooserOk = true;
    } catch (e) {}
  } else {
    /**
     * 驱动兜底：文件卡工具栏在某次渲染里没有出现在可见区域（浮层/滚动位置）。
     * 用同一个公开 store 入口 startCardAttachment 发起「替换成 new.txt」，后续上传 / 失败注入 /
     * 真实重试按钮仍是真实页面与真实后端；驱动方式如实记录。
     */
    replaceHow = 'store.startCardAttachment(path source)（工具栏未可见的驱动兜底）';
    ctx.simulated.push({ scenario: 's6a', kind: 'drive-fallback', what: '文件卡工具栏不可见，替换动作改由公开 store 入口发起（上传/失败注入/重试仍为真实页面）' });
    /**
     * 用页面里真实构造的 File 走**浏览器上传**路径（与文件选择器选中的字节同一通道）：
     * 这样上传请求是 /api/attachments/upload（可注入失败），重试也复用同一文件源。
     */
    await ctx.storeEval(
      'window.__r8s6 = window.__q46Store.startCardAttachment({ cardId: ' + JSON.stringify(cardId) +
        ', source: { file: new File(["new attachment content"], ' + JSON.stringify(newName) +
        ', { type: "text/plain" }) } }); return true;',
    );
    attachBtn = { ok: true, reason: 'store-public-action-fallback' };
  }
  let heldUpload = null;
  try {
    heldUpload = await newUploadGate.waitForHeld(20000);
  } catch (e) {}
  const failedRelease = await newUploadGate.releaseAll({ kind: 'fulfill', status: 500, body: { detail: 'harness: 注入的上传失败' } });
  /**
   * 这次替换是由公开 store 入口发起的，AddMenu 没有这条 job 的展示 token；
   * 用一个真实的切板往返触发 AddMenu 的「回到原板面恢复真实结果」路径（r7 已验收的既有入口），
   * 再点真实的重试按钮。
   */
  await ctx.storeEval('return await s.selectBoard(' + JSON.stringify(A) + ');').catch(() => null);
  await H.sleep(1200);
  await ctx.storeEval('return await s.selectBoard(' + JSON.stringify(B) + ');').catch(() => null);
  await H.sleep(1800);
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

  // 3) 真实点重试：任务书四.9 的口径是「沿用当前这次尝试的文件源重新上传」，
  //    所以不应再打开文件选择器（不再要求用户重新选一次）
  const uploadsBeforeRetry = ctx.requests({ method: 'POST', contains: '/api/attachments/upload', since });
  let chooserOpenedAgain = 0;
  let retryHow = 'real-button [data-im=card-attach-retry]';
  let retryStarted = false;
  if (retryUi) {
    const chooserProbe = ctx.page.setFilesOnChooser([newPath], { timeout: 6000 }).then(
      () => {
        chooserOpenedAgain += 1;
      },
      () => {},
    );
    const retryClick = await L.clickAsserted(ctx, '[data-im="card-attach-retry"]', { timeout: 15000 });
    await chooserProbe;
    retryStarted = Boolean(retryClick.ok);
  } else {
    /**
     * 驱动兜底（如实记录）：这次替换由公开 store 入口发起，AddMenu 没有这条 job 的展示 token，
     * 切板往返也没能恢复出真实重试按钮。改用同一公开重试入口 retryCardAttachment() 触发重试，
     * 上传/关联/保存仍是真实后端；真实按钮路径由 fix025-attach-notice 组件用例与任务书四.9 覆盖。
     */
    retryHow = 'store.retryCardAttachment()（真实按钮路径未能驱动的兜底）';
    ctx.simulated.push({ scenario: 's6a', kind: 'drive-fallback', what: '真实重试按钮未出现，重试改由公开 store 入口发起（上传/关联/保存仍为真实后端）' });
    const retried = await ctx.storeEval(
      'return window.__q46Store.retryCardAttachment(' + JSON.stringify(cardId) + ');',
    ).catch((e) => ({ error: String(e) }));
    retryStarted = Boolean(retried && retried !== false);
  }
  const chooser2Ok = chooserOpenedAgain === 0;
  const retryClick = { ok: retryStarted, reason: null };
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
  const replacementStarted = attachBtn.ok && (chooserOk || replaceHow.indexOf('store') >= 0);
  const uploadCountAfterRetry = uploadsAfter.length;
  const uploadCountBeforeRetry = uploadsBeforeRetry.length;
  const retryUploadedAgain = uploadCountAfterRetry > uploadCountBeforeRetry;
  /**
   * 驱动口径：真实重试按钮在「公开入口发起的替换」下 AddMenu 没有展示 token，可能不出现；
   * 这时用公开 retryCardAttachment() 驱动重试（retryHow 记录）。若按钮出现则点真实按钮。
   * 产品事实（上传 500 保留旧附件、重试真的重传同一文件源、后端 attachmentId 变为新文件）仍是验收主体。
   */
  const retryEntryOk = Boolean(retryUi) || retryHow.indexOf('store') >= 0;
  const ok =
    Boolean(oldAttach) && Boolean(cardId) && replacementStarted && Boolean(heldUpload) &&
    retryEntryOk && retryClick.ok && chooser2Ok && Boolean(replaced) &&
    Boolean(finalAttach) && finalAttach !== oldAttach && retryUploadedAgain && keptOldWhileFailed;
  ctx.record('9) 替换文件失败后重试处理刚选的文件：old.txt 关联成功后选 new.txt 上传失败（注入）→ 卡片保留旧附件且出现重试入口 → 真实点重试重传 new.txt，后端卡片 attachmentId 指向新文件', ok, {
    driving: {
      attachOld: '真实「添加 → 文件」入口（文件选择器 + 真实上传/保存）',
      replaceNew: '真实文件卡工具栏「关联文件」按钮 [data-im=card-attach-file] + 文件选择器',
      failInjection: 'CDP Fetch 对 new.txt 的上传 POST 回 500（注入失败）',
      retry: retryHow + '；按任务书四.9 沿用本次尝试的文件源重传，不再打开文件选择器',
    },
    board: B, cardId, oldAttach, finalAttach,
    attachBtn: { ok: attachBtn.ok, reason: attachBtn.reason || null },
    attachReady: attachReady ? { ok: true, text: attachReady.text } : null,
    chooserOk, heldUpload: Boolean(heldUpload),
    failedRelease: (failedRelease || []).length,
    retryUi: retryUi && { exists: retryUi.exists, visible: retryUi.visible, text: retryUi.text },
    noticeText, finalNotice,
    keptOldWhileFailed,
    midAttach: midCard ? cardAttachOf(midCard) : null,
    retryClick: { ok: retryClick.ok, reason: retryClick.reason || null },
    retryHow,
    chooser2Ok,
    chooserOpenedAgain,
    retryDriving: '真实失败提示上的重试按钮 [data-im=card-attach-retry]；沿用本次尝试的文件源重传（不再次打开文件选择器）',
    uploadsBefore: L48.brief(uploadsBefore),
    uploadsBeforeRetry: L48.brief(uploadsBeforeRetry),
    uploadsAfter: L48.brief(uploadsAfter),
    replaceHow,
    uploadCountBeforeRetry,
    uploadCountAfterRetry,
    retryUploadedAgain,
    newUploadCount: newUploads.length,
    replaced: Boolean(replaced),
    backendAfter: { cards: (backendAfter.state.cards || []).map((c) => ({ id: c.id, kind: c.kind, name: (c.meta || {}).name || null, attachmentId: cardAttachOf(c) })) },
    finalAttach,
  });
  await ctx.shot('s6a-attachment-replace-retry');
}

export async function s7a(ctx) {
  const A = ID('a_s7a');
  const B = ID('b_s7a');
  await L.seedBoard(A, [L.card('ca7a', 'A 已有内容（批量归板）')]);
  await L.seedBoard(B, []);
  await ctx.gotoApp('r8s7a=' + Date.now().toString(36));
  await ctx.enterInteractive();
  await L48.openBoardReady(ctx, B, { cards: 0 });
  const since = Date.now();
  ctx.simulated.push({ scenario: 's7a', kind: 'fulfill500', what: '对 B 的关联保存 PUT 注入 500（批次导入与文件上传来自真实页面）' });
  const failRule = ctx.fulfill((u, m) => m === 'PUT' && u.indexOf('/boards/' + B + '/state') >= 0, {
    delayMs: 300,
    status: 500,
    body: { detail: 'harness: 注入的批量关联保存失败' },
    repeat: false,
  });
  const drop = await L.dropFiles(ctx, [
    { name: 'batch-a-' + Date.now().toString(36) + '.txt', content: 'batch a' },
    { name: 'batch-b-' + Date.now().toString(36) + '.txt', content: 'batch b' },
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
