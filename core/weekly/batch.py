"""候选批次组装（2026-11-08）。

把「扫候选 → 配节目提示 → 附预约卡 → 附 OCR 原文 → 附已排场次上下文」从
scripts/weekly_ai.py 搬进 core，让所有消费者共用同一份素材：

* core/weekly/agent.py    → 写成 Harness 任务包（out/ai_tasks/），由 DSH 会话读图
* scripts/weekly_ai.py show → 把单条候选摊开给人工复核

搬家的原因是 core 不能 import scripts（会变成反向依赖），而 agent 又要复用
RECHECK_HINT / TIME_HINT 这些提示词原文——两份副本迟早写歪。
"""
from __future__ import annotations

import datetime as dt
import json
import re
import time

from core import weekly

#: 汉字 / 全角标点区间（OCR 空格清理用）
_CJK = r"[\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uff00-\uffef]"
_CJK_GAP = re.compile(r"(?<=%s)[ \t]+(?=%s)" % (_CJK, _CJK))

RECHECK_HINT = (
    "二次判读（2026-11-06 起强制）：先按图/正文初判 week / single / none；"
    "凡是初判为 week 或 single 的，必须再读一遍整段动态（标题+正文+图+reserve）并结合 ctx "
    "（该主播这个窗口里已经排进日程的场次）重新判断一次「这到底是不是预告」，"
    "结论写进结果的 recheck 字段：confirmed=确认是预告；downgraded=其实是闲聊/回顾，改判 none；"
    "unsure=拿不准（会进人工复核）。"
    "典型「不是预告」：播完的感想与感谢（今天玩得好开心、谢谢大家）、收尾道别（明天见、下次见）、"
    "只泛泛说「明天也播」却没有时间和节目、回应弹幕或吐槽自己的行程。"
    "典型「是预告」：给出新的时间/节目、改档、临时加播、请假/取消、开播前的倒计时。"
    "例：东爱璃Lovely 2026-11-05 01:50「今天也是玩得好开心的一天…明天也是大直播…明天见！」"
    "→ 播后回顾+道别，没有时间和节目，recheck=downgraded、is_schedule=false。"
)

TIME_HINT = (
    "时间口径（2026-11-09）：能看清具体几点就写 HH:MM（time 字段，24 小时制）。"
    "中文数字要换算（晚上十点 → 22:00，十一点半 → 11:30）；区间写开始时间"
    "（「9~12」「9点到12点」→ 09:00，「晚上9~12」→ 21:00）。"
    "只写了模糊时段（上午/中午/下午/傍晚/晚上/深夜/凌晨）而没给点数时：time 留空、"
    "time_text 填原文，日程表显示「待定」，原文会并进正文。"
    "同一个时间给了两三种可能（「晚上十点或十一点」）也算待定：time 留空、time_text 填原文。"
    "整条就是休息：time 留空、time_text=\"休息\"。"
)


def tight(s: str) -> str:
    """Windows OCR 会在每个汉字之间塞一个空格，去掉以免白占 token。"""
    return _CJK_GAP.sub("", str(s or ""))


def attach_ocr(conn, item: dict) -> str:
    """给一条候选附上 OCR 原文（失败/读不出就什么都不附，不影响判图）。"""
    imgs = item.get("images") or []
    path = (imgs[0].get("path") if imgs else None) or ""
    if not path:
        return ""
    try:
        from scripts.weekly_ocr import ensure
        res = ensure(conn, str(item.get("event_id") or ""), path)
        txt = (res.get("text") or "").strip()
        if not txt:
            return ""
        txt = tight(txt)
        item["ocr_text"] = txt[:800]
        item["ocr_note"] = ("Windows OCR 原文，可能很乱（手写体基本读不出）。"
                            "它只作参考：以图片为准，OCR 与图上明显不符时不要照抄。")
        return txt
    except Exception:                            # noqa: BLE001
        return ""


def attach_reserve(conn, item: dict) -> str:
    """候选挂着「直播预约」卡时，把卡上的节目/时间交给判图模型一起看。

    预约卡是平台按开播计划生成的，时间比正文可靠；正文/图片负责补节目内容。
    """
    eid = str(item.get("event_id") or "")
    if not eid:
        return ""
    try:
        row = conn.execute("SELECT extra FROM event WHERE event_id=?", (eid,)).fetchone()
        ex = json.loads(((row[0] if row else "") or "{}"))
        rv = ex.get("reserve") or {}
        if not rv or int(rv.get("stype") or 0) == 1:
            return ""
        card = weekly.parse_reserve(rv, float(item.get("posted_at") or time.time())) or {}
        item["reserve"] = {"title": str(rv.get("title") or ""),
                           "time_text": str(rv.get("time_text") or ""),
                           "time": card.get("time") or "",
                           "date": card.get("date") or "",
                           "count_text": str(rv.get("count_text") or ""),
                           "ended": bool(rv.get("ended"))}
        item["reserve_note"] = ("这条动态挂着「直播预约」卡（上面 reserve 是卡上的节目/时间）。"
                                "它已经说明是日程类：卡上有开播时间就按 single 处理，"
                                "图片和正文只用来补节目内容、联动对象；卡片按钮若已「已结束」，"
                                "只在 notes 里注明，不判取消。")
        return item["reserve"]["time_text"] or item["reserve"]["time"]
    except Exception:                            # noqa: BLE001
        return ""


