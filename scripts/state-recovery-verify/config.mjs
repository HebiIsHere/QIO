/** 验收装置共用配置（端口 / 地址 / 独立临时数据目录）。所有采集脚本统一从这里取。 */
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
/**
 * 被验收的工作树根：默认是脚本所在的仓库根。
 * 复核集成分支时可用 QIO_SR_ROOT 指向那个工作树（脚本不必复制过去，也不会写对方的工作树）。
 */
export const ROOT = process.env.QIO_SR_ROOT ? resolve(process.env.QIO_SR_ROOT) : resolve(here, "..", "..");
export const BACKEND_PORT = Number(process.env.QIO_SR_BACKEND_PORT || 8933);
export const APP_PORT = Number(process.env.QIO_SR_APP_PORT || 5433);
export const BACKEND = "http://127.0.0.1:" + BACKEND_PORT;
export const APP = "http://127.0.0.1:" + APP_PORT;
export const DATA_DIR = process.env.QIO_SR_DATA_DIR || join(process.env.TEMP || ".", "qio-sr-data");
export const BOARD = "board_default";
