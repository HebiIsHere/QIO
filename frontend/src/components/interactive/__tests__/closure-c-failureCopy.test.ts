/**
 * 失败文案的人话化（C 本轮新增）。
 *
 * 触发（实测）：底层把服务端的真实原因原样带上来，界面直接铺出去 —— 亮色实机里出现过
 * `/api/interactive/boards/board_default/submissions -> 500: …` 这种带接口路径与状态码的原文，
 * 以及 `保存前的影响预判没有完成，本次未提交（这次保存前的影响预判没有完成，保存已暂停（…））`
 * 这种套了两层的句子。这里锁住三件事：不出现开发术语、不再重复包裹、空输入不编造原因。
 */
import { describe, expect, it } from "vitest";
import { humanizeFailure, scrubInternalTerms } from "../displayText";

const FORBIDDEN = ["/api/", "stale_check", "stale_state", "impact_confirmation_required", "checkId", "seq"];

describe("失败原因分层", () => {
  it("剥掉传输层外壳，只留真实原因", () => {
    const result = humanizeFailure("/api/interactive/boards/board_default/submissions -> 500: 服务端处理这次提交时出错了");
    expect(result.short).toBe("服务端处理这次提交时出错了");
    expect(result.detail).not.toContain("/api/");
  });

  it("套了两层的包裹合成一句，不再重复同一件事", () => {
    const nested =
      "保存前的影响预判没有完成，本次未提交（这次保存前的影响预判没有完成，保存已暂停" +
      "（/api/interactive/boards/board_default/state -> 500: 磁盘写满））";
    const result = humanizeFailure(nested);
    expect(result.short).toBe("保存前的影响预判没有完成；磁盘写满");
    expect(result.short.match(/保存前的影响预判没有完成/g)!.length).toBe(1);
  });

  it("只有外层说明、没有内层原因时，就用那句说明，不编造状态码", () => {
    const result = humanizeFailure("等待确认期间板面又有改动，已重新核实这次改动的影响");
    expect(result.short).toBe("等待确认期间板面又有改动，已经重新核实");
    expect(result.short).not.toContain("服务端");
  });

  it("内部错误代码换成用户能懂的说法", () => {
    expect(humanizeFailure("/api/x -> 409: stale_check").short).toBe("这次改动的确认已经过期");
    expect(humanizeFailure("/api/x -> 409: stale_state").short).toBe("板面版本已经变了");
  });

  it("只有状态码、没有正文时按状态给一句人话，不编造细节", () => {
    expect(humanizeFailure("/api/x -> 500: ").short).toContain("服务端这次没能处理成功");
    expect(humanizeFailure("/api/x -> 401: ").short).toContain("本机接口拒绝了这次请求");
  });

  it("没有原因时如实说没有拿到，不瞎猜", () => {
    expect(humanizeFailure(null).short).toBe("没有拿到失败原因");
    expect(humanizeFailure("").short).toBe("没有拿到失败原因");
  });

  it("默认不截断：真实原因完整保留（提交区默认区要读得到）", () => {
    const long = "网络中断：" + "这是一段很长的失败原因说明。".repeat(30);
    expect(humanizeFailure(long).short).toBe(long);
  });

  it("maxShort > 0 时给窄位用短句（顶部保存状态行）", () => {
    const long = "网络中断：" + "这是一段很长的失败原因说明。".repeat(30);
    expect(humanizeFailure(long, { maxShort: 30 }).short.length).toBeLessThanOrEqual(31);
    expect(humanizeFailure(long, { maxShort: 30 }).detail).toBe(long);
  });

  it("任何输出都不出现开发术语", () => {
    const samples = [
      "/api/interactive/boards/x/submissions -> 500: boom",
      "/api/x -> 409: stale_check",
      "/api/x -> 409: stale_state",
      "/api/x -> 409: impact_confirmation_required",
      "保存前的影响预判没有完成，本次未提交（这次保存前的影响预判没有完成，保存已暂停（/api/x -> 500: draft_too_long））",
    ];
    for (const sample of samples) {
      const { short, detail } = humanizeFailure(sample);
      for (const term of FORBIDDEN) {
        expect(short.includes(term), sample + " -> short 含 " + term).toBe(false);
        expect(detail.includes(term), sample + " -> detail 含 " + term).toBe(false);
      }
    }
  });

  it("scrubInternalTerms 供详情区复用：接口路径与内部代码一律换掉", () => {
    const scrubbed = scrubInternalTerms("保存失败原因：/api/interactive/boards/x/state -> 500: stale_check 已保留");
    expect(scrubbed).not.toContain("/api/");
    expect(scrubbed).not.toContain("stale_check");
    expect(scrubbed).toContain("这次改动的确认已经过期");
  });
});
