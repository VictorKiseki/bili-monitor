"""长跑体检：一条命令看清这次运行跑得怎么样

用法：
  python scripts/run_report.py                 # 读 data/bot.db + data/supervisor.json
  python scripts/run_report.py --logs 40       # 顺带打印最近 40 行日志

输出：运行时长、总开关档位、名单/在播、事件分类计数、投递状态、死信明细、日志里的失败行。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def fmt_dur(sec: float) -> str:
    sec = int(max(0, sec))
    h, m = divmod(sec // 60, 60)
    return ("%d 小时 " % h if h else "") + "%d 分钟" % m


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def q(conn, sql, args=()):
    return list(conn.execute(sql, args).fetchall())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "data" / "bot.db"))
    ap.add_argument("--state", default=str(ROOT / "data" / "supervisor.json"))
    ap.add_argument("--logs", type=int, default=0)
    ap.add_argument("--log-file", default=str(ROOT / "logs" / "bot.log"))
    a = ap.parse_args()

    db = Path(a.db)
    if not db.exists():
        print("找不到数据库：%s" % db)
        return 2
    conn = sqlite3.connect("file:%s?mode=ro" % db.as_posix(), uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    now = int(time.time())

    st = load_state(Path(a.state))
    started = st.get("child_started_at") or st.get("started_at")
    print("=== 运行 ===")
    if started:
        print("守护 pid=%s  子进程 pid=%s  已跑 %s  重启 %s 次  最近动作 %s"
              % (st.get("pid"), st.get("child_pid"), fmt_dur(now - started),
                 st.get("restarts"), st.get("last_event")))
    else:
        print("没找到守护状态文件（可能是前台手工跑的）")

    meta = {r["k"]: r["v"] for r in q(conn, "SELECT k, v FROM meta")}
    mode = meta.get("global_mode", "full")
    labels = {"full": "全开", "collect": "只采集", "off": "全停"}
    last_tick = int(meta.get("last_tick") or 0)
    print("总开关 %s（%s）  最后心跳 %s 前"
          % (labels.get(mode, mode), mode,
             fmt_dur(now - last_tick) if last_tick else "从未"))

    print()
    print("=== 名单 ===")
    subs = q(conn, "SELECT COUNT(*) c, SUM(enabled) e FROM subject")[0]
    living = q(conn, "SELECT COUNT(*) c FROM subject_state WHERE live_status=1")[0]["c"]
    print("主播 %d（启用 %d）  在播 %d" % (subs["c"], subs["e"] or 0, living))

    print()
    print("=== 事件 ===")
    ev = q(conn, "SELECT COUNT(*) c FROM event")[0]["c"]
    print("共 %d 条" % ev)
    for r in q(conn, "SELECT type, COUNT(*) c FROM event GROUP BY type ORDER BY c DESC"):
        print("  %-12s %d" % (r["type"], r["c"]))

    print()
    print("=== 投递 ===")
    tot = 0
    for r in q(conn, "SELECT status, COUNT(*) c FROM delivery GROUP BY status"):
        print("  %-10s %d" % (r["status"], r["c"]))
        tot += r["c"]
    print("  合计 %d" % tot)
    bad = q(conn, "SELECT delivery_id, event_id, attempts, last_error FROM delivery "
                  "WHERE status='failed' ORDER BY delivery_id DESC LIMIT 10")
    if bad:
        print("  最近失败：")
        for r in bad:
            print("    #%s %s attempts=%s  %s" % (r["delivery_id"], r["event_id"],
                                                  r["attempts"], (r["last_error"] or "")[:80]))

    print()
    print("=== 数据量 ===")
    for t in ("event", "delivery", "seen_dynamic", "subject_state"):
        n = q(conn, "SELECT COUNT(*) c FROM %s" % t)[0]["c"]
        print("  %-14s %d" % (t, n))
    print("  库文件 %.1f KB" % (db.stat().st_size / 1024))

    if a.logs:
        print()
        print("=== 日志最后 %d 行 ===" % a.logs)
        try:
            lines = Path(a.log_file).read_text(encoding="utf-8", errors="replace").splitlines()
            for ln in lines[-a.logs:]:
                print("  " + ln)
            warns = [ln for ln in lines if ("失败" in ln or "ERROR" in ln or "弃" in ln)]
            print()
            print("  疑似失败行 %d 条" % len(warns))
        except FileNotFoundError:
            print("  没有日志文件：%s" % a.log_file)
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
