"""弹幕引擎运行日志与健康监测（2026-11-11）。

为什么单独一份
--------------
「弹幕引擎离线」「已连通但收不到弹幕」这类现场，过去只留下一条 DevTools 超时或
一条 1006 断开，真正的原因（浏览器没起来、profile 被锁、CDP 假死、token 被风控、
brotli 缺失整包丢帧）散落在 stdout、Edge 自己的 stderr 和内存状态里，事后只能靠
手工挂探针复现。

这里做两件事：
1. EngineLog：引擎关键节点统一写 `logs/livedeck_engine.log`（按大小轮转），
   同时在内存里留最近若干条，供 `/api/engine` 查看；
2. EngineWatch：按固定节拍检查「引擎离线 / WS 在但一帧都没收到 / 帧在来但没有弹幕 /
   解码失败在涨」，把状态翻译成带排查提示的 warn / error 日志，且同一问题不刷屏。

日志格式（一行一条，UTF-8、LF）：

    2026-11-11 00:12:03.123 WARN  [bridge] 弹幕桥启动失败：BridgeError: ... room=2001
"""
from __future__ import annotations

import threading
import time
from collections import deque
from pathlib import Path

# 默认落在仓库根的 logs/ 下；logs/ 在 .gitignore 里，不会进包
DEFAULT_PATH = Path(__file__).resolve().parent.parent / "logs" / "livedeck_engine.log"

LEVEL_ORDER = {"info": 10, "warn": 20, "error": 30}


def _stamp(ts: float) -> str:
    lt = time.localtime(ts)
    return "%s.%03d" % (time.strftime("%Y-%m-%d %H:%M:%S", lt), int((ts % 1) * 1000))


class EngineLog:
    """引擎日志：文件 + 内存环形缓冲。

    enabled=False 时只留内存（测试与「不要落盘」的场景），任何写盘失败都不抛异常——
    日志坏了不能把引擎一起带崩。
    """

    def __init__(self, path: str | Path | None = None, echo=None, enabled: bool = True,
                 max_bytes: int = 2 * 1024 * 1024, backups: int = 3, ring_size: int = 800):
        self.path = Path(path) if path else DEFAULT_PATH
        self.echo = echo
        self.enabled = bool(enabled)
        self.max_bytes = max(4096, int(max_bytes))
        self.backups = max(1, int(backups))
        self.counts = {"info": 0, "warn": 0, "error": 0}
        self._ring = deque(maxlen=ring_size)
        self._lock = threading.RLock()

    # ---------- 写 ----------
    def write(self, level: str, area: str, message: str, room: int = 0, detail: str = "") -> dict:
        level = level if level in LEVEL_ORDER else "info"
        ts = time.time()
        entry = {"ts": round(ts, 3), "time": _stamp(ts), "level": level,
                 "area": str(area or ""), "room": int(room or 0), "msg": str(message or "")}
        if detail:
            entry["detail"] = str(detail)[:1000]
        with self._lock:
            self._ring.append(entry)
            self.counts[level] = self.counts.get(level, 0) + 1
        # 只有 warn / error 才回显到常规日志（info 只进文件，避免刷屏）
        if level != "info" and self.echo is not None:
            try:
                self.echo("[%s] %s" % (area, message))
            except Exception:  # noqa: BLE001
                pass
        if self.enabled:
            self._append(entry)
        return entry

    def info(self, area: str, message: str, room: int = 0, detail: str = "") -> dict:
        return self.write("info", area, message, room, detail)

    def warn(self, area: str, message: str, room: int = 0, detail: str = "") -> dict:
        return self.write("warn", area, message, room, detail)

    def error(self, area: str, message: str, room: int = 0, detail: str = "") -> dict:
        return self.write("error", area, message, room, detail)

    def _append(self, entry: dict) -> None:
        line = "%s %-5s [%s] %s" % (entry["time"], entry["level"].upper(),
                                    entry["area"], entry["msg"])
        if entry.get("room"):
            line += " room=%d" % entry["room"]
        if entry.get("detail"):
            line += " | " + entry["detail"].replace("\n", " ").replace("\r", " ")
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self.path.exists() and self.path.stat().st_size > self.max_bytes:
                    self._rotate()
                with open(self.path, "a", encoding="utf-8", newline="\n") as f:
                    f.write(line + "\n")
            except OSError:
                # 盘满 / 权限不足：降级为只留内存，不影响引擎
                self.enabled = False

    def _rotate(self) -> None:
        p = self.path
        oldest = p.with_name(p.name + ".%d" % self.backups)
        try:
            if oldest.exists():
                oldest.unlink()
            for i in range(self.backups - 1, 0, -1):
                src = p.with_name(p.name + ".%d" % i)
                if src.exists():
                    src.replace(p.with_name(p.name + ".%d" % (i + 1)))
            p.replace(p.with_name(p.name + ".1"))
        except OSError:
            pass

    # ---------- 读 ----------
    def tail(self, n: int = 100) -> list[dict]:
        with self._lock:
            return list(self._ring)[-max(1, int(n)):]

    def last_error(self) -> dict | None:
        with self._lock:
            for e in reversed(self._ring):
                if e["level"] == "error":
                    return dict(e)
        return None

    def snapshot(self, n: int = 20) -> dict:
        return {
            "path": str(self.path) if self.enabled else "",
            "counts": dict(self.counts),
            "recent": self.tail(n),
            "last_error": self.last_error(),
        }


