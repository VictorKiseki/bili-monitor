# -*- coding: utf-8 -*-
"""把当日日程（PSPLIVE 蓝白图片版）推送到 QQ 官方机器人。

  python scripts/daily_push.py                  # 出图并推送今天
  python scripts/daily_push.py --date 2026-11-05
  python scripts/daily_push.py --no-send        # 只生成，不推送

图片用 scripts/card_pil.py 的 PIL 渲染（不依赖浏览器：本机 msedge 静默失效）。
日程数据只读，本脚本不写库。
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import card_pil  # noqa: E402
import daily_post as DP  # noqa: E402
from channels.official_qq import OfficialQQChannel  # noqa: E402
from shared.contracts import ChannelType, RenderedMessage, Target, TargetKind  # noqa: E402

DB = ROOT / "data" / "bot.db"
PUSH_MARKER = ROOT / "data" / "daily_push.last"


def env(key: str, fname: str = "BBOT.env") -> str:
    p = ROOT / fname
    if not p.exists():
        return ""
    for ln in p.read_text("utf-8").splitlines():
        ln = ln.strip()
        if ln.startswith(key + "="):
            return ln.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def build_card(d: dt.date) -> tuple[Path | None, dict]:
    """生成 txt/html/json/png；没有排期时不出图。"""
    week_start = d - dt.timedelta(days=d.weekday())
    view = DP.W.week_view(DP.open_db(), week_start.isoformat())
    entries = list(view["days"][d.weekday()]["entries"])
    label = "%d月%d日 %s" % (d.month, d.day, DP.CN_WEEK[d.weekday()])
    DP.OUT.mkdir(parents=True, exist_ok=True)
    stem = DP.OUT / d.isoformat()
    text, at_uids = DP.build_text(label, entries)
    stem.with_suffix(".txt").write_text(text, encoding="utf-8")
    faces: dict[int, str] = {}
    for e in entries:
        for u, _n in DP.split_names(e):
            if u and u not in faces:
                faces[u] = DP.fetch_face(u)
    logo, badge = DP.find_brand()
    stem.with_suffix(".html").write_text(
        DP.build_html(label, entries, faces, logo, badge, d), encoding="utf-8")
    png: Path | None = None
    if entries:
        png = card_pil.render(label, entries, faces, logo, badge, d, stem.with_suffix(".png"))
    timed = len([e for e in entries if e.get("time") and not e.get("cancelled")])
    meta = {"date": d.isoformat(), "week_start": week_start.isoformat(),
            "label": label, "count": len(entries), "timed": timed,
            "text": text, "at_uids": at_uids,
            "entries": entries, "png": str(png) if png else ""}
    return png, meta


def caption(meta: dict) -> str:
    head = "📺 PSPLIVE 直播日程 · %s" % meta["label"]
    if not meta.get("count"):
        return head + "\n今天还没有识别到排期，周表发出来后会更新～"
    if not meta.get("timed"):
        return head + "\n今天全是休息 / 待定，好好休息～"
    return head + "\n共 %d 场 · 完整安排见图，时间以主播实际开播为准" % meta["timed"]


def pick_target(target_id: int | None = None) -> Target:
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    sql = "SELECT * FROM target WHERE enabled=1"
    args: tuple = ()
    if target_id:
        sql += " AND target_id=?"
        args = (target_id,)
    rows = conn.execute(sql, args).fetchall()
    if not rows:
        raise SystemExit("[FAIL] 库里没有可用的推送目标（target 表）")
    r = rows[0]
    return Target(target_id=int(r["target_id"]),
                  channel_type=ChannelType(r["channel_type"]),
                  kind=TargetKind(r["kind"]), external_id=r["external_id"],
                  display_name=r["display_name"] or "", enabled=True)


async def push(target: Target, png: Path | None, text: str) -> int:
    app_id, secret = env("QQBOT_APP_ID"), env("QQBOT_APP_SECRET")
    if not app_id or not secret:
        print("[FAIL] BBOT.env 里没有 QQBOT_APP_ID / QQBOT_APP_SECRET")
        return 2
    ch = OfficialQQChannel(app_id, secret)
    try:
        msg = RenderedMessage(text=text, image_path=str(png) if png else None)
        res = await ch.send(target, msg)
        print("[推送] target=%s kind=%s status=%s code=%s msg=%s"
              % (target.display_name, target.kind.value, res.status.value,
                 res.error_code, (res.error_msg or "")[:120]))
        return 0 if res.status.value == "ok" else 3
    finally:
        await ch.close()


def _mark_pushed(d: dt.date) -> None:
    """记下「哪一天已经推过」，run_bot.py 的推送循环靠它判断开机后要不要补推。"""
    try:
        PUSH_MARKER.parent.mkdir(parents=True, exist_ok=True)
        PUSH_MARKER.write_text(d.isoformat(), encoding="utf-8")
    except Exception:                            # noqa: BLE001
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=dt.date.today().isoformat())
    ap.add_argument("--target-id", type=int, default=None)
    ap.add_argument("--no-send", action="store_true")
    args = ap.parse_args()

    d = dt.date.fromisoformat(args.date)
    png, meta = build_card(d)
    text = caption(meta)
    print("[生成] %s 共 %d 场 图=%s" % (meta["label"], meta["count"],
                                        ("%d KB" % (png.stat().st_size // 1024)) if png else "无"))
    print("[文案] " + text.replace("\n", " / "))
    if args.no_send:
        return 0
    rc = asyncio.run(push(pick_target(args.target_id), png, text))
    if rc == 0:
        _mark_pushed(d)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())