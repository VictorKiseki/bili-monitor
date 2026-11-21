"""Console 进程页：后台任务 / 独立进程的状态与开关（由 web/console.py 混入）。

四类对象：
  · loop     采集 / 推送进程里的循环（直播采集 / 动态采集 / 派发 / 缓存清理 / QQ 收件 /
             清理通知）——开关落 meta['tasks_off']，归属见 core.tasks.OWNER
  · worker   日程进程组（scripts/schedule_worker.py：周表扫描 / 预约刷新 / 日程交接 /
             每日推送）——运行态由 worker 写 meta['worker_state']
  · process  独立进程（采集进程 collector / 推送进程 pusher / 弹幕监控台 livedeck /
             录播清理守护 cleanup_daemon / 守护 supervisor）——采集与推送的运行态由
             监控台主进程里的栈管理器写 data/stack.json
  · self     监控台主进程本身（可重启；停止要二次确认）

三进程模型（2026-11-11）：监控台主进程（run_console.py）只负责页面与托管，采集
（只入库）与推送（只发送）各自是独立进程，绑上账号后由栈管理器自动拉起；绑不上时
它们不跑，监控台照样能开——页面服务不会被采集或推送拖住。

设计取舍：
  · 开关不写 config，写 meta（core/tasks.Board），循环侧 1 秒缓存 → 点完最快下一秒生效；
  · supervisor / schedule_worker 仍只读：停它们等于停整条链路，要停整条服务请用
    service.ps1 stop（会先写停止文件，让进程优雅退出）；
  · 主进程（self）另给两个动作：重启与停止。托管（data/supervisor.json 里那个守护
    活着）时分别写 restart.request / supervisor.stop 交给守护；独立运行时重启交给
    scripts/restart_console.ps1（它先结束本进程、再按原命令行拉起），停止则让采集
    进程写 bot.stop、单独的管理台直接关掉服务器。停止不可撤销，接口要求 confirm=true；
  · livedeck 是否在跑按本机端口 8760 判活（socket 连接 0.4s 超时），启动记 pid 到 meta；
  · 「开机自启」= 监控台启动时顺手拉起哪些子进程，落 meta['process_autostart']，
    只保存用户显式改过的 key（默认值见 core.tasks.AUTOSTART_DEFAULTS）。它只影响
    下一次启动监控台，不改变当前运行状态；监控台自身的系统级自启由 service.ps1
    负责，进程页在主进程行提供 Windows 登录自启开关。
"""
from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from core import tasks as T
from shared import readiness
from shared.proc import no_window_flags
from web.console_core import ApiError

ROOT = Path(__file__).resolve().parent.parent

LIVEDECK_PORT = 8760
LIVEDECK_SCRIPT = "run_livedeck.py"
LIVEDECK_LOG = Path(".research/run.log")
SUPERVISOR_STATE = Path("data/supervisor.json")
STACK_STATE = Path("data/stack.json")
PID_META_KEY = "livedeck_pid"
OS_AUTOSTART_TTL = 60.0                # 秒：避免轮询频繁调用 Windows 自启查询
OS_AUTOSTART_LOCK = threading.Lock()
OS_AUTOSTART_LABELS = {"task": "登录自启：计划任务", "startup": "登录自启：启动文件夹",
                       "none": "未设开机自启", "conflict": "自启项属于其他安装",
                       "unknown": "无法读取自启状态", "unsupported": "仅 Windows 支持登录自启"}
BOT_ALIVE_WINDOW_S = 180.0     # meta last_tick 在这个时间内算主循环活着
CONSOLE_PORT_DEFAULT = 8765    # 真实端口由 make_server() 写进 console._web_port
RESTART_SCRIPT = ROOT / "scripts" / "restart_console.ps1"
WORKER_ALIVE_WINDOW_S = T.WORKER_ALIVE_WINDOW_S


def _port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _pid_listening(port: int) -> int:
    """从 netstat 找监听某端口的 PID（livedeck 被 vbs/bat 拉起时我们不知道 pid）。

    netstat 的中文输出是本地 ANSI 代码页（cp936），不能按 UTF-8 解码：读线程会
    UnicodeDecodeError，stdout 直接变成 None（自测 exit=1 的来源）。这里按字节读，
    再用 Windows 的 `mbcs`（ANSI 代码页）宽松解码，非 Windows 回退 utf-8。
    """
    try:
        raw = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True,
                             timeout=5, creationflags=no_window_flags()).stdout or b""
    except Exception:  # noqa: BLE001
        return 0
    try:
        out = raw.decode("mbcs", errors="replace")
    except LookupError:  # 非 Windows 没有 mbcs 编码
        out = raw.decode("utf-8", errors="replace")
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[3] == "LISTENING" and parts[1].endswith(":" + str(port)):
            try:
                return int(parts[4])
            except ValueError:
                continue
    return 0


