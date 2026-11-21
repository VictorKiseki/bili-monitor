#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把某一天的日程导出成 B 站动态可发布的内容：文字版 + 图片版（PSPLIVE 蓝白风）。

用法：
  python scripts/daily_post.py                 # 今天
  python scripts/daily_post.py --date 2026-11-04 --render
  python scripts/daily_post.py --payload       # 额外写发布载荷（含 at_uids）

只读本地库，不调用任何发布接口。
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import html
import json
import sqlite3
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import weekly as W  # noqa: E402

DB = ROOT / "data" / "bot.db"
AVATARS = ROOT / "data" / "avatars"
BRAND = ROOT / "data" / "brand"
OUT = ROOT / "out" / "daily"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
      "Referer": "https://www.bilibili.com/"}
CN_WEEK = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
EN_WEEK = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
BLUE = "#33B1FF"


def open_db(path: Path = DB) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def fetch_face(uid: int) -> str:
    """返回头像文件路径；缓存命中直接返回，失败返回空串。"""
    AVATARS.mkdir(parents=True, exist_ok=True)
    for ext in (".jpg", ".png", ".webp"):
        p = AVATARS / ("%s%s" % (uid, ext))
        if p.exists() and p.stat().st_size > 0:
            return str(p)
    url = "https://api.bilibili.com/x/web-interface/card?mid=%d&photo=false" % uid
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        face = ((data.get("data") or {}).get("card") or {}).get("face") or ""
        if face.startswith("//"):
            face = "https:" + face
        if face:
            req2 = urllib.request.Request(face, headers=UA)
            with urllib.request.urlopen(req2, timeout=20) as r2:
                raw = r2.read()
            p = AVATARS / ("%s.jpg" % uid)
            p.write_bytes(raw)
            return str(p)
    except Exception as exc:
        print("[头像] %s 失败：%s" % (uid, exc), file=sys.stderr)
    return ""


def split_names(entry: dict) -> list[tuple[int, str]]:
    """联动条目拆成 [(uid, 单个名字)]；普通条目就一个。"""
    uname = (entry.get("uname") or "").strip()
    names = [n.strip() for n in uname.split(" × ") if n.strip()]
    uids = [int(u) for u in (entry.get("uids") or [])]
    if not uids:
        uids = [int(entry.get("uid") or 0)]
    if len(uids) == len(names) and len(names) > 1:
        return list(zip(uids, names))
    return [(uids[0], uname)]


def build_text(day_label: str, entries: list[dict]) -> tuple[str, list[int]]:
    timed, rest = [], []
    at_uids: list[int] = []
    for e in entries:
        if e.get("cancelled"):
            continue
        people = split_names(e)
        names = " ".join("@" + n for _u, n in people)
        for u, _n in people:
            if u and u not in at_uids:
                at_uids.append(u)
        text = W.entry_body_text(e)
        lbl = W.entry_time_label(e)
        if lbl:
            timed.append("· %s %s%s" % (lbl, names, ("：" + text) if text else ""))
        else:
            rest.append(" ".join("@" + n for _u, n in people)
                        + ("：" + text if text else ""))
    body = ["%s 直播日程" % day_label, ""]
    body += timed
    if rest:
        body.append("")
        body.append("休息 / 待定：" + "、".join(rest))
    body += ["", "今天的直播安排如上，点进直播间就能看到～",
             "（日程由周表 / 单场预告自动汇总，可能临时调整）",
             "", "#PSPLIVE# #直播日程#"]
    return "\n".join(body), at_uids


def build_payload(text: str, entries: list[dict]) -> dict:
    """真 @ 的发布载荷：at_uids 给接口，at_control 记录 uid→@名字 的对应。"""
    at_uids, at_control = [], []
    seen = set()
    for e in entries:
        if e.get("cancelled"):
            continue
        for u, n in split_names(e):
            if u and u not in seen:
                seen.add(u)
                at_uids.append(u)
                at_control.append({"uid": u, "name": "@" + n})
    return {"text": text, "at_uids": at_uids, "at_control": at_control,
            "scene": "dynamic", "note": "发布时必须带 at_uids，正文里的 @名字 才会变真 @"}


def data_uri(path: str) -> str:
    ext = Path(path).suffix.lower()
    mime = "image/webp" if ext == ".webp" else ("image/png" if ext == ".png" else "image/jpeg")
    return "data:%s;base64,%s" % (mime, base64.b64encode(Path(path).read_bytes()).decode("ascii"))


def img_tag(path: str, size: int, cls: str = "av") -> str:
    if not path:
        return '<span class="%s ph"></span>' % cls
    ext = Path(path).suffix.lower()
    mime = "image/webp" if ext == ".webp" else ("image/png" if ext == ".png" else "image/jpeg")
    b64 = base64.b64encode(Path(path).read_bytes()).decode("ascii")
    return ('<img class="%s" style="width:%dpx;height:%dpx" src="data:%s;base64,%s">'
            % (cls, size, size, mime, b64))


