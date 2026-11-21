"""弹幕帧解码、按房间缓冲与 SSE 分发。

帧协议（B 站直播弹幕 WS）：
    16 字节头  struct.unpack(">IHHII", ...) -> (总长, 头长, 协议版本, 操作码, 序号)
    操作码 2 心跳、3 心跳回复(人气值)、5 消息、7 认证、8 认证回复
    协议版本 0/1 明文、2 zlib、3 brotli
    消息体按 b"\x00" 分片，每片是一条 JSON
"""
from __future__ import annotations

import base64
import json
import queue
import struct
import threading
import time
import zlib
from collections import deque
from dataclasses import dataclass, field

try:
    import brotli
except Exception:  # noqa: BLE001
    brotli = None

# brotli 是可选依赖（requirements-optional.txt），自带运行时里不一定装了。
# 服务器按认证包里的 protover 决定压缩方式（3=brotli / 2=zlib / 0,1=明文），
# 所以认证前必须按本机能力选版本：报 3 却解不开 brotli 时，ver=3 的帧会在解压这一步
# 被整包丢弃，表现成「认证通过、WS 也连着，但弹幕流一条都没有」。
HAS_BROTLI = brotli is not None

# 解不开被丢弃的帧计数（键是声明的协议版本），暴露到 /api/diag，别再静默丢。
DECODE_STATS: dict[str, int] = {}

# 大航海三档：guard_level 1=总督、2=提督、3=舰长（B 站约定，数字越小越贵）。
GUARD_NAMES = {1: "总督", 2: "提督", 3: "舰长"}


def protover_for_server() -> int:
    """认证包该报的 protover：有 brotli 就 3，没有退回 stdlib 的 zlib（2）。"""
    return 3 if HAS_BROTLI else 2


@dataclass
class Msg:
    seq: int
    room: int
    kind: str                 # danmaku / gift / sc / guard / online / status
    ts: float
    uid: int = 0
    uname: str = ""
    text: str = ""
    color: str = "#ffffff"
    medal: str = ""
    price: float = 0.0
    num: int = 0
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = {
            "seq": self.seq, "room": self.room, "kind": self.kind,
            "ts": round(self.ts, 3), "uid": self.uid, "uname": self.uname,
            "text": self.text, "color": self.color, "medal": self.medal,
            "price": self.price, "num": self.num,
        }
        if self.extra:
            d.update(self.extra)
        return d


def _hex_color(v) -> str:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return "#ffffff"
    if n <= 0 or n > 0xFFFFFF:
        return "#ffffff"
    return "#%06x" % n


def _pb_varint(buf: bytes, i: int) -> tuple[int, int]:
    val = 0
    shift = 0
    while i < len(buf):
        b = buf[i]
        i += 1
        val |= (b & 0x7F) << shift
        shift += 7
        if not b & 0x80:
            return val, i
    raise ValueError("bad varint")


def pb_fields(buf: bytes) -> dict[int, list]:
    """极简 protobuf 解析：字段号 -> [值]（wire0 为 int、wire2 为 bytes）。"""
    out: dict[int, list] = {}
    i = 0
    n = len(buf)
    while i < n:
        key, i = _pb_varint(buf, i)
        f, wt = key >> 3, key & 7
        if wt == 0:
            v, i = _pb_varint(buf, i)
        elif wt == 1:
            v = buf[i:i + 8]
            i += 8
        elif wt == 2:
            ln, i = _pb_varint(buf, i)
            v = buf[i:i + ln]
            i += ln
        elif wt == 5:
            v = buf[i:i + 4]
            i += 4
        else:
            raise ValueError("bad wire type %d" % wt)
        out.setdefault(f, []).append(v)
    return out


def pb_int(fields: dict, num: int, default: int = 0) -> int:
    v = fields.get(num) or []
    return int(v[0]) if v and isinstance(v[0], int) else default


def pb_bytes(fields: dict, num: int) -> bytes:
    v = fields.get(num) or []
    return v[0] if v and isinstance(v[0], (bytes, bytearray)) else b""


def pb_str(fields: dict, num: int) -> str:
    return bytes(pb_bytes(fields, num)).decode("utf-8", "replace")


def pb_msg(fields: dict, num: int) -> dict:
    raw = pb_bytes(fields, num)
    if not raw:
        return {}
    try:
        return pb_fields(bytes(raw))
    except Exception:  # noqa: BLE001
        return {}


