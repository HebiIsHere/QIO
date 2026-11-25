import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const SRC = resolve(process.cwd(), "src");
const view = readFileSync(resolve(SRC, "views/InteractiveView.vue"), "utf8");
const conversation = readFileSync(
  resolve(SRC, "components/interactive/v2/InteractiveConversation.vue"),
  "utf8",
);

describe("互动页：悬浮对话与操作行", () => {
  it("对话不再放在 workspace 的 composer 槽里，板面不会被抬高", () => {
    expect(view).not.toContain("<template #composer>");
    expect(view).not.toContain('data-iw-region="composer"');
    expect(view).toContain('data-im="floating-chat"');
    expect(view).toContain("position: absolute");
  });

  it("对话卡内部：消息在上、操作行居中、输入区在下（三者在同一条 860 轴上）", () => {
    const stream = conversation.indexOf('data-ic-stream');
    const rail = conversation.indexOf('data-ic-rail');
    const composer = conversation.indexOf('class="ic-composer"');
    expect(stream).toBeGreaterThan(-1);
    expect(rail).toBeGreaterThan(stream);
    expect(composer).toBeGreaterThan(rail);
  });

  it("操作行由对话卡的 mode-switch / toolbar 槽承载，提交仍与聊天发送分离", () => {
    expect(view).toContain("<template #mode-switch>");
    expect(view).toContain("<template #toolbar>");
    expect(view).toMatch(/<BoardToolbar[\s\S]{0,200}rail/);
    expect(view).toContain("BoardToolbar");
    expect(view).toContain("InteractiveConversation");
  });
});
