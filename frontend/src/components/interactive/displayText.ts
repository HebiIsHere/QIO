/**
 * 失败文案的人话化（C 本轮新增，只做**展示层**）。
 *
 * 为什么需要它：底层（services/interactive.ts + stores/interactive.ts）如实保留了服务端的
 * 真实原因，但那条字符串的**外壳**是给排查用的：`/api/interactive/boards/xxx/submissions -> 500: …`。
 * 直接铺在界面上会有三个问题（本轮验收点 4）：
 * 1. 出现 `/api/…`、`stale_check`、`checkId`、状态码前缀这类开发术语；
 * 2. 多层包裹重复（实测：`保存前的影响预判没有完成，本次未提交（这次保存前的影响预判没有完成，保存已暂停（…））`）；
 * 3. 主要决定（「出了什么事」）与辅助说明（完整原文）混在一行里，用户读不出层级。
 *
 * 这里做三件事，且**不改变任何数据**：解开重复包裹 → 去掉传输层外壳 / 内部代码 → 拆成
 * 「一句短原因（短） + 完整原文（详情）」。原始字符串仍然可由调用方放进 `title` 或详情区。
 */

/** 服务端返回的内部错误代码（M4 定稿）→ 用户能懂的说法 */
const CODE_TEXT: Record<string, string> = {
  stale_check: "这次改动的确认已经过期",
  stale_state: "板面版本已经变了",
  impact_confirmation_required: "改动会影响正在执行的任务，需要先确认",
  draft_too_long: "草稿太长了",
  submission_failed: "服务端没能处理这次提交",
  no_credentials: "还没有配置可用的模型凭据",
  not_found: "这项内容在服务端已经不存在了",
};

/**
 * 重复包裹：底层为了「说清是谁没成功」在句子外面又套了一层。
 * 解开后把外层那句话留作前缀（它是有用的一层信息），内层继续往下解。
 * 只解 4 层：真实数据不会更深，无限递归反而会掩盖异常输入。
 */
const WRAPPERS: { pattern: RegExp; keep: string }[] = [
  { pattern: /^这次保存前的影响预判没有完成，保存已暂停（([\s\S]+)）$/, keep: "保存前的影响预判没有完成" },
  { pattern: /^板面没有保存成功，本次未提交（([\s\S]+)）$/, keep: "板面没有保存成功" },
  { pattern: /^这次改动的影响确认已经过期，请重新确认（([\s\S]+)）$/, keep: "这次改动的确认已经过期，需要重新确认" },
  // 没有内层、本身就是一句完整说明的情况：整句留下来，后面的原因由词组拼接
  { pattern: /^等待确认期间板面又有改动，已重新核实这次改动的影响$/, keep: "等待确认期间板面又有改动，已经重新核实" },
  { pattern: /^(.{2,24}?)，本次未提交（([\s\S]+)）$/, keep: "" },
];

/** `/api/interactive/boards/x/state -> 500: 具体原因` 这层传输外壳 */
const TRANSPORT = /^(\/[^\s]*?)\s*->\s*(\d{3})\s*:\s*([\s\S]*)$/;

function unwrap(text: string): { text: string; prefixes: string[] } {
  let current = text.trim();
  const prefixes: string[] = [];
  for (let round = 0; round < 4; round += 1) {
    let matched = false;
    for (const wrapper of WRAPPERS) {
      const hit = wrapper.pattern.exec(current);
      if (!hit) continue;
      // 各条包裹正则的捕获组位置不统一（有的第 1 组、有的第 2 组），这里统一取「最后一个存在的组」。
      // 之前只读 hit[2]，于是第 1 组的包裹全部被当成「没有内层」跳过，重复包裹根本没解开（单测抓到的真实缺陷）。
      const groups = hit.slice(1).filter((value) => typeof value === "string");
      const inner = (groups[groups.length - 1] ?? "").trim();
      if (!inner) {
        // 没有内层的终态说明：整句作为前缀留下
        if (wrapper.keep && !prefixes.includes(wrapper.keep)) prefixes.push(wrapper.keep);
        current = "";
        matched = true;
        break;
      }
      if (wrapper.keep && !inner.startsWith(wrapper.keep)) prefixes.push(wrapper.keep);
      current = inner;
      matched = true;
      break;
    }
    if (!matched) break;
  }
  return { text: current, prefixes: [...new Set(prefixes)] };
}

