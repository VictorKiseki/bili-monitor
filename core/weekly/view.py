"""只读展示层：week_view 组装、直播状态标注、前后周计算。"""

from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
import time

from .parse import EXTRA_HINTS, WEEKDAYS, WEEKDAY_CN, _join_note, _pub_times, condense_texts, fuzzy_minutes, is_rest, load_aliases, load_programs, match_program, merge_collabs, normalize_time, program_public, same_show, week_label_of

from .store import _collab_uids, pending_count, scan, week_changes, weekly_off_uids

def week_view(conn: sqlite3.Connection, week_start: str) -> dict:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM weekly_schedule WHERE week_start=? ORDER BY uid",
        (week_start,)).fetchall()
    # 监控名单：人工联动的名字、联动补登都从这里取
    subjects = {int(r[0]): (r[1] or "") for r in
                conn.execute("SELECT uid, name FROM subject").fetchall()}
    days = _week_days(rows)
    sessions = _apply_sessions(conn, week_start, rows, days)
    manuals, orphans = _apply_manuals(conn, week_start, days, subjects)
    programs = _apply_programs(conn, days, subjects)
    collab_info = _apply_collab_names(conn, days, subjects)
    collabs = collab_info["pairs"]
    _dedupe_entries(days)
    _sort_days(days)
    roster_list = _build_roster(days)
    pending = pending_count(conn)
    review = _review_count(conn, week_start)
    progs = program_public(programs, subjects)
    hits = _program_hits(days)
    for p in progs:
        p["week_entries"] = hits.get(p["program_id"], 0)
    return {"ok": True, "week_start": week_start, "week_label": week_label_of(week_start),
            "days": days, "streamers": len(rows), "sessions": len(sessions),
            "manuals": len(manuals), "manual_orphans": orphans,
            "collabs": collabs, "external": collab_info["externals"], "roster": roster_list,
            "programs": progs, "program_hits": sum(hits.values()),
            "pending": pending, "needs_review": review,
            "changes": week_changes(conn, week_start)}

def _week_days(rows: list) -> list[dict]:
    """把周表行展开成 7 天的条目桶（只填 week 来源的基础条目）。"""
    days = [{"day": WEEKDAYS[i], "label": WEEKDAY_CN[i], "entries": []} for i in range(7)]
    for r in rows:
        for d in json.loads(r["days_json"] or "[]"):
            try:
                idx = WEEKDAYS.index(d.get("day"))
            except ValueError:
                continue
            entries = d.get("entries") or []
            for e in entries:
                # 老数据可能只在 time_text 里写「9~12」；展示/排序统一成具体钟点（2026-11-09）。
                time_value = e.get("time")
                if not time_value:
                    parsed, _raw = normalize_time(e.get("time_text") or "")
                    if parsed:
                        time_value = parsed
                days[idx]["entries"].append({
                    "uid": r["uid"], "uname": r["uname"], "day": WEEKDAYS[idx],
                    "time": time_value,
                    "time_text": e.get("time_text") or "", "text": e.get("text") or "",
                    "image_url": r["image_url"], "url": r["url"],
                    "confidence": r["confidence"], "needs_review": bool(r["needs_review"]),
                    "week_label": r["week_label"], "source": "week", "kind": "base",
                    "event_id": r["event_id"], "updated_at": r["updated_at"],
                    "ocr_signal": (r["ocr_signal"] if "ocr_signal" in r.keys() else "") or ""})
            if not entries and is_rest(d.get("note")):
                # 周表这一天写着「休」：合成一条「休息」，文字版和图片版都要能显示出来。
                days[idx]["entries"].append({
                    "uid": r["uid"], "uname": r["uname"], "day": WEEKDAYS[idx],
                    "time": "", "time_text": "休息", "text": "",
                    "image_url": r["image_url"], "url": r["url"],
                    "confidence": r["confidence"], "needs_review": bool(r["needs_review"]),
                    "week_label": r["week_label"], "source": "week", "kind": "rest",
                    "rest": True, "event_id": r["event_id"], "updated_at": r["updated_at"],
                    "ocr_signal": ""})
    return days

