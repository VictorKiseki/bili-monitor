"""L1：把新动态里的「少图图文」收进周表候选队列，并落盘图片。

用法：
    python scripts/weekly_scan.py                     # 扫全库（增量去重），输出待判定批次
    python scripts/weekly_scan.py --since 1793000000  # 只扫某时间之后的动态
    python scripts/weekly_scan.py --pending-only      # 不扫库，只导出还没判定的候选
    python scripts/weekly_scan.py --json data/schedules/inbox/batch.json

输出给视觉回合的 JSON 结构：
    {"generated_at":..., "count":N, "items":[{event_id,uid,uname,text,title,posted_at,url,
      images:[{url,path,w,h}]}]}

    2026-11-05：加了 title（动态标题）。开播时间、周次常只写在标题里
    （例：「屋里吃饭 11月6日 周五晚7点」），判定时要和 text 一起读。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import weekly  # noqa: E402
from store.sqlite import SCHEMA  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/bot.db")
    ap.add_argument("--since", type=int, default=0)
    ap.add_argument("--days", type=float, default=0,
                    help="只扫最近 N 天（优先于 --since，0 表示不限制）")
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--pending-only", action="store_true")
    ap.add_argument("--no-download", action="store_true")
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(SCHEMA)

    since = args.since
    if args.days:
        since = int(time.time() - args.days * 86400)
    added = []
    if not args.pending_only:
        added = weekly.scan(conn, since=since, limit=args.limit,
                            download=not args.no_download)
    batch = weekly.pending_batch(conn, limit=args.limit)

    payload = {"generated_at": int(time.time()), "count": len(batch),
               "new": len(added), "items": batch}
    if args.json:
        p = Path(args.json)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", "utf-8")
        print(f"批次写入 {p}（新增 {len(added)}，待判定 {len(batch)}）")
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
