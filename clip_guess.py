"""CLIP zero-shot 六宫格识别器（独立脚本，可单独运行）。

识别 tencentcloud.com 注册验证码（click_image_uncheck 六宫格）的答案格：
对每个格子图像与指令文本算 CLIP 相似度，输出最匹配的格子编号（1-6）。
低置信度（最高分与次高分差距过小）时输出 0 表示建议换题。

用法：
    python clip_guess.py <挑战图.jpg> <指令>
    python clip_guess.py --file live_bridge.jpg --text Bridge --json

依赖：torch、transformers、pillow、numpy；模型权重目录 clip-model/（见 README）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence

import numpy as np
from PIL import Image

TEXT_TEMPLATES = [
    "a photo of a {noun}",
    "a photo of the {noun}",
    "a photo of {noun}",
    "{noun}",
]

# 常见中文题面 -> 英文（CLIP 为英文模型）。题库词汇常见，覆盖常用名词。
ZH_EN = {
    "棕色沙发": "brown sofa", "白色沙发": "white sofa", "沙发": "sofa",
    "割草机": "lawn mower", "苏打水瓶": "soda bottle", "水瓶": "water bottle",
    "瓶子": "bottle", "乐器": "musical instrument", "猎犬": "hound",
    "狗": "dog", "清洁工具": "cleaning tools", "灯罩": "lampshade",
    "鱼": "fish", "家具": "furniture", "鸵鸟": "ostrich", "鹰": "eagle",
    "葡萄": "grapes", "飞机": "airplane", "巴士": "bus", "海象": "walrus",
    "闹钟": "alarm clock", "鸟": "bird", "花": "flower", "花瓶": "vase",
    "水果": "fruit", "桌子": "table", "椅子": "chair", "钟表": "clock",
    "头盔": "helmet", "包": "bag", "鞋子": "shoes", "帽子": "hat",
    "书": "book", "自行车": "bicycle", "汽车": "car", "船": "boat",
    "火车": "train", "树": "tree", "蔬菜": "vegetables", "蛋糕": "cake",
    "桥": "bridge", "狐猴": "lemur", "白鹭": "egret", "足球": "football",
}

_MODEL: Any | None = None
_PROCESSOR: Any | None = None

# ------------------------------------------------------------------ 模型获取

DEFAULT_HF_REPO = "openai/clip-vit-base-patch32"
_DOWNLOAD_FLAG = "_downloading"


def _setup_download_env():
    """给下载过程临时挂代理, 返回 restore() 供 finally 还原。

    huggingface.co 国内直连会 20s 超时; 邮箱/上传等业务请求不需要代理,
    所以下完必须还原, 否则 requests 也会被塞进代理。
    """
    # 不设 TX_DL_PROXY 就直连: GitHub Actions runner 直连 HF 正常,
    # 只有国内本地开发才需要临时挂代理。
    proxy = os.environ.get("TX_DL_PROXY", "")
    if not proxy:
        return lambda: None
    keys = ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy")
    saved = {k: os.environ.get(k) for k in keys}
    for k in keys:
        os.environ.setdefault(k, proxy)

    def restore() -> None:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    return restore


def _model_complete(path: Path) -> bool:
    return (path / "config.json").exists() and any(
        (path / f).exists() for f in ("pytorch_model.bin", "model.safetensors")
    )


def ensure_model(model_dir: str | os.PathLike | None = None) -> str:
    """确保本地 CLIP 模型目录完整; 缺失时自动下载 openai/clip-vit-base-patch32

    返回可用模型目录(绝对路径)。已完整则零开销直接返回。
    """
    if model_dir is None:
        model_dir = os.environ.get("TX_CLIP_MODEL",
                                   str(Path(__file__).resolve().with_name("clip-model")))
    path = Path(model_dir).resolve()
    if _model_complete(path):
        return str(path)

    if not os.environ.get("TX_CLIP_MODEL"):
        os.environ["TX_CLIP_MODEL"] = str(path)  # 本次会话内 _load 直接用
    print(f"[model] 本地模型缺失({path}), 开始自动下载 {DEFAULT_HF_REPO} ...", flush=True)
    restore_env = _setup_download_env()
    path.mkdir(parents=True, exist_ok=True)
    flag = path / _DOWNLOAD_FLAG
    try:
        flag.touch()
        try:
            from huggingface_hub import snapshot_download
            snapshot_download(DEFAULT_HF_REPO, local_dir=str(path))
            print(f"[model] 下载完成: {path}", flush=True)
        except Exception as e1:
            print(f"[model] snapshot_download 失败({str(e1)[:120]}), 改用 transformers 下载…", flush=True)
            from transformers import CLIPModel, CLIPProcessor
            m = CLIPModel.from_pretrained(DEFAULT_HF_REPO)
            pr = CLIPProcessor.from_pretrained(DEFAULT_HF_REPO)
            m.save_pretrained(str(path))
            pr.save_pretrained(str(path))
            print(f"[model] 下载完成: {path}", flush=True)
    finally:
        flag.unlink(missing_ok=True)
        restore_env()
    if not _model_complete(path):
        raise RuntimeError(f"模型下载不完整: {path}")
    return str(path)


# ------------------------------------------------------------------ 模型

def _load() -> None:
    global _MODEL, _PROCESSOR
    if _MODEL is not None:
        return
    from transformers import CLIPModel, CLIPProcessor
    name = os.environ.get(
        "TX_CLIP_MODEL",
        str(Path(__file__).resolve().with_name("clip-model")),
    )
    name = ensure_model(name)  # 缺失自动下载
    _PROCESSOR = CLIPProcessor.from_pretrained(name)
    _MODEL = CLIPModel.from_pretrained(name)
    _MODEL.eval()


def torch_inference_mode():
    import torch
    return torch.inference_mode()


def _tensor(out) -> np.ndarray:
    """兼容新旧 transformers：输出可能带 pooler_output 或是裸 tensor。"""
    value = getattr(out, "pooler_output", None)
    if value is None:
        value = out
    if hasattr(value, "detach"):
        value = value.detach()
    return np.asarray(value.cpu()) if hasattr(value, "cpu") else np.asarray(value)


def _encode_images(cells: Sequence[np.ndarray]) -> np.ndarray:
    images = [Image.fromarray(cell) for cell in cells]
    inputs = _PROCESSOR(images=images, return_tensors="pt")
    with torch_inference_mode():
        out = _MODEL.get_image_features(**inputs)
    feats = _tensor(out)
    return feats / np.linalg.norm(feats, axis=-1, keepdims=True)


def _encode_texts(texts: Sequence[str]) -> np.ndarray:
    inputs = _PROCESSOR(text=texts, return_tensors="pt", padding=True)
    with torch_inference_mode():
        out = _MODEL.get_text_features(**inputs)
    feats = _tensor(out)
    return feats / np.linalg.norm(feats, axis=-1, keepdims=True)


# ------------------------------------------------------------------ 文本

def to_english(instruction: str) -> str:
    text = instruction.strip().strip('"').strip()
    if not text:
        return text
    if text.isascii():
        return text
    lowered = text.lower()
    if lowered in ZH_EN:
        return ZH_EN[lowered]
    for zh, en in sorted(ZH_EN.items(), key=lambda item: -len(item[0])):
        if zh in lowered:
            return en
    return text


# ------------------------------------------------------------------ 图片切格

def split_image_grid(image: Image.Image, rows: int = 2, cols: int = 3) -> list[np.ndarray]:
    """六宫格按行列均分；顶部可能留白 34px 时用 region 方式切分更准。"""
    width, height = image.size
    cells = []
    for r in range(rows):
        for c in range(cols):
            box = (c * width // cols, r * height // rows,
                   (c + 1) * width // cols, (r + 1) * height // rows)
            cells.append(np.asarray(image.crop(box)))
    return cells


def split_cells_by_regions(image: Image.Image,
                           regions: Sequence[Sequence[int]]) -> list[np.ndarray]:
    """按服务端 json_payload.select_region_list 的 range 精确切格。"""
    cells = []
    for x0, y0, x1, y1 in regions:
        box = (max(0, int(x0)), max(0, int(y0)),
               min(image.width, int(x1)), min(image.height, int(y1)))
        cells.append(np.asarray(image.crop(box).convert("RGB")))
    return cells


def load_cells(path: str, regions: Sequence[Sequence[int]] | None = None) -> list[np.ndarray]:
    image = Image.open(path).convert("RGB")
    if regions:
        return split_cells_by_regions(image, regions)
    return split_image_grid(image)


# ------------------------------------------------------------------ 打分与选择

def score_cells(instruction: str, cells: Sequence[np.ndarray]) -> list[dict[str, Any]]:
    """返回 [{index, score}]，index 为 0 基格子序号，按分数降序。"""
    _load()
    noun = to_english(instruction)
    texts = [t.format(noun=noun) for t in TEXT_TEMPLATES]
    text_feat = _encode_texts(texts).mean(axis=0, keepdims=True)
    text_feat = text_feat / np.linalg.norm(text_feat)
    image_feat = _encode_images(cells)
    scores = (image_feat @ text_feat.T).ravel()
    ranked = sorted(
        ({"index": i, "score": float(s)} for i, s in enumerate(scores)),
        key=lambda item: item["score"], reverse=True,
    )
    return ranked


def pick_cells(instruction: str, cells: Sequence[np.ndarray], *, gap: float = 0.015,
               min_score: float = 0.185) -> list[int]:
    """选分数显著高于其它格的格子（1 基编号）。低置信度返回空列表（建议换题）。"""
    ranked = score_cells(instruction, cells)
    best = ranked[0]
    if best["score"] < min_score:
        return []
    if len(ranked) > 1:
        second = ranked[1]
        if second["score"] > best["score"] - gap:
            return []
    return [best["index"] + 1]


# ------------------------------------------------------------------ CLI

def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="六宫格验证码 CLIP 识别器")
    parser.add_argument("image", help="挑战图路径（672x480 六宫格）")
    parser.add_argument("instruction", nargs="?", help="题面指令，如 Bridge / 棕色沙发")
    parser.add_argument("--regions-json", help="json_payload.select_region_list 的 JSON 文件（可选）")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    args = parser.parse_args(argv)

    regions = None
    if args.regions_json:
        data = json.loads(Path(args.regions_json).read_text(encoding="utf-8"))
        regions = [tuple(item["range"]) if isinstance(item, dict) else tuple(item)
                   for item in data]
    cells = load_cells(args.image, regions)
    if args.instruction is None:
        parser.error("需要提供 instruction 题面指令")
    ranked = score_cells(args.instruction, cells)
    picks = pick_cells(args.instruction, cells)
    if args.json:
        print(json.dumps({
            "instruction": args.instruction,
            "english": to_english(args.instruction),
            "picks": picks,
            "scores": [{"cell": r["index"] + 1, "score": round(r["score"], 4)}
                       for r in ranked],
        }, ensure_ascii=False, indent=2))
    else:
        for r in ranked:
            mark = " <== 选择" if r["index"] + 1 in picks else ""
            print(f"格{r['index'] + 1}: {r['score']:.4f}{mark}")
        if not picks:
            print("低置信度：建议换题")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
