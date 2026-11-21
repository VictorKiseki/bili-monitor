"""临时日程解析：从文字/图片里读日期与时间，合并成 reserve，再变会话。"""

from __future__ import annotations

import datetime as dt
import re
import sqlite3
import time

from .parse import _cjk_tight, _join_note, _now_ts, monday_of_iso, normalize_day, normalize_time, same_show

from .fetch import SCHEDULE_DIR
from .store import weekly_off_uids

_RESERVE_TITLE_RE = re.compile(r"^\s*(?:直播)?预约\s*[：:]\s*")

_RESERVE_TIME_RE = re.compile(
    r"(\d{1,2})\s*[-./月]\s*(\d{1,2})\s*[日号]?\s*(\d{1,2})\s*[:：]\s*(\d{2})")

_TIME_ONLY_RE = re.compile(r"(\d{1,2})\s*[:：]\s*(\d{2})")

_WEEKDAY_TXT_RE = re.compile(r"(?:周|星期|礼拜)([一二三四五六日天])")

# 预约卡的时间有三种写法（2026-11-05 实测）：
#   「10-30 19:30 直播」「今天 20:00 直播」「预计10-31 17:30发布（视频预约）」
_REL_DAY = (("大后天", 3), ("后天", 2), ("明天", 1), ("明日", 1), ("明晚", 1),
            ("今天", 0), ("今日", 0), ("今晚", 0))

# 绝对日期：标题/正文/预约卡里「11月6日」「11.6」「11-06」都能认。
# 点/横线写法跟时间（9.30、19.30）长得像，只接受离基准 10 天内的，避免把「9.30」当成 9 月 30 日。
_MONTH_DAY_RE = re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?")

_DOT_DATE_RE = re.compile(r"(?<!\d)(\d{1,2})\s*[.．]\s*(\d{1,2})(?!\d)")

_DASH_DATE_RE = re.compile(r"(?<!\d)(\d{1,2})\s*[-/]\s*(\d{1,2})(?!\d)")

def absolute_date_hint(text: str, base: dt.date) -> dt.date | None:
    """只认写死的日期（月日），不认「今天/周五」。"""
    t = _cjk_tight(str(text or ""))
    if not t:
        return None
    for rx, near in ((_MONTH_DAY_RE, 120), (_DOT_DATE_RE, 10), (_DASH_DATE_RE, 10)):
        for m in rx.finditer(t):
            mo, da = int(m.group(1)), int(m.group(2))
            if not (1 <= mo <= 12 and 1 <= da <= 31):
                continue
            cand = []
            for y in (base.year, base.year + 1, base.year - 1):
                try:
                    d = dt.date(y, mo, da)
                except ValueError:
                    continue
                if abs((d - base).days) <= near:
                    cand.append(d)
            if cand:
                return min(cand, key=lambda d: abs((d - base).days))
    return None

def text_date_hint(text: str, base_ts: float) -> dict | None:
    """从一段文字里读日期：先找绝对日期，再找「今天/明天/周几」这类相对日。

    base_ts 是相对日的基准：标题/正文用发帖时间，预约卡用当前时间。
    """
    t = _cjk_tight(str(text or ""))
    if not t:
        return None
    base = dt.date.fromtimestamp(float(base_ts or time.time()))
    absd = absolute_date_hint(t, base)
    if absd:
        return {"date": absd.isoformat(), "date_kind": "abs"}
    off = None
    for kw, delta in _REL_DAY:
        if kw in t:
            off = delta
            break
    if off is None:
        wm = _WEEKDAY_TXT_RE.search(t)
        idx = normalize_day(wm.group(0)) if wm else None
        if idx is not None:
            off = (idx - base.weekday()) % 7
    if off is None:
        return None
    return {"date": (base + dt.timedelta(days=off)).isoformat(), "date_kind": "rel"}

