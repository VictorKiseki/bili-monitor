"""数据库读写层：候选池、日程会话写入、手工操作、节目单持久化。"""

from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
import time
from pathlib import Path

from .parse import EXCLUDE_UIDS, WEEKDAYS, _cjk_tight, _images_of, _now_ts, infer_week_start, monday_of_iso, normalize_days, normalize_time, parse_week_label, resolve_session_date, same_show

from .fetch import _download, cache_dest, ensure_images

def program_save(conn: sqlite3.Connection, name: str, aliases=None, hosts=None,
                 weekday: str = "", time_="", note: str = "", enabled: bool = True,
                 program_id=None) -> dict:
    """新增/修改一个常驻节目。hosts 是常驻主持 uid 列表（按显示顺序）。"""
    name = str(name or "").strip()
    if not name:
        raise ValueError("节目名不能为空")
    wk = str(weekday or "").strip().upper()[:3]
    if wk and wk not in WEEKDAYS:
        raise ValueError("固定星期要么留空，要么 MON..SUN")
    t, _raw = normalize_time(time_ or "")
    crew = [u for u in _collab_uids(hosts) if u > 0]
    alias_list = [str(a).strip() for a in (aliases or []) if str(a).strip()]
    if name not in alias_list:
        alias_list.insert(0, name)
    now = time.time()
    conn.row_factory = sqlite3.Row
    vals = (name, json.dumps(alias_list, ensure_ascii=False), json.dumps(crew),
            wk, t or "", str(note or "")[:300], 1 if enabled else 0, now)
    try:
        if program_id:
            row = conn.execute("SELECT program_id FROM weekly_program WHERE program_id=?",
                               (int(program_id),)).fetchone()
            if not row:
                raise ValueError("没有这个常驻节目")
            conn.execute("UPDATE weekly_program SET name=?, aliases_json=?, hosts_json=?,"
                         " weekday=?, time=?, note=?, enabled=?, updated_at=? WHERE program_id=?",
                         vals + (int(program_id),))
            pid = int(program_id)
        else:
            cur = conn.execute("INSERT INTO weekly_program(name, aliases_json, hosts_json, weekday,"
                               " time, note, enabled, created_at, updated_at)"
                               " VALUES(?,?,?,?,?,?,?,?,?)", vals + (now,))
            pid = int(cur.lastrowid)
        conn.commit()
    except sqlite3.IntegrityError as e:
        conn.rollback()
        raise ValueError("已存在同名常驻节目：%s" % name) from e
    return {"ok": True, "program_id": pid}

def program_delete(conn: sqlite3.Connection, program_id: int) -> dict:
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT program_id FROM weekly_program WHERE program_id=?",
                       (int(program_id),)).fetchone()
    if not row:
        raise ValueError("没有这个常驻节目")
    conn.execute("DELETE FROM weekly_program WHERE program_id=?", (int(program_id),))
    conn.commit()
    return {"ok": True, "program_id": int(program_id)}

def weekly_off_uids(conn: sqlite3.Connection) -> set[int]:
    """日程表监控被关掉的主播（subject.weekly_on=0，2026-11-07）。

    关掉后：不再把 TA 的新动态收进候选池、待判定批次里也不出现，
    视觉回合不会为 TA 花 token；已经抽出来的 weekly_schedule 历史行保留
    （同一周别的号还要看，删了会丢数据）。
    表或列缺失（老库、只读副本）时按「全开」处理，绝不影响调用方。
    """
    try:
        if "weekly_on" not in {r[1] for r in conn.execute("PRAGMA table_info(subject)")}:
            return set()
        return {int(r[0]) for r in conn.execute("SELECT uid FROM subject WHERE weekly_on=0")}
    except sqlite3.Error:
        return set()

def pending_count(conn: sqlite3.Connection) -> int:
    """待判定候选数（扣掉日程表监控关掉的主播）；gate / 页面 / 状态都走这里。"""
    off = weekly_off_uids(conn)
    if not off:
        return int(conn.execute(
            "SELECT COUNT(*) FROM weekly_candidate WHERE status='pending'").fetchone()[0])
    q = ",".join("?" * len(off))
    return int(conn.execute(
        "SELECT COUNT(*) FROM weekly_candidate WHERE status='pending' AND uid NOT IN (%s)" % q,
        tuple(sorted(off))).fetchone()[0])

