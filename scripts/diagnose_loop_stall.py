"""CI 响应性诊断：慢推理/慢检索场景下事件循环停顿的**测量证据**脚本（R9-F W4）。

背景（CI 事实）：
* run 39771507126：test_health_probe_stays_responsive_while_slow_prediction_runs 349ms > 300ms（重跑仍红）
* run 39784106563：test_a_slow_retrieval_does_not_stall_the_turn_loop 981ms > 300ms

本脚本**复用 backend/tests/test_interactive_during_heavy_work.py 的夹具与替身**
（_SlowEmbedding / _SlowRecall / _FakeAdapter 与 ctx 构造方式），在指定仓库上重复运行
同一场景 N 次，记录：
* 每次运行的最大心跳 gap、完整 gap 时间线；
* gap > 阈值（默认 50ms）时由后台采样线程用 sys._current_frames() 抓**主线程栈**
  （以及其它线程的最深帧），用于区分：
    - sync_db / sync_compute / sync_sleep / sync_python：事件循环被真实同步代码占住；
    - worker_thread_execution：主线程在 selector 等待、慢活在执行器线程里（GIL 竞争）；
    - loop_idle_wait：主线程空等（runner/OS 调度延迟）；
    - first_init：首次初始化（导入/迁移/模型加载）。
* 空载与 CPU/磁盘压力对照。

用法（在**目标仓库**的 backend 目录里跑，使用目标仓库的依赖）：
    cd <repo>/backend
    uv run --frozen python <this-script> --repo <repo> --label <name> --runs 30 \
        --scenarios turn-recall,turn-predict --pressure none

输出：<out>/<label>__<scenario>__<pressure>.json + <out>/summary.csv + 控制台摘要。
不改产品代码、不改测试文件。
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import importlib.util
import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# ---------------------------------------------------------------------------
# 目标仓库解析（必须在 import agent.* 之前完成）
# ---------------------------------------------------------------------------


def _resolve_repo(raw: str | None) -> Path:
    if raw:
        repo = Path(raw).resolve()
    else:
        repo = Path(__file__).resolve().parents[1]
    assert (repo / "backend" / "src" / "agent").is_dir(), f"不是 QIO 仓库：{repo}"
    return repo


def _prepare_imports(repo: Path) -> None:
    src = str(repo / "backend" / "src")
    if src not in sys.path:
        sys.path.insert(0, src)


def _load_heavy_work_module(repo: Path):
    """复用目标仓库测试文件的夹具/替身（不复制一份，避免漂移）。"""
    path = repo / "backend" / "tests" / "test_interactive_during_heavy_work.py"
    assert path.is_file(), f"缺少目标测试文件：{path}"
    spec = importlib.util.spec_from_file_location("heavy_work_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _git_sha(repo: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=20,
        )
        return out.stdout.strip() or "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


# ---------------------------------------------------------------------------
# 采样与分类
# ---------------------------------------------------------------------------

#: 判定"主线程在事件循环里等 I/O"的帧特征（asyncio/selector 等待）。
_IDLE_FILE_HINTS = ("asyncio", "selectors", "windows_events", "proactor", "iocp")
_IDLE_FUNCS = {
    "select", "poll", "epoll", "kevent", "wait", "_run_once", "run_forever",
    "run_until_complete", "_poll", "select_epoll", "wait_for_handle",
}
#: 同步阻塞特征。
_DB_FUNCS = {"execute", "executemany", "commit", "fetchone", "fetchall", "fetchmany", "executescript"}


def _deepest(frames: list[traceback.FrameSummary]) -> traceback.FrameSummary | None:
    return frames[-1] if frames else None


def _classify_stack(main_frames: list[traceback.FrameSummary],
                    thread_deepest: dict[str, str]) -> str:
    """把一次采样归类（优先级从高到低）。"""
    deep = _deepest(main_frames)
    if deep is None:
        return "unknown"
    fn, file = deep.name, deep.filename.replace("\\", "/")
    low = file.lower()
    if "importlib" in low or "migrate.py" in low or "onnx" in low and fn in ("load", "InferenceSession"):
        return "first_init"
    idle = fn in _IDLE_FUNCS or any(h in low for h in _IDLE_FILE_HINTS)
    if idle:
        # 主线程在等：慢活是不是正在别的线程里跑？
        busy = [
            name for name, deepest in thread_deepest.items()
            if deepest in ("embed_texts", "search", "entity_card_search", "sleep", "copyfile")
        ]
        return "worker_thread_execution" if busy else "loop_idle_wait"
    if "sqlite3" in low or fn in _DB_FUNCS:
        return "sync_db"
    if "numpy" in low or "onnxruntime" in low:
        return "sync_compute"
    if fn == "sleep" and "/time.py" in low:
        return "sync_sleep"
    if "/agent/" in low:
        return "sync_python"
    return "sync_python" if low.endswith(".py") else "sync_native"


@dataclass
class Sample:
    t: float
    kind: str
    main_stack: list[str]
    threads: dict[str, str]


@dataclass
class Gap:
    start: float
    end: float
    ms: float


@dataclass
class RunResult:
    index: int
    scenario: str
    pressure: str
    gap_ms: float
    max_gap: Gap | None
    kinds: dict[str, int] = field(default_factory=dict)
    samples_in_gap: list[Sample] = field(default_factory=list)
    gap_records: list[dict] = field(default_factory=list)
    marks: list[tuple[float, str]] = field(default_factory=list)
    turn_ms: float = 0.0
    ok: bool = True
    error: str | None = None


class LoopProbe:
    """心跳测停顿 + 后台栈采样（gap 超阈值时给出可解释的证据）。"""

    def __init__(self, *, tick: float = 0.005, sample_every: float = 0.005,
                 gap_threshold_ms: float = 50.0) -> None:
        self.tick = tick
        self.sample_every = sample_every
        self.gap_threshold_ms = gap_threshold_ms
        self.marks: list[tuple[float, str]] = []
        self._gaps: list[Gap] = []
        self._samples: list[Sample] = []
        self._stop = threading.Event()
        self._main_ident = threading.main_thread().ident

    def mark(self, label: str) -> None:
        self.marks.append((time.perf_counter(), label))

    async def measure(self, work: Callable[[], Any]) -> tuple[Any, float, Gap | None, dict[str, int], list[Sample], list[tuple[float, str]], list[dict]]:
        stop = False
        last = time.perf_counter()
        gaps: list[Gap] = []
        self._gaps = gaps
        self._samples = []
        self._stop.clear()

        async def _ticker() -> None:
            nonlocal last
            while not stop:
                await asyncio.sleep(self.tick)
                now = time.perf_counter()
                if now - last >= self.tick * 1.5:  # 只记"比心跳周期长"的间隔
                    gaps.append(Gap(last, now, (now - last) * 1000))
                last = now

        def _sampler() -> None:
            while not self._stop.is_set():
                time.sleep(self.sample_every)
                try:
                    frames = sys._current_frames()
                except Exception:  # noqa: BLE001
                    continue
                main_frame = frames.get(self._main_ident)
                if main_frame is None:
                    continue
                main_stack = [f"{f.filename.replace(chr(92), '/')}:{f.lineno}:{f.name}"
                              for f in traceback.extract_stack(main_frame)]
                threads: dict[str, str] = {}
                for thread in threading.enumerate():
                    frame = frames.get(thread.ident) if thread.ident else None
                    if frame is None:
                        continue
                    stack = traceback.extract_stack(frame)
                    deep = _deepest(stack)
                    threads[thread.name] = deep.name if deep else "?"
                self._samples.append(
                    Sample(time.perf_counter(), _classify_stack(
                        traceback.extract_stack(main_frame), threads), main_stack, threads)
                )

        sampler = threading.Thread(target=_sampler, name="loop-probe-sampler", daemon=True)
        sampler.start()
        ticker = asyncio.create_task(_ticker())
        t0 = time.perf_counter()
        result: Any = None
        try:
            await asyncio.sleep(0)
            result = await work()
            for _ in range(50):
                seen = len(gaps)
                await asyncio.sleep(0)
                if len(gaps) > seen:
                    break
        finally:
            stop = True
            self._stop.set()
            ticker.cancel()
            try:
                await ticker
            except asyncio.CancelledError:
                pass
            sampler.join(timeout=2.0)
        turn_ms = (time.perf_counter() - t0) * 1000

        max_gap = max(gaps, key=lambda g: g.ms) if gaps else None
        kinds: Counter[str] = Counter()
        in_gap: list[Sample] = []
        gap_records: list[dict] = []
        for gap in gaps:
            if gap.ms < 20.0:
                continue
            lo, hi = gap.start - 0.003, gap.end + 0.003
            window = [s for s in self._samples if lo <= s.t <= hi]
            window_kinds = Counter(s.kind for s in window)
            mid = window[len(window) // 2] if window else None
            gap_records.append({
                "start": round(gap.start, 6), "end": round(gap.end, 6),
                "ms": round(gap.ms, 3),
                "kind": window_kinds.most_common(1)[0][0] if window_kinds else "unsampled",
                "kinds": dict(window_kinds),
                "top": mid.main_stack[-8:] if mid else [],
                "threads": mid.threads if mid else {},
            })
        if max_gap is not None and max_gap.ms >= self.gap_threshold_ms:
            lo, hi = max_gap.start - 0.003, max_gap.end + 0.003
            in_gap = [s for s in self._samples if lo <= s.t <= hi]
            kinds.update(s.kind for s in in_gap)
        return result, turn_ms, max_gap, dict(kinds), in_gap, list(self.marks), gap_records


# ---------------------------------------------------------------------------
# 压力注入
# ---------------------------------------------------------------------------


class CpuPressure:
    def __init__(self, workers: int) -> None:
        self.workers = max(1, workers)
        self.procs: list[subprocess.Popen] = []

    def __enter__(self):
        code = "import time\nt=time.time()+600\nwhile time.time()<t: pass"
        for _ in range(self.workers):
            self.procs.append(subprocess.Popen([sys.executable, "-c", code]))
        return self

    def __exit__(self, *exc):
        for proc in self.procs:
            proc.kill()
        for proc in self.procs:
            proc.wait(timeout=10)
        return False


class DiskPressure:
    def __init__(self, directory: Path, *, mb: int = 8) -> None:
        self.directory = directory
        self.mb = mb
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._burn, daemon=True)

    def _burn(self) -> None:
        blob = b"x" * (1024 * 1024)
        i = 0
        while not self._stop.is_set():
            path = self.directory / f"pressure-{i % 4}.bin"
            try:
                with open(path, "wb") as handle:
                    for _ in range(self.mb):
                        handle.write(blob)
                    handle.flush()
                    os.fsync(handle.fileno())
            except OSError:
                pass
            i += 1

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(timeout=10)
        return False


# ---------------------------------------------------------------------------
# 场景（与失败用例同一口径）
# ---------------------------------------------------------------------------

SCENARIOS = ("turn-recall", "turn-predict", "injection")


def _make_ctx(repo: Path, tmp: Path, heavy_mod):
    from agent.api.bus import EventBus
    from agent.config import Settings
    from agent.credentials.store import MemoryKeyring
    from agent.services.app import AppContext
    from agent.storage.db import connect
    from agent.storage.migrate import apply_migrations

    conn = connect(tmp / "app.db")
    apply_migrations(conn)
    ctx = AppContext(Settings(data_dir=tmp), conn, EventBus())
    ctx.credentials._kr = MemoryKeyring()
    return ctx


async def _run_one(repo: Path, scenario: str, index: int, *, tmp_root: Path,
                   threshold_ms: float, heavy_mod) -> RunResult:
    from unittest.mock import AsyncMock

    tmp = Path(tempfile.mkdtemp(prefix=f"diag-{scenario}-", dir=str(tmp_root)))
    ctx = _make_ctx(repo, tmp, heavy_mod)
    topic = ctx.topics.nodes.create_topic("测试话题").id
    ctx.build_adapter = AsyncMock(return_value=heavy_mod._FakeAdapter())

    probe = LoopProbe(gap_threshold_ms=threshold_ms)
    if scenario == "turn-recall":
        slow = heavy_mod._SlowRecall(delay=0.2)  # 与 RECALL_STIMULUS_SECONDS 一致
        ctx.selector.recall = slow
        original = slow.search

        def _marked(query, top_k, _orig=original, _probe=probe):
            _probe.mark("slow_recall_enter")
            out = _orig(query, top_k)
            _probe.mark("slow_recall_exit")
            return out

        slow.search = _marked  # type: ignore[assignment]

        async def _work():
            probe.mark("turn_start")
            out = await ctx.run_turn("回忆一下之前的偏好", topic_id=topic)
            probe.mark("turn_end")
            return out
    elif scenario == "turn-predict":
        slow = heavy_mod._SlowEmbedding(delay=0.6)  # 与 SLOW_STIMULUS_SECONDS 一致
        ctx.predictor.embedding = slow
        original_embed = slow.embed_texts

        def _marked_embed(texts, _orig=original_embed, _probe=probe):
            _probe.mark("slow_embed_enter")
            out = _orig(texts)
            _probe.mark("slow_embed_exit")
            return out

        slow.embed_texts = _marked_embed  # type: ignore[assignment]

        async def _work():
            probe.mark("turn_start")
            out = await ctx.run_turn("帮我看看这段代码", topic_id=topic)
            probe.mark("turn_end")
            return out
    else:  # injection：修复后的 offload 路径本身
        slow = heavy_mod._SlowRecall(delay=0.2)
        ctx.selector.recall = slow

        async def _work():
            probe.mark("injection_start")
            out = await ctx.heavy.run(
                ctx.build_injection, "回忆一下之前的偏好", topic_id=topic, short_term=[]
            )
            probe.mark("injection_end")
            return out

    try:
        result, turn_ms, max_gap, kinds, samples, marks, gap_records = await probe.measure(_work)
        ok = bool(result.get("ok")) if isinstance(result, dict) else True
        return RunResult(index, scenario, "", max_gap.ms if max_gap else 0.0, max_gap,
                         kinds, samples, gap_records, marks, turn_ms, ok=ok)
    except Exception as exc:  # noqa: BLE001
        return RunResult(index, scenario, "", 0.0, None, {}, [], [], [], 0.0,
                         ok=False, error=f"{type(exc).__name__}: {exc}")


def _serialize_run(run: RunResult) -> dict:
    return {
        "index": run.index,
        "scenario": run.scenario,
        "pressure": run.pressure,
        "gap_ms": round(run.gap_ms, 3),
        "turn_ms": round(run.turn_ms, 3),
        "ok": run.ok,
        "error": run.error,
        "max_gap": (
            {"start": run.max_gap.start, "end": run.max_gap.end, "ms": round(run.max_gap.ms, 3)}
            if run.max_gap else None
        ),
        "kinds_in_gap": run.kinds,
        "gap_records": run.gap_records,
        "samples_in_gap": [
            {"t": s.t, "kind": s.kind, "main_stack": s.main_stack[-12:], "threads": s.threads}
            for s in run.samples_in_gap[:40]
        ],
        "marks": [[round(t - run.marks[0][0], 4), label] for t, label in run.marks] if run.marks else [],
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


async def _run_scenario(repo: Path, scenario: str, runs: int, *, pressure: str,
                        threshold_ms: float, tmp_root: Path, heavy_mod) -> list[RunResult]:
    results: list[RunResult] = []
    for i in range(runs):
        run = await _run_one(repo, scenario, i, tmp_root=tmp_root,
                             threshold_ms=threshold_ms, heavy_mod=heavy_mod)
        run.pressure = pressure
        results.append(run)
        status = "ok" if run.ok else f"ERROR {run.error}"
        print(f"  [{scenario}/{pressure}] run {i + 1}/{runs}: gap={run.gap_ms:8.1f}ms "
              f"turn={run.turn_ms:8.1f}ms kinds={run.kinds} {status}", flush=True)
    return results


def _summary(results: list[RunResult]) -> dict:
    gaps = [r.gap_ms for r in results]
    kinds: Counter[str] = Counter()
    for run in results:
        kinds.update(run.kinds)
    over = [g for g in gaps if g >= 300.0]
    return {
        "runs": len(results),
        "gap_ms": {
            "min": round(min(gaps), 1) if gaps else None,
            "p50": round(statistics.median(gaps), 1) if gaps else None,
            "p90": round(sorted(gaps)[int(len(gaps) * 0.9)] if gaps else 0.0, 1),
            "max": round(max(gaps), 1) if gaps else None,
            "over_budget_300ms": len(over),
        },
        "kinds_in_gaps": dict(kinds.most_common()),
        "errors": sum(1 for r in results if not r.ok),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="QIO 事件循环停顿诊断（R9-F W4）")
    parser.add_argument("--repo", default=None, help="目标仓库根目录（默认脚本所在仓库）")
    parser.add_argument("--label", default=None, help="标签（默认仓库目录名 + SHA 前 8 位）")
    parser.add_argument("--runs", type=int, default=30)
    parser.add_argument("--scenarios", default="turn-recall,turn-predict,injection")
    parser.add_argument("--pressure", default="none", choices=("none", "cpu", "disk", "both"))
    parser.add_argument("--gap-threshold-ms", type=float, default=50.0)
    parser.add_argument("--out", default=None, help="证据输出目录")
    parser.add_argument("--warmup", type=int, default=1, help="正式测量前的预热次数（不计入结果）")
    args = parser.parse_args()

    repo = _resolve_repo(args.repo)
    _prepare_imports(repo)
    heavy_mod = _load_heavy_work_module(repo)
    sha = _git_sha(repo)
    label = args.label or f"{repo.name}-{sha[:8]}"
    out = Path(args.out).resolve() if args.out else (
        Path(__file__).resolve().parents[1] / "_r9f-evidence" / "w4-loop-stall"
    )
    out.mkdir(parents=True, exist_ok=True)

    scenarios = [s.strip() for s in args.scenarios.split(",") if s.strip()]
    assert all(s in SCENARIOS for s in scenarios), f"未知场景：{scenarios}"

    env = {
        "label": label,
        "repo": str(repo),
        "sha": sha,
        "python": sys.version,
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "runs_requested": args.runs,
        "gap_threshold_ms": args.gap_threshold_ms,
        "pressure": args.pressure,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "test_file_sha256": __import__("hashlib").sha256(
            (repo / "backend" / "tests" / "test_interactive_during_heavy_work.py").read_bytes()
        ).hexdigest(),
    }
    print(f"== diagnose_loop_stall ==\nrepo={repo}\nsha={sha}\nlabel={label}\n"
          f"runs={args.runs} scenarios={scenarios} pressure={args.pressure}\nout={out}\n")

    tmp_root = Path(tempfile.mkdtemp(prefix="qio-diag-"))
    all_runs: list[RunResult] = []
    summaries: dict[str, dict] = {}

    async def _main() -> None:
        for scenario in scenarios:
            print(f"-- scenario {scenario} (warmup {args.warmup}) --", flush=True)
            for _ in range(max(0, args.warmup)):
                await _run_one(repo, scenario, -1, tmp_root=tmp_root,
                               threshold_ms=args.gap_threshold_ms, heavy_mod=heavy_mod)
            ctx_pressure = None
            if args.pressure in ("cpu", "both"):
                ctx_pressure = CpuPressure(max(1, (os.cpu_count() or 4) // 2))
            elif args.pressure == "disk":
                ctx_pressure = DiskPressure(tmp_root)
            if args.pressure == "both":
                ctx_pressure = CpuPressure(max(1, (os.cpu_count() or 4) // 2))
                with ctx_pressure:
                    with DiskPressure(tmp_root):
                        runs = await _run_scenario(repo, scenario, args.runs,
                                                   pressure=args.pressure,
                                                   threshold_ms=args.gap_threshold_ms,
                                                   tmp_root=tmp_root, heavy_mod=heavy_mod)
            elif ctx_pressure is not None:
                with ctx_pressure:
                    runs = await _run_scenario(repo, scenario, args.runs, pressure=args.pressure,
                                               threshold_ms=args.gap_threshold_ms,
                                               tmp_root=tmp_root, heavy_mod=heavy_mod)
            else:
                runs = await _run_scenario(repo, scenario, args.runs, pressure=args.pressure,
                                           threshold_ms=args.gap_threshold_ms,
                                           tmp_root=tmp_root, heavy_mod=heavy_mod)
            all_runs.extend(runs)
            key = f"{scenario}__{args.pressure}"
            summaries[key] = _summary(runs)
            payload = {"env": env, "scenario": scenario, "summary": summaries[key],
                       "runs": [_serialize_run(r) for r in runs]}
            path = out / f"{label}__{scenario}__{args.pressure}.json"
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"   -> {path}\n   summary: {json.dumps(summaries[key], ensure_ascii=False)}\n",
                  flush=True)

    asyncio.run(_main())

    csv_path = out / f"summary__{label}__{args.pressure}.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["scenario", "pressure", "index", "gap_ms", "turn_ms", "ok",
                         "kinds_in_gap", "max_gap_start", "max_gap_end", "error"])
        for run in all_runs:
            writer.writerow([
                run.scenario, run.pressure, run.index, f"{run.gap_ms:.3f}", f"{run.turn_ms:.3f}",
                run.ok, json.dumps(run.kinds, ensure_ascii=False),
                f"{run.max_gap.start:.6f}" if run.max_gap else "",
                f"{run.max_gap.end:.6f}" if run.max_gap else "",
                run.error or "",
            ])
    print(f"CSV -> {csv_path}")
    print(json.dumps({"env": env, "summaries": summaries}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())