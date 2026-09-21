/**
 * QIO 原子控件画廊：把五个 ui/ 组件（QInput / QSelect / QNumber / QSlider / QConfirm）
 * 的**全部状态**并排渲染出来，供 `scripts/ui-catalog/atoms.mjs` 逐个截图。
 *
 * 为什么需要这一页：这些控件的边界态（禁用、错误、空选项、上限夹取、危险档确认层）
 * 在真实页面里大多到不了 —— 要么需要特定数据，要么会被交互挡住。穷举截图的单位是「状态」，
 * 所以这里用产品自己的组件 + 产品自己的令牌把状态**真实**摆出来，不复制样式、不写死颜色。
 *
 * 约定（采集脚本依赖）：
 * - 每个状态一个 `<section id="<状态 id>">`，带 `data-group` 与 `data-state`；
 * - 区块内 `h2` 是中文标题，`p.note` 是一句说明 —— 截图里能直接读到「这是什么状态」；
 * - 需要交互才成立的状态（聚焦 / 打开菜单 / 高亮 / 确认层）由采集脚本操作，
 *   这一页只负责把控件和**可点的触发按钮**摆好。
 *
 * 注意：这一页在产品构建之外（见 ui-catalog.html 的注释），不进 src/。
 */
import { createApp, defineComponent, h, ref, type VNodeChild } from "vue";
import "../src/styles/tokens.css";
import "../src/styles/base.css";
import QInput from "../src/components/ui/QInput.vue";
import QSelect from "../src/components/ui/QSelect.vue";
import QNumber from "../src/components/ui/QNumber.vue";
import QSlider from "../src/components/ui/QSlider.vue";
import QConfirm from "../src/components/ui/QConfirm.vue";
import { applyTheme, getThemePreference, resolveTheme } from "../src/utils/theme";
import { applyMotion, getMotionPreference, resolveMotion } from "../src/utils/motion";

// 与产品同一套启动逻辑：主题偏好来自 localStorage(qio-theme)，
// 采集脚本靠 addInitScript 预置它 —— 不预置就会拍出「暗色图是浅色」。
applyTheme(resolveTheme(getThemePreference()));
applyMotion(resolveMotion(getMotionPreference()));
document.documentElement.dataset.gallery = "atoms";

interface BlockSpec {
  /** 截图定位用的稳定 id（也是 manifest 里的文件名前缀） */
  id: string;
  /** 控件名（QInput / QSelect / …） */
  group: string;
  /** 中文标题：截图里能直接读懂这是什么状态 */
  title: string;
  /** 一句说明：这个状态为什么长这样 */
  note: string;
  /** 机器可读的状态名（default / error / disabled / open …） */
  state: string;
  setup: () => () => VNodeChild;
}

const SIZE_OPTIONS = [
  { value: "max", label: "上限（不限制）" },
  { value: "high", label: "高（更慢、更稳）" },
  { value: "medium", label: "标准" },
  { value: "low", label: "省电" },
];