def _brotli_body(body: bytes) -> bytes | None:
    if brotli is None:
        return None
    try:
        return brotli.decompress(body)
    except Exception:  # noqa: BLE001
        return None


def _zlib_body(body: bytes) -> bytes | None:
    # 服务器偶尔用裸 deflate（-15）或自动探测头（47），三种都试
    for wbits in (15, -15, 47):
        try:
            return zlib.decompress(body, wbits)
        except Exception:  # noqa: BLE001
            continue
    return None


def decode_body(ver: int, body: bytes) -> bytes | None:
    """把消息体按协议版本解压；声明的版本解不开时按另一种压缩兜底。

    服务器不总守约（请求 protover=1 也可能回 ver=2 的 zlib 帧），所以先按声明来，
    失败再试另一种，最后才认「本来就是明文」。都失败返回 None，由调用方记数丢弃。
    """
    if ver in (0, 1):
        return body or None
    funcs = (_brotli_body, _zlib_body) if ver == 3 else (_zlib_body, _brotli_body)
    for fn in funcs:
        data = fn(body)
        if data is not None:
            return data
    if body[:1] in (b"{", b"["):     # 明文被标成了压缩版本
        return body
    return None


def decode_packet(raw: bytes) -> list[dict]:
    """把一条 WS 二进制消息拆成若干 JSON 事件（可能粘包）。"""
    out: list[dict] = []
    off = 0
    n = len(raw)
    while off + 16 <= n:
        try:
            plen, hlen, ver, op, _seq = struct.unpack(">IHHII", raw[off:off + 16])
        except struct.error:
            break
        if plen < 16 or hlen < 16 or off + plen > n:
            break
        body = raw[off + hlen:off + plen]
        off += plen
        if op in (2, 3):
            out.append({"__op": op, "__body": body})
            continue
        if op == 8:
            try:
                out.append({"__op": 8, "__json": json.loads(body.decode("utf-8", "replace") or "{}")})
            except ValueError:
                out.append({"__op": 8, "__json": {}})
            continue
        if op != 5:
            continue
        data = decode_body(ver, body)
        if data is None:
            key = "ver%d" % ver
            DECODE_STATS[key] = DECODE_STATS.get(key, 0) + 1
            continue
        for part in data.split(b"\x00"):
            part = part.strip()
            if not part:
                continue
            try:
                j = json.loads(part.decode("utf-8", "replace"))
            except ValueError:
                continue
            if isinstance(j, dict):
                out.append(j)
    return out


