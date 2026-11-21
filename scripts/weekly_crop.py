#!/usr/bin/env python3
"""把周表图切成「一行一张」，专治手写体：整图一眼扫过去容易串行/漏字。

    python scripts/weekly_crop.py data/schedules/<uid>/<dyn>_0.jpg
    python scripts/weekly_crop.py <img> --rows 7 --top 0.10 --bottom 0.95
    python scripts/weekly_crop.py <img> --auto            # 投影切带（印刷体模板更准）

默认按 7 行等分：周表几乎都是「周一到周日」固定行距，等分比找分隔线稳。
切完的文件在 <img 同目录>/crops/<stem>/，manifest.json 记录每条的 y 范围和路径。
给视觉回合用：逐条 read_image 读一行，两遍结果不一致的行才进人工复核。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from PIL import Image

INK = 170                 # 灰度低于这个值算「有字」
MIN_BAND = 0.012          # --auto 模式：一条文字带至少占图高 1.2%


def ink_profile(img: Image.Image) -> list[float]:
    g = img.convert("L")
    w, h = g.size
    px = g.load()
    step = max(1, w // 900)                  # 横向抽样，够用且快
    prof = []
    for y in range(h):
        dark = 0
        for x in range(0, w, step):
            if px[x, y] < INK:
                dark += 1
        prof.append(dark / (w / step))
    return prof


def smooth(prof: list[float], win: int) -> list[float]:
    if win <= 1:
        return prof
    half = win // 2
    q = [0.0] * (len(prof) + 1)
    for i, v in enumerate(prof):
        q[i + 1] = q[i] + v
    out = []
    for i in range(len(prof)):
        a = max(0, i - half)
        b = min(len(prof), i + half + 1)
        out.append((q[b] - q[a]) / (b - a))
    return out


def ink_span(prof: list[float], h: int) -> tuple[int, int]:
    top = max(prof) if prof else 0.0
    if top <= 0:
        return int(h * 0.05), int(h * 0.95)
    ys = [y for y, v in enumerate(prof) if v > top * 0.04]
    return ys[0], ys[-1]


def auto_bands(prof: list[float], h: int) -> list[tuple[int, int]]:
    top = max(prof) if prof else 0.0
    if top <= 0:
        return []
    thr = max(0.006, top * 0.18)
    runs, start = [], None
    for y, v in enumerate(prof):
        if v >= thr and start is None:
            start = y
        elif v < thr and start is not None:
            runs.append((start, y))
            start = None
    if start is not None:
        runs.append((start, h - 1))
    merged: list[list[int]] = []
    gap = max(2, int(h * 0.01))
    for a, b in runs:
        if merged and a - merged[-1][1] <= gap:
            merged[-1][1] = b
        else:
            merged.append([a, b])
    min_band = max(6, int(h * MIN_BAND))
    return [(a, b) for a, b in merged if b - a >= min_band]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("--out", default="")
    ap.add_argument("--rows", type=int, default=7, help="等分几行（默认 7）")
    ap.add_argument("--auto", action="store_true", help="按墨迹投影切带，不按行等分")
    ap.add_argument("--top", type=float, default=None, help="起始比例（0~1），默认按墨迹自动")
    ap.add_argument("--bottom", type=float, default=None, help="结束比例（0~1），默认按墨迹自动")
    ap.add_argument("--scale", type=float, default=2.0, help="放大倍数，手写体建议 2~3")
    ap.add_argument("--json", action="store_true", help="只输出 manifest JSON")
    args = ap.parse_args()

    src = Path(args.image)
    if not src.is_file():
        print("找不到图片：%s" % src, file=sys.stderr)
        return 2
    img = Image.open(src)
    w, h = img.size
    out_dir = Path(args.out) if args.out else src.parent / "crops" / src.stem
    out_dir.mkdir(parents=True, exist_ok=True)

    prof = smooth(ink_profile(img), max(3, int(h * 0.004)))
    span0, span1 = ink_span(prof, h)
    y0 = int(h * args.top) if args.top is not None else max(0, span0 - int(h * 0.01))
    y1 = int(h * args.bottom) if args.bottom is not None else min(h, span1 + int(h * 0.01))

    bands = auto_bands(prof, h) if args.auto else []
    mode = "auto"
    if args.auto and len(bands) >= 4:
        pass
    else:
        n = max(1, args.rows)
        step = (y1 - y0) / n
        bands = [(int(y0 + i * step), int(y0 + (i + 1) * step)) for i in range(n)]
        mode = "grid"

    pad = max(2, int((y1 - y0) * 0.006))
    items = []
    for i, (a, b) in enumerate(bands):
        box = (0, max(0, a - pad), w, min(h, b + pad))
        crop = img.crop(box)
        if args.scale and args.scale != 1:
            crop = crop.resize((int(crop.width * args.scale), int(crop.height * args.scale)),
                               Image.LANCZOS)
        name = "row%d.png" % i
        crop.save(out_dir / name)
        items.append({"index": i, "y0": box[1], "y1": box[3], "path": str(out_dir / name)})

    manifest = {"image": str(src), "size": [w, h], "mode": mode,
                "span": [y0, y1], "bands": items, "count": len(items)}
    (out_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1),
                                           encoding="utf-8")
    if args.json:
        print(json.dumps(manifest, ensure_ascii=False))
    else:
        print("切出 %d 条（%s，y %d~%d），目录：%s" % (len(items), mode, y0, y1, out_dir))
        for it in items:
            print("  %s" % it["path"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
