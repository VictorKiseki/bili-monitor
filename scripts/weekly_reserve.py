"""直播预约 → 日程：抓最近的动态，把挂在上面的「直播预约」模块落成单场日程。

背景（docs/21 §27）：主播的预告有两种发布方式 —— 发动态，或在直播间/动态里挂
「直播预约」。预约模块不单独成一条动态，而是作为 `additional` 挂在普通图文动态上：

    modules.module_dynamic.additional = {
      "type": "ADDITIONAL_TYPE_RESERVE",
      "reserve": {"title": "直播预约：影之刃驴",
                  "desc1": {"text": "10-29 20:00 直播"},
                  "desc2": {"text": "10人预约"},
                  "rid": 5846955, "reserve_total": 10, "stype": 2,
                  "button": {"uncheck": {"text": "已结束", "disable": 1}}}}

所以「预约」的内容（时间 + 节目名）直接落在动态里，不需要视觉模型，也不需要
观众权限：只要按 uid 拉最近动态就能拿到。

用法：
    python scripts/weekly_reserve.py                    # 扫全部主播最近 3 天动态
    python scripts/weekly_reserve.py --days 7           # 放宽窗口
    python scripts/weekly_reserve.py --dry-run          # 只看会写什么
    python scripts/weekly_reserve.py --json out.json    # 落结果，便于自动任务复核
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

from collector.bili import BiliClient  # noqa: E402
from core import weekly  # noqa: E402
from store.sqlite import SCHEMA  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/bot.db")
    ap.add_argument("--days", type=float, default=3, help="只看最近 N 天的动态")
    ap.add_argument("--limit", type=int, default=0, help="最多处理几个主播（0=全部）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(SCHEMA)

    payload = weekly.refresh_reserves(
        BiliClient(), conn, days=args.days, limit=args.limit, dry_run=args.dry_run)
    print(json.dumps(payload, ensure_ascii=False, indent=1))
    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
