/**
 * 验证：反复开关星球之后，入口球的尺度是否稳定（不因过渡帧被写小）。
 *
 * 判据：每一轮"收起完成"后读到的 data-stage-k 必须一致，且不低于正常球态的 0.6
 * （回归前实测会出现 0.34 这种被过渡帧写死的小尺度）。
 */
import { createSession, launchBrowser, runGroup, sleep } from "./lib.mjs";

async function readStage(page) {
  return page.evaluate(() => {
    const stage = document.querySelector(".planet-stage");
    const canvas = stage ? stage.querySelector("canvas") : null;
    return {
      k: stage ? Number(stage.getAttribute("data-stage-k")) : null,
      canvasW: canvas ? canvas.offsetWidth : null,
    };
  });
}

async function clickDock(page) {
  const box = await page.locator(".dock").boundingBox();
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await sleep(100);
  await page.mouse.down();
  await sleep(60);
  await page.mouse.up();
}

async function main() {
  const browser = await launchBrowser();
  const session = await createSession(browser, { group: "probe", theme: "dark" });
  const { page } = session;
  try {
    await session.goto("#/", { settle: 800 });
    await sleep(4000); // 等空闲挂载 + 球态布局就位

    const rows = [];
    for (let round = 0; round < 3; round += 1) {
      await clickDock(page); // 展开
      await sleep(4000);
      await page.keyboard.press("Escape"); // 收起（星球页有 Esc 关闭）
      await sleep(3500);
      const after = await readStage(page);
      rows.push({ round: round + 1, ...after });
      console.log(`[round ${round + 1}] k=${after.k} canvasW=${after.canvasW}`);
    }
    const ks = rows.map((r) => r.k).filter((k) => Number.isFinite(k));
    const stable = ks.length > 0 && Math.max(...ks) - Math.min(...ks) < 0.02;
    const healthy = ks.length > 0 && Math.min(...ks) >= 0.6;
    console.log(`稳定：${stable} / 尺度正常（≥0.6）：${healthy} / 样本 ${JSON.stringify(ks)}`);
    console.log("console errors:", session.consoleErrors.slice(0, 5));
  } finally {
    await session.close();
    await browser.close();
  }
}

runGroup(main);