def scan(conn: sqlite3.Connection, since: int = 0, limit: int = 500,
         download: bool = True, now: float | None = None) -> list[dict]:
    """把新的单图/少图图文动态收进候选队列，落盘图片，返回待判定批次。"""
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT event_id, subject_uid, occurred_at, url, cover_url, extra FROM event"
        " WHERE type='dynamic' AND occurred_at >= ? ORDER BY occurred_at ASC LIMIT ?",
        (int(since), int(limit))).fetchall()
    off = weekly_off_uids(conn)
    batch: list[dict] = []
    # M2（2026-11-07）：原来每条 INSERT 后立刻 commit()，一轮 500 条就是 500 次 fsync；
    # 先攒行、循环结束一次性 executemany + commit，语义仍是 INSERT OR IGNORE（重复无副作用）。
    pending_rows: list[tuple] = []
    for r in rows:
        extra = json.loads(r["extra"] or "{}")
        if extra.get("subtype") != "DRAW":
            continue
        uid = int(r["subject_uid"])
        if uid in EXCLUDE_UIDS or uid in off:
            continue
        n = int(extra.get("image_count") or 0)
        imgs = _images_of(extra, r["cover_url"])
        if not imgs or n < 1 or n > 3:
            continue
        if conn.execute("SELECT 1 FROM weekly_candidate WHERE event_id=?",
                        (r["event_id"],)).fetchone():
            continue
        uname = extra.get("uname") or ""
        dyn_id = (extra.get("dyn_id") or "").strip()
        if not dyn_id:
            dyn_id = (r["event_id"] or "").rsplit(":", 1)[-1]
        local = []
        for i, img in enumerate(imgs[:3]):
            dest = cache_dest(int(uid), dyn_id, i, img["url"])
            ok = _download(img["url"], dest) if download else True
            local.append({"url": img["url"], "path": str(dest) if ok else None,
                          "w": img.get("w") or 0, "h": img.get("h") or 0})
        images_json = json.dumps(local, ensure_ascii=False)
        pending_rows.append(
            (r["event_id"], uid, uname, dyn_id, r["url"], int(r["occurred_at"]),
             (extra.get("text") or "")[:800], images_json))
        batch.append({"event_id": r["event_id"], "uid": uid, "uname": uname,
                      "dyn_id": dyn_id, "url": r["url"], "posted_at": int(r["occurred_at"]),
                      "text": extra.get("text") or "", "images": local})
    if pending_rows:
        conn.executemany(
            "INSERT OR IGNORE INTO weekly_candidate"
            "(event_id, uid, uname, dyn_id, url, pub_ts, text, images_json, status)"
            " VALUES(?,?,?,?,?,?,?,?, 'pending')", pending_rows)
        conn.commit()
    return batch

_STRONG = re.compile(r"周表|本周|这周|下周|每周|排期|安排表|日程")

_WEAK = re.compile(r"直播|安排|时间|点|见|播")

def candidate_score(text: str, images: list[dict]) -> int:
    """只用来排序（先看像周表的），不能用来丢弃：手写/无关键字的周表也要轮到。"""
    s = 0
    if _STRONG.search(text or ""):
        s += 4
    if _WEAK.search(text or ""):
        s += 1
    if images and images[0].get("w") and images[0].get("h"):
        ratio = images[0]["w"] / images[0]["h"]
        if 1.2 <= ratio <= 2.6:                 # 海报/横版日程更常见
            s += 1
    return s

def pending_batch(conn: sqlite3.Connection, limit: int = 100, *, exclude_event_ids=()) -> list[dict]:
    """待判定队列。人工标过「周表/单场」的排最前，再按可疑度。

    2026-11-05 起带上动态标题（event.title）：开播时间/周次常只写在标题里
    （例：「屋里吃饭 11月6日 周五晚7点」），只给正文会漏。
    """
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT c.event_id, c.uid, c.uname, c.dyn_id, c.url, c.pub_ts, c.text,"
        " c.images_json, c.human_scope, e.title AS title"
        " FROM weekly_candidate c LEFT JOIN event e ON e.event_id = c.event_id"
        " WHERE c.status='pending'").fetchall()
    out = []
    for r in rows:
        if r["event_id"] in exclude_event_ids:
            continue
        images = json.loads(r["images_json"] or "[]")
        title = str(r["title"] or "")
        text = r["text"] or ""
        out.append({"event_id": r["event_id"], "uid": r["uid"], "uname": r["uname"],
                    "dyn_id": r["dyn_id"], "url": r["url"], "posted_at": r["pub_ts"],
                    "text": text, "title": title, "images": images,
                    "human_scope": r["human_scope"],
                    "score": candidate_score(text + " " + title, images)})
    off = weekly_off_uids(conn)
    if off:                                   # 关了日程表监控的号：待判队列里不出现
        out = [x for x in out if int(x["uid"]) not in off]
    pri = {"week": 0, "single": 1}
    out.sort(key=lambda x: (pri.get(x["human_scope"], 2), -x["score"], -x["posted_at"]))
    return out[:int(limit)]

