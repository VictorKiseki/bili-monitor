"""B 站只读取数客户端（2026-10-27 实测修正版）。

重要修正：直播接口在 api.live.bilibili.com（不是 api.bilibili.com），
且不需要 WBI —— UID 解析与直播状态都能免签名拿到。

实测可用的 4 个接口：
1. api.live.bilibili.com/room/v1/Room/getRoomInfoOld?mid=UID    UID -> 直播间号 + 直播状态
2. api.live.bilibili.com/room/v1/Room/get_status_info_by_uids  **批量**取 30 个主播的直播状态
3. api.live.bilibili.com/room/v1/Room/get_info?room_id=        单个房间详情
4. api.bilibili.com/x/polymer/web-dynamic/v1/feed/space        动态流（需 WBI + buvid3）
   —— 视频投稿 / 专栏 也从这里派生，不单独调接口
"""
from __future__ import annotations

import hashlib
import os
import re
import threading
import time
import urllib.parse
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import httpx

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
API = "https://api.bilibili.com"
LIVE = "https://api.live.bilibili.com"
# 动态列表的两个端点：desktop 版实测不受 -352 软校验影响（2026-10-28）
DESKTOP_DYN = f"{API}/x/polymer/web-dynamic/desktop/v1/feed/space"
SPACE_DYN = f"{API}/x/polymer/web-dynamic/v1/feed/space"

MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]

RETRYABLE = {-352, -412, -509, -799}
# S5（2026-11-14）：账号级风控码。判「撞风控」必须同时看 kind —— HTTP 412
# 与业务码 -412 数值相同，以前只看 code，HTTP 412 被当成业务风控静默吞掉，
# 真正的账号级信号既不重试也不上报。follow_feed 也用这一份，别再各写一遍。
RISK_CODES = (-352, -412, -509, -799)

# S5（2026-11-14）：三处重试原来各写一套系数（bili 2/3/0.3、follow_feed 1.5/2/0.5、
# 取流 2/3/0.4），同一种「网络抖动」在三条路径上等的时间都不一样。收到一张表，
# 改一处三条路径同时生效。
BACKOFF_NET_S = 2.0        # 连接层失败：按轮次线性加长
BACKOFF_HTTP_S = 3.0       # HTTP 412/429：服务端限流，等久一点
BACKOFF_BIZ_S = 3.0        # 业务风控码（-412/-509/-799）
BACKOFF_SOFT_S = 0.3       # -352 是随机软校验，立刻重试


def backoff_seconds(kind: str, attempt: int, code: int = 0) -> float:
    """统一退避：kind ∈ {"net","http","biz"}，attempt 从 0 开始。"""
    step = max(1, int(attempt) + 1)
    if kind == "net":
        return BACKOFF_NET_S * step
    if kind == "http":
        return BACKOFF_HTTP_S * step
    return BACKOFF_SOFT_S if code == -352 else BACKOFF_BIZ_S * step
STATUS_TEXT = {0: "未开播", 1: "直播中", 2: "轮播(按未开播)"}

# 登录态（可选增强，2026-10-28）：带上 SESSDATA 后动态接口几乎不吃 -352，空响应也少。
# 凭据只从环境变量或本地文件读，不写进代码、不进日志。
SESSDATA_ENV = "BILI_SESSDATA"
SESSDATA_FILES = ("config/bili.env", "BILI.env", ".env")


def load_sessdata() -> str | None:
    """按 环境变量 -> config/bili.env -> BILI.env -> .env 的顺序找 SESSDATA。"""
    v = os.environ.get(SESSDATA_ENV, "").strip()
    if v:
        return v
    root = Path(__file__).resolve().parent.parent
    for name in SESSDATA_FILES:
        p = root / name
        try:
            if not p.is_file():
                continue
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            if key.strip().upper() in ("SESSDATA", "BILI_SESSDATA"):
                val = val.strip().strip('"').strip("'")
                if val:
                    return val
    return None


# S4 的脱敏规则已抽到 shared/redact.py（2026-11-14）：采集日志、web/ 与 livedeck/ 的
# 异常日志共用同一份实现（在此之前规则只存在于本文件，却没有任何调用点）。
from shared.redact import (  # noqa: E402 - 保持既有导入顺序
    FULL_REDACT as _FULL_REDACT,
    FULL_REDACT_KEY_TOKENS as _FULL_REDACT_KEY_TOKENS,
    SENSITIVE_KEY_TOKENS as _SENSITIVE_KEY_TOKENS,
    is_full_redact_key as _is_full_redact_key,
    is_sensitive_key as _is_sensitive_key,
    mask_secret as _mask_secret,
    mask_sessdata,
    scrub_text,
    scrubbed,
)