def parse_reserve(reserve: dict, posted_ts: float,
                  now_ts: float | None = None) -> dict | None:
    """把动态里的「直播预约」模块解析成一条单场场次。

    stype=1 是「视频预约」（新视频首发），不是直播，直接不收；只认 stype=2/0。

    卡上的相对日（「今天 19:00 直播」）按**当前时间**算，不按发帖时间：B站这张卡
    是展示时实时渲染的，11-05 00:16 发的动态里写「今天」，指的可能是 11-06 的直播。
    卡上写死「10-30 19:30」这种绝对日期时直接用。日期读不出来时 date=None，
    交给 combine_reserve 用标题/正文补。
    """
    rv = reserve or {}
    if int(rv.get("stype") or 0) == 1:
        return None
    text = _RESERVE_TITLE_RE.sub("", str(rv.get("title") or "")).strip()
    if not text:
        return None
    txt = str(rv.get("time_text") or "")
    base = dt.date.fromtimestamp(float(posted_ts or time.time()))
    now = dt.date.fromtimestamp(float(now_ts)) if now_ts else dt.date.today()
    hh = mi = None
    date = None
    date_kind = ""
    m = _RESERVE_TIME_RE.search(txt)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        hh, mi = int(m.group(3)), int(m.group(4))
        for y in (now.year, now.year + 1, now.year - 1):
            try:
                d = dt.date(y, month, day)
            except ValueError:
                continue
            if abs((d - now).days) <= 120:
                date, date_kind = d, "abs"
                break
    else:
        tm = _TIME_ONLY_RE.search(txt)
        if tm:
            hh, mi = int(tm.group(1)), int(tm.group(2))
            dh = text_date_hint(txt, now_ts or time.time()) or {}
            if dh.get("date"):
                date_kind = dh.get("date_kind") or "rel"
                date = dt.date.fromisoformat(dh["date"])
    if hh is None or mi is None:
        return None
    t, raw = normalize_time("%02d:%02d" % (hh, mi))
    return {"date": date.isoformat() if date else None, "date_kind": date_kind,
            "time": t, "time_text": raw if t is None else "",
            "text": text, "reserve_id": str(rv.get("reserve_id") or ""),
            "reserve_total": int(rv.get("reserve_total") or 0),
            "count_text": str(rv.get("count_text") or "")}

_TEXT_TIME_RE = re.compile(
    r"(\d{1,2})\s*[:：]\s*(\d{2})|(\d{1,2})\s*[.．]\s*(\d{2})")

_TEXT_POINT_RE = re.compile(r"(\d{1,2})\s*点\s*(半|\d{1,2}\s*分)?")

_EMOJI_TOKEN_RE = re.compile(r"\[[^\]]{1,20}\]")

# 卡片标题只剩这些词时等于没写内容，改用正文话题
_GENERIC_TITLES = {"直播", "直播预约", "预约", "今晚直播", "今天直播", "直播中", "预约直播"}

# 只有明确说「取消预约」才算主播取消，避免把「取消××活动」误当日程作废
_CANCEL_RESERVE_RE = re.compile(r"取消预约|预约取消|预约已取消")

def text_schedule_hint(text: str, posted_ts: float) -> dict | None:
    """从动态正文/标题里读出「几点、哪天」，用来和预约卡互校、或补卡片没写清的时间。"""
    t = _cjk_tight(str(text or ""))
    if not t:
        return None
    m = _TEXT_TIME_RE.search(t)
    hh = mi = 0
    if m:
        if m.group(1) is not None:
            hh, mi = int(m.group(1)), int(m.group(2))
        else:
            hh, mi = int(m.group(3)), int(m.group(4))
            if hh < 6:                       # 11.2 这种是日期，不是时间
                return None
    else:
        p = _TEXT_POINT_RE.search(t)
        if not p:
            return None
        hh = int(p.group(1))
        tail = (p.group(2) or "").strip()
        if tail.startswith("半"):
            mi = 30
        else:
            digits = re.sub(r"\D", "", tail)
            mi = int(digits) if digits else 0
    if not (0 <= hh <= 29 and 0 <= mi <= 59):
        return None
    # 「今晚8点」「明晚7点」「下午3点」这种 12 小时制：有晚间/下午字样且 <12 就补 12 小时
    head = t[max(0, m.start() - 12 if m else p.start() - 12):(m.start() if m else p.start())]
    if hh < 12 and re.search(r"(晚|夜|下午|中午)", head) and not re.search(r"(早|上午|凌晨)", head):
        hh += 12
    dh = text_date_hint(t, posted_ts) or {}
    return {"time": "%02d:%02d" % (hh, mi), "date": dh.get("date"),
            "date_kind": dh.get("date_kind") or ""}