class EngineWatch:
    """引擎健康检查：把「不正常的静止」翻译成可排查的日志。

    tick() 是纯函数式判定（输入现成状态、输出待写日志），便于离线测试；
    去重按 key 记时间，同一问题每 repeat 秒最多提一次，不刷屏。
    """

    def __init__(self, offline_after: float = 90.0, silent_after: float = 90.0,
                 no_danmu_after: float = 600.0, repeat: float = 600.0):
        self.offline_after = float(offline_after)
        self.silent_after = float(silent_after)
        self.no_danmu_after = float(no_danmu_after)
        self.repeat = float(repeat)
        self._bad_since = 0.0
        self._waiting = False     # 上次体检时舞台是不是空的（空舞台不是故障）
        self._seen: dict[str, float] = {}
        self._drops = 0
        self._drop_at = 0.0

    def reset(self) -> None:
        self._bad_since = 0.0
        self._waiting = False
        self._seen.clear()
        self._drops = 0

    def _due(self, key: str, now: float) -> bool:
        last = self._seen.get(key)
        if last is not None and now - last < self.repeat:
            return False
        self._seen[key] = now
        return True

    def tick(self, now: float, bridge: dict | None, rooms: dict, decode: dict | None) -> list[dict]:
        """返回待写日志：[{level, area, room, msg, detail}]。"""
        out: list[dict] = []
        bridge = bridge or {}
        status = str(bridge.get("status") or "unknown")
        # 舞台为空：引擎按设计就没起，这时「离线」不是故障。只说一次「等待选台」，
        # 否则每次启动空舞台都堆一条 error，把真正的故障埋掉。
        if bridge.get("waiting"):
            self._bad_since = 0.0
            if not self._waiting:
                self._waiting = True
                out.append({
                    "level": "info", "area": "watch", "room": 0,
                    "msg": "舞台为空，弹幕引擎待命：等待选台",
                    "detail": "选中房间后会立刻尝试连接弹幕引擎，无需手动重启",
                })
        elif status != "online":
            self._waiting = False
            if not self._bad_since:
                self._bad_since = now
            bad_for = now - self._bad_since
            if bad_for >= self.offline_after and self._due("offline", now):
                err = str(bridge.get("error") or "")[:400]
                out.append({
                    "level": "error", "area": "watch", "room": 0,
                    "msg": "弹幕引擎离线 %.0f 秒（status=%s）" % (bad_for, status),
                    "detail": (err + "｜" if err else "") +
                              "排查顺序：logs/livedeck_engine.log 最近 error → Edge 是否被杀/抢 CPU → "
                              "profile ACL 与残留 msedge 进程 → 端口 %s 是否仍被占用" % bridge.get("port", "?"),
                })
        else:
            self._waiting = False
            self._bad_since = 0.0

        for room, s in (rooms or {}).items():
            try:
                rid = int(room)
            except (TypeError, ValueError):
                continue
            state = str(s.get("state") or "")
            frames = int(s.get("frames") or 0)
            last_frame = float(s.get("last_frame") or 0)
            connect_at = float(s.get("connect_at") or 0)
            danmu = int(s.get("danmu") or 0)
            if state in ("open", "auth"):
                if frames == 0 and connect_at and now - connect_at >= self.silent_after:
                    if self._due("noframe:%d" % rid, now):
                        out.append({
                            "level": "warn", "area": "ws", "room": rid,
                            "msg": "房间已认证 %.0f 秒，但一帧弹幕都没收到" % (now - connect_at),
                            "detail": "WS 通、认证通过却零帧：多半是 token 与登录态不匹配 / 被风控，"
                                      "或服务端没有为这个房间推流；先看 /api/diag 的 cmds 与 decode",
                        })
                elif frames > 0 and last_frame and now - last_frame >= self.silent_after * 2:
                    if self._due("noframe:%d" % rid, now):
                        out.append({
                            "level": "warn", "area": "ws", "room": rid,
                            "msg": "房间已 %.0f 秒没有新帧（此前收到 %d 帧）" % (now - last_frame, frames),
                            "detail": "先看同一时刻其它房间是否也停：都停→引擎/CDP 侧问题；"
                                      "只这一间→房间下播或被限流",
                        })
                elif frames > 0 and danmu == 0 and connect_at and \
                        now - connect_at >= self.no_danmu_after:
                    if self._due("nodanmu:%d" % rid, now):
                        out.append({
                            "level": "warn", "area": "ws", "room": rid,
                            "msg": "房间收到 %d 帧但 0 条 DANMU_MSG" % frames,
                            "detail": "帧能解、只是没有普通弹幕：房间冷清属正常；若所有房间都这样，"
                                      "查认证 uid/token 是否与登录态一致",
                        })

        raw_drops = (decode or {}).get("dropped") or {}
        dropped = sum(int(v or 0) for k, v in raw_drops.items() if str(k) != "total")
        if dropped > self._drops:
            self._drops = dropped
            # 丢帧通常在几秒内成片出现，60 秒内只报一次，避免刷屏
            if now - self._drop_at >= 60:
                out.append({
                    "level": "error", "area": "decode", "room": 0,
                    "msg": "累计 %d 帧解码失败被丢弃" % dropped,
                    "detail": "认证 protover 与本机解压能力不匹配：看 /api/diag 的 "
                              "decode.brotli / decode.protover；缺 brotli 时应自动退回 protover=2",
                })
                self._drop_at = now
        return out