class BiliError(RuntimeError):
    """B 站接口错误。S5（2026-11-14）：kind 区分错误来源。

    · "biz"  —— 响应体里的业务码（body["code"]），-352/-412/-509/-799 才算撞风控；
    · "http" —— HTTP 状态码非 200，code 记 -status_code（-412/-429）；
    · "net"  —— 连接层失败，code 固定 -1。

    以前只有 code：「HTTP 412」与「业务 -412」共用一个命名空间，于是
    fetch_dynamics 把 HTTP 429 当风控静默吞掉、把 HTTP 412 当业务风控跳过，
    真正的账号级封禁信号既不重试也不上报。
    """

    def __init__(self, code: int, message: str, path: str, kind: str = "biz"):
        super().__init__(f"[{path}] code={code} message={message}")
        self.code = code
        self.message = message
        self.path = path
        self.kind = kind

    @property
    def is_risk(self) -> bool:
        """只有业务码才算「撞风控」；HTTP 412/429 属于账号级信号，必须往上抛。"""
        return self.kind == "biz" and self.code in RISK_CODES


@dataclass
class LiveState:
    uid: int
    room_id: int
    live_status: int
    title: str = ""
    uname: str = ""
    online: int = 0
    live_time: int | str = 0
    area_name: str = ""
    cover: str = ""
    face: str = ""

    @property
    def is_living(self) -> bool:
        return self.live_status == 1

    @property
    def status_text(self) -> str:
        return STATUS_TEXT.get(self.live_status, str(self.live_status))


@dataclass
class DynItem:
    id_str: str
    type: str
    pub_ts: int
    subtype: str = ""
    title: str = ""
    summary: str = ""
    url: str = ""
    image_count: int = 0
    cover: str = ""
    # 2026-11-03 周表识别：保留全部图片与尺寸（接口本来就给 width/height/size，
    # 之前只取 url 丢掉了，导致多图动态只留第一张、也没法按版式预筛）
    images: list[dict] = field(default_factory=list)
    face: str = ""
    uname: str = ""
    # --- 2026-10-28 v6：卡片需要完整正文（保留换行）、内联表情、转发原文、视频资料 ---
    text: str = ""
    rich: list = field(default_factory=list)
    fwd: dict = field(default_factory=dict)
    video: dict = field(default_factory=dict)
    # 2026-11-05 直播预约：动态上挂的 ADDITIONAL_TYPE_RESERVE 模块
    # （title / 时间 desc1 / 预约人数 desc2 / rid），见 core/weekly.parse_reserve
    reserve: dict = field(default_factory=dict)


def _to_ts(v: Any) -> int:
    """直播接口的 live_time 有时是时间戳、有时是 'YYYY-mm-dd HH:MM:SS' 字符串。"""
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str):
        s = v.strip()
        if s.isdigit():
            return int(s)
        try:
            return int(time.mktime(time.strptime(s, "%Y-%m-%d %H:%M:%S")))
        except ValueError:
            return 0
    return 0