def build_html(day_label: str, entries: list[dict], faces: dict[int, str],
               logo_path: str, badge_path: str, d: dt.date) -> str:
    rows = []
    for e in entries:
        if e.get("cancelled"):
            continue
        people = split_names(e)
        avs = "".join(
            '<span class="who">%s<span class="nm">%s</span></span>'
            % (img_tag(faces.get(u, ""), 66), html.escape(n)) for u, n in people)
        text = html.escape(W.entry_body_text(e))
        lbl = W.entry_time_label(e)
        tm = lbl or "待定"
        muted = lbl in ("", "休息", "待定")
        rows.append(
            '<div class="row%s%s">'
            '<div class="tm">%s</div>'
            '<div class="bd"><div class="avs">%s</div>'
            '<div class="tx">%s</div></div>'
            '</div>' % (" muted" if muted else "",
                        " collab" if len(people) > 1 else "",
                        html.escape(tm), avs, text))
    count = len([e for e in entries
                 if W.entry_time_label(e) not in ("", "休息", "待定")
                 and not e.get("cancelled")])
    tpl = """<!doctype html><html><head><meta charset="utf-8"><style>
*{box-sizing:border-box;margin:0;padding:0}
body{width:1080px;min-height:1440px;height:auto;position:relative;overflow:hidden;
  background:linear-gradient(180deg,#EAF5FF 0%,#F7FBFF 42%,#FFFFFF 100%);
  color:#0D2B4B;font-family:"Microsoft YaHei","PingFang SC",sans-serif;
  padding:52px 56px 40px;display:flex;flex-direction:column}
/* 背景装饰：右上角斜纹 + 淡蓝光斑 + 星星 */
body:before{content:"";position:absolute;right:-120px;top:-160px;width:640px;height:640px;
  background:radial-gradient(circle at 50% 50%, rgba(51,177,255,.20), rgba(51,177,255,0) 65%)}
body:after{content:"";position:absolute;left:-90px;bottom:-120px;width:420px;height:420px;
  border-radius:50%;background:radial-gradient(circle at 50% 50%, rgba(51,177,255,.14), rgba(51,177,255,0) 70%)}
.stripe{position:absolute;right:-40px;top:120px;width:260px;height:260px;opacity:.5;
  background:repeating-linear-gradient(135deg,#BFE4FF 0 10px,transparent 10px 26px);
  transform:rotate(8deg);border-radius:28px}
.hd{display:flex;align-items:flex-end;justify-content:space-between;position:relative;z-index:2}
.logo{height:86px;display:block}
.dt{text-align:right}
.dt .d1{font-size:78px;font-weight:900;line-height:.95;color:#0D2B4B;letter-spacing:-1px;
  font-family:Arial,sans-serif}
.dt .d1 b{color:@BLUE@}
.dt .d2{font-size:22px;font-weight:700;letter-spacing:4px;color:#5C86AE;margin-top:8px}
.rule{height:4px;margin:22px 0 0;border-radius:2px;position:relative;z-index:2;
  background:linear-gradient(90deg,@BLUE@ 0%,#8FD3FF 38%,rgba(143,211,255,0) 100%)}
.meta{display:flex;align-items:center;justify-content:space-between;
  margin-top:18px;position:relative;z-index:2}
.meta .t{font-size:24px;color:#4A688A;font-weight:600}
.meta .t em{font-style:normal;color:@BLUE@;font-weight:800}
.pill{background:@BLUE@;color:#fff;font-size:18px;font-weight:700;letter-spacing:2px;
  padding:8px 18px;border-radius:999px;display:flex;align-items:center;gap:8px}
.pill img{height:20px;display:block;filter:brightness(0) invert(1)}
.list{margin-top:28px;display:flex;flex-direction:column;gap:20px;position:relative;z-index:2}
.row{display:grid;grid-template-columns:118px 1fr;gap:16px;
  background:#fff;border:1px solid #DCEBFA;border-left:7px solid @BLUE@;
  border-radius:18px;padding:20px 24px;
  box-shadow:0 4px 14px rgba(51,177,255,.08)}
.row.muted{border-left-color:#BFD8EE;border-style:dashed;border-left-style:solid;
  background:#F7FBFF;box-shadow:none}
.tm{font-size:40px;font-weight:900;color:@BLUE@;line-height:1.1;
  font-variant-numeric:tabular-nums;padding-top:2px}
.row.muted .tm{font-size:24px;color:#93AEC7;font-weight:700;padding-top:12px}
.bd{display:flex;flex-direction:column;gap:6px;min-width:0}
.avs{display:flex;flex-wrap:wrap;gap:10px 22px;align-items:center}
.who{display:flex;align-items:center;gap:10px}
.av{width:66px;height:66px;border-radius:50%;object-fit:cover;background:#E6F2FD;
  border:3px solid #CDE7FF;box-shadow:0 2px 6px rgba(51,177,255,.18)}
.av.ph{display:inline-block}
.nm{font-size:27px;font-weight:800;color:#12365C}
.tx{font-size:22px;color:#4A6884;line-height:1.35}
.row.muted .nm{font-size:23px;color:#41637F;font-weight:700}
.row.muted .tx{font-size:20px;color:#7D97AE}
/* 联动场次：头像竖排，名字在下面 */
.row.collab .avs{gap:12px 26px}
.row.collab .who{flex-direction:column;gap:6px;align-items:center;width:104px}
.row.collab .av{width:54px;height:54px}
.row.collab .nm{font-size:16.5px;font-weight:700;text-align:center;line-height:1.25;color:#1B4573}
.foot{margin-top:auto;position:relative;z-index:2;display:flex;align-items:center;
  justify-content:space-between;border-top:2px solid #DCEBFA;padding-top:18px}
.foot .l{display:flex;align-items:center;gap:12px;color:#5C86AE;font-size:18px;font-weight:600}
.foot .l img{height:30px}
.foot .r{color:#8FA9C2;font-size:17px}
</style></head><body>
<div class="stripe"></div>
<div class="hd">
  <img class="logo" src="@@LOGO@@">
  <div class="dt"><div class="d1">@@MM@@<b>.</b>@@DD@@</div>
    <div class="d2">@@EN@@ · @@CN@@</div></div>
</div>
<div class="rule"></div>
<div class="meta">
  <div class="t">今日直播日程 · 共 <em>@@COUNT@@</em> 场</div>
  <div class="pill"><img src="@@BADGE@@">PSPLIVE</div>
</div>
<div class="list">@@ROWS@@</div>
<div class="foot">
  <div class="l"><img src="@@BADGE@@">PSPLIVE 直播日程 · 周表自动识别</div>
  <div class="r">时间以主播实际开播为准</div>
</div>
</body></html>"""
    return (tpl.replace("@@BLUE@@", BLUE)
               .replace("@@LOGO@@", data_uri(logo_path) if logo_path else "")
               .replace("@@BADGE@@", data_uri(badge_path) if badge_path else "")
               .replace("@@MM@@", "%02d" % d.month)
               .replace("@@DD@@", "%02d" % d.day)
               .replace("@@EN@@", EN_WEEK[d.weekday()])
               .replace("@@CN@@", CN_WEEK[d.weekday()])
               .replace("@@COUNT@@", str(count))
               .replace("@@ROWS@@", "".join(rows)))


