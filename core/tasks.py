"""后台任务与进程的统一登记表（2026-11-08）

监控台的「进程」页把主进程以外的东西都当一行展示，分四类：

  · loop    —— 采集 / 推送进程里的常驻循环（直播采集、动态采集、投递派发、QQ 收件…）
               开关 = 暂停 / 恢复这个循环干活，立即生效，不用重启进程；
               归属见下面的 OWNER（live/dynamic 归采集进程，dispatch 等归推送进程）
  · worker  —— 日程进程组（scripts/schedule_worker.py）里的四个循环
               （周表扫描 / 预约刷新 / 日程交接 / 每日推送），由守护进程拉起
  · process —— 独立进程（采集进程、推送进程、弹幕监控台 8760、录播清理守护），
               开关 = 启动 / 停止进程；采集与推送由监控台主进程托管，绑上账号自动起。
               另有「开机自启」= 监控台启动时要不要顺手把这个进程拉起来，落
               meta['process_autostart']（JSON 对象，只存用户显式改过的 key）
  · self    —— 监控台主进程本身（run_console.py + 内嵌 Web），只读：停它等于停整个服务

开关落库在 meta['tasks_off']（JSON 数组），进程重启后照样生效；循环侧每次醒来
读一次，带 1 秒 TTL 缓存，所以点完开关最多一两秒就停。主进程循环的运行态在内存里；
日程进程组的运行态写在 meta['worker_state']，所以独立跑管理台也能看到。

「开机自启」不是 Windows 计划任务：监控台本身由 scripts/service.ps1 install 装成
计划任务（或启动文件夹快捷方式），这里只管「监控台起来之后，哪些子进程跟着起来」。
监控台自己的系统级自启状态在进程页只读显示。

注意：暂停 ≠ 停用。暂停只是让这个循环这一轮不干活，其它循环照常；
要让整条流水线停，用概览页的「全停」总开关。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

META_KEY = "tasks_off"
AUTOSTART_KEY = "process_autostart"     # 进程页「随监控台启动」：JSON 对象 {key: bool}
WORKER_STATE_KEY = "worker_state"       # 日程进程组写的心跳（pid / last_beat / tasks）
WORKER_ALIVE_WINDOW_S = 120.0           # last_beat 在这个时间内算进程组活着
_OFF_TTL = 1.0          # 秒：循环侧读开关的缓存时间


@dataclass(frozen=True)
class Task:
    """目录里的一行。desc/off_hint 会原样显示在管理台，所以写人话、写清楚代价。"""
    key: str
    name: str
    kind: str = "loop"          # loop | worker | process | self
    desc: str = ""
    off_hint: str = ""          # 关掉之后会发生什么
    toggle: bool = True         # 能不能在管理台开关
    note: str = ""              # 只读行的补充说明


CATALOG: tuple[Task, ...] = (
    Task("live", "直播采集",
         desc="采集进程里每 45 秒批量问一次直播状态，判断开播 / 下播。",
         off_hint="暂停后不再判断开播下播，状态不再刷新；恢复后不会补推中间漏掉的那几场。"),
    Task("dynamic", "动态采集",
         desc="采集进程里每 4 分钟取一次动态（关注流优先，含视频投稿、专栏、转发）。",
         off_hint="暂停后动态 / 视频 / 专栏都不再产事件；恢复后按水位对齐，不补推历史。"),
    Task("dispatch", "投递派发",
         desc="推送进程里把待发队列发到 QQ（卡片图、限速、重试都在这一步）。",
         off_hint="暂停后事件照常入库但不发送，恢复后接着发积压。"),
    Task("weekly_scan", "周表候选扫描",
         kind="worker",
         desc="日程进程组里每 10 分钟把最近的周表图文收进候选池，顺手清理已判定的图。",
         off_hint="暂停后不会再有新候选，管理台的「待确认」不会长；已有的照常判定。"),
    Task("cache_gc", "缓存清理",
         desc="推送进程里每 30 分钟兜底扫一次卡片 / 图源 / 顶部图缓存。",
         off_hint="暂停后磁盘只靠「推送完就删」回收，卡住的残留会一直留着。"),
    Task("qq_inbox", "QQ 收件",
         desc="推送进程里私聊机器人收指令（清理确认「保留 1 3」等）。",
         off_hint="暂停后私聊指令不响应，WebSocket 连接保留；推送不受影响。"),
    Task("cleanup_notify", "清理通知",
         desc="推送进程里发现待确认的清理计划时，用本 Bot 发一条通知到 QQ。",
         off_hint="暂停后不会再发通知，清理守护看到没 sent 就不会进入倒计时，不会误删。"),
    Task("reserve", "直播预约刷新",
         kind="worker",
         desc="日程进程组里每 30 分钟把动态里挂的直播预约落成单场日程。",
         off_hint="暂停后不再补预约，周表里会缺预约场次。"),
    Task("weekly_agent", "日程识别",
         kind="worker",
         desc="日程进程组按时段或手动请求，使用 Harness 文件交接或自定义 Agent API 处理候选。",
         off_hint="暂停后停止提交候选与回收结果，待确认候选保留。"),
    Task("daily_push", "每日日程推送",
         kind="worker",
         desc="日程进程组里每天按设置时间（默认 08:10，管理台日历页可改）把当天日程推到 QQ（脚本化，不依赖会话）。",
         off_hint="暂停后当天不会自动推；恢复后按下一个推送时间生效，中间的不补。"),
    Task("schedule_worker", "日程进程组",
         kind="worker", toggle=False,
         desc="独立进程 scripts/schedule_worker.py：周表扫描 / 预约刷新 / 日程交接 / 每日推送。",
         note="由守护进程拉起和重启，进程页只读；要停请用 scripts/service.ps1 stop。"),
    Task("livedeck", "弹幕监控台",
         kind="process",
         desc="多屏直播监控 + 弹幕，独立进程，127.0.0.1:8760。",
         off_hint="停止后 8760 打不开，弹幕和副屏都断；不影响 QQ 推送。",
         note="默认不随监控台启动；要开机就开，把本行「开机自启」打开。"),
    Task("cleanup_daemon", "录播清理守护",
         kind="process",
         desc="秒级盯录制目录，按规则清理（独立进程，详情见「录播清理」页）。",
         off_hint="停止后不再自动清理，磁盘只会涨。",
         note="默认不随监控台启动；清理自己的登录自启仍在「录播清理」页单独设置。"),
    Task("collector", "采集进程",
         kind="process", toggle=False,
         desc="独立进程 scripts/run_bot.py --collector：直播 + 动态取数入库，不推送、不起页面。",
         note="绑上 B 站账号后随监控台自动拉起，可在「开机自启」里关掉；"
              "停掉整个服务请用 scripts/service.ps1 stop。"),
    Task("pusher", "推送进程",
         kind="process", toggle=False,
         desc="独立进程 scripts/run_pusher.py：把待发队列发到 QQ，收指令、发清理通知。",
         note="绑上 QQ 机器人并抓到推送目标后随监控台自动拉起，可在「开机自启」里关掉；"
              "停掉整个服务请用 scripts/service.ps1 stop。"),
    Task("supervisor", "守护进程",
         kind="process", toggle=False,
         desc="看护主进程：崩了重启、卡死重启、单实例、日志轮转。",
         note="停它会连带停掉主进程（监控台就在里面），所以这里只读；"
              "要停请用 scripts/service.ps1 stop。"),
    Task("main", "监控台主进程",
         kind="self", toggle=False,
         desc="run_console.py + 内嵌 Web 服务，127.0.0.1:8765；采集 / 推送进程由它托管。",
         note="这是本页所在的进程，可以重启；停止会关掉整个服务，需要二次确认。"),
)

BY_KEY: dict[str, Task] = {t.key: t for t in CATALOG}

#: 哪些进程有「随监控台启动」开关，以及没写过 meta 时的默认值。
#: 默认值就是现在的行为：采集 / 推送绑上账号就自动起；弹幕监控台 / 清理守护本来
#: 要手动点「启动」，默认关，避免升级后凭空多出两个后台进程。
AUTOSTART_DEFAULTS: dict[str, bool] = {
    "collector": True,
    "pusher": True,
    "livedeck": False,
    "cleanup_daemon": False,
}


def autostart_default(key: str) -> bool:
    """这个进程没有显式开关时的默认自启状态。"""
    return bool(AUTOSTART_DEFAULTS.get(str(key or ""), False))


def pid_alive(pid: int) -> bool:
    """进程还活着吗。Windows 上绝不能用 os.kill(pid, 0)（会真的终止进程）。"""
    try:
        pid = int(pid)
    except Exception:                                   # noqa: BLE001
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False
        try:
            code = wintypes.DWORD()
            if not ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code)):
                return False
            return code.value == 259          # STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class Board:
    """运行态登记 + 开关读写。一个进程里只有一个（BOARD）。"""

    def __init__(self) -> None:
        self._db: Path | None = None
        self._off: set[str] = set()
        self._off_at = 0.0
        self._runtime: dict[str, dict] = {}
        self._lock = threading.RLock()

    # ---------- 绑定与开关 ----------
    def bind(self, db_path: str | Path | None) -> None:
        with self._lock:
            p = Path(db_path) if db_path else None
            if p != self._db:
                self._db = p
                self._off_at = 0.0

    @property
    def db_path(self) -> Path | None:
        return self._db

    def _load_off(self) -> set[str]:
        if self._db is None:
            return set()
        try:
            conn = sqlite3.connect(str(self._db), timeout=2)
            try:
                row = conn.execute("SELECT v FROM meta WHERE k=?", (META_KEY,)).fetchone()
            finally:
                conn.close()
        except sqlite3.Error:
            return set()
        if not row or not row[0]:
            return set()
        try:
            data = json.loads(row[0])
        except ValueError:
            return set()
        if not isinstance(data, list):
            return set()
        return {str(x) for x in data if str(x) in BY_KEY}

    def off_keys(self, force: bool = False) -> set[str]:
        """当前被关掉的 key 集合（1 秒缓存，循环里每次醒来调用都不心疼）。"""
        with self._lock:
            now = time.time()
            if force or now - self._off_at >= _OFF_TTL:
                self._off = self._load_off()
                self._off_at = now
            return set(self._off)

    def paused(self, key: str) -> bool:
        return key in self.off_keys()

    def set_off(self, key: str, off: bool) -> set[str]:
        """写库并刷新缓存；返回写完之后被关掉的 key 集合。"""
        if key not in BY_KEY:
            raise KeyError(key)
        if not BY_KEY[key].toggle:
            raise ValueError("这个条目是只读的：%s" % key)
        with self._lock:
            cur = set(self.off_keys(force=True))
            if off:
                cur.add(key)
            else:
                cur.discard(key)
            if self._db is None:
                raise RuntimeError("没有绑定数据库，无法保存开关")
            conn = sqlite3.connect(str(self._db), timeout=5)
            try:
                conn.execute(
                    "INSERT INTO meta(k, v) VALUES(?, ?) "
                    "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                    (META_KEY, json.dumps(sorted(cur), ensure_ascii=False)))
                conn.commit()
            finally:
                conn.close()
            self._off = cur
            self._off_at = time.time()
            return set(cur)

    # ---------- 运行态 ----------
    def start(self, key: str, **info) -> None:
        with self._lock:
            now = time.time()
            row = self._runtime.setdefault(key, {"counters": {}})
            row.update(info)
            row.setdefault("started_at", now)
            row["beat_at"] = now

    def beat(self, key: str, **counters) -> None:
        with self._lock:
            row = self._runtime.setdefault(key, {"counters": {}})
            row["beat_at"] = time.time()
            if counters:
                c = row.setdefault("counters", {})
                for k, v in counters.items():
                    try:
                        c[k] = c.get(k, 0) + v
                    except Exception:                   # noqa: BLE001
                        c[k] = v

    def alive(self, key: str) -> bool:
        with self._lock:
            return key in self._runtime

    def runtime(self, key: str) -> dict:
        with self._lock:
            row = self._runtime.get(key)
            return dict(row) if row else {}

    def reset(self) -> None:
        """只给自测用：清掉内存里的运行态与缓存。"""
        with self._lock:
            self._runtime.clear()
            self._off = set()
            self._off_at = 0.0
            self._db = None


BOARD = Board()


#: 每个循环归属哪个进程：采集进程只取数入库，推送进程只发送，
#: 进程页据此判断「这一行该不该转」。没登记的按监控台主进程算。
OWNER: dict[str, str] = {
    "live": "collector",
    "dynamic": "collector",
    "dispatch": "pusher",
    "cache_gc": "pusher",
    "qq_inbox": "pusher",
    "cleanup_notify": "pusher",
}


def owner_of(key: str) -> str:
    """这个循环该由谁跑：collector / pusher / main。"""
    return OWNER.get(str(key or ""), "main")


def group_label(kind: str) -> str:
    return {"loop": "进程内循环（采集 / 推送）", "worker": "日程进程组（独立进程）",
            "process": "独立进程", "self": "主进程"}.get(kind, kind)
