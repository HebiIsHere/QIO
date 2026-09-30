"""工具 worker：独立工具子进程的入口与执行协议。

两种启动方式，**协议完全相同**：

* 正式（冻结）环境：`qio-backend.exe --tool-worker`
* 开发与测试环境：`python <本文件路径>`

协议：

* 标准输入：一个 JSON 对象 `{"code": "<工具代码>", "arguments": {...}}`
* 标准输出：**恰好一行** JSON 结果
  `{"ok": bool, "value": ..., "stdout": "...", "stderr": "...",
    "error": str|null, "error_type": str|null}`
* 工具自己的 `print` 与异常栈不直接写结果通道：它们被捕获进结果的
  `stdout` / `stderr` 字段，保证结果与日志分离。

约束（见 docs/status.md 与 plan 文档）：

* 本模块**只 import 标准库**，不 import 后端的任何模块 —— 它必须在后端服务、
  数据库、凭据加载之前就能独立运行；冻结入口靠这一点在启动服务前分流。
* 本模块不读取凭据、不连接数据库、不做任何权限判断：权限检查与审批在调用它的
  那一层完成（见 tools/policy.py、tools/registry.py）。
* 独立子进程**不是安全沙箱**：它只是把生成代码与后端主进程隔开，限制崩溃、超时
  与资源占用的影响范围；能力边界由执行策略与审批决定，不由此声明兜底。
"""

from __future__ import annotations

import contextlib
import json
import sys
import traceback

RESULT_LIMIT_CHARS = 200_000
_TRUNCATION_MARK = "\n…[输出已截断]"


class _CappedBuffer:
    """只保留前 `limit` 个字符的捕获缓冲（超出部分丢弃并标记截断）。

    `contextlib.redirect_stdout` 需要一个 `.write()` / `.flush()`；工具的一次
    `print('x' * 10**9)` 不该先把 worker 的内存吃光再在最后截断 —— 限制必须
    发生在写入的那一刻。
    """

    def __init__(self, limit: int = RESULT_LIMIT_CHARS) -> None:
        self._limit = limit
        self._parts: list[str] = []
        self._size = 0
        self.truncated = False

    def write(self, text: object) -> int:
        if not isinstance(text, str):
            text = str(text)
        room = self._limit - self._size
        if room > 0:
            chunk = text[:room]
            self._parts.append(chunk)
            self._size += len(chunk)
        if len(text) > room:
            self.truncated = True
        return len(text)

    def writelines(self, lines) -> None:  # noqa: ANN001 - 与 StringIO 同形
        for line in lines:
            self.write(line)

    def flush(self) -> None:
        return None

    def getvalue(self) -> str:
        text = "".join(self._parts)
        if not self.truncated:
            return text
        # 标记必须留在上限之内，否则外面再截一次就会把「已截断」这半句剪掉，
        # 读的人只看到一长串正常输出、以为这就是全部。
        keep = max(0, self._limit - len(_TRUNCATION_MARK))
        return text[:keep] + _TRUNCATION_MARK


def _dump(obj: object) -> str:
    """结果通道只写一行 JSON。"""
    return json.dumps(obj, ensure_ascii=False, default=None)


def execute(payload: dict) -> dict:
    """执行一次工具请求，返回结果字典（不写标准输出）。"""
    code = str(payload.get("code") or "")
    arguments = payload.get("arguments")
    if not isinstance(arguments, dict):
        arguments = {}

    captured_out = _CappedBuffer()
    captured_err = _CappedBuffer()
    namespace: dict = {}
    try:
        compiled = compile(code, "<tool>", "exec")
        with contextlib.redirect_stdout(captured_out), contextlib.redirect_stderr(
            captured_err
        ):
            exec(compiled, namespace)  # noqa: S102 - 执行生成代码是本模块的唯一职责
            run = namespace.get("run")
            if not callable(run):
                raise RuntimeError("工具代码必须定义 run(**kwargs) 函数")
            value = run(**arguments)
    except BaseException as exc:  # noqa: BLE001 - 任何异常都要变成结构化结果
        if isinstance(exc, (KeyboardInterrupt, SystemExit)) and not isinstance(
            exc, Exception
        ):
            # 进程被外部中断：交回上层，不伪装成工具错误。
            raise
        return _error_result(exc, captured_out, captured_err)

    if not isinstance(value, dict):
        return _error_result(
            RuntimeError(
                f"工具必须返回一个 JSON 对象（dict），实际是 {type(value).__name__}"
            ),
            captured_out,
            captured_err,
        )
    try:
        _check_serializable(value)
    except (TypeError, ValueError) as exc:
        return _error_result(exc, captured_out, captured_err)
    return {
        "ok": True,
        "value": value,
        "stdout": _clip(captured_out.getvalue()),
        "stderr": _clip(captured_err.getvalue()),
        "error": None,
        "error_type": None,
    }


def _error_result(exc: BaseException, out: _CappedBuffer, err: _CappedBuffer) -> dict:
    detail = str(exc).strip() or "（异常没有给出说明）"
    return {
        "ok": False,
        "value": None,
        "stdout": _clip(out.getvalue()),
        # 完整异常栈留给上层做脱敏与限长，不在 worker 里丢弃
        "stderr": _clip(err.getvalue() + "".join(traceback.format_exception(exc))),
        "error": f"{type(exc).__name__}: {detail}",
        "error_type": type(exc).__name__,
    }


def _check_serializable(value: object) -> None:
    json.dumps(value, ensure_ascii=False, allow_nan=False)


def _clip(text: str) -> str:
    return text if len(text) <= RESULT_LIMIT_CHARS else text[:RESULT_LIMIT_CHARS]


def main(argv: list[str] | None = None) -> int:
    """读 stdin 请求 → 执行 → 写 stdout 结果。返回退出码。"""
    del argv  # 只接受协议数据；命令行参数（如 --tool-worker）不参与解析
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"工具请求不是合法 JSON：{exc}", file=sys.stderr)
        return 2
    if not isinstance(payload, dict):
        print("工具请求必须是一个 JSON 对象", file=sys.stderr)
        return 2
    result = execute(payload)
    sys.stdout.write(_dump(result) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
