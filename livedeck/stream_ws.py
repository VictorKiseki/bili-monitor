"""把 B 站 HTTP-FLV 流以 WebSocket 二进制帧转发给前端。

浏览器的 HTTP/1.1 同源并发连接上限只有 6 条，16 路 <video> 各占一条会把 SSE 挤死；
WebSocket 不受这个连接池限制，因此直播流统一走 ws://127.0.0.1:<port>/flv?room=<id>&qn=<qn>。
顺带在这里统计每路的下行字节数，供顶栏「下行速度」与逐格悬浮提示使用。
"""
from __future__ import annotations

import threading
import time
from collections import deque
from urllib.parse import parse_qs, urlparse

from websockets.sync.server import serve

from livedeck.app import DeckError

CHUNK = 64 * 1024
RATE_WINDOW = 4.0      # 速度统计滑窗（秒）


class StreamWS:
    """极简 WS-FLV 中继：一条连接对应一个直播间。"""

    def __init__(self, deck, log) -> None:
        self.deck = deck
        self.log = log
        self.port = 0
        self._server = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._rate: dict[int, deque] = {}
        self._total: deque = deque(maxlen=4096)
        # 每个房间当前活着的上游中继；房间下屏时要主动关掉，否则没人看的 FLV 还会继续下行
        self._relay_lock = threading.Lock()
        self._relays: dict[int, list[dict]] = {}

    # ---------- 生命周期 ----------
    def start(self, host: str = "127.0.0.1", prefer: int = 0) -> int:
        last = None
        for port in ([prefer] if prefer else []) + [0]:
            try:
                srv = serve(self._handle, host, port, max_size=None, compression=None)
                break
            except OSError as e:  # 端口被占时退回随机端口
                last = e
                srv = None
        if srv is None:
            raise RuntimeError("直播流通道启动失败：%s" % last)
        self._server = srv
        self.port = int(srv.socket.getsockname()[1])
        self._thread = threading.Thread(target=self._serve, name="deck-flv-ws", daemon=True)
        self._thread.start()
        return self.port

    def _serve(self) -> None:
        try:
            self._server.serve_forever()
        except Exception:  # noqa: BLE001
            pass

    def relay_count(self) -> int:
        with self._relay_lock:
            return sum(len(v) for v in self._relays.values())

    def stop(self) -> None:
        self.prune([])
        srv = self._server
        self._server = None
        if srv is None:
            return
        try:
            srv.shutdown()
        except Exception:  # noqa: BLE001
            pass
        try:
            srv.close()
        except Exception:  # noqa: BLE001
            pass

    # ---------- 中继清理 ----------
    def _allowed(self) -> set:
        try:
            return {int(r) for r in (self.deck.active or [])}
        except Exception:  # noqa: BLE001
            return set()

    def prune(self, active=None) -> int:
        """房间下屏后立刻停掉对应中继，返回关闭的连接数。

        只靠「客户端断开」不够：浏览器刷新/隐藏时旧连接可能半死，服务端会继续把
        FLV 从 CDN 读进 socket，表现为舞台清空后仍有 ~1MB/s 的不明下行。
        """
        keep = self._allowed() if active is None else {int(x) for x in active}
        with self._relay_lock:
            rooms = [(room, list(seats)) for room, seats in self._relays.items()]
        closed = 0
        for room, seats in rooms:
            if room in keep:
                continue
            for seat in seats:
                seat["stop"] = True
                try:
                    seat["resp"].close()   # 把阻塞在 iter_bytes 的线程叫醒
                except Exception:  # noqa: BLE001
                    pass
                closed += 1
        if closed:
            self.log("直播流：%d 路已下屏，停止中继" % closed)
        return closed

    # ---------- 下行统计 ----------
    def record(self, room: int, nbytes: int) -> None:
        now = time.time()
        with self._lock:
            q = self._rate.get(int(room))
            if q is None:
                q = deque(maxlen=512)
                self._rate[int(room)] = q
            q.append((now, nbytes))
            self._total.append((now, nbytes))

    def stats(self, window: float = RATE_WINDOW) -> dict:
        """按房间返回最近 window 秒的平均下行字节/秒。"""
        now = time.time()
        cut = now - window
        with self._lock:
            rooms: dict[str, float] = {}
            for room, q in list(self._rate.items()):
                while q and q[0][0] < cut:
                    q.popleft()
                if not q:
                    self._rate.pop(room, None)
                    continue
                rooms[str(room)] = sum(n for _, n in q) / window
            while self._total and self._total[0][0] < cut:
                self._total.popleft()
            total = sum(n for _, n in self._total) / window
            active = sorted(rooms, key=lambda k: -rooms[k])
        return {"total_bps": total, "rooms": rooms, "window": window, "active": active}

    # ---------- 单个连接 ----------
    def _handle(self, conn) -> None:
        room, qn = 0, 10000
        try:
            path = conn.request.path
            qs = parse_qs(urlparse(path).query)
            room = int((qs.get("room") or ["0"])[0])
            qn = int((qs.get("qn") or ["10000"])[0] or 10000)
        except Exception:  # noqa: BLE001
            room = 0
        if not room:
            conn.close(code=4400, reason="missing room")
            return
        resp = None
        try:
            url = self.deck.flv_url(room, qn=qn)
            resp = self.deck.stream_http.send(
                self.deck.stream_http.build_request("GET", url), stream=True)
            if resp.status_code != 200:
                resp.close()
                resp = None
                url = self.deck.flv_url(room, qn=qn, force=True)
                resp = self.deck.stream_http.send(
                    self.deck.stream_http.build_request("GET", url), stream=True)
                if resp.status_code != 200:
                    resp.close()
                    conn.close(code=4404, reason="stream unavailable")
                    return
        except DeckError as e:
            conn.close(code=4404, reason=str(e)[:110])
            return
        except Exception as e:  # noqa: BLE001
            self.log("直播流 %s 拉取失败：%s" % (room, e))
            conn.close(code=4500, reason="upstream error")
            return

        sent = 0
        started = time.time()
        seat = {"resp": resp, "stop": False}
        with self._relay_lock:
            self._relays.setdefault(room, []).append(seat)
        next_room_check = time.time() + 1.0
        # 上游 FLV 常按很小的 tag 分片到达；逐片发 WebSocket 会让 16 路每秒产生上千个
        # 小帧，浏览器每个帧都要回调一次。攒到 32KB 或 120ms 再发，延迟几乎不变，帧数降一个量级。
        buf = bytearray()
        next_flush = time.time() + 0.04

        def flush() -> int:
            nonlocal next_flush
            if not buf:
                return 0
            data = bytes(buf)
            buf.clear()
            next_flush = time.time() + 0.04
            t0 = time.time()
            conn.send(data)   # 客户端慢时会阻塞在这里
            if time.time() - t0 > 5:
                return -1
            return len(data)

        try:
            for chunk in resp.iter_bytes(CHUNK):
                if seat["stop"]:
                    break
                now = time.time()
                if now >= next_room_check:
                    next_room_check = now + 1.0
                    if room not in self._allowed():
                        break
                if not chunk:
                    continue
                buf += chunk
                now = time.time()
                if len(buf) >= 16384 or now >= next_flush:
                    n = flush()
                    if n < 0:
                        # 接收端跟不上就不能无限积压在 socket 缓冲里，直接断开让前端重连
                        self.log("直播流 %s 客户端接收过慢，主动断开" % room)
                        break
                    sent += n
                    self.record(room, n)
            if buf:
                n = flush()
                if n > 0:
                    sent += n
                    self.record(room, n)
        except Exception:  # noqa: BLE001  客户端关页 / 上游断流都走这里
            pass
        finally:
            with self._relay_lock:
                seats = self._relays.get(room) or []
                if seat in seats:
                    seats.remove(seat)
                    if not seats:
                        self._relays.pop(room, None)
            try:
                resp.close()
            except Exception:  # noqa: BLE001
                pass
            if sent:
                self.log("直播流 %s 结束：%.1f MB / %.0fs" % (room, sent / 1048576.0, time.time() - started))