def _apply_sessions(conn: sqlite3.Connection, week_start: str, rows: list,
                    days: list[dict]) -> list:
    """把单场预告并进对应那天：改期改人、取消、补充都走这里，返回单场行。"""
    sessions = conn.execute("SELECT * FROM weekly_session WHERE week_start=?"
                            " ORDER BY date, time", (week_start,)).fetchall()
    # 2026-11-09：只有监控名单里开着日程（weekly_on=1）的主播才上表；
    # 关掉日程的人即使在播、即使预约卡写了时间，也不进这一周的日程表。
    off = weekly_off_uids(conn)
    if off:
        sessions = [s for s in sessions if int(s["uid"]) not in off]
    pub = _pub_times(conn, [r["event_id"] for r in rows] + [s["event_id"] for s in sessions])
    for s in sessions:
        try:
            idx = dt.date.fromisoformat(s["date"]).weekday()
        except ValueError:
            continue
        bucket = days[idx]["entries"]
        mine = [e for e in bucket if int(e["uid"]) == int(s["uid"])
                and not e.get("cancelled") and not e.get("rest")]
        s_ts = pub.get(s["event_id"] or "", 0) or (s["created_at"] or 0)
        same_time = [e for e in mine if (e.get("time") or "") == (s["time"] or "")]
        extra = any(h in (s["text"] or "") for h in EXTRA_HINTS)
        # 带「加播/加场」字样的不是改期，是另开一场，不能吃掉周表里那场
        similar = [] if extra else [e for e in mine
                                    if same_show(e.get("text") or "", s["text"] or "")]
        target = None
        if same_time:
            # 同一时间：内容对得上就是同一场（措辞不同也只留一条）；
            # 内容完全不搭时，只有明确「取消/改档」的单场才并进去，否则视为同时段两场各留一条
            best = [e for e in same_time if same_show(e.get("text") or "", s["text"] or "")]
            if best:
                target = best[0]
            elif s["kind"] in ("cancel", "override"):
                target = same_time[0]
        elif similar:                       # 内容对得上但时间不一样：视为改期
            target = similar[0]
        if target is not None:
            old_ts = pub.get(target.get("event_id") or "", 0) or (target.get("updated_at") or 0)
            if s_ts and old_ts and s_ts <= old_ts:
                # 单场预告比周表还旧（周表后来重发过）：这条是过期信息
                continue
            if s["kind"] == "cancel":
                target["cancelled"] = True
                target["kind"] = "cancel"
                target["cancel_note"] = s["text"]
                target["cancel_kind"] = s["cancel_kind"] if "cancel_kind" in s.keys() else ""
                continue
            old_time = target.get("time") or ""
            new_time = s["time"] or ""
            if old_time != new_time and new_time:
                target["orig_time"] = old_time
                target["time"] = new_time
                target["time_changed"] = {"from": old_time, "to": new_time}
                target["kind"] = "override"
                target["note"] = _join_note(target.get("note"),
                                            "单场改期 %s → %s" % (old_time or "—", new_time or "—"))
            elif old_time and not new_time:
                # 单场只说了「晚上」这类模糊时段：不去覆盖周表已经给出的具体钟点
                target["note"] = _join_note(target.get("note"), "单场补充未给具体时间")
            if s["text"] and len(s["text"]) > len(target.get("text") or ""):
                target["orig_text"] = target.get("orig_text") or target.get("text") or ""
                target["text"] = s["text"]
            target["merged"] = int(target.get("merged") or 0) + 1
            target["single_event"] = s["event_id"]
            if s["kind"] == "override":
                target["overridden"] = True
            continue
        s_time = s["time"]
        if not s_time:
            parsed, _raw = normalize_time(s["time_text"] or "")
            if parsed:
                s_time = parsed
        entry = {"uid": s["uid"], "uname": s["uname"], "day": WEEKDAYS[idx],
                 "time": s_time,
                 "time_text": s["time_text"] or "", "text": s["text"],
                 "image_url": None, "url": s["url"], "confidence": s["confidence"],
                 "needs_review": bool(s["needs_review"]), "week_label": None,
                 "source": "single", "kind": s["kind"], "updated_at": s["updated_at"],
                 "date": s["date"], "session_id": s["session_id"],
                 "event_id": s["event_id"]}
        entry["cancel_kind"] = (s["cancel_kind"] if "cancel_kind" in s.keys() else "") or ""
        if s["kind"] == "cancel":
            entry["cancelled"] = True
            bucket.append(entry)
            continue
        entry["kind"] = "supplement"
        bucket.append(entry)
    return sessions

