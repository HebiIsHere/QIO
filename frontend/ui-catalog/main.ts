/**
 * QIO 原子控件画廊：把四个 ui/ 组件（QInput / QSelect / QNumber / QConfirm）
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
import QConfirm from "../src/components/ui/QConfirm.vue";
import QCombo from "../src/components/ui/QCombo.vue";
import QIcon from "../src/components/ui/QIcon.vue";
import QIconButton from "../src/components/ui/QIconButton.vue";
import QModal from "../src/components/ui/QModal.vue";
import { ICON_NAMES } from "../src/components/ui/icons";
import { ensureQioTipLayer } from "../src/components/ui/mountQioTipLayer";
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

const VENDORS = [
  { value: "deepseek", label: "DeepSeek" },
  { value: "openai", label: "OpenAI" },
  { value: "anthropic", label: "Anthropic", hint: "需要海外网络" },
  { value: "qwen", label: "通义千问" },
];

// ---------------------------------------------------------------- QIconButton / QIcon
const newBlocks: BlockSpec[] = [
  {
    id: "atoms-qiconbutton-states",
    group: "QIconButton",
    title: "图标按钮：普通 / 安静 / 危险 / 禁用",
    note: "统一 32×32、14px 圆角；aria-label 必填、无原生 title；禁用仍是语义化 button。",
    state: "states",
    setup: () => () =>
      h("div", { class: "row-demo" }, [
        h(QIconButton, { icon: "close", label: "关闭" }),
        h(QIconButton, { icon: "copy", label: "复制", tone: "quiet" }),
        h(QIconButton, { icon: "trash", label: "删除", tone: "danger" }),
        h(QIconButton, { icon: "refresh", label: "刷新（禁用）", disabled: true }),
        h(QIconButton, { icon: "collapse", label: "收起", size: "sm", expanded: true }),
      ]),
  },
  {
    id: "atoms-qiconbutton-tip",
    group: "QIconButton",
    title: "用途说明：悬停 500ms",
    note: "悬停 500ms 后出现统一浮层（角色 tooltip）；键盘聚焦立即出现，Esc/滚动/换页消失。截图脚本会把指针停在这个按钮上。",
    state: "tip",
    setup: () => () =>
      h("div", { class: "row-demo" }, [
        h(QIconButton, {
          icon: "download",
          label: "下载",
          description: "把这份材料下载到本机（不改动源文件）",
          "data-role": "tip-target",
        }),
        h(QIconButton, {
          icon: "info",
          label: "说明",
          description: "这条说明很长：它会换行而不是把浮层撑出屏幕，也不会挡住按钮本身；浮层最大宽度 280px，始终留在视口内。",
          "data-role": "tip-target-long",
        }),
      ]),
  },
  {
    id: "atoms-qiconbutton-busy",
    group: "QIconButton",
    title: "处理中（busy）",
    note: "保留尺寸与位置，挡住重复点击（原生 disabled + aria-busy），视觉只表示“正在进行”。",
    state: "busy",
    setup: () => () => h("div", { class: "row-demo" }, [h(QIconButton, { icon: "refresh", label: "正在刷新", busy: true })]),
  },
  {
    id: "atoms-qiconbutton-focus",
    group: "QIconButton",
    title: "键盘焦点 + 立即说明",
    note: "键盘 Tab 聚焦时焦点环可见（不用 outline:none 抹掉），用途说明立即出现（不等 500ms）。截图脚本会真实按 Tab 造出 focus-visible。",
    state: "focus",
    setup: () => () =>
      h("div", { class: "row-demo" }, [
        h("button", { class: "qio-btn", type: "button", "data-role": "focus-anchor" }, "先聚焦这里"),
        h(QIconButton, { icon: "settings", label: "设置", description: "打开设置页", "data-role": "icon-focus" }),
      ]),
  },

  {
    id: "atoms-qicon-all",
    group: "QIcon",
    title: "图标全表（24 viewBox / 1.6 描边）",
    note: "全站图标唯一来源 icons.ts；交互图标不允许用 emoji / Unicode 字符 / 图片代替。",
    state: "all",
    setup: () => () =>
      h(
        "div",
        { class: "icon-grid" },
        ICON_NAMES.map((name) =>
          h("div", { class: "icon-cell", "data-icon-name": name }, [
            h(QIcon, { name, size: 20 }),
            h("span", { class: "icon-label" }, name),
          ]),
        ),
      ),
  },

  // ---------------------------------------------------------------- QModal
  {
    id: "atoms-qmodal-normal",
    group: "QModal",
    title: "中央弹窗 · 普通",
    note: "三槽标题栏：左槽留空、标题中轴、右槽关闭图标按钮；正文与按钮组居中，动作按钮等宽同高。",
    state: "normal",
    setup: () => {
      const open = ref(false);
      return () =>
        h("div", { class: "confirm-demo" }, [
          h("button", { class: "qio-btn", type: "button", "data-role": "modal-trigger", onClick: () => (open.value = true) }, "打开中央弹窗"),
          h(
            QModal,
            {
              open: open.value,
              title: "保存这项设置？",
              kicker: "保存后立即生效",
              modalId: "catalog-modal-normal",
              width: "440px",
              onClose: () => (open.value = false),
            },
            {
              default: () =>
                h("p", null, "保存后当前页面会按新设置重新加载；已经发出的对话与草稿不受影响。"),
              footer: () => [
                h("button", { class: "qio-btn", type: "button", "data-role": "modal-cancel", onClick: () => (open.value = false) }, "取消"),
                h("button", { class: "qio-btn primary", type: "button", "data-role": "modal-confirm", onClick: () => (open.value = false) }, "保存"),
              ],
            },
          ),
        ]);
    },
  },
  {
    id: "atoms-qmodal-danger-long",
    group: "QModal",
    title: "中央弹窗 · 危险 + 长标题",
    note: "tone=danger 只让标题变色，不把整个弹窗染红；长标题自然换行且仍居中，不会被关闭按钮挤偏。",
    state: "danger-long",
    setup: () => {
      const open = ref(false);
      return () =>
        h("div", { class: "confirm-demo" }, [
          h("button", { class: "qio-btn", type: "button", "data-role": "modal-trigger", onClick: () => (open.value = true) }, "打开危险弹窗"),
          h(
            QModal,
            {
              open: open.value,
              title: "删除这项凭据并撤销它在所有环境里的授权？",
              kicker: "此操作不可恢复",
              modalId: "catalog-modal-danger",
              tone: "danger",
              width: "420px",
              onClose: () => (open.value = false),
            },
            {
              default: () =>
                h("p", null, "密钥会从系统凭据库删除；依赖它的能力立即不可用。本地删除与厂商吊销是两件事，这里只做本地删除。"),
              footer: () => [
                h("button", { class: "qio-btn", type: "button", "data-role": "modal-cancel", onClick: () => (open.value = false) }, "取消"),
                h("button", { class: "qio-btn danger-solid", type: "button", "data-role": "modal-confirm", onClick: () => (open.value = false) }, "删除"),
              ],
            },
          ),
        ]);
    },
  },
  {
    id: "atoms-qmodal-nodismiss",
    group: "QModal",
    title: "中央弹窗 · 不可收起",
    note: "dismissible=false：没有关闭按钮，Esc/外部点击都不关闭（影响确认、审批进行中等场景）。",
    state: "no-dismiss",
    setup: () => {
      const open = ref(false);
      return () =>
        h("div", { class: "confirm-demo" }, [
          h("button", { class: "qio-btn", type: "button", "data-role": "modal-trigger", onClick: () => (open.value = true) }, "打开不可取消弹窗"),
          h(
            QModal,
            {
              open: open.value,
              title: "这次执行会修改 3 个文件",
              kicker: "需要你明确确认",
              modalId: "catalog-modal-nodismiss",
              dismissible: false,
              onClose: () => (open.value = false),
            },
            {
              default: () => h("p", null, "确认后才会开始执行；外部点击与 Esc 不会撤销这次确认。"),
              footer: () => [
                h("button", { class: "qio-btn", type: "button", "data-role": "modal-cancel", onClick: () => (open.value = false) }, "返回"),
                h("button", { class: "qio-btn primary", type: "button", "data-role": "modal-confirm", onClick: () => (open.value = false) }, "确认执行"),
              ],
            },
          ),
        ]);
    },
  },

  {
    id: "atoms-qmodal-three-actions",
    group: "QModal",
    title: "中央弹窗 · 三个选项",
    note: "关闭询问这类三选一：按钮组整体居中、同高、等宽（三个都一样宽，不会一个宽两个窄）；窄窗纵排。",
    state: "three-actions",
    setup: () => {
      const open = ref(false);
      return () =>
        h("div", { class: "confirm-demo" }, [
          h("button", { class: "qio-btn", type: "button", "data-role": "modal-trigger", onClick: () => (open.value = true) }, "打开三选项弹窗"),
          h(
            QModal,
            {
              open: open.value,
              title: "退出 QIO？",
              kicker: "退出会结束当前运行",
              modalId: "catalog-modal-three",
              width: "460px",
              onClose: () => (open.value = false),
            },
            {
              default: () => h("p", null, "正在执行的对话与任务会中止；草稿与已保存的记录不会丢失。"),
              footer: () => [
                h("button", { class: "qio-btn", type: "button", "data-role": "modal-cancel", onClick: () => (open.value = false) }, "取消"),
                h("button", { class: "qio-btn", type: "button", onClick: () => (open.value = false) }, "最小化到托盘"),
                h("button", { class: "qio-btn danger-solid", type: "button", onClick: () => (open.value = false) }, "退出 QIO"),
              ],
            },
          ),
        ]);
    },
  },

  {
    id: "atoms-qmodal-busy",
    group: "QModal",
    title: "中央弹窗 · 处理中（busy）",
    note: "提交中：关闭按钮禁用、Esc/外部点击都不关闭，防止关闭中的重复提交；正文与尺寸不跳动。",
    state: "busy",
    setup: () => {
      const open = ref(false);
      return () =>
        h("div", { class: "confirm-demo" }, [
          h("button", { class: "qio-btn", type: "button", "data-role": "modal-trigger", onClick: () => (open.value = true) }, "打开处理中弹窗"),
          h(
            QModal,
            {
              open: open.value,
              title: "正在保存设置…",
              kicker: "请不要关闭窗口",
              modalId: "catalog-modal-busy",
              busy: true,
              width: "420px",
              onClose: () => (open.value = false),
            },
            {
              default: () => h("p", null, "这一次保存会写入本机配置；完成后弹窗会自己关闭。"),
              footer: () => [
                h("button", { class: "qio-btn", type: "button", "data-role": "modal-cancel", onClick: () => (open.value = false) }, "稍后处理"),
                h("button", { class: "qio-btn primary", type: "button", disabled: true }, "保存中…"),
              ],
            },
          ),
        ]);
    },
  },

  // ---------------------------------------------------------------- QConfirm 补充
  {
    id: "atoms-qconfirm-layer-long",
    group: "QConfirm",
    title: "layer 档 · 长标题与长文本",
    note: "长标题/长 URL 换行不撑破、标题仍居中；动作按钮等宽同高；320 宽视口下按钮组纵排（截图脚本复核）。",
    state: "layer-long",
    setup: () => {
      const open = ref(false);
      return () =>
        h("div", { class: "confirm-demo" }, [
          h("button", { class: "qio-btn", type: "button", "data-role": "trigger", onClick: () => (open.value = true) }, "打开长文本确认层"),
          h(QConfirm, {
            open: open.value,
            variant: "layer",
            title: "覆盖保存这个已经存在的配置文件？",
            detail:
              "同名文件会被这一次的内容替换；旧内容不会自动备份。若这份配置来自 https://example.com/some/very/long/path/that/should/wrap/instead/of/overflowing/config.json ，请先确认来源。",
            confirmText: "覆盖保存",
            cancelText: "取消",
            onConfirm: () => (open.value = false),
            onCancel: () => (open.value = false),
          }),
        ]);
    },
  },

  // ---------------------------------------------------------------- 状态/表单补充
  {
    id: "atoms-qinput-busy",
    group: "QInput",
    title: "处理中（busy）",
    note: "保留内容、挡住编辑；只表达“正在进行”，不伪造进度；真实进度由业务状态表达。",
    state: "busy",
    setup: () => () => h(QInput, { modelValue: "正在校验地址…", busy: true, ariaLabel: "服务地址" }),
  },
  {
    id: "atoms-qselect-placeholder",
    group: "QSelect",
    title: "空值占位",
    note: "未选择时显示 placeholder（弱化色）；没有可选项时显示 emptyText，而不是一行空白。",
    state: "placeholder",
    setup: () => () => h(QSelect, { options: SIZE_OPTIONS, modelValue: "", placeholder: "请选择档位" }),
  },
  {
    id: "atoms-qselect-busy",
    group: "QSelect",
    title: "处理中（busy）",
    note: "保留当前值，不展开菜单（避免在保存过程中改值）；cursor 提示进度。",
    state: "busy",
    setup: () => () => h(QSelect, { options: SIZE_OPTIONS, modelValue: "medium", busy: true }),
  },
  {
    id: "atoms-qcombo-default",
    group: "QCombo",
    title: "可搜索下拉（收起）",
    note: "触发框 38px、14px 圆角；未选时弱化 placeholder。",
    state: "closed",
    setup: () => {
      const v = ref("");
      return () => h(QCombo, { options: VENDORS, modelValue: v.value, placeholder: "选择厂商", "onUpdate:modelValue": (x: string) => (v.value = x) });
    },
  },
  {
    id: "atoms-qcombo-open",
    group: "QCombo",
    title: "可搜索下拉（打开）",
    note: "菜单 14px 圆角、选项行 14px；键盘 ↑↓/Home/End/Enter/Esc/Tab 完整（截图脚本点开）。",
    state: "open",
    setup: () => {
      const v = ref("");
      return () => h(QCombo, { options: VENDORS, modelValue: v.value, placeholder: "选择厂商", "onUpdate:modelValue": (x: string) => (v.value = x) });
    },
  },
  {
    id: "atoms-qcombo-empty",
    group: "QCombo",
    title: "无匹配结果",
    note: "搜索不到时在原位说明（空状态文案由 emptyText 提供），不是一条空白菜单。",
    state: "empty",
    setup: () => {
      const v = ref("");
      return () => h(QCombo, { options: VENDORS, modelValue: v.value, placeholder: "选择厂商", "onUpdate:modelValue": (x: string) => (v.value = x) });
    },
  },
  {
    id: "atoms-qcombo-disabled-busy",
    group: "QCombo",
    title: "禁用 / 处理中",
    note: "禁用整体降透明度、不展开；处理中保留当前值、不展开（避免保存过程中改值）。",
    state: "disabled-busy",
    setup: () => () =>
      h("div", { class: "row-demo" }, [
        h(QCombo, { options: VENDORS, modelValue: "deepseek", disabled: true }),
        h(QCombo, { options: VENDORS, modelValue: "openai", busy: true }),
      ]),
  },

  {
    id: "atoms-badges",
    group: "Badge / State",
    title: "计数徽标与状态徽章",
    note: "徽标/标签/状态统一 14px 圆角 token；状态色只来自语义令牌，不用硬编码色值。",
    state: "badges",
    setup: () => () =>
      h("div", { class: "row-demo" }, [
        h("span", { class: "qio-badge" }, "3"),
        h("span", { class: "qio-badge link" }, "12"),
        h("span", { class: "qio-tag" }, "已归档"),
        h("span", { class: "qio-state ok" }, "已完成"),
        h("span", { class: "qio-state warn" }, "等待中"),
        h("span", { class: "qio-state err" }, "失败"),
        h("span", { class: "qio-state info" }, "处理中"),
        h("span", { class: "qio-state quiet" }, "未知"),
      ]),
  },
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
...newBlocks,
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
  .row-demo { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
  .icon-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(118px, 1fr)); gap: 8px; width: 100%; }
  .icon-cell { display: flex; align-items: center; gap: 8px; min-width: 0; padding: 6px 8px;
               border: 1px solid var(--border-subtle); border-radius: var(--qio-ui-radius); }
  .icon-label { font-family: var(--mono); font-size: 10.5px; color: var(--text-muted); overflow-wrap: anywhere; }
  .blk { border-radius: var(--qio-ui-radius); }
`;

function boot() {
  const style = document.createElement("style");
  style.textContent = STYLE;
  document.head.appendChild(style);

  // 用途说明浮层在画廊里也必须只挂一次（产品里由 App.vue 挂载）
  ensureQioTipLayer();

  const root = document.getElementById("gallery");
  if (!root) throw new Error("缺少 #gallery 容器");

  const head = document.createElement("header");
  head.className = "gal-head";
  head.innerHTML =
    "<h1>QIO 原子控件画廊</h1><p>ui/ 组件全状态并排展示（含本轮新增图标按钮、用途说明、中央弹窗、确认层、表单状态）—— 只用于 UI 穷举截图，不是产品页面。</p>";
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