def set_candidate_scope(conn: sqlite3.Connection, event_ids, scope: str,
                        now: float | None = None) -> dict:
    """人工给候选定性质（支持批量）。

    week   —— 是周表，排到队列最前，等视觉回合抽取；
    single —— 是单场日程，同样排前面，但按「一天一场」抽；
    none   —— 不是日程，直接移出队列（status=skipped）。
    """
    conn.row_factory = sqlite3.Row
    scope = str(scope or "").strip().lower()
    if scope not in ("week", "single", "none"):
        raise ValueError("scope 只能是 week / single / none")
    ids = [str(x) for x in (event_ids or []) if str(x).strip()]
    if not ids:
        raise ValueError("没有选中候选")
    now = now or time.time()
    label = {"week": "周表", "single": "单场", "none": "非日程"}[scope]
    n = 0
    for eid in ids:
        row = conn.execute("SELECT status FROM weekly_candidate WHERE event_id=?",
                           (eid,)).fetchone()
        if not row:
            continue
        if scope == "none":
            conn.execute("UPDATE weekly_candidate SET status='skipped', human_scope='none',"
                         " human_at=?, decided_at=?, note=? WHERE event_id=?",
                         (now, now, "人工标为非日程", eid))
        else:
            # 之前判定完时图可能已被清掉，改回待判定前先把图补回来
            ensure_images(conn, eid)
            conn.execute("UPDATE weekly_candidate SET status='pending', human_scope=?,"
                         " human_at=?, decided_at=NULL, note=? WHERE event_id=?",
                         (scope, now, "人工标为" + label, eid))
        n += 1
    conn.commit()
    return {"ok": True, "count": n, "scope": scope}

# ---------------- 判定口径 ----------------
WEEK_MIN_DAYS = 4          # 少于 4 天就不叫周表：周表必须是「一周多天多场」

def classify_scope(result: dict) -> str:
    """week / single / none。

    2026-11-03 用户口径：周表必须是按周组织的多日程；「今晚 8 点歌杂」这类
    单场只是日程表的补充/调整，不算周表。模型给了 scope 就用，没给按天数推。
    """
    result = result or {}
    scope = str(result.get("scope") or "").strip().lower()
    if scope in ("week", "weekly", "周表"):
        return "week"
    if scope in ("single", "session", "day", "单场", "单日"):
        return "single"
    if scope in ("none", "not", "非周表"):
        return "none"
    n = len(normalize_days(result.get("days")))
    if n >= 2:
        return "week"
    if n == 1 or result.get("sessions"):
        return "single"
    return "none"

def _flat_entries(days: list[dict]) -> list[tuple]:
    out = []
    for d in days or []:
        for e in d.get("entries") or []:
            out.append((d.get("day"), e.get("time"), (e.get("text") or "").strip()))
    return out

def _diff_days(old: list[dict], new: list[dict]) -> dict:
    """粗比较，只用于让管理台看出「这周改了什么」。

    逐天三遍对齐：完全一致 → 同节目换时间（改档）→ 同时间换节目 → 剩下的算增/删。
    """
    items = []
    added = changed = removed = 0
    old_days: dict[str, list] = {}
    for d, t, x in _flat_entries(old):
        old_days.setdefault(d, []).append([t, x, False])
    new_days: dict[str, list] = {}
    for d, t, x in _flat_entries(new):
        new_days.setdefault(d, []).append([t, x, False])
    for day, nlist in new_days.items():
        olist = old_days.get(day) or []
        for n in nlist:                                  # 完全一致
            for o in olist:
                if not o[2] and not n[2] and o[0] == n[0] and o[1] == n[1]:
                    o[2] = n[2] = True
                    break
        for n in nlist:                                  # 同节目、换时间
            if n[2]:
                continue
            for o in olist:
                if not o[2] and not n[2] and o[1] == n[1] and o[0] != n[0]:
                    o[2] = n[2] = True
                    changed += 1
                    items.append({"day": day, "time": n[0], "old_time": o[0],
                                  "text": n[1], "action": "changed"})
                    break
        for n in nlist:                                  # 同时间、换节目
            if n[2]:
                continue
            for o in olist:
                if not o[2] and not n[2] and o[0] == n[0] and o[1] != n[1]:
                    o[2] = n[2] = True
                    changed += 1
                    items.append({"day": day, "time": n[0], "text": n[1],
                                  "old": o[1], "action": "changed"})
                    break
        for n in nlist:
            if not n[2]:
                added += 1
                items.append({"day": day, "time": n[0], "text": n[1], "action": "added"})
    for day, olist in old_days.items():
        for o in olist:
            if not o[2]:
                removed += 1
                items.append({"day": day, "time": o[0], "text": o[1], "action": "removed"})
    return {"added": added, "changed": changed, "removed": removed, "items": items}

