/**
 * 冒烟脚本：验证采集链路通了（浏览器起来、页面能开、事件能注入、截图非空）。
 * 用法（服务需在跑）：
 *   node scripts/ui-catalog/smoke.mjs
 * 产物：frontend/e2e-shots/ui-catalog/smoke/*.png 与 manifest-smoke.json
 */
import { createSession, launchBrowser, runGroup, sleep } from "./lib.mjs";

await runGroup(async () => {
  const browser = await launchBrowser();
  const s = await createSession(browser, { group: "smoke", name: "冒烟", theme: "dark" });
  console.log(`base=${s.base} api=${s.api}`);

  await s.goto("#/", { waitFor: ".conversation" });
  await s.shot("smoke-conversation", "对话页冒烟");

  await s.inject("TURN_START", { turn_id: "smoke_turn_1", revision: 1 });
  await s.inject("ASSISTANT", { content: "这是一段用于冒烟测试的流式回答。" });
  await sleep(600);
  await s.shot("smoke-generating", "流式生成中冒烟");

  // 收尾：把这一轮结束掉，避免退出时界面停在运行态
  await s.inject("TURN_END", { turn_id: "smoke_turn_1", status: "completed", revision: 2 });
  await s.close();
  await browser.close();
  console.log(`\n控制台错误 ${s.consoleErrors.length} 条`);
  for (const e of s.consoleErrors.slice(0, 5)) console.log("  -", e);
});
