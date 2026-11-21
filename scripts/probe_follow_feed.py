"""关注流可行性探针（测试路径，2026-11-05）。

做什么
------
验证这个假设：把监控名单全部关注后，用「自己的动态首页」
/x/polymer/web-dynamic/v1/feed/all 一次请求就能覆盖多个 UP 的最新动态，
从而把逐人 N 次请求压成 1 次请求。

子命令
------
    status     凭据 / 登录态 / 关注数 / 关注流一次探测（只读）
    follow     把 config/subjects.json 里还没关注的对象关注上（写操作，记 journal）
    restore    按 journal 取关「本测试路径关注的」，回到测试前状态（写操作）
    feed       取一轮关注流并打印（只读）
    snapshot   逐人取数 + 关注流取数，存一份可复算的快照（只读）
    compare    载入快照，算关注流对逐人结果的召回（离线）
    monitor    持续轮询关注流 + 周期性逐人取数，测「新动态多快出现」（只读）

安全边界
--------
- 写操作只有 follow / restore，且都要求 --yes 或交互确认；
- 凭据只用 config/bili_follow.env，不碰 config/bili.env，不影响正在跑的监控；
- journal 记录本路径关注了谁，restore 只取关这些 uid，不动账号原有关系。

典型流程：
    python scripts/follow_feed_login.py
    python scripts/probe_follow_feed.py status
    python scripts/probe_follow_feed.py snapshot            # 测召回
    python scripts/probe_follow_feed.py follow --dry-run    # 看要关注谁
    python scripts/probe_follow_feed.py follow --yes
    python scripts/probe_follow_feed.py snapshot
    python scripts/probe_follow_feed.py monitor --minutes 32
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from collector.bili import BiliClient, BiliError  # noqa: E402
from collector.follow_feed import (  # noqa: E402
    DEFAULT_ENV, FollowFeedClient, collect_feed_round, coverage_report,
    dedupe_entries, error_hint, follow_plan, load_follow_cookies, load_journal,
    load_subjects, new_journal, percentile, save_journal, subject_map,
)

RUNS = ROOT / "logs" / "follow_compare"
JSON_MODE = False


def say(*a: Any, **k: Any) -> None:
    if JSON_MODE:
        print(*a, file=sys.stderr, **k)
    else:
        print(*a, **k)


def emit(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def hhmmss(ts: float | int) -> str:
    return datetime.fromtimestamp(float(ts)).strftime("%m-%d %H:%M:%S")


def age(ts: float | int) -> str:
    d = int(time.time() - float(ts))
    if d < 0:
        return "未来?"
    if d < 3600:
        return "%dm" % (d // 60)
    if d < 86400:
        return "%.1fh" % (d / 3600.0)
    return "%.1fd" % (d / 86400.0)


def make_client(env: str | None) -> FollowFeedClient:
    cookies = load_follow_cookies(env or None)
    return FollowFeedClient(cookies)


# ---------- status ----------
def cmd_status(args: argparse.Namespace) -> int:
    cookies = load_follow_cookies(args.env or None)
    say("凭据来源：", cookies.source or "(无)")
    say("SESSDATA：", cookies.masked or "(空)", " bili_jct：",
        ("有 %d 位" % len(cookies.bili_jct)) if cookies.bili_jct else "(缺)")
    say("可读关注流：", cookies.can_read, " 可关注/取关：", cookies.can_follow)
    if not cookies.sessdata:
        say("[失败] 没有任何 SESSDATA。先跑 scripts/follow_feed_login.py")
        return 2

    client = FollowFeedClient(cookies, min_interval=args.throttle)
    info = client.login_info()
    say("账号：", info.get("uname") or "-", " mid=", info.get("mid"),
        " nav=", "ok" if info.get("ok") else "失败(%s %s)" % (info.get("code"), info.get("message")))
    result: dict[str, Any] = {"cookies": {"source": cookies.source, "masked": cookies.masked,
                                          "has_jct": bool(cookies.bili_jct)},
                              "nav": info}
    if not info.get("ok"):
        if JSON_MODE:
            emit(result)
        return 1

    if args.csrf and cookies.bili_jct:
        csrf = client.check_csrf()
        say("bili_jct 校验：", csrf)
        result["csrf"] = csrf

    try:
        follows, total = client.followings()
        say("关注总数：", total, " 列表拉回：", len(follows))
        result["followings"] = {"total": total, "returned": len(follows)}
    except BiliError as e:
        say("关注列表失败：code=%s %s %s" % (e.code, e.message, error_hint(e.code)))
        result["followings"] = {"error": str(e)}

    try:
        r = collect_feed_round(client, pages=1)
        uids = {e.uid for e in r["entries"]}
        subjects = load_subjects(args.subjects)
        mon = subject_map(subjects)
        hits = [e for e in r["entries"] if e.uid in mon]
        say("关注流：%d 条 / %d 个 UP / %d 页  update_num=%s has_more=%s  %d ms %d B  risk=%d"
            % (len(r["entries"]), len(uids), r["pages"], r["update_num"],
               r["has_more"], r["ms"], r["bytes"], r["risk"]))
        if r["entries"]:
            say("  最新：", hhmmss(r["newest_ts"]), "（%s 前）最旧：%s（%s 前）"
                % (age(r["newest_ts"]), hhmmss(r["oldest_ts"]), age(r["oldest_ts"])))
            say("  其中监控名单内：%d 条，来自 %d 人；名单外：%d 人"
                % (len(hits), len({e.uid for e in hits}), len(uids - set(mon))))
        else:
            say("  空。若账号关注数为 0，这是正常结果（先 follow）")
        result["feed"] = {"items": len(r["entries"]), "uids": len(uids),
                          "pages": r["pages"], "update_num": r["update_num"],
                          "has_more": r["has_more"], "ms": r["ms"],
                          "bytes": r["bytes"], "risk": r["risk"],
                          "monitored_items": len(hits),
                          "monitored_uids": len({e.uid for e in hits})}
    except BiliError as e:
        say("关注流失败：code=%s %s %s" % (e.code, e.message, error_hint(e.code)))
        result["feed"] = {"error": str(e)}

    if JSON_MODE:
        emit(result)
    return 0


# ---------- feed ----------
def cmd_feed(args: argparse.Namespace) -> int:
    client = make_client(args.env)
    try:
        r = collect_feed_round(client, pages=args.pages, since_ts=0)
    except BiliError as e:
        say("关注流失败：code=%s %s %s" % (e.code, e.message, error_hint(e.code)))
        return 1
    if JSON_MODE:
        emit({"pages": r["pages"], "items": [e.to_dict() for e in r["entries"]],
              "update_num": r["update_num"], "has_more": r["has_more"],
              "ms": r["ms"], "bytes": r["bytes"], "risk": r["risk"],
              "oldest_ts": r["oldest_ts"], "newest_ts": r["newest_ts"]})
        return 0
    say("关注流：%d 条 / %d 页  update_num=%s has_more=%s  %d ms %d B  risk=%d"
        % (len(r["entries"]), r["pages"], r["update_num"], r["has_more"],
           r["ms"], r["bytes"], r["risk"]))
    mon = subject_map(load_subjects(args.subjects))
    for e in r["entries"][: max(10, args.show)]:
        it = e.item
        say("  %s  %-12s %-8s %s  %s" % (hhmmss(it.pub_ts), mon.get(e.uid, "?"),
                                         it.uname[:10], it.type.replace("DYNAMIC_TYPE_", ""),
                                         it.title[:40]))
    return 0


# ---------- follow / restore ----------
def journal_path(args: argparse.Namespace, mid: int) -> Path:
    if args.journal:
        p = Path(args.journal)
        return p if p.is_absolute() else ROOT / p
    return RUNS / ("journal_%d.json" % int(mid or 0))


def cmd_follow(args: argparse.Namespace) -> int:
    cookies = load_follow_cookies(args.env or None)
    if not cookies.can_follow:
        say("[失败] 缺少 bili_jct，无法关注。先跑 scripts/follow_feed_login.py")
        return 2
    client = FollowFeedClient(cookies, min_interval=args.throttle)
    info = client.login_info()
    if not info.get("ok"):
        say("[失败] 登录态无效：", info)
        return 2
    subjects = load_subjects(args.subjects)
    try:
        follows, total = client.followings()
    except BiliError as e:
        say("[失败] 读关注列表：code=%s %s" % (e.code, e.message))
        return 1
    have = {int(x.get("mid") or 0) for x in follows}
    only = None
    if args.only:
        only = [int(x) for x in args.only.replace(",", " ").split() if x.strip().isdigit()]
    plan = follow_plan(subjects, have, only_uids=only, max_n=args.limit)
    jp = journal_path(args, int(info.get("mid") or 0))
    journal = load_journal(jp) or new_journal(int(info.get("mid") or 0), info.get("uname") or "")
    for s in subjects:
        if s["uid"] in have:
            journal["already_followed"].append({"uid": s["uid"], "name": s["name"]})
    journal["already_followed"] = list({x["uid"]: x for x in journal["already_followed"]}.values())

    say("账号 %s（mid %s），当前关注 %d 人；名单 %d 人；本次待关注 %d 人"
        % (info.get("uname"), info.get("mid"), total, len(subjects), len(plan)))
    for s in plan:
        say("  + %-10s uid=%d" % (s["name"], s["uid"]))
    if not plan:
        save_journal(jp, journal)
        say("没有需要新关注的账号。journal：", jp)
        return 0
    if args.dry_run:
        say("--dry-run：没有发任何请求")
        return 0
    if not args.yes:
        try:
            ans = input("确认关注以上 %d 个账号？输入 yes 继续：" % len(plan))
        except EOFError:
            ans = ""
        if ans.strip().lower() not in ("yes", "y"):
            say("已取消")
            return 1

    aborted = ""
    for i, s in enumerate(plan, 1):
        r = client.follow(s["uid"])
        code = int(r.get("code") or 0)
        if r.get("ok"):
            journal["test_followed"].append({"uid": s["uid"], "name": s["name"],
                                             "at": time.time(), "code": code})
            say("[%d/%d] %-10s uid=%d  ok" % (i, len(plan), s["name"], s["uid"]))
        else:
            journal["failed"][str(s["uid"])] = {"name": s["name"], "code": code,
                                                "message": r.get("message", ""),
                                                "hint": r.get("hint", ""),
                                                "at": time.time()}
            say("[%d/%d] %-10s uid=%d  失败 code=%s %s %s"
                % (i, len(plan), s["name"], s["uid"], code, r.get("message", ""), r.get("hint", "")))
            if code in (-101, -111):
                aborted = "登录态/CSRF 失效，停止"
            elif code == -412:
                aborted = "撞到 -412，立即停手（别连续重试）"
            elif code in (-352, 22002):
                aborted = "风控/账号限制，停止"
        save_journal(jp, journal)
        if aborted:
            say("[中止]", aborted)
            break
        time.sleep(max(0.0, args.gap))

    try:
        follows2, total2 = client.followings()
        got = {int(x.get("mid") or 0) for x in follows2}
        missing = [x for x in journal["test_followed"] if x["uid"] not in got]
        say("关注总数：%d；journal 内已关注 %d 个；关注列表里没验证到的 %d 个"
            % (total2, len(journal["test_followed"]), len(missing)))
    except BiliError as e:
        say("关注数复查失败：code=%s %s" % (e.code, e.message))
    say("journal：", jp, "  （回滚：python scripts/probe_follow_feed.py restore --yes）")
    return 0 if not aborted else 3


def cmd_restore(args: argparse.Namespace) -> int:
    cookies = load_follow_cookies(args.env or None)
    if not cookies.can_follow:
        say("[失败] 缺少 bili_jct，无法取关")
        return 2
    client = FollowFeedClient(cookies, min_interval=args.throttle)
    info = client.login_info()
    if not info.get("ok"):
        say("[失败] 登录态无效：", info)
        return 2
    jp = journal_path(args, int(info.get("mid") or 0))
    journal = load_journal(jp)
    if not journal:
        say("[失败] 找不到 journal：", jp)
        return 1
    targets = [x for x in journal.get("test_followed") or [] if x.get("uid")]
    if not targets:
        say("journal 里没有本路径关注的账号，无需回滚")
        return 0
    try:
        follows, _ = client.followings()
    except BiliError as e:
        say("[失败] 读关注列表：code=%s %s" % (e.code, e.message))
        return 1
    have = {int(x.get("mid") or 0) for x in follows}
    todo = [x for x in targets if int(x["uid"]) in have]
    say("journal 记录 %d 个，当前仍关注 %d 个" % (len(targets), len(todo)))
    for x in todo:
        say("  - %-10s uid=%d" % (x.get("name", ""), int(x["uid"])))
    if not todo:
        journal["restored_at"] = time.time()
        save_journal(jp, journal)
        return 0
    if args.dry_run:
        say("--dry-run：没有发任何请求")
        return 0
    if not args.yes:
        try:
            ans = input("确认取关以上 %d 个账号？输入 yes 继续：" % len(todo))
        except EOFError:
            ans = ""
        if ans.strip().lower() not in ("yes", "y"):
            say("已取消")
            return 1

    remain = []
    for i, x in enumerate(todo, 1):
        r = client.unfollow(int(x["uid"]))
        if r.get("ok"):
            say("[%d/%d] %-10s uid=%d  已取关" % (i, len(todo), x.get("name", ""), int(x["uid"])))
        else:
            say("[%d/%d] %-10s uid=%d  失败 code=%s %s %s"
                % (i, len(todo), x.get("name", ""), int(x["uid"]),
                   r.get("code"), r.get("message", ""), r.get("hint", "")))
            remain.append(x)
        time.sleep(max(0.0, args.gap))
    if remain:
        journal["test_followed"] = remain
        journal["restored_at"] = None
    else:
        journal["test_followed"] = []
        journal["restored_at"] = time.time()
    save_journal(jp, journal)
    say("剩余未取关：", len(remain), " journal：", jp)
    return 0 if not remain else 3


# ---------- 逐人取数 ----------
def people_snapshot(client: BiliClient, subjects: list[dict[str, Any]],
                    per_user: int) -> dict[str, Any]:
    items: dict[int, list[dict[str, Any]]] = {}
    stats = {"errors": 0, "empty": 0, "requests": 0, "ms": 0}
    t_all = time.time()
    for i, s in enumerate(subjects, 1):
        t0 = time.time()
        try:
            lst, _, _ = client.fetch_dynamics(s["uid"], "", per_user, 1)
        except Exception as e:                   # noqa: BLE001
            stats["errors"] += 1
            items[s["uid"]] = []
            say("  [%2d/%d] %-10s 失败：%s" % (i, len(subjects), s["name"], e))
            continue
        if not lst:
            stats["empty"] += 1
        items[s["uid"]] = [{"id": x.id_str, "pub_ts": int(x.pub_ts),
                            "type": x.type, "title": x.title,
                            "uname": x.uname} for x in lst]
        say("  [%2d/%d] %-10s %2d 条  %5d ms%s"
            % (i, len(subjects), s["name"], len(lst),
               int((time.time() - t0) * 1000), "" if lst else "  (空)"))
    stats["ms"] = int((time.time() - t_all) * 1000)
    stats["requests"] = client.request_count
    stats["risk"] = client.last_risk
    return {"items": items, "stats": stats}


def print_coverage(rep: dict[str, Any], show_missing: int = 5) -> None:
    overall = rep["overall"]
    say("总体：名单 %d 人，有命中 %d 人；逐人条目 %d，关注流命中 %d，缺 %d（召回 %s）"
        % (overall["monitored"], overall["users_with_feed"], overall["snapshot_items"],
           overall["matched"], overall["missing"],
           ("%.1f%%" % overall["recall_pct"]) if overall["recall_pct"] is not None else "-"))
    say("      关注流条目 %d；名单内但逐人没取到的 %d；名单外 UP %d 个"
        % (overall["feed_items"], overall["feed_only"], len(rep["foreign_uids"])))
    rows = sorted(rep["users"].values(), key=lambda r: (r["matched"] - r["snapshot"]), reverse=False)
    for r in rows:
        if r["snapshot"] == 0 and r["feed"] == 0:
            continue
        say("  %-12s 逐人%2d 命中%2d 缺%2d 流内%2d%s"
            % (r["name"][:12], r["snapshot"], r["matched"], r["missing"], r["feed"],
               ("  缺: " + ",".join(r["missing_ids"][:show_missing])) if r["missing"] else ""))


def cmd_snapshot(args: argparse.Namespace) -> int:
    cookies = load_follow_cookies(args.env or None)
    if not cookies.can_read:
        say("[失败] 没有 SESSDATA")
        return 2
    subjects = load_subjects(args.subjects)
    say("逐人取数（%d 人 × 最多 %d 条）…" % (len(subjects), args.per_user))
    people = BiliClient(sessdata=cookies.sessdata, min_interval=args.throttle)
    snap = people_snapshot(people, subjects, args.per_user)
    say("逐人完成：%d ms，空 %d，错误 %d，risk %d"
        % (snap["stats"]["ms"], snap["stats"]["empty"], snap["stats"]["errors"],
           snap["stats"].get("risk", 0)))

    feedc = FollowFeedClient(cookies, min_interval=args.throttle)
    info = feedc.login_info()
    taken = time.time()
    say("关注流取数（%d 页）…" % args.pages)
    try:
        r = collect_feed_round(feedc, pages=args.pages)
    except BiliError as e:
        say("[失败] 关注流：code=%s %s %s" % (e.code, e.message, error_hint(e.code)))
        return 1
    feed_items = [e.to_dict(first_seen=0) for e in r["entries"]]
    say("关注流完成：%d 条 / %d 页 / %d ms / %d B"
        % (len(feed_items), r["pages"], r["ms"], r["bytes"]))

    RUNS.mkdir(parents=True, exist_ok=True)
    p = RUNS / ("round_%s.json" % datetime.now().strftime("%Y%m%d-%H%M%S"))
    payload = {
        "kind": "follow_feed_round",
        "version": 1,
        "taken_at": taken,
        "account": {"mid": info.get("mid"), "uname": info.get("uname")},
        "subjects": subjects,
        "people": {"per_user": args.per_user,
                   "items": {str(k): v for k, v in snap["items"].items()},
                   "stats": snap["stats"]},
        "feed": {"pages": r["pages"], "items": feed_items,
                 "update_num": r["update_num"], "has_more": r["has_more"],
                 "offset": r["offset"], "ms": r["ms"], "bytes": r["bytes"],
                 "risk": r["risk"]},
    }
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    say("快照：", p)
    if args.json:
        emit({"path": str(p), "people": snap["stats"], "feed": len(feed_items)})
        return 0
    if not args.no_compare:
        rep = coverage_report({int(k): v for k, v in snap["items"].items()},
                              feed_items, subject_map(subjects))
        print_coverage(rep)
    return 0


def latest_round() -> Path | None:
    files = sorted(RUNS.glob("round_*.json"))
    return files[-1] if files else None


def cmd_compare(args: argparse.Namespace) -> int:
    p = Path(args.file) if args.file else latest_round()
    if not p:
        say("[失败] logs/follow_compare 下没有 round_*.json，先跑 snapshot")
        return 1
    if not p.is_absolute():
        p = ROOT / p
    data = json.loads(p.read_text(encoding="utf-8"))
    people = {int(k): v for k, v in (data.get("people", {}).get("items") or {}).items()}
    feed = data.get("feed", {}).get("items") or []
    mon = subject_map(data.get("subjects") or load_subjects(args.subjects))
    since = 0
    if args.since_hours and args.since_hours > 0:
        since = int(float(data.get("taken_at") or time.time()) - args.since_hours * 3600)
    rep = coverage_report(people, feed, mon, since_ts=since)
    say("快照：", p, " 取于", hhmmss(data.get("taken_at") or 0))
    print_coverage(rep)
    if JSON_MODE:
        emit(rep)
    return 0


# ---------- monitor ----------
def cmd_monitor(args: argparse.Namespace) -> int:
    cookies = load_follow_cookies(args.env or None)
    if not cookies.can_read:
        say("[失败] 没有 SESSDATA")
        return 2
    subjects = load_subjects(args.subjects)
    mon = subject_map(subjects)
    feedc = FollowFeedClient(cookies, min_interval=args.throttle)
    info = feedc.login_info()
    if not info.get("ok"):
        say("[失败] 登录态无效：", info)
        return 2
    people = BiliClient(sessdata=cookies.sessdata, min_interval=args.throttle)

    started = time.time()
    cycles = max(1, int(round(args.minutes * 60.0 / args.interval)))
    feed_seen: dict[str, dict[str, Any]] = {}
    people_seen: dict[str, dict[str, Any]] = {}
    feed_polls = 0
    people_rounds = 0
    say("监控 %d 分钟：每 %ds 取一次关注流（%d 页），每 %d 轮做一次逐人取数（%d 人 × %d 条）"
        % (args.minutes, args.interval, args.pages, args.people_every,
           len(subjects), args.per_user))
    say("请求最小间隔 %.1fs；Ctrl+C 可提前结束并出报告" % args.throttle)
    try:
        for c in range(1, cycles + 1):
            t_poll = time.time()
            try:
                r = collect_feed_round(feedc, pages=args.pages)
                feed_polls += 1
                for e in r["entries"]:
                    feed_seen.setdefault(e.item.id_str, {
                        "id": e.item.id_str, "uid": e.uid,
                        "pub_ts": int(e.item.pub_ts), "uname": e.item.uname,
                        "first_seen": t_poll})
                new_n = sum(1 for x in feed_seen.values() if x["first_seen"] == t_poll)
                say("[%2d/%d] 流 %3d 条（+%d）%d ms risk=%d"
                    % (c, cycles, len(r["entries"]), new_n, r["ms"], r["risk"]))
            except BiliError as e:
                say("[%2d/%d] 流失败 code=%s %s" % (c, cycles, e.code, e.message))

            if (c - 1) % max(1, args.people_every) == 0 or c == cycles:
                t_people = time.time()
                snap = people_snapshot(people, subjects, args.per_user)
                people_rounds += 1
                for uid, lst in snap["items"].items():
                    for x in lst:
                        people_seen.setdefault(x["id"], {**x, "uid": int(uid), "first_seen": t_people})
                say("        逐人：%d ms，空 %d，错误 %d，累计人员条目 %d"
                    % (snap["stats"]["ms"], snap["stats"]["empty"],
                       snap["stats"]["errors"], len(people_seen)))

            if c < cycles:
                nxt = started + c * args.interval
                time.sleep(max(0.0, nxt - time.time()))
    except KeyboardInterrupt:
        say("\n[中断] 按当前数据出报告")

    _monitor_report(args, started, feed_seen, people_seen, mon,
                    feed_polls, people_rounds, cycles)
    return 0


def _monitor_report(args, started, feed_seen, people_seen, mon,
                    feed_polls, people_rounds, cycles) -> None:
    elapsed = time.time() - started
    rows = []
    for id_, p in people_seen.items():
        f = feed_seen.get(id_)
        rows.append({
            "id": id_, "uid": p["uid"], "name": mon.get(p["uid"], p.get("uname", "")),
            "pub_ts": p["pub_ts"],
            "t_people": p["first_seen"],
            "t_feed": f["first_seen"] if f else None,
            "lag": (f["first_seen"] - p["first_seen"]) if f else None,
        })
    # 只看监控开始后才发布的：之前发布的，两边都是「第一次看到」，延迟无意义
    fresh = [r for r in rows if r["pub_ts"] >= started - 60]
    lags = [r["lag"] for r in fresh if r["lag"] is not None]
    pub_delays = [r["t_feed"] - r["pub_ts"] for r in fresh if r["t_feed"]]
    matched = sum(1 for r in fresh if r["t_feed"] is not None)
    missing = [r for r in fresh if r["t_feed"] is None]
    feed_only = [x for x in feed_seen.values()
                 if int(x["uid"]) in mon and x["pub_ts"] >= started - 60
                 and x["id"] not in people_seen]

    say("\n=== 结果（%.1f 分钟，%d 轮流 / %d 轮逐人）===" % (elapsed / 60.0, feed_polls, people_rounds))
    say("关注流累计 %d 条（%d 个 UP）；逐人累计 %d 条；监控期内新发布 %d 条"
        % (len(feed_seen), len({x["uid"] for x in feed_seen.values()}),
           len(people_seen), len(fresh)))
    if fresh:
        say("新动态被关注流覆盖：%d/%d（%.1f%%）；延迟 p50=%ds p95=%ds max=%ds"
            % (matched, len(fresh), 100.0 * matched / len(fresh),
               percentile(lags, 50), percentile(lags, 95), max(lags) if lags else 0))
    if pub_delays:
        say("发布→流里看到：p50=%ds p95=%ds max=%ds（含轮询粒度）"
            % (percentile(pub_delays, 50), percentile(pub_delays, 95), max(pub_delays)))
    say("流里出现、逐人这轮没取到（名单内）：%d 条 / %d 人"
        % (len(feed_only), len({x["uid"] for x in feed_only})))
    for r in missing[:10]:
        say("  漏：%-10s %s  %s" % (r["name"][:10], hhmmss(r["pub_ts"]), r["id"]))
    for r in sorted([x for x in rows if x["lag"] is not None],
                    key=lambda x: x["lag"], reverse=True)[:5]:
        say("  慢：%-10s 逐人 %s → 流 %s（%+ds）"
            % (r["name"][:10], hhmmss(r["t_people"]), hhmmss(r["t_feed"]), r["lag"]))

    RUNS.mkdir(parents=True, exist_ok=True)
    p = RUNS / ("monitor_%s.json" % datetime.now().strftime("%Y%m%d-%H%M%S"))
    p.write_text(json.dumps({
        "kind": "follow_feed_monitor", "version": 1,
        "started_at": started, "elapsed_s": int(elapsed),
        "cycles": cycles, "feed_polls": feed_polls, "people_rounds": people_rounds,
        "feed_seen": feed_seen, "people_seen": people_seen,
        "fresh_ids": [r["id"] for r in fresh],
        "summary": {"fresh": len(fresh), "matched": matched,
                    "missing": len(missing),
                    "lag_p50": percentile(lags, 50), "lag_p95": percentile(lags, 95),
                    "pub_delay_p50": percentile(pub_delays, 50),
                    "pub_delay_p95": percentile(pub_delays, 95),
                    "pub_delay_max": max(pub_delays) if pub_delays else None,
                    "feed_only": len(feed_only)},
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    say("报告：", p)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="关注流测试路径探针")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    sub = ap.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--env", default="", help="凭据文件（默认 config/bili_follow.env）")
    common.add_argument("--subjects", default="config/subjects.json", help="名单文件")
    common.add_argument("--interval", type=float, default=60.0, help="监控循环周期（秒，仅 monitor 用）")
    common.add_argument("--throttle", type=float, default=1.2, help="单次请求最小间隔（秒）")

    p = sub.add_parser("status", parents=[common], help="凭据/登录/关注数/流探测")
    p.add_argument("--csrf", action="store_true", help="用关注自己验证 bili_jct（一次写请求）")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("feed", parents=[common], help="取一轮关注流")
    p.add_argument("--pages", type=int, default=1)
    p.add_argument("--show", type=int, default=20)
    p.set_defaults(func=cmd_feed)

    p = sub.add_parser("follow", parents=[common], help="关注名单里未关注的对象")
    p.add_argument("--limit", type=int, default=0, help="最多关注几个（0=全部）")
    p.add_argument("--only", default="", help="只关注这些 uid（逗号/空格分隔）")
    p.add_argument("--journal", default="", help="journal 路径")
    p.add_argument("--gap", type=float, default=1.5, help="两次关注之间额外等待（秒）")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_follow)

    p = sub.add_parser("restore", parents=[common], help="按 journal 取关测试关注")
    p.add_argument("--journal", default="")
    p.add_argument("--gap", type=float, default=1.5)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_restore)

    p = sub.add_parser("snapshot", parents=[common], help="逐人+关注流快照")
    p.add_argument("--per-user", type=int, default=8)
    p.add_argument("--pages", type=int, default=1)
    p.add_argument("--no-compare", action="store_true")
    p.set_defaults(func=cmd_snapshot)

    p = sub.add_parser("compare", parents=[common], help="离线复算召回")
    p.add_argument("--file", default="", help="round_*.json，默认取最新")
    p.add_argument("--since-hours", type=float, default=0)
    p.set_defaults(func=cmd_compare)

    p = sub.add_parser("monitor", parents=[common], help="持续对比新动态到达时间")
    p.add_argument("--minutes", type=float, default=32.0)
    p.add_argument("--people-every", type=int, default=4, help="每几轮做一次逐人取数")
    p.add_argument("--per-user", type=int, default=6)
    p.add_argument("--pages", type=int, default=1)
    p.set_defaults(func=cmd_monitor)
    return ap


def main(argv: list[str] | None = None) -> int:
    global JSON_MODE
    ap = build_parser()
    args = ap.parse_args(argv)
    JSON_MODE = bool(getattr(args, "json", False))
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