def _apply_manuals(conn: sqlite3.Connection, week_start: str, days: list[dict],
                   subjects: dict[int, str]) -> tuple[list, int]:
    """人工修改（add/modify/remove）落到对应那天的条目上，返回人工行与孤儿数。"""
    # 人工修改：叠在自动结果之上（add 追加 / modify 覆盖 / remove 划线）
    manuals = conn.execute("SELECT * FROM weekly_manual WHERE week_start=?"
                           " ORDER BY manual_id", (week_start,)).fetchall()
    orphans = 0
    for m in manuals:
        try:
            idx = WEEKDAYS.index(m["day"])
        except ValueError:
            continue
        bucket = days[idx]["entries"]
        if m["op"] == "add":
            crew = [int(m["uid"])] + [u for u in _collab_uids(m["collab_json"])
                                      if u != int(m["uid"])]
            names = [m["uname"] or subjects.get(int(m["uid"]), "")
                     or ("UID%s" % m["uid"])]
            names += [subjects.get(u, "") or ("UID%s" % u) for u in crew[1:]]
            bucket.append({"uid": m["uid"], "uname": " × ".join(n for n in names if n),
                           "day": WEEKDAYS[idx], "time": m["time"],
                           "time_text": m["time_text"] or "", "text": m["text"],
                           "image_url": None, "url": None, "confidence": None,
                           "needs_review": False, "week_label": None, "source": "manual",
                           "kind": "manual", "manual_id": m["manual_id"],
                           "uids": crew, "collab": len(crew) > 1,
                           "partners": [{"uid": u,
                                         "uname": subjects.get(u, "") or ("UID%s" % u)}
                                        for u in crew[1:]],
                           "note": m["note"] or "", "updated_at": m["updated_at"]})
            continue
        target = [e for e in bucket if e["uid"] == m["uid"]
                  and (m["target_time"] or "") == (e.get("time") or "")
                  and (not m["target_text"] or (e.get("text") or "") == m["target_text"])]
        target = [e for e in target if not e.get("rest")]
        if not target and m["target_text"]:      # 时间被新周表改了：按节目名兜底
            target = [e for e in bucket if e["uid"] == m["uid"]
                      and (e.get("text") or "") == m["target_text"]]
        if not target and m["target_time"]:      # 节目名被改了：按时间兜底
            target = [e for e in bucket if e["uid"] == m["uid"]
                      and (e.get("time") or "") == m["target_time"]]
        if not target:
            orphans += 1
            continue
        for e in target:
            e["manual_id"] = m["manual_id"]
            if m["op"] == "remove":
                e["cancelled"] = True
                e["kind"] = "manual-remove"
                e["cancel_note"] = "手动移除"
            else:
                e["orig_text"] = e.get("orig_text") or e["text"]
                e["edited"] = True
                e["kind"] = "manual-edit"
                e["text"] = m["text"] or e["text"]
                if m["time"]:
                    e["time"] = m["time"]
                    # 人工把「休息」改成有具体时间：不再是休息条目
                    e["time_text"] = ""
                    e.pop("rest", None)
                partner = [u for u in _collab_uids(m["collab_json"])
                           if u != int(m["uid"])]
                if partner:                       # 人工联动：把对象并进这一条
                    base = [int(u) for u in (e.get("uids") or [e["uid"]])]
                    for u in partner:
                        if u not in base:
                            base.append(u)
                    e["uids"] = base
                    e["collab"] = True
                    # 名字保持「自己」一个，合并/收尾时按 uids 统一重排，避免叠加
                    e["uname"] = (e.get("uname") or "").split(" × ")[0]
                    e["partners"] = [{"uid": u, "uname": subjects.get(u, "") or ("UID%s" % u)}
                                     for u in partner]
    return manuals, orphans

