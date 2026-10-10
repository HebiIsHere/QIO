/** 验收装置共用配置（端口 / 地址 / 独立临时数据目录）。所有采集脚本统一从这里取。 */
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
export const ROOT = resolve(here, "..", "..");
export const BACKEND_PORT = Number(process.env.QIO_SR_BACKEND_PORT || 8933);
export const APP_PORT = Number(process.env.QIO_SR_APP_PORT || 5433);
export const BACKEND = "http://127.0.0.1:" + BACKEND_PORT;
export const APP = "http://127.0.0.1:" + APP_PORT;
export const DATA_DIR = process.env.QIO_SR_DATA_DIR || join(process.env.TEMP || ".", "qio-sr-data");
export const BOARD = "board_default";