def attach_context(conn, item: dict) -> None:
    """给候选配一份「该主播这几天已经排了什么」的上下文。

    判读模型常把「今天播得开心/明天见」这种收尾动态当成明日预告；
    把已排场次摆在旁边，二次判读时就能看出内容是否只是对已排/已播场次的复述。
    """
    uid = int(item.get("uid") or 0)
    if not uid:
        return
    posted = float(item.get("posted_at") or time.time())
    base = dt.date.fromtimestamp(posted)
    lo = (base - dt.timedelta(days=2)).isoformat()
    hi = (base + dt.timedelta(days=9)).isoformat()
    sessions = []
    for r in conn.execute(
            "SELECT date, time, time_text, text, kind, event_id FROM weekly_session"
            " WHERE uid=? AND date BETWEEN ? AND ? ORDER BY date, time",
            (uid, lo, hi)).fetchall():
        sessions.append({"date": r["date"], "time": r["time"] or "",
                         "time_text": r["time_text"] or "", "text": r["text"] or "",
                         "kind": r["kind"] or "", "from": r["event_id"] or ""})
    weeks = []
    try:
        cur = dt.date.fromisoformat(weekly.monday_of_iso(base.isoformat()))
        starts = [cur.isoformat(), (cur + dt.timedelta(days=7)).isoformat()]
        for ws in starts:
            row = conn.execute("SELECT week_label, days_json FROM weekly_schedule"
                               " WHERE uid=? AND week_start=?", (uid, ws)).fetchone()
            if not row:
                continue
            days = json.loads(row["days_json"] or "[]")
            entries = []
            for d in days:
                for e in (d.get("entries") or []):
                    entries.append({"day": d.get("day"), "time": e.get("time") or "",
                                    "time_text": e.get("time_text") or "",
                                    "text": e.get("text") or ""})
            weeks.append({"week_start": ws, "label": row["week_label"] or "",
                          "entries": entries})
    except Exception:                            # noqa: BLE001
        pass
    item["ctx"] = {"posted_date": base.isoformat(), "window": lo + "~" + hi,
                   "sessions": sessions[:24], "weeks": weeks}
    item["ctx_note"] = ("ctx 是该主播这几天已经排进日程的场次（sessions=单场/预约，"
                        "weeks=周表）。二次判读时用它对照：若这条动态只是复述/感谢/道别"
                        "已经排过或已经播过的场次，就不是新预告。")


def load_programs(conn) -> list[dict]:
    """常驻联动节目 + uid→主播名（判图时看到节目名就知道常驻主持是谁）。"""
    return weekly.load_programs(conn)


def subject_names(conn) -> dict:
    """uid → 主播名。注意 subject 表的列名是 name，不是 uname。"""
    return {r["uid"]: r["name"] for r in conn.execute(
        "SELECT uid, name FROM subject").fetchall()}


def build(conn, limit: int = 12, ocr_max: int = 12, download: bool = True,
          since_days: int = 3) -> dict:
    """扫最近几天的动态补候选，再把一批候选组装成可直接喂模型的 items。

    返回 {"items", "added", "ocr", "programs", "subs"}；items 里每条都带
    text/title/images/program_hint/reserve/ocr_text/ctx 等字段。
    """
    since = int(time.time() - max(1, int(since_days)) * 86400)
    added = weekly.scan(conn, since=since, limit=200, download=bool(download))
    batch = weekly.pending_batch(conn, limit=int(limit))
    programs = load_programs(conn)
    subs = subject_names(conn)
    for item in batch:
        p = weekly.match_program(item.get("text") or "", programs)
        if p:
            item["program_hint"] = {"program_id": p["program_id"], "name": p["name"],
                                    "hosts": [{"uid": u, "uname": subs.get(u) or ("UID%s" % u)}
                                              for u in p["hosts"]],
                                    "note": p.get("note") or ""}
    for item in batch:
        attach_reserve(conn, item)
    ocr_n = 0
    for item in batch:
        if ocr_n >= max(0, int(ocr_max)):
            break
        if attach_ocr(conn, item):
            ocr_n += 1
    for item in batch:
        attach_context(conn, item)
    conn.commit()
    return {"items": batch, "added": len(added), "ocr": ocr_n,
            "programs": weekly.program_public(programs, subs), "subs": subs}