def _apply_programs(conn: sqlite3.Connection, days: list[dict],
                    subjects: dict[int, str]) -> list[dict]:
    """把常驻联动节目并进 days，并给落地的条目打上节目标。"""
    # 常驻联动节目：动态常只写嘉宾、不写常驻主持，按节目名/别名认出这一场并补齐主持
    programs = load_programs(conn)
    if programs:
        for d in days:
            for e in d["entries"]:
                p = match_program(e.get("text") or "", programs)
                if not p:
                    continue
                e["program_id"] = p["program_id"]
                e["program_name"] = p["name"]
                crew = [int(u) for u in p["hosts"]]
                for u in [int(e["uid"])] + [int(u) for u in (e.get("uids") or [])]:
                    if u not in crew:
                        crew.append(u)
                e["uids"] = crew
                e["collab"] = len(crew) > 1
                e["uname"] = (e.get("uname") or "").split(" × ")[0]
                e["partners"] = [{"uid": u, "uname": subjects.get(u) or ("UID%s" % u)}
                                 for u in crew if u != int(e["uid"])]
        # 同一天、同一时间、同一节目的多条（主持自己排的 + 嘉宾发的单场预告）并成一条
        for d in days:
            groups: dict[tuple, list[dict]] = {}
            for e in d["entries"]:
                if not e.get("program_id") or e.get("cancelled") or not e.get("time"):
                    continue
                groups.setdefault((e["time"], e["program_id"]), []).append(e)
            for _key, mine in groups.items():
                if len(mine) < 2:
                    continue
                keep = mine[0]
                for e in mine[1:]:
                    if len(e.get("text") or "") > len(keep.get("text") or ""):
                        keep = e
                crew: list[int] = []
                texts: list[str] = []
                for e in mine:
                    for u in [int(e["uid"])] + [int(u) for u in (e.get("uids") or [])]:
                        if u not in crew:
                            crew.append(u)
                    txt = (e.get("text") or "").strip()
                    if txt and txt not in texts:
                        texts.append(txt)
                if len(crew) > 1:
                    keep["uids"] = crew
                    keep["collab"] = True
                keep["text"] = condense_texts(texts) or keep.get("text") or ""
                for e in mine:
                    if e is not keep and e in d["entries"]:
                        d["entries"].remove(e)
    return programs

def _apply_collab_names(conn: sqlite3.Connection, days: list[dict],
                        subjects: dict[int, str]) -> dict:
    """算联动信息，并把联动条目的 uname 统一成「A × B」。"""
    # 监控名单里的全部主播：文案点到的联动对象即使本周没排期，也要能认出名、补一条日程
    collab_info = merge_collabs(days, load_aliases(conn), subjects)
    # 收尾：只要是联动条目，就按 uids 顺序用监控名单里的名字重排一遍
    for d in days:
        for e in d["entries"]:
            crew = [int(u) for u in (e.get("uids") or [])]
            if len(crew) < 2:
                continue
            parts = (e.get("uname") or "").split(" × ")
            fallback = dict(zip(crew, parts)) if len(parts) == len(crew) else {}
            names = [subjects.get(u) or fallback.get(u) or ("UID%s" % u) for u in crew]
            e["uname"] = " × ".join(names)
            e["collab"] = True
            e["partners"] = [{"uid": u, "uname": n} for u, n in zip(crew, names)
                             if u != int(e["uid"])]
    return collab_info