class Hub:
    """所有房间的弹幕汇聚点：缓冲 + 订阅。"""

    def __init__(self, log=lambda m: None, engine=None):
        self.log = log
        # 引擎日志（livedeck/engine_log.py）：None 时只走 log 回调，不落盘
        self.engine = engine
        self._drops_seen = 0
        self._drop_log_at = 0.0
        self.lock = threading.RLock()
        self.seq = 0
        self.buffers: dict[int, deque] = {}
        self.stats: dict[int, dict] = {}
        self.subs: set[queue.Queue] = set()
        self.rate = deque(maxlen=600)   # (ts,) 用于算速率
        # 刚发出的弹幕：B 站会把同一条再广播回来，收到时要吞掉，否则自己发的一行会上屏两次
        self._echoes: list[dict] = []
        # 每个房间收到过哪些 cmd（排查「某类消息收不到」时看 /api/diag 的 cmds）
        self.cmd_counts: dict[int, dict[str, int]] = {}
        # 礼物类 cmd 的原始样本（排查字段用，最多每房 3 条）
        self.cmd_samples: dict[int, list] = {}
        if not HAS_BROTLI:
            self.log("brotli 未安装：弹幕认证走 protover=2（zlib），不影响收弹幕")

    # ---------- 缓冲 ----------
    def _buf(self, room: int) -> deque:
        b = self.buffers.get(room)
        if b is None:
            b = deque(maxlen=400)
            self.buffers[room] = b
        return b

    def stat(self, room: int) -> dict:
        s = self.stats.get(room)
        if s is None:
            s = {"state": "idle", "detail": "", "count": 0, "last_ts": 0.0,
                 "online": 0, "live_status": 0, "since": time.time(), "connect_at": 0.0,
                 # 帧级指标：排查「WS 通但没东西」时，count 只算弹幕类消息，分不清是
                 # 没帧、还是帧都解不开/都不是弹幕
                 "frames": 0, "bytes": 0, "last_frame": 0.0, "danmu": 0}
            self.stats[room] = s
        return s

    def reset_room(self, room: int) -> None:
        # 房间下屏就把缓冲一起丢掉：反复开关不同房间时，这些缓冲会一直留在内存里
        with self.lock:
            self.stats.pop(room, None)
            self.buffers.pop(room, None)
            self.cmd_counts.pop(room, None)
            self.cmd_samples.pop(room, None)

    def history(self, rooms: list[int], limit: int = 120) -> list[dict]:
        with self.lock:
            items: list[dict] = []
            for r in rooms:
                b = self.buffers.get(r)
                if b:
                    items.extend(b)
            items.sort(key=lambda m: m.seq)
            return [m.to_dict() for m in items[-limit:]]

    # ---------- 订阅 ----------
    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=4000)
        with self.lock:
            self.subs.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self.lock:
            self.subs.discard(q)

    def publish(self, event: dict) -> None:
        with self.lock:
            subs = list(self.subs)
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass

    # ---------- 状态 ----------
    def set_status(self, room: int, state: str, detail: str = "") -> None:
        with self.lock:
            s = self.stat(room)
            s["state"] = state
            s["detail"] = detail
            if state == "open":
                s["connect_at"] = time.time()
                s["since"] = time.time()
        self.publish({"type": "status", "room": room, "state": state, "detail": detail,
                      "ts": time.time()})

    # ---------- 引擎日志 ----------
    def _ev(self, level: str, area: str, message: str, room: int = 0, detail: str = "") -> None:
        eng = self.engine
        if eng is None:
            return
        try:
            eng.write(level, area, message, room=room, detail=detail)
        except Exception:  # noqa: BLE001
            pass

    def _note_drops(self, room: int) -> None:
        """解码失败计数在涨就记 error（每 60 秒最多一条），别让整包丢弃再次静默。"""
        total = sum(int(v or 0) for v in DECODE_STATS.values())
        if total <= self._drops_seen:
            return
        first = total - self._drops_seen
        self._drops_seen = total
        if self.engine is None:
            return
        now = time.time()
        if now - self._drop_log_at < 60:
            return
        self._drop_log_at = now
        self._ev("error", "decode", "解码失败丢弃 %d 帧（累计 %d）" % (first, total), room,
                 "认证版本与本机解压能力不匹配／帧损坏：看 /api/diag 的 decode.brotli 与 decode.protover")

    # ---------- 入口 ----------
    def feed_bridge_event(self, ev: dict) -> None:
        """Edge 桥回传的事件。"""
        t = ev.get("type")
        room = int(ev.get("room") or 0)
        if not room:
            return
        if t == "open":
            self.set_status(room, "open")
        elif t == "connecting":
            self.set_status(room, "connecting", "第 %s 次" % (int(ev.get("tries") or 0) + 1))
        elif t == "close":
            code = int(ev.get("code") or 0)
            reason = str(ev.get("reason") or "")
            self.log("弹幕桥 %s 断开：code=%s %s" % (room, code, reason[:80]))
            self.set_status(room, "closed", "code %s" % code)
            self._ev("warn", "ws", "房间 %s 弹幕 WS 断开：code=%s" % (room, code), room,
                     reason[:200] or "服务端主动断开或网络抖动，页面侧会自动重连")
        elif t == "denied":
            msg = str(ev.get("msg") or "")
            self.log("弹幕桥 %s 取 token 被拒：code=%s %s" % (room, ev.get("code"), msg[:80]))
            self.set_status(room, "denied", "token code=%s %s" % (ev.get("code"), msg))
            self._ev("error", "token", "房间 %s 取弹幕 token 被拒：code=%s" % (room, ev.get("code")),
                     room, msg[:300])
        elif t == "error":
            msg = str(ev.get("msg") or "")
            self.set_status(room, "error", msg[:160])
            self._ev("warn", "page", "房间 %s 页面侧建连报错" % room, room, msg[:300])
        elif t == "closed":
            self.set_status(room, "removed", str(ev.get("why") or ""))
        elif t == "frame":
            try:
                raw = base64.b64decode(ev.get("data") or "")
            except Exception:  # noqa: BLE001
                return
            self.feed_frame(room, raw)

    def feed_frame(self, room: int, raw: bytes) -> None:
        now = time.time()
        with self.lock:
            s = self.stat(room)
            s["frames"] = int(s.get("frames") or 0) + 1
            s["bytes"] = int(s.get("bytes") or 0) + len(raw)
            s["last_frame"] = now
        events = decode_packet(raw)
        self._note_drops(room)
        for j in events:
            op = j.get("__op")
            if op == 8:
                body = j.get("__json") or {}
                code = int(body.get("code") or 0)
                if code == 0:
                    self.log("弹幕桥 %s 认证通过" % room)
                    self.set_status(room, "auth", "认证通过")
                    self._ev("info", "auth", "房间 %s 弹幕认证通过" % room, room)
                else:
                    self.log("弹幕桥 %s 认证被拒：code=%s" % (room, code))
                    self.set_status(room, "denied", "auth code=%s" % code)
                    self._ev("error", "auth", "房间 %s 弹幕认证被拒：code=%s" % (room, code), room,
                             "认证被拒通常代表 token 与 uid/buvid 不配对，或登录态已失效")
                continue
            if op is not None:
                continue
            self._handle_message(room, j, now)

    def _handle_message(self, room: int, j: dict, now: float) -> None:
        cmd = str(j.get("cmd") or "")
        if not cmd:
            return
        self._count_cmd(room, cmd, j)
        if cmd.startswith("DANMU_MSG"):
            self._handle_danmaku(room, j, now)
        elif cmd == "SUPER_CHAT_MESSAGE":
            self._handle_super_chat(room, j, now)
        elif cmd in ("SEND_GIFT_V2", "COMBO_SEND_V2") or (j.get("data") or {}).get("pb"):
            self._handle_gift_v2(room, j, now)
        elif cmd == "SEND_GIFT":
            self._handle_gift(room, j, now)
        elif cmd == "GUARD_BUY":
            self._handle_guard(room, j, now)
        elif cmd == "ONLINE_RANK_COUNT":
            self._handle_online_rank(room, j, now)
        elif cmd == "WATCHED_CHANGE":
            self._handle_watched(room, j, now)
        elif cmd in ("LIVE", "PREPARING", "ROOM_REAL_TIME_MESSAGE_UPDATE", "ROOM_CHANGE"):
            self._handle_live_status(room, cmd, now)

    def _count_cmd(self, room: int, cmd: str, j: dict) -> None:
        """给每个房间记消息条数，礼物/上舰/SC 留 3 条样本给调试用。"""
        with self.lock:
            c = self.cmd_counts.get(room)
            if c is None:
                c = self.cmd_counts[room] = {}
            c[cmd] = c.get(cmd, 0) + 1
            if "GIFT" in cmd or cmd in ("GUARD_BUY", "SUPER_CHAT_MESSAGE"):
                smp = self.cmd_samples.setdefault(room, [])
                if len(smp) < 3:
                    import json as _json
                    smp.append(_json.dumps(j, ensure_ascii=False)[:1600])

    def _handle_danmaku(self, room: int, j: dict, now: float) -> None:
        """普通弹幕：解析时间戳/牌子/表情，回显过的跳过，然后广播。"""
        info = j.get("info") or []
        if len(info) < 3:
            return
        meta = info[0] or []
        user = info[2] or []
        text = str(info[1]) if len(info) > 1 else ""
        medal = ""
        if len(info) > 3 and isinstance(info[3], list) and info[3]:
            m = info[3]
            if len(m) > 2 and m[1]:
                medal = "%s·%s" % (m[1], m[0])
        extra = {}
        # 表情弹幕：info[0][13] 是表情信息（url/宽高/唯一名），info[1] 是表情中文名
        emo = meta[13] if len(meta) > 13 else None
        if isinstance(emo, dict) and (emo.get("url") or emo.get("emoticon_unique")):
            url = str(emo.get("url") or "")
            if url.startswith("http://"):
                url = "https://" + url[len("http://"):]
            extra["emoticon"] = {
                "url": url,
                "unique": str(emo.get("emoticon_unique") or ""),
                "width": int(emo.get("width") or 0) or 0,
                "height": int(emo.get("height") or 0) or 0,
            }
        # 自己刚才发的弹幕会被 B 站广播回来：本地已经显示过，这里吞掉避免两条
        if self._take_echo(room, str(user[1]) if len(user) > 1 else "", text,
                           str((extra.get("emoticon") or {}).get("unique") or "")):
            return
        with self.lock:
            s = self.stat(room)
            s["danmu"] = int(s.get("danmu") or 0) + 1
        msg = Msg(
            seq=self._next_seq(), room=room, kind="danmaku", ts=now,
            uid=int(user[0]) if user and str(user[0]).isdigit() else 0,
            uname=str(user[1]) if len(user) > 1 else "",
            text=text,
            color=_hex_color(meta[3] if len(meta) > 3 else 16777215),
            medal=medal,
            extra=extra,
        )
        self._emit(msg)

    def _handle_super_chat(self, room: int, j: dict, now: float) -> None:
        """醒目留言（SC）。"""
        d = j.get("data") or {}
        ui = d.get("user_info") or {}
        msg = Msg(seq=self._next_seq(), room=room, kind="sc", ts=now,
                  uid=int(d.get("uid") or 0), uname=str(ui.get("uname") or ""),
                  text=str(d.get("message") or ""), price=float(d.get("price") or 0),
                  extra={"duration": d.get("time") or 0})
        self._emit(msg)
        return

    def _handle_gift_v2(self, room: int, j: dict, now: float) -> None:
        """2026 版 protobuf 礼物（SEND_GIFT_V2 / COMBO_SEND_V2 / 带 pb 的 data）。"""
        # 2026 起普通礼物走 SEND_GIFT_V2，data 只有一个 base64 的 protobuf（SendGiftV2）。
        # 字段号来自 bilibili 直播 protobuf：1 uid / 2 uname / 8 medal_info / 10 gift_list(重复)；
        # GiftItem: 1 gift_id / 2 gift_name / 3 num / 5 price / 7 total_coin / 8 coin_type。
        d = j.get("data") or {}
        raw = base64.b64decode(str(d.get("pb") or ""))
        top = pb_fields(raw) if raw else {}
        uid = pb_int(top, 1)
        uname = pb_str(top, 2)
        medal = pb_msg(top, 8)
        mname = pb_str(medal, 6)
        badge = (mname + str(pb_int(medal, 5))) if mname else ""
        for item in top.get(10) or []:
            if not isinstance(item, (bytes, bytearray)):
                continue
            try:
                g = pb_fields(bytes(item))
            except Exception:  # noqa: BLE001
                continue
            gid = pb_int(g, 1)
            name = pb_str(g, 2) or ("礼物 %d" % gid if gid else "礼物")
            num = pb_int(g, 3) or 1
            coin = pb_str(g, 8) or "gold"
            price = pb_int(g, 7) or pb_int(g, 5) * num
            if not uname:
                uname = pb_str(pb_msg(g, 29), 1)
            self._emit(Msg(seq=self._next_seq(), room=room, kind="gift", ts=now,
                           uid=uid, uname=uname, text=name, num=num, price=float(price),
                           medal=badge,
                           extra={"gift": name, "coin_type": coin, "gift_id": gid}))
        return

    def _handle_gift(self, room: int, j: dict, now: float) -> None:
        """老版 JSON 礼物（SEND_GIFT）。"""
        d = j.get("data") or {}
        num = int(d.get("num") or 1)
        coin = float(d.get("total_coin") or 0)
        msg = Msg(seq=self._next_seq(), room=room, kind="gift", ts=now,
                  uid=int(d.get("uid") or 0), uname=str(d.get("uname") or ""),
                  text=str(d.get("giftName") or ""), num=num, price=coin,
                  extra={"gift": d.get("giftName") or "", "coin_type": d.get("coin_type") or "gold"})
        self._emit(msg)
        return

    def _handle_guard(self, room: int, j: dict, now: float) -> None:
        """上舰 / 续舰（GUARD_BUY）：舰长、提督、总督三档，带 guard_level 给前端染等级色。"""
        d = j.get("data") or {}
        level = int(d.get("guard_level") or 0)
        name = str(d.get("gift_name") or "") or GUARD_NAMES.get(level, "舰长")
        months = int(d.get("num") or 1)
        msg = Msg(seq=self._next_seq(), room=room, kind="guard", ts=now,
                  uid=int(d.get("uid") or 0), uname=str(d.get("username") or ""),
                  text=name, num=months,
                  price=float(d.get("price") or 0) / 1000.0,
                  extra={"guard_level": level, "gift": name, "months": months})
        self._emit(msg)
        return

    def _handle_online_rank(self, room: int, j: dict, now: float) -> None:
        """在线人数（ONLINE_RANK_COUNT），有数才推。"""
        # B 站房间页右侧「房间观众(N)」就是这里的 online_count（在线榜人数）。
        d = j.get("data") or {}
        num = int(d.get("online_count") or d.get("count") or 0)
        if num:
            with self.lock:
                s = self.stat(room)
                s["viewers"] = num
                s["online"] = num
            self.publish({"type": "viewers", "room": room, "viewers": num, "ts": now})
        return

    def _handle_watched(self, room: int, j: dict, now: float) -> None:
        """累计观看人数（WATCHED_CHANGE），有数才推。"""
        # 「看过」是累计观看人数，和「房间观众」不是一个口径，单独存。
        d = j.get("data") or {}
        num = int(d.get("num") or 0)
        if num:
            with self.lock:
                s = self.stat(room)
                s["watched"] = num
            self.publish({"type": "watched", "room": room, "watched": num, "ts": now})
        return

    def _handle_live_status(self, room: int, cmd: str, now: float) -> None:
        """开播/下播/房间信息变化（LIVE / PREPARING / ROOM_CHANGE 等）。"""
        with self.lock:
            s = self.stat(room)
            s["live_status"] = 1 if cmd == "LIVE" else (2 if cmd == "PREPARING" else s["live_status"])
        self.publish({"type": "room_live", "room": room, "live_status": s.get("live_status", 0), "ts": now})
        return

    def emit_local(self, room: int, uname: str, text: str, color: str = "#7be2ff",
                   extra: dict | None = None) -> None:
        """本地回显刚发出的弹幕（服务端已经 200 时立刻上屏）。"""
        data = {"self": True}
        if extra:
            data.update(extra)
        msg = Msg(seq=self._next_seq(), room=room, kind="danmaku", ts=time.time(),
                  uname=uname, text=text, color=color, extra=data)
        self._emit(msg)

    def pending_echo(self, room: int, uname: str, text: str, unique: str = "") -> None:
        """发送前登记一条本地回显，用来吞掉 B 站广播回来的同一条弹幕。"""
        now = time.time()
        with self.lock:
            self._echoes = [e for e in self._echoes if now - e["ts"] < 25]
            self._echoes.append({"room": int(room), "uname": (uname or "").strip(),
                                 "text": (text or "").strip(), "unique": unique or "", "ts": now})

    def cancel_echo(self, room: int, text: str, unique: str = "") -> None:
        """发送失败时撤销登记，别把别人发的同样内容也吞掉。"""
        text = (text or "").strip()
        with self.lock:
            for i in range(len(self._echoes) - 1, -1, -1):
                e = self._echoes[i]
                if e["room"] == int(room) and e["text"] == text and (not unique or e["unique"] == unique):
                    self._echoes.pop(i)
                    break

    def _take_echo(self, room: int, uname: str, text: str, unique: str = "") -> bool:
        """这条 DANMU_MSG 是不是自己刚发的那条？是就吞掉（本地已经上屏过）。"""
        now = time.time()
        text = (text or "").strip()
        with self.lock:
            self._echoes = [e for e in self._echoes if now - e["ts"] < 25]
            for i, e in enumerate(self._echoes):
                if e["room"] != int(room):
                    continue
                if e["uname"] and uname and e["uname"] != uname:
                    continue
                same_text = bool(e["text"]) and e["text"] == text
                same_uniq = bool(e["unique"]) and e["unique"] == (unique or "")
                if not (same_text or same_uniq):
                    continue
                self._echoes.pop(i)
                return True
        return False

    def _next_seq(self) -> int:
        with self.lock:
            self.seq += 1
            return self.seq

    def _emit(self, msg: Msg) -> None:
        with self.lock:
            self._buf(msg.room).append(msg)
            s = self.stat(msg.room)
            s["count"] += 1
            s["last_ts"] = msg.ts
            self.rate.append(msg.ts)
        self.publish({"type": "msg", "data": msg.to_dict(), "ts": msg.ts, "room": msg.room})

    def rate_per_min(self) -> int:
        now = time.time()
        with self.lock:
            while self.rate and now - self.rate[0] > 60:
                self.rate.popleft()
            return len(self.rate)
