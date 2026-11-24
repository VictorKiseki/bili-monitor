"""日程识别编排：闸门、Harness 文件交接与自定义 Agent API。

闸门沿用管理台的 weekly_ai_* 设置：开关、每批条数、每日时段、人工「排队」标记。
到点且有待判候选时，本模块把候选写成任务文件（out/ai_tasks/），由 DSH 会话读图后
把结果写进 out/ai_results/；`harness_ingest` 扫描结果、校验、洗字段后交给
`core.weekly.ingest` 入库。自定义 Agent API 也走同一入库入口。

    python scripts/weekly_agent.py status
    python scripts/weekly_agent.py export --limit 6 --dry-run
    python scripts/weekly_agent.py ingest
"""
from __future__ import annotations

import datetime as dt
import re
import time

from core import weekly
from core.weekly import agent_api
from core.weekly import batch as batchmod
from core.weekly import feature
from core.weekly import harness as harnessmod

#: 与 scripts/weekly_ai.py、web/console_weekly.py 共用同一组 meta 键
DEFAULTS = {
    "weekly_ai_enabled": "1",
    "weekly_ai_limit": "12",
    "weekly_ai_slots": "08:00,13:00,19:00,00:00",
    "weekly_ai_slots_updated_at": "0",
    "weekly_ai_request_at": "0",
    "weekly_ai_last_run": "0",
    "weekly_ai_last_result": "",
}
#: 本模块自己的记账键
AGENT_KEYS = {
    "weekly_agent_last_run": "0",
    "weekly_agent_last_error": "",
    "weekly_agent_last_items": "0",
}


def open_conn(path) -> "sqlite3.Connection":
    """独立进程用的连接（管理台/脚本各有各的，别跨线程共用）。"""
    import sqlite3

    from store.sqlite import SCHEMA, migrate

    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(SCHEMA)
    migrate(conn)
    for k, v in dict(DEFAULTS, **AGENT_KEYS).items():
        conn.execute("INSERT OR IGNORE INTO meta(k, v) VALUES(?, ?)", (k, v))
    conn.commit()
    return conn


def cfg(conn) -> dict:
    """识别开关 + 时段 + 批次上限（只读 meta，不碰模型配置）。"""
    rows = conn.execute("SELECT k, v FROM meta WHERE k LIKE 'weekly_ai_%'").fetchall()
    out = dict(DEFAULTS)
    out.update({r["k"]: r["v"] for r in rows})
    out["enabled"] = str(out["weekly_ai_enabled"]) not in ("0", "false", "no", "")
    out["limit"] = int(out["weekly_ai_limit"] or 12)
    out["slots"] = [s.strip() for s in str(out["weekly_ai_slots"] or "").split(",") if s.strip()]
    out["request_at"] = float(out["weekly_ai_request_at"] or 0)
    out["last_run"] = float(out["weekly_ai_last_run"] or 0)
    out["last_result"] = out["weekly_ai_last_result"] or ""
    return out


def pending_count(conn) -> int:
    """待判候选数（口径与 core.weekly 一致：关了日程表监控的主播不计入）。"""
    return weekly.pending_count(conn)


def gate(conn) -> dict:
    """跑不跑：开关 + 有无待判 + 是否人工点了「立即识别」。"""
    c = cfg(conn)
    n = pending_count(conn)
    manual = c["request_at"] > c["last_run"]
    if not c["enabled"]:
        return {"go": False, "reason": "disabled", "pending": n, "manual": manual}
    if n == 0:
        return {"go": False, "reason": "no-pending", "pending": 0, "manual": manual}
    return {"go": True, "reason": "manual" if manual else "scheduled",
            "pending": n, "limit": c["limit"], "manual": manual}


def _slot_ts(slot: str, base: dt.date) -> float | None:
    hh, sep, mm = str(slot or "").partition(":")
    if not (sep and hh.isdigit() and mm.isdigit()):
        return None
    try:
        when = dt.datetime.combine(base, dt.time(int(hh) % 24, int(mm) % 60))
    except ValueError:
        return None
    return when.timestamp()


def slot_updates(conn, slots: list[str], *, now: float | None = None) -> dict:
    """时段有变化时一并记录启用时间，已过去的新时段从次日开始。"""
    canonical = set()
    for slot in slots:
        match = re.fullmatch(r"([01]?[0-9]|2[0-3]):([0-5][0-9])", slot)
        if not match:
            raise ValueError("时段要写成 HH:MM，例如 08:00")
        canonical.add(f"{int(match[1]):02d}:{match[2]}")
    updates = {"weekly_ai_slots": ",".join(sorted(canonical))}
    if canonical != {slot.zfill(5) for slot in cfg(conn)["slots"]}:
        updates["weekly_ai_slots_updated_at"] = str(time.time() if now is None else now)
    return updates


