#!/usr/bin/env python3
"""日程推送的告警/漏推检查（给 DSH 巡检会话当闸门用）。

推送本身由 scripts/schedule_worker.py 的 push_loop 跑（纯 Python，不经过模型）。
只有两种情况需要 Agent 介入：

  alert   —— push_loop 两次都失败，写了 data/push_alert.json
  missed  —— 今天已过推送时间，但 data/daily_push.last 还不是今天（日程进程组没在跑）

推送时间从 core.weekly.push 读，和实际推送循环同一口径：库里的
meta['daily_push_at'] 优先，其次 BBOT_PUSH_AT，最后 08:10。管理台改过时间后，
这里的漏推判断会立刻跟着新时间走，不会还按旧的 08:10 报「已漏推」。

    python scripts/push_alert.py check    # {"go": true/false, "reason": ...}
    python scripts/push_alert.py show     # 告警详情（含 stderr 尾巴）
    python scripts/push_alert.py clear    # 处理完清掉
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.weekly import push as P                     # noqa: E402

ALERT = ROOT / "data" / "push_alert.json"
MARKER = ROOT / "data" / "daily_push.last"


def load_alert() -> dict | None:
    try:
        return json.loads(ALERT.read_text(encoding="utf-8"))
    except Exception:                        # noqa: BLE001
        return None


def marker() -> str:
    try:
        return MARKER.read_text(encoding="utf-8").strip()
    except Exception:                        # noqa: BLE001
        return ""


def status(now: float | None = None, db_path: str | Path | None = None) -> dict:
    cur = time.time() if now is None else now
    today = time.strftime("%Y-%m-%d", time.localtime(cur))
    at = P.get_push_at(db_path=db_path or (ROOT / "data" / "bot.db"))
    alert, mark = load_alert(), marker()
    if alert:
        return {"go": True, "reason": "alert", "day": today,
                "alert": alert, "marker": mark, "push_at": at}
    if ALERT.exists():          # 文件在但解析不了：同样要人看一眼，别当没告警
        return {"go": True, "reason": "alert-broken", "day": today,
                "path": str(ALERT), "marker": mark, "push_at": at}
    if not at:
        return {"go": False, "reason": "off", "day": today, "marker": mark,
                "push_at": ""}
    try:
        due = P.today_due(at, cur)
    except ValueError:
        due = P.today_due(P.DEFAULT_PUSH_AT, cur)
        at = P.DEFAULT_PUSH_AT
    if mark != today:
        if cur < due:
            return {"go": False, "reason": "not-due", "day": today,
                    "due": time.strftime("%H:%M", time.localtime(due)),
                    "marker": mark, "push_at": at}
        return {"go": True, "reason": "missed", "day": today,
                "due": time.strftime("%H:%M", time.localtime(due)),
                "marker": mark, "push_at": at}
    return {"go": False, "reason": "ok", "day": today, "marker": mark,
            "push_at": at}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["check", "status", "show", "clear"])
    args = ap.parse_args()
    if args.cmd in ("check", "status"):
        print(json.dumps(status(), ensure_ascii=False))
        return 0
    if args.cmd == "show":
        s = status()
        s["detail"] = (load_alert() or {}).get("detail") or ""
        print(json.dumps(s, ensure_ascii=False, indent=1))
        return 0
    try:
        ALERT.unlink(missing_ok=True)
        print(json.dumps({"cleared": True, "path": str(ALERT)}, ensure_ascii=False))
    except Exception as e:                   # noqa: BLE001
        print(json.dumps({"cleared": False, "error": f"{type(e).__name__}: {e}"},
                         ensure_ascii=False))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
