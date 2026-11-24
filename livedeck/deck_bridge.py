"""LiveDeck 的观看地址解析与 Edge 弹幕桥调度 mixin。"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import NamedTuple
from urllib.parse import urlencode

from livedeck.deck_base import (
    DANMU_INFO,
    PLAY_INFO,
    PLAY_TTL,
    PLAY_URL,
    QUALITY_LABEL,
    TOKEN_TTL,
    DeckError,
)
from livedeck.hub import protover_for_server
# S2（2026-11-14）：FLV 直链是拿 B 站响应拼出来的（host + base_url + extra），
# 以前零校验就交给浏览器和本地转发 —— B 站被打/被投毒时这就是一条通往内网的通道。
# 判定与 QQ 网关共用 shared/netsafe.py（只认 B 站 CDN 域、只允许 http(s)、不许 userinfo）。
from shared.netsafe import STREAM_SUFFIXES as STREAM_HOST_SUFFIXES, stream_url_ok


class PlaySource(NamedTuple):
    kind: str
    url: str
    accept: list[int]
    desc: dict[int, str]
    current: int


class DeckBridge:
    # ---------- 弹幕桥 ----------
    def _engine_ev(self, level: str, area: str, message: str, room: int = 0,
                   detail: str = "") -> None:
        """写引擎日志；LiveDeck 没注入 engine（单测）时静默跳过。"""
        eng = getattr(self, "engine", None)
        if eng is None:
            return
        try:
            eng.write(level, area, message, room=room, detail=detail)
        except Exception:  # noqa: BLE001
            pass

    def _engine_state(self) -> str:
        """对外展示的引擎状态（快照 / SSE / 体检共用一套判断）。

        waiting 表示「舞台为空，还没有要收弹幕的房间」：这时引擎没起来是正常的，
        页面该说「等待选台」，日志也不该按故障报 error。舞台清空后引擎可能还在跑
        （sync([]) 只是不同步房间，不关浏览器），对外仍然报 waiting——「没有要收的
        房间」比「浏览器还在」更接近用户关心的事，桥是否在跑看 bridge.state。
        """
        status = str(getattr(self.bridge, "status", "") or "idle")
        if not self._watched_rooms():
            return "waiting"
        if status in ("online", "starting", "stale", "error"):
            return status
        # idle / stopped / restart：还没真正发起，按「启动中」算，别显示成故障
        return "starting"

    def _publish_engine(self, force: bool = False) -> str:
        """状态一变就推 SSE，页面不用等 12 秒一次的 pollSnap。"""
        status = self._engine_state()
        error = str(getattr(self.bridge, "last_error", "") or "")
        sig = (status, error)
        if not force and sig == getattr(self, "_engine_sig", None):
            return status
        self._engine_sig = sig
        try:
            self.hub.publish({"type": "engine", "status": status,
                              "state": str(getattr(self.bridge, "status", "") or ""),
                              "error": error, "ts": time.time()})
        except Exception:  # noqa: BLE001
            pass
        return status

    def _engine_kick(self, why: str) -> None:
        """舞台刚有房间、引擎还没起来：立刻拉浏览器，不等下一轮 reconcile（8 秒）。

        先 ensure() 再取 token：浏览器启动要几秒，页面在这段时间能看到「引擎启动中」；
        反过来先串行取完所有 token 才动浏览器，只会让「离线」多挂十几秒（而且 token
        全失败时 items 为空，浏览器根本不会被拉起）。
        """
        before = str(getattr(self.bridge, "status", "") or "")
        try:
            self.bridge.mark_starting()
        except Exception:  # noqa: BLE001
            pass
        # 每次 kick 都留一条：只有失败才记日志的话，下次「加房没反应」还是无从查起
        self._engine_ev("info", "bridge", "引擎即刻连接：%s（原状态 %s）" % (
            why, before or "无"), detail="先起浏览器，再取 token")
        self._publish_engine(force=True)

        def work() -> None:
            live = False
            try:
                live = bool(self.bridge.ensure())
                if not live:
                    self._engine_ev("error", "bridge", "引擎即刻启动失败：%s" % (
                        self.bridge.last_error or "未知原因"), detail=why)
                else:
                    self._engine_ev("info", "bridge", "引擎即刻连接完成：%s" % why)
            except Exception as e:  # noqa: BLE001
                self._engine_ev("error", "bridge", "引擎即刻启动异常：%s: %s" % (
                    e.__class__.__name__, e), detail=why)
            finally:
                self._publish_engine(force=True)
            if live:
                # 起不来就不要再 sync：sync 里还会再调 ensure()，白等一轮启动超时
                self._sync_bridge(force=True)

        threading.Thread(target=work, daemon=True, name="deck-kick").start()

    # 正常启动 1-3 秒就到位；「启动中」超过这个宽限期就认为这一轮启动没人接着干了
    STARTING_GRACE = 30.0

    def _starting_guard(self, now: float, state: str) -> bool:
        """卡在「启动中」的自愈：补一次启动，返回这一轮是否补了。

        启动超时/被异常打断、或者 token 全失败绕过 kick 时，状态会一直停在 idle/stopped/
        error（对外一律显示「启动中」）。以前只能等下一轮「离线」体检——那只是记日志，
        并不会把引擎拉起来。宽限期一过就补一次，用户不用手动重启。
        """
        if state != "starting":
            self._starting_since = 0.0
            return False
        since = float(getattr(self, "_starting_since", 0.0) or 0.0)
        if not since:
            self._starting_since = now
            return False
        if now - since < self.STARTING_GRACE:
            return False
        self._starting_since = now
        self._engine_ev("warn", "bridge", "引擎停在「启动中」超过 %d 秒，重新拉起弹幕桥" % int(
            self.STARTING_GRACE), detail="正常启动只需 1-3 秒；持续不动说明启动没被发起或被异常打断")
        threading.Thread(target=self._engine_kick, args=("启动中超时自愈",), daemon=True,
                         name="deck-kick-timeout").start()
        return True

    def _ensure_buvid(self) -> str:
        if self.buvid:
            return self.buvid
        try:
            self.buvid = self._client._ensure_buvid3() or ""
        except Exception as e:  # noqa: BLE001
            self.buvid = ""
            self._engine_ev("warn", "token", "取 buvid3 失败：%s" % e.__class__.__name__,
                            detail="没有 buvid 时弹幕认证可能被拒（-101 / code 1006）")
        return self.buvid

    def _playinfo(self, room_id: int, qn: int) -> tuple[str, list[int], dict, int]:
        """同步测试页使用新版接口的 HTTP-FLV 直链。"""
        _, url, accept, desc, current = self._play_source(room_id, qn, allow_hls=False)
        return url, accept, desc, current

    def _play_source(self, room_id: int, qn: int, *, allow_hls: bool) -> PlaySource:
        params = self._client._sign({
            "room_id": room_id, "protocol": "0,1", "format": "0,1,2", "codec": "0,1",
            "qn": qn, "platform": "web", "ptype": "8", "dolby": "5", "panorama": "1",
        })
        headers = {"Cookie": "SESSDATA=%s" % self.creds.sessdata} if self.creds.sessdata else {}
        try:
            body = self.http.get(PLAY_INFO + "?" + urlencode(params), headers=headers, timeout=12).json()
        except Exception as e:  # noqa: BLE001
            raise DeckError("取直播流失败：%s" % e.__class__.__name__, 502) from e
        if int(body.get("code") or 0) != 0:
            raise DeckError("取直播流失败（%s）" % (body.get("message") or body.get("code")), 502)
        data = body.get("data") or {}
        pi = ((data.get("playurl_info") or {}).get("playurl")) or {}
        desc = {int(x.get("qn")): x.get("desc") for x in (pi.get("g_qn_desc") or []) if x.get("qn")}
        codecs = {
            (stream.get("protocol_name"), fmt.get("format_name"), codec.get("codec_name")): codec
            for stream in pi.get("stream") or []
            for fmt in stream.get("format") or []
            for codec in fmt.get("codec") or []
        }
        formats = [("flv", "http_stream", "flv", ("avc", "hevc"))]
        if allow_hls:
            formats.extend([
                ("hls", "http_hls", "fmp4", ("avc", "hevc", "av1")),
                ("hls", "http_hls", "ts", ("avc", "hevc")),
            ])
        for kind, protocol, fmt, names in formats:
            for name in names:
                c = codecs.get((protocol, fmt, name)) or {}
                base = c.get("base_url")
                if not base:
                    continue
                for ui in c.get("url_info") or []:
                    host = ui.get("host") or ""
                    if not host:
                        continue
                    sep = "" if base.endswith("?") or base.endswith("&") else ("&" if "?" in base else "?")
                    url = host + base + sep + (ui.get("extra") or "")
                    # 跳过不可信 CDN，仍可使用 B 站返回的其他地址。
                    if not stream_url_ok(url):
                        continue
                    accept = [int(x) for x in (c.get("accept_qn") or []) if str(x).isdigit()]
                    return PlaySource(kind, url, accept, desc, int(c.get("current_qn") or qn))
        raise DeckError("这个直播间暂时没有可播放的流（可能未开播）", 404)

    @staticmethod
    def _quality_info(source: PlaySource, at: float) -> dict:
        return {
            "current": source.current,
            "list": [{"qn": qn, "label": source.desc.get(qn) or label}
                     for qn, label in QUALITY_LABEL if not source.accept or qn in source.accept],
            "at": at,
        }

    def qualities(self, room_id: int) -> dict:
        """该房间可选画质。带登录 Cookie 才能拿到原画/蓝光档。"""
        hit = self._qualities.get(room_id)
        if hit and time.time() - hit.get("at", 0) < 120:
            return hit
        try:
            source = self._play_source(room_id, int((hit or {}).get("current") or 10000), allow_hls=True)
        except DeckError:
            source = PlaySource("flv", "", [], {}, 10000)
        out = self._quality_info(source, time.time())
        self._qualities[room_id] = out
        return out

    def playback(self, room_id: int, qn: int, force: bool = False) -> dict:
        """舞台优先走 HTTP-FLV 中继，仅有 HLS 时返回经校验的播放列表。"""
        source = self._resolve_playback(room_id, qn, force, allow_hls=True)
        result = self._quality_info(source, time.time())
        result["kind"] = source.kind
        if source.kind == "hls":
            result.update(url=source.url, cdn_suffixes=STREAM_HOST_SUFFIXES)
        return result

    def flv_url(self, room_id: int, qn: int = 10000, force: bool = False) -> str:
        """取该房间指定画质的 HTTP-FLV 直链（约 2 分钟过期，按 qn 缓存）。"""
        return self._resolve_playback(room_id, qn, force, allow_hls=False).url

    def _resolve_playback(self, room_id: int, qn: int, force: bool, *, allow_hls: bool) -> PlaySource:
        now = time.time()
        try:
            qn = int(qn or 10000)
        except (TypeError, ValueError):
            qn = 10000
        status = None
        inf = self.info.get(room_id)
        if inf is not None:
            status = inf.get("live_status")
        if status is None:
            try:
                status = (self.fetch_info(room_id) or {}).get("live_status")
            except Exception:  # noqa: BLE001
                status = None
        if status == 0 and not force:
            raise DeckError("这个直播间当前未开播", 404)
        with self.lock:
            hit = self._play.get((room_id, qn))
            if hit and not force and now - hit[0] < PLAY_TTL and (allow_hls or hit[1].kind == "flv"):
                return hit[1]
        try:
            source = self._play_source(room_id, qn, allow_hls=allow_hls)
        except DeckError:
            # 兜底：老的 playUrl 接口（只给一档，q参数被忽略）
            try:
                r = self.http.get(PLAY_URL, params={"cid": room_id, "quality": 4, "platform": "web"})
                data = r.json() or {}
            except Exception as e:  # noqa: BLE001
                raise DeckError("取直播流失败：%s" % type(e).__name__, 502) from e
            if data.get("code") != 0:
                raise DeckError("取直播流失败（%s）" % data.get("message"), 502)
            urls = (data.get("data") or {}).get("durl") or []
            url = next((item["url"] for item in urls if stream_url_ok(item.get("url"))), "")
            if not url:
                raise DeckError("这个直播间暂时没有可播放的流（可能未开播）", 404)
            source = PlaySource("flv", url, [], {}, qn)
        if source.accept:
            self._qualities[room_id] = self._quality_info(source, now)
        with self.lock:
            self._play[(room_id, qn)] = (now, source)
        return source

    def _sign_danmu_url(self, room_id: int) -> str:
        params = self._client._sign({"id": room_id, "type": 0})
        from urllib.parse import urlencode
        return DANMU_INFO + "?" + urlencode(params)

    def danmu_token(self, room_id: int, force: bool = False) -> dict:
        """取弹幕服务器地址 + token。

        必须由 Python 侧带 WBI 签名去请求：headless 浏览器页面里 fetch 同一个已签名
        地址会被风控返回 -352，而 Python 侧返回 code 0。页面只负责建 WebSocket。
        """
        now = time.time()
        with self.lock:
            hit = self._danmu.get(room_id)
        if hit and not force and now - hit[0] < TOKEN_TTL - 90:
            return hit[1]
        url = self._sign_danmu_url(room_id)
        # 取 token 时带上登录 Cookie（必须和 WS 认证里的 buvid 一致）：
        # 匿名取到的 token 会让服务端把发送者昵称脱敏成「礼***」这种形式。
        cookie = dict(self._bridge_cookies())
        buvid = self._ensure_buvid()
        if buvid:
            cookie.setdefault("buvid3", buvid)
        headers = {"Cookie": "; ".join("%s=%s" % (k, v) for k, v in cookie.items())} if cookie else None
        try:
            body = self.http.get(url, timeout=15.0, headers=headers).json()
        except Exception as e:  # noqa: BLE001
            self._engine_ev("warn", "token", "房间 %s 取弹幕服务器失败：%s" % (
                room_id, e.__class__.__name__), room_id, str(e)[:300])
            raise DeckError("获取弹幕服务器信息失败：%s" % e.__class__.__name__, 502)
        if int(body.get("code") or 0) != 0:
            self._engine_ev("error", "token", "房间 %s 取弹幕服务器被拒：code=%s" % (
                room_id, body.get("code")), room_id, str(body.get("message") or "")[:200])
            raise DeckError("获取弹幕服务器信息失败：%s（code=%s）"
                            % (body.get("message") or "", body.get("code")), 502)
        data = body.get("data") or {}
        hosts = data.get("host_list") or []
        if not hosts:
            self._engine_ev("error", "token", "房间 %s 没有可用的弹幕服务器" % room_id, room_id,
                            "接口 code=0 但 host_list 为空，多为风控或接口变更")
            raise DeckError("没有可用的弹幕服务器", 502)
        host = hosts[0]
        out = {
            "token": data.get("token") or "",
            "host": host.get("host") or "",
            "wss_port": int(host.get("wss_port") or 443),
        }
        if not out["host"]:
            raise DeckError("弹幕服务器信息不完整", 502)
        with self.lock:
            self._danmu[room_id] = (now, out)
        return out

    def _bridge_items(self) -> list[dict]:
        buvid = self._ensure_buvid()
        # 登录态下 token 必须用登录 Cookie 取、WS 认证必须带登录 uid，两者要配对：
        # 只换其中一个（匿名 token+登录 uid / 登录 token+uid 0）都会被 1006 掐断，或者
        # 虽能连上但昵称被脱敏。没有登录态时退回匿名收流（能收，但昵称会被 B 站抹成「礼***」）。
        uid = int(self.creds.dedeuserid or 0) if self.creds.logged_in else 0
        with self.lock:
            watched = self._watched_rooms()
            cached = dict(self._danmu)
            real_of = {rid: int((self.info.get(rid) or {}).get("room_id") or rid) for rid in watched}
        if not watched:
            return []
        # 取 token 是 WBI 签名 + 一次 HTTP 往返：串行时 16 间房要好几秒，这段时间页面
        # 只能显示「引擎离线」。并发取（上限 4）把等待压到一个来回的量级，顺序仍按舞台。
        got: dict[int, dict] = {}
        with ThreadPoolExecutor(max_workers=min(4, len(watched)),
                                thread_name_prefix="deck-token") as pool:
            futs = {pool.submit(self.danmu_token, real): rid for rid, real in real_of.items()}
            for fut in as_completed(futs):
                rid = futs[fut]
                try:
                    got[rid] = fut.result()
                except Exception:  # noqa: BLE001
                    hit = cached.get(real_of[rid])
                    if not hit:
                        self.log("房间 %s 取弹幕服务器失败，跳过" % rid)
                        self._engine_ev("warn", "sync", "房间 %s 取弹幕服务器失败，本轮跳过" % rid, rid,
                                        "没有缓存 token 可兜底，这个房间本轮不会连弹幕")
                        continue
                    got[rid] = hit[1]
        items = []
        for rid in watched:
            t = got.get(rid)
            if t is None:
                continue
            # protover 按本机能不能解 brotli 决定：报 3 但没装 brotli 会收到解不开的帧
            items.append({"room": rid, "real": real_of[rid], "uid": uid, "buvid": buvid,
                          "protover": protover_for_server(),
                          "token": t["token"], "host": t["host"], "wss_port": t["wss_port"]})
        return items

    def _sync_bridge(self, force: bool = False) -> dict:
        with self.lock:
            if not self._watched_rooms():
                if self.bridge.status == "online":
                    self.bridge.sync([])
                self._publish_engine()
                return {"ok": True, "rooms": 0}
            if not force and not self._dirty and time.time() - self._token_at < TOKEN_TTL:
                return {"ok": True, "rooms": len(self.bridge.rooms)}
        items = self._bridge_items()
        if not items:
            # token 全失败时以前直接返回：浏览器压根不会启动，bridge.status 停在 idle/stopped，
            # 页面就一直显示「引擎启动中」（要等 token 恢复才动，看起来像卡死）。
            # 浏览器跟 token 无关，先把桥拉起来：起不来就是明确的 error，而不是永远的「启动中」。
            if str(getattr(self.bridge, "status", "")) != "online":
                try:
                    self.bridge.ensure()
                except Exception as e:  # noqa: BLE001
                    self._engine_ev("error", "bridge", "引擎启动失败：%s: %s" % (
                        e.__class__.__name__, e), detail="token 全失败时仍要先起浏览器")
            self._publish_engine(force=True)
            return {"ok": False, "error": "没有可用的房间", "rooms": 0}
        self.bridge.set_buvid(items[0]["buvid"])
        if self.bridge.set_cookies(self._bridge_cookies()):
            self.log("登录态变化，重建弹幕桥上下文")
            self.bridge.stop()
            self.bridge._stopping = False
        res = self.bridge.sync(items)
        if res.get("ok"):
            self._token_at = time.time()
            self._dirty = False
            # 同内容同步常被 boot 与 reconcile 并发触发，30 秒内只记一条，免得日志重复
            if force:
                sig = tuple(it["room"] for it in items)
                now = time.time()
                last = getattr(self, "_sync_log", None)
                if last is None or last[0] != sig or now - last[1] > 30:
                    self._sync_log = (sig, now)
                    self._engine_ev("info", "sync", "弹幕房间同步完成：%d 间" % len(items))
        else:
            self._engine_ev("error", "sync", "弹幕桥同步失败：%s" % (res.get("error") or "未知错误"),
                            detail="房间数 %d；引擎会由 reconcile 继续重试" % len(items))
        # 成功/失败都推一次：页面顶栏能立刻从「启动中」切到「就绪」或「离线」
        self._publish_engine(force=True)
        return res

    def _bridge_cookies(self) -> dict:
        c = self.creds
        if not c.logged_in:
            return {}
        return {k: v for k, v in {
            "SESSDATA": c.sessdata, "bili_jct": c.bili_jct, "DedeUserID": c.dedeuserid,
        }.items() if v}

    def _on_creds_changed(self) -> None:
        """登录态变了：桥上带着新 Cookie 重新握手，并重取 token（token 绑定取它时的登录态）。"""
        self._dirty = True
        self._engine_ev("info", "auth", "登录态变化：重建弹幕桥上下文并重取 token")
        with self.lock:
            self._danmu.clear()
            self._emo.clear()
        def work() -> None:
            try:
                self._sync_bridge(force=True)
            except Exception as e:  # noqa: BLE001
                self.log("登录后重建弹幕桥失败：%s" % e)
                self._engine_ev("error", "auth", "登录后重建弹幕桥失败：%s" % e,
                                detail="token 绑定登录态，重建失败时弹幕会一直停在旧上下文")
        threading.Thread(target=work, daemon=True, name="deck-relogin").start()

    def _janitor(self) -> None:
        """每 5 分钟扫一遍：清掉残留的 headless Edge 与没人用的临时 profile。"""
        self._jan_at = time.time()
        try:
            res = self.bridge.janitor()
        except Exception as e:  # noqa: BLE001
            self.log("弹幕桥自动清理失败：%s" % e)
            self._engine_ev("warn", "janitor", "弹幕桥自动清理失败：%s" % e)
            return
        self._jan_last = dict(res or {})
        if res.get("killed") or res.get("profiles"):
            self.log("弹幕桥清理完成：进程 %d / 目录 %d" % (res.get("killed", 0), res.get("profiles", 0)))

    def _reconcile(self) -> None:
        with self.lock:
            watched = self._watched_rooms()
            dirty = self._dirty
            stale = time.time() - self._token_at > TOKEN_TTL
        if not watched:
            return
        # auth/token 被拒时也要重签一次，否则旧 token 会一直重试到过期
        denied = any(s.get("state") in ("denied", "error")
                     for s in (self.hub.stat(r) for r in watched))
        if denied and time.time() - self._resync_at > 20:
            self._resync_at = time.time()
            rooms = [r for r in watched if self.hub.stat(r).get("state") in ("denied", "error")]
            self._engine_ev("warn", "token", "有房间认证/取 token 被拒，重签：%s" % (
                ",".join(str(r) for r in rooms[:5]) or "?"), detail="每 20 秒最多重签一次")
            with self.lock:
                self._danmu.clear()
            self._sync_bridge(force=True)
            return
        # CDP 保活：headless 页面卡死时读线程还活着、status 仍是 online，但所有命令
        # 都超时；连续两次探测失败就当连接失效，让下面的重建分支接手。
        if self.bridge.status == "online" and time.time() - self._ping_at > 30:
            self._ping_at = time.time()
            if self.bridge.ping():
                self._ping_fail = 0
            else:
                self._ping_fail += 1
                self._engine_ev("warn", "cdp", "CDP 保活探测失败（连续 %d 次）" % self._ping_fail,
                                detail=str(getattr(self.bridge, "last_recv_error", "") or "")[:300])
                if self._ping_fail >= 2:
                    self._ping_fail = 0
                    self.log("弹幕桥 CDP 无响应，标记重建")
                    self._engine_ev("error", "cdp", "CDP 连续无响应，标记重建弹幕桥")
                    self.bridge.status = "stale"
        if self.bridge.status != "online" or dirty or stale:
            self._sync_bridge(force=True)
