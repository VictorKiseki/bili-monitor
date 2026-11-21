"""业务门面：选台、房间信息、发弹幕、扫码登录、弹幕桥调度。"""
from __future__ import annotations

import json
import threading
import time
from typing import Any
from urllib.parse import urlencode

import httpx

from collector import bili_login
from collector.bili import BiliClient
from livedeck import config
from livedeck.bridge import EdgeBridge
from livedeck.deck_base import (
    DANMU_INFO,
    EMOTICON_API,
    EMO_TTL,
    FACE_TTL,
    GROUP_NAME_MAX,
    INFO_TTL,
    LIVE_API,
    MASTER_INFO,
    MAX_COMBO_ROOMS,
    MAX_LISTEN,
    MAX_ROOMS,
    NAV,
    PLAY_INFO,
    PLAY_TTL,
    PLAY_URL,
    QUALITY_KEYS,
    QUALITY_LABEL,
    ROOM_INFO,
    SEND_MSG,
    TOKEN_TTL,
    UA,
    WATCHED_API,
    DeckError,
    _loop,
    clean_text,
    normalize_color,
)
from livedeck.deck_bridge import DeckBridge as _DeckBridge
from livedeck.deck_chat import DeckChat as _DeckChat
from shared import proxy as proxy_cfg
from livedeck.deck_info import DeckInfo as _DeckInfo
from livedeck.deck_login import DeckLogin as _DeckLogin
from livedeck.deck_rooms import DeckRooms as _DeckRooms
from livedeck.engine_log import EngineLog, EngineWatch
from livedeck.hub import DECODE_STATS, HAS_BROTLI, Hub, Msg, protover_for_server