def _record_change(conn, uid, week_start, event_id, scope, diff, now) -> None:
    conn.execute("INSERT INTO weekly_change(uid, week_start, event_id, scope,"
                 " added, changed, removed, diff_json, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                 (uid, week_start, event_id, scope, int(diff.get("added") or 0),
                  int(diff.get("changed") or 0), int(diff.get("removed") or 0),
                  json.dumps(diff.get("items") or [], ensure_ascii=False), now))

def _sessions_of(result: dict, posted_ts: float, week_start: str | None) -> list[dict]:
    """把 single 结果摊平成场次列表，也兼容模型只给一天 days 的写法。"""
    out = []
    for s in result.get("sessions") or []:
        if isinstance(s, str):
            s = {"text": s}
        if not isinstance(s, dict):
            continue
        t, raw = normalize_time(s.get("time") or s.get("time_text") or "")
        text = str(s.get("text") or "").strip()
        if not (t or text):
            continue
        date, how = resolve_session_date(s, posted_ts, week_start)
        kind = str(s.get("kind") or "supplement").strip().lower()
        if kind not in ("supplement", "override", "cancel"):
            kind = "supplement"
        out.append({"date": date, "date_how": how, "time": t,
                    "time_text": raw if t is None else "", "text": text,
                    "kind": kind, "replaces": s.get("replaces")})
    if out:
        return out
    for d in normalize_days(result.get("days")):
        for e in d.get("entries") or []:
            date, how = resolve_session_date({"day": d["day"]}, posted_ts, week_start)
            out.append({"date": date, "date_how": how, "time": e.get("time"),
                        "time_text": e.get("time_text") or "", "text": e.get("text") or "",
                        "kind": "supplement", "replaces": None})
    return out

# ---------------- 结果入库 ----------------
def _ocr_signal(conn: sqlite3.Connection, event_id: str, image_path, days: list[dict]):
    """跑（或读缓存）Windows OCR，作为视觉结果的第二意见。

    返回 (signal, note, text)。手写体 OCR 基本读不出东西，这时 signal=na，
    不当成冲突；只有 OCR 明确读到一组时间、且和视觉对不上一半以上才报 diff。
    """
    if not image_path:
        return "", "", ""
    text = ""
    try:
        p = Path(str(image_path))
        if not p.exists():
            return "", "", ""
        from scripts.weekly_ocr import compare, ensure
        res = ensure(conn, str(event_id), p)
        text = (res.get("text") or "")[:2000]
        text = _cjk_tight(text)
        cmp = compare(res.get("text") or "", days)
        sig = str(cmp.get("signal") or "")
        if sig == "na":                          # 没读到东西不算结论，也别往备注里塞噪音
            return sig, "", text
        return sig, str(cmp.get("note") or ""), text
    except Exception:                            # noqa: BLE001
        return "", "", text

def _note_with(note: str, extra: str) -> str:
    """把补充说明（OCR / 二次判读）接到备注后面；空备注不加分隔符。"""
    if not extra:
        return note
    return (note + "；" if note else "") + extra


def _resolve_scope(res: dict, cand) -> tuple[dict, str, str, str]:
    """定稿 scope：模型初判 → 人工标记 → 二次判读，返回 (res, scope, human, recheck_bad)。"""
    scope = classify_scope(res)
    if not res.get("is_schedule"):
        scope = "none"
    # 人工在管理台标过性质就以人工为准（标错会被下面的校验挡住）
    human = str((cand["human_scope"] if "human_scope" in cand.keys() else "") or "").strip()
    if human in ("week", "single"):
        scope = human
    # 二次判读（2026-11-06）：初判像预告的，模型要重读整段语义再定稿。
    # downgraded = 其实是播后闲聊/回顾/道别，直接当非日程；
    # 没带结论或 unsure 的一律进人工复核，免得悄悄写进日程表。
    recheck = str(res.get("recheck") or "").strip().lower()
    recheck_bad = ""
    if scope in ("week", "single") and human not in ("week", "single"):
        if recheck == "downgraded":
            scope = "none"
            res = dict(res, note=("二次判读：不是预告"
                                  + ("（%s）" % res.get("note") if res.get("note") else "")))
        elif recheck != "confirmed":
            recheck_bad = recheck or "missing"
    return res, scope, human, recheck_bad