class BiliClient:
    def __init__(self, timeout: float = 15.0, min_interval: float = 1.2,
                 retries: int = 2, sessdata: str | None = None):
        self.http = httpx.Client(
            timeout=timeout,
            headers={
                "User-Agent": UA,
                "Referer": "https://www.bilibili.com/",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9",
            },
        )
        self.min_interval = max(0.0, float(min_interval))
        self.retries = retries
        # H6：多个采集线程共用一个 BiliClient 时，下面这些状态都会被并发读写。
        # _lock 保护「时隙预留 + 统计」，用 RLock 是为了 _ensure_buvid3 已持锁时
        # 还能重入 _apply_cookies（见该函数注释）。
        self._lock = threading.RLock()
        self._last_call = 0.0            # 最近一次「占用」时隙的 monotonic 时刻
        self._next_slot = 0.0            # 下一个可用时隙（monotonic），由 _throttle 预留
        self._sent_marks: deque[float] = deque(maxlen=4096)  # 最近发车时刻，供 record_qps
        self._buvid3: str | None = None
        self._cookie_tried = False
        self._mixin_key: str | None = None
        self._mixin_date = ""
        self.request_count = 0
        self.last_endpoint = ""          # 最近一次动态请求走的端点（space / desktop / desktop-fallback）
        self.last_risk = 0               # 最近一次 fetch_dynamics 里撞了几次风控
        self.last_dropped_rcmd = 0
        # 登录态：sessdata 传空字符串表示「强制匿名」，传 None 表示「自动读凭据」
        self.sessdata = load_sessdata() if sessdata is None else (sessdata or None)
        self.logged_in = bool(self.sessdata)

    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> "BiliClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---------- 基础设施 ----------
    def _throttle(self) -> None:
        """H6：跨线程按「预留下一个时隙」限速，而不是读-判-写。

        旧实现 gap=now-_last_call → sleep → _last_call=now 在并发下会同时放行
        多个线程（QPS 翻倍），削弱 -352 保护。这里锁内只做「算出本次可发车
        时刻 slot 并把它占掉」，sleep 放到锁外，否则一个请求的等待会串行化
        所有线程。预留时隙而不是记录已发车时刻，能保证相邻请求间隔 >= min_interval。
        """
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next_slot)
            self._next_slot = slot + self.min_interval
            self._last_call = slot
            self._sent_marks.append(slot)     # QPS 统计与时隙在同一临界区，保证一致
        delay = slot - now
        if delay > 0:
            time.sleep(delay)

    def _bump_request(self) -> None:
        """请求计数自增；与 _throttle/record_qps 共用一把锁，避免 += 丢更新。"""
        with self._lock:
            self.request_count += 1

    def record_qps(self, window_s: float = 10.0) -> float:
        """最近 window_s 秒的实际发车速率（QPS），用于观测是否逼近 min_interval。

        统计窗口按「最早一条记录到现在」收敛，否则刚启动时用满 window 当分母
        会把速率算低（例如 40 次/0.8s 显示成 8 QPS）。
        """
        if window_s <= 0:
            return 0.0
        now = time.monotonic()
        with self._lock:
            recent = [t for t in self._sent_marks if now - t <= window_s]
        if not recent:
            return 0.0
        span = min(window_s, max(now - recent[0], 1e-3))
        return len(recent) / span

    def _apply_cookies(self) -> None:
        """把 buvid3 与 SESSDATA 组装成一个 Cookie 头。

        实测：httpx 的 cookie jar 带 domain 后不会自动带上，必须直接写 header。
        """
        # H6：多线程会同时进来，组装 + 赋值放在锁里，避免写出半条 Cookie。
        with self._lock:
            parts = []
            if self._buvid3:
                parts.append(f"buvid3={self._buvid3}")
            if self.sessdata:
                parts.append(f"SESSDATA={self.sessdata}")
            if parts:
                self.http.headers["Cookie"] = "; ".join(parts)

    def _ensure_buvid3(self) -> str | None:
        # H6：「是否已尝试」的判断与占位必须原子，否则并发时会同时打 finger/spi。
        # 网络请求不能放锁内；后到的线程看到 _cookie_tried 会直接匿名返回 None，
        # 只是少一个 buvid3，不影响取数正确性。
        with self._lock:
            if self._buvid3 or self._cookie_tried:
                return self._buvid3
            self._cookie_tried = True
        try:
            r = self.http.get(f"{API}/x/frontend/finger/spi")
            body = r.json()
            b3 = (body.get("data") or {}).get("b_3")
        except Exception:                        # noqa: BLE001  buvid3 缺失只影响动态接口
            b3 = None
        if b3:
            with self._lock:
                self._buvid3 = b3
        self._apply_cookies()                    # 即使 buvid3 没拿到，也要把 SESSDATA 带上
        with self._lock:
            return self._buvid3

    def check_login(self) -> dict[str, Any]:
        """用 nav 接口确认 SESSDATA 是否有效；无效时不影响匿名取数。"""
        out: dict[str, Any] = {"configured": bool(self.sessdata),
                               "ok": False, "uname": "", "mid": 0,
                               "code": 0, "message": "", "state": "unknown",
                               "masked": mask_sessdata(self.sessdata)}
        self._ensure_buvid3()
        try:
            self._throttle()
            body = self.http.get(f"{API}/x/web-interface/nav").json()
            self._bump_request()
            out["code"] = body.get("code", -1)
            out["message"] = body.get("message", "")
            data = body.get("data") or {}
            if body.get("code") == 0 and data.get("isLogin"):
                out["ok"] = True
                out["state"] = "valid"
                out["uname"] = data.get("uname", "")
                out["mid"] = data.get("mid", 0)
            elif (body.get("code") == 0 and data.get("isLogin") is False
                  or body.get("code") == -101):
                out["state"] = "expired"
                out["message"] = out["message"] or "B 站返回未登录（SESSDATA 无效或过期）"
        except Exception as e:                    # noqa: BLE001
            out["message"] = f"{type(e).__name__}: {e}"
        return out

    def _refresh_mixin_key(self) -> str:
        today = time.strftime("%Y-%m-%d")
        with self._lock:                     # H6：缓存命中判断也读共享字段，进锁
            if self._mixin_key and self._mixin_date == today:
                return self._mixin_key
        self._throttle()
        r = self.http.get(f"{API}/x/web-interface/nav")
        self._bump_request()
        body = r.json()                          # 未登录 code=-101，但 data.wbi_img 照常返回
        img = (body.get("data") or {}).get("wbi_img") or {}
        img_url, sub_url = img.get("img_url", ""), img.get("sub_url", "")
        if not (img_url and sub_url):
            raise BiliError(body.get("code", -1), "nav 未返回 wbi_img", "/x/web-interface/nav")
        raw = (img_url.rsplit("/", 1)[-1].split(".")[0]
               + sub_url.rsplit("/", 1)[-1].split(".")[0])
        key = "".join(raw[i] for i in MIXIN_KEY_ENC_TAB)[:32]
        with self._lock:                     # 写回成对字段，避免读到半更新的缓存
            self._mixin_key = key
            self._mixin_date = today
        return key

    def _sign(self, params: dict[str, Any]) -> dict[str, Any]:
        mixin_key = self._refresh_mixin_key()
        signed = dict(params)
        signed["wts"] = int(time.time())
        q = urllib.parse.urlencode(sorted(
            (k, v) for k, v in signed.items()
            if not any(c in str(v) for c in "!'()*")
        ))
        signed["w_rid"] = hashlib.md5((q + mixin_key).encode()).hexdigest()
        return signed

    def _get(self, url: str, params: Any, wbi: bool = False,
             allow_code: Iterable[int] = (),
             max_attempts: int | None = None) -> dict[str, Any]:
        self._ensure_buvid3()
        allow = set(allow_code)
        last: BiliError | None = None
        rounds = self.retries + 1 if max_attempts is None else max(1, max_attempts)
        for attempt in range(rounds):
            self._throttle()
            if wbi and isinstance(params, dict):
                q = self._sign(params)
            else:
                q = params
            try:
                r = self.http.get(url, params=q)
            except httpx.HTTPError as e:
                last = BiliError(-1, f"网络错误 {e.__class__.__name__}", url, kind="net")
                time.sleep(backoff_seconds("net", attempt))
                continue
            self._bump_request()
            if r.status_code != 200:
                last = BiliError(-r.status_code, f"HTTP {r.status_code}", url, kind="http")
                if r.status_code in (412, 429):
                    time.sleep(backoff_seconds("http", attempt))
                    continue
                raise last
            body = r.json()
            code = body.get("code", 0)
            if code == 0 or code in allow:
                return body
            last = BiliError(code, body.get("message", ""), url)
            if code in RETRYABLE:
                # 2026-10-28：-352 是服务端随机抽的软校验，隔 3 秒再试纯属浪费
                # （实测 16% 的请求中招，每轮因此多等 30~60s）。改成 0.3s 立即重试。
                time.sleep(backoff_seconds("biz", attempt, code))
                continue
            raise last
        raise last or BiliError(-1, "未知错误", url)

    # ---------- 搜索 ----------
    def search_user(self, keyword: str, limit: int = 5) -> list[dict[str, Any]]:
        """按昵称搜 UP 主（wbi 签名）。返回 [{uid, uname, fans, face, sign}]，按粉丝数降序。"""
        kw = (keyword or "").strip()
        if not kw:
            return []
        body = self._get(f"{API}/x/web-interface/wbi/search/type",
                         {"search_type": "bili_user", "keyword": kw, "page": 1},
                         wbi=True, allow_code=(-403, -352))
        out: list[dict[str, Any]] = []
        for it in (body.get("data") or {}).get("result") or []:
            try:
                uid = int(it.get("mid") or 0)
            except (TypeError, ValueError):
                continue
            if not uid:
                continue
            out.append({"uid": uid,
                        "uname": re.sub(r"<[^>]+>", "", str(it.get("uname") or "")),
                        "fans": int(it.get("fans") or 0),
                        "face": ("https:" + it["upic"]) if str(it.get("upic") or "").startswith("//") else str(it.get("upic") or ""),
                        "sign": str(it.get("usign") or "")})
        out.sort(key=lambda x: -x["fans"])
        return out[:limit]

    # ---------- 直播 ----------
    def get_room_by_mid(self, mid: int) -> dict[str, Any]:
        """UID -> 直播间信息（免 WBI，比 acc/info 稳）。"""
        body = self._get(f"{LIVE}/room/v1/Room/getRoomInfoOld", {"mid": mid})
        return body.get("data") or {}

    def fetch_live(self, room_id: int) -> LiveState:
        body = self._get(f"{LIVE}/room/v1/Room/get_info", {"room_id": room_id})
        d = body.get("data") or {}
        return LiveState(
            uid=int(d.get("uid") or 0),
            room_id=int(d.get("room_id") or room_id),
            live_status=int(d.get("live_status") or 0),
            title=d.get("title", ""),
            uname=d.get("uname", ""),
            online=int(d.get("online") or 0),
            live_time=_to_ts(d.get("live_time")),
            area_name=d.get("area_name", ""),
            cover=d.get("user_cover") or d.get("keyframe") or "",
            face=d.get("face", ""),
        )

    def fetch_live_batch(self, uids: list[int]) -> dict[int, LiveState]:
        """一次请求取多个主播的直播状态（30 个主播 1 个请求）。"""
        params = [("uids[]", str(u)) for u in uids]
        body = self._get(f"{LIVE}/room/v1/Room/get_status_info_by_uids", params)
        out: dict[int, LiveState] = {}
        for key, v in (body.get("data") or {}).items():
            try:
                uid = int(v.get("uid") or key)
            except (TypeError, ValueError):
                continue
            out[uid] = LiveState(
                uid=uid,
                room_id=int(v.get("room_id") or 0),
                live_status=int(v.get("live_status") or 0),
                title=v.get("title") or "",
                uname=v.get("uname") or "",
                online=int(v.get("online") or 0),
                live_time=_to_ts(v.get("live_time")),
                area_name=v.get("area_name") or "",
                cover=v.get("cover_from_user") or "",
                face=v.get("face") or "",
            )
        return out

    # ---------- 动态 ----------

    @classmethod
    def _norm_item(cls, it: dict[str, Any]) -> dict[str, Any]:
        """把两个端点的差异抹平，parse_dynamic 只认 space 形状。

        desktop 端点（/desktop/v1/feed/space）：modules 是 list，
        module_dynamic 用 dyn_archive / dyn_forward / dyn_draw / dyn_word 这套键，
        没有 major / desc。实测该端点 0 次 -352，但图文动态拿不到正文（只有图）。
        """
        mods = it.get("modules")
        if isinstance(mods, list):
            merged: dict[str, Any] = {}
            for m in mods:
                if isinstance(m, dict):
                    merged.update(m)
            it = dict(it)
            it["modules"] = merged
        mod = it.get("modules") or {}
        md = mod.get("module_dynamic")
        if isinstance(md, dict) and "major" not in md:
            md2 = dict(md)
            major: dict[str, Any] = {}
            desc = md2.get("desc")
            if isinstance(md.get("dyn_archive"), dict):
                major = {"type": "MAJOR_TYPE_ARCHIVE", "archive": md["dyn_archive"]}
            elif isinstance(md.get("dyn_article"), dict):
                major = {"type": "MAJOR_TYPE_ARTICLE", "article": md["dyn_article"]}
            elif isinstance(md.get("dyn_forward"), dict):
                inner = md["dyn_forward"].get("item") or {}
                fname = (((cls._norm_item(inner).get("modules") or {})
                          .get("module_author") or {}).get("user") or {}).get("name") or ""
                major = {"type": "MAJOR_TYPE_NONE"}
                desc = {"text": f"转发了 @{fname}" if fname else "转发了一条动态"}
            elif isinstance(md.get("dyn_draw"), dict):
                # desktop 的图片键是 src（不是 space 的 url），不转一下卡片就只能拿到空封面
                pics = []
                for p in md["dyn_draw"].get("items") or []:
                    src = (p or {}).get("src") or (p or {}).get("url") or ""
                    if src:
                        pics.append({"url": src})
                text = ((md2.get("desc") or {}).get("text")
                        or (md["dyn_draw"].get("desc") or {}).get("text") or "")
                major = {"type": "MAJOR_TYPE_OPUS",
                         "opus": {"pics": pics, "summary": {"text": text}}}
            elif isinstance(md.get("dyn_word"), dict):
                major = {"type": "MAJOR_TYPE_WORD"}
                desc = {"text": md["dyn_word"].get("text") or ""}
            if major:
                md2["major"] = major
            if desc is not None:
                md2["desc"] = desc
            it["modules"] = dict(mod, module_dynamic=md2)
        return it

    def _fetch_dyn_body(self, uid: int, offset: str,
                        endpoint: str = "space") -> dict[str, Any]:
        """取一页动态原始响应。

        endpoint=space 是主力（内容全，实测约 36% 概率 -352）；
        endpoint=desktop 只在 space 连续被风控时兜底（实测 48 次 0 次 -352）。
        max_attempts=1：重试交给外层 fetch_dynamics，避免两层嵌套退避。
        """
        params = {
            "host_mid": uid,
            "offset": offset,
            "timezone_offset": -480,
            # 带上是实测能救回「新动态风格」账号的开关（老账号带不带都行）
            "features": "itemOpusStyle",
        }
        url = SPACE_DYN if endpoint == "space" else DESKTOP_DYN
        with self._lock:                     # 诊断字段也可能多线程同时写，统一进锁
            self.last_endpoint = endpoint
        return self._get(url, params, wbi=True, max_attempts=1)

    def fetch_dynamics(self, uid: int, offset: str = "", limit: int = 10,
                       empty_retries: int = 2,
                       drop_live_rcmd: bool = True) -> tuple[list[DynItem], str, int]:
        """取某主播的动态列表。

        坑（2026-10-27 实测）：接口会**静默返回空**——code=0、items=[]、
        has_more=false，但实际有数据。实测连打 8 次只有 2 次非空。
        因此 items 为空且不是翻页（offset 非空）时必须重试，否则会漏推送。

        返回 (items, next_offset, 用了重试次数)。
        """
        raw: list[dict[str, Any]] = []
        data: dict[str, Any] = {}
        used = 0
        # last_* 是「最近一次调用」的诊断快照，并发时按最后一次写入为准；
        # 但 += / 成对写入仍要进锁，否则会读到别的线程写了一半的状态。
        with self._lock:
            self.last_endpoint = ""
            self.last_risk = 0
        for attempt in range(empty_retries + 1):
            body: dict[str, Any] = {}
            try:
                body = self._fetch_dyn_body(uid, offset)
            except BiliError as e:
                if not e.is_risk:            # S5：HTTP 412/429 不再当业务风控吞掉
                    raise
                with self._lock:             # -352：随机软校验，0.3s 立刻再来
                    self.last_risk += 1
            if body:
                data = body.get("data") or {}
                raw = [self._norm_item(x) for x in (data.get("items") or [])]
                used = attempt
                if raw:
                    break
                # 翻页请求（offset 非空）返回空是正常的：已到末尾
                if offset:
                    break
            if attempt < empty_retries:
                time.sleep(0.3 if not body else 1.0)
        # 兜底：space 空或一直被风控时再问一次 desktop 端点。
        # 实测 11 个「space 三连空」的号里，desktop 有 4 个能拿到数据 → 空响应部分是端点相关的。
        if not raw and not offset:
            try:
                dbody = self._fetch_dyn_body(uid, offset, endpoint="desktop")
            except BiliError:
                dbody = {}
            ddata = dbody.get("data") or {}
            draw = [self._norm_item(x) for x in (ddata.get("items") or [])]
            if draw:
                raw, data = draw, ddata
                with self._lock:
                    self.last_endpoint = "desktop-fallback"
        items = [parse_dynamic(it) for it in raw[:limit]]
        # 平台自动生成的「开播提醒」动态：不是主播自己发的，且会和开播通知重复
        with self._lock:
            self.last_dropped_rcmd = 0
        if drop_live_rcmd:
            keep = [i for i in items if i.type != "DYNAMIC_TYPE_LIVE_RCMD"]
            with self._lock:
                self.last_dropped_rcmd = len(items) - len(keep)
            items = keep
        return items, str(data.get("offset") or ""), used

    def fetch_dynamic_detail(self, id_str: str) -> DynItem | None:
        """单条动态详情（补全用，2026-11-08）。

        为什么不用 feed/space 单翻：详情接口只认一个 id，不受「这一页里有没有它」影响；
        但必须带 features=itemOpusStyle —— 不带时图文动态返回的是 MAJOR_TYPE_DRAW，
        正文在 desc 里也是 null（2026-11-08 实测：同一条动态带参数才有 opus.summary.text）。
        """
        try:
            body = self._get(f"{API}/x/polymer/web-dynamic/v1/detail",
                             {"id": str(id_str), "features": "itemOpusStyle"}, wbi=True)
        except BiliError:
            return None
        if int(body.get("code") or 0) != 0:
            return None
        it = (body.get("data") or {}).get("item") or {}
        if not isinstance(it, dict) or not it.get("id_str"):
            return None
        return parse_dynamic(self._norm_item(it))