const BLOCKS: BlockSpec[] = [
  // ---------------------------------------------------------------- QInput
  {
    id: "atoms-qinput-default",
    group: "QInput",
    title: "默认（带值）",
    note: "输入框的常态：常规字符、常规字重。",
    state: "default",
    setup: () => {
      const v = ref("QIO");
      return () => h(QInput, { modelValue: v.value, "onUpdate:modelValue": (x: string) => (v.value = x) });
    },
  },
  {
    id: "atoms-qinput-placeholder",
    group: "QInput",
    title: "占位（空值）",
    note: "没有值时显示占位文案，颜色比正文弱。",
    state: "placeholder",
    setup: () => {
      const v = ref("");
      return () =>
        h(QInput, {
          modelValue: v.value,
          placeholder: "和 QIO 说点什么…",
          "onUpdate:modelValue": (x: string) => (v.value = x),
        });
    },
  },
  {
    id: "atoms-qinput-focus",
    group: "QInput",
    title: "聚焦",
    note: "焦点环 + 描边用强调色（截图前由脚本把焦点放进来）。",
    state: "focus",
    setup: () => {
      const v = ref("api.example.com");
      return () => h(QInput, { modelValue: v.value, "onUpdate:modelValue": (x: string) => (v.value = x) });
    },
  },
  {
    id: "atoms-qinput-error",
    group: "QInput",
    title: "校验错误",
    note: "error=true：描边与文字走危险色，并带 aria-invalid。",
    state: "error",
    setup: () => {
      const v = ref("http://plain-http.example.com/v1");
      return () =>
        h(QInput, { modelValue: v.value, error: true, "onUpdate:modelValue": (x: string) => (v.value = x) });
    },
  },
  {
    id: "atoms-qinput-mono",
    group: "QInput",
    title: "等宽（mono）",
    note: "密钥指纹、标识符这类要逐字符读的值走等宽字。",
    state: "mono",
    setup: () => {
      const v = ref("0f8a2b41c7e93d55");
      return () => h(QInput, { modelValue: v.value, mono: true, "onUpdate:modelValue": (x: string) => (v.value = x) });
    },
  },
  {
    id: "atoms-qinput-password",
    group: "QInput",
    title: "密码（type=password）",
    note: "密钥输入：字符被遮住，但仍是同一种输入框。",
    state: "password",
    setup: () => {
      const v = ref("sk-live-8f31c2");
      return () =>
        h(QInput, { modelValue: v.value, type: "password", "onUpdate:modelValue": (x: string) => (v.value = x) });
    },
  },
  {
    id: "atoms-qinput-disabled",
    group: "QInput",
    title: "禁用",
    note: "不可编辑的只读展示态（整体降透明度，光标不变成文本）。",
    state: "disabled",
    setup: () => () => h(QInput, { modelValue: "由服务端下发", disabled: true }),
  },

  // ---------------------------------------------------------------- QSelect
  {
    id: "atoms-qselect-closed",
    group: "QSelect",
    title: "关闭（未选）",
    note: "收起态：显示当前值，没有值时是空行 + 右侧箭头。",
    state: "closed",
    setup: () => {
      const v = ref("");
      return () => h(QSelect, { options: SIZE_OPTIONS, modelValue: v.value, "onUpdate:modelValue": (x: string) => (v.value = x) });
    },
  },
  {
    id: "atoms-qselect-open",
    group: "QSelect",
    title: "打开（菜单展开）",
    note: "点开后的菜单：浮在上方、每项一行（截图前由脚本点开）。",
    state: "open",
    setup: () => {
      const v = ref("");
      return () => h(QSelect, { options: SIZE_OPTIONS, modelValue: v.value, "onUpdate:modelValue": (x: string) => (v.value = x) });
    },
  },
  {
    id: "atoms-qselect-highlight",
    group: "QSelect",
    title: "打开 + 键盘高亮",
    note: "方向键移动的高亮项与「已选中」是两种标记，视觉上必须能区分（脚本用 ↓ 造出高亮）。",
    state: "highlight",
    setup: () => {
      const v = ref("");
      return () => h(QSelect, { options: SIZE_OPTIONS, modelValue: v.value, "onUpdate:modelValue": (x: string) => (v.value = x) });
    },
  },
  {
    id: "atoms-qselect-selected",
    group: "QSelect",
    title: "已选中（收起）",
    note: "选中之后收起：显示的是选项文案而不是内部值。",
    state: "selected",
    setup: () => {
      const v = ref("medium");
      return () => h(QSelect, { options: SIZE_OPTIONS, modelValue: v.value, "onUpdate:modelValue": (x: string) => (v.value = x) });
    },
  },
  {
    id: "atoms-qselect-empty",
    group: "QSelect",
    title: "空选项",
    note: "没有可选项时不展开菜单（点了也不响应），仍保持可聚焦。",
    state: "empty",
    setup: () => () => h(QSelect, { options: [], modelValue: "" }),
  },
  {
    id: "atoms-qselect-disabled",
    group: "QSelect",
    title: "禁用",
    note: "整体降透明度，点击与方向键都不响应。",
    state: "disabled",
    setup: () => () => h(QSelect, { options: SIZE_OPTIONS, modelValue: "high", disabled: true }),
  },

  // ---------------------------------------------------------------- QNumber
  {
    id: "atoms-qnumber-default",
    group: "QNumber",
    title: "常规",
    note: "数值 + 右侧 ＋/− 步进（步进按 step 走）。",
    state: "default",
    setup: () => {
      const v = ref<number | null>(12);
      return () => h(QNumber, { modelValue: v.value, min: 1, max: 50, "onUpdate:modelValue": (x: number | null) => (v.value = x) });
    },
  },
  {
    id: "atoms-qnumber-unit",
    group: "QNumber",
    title: "带单位",
    note: "单位紧贴数值右侧，读「24 小时」不用回头看标题。",
    state: "unit",
    setup: () => {
      const v = ref<number | null>(24);
      return () => h(QNumber, { modelValue: v.value, min: 1, max: 72, unit: "小时", "onUpdate:modelValue": (x: number | null) => (v.value = x) });
    },
  },
  {
    id: "atoms-qnumber-empty",
    group: "QNumber",
    title: "空值",
    note: "没有值时输入框为空（而不是显示 0）—— 空与 0 是两件事。",
    state: "empty",
    setup: () => {
      const v = ref<number | null>(null);
      return () => h(QNumber, { modelValue: v.value, placeholder: "不限制", min: 0, max: 100, "onUpdate:modelValue": (x: number | null) => (v.value = x) });
    },
  },
  {
    id: "atoms-qnumber-focus",
    group: "QNumber",
    title: "聚焦",
    note: "聚焦时整个步进器一起亮（焦点环用强调色，脚本把焦点放进来）。",
    state: "focus",
    setup: () => {
      const v = ref<number | null>(6);
      return () => h(QNumber, { modelValue: v.value, min: 0, max: 20, "onUpdate:modelValue": (x: number | null) => (v.value = x) });
    },
  },
  {
    id: "atoms-qnumber-clamp",
    group: "QNumber",
    title: "上限夹取",
    note: "已经到 max=10：脚本会点一次 ＋，值仍然停在 10（不会越过上限）。",
    state: "clamp",
    setup: () => {
      const v = ref<number | null>(10);
      return () => h(QNumber, { modelValue: v.value, min: 1, max: 10, "onUpdate:modelValue": (x: number | null) => (v.value = x) });
    },
  },
  {
    id: "atoms-qnumber-disabled",
    group: "QNumber",
    title: "禁用",
    note: "输入框与两个步进按钮一起禁用。",
    state: "disabled",
    setup: () => () => h(QNumber, { modelValue: 8, min: 0, max: 99, disabled: true }),
  },

  // ---------------------------------------------------------------- QSlider
  {
    id: "atoms-qslider-zero",
    group: "QSlider",
    title: "0（最小）",
    note: "已填充段为空，滑块在最左。",
    state: "zero",
    setup: () => {
      const v = ref(0);
      return () => h(QSlider, { modelValue: v.value, label: "动画速度", "onUpdate:modelValue": (x: number) => (v.value = x) });
    },
  },
  {
    id: "atoms-qslider-mid",
    group: "QSlider",
    title: "中值",
    note: "轨道左半段是强调色（已填充），右半段是弱边框色。",
    state: "mid",
    setup: () => {
      const v = ref(0.5);
      return () => h(QSlider, { modelValue: v.value, label: "动画速度", "onUpdate:modelValue": (x: number) => (v.value = x) });
    },
  },
  {
    id: "atoms-qslider-full",
    group: "QSlider",
    title: "1（最大）",
    note: "滑块在最右，整条轨道都是已填充色。",
    state: "full",
    setup: () => {
      const v = ref(1);
      return () => h(QSlider, { modelValue: v.value, label: "动画速度", "onUpdate:modelValue": (x: number) => (v.value = x) });
    },
  },
  {
    id: "atoms-qslider-disabled",
    group: "QSlider",
    title: "禁用",
    note: "整体降透明度、指针不再提示可拖动。",
    state: "disabled",
    setup: () => () => h(QSlider, { modelValue: 0.4, label: "动画速度", disabled: true }),
  },

  // ---------------------------------------------------------------- QConfirm
  {
    id: "atoms-qconfirm-inline-normal",
    group: "QConfirm",
    title: "inline 档 · 普通",
    note: "就地展开在条目里（中等风险）：确认按钮用品牌主操作色。",
    state: "inline-normal",
    setup: () => {
      const open = ref(false);
      return () =>
        h("div", { class: "confirm-demo" }, [
          !open.value
            ? h(
                "button",
                { class: "qio-btn", type: "button", "data-role": "trigger", onClick: () => (open.value = true) },
                "归档这条知识",
              )
            : null,
          h(QConfirm, {
            open: open.value,
            variant: "inline",
            title: "归档这条知识？",
            detail: "归档后不再参与回答，可以随时恢复。",
            confirmText: "归档",
            cancelText: "取消",
            onConfirm: () => (open.value = false),
            onCancel: () => (open.value = false),
          }),
        ]);
    },
  },
  {
    id: "atoms-qconfirm-inline-danger",
    group: "QConfirm",
    title: "inline 档 · 危险",
    note: "tone=danger：确认按钮改中性实心 —— 危险动作不该长得像被推荐的默认选择。",
    state: "inline-danger",
    setup: () => {
      const open = ref(false);
      return () =>
        h("div", { class: "confirm-demo" }, [
          !open.value
            ? h(
                "button",
                { class: "qio-btn", type: "button", "data-role": "trigger", onClick: () => (open.value = true) },
                "清除配置",
              )
            : null,
          h(QConfirm, {
            open: open.value,
            variant: "inline",
            tone: "danger",
            title: "清除这个搜索通道的配置？",
            detail: "清除后需要重新填写地址与密钥。",
            confirmText: "清除",
            cancelText: "取消",
            onConfirm: () => (open.value = false),
            onCancel: () => (open.value = false),
          }),
        ]);
    },
  },
  {
    id: "atoms-qconfirm-popover-normal",
    group: "QConfirm",
    title: "popover 档 · 普通",
    note: "浮在触发点附近的小浮层（可逆但有影响）。",
    state: "popover-normal",
    setup: () => {
      const open = ref(false);
      return () =>
        h("div", { class: "confirm-demo" }, [
          !open.value
            ? h(
                "button",
                { class: "qio-btn", type: "button", "data-role": "trigger", onClick: () => (open.value = true) },
                "替换密钥",
              )
            : null,
          h(QConfirm, {
            open: open.value,
            variant: "popover",
            title: "替换成新的密钥？",
            detail: "旧密钥立即失效。",
            confirmText: "替换",
            cancelText: "取消",
            onConfirm: () => (open.value = false),
            onCancel: () => (open.value = false),
          }),
        ]);
    },
  },
  {
    id: "atoms-qconfirm-popover-danger",
    group: "QConfirm",
    title: "popover 档 · 危险",
    note: "同样一个浮层，危险档的确认按钮换成中性实心。",
    state: "popover-danger",
    setup: () => {
      const open = ref(false);
      return () =>
        h("div", { class: "confirm-demo" }, [
          !open.value
            ? h(
                "button",
                { class: "qio-btn", type: "button", "data-role": "trigger", onClick: () => (open.value = true) },
                "撤销凭据",
              )
            : null,
          h(QConfirm, {
            open: open.value,
            variant: "popover",
            tone: "danger",
            title: "撤销这项凭据？",
            detail: "撤销后依赖它的能力立刻不可用。",
            confirmText: "撤销",
            cancelText: "取消",
            onConfirm: () => (open.value = false),
            onCancel: () => (open.value = false),
          }),
        ]);
    },
  },
  {
    id: "atoms-qconfirm-layer-normal",
    group: "QConfirm",
    title: "layer 档 · 普通",
    note: "带遮罩的轻量浮动层（真正需要「停下来看一眼」的动作）；截图取整屏。",
    state: "layer-normal",
    setup: () => {
      const open = ref(false);
      return () =>
        h("div", { class: "confirm-demo" }, [
          h(
            "button",
            { class: "qio-btn", type: "button", "data-role": "trigger", onClick: () => (open.value = true) },
            "打开确认层（普通）",
          ),
          h(QConfirm, {
            open: open.value,
            variant: "layer",
            title: "覆盖保存？",
            detail: "同名文件会被这一次的内容替换。",
            confirmText: "覆盖保存",
            cancelText: "取消",
            onConfirm: () => (open.value = false),
            onCancel: () => (open.value = false),
          }),
        ]);
    },
  },
  {
    id: "atoms-qconfirm-layer-danger",
    group: "QConfirm",
    title: "layer 档 · 危险",
    note: "删除凭据这类高风险动作：遮罩 + 焦点陷阱，确认按钮中性实心；截图取整屏。",
    state: "layer-danger",
    setup: () => {
      const open = ref(false);
      return () =>
        h("div", { class: "confirm-demo" }, [
          h(
            "button",
            { class: "qio-btn", type: "button", "data-role": "trigger", onClick: () => (open.value = true) },
            "打开确认层（危险）",
          ),
          h(QConfirm, {
            open: open.value,
            variant: "layer",
            tone: "danger",
            title: "删除这项凭据？",
            detail: "密钥会从系统凭据库中删除，无法撤销。",
            confirmText: "删除",
            cancelText: "取消",
            onConfirm: () => (open.value = false),
            onCancel: () => (open.value = false),
          }),
        ]);
    },
  },
];