def _store_week(conn, cand, res: dict, event_id: str, conf: float, hard: bool,
                image_url, image_path, human: str, recheck_bad: str, now: float,
                dry_run: bool, summary: dict) -> bool:
    """week：整周覆盖写 weekly_schedule 并记 diff；只有一天时降级为 single 并返回 False。"""
    days = normalize_days(res.get("days"))
    if len(days) < 2:                    # 说是周表却只有一天：降级为单场
        if human == "week":
            raise ValueError("人工标为周表，但只抽出 %d 天" % len(days))
        return False
    week_start = (parse_week_label(res.get("week_label"), cand["pub_ts"])
                  or _valid_week(res.get("week_start"))
                  or infer_week_start(cand["pub_ts"]))
    inferred = 0 if parse_week_label(res.get("week_label"), cand["pub_ts"]) else 1
    ocr_sig, ocr_note, ocr_text = _ocr_signal(conn, event_id, image_path, days)
    review = 1 if (conf < 0.7 or len(days) < WEEK_MIN_DAYS or inferred
                   or hard or res.get("needs_review")
                   or ocr_sig == "diff" or recheck_bad) else 0
    note = _note_with(_note_with(str(res.get("note") or "")[:300], ocr_note),
                      ("二次判读未确认（%s）" % recheck_bad) if recheck_bad else "")
    old = conn.execute("SELECT days_json FROM weekly_schedule"
                       " WHERE uid=? AND week_start=?",
                       (cand["uid"], week_start)).fetchone()
    diff = _diff_days(json.loads(old["days_json"] or "[]") if old else [], days)
    if not dry_run:
        conn.execute(
            "INSERT INTO weekly_schedule(uid, week_start, uname, week_label,"
            " week_inferred, event_id, dyn_id, url, image_url, posted_at,"
            " days_json, confidence, extractor, needs_review, note, created_at,"
            " updated_at, ocr_signal, ocr_text)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(uid, week_start) DO UPDATE SET"
            " uname=excluded.uname, week_label=excluded.week_label,"
            " week_inferred=excluded.week_inferred, event_id=excluded.event_id,"
            " dyn_id=excluded.dyn_id, url=excluded.url, image_url=excluded.image_url,"
            " posted_at=excluded.posted_at, days_json=excluded.days_json,"
            " confidence=excluded.confidence, extractor=excluded.extractor,"
            " needs_review=excluded.needs_review, note=excluded.note,"
            " updated_at=excluded.updated_at, ocr_signal=excluded.ocr_signal,"
            " ocr_text=excluded.ocr_text",
            (cand["uid"], week_start, cand["uname"], res.get("week_label"),
             1 if inferred else 0, cand["event_id"], cand["dyn_id"], cand["url"],
             image_url, cand["pub_ts"], json.dumps(days, ensure_ascii=False), conf,
             str(res.get("extractor") or "vlm"), review,
             note, now, now, ocr_sig, ocr_text))
        conn.execute("UPDATE weekly_candidate SET status='done', note=?, decided_at=?"
                     " WHERE event_id=?",
                     (str(res.get("note") or "")[:300], now, event_id))
        _record_change(conn, cand["uid"], week_start, event_id, "week", diff, now)
        conn.commit()
    summary["saved"] += 1
    summary["review"] += review
    summary["items"].append({
        "event_id": event_id, "status": "saved", "scope": "week",
        "uid": cand["uid"], "week_start": week_start, "days": len(days),
        "confidence": conf, "diff": {k: diff[k] for k in ("added", "changed", "removed")}})
    return True