_MAX_TEXT = 1600


def _rich_tokens(nodes: list[dict[str, Any]] | None) -> list[list[str]]:
    """把 rich_text_nodes 压成可 JSON 序列化的 token 列表，供卡片内联渲染表情。

    结构：[["t", 文本], ["e", 表情图 url, 表情名]]
    2026-10-28 实测：表情节点的 emoji.icon_url 是 i0.hdslb.com/bfs/garb/...png，
    text 是 "[奈奈莉娅-夏日偶像_kira]" 这种占位串，直接当文字画就是一堆方括号。
    """
    toks: list[list[str]] = []
    for n in nodes or []:
        if not isinstance(n, dict):
            continue
        if (n.get("type") or "") == "RICH_TEXT_NODE_TYPE_EMOJI":
            emo = n.get("emoji") or {}
            url = (emo.get("icon_url") or emo.get("webp_url")
                   or emo.get("gif_url") or n.get("icon_url") or "")
            name = emo.get("text") or n.get("text") or ""
            toks.append(["e", url, name])
        else:
            s = n.get("orig_text") or n.get("text") or ""
            if s:
                toks.append(["t", s])
    merged: list[list[str]] = []
    for t in toks:
        if t[0] == "t" and merged and merged[-1][0] == "t":
            merged[-1][1] += t[1]
        else:
            merged.append(t)
    return merged