def _text_topic(text: str) -> str:
    """正文里除了时间/日期/at/表情之外的「节目是什么」。"""
    t = _EMOJI_TOKEN_RE.sub(" ", _cjk_tight(str(text or "")))
    t = re.sub(r"@[^\s，,。！!~～]+", " ", t)
    t = re.sub(r"(今天|今晚|今日|明天|明晚|明天晚上|后天|大后天|次日|晚上|下午|上午|中午)", " ", t)
    t = _TEXT_TIME_RE.sub(" ", t)
    t = _TEXT_POINT_RE.sub(" ", t)
    t = re.sub(r"[，,。.!！~～、：:；;\s]+", " ", t).strip()
    return t[:40]

def combine_reserve(reserve: dict, posted_ts: float, dyn_text: str = "",
                    dyn_title: str = "", now_ts: float | None = None) -> dict | None:
    """预约卡 × 动态正文：合成一条日程。

    时间以预约卡为准（平台按开播计划生成），正文写的时间对不上就提醒复核；
    卡片没写清时间时退回正文；内容优先用卡片标题，标题太笼统才用正文话题。
    卡片按钮已是「已结束」只在备注里写明，不判取消（2026-11-06 起）。
    """
    rv = reserve or {}
    card = parse_reserve(rv, posted_ts, now_ts)
    text = _cjk_tight(str(dyn_text or ""))
    title_txt = _cjk_tight(str(dyn_title or ""))
    hint = text_schedule_hint(text, posted_ts)
    t_hint = text_schedule_hint(title_txt, posted_ts)
    notes: list[str] = []
    review = 0
    base = dt.date.fromtimestamp(float(posted_ts or time.time()))
    # 日期优先级（2026-11-06 定）：卡片写死的绝对日期 > 标题/正文绝对日期 >
    # 标题/正文相对日（按发帖日算）> 卡片相对日（按当前算）> 发帖日兜底。
    # 卡片上的「今天/明天」是展示时实时渲染的，跟发帖日经常对不上
    # （实测 29080：11-05 00:16 发的卡片写「今天 19:00」，实际直播在 11-06）。
    abs_post = absolute_date_hint(title_txt, base) or absolute_date_hint(text, base)
    rel_post = ""
    for h in (t_hint, hint):
        if h and h.get("date") and (h.get("date_kind") or "") == "rel":
            rel_post = h["date"]
            break
    card_date = (card or {}).get("date")
    card_kind = (card or {}).get("date_kind") or ""
    raw_card_txt = str(rv.get("time_text") or "").strip()
    if card_date and card_kind == "abs":
        date = card_date
        if abs_post and abs_post.isoformat() != card_date:
            notes.append("预约卡写 %s、标题/正文写 %s，以卡片为准"
                         % (card_date[5:], abs_post.isoformat()[5:]))
            review = 1
    elif abs_post:
        date = abs_post.isoformat()
        if card_date and card_date != date:
            notes.append("预约卡写「%s」，标题/正文写 %s，按标题/正文"
                         % (raw_card_txt or card_date, date))
    elif rel_post:
        date = rel_post
    elif card_date:
        date = card_date
    else:
        date = base.isoformat()
        notes.append("卡片和正文都没写日期，按发帖日 %s" % date)
    tm = (card or {}).get("time") or (t_hint or {}).get("time") or (hint or {}).get("time")
    time_text = (card or {}).get("time_text") or ""
    if card and hint and hint.get("time") and card.get("time") \
            and hint["time"] != card["time"]:
        notes.append("正文写 %s、预约卡写 %s，以卡片为准"
                     % (hint["time"], card.get("time") or time_text or "—"))
        review = 1
    if (not card) and hint and hint.get("time"):
        notes.append("卡片没写清时间，按正文 %s" % hint["time"])
    if not tm:
        return None
    title = (card or {}).get("text") or ""
    topic = _text_topic(text)
    if (not title) or len(title) < 2 or title in _GENERIC_TITLES:
        if topic:
            title = topic
    elif (topic and topic not in title and title not in topic
          and not _CANCEL_RESERVE_RE.search(text)):
        notes.append("正文补充：%s" % topic)
    if rv.get("ended"):
        notes.append("预约卡显示已结束")
    return {"date": date, "time": tm, "time_text": time_text, "text": title,
            "cancel_kind": "", "notes": notes,
            "needs_review": review,
            "count_text": (card or {}).get("count_text") or "",
            "reserve_id": (card or {}).get("reserve_id") or "",
            "reserve_total": int((card or {}).get("reserve_total") or 0)}