def _dedupe_entries(days: list[dict]) -> None:
    """同一天、同一时间、同一拨人的重复条目合并成一条（信息更全的留下）。"""
    # 最后再排一遍重：同一主播、同一天、同一时间、内容相近的，只留信息最全的那条
    for d in days:
        kept: list[dict] = []
        for e in d["entries"]:
            if e.get("rest"):
                kept.append(e)
                continue
            dup = None
            if not e.get("cancelled"):
                for k in kept:
                    if (not k.get("cancelled") and not k.get("rest") and int(k["uid"]) == int(e["uid"])
                            and (k.get("time") or "") == (e.get("time") or "")
                            and same_show(k.get("text") or "", e.get("text") or "")):
                        dup = k
                        break
            if dup is None:
                kept.append(e)
                continue
            if len(e.get("text") or "") > len(dup.get("text") or ""):
                dup["orig_text"] = dup.get("orig_text") or dup.get("text") or ""
                dup["text"] = e["text"]
            if e.get("source") == "single" and dup.get("source") == "week":
                dup["kind"] = "override"
            dup["merged"] = int(dup.get("merged") or 0) + 1
        d["entries"] = kept

def _sort_days(days: list[dict]) -> None:
    """每天按代表钟点排序，没时间的排最后。"""
    for d in days:
        # 具体时间、模糊时段（晚上）、时间区间（9~12）都按代表钟点排序；
        # 完全没有时间（休/待定）的排最后。
        def _sk(e):
            m = _entry_minutes(e)
            return (m is None, m if m is not None else 24 * 60, e.get("uname") or "")

        d["entries"].sort(key=_sk)

def _build_roster(days: list[dict]) -> list[dict]:
    """整周出现过的人按 uid 合并，生成出勤名单。"""
    # 名册：这一周出现过的所有主播（联动条目按 uids 拆开），供「按主播筛选」
    roster: dict[int, dict] = {}
    for d in days:
        for e in d["entries"]:
            if e.get("rest"):               # 休息不算出勤
                continue
            crew = [(int(e["uid"]), (e.get("uname") or "").split(" × ")[0])]
            for p in e.get("partners") or []:
                crew.append((int(p["uid"]), p.get("uname") or ""))
            for uid, nm in crew:
                rec = roster.setdefault(uid, {"uid": uid, "uname": "", "count": 0})
                rec["count"] += 1
                if nm and not rec["uname"]:
                    rec["uname"] = nm
    roster_list = sorted(roster.values(), key=lambda r: r["uname"] or str(r["uid"]))
    return roster_list

def _review_count(conn: sqlite3.Connection, week_start: str) -> int:
    """待复核条数（含单场补登的标记）。"""
    review = conn.execute("SELECT COUNT(*) FROM weekly_schedule WHERE needs_review=1"
                          " AND week_start=?", (week_start,)).fetchone()[0]
    review += conn.execute("SELECT COUNT(*) FROM weekly_session WHERE needs_review=1"
                           " AND week_start=?", (week_start,)).fetchone()[0]
    return review

def _program_hits(days: list[dict]) -> dict[int, int]:
    """数每个节目本周落了几条。"""
    hits: dict[int, int] = {}
    for d in days:
        for e in d["entries"]:
            pid = e.get("program_id")
            if pid and not e.get("rest"):
                hits[pid] = hits.get(pid, 0) + 1
    return hits

# ---------- 直播状态联动（2026-11-03）----------
# 日程表上「时间到了、人也真的在播」的那条要能看出来，点开直接进直播间。
LIVE_NEAREST_MAX_MIN = 480   # 在播时标「离现在最近的一场」，最远认 8 小时（超长直播也覆盖）

LIVE_CROSS_DAY_MIN = 360     # 凌晨 6 点前把昨晚那一列也算进来（跨零点直播）

def _entry_minutes(entry: dict) -> int | None:
    """条目时间的分钟数；9~12 / 20:00 / 8点 都尽量认。"""
    t, _raw = normalize_time(entry.get("time")) if entry.get("time") else (None, "")
    if t:
        try:
            hh, mm = t.split(":")
            return int(hh) * 60 + int(mm)
        except ValueError:
            pass
    text = entry.get("time_text") or ""
    fm = fuzzy_minutes(text)
    if fm is not None:
        return fm
    m = re.search(r"(\d{1,2})[:：.](\d{2})", text)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    m = re.search(r"(\d{1,2})\s*[~～\-至]\s*\d{1,2}", text)
    if m:
        return int(m.group(1)) * 60
    m = re.search(r"(\d{1,2})\s*点", text)
    if m:
        return int(m.group(1)) * 60
    return None