def _dyn_text(major: dict[str, Any], desc: dict[str, Any]) -> tuple[str, list[list[str]]]:
    """取正文与富文本 token：新版图文在 major.opus.summary，旧版在 desc。"""
    opus = major.get("opus") or {}
    osum = opus.get("summary") or {}
    text = osum.get("text") or desc.get("text") or ""
    toks = _rich_tokens(osum.get("rich_text_nodes") or desc.get("rich_text_nodes"))
    return text, toks


def _parse_orig(orig: dict[str, Any] | None) -> dict[str, Any]:
    """转发动态的原文（2026-10-28 实测结构）。

    坑：orig.modules.module_dynamic.desc 常为 null，正文在 major.opus.summary.text；
    转发的视频/专栏在 orig.major.archive / article。
    """
    if not orig:
        return {}
    mods = orig.get("modules") or {}
    au = mods.get("module_author") or {}
    md = mods.get("module_dynamic") or {}
    major = md.get("major") or {}
    desc = md.get("desc") or {}
    mtype = major.get("type") or ""
    out: dict[str, Any] = {"uname": au.get("name") or "", "kind": "DYNAMIC"}
    # 只看 major 的形状，别看 orig.type：实测有一条 orig.type=DYNAMIC_TYPE_ARTICLE
    # 但 major 是 MAJOR_TYPE_OPUS（正文和配图都在 opus 里），按 type 分流会漏掉原文。
    if major.get("archive") or mtype == "MAJOR_TYPE_ARCHIVE":
        arch = major.get("archive") or {}
        bvid = arch.get("bvid") or ""
        out.update(kind="VIDEO", title=arch.get("title") or "",
                   cover=arch.get("cover") or "",
                   text=(arch.get("desc") or "")[:_MAX_TEXT],
                   duration=arch.get("duration_text") or "")
        if bvid:
            out["url"] = "https://www.bilibili.com/video/%s" % bvid
    elif major.get("article") or mtype == "MAJOR_TYPE_ARTICLE":
        art = major.get("article") or {}
        covers = art.get("covers") or []
        out.update(kind="ARTICLE", title=art.get("title") or "",
                   cover=(covers[0] if covers else ""),
                   text=(art.get("desc") or "")[:_MAX_TEXT])
        if art.get("id"):
            out["url"] = "https://www.bilibili.com/read/cv%s" % art["id"]
    else:
        text, toks = _dyn_text(major, desc)
        pics = (major.get("opus") or {}).get("pics") or []
        imgs = [x.get("url") for x in pics if isinstance(x, dict) and x.get("url")][:4]
        out.update(text=text[:_MAX_TEXT], rich=toks, images=imgs)
    return {k: v for k, v in out.items() if v not in ("", [], None)}