def _store_single(conn, cand, res: dict, event_id: str, conf: float, hard: bool,
                  image_path, recheck_bad: str, now: float, dry_run: bool,
                  summary: dict) -> None:
    """single：单场预告，只补/改/取消一条；同主播同天同时间的重复场次只留先入表的。"""
    # ---- single：单场预告，只补/改/取消一条 ----
    sessions = _sessions_of(res, cand["pub_ts"], None)
    if not sessions:
        raise ValueError("single 结果里没有可用场次")
    review = 1 if (conf < 0.7 or hard or res.get("needs_review")
                   or recheck_bad
                   or any(s["date_how"] == 2 for s in sessions)) else 0
    single_note = _note_with(str(res.get("note") or "")[:300],
                             ("二次判读未确认（%s）" % recheck_bad) if recheck_bad else "")
    saved = 0
    if not dry_run:
        for i, s in enumerate(sessions):
            week_start = monday_of_iso(s["date"])
            # 同一场可能被两条动态各发一次（如「屋里吃饭」发了图文+预约两条）：
            # 同主播同天同时间且内容像，就只留先入表的那条
            if s.get("kind") != "cancel":
                dup = [row for row in conn.execute(
                    "SELECT text FROM weekly_session"
                    " WHERE uid=? AND date=? AND time=? AND event_id<>?"
                    " AND kind<>'cancel'",
                    (cand["uid"], s["date"], s["time"], cand["event_id"])).fetchall()
                       if same_show(row["text"] or "", s["text"] or "")]
                if dup:
                    continue
            conn.execute(
                "INSERT INTO weekly_session(session_id, uid, uname, date, week_start,"
                " time, time_text, text, kind, replaces, event_id, url, image_path,"
                " confidence, needs_review, note, created_at, updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(session_id) DO UPDATE SET"
                " date=excluded.date, week_start=excluded.week_start, time=excluded.time,"
                " time_text=excluded.time_text, text=excluded.text, kind=excluded.kind,"
                " replaces=excluded.replaces, url=excluded.url, image_path=excluded.image_path,"
                " confidence=excluded.confidence, needs_review=excluded.needs_review,"
                " note=excluded.note, updated_at=excluded.updated_at",
                (f"{event_id}#{i}", cand["uid"], cand["uname"], s["date"], week_start,
                 s["time"], s["time_text"], s["text"], s["kind"],
                 json.dumps(s.get("replaces"), ensure_ascii=False) if s.get("replaces") else None,
                 cand["event_id"], cand["url"], image_path, conf, review,
                 single_note, now, now))
            saved += 1
        conn.execute("UPDATE weekly_candidate SET status='done', note=?, decided_at=?"
                     " WHERE event_id=?",
                     (str(res.get("note") or "")[:300], now, event_id))
        first_week = monday_of_iso(sessions[0]["date"])
        _record_change(conn, cand["uid"], first_week, event_id, "single",
                       {"added": saved, "changed": 0, "removed": 0,
                        "items": [{"date": s["date"], "time": s["time"], "text": s["text"],
                                   "kind": s["kind"], "action": "session"} for s in sessions]},
                       now)
        conn.commit()
    summary["single"] += saved
    summary["review"] += review
    summary["items"].append({"event_id": event_id, "status": "saved", "scope": "single",
                             "uid": cand["uid"], "sessions": saved,
                             "dates": [s["date"] for s in sessions], "confidence": conf})


def ingest(conn: sqlite3.Connection, results: list[dict], dry_run: bool = False, *,
           keep_pending_on_error: bool = False) -> dict:
    """收卷：week 写 weekly_schedule，single 写 weekly_session，none 只标候选。

    · week   —— 一周多天多场，整周覆盖（UPSERT），顺带记 diff；
    · single —— 单场预告，只动某一天的一条：supplement 补 / override 改 / cancel 取消；
    · none   —— 不是日程，候选标 skipped。
    每次落库都往 weekly_change 记一笔，管理台据此显示「这周改了什么」。
    """
    conn.row_factory = sqlite3.Row
    now = time.time()
    summary = {"saved": 0, "skipped": 0, "error": 0, "review": 0,
               "single": 0, "items": []}
    for res in results or []:
        res = res or {}
        event_id = str(res.get("event_id") or "")
        cand = conn.execute("SELECT * FROM weekly_candidate WHERE event_id=?",
                            (event_id,)).fetchone()
        if not cand:
            summary["error"] += 1
            summary["items"].append({"event_id": event_id, "status": "error", "note": "候选不存在"})
            continue
        res, scope, human, recheck_bad = _resolve_scope(res, cand)
        try:
            if scope == "none":
                summary["skipped"] += 1
                if not dry_run:
                    conn.execute("UPDATE weekly_candidate SET status='skipped', note=?, decided_at=?"
                                 " WHERE event_id=?",
                                 (str(res.get("note") or "非周表")[:300], now, event_id))
                    conn.commit()
                summary["items"].append({"event_id": event_id, "status": "skipped"})
                continue
            conf = float(res.get("confidence") or 0)
            # 手写表要求读两遍（passes>=2），只读一遍的一律进人工复核
            hard = bool(res.get("handwritten")) and int(res.get("passes") or 1) < 2
            images = json.loads(cand["images_json"] or "[]")
            image_url = images[0]["url"] if images else None
            image_path = images[0].get("path") if images else None
            if scope == "week" and _store_week(conn, cand, res, event_id, conf, hard,
                                               image_url, image_path, human, recheck_bad,
                                               now, dry_run, summary):
                continue
            _store_single(conn, cand, res, event_id, conf, hard, image_path,
                          recheck_bad, now, dry_run, summary)
        except Exception as e:                          # noqa: BLE001
            summary["error"] += 1
            if not dry_run and keep_pending_on_error:
                conn.rollback()
            elif not dry_run:
                conn.execute("UPDATE weekly_candidate SET status='error', note=?, decided_at=?"
                             " WHERE event_id=?", (str(e)[:300], now, event_id))
                conn.commit()
            summary["items"].append({"event_id": event_id, "status": "error", "note": str(e)})
    return summary

