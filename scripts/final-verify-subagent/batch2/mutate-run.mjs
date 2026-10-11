/**
 * 第二批独立对抗性探针 · 突变运行器（可复现）。
 *
 * 用法：
 *   node mutate-run.mjs <mutationName> [vitest 文件过滤串]
 *
 * 行为：
 *   1) 只改产品代码里一处**精确字符串**（必须唯一命中，否则拒绝执行）；
 *   2) 跑指定的探针（前端 vitest / 后端 python）；
 *   3) **无论结果如何**都用 git checkout -- 还原该文件；
 *   4) 打印 vitest / python 的退出码。
 *
 * 这一步只用于证明探针「承重」：不改产品代码的语义、不提交、不推送。
 */
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { tmpdir } from "node:os";
import { fileURLToPath } from "node:url";
import { dirname, join, resolve } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const repo = resolve(here, "..", "..", "..");
const frontend = resolve(repo, "frontend");
const backend = resolve(repo, "backend");
const config = resolve(here, "vitest.probe.config.mjs");
const probe08 = resolve(here, "probe08_impact_gate.py");
const pythonExe = resolve(backend, ".venv", "Scripts", "python.exe");

const IMPACT_SAVE = resolve(repo, "frontend/src/stores/interactive.ts");
const SESSION = resolve(repo, "frontend/src/stores/session.ts");
const INTERACTIVE_STORE_PY = resolve(repo, "backend/src/agent/api/interactive_store.py");
const INTENTS_PY = resolve(repo, "backend/src/agent/interactive/intents.py");
const SUBMISSION_PY = resolve(repo, "backend/src/agent/interactive/submission.py");

const MUTATIONS = {
  // ---- 前端（vitest） ----
  m06_newerCandidate_false: {
    kind: 'vitest', file: IMPACT_SAVE,
    from: "        const newerCandidate = boardLocalRev !== putRev;",
    to: "        const newerCandidate = false; // [MUTATION m06] 临时把旧回执判断改成恒 false",
    desc: "06：旧回执不再被识别为旧回执",
  },
  m07_consume_disabled: {
    kind: 'vitest', file: IMPACT_SAVE,
    from: [
      "        for (const [key, version] of pendingDraftClears) {",
      "          if (Object.prototype.hasOwnProperty.call(drafts.value, key) && (draftKeySeq.get(key) ?? 0) !== version) {",
      "            pendingDraftClears.delete(key);",
      "            continue;",
      "          }",
      "          pendingDraftClears.delete(key);",
      "          clearDraft(key);",
      "        }",
    ].join("\n"),
    to: "        // [MUTATION m07] saveNow 里消化 pendingDraftClears 的那段被临时禁用",
    desc: "07：保存成功后不再消化登记式清除",
  },
  m02_drop_version: {
    kind: 'vitest', file: SESSION,
    from: "        if ((linked || sameAttempt) && sameVersion && sameTopic) {",
    to: "        if ((linked || sameAttempt) && sameTopic) { // [MUTATION m02] 去掉版本条件",
    desc: "02：成功清理不再要求内容版本一致",
  },
  m03_drop_topic: {
    kind: 'vitest', file: SESSION,
    from: "        if ((linked || sameAttempt) && sameVersion && sameTopic) {",
    to: "        if ((linked || sameAttempt) && sameVersion) { // [MUTATION m03] 去掉话题条件",
    desc: "03：成功清理不再要求话题一致",
  },
  // ---- 后端（python + 真实临时库） ----
  m08_drop_server_gate: {
    kind: 'python', file: INTERACTIVE_STORE_PY, script: probe08,
    from: "    elif gate[\"runningIds\"]:",
    to: "    elif False:  # [MUTATION m08] 服务端影响门被临时禁用",
    desc: "08：不带确认改材料时服务端不再拒绝",
  },
  m08_drop_stale_check: {
    kind: 'python', file: INTENTS_PY, script: probe08,
    from: "    if entry.get(\"stateVersion\") != current_seq:",
    to: "    if False:  # [MUTATION m08] 过期确认不再被拒",
    desc: "08：过期 checkId 的确认不再被拒",
  },
  m08_drop_stale_state: {
    kind: 'python', file: SUBMISSION_PY, script: probe08,
    from: "        if requested_version != loaded[\"seq\"]:",
    to: "        if False:  # [MUTATION m08] 过期提交版本不再被拒",
    desc: "08：过期 baseStateVersion 的提交不再被拒",
  },
  // ---- 03 承重链：成功清理的话题条件之外，还有两层更早的守卫 ----
  m03_drop_capture_topic_check: {
    kind: 'vitest', file: SESSION,
    from: "      armed && armed.topicId === host.currentTopicId && !isBlankText(host.draft) ? armed : null;",
    to: "      armed && !isBlankText(host.draft) ? armed : null; // [MUTATION] capture 话题校验被去掉",
    desc: "03：captureAttribution 不再要求重发关联与当前话题一致",
  },
  m03_drop_bind_disarm: {
    kind: 'vitest', file: SESSION,
    from: [
      "     */",
      "    disarmResend();",
      "    const stored = readDraft(nextKey);",
    ].join("\n"),
    to: [
      "     */",
      "    // [MUTATION] bind 里的 disarmResend 被临时禁用",
      "    const stored = readDraft(nextKey);",
    ].join("\n"),
    desc: "03：切话题时不再解除显式重发关联",
  },
  m03_drop_bind_and_capture: {
    kind: 'vitest', file: SESSION,
    edits: [
      {
        from: "      armed && armed.topicId === host.currentTopicId && !isBlankText(host.draft) ? armed : null;",
        to: "      armed && !isBlankText(host.draft) ? armed : null;",
      },
      {
        from: "    disarmResend();\n    const stored = readDraft(nextKey);",
        to: "    // [MUTATION] bind 里的 disarmResend 被临时禁用\n    const stored = readDraft(nextKey);",
      },
    ],
    desc: "03：同时去掉 bind 解除关联与 capture 话题校验（两层一起拆）",
  },
};

