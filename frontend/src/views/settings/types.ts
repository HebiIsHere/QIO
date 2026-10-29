/**
 * 设置分类的唯一来源（§3 五类信息架构）。
 *
 * 导航、搜索索引与验收脚本都读这一份：分类 id 换个名字就会让
 * `data-settings-key` 前缀、搜索跳转与草稿归属同时错位，所以不许各写一份。
 */
export type SettingsCategoryId = "regular" | "models" | "memory" | "tools" | "about";

export interface SettingsCategory {
  id: SettingsCategoryId;
  label: string;
  /** 搜索里的分组说明（导航里不显示，避免标题栏化） */
  description: string;
}

export const SETTINGS_CATEGORIES: SettingsCategory[] = [
  { id: "regular", label: "常规", description: "外观、输出、窗口行为与引导入口" },
  { id: "models", label: "模型与服务", description: "Key 接入、模型与用途分配、联网搜索" },
  { id: "memory", label: "记忆与整理", description: "记忆分段、后台整理与对话深度" },
  { id: "tools", label: "工具与权限", description: "权限模式、工作区、工具记录与生效参数" },
  { id: "about", label: "关于与更新", description: "版本、更新、许可证与第三方声明" },
];

export const SETTINGS_CATEGORY_IDS: SettingsCategoryId[] = SETTINGS_CATEGORIES.map((c) => c.id);

export function settingsCategoryLabel(id: SettingsCategoryId): string {
  return SETTINGS_CATEGORIES.find((c) => c.id === id)?.label ?? id;
}
