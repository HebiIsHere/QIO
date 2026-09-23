/**
 * 首次引导（欢迎页）预览采集。
 *
 * 产出 6 张六步向导图 + 两条新行为的证据图：
 * - 「刚更新到这个版本 → 无论如何展开一次欢迎页」；
 * - 「未完成时的对话页『继续设置』轻提示」。
 *
 * 前置：已用 e2e_up.py 起好后端 8734 / 前端 5199，且 QIO_DATA_DIR 指向干净数据域
 * （数据域路径写在 %TEMP%\qio-preview-datadir.txt，由控制器启动脚本落盘）。
 */
import path from "node:path";
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { createSession, launchBrowser, runGroup, sleep } from "./lib.mjs";

const QIO_ROOT = path.resolve(process.cwd());
const PY = path.join(QIO_ROOT, "backend", ".venv", "Scripts", "python.exe");
const DATA_DIR = readFileSync(
  path.join(process.env.TEMP, "qio-preview-datadir.txt"),
  "utf-8",
).trim();
const DB = path.join(DATA_DIR, "app.db");
const VERSION = "0.1.6";

/** 直接改设置表，模拟「已展示过的版本落后于当前版本」这类历史状态。 */
function setSetting(key, value) {
  const code =
    "import sqlite3,sys\n" +
    "c=sqlite3.connect(sys.argv[1])\n" +
    "c.execute(\"INSERT INTO settings(key,value,updated_at) VALUES(?,?,datetime('now')) \"\n" +
    "          \"ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at\",\n" +
    "          (sys.argv[2], sys.argv[3]))\n" +
    "c.commit()\n" +
    "c.close()\n";
  execFileSync(PY, ["-c", code, DB, key, value]);
}

async function apiStatus() {
  const resp = await fetch(`${process.env.QIO_API || "http://127.0.0.1:8734"}/api/onboarding/status`);
  return resp.json();
}

async function main() {
  const browser = await launchBrowser();
  const session = await createSession(browser, { group: "onboarding", theme: "dark" });
  const { page } = session;

  try {
    // --- 六步向导 -------------------------------------------------------
    await session.goto("#/", { waitFor: ".onboarding", settle: 600 });
    await session.shot("01-welcome", "欢迎页 · 第 1 步「欢迎」", {
      note: "首次启动（或刚更新到本版本）自动展开的全屏向导",
    });

    await page.click(".onboarding-actions .primary");
    await sleep(300);
    await session.shot("02-credential", "第 2 步「连接模型」", {
      note: "复用 identifyCredential 的 API Key 输入，可跳过",
    });

    await page.click(".onboarding-actions .skip");
    await sleep(300);
    const inputs = page.locator(".onboarding-body input.qio-input");
    await inputs.nth(0).fill("小舟");
    await inputs.nth(1).fill("在做本地优先的个人助手");
    await inputs.nth(2).fill("独立开发者");
    await page.click(".onboarding-body .style-chip >> text=简洁");
    await sleep(200);
    await session.shot("03-profile", "第 3 步「认识你」", {
      note: "称呼是完成判定的必填项；标签与回答风格会写进「我」实体卡",
    });

    await page.click(".onboarding-actions .primary");
    await sleep(400);
    await session.shot("04-preference", "第 4 步「偏好」", {
      note: "主题写入 qio-theme，立刻生效",
    });

    await page.click(".onboarding-actions .primary");
    await sleep(300);
    await page.click(".onboarding-body .goal >> text=学习");
    await page.click(".onboarding-body .goal >> text=写作");
    await sleep(200);
    await session.shot("05-goal", "第 5 步「目标」", {
      note: "勾选项会生成种子话题（幂等）",
    });

    await page.click(".onboarding-actions .primary");
    await sleep(300);
    await session.shot("06-done", "第 6 步「完成」", {
      note: "汇总已设置项 + 「进入对话」",
    });

    await page.click(".onboarding-actions .primary");
    await sleep(600);
    const afterComplete = await apiStatus();
    await session.shot("07-after-complete", "完成后回到对话页", {
      note: `done=${afterComplete.done} / welcome_version=${afterComplete.welcome_version}`,
    });

    // --- 新行为 1：刚更新到这个版本 → 强制展开一次 -------------------------
    setSetting("onboarding.welcome_version", "0.1.5");
    const forced = await apiStatus();
    await session.goto("#/", { waitFor: ".onboarding", settle: 700 });
    await session.shot("08-update-forced", "刚更新到本版本 → 强制展开一次欢迎页", {
      note: `done=${forced.done} 但 welcome_version=${forced.welcome_version} ≠ ${forced.app_version} → show_wizard=${forced.show_wizard}`,
    });
    const stamped = await apiStatus();
    console.log(
      `[check] 展开后 welcome_version=${stamped.welcome_version} show_wizard=${stamped.show_wizard}`,
    );

    // --- 新行为 2：未完成时的「继续设置」轻提示 -----------------------------
    setSetting("onboarding.done", "0");
    setSetting("onboarding.hint_dismissed", "0");
    setSetting("onboarding.welcome_version", VERSION);
    await session.goto("#/", { waitFor: ".setup-hint", settle: 600 });
    await session.shot("09-continue-hint", "对话页「继续设置」轻提示", {
      note: "未完成首次引导时可关闭；关闭状态落库",
    });
    await session.shotEl(".setup-hint", "10-continue-hint-detail", "轻提示（局部）");
    await page.click(".setup-hint .close");
    await sleep(500);
    const dismissed = await apiStatus();
    console.log(`[check] 关闭提示后 hint_dismissed=${dismissed.hint_dismissed}`);
    await session.shot("11-hint-dismissed", "关闭提示后的对话页", {
      note: `hint_dismissed=${dismissed.hint_dismissed}`,
    });
  } finally {
    await session.close();
    await browser.close();
  }
}

runGroup(main);
