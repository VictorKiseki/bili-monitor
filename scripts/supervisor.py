#!/usr/bin/env python
"""守护进程：把主循环当子进程跑，崩了自动重启，卡住了自动重启。

用法：
  python scripts/supervisor.py -- python scripts/run_bot.py --channel qq --web
  python scripts/supervisor.py                     # 不带参数 = 默认子进程 run_bot.py
  python scripts/supervisor.py --status            # 看守护是否在跑（读状态文件）
  python scripts/supervisor.py --stop              # 让运行中的守护优雅退出（写停止文件）

守护做的事：
  1. 单实例：状态文件里已有活着的 pid 就拒绝启动（返回码 3）
  2. 管两个子进程：主进程 run_bot.py + 日程进程组 schedule_worker.py（日程功能关闭时不起）
  3. 任一个非 0 退出 → 指数退避重启（5s,10s,20s...最多 300s）
  4. 主进程心跳超时（默认 900 秒没有 [心跳]）或进程组心跳超时 → 判定卡死，杀掉重启
  5. 10 分钟内主进程连续崩 5 次 → 放弃并留下明确日志（返回码 5），避免无限重启刷屏
  6. 每次重启前轮转日志（supervisor.log / bot.log / bot.console.log / schedule_worker.*）
  7. Ctrl+C / --stop → 给两个子进程发 CTRL_BREAK 让它们自己收尾，再强杀兜底
"""
from __future__ import annotations

import argparse
import secrets
import ctypes
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from shared import online_update

DEFAULT_CHILD = [sys.executable, str(ROOT / "scripts" / "run_bot.py")]
DEFAULT_WORKER = [sys.executable, str(ROOT / "scripts" / "schedule_worker.py")]

PY = sys.executable


def worker_command(db: str, log_dir: str, stop_file: str) -> list[str]:
    """日程进程组的默认命令；日志轮转和停止文件由守护统一给。"""
    return [PY, str(ROOT / "scripts" / "schedule_worker.py"),
            "--db", str(db),
            "--log-file", str(Path(log_dir) / "schedule_worker.log"),
            "--stop-file", str(stop_file)]


def now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
            return bool(ok) and code.value == STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
    except Exception:
        return True


def sup_feature_enabled(db: str | Path) -> bool:
    """模块级只读判断（--status 在构造 Supervisor 之前用）。"""
    path = Path(db)
    if not path.exists():
        return False
    try:
        con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=3)
        try:
            row = con.execute(
                "SELECT v FROM meta WHERE k='weekly_feature_enabled'").fetchone()
        finally:
            con.close()
    except Exception:
        return False
    return bool(row) and str(row[0] or "").strip().lower() in ("1", "true", "yes", "on")


