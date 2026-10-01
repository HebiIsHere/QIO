"""工具 worker：独立工具子进程的入口与执行协议。

三种启动方式，**协议完全相同**（同一份实现；容器路径由 tools/sandbox.py 把本文件
源码 exec 起来）：

* 正式（冻结）环境：`qio-backend.exe --tool-worker`
* 开发与测试环境：`python <本文件路径>`
* 容器隔离执行：`docker run --rm -i … python -c <引导脚本>`

协议：

* 标准输入：一个 JSON 对象 `{"code": "<工具代码>", "arguments": {...}}`
* 标准输出：**恰好一行** JSON 结果
  `{"ok": bool, "value": ..., "stdout": "...", "stderr": "...",
    "error": str|null, "error_type": str|null}`
* 工具自己的 `print` 与异常栈不直接写结果通道：它们被捕获进结果的
  `stdout` / `stderr` 字段，保证结果与日志分离。

**编码契约（字节层，钉死为 UTF-8，不跟随系统 locale）：**

以前这里直接用 `sys.stdout.write(json.dumps(..., ensure_ascii=False))` 写结果，于是
「结果通道能不能用」取决于进程在哪个 locale 下启动：在 cp1252 的机器（GitHub 的
`windows-latest`、英文 Windows 用户机）上，只要结果里出现中文（工具有个中文报错、
参数里有中文），`sys.stdout.write` 就抛 `UnicodeEncodeError` —— 进程死在结果通道
上，父进程只看到「标准输出为空」；而 `sys.stderr` 的 backslashreplace 会把中文写成
字面 `\u5de5\u5177…`，错误通道同样读不成话。冻结后端（PyInstaller onefile）同理。

现在：

* 读请求走 `sys.stdin.buffer`、写结果走 `sys.stdout.buffer`，两边都是**字节层**，
  编码由协议固定为 UTF-8；文本层（warnings、第三方库直接 print）也 reconfigure 成
  UTF-8 兜底，但结果通道不依赖它；
* 结果行仍然是**一行 JSON**；只有遇到无法编成 UTF-8 的罕见字符（孤立代理）才降级成
  `ensure_ascii=True` 的纯 ASCII 行 —— 那仍是同一个 JSON 值（读回来一模一样）。

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
_STACK_TRUNCATION_MARK = "\n…[异常栈已截断]"

# 协议通道的编码：钉死为 UTF-8，与系统 locale 无关（见模块 docstring 的编码契约）。
PROTOCOL_ENCODING = "utf-8"

# 结果行（含工具返回值）的字节上限：父进程愿意为一个 worker 缓冲的上限是 2 MiB
# （tools/sandbox.py），这里留出余量。超限时**不**把协议撑爆，而是换成一条合法的
# 协议失败 —— 否则父进程只能把子进程连同还没写完的结果一起杀掉，读的人看到的是
# 「输出超过上限」，分不清是工具在刷屏还是返回值太大。
MAX_RESULT_LINE_BYTES = 1_500_000


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


def _project_root() -> str:
    """本次调用的项目根（父进程已经把项目文件写在当前工作目录里）。

    放进 `sys.path` 的目的只有一个：入口代码能 import 自己的工作区文件
    （`from pkg.util import double`）。项目文件由父进程写进一次性临时目录，
    随调用结束一起消失。
    """
    import os

    root = os.getcwd()
    if root not in sys.path:
        sys.path.insert(0, root)
    return root


def _load_entry(entry: str):
    """按 `包.模块:函数` 加载入口（多文件项目的包内相对 import 靠它成立）。"""
    module_name, _, func_name = entry.partition(":")
    if not module_name or not func_name:
        raise RuntimeError(f"入口格式必须是 `包.模块:函数`，实际是 {entry!r}")
    import importlib

    _project_root()
    module = importlib.import_module(module_name)
    run = getattr(module, func_name, None)
    if not callable(run):
        raise RuntimeError(f"入口 {entry} 不是可调用对象（没有这个函数？）")
    return run


def execute(payload: dict) -> dict:
    """执行一次工具请求，返回结果字典（不写标准输出）。"""
    code = str(payload.get("code") or "")
    entry = str(payload.get("entry") or "")
    arguments = payload.get("arguments")
    if not isinstance(arguments, dict):
        arguments = {}

    captured_out = _CappedBuffer()
    captured_err = _CappedBuffer()
    namespace: dict = {}
    try:
        with contextlib.redirect_stdout(captured_out), contextlib.redirect_stderr(
            captured_err
        ):
            if entry:
                run = _load_entry(entry)
            else:
                _project_root()
                compiled = compile(code, "<tool>", "exec")
                # noqa: S102 - 执行生成代码是本模块的唯一职责
                exec(compiled, namespace)
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
        # 完整异常栈留给上层做脱敏与限长，不在 worker 里丢弃（真被截断就标出来）
        "stderr": _clip_stack(err.getvalue(), "".join(traceback.format_exception(exc))),
        "error": f"{type(exc).__name__}: {detail}",
        "error_type": type(exc).__name__,
    }


def _check_serializable(value: object) -> None:
    json.dumps(value, ensure_ascii=False, allow_nan=False)


def _clip(text: str) -> str:
    return text if len(text) <= RESULT_LIMIT_CHARS else text[:RESULT_LIMIT_CHARS]


def _clip_stack(output: str, stack: str) -> str:
    """工具输出 + 异常栈合成一个限长字段；截断处留下可见标记。

    以前异常栈被静默剪掉尾巴：读的人只看到半截 traceback，却以为那就是全部。
    """
    combined = output + stack
    if len(combined) <= RESULT_LIMIT_CHARS:
        return combined
    keep = max(0, RESULT_LIMIT_CHARS - len(_STACK_TRUNCATION_MARK))
    return combined[:keep] + _STACK_TRUNCATION_MARK


def _pin_text_streams() -> None:
    """文本层兜底：把 stdin/stdout/stderr 钉成 UTF-8，不跟随系统 locale。

    结果通道本身走字节层（见 `_emit_result`），这里覆盖的是「不是我们写的」那部分
    输出（warnings、依赖库直接 print 到真实 stderr）。拿不到 reconfigure 就在字节层
    兜住，不影响协议。
    """
    for stream, errors in (
        (sys.stdin, "replace"),
        (sys.stdout, "strict"),
        (sys.stderr, "backslashreplace"),
    ):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding=PROTOCOL_ENCODING, errors=errors)
        except (AttributeError, OSError, ValueError):
            # 某些嵌入场景（自定义流对象、已读过的包装器）不允许 reconfigure：
            # 字节层仍然是 UTF-8，所以这不是致命问题。
            continue


def _read_request_bytes() -> bytes:
    """按协议读请求：优先字节层，避免用 locale 编码去解释 UTF-8 请求体。"""
    buffer = getattr(sys.stdin, "buffer", None)
    if buffer is not None:
        return buffer.read()
    text = sys.stdin.read()
    return (text or "").encode(PROTOCOL_ENCODING, errors="replace")


def _write_bytes(stream, data: bytes) -> None:
    """写一行到协议通道；字节层可用时完全绕开文本层的 locale 编码。"""
    if stream is None:  # 无控制台的冻结构建：没有通道可写
        return
    buffer = getattr(stream, "buffer", None)
    if buffer is not None:
        buffer.write(data + b"\n")
        buffer.flush()
        return
    stream.write(data.decode(PROTOCOL_ENCODING, errors="replace") + "\n")
    stream.flush()


def _write_stderr(message: str) -> None:
    _write_bytes(sys.stderr, message.encode(PROTOCOL_ENCODING, errors="backslashreplace"))


def _result_bytes(result: dict) -> bytes:
    """结果行的 UTF-8 字节；超过上限时换成一条**合法**的协议失败。"""
    try:
        data = _dump(result).encode(PROTOCOL_ENCODING)
    except UnicodeEncodeError:
        # 孤立代理之类无法编成 UTF-8 的字符：改用 JSON 自己的转义写出去，
        # 读回来仍然是同一个值（ensure_ascii 只改表示，不改语义）。
        data = json.dumps(result, ensure_ascii=True, default=None).encode(PROTOCOL_ENCODING)
    if len(data) <= MAX_RESULT_LINE_BYTES:
        return data
    overflow = {
        "ok": False,
        "value": None,
        "stdout": "",
        "stderr": "",
        "error": (
            f"工具结果超过上限（{len(data)} 字节，最多 {MAX_RESULT_LINE_BYTES} 字节）："
            "结果通道装不下这么大的返回值。"
        ),
        "error_type": "ResultTooLarge",
    }
    return _dump(overflow).encode(PROTOCOL_ENCODING)


def _emit_result(result: dict) -> None:
    """结果通道：恰好一行 JSON，UTF-8 字节。"""
    _write_bytes(sys.stdout, _result_bytes(result))


def main(argv: list[str] | None = None) -> int:
    """读 stdin 请求 → 执行 → 写 stdout 结果。返回退出码。"""
    del argv  # 只接受协议数据；命令行参数（如 --tool-worker）不参与解析
    _pin_text_streams()
    raw = _read_request_bytes()
    try:
        text = raw.decode(PROTOCOL_ENCODING, errors="replace") if raw.strip() else ""
        payload = json.loads(text) if text else {}
    except (json.JSONDecodeError, ValueError) as exc:
        _write_stderr(f"工具请求不是合法 JSON：{exc}")
        return 2
    if not isinstance(payload, dict):
        _write_stderr("工具请求必须是一个 JSON 对象")
        return 2
    result = execute(payload)
    _emit_result(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
