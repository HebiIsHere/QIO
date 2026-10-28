/**
 * 页面内探针源码（安装到 window.__stg）。
 *
 * 单独放一个 .page.js 源文件、运行时读文本再 eval：探针是浏览器代码（依赖 window），
 * 直接 import 会把它当 Node 模块解析；写成转义字符串又没法读、没法改。
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
export const PAGE_HELPERS = readFileSync(join(here, "page-helpers.page.js"), "utf8");
export const PAGE_HELPERS_PATH = join(here, "page-helpers.page.js");
