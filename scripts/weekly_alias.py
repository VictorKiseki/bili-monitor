#!/usr/bin/env python3
"""主播昵称（联动识别用，2026-11-04）。

自动拆名能覆盖「礼墨Sumi → 礼墨 / Sumi」「奈奈莉娅Channel → 奈奈莉娅」，
但「鸽 / 狍 / 礼小龙」这类外号拆不出来，人工登记到这里，日程里点到就算联动。

    python scripts/weekly_alias.py list
    python scripts/weekly_alias.py add --uid 3821157 --alias 狍 --note "东爱璃外号"
    python scripts/weekly_alias.py remove --uid 3821157 --alias 狍
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


def main() -> int:
    ap = argparse.ArgumentParser(description="主播昵称登记（联动识别）")
    ap.add_argument("--db", default=str(ROOT / "data" / "bot.db"))
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="列出所有昵称")
    ap_add = sub.add_parser("add", help="新增昵称")
    ap_add.add_argument("--uid", type=int, required=True)
    ap_add.add_argument("--alias", required=True)
    ap_add.add_argument("--note", default="")
    ap_rm = sub.add_parser("remove", help="删除昵称")
    ap_rm.add_argument("--uid", type=int, required=True)
    ap_rm.add_argument("--alias", required=True)
    args = ap.parse_args()

    conn = open_db(args.db)
    if args.cmd == "list":
        rows = conn.execute(
            "SELECT a.uid, a.alias, a.note, COALESCE(s.name, '') AS name"
            " FROM streamer_alias a LEFT JOIN subject s ON s.uid=a.uid"
            " ORDER BY a.uid, a.alias").fetchall()
        for r in rows:
            print("%s\t%s\t%s\t%s" % (r["uid"], r["name"] or "-", r["alias"], r["note"] or ""))
        print("共 %d 条" % len(rows))
        return 0
    if args.cmd == "add":
        conn.execute("INSERT OR REPLACE INTO streamer_alias(uid, alias, note, created_at)"
                     " VALUES(?, ?, ?, ?)", (args.uid, args.alias.strip(), args.note, time.time()))
        conn.commit()
        print("已登记：%s → %s" % (args.uid, args.alias.strip()))
        return 0
    cur = conn.execute("DELETE FROM streamer_alias WHERE uid=? AND alias=?",
                       (args.uid, args.alias.strip()))
    conn.commit()
    print("已删除 %d 条" % cur.rowcount)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