def _valid_week(value) -> str | None:
    try:
        d = dt.date.fromisoformat(str(value or ""))
    except ValueError:
        return None
    if d.weekday() != 0:
        d = d - dt.timedelta(days=d.weekday())
    return d.isoformat()

# ---------------- 人工修改 ----------------
MANUAL_OPS = ("add", "modify", "remove")

def _subject_name(conn, uid: int) -> str:
    row = conn.execute("SELECT name FROM subject WHERE uid=?", (int(uid),)).fetchone()
    return (row["name"] if row else "") or ""

def _collab_uids(raw) -> list[int]:
    """把 collab_uids / collab_json 洗成去重后的 uid 列表。"""
    if raw is None or raw == "":
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return []
    out: list[int] = []
    for u in raw or []:
        try:
            u = int(u)
        except (TypeError, ValueError):
            continue
        if u and u not in out:
            out.append(u)
    return out

def manual_add(conn: sqlite3.Connection, uid, week_start: str, day: str, op: str = "add",
               time=None, text: str = "", target_time=None, target_text=None,
               note: str = "", uname: str = "", collab_uids=None,
               now: float | None = None) -> dict:
    """人工加/改/删一条日程。叠加在自动识别结果之上，下次重识别不会丢。

    collab_uids：人工联动，一起上表的其他主播 uid（周视图会合并成一条）。
    """
    conn.row_factory = sqlite3.Row
    day = str(day or "").strip().upper()[:3]
    if day not in WEEKDAYS:
        raise ValueError("星期必须是 MON..SUN")
    op = str(op or "add").strip().lower()
    if op not in MANUAL_OPS:
        raise ValueError("op 只能是 add / modify / remove")
    ws = _valid_week(week_start)
    if not ws:
        raise ValueError("week_start 必须是某周的周一（YYYY-MM-DD）")
    t, raw = normalize_time(time or "")
    text = str(text or "").strip()
    if op == "add" and not (t or text):
        raise ValueError("新增条目至少要有时间或内容")
    tt, _ = normalize_time(target_time or "")
    now = now or _now_ts()
    uname = str(uname or "").strip() or _subject_name(conn, uid)
    crew = [u for u in _collab_uids(collab_uids) if u != int(uid)]
    cur = conn.execute(
        "INSERT INTO weekly_manual(uid, uname, week_start, day, op, time, time_text, text,"
        " target_time, target_text, collab_json, note, created_at, updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (int(uid), uname, ws, day, op, t, raw if t is None else "", text, tt,
         str(target_text or "").strip(), json.dumps(crew), str(note or "")[:300], now, now))
    mid = cur.lastrowid
    _record_change(conn, int(uid), ws, None, "manual",
                   {"added": 1 if op == "add" else 0, "changed": 1 if op == "modify" else 0,
                    "removed": 1 if op == "remove" else 0,
                    "items": [{"day": day, "time": t, "text": text, "op": op,
                               "collab": crew, "action": "manual"}]}, now)
    conn.commit()
    return {"ok": True, "manual_id": mid}

