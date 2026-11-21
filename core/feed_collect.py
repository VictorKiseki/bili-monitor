"""关注流采集（feed 模式）：一次取流拿回多号动态，替代「逐人轮询」。

为什么改：29 个号逐人轮询一轮 29 次请求 / 30 秒上下；关注流一次请求就是 20 条、
稳态 200ms 左右（实测见 docs/22-关注流聚合可行性实测.md）。
代价：关注面大 / 高峰期聚合流会吞条目（101 个关注时实测漏 16.4%），
所以主路径走关注流，另有低频逐人补漏（core/watcher.py 的 sweep）。

本模块只放纯函数：模式判定、取流翻页、关注同步。状态机、去重、推送仍归 Watcher。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable, Iterable

from collector.follow_feed import (FollowCookies, dedupe_entries, follow_plan,
                                   load_follow_cookies, parse_feed_entries)

MODE_AUTO = "auto"
MODE_FEED = "feed"
MODE_PER_USER = "per_user"

_MODE_ALIASES = {
    "": MODE_AUTO,
    "auto": MODE_AUTO,
    "feed": MODE_FEED,
    "follow": MODE_FEED,
    "follow_feed": MODE_FEED,
    "per_user": MODE_PER_USER,
    "peruser": MODE_PER_USER,
    "poll": MODE_PER_USER,
    "legacy": MODE_PER_USER,
    "逐人": MODE_PER_USER,
    "逐人轮询": MODE_PER_USER,
    "关注流": MODE_FEED,
}


def normalize_mode(value: Any) -> str:
    """把配置里的写法归一成 auto / feed / per_user。"""
    key = str(value or "").strip().lower()
    return _MODE_ALIASES.get(key, MODE_AUTO)


def resolve_mode(configured: Any = MODE_AUTO,
                 cookies: FollowCookies | None = None) -> str:
    """auto 的判定规则：能读关注流且能写关注关系（有 bili_jct）才用 feed。

    关注流端点未登录返回的是推荐流，所以 SESSDATA 与 bili_jct 缺一不可。
    显式写 feed 时也走同一判定，由调用方把「回退原因」写给用户看。
    """
    mode = normalize_mode(configured)
    if mode == MODE_PER_USER:
        return MODE_PER_USER
    c = cookies if cookies is not None else load_follow_cookies()
    return MODE_FEED if (c.can_read and c.can_follow) else MODE_PER_USER


def fallback_reason(cookies: FollowCookies | None = None) -> str:
    c = cookies if cookies is not None else load_follow_cookies()
    if not c.can_read:
        return "缺少 SESSDATA（先扫码登录 B 站）"
    if not c.can_follow:
        return "缺少 bili_jct（先运行 scripts/follow_feed_login.py 或到管理台扫码）"
    return ""


def feed_round_once(client, *, max_pages: int = 3, burst_threshold: int = 15,
                    burst_pages: int = 3, since_ts: int = 0,
                    known_ids: Iterable[str] = ()) -> dict[str, Any]:
    """取一轮关注流（1..N 页）。

    翻页停止条件按「见到上一轮的已知 id」判定，而不是只看时间窗：
    置顶旧动态的 pub_ts 很旧，会把 min(pub_ts) 立刻拉到时间窗之前，
    只看时间窗会在第 1 页就误停、漏掉同一时刻的新条目。
    时间窗（since_ts）只在翻到「比上一轮更旧」的整页时兜底。
    """
    known = {str(x) for x in (known_ids or ()) if x}
    limit = max(1, int(max_pages or 1))
    burst_limit = max(limit, int(burst_pages or limit))
    threshold = int(burst_threshold or 0)
    entries = []
    pages = 0
    raw_total = 0
    known_hit = False
    risk0 = int(getattr(client, "last_risk", 0) or 0)
    started = time.time()
    offset = ""
    while pages < limit:
        body = client.feed_page(offset, page=pages + 1)
        data = body.get("data") or {}
        raw = data.get("items") or []
        batch = parse_feed_entries(raw)
        raw_total += len(raw)
        pages += 1
        entries.extend(batch)
        if pages == 1 and threshold > 0 and len(batch) >= threshold:
            limit = burst_limit
        if known and any(e.item.id_str in known for e in batch):
            known_hit = True
            break
        offset = str(data.get("offset") or "")
        if not raw or not data.get("has_more"):
            break
        if since_ts and batch and min(int(e.item.pub_ts or 0) for e in batch) <= int(since_ts):
            break
    entries = dedupe_entries(entries)
    ts = [int(e.item.pub_ts or 0) for e in entries if e.item.pub_ts]
    return {
        "entries": entries,
        "pages": pages,
        "raw_total": raw_total,
        "newest_ts": max(ts) if ts else 0,
        "oldest_ts": min(ts) if ts else 0,
        "ms": int((time.time() - started) * 1000),
        "bytes": int(getattr(client, "last_feed_bytes", 0) or 0),
        "risk": int(getattr(client, "last_risk", 0) or 0) - risk0,
        "known_hit": known_hit,
        "empty": not entries,
    }


def entries_by_uid(entries: Iterable) -> dict[int, list]:
    out: dict[int, list] = {}
    for e in entries:
        out.setdefault(int(e.uid), []).append(e)
    return out


def follow_sync(client, subjects: Iterable, *, log: Callable[[str], None] = print,
                journal_dir: str | Path | None = None, gap_s: float = 2.0,
                max_n: int = 0, now: float | None = None) -> dict[str, Any]:
    """把监控名单里「还没关注」的号补关注（只关注，不取关）。

    取关会改变用户自己的关注列表，风险大且没必要：即使某个号被取关，
    补漏 sweep 仍然能逐人抓到。想清理关注请自己动手或跑
    scripts/probe_follow_feed.py restore。
    """
    jct = str(getattr(client, "bili_jct", "") or "")
    if not jct:
        log("[关注流] 没有 bili_jct，跳过关注同步（关注是写操作，必须有 CSRF token）")
        return {"ok": False, "reason": "no_jct", "planned": 0, "followed": 0, "failed": []}
    try:
        info = client.login_info()
        if not info.get("ok"):
            log("[关注流] 关注同步跳过：登录态无效 %s" % (info.get("message") or ""))
            return {"ok": False, "reason": "login", "planned": 0, "followed": 0, "failed": []}
        lst, total = client.followings()
    except Exception as e:                                  # noqa: BLE001
        log(f"[关注流] 取关注列表失败：{type(e).__name__}: {e}")
        return {"ok": False, "reason": "list", "planned": 0, "followed": 0, "failed": [],
                "error": f"{type(e).__name__}: {e}"}
    have = {int(x.get("mid") or 0) for x in lst if int(x.get("mid") or 0)}
    rows = [{"uid": int(getattr(s, "uid", 0) or 0),
             "name": str(getattr(s, "name", "") or "")} for s in subjects]
    rows = [r for r in rows if r["uid"]]
    plan = follow_plan(rows, have, max_n=max_n)
    started = time.time()
    followed: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for item in plan:
        try:
            res = client.follow(int(item["uid"]))
        except Exception as e:                              # noqa: BLE001
            res = {"ok": False, "code": -1, "message": f"{type(e).__name__}: {e}"}
        if res.get("ok"):
            followed.append(item)
        else:
            failed.append({**item, "code": res.get("code"), "message": res.get("message")})
        if gap_s:
            time.sleep(float(gap_s))
    result = {
        "ok": True,
        "following": len(have),
        "total": int(total or len(have)),
        "planned": len(plan),
        "followed": len(followed),
        "failed": failed,
        "ms": int((time.time() - started) * 1000),
    }
    if followed or failed:
        log("[关注流] 关注同步：原有 %d 个，本次新增 %d 个，失败 %d 个"
            % (len(have), len(followed), len(failed)))
    if journal_dir is not None and (followed or failed):
        try:
            write_journal(journal_dir, result, now=now)
        except Exception as e:                              # noqa: BLE001
            log(f"[关注流] 关注 journal 写入失败：{type(e).__name__}: {e}")
    return result


def write_journal(journal_dir: str | Path, result: dict[str, Any],
                  now: float | None = None) -> Path:
    """关注动作留痕，便于人工回滚（不自动取关）。"""
    d = Path(journal_dir)
    d.mkdir(parents=True, exist_ok=True)
    ts = int(now if now is not None else time.time())
    p = d / ("follow-sync-%s.json" % time.strftime("%Y%m%d-%H%M%S", time.localtime(ts)))
    payload = {"version": 1, "at": ts, **result}
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)
    return p
