#!/usr/bin/env python3
"""用「真实开播事件」回归检验周表抽得准不准 —— 手写体识别的校准机制。

周表是预测，live_start 是事实：同一周结束后一比，就知道哪个主播的模板/手写
被读错了几条。准确率低的主播，下次识别强制走「两遍 + 逐行裁切」。

    python scripts/weekly_verify.py                 # 本周
    python scripts/weekly_verify.py --week 2026-11-02 --json
    python scripts/weekly_verify.py --uid 435243735
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import weekly as W  # noqa: E402

TOL_MIN = 90          # 开播比预告早/晚多少分钟内算命中（主播普遍晚点）


def week_bounds(week_start: str) -> tuple[int, int]:
    d = dt.date.fromisoformat(week_start)
    a = time.mktime(dt.datetime(d.year, d.month, d.day).timetuple())
    return int(a), int(a + 7 * 86400)


def live_starts(conn, uid: int, start: int, end: int) -> list[dict]:
    rows = conn.execute(
        "SELECT occurred_at, title FROM event WHERE subject_uid=? AND type='live_start'"
        " AND occurred_at>=? AND occurred_at<? ORDER BY occurred_at",
        (uid, start, end)).fetchall()
    return [{"ts": r["occurred_at"], "title": r["title"]} for r in rows]


def match(preds: list[dict], lives: list[dict]) -> dict:
    hit = [False] * len(lives)
    matched, missed, wrong = [], [], []
    for p in preds:
        want = p["ts"]
        best, best_d = None, None
        for i, lv in enumerate(lives):
            if hit[i]:
                continue
            d = abs(lv["ts"] - want) / 60.0
            if best_d is None or d < best_d:
                best, best_d = i, d
        if best is not None and best_d is not None and best_d <= TOL_MIN:
            hit[best] = True
            matched.append({**p, "live_ts": lives[best]["ts"], "delta_min": round(best_d)})
        else:
            wrong.append(p)
    for i, lv in enumerate(lives):
        if not hit[i]:
            missed.append(lv)
    return {"matched": matched, "wrong": wrong, "unscheduled": missed}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/bot.db")
    ap.add_argument("--week", default="")
    ap.add_argument("--uid", type=int, default=0)
    ap.add_argument("--include-future", action="store_true",
                    help="连还没到的日子一起比（默认只比已经过去的日子）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    week = args.week or W.infer_week_start(time.time())
    start, end = week_bounds(week)

    sql = "SELECT * FROM weekly_schedule WHERE week_start=?"
    params: list = [week]
    if args.uid:
        sql += " AND uid=?"
        params.append(args.uid)
    rows = conn.execute(sql, params).fetchall()

    report = {"week": week, "week_label": W.week_label_of(week), "rows": [], "summary": {}}
    total_pred = total_hit = future = 0
    today = dt.date.today().isoformat()
    for r in rows:
        days = json.loads(r["days_json"] or "[]")
        preds = []
        for d in days:
            try:
                idx = W.WEEKDAYS.index(d.get("day"))
            except ValueError:
                continue
            for e in d.get("entries") or []:
                t = e.get("time")
                if not t:
                    continue
                hh, mm = t.split(":")
                day = dt.date.fromisoformat(week) + dt.timedelta(days=idx)
                if not args.include_future and day.isoformat() >= today:
                    future += 1                 # 还没到的日子不能算没播
                    continue
                ts = time.mktime(dt.datetime(day.year, day.month, day.day,
                                             int(hh), int(mm)).timetuple())
                preds.append({"day": d["day"], "time": t, "text": e.get("text") or "",
                              "ts": int(ts)})
        lives = live_starts(conn, r["uid"], start, end)
        res = match(preds, lives)
        total_pred += len(preds)
        total_hit += len(res["matched"])
        report["rows"].append({
            "uid": r["uid"], "uname": r["uname"], "predicted": len(preds),
            "matched": len(res["matched"]), "wrong": res["wrong"],
            "unscheduled": res["unscheduled"], "needs_review": bool(r["needs_review"]),
            "extractor": r["extractor"]})
    report["summary"] = {"streamers": len(rows), "predicted": total_pred,
                         "matched": total_hit, "future_skipped": future,
                         "rate": round(total_hit / total_pred, 3) if total_pred else None}

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 0
    print("周 %s（%s） 已过去的定时预告命中 %d/%d 条，命中率 %s（另有 %d 条还没到日子）" % (
        week, report["week_label"], total_hit, total_pred,
        ("%.0f%%" % (100 * total_hit / total_pred)) if total_pred else "n/a", future))
    for r in report["rows"]:
        print("\n- %s（uid %s） 预告 %d 条，命中 %d 条%s" % (
            r["uname"], r["uid"], r["predicted"], r["matched"],
            "  [需复核]" if r["needs_review"] else ""))
        for w in r["wrong"]:
            print("    ✗ 预告了没播：%s %s %s" % (w["day"], w["time"], w["text"]))
        for u in r["unscheduled"]:
            print("    ＋ 没预告也播了：%s %s" % (
                time.strftime("%m-%d %H:%M", time.localtime(u["ts"])), u["title"][:24]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