const [name, filter] = process.argv.slice(2);
const m = MUTATIONS[name];
if (!m) {
  console.error("未知突变：" + String(name) + "；可用：" + Object.keys(MUTATIONS).join(", "));
  process.exit(2);
}

const original = readFileSync(m.file, "utf8").replace(/\r\n/g, "\n");
const edits = m.edits || [{ from: m.from, to: m.to }];
let mutated = original;
for (const e of edits) {
  const hits = mutated.split(e.from).length - 1;
  if (hits !== 1) {
    console.error("拒绝执行：目标字符串命中 " + hits + " 次（要求恰好 1 次）。文件=" + m.file);
    process.exit(3);
  }
  mutated = mutated.replace(e.from, e.to);
}

let code = 1;
try {
  writeFileSync(m.file, mutated, "utf8");
  console.log("[MUTATION] " + name + " :: " + m.desc);
  console.log("[MUTATION] file=" + m.file);
  if (m.kind === "python") {
    const env = Object.assign({}, process.env, {
      QIO_DATA_DIR: mkdtempSync(join(tmpdir(), "qio-b2-mut-")),
      QIO_DEV_INSECURE: '1',
      QIO_DISABLE_DB_CHECK: '1',
    });
    const run = spawnSync(pythonExe, [m.script], { cwd: backend, stdio: 'inherit', env });
    code = run.status ?? 1;
  } else {
    const args = ["vitest", "run", "--config", config];
    if (filter) args.push(filter);
    const run = spawnSync('npx', args, { cwd: frontend, stdio: 'inherit', shell: true });
    code = run.status ?? 1;
  }
} finally {
  const restore = spawnSync("git", ["checkout", "--", m.file], { cwd: repo, stdio: "inherit", shell: true });
  console.log("[RESTORE] git checkout -- " + m.file + " (exit " + (restore.status ?? "null") + ")");
}
console.log("[MUTATION] exit code = " + code);
process.exit(code === 0 ? 0 : 1);
