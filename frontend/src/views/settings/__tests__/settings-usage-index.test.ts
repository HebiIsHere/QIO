/**
 * 用途目录镜像守卫：前端的用途 key 必须与后端 usages.py 的 USAGE_KEYS 一致。
 *
 * 为什么需要它：搜索索引里的用途名称是前端的一份静态镜像（搜索必须在模型目录
 * 加载之前也能用）。后端加/改用途而前端忘同步时，用户会在搜索里看到不存在的用途，
 * 或者搜不到真实用途 —— 这种错不需要运行时就该被拦下。
 *
 * 实现：读 usages.py 的**源码文本**做正则解析（不 import Python、不要求后端环境）。
 * 找不到该文件时（例如只 checkout 了前端的分支）该用例显式 skip，而不是假装通过。
 */
import { describe, expect, it } from "vitest";
import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { USAGE_KEYS_FOR_INDEX } from "../searchIndex";

/** 工作区相对路径的候选位置（本 worktree / B 的 worktree / 集成 worktree） */
function findUsagesPy(): string | null {
  const candidates = [
    resolve(process.cwd(), "..", "backend", "src", "agent", "credentials", "usages.py"),
    resolve(process.cwd(), "..", "..", "qio-stg-b", "backend", "src", "agent", "credentials", "usages.py"),
    resolve(process.cwd(), "..", "..", "qio-stg", "backend", "src", "agent", "credentials", "usages.py"),
  ];
  return candidates.find((candidate) => existsSync(candidate)) ?? null;
}

function parseUsageKeys(source: string): string[] {
  const start = source.indexOf("USAGES: tuple[UsageSpec, ...]");
  const end = source.indexOf("USAGE_KEYS:");
  const block = start >= 0 && end > start ? source.slice(start, end) : source;
  const keys: string[] = [];
  const pattern = /UsageSpec\(\s*\n\s*"([^"]+)"/g;
  let match: RegExpExecArray | null;
  while ((match = pattern.exec(block)) !== null) keys.push(match[1]);
  return keys;
}

const USAGES_PY = findUsagesPy();
const PARSED_KEYS = USAGES_PY ? parseUsageKeys(readFileSync(USAGES_PY, "utf8")) : null;

describe("搜索索引的用途目录与后端 usages.py 对齐", () => {
  it("前端用途 key 列表集中且顺序固定（基线 12 个预设用途，不含 custom:<tag>）", () => {
    expect(USAGE_KEYS_FOR_INDEX).toEqual([
      "main-chat",
      "general-chat",
      "code",
      "subagent",
      "research",
      "vision",
      "video",
      "audio",
      "embedding",
      "memory-organize",
      "knowledge-distill",
      "entity-extract",
    ]);
    expect(USAGE_KEYS_FOR_INDEX.some((key) => key.startsWith("custom:"))).toBe(false);
  });

  it.skipIf(!USAGES_PY)("等于 usages.py 里 USAGES 的 key 列表（含顺序）", () => {
    // 文件存在但解析不到任何 key，说明解析规则或后端结构变了：必须失败而不是静默跳过
    expect(PARSED_KEYS && PARSED_KEYS.length).toBeGreaterThan(0);
    expect(PARSED_KEYS).toEqual(USAGE_KEYS_FOR_INDEX);
  });
});