def find_brand() -> tuple[str, str]:
    """蓝色横版字标 + 蓝色宝石徽标（都是官网素材，字标已按品牌蓝 #33B1FF 重着色）。"""
    logos = sorted(BRAND.glob("psplive-logo-blue.png")) + sorted(BRAND.glob("homelogo*.webp"))
    badge = sorted(BRAND.glob("clublogo*.webp"))
    return (str(logos[0]) if logos else "", str(badge[0]) if badge else "")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=dt.date.today().isoformat())
    ap.add_argument("--render", action="store_true")
    ap.add_argument("--payload", action="store_true")
    args = ap.parse_args()
    d = dt.date.fromisoformat(args.date)
    week_start = d - dt.timedelta(days=d.weekday())
    conn = open_db()
    view = W.week_view(conn, week_start.isoformat())
    day = view["days"][d.weekday()]
    entries = [e for e in day["entries"]]
    label = "%d月%d日 %s" % (d.month, d.day, CN_WEEK[d.weekday()])
    OUT.mkdir(parents=True, exist_ok=True)
    stem = OUT / d.isoformat()

    text, at_uids = build_text(label, entries)
    stem.with_suffix(".txt").write_text(text, encoding="utf-8")

    faces: dict[int, str] = {}
    for e in entries:
        for u, _n in split_names(e):
            if u and u not in faces:
                faces[u] = fetch_face(u)
    logo, badge = find_brand()
    stem.with_suffix(".html").write_text(
        build_html(label, entries, faces, logo, badge, d), encoding="utf-8")

    meta = {"date": d.isoformat(), "week_start": week_start.isoformat(),
            "entries": entries, "at_uids": at_uids, "text_chars": len(text)}
    if args.payload:
        meta["payload"] = build_payload(text, entries)
    stem.with_suffix(".json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    print(text)
    print("\n[写出] %s.txt / .html / .json" % stem)
    print("[@对象] %s" % ",".join(str(u) for u in at_uids))
    if args.render:
        # 2026-11-04 起改用 PIL 渲染：本机 msedge 静默失效（rc=0、不出文件），
        # 截图链路不可靠，见 scripts/card_pil.py 顶部说明。
        import card_pil
        png = card_pil.render(label, entries, faces, logo, badge, d, stem.with_suffix(".png"))
        print("[渲染] %s (%d KB)" % (png, png.stat().st_size // 1024))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