def _by_uid(entries: list[dict]) -> dict[int, list[dict]]:
    """按主播分组（跳过取消的条目）。"""
    out: dict[int, list[dict]] = {}
    for e in entries:
        if e.get("cancelled"):
            continue
        out.setdefault(e["uid"], []).append(e)
    return out

def annotate_live(view: dict, live: dict, now: float | None = None) -> dict:
    """把「正在直播」标到今天的日程条目上。

    live: {uid: {"room_id": 123, "title": "直播标题"}}，只放 live_status==1 的主播。
    命中条件：① 这一天就是今天；② 该主播在播；③ 今天的日程里挑「离现在最近的那一场」
    （距离 ≤ LIVE_NEAREST_MAX_MIN，距离相同优先已开始的那场）——不再卡固定窗口，
    超长直播、跨零点直播、比表上时间晚开很久，都能对上表里最近的一条；
    某主播今天只有一条且没写时间，就认为「这场」就是它。
    """
    now = time.time() if now is None else now
    view["live_now"] = 0
    try:
        start = dt.date.fromisoformat(str(view.get("week_start") or ""))
    except ValueError:
        return view
    today = dt.date.fromtimestamp(now)
    offset = (today - start).days
    if not (0 <= offset < 7) or not live:
        return view
    entries = view["days"][offset]["entries"]
    cur = dt.datetime.fromtimestamp(now)
    minutes_now = cur.hour * 60 + cur.minute
    scan = [(0, offset)]
    if minutes_now < LIVE_CROSS_DAY_MIN and offset > 0:
        scan.append((-1, offset - 1))      # 凌晨还在播：把昨晚那一列也纳入候选
    picks: dict[int, tuple] = {}           # uid -> ((距离, 是否已开始), 条目)
    for day_shift, idx in scan:
        for e in view["days"][idx]["entries"]:
            if e.get("cancelled") or e.get("rest"):
                continue
            uids = e.get("uids") or [e["uid"]]
            if not any(live.get(u) or live.get(str(u)) for u in uids):
                continue
            mins = _entry_minutes(e)
            if mins is None:
                continue
            delta = minutes_now - 1440 * day_shift - mins   # 前一天的行：+1440
            if abs(delta) > LIVE_NEAREST_MAX_MIN:
                continue
            key = (abs(delta), 0 if delta >= 0 else 1)
            for u in uids:
                old = picks.get(u)
                if old is None or key < old[0] or old[1] is e:
                    picks[u] = (key, e)
    # 今天只排了一条、又没写时间的，就认为「这场」就是它
    for uid, mine in _by_uid(entries).items():
        if uid in picks or len(mine) != 1 or _entry_minutes(mine[0]) is not None:
            continue
        if mine[0].get("rest"):
            continue
        if live.get(uid) or live.get(str(uid)):
            picks[uid] = ((0, 0), mine[0])
    marked = 0
    done: set[int] = set()
    for uid, (_key, pick) in picks.items():
        if id(pick) in done:                 # 联动条目被两位主播各选一次，只标一遍
            continue
        done.add(id(pick))
        info = live.get(uid) or live.get(str(uid)) or {}
        pick["live"] = True
        pick["live_room_id"] = int(info.get("room_id") or 0)
        pick["live_title"] = info.get("title") or ""
        if pick["live_room_id"]:
            pick["live_url"] = "https://live.bilibili.com/%d" % pick["live_room_id"]
        marked += 1
    view["live_now"] = marked
    return view

def prev_next_week(week_start: str, delta: int) -> str:
    d = dt.date.fromisoformat(week_start) + dt.timedelta(days=7 * delta)
    return d.isoformat()
