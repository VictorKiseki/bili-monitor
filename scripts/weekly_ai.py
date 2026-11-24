#!/usr/bin/env python3
"""日程识别的「跑不跑」闸门与批次导出（2026-11-03 / 2026-11-08 瘦身为壳）。

2026-11-08 起真正干活的模块搬进了 core：

* core.weekly.batch  —— 扫候选、配节目提示、附预约卡 / OCR / 已排场次上下文；
* core.weekly.agent  —— 开关/时段/排队闸门 + Harness 任务导出与结果回收。

本脚本保留两种用法：

1. 命令行（闸门与人工复核；导出/回收走 scripts/weekly_agent.py）：

    python scripts/weekly_ai.py gate            # 打印 JSON：{"go": true/false, "reason": ...}
    python scripts/weekly_ai.py status          # 配置 + 队列状态（管理台用）
    python scripts/weekly_ai.py show <event_id> # 摊开一条候选的素材（人工复核用）
    python scripts/weekly_ai.py set --enabled 0 --limit 8 --slots 08:00,13:00,19:00,00:00
    python scripts/weekly_ai.py request         # 管理台「立刻执行」写的排队标记

2. 老朋友：scripts/selftest_weekly.py 用 importlib 加载本模块后调 open_db / gate，
   所以这些名字必须留在模块级（薄转发不算负担）。
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
from core.weekly import agent as agentmod  # noqa: E402
from core.weekly import batch as batchmod  # noqa: E402

#: 与 core.weekly.agent 共用（管理台、常驻循环、本脚本读的是同一组 meta 键）
DEFAULTS = dict(agentmod.DEFAULTS, **agentmod.AGENT_KEYS)
RECHECK_HINT = batchmod.RECHECK_HINT
TIME_HINT = batchmod.TIME_HINT


def open_db(path: str) -> sqlite3.Connection:
    """老入口：建/升级库并补齐默认 meta（转发 core.weekly.agent.open_conn）。"""
    return agentmod.open_conn(path)


def cfg(conn: sqlite3.Connection) -> dict:
    return agentmod.cfg(conn)


def pending_count(conn: sqlite3.Connection) -> int:
    """待判候选数。口径统一走 core.weekly：日程表监控关掉的主播不计入。"""
    return agentmod.pending_count(conn)


def gate(conn: sqlite3.Connection) -> dict:
    return agentmod.gate(conn)


def _show(conn, event_id: str) -> dict:
    """把一条候选的完整素材摊开（人工复核或外部会话判读拿不准时用）。"""
    r = conn.execute(
        "SELECT c.*, e.title AS title FROM weekly_candidate c"
        " LEFT JOIN event e ON e.event_id=c.event_id WHERE c.event_id=?",
        (event_id,)).fetchone()
    if not r:
        return {"ok": False, "error": "候选不存在"}
    item = {"event_id": r["event_id"], "uid": r["uid"], "uname": r["uname"],
            "dyn_id": r["dyn_id"], "url": r["url"], "posted_at": r["pub_ts"],
            "text": r["text"] or "", "title": (r["title"] or "") if "title" in r.keys() else "",
            "images": json.loads(r["images_json"] or "[]"),
            "status": r["status"], "human_scope": r["human_scope"]}
    batchmod.attach_reserve(conn, item)
    batchmod.attach_ocr(conn, item)
    batchmod.attach_context(conn, item)
    item["note"] = "这条的现状：" + str(r["status"] or "") + " " + (r["note"] or "")
    return {"ok": True, "item": item}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/bot.db")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("gate")
    sub.add_parser("status")
    sub.add_parser("request")
    p_show = sub.add_parser("show")
    p_show.add_argument("event_id")
    p_done = sub.add_parser("done")
    p_done.add_argument("--result", default="")
    p_set = sub.add_parser("set")
    p_set.add_argument("--enabled", default=None)
    p_set.add_argument("--limit", type=int, default=None)
    p_set.add_argument("--slots", default=None)
    args = ap.parse_args()

    conn = open_db(args.db)
    c = cfg(conn)

    if args.cmd == "gate":
        print(json.dumps(gate(conn), ensure_ascii=False))
        return 0

    if args.cmd == "status":
        print(json.dumps({"ok": True, "config": {k: c[k] for k in
                                                 ("enabled", "limit", "slots", "last_run",
                                                  "last_result", "request_at")},
                          "pending": pending_count(conn), "now": int(time.time())},
                         ensure_ascii=False))
        return 0

    if args.cmd == "show":
        print(json.dumps(_show(conn, args.event_id), ensure_ascii=False, indent=1))
        return 0

    if args.cmd == "request":
        now = time.time()
        conn.execute("UPDATE meta SET v=? WHERE k='weekly_ai_request_at'", (str(now),))
        conn.commit()
        print(json.dumps({"ok": True, "request_at": now}, ensure_ascii=False))
        return 0

    if args.cmd == "set":
        updates = {}
        if args.enabled is not None:
            updates["weekly_ai_enabled"] = "1" if str(args.enabled) in ("1", "true", "on") else "0"
        if args.limit is not None:
            updates["weekly_ai_limit"] = str(max(1, min(int(args.limit), 60)))
        if args.slots is not None:
            slots = [s.strip() for s in args.slots.split(",") if s.strip()]
            normalized = []
            for s in slots:
                hh, _, mm = s.partition(":")
                if not (hh.isdigit() and mm.isdigit() and 0 <= int(hh) <= 24 and 0 <= int(mm) <= 59):
                    print(json.dumps({"ok": False, "error": "时间格式要 HH:MM"}, ensure_ascii=False))
                    return 2
                normalized.append("%02d:%02d" % (int(hh) % 24, int(mm)))
            updates.update(agentmod.slot_updates(conn, normalized))
        for k, v in updates.items():
            conn.execute("UPDATE meta SET v=? WHERE k=?", (v, k))
        conn.commit()
        c2 = cfg(conn)
        print(json.dumps({"ok": True, "config": {k: c2[k] for k in
                                                 ("enabled", "limit", "slots")}}, ensure_ascii=False))
        return 0

    if args.cmd == "done":
        now = time.time()
        conn.execute("UPDATE meta SET v=? WHERE k='weekly_ai_last_run'", (str(now),))
        conn.execute("UPDATE meta SET v=? WHERE k='weekly_ai_last_result'", (args.result,))
        conn.execute("UPDATE meta SET v=? WHERE k='weekly_ai_request_at'", ("0",))
        conn.commit()
        print(json.dumps({"ok": True, "last_run": now, "result": args.result}, ensure_ascii=False))
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
