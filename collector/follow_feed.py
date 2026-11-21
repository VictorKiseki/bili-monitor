"""关注流取数（测试路径，2026-11-05）。

假设：B 站动态首页 /x/polymer/web-dynamic/v1/feed/all 按时间聚合「已关注」UP
的动态，一次请求就能拿到多个 UP 的最新更新。若监控账号把名单全部关注，就有
机会用 1 个请求代替 27 个 feed/space 请求。

边界（刻意隔离）：
- 不改 collector/bili.py 的取数逻辑，也不改 core/watcher.py 的轮询；
- 关注/取流凭据单独放 config/bili_follow.env（SESSDATA + bili_jct），
  与现有 config/bili.env 只读凭据分开；
- 关注是写操作，必须显式跑 scripts/probe_follow_feed.py follow，
  并用 journal 记录「这条测试路径自己关注了谁」，可一键 restore。

本模块只提供：cookie 读写、关注/取关、关注流取数、对比统计。
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

import httpx

from collector.bili import (
    API,
    BiliClient,
    BiliError,
    DynItem,
    RISK_CODES,
    backoff_seconds,
    load_sessdata,
    mask_sessdata,
    parse_dynamic,
)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV = "config/bili_follow.env"
DEFAULT_SUBJECTS = "config/subjects.json"

FEED_ALL = f"{API}/x/polymer/web-dynamic/v1/feed/all"
RELATION = f"{API}/x/relation"
RELATION_MODIFY = f"{API}/x/relation/modify"
RELATION_FOLLOWINGS = f"{API}/x/relation/followings"

# /x/relation/modify 的常见失败码（实测 + 社区已知）
FOLLOW_HINTS = {
    -101: "未登录：SESSDATA 失效，重新扫码",
    -111: "CSRF 校验失败：bili_jct 缺失或过期，重新扫码",
    -352: "风控软校验（-352）：等几秒重试",
    -412: "请求被拦截（-412）：停手，别连续重试",
    22001: "关注失败：可能达到关注上限",
    22002: "关注失败：账号异常或被限制",
    22003: "不能关注自己（csrf 正常）",
    22014: "目标账号在黑名单里",
}


def error_hint(code: int) -> str:
    return FOLLOW_HINTS.get(int(code), "")


# ---------- 凭据 ----------
@dataclass
class FollowCookies:
    sessdata: str = ""
    bili_jct: str = ""
    dede_user_id: str = ""
    source: str = ""

    @property
    def can_read(self) -> bool:
        return bool(self.sessdata)

    @property
    def can_follow(self) -> bool:
        return bool(self.sessdata and self.bili_jct)

    @property
    def masked(self) -> str:
        return mask_sessdata(self.sessdata)


def parse_cookie_string(text: str) -> dict[str, str]:
    """解析浏览器 document.cookie 形式的整串，也兼容 KEY=VALUE。"""
    out: dict[str, str] = {}
    for part in (text or "").split(";"):
        name, sep, val = part.partition("=")
        if not sep:
            continue
        name, val = name.strip(), val.strip()
        if name and val:
            out[name] = val
    return out


def _read_env_file(p: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        text = p.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and val:
            out[key] = val
    return out


def load_follow_cookies(path: str | Path | None = None) -> FollowCookies:
    """按 环境变量 -> 指定文件 -> config/bili_follow.env -> config/bili.env(SESSDATA) 读取。

    bili_jct 只能来自扫码登录，现有 config/bili.env 只有 SESSDATA，所以
    没跑过 scripts/follow_feed_login.py 时 can_follow 为 False。
    """
    vals: dict[str, str] = {}
    source = ""

    raw = os.environ.get("BILI_FOLLOW_COOKIE", "").strip()
    if raw:
        vals.update(parse_cookie_string(raw))
        source = "env:BILI_FOLLOW_COOKIE"
    for env, key in (("BILI_FOLLOW_SESSDATA", "SESSDATA"),
                     ("BILI_JCT", "bili_jct"),
                     ("BILI_FOLLOW_DEDEUSERID", "DedeUserID"),
                     ("BILI_SESSDATA", "SESSDATA")):
        v = os.environ.get(env, "").strip()
        if v:
            vals.setdefault(key, v)
            source = source or f"env:{env}"

    p = Path(path) if path else Path(os.environ.get("BILI_FOLLOW_ENV_FILE", str(ROOT / DEFAULT_ENV)))
    if not p.is_absolute():
        p = ROOT / p
    if p.is_file():
        for k, v in _read_env_file(p).items():
            ku = k.upper()
            if ku == "SESSDATA":
                vals.setdefault("SESSDATA", v)
            elif ku == "BILI_JCT":
                vals.setdefault("bili_jct", v)
            elif ku == "DEDEUSERID":
                vals.setdefault("DedeUserID", v)
        source = source or str(p)

    if not vals.get("SESSDATA"):
        s = load_sessdata()
        if s:
            vals["SESSDATA"] = s
            source = (source + "+" if source else "") + "config/bili.env(SESSDATA)"

    return FollowCookies(
        sessdata=vals.get("SESSDATA", ""),
        bili_jct=vals.get("bili_jct", ""),
        dede_user_id=vals.get("DedeUserID", ""),
        source=source,
    )


def save_follow_cookies(path: str | Path, cookies: FollowCookies,
                        uname: str = "", mid: int = 0) -> Path:
    p = Path(path)
    if not p.is_absolute():
        p = ROOT / p
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# B 站关注流测试路径凭据（独立于 config/bili.env，勿提交/外发）",
        "# 由 scripts/follow_feed_login.py 扫码写入；bili_jct 是写操作（关注/取关）的 CSRF token。",
        "# 保存于 %s  账号 %s mid=%s" % (time.strftime("%Y-%m-%d %H:%M:%S"), uname or "-", mid or 0),
        "SESSDATA=" + cookies.sessdata,
        "bili_jct=" + cookies.bili_jct,
        "DedeUserID=" + (str(cookies.dede_user_id) or str(mid or "")),
    ]
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, p)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return p


# ---------- 关注流客户端 ----------
@dataclass
class FeedEntry:
    uid: int
    item: DynItem
    index: int = 0

    def to_dict(self, first_seen: int | float = 0) -> dict[str, Any]:
        it = self.item
        d: dict[str, Any] = {
            "id": it.id_str,
            "uid": int(self.uid),
            "type": it.type,
            "subtype": it.subtype,
            "pub_ts": int(it.pub_ts),
            "uname": it.uname,
            "title": it.title,
            "url": it.url,
        }
        if first_seen:
            d["first_seen"] = int(first_seen)
        return d


class FollowFeedClient(BiliClient):
    """在 BiliClient 之上加：bili_jct、关注关系、关注流。

    复用 BiliClient 的节流 / WBI 签名 / buvid3 / _norm_item / parse_dynamic，
    但不改它的任何取数路径。
    """

    def __init__(self, cookies: FollowCookies | None = None, *,
                 timeout: float = 15.0, min_interval: float = 1.2,
                 retries: int = 2):
        self.follow_cookies = cookies or load_follow_cookies()
        super().__init__(timeout=timeout, min_interval=min_interval,
                         retries=retries, sessdata=self.follow_cookies.sessdata or "")
        self.bili_jct = self.follow_cookies.bili_jct
        self.dede_user_id = self.follow_cookies.dede_user_id
        self.mid = 0
        self.uname = ""
        self.last_feed_bytes = 0
        self.last_feed_ms = 0
        self._apply_cookies()

    def _apply_cookies(self) -> None:
        # H6（2026-11-14）：父类同名方法有锁，这里以前没有。虽然 _cookie_tried 门闩
        # 让「建好后重写」只发生一次，但 httpx 的 headers 不是线程安全的，写一个
        # 被另一个线程读到半条 Cookie 就等于请求带着残缺凭据。RLock 可重入，
        # 不影响 _ensure_buvid3 持锁调用本方法。
        with self._lock:
            parts = []
            if self._buvid3:
                parts.append(f"buvid3={self._buvid3}")
            if self.sessdata:
                parts.append(f"SESSDATA={self.sessdata}")
            if self.bili_jct:
                parts.append(f"bili_jct={self.bili_jct}")
            if self.dede_user_id:
                parts.append(f"DedeUserID={self.dede_user_id}")
            if parts:
                self.http.headers["Cookie"] = "; ".join(parts)

    # ---- 登录与关系 ----
    def login_info(self) -> dict[str, Any]:
        info = self.check_login()
        info["has_jct"] = bool(self.bili_jct)
        info["can_follow"] = bool(info.get("ok") and self.bili_jct)
        if info.get("ok"):
            self.mid = int(info.get("mid") or 0)
            self.uname = info.get("uname") or ""
        return info

    def followings(self) -> tuple[list[dict[str, Any]], int]:
        """当前账号的关注列表（分页）。"""
        if not self.mid:
            self.login_info()
        out: list[dict[str, Any]] = []
        total = 0
        pn = 1
        while pn <= 40:
            body = self._get(RELATION_FOLLOWINGS,
                             {"vmid": self.mid, "pn": pn, "ps": 50,
                              "order": "desc", "order_type": "attention"})
            data = body.get("data") or {}
            total = int(data.get("total") or 0)
            lst = data.get("list") or []
            out.extend(x for x in lst if isinstance(x, dict))
            if not lst or len(out) >= total:
                break
            pn += 1
        return out, total

    def relation_attr(self, fid: int) -> dict[str, Any]:
        """0 未关注 / 2 已关注 / 6 互关 / 128 拉黑（查不到时 attr=-1）。"""
        try:
            body = self._get(RELATION, {"fid": int(fid)})
        except BiliError as e:
            return {"fid": int(fid), "attribute": -1, "code": e.code, "message": e.message}
        data = body.get("data") or {}
        return {"fid": int(fid), "attribute": int(data.get("attribute") or 0),
                "code": int(body.get("code") or 0), "message": body.get("message") or ""}

    def _post(self, url: str, data: dict[str, Any], *,
              referer: str = "https://www.bilibili.com/",
              attempts: int | None = None) -> dict[str, Any]:
        self._ensure_buvid3()
        rounds = self.retries + 1 if attempts is None else max(1, int(attempts))
        last: BiliError | None = None
        for attempt in range(rounds):
            self._throttle()
            try:
                r = self.http.post(url, data=data,
                                   headers={"Referer": referer,
                                            "Origin": "https://space.bilibili.com"})
            except httpx.HTTPError as e:
                last = BiliError(-1, "网络错误 %s" % e.__class__.__name__, url, kind="net")
                time.sleep(backoff_seconds("net", attempt))
                continue
            self._bump_request()          # S5：计数也走父类的加锁入口
            if r.status_code != 200:
                if r.status_code in (412, 429):
                    last = BiliError(-r.status_code, "HTTP %d" % r.status_code, url, kind="http")
                    time.sleep(backoff_seconds("http", attempt))
                    continue
                raise BiliError(-r.status_code, "HTTP %d" % r.status_code, url, kind="http")
            try:
                body = r.json()
            except ValueError:
                raise BiliError(-1, "返回不是 JSON", url)
            code = int(body.get("code") or 0)
            if code == 0:
                return body
            last = BiliError(code, body.get("message") or "", url)
            if code in RISK_CODES:
                time.sleep(backoff_seconds("biz", attempt, code))
                continue
            raise last
        raise last or BiliError(-1, "未知错误", url)

    def _modify(self, uid: int, act: int, re_src: int = 11,
                attempts: int = 3) -> dict[str, Any]:
        res: dict[str, Any] = {"uid": int(uid), "act": int(act), "ok": False,
                               "code": 0, "message": "", "hint": ""}
        if not self.bili_jct:
            res.update(code=-111, message="缺少 bili_jct",
                       hint=error_hint(-111))
            return res
        if not self.mid:
            self.login_info()
        payload = {"fid": int(uid), "act": int(act), "re_src": int(re_src),
                   "csrf": self.bili_jct}
        try:
            body = self._post(RELATION_MODIFY, payload,
                              referer="https://space.bilibili.com/%d" % int(uid),
                              attempts=attempts)
            code = int(body.get("code") or 0)
            res.update(ok=code == 0, code=code,
                       message=str(body.get("message") or ""),
                       hint="" if code == 0 else error_hint(code))
        except BiliError as e:
            res.update(code=e.code, message=e.message, hint=error_hint(e.code))
        return res

    def follow(self, uid: int) -> dict[str, Any]:
        return self._modify(uid, 1)

    def unfollow(self, uid: int) -> dict[str, Any]:
        return self._modify(uid, 2)

    def check_csrf(self) -> dict[str, Any]:
        """用「关注自己」验证 bili_jct：B 站会拒绝（22003），但 csrf 校验先执行。

        不会真的关注成功；万一返回 0（理论上不可能），立刻取关。
        """
        if not (self.mid and self.bili_jct):
            return {"ok": False, "code": -111, "message": "缺少 mid 或 bili_jct"}
        r = self._modify(self.mid, 1, attempts=1)
        if r.get("ok"):
            self._modify(self.mid, 2, attempts=1)
        return {"ok": int(r.get("code") or 0) not in (-111, -101),
                "code": r.get("code"), "message": r.get("message")}

    # ---- 关注流 ----
    def feed_page(self, offset: str = "", type_: str = "all",
                  page: int = 1, attempts: int = 3) -> dict[str, Any]:
        self._ensure_buvid3()
        params: dict[str, Any] = {"type": type_, "page": page,
                                  "features": "itemOpusStyle",
                                  "timezone_offset": -480}
        if offset:
            params["offset"] = offset
        last: BiliError | None = None
        for attempt in range(max(1, int(attempts))):
            self._throttle()
            q = self._sign(params)
            t0 = time.time()
            try:
                r = self.http.get(FEED_ALL, params=q)
            except httpx.HTTPError as e:
                last = BiliError(-1, "网络错误 %s" % e.__class__.__name__, FEED_ALL, kind="net")
                time.sleep(backoff_seconds("net", attempt))
                continue
            self._bump_request()
            self.last_feed_bytes = len(r.content)
            self.last_feed_ms = int((time.time() - t0) * 1000)
            if r.status_code != 200:
                if r.status_code in (412, 429):
                    last = BiliError(-r.status_code, "HTTP %d" % r.status_code, FEED_ALL, kind="http")
                    time.sleep(backoff_seconds("http", attempt))
                    continue
                raise BiliError(-r.status_code, "HTTP %d" % r.status_code, FEED_ALL, kind="http")
            try:
                body = r.json()
            except ValueError:
                raise BiliError(-1, "返回不是 JSON", FEED_ALL)
            code = int(body.get("code") or 0)
            if code == 0:
                return body
            last = BiliError(code, body.get("message") or "", FEED_ALL)
            if code in RISK_CODES:
                with self._lock:          # S5：诊断计数与父类共用一把锁，避免丢更新
                    self.last_risk += 1
                time.sleep(backoff_seconds("biz", attempt, code))
                continue
            raise last
        raise last or BiliError(-1, "未知错误", FEED_ALL)


# ---------- 解析与统计 ----------
def parse_feed_entries(raw_items: Iterable[dict[str, Any]],
                       drop_live_rcmd: bool = True) -> list[FeedEntry]:
    out: list[FeedEntry] = []
    for idx, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            continue
        try:
            it = BiliClient._norm_item(raw)
            item = parse_dynamic(it)
        except Exception:                       # noqa: BLE001  单条坏数据不带崩整轮
            continue
        mods = it.get("modules") or {}
        au = mods.get("module_author") or {}
        try:
            uid = int(au.get("mid") or 0)
        except (TypeError, ValueError):
            uid = 0
        if drop_live_rcmd and item.type == "DYNAMIC_TYPE_LIVE_RCMD":
            continue
        out.append(FeedEntry(uid=uid, item=item, index=idx))
    return out


def dedupe_entries(entries: Iterable[FeedEntry]) -> list[FeedEntry]:
    seen: set[str] = set()
    out: list[FeedEntry] = []
    for e in entries:
        key = e.item.id_str
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(e)
    return out


def should_fetch_next(has_more: bool, oldest_ts: int, since_ts: int,
                      pages_done: int, max_pages: int) -> bool:
    """多页取流的停止条件：翻到头 / 到页数上限 / 已经翻过时间窗。"""
    if not has_more or pages_done >= max(1, int(max_pages)):
        return False
    if not oldest_ts:
        return False
    if since_ts and int(oldest_ts) <= int(since_ts):
        return False
    return True


def collect_feed_round(client: FollowFeedClient, *, pages: int = 1,
                       since_ts: int = 0,
                       drop_live_rcmd: bool = True) -> dict[str, Any]:
    """一次「关注流轮询」：1 页或多页，返回这一轮的条目与指标。"""
    started = time.time()
    risk0 = int(getattr(client, "last_risk", 0))
    entries: list[FeedEntry] = []
    offset = ""
    has_more = False
    pages_done = 0
    update_num = 0
    raw_total = 0
    pages_info: list[dict[str, Any]] = []
    while pages_done < max(1, int(pages)):
        body = client.feed_page(offset)
        data = body.get("data") or {}
        raw = data.get("items") or []
        raw_total += len(raw)
        entries.extend(parse_feed_entries(raw, drop_live_rcmd=drop_live_rcmd))
        offset = str(data.get("offset") or "")
        has_more = bool(data.get("has_more"))
        update_num = int(data.get("update_num") or 0)
        pages_done += 1
        oldest = min((e.item.pub_ts for e in entries if e.item.pub_ts), default=0)
        pages_info.append({"n": pages_done, "raw": len(raw), "oldest": oldest,
                           "has_more": has_more})
        if not should_fetch_next(has_more, oldest, since_ts, pages_done, pages):
            break
        if not raw:
            break
    entries = dedupe_entries(entries)
    ts = [e.item.pub_ts for e in entries if e.item.pub_ts]
    return {
        "entries": entries,
        "offset": offset,
        "has_more": has_more,
        "pages": pages_done,
        "update_num": update_num,
        "raw_total": raw_total,
        "oldest_ts": min(ts) if ts else 0,
        "newest_ts": max(ts) if ts else 0,
        "span_s": (max(ts) - min(ts)) if ts else 0,
        "ms": int((time.time() - started) * 1000),
        "bytes": int(getattr(client, "last_feed_bytes", 0)),
        "risk": int(getattr(client, "last_risk", 0)) - risk0,
        "empty": not entries,
        "pages_info": pages_info,
    }


def percentile(values: Sequence[int | float], p: float) -> int:
    vals = sorted(float(v) for v in values)
    if not vals:
        return 0
    k = (len(vals) - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(vals) - 1)
    return int(vals[lo] + (vals[hi] - vals[lo]) * (k - lo))


def group_entries_by_uid(entries: Iterable[FeedEntry]) -> dict[int, list[FeedEntry]]:
    out: dict[int, list[FeedEntry]] = {}
    for e in entries:
        out.setdefault(int(e.uid), []).append(e)
    return out


def coverage_report(per_user: dict[int, list[dict[str, Any]]],
                    feed_items: Iterable[dict[str, Any]],
                    monitored: dict[int, str],
                    since_ts: int = 0) -> dict[str, Any]:
    """对比「逐人 feed/space」与「关注流 feed/all」的召回。

    per_user: {uid: [条目 dict]}，条目至少含 id / pub_ts；
    feed_items: 关注流累计条目（可带 first_seen）；
    since_ts: 只比较这个时间之后的条目（0 = 全部）。
    """
    feed_list = [x for x in feed_items if x.get("id")]
    feed_by_id = {str(x["id"]): x for x in feed_list}
    feed_uids = {int(x.get("uid") or 0) for x in feed_list}

    rows: dict[int, dict[str, Any]] = {}
    total_truth = total_matched = 0
    users_hit = 0
    for uid, name in monitored.items():
        truth = [x for x in per_user.get(uid, [])
                 if int(x.get("pub_ts") or 0) >= int(since_ts or 0)]
        matched = [x for x in truth if str(x.get("id")) in feed_by_id]
        missing = [x for x in truth if str(x.get("id")) not in feed_by_id]
        feed_n = sum(1 for x in feed_list if int(x.get("uid") or 0) == int(uid))
        total_truth += len(truth)
        total_matched += len(matched)
        if matched or feed_n:
            users_hit += 1
        rows[int(uid)] = {
            "uid": int(uid), "name": name,
            "snapshot": len(truth), "matched": len(matched),
            "missing": len(missing), "feed": feed_n,
            "missing_ids": [str(x.get("id")) for x in missing],
        }

    snapshot_ids = {str(x.get("id")) for items in per_user.values() for x in items}
    feed_only = [x for x in feed_list
                 if int(x.get("uid") or 0) in monitored
                 and str(x.get("id")) not in snapshot_ids]
    foreign = [x for x in feed_list if int(x.get("uid") or 0) not in monitored]
    lags = [int(x["first_seen"]) - int(x.get("pub_ts") or 0)
            for x in feed_list
            if x.get("first_seen") and x.get("pub_ts")
            and int(x["first_seen"]) >= int(x.get("pub_ts") or 0)]
    return {
        "since_ts": int(since_ts or 0),
        "users": rows,
        "overall": {
            "monitored": len(monitored),
            "users_with_feed": users_hit,
            "snapshot_items": total_truth,
            "matched": total_matched,
            "missing": total_truth - total_matched,
            "recall_pct": round(100.0 * total_matched / total_truth, 1) if total_truth else None,
            "feed_items": len(feed_list),
            "feed_only": len(feed_only),
            "foreign": len(foreign),
            "lag_p50_s": percentile(lags, 50),
            "lag_p95_s": percentile(lags, 95),
        },
        "feed_only_ids": [str(x.get("id")) for x in feed_only],
        "foreign_uids": sorted({int(x.get("uid") or 0) for x in foreign}),
        "lag_samples": len(lags),
    }


# ---------- 名单与 journal ----------
def load_subjects(path: str | Path = DEFAULT_SUBJECTS) -> list[dict[str, Any]]:
    p = Path(path)
    if not p.is_absolute():
        p = ROOT / p
    data = json.loads(p.read_text(encoding="utf-8"))
    if isinstance(data, list):
        data = {"subjects": data}
    elif not isinstance(data, dict):
        data = {}
    out: list[dict[str, Any]] = []
    for s in data.get("subjects") or []:
        if not isinstance(s, dict):
            continue
        try:
            uid = int(s.get("uid") or 0)
        except (TypeError, ValueError):
            continue
        if not uid:
            continue
        out.append({"uid": uid, "name": str(s.get("name") or ""),
                    "room_id": int(s.get("room_id") or 0)})
    return out


def subject_map(subjects: Iterable[dict[str, Any]]) -> dict[int, str]:
    return {int(s["uid"]): str(s.get("name") or "") for s in subjects}


def follow_plan(subjects: Iterable[dict[str, Any]], following_uids: Iterable[int],
                only_uids: Iterable[int] | None = None,
                max_n: int = 0) -> list[dict[str, Any]]:
    have = {int(x) for x in following_uids}
    only = {int(x) for x in only_uids} if only_uids else None
    out = [s for s in subjects
           if int(s["uid"]) not in have and (only is None or int(s["uid"]) in only)]
    if max_n and max_n > 0:
        out = out[:max_n]
    return out


def new_journal(mid: int, uname: str) -> dict[str, Any]:
    return {
        "version": 1,
        "started_at": time.time(),
        "account": {"mid": int(mid), "uname": uname},
        "test_followed": [],
        "already_followed": [],
        "failed": {},
        "results": [],
        "restored_at": None,
    }


def load_journal(path: str | Path) -> dict[str, Any] | None:
    p = Path(path)
    if not p.is_absolute():
        p = ROOT / p
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save_journal(path: str | Path, data: dict[str, Any]) -> Path:
    p = Path(path)
    if not p.is_absolute():
        p = ROOT / p
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)
    return p