const STYLE = `
  html, body { margin: 0; background: var(--bg-base); color: var(--text-primary); }
  .gal-head { padding: 28px 32px 8px; }
  .gal-head h1 { margin: 0 0 6px; font-family: var(--serif); font-size: 22px; }
  .gal-head p { margin: 0; color: var(--text-secondary); font-size: 13px; }
  .gal-grid { padding: 20px 32px 80px; display: grid; gap: 18px; grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .blk { background: var(--bg-surface); border: 1px solid var(--border-subtle); border-radius: 14px; padding: 16px 18px 20px; }
  .blk-head { display: flex; align-items: baseline; gap: 10px; margin-bottom: 4px; }
  .blk-group { font-family: var(--mono); font-size: 11px; letter-spacing: .08em; color: var(--accent);
               border: 1px solid currentColor; border-radius: 999px; padding: 1px 8px; }
  .blk h2 { margin: 0; font-size: 14px; font-weight: 600; }
  .blk .note { margin: 4px 0 14px; color: var(--text-secondary); font-size: 12px; line-height: 1.5; }
  .stage { min-height: 44px; display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
  .stage .qio-input, .stage .confirm-demo { min-width: 260px; }
  .confirm-demo { display: flex; align-items: center; }
`;

function boot() {
  const style = document.createElement("style");
  style.textContent = STYLE;
  document.head.appendChild(style);

  const root = document.getElementById("gallery");
  if (!root) throw new Error("缺少 #gallery 容器");

  const head = document.createElement("header");
  head.className = "gal-head";
  head.innerHTML =
    "<h1>QIO 原子控件画廊</h1><p>五个 ui/ 组件的全状态并排展示 —— 只用于 UI 穷举截图，不是产品页面。</p>";
  root.appendChild(head);

  const grid = document.createElement("div");
  grid.className = "gal-grid";
  root.appendChild(grid);

  for (const spec of BLOCKS) {
    const section = document.createElement("section");
    section.className = "blk";
    section.id = spec.id;
    section.dataset.group = spec.group;
    section.dataset.state = spec.state;

    const head = document.createElement("div");
    head.className = "blk-head";
    const tag = document.createElement("span");
    tag.className = "blk-group";
    tag.textContent = spec.group;
    const title = document.createElement("h2");
    title.textContent = spec.title;
    head.append(tag, title);

    const note = document.createElement("p");
    note.className = "note";
    note.textContent = spec.note;

    const stage = document.createElement("div");
    stage.className = "stage";

    section.append(head, note, stage);
    grid.appendChild(section);

    createApp(defineComponent({ setup: spec.setup })).mount(stage);
  }

  // 采集脚本用它确认「页面已挂载完」再开始截图
  (window as unknown as { __qioGallery?: { blocks: string[] } }).__qioGallery = {
    blocks: BLOCKS.map((b) => b.id),
  };
  document.documentElement.dataset.galleryReady = "1";
}

boot();
