/**
 * E 路独立核验：针对「基线 40 红」的 11 个既有测试文件的**定向运行配置**。
 *
 * 为什么需要它：这些文件属于产品测试套件（在 frontend/src 内），而 E 被禁止运行全量；
 * 用本配置把 collect 范围**精确限制**在这 11 个文件上，等价于用户给的
 * 「npx vitest run <文件>」定向节点，而不是 npm test（无参）。
 * 本文件只新增，不修改产品代码、不修改仓库既有 vitest.config.ts。
 *
 * 用法（root 指向 worktree 根）：
 *   cd D:\qio-dev\qa-e\frontend
 *   npx vitest run --root '..' -c 'D:\qio-dev\qa-e\scripts\audit-verify-8fa8452\vitest.contract.config.ts'
 *   （加 positional 子串可只跑其中一个文件）
 */
const FE_NM = "D:/qio-dev/qa-e/frontend/node_modules";

export default {
  resolve: {
    alias: {
      pinia: FE_NM + "/pinia",
      vue: FE_NM + "/vue",
      "@vue/test-utils": FE_NM + "/@vue/test-utils",
    },
  },
  test: {
    environment: "jsdom",
    include: [
      "frontend/src/stores/__tests__/d5SendRecovery.test.ts",
      "frontend/src/stores/__tests__/d6SendProtection.test.ts",
      "frontend/src/stores/__tests__/final-b2-reopenResend.test.ts",
      "frontend/src/stores/__tests__/d6FailedSendProtection.verify.test.ts",
      "frontend/src/stores/__tests__/final-b2-sendIdentity.test.ts",
      "frontend/src/stores/__tests__/final-b2-readError.test.ts",
      "frontend/src/stores/__tests__/final-b2-chatTopicDrafts.test.ts",
      "frontend/src/stores/__tests__/d5bFailedSendPersist.verify.test.ts",
      "frontend/src/stores/__tests__/d4ChatDraftRace.verify.test.ts",
      "frontend/src/stores/__tests__/d6LeadNoSilentTrim.verify.test.ts",
      "frontend/src/stores/__tests__/d5FailedSendRecovery.verify.test.ts",
    ],
  },
};
