"""录播清理管理：给 Web 管理台提供开关、设置、状态、日志与登录自启。

边界：
  · 配置读写的唯一真源是 config/recorder_cleanup.json；
  · 守护进程不靠 PID 强杀，而是写 logs/recorder_cleanup.stop，让 --loop 自己退出；
  · 状态来自 logs/recorder_cleanup_status.json；锁文件里的 PID 只是线索，必须再用
    进程创建时间 / 命令行确认身份（recorder_cleanup.is_recorder_cleanup_process），
    确认不了就只写停止标记，绝不发送终止信号；
  · 扫描/立即执行在管理台进程里跑，仍然复用 Cleaner 的单实例锁，避免和守护同时删。
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from shared.proc import no_window_flags  # noqa: E402

import cleanup_review as review  # noqa: E402
import recorder_cleanup as rc  # noqa: E402

CONFIG_PATH = ROOT / "config" / "recorder_cleanup.json"
LOG_PATH = ROOT / "logs" / "recorder_cleanup.log"
LOCK_PATH = rc.LOCK_FILE
STOP_PATH = rc.STOP_FILE
STATUS_PATH = rc.STATUS_FILE
SCRIPT_PATH = ROOT / "scripts" / "recorder_cleanup.py"
INSTALLER_PATH = ROOT / "scripts" / "install_cleanup_autostart.ps1"


class CleanupError(Exception):
    """带 HTTP 状态码的业务错误，由 web.console.Handler 转成 JSON。"""

    def __init__(self, message: str, code: int = 400):
        super().__init__(message)
        self.message = message
        self.code = code


def _startup_link() -> Path:
    base = os.environ.get("APPDATA")
    if base:
        return Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "BililiveRecorderCleanup.lnk"
    return Path.home() / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "BililiveRecorderCleanup.lnk"


def _as_bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in ("1", "true", "yes", "on", "y")


def _pid_alive(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True
    # Windows：os.kill(pid, 0) 会终止进程，绝不能用；用 OpenProcess 查退出码
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _tail(path: Path, lines: int = 20) -> str:
    try:
        text = path.read_text("utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])


class CleanupManager:
    def __init__(
        self,
        config_path: str | Path = CONFIG_PATH,
        log_path: str | Path = LOG_PATH,
        lock_path: str | Path = LOCK_PATH,
        stop_path: str | Path = STOP_PATH,
        status_path: str | Path = STATUS_PATH,
        script_path: str | Path = SCRIPT_PATH,
        autostart_path: str | Path | None = None,
        python_executable: str | None = None,
        plan_path: str | Path = rc.REVIEW_PLAN_FILE,
        decisions_path: str | Path = rc.REVIEW_DECISIONS_FILE,
        notify_path: str | Path = rc.REVIEW_NOTIFY_FILE,
        schedule_path: str | Path = rc.REVIEW_SCHEDULE_FILE,
        trigger_path: str | Path = rc.REVIEW_TRIGGER_FILE,
    ):
        self.config_path = Path(config_path)
        self.log_path = Path(log_path)
        self.lock_path = Path(lock_path)
        self.stop_path = Path(stop_path)
        self.status_path = Path(status_path)
        self.script_path = Path(script_path)
        self.autostart_path = Path(autostart_path) if autostart_path else _startup_link()
        self.python_executable = python_executable
        self.plan_path = Path(plan_path)
        self.decisions_path = Path(decisions_path)
        self.notify_path = Path(notify_path)
        self.schedule_path = Path(schedule_path)
        self.trigger_path = Path(trigger_path)
        self._run_lock = threading.Lock()
        self._log_lock = threading.Lock()
        self._logger_instance = None

    # ---------- 配置 ----------
    def config(self) -> dict:
        cfg = rc.load_config(self.config_path)
        return self._validated(cfg)

    def _validated(self, cfg: dict) -> dict:
        def number(key: str, lo: float, hi: float) -> float:
            try:
                v = float(cfg.get(key))
            except (TypeError, ValueError):
                raise CleanupError("%s 必须是数字" % key)
            if not math.isfinite(v) or v < lo or v > hi:
                raise CleanupError("%s 必须在 %s 到 %s 之间" % (key, lo, hi))
            return v

        root = str(cfg.get("root") or "").strip()
        max_gb = number("max_gb", 0.01, 1_000_000)
        min_free_gb = number("min_free_gb", 0, 1_000_000)
        keep = number("keep_recent_minutes", 1, 10080)
        try:
            interval_days = int(cfg.get("interval_days", 1))
        except (TypeError, ValueError):
            raise CleanupError("interval_days 必须是整数")
        if interval_days < 1 or interval_days > 365:
            raise CleanupError("interval_days 必须在 1 到 365 天之间")
        try:
            run_time = review.parse_run_time(cfg.get("run_time", "03:00"))
        except ValueError as e:
            raise CleanupError(str(e))
        try:
            wait_minutes = int(cfg.get("review_wait_minutes", 60))
        except (TypeError, ValueError):
            raise CleanupError("review_wait_minutes 必须是整数")
        if wait_minutes < 1 or wait_minutes > 1440:
            raise CleanupError("review_wait_minutes 必须在 1 到 1440 分钟之间")
        cfg["root"] = root
        cfg["max_gb"] = max_gb
        cfg["min_free_gb"] = min_free_gb
        cfg["keep_recent_minutes"] = keep
        cfg["interval_days"] = interval_days
        cfg["run_time"] = run_time
        cfg["review_wait_minutes"] = wait_minutes
        cfg["protected_dir"] = str(cfg.get("protected_dir") or "").strip()
        cfg["enabled"] = bool(root) and _as_bool(cfg.get("enabled", True), True)
        cfg["dry_run"] = _as_bool(cfg.get("dry_run"), False)
        return cfg

    def public_config(self) -> dict:
        cfg = self.config()
        return {
            "root": cfg["root"],
            "max_gb": cfg["max_gb"],
            "min_free_gb": cfg["min_free_gb"],
            "keep_recent_minutes": cfg["keep_recent_minutes"],
            "interval_days": cfg["interval_days"],
            "run_time": cfg["run_time"],
            "review_wait_minutes": cfg["review_wait_minutes"],
            "protected_dir": str(review.protected_dir_for(cfg)) if cfg["root"] else "",
            "enabled": cfg["enabled"],
            "dry_run": cfg["dry_run"],
        }

    def update_config(self, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise CleanupError("请求体必须是 JSON 对象")
        allowed = {"root", "max_gb", "min_free_gb", "keep_recent_minutes", "interval_days",
                   "run_time", "review_wait_minutes", "dry_run"}
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise CleanupError("不支持修改：%s" % "、".join(unknown))
        cfg = self.config()
        if "root" in payload:
            if cfg["root"]:
                raise CleanupError("已设置录像目录。如需更换，请停止清理并修改本机清理配置。")
            root = payload["root"]
            if not isinstance(root, str) or not root.strip():
                raise CleanupError("请填写录像目录")
            path = Path(root.strip()).expanduser()
            if not path.is_absolute() or not path.is_dir():
                raise CleanupError("请填写已存在的录像目录的完整路径")
            payload = dict(payload, root=str(path.resolve()))
        cfg.update(payload)
        cfg = self._validated(cfg)
        rc.save_config(self.config_path, cfg)
        return {"ok": True, "config": self.public_config(),
                "message": "设置已保存；常驻循环下一轮会重新读取"}

    # ---------- 进程 ----------
    # M13：锁文件里的 PID 只是线索。Windows 会复用 PID，进程退出后残留的锁很可能
    # 指向一个无关进程，所以「PID 还活着」不等于「我们的守护还活着」，必须再校验身份。
    def _lock_detail(self) -> dict:
        """读锁旁 .info / 锁文件里的 "pid role [start]"，读不到返回 {}。"""
        return rc.read_lock_info(self.lock_path)

    def _lock_info(self) -> tuple[int, str]:
        # 保留旧签名：只回答 (pid, role)，不代表身份已确认，别拿它直接下判断。
        info = self._lock_detail()
        try:
            pid = int(info.get("pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        return pid, str(info.get("role") or "")

    def _lock_pid(self) -> int:
        return self._lock_info()[0]

    def _pid_identity(self, pid: int, start_time: float | None) -> bool:
        """确认 pid 真的是 recorder_cleanup 进程；拿不准一律 False。"""
        return rc.is_recorder_cleanup_process(pid, expected_start=start_time)

    def _running_state(self) -> tuple[int, str]:
        """返回 (pid, 状态)：running / unverified / other / stopped。

        unverified = 锁里的 PID 还活着，但创建时间或命令行对不上（极可能是 PID
        复用）。这种「活人」一律按没在运行对待，任何调用方都不许对它发终止信号。
        """
        info = self._lock_detail()
        try:
            pid = int(info.get("pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        role = str(info.get("role") or "")
        if not pid or not _pid_alive(pid):
            return 0, "stopped"
        # 旧版锁文件只有 PID（role 为空），按守护处理；once/manual 是控制台自己的短任务
        if role not in ("", "daemon"):
            return 0, "other"
        if not self._pid_identity(pid, info.get("start_time")):
            return 0, "unverified"
        return pid, "running"

    def running_pid(self) -> int:
        """确认过身份的守护 PID；0 = 没在运行（含「PID 活着但身份不明」）。"""
        return self._running_state()[0]

    def _lock_held(self) -> bool:
        """单实例锁是否被别的进程持有；只试探一次，抢到就立刻还回去。"""
        fd = rc.acquire_lock(self.lock_path, "probe")
        if fd is None:
            return True
        rc.release_lock(fd, rc.lock_info_path(self.lock_path))
        return False

    def _python(self) -> str:
        if self.python_executable:
            return self.python_executable
        exe = Path(sys.executable)
        if os.name == "nt":
            pythonw = exe.with_name("pythonw.exe")
            if pythonw.exists():
                return str(pythonw)
        return sys.executable

    def start(self) -> dict:
        pid, state = self._running_state()
        if pid:
            return {"ok": True, "already": True, "pid": pid, "message": "清理守护已经在运行"}
        if state == "unverified":
            # 锁里的 PID 活着但身份对不上。先看单实例锁是否真被持有：
            # 没人持锁 = 陈旧锁，可以照常启动；有人持锁则宁可中止，也不起第二个守护。
            if self._lock_held():
                raise CleanupError(
                    "锁文件里的 PID %s 仍存活，但无法确认是本清理进程（PID 可能已被复用）；"
                    "为避免启动重复实例已中止，请先确认没有清理守护在运行" % self._lock_pid(), 409)
            self._logger().warning(
                "锁文件里的 PID %s 无法确认为本清理进程（PID 可能已被复用），按陈旧锁处理",
                self._lock_pid())
        cfg = self.config()
        self._require_root(cfg)
        cfg["enabled"] = True
        cfg = self._validated(cfg)
        rc.save_config(self.config_path, cfg)
        # 启动前清掉停止标记（过期标记由 stop_requested 顺手删除并记日志），
        # 否则新守护刚起来就会读到上一条停止请求而退出。
        if rc.stop_requested(self.stop_path, self._logger()):
            self._logger().warning("启动前清除上一次的停止标记：%s", self.stop_path)
        rc.clear_stop_request(self.stop_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        runner_log = self.log_path.parent / "recorder_cleanup_runner.log"
        args = [self._python(), str(self.script_path), "--loop",
                "--config", str(self.config_path)]
        kwargs = {
            "cwd": str(ROOT),
            "stdin": subprocess.DEVNULL,
            "close_fds": True,
        }
        if os.name == "nt":
            kwargs["creationflags"] = (subprocess.DETACHED_PROCESS |
                                       subprocess.CREATE_NEW_PROCESS_GROUP)
        else:
            kwargs["start_new_session"] = True
        try:
            with open(runner_log, "ab") as out:
                kwargs["stdout"] = out
                kwargs["stderr"] = out
                proc = subprocess.Popen(args, **kwargs)
        except OSError as e:
            raise CleanupError("启动清理守护失败：%s" % e, 500)

        deadline = time.monotonic() + 6.0
        while time.monotonic() < deadline:
            pid = self.running_pid()
            if pid:
                return {"ok": True, "started": True, "pid": pid,
                        "message": "清理守护已启动（PID %d）" % pid}
            if proc.poll() is not None:
                break
            time.sleep(0.1)
        tail = _tail(runner_log)
        raise CleanupError("清理守护没能启动（退出码 %s）%s"
                           % (proc.poll(), ("；" + tail[-600:]) if tail else ""), 500)

    def stop(self) -> dict:
        """停止守护：只写带时间戳/TTL 的停止标记，绝不按 PID 发终止信号。"""
        pid, state = self._running_state()
        if state == "unverified":
            # 身份对不上（很可能 PID 已被复用）：不杀、不等待，只留停止标记并告警。
            # 真正的守护（如果还在）下一轮会自己看到标记退出；误杀无辜进程才是更大风险。
            rc.write_stop_request(self.stop_path)
            self._logger().warning(
                "无法确认为本清理进程（PID %s，可能已被复用），已改为发送停止标记：%s",
                self._lock_pid(), self.stop_path)
            return {"ok": True, "running": False, "stopped": False,
                    "message": "无法确认为本清理进程（PID %s 可能已被复用），已改为发送停止标记，未强制终止"
                               % self._lock_pid()}
        if not pid:
            # 本来就没在跑：顺手清掉遗留标记，免得下一位守护一启动就被停掉
            rc.clear_stop_request(self.stop_path)
            return {"ok": True, "running": False, "stopped": False,
                    "message": "清理守护本来就没有运行"}
        rc.write_stop_request(self.stop_path)
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if not self.running_pid():
                return {"ok": True, "running": False, "stopped": True,
                        "message": "清理守护已停止"}
            time.sleep(0.2)
        raise CleanupError("停止请求已发出，但守护还在运行（可能正在处理大文件），请稍后刷新", 409)

    def restart(self) -> dict:
        pid, state = self._running_state()
        if state == "unverified":
            # 身份不明时既不发终止信号也不重启，否则可能出现两个守护同时删文件
            rc.write_stop_request(self.stop_path)
            self._logger().warning(
                "无法确认为本清理进程（PID %s，可能已被复用），已改为发送停止标记；"
                "为避免重复实例未执行重启", self._lock_pid())
            raise CleanupError(
                "无法确认为本清理进程（PID %s 可能已被复用），已改为发送停止标记；"
                "未重启以免出现重复实例" % self._lock_pid(), 409)
        if pid:
            self.stop()
        return self.start()

    def set_enabled(self, enabled: bool) -> dict:
        enabled = bool(enabled)
        cfg = self.config()
        if enabled:
            self._require_root(cfg)
        cfg["enabled"] = enabled
        cfg = self._validated(cfg)
        rc.save_config(self.config_path, cfg)
        if enabled:
            out = self.start()
            out["enabled"] = True
            return out
        out = self.stop()
        out["enabled"] = False
        return out

    # ---------- 扫描 / 立即执行 ----------
    @staticmethod
    def _require_root(cfg: dict) -> None:
        if not cfg["root"]:
            raise CleanupError("请先在录播清理设置中填写录像目录")

    def _logger(self):
        with self._log_lock:
            if self._logger_instance is None:
                self._logger_instance = rc.build_logger(self.log_path)
            return self._logger_instance

    def close(self) -> None:
        """释放日志文件句柄；测试和热重载用，正常控制台进程不需要显式调用。"""
        with self._log_lock:
            logger = self._logger_instance
            self._logger_instance = None
            if logger is None:
                return
            for h in list(logger.handlers):
                try:
                    h.close()
                except Exception:  # noqa: BLE001
                    pass
                logger.removeHandler(h)

    def _result_dict(self, res) -> dict:
        return {
            "dry_run": res.dry_run,
            "candidates": res.candidates,
            "deleted_count": len(res.deleted),
            "deleted": res.deleted[:40],
            "bytes_freed": res.bytes_freed,
            "bytes_freed_gib": round(res.bytes_freed / rc.GIB, 3),
            "total_before": res.total_before,
            "total_after": res.total_after,
            "free_before": res.free_before,
            "free_after": res.free_after,
            "skipped_recent": res.skipped_recent,
            "errors": res.errors[:20],
        }

    def scan(self) -> dict:
        with self._run_lock:
            cfg = self.config()
            self._require_root(cfg)
            res = rc._cleaner_from_config(cfg, self._logger(), dry_run=True).run()
        return {"ok": True, "result": self._result_dict(res)}

    def run_once(self) -> dict:
        """立即检查一次：非 dry-run 时让守护创建待确认计划，绝不直接删。"""
        with self._run_lock:
            cfg = self.config()
            self._require_root(cfg)
            if cfg["dry_run"]:
                logger = self._logger()
                res = rc._cleaner_from_config(cfg, logger, dry_run=True).run()
                return {"ok": True, "dry_run": True, "result": self._result_dict(res),
                        "message": "dry-run：只报告，不创建待确认计划"}
            if not self.running_pid():
                self.start()
            active = review.load_plan(self.plan_path)
            review.write_trigger(self.trigger_path)
            if review.is_active(active):
                msg = "已有待确认计划 %s，守护会继续推进；请先回复 Bot 或等待截止" % active.get("plan_id")
            else:
                msg = "已请求守护立即检查；Bot 通知发出后等待 1 小时，期间可在 QQ 回复保留"
            return {"ok": True, "review": True, "triggered": True,
                    "plan": rc.review_summary(active), "message": msg}

    # ---------- 状态 ----------
    def status(self) -> dict:
        cfg = self.config()
        pid, pid_state = self._running_state()
        lock_pid = self._lock_pid()
        st = rc.read_status(self.status_path)
        now = int(time.time())
        last = st.get("last_run") if isinstance(st.get("last_run"), dict) else None
        started_at = int(st.get("started_at") or 0)
        plan = review.load_plan(self.plan_path)
        ns = review.load_notify(self.notify_path)
        active = review.is_active(plan)
        try:
            days = int(cfg["interval_days"])
            run_time = review.parse_run_time(cfg["run_time"])
            fp = review.schedule_fingerprint(days, run_time)
            sched = review.load_schedule(self.schedule_path)
            if sched.get("fingerprint") == fp and int(sched.get("next_run_at") or 0) > 0:
                next_at = int(sched["next_run_at"])
            else:
                next_at = review.first_run_at(now, run_time)
        except Exception:  # noqa: BLE001
            days, run_time = 1, "03:00"
            next_at = 0
        if not cfg["root"]:
            next_at = 0
        pv = rc.review_summary(plan)
        if plan:
            pv["candidates"] = plan.get("candidates") or []
            keep, keep_all = review.keep_set(plan, self.decisions_path)
            pv["keep"] = sorted(keep)
            pv["keep_all"] = keep_all
            pv["notify"] = ns if ns.get("plan_id") == plan.get("plan_id") else {}
        pv["active"] = active
        try:
            if cfg["root"]:
                du = shutil.disk_usage(rc._config_root(cfg))
                disk = {"total": du.total, "used": du.used, "free": du.free}
            else:
                disk = {"error": "请先填写录像目录"}
        except OSError as e:
            disk = {"error": "%s: %s" % (type(e).__name__, e)}
        try:
            ls = self.log_path.stat()
            log = {"path": str(self.log_path), "bytes": ls.st_size, "updated_at": int(ls.st_mtime)}
        except OSError:
            log = {"path": str(self.log_path), "bytes": 0, "updated_at": 0}
        return {
            "ok": True,
            "now": now,
            "running": bool(pid),
            "pid": pid,
            "lock_pid": lock_pid,
            "stale_lock": bool(lock_pid and not pid),
            "pid_unverified": pid_state == "unverified",
            "phase": "review_wait" if active else (st.get("phase") or ""),
            "started_at": started_at,
            "uptime_s": (now - started_at) if (pid and started_at) else 0,
            "updated_at": int(st.get("updated_at") or 0),
            "last_error": st.get("last_error") or "",
            "last_run_at": int((last or {}).get("ts") or 0),
            "next_run_at": next_at,
            "schedule": {"interval_days": days, "run_time": run_time, "next_run_at": next_at},
            "last_run": last,
            "last_review": st.get("last_review") or {},
            "review": pv,
            "config": self.public_config(),
            "disk": disk,
            "log": log,
            "autostart": self.autostart_status(),
            "script": str(self.script_path),
        }

    # ---------- 日志 ----------
    _LINE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}) "
                          r"(DEBUG|INFO|WARNING|ERROR|CRITICAL) (.*)$")
    _LEVEL = {"DEBUG": "info", "INFO": "info", "WARNING": "warn",
              "ERROR": "error", "CRITICAL": "error"}

    def _parse_log(self) -> list[dict]:
        try:
            text = self.log_path.read_text("utf-8", errors="replace")
        except OSError:
            return []
        rows: list[dict] = []
        for line in text.splitlines():
            m = self._LINE_RE.match(line)
            if m:
                date, clock, level, msg = m.groups()
                rows.append({"date": date, "time": clock,
                             "level": self._LEVEL.get(level, "info"),
                             "msg": msg})
            elif rows:
                rows[-1]["msg"] += "\n" + line
        return rows

    def logs(self, lines: int = 300, level: str = "", q: str = "") -> dict:
        if level and level not in ("error", "warn", "info"):
            raise CleanupError("level 只能是 error / warn / info")
        lines = max(1, min(int(lines or 300), 2000))
        rows = self._parse_log()
        counts = {"error": 0, "warn": 0, "info": 0}
        for r in rows:
            counts[r["level"]] = counts.get(r["level"], 0) + 1
        needle = (q or "").strip().lower()
        picked = [r for r in rows
                  if (not level or r["level"] == level)
                  and (not needle or needle in r["msg"].lower()
                       or needle in r["date"] or needle in r["time"])]
        try:
            ls = self.log_path.stat()
            size, mtime = ls.st_size, int(ls.st_mtime)
        except OSError:
            size, mtime = 0, 0
        files = []
        try:
            for p in sorted(self.log_path.parent.glob(self.log_path.name + "*")):
                if p.is_file():
                    s = p.stat()
                    files.append({"name": p.name, "bytes": s.st_size,
                                  "updated_at": int(s.st_mtime)})
        except OSError:
            pass
        return {"ok": True, "rows": picked[-lines:], "scanned": len(rows),
                "counts": counts, "path": str(self.log_path),
                "bytes": size, "updated_at": mtime, "files": files,
                "picked": [self.log_path.name] if size else []}

    # ---------- 登录自启 ----------
    def autostart_status(self) -> dict:
        try:
            exists = self.autostart_path.exists()
        except OSError:
            exists = False
        return {"configured": exists, "path": str(self.autostart_path)}

    def set_autostart(self, enabled: bool) -> dict:
        if enabled:
            if os.name != "nt":
                raise CleanupError("登录自启只支持 Windows")
            if not INSTALLER_PATH.exists():
                raise CleanupError("找不到安装脚本：%s" % INSTALLER_PATH, 500)
            try:
                r = subprocess.run(
                    ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                     "-File", str(INSTALLER_PATH)],
                    cwd=str(ROOT), capture_output=True, text=True, timeout=40,
                    creationflags=no_window_flags())
            except OSError as e:
                raise CleanupError("调用 PowerShell 失败：%s" % e, 500)
            if r.returncode != 0:
                detail = (r.stderr or r.stdout or "").strip()[-500:]
                raise CleanupError("安装登录自启失败：%s" % (detail or ("退出码 %s" % r.returncode)), 500)
            if not self.autostart_path.exists():
                raise CleanupError("安装脚本已执行，但没有找到启动快捷方式", 500)
            return {"ok": True, "configured": True,
                    "path": str(self.autostart_path),
                    "message": "已加入登录启动项（下次登录自动运行清理守护）"}
        try:
            self.autostart_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            raise CleanupError("移除登录自启失败：%s" % e, 500)
        return {"ok": True, "configured": False,
                "path": str(self.autostart_path),
                "message": "已移除登录启动项（当前守护不受影响）"}