def _pythonw() -> str:
    exe = Path(sys.executable)
    cand = exe.with_name("pythonw.exe")
    return str(cand if cand.exists() else exe)


log = logging.getLogger("console.tasks")

# H6（2026-11-14）：netstat 只按端口给 PID。端口被别的程序占用（或 PID 被复用）时，
# 以前会直接 taskkill /F 把无关进程杀掉。杀之前必须确认「进程名是 python，且命令行
# 指向本项目」——与本文件 _kill_livedeck_edge() 用 CIM + CommandLine 白名单保持一致。
_KILL_MARKERS = ("run_livedeck", "livedeck")


def _proc_signature(pid: int) -> str:
    """返回「进程名|命令行」；查不到（进程不存在 / 没权限）返回空串。"""
    cmd = ("$p = Get-CimInstance Win32_Process -Filter \"ProcessId=%d\"; "
           "if ($p) { $p.Name + '|' + $p.CommandLine }" % int(pid))
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
                           capture_output=True, timeout=8, creationflags=no_window_flags())
    except Exception:  # noqa: BLE001
        return ""
    raw = r.stdout or b""
    try:
        return raw.decode("mbcs", errors="replace").strip()
    except LookupError:
        return raw.decode("utf-8", errors="replace").strip()


def _is_our_process(pid: int) -> bool:
    sig = _proc_signature(pid)
    if not sig:
        return False
    name, _, cmdline = sig.partition("|")
    if "python" not in name.lower():
        return False
    return any(m in (name + " " + cmdline).lower() for m in _KILL_MARKERS)


def _kill(pid: int) -> bool:
    if pid <= 0:
        return False
    if not _is_our_process(pid):
        log.warning("拒绝结束 pid=%s：进程名/命令行不像本项目的 Python 服务（端口可能被别的程序占用）", pid)
        return False
    try:
        r = subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True,
                           timeout=8, creationflags=no_window_flags())
    except Exception:  # noqa: BLE001
        return False
    if getattr(r, "returncode", 0) != 0:
        # 以前不看退出码，taskkill 失败也当成功：调用方会白等端口释放，
        # 最后报「已停止」但进程还在。现在如实返回，让上层走「拒绝/换 pid」分支。
        log.warning("taskkill 未能结束 pid=%s（退出码 %s）", pid, getattr(r, "returncode", "?"))
        return False
    return True


def _kill_livedeck_edge() -> int:
    """livedeck 会拉起 msedge（用户数据目录含 livedeck-edge-），服务停了浏览器窗口要一起收。"""
    cmd = ("Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | "
           "Where-Object { $_.CommandLine -like '*livedeck-edge-*' } | "
           "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }")
    try:
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
                       capture_output=True, timeout=15,
                       creationflags=no_window_flags())
        return 1
    except Exception:  # noqa: BLE001
        return 0


def _spawn_restart_helper(port: int, timeout_sec: int = 120) -> int:
    """独立运行（没有守护看护）时，把重启交给 scripts/restart_console.ps1。

    那个脚本会先结束本进程、再按记下的原命令行用 pythonw 重新拉起，所以它必须脱离
    当前进程：DETACHED_PROCESS 是 livedeck 那条路验证过的写法，能脱离作业对象更好。
    """
    if not RESTART_SCRIPT.exists():
        raise ApiError("找不到 scripts/restart_console.ps1，无法重启独立运行的管理台", 500)
    cmd = ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
           "-File", str(RESTART_SCRIPT), "-Port", str(int(port)),
           "-TimeoutSec", str(int(timeout_sec))]
    base = getattr(subprocess, "DETACHED_PROCESS", 0) | \
        getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    breakaway = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
    tries = ([base | breakaway] if breakaway else []) + [base]
    last = ""
    for flags in tries:
        try:
            proc = subprocess.Popen(cmd, cwd=str(ROOT), stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    creationflags=flags)
            return int(proc.pid)
        except Exception as e:  # noqa: BLE001
            last = str(e)
    raise ApiError("拉起 scripts/restart_console.ps1 失败：%s" % last, 500)


def _shutdown_web_server(srv, delay: float = 0.8) -> None:
    """独立运行的管理台：等这次的响应写回去，再关服务器，主线程自然退出。"""
    def _run() -> None:
        time.sleep(delay)
        try:
            srv.shutdown()
        except Exception:  # noqa: BLE001
            pass
        try:
            srv.server_close()
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=_run, name="console-stop", daemon=True).start()