def _parse_reserve_module(dyn: dict[str, Any]) -> dict[str, Any]:
    """动态上的「直播预约」模块（2026-11-05 实测结构）。

    modules.module_dynamic.additional = {"type": "ADDITIONAL_TYPE_RESERVE",
      "reserve": {"title": "直播预约：女大生夜谈SP！这算出轨吗？",
                  "desc1": {"text": "10-30 19:30 直播", "visible": false},
                  "desc2": {"text": "836人预约"},
                  "rid": 5846955, "reserve_total": 836, "stype": 2,
                  "up_mid": "435243735",
                  "button": {"uncheck": {"text": "已结束", "disable": 1}}}}
    desc1.visible 常为 false，但 text 里带着开播时间，照样取。
    """
    add = dyn.get("additional") or {}
    if not isinstance(add, dict):
        return {}
    if add.get("type") != "ADDITIONAL_TYPE_RESERVE":
        return {}
    rv = add.get("reserve")
    if not isinstance(rv, dict):
        return {}
    btn = rv.get("button") or {}
    unchk = btn.get("uncheck") or {}
    return {
        "title": str(rv.get("title") or ""),
        "time_text": str((rv.get("desc1") or {}).get("text") or ""),
        "count_text": str((rv.get("desc2") or {}).get("text") or ""),
        "reserve_total": int(rv.get("reserve_total") or 0),
        "reserve_id": str(rv.get("rid") or ""),
        "stype": int(rv.get("stype") or 0),
        "up_mid": str(rv.get("up_mid") or ""),
        "ended": bool(unchk.get("disable")),
    }


