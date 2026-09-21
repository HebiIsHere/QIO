"""本地 ONNX 嵌入模型的自检与 A/B 对比（离线，不联网，不调用任何大模型）。

用途：

1. **自检**：模型文件能不能真的加载、输入名是不是预期的三个、输出维度对不对、
   向量有没有归一化 —— 打包之前必须过这一关，否则「内置了模型」只是文件名对。
2. **对比**：同一批中文文本分别用 fp32 与 int8 编码，报告
   * 同一段文本两种精度的余弦一致度（越接近 1 越说明量化没改变语义）；
   * 一组「查询 → 正确片段」的排序结果：recall@1 / recall@3 与 top-1 一致率；
   * 单条编码耗时（ms/条）与整批耗时 —— 给「fp32 比 int8 慢多少」一个本机数字。

用法（模型目录里有 model.onnx 与 model_quantized.onnx）：

    cd backend
    uv run --frozen python ../scripts/models/onnx_ab.py --model-dir "C:\\Tools\\models\\bge-small-zh-v1.5"

注意：两种精度的向量**不在同一个空间**里，所以对比时各自建索引、各自排序，
只比较「排序结果是否一致」，不直接比跨模型的相似度。
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import numpy as np


def load_session(model_path: Path):
    import onnxruntime as ort

    so = ort.SessionOptions()
    so.intra_op_num_threads = 0  # 由 onnxruntime 自己决定
    return ort.InferenceSession(str(model_path), sess_options=so, providers=["CPUExecutionProvider"])


def load_tokenizer(model_dir: Path):
    from tokenizers import Tokenizer

    path = model_dir / "tokenizer.json"
    if not path.exists():
        raise SystemExit(f"缺少 tokenizer.json：{path}")
    return Tokenizer.from_file(str(path))


def embed_batch(session, tokenizer, texts: list[str], max_len: int = 512) -> np.ndarray:
    inputs = [i.name for i in session.get_inputs()]
    vectors: list[np.ndarray] = []
    for text in texts:
        enc = tokenizer.encode(text[:4000])
        ids = np.array([enc.ids[:max_len]], dtype=np.int64)
        mask = np.array([enc.attention_mask[:max_len]], dtype=np.int64)
        feed = {inputs[0]: ids, inputs[1]: mask}
        if len(inputs) > 2:
            feed[inputs[2]] = np.zeros_like(ids)
        hidden = session.run(None, feed)[0]
        mask_f = mask.astype(np.float32)[:, :, None]
        pooled = (hidden * mask_f).sum(axis=1) / np.maximum(mask_f.sum(axis=1), 1e-9)
        vectors.append(pooled[0])
    matrix = np.stack(vectors).astype(np.float32)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    return matrix / np.maximum(norms, 1e-9)


DOCS = [
    "虚拟滚动列表在长对话里要动态测量每一条消息的真实高度，否则会出现重叠。",
    "跟随底部的判定口径是：距底部 120 像素以内算跟随，用户上翻就立刻停止跟随。",
    "知识页默认筛选应当与「有内容可看」一致，而不是筛出空列表。",
    "审批窗口在用户正在输入时不自动弹出，只亮出「有 N 项操作等待确认」的入口。",
    "星球相机聚焦距离是 2.25，展开动画 420 毫秒，收起 280 毫秒。",
    "凭据密钥只写不读：保存后不再显示明文，仅写入本机密钥库。",
    "分段边界里容量只是兜底：长度到点分块，但不表示这一阶段的任务已经做完。",
    "话题切换本身不证明当前阶段结束，回来时依据接续意图与容量决定是否分块。",
]

QUERIES = [
    ("滚动到底部什么时候算跟随？", 1),
    ("长聊天列表消息重叠怎么解决", 0),
    ("知识面板筛出来是空的", 2),
    ("输入的时候审批弹窗会不会打扰我", 3),
    ("星球展开动画多长时间", 4),
    ("明文密钥会不会被存下来", 5),
    ("长度到点就代表任务完成了吗", 6),
    ("我切走再回来会不会自动断开话题", 7),
]


def rank(session, tokenizer, docs: list[str], queries: list[str]) -> tuple[np.ndarray, np.ndarray]:
    mat = embed_batch(session, tokenizer, docs)
    q = embed_batch(session, tokenizer, queries)
    return mat, q


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model-dir", required=True)
    args = ap.parse_args()
    model_dir = Path(args.model_dir)

    fp32_path = model_dir / "model.onnx"
    int8_path = model_dir / "model_quantized.onnx"
    for path in (fp32_path, int8_path):
        if not path.exists():
            raise SystemExit(f"缺少模型文件：{path}")

    tokenizer = load_tokenizer(model_dir)
    sessions: dict[str, object] = {}
    for label, path in (("fp32", fp32_path), ("int8", int8_path)):
        t0 = time.perf_counter()
        session = load_session(path)
        inputs = [(i.name, i.shape) for i in session.get_inputs()]
        print(f"[加载] {label}: {path.name} ({path.stat().st_size / 1024 / 1024:.1f}MB) "
              f"用时 {time.perf_counter() - t0:.2f}s 输入={inputs}")
        sessions[label] = session

    # 1) 自检：向量维度与非零
    probe = embed_batch(sessions["fp32"], tokenizer, [DOCS[0], QUERIES[0][0]])
    print(f"[自检] fp32 输出形状={probe.shape} 归一化={np.allclose(np.linalg.norm(probe, axis=1), 1, atol=1e-3)}")
    if probe.shape[1] != 512:
        raise SystemExit("输出维度不是 512，模型与 QIO 期望不一致")

    # 2) 同一段文本两种精度的向量一致度
    fp32_vecs = embed_batch(sessions["fp32"], tokenizer, DOCS + [q for q, _ in QUERIES])
    int8_vecs = embed_batch(sessions["int8"], tokenizer, DOCS + [q for q, _ in QUERIES])
    sims = (fp32_vecs * int8_vecs).sum(axis=1)
    print(f"[一致度] 同一文本 fp32↔int8 余弦：均值={sims.mean():.4f} 最小={sims.min():.4f}")

    # 3) 排序对比（各自建索引、各自排序）
    doc_mat_fp32, q_mat_fp32 = rank(sessions["fp32"], tokenizer, DOCS, [q for q, _ in QUERIES])
    doc_mat_int8, q_mat_int8 = rank(sessions["int8"], tokenizer, DOCS, [q for q, _ in QUERIES])

    def report(label: str, doc_mat: np.ndarray, q_mat: np.ndarray) -> np.ndarray:
        order = np.argsort(-(q_mat @ doc_mat.T), axis=1)
        hits1 = sum(1 for i, (_, want) in enumerate(QUERIES) if order[i][0] == want)
        hits3 = sum(1 for i, (_, want) in enumerate(QUERIES) if want in order[i][:3])
        print(f"[排序] {label}: recall@1={hits1}/{len(QUERIES)} recall@3={hits3}/{len(QUERIES)}")
        return order

    order_fp32 = report("fp32", doc_mat_fp32, q_mat_fp32)
    order_int8 = report("int8", doc_mat_int8, q_mat_int8)
    same_top1 = int((order_fp32[:, 0] == order_int8[:, 0]).sum())
    print(f"[排序] top-1 一致率：{same_top1}/{len(QUERIES)}")

    # 4) 耗时（同一批文本，各跑两轮取第二轮，避免首次预热算进去）
    payload = DOCS + [q for q, _ in QUERIES] + ["再补一条用于拉长批次的句子。"] * 5
    timings: dict[str, float] = {}
    for label, session in sessions.items():
        embed_batch(session, tokenizer, payload[:4])  # 预热
        t0 = time.perf_counter()
        embed_batch(session, tokenizer, payload)
        dt = time.perf_counter() - t0
        timings[label] = dt / len(payload) * 1000
        print(f"[耗时] {label}: 整批 {len(payload)} 条 {dt:.2f}s → {timings[label]:.1f} ms/条")
    if timings["int8"] > 0:
        print(f"[耗时] fp32 / int8 = {timings['fp32'] / timings['int8']:.2f}×")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
