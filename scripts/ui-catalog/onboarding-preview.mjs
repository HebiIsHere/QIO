/**
 * 首次引导 v2 预览采集：七步向导逐页截图 + 提交后的落库核对。
 *
 * 前置：e2e_up.py 已起好 8734 / 5199，且数据域里已有可用凭据
 * （新用户规则下"连接模型"这一步不能跳过，所以预览实例复用了旧数据域）。
 */
import path from "node:path";
import { createSession, launchBrowser, runGroup, sleep } from "./lib.mjs";

const API = process.env.QIO_API || "http://127.0.0.1:8734";

async function apiJson(pathname) {
  const resp = await fetch(`${API}${pathname}`);
  return resp.json();
}

async function main() {
  const browser = await launchBrowser();
  const session = await createSession(browser, {
    group: process.env.QIO_PREVIEW_GROUP || "onboarding-v2",
    theme: process.env.QIO_PREVIEW_THEME || "dark",
  });
  const { page } = session;

  try {
    await session.goto("#/", { waitFor: ".onboarding", settle: 600 });
    await session.shot("01-welcome", "第 1 步「欢迎」");

    await page.click(".onboarding-actions .primary");
    await sleep(300);
    await session.shot("02-credential", "第 2 步「连接模型」", {
      note: "新用户不能跳过这一步；已有可用密钥时可以直接继续",
    });
    await page.click(".onboarding-actions .primary");
    await sleep(300);

    const inputs = page.locator(".onboarding-body input.qio-input");
    await inputs.nth(0).fill("祠莎");
    await inputs.nth(1).fill("开发 QIO");
    await inputs.nth(2).fill("学生");
    await session.shot("03-profile", "第 3 步「认识你」");

    await page.click(".onboarding-actions .primary");
    await sleep(300);
    await page.click(".onboarding-body .chip >> text=简洁");
    await page.click(".onboarding-body .chip >> text=先讲逻辑再给结论");
    const scopeInputs = page.locator(".onboarding-body .pref-row input.qio-input");
    await scopeInputs.nth(2).fill("开发 QIO");
    await session.shot("04-preference", "第 4 步「偏好」", {
      note: "每项都能单独指定只在某个话题里生效",
    });

    await page.click(".onboarding-actions .primary");
    await sleep(300);
    await page.fill(".onboarding-body .goal-input input.qio-input", "把 QIO 的记忆问题做完");
    await page.click(".onboarding-body .add-goal");
    await sleep(200);
    await session.shot("05-goal", "第 5 步「目标」", {
      note: "自由填写；每条会新建一个话题，也可以一条都不写",
    });

    await page.click(".onboarding-actions .primary");
    await sleep(5000); // 追问要现问一次模型
    await session.shot("06-followup", "第 6 步「追问」", {
      note: "按你写的内容现问，可以跳过；跳过的不会出现在清单里",
    });

    await page.click(".onboarding-actions .primary");
    await sleep(400);
    await session.shot("07-review", "第 7 步「核对并完成」", {
      note: "核对之前什么都不写；可以逐条修改或删除",
    });

    await page.click(".onboarding-actions .finish");
    await sleep(1200);
    await session.shot("08-after-submit", "提交后回到对话页");

    const knowledge = await apiJson("/api/knowledge");
    const entities = await apiJson("/api/entities");
    const topics = await apiJson("/api/graph/topics");
    console.log(
      `[check] 知识 ${knowledge.knowledge.length} 条；实体 ${entities.entities.length} 张；话题 ${topics.topics.length} 个`,
    );
    console.log(
      "[check] 画像知识的来源/范围：" +
        knowledge.knowledge
          .slice(0, 5)
          .map((k) => `${k.source}/${k.scope}`)
          .join(" | "),
    );
  } finally {
    await session.close();
    await browser.close();
  }
}

runGroup(main);