class Supervisor:
    def __init__(self, args: argparse.Namespace, child: list[str], passthrough: list[str],
                 worker: list[str] | None = None) -> None:
        self.args = args
        self.child_cmd = child + passthrough
        self.worker_cmd = list(worker or [])
        self.log_dir = Path(args.log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = Path(args.state_file)
        self.stop_file = Path(args.stop_file)
        self.db = Path(args.db)
        self.supervisor_log = self.log_dir / "supervisor.log"
        self.child_log = self.log_dir / "bot.console.log"
        self.worker_log = self.log_dir / "schedule_worker.console.log"
        self.worker_stop_file = Path(getattr(
            args, "worker_stop_file", ROOT / "data" / "schedule_worker.stop"))
        self.restarts = 0
        self.worker_restarts = 0
        self.crash_times: list[float] = []
        self.started_at = time.time()
        self.child: subprocess.Popen | None = None
        self.worker: subprocess.Popen | None = None
        self.stopping = False
        self._child_started_at = 0.0
        self._worker_started_at = 0.0
        self.last_state: dict = {}
        self.updater = (online_update.OnlineUpdate(ROOT)
                        if os.name == "nt" and not getattr(args, "once", False)
                        and any(Path(arg).name.lower() == "run_console.py" for arg in child)
                        else None)

    # ---------- 日志 ----------
    def log(self, msg: str) -> None:
        line = f"{now_str()} [守护] {msg}"
        print(line, flush=True)
        with open(self.supervisor_log, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def rotate(self, path: Path) -> None:
        max_bytes = int(self.args.log_max_mb * 1024 * 1024)
        if max_bytes <= 0 or not path.exists() or path.stat().st_size < max_bytes:
            return
        keep = self.args.log_keep
        for i in range(keep - 1, 0, -1):
            src, dst = path.with_name(f"{path.name}.{i}"), path.with_name(f"{path.name}.{i + 1}")
            if src.exists():
                if dst.exists():
                    dst.unlink()
                src.rename(dst)
        path.rename(path.with_name(path.name + ".1"))
        self.log(f"日志轮转 {path.name} → {path.name}.1（保留 {keep} 份）")

    # ---------- 状态 ----------
    def write_state(self, **kw) -> None:
        data = {"pid": os.getpid(), "started_at": int(self.started_at),
                "restarts": self.restarts, "worker_restarts": self.worker_restarts,
                "stall_seconds": self.args.stall_seconds, "child": self.child_cmd,
                "worker": self.worker_cmd, "updated_at": int(time.time())}
        data.update(self.last_state)
        data.update(kw)
        self.last_state = data
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.state_file)

    def already_running(self) -> int | None:
        if not self.state_file.exists():
            return None
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except Exception:
            return None
        pid = int(data.get("pid") or 0)
        if pid != os.getpid() and pid_alive(pid):
            return pid
        return None

    # ---------- 心跳 ----------
    def heartbeat_age(self) -> float | None:
        # 监控台主进程（run_console.py）单独写 data/console.beat：
        # 采集进程默认关闭时库里不会有 last_tick，只看 last_tick 会把正常的页面判成卡死。
        beat = self.state_file.parent / "console.beat"
        try:
            return max(0.0, time.time() - beat.stat().st_mtime)
        except OSError:
            pass
        if self.args.stall_seconds <= 0 or not self.db.exists():
            return None
        try:
            con = sqlite3.connect(f"file:{self.db.as_posix()}?mode=ro", uri=True, timeout=3)
            try:
                row = con.execute("SELECT v FROM meta WHERE k='last_tick'").fetchone()
            finally:
                con.close()
        except Exception:
            return None
        if not row or not row[0]:
            return None
        try:
            return time.time() - float(row[0])
        except (TypeError, ValueError):
            return None

    # ---------- 日程功能开关 ----------
    def feature_enabled(self) -> bool:
        """读 meta['weekly_feature_enabled']；没有库 / 没有键都按关闭处理。"""
        if not self.db.exists():
            return False
        try:
            con = sqlite3.connect(f"file:{self.db.as_posix()}?mode=ro", uri=True, timeout=3)
            try:
                row = con.execute(
                    "SELECT v FROM meta WHERE k='weekly_feature_enabled'").fetchone()
            finally:
                con.close()
        except Exception:
            return False
        return bool(row) and str(row[0] or "").strip().lower() in ("1", "true", "yes", "on")

    def worker_alive(self) -> bool:
        return self.worker is not None and self.worker.poll() is None

    def sync_worker_gate(self) -> None:
        """按总开关拉起/收起日程进程组（每轮和每个 wait_child 周期都会调用）。"""
        if not self.worker_cmd or self.stopping:
            return
        if self.feature_enabled():
            if not self.worker_alive():
                self.log("日程功能已开启，拉起日程进程组")
                self.spawn_worker()
            return
        if self.worker_alive():
            self.log("日程功能已关闭，停止日程进程组")
            self.stop_worker()
            self.worker = None
            self.write_state(worker_pid=0, last_event="worker_disabled")

    # ---------- 子进程 ----------
    def spawn(self, trial: str = "") -> None:
        stale = Path(self.args.child_stop_file)
        if stale.exists():          # 上次停止留下的文件，不清掉子进程会立刻退出
            stale.unlink()
        self.rotate(Path(self.args.log_dir) / "bot.log")
        self.rotate(self.child_log)
        self.rotate(self.supervisor_log)
        # CREATE_NO_WINDOW：子进程不再弹黑窗口（2026-10-28）
        flags = 0
        if os.name == "nt":
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | 0x08000000
        env = dict(os.environ)
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env["PYTHONUNBUFFERED"] = "1"
        env.pop("BILI_UPDATE_TRIAL", None)
        env.pop("BILI_ONLINE_SUPERVISED", None)
        if self.updater is not None:
            env["BILI_ONLINE_SUPERVISED"] = "1"
        if trial:
            env["BILI_UPDATE_TRIAL"] = trial
        fh = open(self.child_log, "a", encoding="utf-8")
        self.log(f"启动子进程：{' '.join(self.child_cmd)}")
        self.child = subprocess.Popen(self.child_cmd, cwd=str(ROOT), stdin=subprocess.DEVNULL,
                                      stdout=fh, stderr=subprocess.STDOUT,
                                      creationflags=flags, env=env)
        self._child_started_at = time.time()
        self.log(f"子进程 pid={self.child.pid}")
        self.write_state(child_pid=self.child.pid, child_started_at=int(time.time()),
                         last_event="child_started")

    def stop_child(self) -> int | None:
        """先写子进程停止文件让它自己收尾；超时再发 CTRL_BREAK；最后才对它动手。"""
        ch = self.child
        if ch is None or ch.poll() is not None:
            return ch.returncode if ch else None
        child_stop = Path(self.args.child_stop_file)
        child_stop.parent.mkdir(parents=True, exist_ok=True)
        child_stop.write_text(now_str(), encoding="utf-8")
        self.log(f"写入子进程停止文件 {child_stop}，等它自己收尾")
        try:
            ch.wait(timeout=self.args.grace_seconds)
            self.log(f"子进程已优雅退出，返回码 {ch.returncode}")
            return ch.returncode
        except subprocess.TimeoutExpired:
            self.log("子进程没有在宽限期内退出，发 CTRL_BREAK")
        try:
            if os.name == "nt":
                ch.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                ch.terminate()
            ch.wait(timeout=10)
            self.log(f"子进程已退出，返回码 {ch.returncode}")
            return ch.returncode
        except Exception as e:
            self.log(f"CTRL_BREAK 后仍未退出：{e}，强制终止")
            ch.terminate()
            try:
                ch.wait(timeout=5)
            except subprocess.TimeoutExpired:
                ch.kill()
                ch.wait(timeout=5)
            return ch.returncode

    def spawn_worker(self) -> None:
        """日程进程组：和主进程分开拉起、分开重启。"""
        if not self.worker_cmd:
            return
        if self.worker is not None and self.worker.poll() is None:
            return
        stale = self.worker_stop_file
        if stale.exists():
            stale.unlink()
        self.rotate(self.worker_log)
        self.rotate(Path(self.args.log_dir) / "schedule_worker.log")
        flags = 0
        if os.name == "nt":
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | 0x08000000
        env = dict(os.environ)
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env["PYTHONUNBUFFERED"] = "1"
        fh = open(self.worker_log, "a", encoding="utf-8")
        self.log(f"启动日程进程组：{' '.join(self.worker_cmd)}")
        self.worker = subprocess.Popen(self.worker_cmd, cwd=str(ROOT), stdin=subprocess.DEVNULL,
                                       stdout=fh, stderr=subprocess.STDOUT,
                                       creationflags=flags, env=env)
        fh.close()
        self._worker_started_at = time.time()
        self.log(f"日程进程组 pid={self.worker.pid}")
        self.write_state(worker_pid=self.worker.pid,
                         worker_started_at=int(time.time()), last_event="worker_started")

    def stop_worker(self) -> int | None:
        wk = self.worker
        if wk is None or wk.poll() is not None:
            return wk.returncode if wk else None
        self.worker_stop_file.parent.mkdir(parents=True, exist_ok=True)
        self.worker_stop_file.write_text(now_str(), encoding="utf-8")
        self.log(f"写入日程进程组停止文件 {self.worker_stop_file}")
        try:
            wk.wait(timeout=self.args.grace_seconds)
            self.log(f"日程进程组已优雅退出，返回码 {wk.returncode}")
            return wk.returncode
        except subprocess.TimeoutExpired:
            self.log("日程进程组没有在宽限期内退出，发 CTRL_BREAK")
        try:
            if os.name == "nt":
                wk.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                wk.terminate()
            wk.wait(timeout=10)
            self.log(f"日程进程组已退出，返回码 {wk.returncode}")
            return wk.returncode
        except Exception as e:
            self.log(f"CTRL_BREAK 后仍未退出：{e}，强制终止")
            wk.terminate()
            try:
                wk.wait(timeout=5)
            except subprocess.TimeoutExpired:
                wk.kill()
                wk.wait(timeout=5)
            return wk.returncode

    def worker_heartbeat_age(self) -> float | None:
        if self.args.stall_seconds <= 0 or not self.db.exists():
            return None
        try:
            con = sqlite3.connect(f"file:{self.db.as_posix()}?mode=ro", uri=True, timeout=3)
            try:
                row = con.execute("SELECT v FROM meta WHERE k='worker_state'").fetchone()
            finally:
                con.close()
            data = json.loads(row[0]) if row and row[0] else {}
            beat = float(data.get("last_beat") or 0)
            return time.time() - beat if beat else None
        except Exception:
            return None

    def _check_worker(self) -> None:
        """进程组退出后按短退避重启；不参与主进程的崩溃计数。"""
        wk = self.worker
        if wk is None or not self.worker_cmd:
            return
        code = wk.poll()
        if code is None:
            return
        if not self.feature_enabled():
            self.log(f"日程进程组退出，返回码 {code}；日程功能已关闭，不重启")
            self.worker = None
            self.write_state(worker_pid=0, last_event="worker_exit_disabled")
            return
        self.log(f"日程进程组退出，返回码 {code}，准备重启")
        self.worker_restarts += 1
        self.write_state(worker_pid=0, last_event="worker_exit", worker_exit_code=code)
        delay = min(60.0, self.args.backoff_base * (2 ** min(self.worker_restarts - 1, 4)))
        self.sleep_interruptible(delay)
        if not self.stop_requested():
            self.spawn_worker()
            self.write_state(last_event="worker_restarted")

    def kill_worker_for_stall(self) -> None:
        wk = self.worker
        if wk is None or wk.poll() is not None:
            return
        self.log("日程进程组心跳超时，强制终止")
        try:
            wk.kill()
            wk.wait(timeout=10)
        except Exception as e:
            self.log(f"终止日程进程组失败：{e}")

    def backoff(self) -> float:
        return min(self.args.backoff_max, self.args.backoff_base * (2 ** min(self.restarts - 1, 6)))

    def sleep_interruptible(self, seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end:
            if self.stop_requested():
                return
            time.sleep(min(1.0, max(0.0, end - time.time())))

    def stop_requested(self) -> bool:
        if self.stop_file.exists():
            self.stopping = True
            return True
        return self.stopping

    # ---------- 主循环 ----------
    def supervise(self) -> int:
        if self.updater is not None:
            if self.updater.journal.exists():
                with self.updater.runtime_lock():
                    self.updater.rollback()
                    self.updater.request.unlink(missing_ok=True)
            elif self.updater.request.exists():
                request = online_update.read_json(self.updater.request)
                status = online_update.read_json(self.updater.status_file)
                if status.get("state") == "complete" and request.get("target") == self.updater.current():
                    self.updater.request.unlink(missing_ok=True)
        while True:
            if self.stop_requested():
                self.log("收到停止请求，退出")
                self.cleanup()
                return 0
            self.spawn()
            self.sync_worker_gate()
            exit_code, reason = self.wait_child()
            if reason == "stopped":
                self.log("按请求停止")
                self.cleanup()
                return 0
            if reason == "restart":
                rf = Path(self.args.restart_file)
                try:
                    rf.unlink()
                except OSError:
                    pass
                self.log("子进程已按请求退出，立即重启（不计入崩溃次数）")
                self.write_state(last_event="restart_requested")
                time.sleep(1.0)
                continue
            if exit_code == 0:
                self.log("子进程正常退出（返回码 0），守护不再重启")
                self.cleanup()
                return 0
            self.restarts += 1
            self.crash_times.append(time.time())
            self.log(f"子进程异常退出（{reason}，返回码 {exit_code}），第 {self.restarts} 次重启")
            self.write_state(last_event="child_exit", last_exit_code=exit_code,
                             last_exit_at=int(time.time()), last_reason=reason)
            if self.restarts > self.args.max_restarts:
                self.log(f"重启次数超过上限 {self.args.max_restarts}，放弃（请查 {self.args.log_dir}/bot.console.log）")
                self.cleanup()
                return 4
            crash_window = [t for t in self.crash_times if time.time() - t <= 600]
            self.crash_times = crash_window
            if len(crash_window) >= self.args.crash_burst:
                self.log(f"10 分钟内崩了 {len(crash_window)} 次，判定不是偶发故障，放弃重启")
                self.cleanup()
                return 5
            delay = self.backoff()
            self.log(f"{int(delay)} 秒后重启（退避）")
            self.sleep_interruptible(delay)

    def wait_child(self) -> tuple[int | None, str]:
        ch = self.child
        assert ch is not None
        next_health = time.time() + self.args.health_seconds
        while True:
            code = ch.poll()
            if code is not None:
                return code, "exit"
            self._check_worker()
            self.sync_worker_gate()
            if self.stop_requested():
                self.stop_worker()
                self.stop_child()
                return ch.returncode, "stopped"
            if Path(self.args.restart_file).exists():
                self.log("收到管理台的重启请求，先让两个子进程优雅退出")
                self.stop_worker()
                self.stop_child()
                return ch.returncode, "restart"
            if self.updater is not None and self.updater.request.exists():
                try:
                    with self.updater.runtime_lock():
                        self.apply_online_update()
                except online_update.UpdateError as exc:
                    if self.updater.journal.exists():
                        raise
                    self.updater.report("failed", str(exc))
                    self.updater.request.unlink(missing_ok=True)
                ch = self.child
                if ch is None:
                    return 1, "update_restart"
                continue
            now = time.time()
            if now >= next_health:
                next_health = now + self.args.health_seconds
                self.check_worker_stall()
                if self.child_gone():
                    continue
                age = self.heartbeat_age()
                # 刚起来的子进程还没写心跳是正常的（首轮基线可能要 2~3 分钟）：
                # 只要"距本次启动"没超过阈值就不算卡死，避免把冷启动/重启误判成卡死
                if age is not None and self._child_started_at:
                    age = min(age, time.time() - self._child_started_at)
                if age is not None and age > self.args.stall_seconds:
                    self.log(f"心跳已经 {int(age)} 秒没更新（阈值 {int(self.args.stall_seconds)} 秒），判定卡死")
                    self.write_state(last_event="stall_kill", stall_age=int(age))
                    self.kill_for_stall()
                    return ch.returncode, "stall"
            time.sleep(1.0)

    def apply_online_update(self) -> None:
        update = self.updater
        assert update is not None
        stopped = False
        target = ""
        try:
            request = online_update.read_json(update.request)
            base, target = request["base"], request["target"]
            if (base != update.current() or not online_update.VERSION_RE.fullmatch(base)
                    or not online_update.VERSION_RE.fullmatch(target)
                    or not online_update.is_newer(target, base) or (ROOT / ".git").exists()):
                raise online_update.UpdateError("当前安装与更新请求不匹配")
            if not online_update.locked_path(ROOT, online_update.KEY).is_file():
                raise online_update.UpdateError("当前安装未配置信任的更新签名公钥")
            update.report("downloading", "正在下载和验证增量包", target)
            archive = update.work / "delta.zip"
            online_update.atomic_write(archive, online_update.download(base, target))
            entries = update.stage(archive, base, target)
            if self.stop_requested():
                raise online_update.UpdateError("守护进程正在停止，更新已取消")
            update.report("applying", "正在停止服务并切换版本", target)
            stopped = True
            if self.stop_worker() not in (None, 0) or self.stop_child() != 0:
                raise online_update.UpdateError("服务未能正常停止")
            update.apply(entries, target)
            token = secrets.token_hex(16)
            update.report("starting", "正在检查新版本启动状态", target)
            self.spawn(token)
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline and not self.stop_requested():
                if self.child_gone():
                    break
                ready = online_update.read_json(update.work / "ready.json")
                if ready == {"token": token, "version": target}:
                    update.finish(target)
                    return
                time.sleep(0.25)
            raise online_update.UpdateError("新版本未通过监控台启动检查")
        except Exception as exc:
            message = str(exc) if isinstance(exc, online_update.UpdateError) else type(exc).__name__
            self.log(f"[在线更新] {message}")
            if update.journal.exists():
                self.stop_child()
                update.rollback()
            else:
                update.report("failed", message, target)
            if stopped and self.child_gone() and not self.stop_requested():
                self.spawn()
        finally:
            update.request.unlink(missing_ok=True)

    def child_gone(self) -> bool:
        return self.child is None or self.child.poll() is not None

    def check_worker_stall(self) -> None:
        wk = self.worker
        if wk is None or wk.poll() is not None or self.args.stall_seconds <= 0:
            return
        age = self.worker_heartbeat_age()
        if age is not None and self._worker_started_at:
            age = min(age, time.time() - self._worker_started_at)
        if age is not None and age > self.args.stall_seconds:
            self.log(f"日程进程组心跳已经 {int(age)} 秒没更新，判定卡死")
            self.write_state(last_event="worker_stall_kill", worker_stall_age=int(age))
            self.kill_worker_for_stall()

    def kill_for_stall(self) -> None:
        ch = self.child
        if ch is None or ch.poll() is not None:
            return
        try:
            if os.name == "nt":
                ch.send_signal(signal.CTRL_BREAK_EVENT)
        except Exception:
            pass
        try:
            ch.wait(timeout=10)
        except subprocess.TimeoutExpired:
            ch.kill()
            try:
                ch.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

    def cleanup(self) -> None:
        if self.worker is not None and self.worker.poll() is None:
            self.stop_worker()
        for path in (self.stop_file, Path(self.args.child_stop_file), self.worker_stop_file):
            if path.exists():
                try:
                    path.unlink()
                except OSError:
                    pass
        self.write_state(last_event="stopped", child_pid=0, worker_pid=0)


def main() -> int:
    argv = sys.argv[1:]
    passthrough: list[str] = []
    if "--" in argv:
        idx = argv.index("--")
        passthrough = argv[idx + 1:]
        argv = argv[:idx]

    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--child", default="", help="子进程命令（默认当前 python + scripts/run_bot.py）")
    ap.add_argument("--log-dir", default=str(ROOT / "logs"))
    ap.add_argument("--db", default=str(ROOT / "data" / "bot.db"), help="用来读心跳的库（和子进程同一个）")
    ap.add_argument("--state-file", default=str(ROOT / "data" / "supervisor.json"))
    ap.add_argument("--stop-file", default=str(ROOT / "data" / "supervisor.stop"))
    ap.add_argument("--restart-file", default=str(ROOT / "data" / "restart.request"),
                    help="管理台写这个文件即可让守护重启子进程")
    ap.add_argument("--child-stop-file", default=str(ROOT / "data" / "bot.stop"),
                    help="写给 run_bot.py 的停止文件（它自己会优雅退出）")
    ap.add_argument("--worker-stop-file", default=str(ROOT / "data" / "schedule_worker.stop"),
                    help="写给 schedule_worker.py 的停止文件")
    ap.add_argument("--worker", default="", help="日程进程组命令（默认当前 python + scripts/schedule_worker.py）")
    ap.add_argument("--no-worker", action="store_true", help="不启动日程进程组（调试/旧部署兼容）")
    ap.add_argument("--max-restarts", type=int, default=50)
    ap.add_argument("--crash-burst", type=int, default=5, help="10 分钟内崩几次就放弃")
    ap.add_argument("--stall-seconds", type=float, default=900.0, help="心跳多少秒不更新算卡死（0 关闭）")
    ap.add_argument("--health-seconds", type=float, default=30.0, help="多久查一次心跳")
    ap.add_argument("--grace-seconds", type=float, default=20.0)
    ap.add_argument("--backoff-base", type=float, default=5.0)
    ap.add_argument("--backoff-max", type=float, default=300.0)
    ap.add_argument("--log-max-mb", type=float, default=10.0)
    ap.add_argument("--log-keep", type=int, default=5)
    ap.add_argument("--status", action="store_true", help="打印状态后退出")
    ap.add_argument("--stop", action="store_true", help="让运行中的守护退出后返回")
    ap.add_argument("--once", action="store_true", help="只启动子进程并等它退出，不重启（调试用）")
    args = ap.parse_args(argv)

    if args.status:
        p = Path(args.state_file)
        if not p.exists():
            print("守护：未运行（没有状态文件）")
            return 1
        data = json.loads(p.read_text(encoding="utf-8"))
        alive = pid_alive(int(data.get("pid") or 0))
        print(json.dumps(data, ensure_ascii=False, indent=2))
        print("守护进程存活：", "是" if alive else "否")
        print("日程功能：", "开启" if sup_feature_enabled(Path(args.db)) else "关闭（默认）")
        print("日程进程组存活：", "是" if pid_alive(int(data.get("worker_pid") or 0)) else "否")
        return 0 if alive else 1

    if args.stop:
        p = Path(args.state_file)
        if not p.exists():
            print("没有状态文件，守护可能未运行")
            return 1
        data = json.loads(p.read_text(encoding="utf-8"))
        pid = int(data.get("pid") or 0)
        if not pid_alive(pid):
            print("状态文件里的守护进程已经不在")
            return 1
        Path(args.stop_file).parent.mkdir(parents=True, exist_ok=True)
        Path(args.stop_file).write_text(str(int(time.time())), encoding="utf-8")
        print(f"已写入停止请求，等待守护 {pid} 退出…")
        deadline = time.time() + 90
        while time.time() < deadline:
            if not pid_alive(pid):
                print("守护已退出")
                if Path(args.stop_file).exists():
                    try:
                        Path(args.stop_file).unlink()
                    except OSError:
                        pass
                return 0
            time.sleep(1)
        print("等待超时，守护仍在运行（可以强杀进程 " + str(pid) + "）")
        return 2

    # 三种写法：
    #   -- <完整子命令>          → 直接用
    #   --child "x y" -- 额外参数 → x y + 额外参数
    #   什么都不给                → 当前 python + scripts/run_bot.py
    if args.child:
        child, extra = args.child.split(), passthrough
    elif passthrough:
        child, extra = passthrough, []
    else:
        child, extra = [PY, str(ROOT / "scripts" / "run_bot.py")], []
    if args.no_worker:
        worker: list[str] = []
    elif args.worker:
        worker = args.worker.split()
    else:
        worker = worker_command(args.db, args.log_dir, args.worker_stop_file)
    sup = Supervisor(args, child, extra, worker)

    other = sup.already_running()
    if other:
        print(f"守护已经在运行（pid {other}），本次不启动")
        return 3

    sup.log(f"守护启动 pid={os.getpid()}  子进程={sup.child_cmd}  进程组={sup.worker_cmd}")
    sup.write_state(last_event="started")
    if args.once:
        sup.spawn()
        if sup.feature_enabled():
            sup.spawn_worker()
        else:
            sup.log("日程功能未开启，--once 不启动日程进程组")
        code = sup.child.wait()
        sup.log(f"子进程退出，返回码 {code}")
        sup.cleanup()
        return int(code or 0)
    try:
        return sup.supervise()
    except KeyboardInterrupt:
        sup.log("收到 Ctrl+C")
        sup.stop_worker()
        sup.stop_child()
        sup.cleanup()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