def slot_due(conn, now: float | None = None) -> dict:
    """定时时段到点了没。返回 {due, slot, next_run}，给常驻循环用。

    规则：最近一个已到的时段须晚于上次运行及最近一次时段修改。
    管理台点「立即识别」由 gate 处理。
    """
    c = cfg(conn)
    ts = float(now if now is not None else time.time())
    today = dt.date.fromtimestamp(ts)
    cands = []
    for day in (today - dt.timedelta(days=1), today, today + dt.timedelta(days=1)):
        for s in c["slots"]:
            v = _slot_ts(s, day)
            if v is not None:
                cands.append((v, s))
    cands.sort()
    passed = [(v, s) for v, s in cands if v <= ts]
    upcoming = [v for v, _s in cands if v > ts]
    if not passed:
        return {"due": False, "slot": "", "last_slot_ts": 0.0,
                "next_run": int(upcoming[0]) if upcoming else 0}
    last_v, last_s = passed[-1]
    since = max(c["last_run"], float(c["weekly_ai_slots_updated_at"] or 0))
    return {"due": last_v > since, "slot": last_s, "last_slot_ts": last_v,
            "next_run": int(upcoming[0]) if upcoming else 0}


def _material(item: dict) -> str:
    """把一条候选摊成人读得懂的素材文本（Harness 任务也读这个）。"""
    lines = []
    lines.append("【主播】%s（uid %s）" % (item.get("uname") or "", item.get("uid") or ""))
    posted = float(item.get("posted_at") or 0)
    if posted:
        lines.append("【发布时间】" + dt.datetime.fromtimestamp(posted).strftime("%Y-%m-%d %H:%M")
                     + "（" + "周一周二周三周四周五周六周日"[dt.date.fromtimestamp(posted).weekday() * 2:
                         dt.date.fromtimestamp(posted).weekday() * 2 + 2] + "）")
    if str(item.get("title") or "").strip():
        lines.append("【动态标题】" + batchmod.tight(item["title"]))
    body = batchmod.tight(item.get("text") or "").strip()
    lines.append("【动态正文】" + (body[:1200] if body else "（无正文）"))
    hint = item.get("program_hint") or {}
    if hint:
        hosts = "、".join(h.get("uname") or "" for h in (hint.get("hosts") or []))
        lines.append("【常驻节目】%s%s" % (hint.get("name") or "",
                                        ("（常驻主持：%s）" % hosts) if hosts else ""))
    rv = item.get("reserve") or {}
    if rv:
        bits = [x for x in (rv.get("title"), rv.get("time_text") or rv.get("time"),
                            rv.get("date"), rv.get("count_text")) if x]
        lines.append("【直播预约卡】" + " / ".join(str(b) for b in bits)
                     + ("（已结束）" if rv.get("ended") else ""))
        lines.append("【预约卡说明】" + str(item.get("reserve_note") or ""))
    if item.get("ocr_text"):
        lines.append("【OCR 原文（可能不准）】" + str(item.get("ocr_text")))
    ctx = item.get("ctx") or {}
    if ctx:
        sess = ctx.get("sessions") or []
        if sess:
            lines.append("【该主播已排场次】" + "；".join(
                "%s %s %s" % (s.get("date") or "", s.get("time") or s.get("time_text") or "",
                               s.get("text") or "") for s in sess[:16]))
        for wk in (ctx.get("weeks") or [])[:2]:
            ents = wk.get("entries") or []
            if ents:
                lines.append("【该主播周表 %s】%s" % (wk.get("label") or wk.get("week_start") or "",
                                                "；".join(
                                                    "%s %s %s" % (e.get("day") or "",
                                                                   e.get("time") or e.get("time_text") or "",
                                                                   e.get("text") or "") for e in ents[:12])))
    lines.append("【提示】" + batchmod.RECHECK_HINT)
    lines.append("【提示】" + batchmod.TIME_HINT)
    return "\n".join(lines)