class ConsoleTasks:
    """进程页：只读状态 + 开关（不参与采集，改了下一轮生效）。"""

    # ---------- 内部探测 ----------

    def _board_off(self) -> set:
        try:
            return set(T.BOARD.off_keys(force=True))
        except Exception:  # noqa: BLE001
            return set()

    def _feature_on(self) -> bool:
        """日程功能总开关。关闭时整个 worker 分组不下发，进程页不出现日程进程组。"""
        from core.weekly import feature as F
        try:
            with self._lock:
                return F.enabled(self._db())
        except Exception:  # noqa: BLE001
            return False

    def _bot_alive(self) -> bool:
        try:
            raw = self._meta_get("last_tick")
            return bool(raw) and (time.time() - float(raw)) < BOT_ALIVE_WINDOW_S
        except Exception:  # noqa: BLE001
            return False

    def _owner_alive(self, owner: str, stack: dict | None = None) -> bool:
        """循环归属的进程还活着吗：main → 主循环心跳；collector / pusher → 栈状态。"""
        if owner == "main":
            return self._bot_alive()
        st = stack if stack is not None else self._stack()
        info = (st.get("roles") or {}).get(owner) or {}
        return bool(info.get("alive"))

    def _managed(self, sup: dict) -> bool:
        """本进程是不是 supervisor.json 里那个守护拉起来的。

        只证明「守护活着」不够：另开一个管理台（换端口、临时库）时守护也在跑，
        但停止它写 supervisor.stop 会把真正在跑的那套服务一起停掉。supervisor 拉起
        的是 .venv 启动器（child_pid），真正的解释器是它的子进程，所以父进程也要算。
        """
        if not sup.get("alive"):
            return False
        mine = {os.getpid(), os.getppid()}
        return bool(mine & {int(sup.get("pid") or 0), int(sup.get("child_pid") or 0)})

    def _stack(self) -> dict:
        """采集 / 推送进程的运行态。

        优先问本进程里的栈管理器（内存里的就是最新的）；没有托管（比如老的
        run_bot.py --web 起法）就退回 data/stack.json；再没有就返回空，页面显示
        「未托管」。读文件时再用 pid_alive 复核一次，避免残留状态把死进程画成活的。
        """
        payload: dict = {}
        mgr = getattr(self, "stack", None)
        if mgr is not None:
            try:
                payload = mgr.status() or {}
            except Exception:  # noqa: BLE001
                payload = {}
        if not payload.get("roles"):
            try:
                data = json.loads(STACK_STATE.read_text("utf-8"))
                payload = data if isinstance(data, dict) else {}
            except Exception:  # noqa: BLE001
                payload = {}
        roles = dict(payload.get("roles") or {})
        for role, info in list(roles.items()):
            if not isinstance(info, dict):
                roles.pop(role)
                continue
            pid = int(info.get("pid") or 0)
            if info.get("alive") and pid and not T.pid_alive(pid):
                info = dict(info, alive=False, pid=0)
                roles[role] = info
        manager = payload.get("manager") or {}
        return {"available": bool(roles),
                "hosted": bool(mgr is not None),
                "updated_at": payload.get("updated_at") or 0,
                "manager": manager if isinstance(manager, dict) else {},
                "bindings": payload.get("bindings") or {},
                "roles": roles}

    def _supervisor(self) -> dict:
        pid = child = 0
        try:
            data = json.loads(SUPERVISOR_STATE.read_text("utf-8"))
            pid = int(data.get("pid") or 0)
            child = int(data.get("child_pid") or 0)
        except Exception:  # noqa: BLE001
            pass
        return {"pid": pid, "child_pid": child, "alive": T.pid_alive(pid)}

    def _worker(self) -> dict:
        """日程进程组：meta['worker_state'] 里有 pid / last_beat / 每个任务的 beat。"""
        state: dict = {}
        try:
            raw = self._meta_get(T.WORKER_STATE_KEY)
            data = json.loads(raw) if raw else {}
            state = data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001
            state = {}
        pid = int(state.get("pid") or 0)
        beat = float(state.get("last_beat") or 0)
        alive = bool(pid) and T.pid_alive(pid) and \
            (time.time() - beat) < WORKER_ALIVE_WINDOW_S
        tasks = state.get("tasks")
        return {"pid": pid, "alive": alive, "last_beat": beat,
                "tasks": tasks if isinstance(tasks, dict) else {}}

    def _livedeck(self) -> dict:
        alive = _port_open(LIVEDECK_PORT)
        pid = 0
        try:
            pid = int(self._meta_get(PID_META_KEY) or 0)
        except Exception:  # noqa: BLE001
            pid = 0
        if alive and not T.pid_alive(pid):
            pid = _pid_listening(LIVEDECK_PORT) or pid
        if not alive:
            pid = 0
        return {"pid": pid, "port": LIVEDECK_PORT, "alive": alive}

    def _cleanup_daemon(self) -> dict:
        try:
            pid = int(self.cleanup.running_pid() or 0)
        except Exception:  # noqa: BLE001
            pid = 0
        return {"pid": pid, "alive": bool(pid) and T.pid_alive(pid)}

    def _autostart_state(self) -> dict:
        """四个可自启进程的开关：默认值打底，再用 meta 里显式改过的覆盖。"""
        out = {k: bool(v) for k, v in T.AUTOSTART_DEFAULTS.items()}
        saved: dict = {}
        try:
            raw = self._meta_get(T.AUTOSTART_KEY, "{}") or "{}"
            data = json.loads(raw)
            saved = data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001
            saved = {}
        for key, val in saved.items():
            if key in out:
                out[key] = bool(val)
        return out

    def _service_autostart(self, action: str) -> str:
        if os.name != "nt":
            return "unsupported"
        try:
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                 "-File", str(ROOT / "scripts" / "service.ps1"), action,
                 "-Port", str(self._web_port or CONSOLE_PORT_DEFAULT), "-Python", sys.executable],
                cwd=ROOT, capture_output=True, timeout=10, creationflags=no_window_flags())
        except (OSError, subprocess.TimeoutExpired):
            return "unknown"
        kind = result.stdout.strip().decode("ascii", errors="replace")
        return kind if result.returncode == 0 and kind in OS_AUTOSTART_LABELS else "unknown"

    def _cache_os_autostart(self, kind: str) -> dict:
        out = {"kind": kind, "label": OS_AUTOSTART_LABELS[kind]}
        self._os_auto_cache = (time.monotonic(), out)
        return dict(out)

    def _os_autostart(self) -> dict:
        with OS_AUTOSTART_LOCK:
            cached = getattr(self, "_os_auto_cache", None)
            if cached and time.monotonic() - cached[0] < OS_AUTOSTART_TTL:
                return dict(cached[1])
            return self._cache_os_autostart(self._service_autostart("autostart-status"))

    def _set_main_autostart(self, on: bool) -> dict:
        with OS_AUTOSTART_LOCK:
            kind = self._service_autostart("autostart-on" if on else "autostart-off")
            if kind == "unknown":
                self._os_auto_cache = None
                raise ApiError("更新 Windows 登录自启失败，请检查当前账号对自启项的权限后重试", 500)
            info = self._cache_os_autostart(kind)
            if kind in ("conflict", "unsupported"):
                raise ApiError(info["label"], 409)
        return {"ok": True, "key": "main", "name": T.BY_KEY["main"].name,
                "on": on, "os_autostart": kind,
                "note": "已开启，监控台将在下次登录 Windows 时启动" if on else "已关闭 Windows 登录自启"}

    def _start_livedeck(self) -> dict:
        """拉起弹幕监控台（独立进程），返回 {pid, alive, note}。"""
        script = ROOT / LIVEDECK_SCRIPT
        if not script.exists():
            raise ApiError("找不到 %s" % LIVEDECK_SCRIPT, 500)
        LIVEDECK_LOG.parent.mkdir(parents=True, exist_ok=True)
        logf = open(LIVEDECK_LOG, "a", encoding="utf-8")
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | \
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        try:
            proc = subprocess.Popen([_pythonw(), "-u", str(script), "--no-open"],
                                    cwd=str(ROOT), stdout=logf, stderr=logf,
                                    stdin=subprocess.DEVNULL, creationflags=flags)
        except Exception as e:  # noqa: BLE001
            raise ApiError("启动弹幕监控台失败：%s" % e, 500) from None
        finally:
            logf.close()
        self._meta_set(PID_META_KEY, str(proc.pid))
        alive = False
        for _ in range(25):                # 最多等 5 秒把端口等起来
            if _port_open(LIVEDECK_PORT):
                alive = True
                break
            time.sleep(0.2)
        return {"pid": proc.pid, "alive": alive,
                "note": "" if alive else "进程已拉起，但 %d 端口还没开始监听，稍后刷新看看" % LIVEDECK_PORT}

    # ---------- 读 ----------

    def _auto_row(self, row: dict, task: T.Task, auto: dict | None = None) -> dict:
        """给每行补「开机自启」：能切的给 bool，切不了的给只读说明。"""
        if task.key in T.AUTOSTART_DEFAULTS:
            state = auto if auto is not None else self._autostart_state()
            on = bool(state.get(task.key, T.autostart_default(task.key)))
            row["autostart"] = on
            row["autostart_text"] = "随监控台启动" if on else "不自动启动"
        elif task.kind == "self" or task.key == "supervisor":
            info = self._os_autostart()
            row["autostart"] = (info["kind"] != "none"
                                if task.key == "main" and info["kind"] in ("task", "startup", "none")
                                else None)
            row["os_autostart"] = info["kind"]
            row["autostart_text"] = info["label"]
        elif task.key == "schedule_worker":
            row["autostart"] = None
            row["autostart_text"] = "随守护启动"
        elif task.kind == "worker":
            # 日程进程组里的任务（周表扫描 / 预约刷新 / 日程交接 / 每日推送）没有
            # 自己的进程，跟着 scripts/schedule_worker.py 一起启停。
            row["autostart"] = None
            row["autostart_text"] = "随日程进程组运行"
        else:
            # 剩下的都是进程内循环（直播 / 动态 / 派发 / 缓存清理 / QQ 收件 / 清理通知）：
            # 以前这里统一显示「—」，看着像是「没做自启」，其实是跟着所属进程跑。
            row["autostart"] = None
            owner = "推送进程" if task.key in ("dispatch", "cache_gc", "qq_inbox", "cleanup_notify") else "采集进程"
            row["autostart_text"] = "随" + owner + "运行"
        return row

    def _row(self, task: T.Task, off: set, stack: dict | None = None,
             auto: dict | None = None) -> dict:
        rt = {}
        try:
            rt = T.BOARD.runtime(task.key) or {}
        except Exception:  # noqa: BLE001
            rt = {}
        paused = bool(task.toggle and task.key in off)
        beat_at = float(rt.get("beat_at") or 0)
        row = {
            "key": task.key, "name": task.name, "kind": task.kind,
            "desc": task.desc, "off_hint": task.off_hint, "note": task.note,
            "toggle": bool(task.toggle), "paused": paused,
            "last_beat": beat_at,
            "age_s": int(max(0.0, time.time() - beat_at)) if beat_at else None,
            "counters": rt.get("counters") or {},
            "pid": 0, "port": 0,
        }

        if task.kind == "self":
            sup = self._supervisor()
            row.update(alive=True, status="self", status_label="运行中（本进程）",
                       pid=os.getpid(), port=int(self._web_port or CONSOLE_PORT_DEFAULT),
                       managed=self._managed(sup))
            return self._auto_row(row, task, auto)

        if task.key == "supervisor":
            sup = self._supervisor()
            row.update(alive=sup["alive"], pid=sup["pid"], child_pid=sup["child_pid"],
                       status="running" if sup["alive"] else "stopped",
                       status_label="运行中" if sup["alive"] else "未运行")
            if sup["alive"]:
                row["desc"] = "%s（子进程 %d）" % (task.desc, sup["child_pid"]) if sup["child_pid"] else task.desc
            return self._auto_row(row, task, auto)

        if task.key in ("collector", "pusher"):
            st = stack if stack is not None else self._stack()
            info = (st.get("roles") or {}).get(task.key) or {}
            alive = bool(info.get("alive"))
            label = "运行中" if alive else ("未托管" if not st.get("available") else "已停止")
            row.update(alive=alive, pid=int(info.get("pid") or 0),
                       status="running" if alive else "stopped", status_label=label,
                       wanted=bool(info.get("wanted")), manual=info.get("manual"),
                       restarts=int(info.get("restarts") or 0),
                       last_exit=info.get("last_exit"),
                       last_error=info.get("last_error") or "",
                       stack_available=bool(st.get("available")))
            if alive:
                row["desc"] = "%s（pid %d）" % (task.desc, int(info.get("pid") or 0))
            elif info.get("last_error"):
                row["desc"] = "%s（最近错误：%s）" % (task.desc, info["last_error"])
            return self._auto_row(row, task, auto)

        if task.key == "livedeck":
            ld = self._livedeck()
            row.update(alive=ld["alive"], pid=ld["pid"], port=ld["port"],
                       status="running" if ld["alive"] else "stopped",
                       status_label="运行中" if ld["alive"] else "已停止")
            return self._auto_row(row, task, auto)

        if task.key == "cleanup_daemon":
            cd = self._cleanup_daemon()
            row.update(alive=cd["alive"], pid=cd["pid"],
                       status="running" if cd["alive"] else "stopped",
                       status_label="运行中" if cd["alive"] else "已停止")
            return self._auto_row(row, task, auto)

        if task.key == "schedule_worker":
            wk = self._worker()
            row.update(alive=wk["alive"], pid=wk["pid"],
                       status="running" if wk["alive"] else "stopped",
                       status_label="运行中" if wk["alive"] else "已停止")
            if wk["alive"]:
                row["desc"] = "%s（pid %d）" % (task.desc, wk["pid"])
            return self._auto_row(row, task, auto)

        if task.kind == "worker":
            wk = self._worker()
            beat = (wk["tasks"].get(task.key) or {})
            beat_at = float(beat.get("beat_at") or 0)
            row.update(pid=wk["pid"] if wk["alive"] else 0,
                       last_beat=beat_at,
                       age_s=int(max(0.0, time.time() - beat_at)) if beat_at else None,
                       counters=beat.get("counters") or {})
            if paused:
                status, label = "paused", "已暂停"
            elif wk["alive"]:
                status, label = "running", "运行中"
            else:
                status, label = "stopped", "未运行"
            row.update(alive=wk["alive"], status=status, status_label=label)
            return self._auto_row(row, task, auto)

        # 进程内的循环：能不能转，先看归属的进程活着没，再看自己的 last_beat
        owner = T.owner_of(task.key)
        bot = self._bot_alive() if owner == "main" else self._owner_alive(owner, stack)
        if paused:
            status, label = "paused", "已暂停"
        elif bot:
            status, label = "running", "运行中"
        else:
            status, label = "stopped", "未运行"
        row.update(alive=bot, status=status, status_label=label)
        return self._auto_row(row, task, auto)

    def tasks(self) -> dict:
        off = self._board_off()
        feature_on = self._feature_on()
        stack = self._stack()
        auto = self._autostart_state()
        groups = []
        for kind in ("loop", "worker", "process", "self"):
            if kind == "worker" and not feature_on:
                continue
            rows = [self._row(t, off, stack, auto) for t in T.CATALOG if t.kind == kind]
            groups.append({"key": kind, "name": T.group_label(kind), "tasks": rows})
        roles = stack.get("roles") or {}
        return {"ok": True, "now": int(time.time()), "groups": groups,
                "off": sorted(off), "bot_alive": self._bot_alive(),
                "autostart": auto,
                "stack": {"available": bool(stack.get("available")),
                          "hosted": bool(stack.get("hosted")),
                          "updated_at": stack.get("updated_at") or 0,
                          "bindings": stack.get("bindings") or {},
                          "roles": {r: {k: v for k, v in (i or {}).items()
                                        if k in ("label", "alive", "pid", "wanted", "manual",
                                                 "autostart", "restarts", "last_error")}
                                    for r, i in roles.items()}}}

    # ---------- 写 ----------

    def set_task(self, key: str, off) -> dict:
        task = T.BY_KEY.get(str(key or ""))
        if task is None:
            raise ApiError("没有这个任务：%s" % key)
        if not task.toggle:
            if task.kind in ("worker", "process"):
                raise ApiError("%s 只读：%s"
                               % (task.name, task.note or "请用 scripts/service.ps1 控制"))
            raise ApiError("%s 只读：停了会连带停掉整个监控台，请用 service.ps1 stop" % task.name)
        want = bool(off)
        try:
            now_off = T.BOARD.set_off(task.key, want)
        except KeyError:
            raise ApiError("没有这个任务：%s" % key) from None
        except ValueError as e:
            raise ApiError(str(e)) from None
        except Exception as e:  # noqa: BLE001
            raise ApiError("保存开关失败：%s" % e, 500) from None
        return {"ok": True, "key": task.key, "name": task.name, "off": want,
                "off_keys": sorted(now_off),
                "note": ("已暂停「%s」，下一轮生效" % task.name) if want
                        else ("已恢复「%s」" % task.name)}

    def set_process_autostart(self, key: str, on) -> dict:
        """更改 Windows 登录自启或随监控台启动的设置。"""
        key = str(key or "")
        if key == "main":
            return self._set_main_autostart(on)
        if key not in T.AUTOSTART_DEFAULTS:
            raise ApiError("这个条目没有开机自启：%s" % key)
        want = bool(on)
        state = self._autostart_state()
        state[key] = want
        saved = {k: v for k, v in state.items() if v != T.autostart_default(k)}
        try:
            self._meta_set(T.AUTOSTART_KEY,
                           json.dumps(saved, ensure_ascii=False, sort_keys=True))
        except Exception as e:  # noqa: BLE001
            raise ApiError("保存开机自启失败：%s" % e, 500) from None
        task = T.BY_KEY.get(key)
        name = task.name if task is not None else key
        return {"ok": True, "key": key, "name": name, "on": want, "autostart": state,
                "note": ("已把「%s」设为随监控台启动，下次启动监控台时生效" % name) if want
                        else ("已取消「%s」的随监控台启动，下次启动监控台时生效" % name)}

    def apply_autostart(self) -> dict:
        """监控台启动时调用：把落库的自启开关应用到本次启动的子进程。"""
        auto = self._autostart_state()
        started: list[str] = []
        mgr = getattr(self, "stack", None)
        if mgr is not None:
            for role in ("collector", "pusher"):
                try:
                    mgr.set_autostart(role, bool(auto.get(role, True)))
                except Exception:  # noqa: BLE001
                    pass
        if auto.get("livedeck"):
            try:
                if not self._livedeck()["alive"]:
                    self._start_livedeck()
                    started.append("livedeck")
            except Exception:  # noqa: BLE001
                pass
        if auto.get("cleanup_daemon"):
            try:
                if not self._cleanup_daemon()["alive"]:
                    cfg = self.cleanup.config() or {}
                    if bool(cfg.get("enabled", True)):
                        self.cleanup.start()
                        started.append("cleanup_daemon")
            except Exception:  # noqa: BLE001
                pass
        return {"autostart": auto, "started": started}

    def task_process(self, key: str, action: str) -> dict:
        task = T.BY_KEY.get(str(key or ""))
        act = str(action or "").strip().lower()
        if task is None:
            raise ApiError("没有这个任务：%s" % key)
        if act not in ("start", "stop", "restart"):
            raise ApiError("action 只能是 start / stop / restart")
        if task.kind != "process":
            raise ApiError("%s 不是独立进程，不能用启停接口" % task.name)
        if task.key == "supervisor":
            raise ApiError("守护进程只能通过 scripts/service.ps1 控制（它负责拉起主进程）")

        if task.key in ("collector", "pusher"):
            mgr = getattr(self, "stack", None)
            if mgr is None:
                raise ApiError("%s 由监控台主进程托管，当前这个管理台没有托管能力："
                               "请用 install.cmd 或 scripts/service.ps1 启动（run_console.py）"
                               % task.name, 409)
            if act in ("start", "restart"):
                ok, text = readiness.bound(task.key, ROOT)
                if not ok:
                    raise ApiError("还起不来：%s" % text, 409)
            try:
                if act == "stop":
                    mgr.set_manual(task.key, "off")
                elif act == "start":
                    mgr.set_manual(task.key, "on")
                else:
                    mgr.set_manual(task.key, None)
                actions = mgr.reconcile()
                if act == "restart":
                    mgr.restart(task.key)
            except Exception as e:  # noqa: BLE001
                raise ApiError("操作%s失败：%s" % (task.name, e), 500) from None
            info = (self._stack().get("roles") or {}).get(task.key) or {}
            return {"ok": True, "key": task.key, "name": task.name, "action": act,
                    "pid": int(info.get("pid") or 0), "alive": bool(info.get("alive")),
                    "actions": actions,
                    "note": ("已停止「%s」：绑上账号也不会自动起，直到在页面里点启动"
                             % task.name) if act == "stop" else ("已启动「%s」" % task.name)}

        if task.key == "cleanup_daemon":
            mgr = self.cleanup
            try:
                if act in ("stop", "restart"):
                    mgr.stop()
                if act in ("start", "restart"):
                    mgr.start()
            except Exception as e:  # noqa: BLE001
                raise ApiError("操作录播清理守护失败：%s" % e, 500) from None
            info = self._cleanup_daemon()
            return {"ok": True, "key": task.key, "name": task.name, "action": act,
                    "pid": info["pid"], "alive": info["alive"]}

        # livedeck
        if act in ("stop", "restart"):
            info = self._livedeck()
            pid = info["pid"] or _pid_listening(LIVEDECK_PORT)
            killed = _kill(pid) if pid else False
            if pid and not killed:
                # 记的 pid 可能已过期（进程换了），按端口再确认一次；两处都对不上就拒绝动手
                alt = _pid_listening(LIVEDECK_PORT)
                if alt and alt != pid:
                    pid, killed = alt, _kill(alt)
            if pid and not killed:
                raise ApiError("端口 %d 上的进程（pid=%s）不是本项目的监控室，已拒绝结束；"
                               "请先确认那个端口被谁占用" % (LIVEDECK_PORT, pid), 409)
            _kill_livedeck_edge()
            if pid:
                for _ in range(20):        # 最多等 4 秒让端口释放
                    if not _port_open(LIVEDECK_PORT):
                        break
                    time.sleep(0.2)
            self._meta_set(PID_META_KEY, "0")
            if act == "stop":
                return {"ok": True, "key": task.key, "name": task.name, "action": act,
                        "pid": pid, "killed": bool(killed), "alive": _port_open(LIVEDECK_PORT)}

        started = self._start_livedeck()
        return {"ok": True, "key": task.key, "name": task.name, "action": act,
                "pid": started["pid"], "alive": started["alive"],
                "note": started["note"]}

    def main_process(self, action: str, confirm: bool = False) -> dict:
        """主进程（本进程）的重启 / 停止。托管与独立运行走不同的路，见模块头部说明。"""
        act = str(action or "").strip().lower()
        if act not in ("stop", "restart"):
            raise ApiError("action 只能是 stop / restart（启动请用 scripts/service.ps1 start）")
        if act == "stop" and not confirm:
            raise ApiError("停止监控台主进程会关掉整个服务，需要二次确认（confirm=true）")
        data = Path(self.db_path).parent
        base = {"ok": True, "key": "main", "name": "监控台主进程", "action": act,
                "port": int(self._web_port or CONSOLE_PORT_DEFAULT), "pid": os.getpid()}
        sup = self._supervisor()
        if self._managed(sup):
            name = "supervisor.stop" if act == "stop" else "restart.request"
            flag = data / name
            try:
                flag.parent.mkdir(parents=True, exist_ok=True)
                flag.write_text(time.strftime("%Y-%m-%d %H:%M:%S"), "utf-8")
            except Exception as e:  # noqa: BLE001
                raise ApiError("写 %s 失败：%s" % (name, e), 500) from None
            note = ("已通知守护优雅停止：主进程与日程进程组会收尾，守护也退出；"
                    "要恢复请运行 scripts/service.ps1 start" if act == "stop"
                    else "已通知守护重启主进程，几秒后自动回来（守护不动）")
            return dict(base, mode="supervisor", flag=str(flag), note=note)

        # 独立运行：没有守护看护，退和回来都得自己安排
        if act == "restart":
            helper = _spawn_restart_helper(base["port"])
            return dict(base, mode="standalone", helper_pid=helper,
                        note="独立运行：已交给 scripts/restart_console.ps1 结束并重新拉起本进程")
        if any(Path(a).name == "run_console.py" for a in sys.argv):
            srv = getattr(self, "_web_server", None)
            if srv is None:
                raise ApiError("没拿到本进程的服务器句柄，无法自停：请在终端 Ctrl+C，"
                               "或用 scripts/restart_console.ps1 -Status 看状态", 500)
            flag = data / "console.stop"
            try:
                flag.parent.mkdir(parents=True, exist_ok=True)
                flag.write_text(time.strftime("%Y-%m-%d %H:%M:%S"), "utf-8")
            except Exception as e:  # noqa: BLE001
                raise ApiError("写 console.stop 失败：%s" % e, 500) from None
            _shutdown_web_server(srv)
            return dict(base, mode="standalone-console", flag=str(flag),
                        note="已写 console.stop 并关掉页面服务：采集 / 推送进程会先停，本进程随后退出；"
                             "要恢复请运行 scripts/service.ps1 start")
        if any(Path(a).name == "run_bot.py" for a in sys.argv):
            flag = data / "bot.stop"
            try:
                flag.parent.mkdir(parents=True, exist_ok=True)
                flag.write_text(time.strftime("%Y-%m-%d %H:%M:%S"), "utf-8")
            except Exception as e:  # noqa: BLE001
                raise ApiError("写 bot.stop 失败：%s" % e, 500) from None
            return dict(base, mode="standalone-bot", flag=str(flag),
                        note="已写 bot.stop：采集循环收尾后本进程退出；独立运行没有守护再拉起它")
        srv = getattr(self, "_web_server", None)
        if srv is None:
            raise ApiError("没拿到本进程的服务器句柄，无法自停：请在终端 Ctrl+C，"
                           "或用 scripts/restart_console.ps1 -Status 看状态", 500)
        _shutdown_web_server(srv)
        return dict(base, mode="standalone-console",
                    note="已请求停止：这一页几秒后断开；要恢复请运行 scripts/restart_console.ps1 -Start")

