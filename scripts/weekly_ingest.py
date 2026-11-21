"""L3：把视觉回合的判定结果写进 weekly_schedule（只记录，不推送）。

输入（DSH Agent 产出）：
    {"results":[
      {"event_id":"...", "is_schedule":true, "week_label":"11.2~11.8",
       "week_start":"2026-11-02", "confidence":0.92,
       "days":[{"day":"MON","entries":[{"time":"9:30","text":"影之刃"}]}],
       "notes":"", "extractor":"vlm"}
    ]}

用法：
    python scripts/weekly_ingest.py data/schedules/inbox/results.json
    python scripts/weekly_ingest.py data/schedules/inbox/results.json --dry-run
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import weekly  # noqa: E402
from store.sqlite import SCHEMA, migrate  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("results", help="视觉回合产出的 JSON 文件")
    ap.add_argument("--db", default="data/bot.db")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    raw = json.loads(Path(args.results).read_text("utf-8"))
    results = raw.get("results") if isinstance(raw, dict) else raw
    if not isinstance(results, list):
        print("输入格式不对：需要 list 或 {results: [...]}")
        return 2

    conn = sqlite3.connect(args.db, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(SCHEMA)
    migrate(conn)                  # 老库补列（ocr_signal/ocr_text 等）
    summary = weekly.ingest(conn, results, dry_run=args.dry_run)
    # 判定完的候选图没人再看了：落库后立刻清，dry-run 只报数不删
    try:
        summary["cache"] = weekly.purge_cache(conn, dry_run=args.dry_run)
    except Exception as e:                      # noqa: BLE001
        summary["cache"] = {"error": str(e)}
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0 if summary["error"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