def _num(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def normalize(result: dict, item: dict, *, extractor: str = "harness") -> dict:
    """把判读输出洗成 weekly.ingest 认的字段（多给/少给都不炸）。"""
    res = dict(result or {})
    res["event_id"] = str(item.get("event_id") or res.get("event_id") or "")
    scope = str(res.get("scope") or "").strip().lower()
    is_sched = res.get("is_schedule")
    if is_sched is None:
        is_sched = scope != "none"
    if isinstance(is_sched, str):
        is_sched = is_sched.strip().lower() in ("1", "true", "yes", "是")
    res["is_schedule"] = bool(is_sched)
    if scope in ("week", "weekly"):
        res["scope"] = "week"
    elif scope in ("single", "session", "day"):
        res["scope"] = "single"
    elif scope == "none":
        res["scope"] = "none"
    else:
        res.pop("scope", None)
    conf = max(0.0, min(1.0, _num(res.get("confidence"), 0.0)))
    res["confidence"] = conf
    res["note"] = str(res.get("note") or "").strip()[:300]
    res["extractor"] = extractor
    re_v = str(res.get("recheck") or "").strip().lower()
    res["recheck"] = re_v if re_v in ("confirmed", "downgraded", "unsure") else ""
    res["handwritten"] = bool(res.get("handwritten"))
    res["needs_review"] = bool(res.get("needs_review"))
    if not isinstance(res.get("days"), list):
        res.pop("days", None)
    if not isinstance(res.get("sessions"), list):
        res.pop("sessions", None)
    return res


# ---------------- Harness 交接（2026-11-08） ----------------
# 不直连模型：把候选写成任务文件，DSH 会话（手动或定时自动化）读图后写结果文件，
# 这里扫描结果、校验、normalize 后用同一个 weekly.ingest 收口。

def harness_export(conn, limit: int | None = None, dry_run: bool = False, log=None,
                   event_ids=None, root=None) -> dict:
    """把待判候选导出成任务文件（已在未完成任务里的候选不重复导出）。"""
    c = cfg(conn)
    use_limit = int(limit or c["limit"] or 12)
    built = batchmod.build(conn, limit=use_limit, ocr_max=min(use_limit, 12), download=True)
    items = built["items"]
    if event_ids:
        want = {str(x) for x in event_ids}
        items = [x for x in items if str(x.get("event_id")) in want]
    open_ids = harnessmod.open_event_ids(root)
    if open_ids:
        items = [x for x in items if str(x.get("event_id")) not in open_ids]
    if not items:
        return {"ok": True, "mode": "harness", "count": 0, "exported": 0,
                "ocr": built.get("ocr", 0), "open": len(open_ids),
                "note": "没有新候选可导出（已导出的任务还没回收）"}
    payload = harnessmod.build_task(items, root=root, material=_material)
    if dry_run:
        return {"ok": True, "mode": "harness", "dry_run": True, "count": len(items),
                "ocr": built.get("ocr", 0), "task": payload}
    path = harnessmod.write_task(payload, root=root)
    _set_meta(conn, {"weekly_agent_last_run": str(time.time()),
                     "weekly_ai_last_run": str(time.time()),
                     "weekly_agent_last_items": str(len(items)),
                     "weekly_agent_last_error": ""})
    if log:
        log("日程识别任务已导出 %s（%d 条）" % (path, len(items)))
    return {"ok": True, "mode": "harness", "count": len(items), "exported": len(items),
            "ocr": built.get("ocr", 0), "batch_id": payload["batch_id"],
            "task_path": str(path), "result_path": payload.get("output_file") or "",
            "prompt": harnessmod.prompt_for(payload, root=root),
            "items": [i["event_id"] for i in payload["items"]]}


def harness_ingest(conn, dry_run: bool = False, log=None, root=None) -> dict:
    """回收结果文件：校验 → normalize → weekly.ingest → 归档。"""
    files = harnessmod.list_results(root)
    if not files:
        return {"ok": True, "mode": "harness", "count": 0, "ingest": None,
                "problems": [], "batches": []}
    tasks = {t.get("batch_id"): t for t in harnessmod.list_tasks(root)}
    rows_all, problems, batches = [], [], []
    for f in files:
        task = tasks.get(f.get("batch_id"))
        known = set(task.get("event_ids") or []) if task else None
        rows, probs = harnessmod.validate(f.get("results"), known)
        if f.get("error"):
            probs = list(probs) + [str(f["error"])]
        problems.extend(probs)
        rows_all.extend(rows)
        batches.append({"path": f.get("path"), "batch_id": f.get("batch_id"),
                        "rows": rows, "problems": probs})
    normalized = [normalize(r, {"event_id": r["event_id"]}) for r in rows_all]
    summary = weekly.ingest(conn, normalized, dry_run=bool(dry_run)) if normalized else \
        {"saved": 0, "skipped": 0, "error": 0, "review": 0, "single": 0, "items": []}
    archived = []
    if not dry_run:
        for b in batches:
            if b["rows"] and not b["problems"]:
                moved = harnessmod.archive(b["batch_id"], root=root, result_path=b["path"])
                if moved.get("result") or moved.get("task"):
                    archived.append(moved)
        tail = "回收 %d 条 / 入库 %d 条 / 复核 %d 条" % (
            len(normalized), summary.get("saved", 0), summary.get("review", 0))
        if problems:
            tail += " / 问题 %d 条" % len(problems)
        _set_meta(conn, {"weekly_ai_last_result": tail,
                         "weekly_agent_last_error": (problems[0] if problems else "")})
        if log and normalized:
            log(tail)
    return {"ok": True, "mode": "harness", "count": len(normalized),
            "ingest": summary, "problems": problems, "archived": archived,
            "batches": [{"batch_id": b["batch_id"], "rows": len(b["rows"]),
                         "problems": len(b["problems"])} for b in batches],
            "at": int(time.time())}


def _set_meta(conn, pairs: dict) -> None:
    for k, v in pairs.items():
        conn.execute("INSERT INTO meta(k, v) VALUES(?, ?)"
                     " ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))
    conn.commit()


def status(conn) -> dict:
    """管理台/CLI 的「现在是什么状态」：开关、队列、上次结果、Harness 任务。"""
    c = cfg(conn)
    rows = {r["k"]: r["v"] for r in conn.execute(
        "SELECT k, v FROM meta WHERE k LIKE 'weekly_agent_%'").fetchall()}
    return {"ok": True, "enabled": c["enabled"], "limit": c["limit"], "slots": c["slots"],
            "pending": pending_count(conn), "last_run": c["last_run"],
            "last_result": c["last_result"], "request_pending": c["request_at"] > c["last_run"],
            "harness": harnessmod.stats(),
            "agent": {"last_run": float(rows.get("weekly_agent_last_run") or 0),
                      "last_items": int(rows.get("weekly_agent_last_items") or 0),
                      "last_error": rows.get("weekly_agent_last_error") or ""},
            "next_run": slot_due(conn).get("next_run") or 0, "now": int(time.time())}


def api_run(conn, config: dict, *, client=None) -> dict:
    """领取本轮执行机会，发送候选，并回收仍待确认的结果。"""
    conn.execute("BEGIN IMMEDIATE")
    decision = gate(conn)
    if not feature.enabled(conn) or not decision["go"] or not (decision["manual"] or slot_due(conn)["due"]):
        conn.rollback()
        return {"ok": True, "count": 0}
    started = time.time()
    _set_meta(conn, {"weekly_ai_last_run": started, "weekly_agent_last_run": started,
                     "weekly_agent_last_items": 0, "weekly_agent_last_error": "",
                     "weekly_ai_last_result": "自定义 Agent 识别中"})

    limit = decision["limit"]
    items = {}
    try:
        batch = batchmod.build(conn, limit=limit, ocr_max=min(limit, 12),
                               exclude_event_ids=harnessmod.open_event_ids())
        items = {item["event_id"]: item for item in batch["items"]}
        if not items:
            _set_meta(conn, {"weekly_ai_last_result": "暂无可提交的候选"})
            return {"ok": True, "count": 0}
        payload = agent_api.build_payload(list(items.values()), _material)
        rows = agent_api.recognize(config, payload, client=client)
    except ValueError as exc:
        error = str(exc)
        _set_meta(conn, {"weekly_agent_last_items": len(items), "weekly_agent_last_error": error,
                         "weekly_ai_last_result": "自定义 Agent 识别失败"})
        return {"ok": False, "count": 0, "error": error}

    counts = {"saved": 0, "single": 0, "review": 0, "skipped": 0, "error": 0}
    for row in rows:
        item = items[row["event_id"]]
        conn.execute("BEGIN IMMEDIATE")
        try:
            if not feature.enabled(conn):
                break
            current = conn.execute(
                "SELECT c.status, c.human_scope, s.weekly_on FROM weekly_candidate c"
                " LEFT JOIN subject s ON s.uid=c.uid WHERE c.event_id=?",
                (row["event_id"],),
            ).fetchone()
            if (current and current["status"] == "pending" and current["weekly_on"] != 0
                    and current["human_scope"] == item.get("human_scope")):
                extractor = "deepseek" if config["mode"] == "deepseek" else "agent_api"
                result = normalize(row, item, extractor=extractor)
                summary = weekly.ingest(conn, [result], keep_pending_on_error=True)
                for key in counts:
                    counts[key] += summary[key]
        finally:
            conn.rollback()
    tail = "识别 %d 条 / 周表 %d 张 / 单场 %d 场 / 复核 %d 条 / 排除 %d 条 / 错误 %d 条" % (
        len(rows), counts["saved"], counts["single"], counts["review"], counts["skipped"], counts["error"])
    _set_meta(conn, {"weekly_agent_last_items": len(items), "weekly_ai_last_result": tail,
                     "weekly_agent_last_error": "部分候选未通过入库校验" if counts["error"] else ""})
    return {"ok": True, "count": len(rows), "ingest": counts}