/** 把内部代码 / 接口路径 / 传输外壳换成用户能读的说法（详情区也用这个，避免从侧门漏出去） */
export function scrubInternalTerms(input: string | null | undefined): string {
  let text = String(input ?? "");
  // 去掉接口路径：/api/interactive/... 一直到空白或中文标点
  text = text.replace(/\/api\/[^\s，。；、）)]*/g, "本机接口");
  // 传输外壳：本机接口 -> 500: 原因
  text = text.replace(/本机接口\s*->\s*(\d{3})\s*:\s*/g, "");
  text = text.replace(/\/\s*->\s*(\d{3})\s*:\s*/g, "");
  for (const [code, human] of Object.entries(CODE_TEXT)) {
    text = text.split(code).join(human);
  }
  return text.replace(/\s+/g, " ").trim();
}

function statusPhrase(status: string): string {
  if (status === "401" || status === "403") return "本机接口拒绝了这次请求（会话令牌可能已失效）";
  if (status === "404") return "这项内容在服务端已经不存在了";
  if (status === "409") return "服务端的板面版本和这次不一致";
  if (status === "429") return "请求太频繁，请稍后再试";
  if (Number(status) >= 500) return `服务端这次没能处理成功（${status}）`;
  return `服务端拒绝了这次请求（${status}）`;
}

/**
 * 「主要决定」那一行：
 * - maxShort = 0（默认）＝**不截断**，完整保留真实原因（提交区默认区用它：那里有高度上限与区内滚动，
 *   完整原因本身就是用户要读的东西；既有测试也要求「长原因完整保留在 DOM 里」）；
 * - maxShort > 0 ＝ 取第一句并在超长时截断（顶部保存状态行用它：那一行是 nowrap 的窄位）。
 */
function shortLine(text: string, maxShort: number): string {
  const chosen = text.trim();
  if (maxShort <= 0 || chosen.length <= maxShort) return chosen;
  const piece = chosen.split(/[。；!?！？]/).map((part) => part.trim()).filter(Boolean)[0] ?? chosen;
  return piece.length > maxShort ? piece.slice(0, maxShort) + "…" : piece;
}

export interface HumanFailure {
  /** 主要决定用：一句短原因（无开发术语、无重复包裹） */
  short: string;
  /** 辅助说明用：解开包裹后的完整原文（已去掉接口路径与内部代码） */
  detail: string;
}

/**
 * 失败原因的层级化展示。
 * - 输入为空 → 如实说「没有拿到原因」，不编造；
 * - 只有传输层外壳（没有正文）→ 按状态码给一句人话；
 * - 其余 → short 给主要决定那一行（默认不截断，完整保留真实原因），detail 给辅助说明。
 */
export function humanizeFailure(
  raw: string | null | undefined,
  options: { maxShort?: number } = {},
): HumanFailure {
  const initial = String(raw ?? "").trim();
  if (!initial) return { short: "没有拿到失败原因", detail: "" };
  const { text: unwrapped, prefixes } = unwrap(initial);
  const transport = TRANSPORT.exec(unwrapped.trim());
  const status = transport ? transport[2] : "";
  let body = scrubInternalTerms(transport ? transport[3] : unwrapped);
  if (CODE_TEXT[body]) body = CODE_TEXT[body];
  if (!body) {
    // 只解出了外层说明（例如「等待确认期间板面又有改动」）：它就是这次要说的原因，别再编一句状态码
    body = prefixes.length ? prefixes.join("；") : statusPhrase(status);
    prefixes.length = 0;
  }
  const short = shortLine([...prefixes, body].join("；"), options.maxShort ?? 0);
  const detail = [prefixes.join("；"), body].filter(Boolean).join("；");
  return { short, detail };
}