def ingest_reserve(conn: sqlite3.Connection, uid: int, uname: str, event_id: str,
                   url: str, posted_ts: float, reserve: dict,
                   image_path: str | None = None, dry_run: bool = False,
                   dyn_text: str = "", dyn_title: str = "",
                   now_ts: float | None = None) -> dict:
    """直播预约 → 单场日程。

    同条动态的正文/标题一起参与判断（combine_reserve）：卡片给时间、正文给内容，
    写冲突就标 needs_review。2026-11-06 起不再判断「取消预约」，预约一律入成
    supplement（卡上「已结束」只写进备注）。2026-11-09 起：只有开着日程监控
    （subject.weekly_on=1）的主播才入表——关掉的人即使在播、预约卡写了时间，
    也不进日程表。
    session_id 用 "<event_id>#reserve"，和视觉回合的 "<event_id>#<i>" 分开，
    这样同一条动态先被当成候选、后又按预约入库时不会互相覆盖。
    """
    rv = reserve or {}
    if int(rv.get("stype") or 0) == 1:
        return {"status": "skipped", "note": "视频预约，不计入直播日程"}
    if int(uid) in weekly_off_uids(conn):
        return {"status": "skipped",
                "note": "该主播没开日程监控（weekly_on=0），不入日程表",
                "uid": int(uid), "uname": uname}
    merged = combine_reserve(rv, posted_ts, dyn_text, dyn_title, now_ts)
    if not merged:
        return {"status": "skipped", "note": "预约信息不全"}
    week_start = monday_of_iso(merged["date"])
    session_id = "%s#reserve" % event_id
    if not dry_run:
        # 同一场可能既有预约卡、又有视觉回合抽的单场：日期时间撞上且内容像，就不再重复入表
        for row in conn.execute(
                "SELECT session_id, text, event_id FROM weekly_session"
                " WHERE uid=? AND date=? AND time=? AND session_id<>?",
                (uid, merged["date"], merged["time"], session_id)).fetchall():
            # 同一条动态的两种读法（视觉回合抽的 vs 预约卡）只留一条：
            # 文本可能一个写「屋里吃饭 秋日小脍」、一个写「屋里吃饭联动：与…」，
            # same_show 认不出来，但 event_id 相同就说明是同一场。
            same_post = bool(row["event_id"]) and row["event_id"] == event_id
            if same_post or same_show(merged["text"], row["text"] or ""):
                return {"status": "duplicate", "session_id": row["session_id"],
                        "date": merged["date"], "time": merged["time"],
                        "note": "同一天同一时间已有同一场（%s）" % row["session_id"]}
    kind = "supplement"
    note = "来自直播预约" + ("（%s）" % merged["count_text"] if merged.get("count_text") else "")
    if merged["notes"]:
        note = _join_note(note, "；".join(merged["notes"]))
    if not dry_run:
        now = _now_ts()
        conn.execute(
            "INSERT INTO weekly_session(session_id, uid, uname, date, week_start,"
            " time, time_text, text, kind, cancel_kind, replaces, event_id, url,"
            " image_path, dyn_text, confidence, needs_review, note, created_at, updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(session_id) DO UPDATE SET"
            " date=excluded.date, week_start=excluded.week_start, time=excluded.time,"
            " time_text=excluded.time_text, text=excluded.text, kind=excluded.kind,"
            " cancel_kind=excluded.cancel_kind, url=excluded.url,"
            " image_path=excluded.image_path, dyn_text=excluded.dyn_text,"
            " needs_review=excluded.needs_review, note=excluded.note,"
            " updated_at=excluded.updated_at",
            (session_id, uid, uname, merged["date"], week_start, merged["time"],
             merged["time_text"], merged["text"], kind, "", None,
             event_id, url, image_path, str(dyn_text or "")[:1600], 0.95,
             merged["needs_review"], note, now, now))
        conn.commit()
    return {"status": "saved", "session_id": session_id, "uid": uid, "uname": uname,
            "date": merged["date"], "week_start": week_start, "time": merged["time"],
            "text": merged["text"], "kind": kind, "cancelled": False,
            "needs_review": merged["needs_review"],
            "reserve_id": merged["reserve_id"], "count_text": merged["count_text"]}