class LiveDeck(_DeckRooms, _DeckInfo, _DeckChat, _DeckLogin, _DeckBridge):
    def __init__(self, log=lambda m: None, engine=None):
        self.log = log
        # 引擎日志：默认只留内存（单测不落盘），server.serve() 会注入写文件的实例
        self.engine = engine if engine is not None else EngineLog(enabled=False, echo=log)
        self.hub = Hub(log=log, engine=self.engine)
        self.bridge = EdgeBridge(self.hub.feed_bridge_event, log=log, engine=self.engine)
        self.lock = threading.RLock()
        self.rooms: dict[int, config.RoomEntry] = {}
        self.info: dict[int, dict] = {}
        self._anchors: dict[int, dict] = {}    # uid -> {uname, face}
        # 头像/昵称缓存（uid -> {face, uname, ts}）：启动时先用上次拉到的图渲染，
        # 缺的、过期的再交给 _prime_faces 后台补（见 deck_info）
        self._faces: dict[int, dict] = config.load_faces()
        self._own_at = 0.0     # 上次判断「采集进程在不在跑」的时间
        self._own_val = False  # 判断结果：True = 自己轮询名单房间的开播状态
        for _uid, _f in self._faces.items():
            if _f.get("face") and time.time() - float(_f.get("ts") or 0) <= FACE_TTL:
                self._anchors[_uid] = {"uname": _f.get("uname") or "", "face": _f.get("face")}
        self._qualities: dict[int, dict] = {}  # room -> {list, at}
        self.hidden: set[int] = set()          # 本工具列表里隐藏的房间
        self.colors: dict[int, dict] = {}      # 房间印象色：room -> {main, second, on}
        self.group_order: list[str] = []       # 左栏分组显示顺序（分组名，自上而下）
        self.stream_ws = None                  # server 启动 WS-FLV 中继后注入，用于速度统计
        self.active: list[int] = []
        self.listening: list[int] = []
        self.buvid = ""
        self.creds = config.load_credentials()
        self.login_checked = 0.0
        self.login_ok = False
        self.login_state = "none"        # none / ok / expired / network / risk
        self.login_error = ""
        self.login_check_ttl = 300       # 校验失败时缩短（见 deck_login.RETRY_TTL）
        self._token_at = 0.0
        self._dirty = True
        self._play: dict[int, tuple[float, str]] = {}
        self._danmu: dict[int, tuple[float, dict]] = {}
        self._emo: dict[int, tuple[float, dict]] = {}
        self._resync_at = 0.0
        self._ping_at = 0.0          # 上次 CDP 保活探测
        self._ping_fail = 0          # 连续探测失败次数
        self._jan_at = 0.0           # 上次清理残留 Edge 的时间
        self._jan_last: dict = {}    # 上次清理结果 {killed, profiles}
        self._watch = EngineWatch()  # 引擎健康体检：离线 / 零帧 / 解码失败
        self._watch_at = 0.0
        self._engine_sig: tuple[str, str] | None = None   # 上次推送的引擎状态（SSE 去重）
        self._starting_since = 0.0   # 从什么时候开始一直「启动中」（看门狗自愈用）
        self._send_at: dict[int, float] = {}
        self._last_send_global = 0.0
        # 连接池走不走代理由构造时的环境变量决定：从带旧代理的父进程继承时（代理软件
        # 已经退出），三个池会一直往死端口上撞。启动先按 config/proxy.json 对齐环境变量。
        try:
            proxy_cfg.apply_to_env()
        except Exception:  # noqa: BLE001
            pass
        self._client = BiliClient(min_interval=0.05, sessdata=self.creds.sessdata or "")
        self.stream_http = self._make_stream_http()
        self.http = self._make_http()
        # 代理配置指纹：改了就在 _proxy_tick 里整体换连接池（2026-11-20）
        self._proxy_rev = proxy_cfg.revision()
        self._proxy_lock = threading.Lock()
        self._stop = threading.Event()
        self.load_rooms()
        state = config.load_state()
        self.grid = int(state.get("grid") or 0)
        self.stream_port = 0  # 由 server 启动 WS-FLV 中继后填入，0 表示回退 HTTP 代理
        # 印象色：只存色相种子，亮度/对比度由前端按标签底色钳制
        for key, val in (state.get("colors") or {}).items():
            try:
                rid = int(key)
            except (TypeError, ValueError):
                continue
            if not isinstance(val, dict):
                continue
            m = normalize_color(val.get("main"))
            if not m:
                continue
            self.colors[rid] = {"main": m, "second": normalize_color(val.get("second")),
                                "on": bool(val.get("on", True))}
        order = state.get("group_order") or []
        if isinstance(order, list):
            # 分组名去重 + 截断；顺序即左栏顺序，缺的分组渲染时补在后面
            seen: set[str] = set()
            for g in order:
                name = clean_text(g, "").strip() if isinstance(g, str) else ""
                name = name[:GROUP_NAME_MAX]
                if name and name not in seen:
                    seen.add(name)
                    self.group_order.append(name)
        hidden = state.get("hidden") or []
        if isinstance(hidden, list):
            self.hidden = {int(r) for r in hidden if str(r).isdigit()}
        # 临时组合：把舞台上若干房间并成一条弹幕流，前端下拉里快速切换
        self.combos: list[dict] = []
        for item in state.get("combos") or []:
            if not isinstance(item, dict):
                continue
            rooms = []
            for r in item.get("rooms") or []:
                try:
                    rid = int(r)
                except (TypeError, ValueError):
                    continue
                if rid > 0 and rid not in rooms:
                    rooms.append(rid)
            if not rooms:
                continue
            self.combos.append({
                "id": int(item.get("id") or (len(self.combos) + 1)),
                "name": clean_text(str(item.get("name") or ""), "")[:24] or "组合",
                "rooms": rooms[:MAX_COMBO_ROOMS],
            })
        # 临时房间在没被手动删除前一直留在列表里（移出舞台不影响）
        for item in state.get("temp") or []:
            try:
                rid = int((item or {}).get("room_id"))
            except (TypeError, ValueError, AttributeError):
                continue
            if rid <= 0 or rid in self.rooms:
                continue
            self.rooms[rid] = config.RoomEntry(
                uid=int((item or {}).get("uid") or 0),
                name=(item or {}).get("name") or ("房间 %d" % rid),
                room_id=rid, grp=(item or {}).get("grp") or "临时", from_db=False)
        saved = state.get("active") or []
        if isinstance(saved, list):
            self.active = [int(r) for r in saved if str(r).isdigit()][:MAX_ROOMS]
        for r in self.active:
            if r not in self.rooms:
                self.rooms[r] = config.RoomEntry(uid=0, name="房间 %d" % r, room_id=r, grp="临时")
            self.hub.stat(r)
        # 只听弹幕的房间：只连弹幕 WS，不上舞台、不拉流，重启后恢复
        saved_listen = state.get("listening") or []
        if isinstance(saved_listen, list):
            for r in saved_listen:
                if not str(r).isdigit():
                    continue
                rid = int(r)
                if rid <= 0 or rid in self.active or rid in self.listening:
                    continue
                self.listening.append(rid)
                if len(self.listening) >= MAX_LISTEN:
                    break
        for r in self.listening:
            if r not in self.rooms:
                self.rooms[r] = config.RoomEntry(uid=0, name="房间 %d" % r, room_id=r, grp="临时")
            self.hub.stat(r)
        self._threads: list[threading.Thread] = []

    # ---------- HTTP 连接池（代理配置变了就整体换掉） ----------
    def _make_stream_http(self) -> httpx.Client:
        # S2（2026-11-14）：read=None 表示「永不超时」，上游静默丢包时这条连接
        # 会一直挂着线程和 socket（全库也没有 settimeout）。FLV 正常每帧都有数据，
        # 30 秒读不到一个字节就一定是坏连接，交给 httpx 抛超时重连。
        return httpx.Client(timeout=httpx.Timeout(20.0, connect=10.0, read=30.0), headers={
            "User-Agent": UA,
            "Referer": "https://live.bilibili.com/",
            "Origin": "https://live.bilibili.com",
        })

    def _make_http(self) -> httpx.Client:
        return httpx.Client(timeout=12.0, headers={
            "User-Agent": UA,
            "Referer": "https://live.bilibili.com/",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
        })

    def apply_proxy(self, force: bool = False) -> bool:
        """代理配置改过就重建三个连接池；返回是否真的换过。

        2026-11-20：监控室是「在跑着的时候改代理」最常见的场景（页面就在这），
        原来只刷新环境变量，三个连接池还绑着启动时的旧代理（代理软件崩了没起来时
        就一直连死端口）。换连接池比重启整个监控室便宜：页面、舞台、弹幕都不动，
        只有下一次请求走新连接；还挂着的旧直播流会报错，由前端自动重连。
        """
        with self._proxy_lock:
            rev = proxy_cfg.revision()
            if not force and rev == self._proxy_rev:
                return False
            new_clients = []
            try:
                with proxy_cfg.applied_for_reload():
                    new_clients.append(BiliClient(min_interval=0.05, sessdata=self.creds.sessdata or ""))
                    new_clients.append(self._make_stream_http())
                    new_clients.append(self._make_http())
            except BaseException:
                self._close_clients(new_clients)
                raise
            old = (self._client, self.stream_http, self.http)
            self._client, self.stream_http, self.http = new_clients
            self._proxy_rev = rev
            self._close_clients(old)
            return True

    @staticmethod
    def _close_clients(clients) -> None:
        for client in clients:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

    def _proxy_tick(self) -> None:
        """周期性看一眼 config/proxy.json：在监控台（8765）那边改的，这边也要跟上。"""
        try:
            if self.apply_proxy():
                self.log("代理设置已变更：已重建 HTTP 连接池（新请求按新模式走）")
        except Exception as e:               # noqa: BLE001
            self.log("代理设置生效失败：%s: %s" % (type(e).__name__, e))

    # ---------- 启动/停止 ----------
    def start(self) -> None:
        self.log("DD全能工作间·监控室 启动：%d 个候选直播间，舞台 %d，只听弹幕 %d"
                 % (len(self.rooms), len(self.active), len(self.listening)))
        self.engine.info("engine", "监控室启动：%d 个候选直播间，舞台 %d，只听弹幕 %d" % (
            len(self.rooms), len(self.active), len(self.listening)))
        # 页面此时连上来就能看到「启动中」或「等待选台」，而不是先闪一段「离线」
        self._publish_engine(force=True)
        threading.Thread(target=self._boot, name="deck-boot", daemon=True).start()
        for name, fn, every in (
            ("deck-reconcile", self._reconcile, 8),
            ("deck-info", self._refresh_info_loop, 60),
            ("deck-janitor", self._janitor, 300),
            ("deck-engine", self._engine_tick, 15),
            ("deck-proxy", self._proxy_tick, 5),
        ):
            t = threading.Thread(target=_loop(name, fn, every, self._stop), name=name, daemon=True)
            t.start()
            self._threads.append(t)

    def _boot(self) -> None:
        # 服务被强杀时弹幕桥的 Edge 不会收到 Browser.close，会一直堆在后台；
        # 启动即清一次，避免每次重启都多一份浏览器进程树。
        try:
            self.bridge.janitor()
        except Exception:  # noqa: BLE001
            pass
        self._ensure_buvid()
        self._verify_login(force=True)
        threading.Thread(target=self._prime_faces, name="deck-faces", daemon=True).start()
        if self._watched_rooms():
            self._sync_bridge(force=True)
        self._publish_engine(force=True)

    def stop(self) -> None:
        self._stop.set()
        self.engine.info("engine", "监控室停止")
        self.prune_streams()
        try:
            self.bridge.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.bridge.janitor()   # 自己也停了，把剩下的残留一起带走
        except Exception:  # noqa: BLE001
            pass
        with self._proxy_lock:
            self._close_clients((self._client, self.stream_http, self.http))

    def prune_streams(self) -> None:
        """把已下屏房间的 FLV 中继关掉（舞台清空后不该还有下行）。"""
        st = self.stream_ws
        if st is None:
            return
        try:
            st.prune(self.active)
        except Exception:  # noqa: BLE001
            pass

    # ---------- 引擎健康 ----------
    def _engine_tick(self) -> None:
        """每 15 秒体检一次：把「连不上 / 收不到 / 解不开」写成可排查的日志。

        只做判定与记录，不改状态；同一问题由 EngineWatch 按 key 去重，不刷屏。
        """
        now = time.time()
        state = self._engine_state()
        waiting = state == "waiting"
        # 卡在「启动中」超过宽限期就补一次启动（每 15 秒体检只做判定，这里才动手）
        self._starting_guard(now, state)
        with self.lock:
            watched = self._watched_rooms()
            bridge = {"status": self.bridge.status, "error": self.bridge.last_error,
                      "port": int(getattr(self.bridge, "_port", 0) or 0),
                      # 空舞台时引擎没起来是设计如此：体检要降级成 info，不报离线故障
                      "waiting": waiting}
        rooms = {}
        for r in watched:
            s = self.hub.stat(r)
            c = self.hub.cmd_counts.get(r) or {}
            rooms[r] = {"state": s.get("state"), "frames": int(s.get("frames") or 0),
                        "last_frame": float(s.get("last_frame") or 0.0),
                        "connect_at": float(s.get("connect_at") or 0.0),
                        "count": int(s.get("count") or 0),
                        "danmu": int(c.get("DANMU_MSG") or 0)}
        findings = self._watch.tick(now, bridge, rooms, {"dropped": dict(DECODE_STATS)})
        for f in findings:
            self.engine.write(f["level"], f["area"], f["msg"],
                              room=int(f.get("room") or 0), detail=str(f.get("detail") or ""))
        self._watch_at = now
        self._publish_engine()   # 状态没变时不重复推

    # ---------- 状态快照 ----------
    def diag(self) -> dict:
        """运行期自检：给「用久了变慢」这类问题用的轻量指标。"""
        hub = self.hub
        with hub.lock:
            buf_rooms = len(hub.buffers)
            buf_msgs = sum(len(b) for b in hub.buffers.values())
            subs = len(hub.subs)
            frames_total = sum(int(s.get("frames") or 0) for s in hub.stats.values())
        with self.lock:
            rooms = len(self.rooms)
            active = len(self.active)
            listening = len(self.listening)
            info_n = len(self.info)
            play_n = len(self._play)
            danmu_n = len(self._danmu)
            emo_n = len(self._emo)
        rss = threads = 0
        try:
            import psutil
            p = psutil.Process()
            rss = round(p.memory_info().rss / 1048576.0, 1)
            threads = p.num_threads()
        except Exception:  # noqa: BLE001
            pass
        relays = 0
        try:
            relays = self.stream_ws.relay_count()
        except Exception:  # noqa: BLE001
            pass
        return {
            "ok": True, "rss_mb": rss, "threads": threads,
            "sse": subs, "relays": relays,
            "rooms": rooms, "active": active, "listening": listening,
            "buffers": buf_rooms, "buffer_msgs": buf_msgs,
            "info": info_n, "play": play_n, "danmu": danmu_n, "emo": emo_n,
            "seq": hub.seq, "rate": hub.rate_per_min(),
            "janitor": {"at": round(self._jan_at, 1), "killed": int(self._jan_last.get("killed") or 0),
                        "profiles": int(self._jan_last.get("profiles") or 0)},
            # 解码能力与丢帧计数：protover 报 3 但这里 brotli=false，说明认证版本选错了
            "decode": {"brotli": HAS_BROTLI, "protover": protover_for_server(),
                       "dropped": dict(DECODE_STATS)},
            # 引擎日志摘录：/api/engine 看完整环形缓冲，这里只留最近 10 条给面板
            "engine": dict(self.engine.snapshot(10), **{
                "watch_at": round(self._watch_at, 1),
                "frames": frames_total,
            }),
            "samples": {str(r): v for r, v in list(hub.cmd_samples.items())},
            "cmds": {str(r): dict(sorted(c.items(), key=lambda kv: -kv[1])[:40])
                     for r, c in list(hub.cmd_counts.items())},
        }

    def snapshot(self) -> dict:
        with self.lock:
            active = list(self.active)
            listening = [r for r in self.listening if r not in active]
            bridge_state = str(self.bridge.status)
            bridge_error = self.bridge.last_error
        watched = list(active)
        for r in listening:
            if r not in watched:
                watched.append(r)
        states = {}
        info_out: dict[str, dict] = {}
        with self.lock:
            infos = dict(self.info)
        for r in watched:
            s = self.hub.stat(r)
            info = infos.get(r) or infos.get(int(s.get("real") or 0)) or {}
            states[str(r)] = {"state": s.get("state"), "count": s.get("count", 0),
                              "online": int(info.get("online") or s.get("online", 0)),
                              "viewers": int(s.get("viewers") or 0),
                              "watched": int(s.get("watched") or 0),
                              "guards": int(info.get("guards") or 0),
                              "live_status": int(info.get("live_status") or 0),
                              "last_ts": s.get("last_ts", 0)}
            info_out[str(r)] = {
                "uname": info.get("uname") or "", "title": info.get("title") or "",
                "live_status": int(info.get("live_status") or 0),
                "known": bool(info),
                "online": int(info.get("online") or 0),
                "watched": int(info.get("watched") or 0),
                "watched_kind": info.get("watched_kind") or "",
                "guards": int(info.get("guards") or 0),
                "area": info.get("area_name") or "",
            }
        try:
            net = self.stream_ws.stats() if self.stream_ws is not None else {}
        except Exception:  # noqa: BLE001
            net = {}
        with self.lock:
            hidden = len(self.hidden)
        return {
            "login": self.login_snapshot(),
            "rooms": self.rooms_payload(),
            "active": active,
            "listening": listening,
            "max_rooms": MAX_ROOMS,
            "max_listen": MAX_LISTEN,
            # status 是对外口径（waiting = 舞台为空、等待选台），state 保留桥的原始状态便于排查
            "bridge": {"status": self._engine_state(), "state": bridge_state,
                       "error": bridge_error, "rooms": states},
            "info": info_out,
            "grid": self.grid,
            "combos": list(self.combos),
            "colors": self.colors_payload(),
            "group_order": list(self.group_order),
            "stream_port": self.stream_port,
            "net": net or {"total_bps": 0.0, "rooms": {}, "window": 4.0},
            "hidden": hidden,
            "stats": {"rate": self.hub.rate_per_min(), "seq": self.hub.seq},
            "now": time.time(),
        }

# 别名用完即删，保持 livedeck.app 的模块命名空间与拆分前完全一致。
del _DeckRooms, _DeckInfo, _DeckChat, _DeckLogin, _DeckBridge