def parse_dynamic(it: dict[str, Any]) -> DynItem:
    modules = it.get("modules") or {}
    author = modules.get("module_author") or {}
    dyn = modules.get("module_dynamic") or {}
    major = dyn.get("major") or {}
    desc = dyn.get("desc") or {}
    id_str = str(it.get("id_str") or "")
    type_ = it.get("type") or ""

    subtype, title, url = "UNKNOWN", "", f"https://t.bilibili.com/{id_str}"
    text: str = ""
    rich: list[list[str]] = []
    fwd: dict[str, Any] = {}
    video: dict[str, Any] = {}
    image_count, cover = 0, ""
    images: list[dict] = []
    if type_ == "DYNAMIC_TYPE_AV":
        arch = major.get("archive") or {}
        subtype, title = "AV", arch.get("title") or ""
        if arch.get("bvid"):
            url = f"https://www.bilibili.com/video/{arch['bvid']}"
        cover = arch.get("cover") or ""
        text = arch.get("desc") or ""
        stat = arch.get("stat") or {}
        video = {"bvid": arch.get("bvid") or "",
                 "duration": arch.get("duration_text") or "",
                 "play": str(stat.get("play") or ""),
                 "danmaku": str(stat.get("danmaku") or "")}
    elif type_ == "DYNAMIC_TYPE_ARTICLE":
        art = major.get("article") or {}
        subtype, title = "ARTICLE", art.get("title") or ""
        if art.get("id"):
            url = f"https://www.bilibili.com/read/cv{art['id']}"
        covers = art.get("covers") or []
        cover = covers[0] if covers else ""
        text, rich = _dyn_text(major, desc)
    elif type_ == "DYNAMIC_TYPE_FORWARD":
        subtype = "FORWARD"
        om = ((it.get("orig") or {}).get("modules") or {}).get("module_author") or {}
        title = f"转发了 @{om.get('name', '')}".strip()
        text, rich = _dyn_text(major, desc)
        fwd = _parse_orig(it.get("orig"))
        if fwd.get("cover"):
            cover = fwd["cover"]
        image_count = len(fwd.get("images") or [])
    elif type_ in ("DYNAMIC_TYPE_DRAW", "DYNAMIC_TYPE_WORD"):
        subtype = "DRAW" if type_ == "DYNAMIC_TYPE_DRAW" else "WORD"
        # 新版图文（MAJOR_TYPE_OPUS）的正文在 major.opus.summary.text，
        # desc.text 是 None —— 不取这里，推送正文就是空的。
        text, rich = _dyn_text(major, desc)
        opus = major.get("opus") or {}
        title = opus.get("title") or ""
        pics = opus.get("pics") or []
        image_count = len(pics)
        for p in pics:
            if not isinstance(p, dict) or not p.get("url"):
                continue
            images.append({"url": p["url"],
                           "w": int(p.get("width") or 0),
                           "h": int(p.get("height") or 0),
                           "size": float(p.get("size") or 0)})
        if pics:
            cover = pics[0].get("url") or ""
        jump = opus.get("jump_url") or ""
        if jump.startswith("//"):
            url = "https:" + jump
        elif jump:
            url = jump
    elif type_ == "DYNAMIC_TYPE_LIVE_RCMD":
        # 开播时平台自动生成，默认由 fetch_dynamics 丢弃
        subtype, title = "LIVE_RCMD", "（平台自动生成的开播提醒）"

    text = (text or "").strip()
    reserve: dict[str, Any] = _parse_reserve_module(dyn)
    return DynItem(
        id_str=id_str,
        type=type_,
        pub_ts=int(author.get("pub_ts") or 0),      # 接口返回字符串，必须 int()
        subtype=subtype,
        title=title,
        # 纯文字消息只用一句摘要；换行压成空格，避免把 QQ 消息撑得很长
        summary=text[:120].replace("\n", " ").strip(),
        url=url,
        image_count=image_count,
        cover=cover,
        images=images,
        face=author.get("face") or "",
        uname=author.get("name") or "",
        # 卡片用：完整正文（保留换行与表情 token）
        text=text[:_MAX_TEXT],
        rich=rich,
        fwd=fwd,
        video=video,
        reserve=reserve,
    )
