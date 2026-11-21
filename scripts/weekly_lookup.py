#!/usr/bin/env python3
"""联动外部对象查证（2026-11-04）。

日程文案里点到、但不在监控名单里的名字（如「萱萱卡娅」），按昵称到 B 站搜一次，
把 uid / 粉丝数 / 签名记进 streamer_search，管理台「日程表」页在最近更新下面展示。
管理台轮询时会自动挂后台线程去查；这个脚本用于手工补查、重置失败项。

    python scripts/weekly_lookup.py list
    python scripts/weekly_lookup.py one --name 萱萱卡娅
    python scripts/weekly_lookup.py pending --limit 5
    python scripts/weekly_lookup.py reset --name 萱萱卡娅
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from store.sqlite import SCHEMA, migrate  # noqa: E402


def open_db(path: str):
    import sqlite3

    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(SCHEMA)
    migrate(conn)
    conn.commit()
    return conn


def resolve(conn, client, name: str) -> dict:
    """搜一个名字并写库；返回这一行（dict）。"""
    hit, status, note = None, "none", ""
    try:
        low = name.lower()
        for it in client.search_user(name, limit=5):
            un = (it.get("uname") or "").lower()
            if low and (low in un or un in low):
                hit = it
                break
        if hit:
            status = "done"
        else:
            note = "B 站没搜到同名 UP 主"
    except Exception as e:                                 # noqa: BLE001
        status, note = "error", "%s: %s" % (type(e).__name__, e)
    if hit:
        conn.execute("UPDATE streamer_search SET uid=?, uname=?, fans=?, face=?, sign=?,"
                     " status=?, note=?, updated_at=? WHERE name=?",
                     (hit["uid"], hit["uname"], hit["fans"], hit["face"], hit["sign"],
                      status, note, time.time(), name))
    else:
        conn.execute("UPDATE streamer_search SET status=?, note=?, updated_at=? WHERE name=?",
                     (status, note, time.time(), name))
    conn.commit()
    return dict(conn.execute("SELECT * FROM streamer_search WHERE name=?", (name,)).fetchone())


def main() -> int:
    ap = argparse.ArgumentParser(description="联动外部对象查证（B 站搜索）")
    ap.add_argument("--db", default=str(ROOT / "data" / "bot.db"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="列出已登记的名字")
    ap_one = sub.add_parser("one", help="查一个名字")
    ap_one.add_argument("--name", required=True)
    ap_pend = sub.add_parser("pending", help="把待查的都查一遍")
    ap_pend.add_argument("--limit", type=int, default=10)
    ap_reset = sub.add_parser("reset", help="把一条重置为待查")
    ap_reset.add_argument("--name", required=True)
    args = ap.parse_args()

    conn = open_db(args.db)
    if args.cmd == "list":
        rows = conn.execute("SELECT * FROM streamer_search ORDER BY updated_at DESC").fetchall()
        for r in rows:
            print("%-16s %-8s uid=%-18s %s" % (r["name"], r["status"], r["uid"] or "-",
                                              r["uname"] or r["note"] or ""))
        print("共 %d 条" % len(rows))
        return 0
    if args.cmd == "reset":
        n = conn.execute("UPDATE streamer_search SET status='pending', note='' WHERE name=?",
                         (args.name,)).rowcount
        if not n:
            conn.execute("INSERT OR IGNORE INTO streamer_search(name, status, updated_at)"
                         " VALUES(?, 'pending', ?)", (args.name, time.time()))
        conn.commit()
        print("已重置：%s" % args.name)
        return 0
    if args.cmd == "one":
        from collector.bili import BiliClient

        with BiliClient(min_interval=1.0) as client:
            row = resolve(conn, client, args.name)
        print("%s → %s %s %s" % (row["name"], row["status"], row["uid"] or "-",
                                 row["uname"] or row["note"] or ""))
        return 0
    # pending
    rows = conn.execute("SELECT name FROM streamer_search WHERE status='pending'"
                        " ORDER BY updated_at LIMIT ?", (args.limit,)).fetchall()
    if not rows:
        print("没有待查的名字")
        return 0
    from collector.bili import BiliClient

    with BiliClient(min_interval=1.2) as client:
        for r in rows:
            out = resolve(conn, client, r["name"])
            print("%-16s → %-6s %s %s" % (out["name"], out["status"], out["uid"] or "-",
                                          out["uname"] or out["note"] or ""))
            time.sleep(0.6)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
