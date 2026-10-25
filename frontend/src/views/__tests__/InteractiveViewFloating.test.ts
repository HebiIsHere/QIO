import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const SRC = resolve(process.cwd(), "src");
const view = readFileSync(resolve(SRC, "views/InteractiveView.vue"), "utf8");
const conversation = readFileSync(
  resolve(SRC, "components/interactive/v2/InteractiveConversation.vue"),
  "utf8",
);

describe("互动页：悬浮对话与工具栏", () => {
  it("对话不再放在 workspace 的 composer 槽里，板面不会被抬高", () => {
    expect(view).not.toContain('<template #composer>');
    expect(view).not.toContain('data-iw-region="composer"');
    expect(view).toContain('data-im="floating-chat"');
    expect(view).toContain("position: absolute");
  });

  it("对话卡内部：消息在上、工具栏和输入区在下且不移动", () => {
    const stream = conversation.indexOf('class="ic-stream"');
    const toolbar = conversation.indexOf('class="ic-toolbar"');
    const composer = conversation.indexOf('class="ic-composer"');
    expect(stream).toBeGreaterThan(-1);
    expect(toolbar).toBeGreaterThan(stream);
    expect(composer).toBeGreaterThan(toolbar);
  });

  it("工具栏由对话卡的 toolbar 槽承载，且提交仍与聊天发送分离", () => {
    expect(view).toContain('<template #toolbar>');
    expect(view).toContain("floating");
    expect(view).toContain("BoardToolbar");
    expect(view).toContain("InteractiveConversation");
  });
});
