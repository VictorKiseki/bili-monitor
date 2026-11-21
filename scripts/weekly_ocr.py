#!/usr/bin/env python3
"""周表图片的 OCR「第二意见」（Windows.Media.Ocr，纯本地，不联网、不调模型）。

为什么：视觉模型读手写/艺术字强，但可能把 9:30 看成 9:00；OCR 对印刷体数字和
星期几更稳，对连笔手写基本无能为力。所以两者互为校验——

  - 一致：多一条确认，ingest 记 agree
  - 不一致：标 needs_review 并在备注里写清「视觉 X / OCR Y」
  - OCR 什么也没读到（手写常见）：记 ocr_na，不当成冲突

用法：
  python scripts/weekly_ocr.py --event-id <UID>:dynamic:<动态ID> --json
  python scripts/weekly_ocr.py --image data/schedules/<uid>/<dyn>_0.jpg --json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.proc import no_window_flags  # noqa: E402

PS1 = ROOT / "data" / "_probe" / "ocr2.ps1"
CACHE = ROOT / "data" / "ocr"
DB = ROOT / "data" / "bot.db"

DAY_RE = re.compile(r"^(MON|TUE|WED|THU|FRI|SAT|SUN)[A-Z]*[.,:;]?$", re.I)
DAY_ALIAS = {"MON": "MON", "MOM": "MON", "TUE": "TUE", "WED": "WED",
             "THU": "THU", "THUR": "THU", "FRI": "FRI", "SAT": "SAT", "SUN": "SUN"}


def _norm(t: str) -> str:
    return re.sub(r"[\s\u3000·・.。,，:：;；'\"!！?？()（）\[\]【】<>《》~～—\-_/\\]+", "", t or "")


def _mkstemp(suffix: str) -> Path:
    """Windows 上 mkstemp 会占着句柄，必须先 close 才能被子进程写/被删除。"""
    fd, p = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    return Path(p)


def _run_ps1(img: Path) -> list[dict]:
    """跑一次 Windows OCR，返回 [{text,x,y,w,h}]。任何失败都返回 []（OCR 只是辅助）。"""
    if not PS1.exists():
        return []
    out = _mkstemp(".tsv")
    try:
        subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                        "-File", str(PS1), str(img), str(out)],
                       capture_output=True, timeout=90,
                       creationflags=no_window_flags())
        raw = out.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:                            # noqa: BLE001
        return []
    finally:
        try:
            out.unlink(missing_ok=True)
        except OSError:
            pass
    lines = []
    for i, ln in enumerate(raw):
        parts = ln.split("\t")
        if i == 0 and parts[0] == "LANG":
            continue
        if len(parts) < 5:
            continue
        try:
            x, y, w, h = (int(float(parts[j])) for j in range(4))
        except ValueError:
            continue
        txt = parts[4].strip()
        if txt:
            lines.append({"text": txt, "x": x, "y": y, "w": w, "h": h})
    return lines


def _grids(w: int, h: int):
    """切成 n×n 块（带重叠），每块单独 OCR。整图 OCR 会被背景/立绘干扰，
    切块反而读得出印刷体；这是实测结论，不是理论最优。"""
    big = w * h > 5_000_000                  # 超大图少切一轮，控制在 ~2-4 秒
    for n, ov in ((1, 0.0), (2, 0.24), (3, 0.18)):
        if n == 3 and big:
            continue
        step = 1.0 / n
        size = min(1.0, step * (1 + ov))
        for i in range(n):
            for j in range(n):
                fx = 0.0 if n == 1 else max(0.0, min(1.0 - size, i * step - ov * step / 2))
                fy = 0.0 if n == 1 else max(0.0, min(1.0 - size, j * step - ov * step / 2))
                yield (int(fx * w), int(fy * h),
                       min(w, int((fx + size) * w)), min(h, int((fy + size) * h)))


def _variants(im):
    """原图 + 2×/3×3 分块（自适应放大倍数）。放大对印刷体有效，对连笔手写无效。"""
    from PIL import Image, ImageOps

    gray = ImageOps.autocontrast(im.convert("L"))
    W, H = gray.size
    for box in _grids(W, H):
        tile = gray.crop(box)
        tw, th = tile.size
        k = 2.0 if max(tw, th) * 2 <= 3000 else (1.5 if max(tw, th) * 1.5 <= 3400 else 1.0)
        big = ImageOps.autocontrast(tile.resize((int(tw * k), int(th * k)), Image.LANCZOS))
        yield big, k, box[0], box[1]


def ocr_image(path: str | Path) -> dict:
    """对一张图做 OCR：多尺度 + 分块，坐标映射回原图，按文本去重。"""
    from PIL import Image

    img = Path(path)
    t0 = time.time()
    im = Image.open(img)
    seen: dict[str, dict] = {}
    for variant, k, ox, oy in _variants(im):
        tmp = _mkstemp(".png")
        try:
            variant.save(tmp)
            for ln in _run_ps1(tmp):
                key = _norm(ln["text"])
                if not key:
                    continue
                cand = {"text": ln["text"],
                        "x": int(ln["x"] / k) + ox, "y": int(ln["y"] / k) + oy,
                        "h": int(ln["h"] / k)}
                old = seen.get(key)
                if old is None or cand["h"] > old["h"]:
                    seen[key] = cand
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
    lines = sorted(seen.values(), key=lambda d: (d["y"], d["x"]))
    return {"image": str(img), "size": list(im.size), "lines": lines,
            "text": "\n".join(l["text"] for l in lines),
            "ms": int((time.time() - t0) * 1000)}


FULLWIDTH = str.maketrans("０１２３４５６７８９：；～", "0123456789:;~")
TIME_RE = re.compile(r"(\d{1,2})\s*[:;]\s*(\d{2})")
RANGE_RE = re.compile(r"(\d{1,2})\s*[~\-—～、，,至到]\s*(\d{1,2})")


def ocr_hours(text: str) -> set[str]:
    """OCR 文本里能认出来的小时（HH 两位、字符串形态）。手写体基本认不出，正常。"""
    t = (text or "").translate(FULLWIDTH).replace("，", " ").replace("。", " ")
    hours: set[str] = set()
    for h, _m in TIME_RE.findall(t):
        if 0 <= int(h) <= 29:
            hours.add("%02d" % int(h))
    for a, b in RANGE_RE.findall(t):
        # 9~12 这类区间两头都算「看到了」，避免把合法的 9:30 判成缺失
        for h in (a, b):
            if 0 <= int(h) <= 29:
                hours.add("%02d" % int(h))
    return hours


def vision_hours(days: list[dict]) -> set[str]:
    out: set[str] = set()
    for d in days or []:
        for e in (d.get("entries") or d.get("items") or []):
            t = str(e.get("time") or "")
            m = re.match(r"(\d{1,2})[:：](\d{2})", t)
            if m:
                out.add("%02d" % int(m.group(1)))
                continue
            tt = str(e.get("time_text") or "")
            mm = re.match(r"\s*(\d{1,2})\s*[~\-—～]", tt)
            if mm:
                out.add("%02d" % int(mm.group(1)))
    return out


def compare(text: str, days: list[dict]) -> dict:
    """视觉结果 vs OCR 的小时集合。

    宁可漏报不要误报：OCR 读连笔手写时经常只蹦出一两个孤立数字（\"0\"、\"03\"），
    拿这些去否视觉结果是自找麻烦。所以要求「至少 2 个像样的开播小时」才比较，
    要判 diff 还要 OCR 读到 3 个以上、且一个都对不上。
    """
    vh = vision_hours(days)
    oh = {h for h in ocr_hours(text) if 6 <= int(h) <= 27}   # 凌晨 6 点前不算开播时间
    if not vh or len(oh) < 2:
        return {"signal": "na", "matched": 0, "total": len(vh),
                "note": "OCR 没读到可用的时间" if vh else ""}
    hit = len(oh & vh)
    ratio = hit / float(len(vh))
    if ratio >= 0.6:
        sig, note = "agree", "OCR 也对上了 %d/%d 个时间" % (hit, len(vh))
    elif ratio <= 0.25 and len(oh) >= 3:
        sig, note = "diff", "OCR 只对上 %d/%d 个时间，视觉结果建议复核" % (hit, len(vh))
    else:
        sig, note = "partial", "OCR 对上 %d/%d 个时间" % (hit, len(vh))
    return {"signal": sig, "matched": hit, "total": len(vh), "note": note}


def day_map(lines: list[dict]) -> dict[str, str]:
    """有 ≥4 个可识别的 MON/TUE… 标签时，把其余行按 y 归到最近的星期。"""
    labels = []
    for ln in lines:
        m = DAY_RE.match(ln["text"].strip())
        if m:
            d = DAY_ALIAS.get(m.group(1).upper()[:3])
            if d:
                labels.append((ln["y"], d))
    if len({d for _, d in labels}) < 4:
        return {}
    labels.sort()
    out: dict[str, list[str]] = {d: [] for _, d in labels}
    for ln in lines:
        if DAY_RE.match(ln["text"].strip()):
            continue
        near = min(labels, key=lambda p: abs(p[0] - ln["y"]))
        if abs(near[0] - ln["y"]) <= 220:        # 太远的（页脚/装饰）不算
            out[near[1]].append(ln["text"])
    return {d: " ".join(v) for d, v in out.items() if v}


def ensure(conn: sqlite3.Connection, event_id: str, image_path: str | Path,
           image_index: int = 0, force: bool = False) -> dict:
    """取缓存或现跑一次 OCR；结果写进 weekly_ocr 表。"""
    row = (None if force else conn.execute(
        "SELECT text, lines_json, ms FROM weekly_ocr WHERE event_id=? AND image_index=?",
        (event_id, image_index)).fetchone())
    if row:
        try:
            return {"text": row["text"], "lines": json.loads(row["lines_json"]),
                    "ms": row["ms"], "cached": True}
        except Exception:                        # noqa: BLE001
            pass
    res = ocr_image(image_path)
    res["days"] = day_map(res["lines"])
    res["cached"] = False
    try:
        conn.execute(
            "INSERT OR REPLACE INTO weekly_ocr(event_id, image_index, image_path, lang,"
            " lines_json, text, days_json, ms, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (event_id, image_index, str(image_path), "", json.dumps(res["lines"], ensure_ascii=False),
             res["text"], json.dumps(res["days"], ensure_ascii=False), res["ms"], time.time()))
        conn.commit()
    except sqlite3.Error:
        pass
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--event-id", default="")
    ap.add_argument("--image", default="")
    ap.add_argument("--image-index", type=int, default=0)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--backfill-week", default="",
                    help="给该周已入库的周表补跑 OCR（YYYY-MM-DD）")
    args = ap.parse_args()

    if not (args.event_id or args.image or args.backfill_week):
        print("需要 --event-id / --image / --backfill-week", file=sys.stderr)
        return 2

    from store.sqlite import SCHEMA, migrate

    conn = sqlite3.connect(str(DB), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    migrate(conn)

    if args.backfill_week:
        from core.weekly import _ocr_signal
        rows = conn.execute("SELECT * FROM weekly_schedule WHERE week_start=?"
                            " AND (ocr_signal IS NULL OR ocr_signal='') ORDER BY uid",
                            (args.backfill_week,)).fetchall()
        n = 0
        for r in rows:
            cand = conn.execute("SELECT images_json FROM weekly_candidate WHERE event_id=?",
                                (r["event_id"],)).fetchone()
            imgs = json.loads((cand["images_json"] if cand else "[]") or "[]")
            path = (imgs[0].get("path") if imgs else "") or ""
            if not path:
                continue
            days = json.loads(r["days_json"] or "[]")
            sig, note, text = _ocr_signal(conn, r["event_id"], path, days)
            if not sig:
                continue
            suffix = ("；" if r["note"] else "") + note
            conn.execute("UPDATE weekly_schedule SET ocr_signal=?, ocr_text=?,"
                         " needs_review=CASE WHEN ?='diff' THEN 1 ELSE needs_review END,"
                         " note=CASE WHEN ?<>'' THEN COALESCE(note,'') || ? ELSE note END"
                         " WHERE uid=? AND week_start=?",
                         (sig, text, sig, note, suffix, r["uid"], r["week_start"]))
            conn.commit()
            n += 1
            print("%-16s %s  %s" % (r["uname"], sig, note))
        print(json.dumps({"ok": True, "week": args.backfill_week, "filled": n}, ensure_ascii=False))
        return 0

    if not args.image:
        row = conn.execute("SELECT url, images_json, uid, dyn_id FROM weekly_candidate"
                           " WHERE event_id=?", (args.event_id,)).fetchone()
        if not row:
            print(json.dumps({"ok": False, "error": "candidate-not-found"}, ensure_ascii=False))
            return 1
        imgs = json.loads(row["images_json"] or "[]")
        idx = min(args.image_index, max(0, len(imgs) - 1))
        path = ROOT / "data" / "schedules" / str(row["uid"]) / (
            "%s_%d.jpg" % (Path(row["url"]).name.split(".")[0] if row["url"] else row["dyn_id"], idx))
        if not path.exists():
            # 退一步：候选里记的本地路径
            cand = [p for p in (ROOT / "data" / "schedules" / str(row["uid"])).glob(
                "%s_*" % row["dyn_id"])]
            if not cand:
                print(json.dumps({"ok": False, "error": "image-not-found", "hint": str(path)},
                                 ensure_ascii=False))
                return 1
            path = sorted(cand)[idx]
    else:
        path = Path(args.image)

    res = ensure(conn, args.event_id or str(path), path, args.image_index, force=args.force)
    out = {"ok": True, "image": str(path), "ms": res.get("ms"), "cached": res.get("cached"),
           "text": res["text"], "days": res.get("days", {})}
    if args.json:
        print(json.dumps(out, ensure_ascii=False))
    else:
        print("用时 %d ms  %s" % (res.get("ms") or 0, path))
        print(res["text"] or "（OCR 没读到文字）")
        for d, t in (res.get("days") or {}).items():
            print("  %s: %s" % (d, t))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