def manual_update(conn: sqlite3.Connection, manual_id: int, **fields) -> dict:
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM weekly_manual WHERE manual_id=?", (int(manual_id),)).fetchone()
    if not row:
        raise ValueError("没有这条人工修改")
    day = str(fields.get("day") or row["day"]).strip().upper()[:3]
    if day not in WEEKDAYS:
        raise ValueError("星期必须是 MON..SUN")
    op = str(fields.get("op") or row["op"]).strip().lower()
    if op not in MANUAL_OPS:
        raise ValueError("op 只能是 add / modify / remove")
    t, raw = normalize_time(fields.get("time") if fields.get("time") is not None else row["time"])
    text = str(fields.get("text", row["text"]) or "").strip()
    tt, _ = normalize_time(fields.get("target_time", row["target_time"]))
    if "collab_uids" in fields:
        crew = [u for u in _collab_uids(fields.get("collab_uids")) if u != int(row["uid"])]
    else:
        crew = [u for u in _collab_uids(row["collab_json"]
                                        if "collab_json" in row.keys() else "[]")
                if u != int(row["uid"])]
    now = time.time()
    conn.execute("UPDATE weekly_manual SET day=?, op=?, time=?, time_text=?, text=?,"
                 " target_time=?, target_text=?, collab_json=?, note=?, updated_at=?"
                 " WHERE manual_id=?",
                 (day, op, t, raw if t is None else "", text, tt,
                  str(fields.get("target_text", row["target_text"]) or "").strip(),
                  json.dumps(crew),
                  str(fields.get("note", row["note"]) or "")[:300], now, int(manual_id)))
    _record_change(conn, row["uid"], row["week_start"], None, "manual",
                   {"added": 0, "changed": 1, "removed": 0,
                    "items": [{"day": day, "time": t, "text": text, "op": op,
                               "collab": crew, "action": "manual-edit"}]}, now)
    conn.commit()
    return {"ok": True, "manual_id": int(manual_id)}

def manual_delete(conn: sqlite3.Connection, manual_id: int) -> dict:
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM weekly_manual WHERE manual_id=?", (int(manual_id),)).fetchone()
    if not row:
        raise ValueError("没有这条人工修改")
    conn.execute("DELETE FROM weekly_manual WHERE manual_id=?", (int(manual_id),))
    _record_change(conn, row["uid"], row["week_start"], None, "manual",
                   {"added": 0, "changed": 0, "removed": 1,
                    "items": [{"day": row["day"], "time": row["time"], "text": row["text"],
                               "action": "manual-undo"}]}, time.time())
    conn.commit()
    return {"ok": True, "manual_id": int(manual_id)}

def session_delete(conn: sqlite3.Connection, session_id: str) -> dict:
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM weekly_session WHERE session_id=?",
                       (str(session_id),)).fetchone()
    if not row:
        raise ValueError("没有这条单场预告")
    conn.execute("DELETE FROM weekly_session WHERE session_id=?", (str(session_id),))
    _record_change(conn, row["uid"], row["week_start"], row["event_id"], "manual",
                   {"added": 0, "changed": 0, "removed": 1,
                    "items": [{"day": row["date"], "time": row["time"], "text": row["text"],
                               "action": "session-deleted"}]}, time.time())
    conn.commit()
    return {"ok": True, "session_id": str(session_id)}

def manual_list(conn: sqlite3.Connection, week_start: str) -> list[dict]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM weekly_manual WHERE week_start=?"
                        " ORDER BY manual_id", (week_start,)).fetchall()
    return [{"manual_id": r["manual_id"], "uid": r["uid"], "uname": r["uname"],
             "day": r["day"], "op": r["op"], "time": r["time"],
             "time_text": r["time_text"] or "", "text": r["text"],
             "target_time": r["target_time"], "target_text": r["target_text"],
             "collab_uids": _collab_uids(r["collab_json"]
                                         if "collab_json" in r.keys() else "[]"),
             "note": r["note"] or "", "updated_at": r["updated_at"]} for r in rows]

# ---------------- 周视图 ----------------
def week_changes(conn: sqlite3.Connection, week_start: str, limit: int = 20) -> list[dict]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT c.*, s.name AS uname FROM weekly_change c"
        " LEFT JOIN subject s ON s.uid=c.uid"
        " WHERE c.week_start=? ORDER BY c.change_id DESC LIMIT ?",
        (week_start, int(limit))).fetchall()
    out = []
    for r in rows:
        out.append({"at": r["created_at"], "uid": r["uid"],
                    "uname": r["uname"] or ("UID%s" % r["uid"]), "scope": r["scope"],
                    "event_id": r["event_id"], "added": r["added"], "changed": r["changed"],
                    "removed": r["removed"], "items": json.loads(r["diff_json"] or "[]")})
    return out
