"""进程栈：监控台主进程托管「采集进程」「推送进程」。

为什么拆（2026-11-11）：
  · 采集要连 B 站，推送要连 QQ；任何一个卡住都不该拖死管理台页面；
  · 安装后用户还没绑账号——此时两个进程都不该跑，只是空转报错；
  · 绑定是渐进发生的：绑了 B 站才起采集，绑了 QQ 才起推送。

分工：
  采集进程 = scripts/run_bot.py --collector   （只入库，不发送，不起管理台）
  推送进程 = scripts/run_pusher.py            （只投递，不采集）

本模块只做「起 / 停 / 看 / 退避重启」，不做业务判定；
绑定口径来自 shared/readiness.py，状态落在 data/stack.json 供页面与排错读取。

「开机自启」（autostart）是绑定之外的第二道闸：进程页关掉它，监控台启动时就不
自动拉起这个角色（手动点「启动」仍然能起）。它只影响下一次监控台启动，不会在
运行中把已经跑着的进程停掉——所以是 set_autostart() 而不是立即 reconcile。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared import proxy as proxy_cfg  # noqa: E402
from shared import readiness  # noqa: E402

ROLES = ("collector", "pusher")
ROLE_LABEL = {"collector": "采集进程", "pusher": "推送进程"}

# 停止节奏：先写停止文件（优雅），过了宽限期再 terminate，再兜底 kill
STOP_GRACE_S = 8.0
KILL_GRACE_S = 4.0


def _default_spawn(argv, cwd, log_path):
    """起子进程：不占控制台窗口，输出追加到 logs/<role>.console.log。

    CREATE_NEW_PROCESS_GROUP 便于整组收掉；CREATE_NO_WINDOW 避免双击场景弹黑框。
    """
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(log_path, "ab")
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | \
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        return subprocess.Popen(argv, cwd=str(cwd), stdin=subprocess.DEVNULL,
                                stdout=fh, stderr=fh, creationflags=flags)
    finally:
        fh.close()


class StackManager:
    """按绑定状态托管采集/推送两个子进程。线程安全，重启策略带退避。"""

    def __init__(self, *, root=None, python=None, db=None, subjects=None,
                 interval: float = 2.0, state_file=None, log_dir=None,
                 spawn=None, clock=time.time, log=None,
                 backoff_base: float = 5.0, max_backoff: float = 300.0,
                 stable_after: float = 120.0, proxy_rev=None):
        self.root = Path(root or ROOT)
        self.python = str(python or sys.executable)
        self.db = str(db or (self.root / "data" / "bot.db"))
        self.subjects = str(subjects or (self.root / "config" / "subjects.json"))
        self.interval = max(0.5, float(interval))
        self.state_file = Path(state_file or (self.root / "data" / "stack.json"))
        self.log_dir = Path(log_dir or (self.root / "logs"))
        self.log = log or (lambda _m: None)
        self._spawner = spawn or _default_spawn
        self._clock = clock
        self.backoff_base = float(backoff_base)
        self.max_backoff = float(max_backoff)
        self.stable_after = float(stable_after)

        self._lock = threading.RLock()
        self._procs: dict[str, object] = {}
        self._info = {role: {"pid": 0, "started_at": 0.0, "restarts": 0, "last_exit": None,
                             "last_error": "", "next_at": 0.0, "stop_at": 0.0,
                             "stop_phase": ""} for role in ROLES}
        self._manual: dict[str, str | None] = {role: None for role in ROLES}
        self._autostart: dict[str, bool] = {role: True for role in ROLES}
        self._wanted: dict[str, bool] = {role: False for role in ROLES}
        self._bindings: dict = {}
        # 代理配置指纹：页面保存后（无论从 8765 还是 8760 存的）下一轮对账就重启子进程
        self._proxy_rev_fn = proxy_rev or proxy_cfg.revision
        try:
            self._proxy_rev = self._proxy_rev_fn()      # 基线：构造时的状态，不比出「变化」
        except Exception:                                # noqa: BLE001
            self._proxy_rev = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started_at = 0.0

    # ---------- 路径 / 命令 ----------
    def _data(self, name: str) -> Path:
        return self.root / "data" / name

    def stop_file(self, role: str) -> Path:
        return self._data("%s.stop" % role)

    def role_argv(self, role: str) -> list[str]:
        py = self.python
        if role == "collector":
            return [py, str(self.root / "scripts" / "run_bot.py"), "--collector",
                    "--db", self.db, "--subjects", self.subjects,
                    "--log-file", str(self.log_dir / "collector.log"),
                    "--stop-file", str(self.stop_file(role))]
        return [py, str(self.root / "scripts" / "run_pusher.py"),
                "--db", self.db,
                "--env", str(self.root / "BBOT.env"),
                "--ids", str(self.root / ".p0_ids.json"),
                "--log-file", str(self.log_dir / "pusher.log"),
                "--stop-file", str(self.stop_file(role))]

    def role_command_label(self, role: str) -> str:
        tail = self.role_argv(role)[1:]
        return " ".join(Path(x).as_posix() if os.path.isabs(str(x)) else str(x) for x in tail)

    # ---------- 绑定状态 ----------
    def binding_state(self) -> dict:
        with self._lock:
            self._bindings = readiness.summary(self.root)
            return json.loads(json.dumps(self._bindings, ensure_ascii=False))

    def desired(self) -> dict[str, bool]:
        b = self.binding_state()
        out = {"collector": bool(b["bili"]["bound"]), "pusher": bool(b["qq"]["bound"])}
        with self._lock:
            for role in ROLES:
                manual = self._manual.get(role)
                if manual == "on":
                    out[role] = True
                elif manual == "off":
                    out[role] = False
                elif not self._autostart.get(role, True):
                    out[role] = False
            self._wanted = out
        return out

    # ---------- 子进程生命周期 ----------
    def _alive(self, role: str) -> bool:
        proc = self._procs.get(role)
        if proc is None:
            return False
        return proc.poll() is None

    def _spawn(self, role: str):
        info = self._info[role]
        argv = self.role_argv(role)
        try:
            self.stop_file(role).unlink(missing_ok=True)
        except OSError:
            pass
        try:
            proc = self._spawner(argv, str(self.root), str(self.log_dir / ("%s.console.log" % role)))
        except Exception as e:                      # noqa: BLE001
            info["last_error"] = "%s: %s" % (type(e).__name__, e)
            info["next_at"] = self._clock() + max(self.backoff_base, 15.0)
            self.log("[栈] %s 启动失败：%s" % (ROLE_LABEL[role], info["last_error"]))
            return False
        self._procs[role] = proc
        info.update({"pid": int(getattr(proc, "pid", 0) or 0), "started_at": self._clock(),
                     "last_error": "", "next_at": 0.0, "stop_at": 0.0, "stop_phase": ""})
        self.log("[栈] %s 已启动 pid=%s" % (ROLE_LABEL[role], info["pid"]))
        return True

    def _request_stop(self, role: str) -> None:
        info = self._info[role]
        if info.get("stop_at"):
            return
        info["stop_at"] = self._clock()
        info["stop_phase"] = "graceful"
        try:
            self.stop_file(role).write_text("stop\n", encoding="ascii")
        except OSError:
            pass
        self.log("[栈] %s 收到停止请求（先写 %s）" % (ROLE_LABEL[role], self.stop_file(role).name))

    def _hard_stop(self, role: str) -> None:
        proc = self._procs.get(role)
        info = self._info[role]
        if proc is None:
            return
        now = self._clock()
        if info.get("stop_phase") == "graceful" and now - info["stop_at"] < STOP_GRACE_S:
            return
        if info.get("stop_phase") in ("graceful", "terminate"):
            if info.get("stop_phase") == "graceful":
                info["stop_phase"] = "terminate"
                try:
                    proc.terminate()
                except Exception:                   # noqa: BLE001
                    pass
                self.log("[栈] %s 宽限期到，terminate" % ROLE_LABEL[role])
                return
            if now - info["stop_at"] < STOP_GRACE_S + KILL_GRACE_S:
                return
        try:
            proc.kill()
        except Exception:                           # noqa: BLE001
            pass
        info["stop_phase"] = "killed"
        self.log("[栈] %s 已强杀" % ROLE_LABEL[role])

    def _reap(self, role: str) -> None:
        """回收已退出的子进程，并按需安排退避重启。"""
        proc = self._procs.get(role)
        if proc is None:
            return
        code = proc.poll()
        if code is None:
            return
        info = self._info[role]
        info["last_exit"] = code
        info["pid"] = 0
        info["started_at"] = 0.0
        self._procs.pop(role, None)
        healthy = bool(info.get("stop_at")) is False
        info["stop_at"] = 0.0
        info["stop_phase"] = ""
        try:
            self.stop_file(role).unlink(missing_ok=True)
        except OSError:
            pass
        if healthy and code not in (0, None):
            info["restarts"] = int(info["restarts"]) + 1
            delay = min(self.max_backoff, self.backoff_base * (2 ** min(info["restarts"] - 1, 6)))
            info["next_at"] = self._clock() + delay
            self.log("[栈] %s 异常退出（code=%s），%.0f 秒后重启（第 %d 次）"
                     % (ROLE_LABEL[role], code, delay, info["restarts"]))
        else:
            if healthy:
                info["next_at"] = self._clock() + 30.0
                self.log("[栈] %s 退出（code=%s），30 秒后按需再起（可能还没绑好账号）"
                         % (ROLE_LABEL[role], code))

    def restart(self, role: str) -> None:
        with self._lock:
            if role not in ROLES:
                raise KeyError(role)
            info = self._info[role]
            info["restarts"] = 0
            info["next_at"] = 0.0
            if self._alive(role):
                proc = self._procs.get(role)
                try:
                    proc.terminate()
                except Exception:                   # noqa: BLE001
                    pass
                self._procs.pop(role, None)
                info.update({"pid": 0, "started_at": 0.0, "stop_at": 0.0, "stop_phase": ""})
            try:
                self.stop_file(role).unlink(missing_ok=True)
            except OSError:
                pass
            self._spawn(role)

    def _proxy_reload(self) -> bool:
        """代理配置被改过：重灌本进程环境变量，并把在跑的子进程重启一次（2026-11-20）。

        为什么必须重启子进程：HTTP(S)_PROXY 是进程启动时读的，子进程只继承启动那一刻的
        环境。系统里的代理软件崩了没再起来时，采集 / 推送会一直往死端口上撞
        （日志里 `[WinError 10061] 由于目标计算机积极拒绝`），用户在页面上选的
        「不使用代理」不重启就等于没选。重启后的子进程会继承这里刚刷新过的环境变量。

        指纹来自 shared/proxy.py 的 revision()：8765 与 8760 保存的是同一个
        config/proxy.json，所以从哪边改都能被下一轮对账（默认 2 秒）捡到。
        """
        rev = self._proxy_rev_fn()
        if rev == self._proxy_rev:
            return False
        try:
            with proxy_cfg.applied_for_reload():
                self._proxy_rev = rev
        except Exception as e:                  # noqa: BLE001  坏配置不能把对账线程带崩
            self.log("[栈] 代理配置应用失败：%s: %s" % (type(e).__name__, e))
            return False
        restarted: list[str] = []
        with self._lock:
            for role in ROLES:
                if self._alive(role):
                    self.restart(role)
                    restarted.append(ROLE_LABEL[role])
        self.log("[栈] 代理设置已变更，重启 %s 让新配置生效"
                 % ("、".join(restarted) if restarted
                    else "（没有在跑的子进程，下次启动即生效）"))
        return True

    def set_autostart(self, role: str, on: bool) -> None:
        """监控台启动时要不要自动拉起这个角色；只影响下次 reconcile，不立即启停。"""
        if role not in ROLES:
            raise KeyError(role)
        with self._lock:
            self._autostart[role] = bool(on)

    def autostart(self, role: str) -> bool:
        if role not in ROLES:
            raise KeyError(role)
        with self._lock:
            return bool(self._autostart.get(role, True))

    def set_manual(self, role: str, mode: str | None) -> None:
        """manual: None=跟随绑定自动、'on'=强制起、'off'=强制停（排错用）。"""
        if role not in ROLES:
            raise KeyError(role)
        if mode not in (None, "on", "off"):
            raise ValueError(mode)
        with self._lock:
            self._manual[role] = mode

    # ---------- 一次对账 ----------
    def reconcile(self) -> list[str]:
        """读绑定状态 → 起该起的、停该停的、回收异常退出的。返回动作列表（日志用）。"""
        actions: list[str] = []
        self._proxy_reload()          # 配置变了先换环境，再判断该起谁，免得新子进程带旧代理
        wanted = self.desired()
        now = self._clock()
        with self._lock:
            for role in ROLES:
                self._reap(role)
                alive = self._alive(role)
                info = self._info[role]
                if alive and info.get("started_at") and now - info["started_at"] > self.stable_after:
                    info["restarts"] = 0
                if wanted[role] and not alive:
                    if now >= float(info.get("next_at") or 0):
                        if self._spawn(role):
                            actions.append("start:" + role)
                elif wanted[role] and alive and info.get("stop_at"):
                    info["stop_at"] = 0.0
                    info["stop_phase"] = ""
                    try:
                        self.stop_file(role).unlink(missing_ok=True)
                    except OSError:
                        pass
                elif (not wanted[role]) and alive:
                    self._request_stop(role)
                    actions.append("stop:" + role)
                if alive and info.get("stop_at"):
                    self._hard_stop(role)
        self.write_state(wanted)
        return actions

    # ---------- 状态落盘 / 对外 ----------
    def status(self) -> dict:
        with self._lock:
            now = self._clock()
            roles = {}
            for role in ROLES:
                info = self._info[role]
                alive = self._alive(role)
                roles[role] = {
                    "label": ROLE_LABEL[role],
                    "wanted": bool(self._wanted.get(role)),
                    "manual": self._manual.get(role),
                    "autostart": bool(self._autostart.get(role, True)),
                    "alive": alive,
                    "pid": int(info.get("pid") or 0) if alive else 0,
                    "uptime_s": int(now - info["started_at"]) if alive and info.get("started_at") else 0,
                    "restarts": int(info.get("restarts") or 0),
                    "last_exit": info.get("last_exit"),
                    "last_error": info.get("last_error") or "",
                    "restart_in_s": max(0, int(float(info.get("next_at") or 0) - now)),
                    "command": self.role_command_label(role),
                    "stop_file": str(self.stop_file(role)),
                }
            bindings = self._bindings or readiness.summary(self.root)
            return {
                "manager": {"pid": os.getpid(), "alive": bool(self._thread and self._thread.is_alive()),
                            "uptime_s": int(now - self._started_at) if self._started_at else 0,
                            "interval_s": self.interval, "python": self.python,
                            "state_file": str(self.state_file), "log_dir": str(self.log_dir)},
                "bindings": bindings,
                "roles": roles,
            }

    def write_state(self, wanted: dict | None = None) -> None:
        try:
            payload = self.status()
            if wanted is not None:
                payload["wanted"] = wanted
            payload["updated_at"] = self._clock()
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_file.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.state_file)
        except OSError:
            pass

    # ---------- 线程 ----------
    def run_forever(self, stop_event: threading.Event | None = None) -> None:
        stop = stop_event or self._stop
        self._started_at = self._clock()
        self.reconcile()
        while not stop.wait(self.interval):
            try:
                self.reconcile()
            except Exception as e:                  # noqa: BLE001
                self.log("[栈] 对账异常：%s: %s" % (type(e).__name__, e))
        self.shutdown()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self.run_forever, name="stack", daemon=True)
        self._thread.start()

    def request_stop(self) -> None:
        self._stop.set()

    def shutdown(self) -> None:
        """收尾：先写停止文件，宽限期内等它自己退，超时再 terminate / kill。"""
        with self._lock:
            for role in ROLES:
                if self._alive(role):
                    self._request_stop(role)
        deadline = self._clock() + STOP_GRACE_S
        while self._clock() < deadline and any(self._alive(r) for r in ROLES):
            time.sleep(0.3)
        for role in ROLES:
            if not self._alive(role):
                continue
            proc = self._procs.get(role)
            try:
                proc.terminate()
            except Exception:                       # noqa: BLE001
                pass
        deadline = self._clock() + KILL_GRACE_S
        while self._clock() < deadline and any(self._alive(r) for r in ROLES):
            time.sleep(0.2)
        for role in ROLES:
            if self._alive(role):
                try:
                    self._procs[role].kill()
                except Exception:                   # noqa: BLE001
                    pass
        with self._lock:
            self._procs.clear()
            for role in ROLES:
                self._info[role].update({"pid": 0, "started_at": 0.0, "stop_at": 0.0, "stop_phase": ""})
        self.write_state()