def _local_reserve_image(uid: int, dyn_id: str) -> str | None:
    """周表扫描可能已经把这图落过盘，能复用就复用，避免日程卡没图。"""
    base = SCHEDULE_DIR / str(uid)
    if not base.is_dir():
        return None
    for p in sorted(base.glob("%s_*" % dyn_id)):
        return str(p)
    return None

def refresh_reserves(client, conn: sqlite3.Connection, days: float = 3.0,
                     limit: int = 0, dry_run: bool = False) -> dict:
    """拉每个开着日程监控的主播最近的动态，把「直播预约」模块落成单场日程。

    2026-11-09 起只处理 weekly_on=1 的主播：关掉日程的人即使正在播、
    预约卡写了时间，也不该出现在日程表里（之前花礼Harei 就是这样混进来的）。
    供日程进程组周期调用（scripts/schedule_worker.py 的 reserve_loop），也可由
    scripts/weekly_reserve.py 薄包装成命令行。client 需有 _fetch_dyn_body。
    """
    from collector.bili import parse_dynamic
    from shared.contracts import EventType, make_event_id

    rows = conn.execute(
        "SELECT uid, name FROM subject WHERE enabled=1 ORDER BY uid").fetchall()
    off = weekly_off_uids(conn)
    if off:
        rows = [r for r in rows if int(r["uid"]) not in off]
    if limit:
        rows = rows[:limit]
    since = int(time.time() - float(days) * 86400)
    saved = skipped = errors = 0
    items: list[dict] = []
    for row in rows:
        uid, uname = int(row["uid"]), row["name"]
        try:
            raw = client._fetch_dyn_body(uid, "")
        except Exception as e:  # noqa: BLE001
            errors += 1
            items.append({"uid": uid, "uname": uname, "status": "error",
                          "note": "%s: %s" % (type(e).__name__, e)})
            continue
        body = (raw or {}).get("data") or {}
        for it in body.get("items") or []:
            dyn = parse_dynamic(it)
            if not dyn.reserve:
                continue
            if int(dyn.pub_ts or 0) < since:
                continue
            event_id = make_event_id(uid, EventType.DYNAMIC, dyn.id_str)
            res = ingest_reserve(
                conn, uid, dyn.uname or uname, event_id, dyn.url, dyn.pub_ts,
                dyn.reserve, image_path=_local_reserve_image(uid, dyn.id_str),
                dry_run=dry_run, dyn_text=dyn.text, dyn_title=dyn.title or "")
            res.update({"uid": uid, "uname": dyn.uname or uname,
                        "dyn_id": dyn.id_str, "event_id": event_id,
                        "title": dyn.reserve.get("title")})
            items.append(res)
            if res.get("status") == "saved":
                saved += 1
            elif res.get("status") == "error":
                errors += 1
            else:
                skipped += 1
    return {"generated_at": int(time.time()), "saved": saved, "skipped": skipped,
            "errors": errors, "items": items}
