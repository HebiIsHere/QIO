/**
 * 把一次小规模采集（--label=X 产生的 X-report-partial.json）合并进全量报告。
 * 用途：某个场景那一格采集失败/未就绪时，只重跑那一格再并回去，不必重跑全部。
 *
 * 用法：node scripts/closure-c-verify/merge.mjs --into=after --from=afterco,co800
 */
import { existsSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { basename, dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const arg = (name, fallback) => {
  const hit = process.argv.find((a) => a.startsWith("--" + name + "="));
  return hit ? hit.split("=").slice(1).join("=") : fallback;
};
const into = arg("into", "after");
const sources = arg("from", "").split(",").filter(Boolean);
const dir = join(here, "shots");
const targetFile = join(dir, into + "-report.json");
const target = JSON.parse(readFileSync(targetFile, "utf8"));

for (const source of sources) {
  const partFile = join(dir, source + "-report-partial.json");
  if (!existsSync(partFile)) {
    console.log("跳过（不存在）：" + partFile);
    continue;
  }
  const part = JSON.parse(readFileSync(partFile, "utf8"));
  for (const run of part.runs) {
    const tag = into + "-" + run.tag.replace(new RegExp("^" + source + "-"), "");
    const index = target.runs.findIndex((item) => item.tag === tag);
    const oldShot = run.shot;
    const newShot = join(dir, into, basename(oldShot).replace(new RegExp("^" + source + "-"), into + "-"));
    if (existsSync(oldShot) && oldShot !== newShot) renameSync(oldShot, newShot);
    const merged = { ...run, tag, shot: newShot };
    if (index >= 0) target.runs[index] = merged;
    else target.runs.push(merged);
    console.log("合并 " + tag);
  }
  target.httpFails = [...new Set([...(target.httpFails || []), ...(part.httpFails || [])])];
}
writeFileSync(targetFile, JSON.stringify(target, null, 2), "utf8");
console.log("已写出 " + targetFile + "（场景数 " + target.runs.length + "）");
